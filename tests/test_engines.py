"""Phase 1 engine-contract regression tests.

All subprocess calls are simulated: no provider, target host or network is
contacted.  The tests pin the *legacy* Claude behaviour while it is being
extracted into ClaudeEngine.
"""
import ast
import contextlib
import io
import inspect
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import sapper  # noqa: E402
from engines import ClaudeEngine, EngineRequest  # noqa: E402


def _ok(payload=None, **extra):
    body = {"result": "ok", "session_id": "ses_test"}
    if payload is not None:
        body = dict(payload)
    body.update(extra)
    return subprocess.CompletedProcess(
        args=["claude"], returncode=0,
        stdout=json.dumps(body), stderr="",
    )


class ClaudeEngineArgvTests(unittest.TestCase):
    def test_legacy_argv_and_stdin_are_unchanged(self):
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "0"}):
            with mock.patch.object(sapper.subprocess, "run", return_value=_ok()) as run:
                envelope = sapper.run_engine(
                    "PROMPT", model="sonnet", timeout=7,
                    skip_permissions=False, append_system=None,
                    cwd=Path("/tmp/work"),
                )
        run.assert_called_once_with(
            ["claude", "-p", "--output-format", "json", "--model", "sonnet"],
            input="PROMPT", capture_output=True, text=True,
            timeout=7, cwd="/tmp/work",
        )
        self.assertEqual(envelope["result"], "ok")
        self.assertEqual(envelope["session_id"], "ses_test")

    def test_skip_permissions_and_append_system_flags_preserved(self):
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "0"}):
            with mock.patch.object(sapper.subprocess, "run", return_value=_ok()) as run:
                sapper.run_engine(
                    "P", model="m", timeout=1,
                    skip_permissions=True, append_system="TRUSTED RULES",
                    cwd=Path("."),
                )
        argv = run.call_args.args[0]
        self.assertEqual(argv, [
            "claude", "-p", "--output-format", "json", "--model", "m",
            "--dangerously-skip-permissions",
            "--append-system-prompt", "TRUSTED RULES",
        ])

    def test_sapper_bin_prefix_is_still_split_and_respected(self):
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "python3 bin/zai-wrapper",
                                            "SAPPER_RETRY_MODEL": "0"}):
            with mock.patch.object(sapper.subprocess, "run", return_value=_ok()) as run:
                sapper.run_engine(
                    "P", model="m", timeout=1, skip_permissions=False,
                    append_system=None, cwd=Path("."),
                )
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["python3", "bin/zai-wrapper"])
        self.assertEqual(argv[2:], ["-p", "--output-format", "json", "--model", "m"])

    def test_envelope_extra_fields_are_preserved(self):
        payload = {"result": "x", "cost_usd": 0.1, "duration_ms": 12,
                   "session_id": "ses_1", "extra": {"nested": True}}
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "0"}):
            with mock.patch.object(sapper.subprocess, "run", return_value=_ok(payload)):
                envelope = sapper.run_engine(
                    "P", model="m", timeout=1, skip_permissions=False,
                    append_system=None, cwd=Path("."),
                )
        self.assertEqual(envelope, payload)
        self.assertEqual(envelope["extra"], {"nested": True})


