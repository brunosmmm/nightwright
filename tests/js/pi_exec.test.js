"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { pathToFileURL } = require("node:url");

const PI_EXEC = path.join(__dirname, "..", "..", "src", "nightwright", "agentic", "pi_exec.mjs");

function layout(over = {}) {
  const runDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-run-"));
  const worktree = fs.mkdtempSync(path.join(os.tmpdir(), "pi-wt-"));
  const doc = {
    pi_provider: "anthropic",
    pi_id: "claude-sonnet-4-6",
    pi_thinking: "high",
    read_only: false,
    cwd: worktree,
    persona: "implementer",
    system_append: "INVARIANT",
    json_schema: { type: "object", properties: { status: { type: "string" } } },
    ...over,
  };
  fs.writeFileSync(path.join(runDir, "request.json"), JSON.stringify(doc));
  fs.writeFileSync(path.join(runDir, "prompt.md"), `task text\n\nINVARIANT`);
  return runDir;
}

function worktreeCwd(runDir) {
  return JSON.parse(fs.readFileSync(path.join(runDir, "request.json"), "utf8")).cwd;
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
    prompt: async (msg, ...extra) => {
      over.promptCalls = (over.promptCalls ?? 0) + 1;
      over.promptText = msg;
      over.promptExtra = extra;
      if (over.promptHook) over.promptHook();
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
  const model = over.model ?? {
    provider: "anthropic",
    id: "claude-sonnet-4-6",
    cost: over.modelCost ?? { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 },
  };
  const runtime = {
    getModel: (provider, id) => {
      over.getModelCall = { provider, id };
      return over.noModel ? null : model;
    },
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
      over.createSessionCalls = (over.createSessionCalls ?? 0) + 1;
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
  const wt = worktreeCwd(runDir);
  assert.notEqual(runDir, wt);
  assert.equal(over.sessionCwd, wt);
  assert.equal(over.createSessionCalls, 1);
  assert.equal(over.promptCalls, 1);
  assert.equal(over.createOpts.cwd, wt);
  assert.equal(over.promptExtra.length, 0);
  for (const [k, v] of Object.entries(over.createOpts)) {
    if (typeof v === "function" && /approval|permission/i.test(k)) {
      assert.fail(`unexpected session callback ${k}`);
    }
  }
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
  assert.equal(over.loaderOpts.noContextFiles, true);
  assert.deepEqual(over.loaderOpts.appendSystemPrompt, ["INVARIANT"]);
  assert.equal(over.createOpts.thinkingLevel, "high");
  assert.deepEqual(over.createOpts.tools, ["read", "bash", "edit", "write"]);
  assert.equal(over.createOpts.settingsManager.tag, "in-memory-settings");
  assert.equal(over.loaderOpts.settingsManager, over.createOpts.settingsManager);
  assert.equal(over.createOpts.jsonSchema, undefined);
  assert.equal(over.createOpts.responseFormat, undefined);
  assert.equal(over.createOpts.schema, undefined);
  assert.equal(over.createOpts.json_schema, undefined);
  const inside = path.join(wt, "AGENTS.md");
  fs.writeFileSync(inside, "# in-worktree agents\n");
  const outside = path.join(path.dirname(wt), "parent-AGENTS.md");
  fs.writeFileSync(outside, "# outside\n");
  const filtered = over.loaderOpts.agentsFilesOverride({ agentsFiles: [] });
  assert.equal(filtered.agentsFiles.length, 1);
  assert.equal(filtered.agentsFiles[0].path, inside);
  assert.equal(filtered.agentsFiles[0].content.trim(), "# in-worktree agents");
  assert.ok(!filtered.agentsFiles.some((f) => f.path === outside));
  const secretOutside = fs.mkdtempSync(path.join(os.tmpdir(), "pi-secret-"));
  const secretFile = path.join(secretOutside, "secret-AGENTS.md");
  fs.writeFileSync(secretFile, "outside secret");
  fs.unlinkSync(inside);
  const linkInside = path.join(wt, "AGENTS.md");
  fs.symlinkSync(secretFile, linkInside);
  const symlinkFiltered = over.loaderOpts.agentsFilesOverride({ agentsFiles: [] });
  assert.equal(symlinkFiltered.agentsFiles.length, 0);
  assert.ok(over.disposed);
  const resultLine = chunksToLines(out.chunks).at(-1);
  assert.equal(resultLine.session_id, "sess-1");
  const prompt = over.promptText;
  assert.equal((prompt.match(/INVARIANT/g) || []).length, 1);
  assert.ok(prompt.includes("Reply with a single JSON object"));
  const schemaBody = JSON.stringify(doc.json_schema, null, 2);
  assert.equal((prompt.match(new RegExp(schemaBody.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g")) || []).length, 1);
});

test("execute: getModel and thinking from request fields", async () => {
  const execute = await loadExecute();
  const runDir = layout({
    pi_provider: "openai",
    pi_id: "gpt-5",
    pi_thinking: "low",
  });
  const custom = { provider: "openai", id: "gpt-5", cost: { input: 1 } };
  const { pi, over } = stubPi({ model: custom });
  await execute(runDir, {
    out: { write() {} },
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.deepEqual(over.getModelCall, { provider: "openai", id: "gpt-5" });
  assert.equal(over.createOpts.thinkingLevel, "low");
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

test("execute: toolResult with string content", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const toolEv = {
    type: "message_end",
    message: {
      role: "toolResult",
      toolCallId: "call-str",
      isError: false,
      content: "plain string body",
    },
  };
  const { pi } = stubPi({ events: [toolEv] });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: { getModel: () => ({ cost: { input: 1 } }), checkAuth: async () => ({}), getAuth: async () => ({}) },
  });
  const tr = chunksToLines(out.chunks).find((l) => l.type === "user");
  assert.equal(tr.message.content[0].content, "plain string body");
});

test("execute: schema footer preserves dollar signs", async () => {
  const execute = await loadExecute();
  const runDir = layout({
    json_schema: {
      type: "object",
      properties: { note: { type: "string", description: "use $9.99 style" } },
    },
  });
  const { pi, over } = stubPi();
  await execute(runDir, {
    out: { write() {} },
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.match(over.promptText, /\$9\.99/);
});

test("execute: createAgentSession throw writes is_error", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const session = stubSession();
  const pi = {
    ModelRuntime: {
      create: async () => ({
        getModel: () => ({ cost: { input: 1 } }),
        checkAuth: async () => ({}),
        getAuth: async () => ({}),
      }),
    },
    SettingsManager: { inMemory: () => ({}) },
    SessionManager: { inMemory: () => ({ getHeader: () => ({ id: "s" }) }) },
    DefaultResourceLoader: class {
      async reload() {}
    },
    createAgentSession: async () => {
      throw new Error("boom");
    },
  };
  const code = await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 1);
  const last = chunksToLines(out.chunks).at(-1);
  assert.equal(last.type, "result");
  assert.equal(last.is_error, true);
  assert.match(last.result, /session failed/);
});

test("execute: GAC escaped PEM scrubbed on gemini, client_email kept", async () => {
  const execute = await loadExecute();
  const runDir = layout({ pi_provider: "gemini", pi_id: "gemini-2.0" });
  const email = "gemini-svc@test.invalid";
  const pemSecret = "super-secret-pem-line-abcdefghij";
  const privateKey =
    `-----BEGIN PRIVATE KEY-----\\n${pemSecret}\\n-----END PRIVATE KEY-----\\n`;
  const gacPath = path.join(os.tmpdir(), `pi-gac-esc-${process.pid}.json`);
  fs.writeFileSync(
    gacPath,
    JSON.stringify({ client_email: email, private_key: privateKey }),
  );
  const prev = process.env.GOOGLE_APPLICATION_CREDENTIALS;
  process.env.GOOGLE_APPLICATION_CREDENTIALS = gacPath;
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const escapedNeedle = privateKey;
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [
          {
            type: "text",
            text: `svc ${email} cat shows ${escapedNeedle}`,
          },
        ],
        usage: { cost: { total: 0.01 } },
      },
    ],
  });
  try {
    await execute(runDir, {
      out,
      err: { write() {} },
      importPi: async () => pi,
      modelRuntime: {
        getModel: () => ({ cost: { input: 1 } }),
        checkAuth: async () => ({}),
        getAuth: async () => ({}),
      },
    });
    const blob = out.chunks.join("");
    assert.ok(blob.includes(email));
    assert.ok(!blob.includes(pemSecret));
    assert.ok(!blob.includes(escapedNeedle));
    assert.ok(blob.includes("[redacted]"));
  } finally {
    if (prev === undefined) delete process.env.GOOGLE_APPLICATION_CREDENTIALS;
    else process.env.GOOGLE_APPLICATION_CREDENTIALS = prev;
    try {
      fs.rmSync(gacPath, { force: true });
    } catch {
      /* ignore */
    }
  }
});

test("execute: GAC client_email not scrubbed on anthropic", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const email = "svc-acct-test@example.invalid";
  const gacPath = path.join(os.tmpdir(), `pi-gac-${process.pid}.json`);
  fs.writeFileSync(
    gacPath,
    JSON.stringify({
      client_email: email,
      private_key: "secret-private-key-material-xyz",
    }),
  );
  const prev = process.env.GOOGLE_APPLICATION_CREDENTIALS;
  process.env.GOOGLE_APPLICATION_CREDENTIALS = gacPath;
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const verdict = JSON.stringify({ status: "done", summary: `from ${email}` });
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: verdict }],
        usage: { cost: { total: 0.01 } },
      },
    ],
  });
  try {
    await execute(runDir, {
      out,
      err: { write() {} },
      importPi: async () => pi,
      modelRuntime: { getModel: () => ({ cost: { input: 1 } }), checkAuth: async () => ({}), getAuth: async () => ({}) },
    });
    const blob = out.chunks.join("");
    assert.ok(blob.includes(email));
  } finally {
    if (prev === undefined) delete process.env.GOOGLE_APPLICATION_CREDENTIALS;
    else process.env.GOOGLE_APPLICATION_CREDENTIALS = prev;
    try {
      fs.rmSync(gacPath, { force: true });
    } catch {
      /* ignore */
    }
  }
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

