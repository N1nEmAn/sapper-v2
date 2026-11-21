"""Offline tests for the OpenCode engine (Phase 2).

Every process event is synthetic: ``Popen`` is injected, the clock is injected
and no provider, target host or network is contacted.
"""
import ast
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import sapper  # noqa: E402
from engines import ClaudeEngine, EngineRequest, select_engine  # noqa: E402
from opencode_engine import OpenCodeEngine  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "opencode_events.ndjson"


def _fixture_lines():
    return FIXTURE.read_text(encoding="utf-8").splitlines()


def _line(event):
    return json.dumps(event)


class _Lines:
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def close(self):
        pass


class _FakePopen:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = 0
        self.exit_code = kwargs.pop("_exit", 0)
        self.expire_after_terminate = kwargs.pop("_expire", False)
        self.stdout = _Lines(kwargs.pop("_stdout", []))
        self.stderr = _Lines(kwargs.pop("_stderr", []))
        self.wait_timeouts = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.terminate_calls and not self.kill_calls and self.expire_after_terminate:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = self.exit_code
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1


def _popen_factory(lines, *, stderr=(), exit_code=0, expire=False):
    created = []

    def factory(argv, **kwargs):
        kwargs.update(
            _stdout=lines, _stderr=stderr, _exit=exit_code, _expire=expire
        )
        proc = _FakePopen(argv, **kwargs)
        created.append(proc)
        return proc

    return factory, created


class _Clock:
    def __init__(self, values):
        self.values = list(values)
        self.index = 0

    def __call__(self):
        if self.index < len(self.values):
            value = self.values[self.index]
        else:
            value = self.values[-1] if self.values else 0.0
        self.index += 1
        return value


def _request(**overrides):
    base = dict(
        prompt="PROMPT", model="sonnet", timeout=5,
        skip_permissions=False, append_system=None, cwd=Path("."),
    )
    base.update(overrides)
    return EngineRequest(**base)


def _engine(popen, *, clock=None):
    return OpenCodeEngine(
        error_cls=sapper.FGError,
        popen=popen,
        clock=clock if clock is not None else (lambda: 0.0),
    )


_ENV_CLEAN = {
    "SAPPER_OPENCODE_MODEL": "",
    "SAPPER_OPENCODE_AGENT": "",
    "SAPPER_OPENCODE_SESSION": "",
    "SAPPER_OPENCODE_ARCHIVED": "",
    "SAPPER_OPENCODE_STANDALONE": "",
    "SAPPER_OPENCODE_BIN": "opencode",
    "SAPPER_ENGINE": "claude",
}


