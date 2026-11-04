"use strict";
// SPEC-0429 / SPEC-0430: the page's inbox answering flow and the campaign drilldown, against the
// fake DOM with a scripted fetch. Run by `node --test` from tests/test_dashboard_answer_page.py.
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

function resp(status, body) {
  return { status, headers: { get: () => null }, json: async () => body };
}

const ESC = {
  id: "ESC-0007", campaign: "C1", spec_id: "SPEC-1", label: "SPEC-1", question: "Ship it?",
  question_text: "Ship it?\nloop exhausted", options: [{ n: 1, label: "resume" }, { n: 2, label: "abandon" }],
  recommendation: 1, facts: ["loop 3/3"], answerable: true, pending: false, why_not: null,
  forms: ["choose", "text", "override"], findings: [{ id: "rev#0", severity: "blocking", title: "bad" }],
  unmet_acs: ["AC 4 sorted"], effects: { choose: "e1", text: "e2", override: "e3" },
};

function snap(inbox, rev = "r1") {
  return {
    schema: "nw.dashboard.v1", generated_at: "2026-11-04T10:00:00+00:00", rev,
    health: { runner: { lease: { held: true, pid: 1 } }, conductor: { pid: 2 }, stewards: [], herdr_watch: null },
    totals: {}, active: [{ campaign: "C1", label: "SPEC-1", state: "PARKED", cost_usd: 1, cap_usd: 5 }],
    needs_you: [], recent_events: [], inbox: { escalations: [], parks: [], auto_applied: [], ...inbox },
  };
}

function setup(hash = "") {
  const doc = new Document(IDS);
  const clock = new Clock(GEN + 5000);
  const calls = [];
  const routes = [];          // [predicate, handler] checked in order; default 304
  const env = {
    document: doc, now: () => clock.now(),
    setTimeout: (f, ms) => clock.setTimeout(f, ms), clearTimeout: (id) => clock.clearTimeout(id),
    visibility: () => doc.visibilityState,
    storage: { getItem: () => null, setItem() {}, removeItem() {} },
    location: { hash, pathname: "/", search: "" },
    history: { replaceState() {} },
    csrf: () => "nonce.mac",
    fetch: async (url, opts) => {
      calls.push({ url, opts: opts || {} });
      for (const [p, h] of routes) if (p(url, opts || {})) return h(url, opts || {});
      return resp(304, null);
    },
  };
  const app = APP.createApp(env);
  return { app, doc, clock, calls, routes, env };
}

const byClass = (doc, id, cls) => doc.getElementById(id).find((e) => e.classList.contains(cls));
const byTag = (doc, id, tag) => doc.getElementById(id).find((e) => e.tag === tag);
const posts = (t) => t.calls.filter((c) => c.opts.method === "POST");

test("an open escalation shows its options, text box, override controls and note", async () => {
  const t = setup();
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }))]);
  t.app.start();
  await settle();
  const opts = byClass(t.doc, "needs", "opt");
  assert.deepEqual(opts.map((b) => b.textContent), ["1. resume (recommended)", "2. abandon"]);
  assert.equal(byTag(t.doc, "needs", "textarea").length, 2);           // text answer + override reason
  assert.equal(byTag(t.doc, "needs", "input").filter((i) => i.attrs.type === "checkbox").length, 2);
  assert.ok(t.doc.getElementById("needs").textContent.includes("loop 3/3"));
  assert.equal(posts(t).length, 0);
});

