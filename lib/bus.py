"""In-process event bus for sapper.

Synchronous and exception-isolated: one broken subscriber must never break
a command. The core emits; plugins subscribe. Nothing here touches the
filesystem, so it stays safe to import from anywhere.

Debug: set SAPPER_EVENTS=1 to echo every event to stderr.
"""

from __future__ import annotations

import os
import sys
import traceback
from typing import Any, Callable

# Event names. Keep them stable - plugins depend on these strings.
FACT_CREATED = "fact.created"
INTENT_CREATED = "intent.created"
INTENT_CLAIMED = "intent.claimed"
INTENT_DONE = "intent.done"
HINT_ADDED = "hint.added"
PROJECT_INIT = "project.init"
PROJECT_DONE = "project.done"


def _brief(value: Any, limit: int = 48) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "~"


class Bus:
    """Tiny pub/sub. Handlers take keyword arguments."""

    def __init__(self, echo: bool | None = None) -> None:
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._echo = (
            os.environ.get("SAPPER_EVENTS") == "1" if echo is None else bool(echo)
        )

    def on(self, event: str, handler: Callable[..., Any]) -> Callable[..., Any]:
        """Subscribe. Usable as a decorator."""
        self._handlers.setdefault(event, []).append(handler)
        return handler

    def off(self, event: str, handler: Callable[..., Any]) -> bool:
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    def clear(self) -> None:
        self._handlers.clear()

    def handlers_for(self, event: str) -> list[Callable[..., Any]]:
        return list(self._handlers.get(event, ()))

    def emit(self, event: str, **payload: Any) -> int:
        """Run every handler for `event`. Returns how many succeeded."""
        delivered = 0
        for handler in list(self._handlers.get(event, ())):
            try:
                handler(event=event, **payload)
                delivered += 1
            except Exception:  # a subscriber must not take down the CLI
                name = getattr(handler, "__name__", repr(handler))
                print(f"[bus] handler {name} failed on {event}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

        if self._echo:
            detail = " ".join(f"{k}={_brief(v)}" for k, v in payload.items())
            print(f"[bus] {event} {detail}".rstrip(), file=sys.stderr)
        return delivered


_default = Bus()


def bus() -> Bus:
    """Process-wide bus."""
    return _default
