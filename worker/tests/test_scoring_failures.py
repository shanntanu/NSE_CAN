from __future__ import annotations

from datetime import timedelta

from nse_alerts.db import utcnow
from nse_alerts.summarizer import SummaryAuthError, SummaryError


async def add_unscored(db, n, prefix="u"):
    for i in range(n):
        await db.execute(
            "insert into event_signals (source, source_uid, symbol, category, subject, text_used, listed_at) "
            "values ('nse', :u, 'TCS', 'announcement', 'Outcome of Board Meeting', 'profit up ' || :u, :l)",
            u=f"{prefix}{i}", l=utcnow() - timedelta(days=400 + i))


async def counts(db):
    rows = await db.fetch_all("select coalesce(scored_model, 'none') as m, count(*) as n from event_signals group by 1")
    return {r["m"]: r["n"] for r in rows}


async def test_out_of_credits_stops_the_run_and_leaves_items_unscored(system):
    await add_unscored(system.local, 10)
    system.summarizer.score_json = SummaryAuthError("HTTP 402 insufficient credits")
    done = await system.signals.score_pending(system.store.cfg)
    assert done == 0
    assert "402" in system.signals.last_run["stopped"]
    assert await counts(system.local) == {"none": 10}          # no keyword guesses were saved as if they were scores


async def test_a_flaky_model_leaves_only_the_failed_items_unscored(system):
    await add_unscored(system.local, 6)
    calls = {"n": 0}
    good = system.summarizer.score_json

    async def flaky(model, messages, cfg, timeout, max_tokens, temperature=None):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise SummaryError("timeout")
        return good

    system.summarizer._call = flaky
    system.store.cfg.data["signals"]["score_concurrency"] = 1
    done = await system.signals.score_pending(system.store.cfg)
    assert done == 3 and system.signals.last_run["failed"] == 3 and not system.signals.last_run["stopped"]
    unscored = await system.local.fetch_one("select count(*) as n from event_signals where scored_at is null")
    assert unscored["n"] == 3


async def test_live_alerts_still_fall_back_to_keywords_when_the_model_is_down(system):
    system.summarizer.score_json = SummaryAuthError("HTTP 402 insufficient credits")
    score = await system.signals.score("TCS", "Dividend", "interim dividend declared", system.store.cfg)
    assert score.model == "keywords" and score.confidence < 0.5


async def test_keyword_guesses_left_by_an_earlier_run_can_be_cleared_and_redone(system):
    await add_unscored(system.local, 4)
    await system.signals.score_pending(system.store.cfg, use_keywords=True)       # what the failed run left behind
    assert await counts(system.local) == {"keywords": 4}
    assert await system.signals.reset_keyword_scores() == 4
    assert await counts(system.local) == {"none": 4}
    assert await system.signals.score_pending(system.store.cfg) == 4
    assert await counts(system.local) == {system.store.cfg.get("summary.model"): 4}
