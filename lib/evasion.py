"""Evasion layers: the "how does the payload get past detection" axis.

A weapon answers *what* to do; a variant answers *how* to express it. This
module adds the third axis that was missing: which evasion layers are stacked
on the payload, and — from real outcomes — which of those layers actually buys
anything against a given defender.

Two things here are deliberately conservative:

* **Attribution needs a control group.** A layer's lift is only reported when
  attempts exist both with and without it. Otherwise it is marked 无对照 —
  a layer that was always present cannot be distinguished from the weapon
  itself, and pretending otherwise is how "免杀经验" becomes superstition.
* **Conflicts are hard errors, not warnings.** Stacking UPX on top of OLLVM
  control-flow flattening is not "slightly worse", it is a sample that tells
  you nothing because two layers fight each other.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REGISTRY_PATH = Path(
    os.environ.get(
        "SAPPER_EVASION_REGISTRY",
        str(Path(__file__).resolve().parent.parent / "kb" / "evasion" / "layers.json"),
    )
)

# How much a layer's static signature counts when ranking variants.
RISK_WEIGHT = {0: 0.0, 1: 0.6, 2: 1.2, 3: 2.0}


class EvasionError(ValueError):
    """Bad layer id, conflicting stack, unmet prerequisite."""


_registry: dict | None = None


def registry() -> dict:
    global _registry
    if _registry is None:
        if not REGISTRY_PATH.is_file():
            raise EvasionError(f"免杀层注册表不存在: {REGISTRY_PATH}")
        _registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return _registry


def reload_registry() -> dict:
    global _registry
    _registry = None
    return registry()


def layers() -> dict[str, dict]:
    return {layer["id"]: layer for layer in registry()["layers"]}


def categories() -> dict[str, str]:
    return dict(registry().get("categories") or {})


def profiles() -> dict[str, str]:
    return {k: v for k, v in (registry().get("profiles") or {}).items()
            if not k.startswith("_")}


def by_category() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {c: [] for c in categories()}
    for layer in registry()["layers"]:
        out.setdefault(layer["cat"], []).append(layer)
    return out


def get(lid: str) -> dict:
    found = layers().get(lid)
    if found is None:
        raise EvasionError(f"未知免杀层 {lid!r}（用 `sapper evasion layers` 查看）")
    return found


def resolve(ids: list[str] | None) -> list[dict]:
    return [get(i) for i in (ids or [])]


# --------------------------------------------------------------------------- #
# stack validation
# --------------------------------------------------------------------------- #
def validate_stack(ids: list[str], *, strict: bool = True) -> list[str]:
    """Return human-readable problems. strict=True also enforces prereqs."""
    problems: list[str] = []
    known = layers()
    seen: set[str] = set()
    for lid in ids:
        if lid not in known:
            problems.append(f"未知层: {lid}")
            continue
        if lid in seen:
            problems.append(f"重复层: {lid}")
        seen.add(lid)

    for lid in seen:
        layer = known.get(lid)
        if not layer:
            continue
        for need in layer.get("needs") or []:
            if need not in seen:
                msg = f"{lid} 需要前置层 {need}"
                problems.append(msg if strict else f"{msg}（软提示）")
        for bad in layer.get("conflicts") or []:
            if bad in seen:
                pair = " / ".join(sorted([lid, bad]))
                problems.append(f"互斥: {pair}")

    # de-dup symmetric conflict pairs reported twice
    deduped: list[str] = []
    for p in problems:
        if p not in deduped:
            deduped.append(p)
    return deduped


def signature_score(ids: list[str] | None) -> float:
    """Lower is stealthier. Counts inherent risk, not number of layers."""
    total = 0.0
    for layer in resolve(ids):
        total += RISK_WEIGHT.get(int(layer.get("risk", 1)), 1.0)
    return round(total, 2)


def cost(ids: list[str] | None) -> int:
    return sum(int(layer.get("cost", 1)) for layer in resolve(ids))


def summarize_stack(ids: list[str] | None) -> str:
    if not ids:
        return "(裸)"
    cats = categories()
    groups: dict[str, list[str]] = {}
    for layer in resolve(ids):
        groups.setdefault(cats.get(layer["cat"], layer["cat"]), []).append(
            layer["name"]
        )
    return " | ".join(f"{c}:{'+'.join(v)}" for c, v in groups.items())


# --------------------------------------------------------------------------- #
# target profile matching
# --------------------------------------------------------------------------- #
def layer_effect(layer: dict, tags: set[str]) -> dict:
    """Split a layer's effect against this target into three buckets.

    A layer listed in both `beats` and `detect` for the same tag is *saturated*:
    it still defeats older builds of that product but the current one has the
    signature. That is the single most useful state to surface, because it is
    exactly where operators waste time ("it used to work").
    """
    beats = {b.lower() for b in (layer.get("beats") or [])}
    caught = {d.lower() for d in (layer.get("detect") or [])}
    return {
        "beats": sorted(beats & tags),
        "detect": sorted(caught & tags),
        "saturated": sorted(beats & caught & tags),
    }


def analyse(ids: list[str] | None, profile: list[str]) -> dict:
    """Per-layer effect plus roll-ups, for both ranking and display."""
    tags = {p.strip().lower() for p in profile if p and p.strip()}
    rows = []
    for layer in resolve(ids):
        eff = layer_effect(layer, tags)
        if not any(eff.values()):
            eff["state"] = "neutral"
        elif eff["saturated"]:
            eff["state"] = "saturated"
        elif eff["detect"] and not eff["beats"]:
            eff["state"] = "caught"
        elif eff["beats"] and not eff["detect"]:
            eff["state"] = "effective"
        else:
            eff["state"] = "mixed"
        eff["layer"] = layer["id"]
        eff["name"] = layer["name"]
        eff["cat"] = layer["cat"]
        rows.append(eff)
    return {
        "rows": rows,
        "effective": [r for r in rows if r["state"] == "effective"],
        "saturated": [r for r in rows if r["state"] == "saturated"],
        "caught": [r for r in rows if r["state"] == "caught"],
    }


def detect_conflicts(ids: list[str] | None, profile: list[str]) -> list[str]:
    """Which layers this target's defenses can still catch."""
    tags = {p.strip().lower() for p in profile if p and p.strip()}
    hits: list[str] = []
    for layer in resolve(ids):
        caught = {d.lower() for d in (layer.get("detect") or [])}
        common = caught & tags
        if common:
            hits.append(f"{layer['name']}({layer['id']}) ← {','.join(sorted(common))}")
    return hits


