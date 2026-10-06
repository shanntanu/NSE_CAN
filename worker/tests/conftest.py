from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nse_alerts.attachments import Attachment
from nse_alerts.channels.base import Channel
from nse_alerts.config import ConfigStore
from nse_alerts.context import ContextStore
from nse_alerts.db import Database, utcnow
from nse_alerts.followup import FollowUps
from nse_alerts.quotes import Quote
from nse_alerts.signals.prices import PriceStore
from nse_alerts.signals.service import SignalService
from nse_alerts.pipeline import Pipeline
from nse_alerts.poller import Poller
from nse_alerts.summarizer import SummaryResult
from nse_alerts.universe import Universe

WORKER = Path(__file__).resolve().parents[1]


class FakeNse:
    def __init__(self):
        self.announcement_items = []
        self.day_items = []
        self.action_items = []

    async def announcements(self, timeout=8):
        return list(self.announcement_items)

    async def announcements_for_day(self, day, timeout=12):
        return list(self.day_items)

    async def corporate_actions(self, timeout=8):
        return list(self.action_items)


class FakeSummarizer:
    def __init__(self):
        self.calls = []
        self.score_json = '{"topic": "financial_results", "sentiment": 70, "confidence": 0.8, "reason": "strong"}'
        self.score_calls = 0

    async def _call(self, model, messages, cfg, timeout, max_tokens, temperature=None):
        self.score_calls += 1
        if isinstance(self.score_json, Exception):
            raise self.score_json
        return self.score_json

    async def summarise(self, item, context, filing_text, cfg):
        self.calls.append(item)
        return SummaryResult(text="Short summary of the filing.", model="fake/model", ms=5)


    async def summarise_follow_up(self, payload, cfg):
        self.calls.append(payload)
        return SummaryResult(text="\u2022 Price moved with the market.\n\u2022 Volume was steady.", model="fake/model", ms=3)


class FakeQuotes:
    def __init__(self):
        self.price = 100.0
        self.volume = 1_000_000
        self.index_price = 22000.0

    async def stock(self, symbol, cfg):
        return Quote(symbol, self.price, 99.0, self.volume, utcnow())

    async def index(self, cfg):
        return Quote("NIFTY 50", self.index_price, 21990.0, None, utcnow())


class FakeFetcher:
    def __init__(self, text=""):
        self.text = text

    async def fetch(self, url, cfg):
        return Attachment(url=url, filename="filing.pdf", data=b"%PDF-1.4 fake")

    async def read_text(self, att, cfg):
        return self.text


class FakeChannel(Channel):
    name = "telegram"
    free_text = True

    def __init__(self):
        self.texts = []
        self.attachments = []

    def enabled(self):
        return True

    def eligible(self, sub):
        return sub.get("telegram_status") == "resolved"

    async def prepare_attachment(self, att):
        return "handle"

    async def send_text(self, sub, rich, plain, handle=None, att=None):
        self.texts.append((sub["id"], plain))
        return str(100 + len(self.texts))

    async def send_attachment(self, sub, handle, att):
        self.attachments.append((sub["id"], att.filename))
        return "att"


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "config.yaml"
    shutil.copy(WORKER / "config.yaml", path)
    return ConfigStore(path)


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    await database.init_schema()
    yield database
    await database.close()


@pytest.fixture
def universe():
    return Universe.load(WORKER.parent / "data" / "nifty50.csv")


@pytest.fixture
async def system(store, db, universe):
    nse = FakeNse()
    channel = FakeChannel()
    summarizer = FakeSummarizer()
    context = ContextStore(db, universe)
    quotes = FakeQuotes()
    signals = SignalService(store, db, summarizer, PriceStore(db), FakeFetcher())
    pipeline = Pipeline(store, db, context, summarizer, FakeFetcher(), {"telegram": channel}, quotes, signals)
    followups = FollowUps(store, db, summarizer, context, quotes, pipeline)
    pipeline.followups = followups
    poller = Poller(store, db, universe, pipeline, nse)
    return type("System", (), dict(nse=nse, channel=channel, summarizer=summarizer, context=context,
                                   pipeline=pipeline, poller=poller, db=db, store=store, quotes=quotes, signals=signals,
                                   followups=followups))()


async def seed_signal(db, symbol="TCS", days_ago=100, topic="financial_results", sentiment=70, r15=1.0, r30=2.0,
                      n15=0.5, n30=1.0, uid=None, confidence=0.8, status="done"):
    from datetime import timedelta
    uid = uid or f"seed-{symbol}-{days_ago}-{topic}-{sentiment}"
    await db.execute(
        "insert into event_signals (source, source_uid, symbol, category, subject, listed_at, topic, sentiment, "
        "confidence, ret_15d_pct, ret_30d_pct, nifty_15d_pct, nifty_30d_pct, returns_status) "
        "values ('nse', :uid, :sym, 'announcement', 'Outcome of Board Meeting', :listed, :t, :s, :c, :r15, :r30, "
        ":n15, :n30, :st)",
        uid=uid, sym=symbol, listed=utcnow() - timedelta(days=days_ago), t=topic, s=sentiment, c=confidence,
        r15=r15, r30=r30, n15=n15, n30=n30, st=status)
