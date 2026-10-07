from __future__ import annotations

import sqlite3
from datetime import timedelta

from conftest import seed_signal
from nse_alerts.archive import archive_old, backup_sqlite
from nse_alerts.db import StatusWriter, utcnow
from test_pipeline import add_subscriber, announcement, settle

HEAVY = {"event_signals", "daily_prices", "context_cache", "event_raw", "events_archive", "deliveries_archive"}
SMALL = {"subscribers", "subscriptions", "events", "deliveries", "corporate_actions", "follow_ups", "system_status"}


async def tables(db):
    rows = await db.fetch_all("select name from sqlite_master where type = 'table'")
    return {r["name"] for r in rows if not r["name"].startswith("sqlite_")}


async def test_heavy_tables_live_only_on_the_server_database(db, local):
    shared, server = await tables(db), await tables(local)
    assert SMALL <= shared and not (HEAVY & shared)
    assert HEAVY <= server and not ({"subscribers", "deliveries"} & server)


async def test_raw_source_json_goes_to_the_server_not_the_shared_database(system):
    await add_subscriber(system.db)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    ev = await system.db.fetch_one("select id, raw from events where source_uid = '1'")
    assert ev["raw"] is None
    raw = await system.local.fetch_one("select raw from event_raw where event_id = :id", id=ev["id"])
    assert '"seq_id": "1"' in raw["raw"]


async def test_status_goes_to_the_server_every_time_and_to_the_shared_database_only_now_and_then(db, local):
    writer = StatusWriter(db, local, shared_interval_sec=3600)
    await writer.set("nse_poll", "one")
    await writer.set("nse_poll", "two")
    assert (await local.fetch_one("select value from system_status where key = 'nse_poll'"))["value"] == "two"
    assert (await db.fetch_one("select value from system_status where key = 'nse_poll'"))["value"] == "one"
    await writer.set("nse_poll", "three", force_shared=True)
    assert (await db.fetch_one("select value from system_status where key = 'nse_poll'"))["value"] == "three"


async def make_event(db, days_old, uid, status="done"):
    created = utcnow() - timedelta(days=days_old)
    return await db.insert_returning_id(
        "insert into events (source, source_uid, category, symbol, subject, listed_at, detected_at, status, created_at) "
        "values ('nse', :uid, 'announcement', 'TCS', 'Board', :t, :t, :st, :t) returning id",
        uid=uid, t=created, st=status)


async def test_old_alerts_move_to_the_server_and_leave_the_shared_database(system):
    sid = await add_subscriber(system.db)
    old = await make_event(system.db, 60, "old")
    await system.db.execute(
        "insert into deliveries (event_id, subscriber_id, channel, status, queued_at) values (:e, :s, 'telegram', 'sent', :n)",
        e=old, s=sid, n=utcnow())
    await system.db.execute(
        "insert into follow_ups (event_id, symbol, due_at, status) values (:e, 'TCS', :n, 'sent')", e=old, n=utcnow())
    await system.db.execute(
        "insert into corporate_actions (symbol, subject, detected_at, event_id) values ('TCS', 'Dividend', :n, :e)",
        n=utcnow(), e=old)
    recent = await make_event(system.db, 2, "recent")

    moved = await archive_old(system.db, system.local, system.store.cfg)
    assert moved == 1
    assert [r["id"] for r in await system.db.fetch_all("select id from events")] == [recent]
    assert await system.db.fetch_one("select * from deliveries") is None
    assert await system.db.fetch_one("select * from follow_ups") is None
    assert (await system.db.fetch_one("select event_id from corporate_actions"))["event_id"] is None
    assert (await system.local.fetch_one("select id from events_archive"))["id"] == old
    assert (await system.local.fetch_one("select event_id from deliveries_archive"))["event_id"] == old
    assert (await system.local.fetch_one("select event_id from follow_ups_archive"))["event_id"] == old


async def test_alerts_still_in_progress_are_not_moved(system):
    sid = await add_subscriber(system.db)
    waiting = await make_event(system.db, 60, "waiting")
    await system.db.execute(
        "insert into follow_ups (event_id, symbol, due_at, status) values (:e, 'TCS', :n, 'pending')",
        e=waiting, n=utcnow())
    busy = await make_event(system.db, 60, "busy", status="processing")
    assert await archive_old(system.db, system.local, system.store.cfg) == 0
    assert {r["id"] for r in await system.db.fetch_all("select id from events")} == {waiting, busy}


async def test_retention_can_be_turned_off(system):
    await make_event(system.db, 400, "ancient")
    system.store.cfg.data["retention"]["shared_days"] = 0
    assert await archive_old(system.db, system.local, system.store.cfg) == 0


def test_backup_makes_a_consistent_copy_and_keeps_only_the_newest(tmp_path):
    source = tmp_path / "local.db"
    conn = sqlite3.connect(source)
    conn.execute("create table t (x integer)")
    conn.execute("insert into t values (42)")
    conn.commit()
    conn.close()
    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(3):
        (backups / f"local-2020010{i}-0000.db").write_bytes(b"old")
    dest = backup_sqlite(f"sqlite+aiosqlite:///{source.as_posix()}", backups, keep=2)
    copied = sqlite3.connect(dest)
    assert copied.execute("select x from t").fetchone() == (42,)
    copied.close()
    assert len(list(backups.glob("local-*.db"))) == 2
    assert backup_sqlite("postgresql+psycopg://x/y", backups, keep=2) is None


async def test_history_matching_reads_the_server_database(system):
    for i in range(5):
        await seed_signal(system.local, "TCS", 100 + 40 * i)
    from nse_alerts.signals.matching import build_report
    r = await build_report(system.local, system.store.cfg, "TCS", "financial_results", 70, 0.8, utcnow())
    assert len(r.cases) == 5
    assert await system.db.fetch_one("select count(*) as n from events") == {"n": 0}