def matched_beats(ids: list[str] | None, profile: list[str]) -> list[str]:
    """Layers that are effective *and not yet known* to this target's defenses."""
    tags = {p.strip().lower() for p in profile if p and p.strip()}
    hits: list[str] = []
    for layer in resolve(ids):
        eff = layer_effect(layer, tags)
        clean = [t for t in eff["beats"] if t not in eff["saturated"]]
        if clean:
            hits.append(f"{layer['name']}({layer['id']}) → {','.join(clean)}")
    return hits


def saturated_layers(ids: list[str] | None, profile: list[str]) -> list[str]:
    tags = {p.strip().lower() for p in profile if p and p.strip()}
    out: list[str] = []
    for layer in resolve(ids):
        eff = layer_effect(layer, tags)
        if eff["saturated"]:
            out.append(f"{layer['name']}({layer['id']}) → 已被 {','.join(eff['saturated'])} 收录")
    return out


def coverage(ids: list[str] | None) -> dict[str, int]:
    """How many layers per category — shows blind spots at a glance."""
    cats = categories()
    out = {c: 0 for c in cats}
    for layer in resolve(ids):
        out[layer["cat"]] = out.get(layer["cat"], 0) + 1
    return out


# --------------------------------------------------------------------------- #
# attribution — the part that turns logs into knowledge
# --------------------------------------------------------------------------- #
def _rate(attempts: list[dict]) -> tuple[float, int]:
    """Laplace-smoothed success rate over attempts."""
    n = len(attempts)
    ok = sum(1 for a in attempts if a.get("result") == "ok")
    return (ok + 1) / (n + 2), n


