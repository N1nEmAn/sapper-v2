"""mitmproxy addon: capture HTTP traffic into a sapper engagement.

    mitmdump -p 8080 -s tools/mitm_sapper.py --set sapper_store=D:/work/target

`sapper_store` accepts either the project directory or its `.sapper` folder.
Filtering, truncation and storage all live in lib/traffic.py so the addon and
`sapper traffic import` share exactly one set of rules.

Nothing here may raise: a proxy that dies because of its logger is worse than
no logger at all.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from mitmproxy import ctx


def _load_traffic_module():
    here = Path(__file__).resolve()
    for candidate in (here.parent.parent / "lib" / "traffic.py",
                      here.parent / "lib" / "traffic.py"):
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("sapper_traffic", candidate)
            module = importlib.util.module_from_spec(spec)
            sys.modules["sapper_traffic"] = module
            spec.loader.exec_module(module)
            return module
    raise RuntimeError(
        "lib/traffic.py not found — run this addon from the sapper checkout, "
        "or pass --set sapper_store=<project>/.sapper"
    )


class SapperTraffic:
    def __init__(self) -> None:
        self.mod = _load_traffic_module()
        self.store = None
        self.kept = 0
        self.dropped = 0

    def load(self, loader) -> None:
        loader.add_option(
            "sapper_store", str, "",
            "project dir (or its .sapper dir) that receives traffic.db",
        )

    def _resolve_store(self):
        if self.store is not None:
            return self.store
        raw = (ctx.options.sapper_store or "").strip()
        if not raw:
            raise RuntimeError("--set sapper_store=<project dir> is required")
        path = Path(raw).expanduser()
        root = path if path.name == ".sapper" else path / ".sapper"
        if not root.is_dir():
            raise RuntimeError(f"{root} does not exist (run `sapper setup` first)")
        self.store = self.mod.TrafficStore(root)
        return self.store

    def response(self, flow) -> None:
        try:
            store = self._resolve_store()
            record = self.mod.record_from_flow(flow)
            if store.add(record):
                self.kept += 1
            else:
                self.dropped += 1
        except Exception as exc:  # never break the proxy
            ctx.log.warn(f"[sapper] traffic capture skipped: {exc}")

    def done(self) -> None:
        if self.kept or self.dropped:
            ctx.log.info(
                f"[sapper] traffic: kept {self.kept}, filtered {self.dropped}"
            )


addons = [SapperTraffic()]