test("choose -> plan POST with CSRF + JSON -> confirm panel -> Confirm -> answer POST with the plan token", async () => {
  const t = setup();
  let answered = false;
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: answered ? [] : [ESC] }, answered ? "r2" : "r1"))]);
  t.routes.push([(u) => u.endsWith("/plan"), () => resp(200, {
    plan: { summary: "chose 1 (resume)", reverses_summary: null, override: null }, effect: "The conductor resumes it.",
    plan_token: "123.tok" })]);
  t.routes.push([(u) => u.endsWith("/answer"), () => { answered = true; return resp(200, { summary: "chose 1 (resume)", record: {} }); }]);
  t.app.start();
  await settle();
  byClass(t.doc, "needs", "opt")[0].click();
  await settle();
  const p = posts(t);
  assert.equal(p.length, 1);
  assert.equal(p[0].url, "/api/escalations/ESC-0007/plan");
  assert.equal(p[0].opts.headers["X-NW-CSRF"], "nonce.mac");
  assert.equal(p[0].opts.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(p[0].opts.body), { form: "choose", choose: 1 });
  const panel = byClass(t.doc, "needs", "confirm")[0];
  assert.ok(panel.textContent.includes("chose 1 (resume)") && panel.textContent.includes("The conductor resumes it."));
  byClass(t.doc, "needs", "primary")[0].click();
  await settle();
  const p2 = posts(t);
  assert.equal(p2.length, 2);
  assert.equal(p2[1].url, "/api/escalations/ESC-0007/answer");
  assert.deepEqual(JSON.parse(p2[1].opts.body), { form: "choose", choose: 1, plan_token: "123.tok" });
  assert.ok(t.doc.getElementById("needs").textContent.includes("ESC-0007 answered: chose 1 (resume)"));
  assert.ok(!t.doc.getElementById("needs").textContent.includes("Ship it?"), "the answered escalation left after the refresh");
});

test("Cancel posts nothing more; a 409 shows who answered and when", async () => {
  const t = setup();
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }))]);
  let n = 0;
  t.routes.push([(u) => u.endsWith("/plan"), () => (++n === 1
    ? resp(200, { plan: { summary: "s" }, effect: "e", plan_token: "1.t" })
    : resp(409, { status: "answered", by: "human", at: "2026-11-04T10:01:00+00:00", error: "ESC-0007 is answered" }))]);
  t.app.start();
  await settle();
  byClass(t.doc, "needs", "opt")[1].click();
  await settle();
  const cancel = byTag(t.doc, "needs", "button").find((b) => b.textContent === "Cancel");
  cancel.click();
  await settle();
  assert.equal(posts(t).length, 1);
  assert.equal(byClass(t.doc, "needs", "confirm").length, 0);
  byClass(t.doc, "needs", "opt")[0].click();
  await settle();
  const msg = byClass(t.doc, "needs", "msg")[0].textContent;
  assert.ok(msg.startsWith("Already answered by human at 2026-11-04T10:01:00+00:00"), msg);
});

test("text and override forms post their fields; a stale CSRF says reload", async () => {
  const t = setup();
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }))]);
  t.routes.push([(u) => u.endsWith("/plan"), () => resp(403, { reason: "bad_csrf", error: "x" })]);
  t.app.start();
  await settle();
  const [text, reason] = byTag(t.doc, "needs", "textarea");
  text.value = "go REST"; text.fire("input");
  byTag(t.doc, "needs", "button").find((b) => b.textContent === "Answer with this text").click();
  await settle();
  assert.deepEqual(JSON.parse(posts(t)[0].opts.body), { form: "text", text: "go REST" });
  assert.ok(byClass(t.doc, "needs", "msg")[0].textContent.includes("reload"));
  const boxes = byTag(t.doc, "needs", "input").filter((i) => i.attrs.type === "checkbox");
  boxes.forEach((b) => { b.checked = true; b.fire("change"); });
  const r2 = byTag(t.doc, "needs", "textarea")[1];
  r2.value = "accepted"; r2.fire("input");
  const note = byTag(t.doc, "needs", "input").find((i) => i.attrs.type !== "checkbox");
  note.value = "from the phone"; note.fire("input");
  byTag(t.doc, "needs", "button").find((b) => b.textContent === "Plan this override").click();
  await settle();
  assert.deepEqual(JSON.parse(posts(t)[1].opts.body),
    { form: "override", waive: ["rev#0"], attest: ["AC 4 sorted"], reason: "accepted", note: "from the phone" });
  assert.ok(reason);
});

test("an auto-applied answer is offered for reversal; an unanswerable one has no controls", async () => {
  const t = setup();
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({
    escalations: [{ ...ESC, id: "ESC-0008", answerable: false, pending: true }],
    auto_applied: [{ ...ESC, id: "ESC-0009", reversible: true, auto_answer: "chose 1 (resume)" }] }))]);
  t.app.start();
  await settle();
  const nodes = byClass(t.doc, "needs", "esc");
  const e8 = nodes.find((n) => n.attrs["data-esc"] === "ESC-0008");
  const e9 = nodes.find((n) => n.attrs["data-esc"] === "ESC-0009");
  assert.equal(e8.find((n) => n.tag === "button" && !n.classList.contains("camp")).length, 0);
  assert.ok(e8.textContent.includes("Waiting for the campaign to park"));
  assert.ok(e9.textContent.includes("auto-applied: chose 1 (resume)"));
  assert.ok(e9.find((n) => n.classList.contains("opt")).length === 2);
});

