"""Telegram housekeeping: find each subscriber's Telegram account and deliver verification codes."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from .channels.telegram import TelegramChannel
from .config import ConfigStore
from .db import Database, utcnow

log = logging.getLogger(__name__)


class HourlyBudget:
    """Caps how many new people the sender account contacts per hour, to protect it from Telegram limits."""

    def __init__(self):
        self.stamps: deque[float] = deque()

    def take(self, limit: int) -> bool:
        now = time.monotonic()
        while self.stamps and now - self.stamps[0] > 3600:
            self.stamps.popleft()
        if len(self.stamps) >= limit:
            return False
        self.stamps.append(now)
        return True


budget = HourlyBudget()


async def resolve_pending(db: Database, tg: TelegramChannel, limit: int = 10, hourly_cap: int = 60) -> None:
    rows = await db.fetch_all(
        "select id, mobile from subscribers where telegram_status = 'unknown' and status in ('pending', 'active') "
        "order by updated_at limit :n",
        n=limit,
    )
    for row in rows:
        if not budget.take(hourly_cap):
            log.warning("hourly cap of %d new Telegram lookups reached, waiting", hourly_cap)
            return
        try:
            found = await tg.resolve_phone(row["mobile"])
        except Exception as exc:
            log.warning("could not look up %s on Telegram: %s", row["mobile"][-4:], exc.__class__.__name__)
            continue
        if found:
            await db.execute(
                "update subscribers set telegram_status = 'resolved', telegram_user_id = :u, "
                "telegram_access_hash = :h, updated_at = :now where id = :id",
                u=found[0], h=found[1], id=row["id"], now=utcnow(),
            )
        else:
            await db.execute(
                "update subscribers set telegram_status = 'not_found', updated_at = :now where id = :id",
                id=row["id"], now=utcnow(),
            )


async def send_codes(db: Database, tg: TelegramChannel, limit: int = 20, hourly_cap: int = 60) -> None:
    rows = await db.fetch_all(
        "select * from subscribers where verify_code is not null and verify_sent_at is null "
        "and telegram_status = 'resolved' and (verify_expires_at is null or verify_expires_at > :now) limit :n",
        now=utcnow(), n=limit,
    )
    for sub in rows:
        if not budget.take(hourly_cap):
            log.warning("hourly cap of %d verification codes reached, waiting", hourly_cap)
            return
        what = "stop alerts" if sub.get("verify_purpose") == "stop" else "start alerts"
        text = (
            f"Your NSE Alerts code to {what} is {sub['verify_code']}. It expires in 10 minutes. "
            "If you did not ask for it, ignore this message."
        )
        try:
            await tg.send_direct(sub, text)
        except Exception as exc:
            log.warning("could not send a code to subscriber %s: %s", sub["id"], exc)
            continue
        await db.execute("update subscribers set verify_sent_at = :now where id = :id", id=sub["id"], now=utcnow())


async def run(store: ConfigStore, db: Database, tg: TelegramChannel) -> None:
    while True:
        cfg = store.cfg.section("channels.telegram")
        if cfg.get("enabled", True):
            try:
                cap = int(cfg.get("max_new_people_per_hour", 60))
                await resolve_pending(db, tg, hourly_cap=cap)
                await send_codes(db, tg, hourly_cap=cap)
            except Exception:
                log.exception("telegram housekeeping failed")
        await asyncio.sleep(float(cfg.get("verification_interval_sec", 2)))
