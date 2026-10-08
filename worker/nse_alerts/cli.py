from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from .app import App, load_universe, setup_logging
from .archive import archive_old, backup_sqlite
from .config import ConfigStore
from .context import fetch_symbol
from .db import Database, init_databases, local_url, open_databases, utcnow
from .signals import commands as sig
from .summarizer import Summarizer, template_summary

UNIVERSE_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"


async def cmd_init_db(args) -> None:
    shared, local = open_databases()
    await init_databases(shared, local)
    print(f"shared database ready: {shared.url.split('@')[-1]}")
    print(f"local (server) database ready: {local.url.split('@')[-1]}")
    await shared.close()
    await local.close()


async def cmd_archive_now(args) -> None:
    """Move alerts older than retention.shared_days from the shared database to the server database."""
    shared, local = open_databases()
    await init_databases(shared, local)
    moved = await archive_old(shared, local, ConfigStore().cfg)
    print(f"moved {moved} alerts to the server database")
    await shared.close()
    await local.close()


async def cmd_backup_local(args) -> None:
    """Make a consistent copy of the server database file (data/backups/, newest 7 kept)."""
    dest = await asyncio.to_thread(
        backup_sqlite, Database(local_url()).url, Path(__file__).resolve().parent.parent / "data" / "backups",
        int(ConfigStore().cfg.get("retention.backups_to_keep", 7)))
    print(f"backup written to {dest}" if dest else
          "the local database is not a SQLite file here; back it up with pg_dump or a disk snapshot")


async def cmd_db_sizes(args) -> None:
    shared, local = open_databases()
    size = await shared.size_bytes()
    print("shared database:", f"{size / 1_048_576:.1f} MB" if size is not None else "SQLite file")
    for table in ("subscribers", "subscriptions", "events", "deliveries", "corporate_actions", "follow_ups"):
        row = await shared.fetch_one(f"select count(*) as n from {table}")
        print(f"  {table:20} {row['n']} rows")
    path = Path(__file__).resolve().parent.parent / "data" / "local.db"
    print("local database:", f"{path.stat().st_size / 1_048_576:.1f} MB" if path.exists() else local.url.split("@")[-1])
    for table in ("event_signals", "daily_prices", "context_cache", "event_raw", "events_archive", "deliveries_archive"):
        row = await local.fetch_one(f"select count(*) as n from {table}")
        print(f"  {table:20} {row['n']} rows")
    await shared.close()
    await local.close()


async def cmd_run(args) -> None:
    await App().run()


async def cmd_telegram_login(args) -> None:
    from .channels.telegram import make_client

    store = ConfigStore()
    client = make_client(store)
    await client.start()  # asks for the phone number, login code and 2FA password in the terminal
    me = await client.get_me()
    print(f"logged in as {me.first_name} ({me.phone}). Session saved; start the worker now.")
    await client.disconnect()


async def cmd_poll_once(args) -> None:
    """Fetch the live NSE feeds and show what the worker would see. Stores nothing, sends nothing."""
    from .sources.nse import NseClient

    store = ConfigStore()
    universe = load_universe(store)
    nse = NseClient(store.cfg)
    try:
        items = await nse.announcements()
        actions = await nse.corporate_actions()
    finally:
        await nse.close()
    now = utcnow()
    print(f"{len(items)} announcements, {len(actions)} corporate actions")
    for it in items[: args.limit]:
        age = (now - it.listed_at).total_seconds() if it.listed_at else float("nan")
        tag = "NIFTY50" if it.symbol in universe else "       "
        print(f"{tag} {it.symbol:12} listed {age:6.0f}s ago  {it.subject[:60]}")
    for it in actions[: args.limit]:
        tag = "NIFTY50" if it.symbol in universe else "       "
        print(f"{tag} {it.symbol:12} {it.subject[:60]}  ({it.detail[:60]})")


async def cmd_refresh_context(args) -> None:
    store = ConfigStore()
    universe = load_universe(store)
    symbols = args.symbols or sorted(universe.symbols)
    cfg = store.cfg.section("context")
    for symbol in symbols:
        data = await asyncio.to_thread(fetch_symbol, symbol, cfg.get("history_period", "1y"), int(cfg.get("news_items", 3)))
        t, f = data.get("technicals", {}), data.get("fundamentals", {})
        print(f"{symbol:12} close {t.get('last_close')}  RSI {t.get('rsi14')}  PE {f.get('pe_trailing')}  "
              f"news {len(data.get('news', []))}")
        if args.json:
            print(json.dumps(data, indent=1))


async def cmd_summarise_test(args) -> None:
    """Run the summary step on a sample item so you can check the key and model."""
    store = ConfigStore()
    cfg = store.cfg
    cfg_s = cfg.section("context")
    data = await asyncio.to_thread(fetch_symbol, args.symbol, cfg_s.get("history_period", "1y"), 3)
    item = {
        "symbol": args.symbol, "company": args.symbol, "category": "announcement",
        "subject": args.subject, "detail": args.detail, "listed_at_ist": "now",
    }
    summarizer = Summarizer()
    try:
        result = await summarizer.summarise(item, data, "", cfg)
        print(f"model {result.model}, {result.ms} ms, {len(result.text.split())} words\n")
        print(result.text)
    except Exception as exc:
        print(f"summary failed: {exc}\n\nTemplate fallback would send:\n{template_summary(item, data)}")
    finally:
        await summarizer.close()


