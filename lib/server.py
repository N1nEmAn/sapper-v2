"""HTTP console for the fact graph.

Security model (see DESIGN-WEB.md):
  * loopback only, Host allowlist        -> blocks DNS rebinding
  * session token (cookie or Bearer)     -> blocks other local agents that scan port
  * Origin / Sec-Fetch-Site checks       -> blocks CSRF from any page
  * JSON + custom header on writes       -> blocks simple-form CSRF
  * exec is a separate switch            -> a read/write session cannot spawn code
  * hash-chained audit log               -> every mutation is attributable

Routes:
    GET  /                          console (bootstraps the session cookie)
    GET  /api/health                liveness + index stats + capability flags
    GET  /api/graph                 facts + intents + stats
    GET  /api/events?since=N        incremental event feed (polling fallback)
    GET  /api/stream?since=N        same feed as SSE
    GET  /api/fact/<id>             one fact
    GET  /api/intent/<id>           one intent
    GET  /api/assets                global asset store
    GET  /api/traffic               captured HTTP
    GET  /api/chat                  transcript
    GET  /api/search?q=             cross-source search
    POST /api/confirm/<id>          approve an intent
    POST /api/ops/<action>[/<id>]   graph mutations (fact/intent/claim/.../dispatch)
    POST /api/chat                  one conversational turn
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import secrets
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).parent))

import assets  # noqa: E402
import index_db  # noqa: E402
import traffic  # noqa: E402

MAX_BODY = 1 << 20           # 1 MiB is plenty for any console action
COOKIE_NAME = "sapper_session"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")

UNAUTHORIZED_PAGE = b"""<!doctype html><meta charset="utf-8">
<title>sapper \xe6\x8e\xa7\xe5\x88\xb6\xe5\x8f\xb0</title>
<style>body{font:14px/1.6 system-ui;margin:60px auto;max-width:560px;color:#2c2c2a}
code{background:#f2f1ec;padding:2px 6px;border-radius:4px}</style>
<h2>\xe9\x9c\x80\xe8\xa6\x81\xe4\xbc\x9a\xe8\xaf\x9d\xe4\xbb\xa4\xe7\x89\x8c</h2>
<p>\xe8\xaf\xb7\xe4\xbd\xbf\xe7\x94\xa8\xe5\x90\xaf\xe5\x8a\xa8 <code>sapper serve</code>
\xe6\x97\xb6\xe6\x89\x93\xe5\x8d\xb0\xe7\x9a\x84\xe9\x93\xbe\xe6\x8e\xa5\xe6\x89\x93\xe5\xbc\x80\xef\xbc\x9a
<code>http://127.0.0.1:PORT/?t=TOKEN</code>\xe3\x80\x82</p>
<p style="color:#6b6a64">\xe7\x9b\xb4\xe6\x8e\xa5\xe8\xae\xbf\xe9\x97\xae\xe6\x8e\xa5\xe5\x8f\xa3\xe4\xb8\x8d\xe4\xbc\x9a\xe6\x8e\x88\xe6\x9d\x83\xef\xbc\x8c
\xe4\xbb\xa5\xe9\x98\xb2\xe5\x90\x8c\xe6\x9c\xba\xe8\xbf\x90\xe8\xa1\x8c\xe7\x9a\x84\xe5\x85\xb6\xe4\xbb\x96\xe5\xb7\xa5\xe5\x85\xb7\xe7\xab\xaf\xe5\x8f\xa3\xe6\x8e\xa5\xe7\xae\xa1\xe3\x80\x82</p>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "sapper-console/2.0"
    protocol_version = "HTTP/1.1"

    db_path: Path | None = None
    web_root: Path | None = None
    confirm_cb = None
    ops = None
    chat = None
    token: str = ""
    allow_exec: bool = False
    port: int = 0
    audit_path: Path | None = None

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        return

    # -- security helpers --------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, payload: object, code: int = 200, extra: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self._send(code, body, "application/json; charset=utf-8", extra)

    def _error(self, code: int, message: str) -> None:
        try:
            self._json({"ok": False, "error": message}, code)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass      # client already gone; nothing useful to send

    def _host_ok(self) -> bool:
        raw = (self.headers.get("Host") or "").strip().lower()
        if not raw:
            return False
        host = raw.rsplit(":", 1)[0] if not raw.endswith("]") else raw
        if raw.count(":") == 1 and not raw.startswith("["):
            host = raw.split(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return False
        if ":" in raw and not raw.startswith("["):
            try:
                if int(raw.rsplit(":", 1)[1]) != self.port:
                    return False
            except ValueError:
                return False
        return True

    def _origin_ok(self) -> bool:
        """Reject cross-site requests. Absent Origin is fine (curl, same-origin GET)."""
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site == "cross-site":
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return (parsed.scheme == "http"
                and (parsed.hostname or "") in {"127.0.0.1", "localhost", "::1"}
                and (parsed.port or 80) == self.port)

    def _profile(self) -> list[str]:
        q = parse_qs(urlparse(self.path).query)
        raw = (q.get("profile") or [""])[0]
        return [t.strip() for t in raw.split(",") if t.strip()]

    def _is_navigation(self) -> bool:
        """Top-level page loads are not data access: let them through so the
        token prompt can be shown. SameSite=Strict already keeps its cookies
        away from cross-site navigations."""
        mode = (self.headers.get("Sec-Fetch-Mode") or "").lower()
        return mode == "navigate" or "text/html" in (self.headers.get("Accept") or "")

    def _presented_token(self) -> str:
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        try:
            jar = SimpleCookie(raw)
        except Exception:
            return ""
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _authed(self) -> bool:
        presented = self._presented_token()
        return bool(presented) and secrets.compare_digest(presented, self.token)

    def _wants_html(self) -> bool:
        return "text/html" in (self.headers.get("Accept") or "")

    def _require_auth(self) -> bool:
        if self._authed():
            return True
        if self._wants_html():
            self._send(401, UNAUTHORIZED_PAGE, "text/html; charset=utf-8")
        else:
            self._error(401, "未认证：请用启动时打印的 ?t=TOKEN 链接打开控制台")
        return False

    def _audit(self, op: str, target: str, ok: bool, detail: str = "") -> None:
        if self.audit_path is None:
            return
        try:
            prev = ""
            if self.audit_path.is_file():
                with self.audit_path.open("rb") as fh:
                    fh.seek(0, 2)
                    size = fh.tell()
                    fh.seek(max(0, size - 4096))
                    lines = fh.read().decode("utf-8", "replace").strip().splitlines()
                    if lines:
                        prev = json.loads(lines[-1]).get("hash", "")
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "op": op, "target": target, "ok": bool(ok),
                "detail": detail[:300],
                "remote": self.client_address[0] if self.client_address else "",
                "prev": prev,
            }
            blob = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            entry["hash"] = hashlib.sha256((prev + blob).encode()).hexdigest()[:32]
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # auditing must never break the request

    def _body(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("application/json"):
            raise ValueError("请求体必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise OverflowError(f"请求体超过 {MAX_BODY} 字节")
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

    # -- static ------------------------------------------------------------
    def _db(self) -> "index_db.IndexDB":
        assert self.db_path is not None
        return index_db.IndexDB(self.db_path)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._error(404, "not found")
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in (".html", ".js", ".css"):
            ctype += "; charset=utf-8"
        extra = {"Content-Security-Policy": CSP} if path.suffix == ".html" else None
        self._send(200, data, ctype, extra)

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._error(421, "Host 不在允许列表（仅 127.0.0.1 / localhost）")
            return
        # Page loads are not data access: a cross-site *navigation* gets the
        # token prompt, never data (SameSite=Strict keeps the cookie away).
        if not self._is_navigation() and not self._origin_ok():
            self._error(403, "跨站请求被拒绝")
            return

        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        # bootstrap: /?t=TOKEN sets the session cookie once
        if route == "/" and (qs.get("t") or [""])[0]:
            if secrets.compare_digest((qs.get("t") or [""])[0], self.token):
                cookie = (f"{COOKIE_NAME}={self.token}; Path=/; HttpOnly; "
                          f"SameSite=Strict; Max-Age=86400")
                # keep every other query param: ?t=...&profile=edr must survive
                # the exchange or a shareable link silently loses its context
                rest = {k: v[0] for k, v in qs.items() if k != "t" and v}
                target = "/" + ("?" + urlencode(rest) if rest else "")
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Set-Cookie", cookie)
                for key, value in SECURITY_HEADERS.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._error(401, "token 无效")
            return

        if not self._require_auth():
            return

        try:
            if route in ("/", "/index.html"):
                assert self.web_root is not None
                self._file(self.web_root / "index.html")
                return

            if route == "/api/health":
                graph = self._db().graph()
                self._json({"ok": True, "stats": graph["stats"], "seq": self._db().last_seq(),
                            "write": self.ops is not None,
                            "exec": bool(self.allow_exec)})
                return

            if route == "/api/graph":
                self._json(self._db().graph())
                return

            if route == "/api/events":
                since = int((qs.get("since") or ["0"])[0])
                self._json({"events": self._db().events_since(since),
                            "last": self._db().last_seq()})
                return

            if route == "/api/stream":
                self._stream(int((qs.get("since") or ["0"])[0]))
                return

            if route == "/api/assets":
                db = assets.AssetDB()
                if not db.available():
                    self._json({"available": False, "assets": [], "edges": [],
                                "stats": {"total": 0, "by_type": {}}, "path": str(db.path)})
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
                self._json(row if row is not None else
                           {"error": "not found", "key": key},
                           200 if row is not None else 404)
                return

            if route == "/api/traffic":
                assert self.db_path is not None
                store = traffic.TrafficStore(self.db_path.parent)
                if not store.available():
                    self._json({"available": False, "rows": [], "stats": {"total": 0}})
                    return
                limit = int((qs.get("limit") or ["100"])[0])
                status_raw = (qs.get("status") or [None])[0]
                self._json({"available": True,
                            "rows": store.list(host=(qs.get("host") or [None])[0],
                                               status=int(status_raw) if status_raw else None,
                                               q=(qs.get("q") or [None])[0], limit=limit),
                            "stats": store.stats()})
                return

            if route.startswith("/api/traffic/"):
                raw = route.rsplit("/", 1)[-1]
                assert self.db_path is not None
                store = traffic.TrafficStore(self.db_path.parent)
                rec = store.get(int(raw)) if raw.isdigit() and store.available() else None
                self._json(rec if rec is not None else {"error": "not found", "id": raw},
                           200 if rec is not None else 404)
                return

            if route == "/api/armory":
                if self.ops is None:
                    self._error(403, "armory requires --allow-write")
                    return
                profile = self._profile()
                self._json({"ok": True, **self.ops.armory_list(profile)})
                return

            if route == "/api/evasion/layers":
                if self.ops is None:
                    self._error(403, "evasion requires --allow-write")
                    return
                self._json({"ok": True, **self.ops.evasion_layers()})
                return

            if route == "/api/plan":
                if self.ops is None:
                    self._error(403, "plan requires --allow-write")
                    return
                self._json({"ok": True, **self.ops.plan_state()})
                return

            if route.startswith("/api/armory/"):
                if self.ops is None:
                    self._error(403, "armory requires --allow-write")
                    return
                rest = route[len("/api/armory/"):]
                wid, _, suffix = rest.partition("/")
                if suffix == "attribute":
                    self._json({"ok": True,
                                **self.ops.armory_attribute(wid, self._profile())})
                    return
                weapon = next((w for w in self.ops.armory_list(self._profile())["weapons"]
                               if w["id"] == wid), None)
                if weapon is None:
                    self._json({"error": "not found", "id": wid}, 404)
                else:
                    self._json({"ok": True, "weapon": weapon})
                return

            if route == "/api/chat":
                if self.chat is None:
                    self._json({"enabled": False, "messages": []})
                    return
                self._json({"enabled": True, "messages": self.chat.view()})
                return

            if route == "/api/search":
                if self.ops is None:
                    self._json({"q": "", "error": "search requires --allow-write",
                                "facts": [], "intents": [], "assets": [], "traffic": []})
                    return
                q = (qs.get("q") or [""])[0]
                self._json(self.ops.search(q, int((qs.get("limit") or ["40"])[0])))
                return

            if route.startswith("/api/ops/dispatch/"):
                if self.ops is None or not self.allow_exec:
                    self._error(403, "exec disabled (start with --allow-exec)")
                    return
                self._json(self.ops.dispatch_status(route.rsplit("/", 1)[-1]))
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

            if self.web_root is not None:
                candidate = (self.web_root / route.lstrip("/")).resolve()
                if str(candidate).startswith(str(self.web_root.resolve())):
                    self._file(candidate)
                    return

            self._error(404, "not found")
        except BrokenPipeError:
            return
        except Exception as exc:
            self._error(500, str(exc))

    def _stream(self, since: int) -> None:
        """Server-sent events: one connection, push on change."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        deadline = time.time() + 600
        last = since
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while time.time() < deadline:
                db = self._db()
                current = db.last_seq()
                if current != last:
                    payload = json.dumps({"events": db.events_since(last), "last": current},
                                         ensure_ascii=False, default=str)
                    self.wfile.write(f"id: {current}\ndata: {payload}\n\n".encode())
                    self.wfile.flush()
                    last = current
                else:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers drop SSE sockets on reload/navigate. On Windows this
            # surfaces as WinError 10053 (ConnectionAbortedError), which is not
            # a subclass of ConnectionResetError — catch it or every reload
            # dumps a traceback.
            return

    # -- writes ------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._error(421, "Host 不在允许列表")
            return
        if not self._origin_ok():
            self._error(403, "跨站请求被拒绝")
            return
        if not self._require_auth():
            return

        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/")

        # CSRF: JSON + custom header can only be produced by our own page.
        if self.headers.get("X-Sapper-Request") != "1" and not (
                (self.headers.get("Authorization") or "").lower().startswith("bearer ")):
            self._error(403, "缺少 X-Sapper-Request 头（跨站表单不可伪造）")
            return

        try:
            if route.startswith("/api/confirm/"):
                if self.confirm_cb is None:
                    self._error(403, "write disabled (start with --allow-write)")
                    return
                iid = route.rsplit("/", 1)[-1]
                self._body()
                self.confirm_cb(iid)
                self._audit("confirm", iid, True)
                self._json({"ok": True, "id": iid})
                return

            if route == "/api/chat/reset" or route == "/api/chat":
                if self.chat is None:
                    self._error(403, "chat disabled (start with --allow-write)")
                    return
                body = self._body()
                if route.endswith("reset"):
                    self.chat.reset()
                    self._audit("chat.reset", "", True)
                    self._json({"ok": True, "messages": []})
                else:
                    result = self.chat.send(body.get("message") or "")
                    self._audit("chat.send", "", True, (body.get("message") or "")[:120])
                    self._json({"ok": True, **result})
                return

            if route.startswith("/api/ops/"):
                if self.ops is None:
                    self._error(403, "write disabled (start with --allow-write)")
                    return
                parts = route[len("/api/ops/"):].split("/")
                action = parts[0]
                target = parts[1] if len(parts) > 1 else ""
                if action in ("dispatch", "dispatch_stop") and not self.allow_exec:
                    self._error(403, "exec disabled (start with --allow-exec)")
                    return
                body = self._body()

                if action == "fact":
                    fact = self.ops.fact(body)
                    self._audit("fact.create", fact["id"], True)
                    self._json({"ok": True, "fact": fact})
                elif action == "intent":
                    intent = self.ops.intent(body)
                    self._audit("intent.create", intent["id"], True)
                    self._json({"ok": True, "intent": intent})
                elif action == "claim" and target:
                    self._json({"ok": True, "intent": self.ops.claim(target, body)})
                    self._audit("intent.claim", target, True)
                elif action == "release" and target:
                    self._json({"ok": True, "intent": self.ops.release(target, body)})
                    self._audit("intent.release", target, True)
                elif action == "done" and target:
                    result = self.ops.done(target, body)
                    self._audit("intent.done", target, True, result["fact"]["id"])
                    self._json({"ok": True, **result})
                elif action == "dispatch" and target:
                    result = self.ops.dispatch(target, body)
                    self._audit("executor.start", target, True, str(result.get("pid")))
                    self._json({"ok": True, **result})
                elif action == "dispatch_stop" and target:
                    result = self.ops.dispatch_stop(target)
                    self._audit("executor.stop", target, bool(result.get("ok")))
                    self._json(result)
                elif action == "plan":
                    sub = (body.get("action") or "").strip()
                    if sub == "add":
                        result = {"step": self.ops.plan_add(
                            body.get("title") or "",
                            after=int(body["after"]) if body.get("after") else None,
                            note=body.get("note") or "")}
                    elif sub == "set":
                        result = {"step": self.ops.plan_set(
                            int(body.get("n") or 0), body.get("status") or "done",
                            body.get("note") or "")}
                    elif sub == "link":
                        result = {"step": self.ops.plan_link(
                            int(body.get("n") or 0), body.get("intent") or "")}
                    elif sub == "state":
                        result = self.ops.plan_state()
                    else:
                        raise ValueError(f"未知的计划动作 {sub!r}")
                    self._audit(f"plan.{sub}", str(body.get("n") or ""), True)
                    self._json({"ok": True, **result})
                elif action == "armory":
                    sub = (body.get("action") or "").strip()
                    prof = body.get("profile") or []
                    if isinstance(prof, str):
                        prof = [t.strip() for t in prof.split(",") if t.strip()]
                    if sub == "use":
                        result = self.ops.armory_use(body.get("weapon") or "",
                                                     body.get("vid") or None, prof)
                    elif sub == "report":
                        result = self.ops.armory_report(
                            body.get("weapon") or "", body.get("vid") or "",
                            body.get("result") or "ok", prof, body.get("note") or "")
                    elif sub == "attribute":
                        result = self.ops.armory_attribute(
                            body.get("weapon") or "", prof)
                    elif sub == "derive":
                        result = self.ops.armory_derive(
                            body.get("weapon") or "", body.get("vid") or "v1", prof,
                            apply=bool(body.get("apply")),
                            top=int(body.get("top") or 2))
                    elif sub == "recommend":
                        result = self.ops.armory_recommend(
                            prof, family=body.get("family") or "",
                            keyword=body.get("keyword") or "",
                            limit=int(body.get("limit") or 5))
                    elif sub == "add":
                        result = self.ops.armory_add(body)
                    elif sub == "seed":
                        result = self.ops.armory_seed()
                    else:
                        raise ValueError(f"未知的武器库动作 {sub!r}")
                    self._audit(f"armory.{sub}", body.get("weapon") or "", True)
                    self._json({"ok": True, **result})
                elif action == "assets":
                    result = self.ops.asset_action(body.get("action") or "",
                                                   body.get("key") or "")
                    self._audit(f"asset.{body.get('action')}", body.get("key") or "",
                                True, json.dumps(result, ensure_ascii=False))
                    self._json({"ok": True, **result})
                else:
                    self._error(404, f"unknown op {action!r}")
                return

            self._error(404, "not found")
        except OverflowError as exc:
            self._error(413, str(exc))
        except BrokenPipeError:
            return
        except Exception as exc:
            self._audit("error", route, False, str(exc))
            self._error(400, str(exc))


class Console:
    def __init__(self, db_path: Path, web_root: Path, host: str = "127.0.0.1",
                 port: int = 8787, confirm_cb=None, ops=None, chat=None,
                 token: str = "", allow_exec: bool = False):
        self.db_path = db_path
        self.web_root = web_root
        self.host = host
        self.port = port
        self.confirm_cb = confirm_cb
        self.ops = ops
        self.chat = chat
        self.token = token or secrets.token_urlsafe(24)
        self.allow_exec = allow_exec
        self.audit_path = Path(db_path).parent / "audit.jsonl"
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
                "chat": staticmethod(self.chat) if self.chat else None,
                "token": self.token,
                "allow_exec": self.allow_exec,
                "port": self.port,
                "audit_path": self.audit_path,
            },
        )
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        httpd.daemon_threads = True
        return httpd

    def serve_forever(self) -> None:
        self._httpd = self.make_server()
        print(f"console : http://{self.host}:{self.port}/?t={self.token}")
        print(f"index   : {self.db_path}")
        print(f"audit   : {self.audit_path}")
        print(f"caps    : write={'on' if self.ops else 'off'} exec={'on' if self.allow_exec else 'off'}")
        print("打开上面那条带 ?t= 的链接完成认证；token 只存在内存里，重启即失效。")
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
