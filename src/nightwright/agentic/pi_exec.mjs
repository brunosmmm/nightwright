/**
 * Local Pi SDK persona child (SPEC-0451).
 *
 * `node pi_exec.mjs <run_dir>` is supervised by runproc when resolution is `pi`.
 * Exports `execute(runDir, deps)` for tests with a stub SDK.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const SCHEMA_FOOTER =
  "Reply with a single JSON object matching this schema and nothing else.\n\n{schema}";

function redact(text, secrets = []) {
  let out = String(text ?? "");
  for (const s of secrets) {
    if (s) out = out.split(s).join("[redacted]");
  }
  return out;
}

function scrub(text, secrets = []) {
  return redact(text, secrets).slice(0, 2000);
}

function writeLine(out, obj, secrets = []) {
  out.write(redact(JSON.stringify(obj), secrets) + "\n");
  const flush = out.flush;
  if (typeof flush === "function") {
    flush.call(out);
  }
}

function agentsUnderCwdOnly(cwd) {
  const root = path.resolve(cwd);
  return ({ agentsFiles }) => ({
    agentsFiles: agentsFiles.filter((f) => {
      const rel = path.relative(root, path.resolve(f.path));
      return rel !== ".." && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel);
    }),
  });
}

async function killDetachedChildren(piMod, deps) {
  try {
    if (typeof deps.killTrackedDetachedChildren === "function") {
      deps.killTrackedDetachedChildren();
      return;
    }
    if (typeof piMod.killTrackedDetachedChildren === "function") {
      piMod.killTrackedDetachedChildren();
      return;
    }
    const entry = import.meta.resolve("@earendil-works/pi-coding-agent");
    const pkgDir = path.dirname(fileURLToPath(entry));
    const shell = await import(
      pathToFileURL(path.join(pkgDir, "dist", "utils", "shell.js")).href,
    );
    shell.killTrackedDetachedChildren?.();
  } catch {
    /* optional export */
  }
}

function failResult(out, err, message, { code = "startup", secrets = [] } = {}) {
  if (err) err.write(scrub(message, secrets) + "\n");
  writeLine(out, {
    type: "result",
    subtype: "error",
    is_error: true,
    result: scrub(message, secrets),
    session_id: null,
    usage: {},
    total_cost_usd: null,
    error_code: code,
  });
}

function mapAssistantContent(content = []) {
  const blocks = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    if (block.type === "text") {
      blocks.push({ type: "text", text: block.text ?? "" });
    } else if (block.type === "toolCall" || block.type === "tool_use") {
      blocks.push({
        type: "tool_use",
        id: block.id ?? "",
        name: block.name ?? "",
        input: block.input ?? block.arguments ?? {},
      });
    }
  }
  return blocks;
}

function mapUserContent(content = []) {
  const blocks = [];
  for (const block of content) {
    if (!block || typeof block !== "object") continue;
    if (block.type === "toolResult" || block.type === "tool_result") {
      const text =
        typeof block.content === "string"
          ? block.content
          : Array.isArray(block.content)
            ? block.content.map((c) => (c?.text != null ? c.text : String(c))).join("")
            : String(block.content ?? "");
      blocks.push({
        type: "tool_result",
        tool_use_id: block.toolCallId ?? block.tool_use_id ?? "",
        content: text,
        is_error: Boolean(block.isError ?? block.is_error),
      });
    }
  }
  return blocks;
}

function emitToolResultLine(out, message, secrets = []) {
  const text = (message.content ?? [])
    .filter((b) => b?.type === "text")
    .map((b) => b.text ?? "")
    .join("");
  writeLine(
    out,
    {
    type: "user",
    message: {
      role: "user",
      content: [
        {
          type: "tool_result",
          tool_use_id: message.toolCallId ?? "",
          content: text,
          is_error: Boolean(message.isError),
        },
      ],
    },
    },
    secrets,
  );
}

