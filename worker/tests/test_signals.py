from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from conftest import seed_signal
from nse_alerts.config import Config
from nse_alerts.db import utcnow
from nse_alerts.signals.matching import (Case, band, build_report, horizon_stats, signal_block, signal_details,
                                         thin_clusters)
from nse_alerts.signals.prices import compute_returns, entry_point, exit_close
from nse_alerts.signals.taxonomy import TOPICS, is_routine, keyword_score, parse_score
from test_pipeline import add_subscriber, announcement, settle


def ist(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=timezone(timedelta(hours=5, minutes=30)))


def prices(start="2024-01-01", days=120, start_price=100.0, step=1.0):
    """A straight-line price series on weekdays: open = price - 0.5, close = price."""
    dates = pd.bdate_range(start, periods=days)
    close = [start_price + step * i for i in range(days)]
    return pd.DataFrame({"open": [c - 0.5 for c in close], "close": close},
                        index=pd.Index([d.date().isoformat() for d in dates]))


# ---- entry and exit rules ------------------------------------------------------------------

def test_entry_follows_when_the_news_came_out():
    p = prices()   # 2024-01-01 is a Monday
    assert entry_point(ist(2024, 1, 3, 8, 30), p).__dict__ == {"date": "2024-01-03", "basis": "open"}
    assert entry_point(ist(2024, 1, 3, 11, 0), p).__dict__ == {"date": "2024-01-03", "basis": "close"}
    assert entry_point(ist(2024, 1, 3, 18, 0), p).__dict__ == {"date": "2024-01-04", "basis": "open"}
    assert entry_point(ist(2024, 1, 6, 11, 0), p).__dict__ == {"date": "2024-01-08", "basis": "open"}   # Saturday
    assert entry_point(ist(2024, 1, 3, 0, 0), p).__dict__ == {"date": "2024-01-04", "basis": "open"}    # date only


def test_entry_is_none_when_prices_do_not_reach_that_far():
    assert entry_point(ist(2030, 1, 3, 18, 0), prices()) is None
    assert entry_point(ist(2024, 1, 3, 18, 0), pd.DataFrame()) is None


def test_exit_is_the_last_close_inside_the_window_and_waits_for_the_window_to_finish():
    p = prices(days=60)
    day, price = exit_close(p, "2024-01-02", 15)          # window ends Wed 17 Jan
    assert day == "2024-01-17" and price == p.loc["2024-01-17", "close"]
    assert exit_close(prices(days=5), "2024-01-02", 15) is None


def test_return_on_ten_thousand_matches_a_hand_calculation():
    stock = prices(start_price=100.0, step=1.0)     # +1 rupee per trading day
    index = prices(start_price=1000.0, step=1.0)
    r = compute_returns(ist(2024, 1, 3, 18, 0), stock, index)   # buys 2024-01-04 open
    buy = stock.loc["2024-01-04", "open"]
    sell15 = stock.loc[stock.index[stock.index <= "2024-01-19"][-1], "close"]
    assert r.entry_date == "2024-01-04" and r.entry_basis == "open"
    assert r.ret_15d_pct == pytest.approx((sell15 / buy - 1) * 100)
    assert r.complete and r.nifty_15d_pct is not None
    assert 10000 * r.ret_15d_pct / 100 == pytest.approx(10000 * (sell15 / buy - 1))


def test_recent_news_has_no_completed_outcome_yet():
    stock = prices(days=10)
    r = compute_returns(ist(2024, 1, 3, 18, 0), stock, None)
    assert r.ret_15d_pct is None and r.ret_30d_pct is None and not r.complete


# ---- matching ---------------------------------------------------------------------------

def test_sentiment_band_in_points_or_percent():
    assert band(60, Config({"signals": {"sentiment_tolerance": 10}})) == (50, 70)
    assert band(60, Config({"signals": {"sentiment_tolerance": 10, "tolerance_mode": "percent"}})) == (54, 66)
    assert band(95, Config({})) == (85, 100) and band(3, Config({})) == (1, 13)


