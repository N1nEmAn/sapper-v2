"""HTTP console for the fact graph.

Read-only over the SQLite index: this server never writes JSON files, so it
cannot corrupt an engagement. Start it with `sapper serve`.

Routes:
    GET /                      web console (single page)
    GET /api/health            liveness + index stats
    GET /api/graph             facts + intents + stats
    GET /api/events?since=N    incremental event feed (polling)
    GET /api/fact/<id>         one fact
    GET /api/intent/<id>       one intent
    GET /api/assets            global asset store (cross-engagement)
"""

from __future__ import annotations

import json
import mimetypes
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))

import assets  # noqa: E402
import index_db  # noqa: E402
import traffic  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    server_version = "sapper-console/1.0"
    db_path: Path | None = None
    web_root: Path | None = None
    confirm_cb = None
    ops = None

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        return

    # -- helpers -----------------------------------------------------------
    def _db(self) -> "index_db.IndexDB":
        assert self.db_path is not None
        return index_db.IndexDB(self.db_path)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(parsed, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return parsed

    def _json(self, payload: object, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(404, "not found")
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".html", ".js", ".css"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"

        try:
            if route in ("/", "/index.html"):
                assert self.web_root is not None
                self._file(self.web_root / "index.html")
                return

            if route == "/api/health":
                graph = self._db().graph()
                self._json({"ok": True, "stats": graph["stats"], "seq": self._db().last_seq(),
                            "write": self.ops is not None})
                return

            if route == "/api/graph":
                self._json(self._db().graph())
                return

            if route == "/api/events":
                qs = parse_qs(parsed.query)
                since = int((qs.get("since") or ["0"])[0])
                self._json({"events": self._db().events_since(since), "last": self._db().last_seq()})
                return

            if route.startswith("/api/fact/"):
                fid = route.rsplit("/", 1)[-1]
                for fact in self._db().graph()["facts"]:
                    if fact["id"] == fid:
                        self._json(fact)
                        return
                self._json({"error": "not found", "id": fid}, 404)
                return

            if route.startswith("/api/intent/"):
                iid = route.rsplit("/", 1)[-1]
                for intent in self._db().graph()["intents"]:
                    if intent["id"] == iid:
                        self._json(intent)
                        return
                self._json({"error": "not found", "id": iid}, 404)
                return

            if route == "/api/assets":
                qs = parse_qs(parsed.query)
                db = assets.AssetDB()
                if not db.available():
                    self._json({"available": False, "assets": [], "edges": [],
                                "stats": {"total": 0, "by_type": {}},
                                "path": str(db.path)})
                    return
                limit = int((qs.get("limit") or ["400"])[0])
                rows = db.list(type=(qs.get("type") or [None])[0],
                               q=(qs.get("q") or [None])[0], limit=limit)
                self._json({"available": True, "assets": rows, "edges": db.edges(),
                            "stats": db.stats(), "path": str(db.path)})
                return

            if route.startswith("/api/asset/"):
                key = route[len("/api/asset/"):]
                db = assets.AssetDB()
                row = db.get(key) if db.available() else None
                if row is None:
                    self._json({"error": "not found", "key": key}, 404)
                else:
                    self._json(row)
                return

            if route == "/api/traffic":
                qs = parse_qs(parsed.query)
                assert self.db_path is not None
                store = traffic.TrafficStore(self.db_path.parent)
                if not store.available():
                    self._json({"available": False, "rows": [], "stats": {"total": 0}})
                    return
                limit = int((qs.get("limit") or ["100"])[0])
                status_raw = (qs.get("status") or [None])[0]
                rows = store.list(
                    host=(qs.get("host") or [None])[0],
                    status=int(status_raw) if status_raw else None,
                    q=(qs.get("q") or [None])[0],
                    limit=limit,
                )
                self._json({"available": True, "rows": rows, "stats": store.stats()})
                return

            if route.startswith("/api/traffic/"):
                raw = route.rsplit("/", 1)[-1]
                assert self.db_path is not None
                store = traffic.TrafficStore(self.db_path.parent)
                rec = store.get(int(raw)) if raw.isdigit() and store.available() else None
                if rec is None:
                    self._json({"error": "not found", "id": raw}, 404)
                else:
                    self._json(rec)
                return

            if route == "/api/search":
                qs = parse_qs(parsed.query)
                q = (qs.get("q") or [""])[0]
                limit = int((qs.get("limit") or ["40"])[0])
                if self.ops is None:
                    self._json({"q": q, "error": "search requires --allow-write",
                                "facts": [], "intents": [], "assets": [], "traffic": []})
                    return
                self._json(self.ops.search(q, limit))
                return

            if route.startswith("/api/ops/dispatch/"):
                iid = route.rsplit("/", 1)[-1]
                if self.ops is None:
                    self._json({"error": "write disabled (start with --allow-write)"}, 403)
                    return
                self._json(self.ops.dispatch_status(iid))
                return

            # static assets under web/
            if self.web_root is not None:
                candidate = (self.web_root / route.lstrip("/")).resolve()
                if str(candidate).startswith(str(self.web_root.resolve())):
                    self._file(candidate)
                    return

            self.send_error(404, "not found")
        except BrokenPipeError:
            return
        except Exception as exc:  # never crash the console on a bad request
            self._json({"error": str(exc)}, 500)

    # -- writes: the only mutating endpoint --------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/")

        if route.startswith("/api/confirm/"):
            iid = route.rsplit("/", 1)[-1]
            if self.confirm_cb is None:
                self._json({"error": "write disabled (start with --allow-write)"}, 403)
                return
            try:
                self.confirm_cb(iid)
                self._json({"ok": True, "id": iid})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return

        if route.startswith("/api/ops/"):
            if self.ops is None:
                self._json({"error": "write disabled (start with --allow-write)"}, 403)
                return
            parts = route[len("/api/ops/"):].split("/")
            action = parts[0]
            target = parts[1] if len(parts) > 1 else ""
            try:
                body = self._body()
                if action == "fact":
                    self._json({"ok": True, "fact": self.ops.fact(body)})
                elif action == "intent":
                    self._json({"ok": True, "intent": self.ops.intent(body)})
                elif action == "claim" and target:
                    self._json({"ok": True, "intent": self.ops.claim(target, body)})
                elif action == "release" and target:
                    self._json({"ok": True, "intent": self.ops.release(target, body)})
                elif action == "done" and target:
                    self._json({"ok": True, **self.ops.done(target, body)})
                elif action == "dispatch" and target:
                    self._json({"ok": True, **self.ops.dispatch(target, body)})
                elif action == "dispatch_stop" and target:
                    self._json(self.ops.dispatch_stop(target))
                else:
                    self._json({"error": f"unknown op {action!r}"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return

        self._json({"error": "not found"}, 404)


class Console:
    def __init__(self, db_path: Path, web_root: Path, host: str = "127.0.0.1",
                 port: int = 8787, confirm_cb=None, ops=None):
        self.db_path = db_path
        self.web_root = web_root
        self.host = host
        self.port = port
        self.confirm_cb = confirm_cb
        self.ops = ops
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def make_server(self) -> ThreadingHTTPServer:
        # staticmethod: a plain function stored on the class would be bound as
        # a method, and the handler instance would arrive as the first arg.
        handler = type(
            "BoundHandler",
            (Handler,),
            {
                "db_path": self.db_path,
                "web_root": self.web_root,
                "confirm_cb": staticmethod(self.confirm_cb) if self.confirm_cb else None,
                "ops": staticmethod(self.ops) if self.ops else None,
            },
        )
        return ThreadingHTTPServer((self.host, self.port), handler)

    def serve_forever(self) -> None:
        self._httpd = self.make_server()
        print(f"console: http://{self.host}:{self.port}  (Ctrl+C to stop)")
        print(f"index  : {self.db_path}")
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
