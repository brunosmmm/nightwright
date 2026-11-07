"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

const PI_EXEC = path.join(__dirname, "..", "..", "src", "nightwright", "agentic", "pi_exec.mjs");

function layout(over = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-run-"));
  const doc = {
    pi_provider: "anthropic",
    pi_id: "claude-sonnet-4-6",
    pi_thinking: "high",
    read_only: false,
    cwd: dir,
    persona: "implementer",
    system_append: "INVARIANT",
    json_schema: { type: "object", properties: { status: { type: "string" } } },
    ...over,
  };
  fs.writeFileSync(path.join(dir, "request.json"), JSON.stringify(doc));
  fs.writeFileSync(path.join(dir, "prompt.md"), `task text\n\nINVARIANT`);
  return dir;
}

function loadExecute() {
  return import(PI_EXEC).then((m) => m.execute);
}

function chunksToLines(chunks) {
  return chunks.join("").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

function stubSession(over = {}) {
  const subs = [];
  const session = {
    state: {
      messages: over.messages ?? [
        {
          role: "assistant",
          stopReason: "stop",
          content: [{ type: "text", text: '{"status":"done","summary":"ok"}' }],
          usage: over.usage ?? { cost: { total: 0.02 } },
        },
      ],
    },
    subscribe(fn) {
      subs.push(fn);
      for (const ev of over.events ?? []) fn(ev);
      return () => {};
    },
    prompt: async (msg) => {
      over.promptText = msg;
      if (over.promptReject) throw over.promptReject;
      return undefined;
    },
    dispose() {
      over.disposed = true;
    },
    abort: over.abort ?? (() => {}),
    sessionId: "sess-1",
  };
  session._emit = (ev) => subs.forEach((fn) => fn(ev));
  return session;
}

const SENTINEL = "pi-test-secret-key-xyzzy-999";

function stubPi(over = {}) {
  const session = stubSession(over);
  const model = {
    provider: "anthropic",
    id: "claude-sonnet-4-6",
    cost: over.modelCost ?? { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 },
  };
  const runtime = {
    getModel: () => (over.noModel ? null : model),
    checkAuth: async () => (over.auth === false ? null : { type: "api_key", source: "ANTHROPIC_API_KEY" }),
    getAuth: async () =>
      over.getAuth ?? {
        auth: { apiKey: SENTINEL },
      },
  };
  const pi = {
    ModelRuntime: {
      create: async (opts) => {
        over.modelRuntimeCreate = opts;
        return runtime;
      },
    },
    SettingsManager: {
      inMemory: (settings, opts) => {
        over.settingsCall = { settings, opts };
        return { tag: "in-memory-settings" };
      },
    },
    SessionManager: {
      inMemory: (cwd) => {
        over.sessionCwd = cwd;
        return { getHeader: () => ({ id: "sess-1" }) };
      },
    },
    DefaultResourceLoader: class {
      constructor(opts) {
        this.opts = opts;
        over.loaderOpts = opts;
      }
      async reload() {}
    },
    createAgentSession: async (opts) => {
      over.createOpts = opts;
      return { session };
    },
  };
  return { pi, session, runtime, over };
}

test("execute: in-memory session, settings, loader flags, appendSystemPrompt", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const doc = JSON.parse(fs.readFileSync(path.join(runDir, "request.json"), "utf8"));
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi, over } = stubPi();
  const code = await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 0);
  assert.equal(over.sessionCwd, runDir);
  assert.deepEqual(over.settingsCall, {
    settings: { cacheWarming: "off", compaction: { enabled: false } },
    opts: { projectTrusted: false },
  });
  assert.equal(typeof over.loaderOpts.agentsFilesOverride, "function");
  assert.notEqual(over.loaderOpts.agentDir, runDir);
  assert.ok(over.loaderOpts.agentDir);
  assert.equal(over.loaderOpts.noExtensions, true);
  assert.equal(over.loaderOpts.noSkills, true);
  assert.equal(over.loaderOpts.noPromptTemplates, true);
  assert.equal(over.loaderOpts.noThemes, true);
  assert.equal(over.loaderOpts.noContextFiles, false);
  assert.deepEqual(over.loaderOpts.appendSystemPrompt, ["INVARIANT"]);
  assert.equal(over.createOpts.thinkingLevel, "high");
  assert.deepEqual(over.createOpts.tools, ["read", "bash", "edit", "write"]);
  assert.equal(over.createOpts.settingsManager.tag, "in-memory-settings");
  assert.equal(over.createOpts.jsonSchema, undefined);
  assert.equal(over.createOpts.responseFormat, undefined);
  assert.ok(over.disposed);
  const prompt = over.promptText;
  assert.equal((prompt.match(/INVARIANT/g) || []).length, 1);
  assert.ok(prompt.includes("Reply with a single JSON object"));
  const schemaBody = JSON.stringify(doc.json_schema, null, 2);
  assert.equal((prompt.match(new RegExp(schemaBody.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g")) || []).length, 1);
});

test("execute: read-only tools", async () => {
  const execute = await loadExecute();
  const runDir = layout({ read_only: true });
  const { pi, over } = stubPi();
  await execute(runDir, {
    out: { write() {} },
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.deepEqual(over.createOpts.tools, ["read", "grep", "find", "ls"]);
});

test("execute: toolResult message_end emits user tool_result line", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const toolEv = {
    type: "message_end",
    message: {
      role: "toolResult",
      toolCallId: "call-abc",
      isError: false,
      content: [{ type: "text", text: "file contents here" }],
    },
  };
  const { pi } = stubPi({
    events: [toolEv],
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "done" }],
        usage: { cost: { total: 0.01 } },
      },
    ],
    modelCost: { input: 1 },
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: { getModel: () => ({ cost: { input: 1 } }), checkAuth: async () => ({}), getAuth: async () => ({}) },
  });
  const lines = chunksToLines(out.chunks);
  const tr = lines.find((l) => l.type === "user");
  assert.deepEqual(tr, {
    type: "user",
    message: {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: "call-abc",
          content: "file contents here",
          is_error: false,
        },
      ],
    },
  });
  const last = lines.at(-1);
  assert.equal(last.total_cost_usd, 0.01);
});

