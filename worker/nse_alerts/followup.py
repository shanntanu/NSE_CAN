"""The 30-minute follow-up: did the news move the price, and what did volume do?"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from .config import Config, ConfigStore
from .context import ContextStore
from .db import Database, as_dt, utcnow
from .market import market_state, session_close
from .messages import build_follow_up_text, fmt_volume
from .quotes import Quote, QuoteService
from .summarizer import SummaryResult, Summarizer, template_follow_up

log = logging.getLogger(__name__)


def pct(now: float | None, before: float | None) -> float | None:
    if now is None or not before:
        return None
    return (now / before - 1) * 100


def classify(change_pct: float | None, index_pct: float | None, band: float) -> str:
    """Compare the stock's move with the market's; inside the band means no clear impact."""
    if change_pct is None:
        return "No clear impact"
    excess = change_pct - (index_pct or 0.0)
    if excess >= band:
        return "Positive"
    if excess <= -band:
        return "Negative"
    return "No clear impact"


class FollowUps:
    def __init__(self, store: ConfigStore, db: Database, summarizer: Summarizer, context: ContextStore,
                 quotes: QuoteService, pipeline):
        self.store = store
        self.db = db
        self.summarizer = summarizer
        self.context = context
        self.quotes = quotes
        self.pipeline = pipeline

    def wanted(self, ev: dict[str, Any], cfg: Config) -> bool:
        fu = cfg.section("follow_up")
        return bool(fu.get("enabled", True)) and ev["category"] in fu.get("categories", ["announcement", "corporate_action"])

    async def schedule(self, ev: dict[str, Any], quote: Quote, index_quote: Quote | None) -> None:
        cfg = self.store.cfg
        if not self.wanted(ev, cfg):
            return
        fu = cfg.section("follow_up")
        now = utcnow()
        due = now + timedelta(minutes=float(fu.get("delay_min", 30)))
        if fu.get("only_in_market_hours", True):
            if market_state(now, cfg) != "market":
                return
            close = session_close(now, cfg)
            if due > close:  # the session ends before 30 minutes: report at the close instead
                due = close
                if due - now < timedelta(minutes=float(fu.get("min_gap_min", 5))):
                    return
        await self.db.execute(
            "insert into follow_ups (event_id, symbol, due_at, status, price_at_alert, volume_at_alert, "
            "index_at_alert, created_at) values (:id, :sym, :due, 'pending', :p, :v, :i, :now) "
            "on conflict (event_id) do nothing",
            id=ev["id"], sym=ev["symbol"], due=due, p=quote.price, v=quote.volume,
            i=index_quote.price if index_quote else None, now=now,
        )

    async def run(self) -> None:
        while True:
            cfg = self.store.cfg
            try:
                await self.process_due(cfg)
            except Exception:
                log.exception("follow-up pass failed")
            await asyncio.sleep(float(cfg.get("follow_up.check_interval_sec", 15)))

    async def process_due(self, cfg: Config) -> int:
        rows = await self.db.fetch_all(
            "select * from follow_ups where status = 'pending' and due_at <= :now order by due_at limit 20",
            now=utcnow(),
        )
        done = 0
        for row in rows:
            claimed = await self.db.execute(
                "update follow_ups set status = 'processing', attempts = attempts + 1 "
                "where event_id = :id and status = 'pending'", id=row["event_id"])
            if claimed:
                try:
                    await self._process(row, cfg)
                    done += 1
                except Exception:
                    log.exception("follow-up for event %s failed", row["event_id"])
                    await self.db.execute("update follow_ups set status = 'failed', processed_at = :now "
                                          "where event_id = :id", id=row["event_id"], now=utcnow())
        return done

    async def _process(self, row: dict[str, Any], cfg: Config) -> None:
        fu = cfg.section("follow_up")
        event_id = row["event_id"]
        stock, index = await asyncio.gather(self.quotes.stock(row["symbol"], cfg), self.quotes.index(cfg))
        if stock is None:
            if int(row["attempts"]) + 1 >= int(fu.get("max_retries", 3)):
                await self.db.execute("update follow_ups set status = 'failed', processed_at = :now "
                                      "where event_id = :id", id=event_id, now=utcnow())
            else:
                await self.db.execute("update follow_ups set status = 'pending', due_at = :due where event_id = :id",
                                      id=event_id, due=utcnow() + timedelta(seconds=30))
            return

        change = pct(stock.price, row["price_at_alert"])
        index_change = pct(index.price if index else None, row["index_at_alert"])
        verdict = classify(change, index_change, float(fu.get("neutral_band_pct", 0.3)))
        volume_since = None
        if stock.volume is not None and row["volume_at_alert"] is not None:
            volume_since = max(0, stock.volume - int(row["volume_at_alert"]))

        ev = await self.db.fetch_one("select * from events where id = :id", id=event_id)
        ctx = self.context.get(row["symbol"]) or {}
        avg_volume = (ctx.get("technicals") or {}).get("avg_volume_20d")
        payload = {
            "event": {"symbol": row["symbol"], "subject": ev["subject"], "detail": ev["detail"],
                      "alert_summary": ev["summary"]},
            "stock_move_since_alert_pct": None if change is None else round(change, 2),
            "nifty50_move_since_alert_pct": None if index_change is None else round(index_change, 2),
            "relative_move_pct": None if change is None else round(change - (index_change or 0.0), 2),
            "verdict_by_rule": verdict,
            "volume_since_alert_shares": volume_since,
            "volume_session_shares": stock.volume,
            "average_daily_volume_20d": avg_volume,
        }
        timeout = float(fu.get("summary_timeout_sec", 15))
        try:
            result = await asyncio.wait_for(self.summarizer.summarise_follow_up(payload, cfg), timeout=timeout)
        except Exception as exc:
            log.warning("follow-up summary for event %s failed (%s), using the template", event_id, exc)
            result = SummaryResult(text=template_follow_up(verdict, change, index_change, volume_since),
                                   model="template", ms=0, fallback=True)

        now = utcnow()
        fev = {
            "symbol": row["symbol"], "company": ev["company"], "category": "follow_up", "source": "followup",
            "subject": f"30-minute update: {ev['subject'] or 'announcement'}", "attachment_url": None,
            "listed_at": as_dt(row["due_at"]), "detected_at": now,
        }
        note = f"Volume since the alert: {fmt_volume(volume_since)}" if volume_since is not None else ""
        rich, plain = build_follow_up_text(fev, result.text, stock, change, verdict, note)

        fev["id"] = await self.db.insert_returning_id(
            "insert into events (source, source_uid, category, symbol, company, subject, detail, listed_at, "
            "detected_at, is_nifty50, summary, summary_model, summary_ms, summary_fallback, status) "
            "values ('followup', :uid, 'follow_up', :sym, :company, :subject, :detail, :listed, :now, :uni, "
            ":summary, :model, :ms, :fb, 'processing') on conflict (source, source_uid) do nothing returning id",
            uid=f"fu:{event_id}", sym=row["symbol"], company=ev["company"], subject=fev["subject"],
            detail=verdict, listed=fev["listed_at"], now=now, uni=bool(ev["is_nifty50"]), summary=result.text,
            model=result.model, ms=result.ms, fb=result.fallback,
        )
        if fev["id"] is None:
            return

        recipients = await self.db.fetch_all(
            "select s.*, d.channel as delivered_channel from deliveries d join subscribers s on s.id = d.subscriber_id "
            "where d.event_id = :id and d.status in ('sent', 'delivered', 'read') and s.status = 'active'",
            id=event_id,
        )
        by_channel: dict[str, list[dict[str, Any]]] = {}
        for r in recipients:
            by_channel.setdefault(r["delivered_channel"], []).append(r)
        await asyncio.gather(*(
            self.pipeline._deliver_channel(self.pipeline.channels[name], subs, fev, rich, plain, None, cfg)
            for name, subs in by_channel.items()
            if name in self.pipeline.channels and self.pipeline.channels[name].enabled()
        ))
        await self.db.execute("update events set status = 'done', processed_at = :now where id = :id",
                              id=fev["id"], now=utcnow())
        await self.db.execute(
            "update follow_ups set status = 'sent', price_now = :p, volume_now = :v, index_now = :i, "
            "change_pct = :c, index_change_pct = :ic, verdict = :verdict, summary = :s, summary_model = :m, "
            "follow_up_event_id = :fid, processed_at = :now where event_id = :id",
            p=stock.price, v=stock.volume, i=index.price if index else None, c=change, ic=index_change,
            verdict=verdict, s=result.text, m=result.model, fid=fev["id"], now=utcnow(), id=event_id,
        )
