"use strict";
// SPEC-0413: the dashboard phone page logic (static/app.js) against a fake DOM, an injected
// clock and a scripted fetch. Run by `node --test` from tests/test_dashboard_page_logic.py.
// NW_DASH_FIXTURE (a JSON path written by that pytest file from the real snapshot builder on the
// SPEC-0401 fixture) enables the "real snapshot" test; the rest use small hand-made documents.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { Document, Clock } = require("./fakedom.js");

const STATIC = path.join(__dirname, "..", "..", "src", "nightwright", "agentic", "dashboard", "static");
const APP = require(path.join(STATIC, "app.js"));
const IDS = [...fs.readFileSync(path.join(STATIC, "index.html"), "utf8").matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
const DATA_IDS = ["needs", "counts", "spend", "active", "health", "events"];

const GEN = Date.parse("2026-11-04T10:00:00+00:00");
const settle = () => new Promise((r) => setImmediate(r));
const httpDate = (ms) => new Date(ms).toUTCString();

function resp(status, body, { date, etag } = {}) {
  const h = {};
  if (date !== undefined) h.date = date;
  if (etag) h.etag = etag;
  return { status, headers: { get: (k) => (h[k.toLowerCase()] === undefined ? null : h[k.toLowerCase()]) }, json: async () => body };
}

function snap(over = {}) {
  return {
    schema: "nw.dashboard.v1", generated_at: "2026-11-04T10:00:00+00:00", rev: "r1",
    health: {
      runner: { lease: { held: true, pid: 11, host: "box", stale: false, heartbeat_age_s: 4 }, runner_code: { stale: false }, draining: false },
      conductor: { pid: 22, lag: 0 }, stewards: [], herdr_watch: null,
    },
    totals: { active: 1, ready: 2, attention: 3, waiting: 4, backlog: 5, conductor_handling: 6, spend_today_usd: 3.5 },
    active: [{ campaign: "C1", spec_id: "SPEC-1", label: "SPEC-1", state: "IMPLEMENTING", loops: "1/3", cost_usd: 1.25, cap_usd: 10, age_s: 10, activity: { kind: "idle" } }],
    needs_you: [], recent_events: [], inbox: { escalations: [], parks: [] },
    ...over,
  };
}

function setup({ t0 = GEN + 5000, hash = "", stored = {}, visible = true } = {}) {
  const doc = new Document(IDS);
  doc.hidden = !visible;
  const clock = new Clock(t0);
  const calls = [];
  const script = [];
  const state = { inflight: 0, max: 0 };
  const store = new Map(Object.entries(stored));
  const replaced = [];
  const env = {
    document: doc,
    now: () => clock.now(),
    setTimeout: (f, ms) => clock.setTimeout(f, ms),
    clearTimeout: (id) => clock.clearTimeout(id),
    visibility: () => doc.visibilityState,
    storage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) },
    location: { hash, pathname: "/", search: "" },
    history: { replaceState: (...a) => replaced.push(a) },
    fetch: async (url, opts) => {
      calls.push({ url, opts, at: clock.now() });
      state.inflight += 1;
      state.max = Math.max(state.max, state.inflight);
      try {
        const h = script.length ? script.shift() : () => resp(304, null, { date: httpDate(clock.now()) });
        return await h();
      } finally {
        state.inflight -= 1;
      }
    },
  };
  const app = APP.createApp(env);
  const text = (id) => doc.getElementById(id).textContent;
  const writes = () => DATA_IDS.reduce((n, id) => n + doc.getElementById(id).replaced, 0);
  return { app, doc, clock, calls, script, state, store, replaced, text, writes };
}

const ok = (body, date) => () => resp(200, body, { date: date === undefined ? undefined : httpDate(date), etag: `"${body.rev}"` });

test("formatters", () => {
  assert.equal(APP.formatUsd(1.256), "$1.26");
  assert.equal(APP.formatUsd(null), "$0.00");
  assert.equal(APP.formatAge(15), "15s");
  assert.equal(APP.formatAge(65), "1m");
  assert.equal(APP.formatAge(3700), "1h 1m");
  assert.equal(APP.formatAge(null), "?");
  assert.equal(APP.displayAge(10, GEN, GEN + 5000), 15);
  assert.equal(APP.displayAge(10, GEN, GEN - 99999), 10);
});

