"""Pre-dispatch guard: scope, destructive actions, exfiltration.

Philosophy: check the *intent*, not the command stream. The executor is an
LLM — it will happily rewrite `rm -rf` as `find -delete` if you block the
former. But it cannot rewrite its own declared target list, which is what
this module inspects.

Three checks, in order of real-world severity:

1. SCOPE      — does the intent touch anything outside the authorized range?
                This is the only check that prevents legal damage.
2. DESTRUCTIVE— does the plan mention wiping, dropping, or bricking things?
3. EXFIL      — does the plan ship data outward (webhook, raw socket, scp)?

A guard that cannot see the scope never denies on scope — it warns instead,
so a fresh project stays usable.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_RULES: dict[str, list[str]] = {
    "destructive": [
        r"rm\s+-rf?\s+/(?!\w)",
        r"rm\s+-rf?\s+\*",
        r"\bmkfs(\.\w+)?\b",
        r"\bdd\b[^\n]*of=/dev/",
        r"\bDROP\s+(TABLE|DATABASE)\b",
        r"\bTRUNCATE\s+TABLE\b",
        r"\bshutdown\b|\breboot\b",
        r":\(\)\s*\{\s*:\|:&\s*\};:",          # fork bomb
        r">\s*/etc/(passwd|shadow|sudoers)",
    ],
    "exfil": [
        r"curl[^\n]*(-d|--data|--data-binary|--data-raw|--upload-file|-F)\b",
        r"curl[^\n]*\s-T\s",
        r"\bnc\b[^\n]*\s-e\s",
        r"\bncat\b[^\n]*\s-e\s",
        r"\bscp\b[^\n]*@",
        r"\brsync\b[^\n]*@",
        r"base64[^\n]*\|[^\n]*(curl|wget|nc)\b",
        r"wget[^\n]*--post-(data|file)",
        r"\bssh\b[^\n]*@[^\n]*<",              # ssh < file
        r"\bpython[23]?\s+-c[^\n]*(requests\.post|urlopen)",
    ],
}

# Only treat these as hostnames; everything else (setup.py, index.db) is a file.
_TLDS = {
    "com", "net", "org", "edu", "gov", "mil", "int", "io", "co", "dev", "app",
    "ai", "sh", "xyz", "top", "info", "biz", "me", "tv", "cc", "site", "online",
    "tech", "cloud", "store", "shop", "wiki", "name", "pro", "mobi", "asia",
    "cn", "hk", "tw", "jp", "kr", "sg", "us", "uk", "de", "fr", "ru", "nl",
    "au", "ca", "br", "in", "id", "th", "vn", "my", "ph",
}

_RE_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")
_RE_HOST = re.compile(r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+([a-zA-Z]{2,24})\b")
_RE_URL = re.compile(r"https?://([^/\s:]+)")


@dataclass
class Decision:
    action: str = "allow"              # allow | ask | deny
    reasons: list[str] = field(default_factory=list)
    hits: list[dict] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)

    def bump(self, level: str, reason: str) -> None:
        order = {"allow": 0, "ask": 1, "deny": 2}
        if order[level] > order[self.action]:
            self.action = level
        self.reasons.append(reason)

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reasons": self.reasons,
            "hits": self.hits,
            "targets": self.targets,
            "out_of_scope": self.out_of_scope,
        }


def load_scope(root: Path) -> dict:
    """Read .sapper/scope.json -> {cidrs: [...], domains: [...], notes: str}."""
    path = root / "scope.json"
    if not path.exists():
        return {"cidrs": [], "domains": [], "notes": ""}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"cidrs": [], "domains": [], "notes": "scope.json unreadable"}
    return {
        "cidrs": [str(x) for x in raw.get("cidrs", [])],
        "domains": [str(x).lower().lstrip(".") for x in raw.get("domains", [])],
        "notes": raw.get("notes", ""),
    }


def save_scope(root: Path, scope: dict) -> None:
    path = root / "scope.json"
    payload = {
        "cidrs": scope.get("cidrs", []),
        "domains": scope.get("domains", []),
        "notes": scope.get("notes", ""),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract_targets(text: str) -> list[str]:
    """Pull IPv4/CIDR and hostnames out of free text, deduped, order kept."""
    found: list[str] = []

    def add(value: str) -> None:
        value = value.strip().rstrip(".,;:'\"")
        if value and value not in found:
            found.append(value)

    for match in _RE_URL.finditer(text):
        add(match.group(1))

    for match in _RE_IPV4.finditer(text):
        candidate = match.group(0)
        try:
            ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            continue
        add(candidate)

    for match in _RE_HOST.finditer(text):
        host = match.group(0)
        tld = match.group(1).lower()
        if tld in _TLDS:
            add(host.lower())

    return found


def _in_cidrs(ip: str, cidrs: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("/")[0])
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _under_domains(host: str, domains: list[str]) -> bool:
    host = host.lower()
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _is_private_target(target: str) -> bool:
    """Private IPv4/CIDR counts as lab-safe. Domains are always public."""
    try:
        return ipaddress.ip_network(target, strict=False).is_private
    except ValueError:
        return False


class Guard:
    def __init__(self, scope: dict | None = None, rules: dict | None = None) -> None:
        self.scope = scope or {"cidrs": [], "domains": [], "notes": ""}
        self.rules = rules or DEFAULT_RULES
        self._compiled = {
            name: [re.compile(p, re.IGNORECASE) for p in patterns]
            for name, patterns in self.rules.items()
        }

    @classmethod
    def for_project(cls, root: Path, rules_path: Path | None = None) -> "Guard":
        rules = DEFAULT_RULES
        if rules_path and rules_path.exists():
            try:
                rules = {**DEFAULT_RULES, **json.loads(rules_path.read_text(encoding="utf-8"))}
            except Exception:
                pass
        return cls(load_scope(root), rules)

    def check_text(self, text: str, *, check_scope: bool = True) -> Decision:
        decision = Decision()
        if not text:
            return decision

        # 1. scope -------------------------------------------------------
        self.targets = extract_targets(text)
        decision.targets = list(self.targets)
        if check_scope and self.targets:
            has_scope = bool(self.scope["cidrs"] or self.scope["domains"])
            if not has_scope:
                # No declared scope: let private ranges through (lab/CTF is the
                # common case) but make the operator declare scope before
                # touching anything public.
                public = [t for t in self.targets if not _is_private_target(t)]
                if public:
                    decision.bump(
                        "ask",
                        f"未声明授权范围，目标含公网地址: {', '.join(public[:3])} "
                        f"(用 sapper scope add 声明)",
                    )
            else:
                outside = []
                for target in self.targets:
                    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?", target):
                        if not _in_cidrs(target, self.scope["cidrs"]):
                            outside.append(target)
                    elif not _under_domains(target, self.scope["domains"]):
                        outside.append(target)
                if outside:
                    decision.out_of_scope = outside
                    decision.bump("deny", f"目标越界: {', '.join(outside[:4])}")

        # 2/3. destructive + exfiltration --------------------------------
        for name, level in (("destructive", "deny"), ("exfil", "ask")):
            for pattern in self._compiled.get(name, []):
                match = pattern.search(text)
                if match:
                    decision.hits.append({"rule": name, "match": match.group(0)[:80]})
                    decision.bump(level, f"{name}: {match.group(0)[:60]}")

        return decision

    def check_intent(self, intent: dict) -> Decision:
        parts = [
            intent.get("title") or "",
            intent.get("description") or "",
            intent.get("doc") or "",
        ]
        return self.check_text("\n".join(parts))
