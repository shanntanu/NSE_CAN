"""Keeps the small shared database small: old alerts move to the server's database, and the server file is backed up."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import Config, ConfigStore
from .db import Database, StatusWriter, utcnow

log = logging.getLogger(__name__)

# (shared table, archive table, key column in the archive, column that links the row to an event)
ARCHIVED = (
    ("events", "events_archive", "id", "id"),
    ("deliveries", "deliveries_archive", "id", "event_id"),
    ("follow_ups", "follow_ups_archive", "event_id", "event_id"),
)


def _in(prefix: str, ids: list[int]) -> tuple[str, dict[str, Any]]:
    names = [f":{prefix}{i}" for i in range(len(ids))]
    return "(" + ", ".join(names) + ")", {f"{prefix}{i}": v for i, v in enumerate(ids)}


async def archive_old(shared: Database, local: Database, cfg: Config, batch: int = 200) -> int:
    """Copy finished alerts older than retention.shared_days to the local database, then delete them from the shared one.

    Rows are copied first and deleted only after the copy succeeded, so a failure leaves them where they were.
    """
    days = cfg.get("retention.shared_days", 30)
    if not days:
        return 0
    cutoff = utcnow() - timedelta(days=int(days))
    moved = 0
    while True:
        rows = await shared.fetch_all(
            "select e.id from events e where e.created_at < :cutoff and e.status in ('done', 'skipped', 'failed') "
            "and not exists (select 1 from follow_ups f where f.event_id = e.id and f.status in ('pending', 'processing')) "
            "order by e.id limit :n",
            cutoff=cutoff, n=batch,
        )
        ids = [int(r["id"]) for r in rows]
        if not ids:
            return moved
        marks, params = _in("i", ids)
        for table, archive, key, link in ARCHIVED:
            for row in await shared.fetch_all(f"select * from {table} where {link} in {marks}", **params):
                cols = list(row)
                await local.execute(
                    f"insert into {archive} ({', '.join(cols)}) values ({', '.join(':' + c for c in cols)}) "
                    f"on conflict ({key}) do nothing",
                    **row,
                )
        # detach rows that point at the events being removed, then delete (explicitly, so SQLite behaves too)
        await shared.execute(f"update corporate_actions set event_id = null where event_id in {marks}", **params)
        await shared.execute(f"update follow_ups set follow_up_event_id = null where follow_up_event_id in {marks}",
                             **params)
        await shared.execute(f"delete from deliveries where event_id in {marks}", **params)
        await shared.execute(f"delete from follow_ups where event_id in {marks}", **params)
        await shared.execute(f"delete from events where id in {marks}", **params)
        moved += len(ids)
        log.info("archived %d alerts older than %s days to the server database", len(ids), days)


def backup_sqlite(db_url: str, dest_dir: Path, keep: int) -> Path | None:
    """Blocking: a consistent copy of the local SQLite file, keeping the newest `keep` copies."""
    prefix = "sqlite+aiosqlite:///"
    if not db_url.startswith(prefix):
        return None
    source = Path(db_url[len(prefix):])
    if not source.exists():
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"local-{utcnow():%Y%m%d-%H%M}.db"
    src, dst = sqlite3.connect(source), sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(dest_dir.glob("local-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return dest


async def check_shared_size(shared: Database, status: StatusWriter, cfg: Config) -> int | None:
    size = await shared.size_bytes()
    if size is None:
        return None
    await status.set("shared_db_bytes", str(size))
    limit_mb = float(cfg.get("retention.warn_shared_mb", 350))
    if size / 1_048_576 > limit_mb:
        log.warning("the shared database is %.0f MB, above the %.0f MB warning level; "
                    "lower retention.shared_days", size / 1_048_576, limit_mb)
    return size


async def run(store: ConfigStore, shared: Database, local: Database, status: StatusWriter) -> None:
    last_backup = 0.0
    while True:
        cfg = store.cfg
        try:
            await archive_old(shared, local, cfg)
            await check_shared_size(shared, status, cfg)
            every = float(cfg.get("retention.backup_every_hours", 24)) * 3600
            if every and time.monotonic() - last_backup >= every:
                dest = await asyncio.to_thread(
                    backup_sqlite, local.url, Path(__file__).resolve().parents[1] / "data" / "backups",
                    int(cfg.get("retention.backups_to_keep", 7)))
                if dest:
                    log.info("local database backed up to %s", dest)
                last_backup = time.monotonic()
        except Exception:
            log.exception("archive and backup pass failed")
        await asyncio.sleep(float(cfg.get("retention.run_every_sec", 21600)))