test("execute: scrub auth.token only", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const err = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    getAuth: { auth: { token: SENTINEL } },
    promptReject: new Error(`boom ${SENTINEL}`),
  });
  const code = await execute(runDir, {
    out,
    err,
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
  });
  assert.equal(code, 1);
  assert.ok(!out.chunks.join("").includes(SENTINEL));
  assert.ok(!err.chunks.join("").includes(SENTINEL));
});

test("execute: scrub OAuth header value only", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const bearer = `Bearer ${SENTINEL}`;
  const { pi } = stubPi({
    getAuth: { auth: { headers: { Authorization: bearer } } },
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: `echo ${bearer}` }],
        usage: { cost: { total: 0.01 } },
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
  const blob = out.chunks.join("");
  assert.ok(!blob.includes(SENTINEL));
  assert.ok(!blob.includes(bearer));
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

test("execute: prompt failure scrubs secret learned during prompt", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const err = { chunks: [], write(s) { this.chunks.push(s); } };
  const LATE = "oauth-late-refresh-token-abcdef";
  let getAuthCalls = 0;
  const runtime = {
    getModel: () => ({ cost: { input: 1 } }),
    checkAuth: async () => ({ type: "oauth" }),
    getAuth: async () => {
      getAuthCalls += 1;
      if (getAuthCalls === 1) return { auth: { apiKey: SENTINEL } };
      return { auth: { accessToken: LATE } };
    },
  };
  const { pi } = stubPi({
    promptReject: new Error(`refresh failed: ${LATE}`),
  });
  const code = await execute(runDir, {
    out,
    err,
    importPi: async () => pi,
    modelRuntime: runtime,
  });
  assert.equal(code, 1);
  assert.ok(getAuthCalls >= 2);
  const blob = out.chunks.join("") + err.chunks.join("");
  assert.ok(!blob.includes(LATE));
  assert.ok(!blob.includes(SENTINEL));
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

test("execute: stopReason aborted is error exit 1", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const err = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "aborted",
        content: [{ type: "text", text: "stopped" }],
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
  assert.equal(last.subtype, "error");
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
      { role: "toolResult", content: [{ type: "text", text: "ignored" }] },
      { role: "user", content: [{ type: "text", text: "nudge" }] },
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

test("execute: usage sums cache fields and Claude aliases", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "a" }],
        usage: { input: 1, output: 2, cacheRead: 3, cacheWrite: 4, cost: { total: 0.01 } },
      },
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "b" }],
        usage: { input: 10, output: 20, cacheRead: 30, cacheWrite: 40, cost: { total: 0.02 } },
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
  assert.equal(last.usage.input_tokens, 11);
  assert.equal(last.usage.output_tokens, 22);
  assert.equal(last.usage.cache_read_tokens, 33);
  assert.equal(last.usage.cache_write_tokens, 44);
  assert.equal(last.usage.cache_read_input_tokens, 33);
  assert.equal(last.usage.cache_creation_input_tokens, 44);
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
  const thinkingSentinel = "secret chain of thought";
  const asstEv = {
    type: "message_end",
    message: {
      role: "assistant",
      content: [
        { type: "thinking", thinking: thinkingSentinel },
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
  assert.ok(!blob.includes(thinkingSentinel));
  const lines = chunksToLines(out.chunks).filter((l) => l.type === "assistant");
  assert.equal(lines[0].message.content.length, 1);
  assert.equal(lines[0].message.content[0].type, "text");
  const last = JSON.parse(out.chunks.at(-1));
  assert.ok(!String(last.result).includes(thinkingSentinel));
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

test("execute: pre-stringify redact hides quote backslash newline in stream", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const nasty = 'pi-key"slash\\' + "\n" + "tail";
  const jsonFragment = JSON.stringify(nasty).slice(1, -1);
  const asstEv = {
    type: "message_end",
    message: {
      role: "assistant",
      content: [
        { type: "text", text: nasty },
        { type: "toolCall", id: "w1", name: "write", input: { body: nasty } },
      ],
    },
  };
  const toolEv = {
    type: "message_end",
    message: {
      role: "toolResult",
      toolCallId: "w1",
      isError: false,
      content: [{ type: "text", text: nasty }],
    },
  };
  let getAuthCalls = 0;
  const runtime = {
    getModel: () => ({ cost: { input: 1 } }),
    checkAuth: async () => ({}),
    getAuth: async () => {
      getAuthCalls += 1;
      return { auth: { apiKey: nasty } };
    },
  };
  const { pi } = stubPi({ events: [asstEv, toolEv] });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: runtime,
  });
  const blob = out.chunks.join("");
  assert.ok(!blob.includes(nasty));
  assert.ok(!blob.includes(jsonFragment));
  assert.ok(blob.includes("[redacted]"));
  assert.ok(getAuthCalls >= 2);
  for (const line of blob.trim().split("\n")) {
    assert.doesNotThrow(() => JSON.parse(line));
  }
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

test("execute: assistant without usage object nulls total_cost_usd", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const { pi } = stubPi({
    messages: [
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: "a" }],
        usage: { cost: { total: 0.01 } },
      },
      {
        role: "assistant",
        stopReason: "stop",
        content: [{ type: "text", text: '{"status":"done"}' }],
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
});

test("execute: scrubs auth.json key access refresh env when getAuth throws", async () => {
  const ACCESS = "pi-oauth-access-token-xy";
  const REFRESH = "pi-oauth-refresh-token-zz";
  const KEY = "pi-api-key-other-provider";
  const ENVSECRET = "pi-cf-env-secret-qq";
  const authDir = fs.mkdtempSync(path.join(os.tmpdir(), "pi-auth-"));
  fs.writeFileSync(
    path.join(authDir, "auth.json"),
    JSON.stringify({
      anthropic: { type: "oauth", access: ACCESS, refresh: REFRESH, expires: 1 },
      cloudflare: { type: "api_key", key: KEY, env: { CF_API_TOKEN: ENVSECRET } },
    }),
  );
  const prevAgentDir = process.env.PI_CODING_AGENT_DIR;
  process.env.PI_CODING_AGENT_DIR = authDir;
  try {
    const execute = await loadExecute();
    const runDir = layout();
    const out = { chunks: [], write(s) { this.chunks.push(s); } };
    const err = { chunks: [], write(s) { this.chunks.push(s); } };
    const runtime = {
      getModel: () => ({ cost: { input: 1 } }),
      checkAuth: async () => ({ type: "oauth", source: "ANTHROPIC_API_KEY" }),
      getAuth: async () => {
        throw new Error("getAuth failed");
      },
    };
    const { pi } = stubPi({
      messages: [
        {
          role: "assistant",
          stopReason: "stop",
          content: [
            {
              type: "text",
              text: `leaked ${REFRESH} env ${ENVSECRET} key ${KEY}`,
            },
          ],
          usage: { cost: { total: 0.01 } },
        },
      ],
    });
    await execute(runDir, {
      out,
      err,
      importPi: async () => pi,
      modelRuntime: runtime,
    });
    const blob = out.chunks.join("") + err.chunks.join("");
    assert.ok(!blob.includes(REFRESH));
    assert.ok(!blob.includes(ENVSECRET));
    assert.ok(!blob.includes(KEY));
    assert.ok(!blob.includes(ACCESS));
    assert.ok(blob.includes("[redacted]"));
  } finally {
    if (prevAgentDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
    else process.env.PI_CODING_AGENT_DIR = prevAgentDir;
  }
});

function shellImportDeps(over = {}) {
  const fakeEntry = pathToFileURL(path.join("/fake", "pkg", "dist", "index.js")).href;
  return {
    resolvePiPackageEntry: () => fakeEntry,
    importShellModule: async (url) => {
      over.shellUrl = url;
      return {
        killTrackedDetachedChildren: () => {
          over.killed = true;
        },
      };
    },
  };
}

test("execute: finally kills detached children via dist/utils/shell.js", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const track = {};
  const { pi } = stubPi();
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
    ...shellImportDeps(track),
  });
  assert.ok(track.shellUrl);
  assert.ok(track.shellUrl.endsWith("/dist/utils/shell.js"));
  assert.ok(!track.shellUrl.includes("/dist/dist/"));
  assert.ok(track.killed);
});

