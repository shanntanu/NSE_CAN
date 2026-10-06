"""Topics, the scoring prompt, and a keyword fallback scorer.

Topics are a fixed list so that "the same kind of news" means the same thing in 2021 and today.
The model assigns the topic and a 1-100 sentiment from the exchange's own text; routine
filings (share certificate losses, newspaper copies) are dropped before any model call.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

PROMPT_VERSION = "v1"

TOPICS: dict[str, str] = {
    "financial_results": "Quarterly or annual results: revenue, profit, margins",
    "dividend": "Dividend declared or recommended, record date, payment",
    "bonus_split": "Bonus issue or stock split",
    "buyback": "Share buyback",
    "fund_raise": "QIP, rights issue, preferential allotment, bonds, debentures, loans",
    "acquisition_merger": "Acquisition, merger, demerger, stake purchase or sale, joint venture",
    "order_contract": "Order win, contract, tender, deal signed",
    "capacity_expansion": "Capex, new plant or facility, expansion",
    "guidance_outlook": "Management commentary, guidance, investor or analyst meet, earnings call updates",
    "rating_action": "Credit rating assigned, upgraded or downgraded",
    "management_change": "Appointment or resignation of CEO, CFO, directors, auditors",
    "regulatory_legal": "Penalty, show-cause notice, court order, investigation, tax demand",
    "product_approval": "Product launch, drug approval, licence, regulatory clearance",
    "operational_update": "Production or sales numbers, plant shutdown, fire, accident, business update",
    "promoter_insider": "Promoter or insider buying, selling or pledging; takeover-code disclosures",
    "esop_allotment": "Allotment of shares under ESOPs or conversions",
    "meeting_governance": "Board or general meeting notices, postal ballot, AGM, voting results",
    "other_material": "Anything else that could matter to the share price",
}

# Exchange categories that carry no price information. Dropped before scoring.
ROUTINE_PATTERNS = [
    r"loss of share certificate", r"duplicate share certificate", r"copy of newspaper publication",
    r"trading window", r"closure of trading window", r"certificate under sebi \(depositories",
    r"reg\. ?74\(5\)", r"reg\. ?24\(f\)", r"compliances?[- ]reg", r"investor grievance",
    r"statement of investor complaints", r"shareholding pattern", r"annual secretarial compliance",
    r"integrated filing", r"newspaper", r"intimation of .*(?:record|book closure)", r"change in .*address",
    r"analysts?/institutional investor meet.*(?:intimation|schedule)", r"schedule of analyst",
    r"annual report",
]
ROUTINE_RE = re.compile("|".join(ROUTINE_PATTERNS), re.I)

SYSTEM_PROMPT = (
    "You classify one NSE company announcement and judge its likely effect on the share price for a typical "
    "investor, using ONLY the text given. Answer with a single JSON object and nothing else:\n"
    '{{"topic": "<one topic id>", "sentiment": <integer 1-100>, "confidence": <number 0-1>, "reason": "<max 15 words>"}}\n'
    "Topic ids and meanings:\n{topics}\n"
    "Sentiment scale: 50 is neutral. 1-20 very negative (fraud, default, large loss, major penalty); "
    "21-40 negative; 41-59 neutral or mixed; 60-79 positive; 80-100 very positive (strong earnings beat, "
    "large premium buyback, big order). Judge size against what the text says; do not assume facts that are "
    "not in it. If the text is too generic to judge (for example only says the company informed the exchange "
    "about a board meeting outcome), use sentiment 50 and confidence 0.3 or less. "
    "Confidence is how sure you are of the sentiment given the text."
)


def system_prompt() -> str:
    topics = "\n".join(f"- {tid}: {desc}" for tid, desc in TOPICS.items())
    return SYSTEM_PROMPT.format(topics=topics)


GENERIC_MAX_CHARS = 170
SUBJECT_RE = re.compile(r"\bsub(?:ject)?\s*[:\-\u2013]\s*", re.I)   # "Sub:" or "Subject:"
LETTERHEAD_RE = re.compile(r"registered office|\bCIN\b|\bTel\b|\be-?mail\b", re.I)


def clean_filing_text(text: str, max_chars: int = 1800) -> str:
    """Drop the letterhead (address, phone, CIN) so the model sees the subject and the substance."""
    flat = " ".join((text or "").split())
    match = SUBJECT_RE.search(flat[:2500])
    if match:
        flat = flat[match.start():]
    elif LETTERHEAD_RE.search(flat[:600]):
        flat = flat[400:]
    return flat[:max_chars]


def is_generic(detail: str) -> bool:
    """True when the exchange text says little beyond the filing type ("X has informed the Exchange about ...")."""
    return len((detail or "").strip()) <= GENERIC_MAX_CHARS


def is_routine(subject: str) -> bool:
    return bool(ROUTINE_RE.search(subject or ""))


@dataclass
class Score:
    topic: str
    sentiment: int
    confidence: float
    reason: str = ""
    model: str = ""


def score_text(symbol: str, subject: str, detail: str, max_chars: int = 1500) -> str:
    """The text the model sees. Same shape for past and live items, so scores are comparable."""
    body = " ".join((detail or "").split())[:max_chars]
    return f"Company: {symbol}\nAnnouncement type: {subject}\nText: {body}"


def parse_score(raw: str, model: str = "") -> Score:
    """Read the model's JSON answer; raises ValueError when it is unusable."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        raise ValueError("no JSON object in the answer")
    data = json.loads(match.group(0))
    topic = str(data.get("topic", "")).strip()
    if topic not in TOPICS:
        topic = "other_material"
    sentiment = int(round(float(data["sentiment"])))
    sentiment = max(1, min(100, sentiment))
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    return Score(topic, sentiment, confidence, str(data.get("reason", ""))[:200], model)


