"""Armory: a self-scoring weapon library.

A weapon is one technique with several *variants* — the same attack expressed
differently to dodge different defenses. The library keeps score per variant,
so the next engagement picks the one that historically worked.

Storage: ~/.sapper/armory/<id>.json (user-level, survives across engagements).
Seed entries ship in kb/armory/ and are copied over by `sapper armory init`.

Scoring uses Laplace smoothing — (ok + 1) / (tries + 2) — so a variant that
succeeded once is not crowned, and a variant that failed once is not buried.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(
    os.environ.get("SAPPER_ARMORY_ROOT", str(Path.home() / ".sapper" / "armory"))
)
SEED_DIR = Path(__file__).resolve().parent.parent / "kb" / "armory"

STREAK_LIMIT = 3          # consecutive failures before a variant is flagged
MIN_TRIES_FOR_PROMOTION = 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def confidence(ok: int, tries: int) -> float:
    """Laplace-smoothed success rate; 0.5 means no information."""
    return round((ok + 1) / (tries + 2), 3)


def _validate_weapon(data: dict) -> list[str]:
    errors = []
    if not isinstance(data.get("id"), str) or not data["id"]:
        errors.append("missing string 'id'")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        errors.append("missing string 'name'")
    variants = data.get("variants")
    if not isinstance(variants, list) or not variants:
        errors.append("'variants' must be a non-empty list")
    else:
        for i, v in enumerate(variants):
            if not isinstance(v, dict) or not v.get("vid"):
                errors.append(f"variant[{i}] missing 'vid'")
    return errors


class Armory:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_ROOT

    # -- storage -----------------------------------------------------------
    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, wid: str) -> Path:
        return self.root / f"{wid}.json"

    def load(self, wid: str) -> dict:
        path = self.path(wid)
        if not path.exists():
            raise FileNotFoundError(f"未找到武器 {wid}（在 {self.root}）")
        data = json.loads(path.read_text(encoding="utf-8"))
        errors = _validate_weapon(data)
        if errors:
            raise ValueError(f"{wid} 格式错误: {'; '.join(errors)}")
        return data

    def save(self, weapon: dict) -> Path:
        self.ensure()
        weapon["updated_at"] = _now()
        path = self.path(weapon["id"])
        path.write_text(
            json.dumps(weapon, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return path

    def all(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not _validate_weapon(data):
                    out.append(data)
            except Exception:
                continue
        return out

    def next_id(self) -> str:
        used = {w["id"] for w in self.all() if w.get("id")}
        n = 1
        while f"w{n:03d}" in used:
            n += 1
        return f"w{n:03d}"

    # -- creation ----------------------------------------------------------
    def add(
        self,
        name: str,
        family: str = "misc",
        variants: list[dict] | None = None,
        match: dict | None = None,
        evasion: dict | None = None,
        wid: str | None = None,
    ) -> dict:
        weapon = {
            "id": wid or self.next_id(),
            "name": name.strip(),
            "family": family.strip() or "misc",
            "match": match or {},
            "variants": [],
            "evasion": evasion or {},
            "created_at": _now(),
            "updated_at": _now(),
        }
        for v in variants or [{"vid": "v1", "desc": "基础", "command": ""}]:
            weapon["variants"].append(
                {
                    "vid": v.get("vid") or f"v{len(weapon['variants'])+1}",
                    "desc": v.get("desc", ""),
                    "command": v.get("command", ""),
                    "sig": v.get("sig", []),
                    "stats": {"tries": 0, "ok": 0, "blocked": 0, "fail": 0},
                    "streak_fail": 0,
                    "confidence": confidence(0, 0),
                }
            )
        self.save(weapon)
        return weapon

    # -- scoring -----------------------------------------------------------
    def report(self, wid: str, vid: str, result: str) -> dict:
        """result: ok | blocked | fail"""
        if result not in ("ok", "blocked", "fail"):
            raise ValueError(f"result 必须是 ok/blocked/fail，收到 {result!r}")
        weapon = self.load(wid)
        for variant in weapon["variants"]:
            if variant["vid"] != vid:
                continue
            stats = variant.setdefault(
                "stats", {"tries": 0, "ok": 0, "blocked": 0, "fail": 0}
            )
            stats["tries"] = stats.get("tries", 0) + 1
            if result == "ok":
                stats["ok"] = stats.get("ok", 0) + 1
                variant["streak_fail"] = 0
            elif result == "blocked":
                stats["blocked"] = stats.get("blocked", 0) + 1
                variant["streak_fail"] = variant.get("streak_fail", 0) + 1
            else:
                stats["fail"] = stats.get("fail", 0) + 1
                variant["streak_fail"] = variant.get("streak_fail", 0) + 1
            variant["confidence"] = confidence(stats["ok"], stats["tries"])
            if variant.get("streak_fail", 0) >= STREAK_LIMIT:
                variant["deprecated"] = True
            self.save(weapon)
            return weapon
        raise ValueError(f"{wid} 没有变体 {vid}")

    # -- query -------------------------------------------------------------
    def search(self, keyword: str) -> list[dict]:
        kw = keyword.lower()
        hits = []
        for weapon in self.all():
            haystack = " ".join(
                [weapon.get("name", ""), weapon.get("family", "")]
                + [v.get("desc", "") + " " + v.get("command", "") for v in weapon["variants"]]
            ).lower()
            if kw in haystack:
                hits.append(weapon)
        return hits

    def best_variant(self, weapon: dict, *, skip_sig_rich: bool = False) -> dict | None:
        """Highest-confidence variant, preferring low-signature ones on ties."""
        candidates = [v for v in weapon["variants"] if not v.get("deprecated")]
        if not candidates:
            return None
        if skip_sig_rich:
            lean = [v for v in candidates if not v.get("sig")]
            if lean:
                candidates = lean
        return max(candidates, key=lambda v: (v.get("confidence", 0.5), -len(v.get("sig", []))))

    # -- seeding -----------------------------------------------------------
    def seed(self, source: Path | None = None) -> int:
        src = Path(source) if source else SEED_DIR
        if not src.is_dir():
            return 0
        self.ensure()
        copied = 0
        for path in sorted(src.glob("*.json")):
            target = self.root / path.name
            if target.exists():
                continue
            shutil.copy2(path, target)
            copied += 1
        return copied


def summarize(weapon: dict) -> str:
    """One-line description for list output."""
    variants = weapon.get("variants", [])
    best = max(variants, key=lambda v: v.get("confidence", 0.5)) if variants else None
    tries = sum(v.get("stats", {}).get("tries", 0) for v in variants)
    ok = sum(v.get("stats", {}).get("ok", 0) for v in variants)
    conf = best.get("confidence", 0.5) if best else 0.5
    return (
        f"{weapon['id']}  {weapon.get('family', '-'):<18} "
        f"{len(variants)}变体  tries={tries:<3} ok={ok:<3} "
        f"best={best['vid'] if best else '-':<3} conf={conf:.2f}  {weapon['name']}"
    )
