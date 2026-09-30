from __future__ import annotations

import asyncio
import io
import logging
import random
from pathlib import Path
from typing import Any

from ..attachments import Attachment
from ..config import ConfigStore
from ..db import Database, utcnow
from .base import Channel, ChannelError

log = logging.getLogger(__name__)

WORKER_DIR = Path(__file__).resolve().parents[2]


def session_path(store: ConfigStore) -> str:
    raw = store.cfg.get("channels.telegram.session", "sessions/nse_alerts")
    path = Path(raw)
    if not path.is_absolute():
        path = WORKER_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def make_client(store: ConfigStore):
    from telethon import TelegramClient

    api_id = store.cfg.secret("TELEGRAM_API_ID")
    api_hash = store.cfg.secret("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in the environment")
    return TelegramClient(session_path(store), int(api_id), api_hash)


class TelegramChannel(Channel):
    """Telegram through the MTProto API (Telethon), sending as a normal user account."""

    name = "telegram"

    def __init__(self, store: ConfigStore, db: Database):
        self.store = store
        self.db = db
        self.client = None
        self._pace = asyncio.Lock()

    def _cfg(self):
        return self.store.cfg.section("channels.telegram")

    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled", True))

    def max_concurrency(self) -> int:
        return int(self._cfg().get("max_concurrency", 8))

    def eligible(self, sub: dict[str, Any]) -> bool:
        return sub.get("telegram_status") == "resolved" and bool(sub.get("telegram_user_id"))

    async def start(self) -> None:
        from telethon import events

        self.client = make_client(self.store)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram session is not logged in. Run: python -m nse_alerts.cli telegram-login")
        me = await self.client.get_me()
        log.info("telegram connected as %s", getattr(me, "username", None) or me.id)

        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_read, events.MessageRead(inbox=False))

    async def close(self) -> None:
        if self.client:
            await self.client.disconnect()

    # ---- incoming ----------------------------------------------------------------

    async def _on_message(self, event) -> None:
        if not event.is_private or not self._cfg().get("handle_stop_keyword", True):
            return
        body = (event.raw_text or "").strip().upper()
        if body != "STOP":
            return
        changed = await self.db.execute(
            "update subscribers set status = 'stopped', updated_at = :now "
            "where telegram_user_id = :uid and status <> 'stopped'",
            uid=event.sender_id, now=utcnow(),
        )
        if changed:
            await event.respond("You are unsubscribed. You can start again any time from the website.")

    async def _on_read(self, event) -> None:
        try:
            chat_id = event.chat_id
            max_id = int(event.max_id)
        except (AttributeError, TypeError, ValueError):
            return
        await self.db.execute(
            "update deliveries set status = 'read', read_at = :now "
            "where channel = 'telegram' and status in ('sent', 'delivered') "
            "and subscriber_id in (select id from subscribers where telegram_user_id = :uid) "
            "and cast(provider_message_id as bigint) <= :max_id",
            uid=chat_id, max_id=max_id, now=utcnow(),
        )

    # ---- phone number lookup ---------------------------------------------------------

    async def resolve_phone(self, phone: str) -> tuple[int, int] | None:
        from telethon import functions, types

        contact = types.InputPhoneContact(
            client_id=random.randrange(1, 2**31), phone=phone, first_name="NSE", last_name=phone[-4:]
        )
        result = await self.client(functions.contacts.ImportContactsRequest([contact]))
        if not result.users:
            return None
        user = result.users[0]
        if not self._cfg().get("keep_contacts", True):
            try:
                await self.client(functions.contacts.DeleteContactsRequest(id=[user]))
            except Exception:
                log.debug("could not remove contact", exc_info=True)
        return user.id, user.access_hash

    # ---- sending ---------------------------------------------------------------------

    def _peer(self, sub: dict[str, Any]):
        from telethon import types

        return types.InputPeerUser(int(sub["telegram_user_id"]), int(sub["telegram_access_hash"]))

    async def _guard(self, coro_factory):
        """Pace sends, retry short flood waits once, and translate Telegram errors."""
        from telethon import errors

        for attempt in (1, 2):
            async with self._pace:
                await asyncio.sleep(float(self._cfg().get("min_interval_ms", 40)) / 1000)
            try:
                return await coro_factory()
            except errors.FloodWaitError as exc:
                limit = float(self._cfg().get("flood_wait_max_sec", 10))
                if attempt == 1 and exc.seconds <= limit:
                    log.warning("telegram flood wait %ss, retrying", exc.seconds)
                    await asyncio.sleep(exc.seconds + 0.5)
                    continue
                raise ChannelError(f"flood wait {exc.seconds}s", retryable=False) from exc
            except errors.PeerFloodError as exc:
                await self.db.set_status("telegram_peer_flood", utcnow().isoformat())
                raise ChannelError("Telegram limited this account for messaging many new people", False) from exc
            except (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError,
                    errors.InputUserDeactivatedError, errors.UserDeactivatedError) as exc:
                raise ChannelError(exc.__class__.__name__, retryable=False) from exc
            except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
                raise ChannelError(f"network: {exc.__class__.__name__}") from exc
            except errors.RPCError as exc:
                raise ChannelError(f"{exc.__class__.__name__}: {exc}") from exc
        raise ChannelError("send failed")

    async def send_direct(self, sub: dict[str, Any], text: str) -> str:
        msg = await self._guard(lambda: self.client.send_message(self._peer(sub), text, link_preview=False))
        return str(msg.id)

    async def send_text(self, sub, rich, plain, handle=None, att=None) -> str:
        msg = await self._guard(
            lambda: self.client.send_message(self._peer(sub), rich, parse_mode="html", link_preview=False)
        )
        return str(msg.id)

    async def prepare_attachment(self, att: Attachment) -> Any:
        if not self._cfg().get("send_attachments", True) or not att.sendable:
            return None
        buf = io.BytesIO(att.data)
        buf.name = att.filename
        return await self.client.upload_file(buf, file_name=att.filename)

    async def send_attachment(self, sub, handle, att) -> str | None:
        if handle is None:
            return None
        msg = await self._guard(
            lambda: self.client.send_file(self._peer(sub), handle, force_document=True, file_name=att.filename)
        )
        return str(msg.id)
