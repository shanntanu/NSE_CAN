from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nse_alerts.attachments import Attachment
from nse_alerts.channels.base import Channel
from nse_alerts.config import ConfigStore
from nse_alerts.context import ContextStore
from nse_alerts.db import Database
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

    async def summarise(self, item, context, filing_text, cfg):
        self.calls.append(item)
        return SummaryResult(text="Short summary of the filing.", model="fake/model", ms=5)


class FakeFetcher:
    async def fetch(self, url, cfg):
        return Attachment(url=url, filename="filing.pdf", data=b"%PDF-1.4 fake")

    async def read_text(self, att, cfg):
        return ""


class FakeChannel(Channel):
    name = "telegram"

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
    pipeline = Pipeline(store, db, context, summarizer, FakeFetcher(), {"telegram": channel})
    poller = Poller(store, db, universe, pipeline, nse)
    return type("System", (), dict(nse=nse, channel=channel, summarizer=summarizer, context=context,
                                   pipeline=pipeline, poller=poller, db=db, store=store))()