test("execute: assistant tool_use in stream", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const asstEv = {
    type: "message_end",
    message: {
      role: "assistant",
      content: [
        { type: "text", text: "reading" },
        { type: "toolCall", id: "t1", name: "read", arguments: { path: "x" } },
      ],
    },
  };
  const { pi } = stubPi({ events: [asstEv] });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const lines = chunksToLines(out.chunks);
  const asst = lines.find((l) => l.type === "assistant");
  assert.deepEqual(asst, {
    type: "assistant",
    message: {
      role: "assistant",
      content: [
        { type: "text", text: "reading" },
        { type: "tool_use", id: "t1", name: "read", input: { path: "x" } },
      ],
    },
  });
});

test("execute: stopReason length is success exit 0", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const verdict = '{"status":"done","summary":"ok"}';
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "length",
        content: [{ type: "text", text: verdict }],
        usage: { cost: { total: 0 } },
      },
    ],
    modelCost: { input: 1 },
  });
  const code = await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 0);
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.is_error, false);
  assert.equal(last.result, verdict);
});

test("execute: prompt failure scrubs sentinel from stdout and stderr", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const err = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({ promptReject: new Error(`provider failed: ${SENTINEL}`) });
  const code = await execute(runDir, {
    out,
    err,
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 1);
  const blob = out.chunks.join("") + err.chunks.join("");
  assert.ok(!blob.includes(SENTINEL));
  assert.ok(blob.includes("[redacted]"));
});

test("execute: no credential does not call prompt", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  let prompted = false;
  const { pi, session } = stubPi({ auth: false });
  session.prompt = async () => {
    prompted = true;
  };
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const code = await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: { getModel: () => ({}), checkAuth: async () => null, getAuth: async () => null },
  });
  assert.equal(code, 1);
  assert.equal(prompted, false);
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.is_error, true);
});

test("execute: unknown model does not call prompt", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  let prompted = false;
  const { pi, session } = stubPi({ noModel: true });
  session.prompt = async () => {
    prompted = true;
  };
  const code = await execute(runDir, {
    out: { write() {} },
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: { getModel: () => null, checkAuth: async () => ({}), getAuth: async () => ({}) },
  });
  assert.equal(code, 1);
  assert.equal(prompted, false);
});

