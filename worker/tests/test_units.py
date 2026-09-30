from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest

from nse_alerts.config import Config, load_config
from nse_alerts.context import compute_technicals, extract_fundamentals, extract_news
from nse_alerts.db import normalise_url, sqlite_schema
from nse_alerts.market import market_state, poll_interval
from nse_alerts.messages import build_text
from nse_alerts.sources.nse import announcement_to_item, corporate_action_to_item, parse_nse_time
from nse_alerts.summarizer import Summarizer, SummaryError, template_summary, trim_words


def utc(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_market_state_follows_ist_hours_weekends_and_holidays():
    cfg = Config({"app": {"timezone": "Asia/Kolkata"},
                  "market": {"open": "09:00", "close": "16:00", "weekdays": [0, 1, 2, 3, 4],
                             "holidays": ["2026-10-02"]}})
    assert market_state(utc(2026, 9, 30, 6, 30), cfg) == "market"    # Wed 12:00 IST
    assert market_state(utc(2026, 9, 30, 11, 0), cfg) == "after"     # Wed 16:30 IST
    assert market_state(utc(2026, 9, 26, 6, 30), cfg) == "weekend"   # Saturday
    assert market_state(utc(2026, 10, 2, 6, 30), cfg) == "weekend"   # listed holiday


def test_poll_interval_reads_each_state():
    sec = Config({"market_hours_interval_sec": 5, "after_market_interval_sec": 60, "weekend_interval_sec": 300})
    assert [poll_interval(sec, s) for s in ("market", "after", "weekend")] == [5, 60, 300]


def test_env_override_changes_a_setting(monkeypatch):
    monkeypatch.setenv("NSE_ALERTS__polling__nse__market_hours_interval_sec", "3")
    monkeypatch.setenv("NSE_ALERTS__summary__model", "some/other-model")
    cfg = load_config()
    assert cfg.get("polling.nse.market_hours_interval_sec") == 3
    assert cfg.get("summary.model") == "some/other-model"


def test_parse_nse_time_is_converted_from_ist_to_utc():
    assert parse_nse_time("30-Sep-2026 12:34:57") == utc(2026, 9, 30, 7, 4).replace(second=57)
    assert parse_nse_time("-") is None


ANNOUNCEMENT = {
    "an_dt": "30-Sep-2026 12:34:56", "attFileSize": "1.33 MB",
    "attchmntFile": "https://nsearchives.nseindia.com/corporate/X.pdf",
    "attchmntText": "Greenlam has informed the Exchange about Copy of Newspaper Publication",
    "desc": "Copy of Newspaper Publication", "exchdisstime": "30-Sep-2026 12:34:57",
    "seq_id": "106801958", "sm_name": "Greenlam Industries Limited", "symbol": "GREENLAM",
}


def test_announcement_uses_dissemination_time_as_listed_time():
    item = announcement_to_item(ANNOUNCEMENT)
    assert item.source_uid == "106801958" and item.symbol == "GREENLAM"
    assert (item.listed_at - item.company_time).total_seconds() == 1
    assert item.attachment_url.endswith("X.pdf")


def test_corporate_action_item_has_a_stable_key():
    row = {"symbol": "SAIL", "subject": "Dividend - Rs 2.35 Per Share", "exDate": "30-Sep-2026",
           "recDate": "30-Sep-2026", "faceVal": "10", "comp": "Steel Authority of India Limited", "bcStartDate": "-"}
    a, b = corporate_action_to_item(row), corporate_action_to_item(dict(row))
    assert a.source_uid == b.source_uid and a.source_uid.startswith("ca:")
    assert "Ex-date 30-Sep-2026" in a.detail and a.category == "corporate_action"


def test_technicals_from_a_steady_uptrend():
    idx = pd.date_range("2025-01-01", periods=260, freq="B")
    hist = pd.DataFrame({"Close": [100 + i for i in range(260)], "Volume": [1000] * 260}, index=idx)
    tech = compute_technicals(hist)
    assert tech["rsi14"] == 100.0 and tech["rsi_zone"] == "overbought"
    assert tech["vs_sma50"] == "above" and tech["vs_sma200"] == "above"
    assert tech["macd_trend"] == "bullish" and tech["pct_below_52w_high"] == 0
    assert tech["return_1m_pct"] > 0 and tech["volume_vs_avg"] == 1.0


def test_technicals_with_no_history_is_empty():
    assert compute_technicals(pd.DataFrame()) == {}


def test_fundamentals_keep_only_fields_that_exist():
    out = extract_fundamentals({"trailingPE": 21.87, "marketCap": 1.6e13, "returnOnEquity": None, "sector": "Energy"})
    assert out == {"market_cap_cr": 1600000.0, "pe_trailing": 21.87, "sector": "Energy"}


def test_news_handles_the_nested_yahoo_shape():
    raw = [{"content": {"title": "Results beat estimates", "provider": {"displayName": "Wire"}, "pubDate": "2026-09-30"}},
           {"content": {}}]
    assert extract_news(raw, 3) == [{"title": "Results beat estimates", "publisher": "Wire", "time": "2026-09-30"}]


def test_trim_words_ends_on_a_sentence():
    text = " ".join(["Word"] * 9 + ["end."]) + " " + " ".join(["more"] * 30)
    trimmed = trim_words(text, 12)
    assert trimmed.endswith("end.") and len(trimmed.split()) <= 12
    assert trim_words("short text", 50) == "short text"


def test_template_summary_uses_cached_numbers():
    item = {"subject": "Dividend - Rs 2.35 Per Share", "detail": "Ex-date 30-Sep-2026"}
    ctx = {"technicals": {"last_close": 120.5, "day_change_pct": 1.2, "rsi14": 55.0, "vs_sma200": "above"},
           "fundamentals": {"pe_trailing": 9.8}}
    text = template_summary(item, ctx)
    assert "Dividend - Rs 2.35" in text and "Last close Rs 120.5" in text and "P/E 9.8" in text


def test_message_text_is_escaped_and_has_the_footer():
    event = {"category": "announcement", "symbol": "TCS", "subject": "Results <Q2>", "source": "nse",
             "listed_at": utc(2026, 9, 30, 7, 4), "detected_at": utc(2026, 9, 30, 7, 4),
             "attachment_url": "https://x/y.pdf"}
    rich, plain = build_text(event, "Profit rose & margins held.")
    assert "&lt;Q2&gt;" in rich and "&amp;" in rich and "<a href=" in rich
    assert "Reply STOP" in plain and plain.startswith("ANNOUNCEMENT | TCS")


def test_postgres_schema_converts_for_sqlite():
    sql = sqlite_schema("create table t (id bigserial primary key, at timestamptz not null default now());")
    assert "integer primary key autoincrement" in sql and "timestamptz" not in sql and "CURRENT_TIMESTAMP" in sql
    assert normalise_url("postgres://u:p@h/db?sslmode=require").startswith("postgresql+psycopg://u:p@h/db")


def _openrouter(handler, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    summarizer = Summarizer()
    summarizer.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return summarizer


async def test_summarizer_sends_the_configured_model_and_cleans_the_answer(store, monkeypatch):
    seen = {}

    def handler(request):
        import json
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "**Profit** rose 10%."}}]})

    result = await _openrouter(handler, monkeypatch).summarise(
        {"symbol": "TCS", "subject": "Results"}, {"technicals": {"rsi14": 50}}, "filing words", store.cfg)
    assert seen["body"]["model"] == store.cfg.get("summary.model") and seen["auth"] == "Bearer test-key"
    assert result.text == "Profit rose 10%." and result.model == seen["body"]["model"]


async def test_summarizer_falls_back_to_the_second_model(store, monkeypatch):
    monkeypatch.setenv("NSE_ALERTS__summary__fallback_model", "backup/model")
    from nse_alerts.config import ConfigStore
    calls = []

    def handler(request):
        import json
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model != "backup/model":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"choices": [{"message": {"content": "Backup answer."}}]})

    cfg = ConfigStore(store.path).cfg
    result = await _openrouter(handler, monkeypatch).summarise({"symbol": "TCS"}, None, "", cfg)
    assert result.text == "Backup answer." and calls[-1] == "backup/model" and len(calls) == 2


async def test_summarizer_without_a_key_raises(store, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(SummaryError):
        await Summarizer().summarise({"symbol": "TCS"}, None, "", store.cfg)