test("typing survives an unrelated snapshot (the inbox is not re-rendered)", async () => {
  const t = setup();
  let rev = "r1";
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, { ...snap({ escalations: [ESC] }, rev), totals: { active: rev === "r1" ? 1 : 2 } })]);
  t.app.start();
  await settle();
  const before = t.doc.getElementById("needs").replaced;
  rev = "r2";
  await t.clock.advance(15000, settle);
  assert.equal(t.doc.getElementById("needs").replaced, before);
});

test("drilldown: #/c/<cid> shows the campaign, its timeline and the live tail; leaving aborts it", async () => {
  const t = setup("#/c/C1");
  const enc = new TextEncoder();
  let push = null;
  let aborted = false;
  t.env.abort = () => { const c = { signal: {}, abort: () => { aborted = true; } }; return c; };
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({}))]);
  t.routes.push([(u) => u === "/api/campaigns/C1", () => resp(200, {
    campaign: "C1", state: { state: "PARKED", label: "SPEC-1", loop: 2, cost_usd: 1, cap_usd: 5, park: { reason: "human_decision" } },
    timeline: [{ text: "1 ts admitted" }, { text: "2 ts parked" }], runs: [{ run: "001-implementer", persona: "implementer", status: "running" }],
    current_run: "001-implementer", running: true })]);
  t.routes.push([(u) => u.startsWith("/api/campaigns/C1/tail?run=001-implementer"), () => {
    const queue = [];
    let wake = null;
    push = (s) => { queue.push(enc.encode(s)); if (wake) { wake(); wake = null; } };
    return { status: 200, body: { getReader: () => ({ read: async () => {
      while (!queue.length) await new Promise((r) => { wake = r; });
      return { done: false, value: queue.shift() };
    } }) } };
  }]);
  t.app.start();
  await settle();
  assert.ok(t.doc.getElementById("main").classList.contains("hidden"));
  assert.ok(!t.doc.getElementById("drill").classList.contains("hidden"));
  assert.equal(t.doc.getElementById("drill-timeline").children.length, 2);
  assert.equal(t.doc.getElementById("drill-timeline").children[0].textContent, "2 ts parked");
  push('event: start\ndata: {"run":"001-implementer","header":"run 001-implementer"}\n\n');
  push('event: lines\ndata: {"lines":["● session","  ok"]}\n\n');
  await settle();
  assert.equal(t.doc.getElementById("drill-tail").textContent, "run 001-implementer● session  ok");
  push('event: end\ndata: {"status":"finished"}\n\n');
  await settle();
  assert.ok(t.doc.getElementById("drill-tail").textContent.endsWith("■ run ended (finished)"));
  t.env.location.hash = "";
  t.app.route();
  assert.ok(aborted, "leaving the drilldown aborts the tail stream");
  assert.ok(!t.doc.getElementById("main").classList.contains("hidden"));
});

// SPEC-0433: the drilldown timeline is the campaign story, collapsible, with a raw toggle.
const STORY = {
  schema: "nw.campaign.story.v1", events: 6, lines: 3,
  loops: [
    { id: "loop0", header: "Loop 1 · 5m · $0.00 → $1.00", groups: [
      { id: "g1", header: "Implementing · IMPLEMENTING → GATING · 5m · $0.00 → $1.00", lines: [
        { id: "l2", text: "Implementer (opus, 5m, $1.00): done — x", tone: "ok", quiet: false, seqs: [2, 3], children: [] }] }] },
    { id: "loop1", header: "Loop 2 · 1m · $1.00 → $1.13", groups: [
      { id: "g4", header: "Gating & triage · GATING → TRIAGING · 1m · $1.00 → $1.13", lines: [
        { id: "l4", text: "Gate #1 on box — FAILED after 40s (pytest)", tone: "bad", quiet: false, seqs: [4],
          children: [{ text: "✗ pytest (40s, exit 1)", tone: "bad", seqs: [4] }] },
        { id: "l5", text: "Gate triager (sonnet, 9s, $0.13): retry — suspected flaky (medium)", tone: "", quiet: false, seqs: [5], children: [] },
        { id: "l6", text: "Priority 3 → 1 (conductor)", tone: "", quiet: true, seqs: [6], children: [] }] },
      { id: "g7", header: "Gating · GATING · 0s · $1.13", lines: [] }] }],
};
const RAW = { 2: "2 run.start", 3: "3 run.end", 4: "4 gate ok=False", 5: "5 run.end gate_triager", 6: "6 priority" };

