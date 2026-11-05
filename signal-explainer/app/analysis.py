"""Deterministic analytics: how unusual the move is, which events drove it, and whether it was stock-specific.

Everything here is pure (no I/O) so the numbers behind a narrative can be tested and reproduced.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

# EDGE groups that describe the market's reaction rather than a fundamental cause.
REACTIVE_GROUPS = {"stock-prices", "indexes", "technical-analysis", "commodity-prices", "currency-prices",
                   "market-price", "trading", "stock-picks"}

GENERAL = "general"


# --- topics ------------------------------------------------------------------------------------------------------
def driver_key(topic_id: str) -> str:
    """'business,revenues,revenue,up,' -> 'revenues,revenue,up' (group, type, sub-type; property dropped)."""
    parts = (topic_id.split(",") + [""] * 5)[:5]
    return ",".join(p for p in parts[1:4])


def driver_label(key: str) -> str:
    if key == GENERAL:
        return "General coverage (no tagged event)"
    group, typ, sub = (key.split(",") + ["", ""])[:3]
    base = (typ or group).replace("-", " ").strip().capitalize()
    return f"{base} · {sub.replace('-', ' ')}" if sub else base


def is_reactive(key: str) -> bool:
    return key.split(",")[0] in REACTIVE_GROUPS


# --- sentiment series --------------------------------------------------------------------------------------------
def _value_on(series: list[dict], d: date, field: str = "daily_sentiment") -> tuple[str, float] | None:
    """Value on ``d`` or the latest earlier date that has one."""
    best = None
    for v in series:
        if v["date"] <= d.isoformat() and v.get(field) is not None:
            best = (v["date"], v[field])
    return best


def summarize_series(series: list[dict], window_start: date, as_of: date) -> dict:
    """Level, change and unusualness of the entity's daily sentiment over [window_start, as_of]."""
    s0, s1 = _value_on(series, window_start), _value_on(series, as_of)
    if not s0 or not s1:
        return {"available": False}
    lookback = (as_of - window_start).days
    change = s1[1] - s0[1]

    # Distribution of same-length changes before the window (no look-ahead).
    hist = [v for v in series if v["date"] < window_start.isoformat() and v.get("daily_sentiment") is not None]
    by_date = {v["date"]: v["daily_sentiment"] for v in hist}
    past = []
    for v in hist:
        prev = (date.fromisoformat(v["date"]) - timedelta(days=lookback)).isoformat()
        if prev in by_date:
            past.append(v["daily_sentiment"] - by_date[prev])
    z = pct = None
    if len(past) >= 20:
        # Floor the spread so a near-flat history doesn't turn every move into 0σ or ∞σ.
        mu, sd = statistics.fmean(past), max(statistics.pstdev(past), 0.005)
        z = (change - mu) / sd
        pct = sum(1 for x in past if abs(x) <= abs(change)) / len(past)

    win = [v for v in series if window_start.isoformat() <= v["date"] <= as_of.isoformat()]
    deltas = []
    for a, b in zip(win, win[1:]):
        if a.get("daily_sentiment") is not None and b.get("daily_sentiment") is not None:
            deltas.append({"date": b["date"], "delta": b["daily_sentiment"] - a["daily_sentiment"]})
    inflections = sorted(deltas, key=lambda x: abs(x["delta"]), reverse=True)[:3]
    ama = [v for v in win if v.get("abnormal_media_attention") is not None]
    peak = max(ama, key=lambda v: v["abnormal_media_attention"]) if ama else None
    last = win[-1] if win else {}
    return {
        "available": True,
        "start_date": s0[0], "start_level": round(s0[1], 4),
        "end_date": s1[0], "end_level": round(s1[1], 4),
        "change": round(change, 4),
        "z_score": None if z is None else round(z, 2),
        "percentile": None if pct is None else round(pct, 3),
        "history_samples": len(past),
        "sentiment_pressure": last.get("sentiment_pressure"),
        "abnormal_media_attention": last.get("abnormal_media_attention"),
        "peak_attention": {"date": peak["date"], "value": round(peak["abnormal_media_attention"], 3)} if peak else None,
        "inflection_days": [{"date": x["date"], "delta": round(x["delta"], 4)} for x in sorted(inflections, key=lambda x: x["date"])],
    }


