"use strict";
// SPEC-0402: the page's live client (static/app.js createApp, env.live) over REAL HTTP against a
// real dashboard server started by tests/test_dashboard_live.py. Node's own fetch,
// AbortController and TextDecoder stand in for the browser's. A fake DOM and the real clock.
// Protocol: prints one JSON line per milestone ({"at": name, ...}); reads the scenario from
// NW_LIVE_SCENARIO ("live" waits for a second render, "busy" expects a 503 fallback).
const fs = require("node:fs");
const path = require("node:path");
const { Document } = require("./fakedom.js");

const STATIC = path.join(__dirname, "..", "..", "src", "nightwright", "agentic", "dashboard", "static");
const APP = require(path.join(STATIC, "app.js"));
const IDS = [...fs.readFileSync(path.join(STATIC, "index.html"), "utf8").matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
const BASE = process.env.NW_LIVE_BASE;
// SPEC-0428: no token; the page sends no Authorization header
const SCENARIO = process.env.NW_LIVE_SCENARIO;
const say = (o) => process.stdout.write(JSON.stringify(o) + "\n");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function until(cond, ms, what) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (cond()) return; await sleep(25); }
  say({ at: "timeout", what });
  process.exit(2);
}

async function main() {
  const doc = new Document(IDS);
  const urls = [];
  const env = {
    document: doc, now: () => Date.now(),
    setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (id) => clearTimeout(id),
    visibility: () => doc.visibilityState,
    storage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { hash: "", pathname: "/", search: "" }, history: { replaceState() {} },
    live: true, abort: () => new AbortController(),
    fetch: (u, o) => { urls.push(u); return fetch(BASE + u, o); },
  };
  const app = APP.createApp(env);
  app.start();
  const text = (id) => doc.getElementById(id).textContent;
  await until(() => text("spend") !== "", 20000, "first render");
  if (SCENARIO === "busy") {
    await until(() => urls.some((u) => u.startsWith("/api/stream")), 5000, "stream attempt");
    await sleep(300);
    say({ at: "polling", indicator: text("live"), mode: app.liveMode(), spend: text("spend") });
    process.exit(0);
  }
  await until(() => app.liveMode() === "live", 20000, "live");
  const first = app.rev();
  say({ at: "live", indicator: text("live"), rev: first, urls });
  await until(() => app.rev() !== first, 20000, "second snapshot");
  say({ at: "changed", rev: app.rev(), polls: urls.filter((u) => u === "/api/snapshot").length });
  doc.setHidden(true);
  say({ at: "hidden", mode: app.liveMode() });
  doc.setHidden(false);
  await until(() => app.liveMode() === "live", 20000, "live again");
  say({ at: "reopened", urls: urls.filter((u) => u.startsWith("/api/stream")) });
  process.exit(0);
}
main().catch((e) => { say({ at: "error", message: String(e && e.stack || e) }); process.exit(1); });
