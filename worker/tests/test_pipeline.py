from __future__ import annotations

import asyncio
from datetime import timedelta

from nse_alerts.db import utcnow
from nse_alerts.sources.base import Item


async def settle():
    """Let the background event tasks created by the poller finish."""
    for _ in range(50):
        await asyncio.sleep(0.02)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if not pending:
            return


def announcement(uid, symbol="TCS", age_sec=5, subject="Outcome of Board Meeting", url="https://x/f.pdf"):
    listed = utcnow() - timedelta(seconds=age_sec)
    return Item(source="nse", source_uid=uid, category="announcement", symbol=symbol, company=symbol,
                subject=subject, detail=f"{symbol} informed the Exchange", attachment_url=url,
                listed_at=listed, company_time=listed, raw={"seq_id": uid})


def action(symbol="SBIN", subject="Dividend - Rs 10 Per Share", ex="01-Oct-2026"):
    return Item(source="nse", source_uid=f"ca:{symbol}{subject}{ex}", category="corporate_action", symbol=symbol,
                company=symbol, subject=subject, detail=f"Ex-date {ex}",
                extra={"ex_date": ex, "record_date": ex})


async def add_subscriber(db, mobile="+919999900001", symbols=("TCS",), status="active", tg="resolved"):
    now = utcnow()
    sid = await db.insert_returning_id(
        "insert into subscribers (mobile, status, telegram_status, telegram_user_id, telegram_access_hash, "
        "created_at, updated_at) values (:m, :st, :tg, 11, 22, :now, :now) returning id",
        m=mobile, st=status, tg=tg, now=now)
    for s in symbols:
        await db.execute("insert into subscriptions (subscriber_id, symbol) values (:i, :s)", i=sid, s=s)
    return sid


async def test_new_announcement_reaches_a_subscriber_with_summary_and_attachment(system):
    sid = await add_subscriber(system.db)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()

    ev = await system.db.fetch_one("select * from events where source_uid = '1'")
    assert ev["status"] == "done" and ev["summary"] == "Short summary of the filing."
    assert ev["summary_model"] == "fake/model"
    d = await system.db.fetch_one("select * from deliveries where subscriber_id = :s", s=sid)
    assert d["status"] == "sent" and d["channel"] == "telegram" and d["within_sla"] in (1, True)
    assert d["attachment_status"] == "sent" and d["since_listed_ms"] >= 5000
    assert system.channel.texts[0][1].startswith("ANNOUNCEMENT | TCS")
    assert system.channel.attachments == [(sid, "filing.pdf")]


async def test_same_item_is_not_alerted_twice(system):
    await add_subscriber(system.db)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    assert len(system.channel.texts) == 1
    count = await system.db.fetch_one("select count(*) as n from events")
    assert count["n"] == 1


async def test_a_restart_does_not_resend_items_already_stored(system, store, db, local, universe):
    from nse_alerts.poller import Poller
    await add_subscriber(system.db)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    fresh = Poller(store, db, universe, system.pipeline, system.nse, local=local)  # empty in-memory memory, as after a restart
    await fresh.poll_nse_once(store.cfg)
    await settle()
    assert len(system.channel.texts) == 1


async def test_old_items_are_stored_but_not_sent(system):
    await add_subscriber(system.db)
    system.nse.announcement_items = [announcement("old", age_sec=3600)]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    ev = await system.db.fetch_one("select status from events where source_uid = 'old'")
    assert ev["status"] == "skipped" and system.channel.texts == []


