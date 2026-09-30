from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Item:
    """One announcement, news item or corporate action, in a source-neutral shape."""

    source: str
    source_uid: str
    category: str                      # announcement | corporate_action | news
    symbol: str
    company: str = ""
    subject: str = ""
    detail: str = ""
    attachment_url: str = ""
    attachment_size: str = ""
    listed_at: datetime | None = None  # when the exchange disseminated it
    company_time: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class SourceError(Exception):
    pass
