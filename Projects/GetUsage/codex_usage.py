#!/usr/bin/env python3
"""Read the signed-in local Codex account's rate-limit windows.

This module deliberately uses only Python's standard library.  It talks to the
local Codex app-server over stdio and exposes the result both as a small Python
API and as a command-line program.  It never reads auth.json or sends account
data over the network.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Mapping, TextIO


APP_SERVER_TIMEOUT_SECONDS = 20.0
HOOK_APP_SERVER_TIMEOUT_SECONDS = 8.0
SCHEMA_VERSION = 1
SOURCE_NAME = "codex app-server account/rateLimits/read"
CLIENT_INFO = {
    "name": "codex-usage",
    "title": "Codex Usage",
    "version": "1.0.0",
}
_EOF = object()


class CodexUsageError(RuntimeError):
    """A short, user-facing failure while looking up Codex usage."""


def _compact_error(error: BaseException | str) -> str:
    """Return a one-line diagnostic safe for stderr or a hook message."""

    text = str(error).replace("\r", " ").replace("\n", " ").strip()
    return (text or error.__class__.__name__ if isinstance(error, BaseException) else text)[:400]


def _configure_utf8_output() -> None:
    """Make CLI output independent of the console's legacy code page."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8")
        except (AttributeError, OSError, TypeError, ValueError):
            # StringIO and embedding hosts do not necessarily expose a
            # reconfigurable text stream. Leave such streams configured as
            # supplied by their host.
            pass


def _finite_number(value: Any) -> float | None:
    """Convert a JSON number to float, rejecting booleans and non-finite data."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_number(value: float | None) -> int | float | None:
    if value is None:
        return None
    return int(value) if value.is_integer() else value


def _clamp_percent(value: Any) -> float | None:
    number = _finite_number(value)
    if number is None:
        return None
    return float(min(100.0, max(0.0, number)))


def format_window_label(minutes: Any) -> str:
    """Render a Codex rate-limit duration using the stable JSON label style."""

    value = _finite_number(minutes)
    if value is None or value <= 0:
        return "window"

    total_minutes = int(round(value))
    if total_minutes < 24 * 60:
        return f"{total_minutes // 60}h" if total_minutes % 60 == 0 else f"{total_minutes}m"

    days, remainder = divmod(total_minutes, 24 * 60)
    hours, remainder = divmod(remainder, 60)
    pieces: list[str] = []
    if days:
        pieces.append(f"{days}d")
    if hours:
        pieces.append(f"{hours}h")
    if remainder or not pieces:
        pieces.append(f"{remainder}m")
    return " ".join(pieces)


def _normalise_path(value: str | os.PathLike[str]) -> Path:
    try:
        raw_path = os.fspath(value)
    except TypeError as error:
        raise CodexUsageError("Codex executable path must be a string or path-like value.") from error
    return Path(os.path.expandvars(os.path.expanduser(raw_path))).resolve()


def _require_explicit_executable(value: str | os.PathLike[str], source: str) -> Path:
    path = _normalise_path(value)
    if not path.is_file():
        raise CodexUsageError(f"Codex executable from {source} does not exist: {path}")
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        raise CodexUsageError(
            f"Codex executable from {source} is a wrapper, not codex.exe: {path}",
        )
    return path


def _newest_desktop_codex(local_appdata: str | os.PathLike[str] | None) -> Path | None:
    if not local_appdata:
        return None
    root = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
    try:
        candidates = [path for path in root.glob("*/codex.exe") if path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    try:
        return max(candidates, key=lambda path: path.parent.stat().st_mtime).resolve()
    except OSError:
        return max(candidates, key=lambda path: str(path).lower()).resolve()


def _case_insensitive_child(directory: Path, name: str) -> Path | None:
    try:
        for child in directory.iterdir():
            if child.name.lower() == name.lower() and child.is_file():
                return child.resolve()
    except OSError:
        return None
    return None


def _vendor_codex_near_wrapper(wrapper: Path) -> Path | None:
    """Find npm's bundled native codex.exe near a codex.cmd/.ps1 wrapper."""

    search_roots: list[Path] = []
    for ancestor in (wrapper.parent, *wrapper.parents):
        if len(search_roots) >= 7:
            break
        search_roots.extend(
            (
                ancestor / "node_modules" / "@openai" / "codex" / "vendor",
                ancestor / "@openai" / "codex" / "vendor",
            ),
        )

    inspected: set[Path] = set()
    for vendor_root in search_roots:
        try:
            resolved_root = vendor_root.resolve()
        except OSError:
            resolved_root = vendor_root
        if resolved_root in inspected or not vendor_root.is_dir():
            continue
        inspected.add(resolved_root)
        try:
            candidates = [
                path
                for path in vendor_root.glob("*/codex/codex.exe")
                if path.is_file()
            ]
        except OSError:
            continue
        if candidates:
            try:
                return max(candidates, key=lambda path: path.stat().st_mtime).resolve()
            except OSError:
                return candidates[0].resolve()
    return None