def case(symbol, days_ago, r15, r30=None):
    return Case(symbol, utcnow() - timedelta(days=days_ago), "x", 70, r15, r30 if r30 is not None else r15, 0.5, 1.0)


def test_cases_of_one_stock_close_together_count_once():
    cases = [case("TCS", 100, 1), case("TCS", 95, 2), case("TCS", 60, 3), case("INFY", 99, 4)]
    kept = thin_clusters(cases, 15)
    assert [(c.symbol, round((utcnow() - c.listed_at).days)) for c in kept] == [("TCS", 100), ("INFY", 99), ("TCS", 60)]


def test_rupee_outcomes_for_ten_thousand():
    cases = [case("A", 400, 10.0, 20.0), case("B", 300, -5.0, -10.0), case("C", 200, 5.0, 0.0)]
    h15 = horizon_stats(cases, "ret_15d_pct", "nifty_15d_pct", 10000)
    assert h15.n == 3 and h15.wins == 2 and h15.avg_pnl == pytest.approx(333.33, abs=0.01)
    assert h15.best_pnl == 1000 and h15.worst_pnl == -500 and h15.median_pnl == 500 and h15.nifty_avg_pnl == 50
    h30 = horizon_stats(cases, "ret_30d_pct", "nifty_30d_pct", 10000)
    assert h30.wins == 1 and h30.avg_pnl == pytest.approx(333.33, abs=0.01)


async def test_report_uses_the_same_stock_when_there_are_enough_cases(system):
    for i in range(6):
        await seed_signal(system.local, "TCS", 100 + 40 * i, r15=2.0, r30=4.0)
    await seed_signal(system.local, "INFY", 90, r15=-9.0, r30=-9.0)
    r = await build_report(system.local, system.store.cfg, "TCS", "financial_results", 70, 0.8, utcnow())
    assert r.scope == "stock" and len(r.cases) == 6 and r.enough
    assert r.d15.avg_pnl == pytest.approx(200) and r.d30.avg_pnl == pytest.approx(400) and r.d15.wins == 6


async def test_report_widens_to_all_nifty_50_stocks_when_the_stock_has_too_few(system):
    await seed_signal(system.local, "TCS", 100, r15=2.0)
    for i, sym in enumerate(["INFY", "HCLTECH", "WIPRO", "TECHM", "ITC"]):
        await seed_signal(system.local, sym, 100 + 50 * i, r15=1.0)
    r = await build_report(system.local, system.store.cfg, "TCS", "financial_results", 70, 0.8, utcnow())
    assert r.scope == "nifty50" and len(r.cases) == 6 and r.enough


async def test_report_filters_by_topic_sentiment_confidence_age_and_status(system):
    await seed_signal(system.local, "TCS", 100, topic="financial_results", sentiment=70)            # counts
    await seed_signal(system.local, "TCS", 200, topic="dividend", sentiment=70)                     # other topic
    await seed_signal(system.local, "TCS", 300, topic="financial_results", sentiment=85)            # outside +-10
    await seed_signal(system.local, "TCS", 400, topic="financial_results", sentiment=70, confidence=0.2)
    await seed_signal(system.local, "TCS", 500, topic="financial_results", sentiment=70, status="pending")
    await seed_signal(system.local, "TCS", 365 * 6, topic="financial_results", sentiment=70)        # older than 5 years
    r = await build_report(system.local, system.store.cfg, "TCS", "financial_results", 70, 0.8, utcnow())
    assert len(r.cases) == 1 and not r.enough


async def test_generic_news_gets_no_section(system):
    r = await build_report(system.local, system.store.cfg, "TCS", "financial_results", 50, 0.3, utcnow())
    assert r.low_information and signal_block(r) == ""


