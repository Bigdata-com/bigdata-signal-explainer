"""Bigdata.com REST client: entity resolution, entity sentiment, news search, co-mentions and transcripts.

Every call uses the signed-in user's own API key (see `for_key`). Responses for windows that end before today are
cached in SQLite per user, so re-running an explanation for a past date is free and returns the same point-in-time
inputs. Knowledge Graph lookups are reference data and are shared.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from datetime import date, timedelta
from typing import Any

import httpx

from .config import Settings
from .store import Store

log = logging.getLogger("bigdata")


class BigdataError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"Bigdata.com [{status}] {message}")
        self.status = status


def _ts(d: date, end: bool = False) -> str:
    return f"{d.isoformat()}T23:59:59Z" if end else f"{d.isoformat()}T00:00:00Z"


class BigdataClient:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0))
        self.sem = asyncio.Semaphore(4)
        # No key of its own: every caller goes through for_key with the signed-in user's key.
        self.api_key = ""
        self.scope = "shared"

    def for_key(self, api_key: str, scope: str) -> "BigdataClient":
        """A view of this client that calls Bigdata.com with ``api_key`` and caches under ``scope`` (the user id)."""
        c = copy.copy(self)
        c.api_key, c.scope = api_key, scope
        return c

    async def close(self) -> None:
        await self.http.aclose()

    async def quotas(self) -> dict:
        """Validates the key; also returns the account's org id."""
        return await self.request("GET", "/v1/subscription/quotas", None, cacheable=False, retries=1)

    async def post(self, path: str, body: dict, *, cacheable: bool = True) -> Any:
        return await self.request("POST", path, body, cacheable=cacheable)

    async def request(self, method: str, path: str, body: dict | None, *, cacheable: bool = True,
                      retries: int = 4) -> Any:
        scope = "shared" if path.startswith("/v1/knowledge-graph") else self.scope
        key = hashlib.sha256(f"{scope}|{path}|{json.dumps(body, sort_keys=True)}".encode()).hexdigest()
        if cacheable and (hit := self.store.cache_get(key)) is not None:
            return hit
        if not self.api_key:
            raise BigdataError(401, "No Bigdata API key. Sign in first.")
        url = self.s.BIGDATA_API_URL.rstrip("/") + path
        headers = {"x-api-key": self.api_key, "content-type": "application/json"}
        delay = 1.5
        for attempt in range(retries):
            last = attempt == retries - 1
            async with self.sem:
                try:
                    r = await self.http.request(method, url, headers=headers, json=body)
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    if last:
                        raise BigdataError(0, str(e)) from e
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
            if r.status_code in (429, 500, 502, 503, 504) and not last:
                await asyncio.sleep(float(r.headers.get("retry-after") or delay))
                delay *= 2
                continue
            if r.status_code >= 400:
                raise BigdataError(r.status_code, r.text[:300])
            payload = r.json()
            if cacheable:
                self.store.cache_put(key, payload)
            return payload
        raise BigdataError(599, "retries exhausted")

    # --- entities ----------------------------------------------------------------------------------------------
    async def resolve(self, query: str) -> dict | None:
        """Ticker, name or rp_entity_id to a Knowledge Graph company record (public listings preferred)."""
        q = query.strip()
        mapped = self.store.one("SELECT rp_entity_id FROM ticker_map WHERE query=?", (q.upper(),))
        if mapped and (e := self.store.get_entity(mapped["rp_entity_id"])):
            return e
        if len(q) == 6 and q.isalnum() and q.upper() == q:
            got = await self.entities_by_id([q])
            if got.get(q):
                return got[q]
        res = await self.post("/v1/knowledge-graph/companies", {"query": q, "types": ["PUBLIC"]})
        cands = res.get("results") or []
        if not cands:
            res = await self.post("/v1/knowledge-graph/companies", {"query": q})
            cands = res.get("results") or []
        if not cands:
            return None
        exact = [c for c in cands if (c.get("ticker") or "").upper() == q.upper()]
        e = (exact or cands)[0]
        self.store.put_entity(e)
        self.store.execute("INSERT OR REPLACE INTO ticker_map VALUES (?,?)", (q.upper(), e["id"]))
        return e

    async def entities_by_id(self, ids: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        todo = []
        for i in ids:
            if e := self.store.get_entity(i):
                out[i] = e
            else:
                todo.append(i)
        if todo:
            res = await self.post("/v1/knowledge-graph/entities/id", {"values": todo})
            for i, e in (res.get("results") or {}).items():
                if e:
                    e.setdefault("id", i)
                    out[i] = e
                    self.store.put_entity(e)
        return out

    # --- sentiment ---------------------------------------------------------------------------------------------
    async def sentiment_series(self, rp_entity_id: str, start: date, end: date) -> list[dict]:
        res = await self.post("/v1/entity-sentiment/", {
            "identifier": {"type": "rp_entity_id", "value": rp_entity_id},
            "timestamp": {"start": start.isoformat(), "end": end.isoformat()}}, cacheable=end < date.today())
        r = res.get("results")
        r = r[0] if isinstance(r, list) and r else r
        return sorted((r or {}).get("values") or [], key=lambda v: v["date"])

    # --- search ------------------------------------------------------------------------------------------------
    async def news_chunks(self, rp_entity_id: str, start: date, end: date) -> list[dict]:
        """All news passages mentioning the entity in [start, end], searched in day slices for even coverage.

        Returns flat passage dicts: doc_id, headline, timestamp, source, url, cnum, text, sentiment, relevance,
        detections.
        """
        days = (end - start).days + 1
        n = min(self.s.SEARCH_SLICES, days)
        step = max(1, -(-days // n))
        slices = []
        d = start
        while d <= end:
            e = min(end, d + timedelta(days=step - 1))
            slices.append((d, e))
            d = e + timedelta(days=1)

        async def one(a: date, b: date) -> list[dict]:
            body = {"search_mode": "fast", "query": {
                "filters": {"entity": {"any_of": [rp_entity_id]}, "timestamp": {"start": _ts(a), "end": _ts(b, True)},
                            "document_type": {"mode": "INCLUDE", "values": ["NEWS"]}},
                "max_chunks": self.s.SLICE_CHUNKS, "ranking_params": {"freshness_boost": 0}}}
            res = await self.post("/v1/search", body, cacheable=b < date.today())
            return _flatten(res)

        parts = await asyncio.gather(*(one(a, b) for a, b in slices))
        seen, out = set(), []
        for p in parts:
            for c in p:
                k = (c["doc_id"], c["cnum"])
                if k not in seen:
                    seen.add(k)
                    out.append(c)
        return out

    async def co_mentions(self, rp_entity_id: str, start: date, end: date, kind: str, limit: int = 50) -> list[dict]:
        """kind: 'topics' or 'entities'. Returns [{id, total_chunks_count, ...}] (entities: companies only)."""
        res = await self.post(f"/v1/search/co-mentions/{kind}", {
            "query": {"filters": {"entity": {"any_of": [rp_entity_id]},
                                  "timestamp": {"start": _ts(start), "end": _ts(end, True)}}},
            "limit": limit}, cacheable=end < date.today())
        r = res.get("results") or {}
        return r.get("topics" if kind == "topics" else "companies") or []

    async def last_earnings_call(self, rp_entity_id: str, before: date) -> dict | None:
        """Most recent earnings-call transcript on or before ``before`` (within ~6 months)."""
        body = {"search_mode": "fast", "query": {
            "filters": {"entity": {"any_of": [rp_entity_id]},
                        "timestamp": {"start": _ts(before - timedelta(days=200)), "end": _ts(before, True)},
                        "document_type": {"mode": "INCLUDE", "values": ["TRANSCRIPT"]}},
            "max_chunks": 60}}
        res = await self.post("/v1/search", body, cacheable=before < date.today())
        calls = [d for d in res.get("results") or [] if "earnings call" in (d.get("headline") or "").lower()]
        if not calls:
            return None
        calls.sort(key=lambda d: d["timestamp"], reverse=True)
        return calls[0]

    async def transcript_chunks(self, doc: dict, rp_entity_id: str) -> list[dict]:
        """Passages from the earnings call itself (the day of the call), for 'what management said'."""
        day = date.fromisoformat(doc["timestamp"][:10])
        body = {"search_mode": "fast", "query": {
            "filters": {"entity": {"any_of": [rp_entity_id]},
                        "timestamp": {"start": _ts(day), "end": _ts(day + timedelta(days=1), True)},
                        "document_type": {"mode": "INCLUDE", "values": ["TRANSCRIPT"]}},
            "max_chunks": 80}}
        res = await self.post("/v1/search", body)
        return [c for c in _flatten(res) if c["doc_id"] == doc["id"]] or _flatten(res)


def _flatten(res: dict) -> list[dict]:
    out = []
    for d in res.get("results") or []:
        src = d.get("source") or {}
        for ch in d.get("chunks") or []:
            out.append({"doc_id": d["id"], "headline": d.get("headline"), "timestamp": d.get("timestamp"),
                        "source": src.get("name"), "source_rank": src.get("rank"), "url": d.get("url"),
                        "cnum": ch.get("cnum"), "text": ch.get("text") or "", "sentiment": ch.get("sentiment"),
                        "relevance": ch.get("relevance"), "detections": ch.get("detections") or []})
    return out
