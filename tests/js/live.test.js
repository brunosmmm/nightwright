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
  const listeners = {};
  const poll = { fail: false, hang: false };   // SPEC-0434: a poll that throws, or never answers
  const env = {
    document: doc, now: () => clock.now(), setTimeout: (f, ms) => clock.setTimeout(f, ms),
    clearTimeout: (id) => clock.clearTimeout(id), visibility: () => doc.visibilityState,
    storage: { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) },
    location: { hash: "", pathname: "/", search: "" }, history: { replaceState() {} },
    live, abort: () => new AbortController(),
    listen: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    fetch: async (url, opts) => {
      calls.push({ url, opts, at: clock.now() });
      if (url.startsWith("/api/stream")) {
        const mode = modes.length ? modes.shift() : "stream";
        if (mode === "throw") throw new Error("network");
        if (mode === "hang") return new Promise((_, rej) => { if (opts.signal) opts.signal.addEventListener("abort", () => rej(new Error("aborted"))); });
        if (mode === 503) return { status: 503, headers: { get: () => null }, body: null };
        const s = fakeStream(opts.signal);
        streams.push(s);
        return s.res;
      }
      polls.push({ url, opts });
      if (poll.fail) throw new Error("network");
      if (poll.hang) return new Promise(() => {});
      const date = new Date(clock.now()).toUTCString();
      const h = { date, etag: `"${pollRev}"` };
      return { status: 200, headers: { get: (k) => h[k.toLowerCase()] ?? null }, json: async () => snap(pollRev, 7) };
    },
  };
  const app = APP.createApp(env);
  const text = (id) => doc.getElementById(id).textContent;
  return { app, doc, clock, calls, polls, streams, modes, text, store, poll,
           fire: (type) => (listeners[type] || []).forEach((fn) => fn()), setPollRev: (r) => { pollRev = r; } };
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
  await t.clock.advance(30000, settle);
  assert.equal(t.text("live"), "live");
  await t.clock.advance(1000, settle);                   // SPEC-0434: silent over 30 s
  assert.equal(t.text("live"), "stalled");
  await t.clock.advance(13000, settle);
  assert.equal(t.text("live"), "stalled");
  await t.clock.advance(1000, settle);                   // 45 s without a byte
  assert.ok(t.streams[0].closed, "the silent stream was aborted");
  assert.ok(t.polls.length >= 2, "polling resumed at once");
  // SPEC-0434: that poll succeeded, so the server is up: the stream reopens at once
  assert.equal(t.streams.length, 2, "reopened after the successful poll");
  assert.equal(t.text("live"), "live");
  assert.ok(silent > 0);
});

test("live: with polls failing too, a silent stream falls back and retries with backoff", async () => {
  const t = setup();
  t.app.start();
  await settle();
  t.poll.fail = true;
  await t.clock.advance(45000, settle);
  assert.ok(t.streams[0].closed, "the silent stream was aborted");
  assert.equal(t.text("live"), "offline");
  await t.clock.advance(1999, settle);
  assert.equal(t.streams.length, 1);
  await t.clock.advance(2, settle);
  assert.equal(t.streams.length, 2, "reconnect after 2 s");
  assert.equal(t.text("live"), "live");
});

test("live: backoff doubles from 2 s to a 30 s cap (the server unreachable, polls failing too)", async () => {
  const t = setup();
  t.modes.push(503, 503, 503, 503, 503, 503, 503);
  t.poll.fail = true;
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
  assert.equal(t.text("live"), "offline");             // SPEC-0434: nothing answers
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

// ---- SPEC-0434 ----

test("SPEC-0434: a stream open that never answers fails over after 10 s and retries", async () => {
  const t = setup();
  t.modes.push("hang");
  t.app.start();
  await settle();
  assert.equal(t.text("live"), "polling");
  await t.clock.advance(9999, settle);
  assert.equal(streamCalls(t).length, 1);
  await t.clock.advance(1, settle);                      // the open watchdog
  assert.ok(streamCalls(t)[0].opts.signal.aborted, "the hung open was aborted");
  await t.clock.advance(2000, settle);
  assert.equal(t.streams.length, 1);
  assert.equal(t.text("live"), "live");
});

test("SPEC-0434: the server comes back: a successful poll reopens the stream without waiting out the backoff", async () => {
  const t = setup();
  t.app.start();
  await settle();
  t.poll.fail = true;
  t.modes.push("throw", "throw", "throw", "throw", "throw");
  t.streams[0].end();                                    // the server went away (a restart)
  await settle();
  assert.equal(t.text("live"), "offline");
  // retries at 2, 6, 14, 30 s fail; the next is 30 s out (60 s); polls at 15, 30 s fail too
  await t.clock.advance(31000, settle);
  const n = streamCalls(t).length;
  t.poll.fail = false;                                   // the new process is up
  t.modes.length = 0;
  await t.clock.advance(14000, settle);                  // the 45 s poll succeeds
  assert.equal(streamCalls(t).length, n + 1, "reopened at the successful poll, not at 60 s");
  assert.equal(streamCalls(t).at(-1).at, t.calls[0].at + 45000);
  assert.equal(t.text("live"), "live");
});

test("SPEC-0434: a reconnect re-fetches the snapshot; the online event retries at once", async () => {
  const t = setup();
  t.app.start();
  await settle();
  t.streams[0].end();
  await settle();
  t.poll.fail = true;                                    // keep the fallback poll from reopening it
  await settle();
  const polls = t.polls.length;
  t.poll.fail = false;
  await t.clock.advance(2000, settle);                   // the retry opens
  assert.equal(t.text("live"), "live");
  assert.ok(t.polls.length > polls, "a poll right after the reconnect");
  t.streams[1].end();
  await settle();
  t.modes.push(503);
  await t.clock.advance(2000, settle);
  const before = streamCalls(t).length;
  t.fire("online");
  await settle();
  assert.equal(streamCalls(t).length, before + 1);
});

test("SPEC-0434: the header stamps the data time and the chip is truthful", async () => {
  const t = setup();
  t.app.start();
  await settle();
  t.streams[0].push(wireOf(snap("r1", 2)));
  await settle();
  assert.ok(/^data as of \d\d:\d\d:\d\d · checked 0s ago$/.test(t.text("updated")), t.text("updated"));
  assert.ok(t.doc.getElementById("live").classList.contains("is-live"));
  await t.clock.advance(31000, settle);
  assert.equal(t.text("live"), "stalled");
  assert.ok(t.doc.getElementById("live").classList.contains("is-stalled"));
  t.streams[0].push(": hb\n\n");
  await settle();
  await t.clock.advance(1000, settle);
  assert.equal(t.text("live"), "live");
});