async def test_the_text_for_the_alert_and_the_list_of_cases(system):
    for i in range(5):
        await seed_signal(system.local, "TCS", 100 + 40 * i, r15=2.0, r30=-1.0)
    r = await build_report(system.local, system.store.cfg, "TCS", "financial_results", 70, 0.8, utcnow())
    block = signal_block(r)
    assert "Past signals (TCS, financial results news, sentiment 70, 5 cases in 5 yrs)" in block
    assert "15 days: avg +₹200, 5 of 5 gained" in block and "30 days: avg -₹100, 0 of 5 gained" in block
    assert "Nifty 50 over the same periods" in block and "Few cases" not in block
    details = signal_details(r)
    assert details.count("•") == 5 and "+₹200 | -₹100" in details and "not investment advice" in details


async def test_no_comparable_cases_says_so(system):
    r = await build_report(system.local, system.store.cfg, "TCS", "financial_results", 70, 0.8, utcnow())
    assert "no comparable past cases" in signal_block(r) and signal_details(r) == ""


# ---- scoring ---------------------------------------------------------------------------------

def test_model_answers_are_parsed_defensively():
    s = parse_score('Sure! {"topic": "dividend", "sentiment": 140, "confidence": 2, "reason": "big"} done', "m")
    assert (s.topic, s.sentiment, s.confidence, s.model) == ("dividend", 100, 1.0, "m")
    assert parse_score('{"topic": "made_up", "sentiment": "55.6"}').topic == "other_material"
    assert parse_score('{"topic": "buyback", "sentiment": 0}').sentiment == 1
    with pytest.raises(ValueError):
        parse_score("no json here")


def test_routine_filings_are_dropped_and_material_ones_are_kept():
    assert is_routine("Loss of Share Certificates") and is_routine("Copy of Newspaper Publication")
    assert is_routine("Trading Window")
    assert not is_routine("Outcome of Board Meeting") and not is_routine("Press Release")
    assert not is_routine("Acquisition")


def test_keyword_fallback_is_low_confidence_so_it_never_drives_a_signal():
    s = keyword_score("Buyback", "Board approved buyback at a premium, strong growth")
    assert s.topic == "buyback" and s.sentiment > 50 and s.confidence < 0.5
    assert all(t in TOPICS for t in ("dividend", "buyback", "financial_results"))


async def test_scoring_uses_the_model_and_caches_identical_text(system):
    cfg = system.store.cfg
    a = await system.signals.score("TCS", "Outcome of Board Meeting", "profit up 20%", cfg)
    b = await system.signals.score("TCS", "Outcome of Board Meeting", "profit up 20%", cfg)
    assert a is b and a.topic == "financial_results" and a.sentiment == 70 and system.summarizer.score_calls == 1


async def test_scoring_falls_back_to_keywords_when_the_model_fails(system):
    from nse_alerts.summarizer import SummaryError
    system.summarizer.score_json = SummaryError("down")
    s = await system.signals.score("TCS", "Dividend", "interim dividend declared", system.store.cfg)
    assert s.model == "keywords" and s.topic == "dividend"


# ---- history pipeline and live alert ------------------------------------------------------------

async def test_score_pending_and_returns_fill_in_the_history(system):
    for uid, days in (("h1", 400), ("h2", 300)):
        await system.local.execute(
            "insert into event_signals (source, source_uid, symbol, category, subject, text_used, listed_at) "
            "values ('nse', :u, 'TCS', 'announcement', 'Outcome of Board Meeting', 'profit up', :l)",
            u=uid, l=utcnow() - timedelta(days=days))
    assert await system.signals.score_pending(system.store.cfg) == 2

    start = (utcnow() - timedelta(days=500)).date()
    for symbol, base in (("TCS", 100.0), ("NIFTY50", 1000.0)):
        frame = prices(start=start.isoformat(), days=400, start_price=base)
        for d, row in frame.iterrows():
            await system.local.execute(
                "insert into daily_prices (symbol, trade_date, open, close) values (:s, :d, :o, :c)",
                s=symbol, d=d, o=float(row["open"]), c=float(row["close"]))
    done, missing = await system.signals.compute_returns()
    assert done == 2 and missing == 0
    row = await system.local.fetch_one("select * from event_signals where source_uid = 'h1'")
    assert row["returns_status"] == "done" and row["ret_15d_pct"] > 0 and row["entry_basis"] in ("open", "close")