test("execute: agent stopReason error writes stderr and exits 1", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const err = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "error",
        errorMessage: "rate limited",
        content: [],
        usage: { cost: { total: 0 } },
      },
    ],
  });
  const code = await execute(runDir, {
    out,
    err,
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 1);
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.is_error, true);
  assert.ok(err.chunks.join("").includes("rate limited"));
});

test("execute: priced cost sum ignores tool_result without usage", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const toolEv = {
    type: "message_end",
    message: {
      role: "toolResult",
      toolCallId: "t",
      isError: false,
      content: [{ type: "text", text: "ok" }],
    },
  };
  const { pi } = stubPi({
    events: [toolEv],
    messages: [
      { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "a" }],
        usage: { cost: { total: 0.01 } } },
      { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "done" }],
        usage: { cost: { total: 0.02 } } },
    ],
    modelCost: { input: 1 },
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, 0.03);
});

test("execute: priced sum zero stored as 0", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "x" }],
        usage: { cost: { total: 0, totalFromProvider: true } },
      },
    ],
    modelCost: { input: 1 },
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, 0);
});

test("execute: unpriced catalog leaves total_cost_usd null", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "x" }],
        usage: { cost: { total: 0 } },
      },
    ],
    modelCost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, null);
  assert.ok(last.usage.input_tokens >= 0);
});

test("execute: missing usage.cost.total nulls total_cost_usd", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "x" }],
        usage: { input: 10, output: 5 },
      },
    ],
    modelCost: { input: 1 },
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, null);
  assert.equal(last.usage.input_tokens, 10);
});

test("execute: startup import failure does not call prompt", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const code = await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => {
      throw new Error("no package");
    },
  });
  assert.equal(code, 1);
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.is_error, true);
});

test("assistant event with U+2028 in tool input is one JSON line", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const asstEv = {
    type: "message_end",
    message: {
      role: "assistant",
      content: [
        {
          type: "toolCall",
          id: "1",
          name: "read",
          arguments: { note: "a\u2028b" },
        },
      ],
    },
  };
  const { pi } = stubPi({ events: [asstEv] });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const raw = out.chunks.find((c) => c.includes("tool_use"));
  assert.ok(raw);
  assert.equal(raw.trimEnd().split("\n").length, 1);
  assert.ok(raw.includes("\u2028") || raw.includes("\\u2028"));
});

test("tier catalog rate yields numeric total_cost_usd", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "x" }],
        usage: { cost: { total: 0.05 } },
      },
    ],
    modelCost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, tiers: [{ inputTokensAbove: 0, input: 2 }] },
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, 0.05);
});

test("execute: parallel toolResult same ms both emit", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const ts = 1;
  const events = [
    {
      type: "message_end",
      message: {
        role: "toolResult",
        toolCallId: "a",
        timestamp: ts,
        content: [{ type: "text", text: "one" }],
      },
    },
    {
      type: "message_end",
      message: {
        role: "toolResult",
        toolCallId: "b",
        timestamp: ts,
        content: [{ type: "text", text: "two" }],
      },
    },
  ];
  const { pi } = stubPi({ events });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const lines = chunksToLines(out.chunks).filter((l) => l.type === "user");
  assert.equal(lines.length, 2);
});

test("execute: thinking block is not written to stdout", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const asstEv = {
    type: "message_end",
    message: {
      role: "assistant",
      content: [
        { type: "thinking", text: "secret chain of thought" },
        { type: "text", text: '{"status":"done"}' },
      ],
    },
  };
  const { pi } = stubPi({ events: [asstEv] });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const blob = out.chunks.join("");
  assert.ok(!blob.includes("secret chain of thought"));
  const lines = chunksToLines(out.chunks).filter((l) => l.type === "assistant");
  assert.equal(lines[0].message.content.length, 1);
  assert.equal(lines[0].message.content[0].type, "text");
});