def attribute(attempts: list[dict], *, min_control: int = 1) -> list[dict]:
    """Per-layer lift from a weapon's attempt log.

    lift = P(ok | layer present) - P(ok | layer absent), both smoothed.
    `control` is the sample size on the absent side; when it is 0 the layer is
    reported with `attributable: False` — we have no idea whether it helped,
    only that it was there.

    Layers that always appear together (same presence pattern across every
    attempt) are marked `collinear`. Three anti-sandbox layers added in one go
    will each show the full lift, and it is a lie to report them as three
    independent wins: you cannot tell which one did the work, or whether any
    single one alone would have.
    """
    rows: list[dict] = []
    if not attempts:
        return rows

    all_layers: set[str] = set()
    for a in attempts:
        all_layers.update(a.get("stack") or [])

    # presence signature: which attempts (by index) contain this layer
    pattern: dict[str, frozenset[int]] = {}
    for lid in all_layers:
        pattern[lid] = frozenset(
            i for i, a in enumerate(attempts) if lid in (a.get("stack") or [])
        )
    grouped: dict[frozenset[int], list[str]] = {}
    for lid, sig in pattern.items():
        grouped.setdefault(sig, []).append(lid)

    for lid in sorted(all_layers):
        with_it = [a for a in attempts if lid in (a.get("stack") or [])]
        without = [a for a in attempts if lid not in (a.get("stack") or [])]
        r_with, n_with = _rate(with_it)
        r_without, n_without = _rate(without)
        attributable = n_without >= min_control
        twins = [x for x in grouped.get(pattern[lid], []) if x != lid]
        rows.append({
            "layer": lid,
            "with": n_with,
            "without": n_without,
            "rate_with": round(r_with, 3),
            "rate_without": round(r_without, 3),
            "lift": round(r_with - r_without, 3) if attributable else None,
            "attributable": attributable,
            "collinear_with": twins,
        })

    # biggest lift first; unattributable rows last
    rows.sort(key=lambda r: (r["attributable"], r["lift"] if r["lift"] is not None else 0),
              reverse=True)
    return rows


