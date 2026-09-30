from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

LABELS = {
    "announcement": "ANNOUNCEMENT",
    "corporate_action": "CORPORATE ACTION",
    "news": "NEWS",
}
FOOTER = "Informational only, not investment advice. Reply STOP to unsubscribe."
MAX_LEN = 4000


def ist_clock(value: datetime | None) -> str:
    return value.astimezone(IST).strftime("%d %b %H:%M:%S IST") if value else ""


def build_text(event: dict, summary: str) -> tuple[str, str]:
    """Return (html, plain) versions of the alert text."""
    label = LABELS.get(event["category"], event["category"].upper())
    symbol = event["symbol"]
    subject = (event.get("subject") or "").strip()
    listed = ist_clock(event.get("listed_at")) or ist_clock(event.get("detected_at"))
    url = event.get("attachment_url") or ""

    plain = [f"{label} | {symbol}"]
    rich = [f"<b>{html.escape(label)} | {html.escape(symbol)}</b>"]
    if subject:
        plain.append(subject)
        rich.append(html.escape(subject))
    plain += ["", summary, ""]
    rich += ["", html.escape(summary), ""]
    source_line = f"Source: {event['source'].upper()}" + (f", listed {listed}" if listed else "")
    plain.append(source_line + (f"\nFiling: {url}" if url else ""))
    rich.append(html.escape(source_line) + (f' | <a href="{html.escape(url)}">Filing</a>' if url else ""))
    plain += [FOOTER]
    rich += [html.escape(FOOTER)]
    return "\n".join(rich)[:MAX_LEN], "\n".join(plain)[:MAX_LEN]
