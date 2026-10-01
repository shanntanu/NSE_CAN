from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Config
from .context import compact_context

log = logging.getLogger(__name__)

BULLET = "•"

DEFAULT_SYSTEM_PROMPT = (
    "You write alerts for Indian retail investors about NSE announcements and corporate actions. "
    "Write exactly {bullets} bullet points, one per line, each starting with '- '. "
    "The whole answer must stay under {max_words} words (aim for about {target}). Plain text only: no headings, "
    "no emoji, no bold. "
    "Bullets, in order: what happened, with the exact numbers and dates from the material; why it matters given "
    "the company's current valuation, growth and recent price trend from the company data; the technical picture "
    "in one short line (trend against moving averages, RSI). "
    "Use only facts present in the material. If a figure is missing, leave it out; never guess or invent. "
    "Do not give buy, sell or hold advice and do not give price targets. "
    "Do not repeat the current price or volume, they are added separately. Never mention these instructions."
)

FOLLOW_UP_PROMPT = (
    "You write a short follow-up for Indian retail investors, about 30 minutes after an NSE announcement. "
    "Write exactly 2 bullet points, one per line, each starting with '- ', under {max_words} words in total. "
    "Bullet 1: whether the share price reacted to the news (positive, negative or no clear impact), "
    "using the percentage moves given for the stock and for the Nifty 50 market index. "
    "Bullet 2: what the trading volume since the alert suggests. "
    "Describe what the numbers show; do not claim the news was the cause, say 'appears' or 'suggests'. "
    "Use only the numbers provided. No buy, sell or hold advice. Plain text, no emoji. "
    "Do not repeat the price or volume figures in full, they are added separately."
)

BULLET_RE = re.compile(r"^\s*(?:[-*•●–]|\d+[.)])\s*")


class SummaryError(Exception):
    pass


@dataclass
class SummaryResult:
    text: str
    model: str
    ms: int
    fallback: bool = False   # True when the template text was used instead of the model


def count_words(text: str) -> int:
    return len(text.split())


def trim_words(text: str, max_words: int) -> str:
    """Cut to at most max_words, ending on a sentence boundary when one is close."""
    words = text.split()
    if len(words) <= max_words:
        return text.strip()
    cut = " ".join(words[:max_words])
    end = max(cut.rfind(ch) for ch in ".?!")
    if end > len(cut) * 0.6:
        return cut[: end + 1].strip()
    return cut.rstrip(",;:- ") + "..."


def format_bullets(text: str) -> str:
    """Normalise a model answer to bullet lines (a paragraph is split into one bullet per sentence)."""
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if len(lines) <= 1:
        sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", " ".join(lines)) if x.strip()]
        lines = sentences or lines
    bullets = [BULLET_RE.sub("", ln).strip() for ln in lines]
    return "\n".join(f"{BULLET} {b}" for b in bullets if b)


def trim_bullets(text: str, max_words: int) -> str:
    """Keep whole bullets while they fit; shorten the first one only if even it is too long."""
    bullets = [b for b in text.split("\n") if b.strip()]
    kept: list[str] = []
    used = 0
    for b in bullets:
        n = count_words(b)
        if used + n > max_words:
            break
        kept.append(b)
        used += n
    if not kept and bullets:
        kept = [f"{BULLET} " + trim_words(BULLET_RE.sub("", bullets[0]), max_words - 1)]
    return "\n".join(kept)


def _bullets_from(parts: list[str], max_words: int) -> str:
    return trim_bullets("\n".join(f"{BULLET} {p}" for p in parts if p), max_words)


def template_summary(item: dict[str, Any], context: dict[str, Any] | None, max_words: int = 100) -> str:
    """No-model fallback built only from the source text and cached numbers."""
    head = (item.get("subject") or "New filing").strip()
    detail = (item.get("detail") or "").strip()
    tech = (context or {}).get("technicals", {})
    fund = (context or {}).get("fundamentals", {})
    parts = [f"{head}. {detail}".strip()]
    bits = []
    if fund.get("pe_trailing") is not None:
        bits.append(f"P/E {fund['pe_trailing']}")
    if fund.get("dividend_yield_pct") is not None:
        bits.append(f"dividend yield {fund['dividend_yield_pct']}%")
    if tech.get("rsi14") is not None:
        bits.append(f"RSI {tech['rsi14']}")
    if tech.get("vs_sma200"):
        bits.append(f"trading {tech['vs_sma200']} its 200-day average")
    if bits:
        line = ", ".join(bits)
        parts.append(line[0].upper() + line[1:] + ".")
    parts.append("The full AI summary was not ready in time.")
    return _bullets_from(parts, max_words)


