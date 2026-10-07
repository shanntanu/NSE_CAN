from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import archive, verification
from .attachments import AttachmentFetcher
from .channels.telegram import TelegramChannel
from .channels.whatsapp import WhatsAppChannel
from .config import ConfigStore
from .context import ContextStore
from .followup import FollowUps
from .db import Database, StatusWriter, init_databases, local_url, utcnow
from .pipeline import Pipeline
from .poller import Poller
from .quotes import QuoteService
from .sources.bse import BseClient
from .sources.nse import NseClient
from .signals.prices import PriceStore
from .signals.service import SignalService
from .summarizer import Summarizer
from .universe import Universe

log = logging.getLogger(__name__)
WORKER_DIR = Path(__file__).resolve().parent.parent


def setup_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)


def load_universe(store: ConfigStore) -> Universe:
    path = Path(store.cfg.get("universe.file", "../data/nifty50.csv"))
    if not path.is_absolute():
        path = WORKER_DIR / path
    return Universe.load(path)


async def supervise(name: str, factory) -> None:
    """Run a background loop forever, restarting it with a growing pause if it crashes."""
    delay = 2.0
    while True:
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed, restarting in %.0fs", name, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


class App:
    def __init__(self, store: ConfigStore | None = None, db: Database | None = None, local: Database | None = None):
        self.store = store or ConfigStore()
        self.db = db or Database()                      # shared and small: subscribers, recent alerts, deliveries
        self.local = local or Database(local_url())     # heavy: history, prices, cached company data, archive
        self.status = StatusWriter(self.db, self.local, float(self.store.cfg.get("status.shared_interval_sec", 30)))
        self.universe = load_universe(self.store)
        self.context = ContextStore(self.local, self.universe)
        self.summarizer = Summarizer()
        self.fetcher = AttachmentFetcher(self.store.cfg)
        self.telegram = TelegramChannel(self.store, self.db)
        self.whatsapp = WhatsAppChannel(self.store)
        self.channels = {"telegram": self.telegram, "whatsapp": self.whatsapp}
        self.quotes = QuoteService()
        self.signals = SignalService(self.store, self.local, self.summarizer, PriceStore(self.local), self.fetcher)
        self.pipeline = Pipeline(self.store, self.db, self.context, self.summarizer, self.fetcher, self.channels,
                                 self.quotes, self.signals)
        self.followups = FollowUps(self.store, self.db, self.summarizer, self.context, self.quotes, self.pipeline)
        self.pipeline.followups = self.followups
        self.nse = NseClient(self.store.cfg)
        self.bse = BseClient(self.store.cfg, self.universe)
        self.poller = Poller(self.store, self.db, self.universe, self.pipeline, self.nse, self.bse, self.notify_admin,
                             local=self.local, status=self.status)

    async def notify_admin(self, text: str) -> None:
        phone = self.store.cfg.get("admin.notify_phone", "")
        if not phone or not self.telegram.client:
            return
        try:
            found = await self.telegram.resolve_phone(phone)
            if found:
                await self.telegram.send_direct(
                    {"telegram_user_id": found[0], "telegram_access_hash": found[1]}, f"NSE Alerts: {text}"
                )
        except Exception:
            log.exception("could not notify the admin")

    async def heartbeat(self) -> None:
        while True:
            await self.status.set("worker_heartbeat", utcnow().isoformat())
            await asyncio.sleep(float(self.store.cfg.get("heartbeat_interval_sec", 10)))

    async def run(self) -> None:
        setup_logging(self.store.cfg.get("app.log_level", "INFO"))
        await init_databases(self.db, self.local)
        await self.context.load_from_db()
        log.info("tracking %d stocks; telegram=%s whatsapp=%s bse=%s",
                 len(self.universe.symbols),
                 self.telegram.enabled(), self.whatsapp.enabled(), self.store.cfg.get("polling.bse.enabled", False))
        if self.telegram.enabled():
            await self.telegram.start()

        tasks = [
            asyncio.create_task(supervise("heartbeat", self.heartbeat)),
            asyncio.create_task(supervise("nse poller", self.poller.run_nse)),
            asyncio.create_task(supervise("bse poller", self.poller.run_bse)),
            asyncio.create_task(supervise("context refresh", lambda: self.context.run(self.store))),
            asyncio.create_task(supervise("follow-ups", self.followups.run)),
            asyncio.create_task(supervise("signals upkeep", lambda: self.signals.run(self.universe))),
            asyncio.create_task(supervise("archive and backup",
                                          lambda: archive.run(self.store, self.db, self.local, self.status))),
        ]
        if self.telegram.client:
            tasks.append(asyncio.create_task(
                supervise("telegram services", lambda: verification.run(self.store, self.db, self.telegram))))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await self.close()

    async def close(self) -> None:
        for closer in (self.nse.close, self.bse.close, self.summarizer.close, self.fetcher.close,
                       self.telegram.close, self.whatsapp.close, self.db.close, self.local.close):
            try:
                await closer()
            except Exception:
                log.debug("error while closing", exc_info=True)