async function drill(body) {
  const t = setup("#/c/C1");
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({}))]);
  t.routes.push([(u) => u === "/api/campaigns/C1", () => resp(200, {
    campaign: "C1", state: { state: "GATING", label: "SPEC-1", loop: 2, cost_usd: 1.13, cap_usd: 5, park: null },
    timeline: [{ text: "old 5" }, { text: "old 6" }], runs: [], current_run: null, running: false, ...body })]);
  t.app.start();
  await settle();
  return t;
}
const tl = (t) => t.doc.getElementById("drill-timeline");
const texts = (t, cls) => byClass(t.doc, "drill-timeline", cls).map((e) => e.textContent);

test("SPEC-0433: the drilldown shows the story newest first, the newest talking group open", async () => {
  const t = await drill({ story: STORY, raw: RAW });
  assert.deepEqual(texts(t, "tl-loop"), ["Loop 2 · 1m · $1.00 → $1.13", "Loop 1 · 5m · $0.00 → $1.00"]);
  const groups = texts(t, "tl-group");
  assert.ok(groups[0].startsWith("▾ Gating · GATING"), groups[0]);          // newest group open
  assert.ok(groups[1].startsWith("▾ Gating & triage"), groups[1]);          // newest with lines open
  assert.ok(groups[2].startsWith("▸ Implementing"), groups[2]);             // older collapsed
  const lines = texts(t, "tl-line");
  assert.deepEqual(lines, ["▸ Gate #1 on box — FAILED after 40s (pytest)  [+1]",
    "▸ Gate triager (sonnet, 9s, $0.13): retry — suspected flaky (medium)", "▸ details (1)"]);
  assert.ok(byClass(t.doc, "drill-timeline", "tl-line")[0].classList.contains("tone-bad"));
  assert.ok(tl(t).textContent.includes("3 lines from 6 events"));
});

test("SPEC-0433: a tap opens a line's details and raw events; groups toggle; open state survives a refresh", async () => {
  const t = await drill({ story: STORY, raw: RAW });
  byClass(t.doc, "drill-timeline", "tl-line")[0].click();
  assert.deepEqual(texts(t, "tl-kid"), ["✗ pytest (40s, exit 1)"]);
  assert.deepEqual(texts(t, "tl-raw"), ["4 gate ok=False"]);
  byClass(t.doc, "drill-timeline", "tl-line")[2].click();                 // quiet details
  assert.ok(texts(t, "tl-line").includes("▸ Priority 3 → 1 (conductor)"));
  byClass(t.doc, "drill-timeline", "tl-group")[2].click();                // open loop 1's group
  assert.ok(texts(t, "tl-line").some((x) => x.startsWith("▸ Implementer (opus")));
  byClass(t.doc, "drill-timeline", "tl-group")[1].click();                // collapse the gate group
  assert.ok(!texts(t, "tl-line").some((x) => x.includes("Gate #1")));
  t.app.route();                                                           // same cid: no reset
  assert.ok(texts(t, "tl-line").some((x) => x.startsWith("▸ Implementer (opus")));
});

test("SPEC-0433: the raw toggle shows every raw event newest first, and back", async () => {
  const t = await drill({ story: STORY, raw: RAW });
  const toggle = () => byClass(t.doc, "drill-timeline", "tl-toggle")[0];
  assert.equal(toggle().textContent, "raw");
  toggle().click();
  assert.deepEqual(texts(t, "tl"), ["6 priority", "5 run.end gate_triager", "4 gate ok=False", "3 run.end", "2 run.start"]);
  assert.equal(toggle().textContent, "story");
  assert.equal(texts(t, "tl-group").length, 0);
  toggle().click();
  assert.ok(texts(t, "tl-group").length > 0);
});