test("real snapshot on the SPEC-0401 fixture renders the page", { skip: !process.env.NW_DASH_FIXTURE }, async () => {
  const fx = JSON.parse(fs.readFileSync(process.env.NW_DASH_FIXTURE, "utf8"));
  const s = fx.snapshot;
  const g = Date.parse(s.generated_at);
  const t = setup({ t0: g + 1000 });
  t.script.push(ok(s, g + 1000));
  t.app.start();
  await t.app.idle();
  assert.equal(t.text("spend"), `spend today $${s.totals.spend_today_usd.toFixed(2)}`);
  assert.equal(t.text("counts"), fx.counts_line);
  const cards = t.doc.getElementById("active").children;
  assert.equal(cards.length, s.active.length);
  const a = s.active[0];
  assert.ok(cards[0].textContent.includes(`$${a.cost_usd.toFixed(2)} / $${a.cap_usd.toFixed(2)}`));
  const bar = t.doc.created.find((e) => e.tag === "progress");
  assert.equal(bar.attrs["aria-valuenow"], String(Math.min(100, Math.round((a.cost_usd / a.cap_usd) * 100))));
  const needs = t.text("needs");
  assert.ok(needs.includes("ESC-0001") && needs.includes("main_dirty"), needs);
  assert.ok(t.text("health").includes("runner") && t.text("health").includes("herdr watch"));
  const evs = t.doc.getElementById("events").children;
  assert.ok(evs.length <= 20 && evs.length === Math.min(20, s.recent_events.length));
  const last = s.recent_events[s.recent_events.length - 1];
  assert.ok(evs[0].textContent.includes(last.summary));
});

test("markup in snapshot values renders literally", async () => {
  const bad = "<img src=x onerror=alert(1)>";
  const s = snap({
    active: [{ ...snap().active[0], label: bad, spec_id: bad }],
    inbox: { escalations: [{ id: "ESC-9", campaign: "C", spec_id: "S", question: bad }], parks: [] },
    recent_events: [{ id: 1, ts: "2026-11-04T10:00:00+00:00", type: "parked", campaign: "C", spec_id: "S", summary: bad }],
  });
  const t = setup();
  t.script.push(ok(s, GEN));
  t.app.start();
  await t.app.idle();
  assert.ok(t.text("needs").includes(bad));
  assert.ok(t.text("active").includes(bad));
  assert.ok(t.text("events").includes(bad));
  const tags = new Set(t.doc.created.map((e) => e.tag));
  assert.ok(!tags.has("img"), [...tags].join());
  for (const [, k] of t.doc.attrWrites) assert.ok(["max", "aria-valuenow", "aria-valuemax", "title"].includes(k), k);
});

test("polling: conditional, 15 s apart, 304 keeps the tree, 200 re-renders, one in flight", async () => {
  const t = setup();
  t.script.push(ok(snap(), GEN + 5000));
  t.app.start();
  await t.app.idle();
  assert.equal(t.calls.length, 1);
  assert.equal(t.calls[0].url, "/api/snapshot");
  assert.equal(t.calls[0].opts.cache, "no-store");
  assert.equal(t.calls[0].opts.credentials, "same-origin");
  assert.equal(t.calls[0].opts.headers["If-None-Match"], undefined);
  const created = t.doc.created.length;
  const writes = t.writes();
  await t.clock.advance(15000, settle);
  assert.equal(t.calls.length, 2);
  assert.equal(t.calls[1].opts.headers["If-None-Match"], '"r1"');
  assert.equal(t.calls[1].opts.cache, "no-store");
  assert.equal(t.calls[1].at - t.calls[0].at, 15000);
  assert.equal(t.doc.created.length, created);          // 304: no element built
  assert.equal(t.writes(), writes);                      // 304: no data section re-rendered
  t.script.push(ok(snap({ rev: "r2", totals: { ...snap().totals, spend_today_usd: 9 } }), GEN + 5000 + 30000));
  await t.clock.advance(15000, settle);
  assert.equal(t.calls.length, 3);
  assert.equal(t.calls[2].at - t.calls[1].at, 15000);
  assert.ok(t.writes() > writes);
  assert.equal(t.text("spend"), "spend today $9.00");
  await t.clock.advance(15000, settle);
  assert.equal(t.calls[3].opts.headers["If-None-Match"], '"r2"');
  assert.equal(t.state.max, 1);
});

test("a slow request is never overlapped: later polls are dropped until it settles", async () => {
  const t = setup();
  let release;
  t.script.push(() => new Promise((r) => { release = () => r(resp(200, snap(), { date: httpDate(GEN) })); }));
  t.app.start();
  await settle();
  t.app.poll();
  t.app.poll();
  await t.clock.advance(60000, settle);
  assert.equal(t.calls.length, 1);
  release();
  await t.app.idle();
  assert.equal(t.state.max, 1);
  await t.clock.advance(15000, settle);
  assert.equal(t.calls.length, 2);
});