def describe_move(summary: dict) -> str:
    """Plain words for how unusual the change is."""
    z = summary.get("z_score")
    if z is None:
        return "not enough history to judge"
    a = abs(z)
    size = "an exceptional" if a >= 3 else "a large" if a >= 2 else "a notable" if a >= 1 else "a routine"
    return f"{size} {'improvement' if summary['change'] >= 0 else 'deterioration'} ({z:+.1f}σ vs its own history)"


# --- driver attribution ------------------------------------------------------------------------------------------
# A topic is credited to the company only when the company is named inside, or this close to, the topic's span.
PROXIMITY_CHARS = 60


def _topics_for(c: dict, rp_entity_id: str) -> list[str] | None:
    """Driver keys this passage contributes to for the entity; None if the entity is not mentioned at all."""
    mentions = [(d["start"], d["end"]) for d in c["detections"]
                if d.get("type") == "entity" and d.get("id") == rp_entity_id]
    if not mentions:
        return None
    keys = {driver_key(d["id"]) for d in c["detections"] if d.get("type") == "topic" and any(
        s <= d["end"] + PROXIMITY_CHARS and e >= d["start"] - PROXIMITY_CHARS for s, e in mentions)}
    return sorted(keys) or [GENERAL]


def _bucket(chunks: list[dict], rp_entity_id: str) -> tuple[dict, int]:
    """Per driver: summed sentiment, passage weight, documents, dates and member passages."""
    b: dict[str, dict] = defaultdict(lambda: {"net": 0.0, "vol": 0.0, "docs": set(), "dates": [], "members": []})
    used = 0
    for c in chunks:
        if c.get("sentiment") is None or (keys := _topics_for(c, rp_entity_id)) is None:
            continue
        used += 1
        w = 1.0 / len(keys)
        for k in keys:
            x = b[k]
            x["net"] += c["sentiment"] * w
            x["vol"] += w
            x["docs"].add(c["doc_id"])
            x["dates"].append((c.get("timestamp") or "")[:10])
            x["members"].append(c)
    return b, used


