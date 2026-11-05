"""SQLite persistence: users, explain runs, batches, resolved entities and a response cache for Bigdata.com calls.

Runs and batches belong to a user. A user is identified by a fingerprint of their Bigdata API key; the key itself is
never written here.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    rp_entity_id TEXT PRIMARY KEY, ticker TEXT, name TEXT, sector TEXT, industry_group TEXT,
    data TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS ticker_map (query TEXT PRIMARY KEY, rp_entity_id TEXT);
CREATE TABLE IF NOT EXISTS api_cache (key TEXT PRIMARY KEY, response TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, name TEXT, org_id TEXT, created_at TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, name TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, batch_id INTEGER,
    ticker TEXT, rp_entity_id TEXT, company TEXT, as_of TEXT, window_start TEXT, lookback_days INTEGER,
    mode TEXT, input_score REAL, status TEXT, progress TEXT, error TEXT,
    surprise REAL, change REAL, headline TEXT, model TEXT,
    facts TEXT, narrative TEXT);
CREATE INDEX IF NOT EXISTS runs_batch ON runs(batch_id);
"""

# Columns added after the first release; ALTER TABLE keeps existing databases working.
MIGRATIONS = [("runs", "user_id", "TEXT"), ("batches", "user_id", "TEXT"),
              # Demo runs anyone can open, signed in or not.
              ("runs", "public", "INTEGER NOT NULL DEFAULT 0")]

RUN_JSON = ("facts", "narrative")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            for table, col, typ in MIGRATIONS:
                if col not in {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            self.conn.execute("CREATE INDEX IF NOT EXISTS runs_user ON runs(user_id, id)")
            self.conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # --- cache -------------------------------------------------------------------------------------------------
    def cache_get(self, key: str) -> Any:
        row = self.one("SELECT response FROM api_cache WHERE key=?", (key,))
        return json.loads(row["response"]) if row else None

    def cache_put(self, key: str, response: Any) -> None:
        self.execute("INSERT OR REPLACE INTO api_cache VALUES (?,?,?)", (key, json.dumps(response), now_iso()))

    # --- entities ----------------------------------------------------------------------------------------------
    def put_entity(self, e: dict) -> None:
        self.execute("INSERT OR REPLACE INTO entities VALUES (?,?,?,?,?,?,?)",
                     (e["id"], e.get("ticker"), e.get("name"), e.get("sector"), e.get("industry_group"),
                      json.dumps(e), now_iso()))

    def get_entity(self, rp_entity_id: str) -> dict | None:
        row = self.one("SELECT data FROM entities WHERE rp_entity_id=?", (rp_entity_id,))
        return json.loads(row["data"]) if row else None

    # --- users -------------------------------------------------------------------------------------------------
    def touch_user(self, user_id: str, name: str | None = None, org_id: str | None = None) -> dict:
        now = now_iso()
        self.execute("INSERT INTO users (id, name, org_id, created_at, last_seen) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, "
                     "name=COALESCE(excluded.name, users.name), org_id=COALESCE(excluded.org_id, users.org_id)",
                     (user_id, name or None, org_id, now, now))
        return self.one("SELECT * FROM users WHERE id=?", (user_id,))

    def get_user(self, user_id: str) -> dict | None:
        return self.one("SELECT * FROM users WHERE id=?", (user_id,))

    def claim_unowned(self, user_id: str) -> None:
        """Give runs and batches created before users existed to this user."""
        self.execute("UPDATE runs SET user_id=? WHERE user_id IS NULL", (user_id,))
        self.execute("UPDATE batches SET user_id=? WHERE user_id IS NULL", (user_id,))

    # --- runs --------------------------------------------------------------------------------------------------
    def create_run(self, **fields: Any) -> int:
        fields = {"created_at": now_iso(), "status": "queued", "progress": "Queued", **fields}
        cols = ",".join(fields)
        return self.execute(f"INSERT INTO runs ({cols}) VALUES ({','.join('?' * len(fields))})", tuple(fields.values()))

    def update_run(self, run_id: int, **fields: Any) -> None:
        for k in RUN_JSON:
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        sets = ",".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), run_id))

    def get_run(self, run_id: int) -> dict | None:
        r = self.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if r:
            for k in RUN_JSON:
                r[k] = json.loads(r[k]) if r[k] else None
        return r

    def recent_runs(self, user_id: str, limit: int = 15) -> list[dict]:
        return self.all("SELECT id, created_at, ticker, company, as_of, lookback_days, mode, status, headline, change, "
                        "surprise FROM runs WHERE batch_id IS NULL AND user_id=? AND public=0 ORDER BY id DESC LIMIT ?",
                        (user_id, limit))

    def public_runs(self) -> list[dict]:
        return self.all("SELECT id, created_at, ticker, company, as_of, lookback_days, mode, status, headline, change, "
                        "surprise FROM runs WHERE public=1 ORDER BY id DESC")

    def batch_runs(self, batch_id: int) -> list[dict]:
        return self.all("SELECT id, ticker, company, as_of, lookback_days, mode, input_score, status, progress, error, "
                        "headline, change, surprise FROM runs WHERE batch_id=? ORDER BY id", (batch_id,))
