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

// Pi's managed installation is not on Node's ESM package search path.
function resolvePiEntry() {
  try {
    return import.meta.resolve("@earendil-works/pi-coding-agent");
  } catch (original) {
    const agentDir = process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
    const install = path.join(agentDir, "install");
    try {
      const version = fs.readFileSync(path.join(install, "current-version"), "utf8").trim();
      if (!/^[0-9A-Za-z][0-9A-Za-z.+-]*$/.test(version)) throw original;
      const entry = path.join(install, "releases", version, "node_modules", "@earendil-works",
        "pi-coding-agent", "dist", "index.js");
      if (!fs.existsSync(entry)) throw original;
      return pathToFileURL(entry).href;
    } catch {
      throw original;
    }
  }
}

function importPiSdk() {
  return import(resolvePiEntry());
}

const SCHEMA_FOOTER =
  "Reply with a single JSON object matching this schema and nothing else.\n\n{schema}";
const SCHEMA_PLACEHOLDER = "{schema}";

const MIN_SECRET_LEN = 8;
const CONTEXT_FILE_NAMES = ["AGENTS.md", "CLAUDE.md"];
const MAX_CONTEXT_FILE_BYTES = 512 * 1024;

function secretsLongestFirst(secrets = []) {
  const seen = new Set();
  const out = [];
  for (const s of secrets) {
    if (typeof s === "string" && s && !seen.has(s)) {
      seen.add(s);
      out.push(s);
    }
  }
  out.sort((a, b) => b.length - a.length);
  return out;
}

function redactString(text, secrets = []) {
  let out = String(text ?? "");
  for (const s of secrets) {
    if (s) out = out.split(s).join("[redacted]");
  }
  return out;
}

function redactDeep(value, secrets = []) {
  if (typeof value === "string") return redactString(value, secrets);
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((v) => redactDeep(v, secrets));
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    out[k] = redactDeep(v, secrets);
  }
  return out;
}

function scrub(text, secrets = []) {
  return redactString(text, secrets).slice(0, 2000);
}

function writeLine(out, obj, secrets = []) {
  const sorted = secretsLongestFirst(secrets);
  out.write(JSON.stringify(redactDeep(obj, sorted)) + "\n");
  const flush = out.flush;
  if (typeof flush === "function") {
    flush.call(out);
  }
}

function pushSecret(secrets, s) {
  if (typeof s === "string" && s.length >= MIN_SECRET_LEN) secrets.push(s);
}

function loadContextFilesUnderCwd(cwd) {
  let root;
  try {
    root = fs.realpathSync(cwd);
  } catch {
    root = path.resolve(cwd);
  }
  const agentsFiles = [];
  for (const name of CONTEXT_FILE_NAMES) {
    const candidate = path.join(root, name);
    let resolved;
    try {
      resolved = fs.realpathSync(candidate);
    } catch {
      continue;
    }
    const rel = path.relative(root, resolved);
    if (rel.startsWith("..") || path.isAbsolute(rel)) continue;
    try {
      const st = fs.statSync(resolved);
      if (!st.isFile() || st.size > MAX_CONTEXT_FILE_BYTES) continue;
      const content = fs.readFileSync(resolved, "utf8");
      agentsFiles.push({ path: candidate, content });
    } catch {
      /* skip unreadable */
    }
  }
  return agentsFiles;
}

function agentsUnderCwdOnly(cwd) {
  return () => ({ agentsFiles: loadContextFilesUnderCwd(cwd) });
}

function listPiBashSpillPaths() {
  const dir = process.env.TMPDIR;
  if (!dir) return new Set();
  try {
    const out = new Set();
    for (const name of fs.readdirSync(dir)) {
      if (name.startsWith("pi-bash")) out.add(path.join(dir, name));
    }
    return out;
  } catch {
    return new Set();
  }
}

function removeNewPiBashOverflowLogs(before = new Set()) {
  const dir = process.env.TMPDIR;
  if (!dir) return;
  try {
    for (const name of fs.readdirSync(dir)) {
      if (!name.startsWith("pi-bash")) continue;
      const full = path.join(dir, name);
      if (before.has(full)) continue;
      try {
        fs.rmSync(full, { force: true, recursive: true });
      } catch {
        /* ignore */
      }
    }
  } catch {
    /* ignore */
  }
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
    const entry =
      typeof deps.resolvePiPackageEntry === "function"
        ? deps.resolvePiPackageEntry()
        : resolvePiEntry();
    const pkgDir = path.dirname(fileURLToPath(entry));
    const shellUrl = pathToFileURL(path.join(pkgDir, "utils", "shell.js")).href;
    const importShell = deps.importShellModule ?? ((u) => import(u));
    const shell = await importShell(shellUrl);
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

const IMAGE_TOOL_RESULT_PLACEHOLDER = "[image omitted]";

function toolResultTextFromPiContent(content) {
  if (!Array.isArray(content) || !content.length) return null;
  if (content.every((b) => b?.type === "text")) {
    return content.map((b) => b.text ?? "").join("");
  }
  if (content.some((b) => b?.type === "text" || b?.type === "image")) {
    const parts = [];
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (block.type === "text") parts.push(block.text ?? "");
      else if (block.type === "image") parts.push(IMAGE_TOOL_RESULT_PLACEHOLDER);
    }
    return parts.length ? parts.join("") : null;
  }
  return null;
}

