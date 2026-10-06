"""Find past news like today's and total up what a ₹10,000 buy would have done afterwards."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import Config
from ..db import Database, as_dt, utcnow
from ..messages import IST
from .prices import pnl


@dataclass
class Case:
    symbol: str
    listed_at: datetime
    subject: str
    sentiment: int
    ret_15d_pct: float | None
    ret_30d_pct: float | None
    nifty_15d_pct: float | None
    nifty_30d_pct: float | None


@dataclass
class HorizonStats:
    n: int = 0
    wins: int = 0
    avg_pnl: float | None = None
    median_pnl: float | None = None
    best_pnl: float | None = None
    worst_pnl: float | None = None
    nifty_avg_pnl: float | None = None


@dataclass
class SignalReport:
    topic: str
    sentiment: int
    confidence: float
    scope: str                      # "stock" | "nifty50" | "none"
    symbol: str
    low: int
    high: int
    lookback_years: int
    amount: float
    cases: list[Case] = field(default_factory=list)
    d15: HorizonStats = field(default_factory=HorizonStats)
    d30: HorizonStats = field(default_factory=HorizonStats)
    enough: bool = False            # False when there are too few cases to lean on
    low_information: bool = False   # the text was too generic to score


def band(sentiment: int, cfg: Config) -> tuple[int, int]:
    tol = float(cfg.get("signals.sentiment_tolerance", 10))
    if cfg.get("signals.tolerance_mode", "points") == "percent":
        tol = sentiment * tol / 100
    return max(1, int(round(sentiment - tol))), min(100, int(round(sentiment + tol)))


def thin_clusters(cases: list[Case], cooldown_days: int) -> list[Case]:
    """Keep the first of any run of same-stock cases closer than `cooldown_days`, so one move is not counted twice."""
    kept: list[Case] = []
    last: dict[str, datetime] = {}
    for c in sorted(cases, key=lambda c: c.listed_at):
        prev = last.get(c.symbol)
        if prev is not None and c.listed_at - prev < timedelta(days=cooldown_days):
            continue
        kept.append(c)
        last[c.symbol] = c.listed_at
    return kept


def horizon_stats(cases: list[Case], attr: str, nifty_attr: str, amount: float) -> HorizonStats:
    pairs = [(getattr(c, attr), getattr(c, nifty_attr)) for c in cases if getattr(c, attr) is not None]
    if not pairs:
        return HorizonStats()
    pnls = [pnl(amount, r) for r, _ in pairs]
    nifty = [pnl(amount, n) for _, n in pairs if n is not None]
    return HorizonStats(
        n=len(pnls),
        wins=sum(1 for p in pnls if p > 0),
        avg_pnl=statistics.fmean(pnls),
        median_pnl=statistics.median(pnls),
        best_pnl=max(pnls),
        worst_pnl=min(pnls),
        nifty_avg_pnl=statistics.fmean(nifty) if nifty else None,
    )


def _case(row: dict[str, Any]) -> Case:
    return Case(row["symbol"], as_dt(row["listed_at"]), row["subject"] or "", int(row["sentiment"]),
                row["ret_15d_pct"], row["ret_30d_pct"], row["nifty_15d_pct"], row["nifty_30d_pct"])


async def fetch_cases(db: Database, topic: str, low: int, high: int, since: datetime, before: datetime,
                      min_conf: float, symbol: str | None, exclude_uid: str | None) -> list[Case]:
    sql = (
        "select symbol, listed_at, subject, sentiment, ret_15d_pct, ret_30d_pct, nifty_15d_pct, nifty_30d_pct "
        "from event_signals where topic = :topic and sentiment between :lo and :hi and returns_status = 'done' "
        "and confidence >= :conf and listed_at >= :since and listed_at < :before"
    )
    params: dict[str, Any] = dict(topic=topic, lo=low, hi=high, conf=min_conf, since=since, before=before)
    if symbol:
        sql += " and symbol = :symbol"
        params["symbol"] = symbol
    if exclude_uid:
        sql += " and source_uid <> :uid"
        params["uid"] = exclude_uid
    return [_case(r) for r in await db.fetch_all(sql + " order by listed_at", **params)]


async def build_report(db: Database, cfg: Config, symbol: str, topic: str, sentiment: int, confidence: float,
                       listed_at: datetime, source_uid: str | None = None) -> SignalReport:
    s = cfg.section("signals")
    years = int(s.get("lookback_years", 5))
    amount = float(s.get("amount", 10000))
    low, high = band(sentiment, cfg)
    report = SignalReport(topic, sentiment, confidence, "none", symbol, low, high, years, amount)
    min_conf = float(s.get("min_confidence", 0.5))
    if confidence < min_conf:
        report.low_information = True
        return report

    since = listed_at - timedelta(days=365 * years)
    cooldown = int(s.get("cooldown_days", 15))
    minimum = int(s.get("min_cases", 5))
    cases = thin_clusters(await fetch_cases(db, topic, low, high, since, listed_at, min_conf, symbol, source_uid),
                          cooldown)
    report.scope = "stock"
    if len(cases) < minimum and s.get("fallback_to_nifty50", True):
        wide = thin_clusters(await fetch_cases(db, topic, low, high, since, listed_at, min_conf, None, source_uid),
                             cooldown)
        if len(wide) > len(cases):
            cases, report.scope = wide, "nifty50"
    if not cases:
        report.scope = "none"
    report.cases = cases
    report.d15 = horizon_stats(cases, "ret_15d_pct", "nifty_15d_pct", amount)
    report.d30 = horizon_stats(cases, "ret_30d_pct", "nifty_30d_pct", amount)
    report.enough = len(cases) >= minimum
    return report


# ---- message text ---------------------------------------------------------------------------

def _rs(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value >= 0 else "-"
    return f"{sign}₹{abs(value):,.0f}"


def _line(label: str, h: HorizonStats) -> str:
    if h.n == 0:
        return f"{label}: no completed cases"
    return f"{label}: avg {_rs(h.avg_pnl)}, {h.wins} of {h.n} gained"


def signal_block(report: SignalReport) -> str:
    """Three short lines for the alert: how many similar past cases, and ₹10,000 outcomes at 15 and 30 days."""
    label = report.topic.replace("_", " ")
    if report.low_information:
        return ""
    if not report.cases:
        return f"Past signals: no comparable past cases found for this type of news ({label})."
    where = f"{report.symbol}" if report.scope == "stock" else "Nifty 50 stocks"
    head = (f"Past signals ({where}, {label} news, sentiment {report.sentiment}, "
            f"{len(report.cases)} case{'s' if len(report.cases) != 1 else ''} in {report.lookback_years} yrs)")
    lines = [head + ":", f"If ₹{report.amount:,.0f} was invested after each:",
             _line("15 days", report.d15), _line("30 days", report.d30)]
    if report.d30.nifty_avg_pnl is not None or report.d15.nifty_avg_pnl is not None:
        lines.append(f"Nifty 50 over the same periods: {_rs(report.d15.nifty_avg_pnl)} | {_rs(report.d30.nifty_avg_pnl)}")
    if not report.enough:
        lines.append("Few cases: treat this as a hint, not a pattern.")
    return "\n".join(lines)


def signal_details(report: SignalReport, limit: int = 12) -> str:
    """The case-by-case list sent as a second message."""
    if not report.cases:
        return ""
    amount = report.amount
    rows = [f"Past cases: {report.symbol if report.scope == 'stock' else 'Nifty 50 stocks'}, "
            f"{report.topic.replace('_', ' ')}, sentiment {report.low}-{report.high}",
            f"Result of ₹{amount:,.0f} bought after the news (15 days | 30 days):"]
    for c in sorted(report.cases, key=lambda c: c.listed_at, reverse=True)[:limit]:
        when = c.listed_at.astimezone(IST).strftime("%d %b %Y")
        rows.append(f"• {when} {c.symbol} (score {c.sentiment}): {_rs(pnl(amount, c.ret_15d_pct))} | "
                    f"{_rs(pnl(amount, c.ret_30d_pct))}")
    if len(report.cases) > limit:
        rows.append(f"... and {len(report.cases) - limit} older")
    rows.append("Past results do not predict future returns. Prices are daily and adjusted; no costs or taxes. "
                "Informational only, not investment advice.")
    return "\n".join(rows)
