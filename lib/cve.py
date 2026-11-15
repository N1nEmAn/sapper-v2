"""CVE knowledge base: local YAML entries, pulled from CIRCL (cvelistV5 mirror).

Why YAML: it is the lingua franca the user asked for — a CVE entry should be
shareable, diffable, and editable without touching code. `yamlmini` keeps the
stdlib-only constraint while supporting exactly the shape below.

Why CIRCL: NVD's API 2.0 returns 503 from CN networks without an API key;
cve.circl.lu mirrors cvelistV5 and is directly reachable. The mirror omits CVSS
metrics, so `severity` is UNKNOWN unless the entry already has one — a missing
score is honest, a guessed one is not.

Entry schema (YAML):
    id: CVE-2020-1938
    description: "..."
    severity: UNKNOWN          # CRITICAL/HIGH/MEDIUM/LOW/UNKNOWN
    cvss: null
    cwe: [CWE-XX]
    vendor: Apache
    product: Apache Tomcat
    versions: ["9.0.0.M1 - 9.0.0.30"]
    published: "2020-02-24"
    references: [url, ...]
    tags: [rce, file-read]
    weapons: [w001]
    source: circl
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from yamlmini import dumps as _ydump, loads as _yload

DEFAULT_ROOT = Path(
    os.environ.get("SAPPER_CVE_ROOT", str(Path.home() / ".sapper" / "cve"))
)
SEED_DIR = Path(__file__).resolve().parent.parent / "kb" / "cve"

CIRCL = "https://cve.circl.lu/api/cve/{cid}"
NVD = "https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={cid}"

_CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$")

# map a CVE type hint (from description / CWE) onto armory families
_TAG_TO_FAMILY = [
    (("file", "read", "traversal", "include", "path"), "exploit/web-file"),
    (("sql", "injection", "sqli"), "exploit/web-sqli"),
    (("ssrf",), "exploit/web-ssrf"),
    (("command", "rce", "code execution", "injection"), "exploit/web-rce"),
    (("deserialization", "deserialize"), "exploit/deser"),
    (("upload",), "exploit/web-upload"),
    (("privilege", "elevation", "escalation"), "privesc/windows"),
    (("buffer overflow", "overflow", "use-after-free"), "exploit/service"),
    (("authentication", "bypass", "auth"), "exploit/service"),
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CVEStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_ROOT

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, cid: str) -> Path:
        return self.root / f"{cid}.yaml"

    def load(self, cid: str) -> dict:
        path = self.path(cid)
        if not path.exists():
            raise FileNotFoundError(f"未找到 {cid}（在 {self.root}）")
        return _yload(path.read_text(encoding="utf-8"))

    def save(self, entry: dict) -> Path:
        self.ensure()
        path = self.path(entry["id"])
        path.write_text(_ydump(entry), encoding="utf-8")
        return path

    def all(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for path in sorted(self.root.glob("*.yaml")):
            try:
                data = _yload(path.read_text(encoding="utf-8"))
                if data.get("id"):
                    out.append(data)
            except Exception:
                continue
        return out

    def get(self, cid: str) -> dict | None:
        try:
            return self.load(cid)
        except FileNotFoundError:
            return None

    # -- fetching ---------------------------------------------------------
    def fetch(self, cid: str, *, source: str = "circl") -> dict:
        if not _CVE_RE.match(cid):
            raise ValueError(f"{cid} 不是合法 CVE 编号（CVE-YYYY-NNNN）")
        url = (CIRCL if source == "circl" else NVD).format(cid=cid)
        req = urllib.request.Request(url, headers={"User-Agent": "sapper-cve/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except Exception as exc:
            raise RuntimeError(
                f"拉取 {cid} 失败（{url}）：{exc}。"
                f"可用 `sapper cve import --file x.yaml` 导入本地条目。"
            ) from exc
        if source == "circl":
            return self._from_cvelist_v5(cid, json.loads(raw))
        return self._from_nvd(cid, json.loads(raw))

    def _from_cvelist_v5(self, cid: str, data: dict) -> dict:
        cna = (data.get("containers") or {}).get("cna") or {}
        desc = next((d.get("value", "") for d in (cna.get("descriptions") or [])
                     if d.get("lang") == "en"), "")
        if not desc and cna.get("descriptions"):
            desc = cna["descriptions"][0].get("value", "")

        affected = cna.get("affected") or []
        vendor = product = ""
        versions: list[str] = []
        if affected:
            first = affected[0]
            vendor = first.get("vendor") or ""
            product = first.get("product") or ""
            for v in first.get("versions") or []:
                versions.append(v.get("version") or "")

        cwes = []
        for pt in cna.get("problemTypes") or []:
            for d in pt.get("descriptions") or []:
                if d.get("cweId"):          # CWE-22; skip human "description"
                    cwes.append(d["cweId"])
        cwes = list(dict.fromkeys(cwes))

        refs = [r.get("url") for r in (cna.get("references") or []) if r.get("url")]
        published = (data.get("cveMetadata") or {}).get("datePublished", "")
        tags = _guess_tags(desc, cwes)

        return {
            "id": cid,
            "description": desc.strip(),
            "severity": "UNKNOWN",
            "cvss": None,
            "cwe": cwes,
            "vendor": vendor,
            "product": product,
            "versions": versions,
            "published": published[:10] if published else "",
            "references": refs,
            "tags": tags,
            "weapons": [],
            "source": "circl",
            "imported_at": _now(),
        }

    def _from_nvd(self, cid: str, data: dict) -> dict:
        vuln = ((data.get("vulnerabilities") or [{}])[0].get("cve") or {})
        desc = next((d.get("value", "") for d in (vuln.get("descriptions") or [])
                     if d.get("lang") == "en"), "")
        metrics = (vuln.get("metrics") or {}).get("cvssMetricV31") or \
                  (vuln.get("metrics") or {}).get("cvssMetricV30") or []
        cvss = None
        severity = "UNKNOWN"
        if metrics:
            cvss = metrics[0].get("cvssData", {}).get("baseScore")
            sev = metrics[0].get("cvssData", {}).get("baseSeverity")
            if sev:
                severity = sev.upper()
        cwes = []
        for w in vuln.get("weaknesses") or []:
            for d in w.get("description") or []:
                if d.get("value"):
                    cwes.append(d["value"])
        refs = [r.get("url") for r in (vuln.get("references") or []) if r.get("url")]
        return {
            "id": cid,
            "description": desc.strip(),
            "severity": severity,
            "cvss": cvss,
            "cwe": cwes,
            "vendor": "",
            "product": "",
            "versions": [],
            "published": (vuln.get("published") or "")[:10],
            "references": refs,
            "tags": _guess_tags(desc, cwes),
            "weapons": [],
            "source": "nvd",
            "imported_at": _now(),
        }

    def import_cve(self, cid: str, *, source: str = "circl", force: bool = False) -> dict:
        self.ensure()
        existing = self.get(cid)
        if existing and not force:
            return existing
        entry = self.fetch(cid, source=source)
        self.save(entry)
        return entry

    # -- seeding ----------------------------------------------------------
    def seed(self) -> int:
        if not SEED_DIR.is_dir():
            return 0
        self.ensure()
        copied = 0
        for path in sorted(SEED_DIR.glob("*.yaml")):
            target = self.root / path.name
            if target.exists():
                continue
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            copied += 1
        return copied

    # -- search -----------------------------------------------------------
    def search(self, keyword: str) -> list[dict]:
        kw = keyword.lower()
        hits = []
        for entry in self.all():
            hay = " ".join([
                entry.get("id", ""), entry.get("description", ""),
                entry.get("vendor", ""), entry.get("product", ""),
                " ".join(entry.get("tags") or []),
                " ".join(entry.get("cwe") or []),
            ]).lower()
            if kw in hay:
                hits.append(entry)
        return hits

    # -- link to weapons --------------------------------------------------
    def guess_family(self, entry: dict) -> str:
        text = ((entry.get("description") or "") + " " +
                " ".join(entry.get("cwe") or []) + " " +
                " ".join(entry.get("tags") or [])).lower()
        for needles, family in _TAG_TO_FAMILY:
            if any(n in text for n in needles):
                return family
        return "vulnscan/cve"


def _guess_tags(desc: str, cwes: list[str]) -> list[str]:
    text = (desc or "").lower() + " " + " ".join(cwes).lower()
    tags = []
    for tag, needles in {
        "rce": ["remote code", "code execution", "rce"],
        "file-read": ["read arbitrary", "file read", "traversal", "include", "path"],
        "sqli": ["sql", "sqli"],
        "ssrf": ["ssrf", "server-side request"],
        "deser": ["deserial", "unserialize"],
        "dos": ["denial of service", "dos"],
        "auth-bypass": ["authentication bypass", "auth bypass", "bypass auth"],
        "privesc": ["privilege escalation", "elevation of privilege"],
        "xss": ["cross-site scripting", "xss"],
        "overflow": ["buffer overflow", "overflow", "use-after-free"],
    }.items():
        if any(n in text for n in needles):
            tags.append(tag)
    return tags
