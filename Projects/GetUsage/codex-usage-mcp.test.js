"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
    buildClaudeHookOutput,
    buildCodexStopHookOutput,
    formatUsageReport,
    normalizeRateLimits,
} = require("./codex-usage-mcp.js");

const SAMPLE_RESPONSE = {
    ordinaryUsageAllowed: true,
    rateLimitsByLimitId: {
        codex: {
            limitId: "codex",
            primary: {
                usedPercent: 1,
                windowDurationMins: 300,
                resetsAt: 1_794_885_748,
            },
            secondary: {
                usedPercent: 0,
                windowDurationMins: 10_080,
                resetsAt: 1_795_453_979,
            },
        },
    },
    rateLimitResetCredits: { availableCount: 3 },
};

test("normalizes remaining quota without returning account identity", () => {
    const normalized = normalizeRateLimits(SAMPLE_RESPONSE, { locale: "en-US" });

    assert.equal(normalized.buckets.length, 1);
    assert.equal(normalized.buckets[0].windows[0].duration, "5h");
    assert.equal(normalized.buckets[0].windows[0].remainingPercent, 99);
    assert.equal(normalized.buckets[0].windows[1].duration, "7d");
    assert.equal(normalized.buckets[0].windows[1].remainingPercent, 100);
    assert.equal(normalized.resetCredits.availableCount, 3);
    assert.equal(JSON.stringify(normalized).includes("accountId"), false);
});

test("formats a compact human-readable report", () => {
    const report = formatUsageReport(normalizeRateLimits(SAMPLE_RESPONSE, { locale: "en-US" }));

    assert.match(report, /5h: 99% remaining/);
    assert.match(report, /7d: 100% remaining/);
    assert.match(report, /earned full resets: 3/);
});

test("Claude Stop hook continues exactly once", () => {
    const first = buildClaudeHookOutput("Stop", "Codex limits — 5h: 99% remaining", false);
    const second = buildClaudeHookOutput("Stop", "Codex limits — 5h: 99% remaining", true);

    assert.equal(first.hookSpecificOutput.hookEventName, "Stop");
    assert.match(first.hookSpecificOutput.additionalContext, /Append this exact compact footer/);
    assert.equal(second, null);
});

test("Codex Stop hook uses a guarded continuation", () => {
    const first = buildCodexStopHookOutput("Codex limits — 5h: 99% remaining", false);
    const second = buildCodexStopHookOutput("Codex limits — 5h: 99% remaining", true);

    assert.equal(first.decision, "block");
    assert.match(first.reason, /Codex limits/);
    assert.equal(second, null);
});
