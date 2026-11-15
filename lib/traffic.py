"""HTTP traffic capture, filtered at the door.

A proxy sees tens of thousands of requests per site. Storing them all turns a
useful evidence table into a landfill within days, so the filter runs *before*
the insert:

    keep  → dynamic content (html/json/xml), auth-ish status codes,
            state-changing methods, security-relevant paths and parameters
    drop  → static assets, 304s, media

Storage is its own SQLite file (`<project>/.sapper/traffic.db`), deliberately
separate from `index.db`: rebuilding the fact index must never wipe evidence.

Capture happens in a mitmproxy addon (tools/mitm_sapper.py) which imports this
module directly, so the filter rules live in exactly one place.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DB_NAME = "traffic.db"

STATIC_EXT = {
    ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".webp", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".webm", ".mp3",
    ".map", ".avif", ".bmp",
}

# Paths that usually carry the interesting bugs.
HOT_PATH = (
    "login", "logout", "auth", "oauth", "token", "signin", "signup", "register",
    "admin", "manage", "console", "dashboard", "api/", "/api", "graphql",
    "upload", "download", "export", "import", "backup", "config", "setting",
    "swagger", "openapi", ".git", ".env", "phpinfo", "debug", "test", "dev",
    "user", "account", "order", "pay", "file", "path", "redirect", "callback",
)

# Query/body parameter names that tend to be exploitable.
HOT_PARAM = (
    "id=", "url=", "uri=", "path=", "file=", "page=", "cmd=", "exec=",
    "redirect=", "next=", "callback=", "token=", "key=", "debug=", "sql=",
    "query=", "data=", "host=", "domain=", "name=",
)

KEEP_STATUS = {200, 201, 202, 206, 301, 302, 303, 307, 308,
               401, 403, 405, 500, 502, 503}
KEEP_MIME = ("json", "xml", "html", "text/plain", "javascript", "form")

MAX_BODY = int(os.environ.get("SAPPER_TRAFFIC_MAX_BODY", "8192"))
MAX_HEADERS = 4096


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path(store_root: Path) -> Path:
    return Path(store_root) / DB_NAME


def _truncate(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    return text[:limit]


def should_store(rec: dict) -> tuple[bool, str]:
    """Return (keep, reason). Reason is recorded for the dropped ones only."""
    path = (rec.get("path") or "/").lower()
    url = (rec.get("url") or "").lower()
    mime = (rec.get("mime") or "").lower()
    status = rec.get("status") or 0
    method = (rec.get("method") or "GET").upper()
    ext = os.path.splitext(urlparse(url).path)[1].lower()

    if ext in STATIC_EXT:
        return False, "static"
    if status == 304:
        return False, "not-modified"
    if status and status >= 400 and status not in KEEP_STATUS:
        return False, f"status-{status}"
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return True, "state-changing"
    if status in (401, 403, 500, 502, 503):
        return True, "error-surface"
    if any(tag in path for tag in HOT_PATH):
        return True, "hot-path"
    if any(tag in url for tag in HOT_PARAM):
        return True, "hot-param"
    if 200 <= status < 400 and any(m in mime for m in KEEP_MIME):
        return True, "content"
    return False, "uninteresting"


class TrafficStore:
    def __init__(self, root: Path | str):
        self.path = db_path(Path(root))
        self._ready = False

    # -- lifecycle --
    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS traffic (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts           TEXT NOT NULL,
                    method       TEXT NOT NULL,
                    scheme       TEXT,
                    host         TEXT,
                    port         INTEGER,
                    path         TEXT NOT NULL,
                    url          TEXT NOT NULL,
                    status       INTEGER,
                    mime         TEXT,
                    req_size     INTEGER,
                    resp_size    INTEGER,
                    req_headers  TEXT,
                    req_body     TEXT,
                    resp_headers TEXT,
                    resp_body    TEXT,
                    tags         TEXT,
                    source       TEXT,
                    flow_id      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_traffic_host ON traffic(host);
                CREATE INDEX IF NOT EXISTS idx_traffic_ts   ON traffic(ts DESC);
                CREATE INDEX IF NOT EXISTS idx_traffic_st   ON traffic(status);
                CREATE TABLE IF NOT EXISTS traffic_meta (k TEXT PRIMARY KEY, v TEXT);
                """
            )

    def exists(self) -> bool:
        return self.path.is_file()

    def available(self) -> bool:
        if not self.exists():
            return False
        try:
            with self.connect() as conn:
                conn.execute("SELECT 1 FROM traffic LIMIT 1")
            return True
        except sqlite3.DatabaseError:
            return False

    # -- writes --
    def add(self, rec: dict) -> int | None:
        keep, reason = should_store(rec)
        if not keep:
            return None
        if not self._ready:
            self.init()
            self._ready = True
        tags = list(rec.get("tags") or [])
        tags.append(reason)
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO traffic (ts,method,scheme,host,port,path,url,status,mime,"
                "req_size,resp_size,req_headers,req_body,resp_headers,resp_body,"
                "tags,source,flow_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.get("ts") or now_iso(), rec.get("method") or "GET",
                    rec.get("scheme"), rec.get("host"), rec.get("port"),
                    rec.get("path") or "/", rec.get("url") or "",
                    rec.get("status"), rec.get("mime"),
                    rec.get("req_size"), rec.get("resp_size"),
                    _truncate(rec.get("req_headers"), MAX_HEADERS),
                    _truncate(rec.get("req_body"), MAX_BODY),
                    _truncate(rec.get("resp_headers"), MAX_HEADERS),
                    _truncate(rec.get("resp_body"), MAX_BODY),
                    json.dumps(tags, ensure_ascii=False),
                    rec.get("source") or "mitm",
                    rec.get("flow_id"),
                ),
            )
            return cur.lastrowid

    def clear(self) -> int:
        if not self.available():
            return 0
        with self.connect() as conn:
            return conn.execute("DELETE FROM traffic").rowcount

    # -- reads --
    def list(self, host: str | None = None, status: int | None = None,
             q: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT id,ts,method,host,path,url,status,mime,resp_size,tags FROM traffic"
        where, params = [], []
        if host:
            where.append("host LIKE ?")
            params.append(f"%{host}%")
        if status:
            where.append("status = ?")
            params.append(int(status))
        if q:
            where.append("(url LIKE ? OR path LIKE ? OR resp_body LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute(sql, params)]
        for r in rows:
            r["tags"] = json.loads(r.get("tags") or "[]")
        return rows

    def get(self, tid: int) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM traffic WHERE id=?", (int(tid),)).fetchone()
        if row is None:
            return None
        rec = dict(row)
        rec["tags"] = json.loads(rec.get("tags") or "[]")
        return rec

    def stats(self) -> dict:
        if not self.available():
            return {"total": 0, "hosts": 0, "first": None, "last": None, "by_status": {}}
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) n, COUNT(DISTINCT host) h, MIN(ts) a, MAX(ts) b FROM traffic"
            ).fetchone()
            by_status = {str(r["status"]): r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) n FROM traffic GROUP BY status ORDER BY n DESC LIMIT 12")}
        return {"total": row["n"], "hosts": row["h"], "first": row["a"],
                "last": row["b"], "by_status": by_status}


