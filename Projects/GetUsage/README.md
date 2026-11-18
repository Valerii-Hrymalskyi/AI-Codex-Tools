# Codex usage reader

`codex_usage.py` reads the rate-limit windows for the ChatGPT account signed
into the local Codex Desktop installation. It is a single Python 3.10+
standard-library module: import it from another local program, run it from the
command line, or invoke it from a lifecycle hook.

It starts the native local `codex.exe app-server --stdio`, calls the supported
`account/rateLimits/read` method, then always stops that child process. It does
not read `auth.json`, persist credentials or usage data, open a network port,
consume quota, or reset a limit.

The previous `codex-usage-mcp.js` bridge and its test are deliberately retained
unchanged as a legacy fallback. The Python version has no MCP server: the local
`task-delegator` MCP server of
[AI-Claude-Tools](https://github.com/Valerii-Hrymalskyi/AI-Claude-Tools) calls
`codex_usage.py --json` as a subprocess.

## Paths

Below, `<clone>` is the folder of this repository and `<python>` is its portable
Python, `<clone>\Utils\Bin\Windows\Python3\python.exe`, installed by
`<clone>\python_setup.bat` (not in git). Any other Python 3.10+ works as well:

```text
Script: <clone>\Projects\GetUsage\codex_usage.py
```

Neither `python`, `node`, nor `codex` needs to be on `PATH`.

## Run it

In PowerShell:

```powershell
& "<python>" "<clone>\Projects\GetUsage\codex_usage.py"

& "<python>" "<clone>\Projects\GetUsage\codex_usage.py" --json
```

The first command prints one human-readable line, for example:

```text
Codex limits: 5h: 45.0%, R: 2026-11-17 05:22 · 7d: 7.0%, R: 2026-11-23 19:13
```

These are **used** percentages, not remaining percentages. Reset times use the
machine's local time zone.

Common options:

```text
--codex-exe PATH  Use this native codex.exe instead of discovery.
--timeout SEC     Set the app-server timeout (default: 20 seconds; 8 with --hook).
--json            Write schema-1 JSON to stdout.
--hook MODE       MODE is codex-stop or claude-stop.
```

For non-hook commands, stdout contains only the requested report or JSON.
Diagnostics go only to stderr. Exit code `0` means success, `1` means that
usage data was unavailable, and `2` means invalid arguments. Hook commands
always exit `0`: on a lookup failure they emit a visible `systemMessage` rather
than breaking the host turn.

## JSON contract and Python API

```python
from codex_usage import CodexUsageError, read_usage

usage: dict = read_usage(codex_exe=None, timeout=20.0)
```

`read_usage()` returns exactly the object printed by `--json`, with schema `1`.
It contains the local measurement time, the native `codex.exe` path, plan and
availability metadata, reset credits, all limit buckets, and for each available
window: duration, used/remaining percentages, local ISO-8601 reset time, Unix
reset time, and seconds until reset. Unknown values are `null`; no account ID,
email address, or token is included.

The complete acceptance contract is also recorded in AI-Claude-Tools,
`Projects/MCP/TaskDelegator/docs/codex-usage-requirements.md`.
Increase `schema` only for a backward-incompatible contract change.

## How `codex.exe` is found

Discovery is deterministic and does not invoke a shell:

1. `--codex-exe PATH`;
2. `CODEX_USAGE_CODEX_BIN`;
3. the newest `%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe` folder;
4. a real `codex.exe` in `PATH`; or, if `PATH` only supplies npm's
   `codex.cmd`/`codex.ps1` wrapper, its nearby
   `node_modules\@openai\codex\vendor\*\codex\codex.exe`.

If none can be found, pass an absolute native executable path explicitly.

## Stop hooks

The two hosts use different output shapes. Both avoid a second model turn.

For Codex Desktop, `--hook codex-stop` writes:

```json
{"continue":false,"stopReason":"Codex limits: 5h: 45.0%, R: 2026-11-17 05:22 · 7d: 7.0%, R: 2026-11-23 19:13"}
```

Codex records this as a visible **Stopped** hook result rather than an appended
assistant-message footer. This is the currently supported no-continuation way
to expose the report in Codex Desktop. A neutral transcript footer would need
`decision: "block"`, which creates an additional model continuation.

For Claude Code, `--hook claude-stop` writes its supported warning shape:

```json
{"systemMessage":"Codex limits: 5h: 45.0%, R: 2026-11-17 05:22 · 7d: 7.0%, R: 2026-11-23 19:13"}
```

Hook lookups use an 8-second app-server timeout by default, so an unresponsive
server does not hold up the host turn for the normal 20 seconds. An explicit
`--timeout SEC` overrides this default.
The behavior is based on the official [Codex Hooks documentation](https://developers.openai.com/docs/hooks): plain stdout is ignored, `systemMessage`
is a hook warning rather than a transcript footer, and `commandWindows` is only
a Windows override. Every command hook must also have the base `command` field.

The following are ready-to-copy fragments. They are examples only; review and
trust new hooks in the host UI before they run. Restart Codex Desktop after
adding or changing a user-level hook so its long-lived app-server reloads it.

### Codex Desktop: `%USERPROFILE%\.codex\hooks.json`

```json
{
  "description": "Show the local Codex usage report after a turn.",
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"<python>\" \"<clone>\\Projects\\GetUsage\\codex_usage.py\" --hook codex-stop",
            "commandWindows": "\"<python>\" \"<clone>\\Projects\\GetUsage\\codex_usage.py\" --hook codex-stop",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

### Claude Code: `%USERPROFILE%\.claude\settings.json`

Merge this `hooks` member with existing settings; do not replace unrelated
configuration:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"<python>\" \"<clone>\\Projects\\GetUsage\\codex_usage.py\" --hook claude-stop",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

No Claude configuration is required for the standalone script or the Codex
hook.

## Tests

```powershell
& "<python>" "<clone>\Projects\GetUsage\test_codex_usage.py" -v
```

The offline suite covers normalization, incomplete/multiple bucket data,
formatting, both hook modes, JSON and exit-code behavior, executable discovery,
and a fake app-server's success, error, premature exit, and timeout paths. The
timeout test verifies that the child process is no longer alive after cleanup.
