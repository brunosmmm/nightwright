"use strict";
// SPEC-0413: drives static/app.js over REAL HTTP against the stub server of
// tests/test_dashboard_page.py (node's own fetch is the page's fetch). Usage:
//   node tests/js/page_http.test.js <port> [full|readonly]
// `full` also changes the active campaign's cost through the stub's control route; `readonly`
// makes no change. Prints one JSON summary line for pytest to assert on.
const fs = require("node:fs");
const path = require("node:path");
const { Document, Clock } = require("./fakedom.js");

const STATIC = path.join(__dirname, "..", "..", "src", "nightwright", "agentic", "dashboard", "static");
const APP = require(path.join(STATIC, "app.js"));
const IDS = [...fs.readFileSync(path.join(STATIC, "index.html"), "utf8").matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);

async function main() {
  const base = `http://127.0.0.1:${process.argv[2]}`;
  const scenario = process.argv[3] || "full";
  const doc = new Document(IDS);
  const clock = new Clock(Date.now());
  const statuses = [];
  const store = new Map();
  let app = null;
  const env = {
    document: doc,
    now: () => clock.now(),
    setTimeout: (f, ms) => clock.setTimeout(f, ms),
    clearTimeout: (id) => clock.clearTimeout(id),
    visibility: () => doc.visibilityState,
    storage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) },
    location: { hash: "", pathname: "/", search: "" },
    history: { replaceState() {} },
    fetch: async (u, o) => {
      const r = await fetch(base + u, o);
      statuses.push(r.status);
      return r;
    },
  };
  const settle = async () => { await app.idle(); await new Promise((r) => setImmediate(r)); };
  const text = (id) => doc.getElementById(id).textContent;
  const banner = () => (doc.getElementById("banner").classList.contains("hidden") ? "" : text("banner"));
  const ctl = async (c) => { const r = await fetch(`${base}/__test/${c}`); if (r.status !== 200) throw new Error(`ctl ${c} ${r.status}`); };
  const out = {};

  app = APP.createApp(env);
  app.start();
  await app.idle();
  out.spend = text("spend");
  out.counts = text("counts");
  out.needs = text("needs");
  out.active_before = text("active");
  out.dot = [...doc.getElementById("dot").classList.set];
  const created = doc.created.length;
  await clock.advance(15000, settle);
  out.second_status = statuses[statuses.length - 1];
  out.rebuilt_on_304 = doc.created.length !== created;
  if (scenario === "full") {
    await ctl("ledger");
    await clock.advance(15000, settle);
    out.after_change_status = statuses[statuses.length - 1];
    out.active_after = text("active");
    out.spend_after = text("spend");
  }
  await ctl("deny401");
  await clock.advance(15000, settle);
  out.banner_401 = banner();
  await ctl("deny403");
  await clock.advance(15000, settle);
  out.banner_403 = banner();
  await ctl("allow");
  await clock.advance(15000, settle);
  out.banner_after = banner();
  out.statuses = statuses;
  console.log(JSON.stringify(out));
}

main().catch((e) => { console.error(e); process.exit(1); });
