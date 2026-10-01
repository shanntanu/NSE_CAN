"""Near-live price and volume from Yahoo Finance one-minute bars (about one minute behind the exchange)."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config

log = logging.getLogger(__name__)


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: float | None
    volume: int | None            # shares traded so far in the session
    as_of: datetime               # time of the latest one-minute bar

    @property
    def day_change_pct(self) -> float | None:
        if not self.prev_close:
            return None
        return (self.price / self.prev_close - 1) * 100


def fetch_quote(ticker: str, label: str | None = None) -> Quote | None:
    """Blocking: latest price, previous close and session volume for a Yahoo ticker."""
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="2d", interval="1m", auto_adjust=False)
    if hist is None or hist.empty:
        return None
    dates = hist.index.date
    last_day = dates[-1]
    today = hist[dates == last_day]
    before = hist[dates != last_day]
    closes = today["Close"].dropna()
    if closes.empty:
        return None
    prev = before["Close"].dropna()
    volume = int(today["Volume"].fillna(0).sum()) if "Volume" in today else None
    return Quote(
        symbol=label or ticker,
        price=float(closes.iloc[-1]),
        prev_close=float(prev.iloc[-1]) if not prev.empty else None,
        volume=volume or None,
        as_of=closes.index[-1].to_pydatetime().astimezone(timezone.utc),
    )


class QuoteService:
    async def _get(self, ticker: str, label: str, cfg: Config) -> Quote | None:
        q = cfg.section("quotes")
        if not q.get("enabled", True):
            return None
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fetch_quote, ticker, label), timeout=float(q.get("timeout_sec", 4))
            )
        except asyncio.TimeoutError:
            log.info("quote for %s timed out", ticker)
        except Exception as exc:
            log.info("quote for %s failed: %s", ticker, exc.__class__.__name__)
        return None

    async def stock(self, symbol: str, cfg: Config) -> Quote | None:
        return await self._get(f"{symbol}.NS", symbol, cfg)

    async def index(self, cfg: Config) -> Quote | None:
        ticker = cfg.get("quotes.index_symbol", "^NSEI")
        return await self._get(ticker, "NIFTY 50", cfg)
