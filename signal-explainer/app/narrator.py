"""Turns the computed facts into a PM-ready narrative with OpenAI (strict JSON schema), with a template fallback."""
from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from .analysis import describe_move
from .config import Settings

log = logging.getLogger("narrator")

SYSTEM = """You are a sell-side-quality equity analyst writing for a fundamental portfolio manager at a long-only
growth manager. A quant sentiment signal on a stock has moved; your job is to explain WHY in plain English.

Rules:
- Use ONLY the facts and evidence passages provided. Never add numbers, events or dates that are not in them.
- Point-in-time: the analysis date is AS_OF. Do not refer to anything after it.
- Cite evidence inline as [E1], [E2] (news) or [M1] (management on the last earnings call). Every driver must cite.
- Drivers explain the CHANGE. 'impact' is how much each event type moved the average news tone versus the prior
  window of equal length; impacts sum to 'tone_change'. A negative impact can mean new negative news OR positive news
  that faded away: evidence marked window='prior' is faded news from the prior window — say 'faded' in that case.
- Treat 'reactive' drivers (stock price / index moves) as the market's reaction, not a cause. Say so if they dominate.
- 'general' means passages with no tagged event; describe what the evidence says rather than calling it 'general'.
- If the news tone change and the sentiment-signal change point in different directions, say so plainly.
- Evidence passages can mention other companies; only attribute to the target what the passage says about it.
- Be specific and concise. No hype, no investment advice, no recommendation to buy or sell.
- 'stock_vs_sector' must use the peer comparison numbers when available.
- If management evidence [M*] is provided, 'thesis_drift' compares what management said on the call with what the news
  says now; otherwise set it to an empty string.
- Sentiment levels are on a -1..+1 scale; describe changes in words (e.g. 'up 0.08 to 0.31') not jargon."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["headline", "tone", "summary", "narrative", "drivers", "stock_vs_sector", "thesis_drift",
                 "what_to_watch", "questions_for_management", "confidence", "caveats"],
    "properties": {
        "headline": {"type": "string", "description": "<= 14 words, the story in one line"},
        "tone": {"type": "string", "enum": ["positive", "negative", "mixed", "neutral"]},
        "summary": {"type": "string", "description": "2 sentences a PM reads first"},
        "narrative": {"type": "string", "description": "120-200 words, cites evidence"},
        "drivers": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["key", "title", "explanation"],
            "properties": {"key": {"type": "string", "description": "driver key exactly as given"},
                           "title": {"type": "string", "description": "<= 8 words, specific to this company"},
                           "explanation": {"type": "string", "description": "1-2 sentences with citations"}}}},
        "stock_vs_sector": {"type": "string"},
        "thesis_drift": {"type": "string"},
        "what_to_watch": {"type": "array", "items": {"type": "string"}},
        "questions_for_management": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "caveats": {"type": "string"},
    },
}


def llm_payload(facts: dict) -> dict:
    """The compact view of the facts the model sees (no raw series)."""
    m = facts["move"]
    return {
        "company": facts["company"], "as_of": facts["as_of"], "window": facts["window"],
        "move": {k: m.get(k) for k in ("start_date", "start_level", "end_date", "end_level", "change", "z_score",
                                       "percentile", "sentiment_pressure", "abnormal_media_attention",
                                       "peak_attention", "inflection_days")} | {"in_words": describe_move(m)},
        "peers": facts.get("peers"),
        "news_tone": {k: facts["attribution"].get(k) for k in ("tone_now", "tone_prior", "tone_change",
                                                                "passages_used", "prior_passages_used")},
        "drivers": [{k: d[k] for k in ("key", "label", "reactive", "direction", "impact", "share", "contribution_now",
                                       "contribution_prior", "documents", "prior_documents", "avg_sentiment",
                                       "first_seen", "last_seen", "lift", "evidence_ids")}
                    for d in facts["attribution"]["drivers"]],
        "evidence": [{k: e[k] for k in ("id", "window", "date", "source", "headline", "sentiment", "text")}
                     for e in facts["attribution"]["evidence"]],
        "management_on_last_call": facts.get("management") or [],
        "input_score": facts.get("input_score"),
    }


async def narrate(settings: Settings, facts: dict) -> tuple[dict, str]:
    if not settings.OPENAI_API_KEY:
        return fallback(facts), "template"
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    resp = await client.responses.create(
        model=settings.OPENAI_MODEL,
        input=[{"role": "system", "content": SYSTEM},
               {"role": "user", "content": "AS_OF = " + facts["as_of"] + "\n\nFACTS:\n" +
                json.dumps(llm_payload(facts), ensure_ascii=False)}],
        text={"format": {"type": "json_schema", "name": "sentiment_narrative", "strict": True, "schema": SCHEMA}},
    )
    return json.loads(resp.output_text), settings.OPENAI_MODEL


def fallback(facts: dict) -> dict:
    """Deterministic narrative used when no LLM key is configured."""
    m, drivers = facts["move"], facts["attribution"]["drivers"]
    name = facts["company"]["name"]
    top = [d for d in drivers if not d["reactive"]][:3] or drivers[:3]
    tone = "positive" if m["change"] > 0.02 else "negative" if m["change"] < -0.02 else "neutral"
    peers = facts.get("peers") or {}
    return {
        "headline": f"{name}: sentiment {'up' if m['change'] >= 0 else 'down'} {abs(m['change']):.2f} "
                    f"over {facts['window']['days']} days",
        "tone": tone,
        "summary": f"{name} news sentiment moved from {m['start_level']:+.2f} to {m['end_level']:+.2f}, "
                   f"{describe_move(m)}. Main drivers: {', '.join(d['label'] for d in top)}.",
        "narrative": " ".join(f"{d['label']} accounted for {d['share']:.0%} of the change in news tone "
                              f"({d['direction']}) {' '.join('[' + e + ']' for e in d['evidence_ids'])}."
                              for d in top),
        "drivers": [{"key": d["key"], "title": d["label"], "explanation":
                     f"{d['documents']} articles now vs {d['prior_documents']} in the prior window."} for d in drivers],
        "stock_vs_sector": f"Verdict: {peers.get('verdict', 'n/a')}." if peers.get("available") else "No peer data.",
        "thesis_drift": "",
        "what_to_watch": [],
        "questions_for_management": [],
        "confidence": "low",
        "caveats": "Template narrative (no LLM configured).",
    }
