"""Weapon taxonomy: `phase/technique`, ordered by kill-chain phase.

The old `family` strings (exploit/tomcat, web/sqli, recon/dir) grew ad hoc. This
gives them a single axis: the phase answers *when in an engagement you reach for
this*, the technique answers *against what / with what*.

A two-segment scheme beats a flat one because:
  * filtering by phase is the common case ("what do I have for privesc"),
  * the technique namespace stays small and self-documenting,
  * a new phase or technique never forces renaming existing entries.

`phase` follows the kill chain; `tool` and `payload` are cross-cutting (a
scanner is used in recon but is *a tool*, not a recon technique).

Techniques are keyed by (phase, technique) — the same technique name may mean
different things in different phases (e.g. `windows` under privesc is a target
OS, under payload it is a payload platform), so a flat name→desc map would
silently overwrite one of them.
"""

PHASES = {
    "recon": "侦察/信息收集",
    "vulnscan": "漏扫/漏洞探测",
    "exploit": "漏洞利用（打点）",
    "privesc": "提权",
    "lateral": "横向移动",
    "post": "后渗透/持久化",
    "payload": "载荷投递/免杀",
    "tool": "独立工具/软件",
}

# (phase, technique) -> 中文说明
TECHNIQUES = {
    # recon
    ("recon", "subdomain"): "子域枚举",
    ("recon", "port"): "端口/服务扫描",
    ("recon", "dir"): "目录/端点枚举",
    ("recon", "api"): "接口侦察",
    ("recon", "fingerprint"): "指纹识别",
    ("recon", "dns"): "DNS 侦察",
    # vulnscan
    ("vulnscan", "web"): "Web 漏洞扫描",
    ("vulnscan", "network"): "网络漏洞扫描",
    ("vulnscan", "config"): "配置缺陷核查",
    ("vulnscan", "cve"): "CVE 批量检测",
    # exploit (打点)
    ("exploit", "web-rce"): "Web RCE/命令注入",
    ("exploit", "web-sqli"): "SQL 注入",
    ("exploit", "web-ssrf"): "SSRF",
    ("exploit", "web-file"): "任意文件读/写",
    ("exploit", "web-upload"): "文件上传",
    ("exploit", "deser"): "反序列化",
    ("exploit", "service"): "服务/中间件漏洞",
    # privesc
    ("privesc", "linux"): "Linux 提权",
    ("privesc", "windows"): "Windows 提权",
    ("privesc", "container"): "容器逃逸",
    ("privesc", "db"): "数据库提权",
    # lateral
    ("lateral", "pass-hash"): "Pass-the-Hash",
    ("lateral", "kerberos"): "Kerberos 攻击",
    ("lateral", "rdp"): "RDP/凭据复用",
    # post
    ("post", "persistence"): "持久化",
    ("post", "exfil"): "数据外传",
    ("post", "cleanup"): "痕迹清理",
    # payload (载荷/免杀)
    ("payload", "windows"): "Windows 载荷",
    ("payload", "linux"): "Linux 载荷",
    ("payload", "document"): "钓鱼文档",
    ("payload", "script"): "脚本投递",
    ("payload", "office"): "Office 宏",
    # tool
    ("tool", "scanner"): "扫描器",
    ("tool", "proxy"): "代理/隧道",
    ("tool", "brute"): "弱口令/爆破",
    ("tool", "cracker"): "破解/离线攻击",
    ("tool", "postex"): "后渗透框架",
    ("tool", "forensics"): "取证",
}


def valid_family(family: str) -> bool:
    if "/" not in family:
        return False
    phase, technique = family.split("/", 1)
    return (phase, technique) in TECHNIQUES


def describe(family: str) -> str:
    if "/" not in family:
        return family
    phase, technique = family.split("/", 1)
    p = PHASES.get(phase, phase)
    t = TECHNIQUES.get((phase, technique), technique)
    return f"{p} / {t}"


def phases() -> dict:
    return dict(PHASES)


def techniques(phase: str) -> dict:
    return {t: d for (p, t), d in TECHNIQUES.items() if p == phase}


def all_families() -> list[str]:
    return [f"{p}/{t}" for (p, t) in sorted(TECHNIQUES)]
