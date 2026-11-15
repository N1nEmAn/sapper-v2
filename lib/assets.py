"""Cross-engagement asset store (the "second graph").

The fact graph under `.sapper/` is per-engagement: it is created, fought over,
and abandoned. Assets are the durable layer underneath — an IP seen three
engagements ago is still there, with a record of when it was last confirmed.

    ~/.sapper/assets.db     (override with SAPPER_ASSETS_DB)

Design mirrors the index: SQLite is derivative, never authoritative. Losing it
costs history, not correctness — every asset can be rebuilt from any engagement
by `sapper assets rebuild --from <dir>`.

Asset key shape:  <type>:<value>      e.g. ip:10.0.0.5, service:10.0.0.5:8080
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ASSET_TYPES = ("ip", "domain", "service", "vuln", "cred", "loot", "endpoint")

# Which fact kind to recreate when seeding this asset type back into a graph.
FACT_KIND_BY_ASSET = {
    "ip": "host",
    "domain": "host",
    "service": "service",
    "vuln": "vuln",
    "cred": "cred",
    "loot": "loot",
    "endpoint": "note",  # sapper has no endpoint kind; note is the honest fit
}

IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}$"
)
SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    key        TEXT PRIMARY KEY,
    type       TEXT NOT NULL,
    value      TEXT NOT NULL,
    parent     TEXT,
    label      TEXT,
    data       TEXT NOT NULL DEFAULT '{}',
    sources    TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    seen       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_assets_type   ON assets(type);
CREATE INDEX IF NOT EXISTS idx_assets_parent ON assets(parent);
CREATE INDEX IF NOT EXISTS idx_assets_seen   ON assets(last_seen DESC);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> Path:
    override = os.environ.get("SAPPER_ASSETS_DB")
    if override:
        return Path(override)
    return Path.home() / ".sapper" / "assets.db"


# --------------------------------------------------------------------------- #
# fact -> asset projection
# --------------------------------------------------------------------------- #
def _host_of(data: dict) -> str | None:
    for key in ("host", "hostname", "ip", "domain", "target"):
        raw = data.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip().lower()
    url = data.get("url") or data.get("endpoint")
    if isinstance(url, str) and "://" in url:
        host = urlparse(url).hostname
        return host.lower() if host else None
    return None


def _host_record(host: str) -> dict | None:
    if IP_RE.fullmatch(host):
        return {"key": f"ip:{host}", "type": "ip", "value": host, "parent": None}
    if DOMAIN_RE.fullmatch(host):
        return {"key": f"domain:{host}", "type": "domain", "value": host, "parent": None}
    return None


def _slug(text: str, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit] or "unknown"


def records_from_fact(fact: dict) -> list[dict]:
    """Project one fact onto zero or more assets.

    A `service` fact yields both the service and its host: the host is the
    asset a later engagement searches for, the service is the detail it gets.
    """
    kind = fact.get("kind")
    data = fact.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    out: list[dict] = []
    host = _host_of(data)
    host_rec = _host_record(host) if host else None

    if kind == "host":
        if host_rec:
            out.append(host_rec)

    elif kind == "service":
        port = data.get("port")
        if host and port:
            value = f"{host}:{port}"
            out.append({
                "key": f"service:{value}", "type": "service", "value": value,
                "parent": host_rec["key"] if host_rec else None,
            })
        elif host_rec:
            out.append(host_rec)

    elif kind == "vuln":
        ident = data.get("cve") or data.get("cve_id")
        url = data.get("url") or data.get("endpoint")
        if ident:
            value = str(ident).strip().upper()
        elif url:
            value = url.strip()
        else:
            value = _slug(fact.get("title") or fact.get("description") or "vuln")
        out.append({
            "key": f"vuln:{value}", "type": "vuln", "value": value,
            "parent": host_rec["key"] if host_rec else None,
        })

    elif kind == "cred":
        user = data.get("user") or data.get("username")
        if user:
            value = f"{user}@{host}" if host else str(user)
            out.append({
                "key": f"cred:{value}", "type": "cred", "value": value,
                "parent": host_rec["key"] if host_rec else None,
            })
            if host_rec:
                out.append(host_rec)

    elif kind == "loot":
        path = data.get("path") or data.get("file") or data.get("url")
        if path:
            out.append({
                "key": f"loot:{path}", "type": "loot", "value": str(path),
                "parent": host_rec["key"] if host_rec else None,
            })

    elif kind == "note":
        url = data.get("url") or data.get("endpoint")
        if isinstance(url, str) and url.strip():
            out.append({
                "key": f"endpoint:{url.strip()}", "type": "endpoint",
                "value": url.strip(),
                "parent": host_rec["key"] if host_rec else None,
            })

    # Attach the fact payload so seeded facts keep product/version/banner detail.
    # A host asset only keeps host-level fields: otherwise a cred fact would
    # write user=admin onto the host, and every later engagement inherits it.
    host_only = ("host", "hostname", "ip", "domain", "target", "port", "os", "banner")
    for rec in out:
        payload = {k: v for k, v in data.items() if isinstance(v, (str, int, float))}
        if rec["type"] in ("ip", "domain"):
            payload = {k: v for k, v in payload.items() if k in host_only}
        rec["data"] = payload
        rec["fact"] = fact.get("id")
        rec["label"] = fact.get("title")
    return out


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class AssetDB:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else db_path()

    # -- lifecycle --
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def exists(self) -> bool:
        return self.path.is_file()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def available(self) -> bool:
        if not self.exists():
            return False
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1 FROM assets LIMIT 1")
            return True
        except sqlite3.DatabaseError:
            return False

    # -- writes --
    def upsert(self, rec: dict) -> None:
        key = rec["key"]
        stamp = now_iso()
        payload = json.dumps(rec.get("data") or {}, ensure_ascii=False, sort_keys=True)
        source = {"fact": rec.get("fact"), "root": rec.get("root"), "ts": stamp}
        with self.connect() as conn:
            row = conn.execute("SELECT data, sources, seen FROM assets WHERE key=?", (key,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO assets (key,type,value,parent,label,data,sources,"
                    "first_seen,last_seen,seen) VALUES (?,?,?,?,?,?,?,?,?,1)",
                    (key, rec["type"], rec["value"], rec.get("parent"),
                     rec.get("label"), payload,
                     json.dumps([source], ensure_ascii=False), stamp, stamp),
                )
            else:
                merged = json.loads(row["data"]) if row["data"] else {}
                merged.update(rec.get("data") or {})
                sources = json.loads(row["sources"]) if row["sources"] else []
                if source["fact"]:
                    sources.append(source)
                conn.execute(
                    "UPDATE assets SET parent=COALESCE(?,parent),"
                    " label=COALESCE(label,?),"
                    " data=?, sources=?, last_seen=?, seen=seen+1 WHERE key=?",
                    (rec.get("parent"), rec.get("label"), json.dumps(merged, ensure_ascii=False, sort_keys=True),
                     json.dumps(sources[-20:], ensure_ascii=False), stamp, key),
                )

    def forget(self, key: str | None = None) -> int:
        with self.connect() as conn:
            if key is None:
                cur = conn.execute("DELETE FROM assets")
            else:
                cur = conn.execute("DELETE FROM assets WHERE key=?", (key,))
            return cur.rowcount

    # -- reads --
    def list(self, type: str | None = None, q: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM assets"
        params: list = []
        where = []
        if type:
            where.append("type=?")
            params.append(type)
        if q:
            where.append("(key LIKE ? OR value LIKE ? OR label LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(int(limit))
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
        for r in rows:
            r["data"] = json.loads(r.get("data") or "{}")
            r["sources"] = json.loads(r.get("sources") or "[]")
        return rows

    def get(self, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM assets WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        rec = dict(row)
        rec["data"] = json.loads(rec.get("data") or "{}")
        rec["sources"] = json.loads(rec.get("sources") or "[]")
        return rec

    def stats(self) -> dict:
        with self.connect() as conn:
            by_type = {r["type"]: r["n"] for r in
                       conn.execute("SELECT type, COUNT(*) n FROM assets GROUP BY type")}
            row = conn.execute("SELECT COUNT(*) n, MIN(first_seen) a, MAX(last_seen) b FROM assets").fetchone()
        return {
            "total": row["n"] or 0,
            "by_type": by_type,
            "first_seen": row["a"],
            "last_seen": row["b"],
        }

    def edges(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, parent FROM assets WHERE parent IS NOT NULL AND parent != ''"
            ).fetchall()
        return [{"source": r["parent"], "target": r["key"]} for r in rows]


# --------------------------------------------------------------------------- #
# module-level helpers used by the CLI / event hook
# --------------------------------------------------------------------------- #
def ingest(fact: dict, root: str | None = None) -> int:
    """Record every asset a fact mentions. Never raises: telemetry only."""
    records = records_from_fact(fact)
    if not records:
        return 0
    db = AssetDB()
    if not db.exists():
        db.init()
    for rec in records:
        rec["root"] = root
        db.upsert(rec)
    return len(records)


def rebuild_from(roots: list[Path]) -> dict:
    db = AssetDB()
    db.init()
    seen = 0
    for root in roots:
        facts_dir = Path(root) / ".sapper" / "facts"
        if not facts_dir.is_dir():
            continue
        for path in sorted(facts_dir.glob("*.json")):
            try:
                fact = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            seen += ingest(fact, root=str(root))
    return {"facts_scanned": seen, "assets": db.stats()["total"]}


def seed_into(store, pattern: str, limit: int = 50, dry_run: bool = False) -> tuple[list, int]:
    """Carry matching assets from previous engagements into the current graph.

    Creates facts, not intents: an inherited asset is a *claim to verify*, and
    the guard/commander should treat it as such. The description says where it
    came from so an operator can tell inherited from freshly verified.

    Idempotent: assets already carried into this graph (data.asset_key) are
    skipped, so re-running seed after adding new history only adds the delta.
    """
    db = AssetDB()
    if not db.available():
        return [], 0
    existing = {
        (fact.get("data") or {}).get("asset_key")
        for fact in store.list_facts()
    }
    existing.discard(None)
    rows = db.list(q=pattern, limit=limit)
    created, skipped = [], 0
    for row in rows:
        if row["key"] in existing:
            skipped += 1
            continue
        kind = FACT_KIND_BY_ASSET.get(row["type"], "note")
        data = {
            k: v for k, v in (row.get("data") or {}).items()
            if SAFE_KEY_RE.match(str(k)) and isinstance(v, (str, int, float))
        }
        data["asset_key"] = row["key"]
        data["inherited"] = "true"
        data["last_seen"] = row["last_seen"]
        desc = (f"跨任务继承（历史 {row['seen']} 次观察，最近 {row['last_seen']}）："
                f"{row['value']}。继承值是待复核线索，不是已验证结论。")
        # The asset value is the honest title; the original fact title belongs to
        # a different engagement and would mislead (e.g. a cred title on a host).
        title = row["value"]
        if dry_run:
            created.append({"dry_run": True, "asset": row["key"], "kind": kind,
                            "title": title})
            continue
        fact = store.add_fact(desc, creator="assets", title=title,
                              kind=kind, data=data)
        created.append(fact)
    return created, skipped


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
