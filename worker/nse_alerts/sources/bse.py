from __future__ import annotations

import csv
import difflib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from ..config import Config
from ..universe import Universe
from .base import Item, SourceError

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

API = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?pageno=1&strCat=-1&strPrevDate={d}&strScrip=&strSearch=P&strToDate={d}&strType=C"
)
ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"


def _norm_name(name: str) -> str:
    name = re.sub(r"\b(ltd|limited|corporation|corp|co|company|india|pvt|the)\b\.?", " ", name.lower())
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def parse_bse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


class BseClient:
    """BSE announcements. Built but switched off by default (BSE blocks cloud addresses)."""

    def __init__(self, cfg: Config, universe: Universe):
        proxy = os.environ.get(cfg.get("polling.bse.proxy_url_env", "BSE_PROXY_URL"), "").strip() or None
        self.client = httpx.AsyncClient(
            proxy=proxy,
            follow_redirects=True,
            headers={
                "User-Agent": cfg.get("polling.nse.user_agent"),
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.bseindia.com/",
                "Origin": "https://www.bseindia.com",
            },
        )
        self.universe = universe
        self.scrip_map: dict[str, str] = {}
        path = cfg.get("polling.bse.scrip_map_file", "")
        if path and Path(path).exists():
            with open(path, encoding="utf-8-sig", newline="") as fh:
                self.scrip_map = {r["scrip_code"].strip(): r["symbol"].strip() for r in csv.DictReader(fh)}
        self._names = {_norm_name(s.company): s.symbol for s in universe.stocks}

    def symbol_for(self, scrip_code: str, long_name: str) -> str | None:
        if scrip_code in self.scrip_map:
            return self.scrip_map[scrip_code]
        norm = _norm_name(long_name)
        if norm in self._names:
            return self._names[norm]
        match = difflib.get_close_matches(norm, list(self._names), n=1, cutoff=0.92)
        return self._names[match[0]] if match else None

    async def announcements(self, now: datetime, timeout: float = 8) -> list[Item]:
        day = now.astimezone(IST).strftime("%Y%m%d")
        try:
            resp = await self.client.get(API.format(d=day), timeout=timeout)
        except httpx.HTTPError as exc:
            raise SourceError(f"BSE request failed: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise SourceError(f"BSE answered HTTP {resp.status_code}")
        try:
            rows = resp.json().get("Table", [])
        except ValueError as exc:
            raise SourceError("BSE answered with something that is not JSON") from exc
        items: list[Item] = []
        for row in rows:
            symbol = self.symbol_for(str(row.get("SCRIP_CD", "")), row.get("SLONGNAME", ""))
            if not symbol:
                continue
            attach = (row.get("ATTACHMENTNAME") or "").strip()
            items.append(
                Item(
                    source="bse",
                    source_uid=str(row.get("NEWSID") or f"{row.get('SCRIP_CD')}:{row.get('NEWS_DT')}"),
                    category="announcement",
                    symbol=symbol,
                    company=row.get("SLONGNAME", ""),
                    subject=(row.get("CATEGORYNAME") or row.get("SUBCATNAME") or "").strip(),
                    detail=(row.get("HEADLINE") or row.get("NEWSSUB") or "").strip(),
                    attachment_url=ATTACH.format(name=attach) if attach else "",
                    listed_at=parse_bse_time(row.get("DissemDT") or row.get("NEWS_DT")),
                    company_time=parse_bse_time(row.get("NEWS_DT")),
                    raw=row,
                )
            )
        return items

    @staticmethod
    def looks_duplicate(subject_a: str, subject_b: str) -> bool:
        return difflib.SequenceMatcher(None, subject_a.lower(), subject_b.lower()).ratio() >= 0.6

    async def close(self) -> None:
        await self.client.aclose()
