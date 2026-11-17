#!/usr/bin/env python3
"""
arena — competition-platform client for sapper.

Two backends are supported behind one small interface, chosen by
``SAPPER_PLATFORM`` or (when unset) by which credentials are present:

  * ``tsecbench`` — the original Tencent Cloud benchmark API.  Its contract is
    unchanged: ``BENCHMARK_BASE_URL`` + ``BENCHMARK_TOKEN``, endpoints under
    ``/openapi/v1/challenges``, one flag per ``unique_code``.

  * ``ctfy`` — the TPCTF 2026 / CTFy platform.  ``CTFY_BASE_URL`` (default
    ``https://ctfy.tsinghua.edu.cn/api/v1``), ``CTFY_TOKEN`` (Bearer token),
    optional ``CTFY_COMPETITION_ID``.  A challenge can have several questions,
    every target is an ``instance``, and answers are submitted against
    ``instance_id`` + ``question_id``.

The rest of sapper was written against the TSecBench shape, so the CTFy backend
normalises its objects back to that shape where it can:

    challenge.unique_code       <- challenge.id
    challenge.flag_count        <- number of questions
    challenge.correct_flag_count<- solved questions
    challenge.is_completed      <- my_team_fully_solved
    start.container_addr        <- entry_urls + service host:port
    submit.status == 200        <- submission HTTP 200
    submit.correct              <- answer was right

Anything CTFy-specific (instance_id, question_id, VPN config, attachments) is
also exposed on the normalised objects and through the extra methods below.

Standard library only (urllib) — no httpx, no SDK install.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ARENA_FILE = "arena.json"
DEFAULT_TIMEOUT = 30
TOKEN_HEADER = "BENCHMARK_TOKEN"
CHALLENGES_PATH = "/openapi/v1/challenges"

DEFAULT_CTFY_BASE_URL = "https://ctfy.tsinghua.edu.cn/api/v1"
DEFAULT_CTFY_START_TIMEOUT = 300
DEFAULT_CTFY_POLL_INTERVAL = 3.0
CTFY_ACTIVE_STATUSES = ("starting", "ready", "stopping")
CTFY_LIVE_STATUSES = ("starting", "ready")

# WireGuard interface names are capped at 15 bytes by Linux. wg-quick derives
# the name from the config file stem, so sapper writes its own cleaned copy
# under <vpn_dir>/.up/<short-stem>.conf instead of using the downloaded name.
WG_IFACE_MAX = 15
VPN_READY_WAIT_S = 45.0
_VPN_HOSTS_BEGIN = "# BEGIN sapper-vpn {tag}"
_VPN_HOSTS_END = "# END sapper-vpn {tag}"

_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class ArenaError(Exception):
    """User-facing arena failure (bad config, HTTP error, bad payload)."""

    def __init__(self, message: str, *, code: str | None = None,
                 status: int | None = None, detail: object | None = None):
        self.code = code
        self.status = status
        self.detail = detail if detail is not None else {}
        super().__init__(message)


# --------------------------------------------------------------------------- #
# backend selection / config helpers
# --------------------------------------------------------------------------- #
def _canonical_platform(value: str | None) -> str | None:
    """Map user-facing platform names to ``ctfy`` or ``tsecbench``."""
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw in ("ctfy", "tpctf", "tpctf2026", "tsinghua", "ctf.tsinghua"):
        return "ctfy"
    if raw in ("tsecbench", "tsec", "benchmark", "tencent"):
        return "tsecbench"
    raise ArenaError(
        f"unknown platform {value!r}; use SAPPER_PLATFORM=ctfy or tsecbench"
    )


def _env_platform() -> str:
    explicit = _canonical_platform(os.environ.get("SAPPER_PLATFORM"))
    if explicit:
        return explicit
    # Hosted TSecBench injects BENCHMARK_*; prefer it when both families are
    # present so a stray CTFY_* variable cannot silently switch backends.
    if any(os.environ.get(k) for k in ("BENCHMARK_BASE_URL", "BENCHMARK_TOKEN")):
        return "tsecbench"
    if any(os.environ.get(k) for k in
           ("CTFY_BASE_URL", "CTFY_TOKEN", "CTFY_COMPETITION_ID")):
        return "ctfy"
    return "tsecbench"


def _normalize_ctfy_base(base_url: str) -> str:
    """Accept both ``https://host`` and ``https://host/api/v1``."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return base
    idx = base.find("/api/v1")
    if idx >= 0:
        return base[: idx + len("/api/v1")]
    if base.endswith("/api"):
        return base + "/v1"
    return base + "/api/v1"


def _resolve_secret(value: object, default_env: str) -> str:
    """Read a config token value: ``ENV:VAR`` indirection or a literal."""
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    if raw.startswith("ENV:"):
        return os.environ.get(raw[4:].strip(), "") or ""
    return raw


def _page_items(data: object, what: str) -> list[dict]:
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [x for x in data["items"] if isinstance(x, dict)]
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    raise ArenaError(f"{what} returned {type(data).__name__}, expected a list/page")


