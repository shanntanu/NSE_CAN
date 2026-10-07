from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

from .config import Config, ConfigStore
from .db import Database, StatusWriter, utcnow
from .market import market_state, poll_interval
from .pipeline import Pipeline
from .sources.base import Item, SourceError
from .sources.bse import BseClient
from .sources.nse import NseClient
from .universe import Universe

log = logging.getLogger(__name__)
SEEN_LIMIT = 20000

INSERT_EVENT = (
    "insert into events (source, source_uid, category, symbol, company, subject, detail, attachment_url, "
    "attachment_size, listed_at, company_time, detected_at, is_nifty50, raw, status) "
    "values (:source, :uid, :category, :symbol, :company, :subject, :detail, :url, :size, :listed, :ctime, "
    ":detected, :uni, :raw, :status) on conflict (source, source_uid) do nothing returning id"
)


class Poller:
    """Polls the exchange feeds, stores every new item and hands alertable ones to the pipeline."""

    def __init__(self, store: ConfigStore, db: Database, universe: Universe, pipeline: Pipeline,
                 nse: NseClient, bse: BseClient | None = None, notify=None, local: Database | None = None,
                 status: StatusWriter | None = None):
        self.store = store
        self.db = db
        self.local = local          # heavy data (raw source JSON) goes here, not to the small shared database
        self.status = status
        self.universe = universe
        self.pipeline = pipeline
        self.nse = nse
        self.bse = bse
        self.notify = notify
        self.seen: dict[str, None] = {}
        self.ann_primed = False
        self.ca_ready: bool | None = None
        self.poll_count = 0
        self.failures = {"nse": 0, "bse": 0}
        self.outage_reported = {"nse": False, "bse": False}

    # ---- decisions ---------------------------------------------------------------

    def _status_for(self, item: Item, detected: datetime, cfg: Config, in_universe: bool) -> str:
        a = cfg.section("alerts")
        if not in_universe or item.category not in a.get("categories", ["announcement", "corporate_action"]):
            return "skipped"
        subject = (item.subject or "").lower()
        include = [s.lower() for s in a.get("include_subjects", [])]
        ignore = [s.lower() for s in a.get("ignore_subjects", [])]
        if include and not any(s in subject for s in include):
            return "skipped"
        if any(s in subject for s in ignore):
            return "skipped"
        if item.category == "announcement" and item.listed_at:
            if (detected - item.listed_at).total_seconds() > float(a.get("max_event_age_sec", 600)):
                return "skipped"
        return "new"

    def _remember(self, key: str) -> bool:
        if key in self.seen:
            return False
        self.seen[key] = None
        if len(self.seen) > SEEN_LIMIT:
            for old in list(self.seen)[: SEEN_LIMIT // 4]:
                del self.seen[old]
        return True

    async def _store_event(self, item: Item, detected: datetime, in_universe: bool, status: str) -> int | None:
        try:
            return await self._insert_event(item, detected, in_universe, status)
        except Exception:
            self.seen.pop(f"{item.source}:{item.source_uid}", None)  # let the next poll try again
            raise

    async def _insert_event(self, item: Item, detected: datetime, in_universe: bool, status: str) -> int | None:
        event_id = await self._insert_event_row(item, detected, in_universe, status)
        if event_id is not None and self.local is not None:
            await self.local.execute(
                "insert into event_raw (event_id, raw) values (:id, :raw) on conflict (event_id) do nothing",
                id=event_id, raw=json.dumps(item.raw, default=str),
            )
        return event_id

    async def _insert_event_row(self, item: Item, detected: datetime, in_universe: bool, status: str) -> int | None:
        return await self.db.insert_returning_id(
            INSERT_EVENT,
            source=item.source, uid=item.source_uid, category=item.category, symbol=item.symbol,
            company=item.company, subject=item.subject, detail=item.detail, url=item.attachment_url or None,
            size=item.attachment_size or None, listed=item.listed_at or detected, ctime=item.company_time,
            detected=detected, uni=in_universe, raw=None, status=status,
        )

    # ---- ingest ------------------------------------------------------------------

    async def ingest_announcements(self, items: list[Item], detected: datetime, cfg: Config) -> list[int]:
        new_ids: list[int] = []
        for item in reversed(items):  # oldest first
            if not self._remember(f"{item.source}:{item.source_uid}"):
                continue
            in_universe = item.symbol in self.universe
            if not in_universe and not cfg.get("universe.store_non_universe", False):
                continue
            status = self._status_for(item, detected, cfg, in_universe)
            if item.source == "bse" and status == "new" and await self._duplicate_of_nse(item, cfg):
                status = "skipped"
            event_id = await self._store_event(item, detected, in_universe, status)
            if event_id is not None and status == "new":
                new_ids.append(event_id)
        return new_ids

    async def _duplicate_of_nse(self, item: Item, cfg: Config) -> bool:
        if not item.listed_at:
            return False
        window = timedelta(minutes=float(cfg.get("polling.bse.duplicate_window_min", 10)))
        rows = await self.db.fetch_all(
            "select subject from events where source = 'nse' and symbol = :s and listed_at between :a and :b",
            s=item.symbol, a=item.listed_at - window, b=item.listed_at + window,
        )
        return any(BseClient.looks_duplicate(item.subject or item.detail, r["subject"] or "") for r in rows)

    async def ingest_actions(self, items: list[Item], detected: datetime, cfg: Config) -> list[int]:
        if self.ca_ready is None:
            count = await self.db.fetch_one("select count(*) as n from corporate_actions")
            self.ca_ready = bool(count and int(count["n"]) > 0)  # an empty table means first run: store, do not alert
        new_ids: list[int] = []
        for item in reversed(items):
            if not self._remember(f"{item.source}:{item.source_uid}"):
                continue
            in_universe = item.symbol in self.universe
            if not in_universe and not cfg.get("universe.store_non_universe", False):
                continue
            status = self._status_for(item, detected, cfg, in_universe) if self.ca_ready else "skipped"
            event_id = await self._store_event(item, detected, in_universe, status)
            e = item.extra
            await self.db.execute(
                "insert into corporate_actions (symbol, company, isin, series, subject, ex_date, record_date, "
                "bc_start, bc_end, nd_start, nd_end, face_value, is_nifty50, event_id, raw, detected_at) "
                "values (:symbol, :company, :isin, :series, :subject, :ex, :rec, :bcs, :bce, :nds, :nde, :fv, "
                ":uni, :eid, :raw, :detected) "
                "on conflict (symbol, subject, ex_date, record_date) do nothing",
                symbol=item.symbol, company=item.company, isin=e.get("isin"), series=e.get("series"),
                subject=item.subject, ex=e.get("ex_date") or "-", rec=e.get("record_date") or "-",
                bcs=e.get("bc_start"), bce=e.get("bc_end"), nds=e.get("nd_start"), nde=e.get("nd_end"),
                fv=e.get("face_value"), uni=in_universe, eid=event_id, raw=None,
                detected=detected,
            )
            if event_id is not None and status == "new":
                new_ids.append(event_id)
        self.ca_ready = True
        return new_ids

    def _dispatch(self, event_ids: list[int]) -> None:
        for event_id in event_ids:
            asyncio.create_task(self.pipeline.handle(event_id))

    # ---- NSE ---------------------------------------------------------------------

    async def poll_nse_once(self, cfg: Config) -> None:
        sec = cfg.section("polling.nse")
        timeout = float(sec.get("timeout_sec", 8))
        self.poll_count += 1
        started = time.monotonic()

        if sec.get("announcements", True):
            items = await self.nse.announcements(timeout)
            detected = utcnow()
            unseen = sum(1 for i in items if f"{i.source}:{i.source_uid}" not in self.seen)
            new_ids = await self.ingest_announcements(items, detected, cfg)
            self._dispatch(new_ids)
            overflow = self.ann_primed and items and unseen == len(items)
            if overflow and sec.get("gap_fill", True):
                log.warning("all %d latest items were new, filling the gap from today's full list", len(items))
                day_items = await self.nse.announcements_for_day(detected, timeout + 4)
                self._dispatch(await self.ingest_announcements(day_items, utcnow(), cfg))
            self.ann_primed = True

        every = max(1, int(sec.get("corporate_actions_every_n_polls", 6)))
        if sec.get("corporate_actions", True) and (self.poll_count - 1) % every == 0:
            actions = await self.nse.corporate_actions(timeout)
            self._dispatch(await self.ingest_actions(actions, utcnow(), cfg))

        await self._record_poll("nse", True, (time.monotonic() - started) * 1000)

    async def _record_poll(self, source: str, ok: bool, latency_ms: float, error: str = "") -> None:
        cfg = self.store.cfg
        before = self.failures[source]
        self.failures[source] = 0 if ok else before + 1
        value = json.dumps({"ok": ok, "latency_ms": int(latency_ms), "failures": self.failures[source],
                            "error": error, "at": utcnow().isoformat()})
        if self.status is not None:
            # every poll goes to the local database; the shared one only hears about it every so often,
            # or at once when the feed starts or stops failing
            await self.status.set(f"{source}_poll", value, force_shared=(ok != (before == 0)))
        else:
            await self.db.set_status(f"{source}_poll", value)
        threshold = int(cfg.get("polling.nse.failure_alert_after", 5))
        if not ok and self.failures[source] >= threshold and not self.outage_reported[source]:
            self.outage_reported[source] = True
            log.error("%s feed has failed %d polls in a row: %s", source.upper(), self.failures[source], error)
            if self.notify:
                await self.notify(f"{source.upper()} feed is failing ({self.failures[source]} polls in a row): {error}")
        elif ok and self.outage_reported[source]:
            self.outage_reported[source] = False
            log.info("%s feed recovered", source.upper())
            if self.notify:
                await self.notify(f"{source.upper()} feed recovered")

    async def run_nse(self) -> None:
        while True:
            cfg = self.store.cfg
            sec = cfg.section("polling.nse")
            started = time.monotonic()
            if sec.get("enabled", True):
                try:
                    await self.poll_nse_once(cfg)
                except SourceError as exc:
                    log.warning("NSE poll failed: %s", exc)
                    await self._record_poll("nse", False, (time.monotonic() - started) * 1000, str(exc))
                except Exception as exc:
                    log.exception("NSE poll crashed")
                    await self._record_poll("nse", False, (time.monotonic() - started) * 1000, repr(exc))
            interval = poll_interval(sec, market_state(utcnow(), cfg))
            await asyncio.sleep(max(0.2, interval - (time.monotonic() - started)))

    # ---- BSE (built, off by default) -------------------------------------------------

    async def run_bse(self) -> None:
        while True:
            cfg = self.store.cfg
            sec = cfg.section("polling.bse")
            started = time.monotonic()
            if sec.get("enabled", False) and self.bse is not None:
                try:
                    items = await self.bse.announcements(utcnow(), float(sec.get("timeout_sec", 8)))
                    detected = utcnow()
                    self._dispatch(await self.ingest_announcements(items, detected, cfg))
                    await self._record_poll("bse", True, (time.monotonic() - started) * 1000)
                except SourceError as exc:
                    log.warning("BSE poll failed: %s", exc)
                    await self._record_poll("bse", False, (time.monotonic() - started) * 1000, str(exc))
                except Exception as exc:
                    log.exception("BSE poll crashed")
                    await self._record_poll("bse", False, (time.monotonic() - started) * 1000, repr(exc))
            interval = poll_interval(sec, market_state(utcnow(), cfg))
            await asyncio.sleep(max(0.5, interval - (time.monotonic() - started)))