class ContractTests(unittest.TestCase):
    def test_engine_result_to_envelope_preserves_all_fields(self):
        payload = {"result": "r", "subtype": "success", "cost_usd": 0.01}
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "0"}):
            with mock.patch.object(sapper.subprocess, "run", return_value=_ok(payload)):
                returned = sapper.run_engine(
                    "P", model="m", timeout=1, skip_permissions=False,
                    append_system=None, cwd=Path("."),
                )
        self.assertEqual(returned, payload)

    def test_engine_request_accepts_reserved_session_without_argv_change(self):
        seen = {}

        def fake_run_once(argv, prompt, timeout, cwd, engine):
            seen["argv"] = list(argv)
            return {"result": "ok"}

        engine = ClaudeEngine(
            run_once=fake_run_once,
            is_quota_error=sapper._is_quota_error,
            rotate_key=lambda: False,
            error_cls=sapper.FGError,
        )
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "0"}):
            result = engine.run(EngineRequest(
                prompt="P", model="m", timeout=1, skip_permissions=False,
                append_system=None, cwd=Path("."), session_id="ses_reserved",
            ))
        self.assertEqual(result.result, "ok")
        self.assertNotIn("ses_reserved", seen["argv"])

    def test_engines_module_uses_stdlib_only(self):
        allowed = {"__future__", "dataclasses", "os", "pathlib", "sys",
                   "time", "typing", "opencode_engine"}  # internal module, lazy import
        tree = ast.parse((ROOT / "lib" / "engines.py").read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertTrue(roots.issubset(allowed), f"unexpected imports: {roots - allowed}")


class RunClaudeCompatibilityTests(unittest.TestCase):
    def test_run_claude_delegates_with_every_argument(self):
        sentinel = {"result": "alias-ok"}
        with mock.patch.object(sapper, "run_engine", return_value=sentinel) as call:
            got = sapper.run_claude(
                "P", model="m", timeout=9, skip_permissions=True,
                append_system="SYS", cwd=Path("/x"),
            )
        call.assert_called_once_with(
            "P", model="m", timeout=9, skip_permissions=True,
            append_system="SYS", cwd=Path("/x"),
        )
        self.assertIs(got, sentinel)

    def test_run_claude_signature_matches_run_engine(self):
        self.assertEqual(
            list(inspect.signature(sapper.run_claude).parameters),
            list(inspect.signature(sapper.run_engine).parameters),
        )

    def test_all_existing_dispatch_call_sites_are_untouched(self):
        targets = [
            sapper.cmd_command,
            sapper._dispatch_reason,
            sapper._dispatch_explore,
            sapper._dispatch_verify,
            sapper._dispatch_blindspot,
            sapper._dispatch_brainstorm,
            sapper._run_reason_raw,
            sapper._run_explore_raw,
            sapper._run_verify_raw,
        ]
        for func in targets:
            with self.subTest(func=func.__name__):
                src = inspect.getsource(func)
                self.assertTrue(
                    "run_engine(" in src or "run_claude(" in src,
                    f"{func.__name__} no longer dispatches through the engine API",
                )


class ErrorAndRetryTests(unittest.TestCase):
    def _call(self, **overrides):
        kwargs = dict(prompt="P", model="m", timeout=5,
                      skip_permissions=False, append_system=None, cwd=Path("."))
        kwargs.update(overrides)
        # Do not pin SAPPER_RETRY_MODEL here: each test controls it so the
        # retry/rotation cases can exercise attempts > 1.
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude"}):
            return sapper.run_engine(**kwargs)

    def test_validation_messages_preserved(self):
        with mock.patch.object(sapper.subprocess, "run") as run:
            with self.assertRaisesRegex(sapper.FGError,
                                        "claude timeout must be at least 1 second"):
                self._call(timeout=0)
            with self.assertRaisesRegex(sapper.FGError,
                                        "claude model cannot be empty"):
                self._call(model="   ")
        run.assert_not_called()

    def test_response_error_paths_preserved(self):
        cases = [
            (subprocess.CompletedProcess([], 0, "not-json", ""), "did not return JSON"),
            (_ok({"result": "bad", "is_error": True}), "reported error"),
            (_ok({"result": 123}), "non-string result"),
            (subprocess.CompletedProcess([], 0, "[1,2]", ""),
             "JSON value instead of an object envelope"),
            (subprocess.CompletedProcess([], 3, "", "boom"), "exited 3: boom"),
            (subprocess.TimeoutExpired(["claude"], 5), "timed out after 5s"),
        ]
        for proc, expected in cases:
            with self.subTest(expected=expected):
                patcher = mock.patch.object(
                    sapper.subprocess, "run",
                    side_effect=proc if isinstance(proc, Exception) else None,
                    return_value=None if isinstance(proc, Exception) else proc,
                )
                with patcher:
                    with mock.patch.dict("os.environ", {"SAPPER_RETRY_MODEL": "0"}):
                        with self.assertRaisesRegex(sapper.FGError, expected):
                            self._call()

    def test_cli_not_found_is_not_retried(self):
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "3"}):
            with mock.patch.object(sapper.subprocess, "run",
                                   side_effect=FileNotFoundError()) as run:
                with self.assertRaisesRegex(sapper.FGError, "CLI not found"):
                    self._call()
        self.assertEqual(run.call_count, 1)

    def test_retry_backoff_formula_preserved(self):
        failures = [
            subprocess.CompletedProcess([], 2, "", "bad-1"),
            subprocess.CompletedProcess([], 2, "", "bad-2"),
        ]
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "2"}):
            with mock.patch.object(sapper.subprocess, "run",
                                   side_effect=failures + [_ok()]) as run:
                with mock.patch.object(sapper.time, "sleep") as sleep:
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        envelope = self._call()
        self.assertEqual(envelope["result"], "ok")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(10), mock.call(20)])
        self.assertIn("attempt 1/3", err.getvalue())
        self.assertIn("attempt 2/3", err.getvalue())

    def test_quota_error_rotates_key_and_retries_without_sleep(self):
        quota_error = subprocess.CompletedProcess([], 1, "", "insufficient balance")
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "2"}):
            with mock.patch.object(sapper.subprocess, "run",
                                   side_effect=[quota_error, _ok()]) as run:
                with mock.patch.object(sapper, "_rotate_key", return_value=True) as rot:
                    with mock.patch.object(sapper.time, "sleep") as sleep:
                        with contextlib.redirect_stderr(io.StringIO()) as err:
                            envelope = self._call()
        self.assertEqual(envelope["result"], "ok")
        self.assertEqual(run.call_count, 2)
        rot.assert_called_once_with()
        sleep.assert_not_called()
        self.assertIn("key rotated", err.getvalue())

    def test_exhausted_retries_reraise_last_error(self):
        failure = subprocess.CompletedProcess([], 2, "", "still bad")
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "1"}):
            with mock.patch.object(sapper.subprocess, "run",
                                   side_effect=[failure, failure]) as run:
                with mock.patch.object(sapper.time, "sleep"):
                    with self.assertRaisesRegex(sapper.FGError, "still bad"):
                        self._call()
        self.assertEqual(run.call_count, 2)


