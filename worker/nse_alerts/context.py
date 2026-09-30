"""Fundamentals and technicals for every tracked stock, refreshed in the background.

The alert path only reads the cache, so the context step costs almost no time.
Data comes from Yahoo Finance via the yfinance library (NSE symbols use the .NS suffix).
Yahoo is an unofficial source: fields can be missing, so every value is optional.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .config import Config
from .db import Database, as_dt, utcnow
from .market import market_state
from .universe import Universe

log = logging.getLogger(__name__)


def _num(value: Any, digits: int = 2) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, digits)


def rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) <= period:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    last_gain, last_loss = gain.iloc[-1], loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    return _num(100 - 100 / (1 + last_gain / last_loss), 1)


def compute_technicals(hist: pd.DataFrame) -> dict[str, Any]:
    """Price-based indicators from a daily OHLCV frame (oldest first)."""
    if hist is None or hist.empty or "Close" not in hist:
        return {}
    close = hist["Close"].dropna()
    if close.empty:
        return {}
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2]) if len(close) > 1 else None

    def sma(n: int) -> float | None:
        return _num(close.tail(n).mean()) if len(close) >= n else None

    def ret(n: int) -> float | None:
        return _num((last / float(close.iloc[-n - 1]) - 1) * 100) if len(close) > n else None

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    window = close.tail(252)
    hi, lo = float(window.max()), float(window.min())
    sma50, sma200 = sma(50), sma(200)
    volume = hist["Volume"].dropna() if "Volume" in hist else pd.Series(dtype=float)
    avg_vol = float(volume.tail(20).mean()) if len(volume) >= 5 else None

    tech: dict[str, Any] = {
        "last_close": _num(last),
        "day_change_pct": _num((last / prev - 1) * 100) if prev else None,
        "sma20": sma(20),
        "sma50": sma50,
        "sma200": sma200,
        "rsi14": rsi(close),
        "macd": _num(macd.iloc[-1], 3),
        "macd_signal": _num(signal.iloc[-1], 3),
        "macd_trend": "bullish" if macd.iloc[-1] > signal.iloc[-1] else "bearish",
        "high_52w": _num(hi),
        "low_52w": _num(lo),
        "pct_below_52w_high": _num((1 - last / hi) * 100),
        "pct_above_52w_low": _num((last / lo - 1) * 100),
        "return_1m_pct": ret(21),
        "return_3m_pct": ret(63),
        "return_6m_pct": ret(126),
        "return_1y_pct": ret(252),
        "avg_volume_20d": _num(avg_vol, 0),
        "volume_vs_avg": _num(float(volume.iloc[-1]) / avg_vol) if avg_vol and len(volume) else None,
        "as_of": str(close.index[-1])[:10],
    }
    if sma50 is not None:
        tech["vs_sma50"] = "above" if last > sma50 else "below"
    if sma200 is not None:
        tech["vs_sma200"] = "above" if last > sma200 else "below"
    rsi14 = tech["rsi14"]
    if rsi14 is not None:
        tech["rsi_zone"] = "overbought" if rsi14 >= 70 else "oversold" if rsi14 <= 30 else "neutral"
    return tech


FUNDAMENTAL_FIELDS = {
    "market_cap_cr": ("marketCap", lambda v: _num(v / 1e7, 0) if v else None),
    "pe_trailing": ("trailingPE", _num),
    "pe_forward": ("forwardPE", _num),
    "price_to_book": ("priceToBook", _num),
    "dividend_yield_pct": ("dividendYield", _num),
    "roe": ("returnOnEquity", lambda v: _num(v * 100, 1) if v is not None else None),
    "debt_to_equity": ("debtToEquity", _num),
    "profit_margin_pct": ("profitMargins", lambda v: _num(v * 100, 1) if v is not None else None),
    "revenue_growth_pct": ("revenueGrowth", lambda v: _num(v * 100, 1) if v is not None else None),
    "earnings_growth_pct": ("earningsGrowth", lambda v: _num(v * 100, 1) if v is not None else None),
    "beta": ("beta", _num),
    "analyst_target": ("targetMeanPrice", _num),
    "analyst_view": ("recommendationKey", lambda v: v if isinstance(v, str) else None),
    "sector": ("sector", lambda v: v if isinstance(v, str) else None),
}


def extract_fundamentals(info: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, (key, fn) in FUNDAMENTAL_FIELDS.items():
        value = info.get(key)
        if value is None:
            continue
        try:
            parsed = fn(value)
        except Exception:
            parsed = None
        if parsed is not None:
            out[name] = parsed
    return out


def extract_news(raw_news: list[dict] | None, limit: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for entry in raw_news or []:
        content = entry.get("content") or entry
        title = content.get("title")
        if not title:
            continue
        provider = content.get("provider") or {}
        out.append(
            {
                "title": title,
                "publisher": provider.get("displayName") or content.get("publisher") or "",
                "time": str(content.get("pubDate") or content.get("providerPublishTime") or ""),
            }
        )
        if len(out) >= limit:
            break
    return out


def fetch_symbol(symbol: str, period: str, news_items: int) -> dict[str, Any]:
    """Blocking yfinance calls for one stock (run in a worker thread)."""
    import yfinance as yf

    ticker = yf.Ticker(f"{symbol}.NS")
    hist = ticker.history(period=period, auto_adjust=False)
    data: dict[str, Any] = {"symbol": symbol, "technicals": compute_technicals(hist)}
    try:
        data["fundamentals"] = extract_fundamentals(ticker.info or {})
    except Exception as exc:
        log.warning("fundamentals unavailable for %s: %s", symbol, exc)
        data["fundamentals"] = {}
    try:
        data["news"] = extract_news(ticker.news, news_items)
    except Exception:
        data["news"] = []
    data["fetched_at"] = utcnow().isoformat()
    return data


class ContextStore:
    def __init__(self, db: Database, universe: Universe):
        self.db = db
        self.universe = universe
        self._cache: dict[str, dict[str, Any]] = {}
        self._updated: dict[str, datetime] = {}

    async def load_from_db(self) -> None:
        for row in await self.db.fetch_all("select symbol, data, updated_at from context_cache"):
            try:
                self._cache[row["symbol"]] = json.loads(row["data"])
                self._updated[row["symbol"]] = as_dt(row["updated_at"])
            except (ValueError, TypeError):
                continue

    def get(self, symbol: str, max_age_sec: float | None = None) -> dict[str, Any] | None:
        data = self._cache.get(symbol)
        if data is None:
            return None
        if max_age_sec is not None:
            age = (utcnow() - self._updated[symbol]).total_seconds()
            if age > max_age_sec:
                return None
        return data

    async def put(self, symbol: str, data: dict[str, Any]) -> None:
        now = utcnow()
        self._cache[symbol] = data
        self._updated[symbol] = now
        await self.db.execute(
            "insert into context_cache (symbol, data, updated_at) values (:s, :d, :now) "
            "on conflict (symbol) do update set data = excluded.data, updated_at = excluded.updated_at",
            s=symbol, d=json.dumps(data), now=now,
        )

    async def refresh(self, cfg: Config, symbols: list[str] | None = None) -> int:
        ctx_cfg = cfg.section("context")
        period = ctx_cfg.get("history_period", "1y")
        news_items = int(ctx_cfg.get("news_items", 3))
        sem = asyncio.Semaphore(int(ctx_cfg.get("concurrency", 4)))
        ok = 0

        async def one(symbol: str) -> None:
            nonlocal ok
            async with sem:
                try:
                    data = await asyncio.to_thread(fetch_symbol, symbol, period, news_items)
                    if data.get("technicals") or data.get("fundamentals"):
                        await self.put(symbol, data)
                        ok += 1
                except Exception as exc:
                    log.warning("context refresh failed for %s: %s", symbol, exc)

        await asyncio.gather(*(one(s) for s in (symbols or sorted(self.universe.symbols))))
        return ok

    async def run(self, store) -> None:
        """Background loop: refresh all stocks, sleeping longer outside market hours."""
        await self.load_from_db()
        while True:
            cfg = store.cfg
            if cfg.get("context.enabled", True):
                started = datetime.now(timezone.utc)
                ok = await self.refresh(cfg)
                log.info("context refreshed for %d/%d stocks in %.0fs", ok, len(self.universe.symbols),
                         (datetime.now(timezone.utc) - started).total_seconds())
                await self.db.set_status("context_last_refresh", json.dumps({"ok": ok, "at": utcnow().isoformat()}))
            state = market_state(utcnow(), cfg)
            key = "refresh_market_hours_sec" if state == "market" else "refresh_after_market_sec"
            await asyncio.sleep(float(cfg.get(f"context.{key}", 600)))


def compact_context(data: dict[str, Any] | None) -> dict[str, Any]:
    """Shape passed to the language model: only fields that exist."""
    if not data:
        return {}
    return {
        "fundamentals": data.get("fundamentals", {}),
        "technicals": data.get("technicals", {}),
        "recent_news": data.get("news", []),
    }
