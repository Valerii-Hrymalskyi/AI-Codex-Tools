#!/usr/bin/env node
"use strict";

/*
 * A local-only bridge from an MCP client to the signed-in Codex account.
 *
 * It intentionally has no network listener and no third-party dependencies.
 * Each request starts `codex app-server --stdio`, asks its supported account
 * endpoint for rate limits, then exits.
 */

const { spawn } = require("node:child_process");
const readline = require("node:readline");

const APP_SERVER_TIMEOUT_MS = 20_000;
const MCP_PROTOCOL_VERSION = "2025-03-26";
const TOOL_NAME = "get_codex_usage";

class CodexUsageError extends Error {
    constructor(message) {
        super(message);
        this.name = "CodexUsageError";
    }
}

function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
}

function clampPercent(value) {
    const number = finiteNumber(value);
    if (number === null) {
        return null;
    }
    return Math.min(100, Math.max(0, number));
}

function compactPercent(value) {
    if (value === null) {
        return "unavailable";
    }
    return `${Number.isInteger(value) ? value : value.toFixed(1)}%`;
}

function formatDuration(minutes) {
    const value = finiteNumber(minutes);
    if (value === null || value <= 0) {
        return "window";
    }

    const rounded = Math.round(value);
    const days = Math.floor(rounded / 1440);
    const hours = Math.floor((rounded % 1440) / 60);
    const remainingMinutes = rounded % 60;
    const parts = [];

    if (days > 0) {
        parts.push(`${days}d`);
    }
    if (hours > 0) {
        parts.push(`${hours}h`);
    }
    if (remainingMinutes > 0 || parts.length === 0) {
        parts.push(`${remainingMinutes}m`);
    }
    return parts.join(" ");
}

function formatResetTime(unixSeconds, locale) {
    const seconds = finiteNumber(unixSeconds);
    if (seconds === null || seconds <= 0) {
        return "reset time unavailable";
    }

    try {
        return new Intl.DateTimeFormat(locale || undefined, {
            dateStyle: "medium",
            timeStyle: "short",
        }).format(new Date(seconds * 1000));
    } catch {
        return new Date(seconds * 1000).toISOString();
    }
}

function normalizeWindow(name, source, locale) {
    if (!source || typeof source !== "object") {
        return null;
    }

    const usedPercent = clampPercent(source.usedPercent);
    return {
        name,
        durationMinutes: finiteNumber(source.windowDurationMins),
        duration: formatDuration(source.windowDurationMins),
        usedPercent,
        remainingPercent: usedPercent === null ? null : Math.max(0, 100 - usedPercent),
        resetsAt: finiteNumber(source.resetsAt),
        resetsAtLocal: formatResetTime(source.resetsAt, locale),
    };
}

function normalizeRateLimits(response, options = {}) {
    const locale = options.locale || process.env.CODEX_USAGE_LOCALE;
    const byLimitId = response && typeof response.rateLimitsByLimitId === "object"
        ? response.rateLimitsByLimitId
        : null;
    let entries = byLimitId && Object.keys(byLimitId).length > 0
        ? Object.entries(byLimitId)
        : [];

    if (entries.length === 0 && response && response.rateLimits) {
        const fallback = response.rateLimits;
        entries = [[fallback.limitId || "codex", fallback]];
    }

    const buckets = entries.map(([id, bucket]) => ({
        id,
        name: bucket && (bucket.limitName || bucket.limitId) || id,
        planType: bucket && bucket.planType || response && response.planType || null,
        rateLimitReachedType: bucket && bucket.rateLimitReachedType || null,
        windows: [
            normalizeWindow("primary", bucket && bucket.primary, locale),
            normalizeWindow("secondary", bucket && bucket.secondary, locale),
        ].filter(Boolean),
    }));

    const resetCredits = response && response.rateLimitResetCredits;
    return {
        ordinaryUsageAllowed: response && typeof response.ordinaryUsageAllowed === "boolean"
            ? response.ordinaryUsageAllowed
            : null,
        buckets,
        resetCredits: {
            availableCount: resetCredits ? finiteNumber(resetCredits.availableCount) : null,
        },
    };
}