class EnvelopeCompatibilityTests(unittest.TestCase):
    """Regression coverage for safe access to envelope['result'] (Phase 1 fix).

    The runner is injected directly so the adapter's own validation is tested;
    no provider, host or network is involved.
    """

    def _engine_returning(self, envelope, *, sleep=None):
        def fake_run_once(argv, prompt, timeout, cwd, engine):
            return envelope

        return ClaudeEngine(
            run_once=fake_run_once,
            is_quota_error=lambda message: False,
            rotate_key=lambda: False,
            error_cls=sapper.FGError,
            sleep=sleep if sleep is not None else mock.Mock(),
        )

    def _run(self, envelope):
        engine = self._engine_returning(envelope)
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "0"}):
            return engine.run(EngineRequest(
                prompt="P", model="m", timeout=1, skip_permissions=False,
                append_system=None, cwd=Path("."),
            ))

    def test_valid_result_is_preserved(self):
        payload = {"result": "answer", "session_id": "ses_1"}
        out = self._run(payload)
        self.assertEqual(out.result, "answer")
        self.assertIs(out.to_envelope(), payload)

    def test_missing_result_returns_empty_string(self):
        payload = {"subtype": "success"}
        out = self._run(payload)
        self.assertEqual(out.result, "")
        self.assertEqual(out.to_envelope(), payload)

    def test_empty_result_is_preserved(self):
        payload = {"result": ""}
        out = self._run(payload)
        self.assertEqual(out.result, "")
        self.assertEqual(out.to_envelope(), payload)

    def test_non_dict_envelope_raises_legacy_error(self):
        with self.assertRaisesRegex(
            sapper.FGError, "returned a JSON value instead of an object envelope"
        ):
            self._run([1, 2])

    def test_unexpected_result_type_raises_legacy_error(self):
        for bad in (123, None, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(
                    sapper.FGError, "response envelope has a non-string result"
                ):
                    self._run({"result": bad})

    def test_run_engine_without_result_no_longer_raises_keyerror(self):
        proc = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"subtype":"success"}', stderr=""
        )
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "0"}), \
             mock.patch.object(sapper.subprocess, "run", return_value=proc):
            envelope = sapper.run_engine(
                "P", model="m", timeout=1, skip_permissions=False,
                append_system=None, cwd=Path("."),
            )
        self.assertEqual(envelope, {"subtype": "success"})

    def test_validation_error_is_retried_like_the_legacy_runner(self):
        calls = []

        def fake_run_once(argv, prompt, timeout, cwd, engine):
            calls.append(1)
            return {"result": 7} if len(calls) == 1 else {"result": "ok"}

        sleep = mock.Mock()
        engine = ClaudeEngine(
            run_once=fake_run_once,
            is_quota_error=lambda message: False,
            rotate_key=lambda: False,
            error_cls=sapper.FGError,
            sleep=sleep,
        )
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_RETRY_MODEL": "1"}), \
             contextlib.redirect_stderr(io.StringIO()):
            out = engine.run(EngineRequest(
                prompt="P", model="m", timeout=1, skip_permissions=False,
                append_system=None, cwd=Path("."),
            ))
        self.assertEqual(out.result, "ok")
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
