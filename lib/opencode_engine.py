#!/usr/bin/env python3
"""OpenCode engine for Sapper (Phase 2).

This adapter is deliberately independent from :mod:`engines.ClaudeEngine`:
it does not import ``sapper``, never emits Claude-only flags, and builds its
own Claude-compatible envelope from ``opencode run --format json`` NDJSON
events.

No new dependencies: standard library only.  A runner and a clock can be
injected so every test is fully offline and deterministic.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:  # script / tests: lib/ is on sys.path
    from engines import EngineRequest, EngineResult
except ImportError:  # package-style import (python -m lib.sapper)
    from .engines import EngineRequest, EngineResult

__all__ = ["OpenCodeEngine"]

_TRUSTED_OPEN = "<<TRUSTED SYSTEM INSTRUCTIONS>>"
_TRUSTED_CLOSE = "<<END TRUSTED SYSTEM INSTRUCTIONS>>"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


class OpenCodeEngine:
    """Run ``opencode run --format json`` and normalise its event stream.

    Observable behaviour:

    * the prompt is a positional argument (the CLI documents ``message...``),
      never stdin;
    * no permission-bypass flag is used (neither ``--auto`` nor the Claude
      flag); a pending ``permission.asked`` fails fast;
    * events from child sessions are ignored; final text comes from
      ``session.text.ended``, with ``session.text.delta`` only as a fallback;
    * timeout terminates and then kills the child process;
    * the returned envelope is ``{"result": str, "sessionID": ...}``.
    """

    name = "opencode"

    def __init__(
        self,
        *,
        error_cls: type[Exception],
        popen: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._error_cls = error_cls
        self._popen = popen if popen is not None else subprocess.Popen
        self._clock = clock if clock is not None else time.monotonic

    # ------------------------------------------------------------------ #
    # command construction
    # ------------------------------------------------------------------ #
    def _argv(self, request: EngineRequest) -> tuple[list[str], str]:
        prefix = os.environ.get("SAPPER_OPENCODE_BIN", "opencode").split()
        if not prefix:
            raise self._error_cls("SAPPER_OPENCODE_BIN cannot be empty")
        engine = prefix[0]
        argv = prefix + ["run", "--format", "json"]

        # Each engine owns its model configuration.  No Claude->OpenCode
        # equivalence is inferred from request.model / SAPPER_MODEL.
        model = os.environ.get("SAPPER_OPENCODE_MODEL", "").strip()
        if model:
            argv += ["--model", model]
        agent = os.environ.get("SAPPER_OPENCODE_AGENT", "").strip()
        if agent:
            argv += ["--agent", agent]
        session = (
            os.environ.get("SAPPER_OPENCODE_SESSION", "").strip()
            or request.session_id
        )
        if session:
            argv += ["--session", session]
        if _env_flag("SAPPER_OPENCODE_ARCHIVED"):
            argv.append("--archived")
        if _env_flag("SAPPER_OPENCODE_STANDALONE"):
            argv.append("--standalone")

        prompt = request.prompt
        if request.append_system:
            # Documented limitation: OpenCode CLI has no system-prompt flag.
            prompt = (
                f"{_TRUSTED_OPEN}\n{request.append_system}\n{_TRUSTED_CLOSE}\n\n"
                f"{prompt}"
            )
        if request.skip_permissions:
            sys.stderr.write(
                "[sapper] OpenCode ignores --skip-permissions: no auto-approve "
                "or permission-bypass flag is used\n"
            )
        # A leading '-' would be parsed as a flag by most CLIs; a leading
        # newline keeps the text identical in meaning without relying on '--'.
        if prompt.startswith("-"):
            prompt = "\n" + prompt
        argv.append(prompt)
        return argv, engine

    # ------------------------------------------------------------------ #
    # process handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def _kill(proc: Any) -> None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _collect(self, request: EngineRequest, argv: list[str], engine: str):
        try:
            proc = self._popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(request.cwd),
                bufsize=1,
            )
        except FileNotFoundError:
            raise self._error_cls(
                f"`{engine}` CLI not found on PATH; install it or fix SAPPER_OPENCODE_BIN"
            )

        out_lines: list[str] = []
        err_lines: list[str] = []

        def read(stream: Any, sink: list[str]) -> None:
            try:
                for line in stream:
                    sink.append(line)
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        reader_out = threading.Thread(
            target=read, args=(proc.stdout, out_lines), daemon=True
        )
        reader_err = threading.Thread(
            target=read, args=(proc.stderr, err_lines), daemon=True
        )
        reader_out.start()
        reader_err.start()

        deadline = self._clock() + request.timeout
        timed_out = False
        try:
            while True:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    proc.wait(timeout=remaining)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if timed_out:
                self._kill(proc)
            reader_out.join(timeout=5)
            reader_err.join(timeout=5)

        if timed_out:
            raise self._error_cls(f"{engine} dispatch timed out after {request.timeout}s")
        if proc.returncode != 0:
            detail = "".join(err_lines).strip()[:500]
            raise self._error_cls(f"{engine} exited {proc.returncode}: {detail}")
        return out_lines, "".join(err_lines)

    # ------------------------------------------------------------------ #
    # NDJSON normalisation
    # ------------------------------------------------------------------ #
    def _parse_lines(self, lines: list[str], engine: str) -> list[dict]:
        events: list[dict] = []
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                raise self._error_cls(
                    f"{engine} did not return JSON. stdout[:300]={line[:300]!r}"
                )
            if not isinstance(event, dict):
                raise self._error_cls(
                    f"{engine} returned a JSON value instead of an object envelope"
                )
            events.append(event)
        return events

    def _normalize(self, events: list[dict], engine: str) -> EngineResult:
        main_sid: str | None = None
        order: list[str] = []
        ended: dict[str, str] = {}
        deltas: dict[str, str] = {}
        opencode_text: dict[str, str] = {}
        pending_permissions: set[str] = set()
        failure: str | None = None
        interruption: str | None = None

        for event in events:
            data = event.get("data")
            if not isinstance(data, dict):
                data = {}

            sid = (
                event.get("sessionID")
                or event.get("session_id")
                or data.get("sessionID")
                or data.get("session_id")
            )
            if isinstance(sid, str) and sid:
                if main_sid is None:
                    main_sid = sid
                elif sid != main_sid:
                    # Sub-session output is not part of the Sapper answer.
                    continue

            typ = event.get("type")
            if typ == "error":
                err = event.get("error")
                message = err.get("message") if isinstance(err, dict) else str(err)
                failure = f"{engine} reported error: {str(message)[:300]}"
            elif typ in ("session.execution.failed", "session.step.failed"):
                detail = data.get("error") or event.get("error")
                failure = f"{engine} execution failed: {str(detail)[:300]}"
            elif typ == "session.execution.interrupted":
                interruption = (
                    f"{engine} dispatch interrupted: {str(data.get('reason', ''))[:200]}"
                )
            elif typ == "permission.asked":
                pid = str(
                    event.get("permissionID")
                    or data.get("permissionID")
                    or data.get("id")
                    or len(pending_permissions)
                )
                pending_permissions.add(pid)
            elif typ == "permission.replied":
                pid = str(
                    event.get("permissionID")
                    or data.get("permissionID")
                    or data.get("id")
                )
                pending_permissions.discard(pid)
            elif typ == "text":
                part = event.get("part")
                if isinstance(part, dict) and part.get("type") == "text":
                    mid = str(
                        event.get("messageID")
                        or part.get("messageID")
                        or "default"
                    )
                    if mid not in order:
                        order.append(mid)
                    text = part.get("text", "")
                    if isinstance(text, str):
                        opencode_text[mid] = opencode_text.get(mid, "") + text
            elif typ in ("session.text.ended", "text.ended"):
                mid = str(data.get("assistantMessageID", "default"))
                if mid not in order:
                    order.append(mid)
                text = data.get("text", "")
                if isinstance(text, str):
                    ended[mid] = text
            elif typ in ("session.text.delta", "text.delta"):
                mid = str(data.get("assistantMessageID", "default"))
                if mid not in order:
                    order.append(mid)
                delta = data.get("delta", "")
                if isinstance(delta, str):
                    deltas[mid] = deltas.get(mid, "") + delta
            # session.created / session.idle / session.execution.succeeded and
            # tool/reasoning events carry no final answer: intentionally ignored.

        if failure:
            raise self._error_cls(failure)
        if interruption:
            raise self._error_cls(interruption)
        if pending_permissions:
            raise self._error_cls(
                f"{engine} requires permission approval; configure permissions "
                "in opencode.json (no auto-approve or bypass flag is used)"
            )

        parts: list[str] = []
        for mid in order:
            text = ended.get(mid)
            if text is None:
                text = opencode_text.get(mid)
            if text is None:
                text = deltas.get(mid, "")
            if text:
                parts.append(text)
        result = "\n".join(parts)

        envelope: dict[str, Any] = {"result": result}
        if main_sid:
            envelope["sessionID"] = main_sid
        return EngineResult(result=result, envelope=envelope, session_id=main_sid)

    # ------------------------------------------------------------------ #
    # Engine interface
    # ------------------------------------------------------------------ #
    def run(self, request: EngineRequest) -> EngineResult:
        if request.timeout < 1:
            raise self._error_cls("opencode timeout must be at least 1 second")
        argv, engine = self._argv(request)
        out_lines, _stderr = self._collect(request, argv, engine)
        result = self._normalize(self._parse_lines(out_lines, engine), engine)

        if not result.result.strip():
            raise self._error_cls(
                "opencode returned no assistant text; refusing empty response"
            )

        return result
