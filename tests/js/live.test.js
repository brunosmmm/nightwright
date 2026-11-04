"use strict";
// SPEC-0402: the page's live stream client (static/app.js) against a fake DOM, a manual clock
// and a scripted fetch whose /api/stream response is a stream the test feeds by hand. Run by
// `node --test` from tests/test_dashboard_live.py. The SSE parser is tested on its own first.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { Document, Clock } = require("./fakedom.js");

const STATIC = path.join(__dirname, "..", "..", "src", "nightwright", "agentic", "dashboard", "static");
const APP = require(path.join(STATIC, "app.js"));
const IDS = [...fs.readFileSync(path.join(STATIC, "index.html"), "utf8").matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
const GEN = Date.parse("2026-11-04T10:00:00+00:00");
const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };

function parse(chunks) {
  const out = { events: [], comments: [], retry: [] };
  const p = APP.createSseParser({
    event: (e) => out.events.push(e), comment: (c) => out.comments.push(c), retry: (n) => out.retry.push(n),
  });
  chunks.forEach((c) => p.push(c));
  return out;
}

test("parser: retry, a snapshot event, a comment", () => {
  const o = parse(["retry: 3000\n\n", "event: snapshot\nid: r1\ndata: {\"a\":1}\n\n", ": hb\n\n"]);
  assert.deepEqual(o.retry, [3000]);
  assert.deepEqual(o.events, [{ type: "snapshot", id: "r1", data: "{\"a\":1}" }]);
  assert.deepEqual(o.comments, ["hb"]);
});

test("parser: chunks split anywhere give the same result as one chunk", () => {
  const wire = "retry: 3000\n\nevent: snapshot\nid: r1\ndata: {\"a\":\ndata: 2}\n\n: hb\n\nevent: snapshot\nid: r2\ndata: x\n\n";
  const whole = parse([wire]);
  for (let i = 1; i < wire.length; i++) assert.deepEqual(parse([wire.slice(0, i), wire.slice(i)]), whole, `split at ${i}`);
  assert.deepEqual(parse([...wire]), whole);
  assert.equal(whole.events[0].data, "{\"a\":\n2}");
});

test("parser: multi-line data joins with a newline; a field with no colon; unknown fields ignored", () => {
  const o = parse(["event: snapshot\ndata: one\ndata: two\ndata\nbogus: 1\n\n"]);
  assert.equal(o.events.length, 1);
  assert.equal(o.events[0].data, "one\ntwo\n");
});

test("parser: CRLF and bare CR, including a CRLF split between chunks", () => {
  const crlf = parse(["event: snapshot\r\nid: r9\r\ndata: x\r\n\r\n"]);
  assert.deepEqual(crlf.events, [{ type: "snapshot", id: "r9", data: "x" }]);
  const split = parse(["event: snapshot\r", "\ndata: y\r", "\n\r", "\n"]);
  assert.deepEqual(split.events, [{ type: "snapshot", id: null, data: "y" }]);
  const cr = parse(["data: z\r\r", "data: w\n\n"]);
  assert.deepEqual(cr.events.map((e) => e.data), ["z", "w"]);
});

test("parser: a bad retry is ignored; an event without data is not dispatched; default type", () => {
  const o = parse(["retry: soon\n\nevent: snapshot\n\ndata: m\n\n"]);
  assert.deepEqual(o.retry, []);
  assert.deepEqual(o.events, [{ type: "message", id: null, data: "m" }]);
});

// ---- the client ----

function snap(rev, spend = 1) {
  return {
    schema: "nw.dashboard.v1", generated_at: "2026-11-04T10:00:00+00:00", rev,
    health: { runner: { lease: { held: true, pid: 1, host: "b", stale: false, heartbeat_age_s: 1 }, runner_code: { stale: false }, draining: false },
              conductor: { pid: 2, lag: 0 }, stewards: [], herdr_watch: null },
    totals: { active: 0, ready: 0, attention: 0, waiting: 0, backlog: 0, conductor_handling: 0, spend_today_usd: spend },
    active: [], needs_you: [], recent_events: [], inbox: { escalations: [], parks: [] },
  };
}

const wireOf = (s) => `event: snapshot\nid: ${s.rev}\ndata: ${JSON.stringify(s)}\n\n`;

