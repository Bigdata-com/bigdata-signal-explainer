from datetime import date, timedelta

from app import analysis


def _series(values, start=date(2026, 1, 1)):
    return [{"date": (start + timedelta(days=i)).isoformat(), "daily_sentiment": v,
             "sentiment_pressure": 0.0, "abnormal_media_attention": 0.0} for i, v in enumerate(values)]


def _chunk(doc, sent, topics=(), ent="AAA111", ent_at=(0, 5), ts="2026-03-01"):
    det = [{"type": "entity", "id": ent, "start": ent_at[0], "end": ent_at[1]}]
    det += [{"type": "topic", "id": t, "start": s, "end": e} for t, s, e in topics]
    return {"doc_id": doc, "cnum": 1, "sentiment": sent, "relevance": 1.0, "detections": det, "text": "x",
            "headline": doc, "timestamp": ts, "source": "S", "url": None}


def test_driver_key_and_label():
    assert analysis.driver_key("business,revenues,revenue,up,") == "revenues,revenue,up"
    assert analysis.driver_label("revenues,revenue,up") == "Revenue · up"
    assert analysis.is_reactive("stock-prices,stock-price,gain")


def test_summarize_series_change_and_z():
    vals = [0.1 + 0.001 * (i % 5) for i in range(200)] + [0.1 + 0.01 * i for i in range(31)]
    s = _series(vals)
    as_of = date.fromisoformat(s[-1]["date"])
    m = analysis.summarize_series(s, as_of - timedelta(days=30), as_of)
    assert m["available"] and m["change"] > 0.25
    assert m["z_score"] > 3  # far outside the flat history
    assert len(m["inflection_days"]) == 3


def test_summarize_series_no_lookahead():
    s = _series([0.0] * 100 + [1.0] * 10)
    as_of = date.fromisoformat(s[99]["date"])
    m = analysis.summarize_series(s, as_of - timedelta(days=30), as_of)
    assert m["end_level"] == 0.0


def test_attribution_impacts_sum_to_tone_change():
    lawsuit = ("legal,litigation,lawsuit,,", 0, 20)
    partner = ("business,partnerships,partnership,,", 0, 20)
    prior = [_chunk("p1", 0.6, [partner]), _chunk("p2", 0.4)]
    cur = [_chunk("c1", -0.8, [lawsuit]), _chunk("c2", 0.6, [partner]), _chunk("c3", 0.4)]
    a = analysis.attribute_drivers(cur, "AAA111", prior)
    total = sum(d["impact"] for d in a["drivers"])
    assert abs(total - a["tone_change"]) < 1e-3
    top = a["drivers"][0]
    assert top["key"] == "litigation,lawsuit," and top["direction"] == "negative"
    assert top["evidence_ids"]


def test_faded_positive_news_cites_prior_window():
    partner = ("business,partnerships,partnership,,", 0, 20)
    prior = [_chunk("p1", 0.9, [partner]), _chunk("p2", 0.1)]
    cur = [_chunk("c1", 0.1), _chunk("c2", 0.1)]
    a = analysis.attribute_drivers(cur, "AAA111", prior)
    d = next(d for d in a["drivers"] if d["key"].startswith("partnerships"))
    assert d["direction"] == "negative"
    ev = {e["id"]: e for e in a["evidence"]}
    assert ev[d["evidence_ids"][0]]["window"] == "prior"


def test_topic_far_from_entity_is_not_credited():
    far = ("business,stock-prices,stock-price,loss,", 400, 440)
    a = analysis.attribute_drivers([_chunk("c1", -0.7, [far], ent_at=(0, 5))], "AAA111")
    assert a["drivers"][0]["key"] == analysis.GENERAL


def test_peer_verdicts():
    assert analysis.peer_context(-0.10, [{"change": -0.09}, {"change": -0.08}])["verdict"] == "sector-wide"
    assert analysis.peer_context(-0.10, [{"change": 0.01}, {"change": -0.01}])["verdict"] == "stock-specific"
    assert analysis.peer_context(-0.10, [])["available"] is False
