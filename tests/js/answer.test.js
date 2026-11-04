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
