from __future__ import annotations

import asyncio
import logging
from typing import Any

from .attachments import Attachment, AttachmentFetcher
from .channels.base import Channel, ChannelError
from .config import Config, ConfigStore
from .context import ContextStore
from .db import Database, as_dt, utcnow
from .messages import build_text, ist_clock
from .summarizer import SummaryResult, Summarizer, template_summary

log = logging.getLogger(__name__)


class Pipeline:
    """Takes one stored event from summary to delivery, recording every step."""

    def __init__(
        self,
        store: ConfigStore,
        db: Database,
        context: ContextStore,
        summarizer: Summarizer,
        fetcher: AttachmentFetcher,
        channels: dict[str, Channel],
        quotes=None,
    ):
        self.store = store
        self.db = db
        self.context = context
        self.summarizer = summarizer
        self.fetcher = fetcher
        self.channels = channels
        self.quotes = quotes
        self.followups = None  # set by the app once the follow-up service exists

    async def handle(self, event_id: int) -> None:
        try:
            await self._handle(event_id)
        except Exception:
            log.exception("event %s failed", event_id)
            await self.db.execute("update events set status = 'failed', processed_at = :now where id = :id",
                                  id=event_id, now=utcnow())

    async def _subscribers(self, symbol: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "select s.* from subscribers s join subscriptions x on x.subscriber_id = s.id "
            "where x.symbol = :symbol and s.status = 'active'",
            symbol=symbol,
        )

    def _clock_start(self, ev: dict[str, Any], cfg: Config):
        detected = as_dt(ev["detected_at"])
        listed = as_dt(ev.get("listed_at")) or detected
        return listed if cfg.get("sla.clock_start", "listed") == "listed" else detected

    async def _handle(self, event_id: int) -> None:
        cfg = self.store.cfg
        claimed = await self.db.execute(
            "update events set status = 'processing' where id = :id and status = 'new'", id=event_id
        )
        if not claimed:
            return
        ev = await self.db.fetch_one("select * from events where id = :id", id=event_id)
        ev["detected_at"] = as_dt(ev["detected_at"])
        ev["listed_at"] = as_dt(ev["listed_at"]) or ev["detected_at"]

        subs = await self._subscribers(ev["symbol"])
        channels = [c for c in self.channels.values() if c.enabled()]
        targets = {c.name: [s for s in subs if c.eligible(s)] for c in channels}
        if not any(targets.values()) and not cfg.get("alerts.generate_summary_without_subscribers", False):
            await self.db.execute("update events set status = 'done', processed_at = :now where id = :id",
                                  id=event_id, now=utcnow())
            return

        att_task = None
        if ev.get("attachment_url"):
            att_task = asyncio.create_task(self.fetcher.fetch(ev["attachment_url"], cfg))
        quote_task = index_task = None
        if self.quotes is not None:
            quote_task = asyncio.create_task(self.quotes.stock(ev["symbol"], cfg))
            if self.followups is not None and self.followups.wanted(ev, cfg):
                index_task = asyncio.create_task(self.quotes.index(cfg))

        summary = await self._summary(ev, att_task, cfg)
        await self.db.execute(
            "update events set summary = :s, summary_model = :m, summary_ms = :ms, summary_fallback = :fb "
            "where id = :id",
            s=summary.text, m=summary.model, ms=summary.ms, fb=summary.fallback, id=event_id,
        )

        quote = await self._result(quote_task)
        rich, plain = build_text(ev, summary.text, quote)
        att: Attachment | None = None
        if att_task is not None:
            try:
                att = await asyncio.wait_for(asyncio.shield(att_task), timeout=float(cfg.get("attachments.fetch_timeout_sec", 8)))
            except (asyncio.TimeoutError, Exception):
                att = None

        await asyncio.gather(
            *(self._deliver_channel(c, targets[c.name], ev, rich, plain, att, cfg) for c in channels if targets[c.name])
        )
        await self.db.execute("update events set status = 'done', processed_at = :now where id = :id",
                              id=event_id, now=utcnow())
        if self.followups is not None and quote is not None:
            sent = await self.db.fetch_one(
                "select count(*) as n from deliveries where event_id = :id and status = 'sent'", id=event_id)
            if sent and int(sent["n"]) > 0:
                await self.followups.schedule(ev, quote, await self._result(index_task))

    @staticmethod
    async def _result(task):
        if task is None:
            return None
        try:
            return await task
        except Exception:
            return None

    async def _summary(self, ev: dict[str, Any], att_task, cfg: Config) -> SummaryResult:
        s = cfg.section("summary")
        ctx = self.context.get(ev["symbol"], float(cfg.get("context.max_age_sec", 172800)))
        elapsed = (utcnow() - self._clock_start(ev, cfg)).total_seconds()
        budget = max(float(s.get("min_budget_sec", 8)), float(s.get("deadline_sec", 35)) - elapsed)
        item = {
            "symbol": ev["symbol"],
            "company": ev.get("company"),
            "category": ev["category"],
            "subject": ev.get("subject"),
            "detail": ev.get("detail"),
            "listed_at_ist": ist_clock(ev["listed_at"]),
        }

        async def work() -> SummaryResult:
            text = ""
            if att_task is not None:
                try:
                    att = await asyncio.wait_for(
                        asyncio.shield(att_task), timeout=float(cfg.get("attachments.fetch_timeout_sec", 8))
                    )
                    text = await self.fetcher.read_text(att, cfg)
                except (asyncio.TimeoutError, Exception):
                    text = ""
            return await self.summarizer.summarise(item, ctx, text, cfg)

        try:
            return await asyncio.wait_for(work(), timeout=budget)
        except asyncio.TimeoutError:
            log.warning("summary for event %s missed its %.0fs budget, using the template", ev["id"], budget)
        except Exception as exc:
            log.warning("summary for event %s failed (%s), using the template", ev["id"], exc)
        return SummaryResult(text=template_summary(item, ctx, int(s.get("max_words", 100))), model="template",
                             ms=0, fallback=True)

    async def _deliver_channel(self, channel: Channel, subs: list[dict[str, Any]], ev: dict[str, Any],
                               rich: str, plain: str, att: Attachment | None, cfg: Config) -> None:
        prep_task = None
        if att is not None and att.sendable:
            prep_task = asyncio.create_task(channel.prepare_attachment(att))
        sem = asyncio.Semaphore(channel.max_concurrency())
        await asyncio.gather(
            *(self._deliver_one(channel, sub, ev, rich, plain, att, prep_task, sem, cfg) for sub in subs)
        )

    async def _deliver_one(self, channel: Channel, sub: dict[str, Any], ev: dict[str, Any], rich: str, plain: str,
                           att: Attachment | None, prep_task, sem: asyncio.Semaphore, cfg: Config) -> None:
        d_cfg = cfg.section("delivery")
        delivery_id = await self.db.insert_returning_id(
            "insert into deliveries (event_id, subscriber_id, channel, status, queued_at) "
            "values (:e, :s, :c, 'queued', :now) on conflict (event_id, subscriber_id, channel) do nothing returning id",
            e=ev["id"], s=sub["id"], c=channel.name, now=utcnow(),
        )
        if delivery_id is None:
            return  # already delivered or being delivered

        handle = None
        if prep_task is not None:
            try:
                handle = await prep_task
            except Exception as exc:
                log.warning("%s could not prepare the attachment: %s", channel.name, exc)
        inline = channel.attachment_inline()

        message_id, attempts, error = None, 0, ""
        max_attempts = int(d_cfg.get("max_attempts", 3))
        target_ms = float(cfg.get("sla.target_sec", 60)) * 1000
        while attempts < max_attempts:
            attempts += 1
            try:
                async with sem:
                    message_id = await channel.send_text(sub, rich, plain, handle if inline else None, att)
                break
            except ChannelError as exc:
                error = str(exc)
                over = (utcnow() - self._clock_start(ev, cfg)).total_seconds() * 1000 > target_ms
                if not exc.retryable or over or attempts >= max_attempts:
                    break
                await asyncio.sleep(float(d_cfg.get("retry_delay_sec", 1.0)))
            except Exception as exc:
                error = f"{exc.__class__.__name__}: {exc}"
                break

        if message_id is None:
            await self.db.execute(
                "update deliveries set status = 'failed', error = :err, attempts = :n where id = :id",
                err=error[:500], n=attempts, id=delivery_id,
            )
            log.error("%s delivery to subscriber %s failed: %s", channel.name, sub["id"], error)
            return

        sent_at = utcnow()
        since_listed = int((sent_at - ev["listed_at"]).total_seconds() * 1000)
        since_detected = int((sent_at - ev["detected_at"]).total_seconds() * 1000)
        clock_ms = since_listed if cfg.get("sla.clock_start", "listed") == "listed" else since_detected
        within = clock_ms <= target_ms
        if not within:
            log.warning("SLA breach: event %s (%s) reached subscriber %s on %s after %.1fs",
                        ev["id"], ev["symbol"], sub["id"], channel.name, clock_ms / 1000)
        att_status = "none"
        if ev.get("attachment_url"):
            att_status = "link_only" if (att is None or not att.sendable or handle is None) else "pending"
            if inline and handle is not None:
                att_status = "sent"
        await self.db.execute(
            "update deliveries set status = 'sent', provider_message_id = :mid, attempts = :n, sent_at = :now, "
            "since_listed_ms = :sl, since_detected_ms = :sd, within_sla = :ok, attachment_status = :att where id = :id",
            mid=message_id, n=attempts, now=sent_at, sl=since_listed, sd=since_detected, ok=within,
            att=att_status, id=delivery_id,
        )

        if att_status == "pending":
            status = "failed"
            for attempt in (1, 2):
                try:
                    async with sem:
                        await channel.send_attachment(sub, handle, att)
                    status = "sent"
                    break
                except ChannelError as exc:
                    log.warning("%s attachment to subscriber %s failed: %s", channel.name, sub["id"], exc)
                    if not exc.retryable:
                        break
                except Exception:
                    log.exception("attachment send crashed")
                    break
            await self.db.execute("update deliveries set attachment_status = :a where id = :id",
                                  a=status, id=delivery_id)