# ---- keyword fallback: used when no model is available, and for offline tests ------------------

KEYWORD_TOPICS = [
    ("financial_results", r"financial results?|quarterly results?|unaudited|audited results"),
    ("dividend", r"dividend"),
    ("bonus_split", r"\bbonus\b|sub-?division|stock split|split of"),
    ("buyback", r"buy-?back"),
    ("fund_raise", r"qip|rights issue|preferential|debenture|\bncd\b|fund ?raising|raising of funds|bond"),
    ("acquisition_merger", r"acquisition|acquire|merger|demerger|amalgamation|joint venture|stake"),
    ("order_contract", r"\border\b|contract|awarded|bagged|tender|letter of award"),
    ("capacity_expansion", r"capacity|capex|expansion|new plant|commission"),
    ("rating_action", r"credit rating|\brating\b|crisil|icra|care ratings"),
    ("management_change", r"resign|appointment|cessation|\bceo\b|\bcfo\b|managing director|auditor"),
    ("regulatory_legal", r"penalty|show cause|notice|court|tribunal|investigation|fine|demand"),
    ("product_approval", r"usfda|approval|launch|licen[cs]e"),
    ("operational_update", r"production|sales volume|shutdown|fire|accident|business update"),
    ("promoter_insider", r"promoter|pledge|insider|takeover regulations|sast"),
    ("esop_allotment", r"esop|allotment of equity|esos"),
    ("guidance_outlook", r"investor meet|analyst|earnings call|conference call|guidance|outlook|press release"),
    ("meeting_governance", r"board meeting|agm|postal ballot|general meeting"),
]
POSITIVE = r"record|growth|increase|strong|wins?|awarded|bagged|upgrade|approval|buy-?back|dividend|bonus|profit up|beat"
NEGATIVE = r"penalty|fine|loss|decline|downgrade|resign|fraud|default|shutdown|fire|accident|notice|investigation|fall"


def keyword_score(subject: str, detail: str) -> Score:
    """Crude offline scorer. Real runs use the model; this keeps the pipeline working if it is down."""
    text = f"{subject} {detail}".lower()
    topic = next((t for t, pat in KEYWORD_TOPICS if re.search(pat, text)), "other_material")
    pos = len(re.findall(POSITIVE, text))
    neg = len(re.findall(NEGATIVE, text))
    sentiment = max(1, min(100, 50 + 8 * (pos - neg)))
    return Score(topic, sentiment, 0.35, "keyword rules", "keywords")
