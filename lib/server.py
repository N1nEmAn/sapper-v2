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
"""

from __future__ import annotations

import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).parent))

import index_db  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    server_version = "sapper-console/1.0"
    db_path: Path | None = None
    web_root: Path | None = None

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        return

    # -- helpers -----------------------------------------------------------
    def _db(self) -> "index_db.IndexDB":
        assert self.db_path is not None
        return index_db.IndexDB(self.db_path)

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
                self._json({"ok": True, "stats": graph["stats"], "seq": self._db().last_seq()})
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


class Console:
    def __init__(self, db_path: Path, web_root: Path, host: str = "127.0.0.1", port: int = 8787):
        self.db_path = db_path
        self.web_root = web_root
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def make_server(self) -> ThreadingHTTPServer:
        handler = type(
            "BoundHandler",
            (Handler,),
            {"db_path": self.db_path, "web_root": self.web_root},
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