// A response whose body the test feeds; abort() (through the signal) ends it like a browser.
function fakeStream(signal) {
  const enc = new TextEncoder();
  const q = [];
  let waiter = null;
  let done = false;
  const wake = () => { if (waiter && (q.length || done)) { const w = waiter; waiter = null; w(); } };
  const s = {
    closed: false,
    push(text) { q.push(enc.encode(text)); wake(); },
    end() { done = true; wake(); },
    res: {
      status: 200, headers: { get: () => null },
      body: { getReader: () => ({ read: async () => {
        if (!q.length && !done) await new Promise((r) => { waiter = r; });
        if (signal && signal.aborted) throw new Error("aborted");
        return q.length ? { done: false, value: q.shift() } : { done: true };
      } }) },
    },
  };
  if (signal) signal.addEventListener("abort", () => { s.closed = true; done = true; wake(); });
  return s;
}

function setup({ visible = true, stored = {}, live = true } = {}) {
  const doc = new Document(IDS);
  doc.hidden = !visible;
  const clock = new Clock(GEN + 5000);
  const store = new Map(Object.entries(stored));
  const polls = [];
  const streams = [];
  const calls = [];
  const modes = [];                       // per /api/stream open: "stream" (default), 503, "throw"
  let pollRev = "r0";
  const env = {
    document: doc, now: () => clock.now(), setTimeout: (f, ms) => clock.setTimeout(f, ms),
    clearTimeout: (id) => clock.clearTimeout(id), visibility: () => doc.visibilityState,
    storage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) },
    location: { hash: "", pathname: "/", search: "" }, history: { replaceState() {} },
    live, abort: () => new AbortController(),
    fetch: async (url, opts) => {
      calls.push({ url, opts, at: clock.now() });
      if (url.startsWith("/api/stream")) {
        const mode = modes.length ? modes.shift() : "stream";
        if (mode === "throw") throw new Error("network");
        if (mode === 503) return { status: 503, headers: { get: () => null }, body: null };
        const s = fakeStream(opts.signal);
        streams.push(s);
        return s.res;
      }
      polls.push({ url, opts });
      const date = new Date(clock.now()).toUTCString();
      const h = { date, etag: `"${pollRev}"` };
      return { status: 200, headers: { get: (k) => h[k.toLowerCase()] ?? null }, json: async () => snap(pollRev, 7) };
    },
  };
  const app = APP.createApp(env);
  const text = (id) => doc.getElementById(id).textContent;
  return { app, doc, clock, calls, polls, streams, modes, text, store, setPollRev: (r) => { pollRev = r; } };
}

const streamCalls = (t) => t.calls.filter((c) => c.url.startsWith("/api/stream"));

test("live: the stream opens, shows live, renders pushed snapshots and ignores an already-shown rev", async () => {
  const t = setup();
  t.app.start();
  await settle();
  assert.equal(t.streams.length, 1);
  assert.equal(t.text("live"), "live");
  t.streams[0].push("retry: 3000\n\n" + wireOf(snap("r1", 2)));
  await settle();
  assert.equal(t.text("spend"), "spend today $2.00");
  const written = t.doc.getElementById("counts").replaced;
  t.streams[0].push(wireOf(snap("r1", 99)));             // same rev: must not repaint
  await settle();
  assert.equal(t.doc.getElementById("counts").replaced, written);
  assert.equal(t.text("spend"), "spend today $2.00");
  t.streams[0].push(wireOf(snap("r2", 3)));
  await settle();
  assert.equal(t.text("spend"), "spend today $3.00");
});

test("live: polling is paused while the stream is live", async () => {
  const t = setup();
  t.app.start();
  await settle();
  const before = t.polls.length;
  t.streams[0].push(wireOf(snap("r1", 2)));
  for (let i = 0; i < 6; i++) {
    await t.clock.advance(15000, settle);
    t.streams[0].push(": hb\n\n");
    await settle();
  }
  assert.equal(t.polls.length, before, "no polls while the stream is live");
  assert.equal(t.text("spend"), "spend today $2.00");
});

test("live: no token anywhere (SPEC-0428): no Authorization header, nothing in the URL", async () => {
  const t = setup({ stored: { "nw-dashboard-token": "s3cr3t-token" } });
  t.app.start();
  await settle();
  t.streams[0].push(wireOf(snap("r1")));
  await settle();
  for (const c of t.calls) assert.ok(!c.url.includes("s3cr3t"), c.url);
  assert.ok(!("Authorization" in (streamCalls(t)[0].opts.headers || {})));
  assert.equal(streamCalls(t)[0].url, "/api/stream");
});

