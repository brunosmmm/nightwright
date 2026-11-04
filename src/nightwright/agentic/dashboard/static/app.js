"use strict";
/* SPEC-0413: the dashboard phone page. Polls GET /api/snapshot every 15 s with If-None-Match,
   pauses while the tab is hidden, and derives ages from the server's clock. (SPEC-0402) When
   env.live is set it reads GET /api/stream through fetch(); polling is the fallback.
   (SPEC-0428) No token: loopback needs no login, the tailnet is gated by whois server side; a
   #token= fragment from an old bookmark is removed and ignored. Writes carry the page's CSRF
   value (env.csrf(), the <meta name="nw-csrf"> the server fills in) as X-NW-CSRF.
   (SPEC-0429) The inbox answers escalations: choose / text / override -> plan -> confirm ->
   answer. (SPEC-0430) #/c/<cid> is a campaign drilldown with a live run tail.
   Every server value is rendered with textContent only. createApp(env) takes every browser
   object it touches, so the logic runs under node with a fake DOM (tests/js). */

var POLL_MS = 15000;
var TICK_MS = 1000;
var STALE_S = 45;
var MAX_EVENTS = 20;
var OLD_TOKEN_KEY = "nw-dashboard-token";   /* SPEC-0428: removed from storage when found */
var TAIL_MAX_LINES = 1000;
var WATCHDOG_MS = 45000;            /* SPEC-0402: a stream silent this long (3 heartbeats) is dropped */
var BACKOFF_MIN_MS = 2000;
var BACKOFF_MAX_MS = 30000;

var MSG_401 = "The dashboard refused this request (401).";
var MSG_403 = "This device is not allowed to view the dashboard.";
var MSG_ERR = "Can't reach the dashboard.";

/* SPEC-0402: an incremental Server-Sent Events parser. push(text) takes chunks split anywhere
   (inside a line, between a CR and its LF); events, comments and `retry:` go to the callbacks. */
function createSseParser(on) {
  var buf = "", data = [], type = "", id = null;

  function dispatch() {
    if (data.length) on.event({ type: type || "message", id: id, data: data.join("\n") });
    data = []; type = "";
  }

  function line(l) {
    if (l === "") return dispatch();
    if (l.charAt(0) === ":") { if (on.comment) on.comment(l.slice(1).replace(/^ /, "")); return; }
    var i = l.indexOf(":");
    var field = i < 0 ? l : l.slice(0, i);
    var val = i < 0 ? "" : l.slice(i + 1).replace(/^ /, "");
    if (field === "data") data.push(val);
    else if (field === "event") type = val;
    else if (field === "id") id = val;
    else if (field === "retry" && /^[0-9]+$/.test(val) && on.retry) on.retry(Number(val));
  }

  function push(text) {
    buf += text;
    var m;
    while ((m = /\r\n|\n|\r/.exec(buf)) !== null) {
      if (m[0] === "\r" && m.index + 1 === buf.length) break;   /* maybe half of a CRLF */
      var l = buf.slice(0, m.index);
      buf = buf.slice(m.index + m[0].length);
      line(l);
    }
  }

  return { push: push };
}

function isErr(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v) &&
    Object.keys(v).length === 1 && typeof v.error === "string";
}

function num(v) {
  var n = Number(v);
  return typeof v === "number" || (typeof v === "string" && v !== "") ? (isFinite(n) ? n : null) : null;
}

function formatUsd(v) {
  var n = num(v);
  return "$" + (n === null ? 0 : n).toFixed(2);
}

function formatAge(s) {
  var n = num(s);
  if (n === null) return "?";
  n = Math.max(0, Math.floor(n));
  if (n < 60) return n + "s";
  if (n < 3600) return Math.floor(n / 60) + "m";
  if (n < 86400) return Math.floor(n / 3600) + "h " + Math.floor((n % 3600) / 60) + "m";
  return Math.floor(n / 86400) + "d " + Math.floor((n % 86400) / 3600) + "h";
}

/* Age now = the age at generated_at plus the server time elapsed since; never negative. */
function displayAge(ageS, generatedMs, serverNowMs) {
  var base = num(ageS);
  if (base === null) return null;
  var delta = isFinite(generatedMs) && isFinite(serverNowMs) ? Math.max(0, serverNowMs - generatedMs) / 1000 : 0;
  return Math.floor(base + delta);
}

function needCount(snap) {
  var inbox = snap.inbox || {};
  var n = 0;
  if (Array.isArray(inbox.escalations)) n += inbox.escalations.length;
  if (Array.isArray(inbox.parks)) n += inbox.parks.length;
  return n;
}

function errorSections(snap) {
  var h = snap.health || {};
  var inbox = snap.inbox || {};
  var cands = [["stewards", h.stewards], ["herdr watch", h.herdr_watch]];
  ["totals", "slots", "gate_pools", "active", "needs_you", "conductor_handling", "waiting",
    "backlog", "delivered", "recent_events"].forEach(function (k) { cands.push([k, snap[k]]); });
  cands.push(["inbox.escalations", inbox.escalations], ["inbox.parks", inbox.parks]);
  return cands.filter(function (c) { return isErr(c[1]); }).map(function (c) { return c[0]; });
}