test("SPEC-0433: a story that failed falls back to the raw events (or the old timeline)", async () => {
  let t = await drill({ story: { error: "ValueError: bad" }, raw: RAW });
  assert.equal(texts(t, "tl")[0], "6 priority");
  assert.equal(byClass(t.doc, "drill-timeline", "tl-toggle").length, 0);
  t = await drill({ story: { error: "x" }, raw: { error: "x" } });
  assert.deepEqual(texts(t, "tl"), ["old 6", "old 5"]);
});

// ---- SPEC-0434: answers land at once; the page survives a server restart ----

const PLAN_OK = () => resp(200, { plan: { summary: "chose 1 (resume)" }, effect: "The conductor resumes it.", plan_token: "1.tok" });
const page = (csrf) => ({ status: 200, headers: { get: () => null },
  text: async () => `<!doctype html><meta name="nw-csrf" content="${csrf}">`, json: async () => null });
const escIds = (t) => byClass(t.doc, "needs", "esc").map((n) => n.attrs["data-esc"]);
const msgs = (t) => byClass(t.doc, "needs", "msg").map((n) => n.textContent);

async function planAndConfirm(t) {
  byClass(t.doc, "needs", "opt")[0].click();
  await settle();
  byClass(t.doc, "needs", "primary")[0].click();
  await settle();
}

test("SPEC-0434: a 200 answer removes the escalation at once, even while the server still lists it", async () => {
  const t = setup();
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }, "r1"))]);
  t.routes.push([(u) => u.endsWith("/plan"), PLAN_OK]);
  t.routes.push([(u) => u.endsWith("/answer"), () => resp(200, { summary: "chose 1 (resume)" })]);
  t.app.start();
  await settle();
  const before = t.calls.filter((c) => c.url === "/api/snapshot").length;
  await planAndConfirm(t);
  assert.deepEqual(escIds(t), [], "gone before any new snapshot");
  assert.ok(t.doc.getElementById("needs").textContent.includes("Nothing needs you."));
  assert.ok(/^\d\d:\d\d:\d\d ESC-0007 answered/.test(msgs(t)[0]), msgs(t)[0]);
  assert.equal(t.calls.filter((c) => c.url === "/api/snapshot").length, before + 1, "refetched at once");
  await t.clock.advance(30000, settle);                  // 304s / the same rev: still hidden
  assert.deepEqual(escIds(t), []);
  await t.clock.advance(31000, settle);                  // past 60 s the server's view wins again
  t.routes.unshift([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }, "r9"))]);
  await t.clock.advance(15000, settle);
  assert.deepEqual(escIds(t), ["ESC-0007"]);
});

test("SPEC-0434: a snapshot without the answered id ends the hiding (a later re-open shows)", async () => {
  const t = setup();
  let listed = true, rev = "r1";
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: listed ? [ESC] : [] }, rev))]);
  t.routes.push([(u) => u.endsWith("/plan"), PLAN_OK]);
  t.routes.push([(u) => u.endsWith("/answer"), () => { listed = false; rev = "r2"; return resp(200, { summary: "s" }); }]);
  t.app.start();
  await settle();
  await planAndConfirm(t);
  assert.equal(t.app.rev(), "r2");
  listed = true; rev = "r3";                              // escalated again under the same id
  await t.clock.advance(15000, settle);
  assert.deepEqual(escIds(t), ["ESC-0007"]);
});

test("SPEC-0434: bad_csrf on a plan refreshes the value from / and retries the plan once", async () => {
  const t = setup();
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }))]);
  t.routes.push([(u) => u === "/", () => page("fresh.value")]);
  t.routes.push([(u, o) => u.endsWith("/plan") && o.headers["X-NW-CSRF"] === "nonce.mac", () => resp(403, { reason: "bad_csrf", error: "stale" })]);
  t.routes.push([(u) => u.endsWith("/plan"), PLAN_OK]);
  t.app.start();
  await settle();
  byClass(t.doc, "needs", "opt")[0].click();
  await settle();
  const p = posts(t);
  assert.deepEqual(p.map((c) => c.opts.headers["X-NW-CSRF"]), ["nonce.mac", "fresh.value"]);
  assert.equal(byClass(t.doc, "needs", "confirm").length, 1);
  assert.deepEqual(msgs(t), []);
});