test("visibility: no request or timer while hidden; visible polls at once and re-arms", async () => {
  const t = setup();
  t.script.push(ok(snap(), GEN + 5000));
  t.app.start();
  await t.app.idle();
  assert.equal(t.clock.armed.length, 2);                 // poll + age tick
  t.doc.setHidden(true);
  assert.equal(t.clock.armed.length, 0);
  await t.clock.advance(120000, settle);
  assert.equal(t.calls.length, 1);
  t.script.push(ok(snap({ rev: "r2" }), GEN + 125000));
  t.doc.setHidden(false);
  await t.app.idle();
  assert.equal(t.calls.length, 2);                       // immediately, no waiting for 15 s
  assert.equal(t.calls[1].opts.headers["If-None-Match"], '"r1"');
  assert.deepEqual(t.clock.armed.map((x) => x.ms).sort((a, b) => a - b), [1000, 15000]);
  t.doc.setHidden(true);
  assert.equal(t.clock.armed.length, 0);
  // a request still pending when the tab hides arms nothing when it settles
  let release;
  t.script.push(() => new Promise((r) => { release = () => r(resp(304, null, { date: httpDate(t.clock.now()) })); }));
  t.doc.setHidden(false);
  await settle();
  t.doc.setHidden(true);
  release();
  await t.app.idle();
  assert.equal(t.clock.armed.length, 0);
});

test("ages tick from the server clock", async () => {
  const run = { ...snap().active[0], activity: { kind: "run", persona: "implementer", elapsed_s: 50 } };
  const s = snap({ active: [run] });
  const t = setup();
  t.script.push(ok(s, GEN + 5000));
  t.app.start();
  await t.app.idle();
  assert.ok(t.text("active").includes("age 15s"), t.text("active"));
  assert.ok(t.text("active").includes("implementer · 55s"), t.text("active"));   // elapsed_s, not age_s
  assert.ok(t.text("health").includes("heartbeat ok"), t.text("health"));             // healthy: no age
  assert.equal(t.text("updated"), "checked 0s ago");
  await t.clock.advance(10000, settle);                                                // no request yet? one 304 at 15 s only
  assert.equal(t.calls.length, 1);
  assert.ok(t.text("active").includes("age 25s"));
  assert.ok(t.text("active").includes("implementer · 1m"));
  // across a 304 whose Date moved on: 20 s after the first response shows 35 s
  t.script.push(() => resp(304, null, { date: httpDate(GEN + 20000) }));
  await t.clock.advance(10000, settle);
  assert.equal(t.calls.length, 2);
  assert.ok(t.text("active").includes("age 35s"), t.text("active"));
});

test("quiet queue: 304s for 10 minutes never show a growing heartbeat age; the header tracks the last poll", async () => {
  const t = setup();
  t.script.push(ok(snap(), GEN + 5000));                 // heartbeat_age_s = 4, lease healthy
  t.app.start();
  await t.app.idle();
  assert.ok(t.text("health").includes("heartbeat ok"), t.text("health"));
  await t.clock.advance(600000, settle);                 // 40 polls, all 304
  assert.ok(t.calls.length >= 40, String(t.calls.length));
  assert.ok(t.text("health").includes("heartbeat ok"), t.text("health"));
  assert.ok(!/heartbeat \d/.test(t.text("health")), t.text("health"));
  assert.ok(/^checked ([0-9]|1[0-5])s ago$/.test(t.text("updated")), t.text("updated"));   // last poll <= 15 s ago
  // a stale lease arrives as a 200 (stale is part of rev) and then shows the ticking age
  const stale = snap({ rev: "r2", generated_at: new Date(GEN + 600000).toISOString() });
  stale.health.runner.lease = { held: true, pid: 11, host: "box", stale: true, heartbeat_age_s: 120 };
  t.script.push(ok(stale, GEN + 600000));
  await t.clock.advance(15000, settle);
  assert.ok(/heartbeat 2m/.test(t.text("health")) && t.text("health").includes("stale lease"), t.text("health"));
});

test("ages: a Date earlier than generated_at is never negative; no Date falls back to the local clock", async () => {
  const early = setup();
  early.script.push(ok(snap(), GEN - 3600000));
  early.app.start();
  await early.app.idle();
  assert.ok(early.text("active").includes("age 10s"), early.text("active"));
  assert.equal(early.text("updated"), "checked 0s ago");
  const none = setup({ t0: GEN + 5000 });
  none.script.push(ok(snap()));                          // no Date header
  none.app.start();
  await none.app.idle();
  assert.ok(none.text("active").includes("age 15s"), none.text("active"));
  await none.clock.advance(3000, settle);
  assert.ok(none.text("active").includes("age 18s"));
});

