from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from ..config import Config, ConfigStore
from ..db import Database, as_dt, utcnow
from ..sources.nse import NseClient
from ..summarizer import Summarizer, SummaryAuthError, SummaryError
from ..universe import Universe
from .matching import SignalReport, build_report
from .prices import INDEX_SYMBOL, PriceStore, compute_returns
from .taxonomy import (Score, clean_filing_text, is_generic, is_routine, keyword_score, parse_score, score_text,
                       system_prompt, PROMPT_VERSION)

log = logging.getLogger(__name__)


class SignalService:
    def __init__(self, store: ConfigStore, db: Database, summarizer: Summarizer, prices: PriceStore, fetcher=None):
        self.store = store
        self.db = db
        self.summarizer = summarizer
        self.prices = prices
        self.fetcher = fetcher
        self._cache: dict[str, Score] = {}

    # ---- scoring -----------------------------------------------------------------------

    async def score(self, symbol: str, subject: str, detail: str, cfg: Config, fallback: bool = True) -> Score:
        """Topic and 1-100 sentiment from the exchange text.

        With fallback (live alerts) keyword rules answer when no model does. Without it (building history) a failure
        is raised instead, so nothing is stored as scored unless a real model scored it.
        """
        text = score_text(symbol, subject, detail)
        if text in self._cache:
            return self._cache[text]
        s = cfg.section("signals")
        model = s.get("score_model") or cfg.get("summary.model")
        messages = [{"role": "system", "content": system_prompt()}, {"role": "user", "content": text}]
        models = [m for m in (model, cfg.get("summary.fallback_model")) if m]
        result: Score | None = None
        last_error: Exception = SummaryError("no model configured")
        for m in models:
            try:
                raw = await self.summarizer._call(
                    m, messages, cfg, float(cfg.get("summary.request_timeout_sec", 20)),
                    int(s.get("scoring_max_tokens", 120)), temperature=0.0)
                result = parse_score(raw, m)
                break
            except SummaryAuthError as exc:
                if not fallback:
                    raise
                last_error = exc
                log.warning("scoring with %s failed: %s", m, exc)
            except (SummaryError, ValueError, KeyError) as exc:
                last_error = exc
                log.warning("scoring with %s failed: %s", m, exc)
        if result is None:
            if not fallback:
                raise last_error if isinstance(last_error, SummaryError) else SummaryError(str(last_error))
            result = keyword_score(subject, detail)
        self._cache[text] = result
        if len(self._cache) > 5000:
            self._cache.clear()
        return result

    async def _save_score(self, row_id: int, score: Score) -> None:
        await self.db.execute(
            "update event_signals set topic = :t, sentiment = :s, confidence = :c, rationale = :r, "
            "scored_model = :m, prompt_version = :v, scored_at = :now where id = :id",
            t=score.topic, s=score.sentiment, c=score.confidence, r=score.reason, m=score.model,
            v=PROMPT_VERSION, now=utcnow(), id=row_id,
        )

    # ---- live: called for every alert --------------------------------------------------

    async def _filing_text(self, att_task, cfg: Config) -> str:
        if att_task is None or self.fetcher is None:
            return ""
        try:
            att = await asyncio.wait_for(asyncio.shield(att_task), timeout=float(cfg.get("attachments.fetch_timeout_sec", 8)))
            return clean_filing_text(await self.fetcher.read_text(att, cfg))
        except Exception:
            return ""

    async def analyse(self, ev: dict[str, Any], cfg: Config, att_task=None) -> SignalReport | None:
        s = cfg.section("signals")
        if not s.get("enabled", True):
            return None
        listed = as_dt(ev.get("listed_at")) or utcnow()
        detail = ev.get("detail") or ""
        if is_generic(detail) and s.get("use_filing_text", True):
            detail = (detail + " " + await self._filing_text(att_task, cfg)).strip()
        score = await self.score(ev["symbol"], ev.get("subject") or "", detail, cfg)
        uid = str(ev["source_uid"])
        row_id = await self.db.insert_returning_id(
            "insert into event_signals (source, source_uid, symbol, category, subject, text_used, listed_at, "
            "topic, sentiment, confidence, rationale, scored_model, prompt_version, scored_at) "
            "values (:src, :uid, :sym, :cat, :subj, :text, :listed, :t, :s, :c, :r, :m, :v, :now) "
            "on conflict (source, source_uid) do update set topic = excluded.topic, sentiment = excluded.sentiment, "
            "confidence = excluded.confidence, rationale = excluded.rationale, scored_model = excluded.scored_model, "
            "prompt_version = excluded.prompt_version, scored_at = excluded.scored_at returning id",
            src=ev["source"], uid=uid, sym=ev["symbol"], cat=ev["category"], subj=ev.get("subject"),
            text=detail[:1500], listed=listed, t=score.topic, s=score.sentiment,
            c=score.confidence, r=score.reason, m=score.model, v=PROMPT_VERSION, now=utcnow(),
        )
        log.info("event %s scored %s / %d (confidence %.2f)", ev.get("id"), score.topic, score.sentiment, score.confidence)
        return await build_report(self.db, cfg, ev["symbol"], score.topic, score.sentiment, score.confidence,
                                  listed, uid)

    # ---- history: backfill, score, returns -----------------------------------------------

    async def backfill_announcements(self, nse: NseClient, symbols: list[str], years: int, cfg: Config,
                                     pause_sec: float = 1.0) -> int:
        cutoff = utcnow() - timedelta(days=365 * years)
        total = 0
        for symbol in symbols:
            try:
                items = await nse.announcements_for_symbol(symbol)
            except Exception as exc:
                log.warning("history for %s failed: %s", symbol, exc)
                continue
            added = 0
            for it in items:
                if not it.listed_at or it.listed_at < cutoff or is_routine(it.subject):
                    continue
                got = await self.db.insert_returning_id(
                    "insert into event_signals (source, source_uid, symbol, category, subject, text_used, "
                    "attachment_url, listed_at) values ('nse', :uid, :sym, 'announcement', :subj, :text, :url, :listed) "
                    "on conflict (source, source_uid) do nothing returning id",
                    uid=it.source_uid, sym=symbol, subj=it.subject, text=it.detail[:1500],
                    url=it.attachment_url if it.attachment_url.startswith("http") else None, listed=it.listed_at,
                )
                added += 1 if got else 0
            total += added
            log.info("%s: %d new history items (of %d fetched)", symbol, added, len(items))
            await asyncio.sleep(pause_sec)
        return total

    async def score_pending(self, cfg: Config, limit: int | None = None, use_keywords: bool = False,
                            use_pdf: bool = False) -> int:
        sql = ("select id, symbol, subject, text_used, attachment_url from event_signals "
               "where scored_at is null order by listed_at desc")
        rows = await self.db.fetch_all(sql + (f" limit {int(limit)}" if limit else ""))
        sem = asyncio.Semaphore(int(cfg.get("signals.score_concurrency", 6)))
        done = 0
        self.last_run = {"failed": 0, "stopped": ""}

        async def one(row: dict[str, Any]) -> None:
            nonlocal done
            async with sem:
                if self.last_run["stopped"]:
                    return
                text = row["text_used"] or ""
                if use_pdf and row.get("attachment_url") and is_generic(text) and self.fetcher is not None:
                    att = await self.fetcher.fetch(row["attachment_url"], cfg)
                    extra = clean_filing_text(await self.fetcher.read_text(att, cfg))
                    if extra:
                        text = (text + " " + extra).strip()[:1500]
                        await self.db.execute("update event_signals set text_used = :t where id = :id",
                                              t=text, id=row["id"])
                try:
                    score = (keyword_score(row["subject"], text) if use_keywords
                             else await self.score(row["symbol"], row["subject"] or "", text, cfg, fallback=False))
                except SummaryAuthError as exc:
                    self.last_run["stopped"] = str(exc)   # out of credits or bad key: stop, keep what is scored
                    return
                except SummaryError as exc:
                    self.last_run["failed"] += 1          # left unscored, picked up on the next run
                    log.warning("item %s left unscored: %s", row["id"], exc)
                    return
                await self._save_score(row["id"], score)
                done += 1
                if done % 200 == 0:
                    log.info("scored %d of %d", done, len(rows))

        await asyncio.gather(*(one(r) for r in rows))
        return done

    async def reset_keyword_scores(self) -> int:
        """Clear scores that were only keyword guesses (left by a run that could not reach the model)."""
        return await self.db.execute(
            "update event_signals set topic = null, sentiment = null, confidence = null, rationale = null, "
            "scored_model = null, prompt_version = null, scored_at = null where scored_model = 'keywords'")

    async def download_prices(self, symbols: list[str], years: int, attempts: int = 4) -> list[str]:
        """Fetch daily prices with pauses and retries (Yahoo refuses rapid-fire requests). Returns failed symbols."""
        failed: list[str] = []
        for symbol in [INDEX_SYMBOL, *symbols]:
            n = 0
            for attempt in range(1, attempts + 1):
                n = await self.prices.download(symbol, years)
                if n:
                    break
                await asyncio.sleep(3 * attempt)
            if n:
                log.info("prices for %s: %d days", symbol, n)
            else:
                log.warning("no prices for %s after %d tries", symbol, attempts)
                failed.append(symbol)
            await asyncio.sleep(1.0)
        return failed

    async def compute_returns(self, limit: int | None = None) -> tuple[int, int]:
        """Fill in 15 and 30 day outcomes for every scored item whose windows have run. Returns (done, no_prices)."""
        self.prices.forget()
        rows = await self.db.fetch_all(
            "select id, symbol, listed_at from event_signals where topic is not null and returns_status = 'pending' "
            "order by symbol, listed_at" + (f" limit {int(limit)}" if limit else ""))
        index = await self.prices.load(INDEX_SYMBOL)
        done = missing = 0
        for row in rows:
            stock = await self.prices.load(row["symbol"])
            r = compute_returns(as_dt(row["listed_at"]), stock, index)
            if r is None:
                missing += 1  # no prices yet (or the news predates them): stays pending and is retried next run
                continue
            status = "done" if r.complete else "pending"
            await self.db.execute(
                "update event_signals set entry_date = :d, entry_basis = :b, entry_price = :p, ret_15d_pct = :r15, "
                "ret_30d_pct = :r30, nifty_15d_pct = :n15, nifty_30d_pct = :n30, returns_status = :s where id = :id",
                d=r.entry_date, b=r.entry_basis, p=r.entry_price, r15=r.ret_15d_pct, r30=r.ret_30d_pct,
                n15=r.nifty_15d_pct, n30=r.nifty_30d_pct, s=status, id=row["id"])
            done += 1 if r.complete else 0
        return done, missing

    async def maintain(self, universe: Universe) -> None:
        """Daily: refresh prices, then settle the outcomes of items that have aged past 15 and 30 days."""
        cfg = self.store.cfg
        years = int(cfg.get("signals.lookback_years", 5))
        await self.download_prices(sorted(universe.symbols), 1 if years > 1 else years)
        done, _ = await self.compute_returns()
        log.info("signals maintenance: %d outcomes completed", done)

    async def run(self, universe: Universe) -> None:
        while True:
            cfg = self.store.cfg
            if cfg.get("signals.enabled", True):
                try:
                    await self.maintain(universe)
                except Exception:
                    log.exception("signals maintenance failed")
            await asyncio.sleep(float(cfg.get("signals.maintenance_interval_sec", 21600)))