/* conn: "ok" | "stale" | "error" | "denied" | "none" (nothing received yet) */
function assess(snap, conn) {
  if (!snap) {
    return { level: "grey", reason: conn === "denied" ? "no access" : conn === "error" ? "offline" : "loading" };
  }
  if (conn === "stale") return { level: "grey", reason: "stale" };
  if (conn === "error") return { level: "grey", reason: "offline" };
  if (conn === "denied") return { level: "grey", reason: "no access" };
  var h = snap.health || {};
  var runner = h.runner;
  var cond = h.conductor;
  var red = [];
  if (isErr(runner)) red.push("runner error");
  else if (!runner || !runner.lease || !runner.lease.held) red.push("runner down");
  if (isErr(cond)) red.push("conductor error");
  else if (!cond || !cond.pid) red.push("conductor down");
  if (red.length) return { level: "red", reason: red.join(", ") };
  var amber = [];
  if (runner.lease.stale) amber.push("stale lease");
  if (runner.draining) amber.push("draining");
  if (runner.runner_code && runner.runner_code.stale) amber.push("stale code");
  var need = needCount(snap);
  if (need) amber.push(need + " need you");
  var errs = errorSections(snap);
  if (errs.length) amber.push(errs.length + " section" + (errs.length === 1 ? "" : "s") + " in error");
  if (amber.length) return { level: "amber", reason: amber.join(", ") };
  return { level: "green", reason: "all good" };
}

function healthLevel(snap, conn) {
  return assess(snap, conn).level;
}