function formatUsageReport(normalized) {
    if (!normalized.buckets.length) {
        return "Codex usage is unavailable. Sign into Codex with ChatGPT on this machine.";
    }

    const multipleBuckets = normalized.buckets.length > 1;
    const sections = [];

    for (const bucket of normalized.buckets) {
        const windows = bucket.windows.map((window) => (
            `${window.duration}: ${compactPercent(window.remainingPercent)} remaining (resets ${window.resetsAtLocal})`
        ));
        const prefix = multipleBuckets ? `${bucket.name}: ` : "";
        sections.push(windows.length > 0
            ? `${prefix}${windows.join("; ")}`
            : `${prefix}quota window data unavailable`);
    }

    const resetCount = normalized.resetCredits.availableCount;
    if (resetCount !== null) {
        sections.push(`earned full resets: ${resetCount}`);
    }

    if (normalized.ordinaryUsageAllowed === false) {
        sections.push("ordinary usage is currently unavailable");
    }

    return `Codex limits — ${sections.join(" · ")}`;
}

function compactError(error) {
    const message = error instanceof Error ? error.message : String(error);
    return message.replace(/[\r\n]+/g, " ").slice(0, 400);
}

function requestRateLimits(options = {}) {
    const timeoutMs = finiteNumber(options.timeoutMs) || APP_SERVER_TIMEOUT_MS;
    const codexBinary = options.codexBinary || process.env.CODEX_USAGE_CODEX_BIN || "codex";

    return new Promise((resolve, reject) => {
        let child;
        try {
            child = spawn(codexBinary, ["app-server", "--stdio"], {
                stdio: ["pipe", "pipe", "pipe"],
                windowsHide: true,
            });
        } catch (error) {
            reject(new CodexUsageError(`Could not start Codex app-server: ${compactError(error)}`));
            return;
        }

        let completed = false;
        let stdoutBuffer = "";
        let stderrBuffer = "";
        let nextId = 1;
        const pending = new Map();

        const finish = (error, result) => {
            if (completed) {
                return;
            }
            completed = true;
            clearTimeout(timer);

            try {
                child.stdin.end();
            } catch {
                // The child may have already exited.
            }
            const terminateTimer = setTimeout(() => {
                if (!child.killed) {
                    child.kill();
                }
            }, 300);
            terminateTimer.unref();

            if (error) {
                reject(error);
            } else {
                resolve(result);
            }
        };

        const timer = setTimeout(() => {
            finish(new CodexUsageError(`Codex app-server did not answer within ${timeoutMs} ms.`));
        }, timeoutMs);
        timer.unref();

        const send = (message) => {
            child.stdin.write(`${JSON.stringify(message)}\n`);
        };

        const request = (method, params = {}) => new Promise((resolveRequest, rejectRequest) => {
            const id = nextId;
            nextId += 1;
            pending.set(String(id), { resolve: resolveRequest, reject: rejectRequest });
            send({ id, method, params });
        });

        const handleMessage = (message) => {
            if (!Object.prototype.hasOwnProperty.call(message, "id")) {
                return;
            }
            const handler = pending.get(String(message.id));
            if (!handler) {
                return;
            }
            pending.delete(String(message.id));
            if (message.error) {
                handler.reject(new CodexUsageError(
                    `Codex app-server ${message.error.code || "error"}: ${message.error.message || "unknown error"}`,
                ));
                return;
            }
            handler.resolve(message.result);
        };

        const run = async () => {
            try {
                await request("initialize", {
                    clientInfo: {
                        name: "codex-usage-mcp",
                        title: "Codex Usage MCP",
                        version: "1.0.0",
                    },
                });
                send({ method: "initialized", params: {} });
                const response = await request("account/rateLimits/read");
                finish(null, response || {});
            } catch (error) {
                finish(error instanceof CodexUsageError
                    ? error
                    : new CodexUsageError(`Could not read Codex limits: ${compactError(error)}`));
            }
        };

        child.stdout.setEncoding("utf8");
        child.stdout.on("data", (chunk) => {
            stdoutBuffer += chunk;
            let lineEnd = stdoutBuffer.indexOf("\n");
            while (lineEnd !== -1) {
                const line = stdoutBuffer.slice(0, lineEnd).trim();
                stdoutBuffer = stdoutBuffer.slice(lineEnd + 1);
                if (line) {
                    try {
                        handleMessage(JSON.parse(line));
                    } catch {
                        // App-server notifications are JSON; ignore malformed diagnostic lines.
                    }
                }
                lineEnd = stdoutBuffer.indexOf("\n");
            }
        });

        child.stderr.setEncoding("utf8");
        child.stderr.on("data", (chunk) => {
            stderrBuffer = `${stderrBuffer}${chunk}`.slice(-1_000);
        });

        child.once("error", (error) => {
            finish(new CodexUsageError(`Could not start Codex app-server: ${compactError(error)}`));
        });
        child.once("close", (code) => {
            if (!completed) {
                const details = stderrBuffer.trim() ? ` ${stderrBuffer.trim()}` : "";
                finish(new CodexUsageError(`Codex app-server exited before replying (code ${code}).${details}`));
            }
        });

        run();
    });
}

