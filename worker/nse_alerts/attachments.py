from __future__ import annotations

import asyncio
import io
import logging
import os
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import httpx

from .config import Config

log = logging.getLogger(__name__)


@dataclass
class Attachment:
    url: str
    filename: str
    data: bytes | None = None       # None when it was too large or could not be fetched
    too_large: bool = False
    text: str = ""                  # extracted filing text, used by the summary

    @property
    def sendable(self) -> bool:
        return self.data is not None


def filename_from_url(url: str) -> str:
    name = os.path.basename(unquote(urlparse(url).path)) or "attachment"
    return name


def extract_pdf_text(data: bytes, max_pages: int, max_chars: int) -> str:
    """Blocking: read the first pages of a PDF. Returns '' when it is not readable text."""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        total = 0
        for page in reader.pages[:max_pages]:
            chunk = (page.extract_text() or "").strip()
            if chunk:
                parts.append(chunk)
                total += len(chunk)
            if total >= max_chars:
                break
        return " ".join(" ".join(parts).split())[:max_chars]
    except Exception as exc:
        log.info("could not read pdf text: %s", exc)
        return ""


class AttachmentFetcher:
    def __init__(self, cfg: Config):
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            headers={
                "User-Agent": cfg.get("polling.nse.user_agent"),
                "Referer": cfg.get("polling.nse.base_url", "https://www.nseindia.com") + "/",
            },
        )

    async def fetch(self, url: str, cfg: Config) -> Attachment:
        att_cfg = cfg.section("attachments")
        att = Attachment(url=url, filename=filename_from_url(url))
        limit = float(att_cfg.get("max_size_mb", 45)) * 1024 * 1024
        try:
            async with self.client.stream("GET", url, timeout=float(att_cfg.get("fetch_timeout_sec", 8))) as resp:
                if resp.status_code != 200:
                    log.info("attachment %s answered HTTP %s", url, resp.status_code)
                    return att
                declared = int(resp.headers.get("content-length", 0) or 0)
                if declared > limit:
                    att.too_large = True
                    return att
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > limit:
                        att.too_large = True
                        return att
                att.data = bytes(buf)
        except httpx.HTTPError as exc:
            log.info("attachment fetch failed for %s: %s", url, exc.__class__.__name__)
        return att

    async def read_text(self, att: Attachment, cfg: Config) -> str:
        att_cfg = cfg.section("attachments")
        if not att.data or not att_cfg.get("extract_text", True):
            return ""
        if not att.filename.lower().endswith(".pdf") and not att.data.startswith(b"%PDF"):
            return ""
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    extract_pdf_text, att.data, int(att_cfg.get("max_pages", 12)), int(att_cfg.get("max_chars", 9000))
                ),
                timeout=float(att_cfg.get("text_timeout_sec", 6)),
            )
        except asyncio.TimeoutError:
            return ""

    async def close(self) -> None:
        await self.client.aclose()