test("execute: long verdict redacts without length cap", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const padding = "x".repeat(2100);
  const verdictObj = { status: "done", summary: "ok", padding, secret: SENTINEL };
  const verdict = JSON.stringify(verdictObj);
  assert.ok(verdict.length > 2000);
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: verdict }],
        usage: { cost: { total: 0.01 } },
      },
    ],
    modelCost: { input: 1 },
    getAuth: { auth: { apiKey: SENTINEL } },
  });
  const code = await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 0);
  const last = JSON.parse(out.chunks.at(-1));
  assert.ok(last.result.length > 2000);
  assert.ok(last.result.includes("[redacted]"));
  assert.ok(!last.result.includes(SENTINEL));
  const parsed = JSON.parse(last.result);
  assert.equal(parsed.status, "done");
  assert.equal(parsed.secret, "[redacted]");
});

test("execute: all-zero catalog with totalFromProvider stores 0", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "x" }],
        usage: { cost: { total: 0, totalFromProvider: true } },
      },
    ],
    modelCost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, 0);
});

test("execute: no assistant message leaves total_cost_usd null", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [],
    events: [
      {
        type: "message_end",
        message: {
          role: "assistant",
          stopReason: "error",
          errorMessage: "fail",
          content: [],
        },
      },
    ],
  });
  const code = await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 1);
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, null);
});

test("execute: ModelRuntime.create uses modelsPath null without deps", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi, over } = stubPi();
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
  });
  assert.deepEqual(over.modelRuntimeCreate, { modelsPath: null });
});

test("execute: catalog-priced zero without totalFromProvider stores 0", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "x" }],
        usage: { cost: { total: 0 } },
      },
    ],
    modelCost: { input: 0.000003, output: 0, cacheRead: 0, cacheWrite: 0 },
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, 0);
});

test("execute: sparse unpriced catalog leaves total_cost_usd null", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "x" }],
        usage: { cost: { total: 0 } },
      },
    ],
    modelCost: {},
  });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, null);
});

test("execute: stream lines scrub assistant text secrets", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const asstEv = {
    type: "message_end",
    message: {
      role: "assistant",
      content: [{ type: "text", text: `leaked ${SENTINEL} here` }],
    },
  };
  const { pi } = stubPi({ events: [asstEv] });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const blob = out.chunks.join("");
  assert.ok(!blob.includes(SENTINEL));
  assert.ok(blob.includes("[redacted]"));
});

test("probeProviders: missing credential lists missing and does not call model", async () => {
  const { probeProviders } = await import(PI_EXEC);
  let modelCalls = 0;
  const runtime = {
    checkAuth: async (p) => (p === "anthropic" ? { type: "api_key" } : null),
    getAuth: async () => {
      modelCalls += 1;
      return { auth: { apiKey: SENTINEL } };
    },
  };
  const rec = await probeProviders(["anthropic", "openai"], {
    importPi: async () => ({ ModelRuntime: { create: async () => runtime } }),
  });
  assert.equal(rec.ok, false);
  assert.deepEqual(rec.missing, ["openai"]);
  assert.equal(rec.error, null);
  assert.equal(modelCalls, 0);
  assert.ok(!JSON.stringify(rec).includes(SENTINEL));
});

test("probeProviders: import failure sets error and ok false", async () => {
  const { probeProviders } = await import(PI_EXEC);
  const rec = await probeProviders(["anthropic"], {
    importPi: async () => {
      throw new Error("no package");
    },
  });
  assert.equal(rec.ok, false);
  assert.ok(rec.error);
});

test("execute: SIGTERM handler kills detached children and aborts", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  let aborted = false;
  let killed = false;
  const { pi } = stubPi({ abort: () => { aborted = true; } });
  let termHandler;
  const origOn = process.on.bind(process);
  process.on = (ev, fn) => {
    if (ev === "SIGTERM") termHandler = fn;
    return origOn(ev, fn);
  };
  try {
    await execute(runDir, {
      out: { write() {} },
      err: { write() {} },
      importPi: async () => pi,
      modelRuntime: await pi.ModelRuntime.create(),
      killTrackedDetachedChildren: () => {
        killed = true;
      },
    });
    assert.ok(termHandler);
    termHandler();
    assert.ok(killed);
    assert.ok(aborted);
  } finally {
    process.on = origOn;
  }
});