test("execute: finally kills detached children after thrown prompt", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const track = {};
  const { pi } = stubPi({ promptReject: new Error("prompt boom") });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
    ...shellImportDeps(track),
  });
  assert.ok(track.killed);
});

test("execute: resource loader failure removes nw-pi-agent temp dir", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const track = {};
  const { pi, over } = stubPi();
  pi.DefaultResourceLoader = class {
    constructor(opts) {
      over.loaderOpts = opts;
      track.agentDir = opts.agentDir;
    }
    async reload() {
      throw new Error("reload failed");
    }
  };
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: await pi.ModelRuntime.create(),
    ...shellImportDeps(track),
  });
  assert.ok(track.agentDir);
  assert.equal(fs.existsSync(track.agentDir), false);
  assert.ok(track.killed);
});

test("execute: stream write uses post-refresh OAuth token from getAuth", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const out = { chunks: [], write(s) { this.chunks.push(s); } };
  const INITIAL = "oauth-access-initial-abc";
  const REFRESHED = "oauth-access-refreshed-xyz";
  let getAuthCalls = 0;
  const runtime = {
    getModel: () => ({ cost: { input: 1 } }),
    checkAuth: async () => ({ type: "oauth" }),
    getAuth: async () => {
      getAuthCalls += 1;
      const token = getAuthCalls === 1 ? INITIAL : REFRESHED;
      return { auth: { accessToken: token } };
    },
  };
  const asstEv = {
    type: "message_end",
    message: {
      role: "assistant",
      content: [{ type: "text", text: `tool dumped ${REFRESHED}` }],
    },
  };
  const { pi } = stubPi({ events: [asstEv] });
  await execute(runDir, {
    out,
    err: { write() {} },
    importPi: async () => pi,
    modelRuntime: runtime,
  });
  const blob = out.chunks.join("");
  assert.ok(getAuthCalls >= 2);
  assert.ok(!blob.includes(REFRESHED));
  assert.ok(!blob.includes(INITIAL));
  assert.ok(blob.includes("[redacted]"));
});