def render_attribution(rows: list[dict]) -> str:
    if not rows:
        return "(还没有足够记录做归因：至少跑两个不同栈的同一把武器)"
    cat = categories()
    lines = []
    for r in rows:
        layer = layers().get(r["layer"], {})
        name = f"{cat.get(layer.get('cat', ''), '')}/{layer.get('name', r['layer'])}"
        if not r["attributable"]:
            lines.append(f"  {name:<28} 出现{r['with']:>2}次 无对照 → 归因不可信")
            continue
        sign = "+" if (r["lift"] or 0) > 0 else ""
        note = ""
        if r.get("collinear_with"):
            note = f"  ⚠ 与 {len(r['collinear_with'])} 层共线，无法区分"
        lines.append(
            f"  {name:<28} 有{r['with']:>2}/无{r['without']:>2}  "
            f"{r['rate_with']:.2f} vs {r['rate_without']:.2f}  "
            f"lift={sign}{r['lift']:.2f}{note}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# derive — propose the next variant instead of waiting for a human
# --------------------------------------------------------------------------- #
def scope_of(family: str) -> str:
    """payload weapons deliver code; everything else rides HTTP."""
    fam = (family or "").lower()
    return "payload" if fam.startswith("payload") else "traffic"


def _bundle_for(lid: str, current: list[str]) -> list[str] | None:
    """lid plus whatever prerequisites it still lacks.

    Returns None when a prerequisite cannot be satisfied (wrong scope, already
    in conflict, or itself missing a prerequisite). A suggestion the user
    cannot apply is worse than no suggestion.
    """
    known = layers()
    need: list[str] = []
    queue = [lid]
    seen: set[str] = set()
    while queue:
        cur = queue.pop()
        if cur in seen or cur in current:
            continue
        seen.add(cur)
        layer = known.get(cur)
        if layer is None:
            return None
        need.append(cur)
        for prereq in layer.get("needs") or []:
            if prereq not in current:
                queue.append(prereq)
    problems = validate_stack(current + need, strict=True)
    if problems:
        return None
    return need


def derive_candidates(
    current: list[str] | None,
    attempts: list[dict],
    profile: list[str] | None = None,
    *,
    limit: int = 3,
    scope: str = "",
) -> list[dict]:
    """Suggest layers to add to a blocked stack.

    Ranking prefers layers that (a) were never tried in this weapon, (b) are
    not already caught by the target profile, (c) have the best observed lift
    elsewhere, and (d) are cheap. A layer that already failed on this weapon is
    never re-suggested — repeating a disproven idea is not self-evolution.
    """
    current = list(current or [])
    tried_without_gain: set[str] = set()
    lift_by_layer = {r["layer"]: r["lift"] for r in attribute(attempts)
                     if r["attributable"] and r["lift"] is not None}

    # layers that appeared in stacks which consistently failed are deprioritised
    failed_stacks = [a for a in attempts if a.get("result") != "ok"]
    for a in failed_stacks:
        for lid in a.get("stack") or []:
            if lift_by_layer.get(lid, 0) <= 0:
                tried_without_gain.add(lid)

    tags = {p.strip().lower() for p in (profile or []) if p and p.strip()}
    out: list[dict] = []
    for layer in registry()["layers"]:
        lid = layer["id"]
        if lid in current:
            continue
        if scope and scope not in (layer.get("applies") or ["payload"]):
            continue          # 流量层不该出现在载荷栈里，反之亦然
        caught = {d.lower() for d in (layer.get("detect") or [])} & tags
        if caught:
            continue  # pointless against this target
        bundle = _bundle_for(lid, current)
        if bundle is None:
            continue          # 前置层补不齐，提了也用不了
        score = 0.0
        reasons = []
        if lid not in tried_without_gain:
            score += 1.0
            reasons.append("本武器未试过")
        lift = lift_by_layer.get(lid)
        if lift is not None:
            score += lift * 2
            reasons.append(f"他处实测 lift {lift:+.2f}")
        score += (4 - int(layer.get("cost", 2))) * 0.25
        score += (3 - int(layer.get("risk", 1))) * 0.15
        if bundle != [lid]:
            extra = [x for x in bundle if x != lid]
            reasons.append("需连同前置层: " + ", ".join(extra))
        out.append({
            "layer": lid,
            "name": layer["name"],
            "cat": layer["cat"],
            "cost": layer.get("cost", 1),
            "risk": layer.get("risk", 1),
            "score": round(score, 2),
            "bundle": bundle,
            "reasons": reasons or ["补盲区"],
        })

    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


def fits_profile(ids: list[str] | None, profile: list[str]) -> bool:
    return not detect_conflicts(ids, profile)


# --------------------------------------------------------------------------- #
# structural prior — what to do before any evidence exists
# --------------------------------------------------------------------------- #
def prior_confidence(
    ids: list[str] | None,
    profile: list[str] | None = None,
    *,
    base: float = 0.20,
    per_layer: float = 0.045,
    per_beat: float = 0.05,
    per_detect: float = 0.06,
    cost_weight: float = 0.02,
    lo: float = 0.05,
    hi: float = 0.88,
) -> float:
    """Expected success of a stack against a defended target, before evidence.

    Two mistakes this exists to prevent:

    * **Naked ranks first.** With every variant at the 0.5 default, sorting by
      cost puts the un-armoured payload on top — the worst possible advice.
      A naked stack starts at `base` instead of 0.5.
    * **"More layers = better".** Layers the target is known to catch are
      penalised while layers that counter its defenses are rewarded, so an
      over-stuffed stack does not automatically outrank a focused one.

    This is a prior, not a measurement: once a variant has real attempts its
    measured confidence takes over (see `estimate`).
    """
    stack = list(ids or [])
    tags = [p for p in (profile or []) if p]
    info = analyse(stack, tags) if tags else {"effective": [], "saturated": [], "caught": []}
    value = (base
             + per_layer * len(stack)
             + per_beat * len(info["effective"])
             + 0.01 * len(info["saturated"])      # some residual value, mostly none
             - per_detect * len(info["caught"])
             - cost_weight * cost(stack))
    return round(max(lo, min(hi, value)), 3)


def estimate(
    variant: dict,
    profile: list[str] | None = None,
    *,
    min_evidence: int = 2,
) -> float:
    """Blend measured confidence with the structural prior.

    Below `min_evidence` attempts the prior dominates; at or above it the
    measurement does. The blend is linear so a single lucky hit cannot crown a
    variant, and a single unlucky miss cannot bury a sound one.
    """
    tries = int((variant.get("stats") or {}).get("tries", 0))
    measured = float(variant.get("confidence", 0.5))
    prior = prior_confidence(variant.get("stack"), profile)
    if tries >= min_evidence:
        return measured
    w = tries / min_evidence
    return round(w * measured + (1 - w) * prior, 3)
