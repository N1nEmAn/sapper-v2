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
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(
    os.environ.get("SAPPER_ARMORY_ROOT", str(Path.home() / ".sapper" / "armory"))
)
SEED_DIR = Path(__file__).resolve().parent.parent / "kb" / "armory"

STREAK_LIMIT = 3          # consecutive failures before a variant is flagged
MIN_TRIES_FOR_PROMOTION = 3
MAX_ATTEMPTS = 400        # ring buffer of raw outcomes, for layer attribution


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def confidence(ok: int, tries: int) -> float:
    """Laplace-smoothed success rate; 0.5 means no information."""
    return round((ok + 1) / (tries + 2), 3)


def _evasion():
    """Lazy import: armory stays usable if the registry file is missing."""
    import importlib.util

    from pathlib import Path as _P

    spec = importlib.util.spec_from_file_location(
        "sapper_evasion", _P(__file__).with_name("evasion.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["sapper_evasion"] = module   # dataclass-free, but keeps module identity
    spec.loader.exec_module(module)
    return module


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
            stack = list(v.get("stack") or [])
            problems = _evasion().validate_stack(stack, strict=False) if stack else []
            hard = [p for p in problems if not p.endswith("（软提示）")]
            if hard:
                raise ValueError(f"变体 {v.get('vid')} 的免杀栈有问题: {'; '.join(hard)}")
            weapon["variants"].append(
                {
                    "vid": v.get("vid") or f"v{len(weapon['variants'])+1}",
                    "desc": v.get("desc", ""),
                    "command": v.get("command", ""),
                    "sig": v.get("sig", []),
                    "stack": stack,
                    "platform": v.get("platform", ""),
                    "stats": {"tries": 0, "ok": 0, "blocked": 0, "fail": 0},
                    "streak_fail": 0,
                    "confidence": confidence(0, 0),
                }
            )
        weapon.setdefault("attempts", [])
        weapon.setdefault("derived", [])
        self.save(weapon)
        return weapon

    # -- scoring -----------------------------------------------------------
    def report(
        self,
        wid: str,
        vid: str,
        result: str,
        *,
        profile: str | list[str] | None = None,
        stack: list[str] | None = None,
        note: str = "",
    ) -> dict:
        """result: ok | blocked | fail

        `profile` is the target's defense fingerprint (e.g. "defender,sandbox-cuckoo").
        It is stored with the attempt so attribution can be scoped later; an
        unscoped comparison across different defenders is meaningless.
        """
        if result not in ("ok", "blocked", "fail"):
            raise ValueError(f"result 必须是 ok/blocked/fail，收到 {result!r}")
        weapon = self.load(wid)
        tags = ([t.strip() for t in profile.split(",") if t.strip()]
                if isinstance(profile, str) else list(profile or []))
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
            # keep a short trail so the console can show the curve, not just a number
            trail = variant.setdefault("history", [])
            trail.append({"ts": _now(), "result": result,
                          "conf": variant["confidence"]})
            variant["history"] = trail[-24:]
            if variant.get("streak_fail", 0) >= STREAK_LIMIT:
                variant["deprecated"] = True

            # raw attempt log — source of truth for layer attribution
            attempts = weapon.setdefault("attempts", [])
            attempts.append({
                "ts": _now(),
                "vid": vid,
                "stack": list(stack if stack is not None else variant.get("stack") or []),
                "profile": tags,
                "result": result,
                "note": note[:200],
            })
            weapon["attempts"] = attempts[-MAX_ATTEMPTS:]
            self.save(weapon)
            return weapon
        raise ValueError(f"{wid} 没有变体 {vid}")

    # -- evasion -----------------------------------------------------------
    def attribute(self, wid: str, *, profile: str | list[str] | None = None) -> list[dict]:
        """Layer lift. Scope by profile — comparing across defenders is noise."""
        weapon = self.load(wid)
        attempts = weapon.get("attempts") or []
        tags = ({t.strip().lower() for t in profile.split(",") if t.strip()}
                if isinstance(profile, str) else
                {str(t).lower() for t in (profile or [])})
        if tags:
            scoped = [a for a in attempts
                      if tags <= {str(p).lower() for p in (a.get("profile") or [])}]
            attempts = scoped or attempts   # fall back rather than show nothing
        return _evasion().attribute(attempts)

    def derive(self, wid: str, vid: str, *, profile: str | list[str] | None = None,
               limit: int = 3) -> dict:
        """Propose the next variant by extending a blocked stack."""
        weapon = self.load(wid)
        variant = next((v for v in weapon["variants"] if v["vid"] == vid), None)
        if variant is None:
            raise ValueError(f"{wid} 没有变体 {vid}")
        tags = ([t.strip() for t in profile.split(",") if t.strip()]
                if isinstance(profile, str) else list(profile or []))
        attempts = [a for a in (weapon.get("attempts") or []) if a.get("vid") == vid]
        if len(attempts) < 2:
            attempts = weapon.get("attempts") or []
        ev = _evasion()
        candidates = ev.derive_candidates(
            variant.get("stack") or [], attempts, tags, limit=limit,
            scope=ev.scope_of(weapon.get("family", "")),
        )
        return {"weapon": weapon, "variant": variant, "candidates": candidates,
                "profile": tags}

    def apply_derived(
        self,
        wid: str,
        vid: str,
        layers: list[str],
        *,
        profile: str | list[str] | None = None,
        desc: str = "",
    ) -> dict:
        """Create a new variant = source stack + `layers`. Never mutates the source."""
        if not layers:
            raise ValueError("至少要加一层")
        weapon = self.load(wid)
        base = next((v for v in weapon["variants"] if v["vid"] == vid), None)
        if base is None:
            raise ValueError(f"{wid} 没有变体 {vid}")
        new_stack = list(base.get("stack") or []) + list(layers)
        ev = _evasion()
        problems = ev.validate_stack(new_stack)
        if problems:
            raise ValueError("免杀栈不成立: " + "; ".join(problems))

        used = {v["vid"] for v in weapon["variants"]}
        n = 1
        while f"v{n}" in used:
            n += 1
        new_vid = f"v{n}"
        tags = ([t.strip() for t in profile.split(",") if t.strip()]
                if isinstance(profile, str) else list(profile or []))
        weapon["variants"].append({
            "vid": new_vid,
            "desc": desc or f"{base.get('desc', '')} + "
                            f"{'/'.join(c['name'] if isinstance(c, dict) else c for c in layers)}",
            "command": base.get("command", ""),
            "sig": base.get("sig", []),
            "stack": new_stack,
            "platform": base.get("platform", ""),
            "stats": {"tries": 0, "ok": 0, "blocked": 0, "fail": 0},
            "streak_fail": 0,
            "confidence": confidence(0, 0),
            "derived_from": vid,
        })
        weapon.setdefault("derived", []).append({
            "vid": new_vid,
            "from": vid,
            "added": list(layers),
            "profile": tags,
            "ts": _now(),
        })
        self.save(weapon)
        return {"weapon": weapon, "variant": weapon["variants"][-1]}

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

    def best_variant(self, weapon: dict, *, skip_sig_rich: bool = False,
                     profile: list[str] | None = None) -> dict | None:
        """Best variant for a target profile.

        Ranking is `estimate()` — measured confidence blended with a structural
        prior — then stealth, then cost. A naked variant is no longer allowed to
        win by being cheap: before any evidence it starts at the prior floor.
        """
        ev = _evasion()
        candidates = [v for v in weapon["variants"] if not v.get("deprecated")]
        if not candidates:
            return None
        if skip_sig_rich:
            lean = [v for v in candidates if not v.get("sig")]
            if lean:
                candidates = lean

        def key(v: dict):
            return (
                ev.estimate(v, profile),
                -ev.signature_score(v.get("stack")),
                -ev.cost(v.get("stack")),
            )

        return max(candidates, key=key)

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


def recommend(
    weapons: list[dict],
    profile: list[str] | None = None,
    *,
    family: str = "",
    keyword: str = "",
    limit: int = 5,
) -> list[dict]:
    """Answer "哪个武器现在能用" for a given target profile.

    Returns ranked picks with the *reason* attached, because an operator who
    cannot see why a weapon was chosen will not trust it the second time.
    """
    ev = _evasion()
    tags = [t.strip() for t in (profile or []) if t.strip()]
    picks: list[dict] = []
    for weapon in weapons:
        if family and family.lower() not in (weapon.get("family") or "").lower():
            continue
        if keyword:
            kw = keyword.lower()
            hay = " ".join([weapon.get("name", ""), weapon.get("family", "")] +
                           [v.get("desc", "") for v in weapon.get("variants", [])]).lower()
            if kw not in hay:
                continue
        best = None
        best_key = None
        for v in weapon.get("variants", []):
            if v.get("deprecated"):
                continue
            conflicts = ev.detect_conflicts(v.get("stack"), tags) if tags else []
            key = (ev.estimate(v, tags),
                   -ev.signature_score(v.get("stack")),
                   -ev.cost(v.get("stack")))
            if best_key is None or key > best_key:
                best, best_key = (v, conflicts), key
        if not best:
            continue
        variant, conflicts = best
        score = ev.estimate(variant, tags)
        info = ev.analyse(variant.get("stack"), tags) if tags else {
            "effective": [], "saturated": [], "caught": []}
        reasons = []
        if info["effective"]:
            reasons.append("生效层: " + ", ".join(r["name"] for r in info["effective"][:2]))
        if info["saturated"]:
            reasons.append("已饱和(旧版能过/新版可检出): "
                           + ", ".join(r["name"] for r in info["saturated"][:2]))
        if info["caught"]:
            reasons.append("会被抓: " + ", ".join(r["name"] for r in info["caught"][:2]))
        tries = (variant.get("stats") or {}).get("tries", 0)
        if tries >= 2:
            reasons.append(f"实战 {tries} 次，置信度 {variant['confidence']:.2f}")
        else:
            reasons.append(f"无充分实战数据，结构先验 {score:.2f}")
        picks.append({
            "weapon": weapon["id"],
            "name": weapon.get("name", ""),
            "family": weapon.get("family", ""),
            "variant": variant["vid"],
            "confidence": variant.get("confidence", 0.5),
            "estimate": score,
            "stack": variant.get("stack") or [],
            "signature_score": ev.signature_score(variant.get("stack")),
            "cost": ev.cost(variant.get("stack")),
            "conflicts": conflicts,
            "effective": [r["layer"] for r in info["effective"]],
            "saturated": [r["layer"] for r in info["saturated"]],
            "caught": [r["layer"] for r in info["caught"]],
            "reasons": reasons,
            "command": variant.get("command", ""),
        })

    picks.sort(key=lambda p: (p["estimate"], -p["signature_score"], -p["cost"]),
               reverse=True)
    return picks[:limit]


def summarize(weapon: dict) -> str:
    """One-line description for list output."""
    ev = _evasion()
    variants = weapon.get("variants", [])
    best = max(variants, key=lambda v: v.get("confidence", 0.5)) if variants else None
    tries = sum(v.get("stats", {}).get("tries", 0) for v in variants)
    ok = sum(v.get("stats", {}).get("ok", 0) for v in variants)
    conf = best.get("confidence", 0.5) if best else 0.5
    stack_n = len(best.get("stack") or []) if best else 0
    return (
        f"{weapon['id']}  {weapon.get('family', '-'):<18} "
        f"{len(variants)}变体 tries={tries:<3} ok={ok:<3} "
        f"best={best['vid'] if best else '-':<3} conf={conf:.2f} "
        f"stack={stack_n}  {weapon['name']}"
    )
