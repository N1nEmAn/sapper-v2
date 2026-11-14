"""SQLite index over the fact graph.

The JSON files under `.sapper/` remain the source of truth. This module keeps
a derived, queryable mirror next to them (`.sapper/index.db`) so the web
console and `sapper serve` can read the graph without walking hundreds of
files, and can stream changes by sequence number.

Design rules:
- The index is optional. If it is missing or corrupt, the CLI must keep
  working; callers treat every failure here as non-fatal.
- Writes are incremental: every domain event appends a row.
- `rebuild()` re-imports everything from the JSON files (used by serve at
  startup, and after a schema change).

Delete `.sapper/index.db` to disable indexing entirely.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DB_NAME = "index.db"
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    description   TEXT NOT NULL,
    doc           TEXT,
    kind          TEXT,
    data          TEXT,
    created_at    TEXT NOT NULL,
    creator       TEXT NOT NULL,
    source_intent TEXT
);

CREATE TABLE IF NOT EXISTS intents (
    id                    TEXT PRIMARY KEY,
    title                 TEXT,
    description           TEXT NOT NULL,
    doc                   TEXT,
    from_ids              TEXT NOT NULL,
    to_id                 TEXT,
    status                TEXT NOT NULL,
    worker                TEXT,
    requires_confirmation INTEGER DEFAULT 0,
    confirmed_at          TEXT,
    created_at            TEXT NOT NULL,
    concluded_at          TEXT,
    creator               TEXT
);

CREATE TABLE IF NOT EXISTS events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    event   TEXT NOT NULL,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def index_path(root: Path) -> Path:
    """root is the .sapper directory."""
    return root / DB_NAME


class IndexDB:
    def __init__(self, path: Path) -> None:
        self.path = path

    # -- lifecycle ---------------------------------------------------------
    def exists(self) -> bool:
        return self.path.exists()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('created_at', ?)",
                (_now(),),
            )

    # -- writes ------------------------------------------------------------
    def upsert_fact(self, fact: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO facts(id, title, description, doc, kind, data,
                                     created_at, creator, source_intent)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title, description=excluded.description,
                     doc=excluded.doc, kind=excluded.kind, data=excluded.data,
                     created_at=excluded.created_at, creator=excluded.creator,
                     source_intent=excluded.source_intent""",
                (
                    fact.get("id"),
                    fact.get("title"),
                    fact.get("description", ""),
                    fact.get("doc"),
                    fact.get("kind"),
                    json.dumps(fact.get("data") or {}, ensure_ascii=False),
                    fact.get("created_at", _now()),
                    fact.get("creator", "unknown"),
                    fact.get("source_intent"),
                ),
            )

    def upsert_intent(self, intent: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO intents(id, title, description, doc, from_ids,
                                       to_id, status, worker, requires_confirmation,
                                       confirmed_at, created_at, concluded_at, creator)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title, description=excluded.description,
                     doc=excluded.doc, from_ids=excluded.from_ids,
                     to_id=excluded.to_id, status=excluded.status,
                     worker=excluded.worker,
                     requires_confirmation=excluded.requires_confirmation,
                     confirmed_at=excluded.confirmed_at,
                     concluded_at=excluded.concluded_at""",
                (
                    intent.get("id"),
                    intent.get("title"),
                    intent.get("description", ""),
                    intent.get("doc"),
                    json.dumps(intent.get("from") or [], ensure_ascii=False),
                    intent.get("to"),
                    intent.get("status", "open"),
                    intent.get("worker"),
                    1 if intent.get("requires_confirmation") else 0,
                    intent.get("confirmed_at"),
                    intent.get("created_at", _now()),
                    intent.get("concluded_at"),
                    intent.get("creator"),
                ),
            )

    def append_event(self, event: str, payload: dict) -> None:
        safe = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO events(ts, event, payload) VALUES(?,?,?)",
                (_now(), event, json.dumps(safe, ensure_ascii=False)),
            )

    # -- reads -------------------------------------------------------------
    def graph(self) -> dict:
        with self.connect() as conn:
            facts = [dict(r) for r in conn.execute("SELECT * FROM facts ORDER BY id")]
            intents = [dict(r) for r in conn.execute("SELECT * FROM intents ORDER BY id")]
        for f in facts:
            f["data"] = json.loads(f.get("data") or "{}")
        for i in intents:
            # present the same field names the JSON files use, so callers do
            # not need to know about SQLite column names.
            i["from"] = json.loads(i.pop("from_ids", "[]") or "[]")
            i["to"] = i.pop("to_id", None)
            i["requires_confirmation"] = bool(i.get("requires_confirmation"))
        return {"facts": facts, "intents": intents, "stats": _stats(facts, intents)}

    def events_since(self, seq: int = 0, limit: int = 200) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT seq, ts, event, payload FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                (seq, limit),
            )
            return [
                {
                    "seq": r["seq"],
                    "ts": r["ts"],
                    "event": r["event"],
                    "payload": json.loads(r["payload"] or "{}"),
                }
                for r in rows
            ]

    def last_seq(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS s FROM events").fetchone()
            return int(row["s"] if row else 0)

    # -- bulk --------------------------------------------------------------
    def rebuild(self, facts: Iterable[dict], intents: Iterable[dict]) -> dict:
        with self.connect() as conn:
            conn.execute("DELETE FROM facts")
            conn.execute("DELETE FROM intents")
        n_f = n_i = 0
        for fact in facts:
            self.upsert_fact(fact)
            n_f += 1
        for intent in intents:
            self.upsert_intent(intent)
            n_i += 1
        return {"facts": n_f, "intents": n_i}


def _stats(facts: list[dict], intents: list[dict]) -> dict:
    open_i = sum(1 for i in intents if i.get("to") is None)
    done_i = len(intents) - open_i
    kinds: dict[str, int] = {}
    for f in facts:
        k = f.get("kind") or "note"
        kinds[k] = kinds.get(k, 0) + 1
    return {
        "facts": len(facts),
        "intents": len(intents),
        "open": open_i,
        "done": done_i,
        "kinds": kinds,
    }