class NormalizationTests(unittest.TestCase):
    def test_synthetic_fixture_yields_final_text_and_main_session(self):
        factory, created = _popen_factory(_fixture_lines())
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            result = _engine(factory).run(_request())
        self.assertEqual(result.result, "final answer\nfallback only")
        self.assertEqual(result.session_id, "ses_main")
        self.assertEqual(result.to_envelope(), {
            "result": "final answer\nfallback only",
            "sessionID": "ses_main",
        })
        self.assertEqual(len(created), 1)

    def test_real_opencode_text_event_returns_answer(self):
        lines = [
            _line({
                "type": "step_start",
                "sessionID": "ses_test",
            }),
            _line({
                "type": "text",
                "sessionID": "ses_test",
                "part": {
                    "id": "part_test",
                    "sessionID": "ses_test",
                    "messageID": "msg_test",
                    "type": "text",
                    "text": "INTEGRACION SAPPER OPENCODE OK",
                },
                "messageID": "msg_test",
            }),
            _line({
                "type": "step_finish",
                "sessionID": "ses_test",
            }),
        ]

        factory, _ = _popen_factory(lines)

        with mock.patch.dict("os.environ", _ENV_CLEAN):
            result = _engine(factory).run(_request())

        self.assertEqual(
            result.result,
            "INTEGRACION SAPPER OPENCODE OK",
        )
        self.assertEqual(result.session_id, "ses_test")
        self.assertEqual(result.to_envelope(), {
            "result": "INTEGRACION SAPPER OPENCODE OK",
            "sessionID": "ses_test",
        })

    def test_no_text_events_returns_empty_result(self):
        lines = [
            _line({"type": "session.created", "sessionID": "ses_empty"}),
            _line({"type": "session.idle", "sessionID": "ses_empty"}),
        ]
        factory, _ = _popen_factory(lines)
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            result = _engine(factory).run(_request())
        self.assertEqual(result.result, "")
        self.assertEqual(result.session_id, "ses_empty")

    def test_error_event_is_controlled_failure(self):
        lines = [_line({"type": "error", "sessionID": "ses_e",
                        "error": {"type": "unknown", "message": "boom"}})]
        factory, _ = _popen_factory(lines)
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(sapper.FGError, "opencode reported error: boom"):
                _engine(factory).run(_request())

    def test_execution_failed_is_controlled_failure(self):
        lines = [_line({"type": "session.execution.failed", "sessionID": "ses_e",
                        "data": {"error": "provider down"}})]
        factory, _ = _popen_factory(lines)
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(sapper.FGError, "opencode execution failed"):
                _engine(factory).run(_request())

    def test_interrupted_process_is_reported(self):
        lines = [_line({"type": "session.execution.interrupted",
                        "sessionID": "ses_i", "data": {"reason": "user abort"}})]
        factory, _ = _popen_factory(lines)
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(sapper.FGError, "dispatch interrupted: user abort"):
                _engine(factory).run(_request())

    def test_permission_asked_without_reply_fails_fast(self):
        lines = [
            _line({"type": "permission.asked", "sessionID": "ses_p",
                   "permissionID": "p1"}),
            _line({"type": "session.text.ended", "sessionID": "ses_p",
                   "data": {"assistantMessageID": "m1", "text": "never"}}),
        ]
        factory, _ = _popen_factory(lines)
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(sapper.FGError, "requires permission approval"):
                _engine(factory).run(_request())

    def test_permission_replied_allows_completion(self):
        lines = [
            _line({"type": "permission.asked", "sessionID": "ses_p",
                   "permissionID": "p1"}),
            _line({"type": "permission.replied", "sessionID": "ses_p",
                   "permissionID": "p1"}),
            _line({"type": "session.text.ended", "sessionID": "ses_p",
                   "data": {"assistantMessageID": "m1", "text": "allowed"}}),
        ]
        factory, _ = _popen_factory(lines)
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            result = _engine(factory).run(_request())
        self.assertEqual(result.result, "allowed")

    def test_invalid_json_line_fails_like_claude(self):
        factory, _ = _popen_factory(["not-json\n"])
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(sapper.FGError, "did not return JSON"):
                _engine(factory).run(_request())

    def test_non_dict_event_fails_like_claude(self):
        factory, _ = _popen_factory(["[1,2]\n"])
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(
                sapper.FGError, "returned a JSON value instead of an object envelope"
            ):
                _engine(factory).run(_request())