function emitMessage(out, role, content, secrets = []) {
  if (role === "assistant") {
    const blocks = mapAssistantContent(content);
    if (!blocks.length) return;
    writeLine(
      out,
      {
        type: "assistant",
        message: { role: "assistant", content: blocks },
      },
      secrets,
    );
  } else if (role === "user") {
    const blocks = mapUserContent(content);
    if (!blocks.length) return;
    writeLine(
      out,
      {
        type: "user",
        message: { role: "user", content: blocks },
      },
      secrets,
    );
  }
}

function addStoredAuthSecrets(store, add) {
  if (!store || typeof store !== "object") return;
  for (const entry of Object.values(store)) {
    if (!entry || typeof entry !== "object") continue;
    add(entry.apiKey);
    add(entry.token);
    add(entry.refreshToken);
    add(entry.accessToken);
    if (entry.auth && typeof entry.auth === "object") {
      add(entry.auth.apiKey);
      add(entry.auth.token);
      add(entry.auth.refreshToken);
      add(entry.auth.accessToken);
      const headers = entry.auth.headers;
      if (headers && typeof headers === "object") {
        for (const v of Object.values(headers)) add(v);
      }
    }
  }
}

async function collectSecretsAsync(modelRuntime, provider, authCheck) {
  const secrets = [];
  const add = (s) => {
    if (typeof s === "string" && s) secrets.push(s);
  };
  try {
    const detail = await modelRuntime.getAuth(provider);
    add(detail?.apiKey);
    add(detail?.token);
    add(detail?.auth?.apiKey);
    add(detail?.auth?.token);
    const headers = detail?.auth?.headers ?? detail?.headers;
    if (headers && typeof headers === "object") {
      for (const v of Object.values(headers)) add(v);
    }
  } catch {
    /* ignore */
  }
  if (authCheck?.source) add(process.env[authCheck.source]);
  if (authCheck?.env && typeof authCheck.env === "object") {
    for (const v of Object.values(authCheck.env)) add(v);
  }
  try {
    const authPath = path.join(os.homedir(), ".pi", "agent", "auth.json");
    const raw = fs.readFileSync(authPath, "utf8");
    addStoredAuthSecrets(JSON.parse(raw), add);
  } catch {
    /* optional store */
  }
  return secrets;
}

function usageFromAssistant(messages) {
  const totals = {
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
  };
  for (const msg of messages) {
    if (msg?.role !== "assistant" || !msg.usage) continue;
    const u = msg.usage;
    totals.input_tokens += Number(u.input ?? u.input_tokens ?? 0);
    totals.output_tokens += Number(u.output ?? u.output_tokens ?? 0);
    totals.cache_read_tokens += Number(u.cacheRead ?? u.cache_read_tokens ?? 0);
    totals.cache_write_tokens += Number(u.cacheWrite ?? u.cache_write_tokens ?? 0);
  }
  totals.cache_read_input_tokens = totals.cache_read_tokens;
  totals.cache_creation_input_tokens = totals.cache_write_tokens;
  totals.total_tokens =
    totals.input_tokens + totals.output_tokens + totals.cache_read_tokens + totals.cache_write_tokens;
  return totals;
}

function rateIsNonZero(n) {
  if (n === undefined || n === null) return false;
  const x = Number(n);
  return Number.isFinite(x) && x !== 0;
}

function catalogPriced(model) {
  const cost = model?.cost;
  if (!cost || typeof cost !== "object") return false;
  const nums = [cost.input, cost.output, cost.cacheRead, cost.cacheWrite];
  for (const n of nums) {
    if (rateIsNonZero(n)) return true;
  }
  for (const tier of cost.tiers ?? []) {
    if (
      rateIsNonZero(tier?.input) ||
      rateIsNonZero(tier?.output) ||
      rateIsNonZero(tier?.cacheRead) ||
      rateIsNonZero(tier?.cacheWrite)
    ) {
      return true;
    }
  }
  return false;
}