test("execute: finally removes only new pi-bash overflow logs from private TMPDIR", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  const hostTmp = fs.mkdtempSync(path.join("/tmp", "nw-pi-host-"));
  const outside = path.join(hostTmp, `pi-bash-outside-${process.pid}.log`);
  fs.writeFileSync(outside, "leave me\n");
  const privateTmp = fs.mkdtempSync(path.join(hostTmp, "child-"));
  const prevTmp = process.env.TMPDIR;
  process.env.TMPDIR = privateTmp;
  const preExisting = path.join(privateTmp, `pi-bash-pre-${process.pid}.log`);
  fs.writeFileSync(preExisting, "keep me\n");
  const spill = path.join(privateTmp, `pi-bash-test-${process.pid}.log`);
  assert.equal(fs.existsSync(spill), false);
  try {
    const { pi } = stubPi({
      promptHook: () => fs.writeFileSync(spill, "overflow output\n"),
    });
    await execute(runDir, {
      out: { write() {} },
      err: { write() {} },
      importPi: async () => pi,
      modelRuntime: await pi.ModelRuntime.create(),
    });
    assert.equal(fs.existsSync(spill), false);
    assert.equal(fs.existsSync(preExisting), true);
    assert.equal(fs.existsSync(outside), true);
  } finally {
    if (prevTmp === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = prevTmp;
    try {
      fs.rmSync(hostTmp, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
});

test("execute: SIGTERM handler kills detached children and aborts", async () => {
  const execute = await loadExecute();
  const runDir = layout();
  let aborted = false;
  const track = {};
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
      ...shellImportDeps(track),
    });
    assert.ok(termHandler);
    termHandler();
    assert.ok(track.killed);
    assert.ok(aborted);
  } finally {
    process.on = origOn;
  }
});