class ProcessHandlingTests(unittest.TestCase):
    def test_argv_uses_own_config_and_no_bypass_flags(self):
        env = dict(_ENV_CLEAN)
        env.update({
            "SAPPER_OPENCODE_MODEL": "anthropic/claude-sonnet-5-5",
            "SAPPER_OPENCODE_AGENT": "sapper",
            "SAPPER_OPENCODE_SESSION": "ses_x",
            "SAPPER_OPENCODE_ARCHIVED": "1",
            "SAPPER_OPENCODE_STANDALONE": "1",
        })
        factory, created = _popen_factory([])
        with mock.patch.dict("os.environ", env), \
             contextlib.redirect_stderr(io.StringIO()):
            _engine(factory).run(_request(skip_permissions=True))
        argv = created[0].argv
        self.assertEqual(argv[:4], ["opencode", "run", "--format", "json"])
        for flag in ("--model", "anthropic/claude-sonnet-5-5", "--agent", "sapper",
                     "--session", "ses_x", "--archived", "--standalone", "PROMPT"):
            self.assertIn(flag, argv)
        for forbidden in ("--auto", "--dangerously-skip-permissions", "-p",
                          "--output-format", "sonnet"):
            self.assertNotIn(forbidden, argv)

    def test_no_opencode_model_config_ignores_request_model(self):
        factory, created = _popen_factory([])
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            _engine(factory).run(_request(model="glm-5.3-flash[1m]"))
        argv = created[0].argv
        self.assertNotIn("--model", argv)
        self.assertNotIn("glm-5.3-flash[1m]", argv)

    def test_append_system_is_delimited_in_prompt(self):
        factory, created = _popen_factory([])
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            _engine(factory).run(_request(append_system="TRUSTED RULES"))
        prompt = created[0].argv[-1]
        self.assertIn("<<TRUSTED SYSTEM INSTRUCTIONS>>", prompt)
        self.assertIn("TRUSTED RULES", prompt)
        self.assertIn("<<END TRUSTED SYSTEM INSTRUCTIONS>>", prompt)
        self.assertIn("PROMPT", prompt)

    def test_skip_permissions_emits_warning_but_no_flag(self):
        factory, created = _popen_factory([])
        with mock.patch.dict("os.environ", _ENV_CLEAN), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            _engine(factory).run(_request(skip_permissions=True))
        self.assertIn("ignores --skip-permissions", err.getvalue())
        self.assertNotIn("--auto", created[0].argv)
        self.assertNotIn("--dangerously-skip-permissions", created[0].argv)

    def test_nonzero_exit_code_is_reported(self):
        factory, _ = _popen_factory([], stderr=["boom\n"], exit_code=3)
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(sapper.FGError, "opencode exited 3: boom"):
                _engine(factory).run(_request())

    def test_timeout_terminates_then_kills(self):
        factory, created = _popen_factory([], expire=True)
        with mock.patch.dict("os.environ", _ENV_CLEAN), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(sapper.FGError, "timed out after 5s"):
                _engine(factory, clock=_Clock([0.0, 5.0, 6.0])).run(_request())
        self.assertEqual(created[0].terminate_calls, 1)
        self.assertEqual(created[0].kill_calls, 1)

    def test_cli_not_found_is_actionable(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError()

        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(sapper.FGError, "CLI not found on PATH"):
                _engine(missing).run(_request())

    def test_timeout_validation(self):
        with mock.patch.dict("os.environ", _ENV_CLEAN):
            with self.assertRaisesRegex(sapper.FGError, "opencode timeout must be at least 1 second"):
                _engine(_popen_factory([])[0]).run(_request(timeout=0))


class SelectionAndIntegrationTests(unittest.TestCase):
    def test_default_engine_is_claude_and_signature_unchanged(self):
        with mock.patch.dict("os.environ", {"SAPPER_ENGINE": ""}):
            engine = select_engine(
                run_once=lambda *a: {"result": "x"},
                is_quota_error=lambda m: False,
                rotate_key=lambda: False,
                error_cls=sapper.FGError,
            )
        self.assertIsInstance(engine, ClaudeEngine)

    def test_opencode_engine_is_selected_explicitly(self):
        with mock.patch.dict("os.environ", {"SAPPER_ENGINE": "opencode"}):
            engine = select_engine(
                run_once=lambda *a: {"result": "x"},
                is_quota_error=lambda m: False,
                rotate_key=lambda: False,
                error_cls=sapper.FGError,
            )
        self.assertIsInstance(engine, OpenCodeEngine)

    def test_unknown_engine_is_rejected(self):
        with mock.patch.dict("os.environ", {"SAPPER_ENGINE": "mystery"}):
            with self.assertRaisesRegex(sapper.FGError, "unknown SAPPER_ENGINE"):
                select_engine(
                    run_once=lambda *a: {}, is_quota_error=lambda m: False,
                    rotate_key=lambda: False, error_cls=sapper.FGError,
                )

    def test_run_engine_dispatches_to_opencode_end_to_end(self):
        factory, created = _popen_factory(_fixture_lines())
        env = dict(_ENV_CLEAN)
        env["SAPPER_ENGINE"] = "opencode"
        with mock.patch.dict("os.environ", env), \
             mock.patch("opencode_engine.subprocess.Popen", factory), \
             contextlib.redirect_stderr(io.StringIO()):
            envelope = sapper.run_engine(
                "PROMPT", model="sonnet", timeout=5, skip_permissions=False,
                append_system=None, cwd=Path("."),
            )
        self.assertEqual(envelope, {
            "result": "final answer\nfallback only",
            "sessionID": "ses_main",
        })
        self.assertEqual(created[0].argv[0], "opencode")

    def test_claude_path_still_uses_legacy_subprocess_contract(self):
        payload = {"result": "claude-ok"}
        proc = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
        with mock.patch.dict("os.environ", {"SAPPER_BIN": "claude",
                                            "SAPPER_ENGINE": "claude",
                                            "SAPPER_RETRY_MODEL": "0"}), \
             mock.patch.object(sapper.subprocess, "run", return_value=proc) as run:
            envelope = sapper.run_engine(
                "P", model="sonnet", timeout=2, skip_permissions=False,
                append_system=None, cwd=Path("."),
            )
        self.assertEqual(envelope, payload)
        run.assert_called_once_with(
            ["claude", "-p", "--output-format", "json", "--model", "sonnet"],
            input="P", capture_output=True, text=True, timeout=2, cwd=".",
        )

    def test_opencode_module_uses_stdlib_and_contract_only(self):
        allowed = {"__future__", "json", "os", "subprocess", "sys", "threading",
                   "time", "pathlib", "typing", "engines"}
        tree = ast.parse((ROOT / "lib" / "opencode_engine.py").read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertTrue(roots.issubset(allowed), f"unexpected imports: {roots - allowed}")


if __name__ == "__main__":
    unittest.main()