function sumCostUsd(assistantMessages, model) {
  let sawAssistant = false;
  let total = 0;
  let anyFromProvider = false;
  for (const msg of assistantMessages) {
    if (msg?.role !== "assistant") continue;
    sawAssistant = true;
    const cost = msg.usage?.cost;
    if (!cost || typeof cost.total !== "number") return null;
    total += cost.total;
    if (cost.totalFromProvider === true) anyFromProvider = true;
  }
  if (!sawAssistant) return null;
  const priced = catalogPriced(model) || anyFromProvider;
  if (!priced) return null;
  return total;
}

export async function execute(runDir, deps = {}) {
  const out = deps.out ?? process.stdout;
  const err = deps.err ?? process.stderr;
  const readFile = deps.readFile ?? ((p) => fs.readFileSync(p, "utf8"));
  const importPi = deps.importPi ?? (() => import("@earendil-works/pi-coding-agent"));

  let req;
  try {
    req = JSON.parse(readFile(path.join(runDir, "request.json")));
  } catch {
    failResult(out, err, "request.json is missing or not JSON");
    return 1;
  }
  if (!req || typeof req !== "object") {
    failResult(out, err, "request.json is missing or not JSON");
    return 1;
  }

  const provider = req.pi_provider;
  const modelId = req.pi_id;
  const thinking = req.pi_thinking ?? "medium";
  if (!provider || !modelId) {
    failResult(out, err, "request.json missing pi_provider or pi_id");
    return 1;
  }

  let promptText;
  try {
    promptText = readFile(path.join(runDir, "prompt.md"));
  } catch {
    failResult(out, err, "prompt.md is missing");
    return 1;
  }

  const schema = JSON.stringify(req.json_schema ?? {}, null, 2);
  const userMessage = `${promptText}\n\n${SCHEMA_FOOTER.replace("{schema}", schema)}`;

  let pi;
  try {
    pi = await importPi();
  } catch (e) {
    failResult(out, err, `pi-coding-agent is not installed (${e})`);
    return 1;
  }

  let modelRuntime;
  try {
    modelRuntime = deps.modelRuntime ?? (await pi.ModelRuntime.create({ modelsPath: null }));
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    failResult(out, err, `ModelRuntime.create failed (${msg})`);
    return 1;
  }
  const model = modelRuntime.getModel(provider, modelId);
  if (!model) {
    failResult(out, err, `unknown model ${provider}/${modelId}`, { secrets: [] });
    return 1;
  }

  let auth;
  try {
    auth = await modelRuntime.checkAuth(provider);
  } catch {
    auth = null;
  }
  if (!auth) {
    failResult(out, err, `no credential for provider ${provider}`);
    return 1;
  }

  const secrets = await collectSecretsAsync(modelRuntime, provider, auth);

  const cwd = req.cwd || ".";
  const readOnly = Boolean(req.read_only);
  const tools = readOnly ? ["read", "grep", "find", "ls"] : ["read", "bash", "edit", "write"];

  let agentDir = deps.agentDir ?? fs.mkdtempSync(path.join(os.tmpdir(), "nw-pi-agent-"));
  const removeAgentDir =
    deps.agentDir == null
      ? () => {
          try {
            fs.rmSync(agentDir, { recursive: true, force: true });
          } catch {
            /* ignore */
          }
        }
      : () => {};

  const settingsManager =
    deps.settingsManager ??
    pi.SettingsManager.inMemory(
      { cacheWarming: "off", compaction: { enabled: false } },
      { projectTrusted: false },
    );

  const resourceLoader =
    deps.resourceLoader ??
    new pi.DefaultResourceLoader({
      cwd,
      agentDir,
      settingsManager,
      noExtensions: true,
      noSkills: true,
      noPromptTemplates: true,
      noThemes: true,
      noContextFiles: false,
      agentsFilesOverride: agentsUnderCwdOnly(cwd),
      appendSystemPrompt: req.system_append ? [req.system_append] : [],
    });
  if (deps.resourceLoader == null) {
    try {
      await resourceLoader.reload();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      failResult(out, err, `resource loader failed (${msg})`, { secrets });
      return 1;
    }
  }

  const sessionManager =
    deps.sessionManager ?? pi.SessionManager.inMemory(cwd);

  let session;
  const holder = { session: null };
  try {
    const created = await pi.createAgentSession({
      cwd,
      model,
      thinkingLevel: thinking,
      modelRuntime,
      tools,
      resourceLoader,
      sessionManager,
      settingsManager,
    });
    session = created.session;
    holder.session = session;

    const emitted = new Set();
    let streamSeq = 0;
    const unsub = session.subscribe((event) => {
      if (event.type !== "message_end" || !event.message) return;
      const msg = event.message;
      if (msg.role === "toolResult") {
        const key = `tr:${msg.toolCallId ?? ""}`;
        if (emitted.has(key)) return;
        emitted.add(key);
        emitToolResultLine(out, msg, secrets);
        return;
      }
      if (msg.role === "assistant" && msg.stopReason === "error") {
        return;
      }
      if (msg.role === "assistant" || msg.role === "user") {
        const key = `msg:${streamSeq++}`;
        emitted.add(key);
        emitMessage(out, msg.role, msg.content, secrets);
      }
    });

    if (deps.sigtermHandler) {
      process.once("SIGTERM", deps.sigtermHandler);
    } else {
      process.once("SIGTERM", () => {
        void killDetachedChildren(pi, deps);
        try {
          session.abort?.();
        } catch {
          /* best effort */
        }
      });
    }

    await session.prompt(userMessage);
    unsub?.();

    const messages = session.state?.messages ?? [];
    const assistants = messages.filter((m) => m.role === "assistant");
    const last = assistants[assistants.length - 1];
    const failed = !last || last.stopReason === "error" || last.stopReason === "aborted";
    let resultText = (last?.content ?? [])
      .filter((b) => b?.type === "text")
      .map((b) => b.text)
      .join("");
    if (failed) {
      const errMsg = last?.errorMessage ?? resultText;
      if (errMsg) {
        err.write(scrub(errMsg, secrets) + "\n");
        resultText = errMsg;
      }
    }
    const usage = usageFromAssistant(assistants);
    const totalCost = sumCostUsd(assistants, model);
    const sessionId = sessionManager.getHeader?.()?.id ?? session.sessionId ?? null;

    writeLine(
      out,
      {
        type: "result",
        subtype: failed ? "error" : "success",
        is_error: failed,
        result: redact(resultText, secrets),
        session_id: sessionId,
        usage,
        total_cost_usd: totalCost,
      },
      secrets,
    );
    return failed ? 1 : 0;
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    failResult(out, err, scrub(msg, secrets));
    return 1;
  } finally {
    try {
      session?.dispose?.();
    } catch {
      /* ignore */
    }
    removeAgentDir();
  }
}

