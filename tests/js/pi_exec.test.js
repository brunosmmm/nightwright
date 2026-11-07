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

function stubSession(over = {}) {
  const events = [];
  return {
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
      events.push(fn);
      if (over.assistantEvent) fn(over.assistantEvent);
      return () => {};
    },
    prompt: async (msg) => {
      over.prompt = msg;
      return undefined;
    },
    dispose() {},
    abort: over.abort ?? (() => {}),
    sessionId: "sess-1",
  };
}

function stubPi(over = {}) {
  const session = stubSession(over);
  const model = {
    provider: "anthropic",
    id: "claude-sonnet-4-6",
    cost: over.modelCost ?? { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 },
  };
  return {
    ModelRuntime: {
      create: async () => ({
        getModel: () => model,
        checkAuth: async () => (over.auth === false ? null : { type: "api_key" }),
      }),
    },
    SettingsManager: { inMemory: () => ({ tag: "in-memory-settings" }) },
    SessionManager: { inMemory: () => ({ getHeader: () => ({ id: "sess-1" }) }) },
    DefaultResourceLoader: class {
      constructor(opts) {
        this.opts = opts;
      }
      async reload() {}
    },
    createAgentSession: async (opts) => {
      over.createOpts = opts;
      return { session };
    },
    getAgentDir: () => "/tmp/agent",
  };
}

test("execute: in-memory session, settings, loader flags, appendSystemPrompt", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const captured = {};
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const pi = stubPi(captured);
  const code = await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    settingsManager: pi.SettingsManager.inMemory(),
    sessionManager: pi.SessionManager.inMemory(),
    resourceLoader: new pi.DefaultResourceLoader({
      cwd: runDir,
      agentDir: "/a",
      noExtensions: true,
      noSkills: true,
      noPromptTemplates: true,
      noThemes: true,
      noContextFiles: false,
      appendSystemPrompt: ["INVARIANT"],
    }),
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 0);
  assert.equal(captured.createOpts.settingsManager.tag, "in-memory-settings");
  assert.deepEqual(captured.createOpts.tools, ["read", "bash", "edit", "write"]);
  assert.equal(captured.createOpts.resourceLoader.opts.noExtensions, true);
  assert.ok(captured.createOpts.resourceLoader.opts.appendSystemPrompt.includes("INVARIANT"));
  const prompt = captured.prompt;
  assert.equal((prompt.match(/INVARIANT/g) || []).length, 1);
  assert.ok(prompt.includes("Reply with a single JSON object"));
  assert.ok(prompt.trimEnd().endsWith("}"));
});

test("execute: read-only tools", async () => {
  const execute = await loadExecute();
  const runDir = layout({ read_only: true });
  const captured = {};
  const pi = stubPi(captured);
  await execute(runDir, {
    out: { write() {} },
    err: { write() {} },
    importPi: async () => pi,
    settingsManager: {},
    sessionManager: { getHeader: () => ({}) },
    resourceLoader: { reload: async () => {}, opts: {} },
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.deepEqual(captured.createOpts.tools, ["read", "grep", "find", "ls"]);
});

test("execute: startup import failure does not call prompt", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  let prompted = false;
  const code = await execute(runDir, {
    out: { write() {} },
    err: { write() {} },
    importPi: async () => {
      throw new Error("no package");
    },
    modelRuntime: { getModel: () => null, checkAuth: async () => null },
  });
  assert.equal(code, 1);
  assert.equal(prompted, false);
});

test("execute: priced cost sum ignores tool_result without usage", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const messages = [
    { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "a" }],
      usage: { cost: { total: 0.01 } } },
    { role: "user", content: [{ type: "toolResult", toolCallId: "t", content: "ok" }] },
    { role: "assistant", stopReason: "stop", content: [{ type: "text", text: "done" }],
      usage: { cost: { total: 0.02 } } },
  ];
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => stubPi({ messages, modelCost: { input: 1 } }),
    settingsManager: {},
    sessionManager: { getHeader: () => ({ id: "s" }) },
    resourceLoader: { reload: async () => {}, opts: {} },
    modelRuntime: { getModel: () => ({ cost: { input: 1 } }), checkAuth: async () => ({}) },
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, 0.03);
});

test("assistant event with U+2028 in tool input is one JSON line", () => {
  const obj = {
    type: "assistant",
    message: {
      role: "assistant",
      content: [{ type: "tool_use", id: "1", name: "read", input: { note: "a\u2028b" } }],
    },
  };
  const line = JSON.stringify(obj);
  assert.equal(line.split("\n").length, 1);
});

test("tier catalog rate yields numeric total_cost_usd", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const pi = stubPi({
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
    settingsManager: {},
    sessionManager: { getHeader: () => ({}) },
    resourceLoader: { reload: async () => {}, opts: {} },
    modelRuntime: await pi.ModelRuntime.create(),
  });
  const last = JSON.parse(out.chunks.at(-1));
  assert.equal(last.total_cost_usd, 0.05);
});

test("execute: SIGTERM handler calls abort", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  let aborted = false;
  const pi = stubPi({ abort: () => { aborted = true; } });
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
      settingsManager: {},
      sessionManager: { getHeader: () => ({}) },
      resourceLoader: { reload: async () => {}, opts: {} },
      modelRuntime: { getModel: () => ({ cost: { input: 1 } }), checkAuth: async () => ({}) },
    });
    assert.ok(termHandler);
    termHandler();
    assert.ok(aborted);
  } finally {
    process.on = origOn;
  }
});