function isTrue(value) {
    return value === true || value === "true" || value === 1 || value === "1";
}

function buildClaudeHookOutput(eventName, report, stopHookActive) {
    if (eventName === "Stop") {
        if (isTrue(stopHookActive)) {
            return null;
        }
        return {
            hookSpecificOutput: {
                hookEventName: "Stop",
                additionalContext: `Append this exact compact footer to the completed answer, then stop without calling tools: ${report}`,
            },
        };
    }

    return {
        hookSpecificOutput: {
            hookEventName: eventName,
            additionalContext: `Current Codex account usage: ${report}`,
        },
    };
}

function buildCodexStopHookOutput(report, stopHookActive) {
    if (isTrue(stopHookActive)) {
        return null;
    }
    return {
        decision: "block",
        reason: `Append this exact compact footer to the completed answer, then stop without calling tools: ${report}`,
    };
}

async function readHookInput() {
    let text = "";
    for await (const chunk of process.stdin) {
        text += chunk;
    }
    if (!text.trim()) {
        return {};
    }
    try {
        return JSON.parse(text);
    } catch (error) {
        throw new CodexUsageError(`Hook input is not valid JSON: ${compactError(error)}`);
    }
}

async function runHook(mode) {
    let input;
    try {
        input = await readHookInput();
        const raw = await requestRateLimits();
        const report = formatUsageReport(normalizeRateLimits(raw));
        let output;

        if (mode === "claude-stop") {
            output = buildClaudeHookOutput("Stop", report, input.stop_hook_active);
        } else if (mode === "claude-post-tool-use") {
            output = buildClaudeHookOutput("PostToolUse", report, false);
        } else if (mode === "claude-post-tool-batch") {
            output = buildClaudeHookOutput("PostToolBatch", report, false);
        } else if (mode === "codex-stop") {
            output = buildCodexStopHookOutput(report, input.stop_hook_active);
        } else {
            throw new CodexUsageError(`Unknown hook mode: ${mode}`);
        }

        if (output) {
            process.stdout.write(`${JSON.stringify(output)}\n`);
        }
    } catch (error) {
        process.stdout.write(`${JSON.stringify({
            systemMessage: `Codex usage report unavailable: ${compactError(error)}`,
        })}\n`);
    }
}

function writeMcpMessage(message) {
    process.stdout.write(`${JSON.stringify(message)}\n`);
}

function mcpError(id, code, message) {
    writeMcpMessage({ jsonrpc: "2.0", id, error: { code, message } });
}

function toolDefinition() {
    return {
        name: TOOL_NAME,
        description: "Read current rate-limit windows for the ChatGPT account signed into local Codex. This is read-only and does not consume or reset a limit.",
        inputSchema: {
            type: "object",
            properties: {
                format: {
                    type: "string",
                    enum: ["summary", "json"],
                    description: "Return a compact human-readable summary (default) or normalized JSON.",
                },
                hook_event: {
                    type: "string",
                    enum: ["PostToolUse", "PostToolBatch", "Stop"],
                    description: "Optional Claude Code hook event. When present, returns hook-compatible JSON as text.",
                },
                stop_hook_active: {
                    description: "Pass the Stop hook's stop_hook_active field to avoid a continuation loop.",
                },
            },
            additionalProperties: false,
        },
        annotations: {
            readOnlyHint: true,
            destructiveHint: false,
            idempotentHint: true,
        },
    };
}

