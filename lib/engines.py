#!/usr/bin/env python3
"""Common engine contract for Sapper.

:class:`ClaudeEngine` is the behaviour-preserving extraction of the Claude
dispatch that used to live inline in ``sapper.run_engine()``.  :func:`select_engine`
chooses between it and the independent :class:`OpenCodeEngine` via
``SAPPER_ENGINE`` (default: ``claude``).

No new dependencies: standard library only.  Nothing in this module imports
``sapper``; the legacy helpers are injected by ``sapper.run_engine()`` so that
existing ``mock.patch("sapper.*")`` hooks keep working.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

__all__ = [
    "Engine",
    "EngineRequest",
    "EngineResult",
    "ClaudeEngine",
    "select_engine",
]


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EngineRequest:
    """One engine invocation, expressed in Sapper's own vocabulary."""

    prompt: str
    model: str
    timeout: int
    skip_permissions: bool
    append_system: str | None
    cwd: Path
    # Reserved for future engines (OpenCode session continuation).  Ignored by
    # ClaudeEngine so its argv stays byte-for-byte identical to the legacy path.
    session_id: str | None = None


@dataclass(frozen=True)
class EngineResult:
    """Normalised result plus the untouched provider envelope.

    ``envelope`` is the exact dict returned by the provider runner.  Callers
    that only read ``["result"]`` keep working, and callers that inspect extra
    provider fields (cost, duration, session id, ...) lose nothing.
    """

    result: str
    envelope: dict
    session_id: str | None = None

    def to_envelope(self) -> dict:
        """Return the original provider envelope (identity is preserved)."""
        return self.envelope


class Engine(Protocol):
    name: str

    def run(self, request: EngineRequest) -> EngineResult:
        ...


# --------------------------------------------------------------------------- #
# Claude engine (legacy behaviour, extracted verbatim)
# --------------------------------------------------------------------------- #
class ClaudeEngine:
    """The historical ``claude -p --output-format json`` dispatcher.

    Every observable detail is preserved: validation messages, argv order,
    stdin/stdout handling, quota rotation, retry count and backoff formula,
    stderr messages and the FGError instance that is finally re-raised.
    """

    name = "claude"

    def __init__(
        self,
        *,
        run_once: Callable[[list[str], str, int, Path, str], dict],
        is_quota_error: Callable[[str], bool],
        rotate_key: Callable[[], bool],
        error_cls: type[Exception],
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._run_once = run_once
        self._is_quota_error = is_quota_error
        self._rotate_key = rotate_key
        self._error_cls = error_cls
        # Injected so mock.patch("sapper.time.sleep") keeps taking effect.
        self._sleep = sleep if sleep is not None else time.sleep

    def run(self, request: EngineRequest) -> EngineResult:
        if request.timeout < 1:
            raise self._error_cls("claude timeout must be at least 1 second")
        if not isinstance(request.model, str) or not request.model.strip():
            raise self._error_cls("claude model cannot be empty")

        prefix = os.environ.get("SAPPER_BIN", "claude").split()
        argv = prefix + [
            "-p",
            "--output-format", "json",
            "--model", request.model,
        ]
        if request.skip_permissions:
            # Legacy Claude flag.  It is intentionally kept here unchanged:
            # no other engine may ever add it.  Do not reuse in new engines.
            argv.append("--dangerously-skip-permissions")
        if request.append_system:
            argv += ["--append-system-prompt", request.append_system]
        engine = prefix[0]

        attempts = 1 + max(0, int(os.environ.get("SAPPER_RETRY_MODEL", "0")))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                envelope = self._run_once(
                    argv, request.prompt, request.timeout, request.cwd, engine,
                )
                # The legacy runner already validates these, but an injected
                # runner may not.  Validate inside the retry block so errors
                # keep the same retry/rotation semantics and the same messages.
                if not isinstance(envelope, dict):
                    raise self._error_cls(
                        f"{engine} returned a JSON value instead of an object envelope"
                    )
                raw_result = envelope.get("result", "")
                if not isinstance(raw_result, str):
                    raise self._error_cls("response envelope has a non-string result")
            except self._error_cls as exc:
                last_error = exc
                if "CLI not found" in str(exc):
                    raise  # not transient
                if self._is_quota_error(str(exc)) and self._rotate_key():
                    sys.stderr.write(
                        f"[sapper] quota/auth error (attempt {attempt}/{attempts}); "
                        "key rotated — retrying immediately\n"
                    )
                    continue
                if attempt < attempts:
                    backoff = min(120, 10 * (2 ** (attempt - 1)))
                    sys.stderr.write(
                        f"[sapper] engine call failed (attempt {attempt}/{attempts}): "
                        f"{exc}; retrying in {backoff}s\n"
                    )
                    self._sleep(backoff)
            else:
                raw_sid = envelope.get("session_id") or envelope.get("sessionID")
                session_id = raw_sid if isinstance(raw_sid, str) else None
                return EngineResult(
                    result=raw_result,
                    envelope=envelope,
                    session_id=session_id,
                )

        assert last_error is not None
        raise last_error


# --------------------------------------------------------------------------- #
# engine selection
# --------------------------------------------------------------------------- #
def select_engine(
    *,
    run_once: Callable[[list[str], str, int, Path, str], dict],
    is_quota_error: Callable[[str], bool],
    rotate_key: Callable[[], bool],
    error_cls: type[Exception],
    sleep: Callable[[float], None] | None = None,
):
    """Return the engine configured by ``SAPPER_ENGINE``.

    The default is Claude with exactly the same injected helpers as the
    original ``run_engine()`` body, so the legacy path is unchanged.
    """
    name = os.environ.get("SAPPER_ENGINE", "claude").strip().lower() or "claude"
    if name == "claude":
        return ClaudeEngine(
            run_once=run_once,
            is_quota_error=is_quota_error,
            rotate_key=rotate_key,
            error_cls=error_cls,
            sleep=sleep,
        )
    if name == "opencode":
        try:  # imported lazily: keeps ClaudeEngine importable on its own
            from opencode_engine import OpenCodeEngine
        except ImportError:
            from .opencode_engine import OpenCodeEngine
        return OpenCodeEngine(error_cls=error_cls)
    raise error_cls(
        f"unknown SAPPER_ENGINE {name!r}; expected 'claude' or 'opencode'"
    )
