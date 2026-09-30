from __future__ import annotations

from typing import Any

from ..attachments import Attachment


class ChannelError(Exception):
    """A send failed. `retryable` tells the delivery loop whether another try makes sense."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class Channel:
    name = ""

    def enabled(self) -> bool:
        raise NotImplementedError

    def max_concurrency(self) -> int:
        return 8

    def eligible(self, sub: dict[str, Any]) -> bool:
        raise NotImplementedError

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def prepare_attachment(self, att: Attachment) -> Any:
        """Upload once per event; the returned handle is reused for every recipient."""
        return None

    async def send_text(self, sub: dict[str, Any], rich: str, plain: str, handle: Any = None,
                        att: Attachment | None = None) -> str:
        raise NotImplementedError

    async def send_attachment(self, sub: dict[str, Any], handle: Any, att: Attachment) -> str | None:
        raise NotImplementedError

    def attachment_inline(self) -> bool:
        """True when the attachment travels inside the text message (WhatsApp template header)."""
        return False