async function handleMcpRequest(request) {
    const { id, method, params = {} } = request;
    if (typeof id === "undefined") {
        return;
    }

    if (method === "initialize") {
        writeMcpMessage({
            jsonrpc: "2.0",
            id,
            result: {
                protocolVersion: MCP_PROTOCOL_VERSION,
                capabilities: { tools: { listChanged: false } },
                serverInfo: { name: "codex-usage", version: "1.0.0" },
            },
        });
        return;
    }

    if (method === "ping") {
        writeMcpMessage({ jsonrpc: "2.0", id, result: {} });
        return;
    }

    if (method === "tools/list") {
        writeMcpMessage({ jsonrpc: "2.0", id, result: { tools: [toolDefinition()] } });
        return;
    }

    if (method !== "tools/call") {
        mcpError(id, -32601, `Method not found: ${method}`);
        return;
    }

    if (params.name !== TOOL_NAME) {
        mcpError(id, -32602, `Unknown tool: ${params.name}`);
        return;
    }

    try {
        const argumentsObject = params.arguments || {};
        const raw = await requestRateLimits();
        const normalized = normalizeRateLimits(raw);
        const report = formatUsageReport(normalized);
        let text;

        if (argumentsObject.hook_event) {
            const hookOutput = buildClaudeHookOutput(
                argumentsObject.hook_event,
                report,
                argumentsObject.stop_hook_active,
            );
            text = hookOutput ? JSON.stringify(hookOutput) : "";
        } else if (argumentsObject.format === "json") {
            text = JSON.stringify(normalized);
        } else {
            text = report;
        }

        writeMcpMessage({
            jsonrpc: "2.0",
            id,
            result: { content: [{ type: "text", text }] },
        });
    } catch (error) {
        writeMcpMessage({
            jsonrpc: "2.0",
            id,
            result: {
                isError: true,
                content: [{ type: "text", text: `Codex usage is unavailable: ${compactError(error)}` }],
            },
        });
    }
}

function runMcpServer() {
    const lineReader = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    lineReader.on("line", (line) => {
        if (!line.trim()) {
            return;
        }
        let request;
        try {
            request = JSON.parse(line);
        } catch {
            mcpError(null, -32700, "Invalid JSON-RPC message.");
            return;
        }

        if (request.method === "notifications/initialized" || typeof request.id === "undefined") {
            return;
        }
        void handleMcpRequest(request);
    });
}

function printHelp() {
    process.stdout.write([
        "Usage:",
        "  node codex-usage-mcp.js                 Start the local stdio MCP server.",
        "  node codex-usage-mcp.js --report        Print a compact rate-limit summary.",
        "  node codex-usage-mcp.js --report --json Print normalized JSON without account identifiers.",
        "  node codex-usage-mcp.js --hook <mode>   Run a hook adapter.",
        "",
        "Hook modes: claude-stop, claude-post-tool-use, claude-post-tool-batch, codex-stop",
        "",
    ].join("\n"));
}

async function main(argv) {
    if (argv.includes("--help") || argv.includes("-h")) {
        printHelp();
        return;
    }

    const hookIndex = argv.indexOf("--hook");
    if (hookIndex !== -1) {
        const mode = argv[hookIndex + 1];
        if (!mode) {
            throw new CodexUsageError("--hook requires a mode.");
        }
        await runHook(mode);
        return;
    }

    if (argv.includes("--report")) {
        const raw = await requestRateLimits();
        const normalized = normalizeRateLimits(raw);
        process.stdout.write(`${argv.includes("--json")
            ? JSON.stringify(normalized)
            : formatUsageReport(normalized)}\n`);
        return;
    }

    runMcpServer();
}

if (require.main === module) {
    main(process.argv.slice(2)).catch((error) => {
        process.stderr.write(`codex-usage-mcp: ${compactError(error)}\n`);
        process.exitCode = 1;
    });
}

module.exports = {
    buildClaudeHookOutput,
    buildCodexStopHookOutput,
    formatDuration,
    formatUsageReport,
    normalizeRateLimits,
};