async def test_stocks_outside_the_universe_are_ignored(system):
    await add_subscriber(system.db, symbols=("NOTNIFTY",))
    system.nse.announcement_items = [announcement("9", symbol="NOTNIFTY")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    count = await system.db.fetch_one("select count(*) as n from events")
    assert count["n"] == 0


async def test_only_active_subscribers_of_that_stock_get_it(system):
    mine = await add_subscriber(system.db, "+919999900001", ("TCS",))
    await add_subscriber(system.db, "+919999900002", ("INFY",))
    await add_subscriber(system.db, "+919999900003", ("TCS",), status="stopped")
    await add_subscriber(system.db, "+919999900004", ("TCS",), tg="not_found")
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    assert [sid for sid, _ in system.channel.texts] == [mine]


async def test_no_subscribers_means_no_summary_cost(system):
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    ev = await system.db.fetch_one("select status, summary from events")
    assert ev["status"] == "done" and ev["summary"] is None and system.summarizer.calls == []


async def test_corporate_actions_are_stored_and_first_run_does_not_alert(system):
    await add_subscriber(system.db, symbols=("SBIN",))
    system.nse.action_items = [action()]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    assert system.channel.texts == []
    stored = await system.db.fetch_one("select * from corporate_actions where symbol = 'SBIN'")
    assert stored["subject"].startswith("Dividend") and stored["is_nifty50"] in (1, True)

    system.nse.action_items = [action(), action("SBIN", "Bonus 1:1", "05-Oct-2026")]
    system.poller.poll_count = 0  # next poll is a corporate-action poll again
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    assert len(system.channel.texts) == 1 and "CORPORATE ACTION | SBIN" in system.channel.texts[0][1]
    rows = await system.db.fetch_all("select subject from corporate_actions order by id")
    assert len(rows) == 2


async def test_overflow_of_the_latest_window_triggers_a_gap_fill(system):
    await add_subscriber(system.db)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    system.nse.announcement_items = [announcement("2"), announcement("3")]    # none seen before
    system.nse.day_items = [announcement("2"), announcement("3"), announcement("missed", subject="Missed filing")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    uids = {r["source_uid"] for r in await system.db.fetch_all("select source_uid from events")}
    assert "missed" in uids
    assert len(system.channel.texts) == 4


async def test_summary_failure_falls_back_to_the_template(system):
    await add_subscriber(system.db)

    async def boom(*a, **k):
        raise RuntimeError("model down")

    system.summarizer.summarise = boom
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    ev = await system.db.fetch_one("select summary, summary_fallback, summary_model from events")
    assert ev["summary_model"] == "template" and ev["summary_fallback"] in (1, True)
    assert len(system.channel.texts) == 1  # the alert still goes out


async def test_delivery_failure_is_recorded(system):
    sid = await add_subscriber(system.db)
    from nse_alerts.channels.base import ChannelError

    async def fail(*a, **k):
        raise ChannelError("privacy", retryable=False)

    system.channel.send_text = fail
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    d = await system.db.fetch_one("select status, error, attempts from deliveries where subscriber_id = :s", s=sid)
    assert d["status"] == "failed" and d["error"] == "privacy" and d["attempts"] == 1


async def test_late_delivery_is_flagged_as_an_sla_breach(system):
    sid = await add_subscriber(system.db)
    system.nse.announcement_items = [announcement("1", age_sec=30)]
    system.store.cfg.data["alerts"]["max_event_age_sec"] = 600
    system.store.cfg.data["sla"]["target_sec"] = 10   # a 30-second-old item cannot make a 10-second target
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    d = await system.db.fetch_one("select within_sla from deliveries where subscriber_id = :s", s=sid)
    assert d["within_sla"] in (0, False)


async def test_config_changes_apply_without_a_restart(system):
    import os, time
    text = system.store.path.read_text(encoding="utf-8").replace("market_hours_interval_sec: 5", "market_hours_interval_sec: 2")
    system.store.path.write_text(text, encoding="utf-8")
    os.utime(system.store.path, (time.time() + 5, time.time() + 5))
    assert system.store.cfg.get("polling.nse.market_hours_interval_sec") == 2


def open_all_day(system):
    system.store.cfg.data["market"].update({"open": "00:00", "close": "23:59", "weekdays": [0, 1, 2, 3, 4, 5, 6]})


async def test_alert_shows_price_and_volume(system):
    await add_subscriber(system.db)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    text = system.channel.texts[0][1]
    assert "Price \u20b9100.00" in text and "Volume 10.00 lakh" in text


async def test_follow_up_is_scheduled_and_sent_after_the_delay(system):
    sid = await add_subscriber(system.db)
    open_all_day(system)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    fu = await system.db.fetch_one("select * from follow_ups")
    assert fu["status"] == "pending" and fu["price_at_alert"] == 100.0 and fu["volume_at_alert"] == 1_000_000
    await system.db.execute("update follow_ups set due_at = :d", d=utcnow() - timedelta(seconds=1))

    system.quotes.price, system.quotes.volume, system.quotes.index_price = 102.0, 1_800_000, 22002.2
    assert await system.followups.process_due(system.store.cfg) == 1

    done = await system.db.fetch_one("select * from follow_ups")
    assert done["status"] == "sent" and done["verdict"] == "Positive" and round(done["change_pct"], 2) == 2.0
    assert done["volume_now"] == 1_800_000 and done["follow_up_event_id"]
    assert len(system.channel.texts) == 2
    update = system.channel.texts[1][1]
    assert update.startswith("UPDATE (Positive) | TCS") and "+2.00% since alert" in update
    assert "Volume since the alert: 8.00 lakh" in update and "Price moved with the market" in update
    d = await system.db.fetch_one("select * from deliveries where event_id = :e", e=done["follow_up_event_id"])
    assert d["subscriber_id"] == sid and d["status"] == "sent"


async def test_follow_up_only_goes_to_people_who_got_the_alert(system):
    open_all_day(system)
    got = await add_subscriber(system.db, "+919999900001")
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    await add_subscriber(system.db, "+919999900002")   # joined after the alert
    await system.db.execute("update follow_ups set due_at = :d", d=utcnow() - timedelta(seconds=1))
    await system.followups.process_due(system.store.cfg)
    assert [sid for sid, _ in system.channel.texts] == [got, got]


async def test_no_follow_up_when_the_alert_arrives_outside_market_hours(system):
    await add_subscriber(system.db)
    system.store.cfg.data["market"]["weekdays"] = []        # always closed
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    assert await system.db.fetch_one("select * from follow_ups") is None
    assert len(system.channel.texts) == 1


async def test_follow_up_is_pulled_forward_to_the_close(system):
    from nse_alerts.db import as_dt
    from nse_alerts.market import session_close
    await add_subscriber(system.db)
    open_all_day(system)
    system.store.cfg.data["follow_up"]["delay_min"] = 3000  # far later than the close
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    fu = await system.db.fetch_one("select due_at from follow_ups")
    assert as_dt(fu["due_at"]) == session_close(utcnow(), system.store.cfg)


async def test_follow_up_retries_when_no_quote_then_gives_up(system):
    await add_subscriber(system.db)
    open_all_day(system)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()

    async def none(symbol, cfg):
        return None

    system.quotes.stock = none
    for _ in range(3):
        await system.db.execute("update follow_ups set due_at = :d where status = 'pending'",
                                d=utcnow() - timedelta(seconds=1))
        await system.followups.process_due(system.store.cfg)
    row = await system.db.fetch_one("select status, attempts from follow_ups")
    assert row["status"] == "failed" and row["attempts"] == 3
    assert len(system.channel.texts) == 1