test("banners and the dot: 401, 403, unreachable, stale, and clearing", async () => {
  const banner = (t) => (t.doc.getElementById("banner").classList.contains("hidden") ? "" : t.text("banner"));
  const dot = (t) => ["green", "amber", "red", "grey"].filter((c) => t.doc.getElementById("dot").classList.contains(c));
  const t401 = setup();
  t401.script.push(() => resp(401, { error: "no" }));
  t401.app.start();
  await t401.app.idle();
  assert.equal(banner(t401), "Not signed in. Open the dashboard link that carries the token.");
  assert.deepEqual(dot(t401), ["grey"]);
  t401.script.push(() => resp(403, { error: "no" }));
  await t401.clock.advance(15000, settle);               // polling continues at 15 s
  assert.equal(banner(t401), "This device is not allowed to view the dashboard.");
  assert.deepEqual(dot(t401), ["grey"]);
  t401.script.push(ok(snap(), GEN + 30000));
  await t401.clock.advance(15000, settle);
  assert.equal(banner(t401), "");
  assert.deepEqual(dot(t401), ["green"]);

  const net = setup();
  net.script.push(ok(snap(), GEN + 5000));
  net.app.start();
  await net.app.idle();
  net.script.push(() => { throw new Error("offline"); });
  await net.clock.advance(15000, settle);
  assert.equal(banner(net), "Can't reach the dashboard.");
  assert.ok(net.text("active").includes("SPEC-1"));      // the last snapshot stays
  assert.ok(net.doc.getElementById("main").classList.contains("greyed"));
  assert.deepEqual(dot(net), ["grey"]);
  net.script.push(() => resp(500, {}));
  await net.clock.advance(15000, settle);
  assert.equal(banner(net), "Can't reach the dashboard.");
  await net.clock.advance(15000, settle);                // default 304
  assert.equal(banner(net), "");
  assert.ok(!net.doc.getElementById("main").classList.contains("greyed"));

  const stale = setup();
  stale.script.push(ok(snap(), GEN + 5000));
  stale.app.start();
  await stale.app.idle();
  let release;
  stale.script.push(() => new Promise((r) => { release = () => r(resp(304, null, { date: httpDate(stale.clock.now()) })); }));
  await stale.clock.advance(45000, settle);              // 45 s is not yet stale
  assert.equal(banner(stale), "");
  await stale.clock.advance(1000, settle);
  assert.equal(banner(stale), "Last update 46 s ago.");
  assert.deepEqual(dot(stale), ["grey"]);
  release();
  await stale.app.idle();
  assert.equal(banner(stale), "");
  assert.deepEqual(dot(stale), ["green"]);
});

test("healthLevel across the four levels", () => {
  const H = APP.healthLevel;
  const withRunner = (r) => snap({ health: { ...snap().health, runner: { ...snap().health.runner, ...r } } });
  const err = { error: "RuntimeError: boom" };
  assert.equal(H(snap(), "ok"), "green");
  // amber
  assert.equal(H(withRunner({ lease: { held: true, pid: 1, stale: true } }), "ok"), "amber");
  assert.equal(H(withRunner({ draining: true }), "ok"), "amber");
  assert.equal(H(withRunner({ runner_code: { stale: true } }), "ok"), "amber");
  assert.equal(H(snap({ inbox: { escalations: [{ id: "ESC-1" }], parks: [] } }), "ok"), "amber");
  assert.equal(H(snap({ inbox: { escalations: [], parks: [{ label: "x" }] } }), "ok"), "amber");
  assert.equal(H(snap({ totals: err }), "ok"), "amber");
  assert.equal(H(snap({ recent_events: err }), "ok"), "amber");
  assert.equal(H(snap({ health: { ...snap().health, herdr_watch: err } }), "ok"), "amber");
  assert.equal(H(snap({ health: { ...snap().health, stewards: err } }), "ok"), "amber");
  // red wins over amber
  assert.equal(H(withRunner({ lease: { held: false } }), "ok"), "red");
  assert.equal(H(snap({ health: { ...snap().health, conductor: { pid: null } } }), "ok"), "red");
  assert.equal(H(snap({ health: { ...snap().health, runner: err } }), "ok"), "red");
  assert.equal(H(snap({ health: { ...snap().health, conductor: err } }), "ok"), "red");
  assert.equal(H(snap({ health: { ...snap().health, conductor: { pid: null } }, inbox: { escalations: [{}], parks: [] } }), "ok"), "red");
  // grey
  for (const c of ["stale", "error", "denied"]) assert.equal(H(snap(), c), "grey");
  assert.equal(H(null, "ok"), "grey");
  assert.equal(H(null, "none"), "grey");
});

