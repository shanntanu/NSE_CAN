from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

LABELS = {
    "announcement": "ANNOUNCEMENT",
    "corporate_action": "CORPORATE ACTION",
    "news": "NEWS",
    "follow_up": "UPDATE",
}
FOOTER = "Informational only, not investment advice. Reply STOP to unsubscribe."
MAX_LEN = 4000


def ist_clock(value: datetime | None) -> str:
    return value.astimezone(IST).strftime("%d %b %H:%M:%S IST") if value else ""


def fmt_price(value: float) -> str:
    return f"₹{value:,.2f}"


def fmt_volume(value: int | None) -> str:
    if value is None:
        return "n/a"
    if value >= 10_000_000:
        return f"{value / 10_000_000:.2f} crore"
    if value >= 100_000:
        return f"{value / 100_000:.2f} lakh"
    return f"{value:,}"


def market_line(quote, since_alert_pct: float | None = None) -> str:
    """One line with the latest price and volume, e.g. 'Price ₹2,065.70 (-0.45% today) | Volume 12.30 lakh'."""
    if quote is None:
        return ""
    change = ""
    if since_alert_pct is not None:
        change = f" ({since_alert_pct:+.2f}% since alert)"
    elif quote.day_change_pct is not None:
        change = f" ({quote.day_change_pct:+.2f}% today)"
    return (
        f"Price {fmt_price(quote.price)}{change} | Volume {fmt_volume(quote.volume)}"
        f" | as of {quote.as_of.astimezone(IST).strftime('%H:%M IST')}"
    )


def _assemble(event: dict, head: str, body: str, market: str, extra: str = "") -> tuple[str, str]:
    symbol = event["symbol"]
    subject = (event.get("subject") or "").strip()
    listed = ist_clock(event.get("listed_at")) or ist_clock(event.get("detected_at"))
    url = event.get("attachment_url") or ""

    plain = [f"{head} | {symbol}"]
    rich = [f"<b>{html.escape(head)} | {html.escape(symbol)}</b>"]
    if subject:
        plain.append(subject)
        rich.append(html.escape(subject))
    plain += ["", body]
    rich += ["", html.escape(body)]
    if market:
        plain += ["", market]
        rich += ["", f"<b>{html.escape(market)}</b>"]
    if extra:
        plain += ["", extra]
        rich += ["", html.escape(extra)]
    plain.append("")
    rich.append("")
    source_line = f"Source: {event['source'].upper()}" + (f", listed {listed}" if listed else "")
    if event["category"] == "follow_up":
        source_line = "Follow-up to the alert above"
        url = ""
    plain.append(source_line + (f"\nFiling: {url}" if url else ""))
    rich.append(html.escape(source_line) + (f' | <a href="{html.escape(url)}">Filing</a>' if url else ""))
    plain.append(FOOTER)
    rich.append(html.escape(FOOTER))
    return "\n".join(rich)[:MAX_LEN], "\n".join(plain)[:MAX_LEN]


def build_text(event: dict, summary: str, quote=None, extra: str = "") -> tuple[str, str]:
    """Return (html, plain) versions of the alert: bullets, the price and volume line, then any extra block."""
    label = LABELS.get(event["category"], event["category"].upper())
    return _assemble(event, label, summary, market_line(quote), extra)


def build_follow_up_text(event: dict, summary: str, quote, since_alert_pct: float | None, verdict: str,
                         extra: str = "") -> tuple[str, str]:
    return _assemble(event, f"UPDATE ({verdict})", summary, market_line(quote, since_alert_pct), extra)