function emitToolResultLine(out, message, secrets = []) {
  let blocks;
  if (typeof message.content === "string") {
    blocks = mapUserContent([
      {
        type: "toolResult",
        toolCallId: message.toolCallId,
        content: message.content,
        isError: message.isError,
      },
    ]);
  } else if (Array.isArray(message.content) && message.content.length) {
    const text = toolResultTextFromPiContent(message.content);
    if (text != null) {
      blocks = [
        {
          type: "tool_result",
          tool_use_id: message.toolCallId ?? "",
          content: text,
          is_error: Boolean(message.isError),
        },
      ];
    } else {
      blocks = mapUserContent(message.content);
    }
  } else {
    blocks = mapUserContent(message.content);
  }
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

function agentAuthJsonPath() {
  const base =
    process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi", "agent");
  return path.join(base, "auth.json");
}

const PROVIDER_ENV_KEYS = {
  anthropic: ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"],
  openai: ["OPENAI_API_KEY"],
  google: ["GOOGLE_API_KEY", "GEMINI_API_KEY"],
  "google-vertex": ["GOOGLE_API_KEY"],
  "amazon-bedrock": [
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
  ],
  bedrock: ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"],
};

const MAX_CREDENTIAL_FILE_BYTES = 64 * 1024;
const GAC_PROVIDERS = new Set(["google", "google-vertex", "gemini"]);
const CREDENTIAL_ENV_KEY =
  /(?:^|_)(?:api[_-]?key|secret|token|password|private[_-]?key|refresh(?:_token)?|access(?:_token|-?_key)?)(?:$|_)/i;

const CREDENTIAL_ENV_PATH_KEYS = new Set([
  "GOOGLE_APPLICATION_CREDENTIALS",
  "ANTHROPIC_IDENTITY_TOKEN_FILE",
  "AWS_WEB_IDENTITY_TOKEN_FILE",
]);

function defaultAdcPath() {
  return path.join(os.homedir(), ".config", "gcloud", "application_default_credentials.json");
}

function envKeyHoldsSecretValue(key) {
  const k = String(key);
  if (CREDENTIAL_ENV_PATH_KEYS.has(k)) return false;
  if (k === "GOOGLE_CLOUD_PROJECT" || k === "GOOGLE_CLOUD_LOCATION") return false;
  if (/_FILE$/i.test(k)) return false;
  return CREDENTIAL_ENV_KEY.test(k);
}

function isLikelyPathValue(val) {
  if (typeof val !== "string" || val.length < 2) return false;
  if (val.startsWith("/") || val.startsWith("~/") || val.startsWith("~\\")) return true;
  if (/^[A-Za-z]:[\\/]/.test(val)) return true;
  return false;
}

function expandHomePath(filePath) {
  if (typeof filePath !== "string" || !filePath.startsWith("~")) return filePath;
  return os.homedir() + filePath.slice(1);
}

function readTrimmedFile(filePath, add, maxBytes = MAX_CREDENTIAL_FILE_BYTES) {
  const expanded = expandHomePath(filePath);
  try {
    const st = fs.statSync(expanded);
    if (!st.isFile() || st.size > maxBytes) return;
    const text = fs.readFileSync(expanded, "utf8").trim();
    if (text) add(text);
  } catch {
    /* optional */
  }
}

function addPrivateKeySecrets(privateKey, add) {
  if (typeof privateKey !== "string" || !privateKey) return;
  add(privateKey);
  if (privateKey.includes("\n")) {
    add(privateKey.replace(/\r\n/g, "\n").replace(/\n/g, "\\n"));
  }
}

function parseGoogleCredentialFile(filePath, add) {
  if (!filePath || typeof filePath !== "string") return;
  const expanded = expandHomePath(filePath);
  try {
    const st = fs.statSync(expanded);
    if (!st.isFile() || st.size > MAX_CREDENTIAL_FILE_BYTES) return;
    const raw = fs.readFileSync(expanded, "utf8");
    const cred = JSON.parse(raw);
    addPrivateKeySecrets(cred.private_key, add);
    add(cred.client_secret);
    add(cred.refresh_token);
    const escapedPk = raw.match(/"private_key"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (escapedPk?.[1]) {
      const fromFile = escapedPk[1]
        .replace(/\\n/g, "\n")
        .replace(/\\r/g, "\r")
        .replace(/\\t/g, "\t")
        .replace(/\\\\/g, "\\")
        .replace(/\\"/g, '"');
      addPrivateKeySecrets(fromFile, add);
      add(escapedPk[1]);
    }
    const escapedRt = raw.match(/"refresh_token"\s*:\s*"((?:[^"\\]|\\.)*)"/);
    if (escapedRt?.[1]) add(escapedRt[1]);
  } catch {
    /* not JSON or missing */
  }
}

function addGoogleApplicationCredentialSecrets(provider, add, authEnv) {
  if (!GAC_PROVIDERS.has(provider)) return;
  const gac =
    process.env.GOOGLE_APPLICATION_CREDENTIALS ||
    authEnv?.GOOGLE_APPLICATION_CREDENTIALS;
  if (gac) {
    parseGoogleCredentialFile(gac, add);
    return;
  }
  parseGoogleCredentialFile(defaultAdcPath(), add);
}

function addEnvMapCredentialValues(envMap, add) {
  if (!envMap || typeof envMap !== "object") return;
  for (const [key, val] of Object.entries(envMap)) {
    if (!envKeyHoldsSecretValue(key)) continue;
    if (isLikelyPathValue(val)) continue;
    add(val);
  }
}

function addAuthObjectSecrets(obj, add) {
  if (!obj || typeof obj !== "object") return;
  add(obj.apiKey);
  add(obj.key);
  add(obj.token);
  add(obj.refreshToken);
  add(obj.refresh);
  add(obj.accessToken);
  add(obj.access);
  addEnvMapCredentialValues(obj.env, add);
  const headers = obj.headers ?? obj.auth?.headers;
  if (headers && typeof headers === "object") {
    for (const v of Object.values(headers)) add(v);
  }
}

function addProviderCredentialEnv(provider, authCheck, add, authEnv) {
  if (authCheck && typeof authCheck === "object") {
    addAuthObjectSecrets(authCheck, add);
    if (authCheck.source) add(process.env[authCheck.source]);
  }
  for (const key of PROVIDER_ENV_KEYS[provider] ?? []) {
    add(process.env[key]);
  }
  const wit = process.env.AWS_WEB_IDENTITY_TOKEN_FILE;
  if (
    wit &&
    (provider === "amazon-bedrock" || provider === "bedrock")
  ) {
    readTrimmedFile(wit, add);
  }
  const identityFile =
    process.env.ANTHROPIC_IDENTITY_TOKEN_FILE ||
    authEnv?.ANTHROPIC_IDENTITY_TOKEN_FILE;
  if (identityFile && provider === "anthropic") {
    readTrimmedFile(identityFile, add);
  }
  if (GAC_PROVIDERS.has(provider)) {
    addGoogleApplicationCredentialSecrets(provider, add, authEnv);
  }
}

function addStoredAuthSecrets(store, add) {
  if (!store || typeof store !== "object") return;
  for (const [name, entry] of Object.entries(store)) {
    if (!entry || typeof entry !== "object") continue;
    addAuthObjectSecrets(entry, add);
    if (entry.auth && typeof entry.auth === "object") {
      addAuthObjectSecrets(entry.auth, add);
    }
    if (GAC_PROVIDERS.has(name)) {
      const gacPath = entry?.env?.GOOGLE_APPLICATION_CREDENTIALS;
      if (typeof gacPath === "string" && gacPath) {
        parseGoogleCredentialFile(gacPath, add);
      }
    }
  }
}

async function collectSecretsAsync(modelRuntime, provider, authCheck) {
  const secrets = [];
  const add = (s) => pushSecret(secrets, s);
  let authEnv;
  try {
    const detail = await modelRuntime.getAuth(provider);
    authEnv = detail?.env;
    add(detail?.apiKey);
    add(detail?.token);
    add(detail?.auth?.apiKey);
    add(detail?.auth?.token);
    add(detail?.auth?.refreshToken);
    add(detail?.auth?.accessToken);
    const headers = detail?.auth?.headers ?? detail?.headers;
    if (headers && typeof headers === "object") {
      for (const v of Object.values(headers)) add(v);
    }
    addEnvMapCredentialValues(detail?.env, add);
  } catch {
    /* keep auth.json backup */
  }
  addProviderCredentialEnv(provider, authCheck, add, authEnv);
  try {
    const raw = fs.readFileSync(agentAuthJsonPath(), "utf8");
    addStoredAuthSecrets(JSON.parse(raw), add);
  } catch {
    /* optional store */
  }
  return secretsLongestFirst(secrets);
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
  const importPi = deps.importPi ?? importPiSdk;

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
  const footer = SCHEMA_FOOTER.split(SCHEMA_PLACEHOLDER).join(schema);
  const userMessage = `${promptText}\n\n${footer}`;

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
  const piBashBefore = listPiBashSpillPaths();

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
      noContextFiles: true,
      agentsFilesOverride: agentsUnderCwdOnly(cwd),
      appendSystemPrompt: req.system_append ? [req.system_append] : [],
    });

  const sessionManager =
    deps.sessionManager ?? pi.SessionManager.inMemory(cwd);

  let session;
  let exitCode = 1;
  let proceed = true;
  try {
    if (deps.resourceLoader == null) {
      try {
        await resourceLoader.reload();
      } catch (e) {
        proceed = false;
        const msg = e instanceof Error ? e.message : String(e);
        failResult(out, err, `resource loader failed (${msg})`, { secrets });
      }
    }
    if (!proceed) {
      /* fall through to finally cleanup */
    } else {
    let created;
    try {
      created = await pi.createAgentSession({
        cwd,
        model,
        thinkingLevel: thinking,
        modelRuntime,
        tools,
        resourceLoader,
        sessionManager,
        settingsManager,
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      failResult(out, err, `session failed (${msg})`, { secrets });
      exitCode = 1;
      created = null;
    }
    if (!created) {
      /* session construction failed */
    } else {
    session = created.session;

    const emitted = new Set();
    let streamSeq = 0;
    let streamWriteChain = Promise.resolve();
    const emitWithFreshSecrets = (emitFn) => {
      streamWriteChain = streamWriteChain.then(async () => {
        const fresh = await collectSecretsAsync(modelRuntime, provider, auth);
        emitFn(fresh);
      });
    };
    const unsub = session.subscribe((event) => {
      if (event.type !== "message_end" || !event.message) return;
      const msg = event.message;
      if (msg.role === "toolResult") {
        const key = `tr:${msg.toolCallId ?? ""}`;
        if (emitted.has(key)) return;
        emitted.add(key);
        emitWithFreshSecrets((fresh) => emitToolResultLine(out, msg, fresh));
        return;
      }
      if (msg.role === "assistant" && msg.stopReason === "error") {
        return;
      }
      if (msg.role === "assistant" || msg.role === "user") {
        const key = `msg:${streamSeq++}`;
        emitted.add(key);
        emitWithFreshSecrets((fresh) => emitMessage(out, msg.role, msg.content, fresh));
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
    await streamWriteChain;
    unsub?.();

    const messages = session.state?.messages ?? [];
    const assistants = messages.filter((m) => m.role === "assistant");
    const last = assistants[assistants.length - 1];
    const failed = !last || last.stopReason === "error" || last.stopReason === "aborted";
    let resultText = (last?.content ?? [])
      .filter((b) => b?.type === "text")
      .map((b) => b.text)
      .join("");
    const secretsAfter = await collectSecretsAsync(modelRuntime, provider, auth);
    const streamSecrets = secretsLongestFirst([...secrets, ...secretsAfter]);

    if (failed) {
      const errMsg = last?.errorMessage ?? resultText;
      if (errMsg) {
        err.write(scrub(errMsg, streamSecrets) + "\n");
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
        result: resultText,
        session_id: sessionId,
        usage,
        total_cost_usd: totalCost,
      },
      streamSecrets,
    );
    exitCode = failed ? 1 : 0;
    }
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    let catchSecrets = secrets;
    try {
      if (modelRuntime && provider) {
        catchSecrets = await collectSecretsAsync(modelRuntime, provider, auth);
      }
    } catch {
      /* keep initial list */
    }
    failResult(out, err, msg, { secrets: catchSecrets });
    exitCode = 1;
  } finally {
    await killDetachedChildren(pi, deps);
    try {
      const dispose = session?.dispose;
      if (typeof dispose === "function") {
        await dispose.call(session);
      }
    } catch {
      /* ignore */
    }
    removeAgentDir();
    removeNewPiBashOverflowLogs(piBashBefore);
  }
  return exitCode;
}

export async function probeProviders(providers, deps = {}) {
  const importPi = deps.importPi ?? importPiSdk;
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