async def test_live_alert_carries_the_past_signals_section_and_a_details_message(system):
    sid = await add_subscriber(system.db)
    for i in range(6):
        await seed_signal(system.local, "TCS", 100 + 40 * i, r15=2.0, r30=4.0)
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    await asyncio.sleep(0.2)
    texts = [t for _, t in system.channel.texts]
    assert "Past signals (TCS, financial results news, sentiment 70, 6 cases in 5 yrs)" in texts[0]
    assert "15 days: avg +₹200, 6 of 6 gained" in texts[0]
    assert any(t.startswith("Past cases: TCS") for t in texts[1:])
    stored = await system.local.fetch_one("select topic, sentiment from event_signals where source_uid = '1'")
    assert stored["topic"] == "financial_results" and stored["sentiment"] == 70


async def test_a_slow_or_failing_analysis_never_holds_back_the_alert(system):
    await add_subscriber(system.db)

    async def slow(*a, **k):
        await asyncio.sleep(30)

    system.signals.analyse = slow
    system.store.cfg.data["signals"]["budget_sec"] = 0.3
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await asyncio.wait_for(settle(), timeout=10)
    assert len(system.channel.texts) == 1 and "Past signals" not in system.channel.texts[0][1]


async def test_signals_can_be_switched_off_in_the_settings(system):
    await add_subscriber(system.db)
    system.store.cfg.data["signals"]["enabled"] = False
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    assert "Past signals" not in system.channel.texts[0][1] and system.summarizer.score_calls == 0


def test_generic_exchange_text_is_recognised():
    from nse_alerts.signals.taxonomy import is_generic
    assert is_generic("Tata Consultancy Services Limited has informed the Exchange about Outcome of Board Meeting")
    assert is_generic("")
    assert not is_generic("The Board approved an interim dividend of Rs 10 per share. " * 5)


async def test_history_scoring_reads_the_filing_when_the_text_is_generic(system):
    system.signals.fetcher.text = "Net profit rose 24% to Rs 1,200 crore; interim dividend Rs 10 per share."
    await system.local.execute(
        "insert into event_signals (source, source_uid, symbol, category, subject, text_used, attachment_url, listed_at) "
        "values ('nse', 'g1', 'TCS', 'announcement', 'Outcome of Board Meeting', 'X has informed the Exchange about "
        "Outcome of Board Meeting', 'https://x/f.pdf', :l)", l=utcnow() - timedelta(days=400))
    seen = []
    original = system.summarizer._call

    async def spy(model, messages, *a, **k):
        seen.append(messages[1]["content"])
        return await original(model, messages, *a, **k)

    system.summarizer._call = spy
    assert await system.signals.score_pending(system.store.cfg, use_pdf=True) == 1
    row = await system.local.fetch_one("select text_used, topic from event_signals where source_uid = 'g1'")
    assert "Net profit rose 24%" in row["text_used"] and "Net profit rose 24%" in seen[0]
    assert row["topic"] == "financial_results"


async def test_history_scoring_leaves_the_text_alone_without_the_pdf_option(system):
    system.signals.fetcher.text = "should not be read"
    await system.local.execute(
        "insert into event_signals (source, source_uid, symbol, category, subject, text_used, attachment_url, listed_at) "
        "values ('nse', 'g2', 'TCS', 'announcement', 'Updates', 'short text', 'https://x/f.pdf', :l)",
        l=utcnow() - timedelta(days=400))
    await system.signals.score_pending(system.store.cfg)
    row = await system.local.fetch_one("select text_used from event_signals where source_uid = 'g2'")
    assert row["text_used"] == "short text"


async def test_live_scoring_reads_the_filing_when_the_exchange_text_is_generic(system):
    await add_subscriber(system.db)
    system.signals.fetcher.text = "Net profit rose 24% to Rs 1,200 crore."
    system.nse.announcement_items = [announcement("1")]
    await system.poller.poll_nse_once(system.store.cfg)
    await settle()
    row = await system.local.fetch_one("select text_used from event_signals where source_uid = '1'")
    assert "Net profit rose 24%" in row["text_used"]
