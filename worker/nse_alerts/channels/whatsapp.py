from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from ..attachments import Attachment
from ..config import ConfigStore
from .base import Channel, ChannelError

log = logging.getLogger(__name__)


def template_text(value: str, limit: int = 900) -> str:
    """WhatsApp template parameters cannot contain newlines, tabs or long runs of spaces."""
    value = re.sub(r"\s*[\r\n\t]+\s*", " | ", value)
    value = re.sub(r" {4,}", "   ", value)
    return value[:limit]


class WhatsAppChannel(Channel):
    """WhatsApp Business Cloud API. Built but off until channels.whatsapp.enabled is true.

    Delivery, read and failure reports arrive on the web app's /api/whatsapp/webhook
    and are written to the deliveries table there.
    """

    name = "whatsapp"

    def __init__(self, store: ConfigStore):
        self.store = store
        self.client = httpx.AsyncClient(timeout=15)

    def _cfg(self):
        return self.store.cfg.section("channels.whatsapp")

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", False))

    def max_concurrency(self) -> int:
        return int(self._cfg().get("max_concurrency", 8))

    def eligible(self, sub: dict[str, Any]) -> bool:
        return bool(sub.get("whatsapp_opt_in")) and bool(sub.get("mobile"))

    def attachment_inline(self) -> bool:
        return bool(self._cfg().get("template_has_document_header", False))

    def _url(self, path: str) -> str:
        version = self._cfg().get("api_version", "v21.0")
        phone_id = self.store.cfg.secret("WHATSAPP_PHONE_NUMBER_ID")
        return f"https://graph.facebook.com/{version}/{phone_id}/{path}"

    def _headers(self) -> dict[str, str]:
        token = self.store.cfg.secret("WHATSAPP_TOKEN")
        if not token:
            raise ChannelError("WHATSAPP_TOKEN is not set", retryable=False)
        return {"Authorization": f"Bearer {token}"}

    async def _post(self, path: str, **kwargs) -> dict:
        try:
            resp = await self.client.post(self._url(path), headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            raise ChannelError(f"network: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            retryable = resp.status_code >= 500 or resp.status_code == 429
            raise ChannelError(f"HTTP {resp.status_code}: {resp.text[:300]}", retryable=retryable)
        return resp.json()

    async def prepare_attachment(self, att: Attachment) -> Any:
        if not self._cfg().get("send_attachments", True) or not att.sendable:
            return None
        data = await self._post(
            "media",
            data={"messaging_product": "whatsapp", "type": "application/pdf"},
            files={"file": (att.filename, att.data, "application/pdf")},
        )
        return data.get("id")

    async def send_text(self, sub, rich, plain, handle=None, att=None) -> str:
        lines = plain.split("\n")
        title = lines[0] if lines else "Alert"
        body = template_text(" ".join(lines[1:]).strip() or title)
        components: list[dict] = []
        if handle and self.attachment_inline() and att:
            components.append(
                {"type": "header", "parameters": [{"type": "document", "document": {"id": handle, "filename": att.filename}}]}
            )
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": template_text(title, 60)},
                                            {"type": "text", "text": body}]}
        )
        data = await self._post(
            "messages",
            json={
                "messaging_product": "whatsapp",
                "to": sub["mobile"].lstrip("+"),
                "type": "template",
                "template": {
                    "name": self._cfg().get("template_name", "nse_alert"),
                    "language": {"code": self._cfg().get("template_language", "en")},
                    "components": components,
                },
            },
        )
        return data["messages"][0]["id"]

    async def send_attachment(self, sub, handle, att) -> str | None:
        if handle is None or self.attachment_inline():
            return None
        data = await self._post(
            "messages",
            json={
                "messaging_product": "whatsapp",
                "to": sub["mobile"].lstrip("+"),
                "type": "document",
                "document": {"id": handle, "filename": att.filename},
            },
        )
        return data["messages"][0]["id"]

    async def close(self) -> None:
        await self.client.aclose()
