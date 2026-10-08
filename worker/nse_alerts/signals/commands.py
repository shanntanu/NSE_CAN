"""Command-line steps for building the history behind the past-signals feature."""
from __future__ import annotations

from datetime import timedelta

from ..app import load_universe
from ..config import ConfigStore
from ..db import LOCAL_SCHEMA_FILE, Database, local_url, utcnow
from ..sources.nse import NseClient
from ..attachments import AttachmentFetcher
from ..summarizer import Summarizer
from .matching import build_report, signal_block, signal_details
from .prices import PriceStore
from .service import SignalService
from .taxonomy import TOPICS


def _service(store: ConfigStore, db: Database) -> SignalService:
    return SignalService(store, db, Summarizer(), PriceStore(db), AttachmentFetcher(store.cfg))


def _symbols(args, store: ConfigStore) -> list[str]:
    if getattr(args, "symbols", None):
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    return sorted(load_universe(store).symbols)


async def cmd_signals_backfill(args) -> None:
    """Download prices and each stock's NSE announcement history (no model calls, no cost)."""
    store = ConfigStore()
    db = Database(local_url())
    await db.init_schema(LOCAL_SCHEMA_FILE)
    svc = _service(store, db)
    years = args.years or int(store.cfg.get("signals.lookback_years", 5))
    symbols = _symbols(args, store)
    print(f"downloading {years} years of prices for {len(symbols)} stocks and the Nifty 50 ...")
    failed = await svc.download_prices(symbols, years)
    if failed:
        print(f"WARNING: no prices for {', '.join(failed)}; run this command again later to fill them in")
    nse = NseClient(store.cfg)
    try:
        added = await svc.backfill_announcements(nse, symbols, years, store.cfg)
    finally:
        await nse.close()
    total = await db.fetch_one("select count(*) as n from event_signals")
    print(f"added {added} announcements; {total['n']} items stored. Next: signals-score, then signals-returns")
    await svc.summarizer.close()
    await db.close()


async def cmd_signals_score(args) -> None:
    """Give every stored item a topic and a 1-100 sentiment (uses your OpenRouter key, one call per item)."""
    store = ConfigStore()
    db = Database(local_url())
    svc = _service(store, db)
    if args.redo_fallback:
        cleared = await svc.reset_keyword_scores()
        print(f"cleared {cleared} items that only had a keyword guess; they will be scored by the model now")
    pending = await db.fetch_one("select count(*) as n from event_signals where scored_at is null")
    count = min(pending["n"], args.limit) if args.limit else pending["n"]
    how = "keyword rules (no model)" if args.keywords else f"model {store.cfg.get('signals.score_model') or store.cfg.get('summary.model')}"
    print(f"scoring {count} of {pending['n']} unscored items with {how} ...")
    if args.pdf:
        print("reading each filing PDF when the exchange text is too generic (slower, better scores)")
    done = await svc.score_pending(store.cfg, args.limit, use_keywords=args.keywords, use_pdf=args.pdf)
    left = (await db.fetch_one("select count(*) as n from event_signals where scored_at is null"))["n"]
    print(f"scored {done}; {left} still unscored")
    if svc.last_run["failed"]:
        print(f"{svc.last_run['failed']} items failed and were left unscored; run the command again to retry them")
    if svc.last_run["stopped"]:
        print("\nSTOPPED EARLY: OpenRouter refused the request, so nothing more can be scored right now.\n"
              f"  {svc.last_run['stopped'][:300]}\n"
              "  Add credits at https://openrouter.ai/settings/credits (or check the key), then run this command "
              "again. Items already scored are kept.")
    elif left == 0:
        print("COMPLETE: every item has been scored. Next: signals-returns")
    await svc.summarizer.close()
    await db.close()


async def cmd_signals_returns(args) -> None:
    """Work out what 10,000 rupees would have done 15 and 30 days after each scored item."""
    store = ConfigStore()
    db = Database(local_url())
    svc = _service(store, db)
    done, missing = await svc.compute_returns()
    print(f"outcomes completed for {done} items; {missing} had no price data")
    await svc.summarizer.close()
    await db.close()


async def cmd_signals_stats(args) -> None:
    db = Database(local_url())
    for title, sql in (
        ("scoring progress (scored_model: who scored it; keywords = a guess, redo with --redo-fallback)",
         "select coalesce(scored_model, 'NOT SCORED YET') as k, count(*) as n from event_signals "
         "group by scored_model order by n desc"),
        ("returns status", "select returns_status as k, count(*) as n from event_signals group by returns_status"),
        ("topics", "select coalesce(topic, 'unscored') as k, count(*) as n from event_signals group by topic order by n desc"),
        ("sentiment bands", "select (sentiment / 10) * 10 as k, count(*) as n from event_signals "
                            "where sentiment is not null group by (sentiment / 10) * 10 order by k"),
        ("low confidence", "select case when confidence < 0.5 then 'low (left out of matching)' else 'ok' end as k, "
                           "count(*) as n from event_signals where confidence is not null group by 1"),
    ):
        rows = await db.fetch_all(sql)
        print(f"\n{title}:")
        for r in rows:
            print(f"  {str(r['k']):32} {r['n']}")
    await db.close()


async def cmd_signals_preview(args) -> None:
    """Show the past-signals section as it would appear for a piece of news (needs the history built first)."""
    store = ConfigStore()
    db = Database(local_url())
    svc = _service(store, db)
    ev = {"id": 0, "source": "preview", "source_uid": f"preview:{args.symbol}", "symbol": args.symbol.upper(),
          "category": "announcement", "subject": args.subject, "detail": args.detail, "listed_at": utcnow()}
    report = await svc.analyse(ev, store.cfg)
    await db.execute("delete from event_signals where source = 'preview'")
    print(f"topic {report.topic} ({TOPICS.get(report.topic, '')}), sentiment {report.sentiment}, "
          f"confidence {report.confidence:.2f}\n")
    block = signal_block(report)
    print(block or "(too generic to score: no past-signals section would be shown)")
    details = signal_details(report)
    if details:
        print("\n--- second message ---\n" + details)
    await svc.summarizer.close()
    await db.close()
