"""One explanation run: resolve -> window -> sentiment, news, topics, peers -> attribution -> narrative."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from . import analysis
from .bigdata import BigdataClient
from .config import Settings
from .narrator import narrate
from .store import Store

log = logging.getLogger("pipeline")


class RunError(RuntimeError):
    pass


async def run_explain(run_id: int, settings: Settings, store: Store, bd: BigdataClient) -> None:
    run = store.get_run(run_id)
    step = lambda msg: store.update_run(run_id, status="running", progress=msg)  # noqa: E731
    try:
        step("Resolving company")
        ent = await bd.resolve(run["ticker"])
        if not ent:
            raise RunError(f"Could not find a company for '{run['ticker']}'")
        eid = ent["id"]
        as_of = date.fromisoformat(run["as_of"])
        store.update_run(run_id, rp_entity_id=eid, company=ent.get("name"))

        call = None
        if run["mode"] == "since_earnings":
            step("Finding the last earnings call")
            call = await bd.last_earnings_call(eid, as_of)
            if not call:
                raise RunError("No earnings call transcript found in the 200 days before the as-of date")
            window_start = date.fromisoformat(call["timestamp"][:10])
        else:
            window_start = as_of - timedelta(days=int(run["lookback_days"] or 30))
        days = max((as_of - window_start).days, 1)
        store.update_run(run_id, window_start=window_start.isoformat(), lookback_days=days)

        step("Pulling sentiment history, news, event topics and peers")
        prior_start = window_start - timedelta(days=days)
        series, chunks, prior_chunks, topics_now, topics_prior, comention = await asyncio.gather(
            bd.sentiment_series(eid, window_start - timedelta(days=settings.BASELINE_DAYS), as_of),
            bd.news_chunks(eid, window_start, as_of),
            bd.news_chunks(eid, prior_start, window_start - timedelta(days=1)),
            bd.co_mentions(eid, window_start, as_of, "topics", 100),
            bd.co_mentions(eid, prior_start, window_start - timedelta(days=1), "topics", 100),
            bd.co_mentions(eid, window_start, as_of, "entities", 40),
        )
        move = analysis.summarize_series(series, window_start, as_of)
        if not move["available"]:
            raise RunError("Bigdata.com has no daily sentiment for this company over the window")

        step(f"Attributing {len(chunks)} news passages to events")
        lift = analysis.topic_lift(topics_now, topics_prior, days, days)
        attribution = analysis.attribute_drivers(chunks, eid, prior_chunks, max_drivers=settings.MAX_DRIVERS,
                                                 evidence_per_driver=settings.EVIDENCE_PER_DRIVER, topic_lift=lift)

        step("Comparing with sector peers")
        chart_start = window_start - timedelta(days=max(2 * days, 60))
        peers = await _peers(bd, settings, ent, comention, window_start, as_of, chart_start)
        peer_ctx = analysis.peer_context(move["change"], peers)

        management = []
        if call:
            step("Reading the earnings call")
            management = _management(await bd.transcript_chunks(call, eid), eid)

        facts = {
            "company": {k: ent.get(k) for k in ("id", "name", "ticker", "sector", "industry_group", "country")},
            "as_of": as_of.isoformat(),
            "window": {"start": window_start.isoformat(), "end": as_of.isoformat(), "days": days, "mode": run["mode"],
                       "prior_start": prior_start.isoformat(),
                       "earnings_call": {"date": call["timestamp"][:10], "headline": call.get("headline")} if call else None},
            "move": move,
            "attribution": attribution,
            "peers": {k: v for k, v in peer_ctx.items() if k != "peers"} | {
                "peers": [{k: p.get(k) for k in ("name", "ticker", "change")} for p in peer_ctx["peers"]]},
            "management": management,
            "input_score": run.get("input_score"),
            "chart": {"series": [v for v in series if v["date"] >= chart_start.isoformat()],
                      "peers": [{"name": p["name"], "series": p["series"]} for p in peers]},
        }

        step(f"Writing the narrative ({settings.OPENAI_MODEL})")
        narrative, model = await narrate(settings, facts)
        z = move.get("z_score")
        store.update_run(run_id, status="done", progress="Done", facts=facts, narrative=narrative, model=model,
                         headline=narrative.get("headline"), change=move["change"],
                         surprise=abs(z) if z is not None else abs(move["change"]) * 10)
    except Exception as e:  # noqa: BLE001 - surface any failure on the run page
        log.exception("run %s failed", run_id)
        store.update_run(run_id, status="error", progress="Failed", error=str(e)[:500])


async def _peers(bd: BigdataClient, s: Settings, ent: dict, comention: list[dict], window_start: date, as_of: date,
                 chart_start: date) -> list[dict]:
    """Most co-mentioned public companies in the same industry group (sector as fallback)."""
    ids = [c["id"] for c in comention if c["id"] != ent["id"]]
    if not ids:
        return []
    meta = await bd.entities_by_id(ids)
    cands = [meta[i] for i in ids if i in meta and meta[i].get("type") == "PUBLIC"]
    same = [c for c in cands if c.get("industry_group") and c.get("industry_group") == ent.get("industry_group")]
    if len(same) < 2:
        same += [c for c in cands if c not in same and c.get("sector") == ent.get("sector")]
    picked = same[: s.PEER_COUNT]

    async def one(p: dict) -> dict:
        series = await bd.sentiment_series(p["id"], chart_start, as_of)
        m = analysis.summarize_series(series, window_start, as_of)
        return {"id": p["id"], "name": p.get("name"), "ticker": p.get("ticker"),
                "change": m.get("change") if m.get("available") else None, "series": series}

    return list(await asyncio.gather(*(one(p) for p in picked)))


def _management(chunks: list[dict], eid: str, n: int = 10) -> list[dict]:
    """The most opinionated, event-tagged passages from the call: what management committed to."""
    tagged = [c for c in chunks if any(d.get("type") == "topic" for d in c["detections"])] or chunks
    tagged.sort(key=lambda c: (abs(c.get("sentiment") or 0) + (c.get("relevance") or 0)), reverse=True)
    out = []
    for i, c in enumerate(tagged[:n], 1):
        out.append({"id": f"M{i}", "date": (c.get("timestamp") or "")[:10], "sentiment": c.get("sentiment"),
                    "text": analysis._trim(c["text"], 500)})
    return out