function createApp(env) {
  var doc = env.document;
  var st = {
    snap: null, rev: null, conn: "none", code: null, generatedMs: NaN,
    anchor: null, lastOk: null, startedAt: null, visibleSince: null,
    inflight: null, pollTimer: null, tickTimer: null, ages: [], shown: {}
  };
  /* SPEC-0402 stream state. mode: off (hidden or not started), polling (waiting to retry),
     reconnecting (a retry in flight), live. gen invalidates a superseded stream. */
  var lv = { mode: "off", gen: 0, ctrl: null, backoff: BACKOFF_MIN_MS, retryTimer: null,
             dogTimer: null, failed: false };

  function hidden() { return env.visibility() === "hidden"; }
  function $(id) { return doc.getElementById(id); }

  function el(tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) cls.split(" ").forEach(function (c) { e.classList.add(c); });
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function put(id, nodes) {
    var host = $(id);
    host.replaceChildren.apply(host, nodes);
  }

  function setText(id, text) {
    if (st.shown[id] === text) return;
    st.shown[id] = text;
    $(id).textContent = text;
  }

  function serverNow() {
    var t = env.now();
    return st.anchor ? st.anchor.server + (t - st.anchor.local) : t;
  }

  /* ---- ages ---- */

  /* A node whose text is prefix + the live age of `base` (an *_age_s style field). */
  function ageNode(tag, cls, prefix, base) {
    var n = el(tag, cls);
    if (num(base) === null) {
      n.textContent = prefix + "?";
      return n;
    }
    var fn = function () { return prefix + formatAge(displayAge(base, st.generatedMs, serverNow())); };
    n.textContent = fn();
    st.ages.push({ node: n, fn: fn });
    return n;
  }

  function updateAges() {
    st.ages.forEach(function (a) {
      var t = a.fn();
      if (a.node.textContent !== t) a.node.textContent = t;
    });
  }

  /* ---- rendering ---- */

  function muted(name, v) { return el("p", "muted", name + ": " + v.error); }

  function row(key, value) {
    var r = el("div", "row");
    r.appendChild(el("span", "k", key));
    r.appendChild(el("span", "v", value));
    return r;
  }

  function labelOf(r) { return (r && (r.label || r.spec_id || r.campaign)) || "-"; }

  /* ---- the decisions inbox (SPEC-0403 payload, SPEC-0429 answers) ---- */

  /* Per-escalation UI state survives re-renders: typed text, ticked boxes, an open confirm
     panel. `inboxSig` skips re-rendering the inbox while its data is unchanged, so typing is
     never interrupted by an unrelated snapshot. */
  var ui = {};
  var flash = [];
  var inboxSig = null;

  function uiOf(id) {
    if (!ui[id]) ui[id] = { text: "", note: "", reason: "", waive: {}, attest: {}, confirm: null, msg: null, busy: false };
    return ui[id];
  }

  /* A campaign link (SPEC-0430): a button that sets the #/c/<cid> route (no href attribute is
     ever written from data, SPEC-0413's guard). */
  function link(text, cid) {
    return button(text, "camp", function () {
      env.location.hash = "#/c/" + encodeURIComponent(cid);
      route();
    });
  }

  function button(text, cls, fn) {
    var b = el("button", cls, text);
    b.setAttribute("type", "button");
    b.addEventListener("click", function (ev) { if (ev && ev.preventDefault) ev.preventDefault(); fn(); });
    return b;
  }

  function field(tag, label, value, onInput) {
    var wrap = el("label", "field");
    wrap.appendChild(el("span", "k", label));
    var f = el(tag, "");
    f.value = value;
    f.addEventListener("input", function () { onInput(f.value); });
    wrap.appendChild(f);
    return wrap;
  }

  function check(label, on, onChange) {
    var wrap = el("label", "check");
    var c = el("input", "");
    c.setAttribute("type", "checkbox");
    c.checked = !!on;
    c.addEventListener("change", function () { onChange(!!c.checked); });
    wrap.appendChild(c);
    wrap.appendChild(el("span", "", label));
    return wrap;
  }

  function rerenderInbox() {
    inboxSig = null;
    if (st.snap) renderNeeds(st.snap);
  }

  function writeHeaders() {
    var h = { "Content-Type": "application/json" };
    var c = env.csrf ? env.csrf() : null;
    if (c) h["X-NW-CSRF"] = c;
    return h;
  }

  async function post(path, body) {
    var res;
    try {
      res = await env.fetch(path, { method: "POST", cache: "no-store", credentials: "same-origin",
                                    headers: writeHeaders(), body: JSON.stringify(body) });
    } catch (e) {
      return { status: 0, body: { error: "Can't reach the dashboard." } };
    }
    var out = null;
    try { out = await res.json(); } catch (e) { out = null; }
    return { status: res.status, body: out && typeof out === "object" ? out : {} };
  }

  function refusalText(r) {
    var b = r.body || {};
    if (r.status === 409) {
      var who = b.by ? " by " + b.by : "";
      var when = b.at ? " at " + b.at : "";
      if (b.status === "answered" || b.status === "withdrawn") return "Already " + b.status + who + when + ". " + (b.error || "");
      return "No longer applicable: " + (b.error || b.status || "conflict");
    }
    if (r.status === 403 && b.reason === "bad_csrf") return "This page is stale: reload it, then answer again.";
    if (r.status === 403) return "This device may not answer (" + (b.reason || "forbidden") + ").";
    if (r.status === 0) return b.error;
    return (b.error || "Refused") + " (" + r.status + ")";
  }

  async function planAnswer(e, body) {
    var u = uiOf(e.id);
    if (u.busy) return;
    if (u.note.trim()) body.note = u.note;
    u.busy = true; u.msg = null;
    rerenderInbox();
    var r = await post("/api/escalations/" + encodeURIComponent(e.id) + "/plan", body);
    u.busy = false;
    if (r.status === 200 && r.body.plan_token) u.confirm = { body: body, res: r.body };
    else {
      u.confirm = null;
      u.msg = { cls: "err", text: refusalText(r) };
      if (r.status === 409) poll();
    }
    rerenderInbox();
  }

  async function confirmAnswer(e) {
    var u = uiOf(e.id);
    if (u.busy || !u.confirm) return;
    var body = {};
    Object.keys(u.confirm.body).forEach(function (k) { body[k] = u.confirm.body[k]; });
    body.plan_token = u.confirm.res.plan_token;
    u.busy = true;
    rerenderInbox();
    var r = await post("/api/escalations/" + encodeURIComponent(e.id) + "/answer", body);
    u.busy = false;
    if (r.status === 200) {
      flash.unshift({ cls: "ok", text: e.id + " answered: " + (r.body.summary || "") +
        (r.body.event_error ? " (the conductor was NOT woken: " + r.body.event_error + ")" : " — the conductor applies it.") });
      flash = flash.slice(0, 3);
      delete ui[e.id];
      poll();
    } else {
      u.confirm = null;
      u.msg = { cls: "err", text: refusalText(r) };
      if (r.status === 409) poll();
    }
    rerenderInbox();
  }

  function confirmNode(e, u) {
    var res = u.confirm.res;
    var plan = res.plan || {};
    var box = el("div", "confirm");
    box.appendChild(el("p", "k", "Confirm this answer"));
    box.appendChild(el("p", "summary", plan.summary || ""));
    if (plan.reverses_summary) box.appendChild(el("p", "muted", "reverses the auto answer: " + plan.reverses_summary));
    var ov = plan.override;
    if (ov) {
      (ov.waive || []).forEach(function (w) { box.appendChild(el("p", "muted", "waive " + w.id + " · " + (w.severity || "") + " " + (w.title || ""))); });
      (ov.attest || []).forEach(function (a) { box.appendChild(el("p", "muted", "attest: " + a)); });
      (ov.implicitly_attested || []).forEach(function (a) { box.appendChild(el("p", "muted", "implicitly attested: " + a)); });
    }
    box.appendChild(el("p", "effect", res.effect || ""));
    var row2 = el("div", "actions");
    row2.appendChild(button(u.busy ? "Answering…" : "Confirm", "primary", function () { confirmAnswer(e); }));
    row2.appendChild(button("Cancel", "", function () { u.confirm = null; rerenderInbox(); }));
    box.appendChild(row2);
    return box;
  }

  function escNode(e, auto) {
    var u = uiOf(e.id);
    var d = el("div", "need esc");
    d.setAttribute("data-esc", String(e.id));
    var head = el("div", "head");
    head.appendChild(el("span", "id", e.id));
    if (e.campaign) head.appendChild(link(labelOf(e), e.campaign));
    else head.appendChild(el("span", "who", labelOf(e)));
    d.appendChild(head);
    if (auto) d.appendChild(el("p", "auto", "auto-applied: " + (e.auto_answer || "") + " — you may reverse it once"));
    d.appendChild(el("p", "question", e.question_text || e.question || ""));
    if (e.error) { d.appendChild(el("p", "muted", e.error)); return d; }
    (Array.isArray(e.facts) ? e.facts : []).forEach(function (f) { d.appendChild(el("p", "fact muted", "· " + f)); });
    if (e.recommendation !== null && e.recommendation !== undefined) {
      var rec = (e.options || []).filter(function (o) { return String(o.n) === String(e.recommendation); })[0];
      d.appendChild(el("p", "muted", "recommended: " + e.recommendation + (rec ? " (" + rec.label + ")" : "")));
    }
    if (u.msg) d.appendChild(el("p", "msg " + u.msg.cls, u.msg.text));
    if (!e.answerable) {
      d.appendChild(el("p", "muted", e.pending ? "Waiting for the campaign to park; answer once it does." : "Not answerable: " + (e.why_not || "?")));
      return d;
    }
    if (u.confirm) { d.appendChild(confirmNode(e, u)); return d; }
    var forms = Array.isArray(e.forms) ? e.forms : [];
    if (forms.indexOf("choose") >= 0) {
      var opts = el("div", "options");
      (e.options || []).forEach(function (o) {
        var label = o.n + ". " + o.label + (String(o.n) === String(e.recommendation) ? " (recommended)" : "");
        opts.appendChild(button(label, "opt", function () { planAnswer(e, { form: "choose", choose: o.n }); }));
      });
      d.appendChild(opts);
    }
    if (forms.indexOf("text") >= 0) {
      d.appendChild(field("textarea", "or answer in your words", u.text, function (v) { u.text = v; }));
      d.appendChild(button("Answer with this text", "", function () {
        if (!u.text.trim()) { u.msg = { cls: "err", text: "Write an answer first." }; rerenderInbox(); return; }
        planAnswer(e, { form: "text", text: u.text });
      }));
    }
    if (forms.indexOf("override") >= 0) {
      var ov = el("details", "override");
      ov.appendChild(el("summary", "", "Override the review (waive findings / attest ACs)"));
      (e.findings || []).forEach(function (f) {
        ov.appendChild(check("waive " + f.id + " · " + (f.severity || "") + " " + (f.title || ""), u.waive[f.id], function (on) { u.waive[f.id] = on; }));
      });
      (e.unmet_acs || []).forEach(function (a) {
        ov.appendChild(check("attest met: " + a, u.attest[a], function (on) { u.attest[a] = on; }));
      });
      ov.appendChild(field("textarea", "reason (required)", u.reason, function (v) { u.reason = v; }));
      ov.appendChild(button("Plan this override", "", function () {
        var w = Object.keys(u.waive).filter(function (k) { return u.waive[k]; });
        var a = Object.keys(u.attest).filter(function (k) { return u.attest[k]; });
        planAnswer(e, { form: "override", waive: w, attest: a, reason: u.reason });
      }));
      if (Object.keys(u.waive).some(function (k) { return u.waive[k]; }) ||
          Object.keys(u.attest).some(function (k) { return u.attest[k]; }) || u.reason) ov.setAttribute("open", "");
      d.appendChild(ov);
    }
    d.appendChild(field("input", "note (optional, kept with the answer)", u.note, function (v) { u.note = v; }));
    return d;
  }

  function renderNeeds(snap) {
    var inbox = snap.inbox || {};
    var esc = inbox.escalations;
    var parks = inbox.parks;
    var auto = inbox.auto_applied;
    var sig = JSON.stringify([esc, parks, auto, flash]);
    if (sig === inboxSig) return;
    inboxSig = sig;
    var nodes = [];
    var count = 0;
    flash.forEach(function (f) { nodes.push(el("p", "msg " + f.cls, f.text)); });
    if (isErr(esc)) nodes.push(muted("inbox.escalations", esc));
    else (esc || []).forEach(function (e) { nodes.push(escNode(e, false)); count++; });
    if (isErr(parks)) nodes.push(muted("inbox.parks", parks));
    else (parks || []).forEach(function (p) {
      var park = p.park || {};
      var d = el("div", "need");
      d.appendChild(el("span", "id", "park"));
      if (p.campaign) d.appendChild(link(labelOf(p), p.campaign));
      else d.appendChild(el("span", "who", labelOf(p)));
      d.appendChild(el("p", "", park.reason + (park.detail ? ": " + park.detail : "")));
      if (p.fix) d.appendChild(el("p", "muted", p.fix));
      nodes.push(d);
      count++;
    });
    if (isErr(auto)) nodes.push(muted("inbox.auto_applied", auto));
    else (auto || []).forEach(function (e) { nodes.push(escNode(e, true)); });
    if (!count && !isErr(esc) && !isErr(parks)) nodes.push(el("p", "muted", "Nothing needs you."));
    put("needs", nodes);
  }

  function renderCounts(snap) {
    var t = snap.totals;
    if (isErr(t)) {
      put("counts", [muted("totals", t)]);
      put("spend", []);
      return;
    }
    t = t || {};
    var g = function (k) { return num(t[k]) === null ? 0 : t[k]; };
    put("counts", [el("p", "", "active " + g("active") + " · queued " + g("ready") +
      " · parked " + g("attention") + " · waiting " + g("waiting") + " · backlog " +
      g("backlog") + " · conductor-handling " + g("conductor_handling"))]);
    var s = el("p", "spend");
    s.appendChild(el("span", "k", "spend today "));
    s.appendChild(el("span", "v", formatUsd(t.spend_today_usd)));
    put("spend", [s]);
  }

  function costNode(r) {
    var cost = num(r.cost_usd);
    var cap = num(r.cap_usd);
    var box = el("div", "cost");
    if (cost === null) cost = 0;
    if (cap === null || cap <= 0) {
      box.appendChild(el("p", "", formatUsd(cost) + " · no cap"));
      return box;
    }
    box.appendChild(el("p", "", formatUsd(cost) + " / " + formatUsd(cap)));
    var ratio = cost / cap;
    var pct = Math.min(100, Math.round(ratio * 100));
    var bar = el("progress", ratio >= 1 ? "crit" : ratio >= 0.8 ? "warn" : "");
    bar.setAttribute("max", "100");
    bar.value = Math.min(100, ratio * 100);
    bar.setAttribute("aria-valuenow", String(Math.max(0, pct)));
    bar.setAttribute("aria-valuemax", "100");
    box.appendChild(bar);
    return box;
  }

  function activityNode(r) {
    var a = r.activity || {};
    if (a.kind === "run") {
      var n = ageNode("p", "", String(a.persona || "run") + " · ", a.elapsed_s);
      return n;
    }
    return el("p", "muted", a.kind === "gates" ? "gates" : "idle");
  }

  function renderActive(snap) {
    var rows = snap.active;
    if (isErr(rows)) { put("active", [muted("active", rows)]); return; }
    if (!Array.isArray(rows) || !rows.length) { put("active", [el("p", "muted", "No active campaigns.")]); return; }
    put("active", rows.map(function (r) {
      var c = el("div", "run");
      var title = labelOf(r);
      if (r.spec_id && r.spec_id !== title) title += " " + r.spec_id;
      var h3 = el("h3", "");
      if (r.campaign) h3.appendChild(link(title, r.campaign)); else h3.textContent = title;
      c.appendChild(h3);
      c.appendChild(el("p", "muted", [r.state, r.loops ? "loop " + r.loops : ""].filter(Boolean).join(" · ")));
      c.appendChild(activityNode(r));
      c.appendChild(costNode(r));
      c.appendChild(ageNode("p", "muted", "age ", r.age_s));
      return c;
    }));
  }

  function renderHealth(snap) {
    var h = snap.health || {};
    var nodes = [];
    var runner = h.runner;
    if (isErr(runner)) nodes.push(muted("runner", runner));
    else {
      var lease = (runner && runner.lease) || {};
      var r = el("div", "row");
      r.appendChild(el("span", "k", "runner"));
      if (!lease.held) r.appendChild(el("span", "v", "down"));
      else {
        r.appendChild(el("span", "v", lease.host ? lease.pid + "@" + lease.host : String(lease.pid)));
        /* heartbeat_age_s resets on every heartbeat and rev ignores it, so across 304s the page
           cannot tick it as an age that only grows: show it (ticking) only for a stale lease,
           whose flip is part of rev and so arrives as a 200. */
        if (lease.stale) {
          r.appendChild(ageNode("span", "v", "heartbeat ", lease.heartbeat_age_s));
          r.appendChild(el("span", "v", "stale lease"));
        } else r.appendChild(el("span", "v", "heartbeat ok"));
      }
      if (runner && runner.draining) r.appendChild(el("span", "v", "draining"));
      if (runner && runner.runner_code && runner.runner_code.stale) r.appendChild(el("span", "v", "stale code"));
      nodes.push(r);
    }
    var cond = h.conductor;
    if (isErr(cond)) nodes.push(muted("conductor", cond));
    else {
      var lag = cond && num(cond.lag);
      nodes.push(row("conductor", (cond && cond.pid ? String(cond.pid) : "down") + (lag ? " · lag " + lag : "")));
    }
    var sts = h.stewards;
    if (isErr(sts)) nodes.push(muted("stewards", sts));
    else {
      var list = Array.isArray(sts) ? sts : [];
      nodes.push(row("stewards", list.length ? list.length + " · " + list.map(function (s) { return s.address; }).join(", ") : "none"));
    }
    var hw = h.herdr_watch;
    if (isErr(hw)) nodes.push(muted("herdr watch", hw));
    else if (hw === null || hw === undefined) nodes.push(row("herdr watch", "n/a"));
    else nodes.push(row("herdr watch", hw.running ? "running" + (hw.pid ? " (" + hw.pid + ")" : "") : "not running"));
    put("health", nodes);
  }

  function clock(ts) {
    var d = new Date(ts);
    if (isNaN(d.getTime())) return "--:--";
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function renderEvents(snap) {
    var evs = snap.recent_events;
    if (isErr(evs)) { put("events", [muted("recent events", evs)]); return; }
    if (!Array.isArray(evs) || !evs.length) { put("events", [el("p", "muted", "No recent events.")]); return; }
    put("events", evs.slice(-MAX_EVENTS).reverse().map(function (e) {
      var d = el("div", "event");
      var when = el("span", "when", clock(e.ts));
      if (typeof e.ts === "string") when.setAttribute("title", e.ts);
      d.appendChild(when);
      d.appendChild(el("span", "what", e.type + " " + labelOf(e)));
      d.appendChild(el("p", "muted", e.summary));
      return d;
    }));
  }

  function render() {
    var snap = st.snap;
    st.ages = [];
    st.generatedMs = Date.parse(snap.generated_at);
    /* Time since the last successful poll (200 or 304), not since generated_at: on a quiet
       queue rev does not change, so generated_at stays old while every poll confirms it. */
    st.ages.push({ node: $("updated"), fn: function () {
      var since = st.lastOk === null ? 0 : Math.max(0, (env.now() - st.lastOk) / 1000);
      return "checked " + formatAge(since) + " ago";
    } });
    renderNeeds(snap);
    renderCounts(snap);
    renderActive(snap);
    renderHealth(snap);
    renderEvents(snap);
    updateAges();
  }

  /* ---- connection chrome: dot, reason, banner ---- */

  function isStale() {
    if (st.conn !== "ok" && st.conn !== "none") return false;
    var ref = Math.max(st.lastOk === null ? st.startedAt : st.lastOk, st.visibleSince || 0);
    return env.now() - ref > STALE_S * 1000;
  }

  function connState() { return st.conn === "ok" && isStale() ? "stale" : st.conn; }

  function chrome() {
    var cs = st.conn === "none" && isStale() ? "stale" : connState();
    var a = assess(st.snap, cs);
    var dot = $("dot");
    if (st.shown.dot !== a.level) {
      ["green", "amber", "red", "grey"].forEach(function (c) { dot.classList.remove(c); });
      dot.classList.add(a.level);
      st.shown.dot = a.level;
    }
    setText("reason", a.reason);
    var msg = "";
    if (cs === "denied") msg = st.code === 403 ? MSG_403 : MSG_401;
    else if (cs === "error") msg = MSG_ERR;
    else if (cs === "stale") {
      var since = st.lastOk === null ? st.startedAt : st.lastOk;
      msg = "Last update " + Math.max(0, Math.floor((env.now() - since) / 1000)) + " s ago.";
    }
    setText("banner", msg);
    $("banner").classList.toggle("hidden", msg === "");
    $("main").classList.toggle("greyed", cs !== "ok" && cs !== "none" && !!st.snap);
  }

  /* ---- timers ---- */

  function clearPoll() {
    if (st.pollTimer !== null) { env.clearTimeout(st.pollTimer); st.pollTimer = null; }
  }

  function clearTick() {
    if (st.tickTimer !== null) { env.clearTimeout(st.tickTimer); st.tickTimer = null; }
  }

  function armPoll() {
    clearPoll();
    if (hidden() || lv.mode === "live") return;
    st.pollTimer = env.setTimeout(function () { st.pollTimer = null; poll(); }, POLL_MS);
  }

  function armTick() {
    clearTick();
    if (hidden()) return;
    st.tickTimer = env.setTimeout(function () {
      st.tickTimer = null;
      if (hidden()) return;
      updateAges();
      chrome();
      armTick();
    }, TICK_MS);
  }

  /* ---- requests ---- */

  /* SPEC-0428: an old `#token=` bookmark still works: the fragment is dropped, never used. */
  function dropFragmentToken() {
    var hash = String(env.location.hash || "");
    if (/(^#|&)token=/.test(hash)) {
      env.history.replaceState(null, "", env.location.pathname + env.location.search);
    }
    try { if (env.storage && env.storage.removeItem) env.storage.removeItem(OLD_TOKEN_KEY); } catch (e) { /* none */ }
  }

  function headers() {
    var h = {};
    if (st.rev) h["If-None-Match"] = '"' + st.rev + '"';
    return h;
  }

  function noteDate(res) {
    var local = env.now();
    var raw = res.headers && res.headers.get ? res.headers.get("Date") : null;
    var ms = raw ? Date.parse(raw) : NaN;
    st.anchor = { server: isFinite(ms) ? ms : local, local: local };
    st.lastOk = local;
    st.conn = "ok";
    st.code = null;
  }

  /* The one place a snapshot reaches the screen, from a poll or the stream: one whose rev is
     already shown is ignored, so the two paths cannot fight. */
  function applySnapshot(body) {
    var rev = typeof body.rev === "string" ? body.rev : null;
    if (rev !== null && rev === st.rev && st.snap) return false;
    st.snap = body;
    st.rev = rev;
    render();
    if (dv.cid) loadDrill();                 /* SPEC-0430: an open drilldown follows the queue */
    return true;
  }

  async function request() {
    var res;
    try {
      res = await env.fetch("/api/snapshot", { cache: "no-store", credentials: "same-origin", headers: headers() });
    } catch (e) {
      st.conn = "error";
      return;
    }
    if (res.status === 304) {
      noteDate(res);
    } else if (res.status === 200) {
      var body = null;
      try { body = await res.json(); } catch (e) { body = null; }
      if (!body || typeof body !== "object" || Array.isArray(body)) { st.conn = "error"; return; }
      noteDate(res);
      applySnapshot(body);
    } else if (res.status === 401 || res.status === 403) {
      st.conn = "denied";
      st.code = res.status;
    } else {
      st.conn = "error";
    }
  }

  function poll() {
    clearPoll();
    if (hidden()) return Promise.resolve();
    if (st.inflight) return st.inflight;
    var p = request().catch(function () { st.conn = "error"; }).then(function () {
      st.inflight = null;
      chrome();
      armPoll();
    });
    st.inflight = p;
    return p;
  }

  /* ---- the live stream (SPEC-0402) ---- */

  function liveLabel() {
    return lv.mode === "live" ? "live" : lv.mode === "reconnecting" ? "reconnecting" : "polling";
  }

  function indicator() {
    var n = $("live");
    if (!n) return;
    var label = liveLabel();
    setText("live", label);
    ["live", "polling", "reconnecting"].forEach(function (c) { n.classList.toggle("is-" + c, c === label); });
  }

  function streamUrl() {
    return "/api/stream" + (st.rev ? "?rev=" + encodeURIComponent(st.rev) : "");
  }

  function streamHeaders() { return {}; }

  function clearLive() {
    lv.gen += 1;
    if (lv.ctrl) { try { lv.ctrl.abort(); } catch (e) { /* already closed */ } lv.ctrl = null; }
    if (lv.retryTimer !== null) { env.clearTimeout(lv.retryTimer); lv.retryTimer = null; }
    if (lv.dogTimer !== null) { env.clearTimeout(lv.dogTimer); lv.dogTimer = null; }
  }

  /* Any bytes from the stream (an event or a heartbeat) are a successful check, like a 304. */
  function sawData(gen) {
    if (lv.dogTimer !== null) env.clearTimeout(lv.dogTimer);
    lv.dogTimer = env.setTimeout(function () { lv.dogTimer = null; streamFailed(gen); }, WATCHDOG_MS);
    st.lastOk = env.now();
    st.conn = "ok";
    st.code = null;
  }

  function onStreamSnapshot(raw) {
    var body = null;
    try { body = JSON.parse(raw); } catch (e) { body = null; }
    if (!body || typeof body !== "object" || Array.isArray(body)) return;
    var gen = typeof body.generated_at === "string" ? Date.parse(body.generated_at) : NaN;
    /* no Date header on a stream: keep the server-clock anchor moving, never behind the build */
    st.anchor = { server: isFinite(gen) ? Math.max(gen, serverNow()) : serverNow(), local: env.now() };
    applySnapshot(body);
  }

  function streamFailed(gen) {
    if (gen !== lv.gen) return;
    var wasLive = lv.mode === "live";
    clearLive();
    lv.mode = "polling";
    lv.failed = true;
    indicator();
    if (hidden()) return;
    if (wasLive) poll();                     /* polling takes over at once and re-arms; a failed
                                                retry changes nothing, the poll chain never stopped */
    lv.retryTimer = env.setTimeout(function () {
      lv.retryTimer = null;
      openStream();
    }, lv.backoff);
    lv.backoff = Math.min(BACKOFF_MAX_MS, lv.backoff * 2);
  }

  async function openStream() {
    if (!env.live || hidden()) return;
    clearLive();
    var gen = lv.gen;
    lv.mode = lv.failed ? "reconnecting" : "polling";
    indicator();
    var ctrl = env.abort();
    lv.ctrl = ctrl;
    var res;
    try {
      res = await env.fetch(streamUrl(), { cache: "no-store", credentials: "same-origin",
                                           headers: streamHeaders(), signal: ctrl.signal });
    } catch (e) {
      return streamFailed(gen);
    }
    if (gen !== lv.gen) return;
    if (res.status !== 200 || !res.body || !res.body.getReader) return streamFailed(gen);
    lv.mode = "live";
    lv.backoff = BACKOFF_MIN_MS;
    clearPoll();                             /* the stream carries updates now */
    sawData(gen);
    chrome();
    indicator();
    var dec = new TextDecoder();
    var parser = createSseParser({
      event: function (ev) { if (ev.type === "snapshot") onStreamSnapshot(ev.data); }
    });
    var reader = res.body.getReader();
    try {
      for (;;) {
        var r = await reader.read();
        if (gen !== lv.gen) return;
        if (r.done) break;
        sawData(gen);
        parser.push(dec.decode(r.value, { stream: true }));
      }
    } catch (e) { /* a dropped connection is a failure like any other */ }
    streamFailed(gen);
  }

  /* ---- the campaign drilldown (SPEC-0430) ---- */

  var dv = { cid: null, doc: null, gen: 0, loading: null,
             tail: { ctrl: null, run: null, lines: [], ended: false, gen: 0, retry: null, note: "" } };

  function routeCid() {
    var m = /^#\/c\/([^&]+)$/.exec(String(env.location.hash || ""));
    if (!m) return null;
    try { return decodeURIComponent(m[1]); } catch (e) { return null; }
  }

  function showDrill(on) {
    var main = $("main"), drill = $("drill");
    if (main) main.classList.toggle("hidden", on);
    if (drill) drill.classList.toggle("hidden", !on);
  }

  function route() {
    var cid = routeCid();
    if (cid === dv.cid) return;
    closeTail();
    dv.cid = cid;
    dv.doc = null;
    dv.gen += 1;
    showDrill(!!cid);
    if (cid) {
      put("drill-title", [el("span", "", cid)]);
      ["drill-state", "drill-run", "drill-tail", "drill-timeline", "drill-runs"].forEach(function (id) { if ($(id)) put(id, []); });
      loadDrill();
    }
  }

  async function loadDrill() {
    if (!dv.cid || hidden()) return;
    if (dv.loading) return dv.loading;
    var gen = dv.gen, cid = dv.cid;
    dv.loading = (async function () {
      var res, body = null;
      try {
        res = await env.fetch("/api/campaigns/" + encodeURIComponent(cid), { cache: "no-store", credentials: "same-origin" });
        try { body = await res.json(); } catch (e) { body = null; }
      } catch (e) { res = { status: 0 }; }
      if (gen !== dv.gen) return;
      if (res.status !== 200 || !body) {
        put("drill-state", [el("p", "msg err", res.status === 404 ? "No such campaign." : "Can't load the campaign (" + res.status + ").")]);
        return;
      }
      dv.doc = body;
      renderDrill();
      var run = body.current_run;
      if (run && (run !== dv.tail.run || (!dv.tail.ctrl && !dv.tail.ended && dv.tail.retry === null))) openTail(run);
    })().finally(function () { dv.loading = null; });
    return dv.loading;
  }

  function section(v, fn) {
    if (isErr(v)) return [el("p", "muted", v.error)];
    return fn(v);
  }

  function renderDrill() {
    var d = dv.doc;
    var s0 = d.state;
    put("drill-title", [el("span", "", (isErr(s0) || !s0 ? d.campaign : (s0.label || d.campaign)) + " · " + d.campaign)]);
    put("drill-state", section(s0, function (s1) {
      var n = [row("state", String(s1.state) + (s1.loop ? " · loop " + s1.loop : "")), costNode(s1)];
      if (s1.title) n.unshift(el("p", "", s1.title));
      if (s1.park) n.push(row("park", s1.park.reason + (s1.park.detail ? ": " + s1.park.detail : "")));
      return n;
    }));
    put("drill-run", [el("p", "muted", d.current_run ? d.current_run + (d.running ? " · running" : " · ended") : "no runs yet")]);
    put("drill-timeline", section(d.timeline, function (t) {
      if (!t.length) return [el("p", "muted", "no events")];
      return t.slice().reverse().map(function (ev) { return el("div", "tl", ev.text); });
    }));
    put("drill-runs", section(d.runs, function (rs) {
      if (!rs.length) return [el("p", "muted", "no runs yet")];
      return rs.map(function (r) {
        return row(String(r.run), [r.persona, r.status, r.verdict_status, r.cost_usd !== null && r.cost_usd !== undefined ? formatUsd(r.cost_usd) : null].filter(Boolean).join(" · "));
      });
    }));
  }

  function renderTail() {
    var t = dv.tail;
    var nodes = t.lines.map(function (l) { return el("div", "tl", l); });
    if (t.note) nodes.push(el("p", "muted", t.note));
    put("drill-tail", nodes);
  }

  function closeTail() {
    var t = dv.tail;
    t.gen += 1;
    if (t.ctrl) { try { t.ctrl.abort(); } catch (e) { /* closed */ } t.ctrl = null; }
    if (t.retry !== null) { env.clearTimeout(t.retry); t.retry = null; }
  }

  function pushLines(lines) {
    var t = dv.tail;
    lines.forEach(function (l) { t.lines.push(String(l)); });
    if (t.lines.length > TAIL_MAX_LINES) t.lines = t.lines.slice(t.lines.length - TAIL_MAX_LINES);
  }

  async function openTail(run) {
    closeTail();
    var t = dv.tail;
    if (t.run !== run) { t.lines = []; t.ended = false; }
    t.run = run;
    t.note = "connecting…";
    renderTail();
    if (!env.abort || hidden()) { t.note = ""; renderTail(); return; }
    var gen = t.gen, cid = dv.cid;
    var ctrl = env.abort();
    t.ctrl = ctrl;
    var res;
    try {
      res = await env.fetch("/api/campaigns/" + encodeURIComponent(cid) + "/tail?run=" + encodeURIComponent(run),
                            { cache: "no-store", credentials: "same-origin", signal: ctrl.signal });
    } catch (e) { res = null; }
    if (gen !== t.gen) return;
    if (!res || res.status !== 200 || !res.body || !res.body.getReader) {
      t.ctrl = null;
      t.note = "tail unavailable" + (res ? " (" + res.status + ")" : "") + "; retrying";
      renderTail();
      t.retry = env.setTimeout(function () { t.retry = null; if (dv.cid === cid && !hidden()) openTail(run); }, 5000);
      return;
    }
    t.lines = [];
    t.note = "";
    var dec = new TextDecoder();
    var parser = createSseParser({
      event: function (ev) {
        var body = null;
        try { body = JSON.parse(ev.data); } catch (e) { return; }
        if (ev.type === "start") { if (body.header) pushLines([body.header]); }
        else if (ev.type === "lines") pushLines(body.lines || []);
        else if (ev.type === "end") { t.ended = true; t.note = "■ run ended (" + (body.status || "?") + ")"; }
      }
    });
    var reader = res.body.getReader();
    try {
      for (;;) {
        var r = await reader.read();
        if (gen !== t.gen) return;
        if (r.done) break;
        parser.push(dec.decode(r.value, { stream: true }));
        renderTail();
      }
    } catch (e) { /* dropped */ }
    if (gen !== t.gen) return;
    t.ctrl = null;
    if (!t.ended) {
      t.note = "tail interrupted; reconnecting";
      t.retry = env.setTimeout(function () { t.retry = null; if (dv.cid === cid && !hidden()) openTail(run); }, 3000);
    }
    renderTail();
  }

  function onVisibility() {
    if (hidden()) {
      clearPoll();
      clearTick();
      clearLive();                           /* a hidden tab holds no client slot */
      closeTail();
      lv.mode = "off";
      return;
    }
    if (dv.cid) { dv.tail.run = dv.tail.ended ? dv.tail.run : null; loadDrill(); }
    st.visibleSince = env.now();
    clearPoll();
    var p = poll();
    armTick();
    if (env.live) {
      lv.backoff = BACKOFF_MIN_MS;
      lv.failed = false;
      var gen = lv.gen;                      /* a hide/show in between opens its own stream */
      p.then(function () { if (gen === lv.gen && !hidden()) openStream(); });
    }
  }

  function start() {
    dropFragmentToken();
    st.startedAt = st.visibleSince = env.now();
    doc.addEventListener("visibilitychange", onVisibility);
    if (env.listen) env.listen("hashchange", route);
    route();
    chrome();
    poll();
    armTick();
    if (env.live) openStream();
  }

  return { start: start, poll: poll, idle: function () { return st.inflight || Promise.resolve(); },
           liveMode: function () { return lv.mode; }, rev: function () { return st.rev; },
           route: route, drill: function () { return dv; } };
}

/* `?selftest=1` (SPEC-0402 / SPEC-0400 mobile check): layout and liveness facts written into
   <pre id="selftest"> as JSON, for a headless browser to read. Browser only. */
function selftest() {
  var d = window.document;
  var bad = [];
  Array.prototype.forEach.call(d.querySelectorAll("button, a, summary, input, textarea, select"), function (n) {
    var r = (n.closest("label") || n).getBoundingClientRect();   /* a label is the tap target */
    if (r.width === 0 && r.height === 0) return;          /* not displayed */
    if (r.height < 44) bad.push((n.tagName + "." + n.className + " " + (n.textContent || "").slice(0, 30)).trim() + " h=" + Math.round(r.height));
  });
  var out = {
    overflow: d.documentElement.scrollWidth > window.innerWidth,
    small_controls: bad,
    live: window.nwApp ? window.nwApp.liveMode() : null,
    rev: window.nwApp ? window.nwApp.rev() : null,
    inbox_buttons: d.querySelectorAll(".esc button").length,
    width: window.innerWidth
  };
  out.ok = !out.overflow && !bad.length && out.live === "live";
  var pre = d.getElementById("selftest");
  pre.textContent = JSON.stringify(out);
  pre.classList.remove("hidden");
}

if (typeof window !== "undefined") {
  var app = createApp({
    fetch: function (u, o) { return window.fetch(u, o); },
    document: window.document,
    storage: window.localStorage,
    location: window.location,
    history: window.history,
    now: function () { return Date.now(); },
    setTimeout: function (f, ms) { return window.setTimeout(f, ms); },
    clearTimeout: function (id) { window.clearTimeout(id); },
    visibility: function () { return window.document.visibilityState; },
    live: typeof window.AbortController === "function" && typeof window.TextDecoder === "function",
    abort: function () { return new window.AbortController(); },
    listen: function (type, fn) { window.addEventListener(type, fn); },
    csrf: function () {
      var m = window.document.querySelector('meta[name="nw-csrf"]');
      return m ? m.getAttribute("content") : null;
    }
  });
  window.nwApp = app;
  app.start();
  if (/(^\?|&)selftest=1(&|$)/.test(window.location.search)) window.setTimeout(selftest, 4000);
} else {
  module.exports = { createApp: createApp, createSseParser: createSseParser, healthLevel: healthLevel, displayAge: displayAge, formatAge: formatAge, formatUsd: formatUsd };
}