test("an error section renders as one muted line and the others still render", async () => {
  const s = snap({ active: { error: "RuntimeError: bad active" }, health: { ...snap().health, stewards: { error: "KeyError: x" } } });
  const t = setup();
  t.script.push(ok(s, GEN));
  t.app.start();
  await t.app.idle();
  assert.equal(t.text("active"), "active: RuntimeError: bad active");
  assert.ok(t.doc.getElementById("active").children[0].classList.contains("muted"));
  assert.ok(t.text("health").includes("stewards: KeyError: x"));
  assert.ok(t.text("health").includes("conductor"));
  assert.ok(t.text("spend").includes("$3.50"));
  assert.ok(t.text("needs").includes("Nothing needs you."));
});

test("recent events: newest first, at most 20, time in a title attribute", async () => {
  const evs = Array.from({ length: 30 }, (_, i) => ({ id: i + 1, ts: `2026-11-04T10:${String(i).padStart(2, "0")}:00+00:00`, type: "parked", campaign: "C", spec_id: "S", summary: `ev-${i + 1}` }));
  const t = setup();
  t.script.push(ok(snap({ recent_events: evs }), GEN));
  t.app.start();
  await t.app.idle();
  const rows = t.doc.getElementById("events").children;
  assert.equal(rows.length, 20);
  assert.ok(rows[0].textContent.includes("ev-30"));
  assert.ok(rows[19].textContent.includes("ev-11"));
  assert.equal(rows[0].children[0].attrs.title, evs[29].ts);
});

test("no cap shows the cost with no bar; bar classes follow the ratio", async () => {
  const mk = (cost, cap) => ({ ...snap().active[0], cost_usd: cost, cap_usd: cap });
  const t = setup();
  t.script.push(ok(snap({ active: [mk(1, null), mk(8, 10), mk(12, 10), mk(1, 10)] }), GEN));
  t.app.start();
  await t.app.idle();
  const bars = t.doc.created.filter((e) => e.tag === "progress");
  assert.equal(bars.length, 3);
  assert.ok(t.text("active").includes("$1.00 · no cap"));
  assert.ok(bars[0].classList.contains("warn"));
  assert.ok(bars[1].classList.contains("crit"));
  assert.equal(bars[1].attrs["aria-valuenow"], "100");
  assert.equal(bars[1].value, 100);
  assert.equal(bars[2].classList.toString(), "");
});

test("token: fragment -> storage -> Authorization; replaced by a later fragment; never in the DOM", async () => {
  const t = setup({ hash: "#token=abc" });
  t.script.push(ok(snap(), GEN));
  t.app.start();
  await t.app.idle();
  assert.equal(t.store.get("nw-dashboard-token"), "abc");
  assert.deepEqual(t.replaced, [[null, "", "/"]]);
  assert.equal(t.calls[0].opts.headers.Authorization, "Bearer abc");
  await t.clock.advance(15000, settle);
  assert.equal(t.calls[1].opts.headers.Authorization, "Bearer abc");
  const all = IDS.map((id) => t.doc.getElementById(id).dump()).join("");
  assert.ok(!all.includes("abc"));
  assert.ok(!JSON.stringify(t.doc.attrWrites).includes("abc"));

  const again = setup({ hash: "#token=def", stored: { "nw-dashboard-token": "abc" } });
  again.script.push(ok(snap(), GEN));
  again.app.start();
  await again.app.idle();
  assert.equal(again.store.get("nw-dashboard-token"), "def");
  assert.equal(again.calls[0].opts.headers.Authorization, "Bearer def");

  const kept = setup({ stored: { "nw-dashboard-token": "xyz" } });          // a later visit with no fragment
  kept.script.push(ok(snap(), GEN));
  kept.app.start();
  await kept.app.idle();
  assert.equal(kept.calls[0].opts.headers.Authorization, "Bearer xyz");
  assert.deepEqual(kept.replaced, []);

  const none = setup();
  none.script.push(ok(snap(), GEN));
  none.app.start();
  await none.app.idle();
  assert.ok(!("Authorization" in none.calls[0].opts.headers));
});
