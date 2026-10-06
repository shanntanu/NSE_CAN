from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from ..config import Config
from .base import Item, SourceError

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

TIME_FORMATS = ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y")


def parse_nse_time(value: str | None) -> datetime | None:
    if not value or value in ("-", "null"):
        return None
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def announcement_to_item(row: dict) -> Item:
    company_time = parse_nse_time(row.get("an_dt") or row.get("sort_date"))
    listed = parse_nse_time(row.get("exchdisstime")) or company_time
    uid = str(row.get("seq_id") or row.get("dt") or row.get("an_dt"))
    return Item(
        source="nse",
        source_uid=uid,
        category="announcement",
        symbol=(row.get("symbol") or "").strip(),
        company=(row.get("sm_name") or "").strip(),
        subject=(row.get("desc") or "").strip(),
        detail=(row.get("attchmntText") or "").strip(),
        attachment_url=(row.get("attchmntFile") or "").strip(),
        attachment_size=(row.get("attFileSize") or row.get("fileSize") or "").strip(),
        listed_at=listed,
        company_time=company_time,
        raw=row,
    )


def corporate_action_key(row: dict) -> tuple[str, str, str, str]:
    return (
        (row.get("symbol") or "").strip(),
        (row.get("subject") or "").strip(),
        (row.get("exDate") or "-").strip(),
        (row.get("recDate") or "-").strip(),
    )


def corporate_action_to_item(row: dict) -> Item:
    symbol, subject, ex_date, rec_date = corporate_action_key(row)
    uid = "ca:" + hashlib.sha1("|".join((symbol, subject, ex_date, rec_date)).encode()).hexdigest()[:16]
    parts = [f"Ex-date {ex_date}", f"record date {rec_date}"]
    face = (row.get("faceVal") or "").strip()
    if face and face != "-":
        parts.append(f"face value Rs {face}")
    bc = row.get("bcStartDate")
    if bc and bc != "-":
        parts.append(f"book closure {bc} to {row.get('bcEndDate', '-')}")
    return Item(
        source="nse",
        source_uid=uid,
        category="corporate_action",
        symbol=symbol,
        company=(row.get("comp") or "").strip(),
        subject=subject,
        detail=", ".join(parts),
        listed_at=parse_nse_time(row.get("caBroadcastDate")),
        raw=row,
        extra={
            "isin": row.get("isin"),
            "series": row.get("series"),
            "ex_date": ex_date,
            "record_date": rec_date,
            "bc_start": row.get("bcStartDate"),
            "bc_end": row.get("bcEndDate"),
            "nd_start": row.get("ndStartDate"),
            "nd_end": row.get("ndEndDate"),
            "face_value": row.get("faceVal"),
        },
    )


class NseClient:
    ANNOUNCEMENTS = "/api/corporate-announcements?index=equities"
    ACTIONS = "/api/corporates-corporateActions?index=equities"

    def __init__(self, cfg: Config):
        proxy_env = cfg.get("polling.nse.proxy_url_env", "NSE_PROXY_URL")
        proxy = os.environ.get(proxy_env, "").strip() or None
        self.base = cfg.get("polling.nse.base_url", "https://www.nseindia.com").rstrip("/")
        self.client = httpx.AsyncClient(
            base_url=self.base,
            proxy=proxy,
            follow_redirects=True,
            headers={
                "User-Agent": cfg.get("polling.nse.user_agent"),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": self.base + "/",
            },
        )

    async def _get_json(self, path: str, timeout: float) -> list[dict]:
        for attempt in (1, 2):
            try:
                resp = await self.client.get(path, timeout=timeout)
            except httpx.HTTPError as exc:
                raise SourceError(f"NSE request failed: {exc.__class__.__name__}: {exc}") from exc
            if resp.status_code in (401, 403) and attempt == 1:
                try:  # refresh cookies once, the API sometimes wants them
                    await self.client.get("/", timeout=timeout)
                except httpx.HTTPError:
                    pass
                continue
            if resp.status_code != 200:
                raise SourceError(f"NSE answered HTTP {resp.status_code} for {path}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise SourceError("NSE answered with something that is not JSON") from exc
            if isinstance(data, dict):
                data = data.get("data", [])
            return data
        raise SourceError(f"NSE blocked the request for {path}")

    async def announcements(self, timeout: float = 8) -> list[Item]:
        return [announcement_to_item(r) for r in await self._get_json(self.ANNOUNCEMENTS, timeout)]

    async def announcements_for_day(self, day: datetime, timeout: float = 12) -> list[Item]:
        d = day.astimezone(IST).strftime("%d-%m-%Y")
        path = f"{self.ANNOUNCEMENTS}&from_date={d}&to_date={d}"
        return [announcement_to_item(r) for r in await self._get_json(path, timeout)]

    async def announcements_for_symbol(self, symbol: str, timeout: float = 60) -> list[Item]:
        """The full announcement history of one stock (several thousand items, about 3 MB)."""
        path = f"{self.ANNOUNCEMENTS}&symbol={symbol}"
        return [announcement_to_item(r) for r in await self._get_json(path, timeout)]

    async def corporate_actions(self, timeout: float = 8) -> list[Item]:
        return [corporate_action_to_item(r) for r in await self._get_json(self.ACTIONS, timeout)]

    async def close(self) -> None:
        await self.client.aclose()