# --------------------------------------------------------------------------- #
# helpers for the addon / import path
# --------------------------------------------------------------------------- #
def record_from_flow(flow) -> dict:
    """Convert a mitmproxy HTTPFlow into our flat record.

    Kept here (not in the addon) so tests can build records without mitmproxy.
    """
    req = flow.request
    resp = getattr(flow, "response", None)
    url = req.pretty_url if hasattr(req, "pretty_url") else str(req.url)
    parsed = urlparse(url)
    body = None
    if req.content:
        try:
            body = req.content.decode("utf-8", "replace")
        except Exception:
            body = None
    resp_body = None
    if resp is not None and resp.content:
        try:
            resp_body = resp.content.decode("utf-8", "replace")
        except Exception:
            resp_body = None
    return {
        "ts": now_iso(),
        "method": req.method,
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "path": parsed.path or "/",
        "url": url,
        "status": resp.status_code if resp is not None else None,
        "mime": (resp.headers.get("content-type", "") if resp is not None else ""),
        "req_size": len(req.content or b""),
        "resp_size": len(resp.content or b"") if resp is not None else 0,
        "req_headers": "\n".join(f"{k}: {v}" for k, v in req.headers.items()),
        "req_body": body,
        "resp_headers": ("\n".join(f"{k}: {v}" for k, v in resp.headers.items())
                         if resp is not None else None),
        "resp_body": resp_body,
        "source": "mitm",
        "flow_id": getattr(flow, "id", None),
    }


def import_records(store: TrafficStore, records: list[dict]) -> dict:
    kept = dropped = 0
    reasons: dict[str, int] = {}
    for rec in records:
        keep, reason = should_store(rec)
        if not keep:
            dropped += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        if store.add(rec):
            kept += 1
    return {"kept": kept, "dropped": dropped, "reasons": reasons}