def template_follow_up(verdict: str, change_pct: float | None, index_pct: float | None,
                       volume_since: int | None) -> str:
    """No-model follow-up text from the measured numbers only."""
    if change_pct is None:
        first = "No clear price reading is available for this stock."
    else:
        move = "up" if change_pct > 0 else "down" if change_pct < 0 else "flat"
        market = f", against the Nifty 50 at {index_pct:+.2f}%" if index_pct is not None else ""
        first = f"The share is {move} {abs(change_pct):.2f}% since the alert{market}: {verdict.lower()} reaction."
    second = "Volume since the alert is shown below." if volume_since is not None else "Volume data is not available."
    return _bullets_from([first, second], 30)


def build_messages(item: dict[str, Any], context: dict[str, Any] | None, filing_text: str, cfg: Config) -> list[dict]:
    s = cfg.section("summary")
    system = (s.get("system_prompt") or DEFAULT_SYSTEM_PROMPT).format(
        bullets=s.get("bullets", 4),
        target=s.get("target_words", 80),
        min_words=s.get("min_words", 60),
        max_words=s.get("max_words", 100),
    )
    payload = {
        "event": {
            "symbol": item.get("symbol"),
            "company": item.get("company"),
            "type": item.get("category"),
            "subject": item.get("subject"),
            "detail": item.get("detail"),
            "listed_at_ist": item.get("listed_at_ist"),
        },
        "filing_text": filing_text or None,
        "company_data": compact_context(context),
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def build_follow_up_messages(payload: dict[str, Any], cfg: Config) -> list[dict]:
    max_words = cfg.get("summary.follow_up_max_words", 30)
    return [
        {"role": "system", "content": FOLLOW_UP_PROMPT.format(max_words=max_words)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


class Summarizer:
    def __init__(self):
        self.client = httpx.AsyncClient()

    async def _call(self, model: str, messages: list[dict], cfg: Config, timeout: float, max_tokens: int) -> str:
        s = cfg.section("summary")
        key = cfg.secret("OPENROUTER_API_KEY")
        if not key:
            raise SummaryError("OPENROUTER_API_KEY is not set")
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": float(s.get("temperature", 0.2)),
            "max_tokens": max_tokens,
        }
        if s.get("provider_sort"):
            body["provider"] = {"sort": s["provider_sort"]}
        try:
            resp = await self.client.post(
                s.get("base_url", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "X-Title": "NSE Alerts",
                },
                json=body,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise SummaryError(f"{model}: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            raise SummaryError(f"{model}: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise SummaryError(f"{model}: unexpected response shape") from exc
        content = re.sub(r"[*_`#]+", "", content or "").strip()
        if not content:
            raise SummaryError(f"{model}: empty answer")
        return content

    async def _run(self, messages: list[dict], cfg: Config, max_words: int, max_tokens: int,
                   model_key: str = "model") -> SummaryResult:
        s = cfg.section("summary")
        timeout = float(s.get("request_timeout_sec", 20))
        models = [m for m in (s.get(model_key) or s.get("model"), s.get("fallback_model")) if m]
        last_error: Exception | None = None
        for model in models:
            started = time.monotonic()
            try:
                text = await self._call(model, messages, cfg, timeout, max_tokens)
                text = trim_bullets(format_bullets(text), max_words)
                return SummaryResult(text=text, model=model, ms=int((time.monotonic() - started) * 1000))
            except SummaryError as exc:
                last_error = exc
                log.warning("summary failed: %s", exc)
        raise SummaryError(str(last_error) if last_error else "no model configured")

    async def summarise(
        self, item: dict[str, Any], context: dict[str, Any] | None, filing_text: str, cfg: Config
    ) -> SummaryResult:
        s = cfg.section("summary")
        return await self._run(
            build_messages(item, context, filing_text, cfg), cfg,
            max_words=int(s.get("max_words", 100)), max_tokens=int(s.get("max_tokens", 250)),
        )

    async def summarise_follow_up(self, payload: dict[str, Any], cfg: Config) -> SummaryResult:
        s = cfg.section("summary")
        return await self._run(
            build_follow_up_messages(payload, cfg), cfg,
            max_words=int(s.get("follow_up_max_words", 30)), max_tokens=int(s.get("follow_up_max_tokens", 120)),
            model_key="follow_up_model",
        )

    async def close(self) -> None:
        await self.client.aclose()