def _path_codex(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    wrappers: list[Path] = []
    for raw_directory in path_value.split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory)
        executable = _case_insensitive_child(directory, "codex.exe")
        if executable is not None:
            return executable
        for wrapper_name in ("codex.cmd", "codex.ps1"):
            wrapper = _case_insensitive_child(directory, wrapper_name)
            if wrapper is not None:
                wrappers.append(wrapper)

    for wrapper in wrappers:
        vendor = _vendor_codex_near_wrapper(wrapper)
        if vendor is not None:
            return vendor
    return None


def find_codex_exe(
    codex_exe: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    local_appdata: str | os.PathLike[str] | None = None,
    path_value: str | None = None,
) -> str:
    """Resolve the native Codex executable without relying on a shell command.

    Keyword-only environment parameters make the discovery order testable while
    leaving the public ``read_usage()`` API small.
    """

    environment = os.environ if environ is None else environ
    if codex_exe is not None:
        return str(_require_explicit_executable(codex_exe, "--codex-exe"))

    configured = environment.get("CODEX_USAGE_CODEX_BIN")
    if configured:
        return str(_require_explicit_executable(configured, "CODEX_USAGE_CODEX_BIN"))

    desktop = _newest_desktop_codex(
        local_appdata if local_appdata is not None else environment.get("LOCALAPPDATA"),
    )
    if desktop is not None:
        return str(desktop)

    path_candidate = _path_codex(
        path_value if path_value is not None else environment.get("PATH"),
    )
    if path_candidate is not None:
        return str(path_candidate)

    raise CodexUsageError(
        "Could not find a native codex.exe. Pass --codex-exe PATH or set "
        "CODEX_USAGE_CODEX_BIN.",
    )


class _StderrTail:
    def __init__(self, stream: Any, limit: int = 2_000) -> None:
        self._stream = stream
        self._limit = limit
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._read, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _read(self) -> None:
        try:
            while True:
                chunk = self._stream.read(1024)
                if not chunk:
                    return
                with self._lock:
                    self._chunks.append(chunk)
                    joined = b"".join(self._chunks)
                    self._chunks = [joined[-self._limit :]]
        except (OSError, ValueError):
            return

    def text(self) -> str:
        with self._lock:
            data = b"".join(self._chunks)
        return data.decode("utf-8", errors="replace").strip()


def _start_stdout_reader(stream: Any) -> queue.Queue[object]:
    lines: queue.Queue[object] = queue.Queue()

    def read_lines() -> None:
        try:
            while True:
                raw = stream.readline()
                if not raw:
                    return
                lines.put(raw.decode("utf-8", errors="replace").strip())
        except (OSError, ValueError):
            return
        finally:
            lines.put(_EOF)

    threading.Thread(target=read_lines, daemon=True).start()
    return lines


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _app_server_command(codex_exe: str) -> list[str]:
    """Build a shell-free command. .py is intentionally supported for tests."""

    executable = Path(codex_exe)
    if executable.suffix.lower() == ".py":
        return [sys.executable, str(executable), "app-server", "--stdio"]
    return [str(executable), "app-server", "--stdio"]