def attribute_drivers(chunks: list[dict], rp_entity_id: str, prior_chunks: list[dict] | None = None, *,
                      max_drivers: int = 6, evidence_per_driver: int = 3,
                      topic_lift: dict[str, dict] | None = None) -> dict:
    """Decompose the change in average news tone about the entity into EDGE event types.

    Each passage mentioning the entity adds its sentiment, shared equally across the event types tagged next to the
    entity (untagged passages go to 'general'). A driver's contribution is its summed sentiment divided by all
    passages in the window, so contributions add up to the window's average tone. The driver's impact on the move
    is its contribution now minus its contribution in the prior window of the same length; impacts add up to the
    change in average tone. A driver can weigh on sentiment through new negative news or through positive news
    fading away, which is why evidence may come from the prior window.
    """
    cur, n_cur = _bucket(chunks, rp_entity_id)
    pri, n_pri = _bucket(prior_chunks or [], rp_entity_id)
    has_prior = n_pri > 0
    contrib = lambda b, k, n: (b[k]["net"] / n) if (n and k in b) else 0.0  # noqa: E731
    impact = {k: contrib(cur, k, n_cur) - (contrib(pri, k, n_pri) if has_prior else 0.0) for k in set(cur) | set(pri)}
    total_abs = sum(abs(v) for v in impact.values()) or 1.0
    ranked = sorted(impact, key=lambda k: abs(impact[k]), reverse=True)

    drivers, evidence, n = [], [], 0
    for k in ranked[:max_drivers]:
        sign = 1 if impact[k] >= 0 else -1
        # New news in the move's direction first; otherwise the faded prior-window news it replaced.
        pools = [("current", [c for c in cur[k]["members"] if c["sentiment"] * sign > 0] if k in cur else []),
                 ("prior", [c for c in pri[k]["members"] if c["sentiment"] * sign < 0] if k in pri else [])]
        picks, seen = [], set()
        for window, pool in pools:
            for c in sorted(pool, key=lambda c: abs(c["sentiment"]) * (c.get("relevance") or 0.5), reverse=True):
                if len(picks) >= evidence_per_driver:
                    break
                if c["doc_id"] in seen:
                    continue
                seen.add(c["doc_id"])
                n += 1
                evidence.append({"id": f"E{n}", "driver": k, "window": window, "headline": c.get("headline"),
                                 "date": (c.get("timestamp") or "")[:10], "source": c.get("source"),
                                 "url": c.get("url"), "sentiment": c["sentiment"], "text": _trim(c["text"])})
                picks.append(f"E{n}")
            if picks:
                break
        x = cur.get(k) or {"net": 0.0, "vol": 0.0, "docs": set(), "dates": []}
        ds = sorted(d for d in x["dates"] if d)
        drivers.append({
            "key": k, "label": driver_label(k), "reactive": is_reactive(k),
            "impact": round(impact[k], 4), "share": round(abs(impact[k]) / total_abs, 3),
            "direction": "positive" if impact[k] > 0.002 else "negative" if impact[k] < -0.002 else "neutral",
            "contribution_now": round(contrib(cur, k, n_cur), 4),
            "contribution_prior": round(contrib(pri, k, n_pri), 4) if has_prior else None,
            "documents": len(x["docs"]), "prior_documents": len(pri[k]["docs"]) if k in pri else 0,
            "avg_sentiment": round(x["net"] / x["vol"], 3) if x["vol"] else None,
            "first_seen": ds[0] if ds else None, "last_seen": ds[-1] if ds else None,
            "lift": (topic_lift or {}).get(k), "evidence_ids": picks,
        })
    tone_now = sum(v["net"] for v in cur.values()) / n_cur if n_cur else None
    tone_prior = sum(v["net"] for v in pri.values()) / n_pri if n_pri else None
    return {"passages_used": n_cur, "passages_seen": len(chunks), "prior_passages_used": n_pri,
            "tone_now": None if tone_now is None else round(tone_now, 4),
            "tone_prior": None if tone_prior is None else round(tone_prior, 4),
            "tone_change": round(tone_now - tone_prior, 4) if tone_now is not None and tone_prior is not None else None,
            "drivers": drivers, "evidence": evidence}


def topic_lift(current: list[dict], prior: list[dict], cur_days: int, prior_days: int) -> dict[str, dict]:
    """Per-day mention rate of each driver in the window vs the window before it (co-mention topic counts)."""
    def agg(rows: list[dict]) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for r in rows:
            out[driver_key(r["id"])] += r.get("total_chunks_count") or 0
        return out

    cur, pri = agg(current), agg(prior)
    res = {}
    for k, c in cur.items():
        cr, pr = c / max(cur_days, 1), pri.get(k, 0) / max(prior_days, 1)
        res[k] = {"per_day": round(cr, 1), "prior_per_day": round(pr, 1),
                  "ratio": round(cr / pr, 2) if pr > 0 else None, "new": pr == 0}
    return res


# --- peers -------------------------------------------------------------------------------------------------------
def peer_context(target_change: float, peers: list[dict]) -> dict:
    """peers: [{name, ticker, change}]. Splits the move into a sector part (peer median) and a stock-specific rest."""
    ch = [p["change"] for p in peers if p.get("change") is not None]
    if not ch:
        return {"available": False, "peers": peers}
    med = statistics.median(ch)
    idio = target_change - med
    if abs(target_change) < 0.01:
        verdict = "no meaningful move"
    elif med * target_change > 0 and abs(med) >= 0.6 * abs(target_change):
        verdict = "sector-wide"
    elif abs(med) <= 0.3 * abs(target_change) or med * target_change < 0:
        verdict = "stock-specific"
    else:
        verdict = "mixed"
    return {"available": True, "peers": peers, "peer_median_change": round(med, 4),
            "idiosyncratic_change": round(idio, 4), "verdict": verdict}


def _trim(text: str, n: int = 420) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"