def _iso_utc(epoch: object) -> str:
    try:
        ts = float(epoch)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class ArenaClient:
    """Synchronous competition API client (stdlib only).

    Defaults to the TSecBench backend so existing manual construction keeps
    working.  ``load()`` selects the backend from the environment/config.
    """

    def __init__(self, base_url: str, token: str, timeout: int = DEFAULT_TIMEOUT,
                 *, platform: str = "tsecbench", competition_id: str = "",
                 state_file: str | os.PathLike | None = None,
                 start_timeout: float = DEFAULT_CTFY_START_TIMEOUT,
                 poll_interval: float = DEFAULT_CTFY_POLL_INTERVAL,
                 vpn_dir: str | os.PathLike | None = None,
                 attachments_dir: str | os.PathLike | None = None,
                 verify_tls: bool = True):
        platform = _canonical_platform(platform) or "tsecbench"
        if not base_url or not base_url.strip():
            raise ArenaError("arena base_url is empty")
        if not token or not token.strip():
            if platform == "ctfy":
                raise ArenaError("CTFy token is empty (set CTFY_TOKEN)")
            raise ArenaError("arena token is empty (set BENCHMARK_TOKEN)")
        self.platform = platform
        self.base_url = (_normalize_ctfy_base(base_url) if platform == "ctfy"
                         else base_url.rstrip("/"))
        self.token = token.strip()
        self.timeout = timeout
        self.competition_id = (competition_id or "").strip()
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.start_timeout = float(start_timeout or DEFAULT_CTFY_START_TIMEOUT)
        self.poll_interval = max(0.5, float(poll_interval or DEFAULT_CTFY_POLL_INTERVAL))
        self.vpn_dir = Path(vpn_dir).expanduser() if vpn_dir else None
        self.attachments_dir = (Path(attachments_dir).expanduser()
                                if attachments_dir else None)
        self.verify_tls = bool(verify_tls)
        self._last_start_wait: dict | None = None
        self._ssl_context = None if self.verify_tls else self._make_insecure_context()

    @staticmethod
    def _make_insecure_context():
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # -- construction ------------------------------------------------------
    @classmethod
    def config_path(cls, store_root: Path | None = None) -> Path:
        if store_root is not None:
            return Path(store_root) / ARENA_FILE
        return Path(ARENA_FILE)

    @classmethod
    def load(cls, store_root: Path | None = None) -> "ArenaClient":
        """Resolve config from env vars first, then an optional arena.json.

        TSecBench env: ``BENCHMARK_BASE_URL``, ``BENCHMARK_TOKEN``.
        CTFy env:      ``CTFY_BASE_URL``, ``CTFY_TOKEN``,
                       ``CTFY_COMPETITION_ID``.

        arena.json may add ``"platform": "ctfy"`` plus any of ``base_url``,
        ``token`` (``ENV:VAR`` or literal), ``competition_id``, ``timeout``,
        ``start_timeout``, ``poll_interval``, ``state_file``, ``vpn_dir``,
        ``attachments_dir``, ``verify_tls``.
        """
        cfg: dict = {}
        if store_root is not None:
            path = cls.config_path(store_root)
            if path.exists():
                try:
                    cfg = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError) as exc:
                    raise ArenaError(f"cannot read arena config {path}: {exc}") from exc
                if not isinstance(cfg, dict):
                    raise ArenaError(f"arena config {path} must be a JSON object")

        platform = (_canonical_platform(cfg.get("platform"))
                    or _env_platform())
        if platform == "ctfy":
            return cls._load_ctfy(cfg, store_root)
        return cls._load_tsecbench(cfg, store_root)

    @classmethod
    def _load_tsecbench(cls, cfg: dict, store_root: Path | None) -> "ArenaClient":
        base_url = os.environ.get("BENCHMARK_BASE_URL") or ""
        token = os.environ.get("BENCHMARK_TOKEN") or ""
        timeout = DEFAULT_TIMEOUT
        if isinstance(cfg.get("base_url"), str) and cfg["base_url"].strip():
            base_url = cfg["base_url"].strip()
        if isinstance(cfg.get("token"), str) and cfg["token"].strip():
            raw = cfg["token"].strip()
            if raw.startswith("ENV:"):
                token = os.environ.get(raw[4:].strip(), "") or token
            else:
                token = raw
        if isinstance(cfg.get("timeout"), int) and cfg["timeout"] > 0:
            timeout = cfg["timeout"]
        if not base_url:
            raise ArenaError(
                "no arena base_url — set BENCHMARK_BASE_URL (the platform injects "
                "it in hosted mode) or write .sapper/arena.json"
            )
        return cls(base_url, token, timeout, platform="tsecbench")

    @classmethod
    def _load_ctfy(cls, cfg: dict, store_root: Path | None) -> "ArenaClient":
        base_url = os.environ.get("CTFY_BASE_URL") or DEFAULT_CTFY_BASE_URL
        token = os.environ.get("CTFY_TOKEN") or ""
        competition_id = os.environ.get("CTFY_COMPETITION_ID") or ""
        timeout = DEFAULT_TIMEOUT
        start_timeout = DEFAULT_CTFY_START_TIMEOUT
        poll_interval = DEFAULT_CTFY_POLL_INTERVAL
        state_file = None
        vpn_dir = None
        attachments_dir = None
        verify_tls = os.environ.get("CTFY_VERIFY_TLS", "1").strip() not in ("0", "false", "no")

        if isinstance(cfg.get("base_url"), str) and cfg["base_url"].strip():
            base_url = cfg["base_url"].strip()
        if "token" in cfg:
            resolved = _resolve_secret(cfg.get("token"), "CTFY_TOKEN")
            if resolved:
                token = resolved
        if isinstance(cfg.get("competition_id"), str) and cfg["competition_id"].strip():
            competition_id = cfg["competition_id"].strip()
        for key, attr in (("timeout", "timeout"), ("start_timeout", "start_timeout")):
            value = cfg.get(key)
            if isinstance(value, (int, float)) and value > 0:
                if attr == "timeout":
                    timeout = int(value)
                else:
                    start_timeout = float(value)
        poll = cfg.get("poll_interval")
        if isinstance(poll, (int, float)) and poll > 0:
            poll_interval = float(poll)
        for key in ("state_file", "vpn_dir", "attachments_dir"):
            value = cfg.get(key)
            if isinstance(value, str) and value.strip():
                path = Path(value).expanduser()
                if not path.is_absolute() and store_root is not None:
                    path = Path(store_root) / path
                if key == "state_file":
                    state_file = path
                elif key == "vpn_dir":
                    vpn_dir = path
                else:
                    attachments_dir = path
        if "verify_tls" in cfg:
            verify_tls = bool(cfg["verify_tls"])

        return cls(
            base_url, token, timeout, platform="ctfy",
            competition_id=competition_id, state_file=state_file,
            start_timeout=start_timeout, poll_interval=poll_interval,
            vpn_dir=vpn_dir, attachments_dir=attachments_dir,
            verify_tls=verify_tls,
        )

    @classmethod
    def write_template(cls, store_root: Path,
                       platform: str | None = None) -> Path:
        path = cls.config_path(store_root)
        if path.exists():
            raise ArenaError(f"arena config already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        chosen = _canonical_platform(platform) or _env_platform()
        if chosen == "ctfy":
            payload = {
                "platform": "ctfy",
                "base_url": DEFAULT_CTFY_BASE_URL,
                "token": "ENV:CTFY_TOKEN",
                "competition_id": "",
                "timeout": 30,
                "start_timeout": DEFAULT_CTFY_START_TIMEOUT,
                "poll_interval": DEFAULT_CTFY_POLL_INTERVAL,
                "_comment": (
                    "TPCTF/CTFy. The hosted platform injects CTFY_BASE_URL and "
                    "CTFY_TOKEN; set CTFY_COMPETITION_ID (or fill competition_id) "
                    "when more than one registered competition is running."
                ),
            }
        else:
            payload = {
                "base_url": "https://benchmark.example.com",
                "token": "ENV:BENCHMARK_TOKEN",
                "timeout": 30,
                "_comment": (
                    "Hosted mode needs nothing here — the platform injects "
                    "BENCHMARK_BASE_URL and BENCHMARK_TOKEN. This file is an "
                    "override for local testing / self-hosted runs."
                ),
            }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    # -- transport ---------------------------------------------------------
    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        if self.platform == "ctfy":
            return {"Authorization": f"Bearer {self.token}", "Accept": accept}
        return {TOKEN_HEADER: self.token, "Accept": accept}

    def _url(self, path: str, params: dict | None = None) -> str:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _parse_http_error(self, method: str, path: str, exc: urllib.error.HTTPError,
                          raw: str) -> ArenaError:
        code = None
        message = raw
        detail: object = {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            if self.platform == "ctfy":
                code = payload.get("code")
                detail = payload.get("detail")
                if isinstance(detail, dict):
                    code = code or detail.get("code")
                    message = (detail.get("message") or detail.get("msg")
                               or json.dumps(detail, ensure_ascii=False))
                elif isinstance(detail, list):
                    message = json.dumps(detail, ensure_ascii=False)
                    detail = {"errors": detail}
                elif isinstance(detail, str):
                    message = detail
                    detail = {"message": detail}
                else:
                    detail = {}
                if payload.get("message"):
                    message = str(payload["message"])
                if isinstance(detail, dict):
                    for extra in ("retry_after_s", "next_session_starts_at"):
                        if extra in payload and extra not in detail:
                            detail[extra] = payload[extra]
            else:
                code = payload.get("code")
                message = payload.get("message", raw)
                detail = payload.get("detail") or {}
        return ArenaError(
            f"arena {method} {path} -> HTTP {exc.code}: {message}",
            code=code, status=exc.code, detail=detail,
        )

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None) -> object:
        url = self._url(path, params)
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        kwargs = {}
        if self._ssl_context is not None and url.lower().startswith("https://"):
            kwargs["context"] = self._ssl_context
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, **kwargs) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise self._parse_http_error(
                method, path, exc, exc.read().decode("utf-8", "replace")[:2000]
            ) from exc
        except urllib.error.URLError as exc:
            raise ArenaError(f"arena cannot reach {url}: {exc.reason}") from exc
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArenaError(f"arena {path} returned non-JSON: {raw[:200]}") from exc

    def _request_bytes(self, method: str, path: str, *,
                       params: dict | None = None) -> tuple[bytes, str]:
        url = self._url(path, params)
        req = urllib.request.Request(url, headers=self._headers("*/*"), method=method)
        kwargs = {}
        if self._ssl_context is not None and url.lower().startswith("https://"):
            kwargs["context"] = self._ssl_context
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, **kwargs) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise self._parse_http_error(
                method, path, exc, exc.read().decode("utf-8", "replace")[:2000]
            ) from exc
        except urllib.error.URLError as exc:
            raise ArenaError(f"arena cannot reach {url}: {exc.reason}") from exc

    def _code(self, unique_code: str) -> str:
        if not isinstance(unique_code, str) or not _CODE_RE.match(unique_code):
            raise ArenaError(f"invalid unique_code {unique_code!r}")
        return unique_code

    @staticmethod
    def _quote(value: object, *, keep_slash: bool = False) -> str:
        return urllib.parse.quote(str(value), safe="/" if keep_slash else "")

    # -- TSecBench endpoints (unchanged contract) --------------------------
    def _tsec_list_challenges(self) -> list[dict]:
        data = self._request("GET", CHALLENGES_PATH)
        if isinstance(data, dict) and "challenges" in data:
            data = data["challenges"]
        if not isinstance(data, list):
            raise ArenaError(f"challenges endpoint returned {type(data).__name__}, expected list")
        out = []
        for item in data:
            if isinstance(item, dict) and isinstance(item.get("unique_code"), str):
                out.append(item)
        return out

    def _tsec_start_challenge(self, unique_code: str) -> dict:
        data = self._request("POST", f"{CHALLENGES_PATH}/start",
                             params={"unique_code": self._code(unique_code)})
        return data if isinstance(data, dict) else {"raw": data}

    def _tsec_get_hint(self, unique_code: str) -> dict:
        data = self._request("GET", f"{CHALLENGES_PATH}/hint",
                             params={"unique_code": self._code(unique_code)})
        return data if isinstance(data, dict) else {"raw": data}

    def _tsec_submit_flag(self, unique_code: str, flag: str) -> dict:
        if not isinstance(flag, str) or not flag.strip():
            raise ArenaError("cannot submit an empty flag")
        data = self._request("POST", f"{CHALLENGES_PATH}/submit",
                             body={"unique_code": self._code(unique_code),
                                   "flag": flag.strip()})
        return data if isinstance(data, dict) else {"raw": data}

    def _tsec_close_challenge(self, unique_code: str) -> dict:
        data = self._request("POST", f"{CHALLENGES_PATH}/close",
                             params={"unique_code": self._code(unique_code)})
        return data if isinstance(data, dict) else {"raw": data}

    # -- CTFy: competitions / challenges -----------------------------------
    @property
    def ctfy_competition_id(self) -> str:
        """Configured competition, or the single registered running one."""
        if self.competition_id:
            return self.competition_id
        comps = self.list_competitions(limit=200)
        running = [c for c in comps
                   if c.get("is_registered") and c.get("phase") == "running"]
        if len(running) == 1:
            self.competition_id = str(running[0].get("id"))
            return self.competition_id
        lines = [
            f"  {c.get('id')}  phase={c.get('phase')}  registered={bool(c.get('is_registered'))}  {c.get('title')}"
            for c in comps
        ]
        if not running:
            raise ArenaError(
                "no registered CTFy competition is running; set CTFY_COMPETITION_ID "
                "explicitly. candidates:\n" + "\n".join(lines or ["  (none)"]),
                code="not_registered",
            )
        raise ArenaError(
            "multiple registered CTFy competitions are running; set "
            "CTFY_COMPETITION_ID to one of:\n" + "\n".join(lines),
            code="competition_ambiguous",
        )

    def list_competitions(self, *, phase: str = "", kind: str = "",
                          limit: int = 200, offset: int = 0) -> list[dict]:
        data = self._request("GET", "/competitions", params={
            "phase": phase, "kind": kind, "limit": limit, "offset": offset,
        })
        return _page_items(data, "competitions")

    def get_competition(self, competition_id: str | None = None) -> dict:
        cid = competition_id or self.ctfy_competition_id
        data = self._request("GET", f"/competitions/{self._quote(cid)}")
        if not isinstance(data, dict):
            raise ArenaError(f"competition {cid} returned {type(data).__name__}")
        return data

    def list_challenges(self) -> list[dict]:
        if self.platform != "ctfy":
            return self._tsec_list_challenges()
        cid = self.ctfy_competition_id
        data = self._request(
            "GET", f"/competitions/{self._quote(cid)}/challenges",
            params={"limit": 1000, "offset": 0},
        )
        items = _page_items(data, "challenges")
        return [self._normalize_challenge(item) for item in items
                if isinstance(item.get("id"), str)]

    def get_challenge(self, challenge_id: str) -> dict:
        if self.platform != "ctfy":
            raise ArenaError("get_challenge is only available on the CTFy backend")
        cid = self.ctfy_competition_id
        data = self._request(
            "GET", f"/challenges/{self._quote(self._code(challenge_id))}",
            params={"competition_id": cid},
        )
        if not isinstance(data, dict):
            raise ArenaError(f"challenge {challenge_id} returned {type(data).__name__}")
        return self._normalize_challenge(data)

    @staticmethod
    def _normalize_challenge(raw: dict) -> dict:
        questions = raw.get("questions") or []
        if not isinstance(questions, list):
            questions = []
        solved = raw.get("my_team_solved_question_ids") or []
        if not isinstance(solved, list):
            solved = []
        flag_count = len(questions) if questions else 1
        correct = len(solved)
        if not questions and raw.get("my_team_fully_solved"):
            correct = 1
        out = dict(raw)
        out.update({
            # legacy TSecBench-shaped fields used across sapper
            "unique_code": str(raw.get("id") or ""),
            "flag_count": flag_count,
            "correct_flag_count": correct,
            "is_completed": bool(
                raw.get("my_team_fully_solved")
                or (questions and len(solved) >= len(questions))
            ),
            "container_addr": [],
            # CTFy-specific but useful to every caller
            "questions": questions,
            "solved_question_ids": solved,
        })
        return out

    # -- CTFy: instances ---------------------------------------------------
    def list_instances(self, *, challenge_id: str = "", status: str = "",
                       competition_id: str | None = None,
                       limit: int = 1000, offset: int = 0) -> list[dict]:
        if self.platform != "ctfy":
            raise ArenaError("list_instances is only available on the CTFy backend")
        cid = competition_id
        if cid is None:
            try:
                cid = self.ctfy_competition_id
            except ArenaError:
                cid = ""
        data = self._request("GET", "/instances", params={
            "challenge_id": challenge_id,
            "status": status,
            "competition_id": cid or "",
            "limit": limit,
            "offset": offset,
        })
        items = _page_items(data, "instances")
        return [self._normalize_instance(item) for item in items
                if isinstance(item.get("id"), str)]

    def get_instance(self, instance_id: str,
                     competition_id: str | None = None) -> dict:
        if self.platform != "ctfy":
            raise ArenaError("get_instance is only available on the CTFy backend")
        params = {}
        cid = competition_id
        if cid is None:
            try:
                cid = self.ctfy_competition_id
            except ArenaError:
                cid = ""
        if cid:
            params["competition_id"] = cid
        data = self._request(
            "GET", f"/instances/{self._quote(instance_id)}", params=params
        )
        if not isinstance(data, dict):
            raise ArenaError(f"instance {instance_id} returned {type(data).__name__}")
        return self._normalize_instance(data)

    @staticmethod
    def _normalize_instance(raw: dict, code: str | None = None) -> dict:
        inst = dict(raw)
        challenge = code or str(raw.get("challenge_id") or "")
        urls = [str(u) for u in (raw.get("entry_urls") or []) if u]
        addrs: list[str] = []
        for service in raw.get("services") or []:
            if not isinstance(service, dict):
                continue
            host = str(service.get("host") or "")
            port = service.get("port")
            if host and port:
                addrs.append(f"{host}:{port}")
            elif host:
                addrs.append(host)
        addrs.extend(urls)
        # preserve order, drop duplicates
        seen = set()
        addrs = [a for a in addrs if not (a in seen or seen.add(a))]
        inst.update({
            "unique_code": challenge,
            "instance_id": str(raw.get("id") or ""),
            "container_addr": addrs,
            "entry_urls": urls,
            "questions": raw.get("questions") or [],
        })
        return inst

    def capacity(self) -> dict:
        """Active instances vs the competition's per-team cap."""
        comp = self.get_competition()
        try:
            cap = int(comp.get("max_instances_per_team_effective")
                      or comp.get("max_instances_per_team") or 0)
        except (TypeError, ValueError):
            cap = 0
        instances = self.list_instances(limit=1000)
        active = [i for i in instances
                  if i.get("status") in CTFY_ACTIVE_STATUSES]
        return {
            "competition_id": comp.get("id") or self.ctfy_competition_id,
            "cap": cap,
            "active": len(active),
            "available": (max(0, cap - len(active)) if cap else None),
            "instances": active,
        }

    def _find_live_instance(self, code: str, *,
                            refresh: bool = True) -> dict | None:
        listing_failed = False
        if refresh:
            try:
                items = self.list_instances(challenge_id=code, limit=100)
            except ArenaError:
                items = []
                listing_failed = True
            live = [i for i in items if i.get("status") in CTFY_LIVE_STATUSES]
            if live:
                live.sort(key=lambda i: (
                    i.get("status") != "ready",
                    -float(i.get("started_at") or 0),
                ))
                inst = self._normalize_instance(live[0], code)
                self._remember_instance(inst)
                return inst
        if listing_failed or not refresh:
            cached = self._cached_instance(code)
            if cached and cached.get("id"):
                try:
                    inst = self.get_instance(str(cached["id"]))
                except ArenaError:
                    self._forget_instance(code)
                else:
                    if inst.get("status") in CTFY_LIVE_STATUSES:
                        self._remember_instance(inst)
                        return inst
                    if inst.get("status") not in CTFY_ACTIVE_STATUSES:
                        self._forget_instance(code)
        return None

    def _poll_instance(self, code: str, instance_id: str) -> dict:
        deadline = time.monotonic() + self.start_timeout
        delay = self.poll_interval
        last: dict | None = None
        while True:
            inst = self.get_instance(instance_id)
            last = inst
            status = str(inst.get("status") or "")
            if status == "ready":
                self._remember_instance(inst)
                return self._normalize_instance(inst, code)
            if status in ("error", "stopped", "stopping"):
                self._remember_instance(inst)
                raise ArenaError(
                    f"instance {instance_id} for {code} ended as {status}"
                    + (f" ({inst.get('error_code')})" if inst.get("error_code") else ""),
                    code=f"instance_{status}", status=409, detail=inst,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArenaError(
                    f"instance {instance_id} for {code} still {status or 'pending'} "
                    f"after {int(self.start_timeout)}s — keep it and retry later; "
                    f"do not start a duplicate",
                    code="start_timeout", detail=last,
                )
            wait = self._start_wait_hint() or (last or {}).get("start_wait")
            msg = f"[arena] {code}: instance {status or 'pending'}"
            if isinstance(wait, dict):
                if wait.get("queue_position"):
                    msg += f", queue={wait['queue_position']}"
                if wait.get("expected_start_p50_s"):
                    msg += f", p50={wait['expected_start_p50_s']}s"
            print(msg, file=sys.stderr)
            time.sleep(min(delay, remaining))
            delay = min(delay * 1.4, 10.0)

    def _start_wait_hint(self) -> dict | None:
        return self._last_start_wait

    def start_challenge(self, unique_code: str, ttl: int | None = None) -> dict:
        if self.platform != "ctfy":
            return self._tsec_start_challenge(unique_code)
        code = self._code(unique_code)
        existing = self._find_live_instance(code)
        if existing is not None:
            if existing.get("status") == "starting":
                return self._poll_instance(code, str(existing["id"]))
            return existing

        cap = self.capacity()
        if cap["cap"] and cap["active"] >= cap["cap"]:
            raise ArenaError(
                f"team instance cap reached ({cap['active']}/{cap['cap']}); "
                f"close an instance before starting {code}",
                code="team_capacity_exceeded", status=429, detail=cap,
            )

        body: dict = {
            "challenge_id": code,
            "competition_id": self.ctfy_competition_id,
        }
        if ttl is not None:
            body["ttl"] = int(ttl)
        try:
            started = self._request("POST", "/instances", body=body)
        except ArenaError as exc:
            if exc.code == "instance_already_running" or (
                    exc.status == 409 and "already" in str(exc).lower()):
                inst = self._find_live_instance(code)
                if inst is not None:
                    if inst.get("status") == "starting":
                        return self._poll_instance(code, str(inst["id"]))
                    return inst
            raise
        if not isinstance(started, dict) or not started.get("id"):
            raise ArenaError(
                f"start instance for {code} returned no id: {started!r}",
                detail=started,
            )
        instance_id = str(started["id"])
        self._last_start_wait = started.get("start_wait") if isinstance(
            started.get("start_wait"), dict) else None
        status = str(started.get("status") or "starting")
        self._remember_instance({
            "id": instance_id,
            "challenge_id": code,
            "competition_id": body["competition_id"],
            "status": status,
            "started_at": time.time(),
            "questions": [],
            "entry_urls": [],
            "services": [],
        })
        if status == "ready":
            try:
                fresh = self.get_instance(instance_id)
            except ArenaError:
                fresh = started
            self._remember_instance(fresh)
            return self._normalize_instance(fresh, code)
        return self._poll_instance(code, instance_id)

    def close_challenge(self, unique_code: str,
                        instance_id: str | None = None) -> dict:
        if self.platform != "ctfy":
            return self._tsec_close_challenge(unique_code)
        code = self._code(unique_code)
        iid = (instance_id or "").strip()
        if not iid:
            cached = self._cached_instance(code)
            if cached and cached.get("id"):
                iid = str(cached["id"])
        if not iid:
            inst = self._find_live_instance(code)
            if inst is not None:
                iid = str(inst["id"])
        if not iid:
            return {"status": 200, "closed": False, "unique_code": code,
                    "reason": "no_live_instance"}
        params = {}
        try:
            params["competition_id"] = self.ctfy_competition_id
        except ArenaError:
            pass
        try:
            data = self._request(
                "DELETE", f"/instances/{self._quote(iid)}", params=params
            )
        except ArenaError as exc:
            if exc.code == "instance_not_found" or exc.status == 404:
                self._forget_instance(code)
                return {"status": 200, "closed": True, "unique_code": code,
                        "instance_id": iid, "result": {"status": "stopped"}}
            raise
        self._mark_instance_stopped(code, iid)
        return {
            "status": 200,
            "closed": True,
            "unique_code": code,
            "instance_id": iid,
            "result": data if isinstance(data, dict) else {"raw": data},
        }

    # -- CTFy: submissions -------------------------------------------------
    @staticmethod
    def _normalize_answer(answer: object) -> str | list[str]:
        if isinstance(answer, str):
            if not answer.strip():
                raise ArenaError("cannot submit an empty answer")
            return answer.strip()
        if isinstance(answer, list):
            values = [str(x).strip() for x in answer]
            if not values or any(not v for v in values):
                raise ArenaError("answer list cannot be empty or contain empty strings")
            return values
        raise ArenaError("answer must be a string or a list of strings")

    def _resolve_instance(self, code: str, instance_id: str | None = None) -> dict:
        if instance_id:
            inst = self.get_instance(instance_id)
            if inst.get("challenge_id") and inst["challenge_id"] != code:
                raise ArenaError(
                    f"instance {instance_id} belongs to {inst['challenge_id']}, not {code}"
                )
        else:
            inst = self._find_live_instance(code)
            if inst is None:
                raise ArenaError(
                    f"no live instance for {code}; start one first "
                    f"(`sapper arena start {code}`)",
                    code="no_instance", status=409,
                )
        if inst.get("status") == "starting":
            inst = self._poll_instance(code, str(inst["id"]))
        if inst.get("status") != "ready":
            raise ArenaError(
                f"instance {inst.get('id')} for {code} is {inst.get('status')}, not ready",
                code="instance_not_ready", detail=inst,
            )
        return inst

    @staticmethod
    def _question_rows(inst: dict) -> list[dict]:
        rows = inst.get("questions") or []
        return [q for q in rows if isinstance(q, dict)]

    @classmethod
    def _resolve_question(cls, inst: dict, question_id: str | None) -> str:
        rows = cls._question_rows(inst)
        ids = [str(q.get("id")) for q in rows if q.get("id")]
        if question_id:
            if ids and question_id not in ids:
                raise ArenaError(
                    f"unknown question {question_id!r} for this instance; "
                    f"available: {', '.join(ids)}",
                    code="unknown_question",
                    detail={"question_ids": ids},
                )
            return question_id
        if not rows:
            return "flag"

        open_rows = [
            q for q in rows
            if q.get("unlocked", True) and not q.get("answered_correctly")
        ]
        if len(open_rows) == 1:
            return str(open_rows[0].get("id") or "flag")
        if not open_rows:
            solved = [str(q.get("id")) for q in rows if q.get("answered_correctly")]
            locked = [str(q.get("id")) for q in rows if not q.get("unlocked", True)]
            raise ArenaError(
                "no unsolved unlocked question remains"
                + (f" (solved: {', '.join(solved)})" if solved else "")
                + (f" (locked: {', '.join(locked)})" if locked else ""),
                code="no_open_question",
                detail={"questions": rows},
            )
        detail_rows = [
            {"id": q.get("id"), "prompt": q.get("prompt"),
             "requires": q.get("requires") or []}
            for q in rows
            if q.get("unlocked", True) and not q.get("answered_correctly")
        ]
        raise ArenaError(
            "this challenge has several open questions; pass --question <id> "
            "explicitly (a wrong answer consumes an attempt): "
            + ", ".join(str(q["id"]) for q in detail_rows),
            code="question_ambiguous",
            detail={"questions": detail_rows},
        )

    def submit_flag(self, unique_code: str, flag: object, *,
                    instance_id: str | None = None,
                    question_id: str | None = None,
                    answer: object | None = None) -> dict:
        if self.platform != "ctfy":
            return self._tsec_submit_flag(unique_code, flag)  # type: ignore[arg-type]
        code = self._code(unique_code)
        payload_answer = self._normalize_answer(answer if answer is not None else flag)
        inst = self._resolve_instance(code, instance_id)
        iid = str(inst["id"])
        qid = self._resolve_question(inst, question_id or None)
        rows = self._question_rows(inst)
        qrow = next((q for q in rows if str(q.get("id")) == qid), None)
        if qrow is not None:
            if not qrow.get("unlocked", True):
                raise ArenaError(
                    f"question {qid} is locked; answer {qrow.get('requires') or 'its prerequisite'} first",
                    code="question_locked", detail=qrow,
                )
            remaining = qrow.get("attempts_remaining")
            if isinstance(remaining, int) and remaining <= 0:
                raise ArenaError(
                    f"question {qid} has no attempts remaining",
                    code="question_attempt_cap_exceeded", status=429, detail=qrow,
                )
        data = self._request("POST", "/submissions", body={
            "instance_id": iid,
            "question_id": qid,
            "answer": payload_answer,
        })
        if not isinstance(data, dict):
            raise ArenaError(f"submission returned {type(data).__name__}")
        correct = bool(data.get("correct"))
        out = dict(data)
        out.update({
            "status": 200,
            "platform": "ctfy",
            "accepted": correct,
            "correct": correct,
            "result": data,
            "raw": data,
            "unique_code": code,
            "instance_id": iid,
            "question_id": data.get("question_id") or qid,
        })
        self._update_cached_after_submit(code, iid, qid, correct, data)
        return out

    def _update_cached_after_submit(self, code: str, instance_id: str,
                                    question_id: str, correct: bool,
                                    reply: dict) -> None:
        cached = self._cached_instance(code)
        if not cached or str(cached.get("id")) != instance_id:
            return
        cached["challenge_id"] = code
        rows = [dict(q) for q in (cached.get("questions") or [])
                if isinstance(q, dict)]
        for row in rows:
            if str(row.get("id")) != question_id:
                continue
            if correct:
                row["answered_correctly"] = True
            if reply.get("attempts_remaining") is not None:
                row["attempts_remaining"] = reply["attempts_remaining"]
        cached["questions"] = rows
        if reply.get("challenge_fully_solved"):
            cached["fully_solved"] = True
        self._remember_instance(cached)

    # -- CTFy: hints -------------------------------------------------------
    def get_hint(self, unique_code: str, hint_id: str | None = None) -> dict:
        if self.platform != "ctfy":
            return self._tsec_get_hint(unique_code)
        code = self._code(unique_code)
        cid = self.ctfy_competition_id
        base = (f"/competitions/{self._quote(cid)}/challenges/"
                f"{self._quote(code)}/hints")
        if hint_id:
            data = self._request(
                "POST", f"{base}/{self._quote(hint_id)}/view"
            )
            out = {"status": 200, "platform": "ctfy",
                   "result": data, "unique_code": code, "hint_id": hint_id}
            if isinstance(data, dict):
                out.update(data)
            return out
        data = self._request("GET", base)
        return {"status": 200, "platform": "ctfy",
                "result": data, "unique_code": code}

    # -- CTFy: VPN ---------------------------------------------------------
    def _require_ready_instance(self, code: str,
                                instance_id: str | None = None) -> dict:
        return self._resolve_instance(code, instance_id)

    def _default_vpn_dir(self) -> Path:
        if self.vpn_dir:
            return self.vpn_dir
        return self._state_path().parent / "vpn"

    @staticmethod
    def _vpn_backend_from(inst: dict, text: str, filename: str) -> str:
        backend = str(inst.get("vpn_backend") or "").lower()
        if backend in ("wireguard", "openvpn"):
            return backend
        head = text[:4096]
        if "[Interface]" in head or filename.endswith(".conf"):
            return "wireguard"
        return "openvpn"

    # CEA's fleet runner proved the access model: every interactive CTFy
    # challenge is reachable through a per-instance WireGuard tunnel, and the
    # target host is a *.internal name.  Two details matter operationally:
    #   1. wg-quick applies the config's DNS= line by replacing
    #      /etc/resolv.conf.  In a container that kills the public resolver the
    #      model gateway needs, so strip DNS= and resolve internal names
    #      ourselves via the VPN's DNS server into /etc/hosts.
    #   2. wg-quick needs root; the image grants agent passwordless sudo for
    #      exactly wg-quick and tee /etc/hosts, and the container must run with
    #      --cap-add=NET_ADMIN.
    @staticmethod
    def _internal_hostnames(inst: dict) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for url in inst.get("entry_urls") or []:
            try:
                host = urllib.parse.urlsplit(str(url)).hostname
            except ValueError:
                host = None
            if host:
                host = host.rstrip(".").lower()
            if host and host.endswith(".internal") and host not in seen:
                seen.add(host)
                out.append(host)
        for service in inst.get("services") or []:
            if not isinstance(service, dict):
                continue
            host = str(service.get("host") or "").rstrip(".").lower()
            if host.endswith(".internal") and host not in seen:
                seen.add(host)
                out.append(host)
        return out

    @classmethod
    def _needs_vpn(cls, inst: dict) -> bool:
        """The instance is VPN-only (CTFy ``network_topology=engagement``).

        ``network_topology`` is the documented authority; stronger signals
        (an endpoint, a vpn-access service or a ``*.internal`` target) win even
        when the topology string is missing or stale.  ``vpn_backend`` alone is
        only a fallback: some responses carry it for every instance.
        """
        topology = str(inst.get("network_topology") or "").lower()
        if topology == "engagement":
            return True
        if inst.get("vpn_endpoint"):
            return True
        if any(str(svc.get("access") or "").lower() == "vpn"
               for svc in inst.get("services") or [] if isinstance(svc, dict)):
            return True
        if cls._internal_hostnames(inst):
            return True
        if topology == "simple":
            return False
        return bool(inst.get("vpn_backend"))

    _WG_DNS_RE = re.compile(r"^\s*DNS\s*=\s*(.+)$", re.IGNORECASE | re.MULTILINE)

    @classmethod
    def _wg_dns_servers(cls, config_text: str) -> list[str]:
        out: list[str] = []
        for match in cls._WG_DNS_RE.finditer(config_text):
            for part in match.group(1).split(","):
                addr = part.strip()
                if not addr or addr in out:
                    continue
                try:
                    ipaddress.ip_address(addr)
                except ValueError:
                    # wg-quick's DNS= also accepts search domains; they are
                    # useless as ``dig @server`` targets.
                    continue
                out.append(addr)
        return out

    @staticmethod
    def _strip_wg_dns(config_text: str) -> str:
        """Remove Interface DNS= lines before wg-quick touches resolv.conf."""
        kept = [ln for ln in config_text.splitlines()
                if not re.match(r"^\s*DNS\s*=", ln, re.IGNORECASE)]
        return "\n".join(kept) + "\n"

    @staticmethod
    def _safe_iface_stem(path: Path, code: str | None = None) -> str:
        """Deterministic WireGuard interface name (<=15 bytes).

        A challenge-scoped hash avoids collisions between concurrent instances
        and the filename-length trap; custom --out names still work when they
        are short and safe.
        """
        if code:
            return "sapper-" + hashlib.sha1(code.encode("utf-8")).hexdigest()[:8]
        stem = path.stem
        if len(stem) <= WG_IFACE_MAX and re.fullmatch(r"[A-Za-z0-9_-]+", stem):
            return stem
        return "sapper-" + hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]

    @staticmethod
    def _vpn_hosts_tag(inst: dict | None, code: str | None,
                       instance_id: str | None = None) -> str:
        raw = ""
        if instance_id:
            raw = str(instance_id)
        elif isinstance(inst, dict) and inst.get("id"):
            raw = str(inst["id"])
        elif code:
            raw = str(code)
        return re.sub(r"[\r\n]+", " ", raw).strip() or "unknown"

    @staticmethod
    def _is_root() -> bool:
        geteuid = getattr(os, "geteuid", None)
        return bool(geteuid and geteuid() == 0)

    def _vpn_command(self, exe: str, args: list[str]) -> list[str]:
        """Root directly, otherwise passwordless sudo (image ships sudoers)."""
        if self._is_root():
            return [exe] + args
        sudo = shutil.which("sudo")
        if sudo:
            return [sudo, "-n", exe] + args
        return [exe] + args

    def _run_vpn_command(self, cmd: list[str], *, timeout: float,
                         input_text: str | None = None):
        kwargs = {"capture_output": True, "text": True, "timeout": timeout}
        if input_text is None:
            kwargs["stdin"] = subprocess.DEVNULL
        else:
            kwargs["input"] = input_text
        return subprocess.run(cmd, **kwargs)

    @staticmethod
    def _iface_exists(iface: str) -> bool:
        return bool(iface) and (Path("/sys/class/net") / iface).exists()

    def _wait_interface(self, iface: str, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._iface_exists(iface):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _read_hosts_text(self) -> str:
        try:
            return Path("/etc/hosts").read_text(encoding="utf-8",
                                                errors="replace")
        except OSError as exc:
            raise ArenaError(f"cannot read /etc/hosts: {exc}") from exc

    def _write_hosts_text(self, text: str) -> None:
        if self._is_root():
            try:
                Path("/etc/hosts").write_text(text, encoding="utf-8")
                return
            except OSError as exc:
                raise ArenaError(f"cannot write /etc/hosts: {exc}") from exc
        sudo = shutil.which("sudo")
        if not sudo:
            raise ArenaError(
                "cannot write /etc/hosts as non-root and sudo is unavailable; "
                "run the container with the sapper VPN sudoers rule"
            )
        proc = self._run_vpn_command([sudo, "-n", "tee", "/etc/hosts"],
                                     timeout=15, input_text=text)
        if proc.returncode != 0:
            raise ArenaError(
                "cannot write /etc/hosts via sudo (needs the image's "
                "sapper-vpn sudoers rule): "
                + (proc.stderr or proc.stdout or "").strip()[:300]
            )

    def _hosts_update(self, entries: list[str], tag: str) -> dict:
        """Replace sapper's marked block for one instance in /etc/hosts."""
        begin = _VPN_HOSTS_BEGIN.format(tag=tag)
        end = _VPN_HOSTS_END.format(tag=tag)
        text = self._read_hosts_text()
        pattern = re.compile(
            re.escape(begin) + r"\n.*?" + re.escape(end) + r"\n?", re.DOTALL)
        text = pattern.sub("", text)
        # A crash between begin/end should not wedge the rest of the file.
        if begin in text:
            text = text.replace(begin + "\n", "").replace(begin, "")
        lines = text.splitlines()
        if entries:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(begin)
            lines.extend(entries)
            lines.append(end)
        new_text = "\n".join(lines).rstrip("\n") + "\n"
        self._write_hosts_text(new_text)
        return {"tag": tag, "entries": list(entries), "written": True}

    def _resolve_internal_host(self, host: str,
                               servers: list[str]) -> tuple[list[str], str]:
        dig = shutil.which("dig")
        if not dig:
            return [], "dig not found (install dnsutils)"
        for server in servers[:2]:
            try:
                proc = self._run_vpn_command(
                    [dig, "+short", "+time=2", "+tries=1", f"@{server}", host],
                    timeout=7)
            except (OSError, subprocess.TimeoutExpired) as exc:
                last = str(exc)
                continue
            ips: list[str] = []
            for line in (proc.stdout or "").splitlines():
                value = line.strip().rstrip(".")
                try:
                    ipaddress.ip_address(value)
                except ValueError:
                    continue
                if value not in ips:
                    ips.append(value)
            if ips:
                return ips, ""
            last = (proc.stderr or "").strip()[:200] or "no address in answer"
        return [], last or "no answer"

    def _map_internal_hosts(self, inst: dict, servers: list[str],
                            tag: str) -> dict:
        hosts = self._internal_hostnames(inst)
        result = {"tag": tag, "hosts": hosts, "entries": [],
                  "written": False, "warnings": []}
        if not hosts:
            return result
        if not servers:
            result["warnings"].append(
                "VPN config has no DNS= line; *.internal names cannot be "
                "mapped into /etc/hosts — use service IPs or set up DNS")
            return result
        entries: list[str] = []
        for host in hosts:
            ips, error = self._resolve_internal_host(host, servers)
            if not ips:
                result["warnings"].append(f"{host}: {error}")
                continue
            entries.extend(f"{ip} {host}" for ip in ips)
        if not entries:
            result["warnings"].append(
                "VPN DNS did not answer for any *.internal target")
            return result
        try:
            self._hosts_update(entries, tag)
        except ArenaError as exc:
            result["warnings"].append(str(exc))
            return result
        result["entries"] = entries
        result["written"] = True
        return result

    # -- CTFy: VPN state ---------------------------------------------------
    def _cached_vpn(self, code: str) -> dict | None:
        state = self._read_state()
        entry = state.get("vpn", {}).get(code)
        return entry if isinstance(entry, dict) else None

    def _remember_vpn(self, code: str, handle: dict) -> None:
        state = self._read_state()
        if not isinstance(state.get("vpn"), dict):
            state["vpn"] = {}
        entry = dict(state["vpn"].get(code) or {})
        entry.update(handle)
        entry["updated_at"] = time.time()
        state["vpn"][code] = entry
        state["platform"] = "ctfy"
        self._write_state(state)

    # -- CTFy: download / up / down ---------------------------------------
    def download_vpn_config(self, unique_code: str, *,
                            out: str | os.PathLike | None = None,
                            instance_id: str | None = None,
                            up: bool = False) -> dict:
        if self.platform != "ctfy":
            raise ArenaError("VPN config is only available on the CTFy backend")
        code = self._code(unique_code)
        inst = self._require_ready_instance(code, instance_id)
        iid = str(inst["id"])
        needs_vpn = self._needs_vpn(inst)
        if not needs_vpn:
            # Direct-access instances return 404 from /vpn-config (CEA's live
            # behaviour).  Skip the doomed request, and keep the cached state
            # honest so a stale tunnel from an older instance cannot appear as
            # "up" in the brief.
            self._remember_vpn(code, {
                "code": code, "instance_id": iid, "needs_vpn": False,
                "up": False,
            })
            return {
                "status": 200, "platform": "ctfy", "unique_code": code,
                "instance_id": iid, "backend": None, "path": None,
                "bytes": 0, "content_type": "",
                "needs_vpn": False, "internal_hosts": [],
                "skipped": "instance is directly reachable; no VPN needed",
            }
        try:
            params = {"competition_id": self.ctfy_competition_id}
        except ArenaError:
            params = {}
        raw, content_type = self._request_bytes(
            "GET", f"/instances/{self._quote(iid)}/vpn-config", params=params
        )
        text = raw.decode("utf-8", "replace")
        backend = self._vpn_backend_from(inst, text, "")
        if out is None:
            # wg-quick derives the interface name from the file stem and Linux
            # caps it at 15 chars, so challenge ids do not fit. Use a short,
            # stable name; the result still carries the challenge id.
            short = "sapper-" + hashlib.sha1(code.encode("utf-8")).hexdigest()[:8]
            ext = ".conf" if backend == "wireguard" else ".ovpn"
            target = self._default_vpn_dir() / f"{short}{ext}"
        else:
            target = Path(out).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        internal_hosts = self._internal_hostnames(inst)
        previous = self._cached_vpn(code) or {}
        same_instance = str(previous.get("instance_id") or "") == iid
        self._remember_vpn(code, {
            "code": code, "instance_id": iid, "backend": backend,
            "config_path": str(target), "needs_vpn": needs_vpn,
            "internal_hosts": internal_hosts,
            "up": bool(previous.get("up")) and same_instance,
        })
        result = {
            "status": 200,
            "platform": "ctfy",
            "unique_code": code,
            "instance_id": iid,
            "backend": backend,
            "path": str(target),
            "bytes": len(raw),
            "content_type": content_type,
            "needs_vpn": needs_vpn,
            "internal_hosts": internal_hosts,
        }
        if up:
            try:
                result["up"] = self.vpn_up(target, backend,
                                           instance=inst, code=code)
            except ArenaError as exc:
                # The config is still on disk; a failed tunnel is a transport
                # problem, not a failed download.  The worker keeps going and
                # the brief tells the agent whether the tunnel is up.
                result["up"] = {
                    "ok": False, "backend": backend, "error": str(exc),
                    "code": exc.code,
                }
                print(f"[arena] {code}: VPN up failed: {exc}", file=sys.stderr)
        return result

    def vpn_up(self, config_path: str | os.PathLike,
               backend: str | None = None, *,
               instance: dict | None = None,
               code: str | None = None) -> dict:
        path = Path(config_path).expanduser()
        if not path.exists():
            raise ArenaError(f"VPN config not found: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        backend = (backend or "").lower()
        if backend not in ("wireguard", "openvpn"):
            backend = "wireguard" if "[Interface]" in text[:4096] else "openvpn"
        if backend == "wireguard":
            exe = shutil.which("wg-quick")
            if not exe:
                raise ArenaError(
                    "wg-quick not found; install wireguard-tools (and run the "
                    "container with --cap-add=NET_ADMIN) or bring the tunnel "
                    "up on the host"
                )
            stem = self._safe_iface_stem(path, code)
            up_dir = path.parent / ".up"
            up_dir.mkdir(parents=True, exist_ok=True)
            effective = up_dir / f"{stem}.conf"
            effective.write_text(self._strip_wg_dns(text), encoding="utf-8")
            try:
                os.chmod(effective, 0o600)
            except OSError:
                pass
            dns_servers = self._wg_dns_servers(text)
            cached = self._cached_vpn(code) if code else None
            current_iid = (str(instance.get("id") or "")
                           if isinstance(instance, dict) else "")
            cached_iid = str((cached or {}).get("instance_id") or "")
            if self._iface_exists(stem):
                same_tunnel = bool(
                    cached and cached.get("up")
                    and str(cached.get("interface") or "") == stem
                    and (not current_iid or cached_iid == current_iid))
                if same_tunnel:
                    # A second worker re-spawned for the same live instance:
                    # reusing the tunnel is correct; a second wg-quick up would
                    # fail with "already exists" and only confuse the brief.
                    handle = dict(cached)
                    handle.update({"ok": True, "up": True,
                                   "already_up": True})
                    return handle
                # The interface carries the previous instance's peer key and
                # AllowedIPs. A new instance for the same challenge must not
                # inherit them, so replace the stale tunnel before up.
                stale_down = self._vpn_command(exe, ["down", str(effective)])
                try:
                    self._run_vpn_command(stale_down, timeout=30)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            cmd = self._vpn_command(exe, ["up", str(effective)])
            try:
                proc = self._run_vpn_command(cmd, timeout=VPN_READY_WAIT_S)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ArenaError(f"VPN up failed for {path}: {exc}") from exc
            if proc.returncode != 0:
                raise ArenaError(
                    f"VPN up failed for {path} (exit {proc.returncode}): "
                    f"{(proc.stderr or proc.stdout or '').strip()[:500]}",
                    detail={"command": cmd, "returncode": proc.returncode},
                )
            if not self._wait_interface(stem):
                raise ArenaError(
                    f"wg-quick returned success but interface {stem!r} is "
                    f"missing (does the container have --cap-add=NET_ADMIN?)",
                    detail={"command": cmd, "interface": stem},
                )
            tag = self._vpn_hosts_tag(instance, code)
            mapping = (self._map_internal_hosts(instance, dns_servers, tag)
                       if isinstance(instance, dict) else
                       {"tag": tag, "hosts": [], "entries": [],
                        "written": False, "warnings": []})
            handle = {
                "ok": True, "up": True, "backend": backend,
                "config_path": str(path), "effective_path": str(effective),
                "interface": stem, "dns_servers": dns_servers,
                "hosts_tag": tag, "hosts": mapping,
                "command": cmd, "returncode": 0,
                "stdout": (proc.stdout or "").strip()[:1000],
            }
            if current_iid:
                handle["instance_id"] = current_iid
            if code:
                self._remember_vpn(code, handle)
            return handle
        exe = shutil.which("openvpn")
        if not exe:
            raise ArenaError(
                "openvpn not found; install openvpn (and run the container "
                "with --cap-add=NET_ADMIN) or bring the tunnel up on the host"
            )
        cmd = self._vpn_command(exe, ["--config", str(path), "--daemon"])
        try:
            proc = self._run_vpn_command(cmd, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArenaError(f"VPN up failed for {path}: {exc}") from exc
        if proc.returncode != 0:
            raise ArenaError(
                f"VPN up failed for {path} (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}",
                detail={"command": cmd, "returncode": proc.returncode},
            )
        handle = {
            "ok": True, "up": True, "backend": backend,
            "config_path": str(path), "command": cmd, "returncode": 0,
            "stdout": (proc.stdout or "").strip()[:1000],
        }
        if code:
            self._remember_vpn(code, handle)
        return handle

    def vpn_down(self, config_path: str | os.PathLike | None = None,
                 backend: str | None = None, *,
                 code: str | None = None,
                 instance_id: str | None = None) -> dict:
        handle = self._cached_vpn(code) if code else None
        if not config_path and not handle:
            raise ArenaError("VPN down needs a config path or a known challenge id")
        path = Path(config_path).expanduser() if config_path else None
        if path is not None and not path.exists():
            path = None
        if backend is None and handle:
            backend = str(handle.get("backend") or "")
        if backend not in ("wireguard", "openvpn"):
            if path is not None:
                head = path.read_text(encoding="utf-8", errors="replace")[:4096]
                backend = "wireguard" if "[Interface]" in head else "openvpn"
            else:
                backend = str((handle or {}).get("backend") or "wireguard")
        tag = ((handle or {}).get("hosts_tag")
               or self._vpn_hosts_tag(None, code, instance_id))
        hosts_result = None
        if tag:
            try:
                hosts_result = self._hosts_update([], tag)
            except ArenaError as exc:
                hosts_result = {"tag": tag, "entries": [], "written": False,
                                "warnings": [str(exc)]}
        if backend == "openvpn":
            if code:
                self._remember_vpn(code, {"up": False})
            raise ArenaError(
                "openvpn down is not automated; stop the daemon you started "
                "(e.g. pkill -f the exact --config path)"
            )
        effective = (handle or {}).get("effective_path")
        if effective and Path(effective).exists():
            target = str(effective)
        elif path is not None:
            stem = self._safe_iface_stem(path, code)
            candidate = path.parent / ".up" / f"{stem}.conf"
            target = str(candidate if candidate.exists() else path)
        elif (handle or {}).get("interface"):
            target = str(handle["interface"])
        elif handle is not None and not handle.get("up"):
            # Download-only state: no tunnel was ever created, so a close or
            # deadline sweep is a no-op rather than a wg-quick error.
            return {"ok": True, "up": False, "backend": backend,
                    "skipped": "tunnel was never brought up by sapper",
                    "hosts": hosts_result}
        else:
            raise ArenaError("VPN down cannot locate the tunnel config")
        exe = shutil.which("wg-quick")
        if not exe:
            raise ArenaError("wg-quick not found")
        cmd = self._vpn_command(exe, ["down", target])
        try:
            proc = self._run_vpn_command(cmd, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArenaError(f"VPN down failed for {target}: {exc}") from exc
        if proc.returncode != 0:
            raise ArenaError(
                f"VPN down failed for {target} (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}"
            )
        if code:
            self._remember_vpn(code, {"up": False, "down_at": time.time(),
                                      "down_returncode": 0})
        return {
            "ok": True, "up": False, "backend": backend, "command": cmd,
            "returncode": 0, "target": target,
            "hosts": hosts_result,
        }

    # -- CTFy: attachments -------------------------------------------------
    def _default_attachments_dir(self) -> Path:
        if self.attachments_dir:
            return self.attachments_dir
        return self._state_path().parent / "attachments"

    @staticmethod
    def _safe_attachment_name(name: str) -> str:
        if not name or name.startswith(("/", "\\")):
            raise ArenaError(f"unsafe attachment name {name!r}")
        parts = PurePosixPath(name.replace("\\", "/")).parts
        if not parts or any(p in ("", ".", "..") for p in parts):
            raise ArenaError(f"unsafe attachment name {name!r}")
        return "/".join(parts)

    def _attachment_bytes(self, code: str, name: str, *, per_team: bool,
                          instance_id: str | None = None) -> tuple[bytes, str]:
        safe = self._safe_attachment_name(name)
        encoded = self._quote(safe, keep_slash=True)
        try:
            params = {"competition_id": self.ctfy_competition_id}
        except ArenaError:
            params = {}
        if per_team:
            inst = self._require_ready_instance(code, instance_id)
            iid = str(inst["id"])
            return self._request_bytes(
                "GET", f"/instances/{self._quote(iid)}/attachments/{encoded}",
                params=params,
            )
        try:
            return self._request_bytes(
                "GET", f"/challenges/{self._quote(code)}/attachments/{encoded}",
                params=params,
            )
        except ArenaError as exc:
            if exc.code == "per_team_attachment":
                try:
                    inst = self._require_ready_instance(code, instance_id)
                except ArenaError:
                    raise exc
                iid = str(inst["id"])
                return self._request_bytes(
                    "GET", f"/instances/{self._quote(iid)}/attachments/{encoded}",
                    params=params,
                )
            raise

    def download_attachments(self, unique_code: str, *,
                             dest: str | os.PathLike | None = None,
                             instance_id: str | None = None,
                             names: list[str] | None = None) -> dict:
        if self.platform != "ctfy":
            raise ArenaError("attachments are only available on the CTFy backend")
        code = self._code(unique_code)
        challenge = self.get_challenge(code)
        all_atts = [a for a in (challenge.get("attachments") or [])
                    if isinstance(a, dict) and a.get("name")]
        if names:
            wanted = set(names)
            all_atts = [a for a in all_atts if a.get("name") in wanted]
        elif not all_atts:
            return {"status": 200, "unique_code": code, "dir": "",
                    "files": [], "errors": []}
        target_dir = (Path(dest).expanduser() if dest
                      else self._default_attachments_dir() / code)
        target_dir.mkdir(parents=True, exist_ok=True)
        files: list[dict] = []
        errors: list[dict] = []
        for att in all_atts:
            name = str(att.get("name"))
            size = int(att.get("size") or 0)
            per_team = bool(att.get("per_team"))
            try:
                safe = self._safe_attachment_name(name)
            except ArenaError as exc:
                errors.append({"name": name, "error": str(exc)})
                continue
            target = target_dir.joinpath(*PurePosixPath(safe).parts)
            if target.exists() and (not size or target.stat().st_size == size):
                files.append({"name": name, "path": str(target),
                              "size": target.stat().st_size, "skipped": True})
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                data, _ctype = self._attachment_bytes(
                    code, safe, per_team=per_team, instance_id=instance_id
                )
                target.write_bytes(data)
                files.append({"name": name, "path": str(target),
                              "size": len(data), "per_team": per_team})
            except ArenaError as exc:
                errors.append({"name": name, "error": str(exc),
                               "code": exc.code, "per_team": per_team})
        return {"status": 200, "unique_code": code, "dir": str(target_dir),
                "files": files, "errors": errors}

    # -- CTFy: human/agent brief ------------------------------------------
    def build_brief(self, unique_code: str) -> str:
        if self.platform != "ctfy":
            raise ArenaError("brief is only available on the CTFy backend")
        code = self._code(unique_code)
        challenge = self.get_challenge(code)
        inst = self._find_live_instance(code)
        rows = self._question_rows(inst) if inst else []
        inst_by_id = {str(q.get("id")): q for q in rows}
        questions = [q for q in (challenge.get("questions") or [])
                     if isinstance(q, dict)]
        if not questions:
            questions = [{"id": qid, "prompt": "", "mode": None,
                          "choices": None, "requires": []}
                         for qid in inst_by_id]

        lines: list[str] = []
        lines.append(f"# {challenge.get('name') or code} ({code})")
        lines.append("")
        lines.append(f"- Platform: CTFy / TPCTF 2026")
        lines.append(f"- Competition: `{self.ctfy_competition_id}`")
        lines.append(f"- Category: {challenge.get('category') or '-'}"
                     f"    Difficulty: {challenge.get('difficulty') or '-'}")
        lines.append(f"- Instance policy: use only the targets below. Never "
                     f"attack the CTFy platform, scoring, VPN server, or other teams.")
        lines.append("")
        if inst:
            lines.append("## Instance")
            lines.append("")
            lines.append(f"- `instance_id`: `{inst.get('id')}`")
            lines.append(f"- status: `{inst.get('status')}`")
            if inst.get("expires_at"):
                lines.append(f"- expires_at: {_iso_utc(inst.get('expires_at'))}")
            if inst.get("ttl"):
                lines.append(f"- ttl: {inst.get('ttl')}s")
            lines.append(f"- start/reuse: `sapper arena start {code}`")
            lines.append(f"- stop: `sapper arena stop {code}`")
            lines.append("")
            lines.append("### Entry points")
            lines.append("")
            services = [s for s in (inst.get("services") or []) if isinstance(s, dict)]
            urls = inst.get("entry_urls") or []
            if services:
                lines.append("| type | host | port | access | label | path |")
                lines.append("|---|---|---|---|---|---|")
                for s in services:
                    lines.append(
                        f"| {s.get('service_type') or '-'} | {s.get('host') or '-'} "
                        f"| {s.get('port') or '-'} | {s.get('access') or '-'} "
                        f"| {s.get('label') or '-'} | {s.get('path') or '/'} |"
                    )
                creds = [s for s in services if s.get("credentials")]
                if creds:
                    lines.append("")
                    lines.append("Credentials (challenge-owned):")
                    lines.append("```json")
                    lines.append(json.dumps(
                        [{"host": s.get("host"), "port": s.get("port"),
                          "credentials": s.get("credentials")} for s in creds],
                        ensure_ascii=False, indent=2))
                    lines.append("```")
            else:
                lines.append("(no service table returned)")
            if urls:
                lines.append("")
                lines.append("Entry URLs:")
                for u in urls:
                    lines.append(f"- {u}")
            lines.append("")
            lines.append("### VPN")
            lines.append("")
            backend = inst.get("vpn_backend") or "unknown"
            topology = inst.get("network_topology") or "unknown"
            vpne = inst.get("vpn_endpoint") or {}
            needs = self._needs_vpn(inst)
            cached = self._cached_vpn(code) or {}
            lines.append(f"- backend: `{backend}`    "
                         f"network_topology: `{topology}`")
            if vpne:
                lines.append(f"- endpoint: `{vpne.get('host')}:{vpne.get('port')}/{vpne.get('proto')}`")
            if needs:
                lines.append("- required: **yes** — this instance is "
                             "VPN-only (`engagement` / `*.internal` target)")
            else:
                lines.append("- required: no — the returned entry points are "
                             "direct")
            if cached.get("up"):
                iface = cached.get("interface") or "-"
                lines.append(f"- status: **up** (`{iface}`), config "
                             f"`{cached.get('effective_path') or cached.get('config_path')}`")
                hosts = cached.get("hosts") or {}
                if hosts.get("entries"):
                    lines.append("- `*.internal` mapped in `/etc/hosts`: " +
                                 ", ".join(f"`{e}`" for e in hosts["entries"]))
                for warning in hosts.get("warnings") or []:
                    lines.append(f"- VPN warning: {warning}")
                lines.append("- do NOT restart/stop the tunnel or edit "
                             "`/etc/hosts`; another worker may share it")
            else:
                lines.append(f"- fetch config: `sapper arena vpn {code}`")
                lines.append(f"- bring it up: `sapper arena vpn {code} --up` "
                             f"(needs `--cap-add=NET_ADMIN`; otherwise run the "
                             f"config on the host)")
                internal = self._internal_hostnames(inst)
                if internal:
                    lines.append("- internal targets: " +
                                 ", ".join(f"`{h}`" for h in internal))
            lines.append(f"- stop tunnel: `sapper arena vpn {code} --down`")
            lines.append("- the `--up` path strips `DNS =` before `wg-quick` "
                         "(keeps the public resolver alive for the model "
                         "gateway) and maps `*.internal` through the tunnel's "
                         "DNS into `/etc/hosts`")
            lines.append("")
        else:
            lines.append("## Instance")
            lines.append("")
            lines.append(f"No live instance yet. Start one with "
                         f"`sapper arena start {code}`.")
            lines.append("")

        lines.append("## Questions — submit each one separately")
        lines.append("")
        lines.append("Every wrong answer can consume an attempt; do not guess.")
        lines.append("")
        for q in questions:
            qid = str(q.get("id") or "")
            state = inst_by_id.get(qid, {})
            lines.append(f"### `{qid}` — mode={q.get('mode') or 'text'}")
            lines.append("")
            prompt = (q.get("prompt") or "").strip()
            if prompt:
                lines.append("```text")
                lines.append(prompt)
                lines.append("```")
            choices = q.get("choices")
            if choices:
                lines.append("Choices: " + ", ".join(f"`{c}`" for c in choices))
            requires = q.get("requires") or state.get("requires") or []
            if requires:
                lines.append("Requires: " + ", ".join(f"`{r}`" for r in requires))
            if state:
                lines.append(
                    f"State: unlocked={state.get('unlocked', True)} "
                    f"answered_correctly={state.get('answered_correctly', False)} "
                    f"attempts_remaining={state.get('attempts_remaining')}"
                )
            lines.append("")
            if q.get("mode") in ("choice", "choices", "multi", "multiple_choice"):
                lines.append("Submit:")
                lines.append("```bash")
                lines.append(
                    f"sapper flag '<answer>' -c {code} --question {qid} --submit"
                )
                lines.append("```")
            else:
                lines.append("Submit a string:")
                lines.append("```bash")
                lines.append(
                    f"sapper flag '<answer>' -c {code} --question {qid} --submit"
                )
                lines.append("```")
                lines.append("Submit an array/structured answer:")
                lines.append("```bash")
                lines.append(
                    f"sapper flag '<json-array>' -c {code} --question {qid} "
                    f"--answer-json --submit"
                )
                lines.append("```")
            lines.append("")

        atts = [a for a in (challenge.get("attachments") or []) if isinstance(a, dict)]
        lines.append("## Attachments")
        lines.append("")
        if atts:
            for a in atts:
                lines.append(f"- `{a.get('name')}` ({a.get('size')} bytes"
                             + (", per-team render" if a.get("per_team") else "")
                             + ")")
            lines.append("")
            lines.append("Download:")
            lines.append("```bash")
            lines.append(f"sapper arena attachments {code}")
            lines.append("```")
        else:
            lines.append("(none)")
        lines.append("")
        desc = (challenge.get("description") or "").strip()
        if desc:
            lines.append("## Description")
            lines.append("")
            lines.append(desc)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def write_brief(self, unique_code: str,
                    path: str | os.PathLike | None = None) -> Path:
        code = self._code(unique_code)
        target = (Path(path).expanduser() if path else
                  self._state_path().parent / "briefs" / f"{code}.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.build_brief(code), encoding="utf-8")
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    def describe(self) -> dict:
        """Non-secret view of the effective configuration."""
        out = {
            "platform": self.platform,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "state_file": str(self._state_path()) if self.platform == "ctfy" else "",
        }
        if self.platform == "ctfy":
            out["competition_id"] = self.competition_id or "(auto)"
            try:
                cap = self.capacity()
            except ArenaError as exc:
                out["capacity_error"] = str(exc)
            else:
                out["cap"] = cap["cap"]
                out["active"] = cap["active"]
                out["available"] = cap["available"]
                out["competition_id"] = cap["competition_id"]
        return out

    # -- CTFy: persistent instance state ----------------------------------
    def _state_path(self) -> Path:
        if self.state_file:
            return self.state_file
        env = os.environ.get("SAPPER_ARENA_STATE")
        if env:
            return Path(env).expanduser()
        root = os.environ.get("SAPPER_ROOT")
        if root:
            return Path(root).expanduser() / "arena-instances.json"
        return Path.home() / ".sapper" / "arena-instances.json"

    def _read_state(self) -> dict:
        path = self._state_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "platform": "ctfy", "instances": {}}
        except OSError as exc:
            raise ArenaError(f"cannot read arena state {path}: {exc}") from exc
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            backup = path.with_suffix(path.suffix + ".bad")
            try:
                os.replace(path, backup)
            except OSError:
                pass
            return {"version": 1, "platform": "ctfy", "instances": {}}
        if not isinstance(state, dict):
            return {"version": 1, "platform": "ctfy", "instances": {}, "vpn": {}}
        if not isinstance(state.get("instances"), dict):
            state["instances"] = {}
        if not isinstance(state.get("vpn"), dict):
            state["vpn"] = {}
        return state

    def _write_state(self, state: dict) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def _cached_instance(self, code: str) -> dict | None:
        state = self._read_state()
        entry = state["instances"].get(code)
        return entry if isinstance(entry, dict) else None

    def _remember_instance(self, inst: dict) -> None:
        code = str(inst.get("challenge_id") or inst.get("unique_code") or "")
        if not code or not inst.get("id"):
            return
        state = self._read_state()
        entry = dict(state["instances"].get(code) or {})
        for key in ("id", "challenge_id", "competition_id", "status",
                    "started_at", "expires_at", "ttl", "entry_urls",
                    "services", "questions", "vpn_backend", "vpn_endpoint",
                    "network_topology", "description", "name", "error_code"):
            if key in inst:
                entry[key] = inst[key]
        entry["updated_at"] = time.time()
        state["instances"][code] = entry
        state["platform"] = "ctfy"
        self._write_state(state)

    def _forget_instance(self, code: str) -> None:
        state = self._read_state()
        if state["instances"].pop(code, None) is not None:
            self._write_state(state)

    def _mark_instance_stopped(self, code: str, instance_id: str) -> None:
        cached = self._cached_instance(code)
        if cached and str(cached.get("id")) == instance_id:
            cached["status"] = "stopping"
            cached["updated_at"] = time.time()
            state = self._read_state()
            state["instances"][code] = cached
            self._write_state(state)