def _close_quietly(stream: Any) -> None:
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
    """Stop the app-server on success, failure, timeout, or Ctrl+C."""

    if process is None:
        return
    try:
        _close_quietly(process.stdin)
        if process.poll() is None:
            if os.name == "nt":
                taskkill = Path(os.environ.get("SystemRoot", r"C:\\Windows")) / "System32" / "taskkill.exe"
                command = str(taskkill) if taskkill.is_file() else "taskkill.exe"
                try:
                    subprocess.run(
                        [command, "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                        check=False,
                        shell=False,
                        creationflags=_creation_flags(),
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                try:
                    process.terminate()
                except OSError:
                    pass
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        # The finally block below has the same direct-kill fallback. This branch
        # exists to give a normally unresponsive child one explicit chance first.
        try:
            process.kill()
        except OSError:
            pass
    finally:
        # A KeyboardInterrupt during taskkill/wait still reaches this block, so
        # the app-server cannot survive as an orphaned codex.exe process.
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _close_quietly(process.stdout)
        _close_quietly(process.stderr)


def _send_rpc(process: subprocess.Popen[bytes], message: Mapping[str, Any]) -> None:
    if process.stdin is None:
        raise CodexUsageError("Codex app-server stdin is unavailable.")
    try:
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        process.stdin.write(payload)
        process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError) as error:
        raise CodexUsageError(f"Could not write to Codex app-server: {_compact_error(error)}") from error


def _exited_before_reply(
    process: subprocess.Popen[bytes],
    stderr_tail: _StderrTail,
) -> CodexUsageError:
    details = stderr_tail.text()
    suffix = f" {details}" if details else ""
    return CodexUsageError(
        f"Codex app-server exited before replying (code {process.poll()}).{suffix}",
    )


def _wait_for_response(
    process: subprocess.Popen[bytes],
    lines: queue.Queue[object],
    expected_id: int,
    deadline: float,
    timeout: float,
    stderr_tail: _StderrTail,
) -> Any:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexUsageError(
                f"Codex app-server did not answer within {timeout:g} s.",
            )
        try:
            item = lines.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            if process.poll() is not None:
                try:
                    item = lines.get_nowait()
                except queue.Empty:
                    raise _exited_before_reply(process, stderr_tail)
            else:
                continue

        if item is _EOF:
            raise _exited_before_reply(process, stderr_tail)
        if not isinstance(item, str) or not item:
            continue
        try:
            message = json.loads(item)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        if str(message.get("id")) != str(expected_id):
            continue
        if "error" in message and message["error"] is not None:
            error = message["error"]
            if isinstance(error, Mapping):
                code = error.get("code", "error")
                message_text = error.get("message", "unknown error")
            else:
                code = "error"
                message_text = str(error)
            raise CodexUsageError(f"Codex app-server {code}: {_compact_error(message_text)}")
        return message.get("result")


def _request_rate_limits(codex_exe: str, timeout: float) -> Mapping[str, Any]:
    if not math.isfinite(timeout) or timeout <= 0:
        raise CodexUsageError("Timeout must be a positive number of seconds.")

    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = subprocess.Popen(
                _app_server_command(codex_exe),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=_creation_flags(),
            )
        except OSError as error:
            raise CodexUsageError(
                f"Could not start Codex app-server: {_compact_error(error)}",
            ) from error

        if process.stdout is None or process.stderr is None:
            raise CodexUsageError("Could not open Codex app-server output streams.")
        stderr_tail = _StderrTail(process.stderr)
        stderr_tail.start()
        lines = _start_stdout_reader(process.stdout)
        deadline = time.monotonic() + timeout

        _send_rpc(
            process,
            {"id": 1, "method": "initialize", "params": {"clientInfo": CLIENT_INFO}},
        )
        _wait_for_response(process, lines, 1, deadline, timeout, stderr_tail)
        _send_rpc(process, {"method": "initialized", "params": {}})
        _send_rpc(process, {"id": 2, "method": "account/rateLimits/read", "params": {}})
        result = _wait_for_response(process, lines, 2, deadline, timeout, stderr_tail)
        if not isinstance(result, Mapping):
            raise CodexUsageError("Codex app-server returned invalid rate-limit data.")
        return result
    finally:
        _stop_process(process)


def _safe_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _as_local_iso(unix_seconds: float | None, local_zone: tzinfo) -> str | None:
    if unix_seconds is None or unix_seconds <= 0:
        return None
    try:
        instant = datetime.fromtimestamp(unix_seconds, tz=timezone.utc).astimezone(local_zone)
    except (OSError, OverflowError, ValueError):
        return None
    return instant.isoformat(timespec="seconds")


def _normalise_window(
    kind: str,
    source: Any,
    *,
    now: datetime,
    local_zone: tzinfo,
) -> dict[str, Any] | None:
    if not isinstance(source, Mapping):
        return None
    minutes = _finite_number(source.get("windowDurationMins"))
    used_percent = _clamp_percent(source.get("usedPercent"))
    reset_seconds = _finite_number(source.get("resetsAt"))
    if reset_seconds is not None and reset_seconds <= 0:
        reset_seconds = None
    remaining_percent = None if used_percent is None else float(max(0.0, 100.0 - used_percent))
    return {
        "kind": kind,
        "label": format_window_label(minutes),
        "window_minutes": _json_number(minutes),
        "used_percent": used_percent,
        "remaining_percent": remaining_percent,
        "resets_at": _as_local_iso(reset_seconds, local_zone),
        "resets_at_unix": _json_number(reset_seconds),
        "resets_in_sec": (
            None
            if reset_seconds is None
            else max(0, int(math.ceil(reset_seconds - now.timestamp())))
        ),
    }


def normalize_rate_limits(
    response: Mapping[str, Any],
    codex_exe: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return schema 1 of the public, account-identifier-free usage contract."""

    measured = now or datetime.now().astimezone()
    if measured.tzinfo is None:
        measured = measured.astimezone()
    local_zone = measured.tzinfo
    assert local_zone is not None

    by_limit_id = response.get("rateLimitsByLimitId")
    entries: list[tuple[str, Any]]
    if isinstance(by_limit_id, Mapping) and by_limit_id:
        entries = [(str(identifier), bucket) for identifier, bucket in by_limit_id.items()]
    else:
        fallback = response.get("rateLimits")
        if isinstance(fallback, Mapping):
            entries = [(str(fallback.get("limitId") or "codex"), fallback)]
        else:
            entries = []

    buckets: list[dict[str, Any]] = []
    plan_type = _safe_text(response.get("planType"))
    for identifier, raw_bucket in entries:
        bucket = raw_bucket if isinstance(raw_bucket, Mapping) else {}
        bucket_plan = _safe_text(bucket.get("planType"))
        if plan_type is None and bucket_plan is not None:
            plan_type = bucket_plan
        limit_reached = bucket.get("rateLimitReachedType")
        if isinstance(limit_reached, str):
            limit_reached = limit_reached.strip() or None
        elif limit_reached is not None and not isinstance(limit_reached, (int, float, bool)):
            limit_reached = str(limit_reached)
        windows = [
            window
            for window in (
                _normalise_window("primary", bucket.get("primary"), now=measured, local_zone=local_zone),
                _normalise_window("secondary", bucket.get("secondary"), now=measured, local_zone=local_zone),
            )
            if window is not None
        ]
        buckets.append(
            {
                "id": str(bucket.get("limitId") or identifier),
                "name": str(bucket.get("limitName") or bucket.get("limitId") or identifier),
                "limit_reached": limit_reached,
                "windows": windows,
            },
        )

    reset_credits = response.get("rateLimitResetCredits")
    available = (
        _finite_number(reset_credits.get("availableCount"))
        if isinstance(reset_credits, Mapping)
        else None
    )
    ordinary = response.get("ordinaryUsageAllowed")
    return {
        "schema": SCHEMA_VERSION,
        "measured_at": measured.isoformat(timespec="seconds"),
        "source": SOURCE_NAME,
        "codex_exe": str(codex_exe),
        "plan_type": plan_type,
        "ordinary_usage_allowed": ordinary if isinstance(ordinary, bool) else None,
        "reset_credits_available": _json_number(available),
        "buckets": buckets,
    }


def read_usage(codex_exe: str | None = None, timeout: float = APP_SERVER_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Read and normalize the signed-in local Codex account's usage.

    Raises:
        CodexUsageError: The executable cannot be found, app-server fails, or
            the app-server response is unavailable or malformed.
    """

    try:
        timeout_seconds = float(timeout)
    except (TypeError, ValueError) as error:
        raise CodexUsageError("Timeout must be a positive number of seconds.") from error
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise CodexUsageError("Timeout must be a positive number of seconds.")
    try:
        executable = find_codex_exe(codex_exe)
        raw = _request_rate_limits(executable, timeout_seconds)
        return normalize_rate_limits(raw, executable)
    except CodexUsageError:
        raise
    except (OSError, TypeError, ValueError) as error:
        raise CodexUsageError(f"Could not read Codex limits: {_compact_error(error)}") from error


def format_usage_text(usage: Mapping[str, Any]) -> str:
    """Format one compact, human-visible rate-limit line using used percent."""

    buckets = usage.get("buckets")
    if not isinstance(buckets, list) or not buckets:
        return "Codex limits: unavailable"

    multiple_buckets = len(buckets) > 1
    sections: list[str] = []
    reached = usage.get("ordinary_usage_allowed") is False
    for bucket in buckets:
        if not isinstance(bucket, Mapping):
            continue
        windows = bucket.get("windows")
        window_parts: list[str] = []
        if isinstance(windows, list):
            for window in windows:
                if not isinstance(window, Mapping):
                    continue
                label = window.get("label") or "window"
                percent = window.get("used_percent")
                percent_text = "unknown" if not isinstance(percent, (int, float)) else f"{float(percent):.1f}%"
                reset = window.get("resets_at")
                if isinstance(reset, str):
                    try:
                        reset_time = datetime.fromisoformat(reset)
                        reset_text = (reset_time + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M")
                    except ValueError:
                        reset_text = reset
                else:
                    reset_text = "unknown"
                window_parts.append(f"{label}: {percent_text}, R: {reset_text}")
        bucket_name = str(bucket.get("name") or bucket.get("id") or "codex")
        if multiple_buckets:
            content = "; ".join(window_parts) if window_parts else "no quota window data"
            sections.append(f"{bucket_name}: {content}")
        else:
            sections.extend(window_parts or ["no quota window data"])
        if bucket.get("limit_reached") not in (None, False, "", 0):
            reached = True

    report = f"Codex limits: {' · '.join(sections) if sections else 'unavailable'}"
    return f"{report} · LIMIT REACHED" if reached else report


def _read_hook_input(stream: TextIO | None = None, timeout: float = 0.1) -> dict[str, Any]:
    """Consume hook stdin without blocking when it is an interactive/open stream.

    The hook payload is currently not needed, but parsing it avoids leaving data
    unread when a host does provide it. Invalid and absent payloads are harmless.
    """

    source = sys.stdin if stream is None else stream
    try:
        if source is None or source.isatty():
            return {}
    except (AttributeError, OSError):
        return {}

    result: list[str] = []
    finished = threading.Event()

    def read_all() -> None:
        try:
            data = source.read()
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            result.append(data)
        except (OSError, ValueError):
            pass
        finally:
            finished.set()

    threading.Thread(target=read_all, daemon=True).start()
    if not finished.wait(timeout):
        return {}
    text = result[0].strip() if result else ""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hook_message(codex_exe: str | None, timeout: float) -> str:
    try:
        usage = read_usage(codex_exe=codex_exe, timeout=timeout)
        return format_usage_text(usage)
    except (CodexUsageError, OSError, ValueError) as error:
        return f"Codex limits unavailable: {_compact_error(error)}"


def _hook_output(mode: str, message: str) -> dict[str, Any]:
    """Build the event-specific JSON output without a model continuation.

    Codex Desktop renders Stop ``systemMessage`` values only as hook warnings,
    not transcript footers. ``continue: false`` with a ``stopReason`` creates a
    visible Stop entry instead, without using ``decision: block`` or a second
    model turn. Claude Code continues to use its visible systemMessage output.
    """

    if mode == "codex-stop":
        return {"continue": False, "stopReason": message}
    return {"systemMessage": message}


def _run_hook(mode: str, codex_exe: str | None, timeout: float) -> int:
    # Input is intentionally ignored after a safe read. Neither output shape
    # asks the model for a continuation.
    _read_hook_input()
    if mode not in {"codex-stop", "claude-stop"}:
        message = f"Codex limits unavailable: Unknown hook mode: {mode}"
    else:
        message = _hook_message(codex_exe, timeout)
    sys.stdout.write(json.dumps(_hook_output(mode, message), ensure_ascii=False) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print schema 1 JSON")
    parser.add_argument(
        "--hook",
        choices=("codex-stop", "claude-stop"),
        metavar="MODE",
        help="emit one hook-compatible JSON object",
    )
    parser.add_argument("--codex-exe", metavar="PATH", help="absolute path to native codex.exe")
    parser.add_argument(
        "--timeout",
        default=None,
        metavar="SEC",
        help="app-server timeout in seconds (default: 20; 8 with --hook)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_output()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code)

    if args.hook and args.json:
        parser.error("--json cannot be combined with --hook")
        return 2

    if args.timeout is None:
        timeout_seconds = (
            HOOK_APP_SERVER_TIMEOUT_SECONDS if args.hook else APP_SERVER_TIMEOUT_SECONDS
        )
        valid_timeout = True
    else:
        try:
            timeout_seconds = float(args.timeout)
            valid_timeout = math.isfinite(timeout_seconds) and timeout_seconds > 0
        except (TypeError, ValueError):
            timeout_seconds = APP_SERVER_TIMEOUT_SECONDS
            valid_timeout = False
    if args.hook:
        if not valid_timeout:
            sys.stdout.write(
                json.dumps(
                    _hook_output(
                        args.hook,
                        "Codex limits unavailable: Timeout must be a positive number of seconds.",
                    ),
                    ensure_ascii=False,
                )
                + "\n",
            )
            return 0
        return _run_hook(args.hook, args.codex_exe, timeout_seconds)
    if not valid_timeout:
        parser.error("--timeout must be a positive number")
        return 2  # argparse.error raises; retained for type checkers.

    try:
        usage = read_usage(codex_exe=args.codex_exe, timeout=timeout_seconds)
    except (CodexUsageError, OSError, ValueError) as error:
        sys.stderr.write(f"Codex limits unavailable: {_compact_error(error)}\n")
        return 1

    if args.json:
        sys.stdout.write(json.dumps(usage, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        sys.stdout.write(format_usage_text(usage) + "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # _request_rate_limits() cleans up its child in finally before this point.
        raise SystemExit(130)
