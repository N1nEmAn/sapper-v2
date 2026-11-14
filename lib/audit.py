"""Post-hoc audit: scan executor output for risky operations.

Not a sandbox — this runs after the fact. The question it answers is
forensic: did this intent touch anything it should not have?

Findings can be recorded as facts, which puts them in the graph and makes
them visible in the console. That is the point: an LLM can rewrite a command
to dodge a rule, but it cannot un-record what the output says it did.

Usage (via CLI):
    sapper audit --file runs/xxx/executor.log
    sapper audit --text "..." --record
"""

from __future__ import annotations

import re

# Lines that look like an executed command: "$ cmd", "> cmd", "[bash] cmd",
# or the transcript form "● Bash(cmd)" used by some agent UIs.
_RE_CMD = re.compile(
    r"^\s*(?:\$|>|➜)\s+(?P<a>.+)$"
    r"|^\s*\[(?:bash|shell|cmd|exec)\]\s*(?P<b>.+)$"
    r"|^\s*[●•]\s*(?:Bash|Shell|Exec)\((?P<c>.+)\)\s*$",
    re.MULTILINE,
)

_MAX_CMD = 300


def extract_commands(text: str, limit: int = 4000) -> list[str]:
    """Pull candidate command lines out of a transcript."""
    commands: list[str] = []
    for match in _RE_CMD.finditer(text):
        raw = match.group("a") or match.group("b") or match.group("c") or ""
        cmd = raw.strip()
        if cmd and len(cmd) <= _MAX_CMD:
            commands.append(cmd)
        if len(commands) >= limit:
            break
    return commands


def scan(text: str, guard, *, sample: int = 12) -> list[dict]:
    """Run the guard over extracted commands plus the whole transcript.

    `guard` is a guard.Guard instance (injected so audit stays free of import
    gymnastics and uses the project's own scope/rules).
    """
    findings: list[dict] = []
    seen: set[str] = set()

    for cmd in extract_commands(text):
        decision = guard.check_text(cmd)
        if decision.action == "allow":
            continue
        key = f"{decision.action}|{cmd[:120]}"
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            {"action": decision.action, "cmd": cmd, "reasons": decision.reasons}
        )
        if len(findings) >= sample:
            break

    # Whole-transcript sweep catches prose that never appeared after a prompt.
    whole = guard.check_text(text)
    if whole.action != "allow":
        key = f"{whole.action}|__transcript__"
        if key not in seen:
            findings.append(
                {
                    "action": whole.action,
                    "cmd": "(整体文本)",
                    "reasons": whole.reasons,
                }
            )
    return findings


def summarize(findings: list[dict]) -> str:
    if not findings:
        return "未发现可疑操作"
    parts = [f"{f['action']}:{f['cmd'][:40]}" for f in findings[:5]]
    tail = "" if len(findings) <= 5 else f" 等 {len(findings)} 项"
    return "; ".join(parts) + tail
