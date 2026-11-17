import contextlib
import hashlib
import io
import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import sapper  # noqa: E402

SETTINGS_PATH = ROOT / "lib" / "_settings_patch.py"
_spec = importlib.util.spec_from_file_location("settings_patch_for_tests", SETTINGS_PATH)
settings_patch = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(settings_patch)


class GraphIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "ship it")

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_node_is_skipped_and_reported_by_validate(self):
        (self.store.facts_dir / "f001.json").write_text('{"id":"f001"}\n', encoding="utf-8")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rendered = sapper.render_graph_text(self.store)
        self.assertIn("goal: ship it", rendered)
        self.assertIn("missing string 'description'", stderr.getvalue())
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("f001.json" in error for error in errors))

    def test_init_refuses_non_empty_directory(self):
        root = Path(self.tmp.name) / "new-store"
        root.mkdir()
        (root / "unrelated.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(sapper.FGError, "non-empty directory"):
            sapper.Store(root).init("", "goal")
        self.assertTrue((root / "unrelated.txt").exists())

    def test_validate_detects_goal_metadata_drift(self):
        goal = self.store.facts_dir / "goal.json"
        data = json.loads(goal.read_text(encoding="utf-8"))
        data["description"] = "different goal"
        goal.write_text(json.dumps(data), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertIn("goal fact description does not match project goal", errors)

    def test_validate_detects_intent_state_mismatch(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "work", "test")
        path = self.store.intents_dir / f"{intent['id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "done"
        path.write_text(json.dumps(data), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("completed status requires a target" in error for error in errors))

    def test_confirmation_is_enforced_at_write_boundary(self):
        fact = self.store.add_fact("starting point", "test", None)
        intent = self.store.add_intent(
            [fact["id"]], "delete data", "test", requires_confirmation=True
        )
        with self.assertRaisesRegex(sapper.FGError, "requires confirmation"):
            self.store.conclude_with_new_fact(intent["id"], "must not run", "test")
        self.store.confirm_intent(intent["id"])
        self.store.claim_intent(intent["id"], "owner")
        with self.assertRaisesRegex(sapper.FGError, "claimed by owner"):
            self.store.conclude_with_new_fact(intent["id"], "wrong worker", "other")
        result, concluded = self.store.conclude_with_new_fact(
            intent["id"], "approval was recorded", "owner"
        )
        self.assertEqual(concluded["to"], result["id"])

    def test_lookup_ids_cannot_escape_store(self):
        with self.assertRaisesRegex(sapper.FGError, "invalid fact id"):
            self.store.get_fact("../../outside")

    def test_stale_counter_never_overwrites_existing_node(self):
        first = self.store.add_fact("first", "test", None)
        project = self.store.load_project()
        project["counters"]["fact"] = 0
        self.store.save_project(project)
        second = self.store.add_fact("second", "test", None)
        self.assertEqual(first["id"], "f001")
        self.assertEqual(second["id"], "f002")
        self.assertEqual(self.store.get_fact("f001")["description"], "first")

    def test_fact_from_intent_closes_the_intent_atomically(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "observe", "test")
        fact = self.store.add_fact("observed", "test", intent["id"])
        self.assertEqual(self.store.get_intent(intent["id"])["to"], fact["id"])
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_linking_existing_fact_sets_its_source_edge(self):
        seed = self.store.add_fact("seed", "test", None)
        result = self.store.add_fact("already observed", "test", None)
        intent = self.store.add_intent([seed["id"]], "link result", "test")
        self.store.conclude_intent(intent["id"], result["id"], "test")
        self.assertEqual(self.store.get_fact(result["id"])["source_intent"], intent["id"])
        self.assertEqual(sapper.validate_graph(self.store), [])

    def test_pick_claim_skips_another_workers_lease(self):
        seed = self.store.add_fact("seed", "test", None)
        first = self.store.add_intent([seed["id"]], "first", "test")
        second = self.store.add_intent([seed["id"]], "second", "test")
        self.store.claim_intent(first["id"], "other-worker")
        args = type("Args", (), {
            "store": str(self.store.root), "claim": True,
            "worker": "my-worker", "any": False,
        })()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sapper.cmd_pick(args)
        self.assertEqual(output.getvalue().strip(), second["id"])

    def test_static_view_escapes_script_terminators(self):
        self.store.add_fact("</script><script>alert(1)</script>", "test", None)
        html = sapper._render_static_html(self.store)
        self.assertNotIn("</script><script>", html)
        self.assertIn(r"\u003c/script\u003e", html)

    def test_prompt_substitution_does_not_rewrite_inserted_braces(self):
        self.store.add_fact("literal {goal} marker", "test", None)
        prompt = sapper.render_prompt(self.store, "{graph_yaml}\nGOAL={goal}")
        self.assertIn("literal {goal} marker", prompt)
        self.assertTrue(prompt.endswith("GOAL=ship it"))

    def test_brainstorm_template_token_does_not_rewrite_graph_text(self):
        self.store.add_fact("literal {max_options} marker", "test", None)
        args = type("Args", (), {
            "max_options": 2, "dry_run": True,
        })()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sapper._dispatch_brainstorm(
                self.store, ROOT / "templates", "sonnet", 10, False, ROOT, args
            )
        self.assertIn("literal {max_options} marker", output.getvalue())

    def test_verify_payload_requires_actionable_issues_on_failure(self):
        with self.assertRaisesRegex(sapper.FGError, "explain"):
            sapper._parse_verify_payload({"verified": False, "issues": ""})
        with self.assertRaisesRegex(sapper.FGError, "leave 'issues' empty"):
            sapper._parse_verify_payload({"verified": True, "issues": "warning"})

    def test_dispatch_verify_returns_failure_status(self):
        seed = self.store.add_fact("seed", "test", None)
        intent = self.store.add_intent([seed["id"]], "check", "test")
        result, _ = self.store.conclude_with_new_fact(intent["id"], "result", "test")
        args = type("Args", (), {
            "intent": intent["id"], "dry_run": False,
        })()
        with mock.patch.object(
            sapper,
            "run_claude",
            return_value={"result": '{"accepted": true, "data": {"verified": false, "issues": "mismatch"}}'},
        ):
            rc = sapper._dispatch_verify(
                self.store, ROOT / "templates", "sonnet", 10, False, ROOT, args
            )
        self.assertEqual(rc, 1)

    def test_completed_graph_validates_and_duplicate_completion_is_rejected(self):
        fact = self.store.add_fact("evidence", "test", None)
        self.store.complete_project([fact["id"]], "finish", "test")
        self.assertEqual(self.store.load_project()["status"], "completed")
        self.assertEqual(sapper.validate_graph(self.store), [])
        with self.assertRaisesRegex(sapper.FGError, "already completed"):
            self.store.complete_project([fact["id"]], "finish again", "test")

    def test_brainstorm_batches_are_unique(self):
        seed = self.store.add_fact("seed", "test", None)
        payload = {
            "accepted": True,
            "data": {
                "options": [
                    {"from": [seed["id"]], "title": "one", "description": "first"}
                ]
            },
        }
        sapper._apply_brainstorm(self.store, payload)
        sapper._apply_brainstorm(self.store, payload)
        docs = [i["doc"] for i in self.store.list_intents()]
        self.assertEqual(len(docs), 2)
        tags = [line for doc in docs for line in doc.splitlines() if line.startswith("[brainstorm batch:")]
        self.assertEqual(len(set(tags)), 2)

    def test_brainstorm_pick_preserves_abandoned_edges(self):
        seed = self.store.add_fact("seed", "test", None)
        payload = {
            "accepted": True,
            "data": {"options": [
                {"from": [seed["id"]], "title": "one", "description": "first"},
                {"from": [seed["id"]], "title": "two", "description": "second"},
            ]},
        }
        sapper._apply_brainstorm(self.store, payload)
        picked = self.store.list_intents()[0]
        args = type("Args", (), {"store": str(self.store.root), "id": picked["id"], "purge": False})()
        sapper.cmd_brainstorm_pick(args)
        intents = self.store.list_intents()
        self.assertEqual(sum(i["to"] is None for i in intents), 1)
        self.assertEqual(sum(i["to"] is not None for i in intents), 1)
        self.assertEqual(sapper.validate_graph(self.store), [])


class SettingsPatchTests(unittest.TestCase):
    def test_settings_patch_is_atomic_and_preserves_unrelated_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(
                json.dumps({"hooks": {"SessionStart": [{"matcher": "x", "hooks": [
                    {"type": "command", "command": "echo keep"
                    }
                ]}]}}),
                encoding="utf-8",
            )
            changed, _ = settings_patch.add(path, "/tmp/sapper-hook.py")
            self.assertTrue(changed)
            data = json.loads(path.read_text(encoding="utf-8"))
            commands = [
                h["command"]
                for groups in data["hooks"].values()
                for group in groups
                for h in group.get("hooks", [])
            ]
            self.assertIn("echo keep", commands)
            self.assertEqual(sum("sapper-hook.py" in command for command in commands), 2)
            settings_patch.remove(path, "/tmp/sapper-hook.py")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"]["SessionStart"][0]["hooks"][0]["command"], "echo keep")

    def test_settings_patch_rejects_non_object_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                settings_patch.add(path, "/tmp/sapper-hook.py")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                settings_patch.remove(path, "/tmp/sapper-hook.py")

    def test_settings_validation_catches_malformed_event_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"SessionStart": {}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hooks.SessionStart"):
                settings_patch.validate(path)


class PeerConfigTests(unittest.TestCase):
    def test_malformed_peer_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sapper.Store(Path(tmp) / ".sapper")
            store.init("", "peers")
            (store.root / sapper.PEERS_FILE).write_text(
                json.dumps({"version": 1, "channel": "x", "peers": {"bad name": {"target": "x"}}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(sapper.FGError, "invalid peer name"):
                sapper._load_peers(store)

    def test_setup_preflights_invalid_settings_before_creating_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".claude").mkdir()
            (project / ".claude" / "settings.json").write_text("[]", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                import os
                os.chdir(project)
                args = type("Args", (), {
                    "store": ".sapper", "goal": "should not initialize",
                    "origin": "", "agents": False, "force": False,
                })()
                with self.assertRaisesRegex(sapper.FGError, "JSON object"):
                    sapper.cmd_setup(args)
            finally:
                os.chdir(old_cwd)
            self.assertFalse((project / ".sapper" / "project.json").exists())


if __name__ == "__main__":
    unittest.main()


class TypedFactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "capture the flag")

    def tearDown(self):
        self.tmp.cleanup()

    def test_typed_fact_roundtrip_and_render(self):
        fact = self.store.add_fact(
            "nginx 1.18 at 10.0.2.15:80", "user",
            kind="service", data={"host": "10.0.2.15", "port": 80},
        )
        self.assertEqual(fact["kind"], "service")
        self.assertEqual(fact["data"]["port"], "80")
        text = sapper.render_graph_text(self.store)
        self.assertIn("<service>", text)
        self.assertIn("port=80", text)
        yaml = sapper._to_yaml_like(sapper.build_graph_view(self.store))
        self.assertIn("<service>", yaml)
        # prompt rendering scrubs braces so flag{...} cannot eat placeholders
        self.assertIn("port=80", yaml)

    def test_unknown_kind_and_bad_data_rejected(self):
        with self.assertRaises(sapper.FGError):
            self.store.add_fact("x", "user", kind="exploit")
        with self.assertRaises(sapper.FGError):
            self.store.add_fact("x", "user", kind="cred", data={"bad key!": "1"})

    def test_validate_rejects_invalid_kind_on_disk(self):
        fact = self.store.add_fact("x", "user", kind="cred")
        p = self.store.facts_dir / f"{fact['id']}.json"
        node = json.loads(p.read_text(encoding="utf-8"))
        node["kind"] = "exploit"
        p.write_text(json.dumps(node), encoding="utf-8")
        errors = sapper.validate_graph(self.store)
        self.assertTrue(any("kind" in e for e in errors))

    def test_explore_payload_carries_kind_and_data(self):
        payload = {"accepted": True, "data": {
            "description": "RCE confirmed",
            "kind": "vuln",
            "data": {"cve": "CVE-2021-41773"},
        }}
        title, desc, doc, kind, data = sapper._parse_explore_payload(payload)
        self.assertEqual(kind, "vuln")
        self.assertEqual(data, {"cve": "CVE-2021-41773"})


class FlagLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = sapper.Store(Path(self.tmp.name) / ".sapper")
        self.store.init("test", "capture the flag")

    def tearDown(self):
        self.tmp.cleanup()

    def test_flag_record_and_dedupe(self):
        fact = self.store.add_fact(
            "flag captured: flag{a}", "agent",
            kind="flag", data={"value": "flag{a}", "challenge": "c01", "status": "recorded"},
        )
        dup = sapper._find_flag_fact(self.store, "flag{a}")
        self.assertEqual(dup["id"], fact["id"])
        self.assertIsNone(sapper._find_flag_fact(self.store, "flag{b}"))

    def test_flag_status_lifecycle_and_type_guard(self):
        fact = self.store.add_fact(
            "flag captured: flag{a}", "agent",
            kind="flag", data={"value": "flag{a}", "status": "recorded"},
        )
        updated = self.store.update_flag_fact(fact["id"], "submitted", '{"ok":true}')
        self.assertEqual(updated["data"]["status"], "submitted")
        self.assertEqual(updated["data"]["result"], '{"ok":true}')
        plain = self.store.add_fact("just text", "agent")
        with self.assertRaises(sapper.FGError):
            self.store.update_flag_fact(plain["id"], "submitted", None)
        with self.assertRaises(sapper.FGError):
            self.store.update_flag_fact(fact["id"], "hacked-status", None)

    def test_status_shows_flag_ledger(self):
        self.store.add_fact(
            "flag captured: flag{z}", "agent",
            kind="flag", data={"value": "flag{z}", "challenge": "c09", "status": "submitted"},
        )
        text = sapper.render_status(self.store)
        self.assertIn("flag(s)", text)
        self.assertIn("status=submitted", text)
        self.assertIn("challenge=c09", text)


class DeadlineTests(unittest.TestCase):
    def test_minutes_and_iso_and_conflict(self):
        import time as _t
        a = type("A", (), {"deadline": None, "minutes": 5})()
        d = sapper.deadline_from_args(a)
        self.assertGreater(d, _t.time())
        b = type("B", (), {"deadline": "2026-10-17T13:00:00", "minutes": None})()
        self.assertIsNotNone(sapper.deadline_from_args(b))
        c = type("C", (), {"deadline": "2026-10-17T13:00:00+08:00", "minutes": 5})()
        with self.assertRaises(sapper.FGError):
            sapper.deadline_from_args(c)
        bad = type("D", (), {"deadline": "not-a-date", "minutes": None})()
        with self.assertRaises(sapper.FGError):
            sapper.deadline_from_args(bad)
        self.assertEqual(sapper.deadline_from_args(type("E", (), {"deadline": None, "minutes": None})()), None)

    def test_format_time_budget(self):
        self.assertEqual(sapper.format_time_budget(None), "")
        self.assertIn("TIME IS UP",
                      sapper.format_time_budget(0))  # 0 -> epoch 1970, always past
        self.assertIn("remaining", sapper.format_time_budget(_t.time() + 600))


import time as _t  # noqa: E402  (DeadlineTests)


class ArenaClientTests(unittest.TestCase):
    """Verify the exact TSecBench Challenges API contract (header, paths,
    params/body, error envelope)."""

    def _arena(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("_arena_test", ROOT / "lib" / "arena.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_client_contract_against_local_server(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        arena = self._arena()
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                seen.append((self.command, self.path, self.headers.get("BENCHMARK_TOKEN"), body))

            def do_GET(self):
                self._record()
                if self.path.startswith("/openapi/v1/challenges/hint"):
                    return self._reply({"unique_code": "c1", "hint": "h"})
                return self._reply([{"unique_code": "c1", "difficulty": "easy", "level": "1",
                                     "total_score": 100, "flag_count": 2, "correct_flag_count": 0,
                                     "is_completed": False, "description": "d",
                                     "container_status": "stopped", "container_addr": []}])

            def do_POST(self):
                self._record()
                if self.path.startswith("/openapi/v1/challenges/submit"):
                    return self._reply({"correct": True, "awarded": 50, "cumulative_score": 50,
                                        "correct_flag_count": 1, "total_flag_count": 2,
                                        "matched_flag_index": 0})
                if self.path.startswith("/openapi/v1/challenges/close"):
                    return self._reply({"unique_code": "c1", "closed": True})
                return self._reply({"unique_code": "c1", "container_addr": ["10.0.0.9:80"]})

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = arena.ArenaClient(f"http://127.0.0.1:{server.server_address[1]}", "tok-x")
            chs = client.list_challenges()
            self.assertEqual(chs[0]["unique_code"], "c1")
            started = client.start_challenge("c1")
            self.assertEqual(started["container_addr"], ["10.0.0.9:80"])
            self.assertEqual(client.get_hint("c1")["hint"], "h")
            res = client.submit_flag("c1", "flag{a}")
            self.assertTrue(res["correct"])
            self.assertTrue(client.close_challenge("c1")["closed"])
        finally:
            server.shutdown()

        # every call carried the BENCHMARK_TOKEN header
        self.assertTrue(all(entry[2] == "tok-x" for entry in seen), seen)
        # exact paths + param/body shapes
        submit = [e for e in seen if e[1] == "/openapi/v1/challenges/submit"][0]
        self.assertEqual(submit[3], {"unique_code": "c1", "flag": "flag{a}"})
        start = [e for e in seen if "start" in e[1]][0]
        self.assertIn("unique_code=c1", start[1])
        hint = [e for e in seen if "hint" in e[1]][0]
        self.assertIn("unique_code=c1", hint[1])
        close = [e for e in seen if "close" in e[1]][0]
        self.assertIn("unique_code=c1", close[1])

    def test_error_envelope_surfaces_code(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        arena = self._arena()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.dumps({"code": "invalid_state", "message": "max active", "detail": {}}).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = arena.ArenaClient(f"http://127.0.0.1:{server.server_address[1]}", "t")
            with self.assertRaises(arena.ArenaError) as ctx:
                client.start_challenge("c1")
            self.assertEqual(ctx.exception.code, "invalid_state")
            self.assertEqual(ctx.exception.status, 409)
        finally:
            server.shutdown()

    def test_config_resolution_env_and_file(self):
        arena = self._arena()
        with mock.patch.dict("os.environ", {"BENCHMARK_BASE_URL": "http://env-base",
                                            "BENCHMARK_TOKEN": "env-tok"}):
            c = arena.ArenaClient.load(None)
            self.assertEqual(c.base_url, "http://env-base")
            self.assertEqual(c.token, "env-tok")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "arena.json").write_text(json.dumps(
                {"base_url": "http://file-base", "token": "ENV:BENCHMARK_TOKEN"}), encoding="utf-8")
            with mock.patch.dict("os.environ", {"BENCHMARK_TOKEN": "env-tok"}):
                c = arena.ArenaClient.load(Path(tmp))
                self.assertEqual(c.base_url, "http://file-base")
                self.assertEqual(c.token, "env-tok")
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(arena.ArenaError):
                arena.ArenaClient.load(None)
        with self.assertRaises(arena.ArenaError):
            arena.ArenaClient("http://x", "")

    def test_rejects_unsafe_unique_code(self):
        arena = self._arena()
        client = arena.ArenaClient("http://x", "t")
        with self.assertRaises(arena.ArenaError):
            client.start_challenge("../etc/passwd")


class ArenaErrorBridgeTests(unittest.TestCase):
    """Regression for the live 409 crash: the platform answered
    "max active challenge instances reached (3)" and the raw ArenaError
    escaped the `except FGError` guard in _bench_parallel, killing the
    whole bench. Modules loaded through sapper._load_arena_module() must
    raise ArenaError catchable as FGError."""

    def test_loaded_arena_error_subclasses_fgerror(self):
        arena = sapper._load_arena_module()
        self.assertTrue(issubclass(arena.ArenaError, sapper.FGError))

    def test_409_max_active_caught_as_fgerror(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        arena = sapper._load_arena_module()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.dumps({
                    "code": "invalid_state",
                    "message": "max active challenge instances reached (3), "
                               "please close an existing challenge before starting a new one",
                    "detail": {},
                }).encode()
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = arena.ArenaClient(
                f"http://127.0.0.1:{server.server_address[1]}", "t")
            with self.assertRaises(sapper.FGError) as ctx:
                client.start_challenge("c1")
            self.assertIn("max active", str(ctx.exception).lower())
        finally:
            server.shutdown()


class CTFyArenaClientTests(unittest.TestCase):
    """CTFy / TPCTF contract: Bearer auth, /api/v1 URL normalization,
    competition auto-select, instance start/poll/reuse, question-level
    submission, capacity pre-check, attachments/VPN and error envelopes."""

    def _arena(self):
        spec = importlib.util.spec_from_file_location(
            "_arena_ctfy_test", ROOT / "lib" / "arena.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _serve(handler):
        import threading
        from http.server import HTTPServer
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_full_ctfy_flow_against_local_server(self):
        import urllib.parse
        from http.server import BaseHTTPRequestHandler
        arena = self._arena()
        seen_auth = []
        state = {"instance": None, "start_posts": 0, "submit_bodies": []}

        challenge = {
            "id": "AUTH-1", "name": "auth one", "category": "web",
            "difficulty": "easy", "description": "d",
            "questions": [
                {"id": "flag1", "prompt": "p1", "mode": "text", "requires": []},
                {"id": "flag2", "prompt": "p2", "mode": "text",
                 "requires": ["flag1"]},
            ],
            "my_team_solved_question_ids": [], "my_team_fully_solved": False,
            "attachments": [{"name": "welcome.txt", "size": 5},
                            {"name": "team.txt", "size": 4, "per_team": True}],
            "closed": False, "scoring": "dynamic", "launch_ttl_s": 3600,
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _bytes(self, data):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _route(self):
                seen_auth.append(self.headers.get("Authorization"))
                return urllib.parse.urlparse(self.path).path

            def do_GET(self):
                path = self._route()
                if path == "/api/v1/competitions":
                    return self._json({"items": [{
                        "id": "C1", "title": "qualifier", "phase": "running",
                        "kind": "jeopardy", "is_registered": True,
                        "max_instances_per_team": 1,
                        "max_instances_per_team_effective": 1,
                    }], "total": 1, "limit": 200, "offset": 0})
                if path == "/api/v1/competitions/C1":
                    return self._json({
                        "id": "C1", "title": "qualifier", "phase": "running",
                        "max_instances_per_team_effective": 1,
                    })
                if path == "/api/v1/competitions/C1/challenges":
                    return self._json({"items": [challenge], "total": 1,
                                       "limit": 1000, "offset": 0})
                if path == "/api/v1/challenges/AUTH-1":
                    return self._json(challenge)
                if path == "/api/v1/instances":
                    items = [state["instance"]] if state["instance"] else []
                    return self._json({"items": items, "total": len(items),
                                       "limit": 1000, "offset": 0})
                if path == "/api/v1/instances/I1":
                    if not state["instance"]:
                        return self._json({"code": "instance_not_found",
                                           "detail": "gone"}, 404)
                    if state["instance"]["status"] == "starting":
                        state["instance"].update({
                            "status": "ready",
                            "entry_urls": ["http://10.0.0.5:8000"],
                            "services": [{
                                "service_type": "web", "host": "10.0.0.5",
                                "port": 80, "access": "vpn", "label": "web",
                                "path": "/", "credentials": {"user": "u", "pass": "p"},
                            }],
                            "vpn_backend": "wireguard",
                            "expires_at": 1893456000, "ttl": 3600,
                            "questions": [
                                {"id": "flag1", "prompt": "p1",
                                 "unlocked": True, "answered_correctly": False,
                                 "attempts_remaining": 3, "attempts_cap": 3},
                                {"id": "flag2", "prompt": "p2",
                                 "requires": ["flag1"], "unlocked": True,
                                 "answered_correctly": False,
                                 "attempts_remaining": 2, "attempts_cap": 2},
                            ],
                        })
                    return self._json(state["instance"])
                if path == "/api/v1/instances/I1/vpn-config":
                    return self._bytes(b"[Interface]\nPrivateKey = x\n")
                if path == "/api/v1/challenges/AUTH-1/attachments/welcome.txt":
                    return self._bytes(b"hello")
                if path == "/api/v1/challenges/AUTH-1/attachments/team.txt":
                    return self._json({
                        "code": "per_team_attachment",
                        "detail": "start an instance and use the instance route",
                    }, 409)
                if path == "/api/v1/instances/I1/attachments/team.txt":
                    return self._bytes(b"team")
                return self._json({"code": "not_found", "detail": path}, 404)

            def do_POST(self):
                path = self._route()
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                if path == "/api/v1/instances":
                    state["start_posts"] += 1
                    state["instance"] = {
                        "id": "I1", "challenge_id": "AUTH-1",
                        "competition_id": "C1", "status": "starting",
                        "entry_urls": [], "services": [], "questions": [],
                        "started_at": 1,
                    }
                    return self._json({"id": "I1", "status": "starting",
                                       "start_wait": {"queue_position": 1}}, 202)
                if path == "/api/v1/submissions":
                    state["submit_bodies"].append(body)
                    return self._json({
                        "id": "s1", "correct": True, "solve_rank": 1,
                        "solve_time_s": 60, "question_id": body["question_id"],
                        "challenge_fully_solved": False,
                        "attempts_remaining": 2, "ranked": True,
                        "unranked_reason": None,
                    })
                return self._json({"code": "not_found", "detail": path}, 404)

            def do_DELETE(self):
                path = self._route()
                if path == "/api/v1/instances/I1":
                    state["instance"]["status"] = "stopping"
                    return self._json({"status": "stopping", "id": "I1",
                                       "teardown_pending": True}, 202)
                return self._json({"code": "not_found", "detail": path}, 404)

        server = self._serve(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = arena.ArenaClient(
                    f"http://127.0.0.1:{server.server_address[1]}/", "tok",
                    platform="ctfy", competition_id="C1",
                    state_file=Path(tmp) / "arena-instances.json",
                    poll_interval=0.5, start_timeout=5,
                    vpn_dir=Path(tmp) / "vpn",
                    attachments_dir=Path(tmp) / "attachments",
                )
                self.assertEqual(client.base_url,
                                 f"http://127.0.0.1:{server.server_address[1]}/api/v1")

                chs = client.list_challenges()
                self.assertEqual(chs[0]["unique_code"], "AUTH-1")
                self.assertEqual(chs[0]["flag_count"], 2)
                self.assertFalse(chs[0]["is_completed"])

                cap = client.capacity()
                self.assertEqual((cap["cap"], cap["active"]), (1, 0))

                inst = client.start_challenge("AUTH-1")
                self.assertEqual(inst["instance_id"], "I1")
                self.assertIn("10.0.0.5:80", inst["container_addr"])
                self.assertIn("http://10.0.0.5:8000", inst["container_addr"])

                # a second spawn reuses the live instance: no extra POST
                again = client.start_challenge("AUTH-1")
                self.assertEqual(again["instance_id"], "I1")
                self.assertEqual(state["start_posts"], 1)

                # two open questions, no --question -> refuse (attempt safety)
                with self.assertRaises(arena.ArenaError) as ctx:
                    client.submit_flag("AUTH-1", "guess")
                self.assertEqual(ctx.exception.code, "question_ambiguous")
                self.assertEqual(state["submit_bodies"], [])

                res = client.submit_flag(
                    "AUTH-1", "a", question_id="flag1", answer=["a", "b"])
                self.assertTrue(res["correct"])
                self.assertEqual(res["question_id"], "flag1")
                self.assertEqual(state["submit_bodies"][-1], {
                    "instance_id": "I1", "question_id": "flag1",
                    "answer": ["a", "b"],
                })

                att = client.download_attachments("AUTH-1")
                self.assertEqual(sorted(f["name"] for f in att["files"]),
                                 ["team.txt", "welcome.txt"])
                self.assertEqual(att["errors"], [])
                self.assertEqual(
                    (Path(att["dir"]) / "welcome.txt").read_bytes(), b"hello")
                self.assertEqual(
                    (Path(att["dir"]) / "team.txt").read_bytes(), b"team")

                vpn = client.download_vpn_config("AUTH-1")
                self.assertEqual(vpn["backend"], "wireguard")
                self.assertTrue(Path(vpn["path"]).read_bytes().startswith(
                    b"[Interface]"))

                closed = client.close_challenge("AUTH-1")
                self.assertTrue(closed["closed"])
                self.assertEqual(closed["instance_id"], "I1")
                state_doc = json.loads(
                    (Path(tmp) / "arena-instances.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    state_doc["instances"]["AUTH-1"]["status"], "stopping")
        finally:
            server.shutdown()

        self.assertTrue(seen_auth)
        self.assertTrue(all(a == "Bearer tok" for a in seen_auth), seen_auth)

    def test_capacity_precheck_refuses_without_post(self):
        from http.server import BaseHTTPRequestHandler
        arena = self._arena()
        posts = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                import urllib.parse
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
                if path == "/api/v1/instances":
                    if query.get("challenge_id") == ["AUTH-2"]:
                        return self._json({"items": []})
                    return self._json({"items": [{
                        "id": "IX", "challenge_id": "OTHER",
                        "status": "ready", "questions": [],
                    }]})
                if path == "/api/v1/competitions/C1":
                    return self._json({"id": "C1",
                                       "max_instances_per_team_effective": 1})
                return self._json({"code": "not_found"}, 404)

            def do_POST(self):
                posts.append(self.path)
                return self._json({"id": "NEW", "status": "starting"})

        server = self._serve(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = arena.ArenaClient(
                    f"http://127.0.0.1:{server.server_address[1]}", "tok",
                    platform="ctfy", competition_id="C1",
                    state_file=Path(tmp) / "state.json")
                with self.assertRaises(arena.ArenaError) as ctx:
                    client.start_challenge("AUTH-2")
                self.assertEqual(ctx.exception.code, "team_capacity_exceeded")
                self.assertEqual(ctx.exception.status, 429)
                self.assertEqual(posts, [])
        finally:
            server.shutdown()

    def test_error_envelope_and_exhausted_question_guard(self):
        from http.server import BaseHTTPRequestHandler
        arena = self._arena()
        posts = []
        instance = {
            "id": "I1", "challenge_id": "AUTH-1", "status": "ready",
            "entry_urls": [], "services": [],
            "questions": [
                {"id": "flag1", "prompt": "p1", "unlocked": True,
                 "answered_correctly": False, "attempts_remaining": 2},
                {"id": "flag2", "prompt": "p2", "unlocked": True,
                 "answered_correctly": False, "attempts_remaining": 0},
            ],
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _json(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if path == "/api/v1/instances":
                    return self._json({"items": [instance]})
                if path == "/api/v1/instances/I1":
                    return self._json(instance)
                return self._json({"code": "not_found"}, 404)

            def do_POST(self):
                posts.append(self.path)
                return self._json({
                    "code": "question_locked",
                    "detail": {"message": "question flag1 is locked",
                               "requires": ["flag0"]},
                }, 403)

        server = self._serve(Handler)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                client = arena.ArenaClient(
                    f"http://127.0.0.1:{server.server_address[1]}", "tok",
                    platform="ctfy", competition_id="C1",
                    state_file=Path(tmp) / "state.json")
                # attempts_remaining == 0 is refused locally, no HTTP POST
                with self.assertRaises(arena.ArenaError) as ctx:
                    client.submit_flag("AUTH-1", "x", question_id="flag2")
                self.assertEqual(ctx.exception.code, "question_attempt_cap_exceeded")
                self.assertEqual(posts, [])
                # a platform 403 keeps its envelope code for deferral logic
                with self.assertRaises(arena.ArenaError) as ctx:
                    client.submit_flag("AUTH-1", "x", question_id="flag1")
                self.assertEqual(ctx.exception.code, "question_locked")
                self.assertEqual(ctx.exception.status, 403)
        finally:
            server.shutdown()

    def test_vpn_topology_detection_and_config_helpers(self):
        arena = self._arena()
        with tempfile.TemporaryDirectory() as tmp:
            client = arena.ArenaClient(
                "http://127.0.0.1:1", "tok", platform="ctfy",
                competition_id="C1", state_file=Path(tmp) / "state.json")
            self.assertTrue(client._needs_vpn({"network_topology": "engagement"}))
            self.assertTrue(client._needs_vpn({
                "vpn_endpoint": {"config_url": "https://example/vpn"}}))
            self.assertTrue(client._needs_vpn({"vpn_backend": "wireguard"}))
            self.assertTrue(client._needs_vpn({
                "services": [{"host": "a.internal", "access": "vpn"}],
                "entry_urls": [],
            }))
            self.assertFalse(client._needs_vpn({
                "network_topology": "simple",
                "entry_urls": ["https://public.example/"],
                "services": [{"host": "public.example", "access": None}],
            }))
            # topology=simple is authoritative over a generic vpn_backend field
            self.assertFalse(client._needs_vpn({
                "network_topology": "simple",
                "vpn_backend": "wireguard",
                "entry_urls": ["https://public.example/"],
                "services": [{"host": "public.example",
                              "access": "public_port"}],
            }))
            inst = {
                "entry_urls": ["http://a.internal:8080/x",
                               "http://a.internal/dup"],
                "services": [{"host": "a.internal"},
                             {"host": "b.internal."},
                             {"host": "public.example"}],
            }
            self.assertEqual(client._internal_hostnames(inst),
                             ["a.internal", "b.internal"])

            config = (
                "[Interface]\nPrivateKey = secret\n"
                "Address = 10.0.0.2/32\n"
                "DNS = 172.16.8.3, 1.1.1.1, search.example\n\n"
                "[Peer]\nAllowedIPs = 172.16.8.0/24\n"
            )
            self.assertEqual(client._wg_dns_servers(config),
                             ["172.16.8.3", "1.1.1.1"])
            cleaned = client._strip_wg_dns(config)
            self.assertNotIn("DNS", cleaned)
            self.assertIn("PrivateKey", cleaned)
            self.assertIn("AllowedIPs", cleaned)

    def test_download_vpn_config_skips_direct_instances(self):
        arena = self._arena()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = arena.ArenaClient(
                "http://127.0.0.1:1", "tok", platform="ctfy",
                competition_id="C1", state_file=root / "state.json")
            inst = {
                "id": "I9", "network_topology": "simple",
                "entry_urls": ["https://public.example/"],
                "services": [{"host": "public.example",
                              "access": "public_port"}],
            }
            with mock.patch.object(client, "_resolve_instance",
                                   return_value=inst), \
                 mock.patch.object(client, "_request_bytes") as raw:
                result = client.download_vpn_config("PUB-1", up=True)
            self.assertFalse(result["needs_vpn"])
            self.assertIn("skipped", result)
            self.assertIsNone(result["path"])
            raw.assert_not_called()
            state = json.loads((root / "state.json").read_text(
                encoding="utf-8"))
            self.assertFalse(state["vpn"]["PUB-1"]["up"])

    def test_vpn_up_strips_dns_maps_internal_and_records_state(self):
        arena = self._arena()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = {}
            client = arena.ArenaClient(
                "http://127.0.0.1:1", "tok", platform="ctfy",
                competition_id="C1", state_file=root / "state.json",
                vpn_dir=root / "vpn")
            config = root / "orig.conf"
            config.write_text(
                "[Interface]\nPrivateKey = not-a-real-key\n"
                "Address = 10.0.0.2/32\nDNS = 172.16.8.3\n\n"
                "[Peer]\nAllowedIPs = 172.16.8.0/24\n", encoding="utf-8")
            inst = {
                "id": "I1", "challenge_id": "AUTH-1",
                "network_topology": "engagement",
                "entry_urls": ["http://a.internal:8080/"],
                "services": [{"host": "a.internal", "port": 8080,
                              "access": "vpn"}],
                "vpn_backend": "wireguard",
                "vpn_endpoint": {"host": "vpn.example", "port": 51820},
            }
            calls = []

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return Proc()

            def fake_which(name):
                # Windows dev boxes and CI images may not ship wireguard-tools;
                # the test must not depend on the host's binaries.
                return f"/usr/bin/{name}" if name in ("wg-quick", "sudo") else None

            with mock.patch.object(client, "_run_vpn_command",
                                   side_effect=fake_run), \
                 mock.patch.object(arena.shutil, "which",
                                   side_effect=fake_which), \
                 mock.patch.object(client, "_wait_interface",
                                   return_value=True), \
                 mock.patch.object(client, "_resolve_internal_host",
                                   return_value=(["172.16.8.9"], "")), \
                 mock.patch.object(client, "_read_hosts_text",
                                   return_value="127.0.0.1 localhost\n"), \
                 mock.patch.object(client, "_write_hosts_text",
                                   side_effect=lambda text: captured.update(
                                       hosts=text)):
                result = client.vpn_up(config, instance=inst, code="AUTH-1")

            self.assertTrue(result["ok"])
            stem = "sapper-" + hashlib.sha1(b"AUTH-1").hexdigest()[:8]
            self.assertEqual(Path(result["effective_path"]).name,
                             stem + ".conf")
            effective = Path(result["effective_path"]).read_text(
                encoding="utf-8")
            self.assertNotIn("DNS", effective)
            self.assertIn("AllowedIPs", effective)
            # the downloaded original must stay untouched for re-import
            self.assertIn("DNS = 172.16.8.3",
                          config.read_text(encoding="utf-8"))
            self.assertIn("# BEGIN sapper-vpn I1", captured["hosts"])
            self.assertIn("172.16.8.9 a.internal", captured["hosts"])
            self.assertEqual(result["dns_servers"], ["172.16.8.3"])
            self.assertTrue(any("wg-quick" in " ".join(cmd)
                                for cmd in calls))
            state = json.loads((root / "state.json").read_text(
                encoding="utf-8"))
            self.assertTrue(state["vpn"]["AUTH-1"]["up"])
            self.assertEqual(state["vpn"]["AUTH-1"]["interface"], stem)

    def test_vpn_up_reuses_live_tunnel_for_the_same_instance(self):
        arena = self._arena()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = arena.ArenaClient(
                "http://127.0.0.1:1", "tok", platform="ctfy",
                competition_id="C1", state_file=root / "state.json",
                vpn_dir=root / "vpn")
            stem = "sapper-" + hashlib.sha1(b"AUTH-1").hexdigest()[:8]
            effective = root / "vpn" / ".up" / f"{stem}.conf"
            client._remember_vpn("AUTH-1", {
                "up": True, "instance_id": "I1", "backend": "wireguard",
                "interface": stem, "hosts_tag": "I1",
                "effective_path": str(effective),
                "config_path": str(root / "vpn" / f"{stem}.conf"),
            })
            config = root / "orig.conf"
            config.write_text("[Interface]\nPrivateKey = x\n\n[Peer]\n"
                              "AllowedIPs = 172.16.8.0/24\n", encoding="utf-8")
            calls = []

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return Proc()

            with mock.patch.object(client, "_iface_exists",
                                   return_value=True), \
                 mock.patch.object(arena.shutil, "which",
                                   side_effect=lambda name: f"/usr/bin/{name}"), \
                 mock.patch.object(client, "_run_vpn_command",
                                   side_effect=fake_run):
                result = client.vpn_up(config, instance={"id": "I1"},
                                       code="AUTH-1")

            self.assertTrue(result["already_up"])
            self.assertTrue(result["up"])
            self.assertEqual(calls, [])
            # the cleaned copy is refreshed even when the tunnel is reused
            self.assertTrue(effective.exists())

    def test_vpn_up_replaces_stale_tunnel_from_an_older_instance(self):
        arena = self._arena()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = arena.ArenaClient(
                "http://127.0.0.1:1", "tok", platform="ctfy",
                competition_id="C1", state_file=root / "state.json",
                vpn_dir=root / "vpn")
            stem = "sapper-" + hashlib.sha1(b"AUTH-1").hexdigest()[:8]
            effective = root / "vpn" / ".up" / f"{stem}.conf"
            client._remember_vpn("AUTH-1", {
                "up": True, "instance_id": "I0", "backend": "wireguard",
                "interface": stem, "hosts_tag": "I0",
                "effective_path": str(effective),
                "config_path": str(root / "vpn" / f"{stem}.conf"),
            })
            config = root / "orig.conf"
            config.write_text("[Interface]\nPrivateKey = x\n\n[Peer]\n"
                              "AllowedIPs = 172.16.9.0/24\n", encoding="utf-8")
            calls = []

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return Proc()

            with mock.patch.object(client, "_iface_exists",
                                   return_value=True), \
                 mock.patch.object(arena.shutil, "which",
                                   side_effect=lambda name: f"/usr/bin/{name}"), \
                 mock.patch.object(client, "_run_vpn_command",
                                   side_effect=fake_run), \
                 mock.patch.object(client, "_wait_interface",
                                   return_value=True):
                result = client.vpn_up(config, instance={"id": "I1"},
                                       code="AUTH-1")

            sequences = [" ".join(cmd) for cmd in calls]
            down_at = next(i for i, s in enumerate(sequences) if " down " in s)
            up_at = next(i for i, s in enumerate(sequences) if " up " in s)
            self.assertLess(down_at, up_at)
            self.assertFalse(result.get("already_up"))
            state = json.loads((root / "state.json").read_text(
                encoding="utf-8"))
            self.assertEqual(state["vpn"]["AUTH-1"]["instance_id"], "I1")

    def test_vpn_down_removes_marked_hosts_block_and_closes_state(self):
        arena = self._arena()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            captured = {}
            client = arena.ArenaClient(
                "http://127.0.0.1:1", "tok", platform="ctfy",
                competition_id="C1", state_file=root / "state.json",
                vpn_dir=root / "vpn")
            stem = "sapper-" + hashlib.sha1(b"AUTH-1").hexdigest()[:8]
            client._remember_vpn("AUTH-1", {
                "up": True, "instance_id": "I1", "backend": "wireguard",
                "interface": stem, "hosts_tag": "I1",
                "config_path": str(root / "orig.conf"),
            })
            hosts_before = (
                "127.0.0.1 localhost\n"
                "# BEGIN sapper-vpn I1\n"
                "172.16.8.9 a.internal\n"
                "# END sapper-vpn I1\n"
                "# keep-tail\n"
            )
            calls = []

            class Proc:
                returncode = 0
                stdout = ""
                stderr = ""

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return Proc()

            def fake_which(name):
                return f"/usr/bin/{name}" if name in ("wg-quick", "sudo") else None

            with mock.patch.object(client, "_run_vpn_command",
                                   side_effect=fake_run), \
                 mock.patch.object(arena.shutil, "which",
                                   side_effect=fake_which), \
                 mock.patch.object(client, "_read_hosts_text",
                                   return_value=hosts_before), \
                 mock.patch.object(client, "_write_hosts_text",
                                   side_effect=lambda text: captured.update(
                                       hosts=text)):
                result = client.vpn_down(code="AUTH-1")

            self.assertTrue(result["ok"])
            self.assertFalse(result["up"])
            self.assertNotIn("a.internal", captured["hosts"])
            self.assertIn("# keep-tail", captured["hosts"])
            self.assertTrue(any("wg-quick" in " ".join(cmd) and "down" in cmd
                                for cmd in calls))
            state = json.loads((root / "state.json").read_text(
                encoding="utf-8"))
            self.assertFalse(state["vpn"]["AUTH-1"]["up"])

    def test_load_ctfy_env_and_template(self):
        arena = self._arena()
        env = {"SAPPER_PLATFORM": "ctfy",
               "CTFY_BASE_URL": "https://ctfy.example.test",
               "CTFY_TOKEN": "tok", "CTFY_COMPETITION_ID": "C1"}
        with mock.patch.dict("os.environ", env, clear=True):
            c = arena.ArenaClient.load(None)
            self.assertEqual(c.platform, "ctfy")
            self.assertEqual(c.base_url, "https://ctfy.example.test/api/v1")
            self.assertEqual(c.competition_id, "C1")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", env, clear=True):
                path = arena.ArenaClient.write_template(Path(tmp))
                cfg = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(cfg["platform"], "ctfy")
                self.assertEqual(cfg["token"], "ENV:CTFY_TOKEN")


class CommanderTests(unittest.TestCase):
    def test_parse_command_payload(self):
        ok = {"accepted": True, "data": {"action": "work", "target": "c03", "minutes": 25, "note": "close"}}
        self.assertEqual(sapper._parse_command_payload(ok), ("work", "c03", 25, "close"))
        stop = {"accepted": True, "data": {"action": "stop", "reason": "all done"}}
        self.assertEqual(sapper._parse_command_payload(stop), ("stop", "", 0, "all done"))
        rej = {"accepted": False, "reason": "bad board"}
        self.assertEqual(sapper._parse_command_payload(rej)[0], "reject")
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hack"}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "work", "minutes": 10}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "work", "target": "c1", "minutes": 0}})

    def test_board_summary_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("c01", "c02"):
                store = sapper.Store(root / name / ".sapper")
                store.init("t", f"flag {name}")
            sapper.Store(root / "c01" / ".sapper").add_fact(
                "flag captured: flag{a}", "agent",
                kind="flag", data={"value": "flag{a}", "challenge": "c01", "status": "submitted"})
            rows = sapper._board_summary(root)
            self.assertEqual([r["name"] for r in rows], ["c01", "c02"])
            by_name = {r["name"]: r for r in rows}
            self.assertEqual(by_name["c01"]["flags_submitted"], 1)
            self.assertEqual(by_name["c02"]["flags_submitted"], 0)
            board = sapper.render_board(rows)
            self.assertIn("c01", board)
            self.assertIn("flags(sub/rec)", board)


class FleetTests(unittest.TestCase):
    def test_fleet_pane_commands(self):
        cfg = {"session": "s", "root": "runs", "deadline": "2026-10-17T13:00:00",
               "panes": []}
        engine = sapper._sapper_engine_prefix()
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "command"}),
            f"{engine} command --root runs --deadline 2026-10-17T13:00:00")
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "auto:c02", "extra_args": "--verify"}),
            f"{engine} --store runs/c02/.sapper auto --deadline 2026-10-17T13:00:00 --verify")
        self.assertEqual(
            sapper._fleet_pane_command(cfg, {"type": "auto:c02"}, base_dir=Path("/w")),
            f"cd /w && {engine} --store runs/c02/.sapper auto --deadline 2026-10-17T13:00:00")
        self.assertEqual(
            sapper._fleet_pane_command({"root": "runs", "panes": []}, {"type": "shell", "command": "top"}),
            "top")
        with self.assertRaises(sapper.FGError):
            sapper._fleet_pane_command(cfg, {"type": "warp"})
        with self.assertRaises(sapper.FGError):
            sapper._fleet_pane_command(cfg, {"type": "shell"})

    def test_fleet_init_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path.cwd()
            try:
                import os
                os.chdir(tmp)
                args = type("A", (), {"action": "init", "file": "fleet.json"})()
                self.assertEqual(sapper.cmd_fleet(args), 0)
                cfg = sapper._fleet_load(Path("fleet.json"))
                self.assertTrue(cfg["panes"])
                args2 = type("A", (), {"action": "init", "file": "fleet.json"})()
                with self.assertRaises(sapper.FGError):
                    sapper.cmd_fleet(args2)
            finally:
                os.chdir(old)


class HireFireTests(unittest.TestCase):
    def test_parse_command_payload_hire_fire(self):
        hire = {"accepted": True, "data": {"action": "hire", "target": "c07", "minutes": 30}}
        self.assertEqual(sapper._parse_command_payload(hire), ("hire", "c07", 30, ""))
        fire = {"accepted": True, "data": {"action": "fire", "target": "c07", "note": "stuck"}}
        self.assertEqual(sapper._parse_command_payload(fire), ("fire", "c07", 0, "stuck"))
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "warp", "target": "c"}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hire", "minutes": 10}})
        with self.assertRaises(sapper.FGError):
            sapper._parse_command_payload({"accepted": True, "data": {"action": "hire", "target": "c", "minutes": -1}})

    def test_active_workers_parsing(self):
        with mock.patch.object(sapper, "_tmux") as tm:
            tm.return_value = mock.Mock(returncode=1)
            self.assertEqual(sapper._active_workers("nope"), set())
            tm.return_value = mock.Mock(returncode=0, stdout="commander\nw-c01\nw-c07\nboard\n")
            self.assertEqual(sapper._active_workers("s"), {"c01", "c07"})

    def test_board_render_worker_column(self):
        rows = [
            {"name": "c01", "status": "active", "flags_submitted": 1, "flags_recorded": 0,
             "kinds": "1 flag", "open_intents": 2, "last_activity": "t", "goal": "g", "worker": "yes"},
            {"name": "c02", "status": "active", "flags_submitted": 0, "flags_recorded": 0,
             "kinds": "-", "open_intents": 0, "last_activity": "t", "goal": "g", "worker": "-"},
        ]
        board = sapper.render_board(rows)
        self.assertIn("worker", board)
        self.assertIn("worker=yes means", board)


import os  # noqa: E402  (KeyRotationTests)


class KeyRotationTests(unittest.TestCase):
    def test_quota_matcher(self):
        self.assertTrue(sapper._is_quota_error("insufficient balance (quota exceeded)"))
        self.assertTrue(sapper._is_quota_error("Error 401: Unauthorized"))
        self.assertTrue(sapper._is_quota_error("账户欠费，请充值"))
        self.assertFalse(sapper._is_quota_error("connection timed out"))
        self.assertFalse(sapper._is_quota_error("claude exited 2: bad args"))

    def test_pool_load_and_rotate(self):
        pool_json = json.dumps([
            {"base_url": "http://a", "token": "t1", "model": "m1"},
            {"base_url": "http://b", "token": "t2", "model": "m2"},
        ])
        with mock.patch.dict("os.environ", {
            "SAPPER_KEY_POOL": pool_json, "ANTHROPIC_AUTH_TOKEN": "t1",
            "ANTHROPIC_BASE_URL": "http://a", "SAPPER_MODEL": "m1"}):
            self.assertTrue(sapper._rotate_key())
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "t2")
            self.assertEqual(os.environ["ANTHROPIC_BASE_URL"], "http://b")
            self.assertEqual(os.environ["SAPPER_MODEL"], "m2")
        with mock.patch.dict("os.environ", {"SAPPER_KEY_POOL": "not json"}):
            self.assertEqual(sapper._load_key_pool(), [])
        with mock.patch.dict("os.environ", {"SAPPER_KEY_POOL": ""}):
            self.assertFalse(sapper._rotate_key())
        # single-entry pool: rotate returns True but keeps the key
        with mock.patch.dict("os.environ", {
            "SAPPER_KEY_POOL": json.dumps([{"base_url": "http://a", "token": "t1"}]),
            "ANTHROPIC_AUTH_TOKEN": "t1"}):
            self.assertTrue(sapper._rotate_key())
            self.assertEqual(os.environ["ANTHROPIC_AUTH_TOKEN"], "t1")


class HookExecutionTests(unittest.TestCase):
    """Regression for #2: the hook script itself must run and emit valid JSON.

    SettingsPatchTests only asserted the command string was written; the hook
    binary was never executed (its fg->sapper rename import bug shipped silently
    because Claude Code swallows hook stderr).
    """

    def _run_hook(self, event, cwd, stdin=None):
        import subprocess
        proc = subprocess.run(
            [sys.executable, str(ROOT / "lib" / "sapper-hook.py"), event],
            capture_output=True, text=True, timeout=30,
            cwd=cwd, input=stdin,
        )
        return proc

    def test_sessionstart_and_userpromptsubmit_execute_and_emit_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = sapper.Store(Path(tmp) / ".sapper")
            store.init("hooktest", "verify hook executes")
            for event, stdin in (("sessionstart", None),
                                 ("userpromptsubmit", '{"prompt": "hi"}')):
                proc = self._run_hook(event, tmp, stdin)
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                payload = json.loads(proc.stdout)
                outer = payload["hookSpecificOutput"]
                self.assertEqual(outer["hookEventName"].lower(), event)
                ctx = outer.get("additionalContext") or outer.get("contextForClaude")
                self.assertIn("fact-graph", ctx)
