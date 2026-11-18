from __future__ import annotations

import ctypes
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

import codex_usage


LOCAL_TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 11, 17, 1, 20, 5, tzinfo=LOCAL_TZ)
PRIMARY_RESET = int(datetime(2026, 11, 17, 5, 22, 28, tzinfo=LOCAL_TZ).timestamp())
SECONDARY_RESET = int(datetime(2026, 11, 23, 19, 12, 59, tzinfo=LOCAL_TZ).timestamp())

SAMPLE_RESPONSE = {
    "accountId": "must-never-leak@example.test",
    "planType": "plus",
    "ordinaryUsageAllowed": True,
    "rateLimitResetCredits": {"availableCount": 3},
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {
                "usedPercent": 45,
                "windowDurationMins": 300,
                "resetsAt": PRIMARY_RESET,
            },
            "secondary": {
                "usedPercent": 7,
                "windowDurationMins": 10080,
                "resetsAt": SECONDARY_RESET,
            },
        },
    },
}


class NormalizationTests(unittest.TestCase):
    def test_normalizes_schema_without_account_identity(self) -> None:
        usage = codex_usage.normalize_rate_limits(
            SAMPLE_RESPONSE,
            r"C:\\Users\\Example\\AppData\\Local\\OpenAI\\Codex\\bin\\hash\\codex.exe",
            now=NOW,
        )

        self.assertEqual(usage["schema"], 1)
        self.assertEqual(usage["measured_at"], "2026-11-17T01:20:05+02:00")
        self.assertEqual(usage["source"], "codex app-server account/rateLimits/read")
        self.assertEqual(usage["plan_type"], "plus")
        self.assertTrue(usage["ordinary_usage_allowed"])
        self.assertEqual(usage["reset_credits_available"], 3)
        self.assertEqual(len(usage["buckets"]), 1)

        primary, secondary = usage["buckets"][0]["windows"]
        self.assertEqual(primary["kind"], "primary")
        self.assertEqual(primary["label"], "5h")
        self.assertEqual(primary["window_minutes"], 300)
        self.assertEqual(primary["used_percent"], 45.0)
        self.assertEqual(primary["remaining_percent"], 55.0)
        self.assertEqual(primary["resets_at"], "2026-11-17T05:22:28+02:00")
        self.assertEqual(primary["resets_at_unix"], PRIMARY_RESET)
        self.assertEqual(primary["resets_in_sec"], PRIMARY_RESET - int(NOW.timestamp()))
        self.assertEqual(secondary["label"], "7d")
        self.assertEqual(secondary["used_percent"], 7.0)
        self.assertNotIn("accountId", json.dumps(usage))
        self.assertNotIn("must-never-leak", json.dumps(usage))

    def test_incomplete_and_multiple_buckets_use_null_and_clamp_percentages(self) -> None:
        response = {
            "ordinaryUsageAllowed": "not-a-boolean",
            "rateLimitsByLimitId": {
                "first": {
                    "limitName": "First",
                    "rateLimitReachedType": "primary",
                    "primary": {"usedPercent": -20, "windowDurationMins": 90},
                    "secondary": {"usedPercent": 250, "windowDurationMins": 3120, "resetsAt": 0},
                },
                "second": {"limitId": "second"},
            },
        }
        usage = codex_usage.normalize_rate_limits(response, "codex.exe", now=NOW)

        self.assertIsNone(usage["ordinary_usage_allowed"])
        self.assertEqual(len(usage["buckets"]), 2)
        first = usage["buckets"][0]
        self.assertEqual(first["limit_reached"], "primary")
        self.assertEqual(first["windows"][0]["used_percent"], 0.0)
        self.assertEqual(first["windows"][0]["remaining_percent"], 100.0)
        self.assertEqual(first["windows"][0]["label"], "90m")
        self.assertEqual(first["windows"][1]["used_percent"], 100.0)
        self.assertEqual(first["windows"][1]["remaining_percent"], 0.0)
        self.assertEqual(first["windows"][1]["label"], "2d 4h")
        self.assertIsNone(first["windows"][1]["resets_at"])
        self.assertEqual(usage["buckets"][1]["windows"], [])

    def test_falls_back_to_rate_limits_when_map_is_missing(self) -> None:
        response = {
            "rateLimits": {
                "limitId": "fallback",
                "primary": {"usedPercent": 1, "windowDurationMins": 300},
            },
        }
        usage = codex_usage.normalize_rate_limits(response, "codex.exe", now=NOW)
        self.assertEqual(usage["buckets"][0]["id"], "fallback")
        self.assertEqual(usage["buckets"][0]["windows"][0]["used_percent"], 1.0)

    def test_duration_labels(self) -> None:
        self.assertEqual(codex_usage.format_window_label(300), "5h")
        self.assertEqual(codex_usage.format_window_label(10080), "7d")
        self.assertEqual(codex_usage.format_window_label(90), "90m")
        self.assertEqual(codex_usage.format_window_label(1440), "1d")
        self.assertEqual(codex_usage.format_window_label(None), "window")


class FormattingAndHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.usage = codex_usage.normalize_rate_limits(SAMPLE_RESPONSE, "codex.exe", now=NOW)

    def test_human_text_uses_used_percentage_and_local_reset_time(self) -> None:
        self.assertEqual(
            codex_usage.format_usage_text(self.usage),
            "Codex limits: 5h: 45.0%, R: 2026-11-17 05:22 · "
            "7d: 7.0%, R: 2026-11-23 19:13",
        )

    def test_human_text_rounds_reset_time_to_nearest_minute(self) -> None:
        reset_window = self.usage["buckets"][0]["windows"][1]
        assert isinstance(reset_window, dict)
        for reset, expected in (
            ("2026-11-23T19:12:59+02:00", "2026-11-23 19:13"),
            ("2026-11-23T19:13:00+02:00", "2026-11-23 19:13"),
            ("2026-11-23T19:12:29+02:00", "2026-11-23 19:12"),
        ):
            reset_window["resets_at"] = reset
            self.assertIn(f"R: {expected}", codex_usage.format_usage_text(self.usage))

    def test_human_text_marks_reached_limits(self) -> None:
        self.usage["ordinary_usage_allowed"] = False
        self.assertTrue(codex_usage.format_usage_text(self.usage).endswith(" · LIMIT REACHED"))

    def test_hook_modes_emit_event_specific_output_and_exit_zero(self) -> None:
        expected_message = (
            "Codex limits: 5h: 45.0%, R: 2026-11-17 05:22 · "
            "7d: 7.0%, R: 2026-11-23 19:13"
        )
        for mode, expected_payload in (
            ("codex-stop", {"continue": False, "stopReason": expected_message}),
            ("claude-stop", {"systemMessage": expected_message}),
        ):
            output = StringIO()
            with (
                mock.patch("codex_usage._read_hook_input", return_value={}),
                mock.patch("codex_usage.read_usage", return_value=self.usage),
                redirect_stdout(output),
            ):
                self.assertEqual(codex_usage._run_hook(mode, None, 20), 0)
            self.assertEqual(json.loads(output.getvalue()), expected_payload)

    def test_hook_error_is_user_visible_but_never_fails_the_host(self) -> None:
        output = StringIO()
        with (
            mock.patch("codex_usage._read_hook_input", return_value={}),
            mock.patch(
                "codex_usage.read_usage",
                side_effect=codex_usage.CodexUsageError("not signed in"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(codex_usage._run_hook("codex-stop", None, 20), 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"continue": False, "stopReason": "Codex limits unavailable: not signed in"},
        )


FAKE_APP_SERVER = r'''
import json
import os
import sys
import time

mode = os.environ.get("CODEX_USAGE_TEST_MODE", "normal")
pid_file = os.environ.get("CODEX_USAGE_TEST_PID_FILE")
if pid_file:
    with open(pid_file, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))

for raw_line in sys.stdin:
    message = json.loads(raw_line)
    if message.get("method") == "initialize":
        if mode == "exit":
            raise SystemExit(0)
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "account/rateLimits/read":
        if mode == "error":
            print(json.dumps({"id": message["id"], "error": {"code": 403, "message": "not signed in"}}), flush=True)
        elif mode == "silent":
            time.sleep(60)
        else:
            print(json.dumps({"id": message["id"], "result": {
                "ordinaryUsageAllowed": True,
                "rateLimitsByLimitId": {"codex": {"primary": {
                    "usedPercent": 12,
                    "windowDurationMins": 300,
                    "resetsAt": 1794885748
                }, "secondary": {
                    "usedPercent": 7,
                    "windowDurationMins": 10080,
                    "resetsAt": 1795441979
                }}}
            }}), flush=True)
'''


def _process_is_alive(pid: int) -> bool:
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class AppServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fake_server = self.root / "fake_codex.py"
        self.fake_server.write_text(textwrap.dedent(FAKE_APP_SERVER), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _read_fake(self, mode: str, timeout: float = 1.0) -> dict[str, object]:
        with mock.patch.dict(os.environ, {"CODEX_USAGE_TEST_MODE": mode}, clear=False):
            return codex_usage.read_usage(str(self.fake_server), timeout=timeout)

    def test_normal_app_server_response(self) -> None:
        usage = self._read_fake("normal")
        self.assertEqual(usage["buckets"][0]["windows"][0]["used_percent"], 12.0)

    def test_app_server_error_response(self) -> None:
        with self.assertRaisesRegex(codex_usage.CodexUsageError, r"403: not signed in"):
            self._read_fake("error")

    def test_app_server_exits_before_reply(self) -> None:
        with self.assertRaisesRegex(codex_usage.CodexUsageError, "exited before replying"):
            self._read_fake("exit")

    def test_timeout_kills_the_child_process(self) -> None:
        pid_file = self.root / "fake.pid"
        with mock.patch.dict(
            os.environ,
            {"CODEX_USAGE_TEST_MODE": "silent", "CODEX_USAGE_TEST_PID_FILE": str(pid_file)},
            clear=False,
        ):
            with self.assertRaisesRegex(codex_usage.CodexUsageError, "did not answer within"):
                codex_usage.read_usage(str(self.fake_server), timeout=0.2)
        self.assertTrue(pid_file.is_file())
        pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while _process_is_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_process_is_alive(pid), "timed-out app-server process survived cleanup")

    def test_public_api_wraps_invalid_timeout_as_codex_usage_error(self) -> None:
        with self.assertRaisesRegex(codex_usage.CodexUsageError, "Timeout must be a positive"):
            codex_usage.read_usage(str(self.fake_server), timeout=None)  # type: ignore[arg-type]

    def test_public_api_wraps_invalid_executable_type_as_codex_usage_error(self) -> None:
        with self.assertRaisesRegex(codex_usage.CodexUsageError, "path must be a string"):
            codex_usage.read_usage(codex_exe=object(), timeout=1)  # type: ignore[arg-type]


class DiscoveryTests(unittest.TestCase):
    def test_explicit_and_environment_paths_win(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            explicit = root / "explicit.exe"
            configured = root / "configured.exe"
            explicit.touch()
            configured.touch()
            self.assertEqual(
                codex_usage.find_codex_exe(
                    str(explicit),
                    environ={"CODEX_USAGE_CODEX_BIN": str(configured)},
                    local_appdata=root / "none",
                ),
                str(explicit.resolve()),
            )
            self.assertEqual(
                codex_usage.find_codex_exe(
                    environ={"CODEX_USAGE_CODEX_BIN": str(configured)},
                    local_appdata=root / "none",
                ),
                str(configured.resolve()),
            )

    def test_selects_newest_desktop_bin_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "Local"
            older = local / "OpenAI" / "Codex" / "bin" / "old" / "codex.exe"
            newer = local / "OpenAI" / "Codex" / "bin" / "new" / "codex.exe"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.touch()
            newer.touch()
            os.utime(older.parent, (1_000_000_000, 1_000_000_000))
            os.utime(newer.parent, (1_700_000_000, 1_700_000_000))
            self.assertEqual(
                codex_usage.find_codex_exe(environ={}, local_appdata=local),
                str(newer.resolve()),
            )

    def test_path_uses_native_exe_or_npm_vendor_exe_but_not_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True)
            wrapper = bin_dir / "codex.cmd"
            wrapper.write_text("@echo off", encoding="utf-8")
            vendor = root / "node_modules" / "@openai" / "codex" / "vendor" / "hash" / "codex" / "codex.exe"
            vendor.parent.mkdir(parents=True)
            vendor.touch()
            self.assertEqual(
                codex_usage.find_codex_exe(
                    environ={"PATH": str(bin_dir)},
                    local_appdata=root / "none",
                ),
                str(vendor.resolve()),
            )
            vendor.unlink()
            with self.assertRaisesRegex(codex_usage.CodexUsageError, "Could not find a native codex.exe"):
                codex_usage.find_codex_exe(
                    environ={"PATH": str(bin_dir)},
                    local_appdata=root / "none",
                )


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.usage = codex_usage.normalize_rate_limits(SAMPLE_RESPONSE, "codex.exe", now=NOW)

    def test_json_success_has_clean_stdout(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with (
            mock.patch("codex_usage.read_usage", return_value=self.usage),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(codex_usage.main(["--json"]), 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["schema"], 1)

    def test_data_failure_uses_exit_one_and_stderr_only(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with (
            mock.patch(
                "codex_usage.read_usage",
                side_effect=codex_usage.CodexUsageError("not signed in"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(codex_usage.main(["--json"]), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "Codex limits unavailable: not signed in\n")

    def test_invalid_argument_has_exit_two(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(Path(codex_usage.__file__).resolve()), "--timeout", "0"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertIn("--timeout must be a positive number", completed.stderr)

    def test_hook_timeout_error_still_exits_zero(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(codex_usage.main(["--hook", "codex-stop", "--timeout", "0"]), 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "continue": False,
                "stopReason": "Codex limits unavailable: Timeout must be a positive number of seconds.",
            },
        )

    def test_hook_and_cli_choose_their_default_timeouts(self) -> None:
        with mock.patch("codex_usage._run_hook", return_value=0) as run_hook:
            self.assertEqual(codex_usage.main(["--hook", "codex-stop"]), 0)
        run_hook.assert_called_once_with("codex-stop", None, 8.0)

        with mock.patch("codex_usage._run_hook", return_value=0) as run_hook:
            self.assertEqual(codex_usage.main(["--hook", "claude-stop", "--timeout", "3.5"]), 0)
        run_hook.assert_called_once_with("claude-stop", None, 3.5)

        stdout = StringIO()
        with mock.patch("codex_usage.read_usage", return_value=self.usage) as read_usage, redirect_stdout(stdout):
            self.assertEqual(codex_usage.main([]), 0)
        read_usage.assert_called_once_with(codex_exe=None, timeout=20.0)

    def test_subprocess_output_is_utf8_despite_legacy_encoding_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_server = root / "fake_codex.py"
            fake_server.write_text(textwrap.dedent(FAKE_APP_SERVER), encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONUTF8": "0",
                    "PYTHONIOENCODING": "cp1251",
                    "CODEX_USAGE_TEST_MODE": "normal",
                },
            )
            command = [
                sys.executable,
                str(Path(codex_usage.__file__).resolve()),
                "--codex-exe",
                str(fake_server),
            ]

            text_result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
            )
            self.assertEqual(text_result.returncode, 0)
            self.assertIn("·", text_result.stdout.decode("utf-8"))
            self.assertEqual(text_result.stderr.decode("utf-8"), "")

            hook_result = subprocess.run(
                [*command, "--hook", "claude-stop"],
                input=b"{}\n",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
            )
            self.assertEqual(hook_result.returncode, 0)
            hook_payload = json.loads(hook_result.stdout.decode("utf-8"))
            self.assertIn("·", hook_payload["systemMessage"])
            self.assertEqual(hook_result.stderr.decode("utf-8"), "")

            missing = root / "несуществующий-codex.exe"
            error_result = subprocess.run(
                [
                    sys.executable,
                    str(Path(codex_usage.__file__).resolve()),
                    "--codex-exe",
                    str(missing),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                check=False,
            )
            self.assertEqual(error_result.returncode, 1)
            self.assertEqual(error_result.stdout.decode("utf-8"), "")
            self.assertIn("несуществующий-codex.exe", error_result.stderr.decode("utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