export async function probeProviders(providers, deps = {}) {
  const importPi = deps.importPi ?? (() => import("@earendil-works/pi-coding-agent"));
  try {
    const pi = await importPi();
    const runtime = deps.modelRuntime ?? (await pi.ModelRuntime.create({ modelsPath: null }));
    const missing = [];
    for (const p of providers) {
      let ok = false;
      try {
        ok = Boolean(await runtime.checkAuth(p));
      } catch {
        ok = false;
      }
      if (!ok) missing.push(p);
    }
    return { ok: missing.length === 0, missing, error: null };
  } catch (e) {
    return { ok: false, missing: [...providers], error: String(e) };
  }
}

async function main(argv = process.argv.slice(2)) {
  if (argv[0] === "--probe") {
    const providers = argv.slice(1).filter(Boolean);
    if (!providers.length) {
      process.stderr.write("usage: pi_exec.mjs --probe <provider>...\n");
      return 2;
    }
    const rec = await probeProviders(providers);
    process.stdout.write(JSON.stringify(rec) + "\n");
    return rec.ok ? 0 : 1;
  }
  if (!argv[0] || argv[0].startsWith("-")) {
    process.stderr.write("usage: node pi_exec.mjs RUN_DIR\n");
    return 2;
  }
  return execute(argv[0]);
}

const entry = process.argv[1] ? path.resolve(process.argv[1]) : "";
if (entry && entry === path.resolve(fileURLToPath(import.meta.url))) {
  main().then((code) => process.exit(code ?? 1));
}