test("SPEC-0434: bad_csrf on an answer never re-sends it blind: re-plan, confirm again, then it lands", async () => {
  const t = setup();
  let csrfOk = "nonce.mac";
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }))]);
  t.routes.push([(u) => u === "/", () => page("after.restart")]);
  t.routes.push([(u, o) => u.endsWith("/plan") || u.endsWith("/answer") ? o.headers["X-NW-CSRF"] !== csrfOk : false,
                 () => resp(403, { reason: "bad_csrf", error: "stale" })]);
  t.routes.push([(u) => u.endsWith("/plan"), PLAN_OK]);
  t.routes.push([(u) => u.endsWith("/answer"), () => resp(200, { summary: "chose 1 (resume)" })]);
  t.app.start();
  await settle();
  byClass(t.doc, "needs", "opt")[0].click();
  await settle();
  csrfOk = "after.restart";                               // the server restarted with a new secret
  byClass(t.doc, "needs", "primary")[0].click();
  await settle();
  const answers = () => posts(t).filter((c) => c.url.endsWith("/answer"));
  assert.equal(answers().length, 1, "the refused answer was not re-sent");
  assert.equal(byClass(t.doc, "needs", "confirm").length, 1, "a fresh confirm panel");
  assert.ok(msgs(t).some((m) => m.includes("restarted") && m.includes("confirm again")), msgs(t).join("|"));
  assert.equal(posts(t).at(-1).url, "/api/escalations/ESC-0007/plan");
  assert.equal(posts(t).at(-1).opts.headers["X-NW-CSRF"], "after.restart");
  byClass(t.doc, "needs", "primary")[0].click();
  await settle();
  assert.equal(answers().length, 2);
  assert.deepEqual(escIds(t), []);
});

test("SPEC-0434: a bad plan token and an expired confirmation re-plan once; a second refusal is shown", async () => {
  const t = setup();
  t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }))]);
  t.routes.push([(u) => u.endsWith("/plan"), PLAN_OK]);
  let n = 0;
  t.routes.push([(u) => u.endsWith("/answer"), () => (++n === 1
    ? resp(400, { reason: "bad_plan_token", error: "the plan token does not match" })
    : resp(409, { status: "plan_expired", error: "the confirmation expired: plan the answer again" }))]);
  t.app.start();
  await settle();
  await planAndConfirm(t);
  assert.equal(byClass(t.doc, "needs", "confirm").length, 1);
  byClass(t.doc, "needs", "primary")[0].click();
  await settle();
  assert.equal(byClass(t.doc, "needs", "confirm").length, 0, "one automatic re-plan only");
  assert.ok(msgs(t).some((m) => m.includes("confirmation expired")), msgs(t).join("|"));
});

test("SPEC-0434: a network error, a timeout and a non-JSON 502 on an answer each leave a reason", async () => {
  for (const [kind, want] of [["throw", "may or may not have been applied"], ["hang", "No reply from the dashboard within 20 s"],
                              ["502", "The dashboard failed (502)"]]) {
    const t = setup();
    t.routes.push([(u) => u === "/api/snapshot", () => resp(200, snap({ escalations: [ESC] }))]);
    t.routes.push([(u) => u.endsWith("/plan"), PLAN_OK]);
    t.routes.push([(u) => u.endsWith("/answer"), () => {
      if (kind === "throw") throw new Error("net");
      if (kind === "hang") return new Promise(() => {});
      return { status: 502, headers: { get: () => null }, json: async () => { throw new Error("not json"); } };
    }]);
    t.app.start();
    await settle();
    await planAndConfirm(t);
    if (kind === "hang") {
      assert.ok(byClass(t.doc, "needs", "primary")[0].textContent === "Answering…");
      await t.clock.advance(20000, settle);
    }
    assert.ok(msgs(t).some((m) => m.includes(want)), `${kind}: ${msgs(t).join("|")}`);
    assert.deepEqual(escIds(t), ["ESC-0007"], `${kind}: still listed (the outcome is the server's)`);
  }
});