test("live: heartbeats keep it alive; 45 s of silence falls back to polling and then retries with backoff", async () => {
  const t = setup();
  t.app.start();
  await settle();
  for (let i = 0; i < 4; i++) {                          // 60 s of heartbeats every 15 s
    await t.clock.advance(15000, settle);
    t.streams[0].push(": hb\n\n");
    await settle();
  }
  assert.equal(t.text("live"), "live");
  assert.equal(t.streams.length, 1);
  const silent = t.clock.now();
  await t.clock.advance(44000, settle);
  assert.equal(t.text("live"), "live");
  await t.clock.advance(1000, settle);                   // 45 s without a byte
  assert.ok(t.streams[0].closed, "the silent stream was aborted");
  assert.equal(t.text("live"), "polling");
  assert.ok(t.polls.length >= 2, "polling resumed at once");
  // retry after 2 s, again after 4 s (that second open fails too: 503), 8 s, ...
  await t.clock.advance(1999, settle);
  assert.equal(t.streams.length, 1);
  await t.clock.advance(2, settle);
  assert.equal(t.streams.length, 2, "reconnect after 2 s");
  assert.equal(t.text("live"), "live");
  assert.ok(silent > 0);
});

test("live: backoff doubles from 2 s to a 30 s cap", async () => {
  const t = setup();
  t.modes.push(503, 503, 503, 503, 503, 503, 503);
  t.app.start();
  await settle();
  const opens = [t.clock.now()];
  for (let i = 0; i < 6; i++) {
    const n = streamCalls(t).length;
    while (streamCalls(t).length === n) await t.clock.advance(500, settle);
    opens.push(t.clock.now());
  }
  const gaps = opens.slice(1).map((v, i) => Math.round((v - opens[i]) / 1000));
  assert.deepEqual(gaps, [2, 4, 8, 16, 30, 30]);
  assert.equal(t.text("live"), "polling");
});

test("live: a 503 on the stream leaves the page polling and still rendering the snapshot", async () => {
  const t = setup();
  t.modes.push(503, 503, 503, 503, 503);
  t.app.start();
  await settle();
  assert.equal(t.text("live"), "polling");
  assert.equal(t.text("spend"), "spend today $7.00");
  const n = t.polls.length;
  await t.clock.advance(15000, settle);
  assert.ok(t.polls.length > n, "15 s conditional polling continues");
  assert.equal(t.polls[t.polls.length - 1].opts.headers["If-None-Match"], '"r0"');
});

test("live: a stream that ends or throws falls back to polling", async () => {
  const t = setup();
  t.app.start();
  await settle();
  t.streams[0].end();
  await settle();
  assert.equal(t.text("live"), "polling");
  t.modes.push("throw");
  await t.clock.advance(2000, settle);
  assert.equal(t.text("live"), "polling");           // the retry threw; the next is 4 s out
  await t.clock.advance(4000, settle);
  assert.equal(t.text("live"), "live");
});

test("live: hidden closes the stream and holds no slot; visible polls at once then reopens with ?rev=", async () => {
  const t = setup();
  t.app.start();
  await settle();
  t.streams[0].push(wireOf(snap("r1", 2)));
  await settle();
  t.doc.setHidden(true);
  await settle();
  assert.ok(t.streams[0].closed, "stream closed when hidden");
  const calls = t.calls.length;
  await t.clock.advance(120000, settle);
  assert.equal(t.calls.length, calls, "no request and no retry while hidden");
  t.setPollRev("r5");
  t.doc.setHidden(false);
  await settle();
  const after = t.calls.slice(calls);
  assert.equal(after[0].url, "/api/snapshot", "refresh first");
  assert.equal(after[0].opts.headers["If-None-Match"], '"r1"');
  assert.equal(after[1].url, "/api/stream?rev=r5", "reopened with the rev now held");
  assert.equal(t.text("live"), "live");
});

test("live: hide then show quickly opens exactly one stream", async () => {
  const t = setup();
  t.app.start();
  await settle();
  t.doc.setHidden(true);
  t.doc.setHidden(false);
  t.doc.setHidden(true);
  t.doc.setHidden(false);
  await settle();
  assert.equal(t.streams.filter((s) => !s.closed).length, 1);
});

test("live: without env.live the page only polls (SPEC-0413 behaviour)", async () => {
  const t = setup({ live: false });
  t.app.start();
  await settle();
  await t.clock.advance(30000, settle);
  assert.equal(streamCalls(t).length, 0);
  assert.ok(t.polls.length >= 2);
});
