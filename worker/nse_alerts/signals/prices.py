"""Daily prices and the return arithmetic: what ₹10,000 would have done 15 and 30 days after a piece of news."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from ..db import Database

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
INDEX_SYMBOL = "NIFTY50"
YAHOO_INDEX = "^NSEI"
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)


def fetch_history(ticker: str, years: int) -> pd.DataFrame:
    """Blocking: adjusted daily Open/Close (splits and dividends applied) from Yahoo Finance."""
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period=f"{years + 1}y", auto_adjust=True)
    if hist is None or hist.empty:
        return pd.DataFrame(columns=["open", "close"])
    frame = pd.DataFrame({"open": hist["Open"], "close": hist["Close"]})
    frame.index = pd.Index([d.date().isoformat() for d in hist.index], name="date")
    return frame.dropna()


class PriceStore:
    def __init__(self, db: Database):
        self.db = db
        self._cache: dict[str, pd.DataFrame] = {}

    async def download(self, symbol: str, years: int) -> int:
        ticker = YAHOO_INDEX if symbol == INDEX_SYMBOL else f"{symbol}.NS"
        frame = await asyncio.to_thread(fetch_history, ticker, years)
        for chunk_start in range(0, len(frame), 500):
            chunk = frame.iloc[chunk_start:chunk_start + 500]
            for d, row in chunk.iterrows():
                await self.db.execute(
                    "insert into daily_prices (symbol, trade_date, open, close) values (:s, :d, :o, :c) "
                    "on conflict (symbol, trade_date) do update set open = excluded.open, close = excluded.close",
                    s=symbol, d=d, o=float(row["open"]), c=float(row["close"]),
                )
        self._cache.pop(symbol, None)
        return len(frame)

    async def load(self, symbol: str) -> pd.DataFrame:
        if symbol not in self._cache:
            rows = await self.db.fetch_all(
                "select trade_date, open, close from daily_prices where symbol = :s order by trade_date", s=symbol)
            frame = pd.DataFrame(rows, columns=["trade_date", "open", "close"])
            self._cache[symbol] = frame.set_index("trade_date") if not frame.empty else frame
        return self._cache[symbol]

    def forget(self) -> None:
        self._cache.clear()


@dataclass
class Entry:
    date: str
    basis: str   # open | close


def entry_point(listed_at: datetime, prices: pd.DataFrame) -> Entry | None:
    """When a reader who saw the news right away could first have bought, at daily-price resolution.

    Listed before the open: buy that day's open. During the session: that day's close (a conservative
    stand-in for buying a little after the news). After the close, or no time recorded: next session's open.
    """
    if prices is None or prices.empty:
        return None
    local = listed_at.astimezone(IST)
    day = local.date().isoformat()
    clock = local.time()
    dates = prices.index
    if clock == time(0, 0):
        clock = time(23, 59)  # old records carry a date only: assume it came after the close
    if day in dates:
        if clock < MARKET_OPEN:
            return Entry(day, "open")
        if clock <= MARKET_CLOSE:
            return Entry(day, "close")
    pos = dates.searchsorted(day, side="right")
    return Entry(dates[pos], "open") if pos < len(dates) else None


def price_on(prices: pd.DataFrame, day: str, basis: str) -> float | None:
    if day not in prices.index:
        return None
    return float(prices.loc[day, basis])


def exit_close(prices: pd.DataFrame, entry_day: str, days: int) -> tuple[str, float] | None:
    """Close of the last session on or before entry_day + `days` calendar days; None if that is not here yet."""
    target = (date.fromisoformat(entry_day) + timedelta(days=days)).isoformat()
    if prices.index[-1] < target:
        return None  # the window has not finished
    pos = prices.index.searchsorted(target, side="right") - 1
    if pos < 0 or prices.index[pos] <= entry_day:
        return None
    last = prices.index[pos]
    return last, float(prices.loc[last, "close"])


@dataclass
class Returns:
    entry_date: str
    entry_basis: str
    entry_price: float
    ret_15d_pct: float | None
    ret_30d_pct: float | None
    nifty_15d_pct: float | None
    nifty_30d_pct: float | None

    @property
    def complete(self) -> bool:
        return self.ret_30d_pct is not None


def compute_returns(listed_at: datetime, stock: pd.DataFrame, index: pd.DataFrame | None,
                    horizons: tuple[int, int] = (15, 30)) -> Returns | None:
    entry = entry_point(listed_at, stock)
    if entry is None:
        return None
    buy = price_on(stock, entry.date, entry.basis)
    if buy is None or buy <= 0:
        return None
    index_buy = price_on(index, entry.date, entry.basis) if index is not None and not index.empty else None

    def one(days: int) -> tuple[float | None, float | None]:
        out = exit_close(stock, entry.date, days)
        if out is None:
            return None, None
        exit_day, sell = out
        stock_ret = (sell / buy - 1) * 100
        nifty_ret = None
        if index_buy and index is not None:
            index_sell = price_on(index, exit_day, "close")
            if index_sell:
                nifty_ret = (index_sell / index_buy - 1) * 100
        return stock_ret, nifty_ret

    r15, n15 = one(horizons[0])
    r30, n30 = one(horizons[1])
    return Returns(entry.date, entry.basis, buy, r15, r30, n15, n30)


def pnl(amount: float, pct: float | None) -> float | None:
    return None if pct is None else amount * pct / 100
