from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from .config import Config


def _parse_hhmm(value: str) -> time:
    hour, minute = str(value).split(":")
    return time(int(hour), int(minute))


def market_state(now: datetime, cfg: Config) -> str:
    """Return 'market', 'after' or 'weekend' for the given moment."""
    tz = ZoneInfo(cfg.get("app.timezone", "Asia/Kolkata"))
    local = now.astimezone(tz)
    weekdays = set(cfg.get("market.weekdays", [0, 1, 2, 3, 4]))
    holidays = {str(d) for d in cfg.get("market.holidays", [])}
    if local.weekday() not in weekdays or local.date().isoformat() in holidays:
        return "weekend"
    opens = _parse_hhmm(cfg.get("market.open", "09:00"))
    closes = _parse_hhmm(cfg.get("market.close", "16:00"))
    return "market" if opens <= local.time() < closes else "after"


def poll_interval(section: Config, state: str) -> float:
    key = {
        "market": "market_hours_interval_sec",
        "after": "after_market_interval_sec",
        "weekend": "weekend_interval_sec",
    }[state]
    return float(section.get(key, 60))


def session_close(now: datetime, cfg: Config) -> datetime:
    """The closing time (UTC) of the trading day that `now` falls on."""
    tz = ZoneInfo(cfg.get("app.timezone", "Asia/Kolkata"))
    local = now.astimezone(tz)
    closes = _parse_hhmm(cfg.get("market.close", "16:00"))
    return datetime.combine(local.date(), closes, tzinfo=tz).astimezone(ZoneInfo("UTC"))