async def cmd_add_subscriber(args) -> None:
    db = Database()
    await db.init_schema()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    now = utcnow()
    sid = await db.insert_returning_id(
        "insert into subscribers (mobile, status, consent_at, created_at, updated_at) "
        "values (:m, 'active', :now, :now, :now) "
        "on conflict (mobile) do update set status = 'active', updated_at = excluded.updated_at returning id",
        m=args.mobile, now=now,
    )
    await db.execute("delete from subscriptions where subscriber_id = :id", id=sid)
    for s in symbols:
        await db.execute("insert into subscriptions (subscriber_id, symbol) values (:id, :s)", id=sid, s=s)
    print(f"subscriber {sid} ({args.mobile}) active for {', '.join(symbols)}")
    await db.close()


async def cmd_sync_universe(args) -> None:
    store = ConfigStore()
    path = Path(store.cfg.get("universe.file", "../data/nifty50.csv"))
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    async with httpx.AsyncClient(headers={"User-Agent": store.cfg.get("polling.nse.user_agent")}) as client:
        resp = await client.get(UNIVERSE_URL, timeout=20)
    resp.raise_for_status()
    path.write_bytes(resp.content)
    print(f"saved {len(load_universe(store).symbols)} symbols to {path}")


async def cmd_status(args) -> None:
    shared, local = open_databases()
    for title, db in (("local (live, every few seconds)", local), ("shared (what the website sees)", shared)):
        print(f"{title}:")
        for row in await db.fetch_all("select key, value, updated_at from system_status order by key"):
            print(f"  {row['key']:24} {row['updated_at']}  {row['value']}")
    counts = await shared.fetch_all("select status, count(*) as n from deliveries group by status")
    print("deliveries:", {r["status"]: r["n"] for r in counts})
    await shared.close()
    await local.close()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")  # the rupee sign and bullets break the Windows console default
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser(prog="nse_alerts")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(fn=cmd_init_db)
    sub.add_parser("archive-now").set_defaults(fn=cmd_archive_now)
    sub.add_parser("backup-local").set_defaults(fn=cmd_backup_local)
    sub.add_parser("db-sizes").set_defaults(fn=cmd_db_sizes)
    sub.add_parser("run").set_defaults(fn=cmd_run)
    sub.add_parser("telegram-login").set_defaults(fn=cmd_telegram_login)
    p = sub.add_parser("poll-once")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(fn=cmd_poll_once)
    p = sub.add_parser("refresh-context")
    p.add_argument("symbols", nargs="*")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_refresh_context)
    p = sub.add_parser("summarise-test")
    p.add_argument("--symbol", default="RELIANCE")
    p.add_argument("--subject", default="Dividend - Rs 10 Per Share")
    p.add_argument("--detail", default="Board recommended a dividend; ex-date and record date to be announced.")
    p.set_defaults(fn=cmd_summarise_test)
    p = sub.add_parser("add-subscriber")
    p.add_argument("--mobile", required=True)
    p.add_argument("--symbols", required=True, help="comma separated, e.g. TCS,INFY")
    p.set_defaults(fn=cmd_add_subscriber)
    p = sub.add_parser("signals-backfill", help="download prices and announcement history")
    p.add_argument("--symbols", help="comma separated; default is all tracked stocks")
    p.add_argument("--years", type=int)
    p.set_defaults(fn=sig.cmd_signals_backfill)
    p = sub.add_parser("signals-score", help="topic and sentiment for every stored item")
    p.add_argument("--limit", type=int)
    p.add_argument("--keywords", action="store_true", help="use keyword rules instead of the model")
    p.add_argument("--pdf", action="store_true", help="read the filing PDF when the exchange text is generic")
    p.add_argument("--redo-fallback", action="store_true",
                   help="re-score items that only got a keyword guess (for example after a credits error)")
    p.set_defaults(fn=sig.cmd_signals_score)
    sub.add_parser("signals-returns", help="15 and 30 day outcomes").set_defaults(fn=sig.cmd_signals_returns)
    sub.add_parser("signals-stats").set_defaults(fn=sig.cmd_signals_stats)
    p = sub.add_parser("signals-preview", help="show the past-signals section for a piece of news")
    p.add_argument("--symbol", required=True)
    p.add_argument("--subject", default="Outcome of Board Meeting")
    p.add_argument("--detail", default="")
    p.set_defaults(fn=sig.cmd_signals_preview)
    sub.add_parser("sync-universe").set_defaults(fn=cmd_sync_universe)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = parser.parse_args()
    setup_logging("INFO")
    try:
        asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
