"use strict";
/* SPEC-0413: the dashboard phone page. Polls GET /api/snapshot every 15 s with If-None-Match,
   pauses while the tab is hidden, and derives ages from the server's clock. (SPEC-0402) When
   env.live is set it reads GET /api/stream through fetch(), so the loopback bearer header rides
   along; polling is the fallback. Every snapshot value is rendered with textContent only.
   createApp(env) takes every browser object it touches,
   so the logic runs under node with a fake DOM (tests/js). */

var POLL_MS = 15000;
var TICK_MS = 1000;
var STALE_S = 45;
var MAX_EVENTS = 20;
var TOKEN_KEY = "nw-dashboard-token";
var WATCHDOG_MS = 45000;            /* SPEC-0402: a stream silent this long (3 heartbeats) is dropped */
var BACKOFF_MIN_MS = 2000;
var BACKOFF_MAX_MS = 30000;

var MSG_401 = "Not signed in. Open the dashboard link that carries the token.";
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
    snap: null, rev: null, conn: "none", code: null, token: null, generatedMs: NaN,
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

  function renderNeeds(snap) {
    var inbox = snap.inbox || {};
    var nodes = [];
    var count = 0;
    var esc = inbox.escalations;
    var parks = inbox.parks;
    if (isErr(esc)) nodes.push(muted("inbox.escalations", esc));
    else (esc || []).forEach(function (e) {
      var d = el("div", "need");
      d.appendChild(el("span", "id", e.id));
      d.appendChild(el("span", "who", labelOf(e)));
      d.appendChild(el("p", "", e.question));
      nodes.push(d);
      count++;
    });
    if (isErr(parks)) nodes.push(muted("inbox.parks", parks));
    else (parks || []).forEach(function (p) {
      var park = p.park || {};
      var d = el("div", "need");
      d.appendChild(el("span", "id", "park"));
      d.appendChild(el("span", "who", labelOf(p)));
      d.appendChild(el("p", "", park.reason + (park.detail ? ": " + park.detail : "")));
      nodes.push(d);
      count++;
    });
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
      c.appendChild(el("h3", "", title));
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

  function readStore(k) {
    try { return env.storage.getItem(k); } catch (e) { return null; }
  }

  function writeStore(k, v) {
    try { env.storage.setItem(k, v); } catch (e) { /* storage unavailable: no token kept */ }
  }

  function takeFragmentToken() {
    var hash = String(env.location.hash || "");
    if (hash.charAt(0) === "#") hash = hash.slice(1);
    var found = null;
    hash.split("&").forEach(function (kv) {
      var i = kv.indexOf("=");
      if (i > 0 && kv.slice(0, i) === "token") {
        try { found = decodeURIComponent(kv.slice(i + 1)); } catch (e) { found = null; }
      }
    });
    if (found) {
      writeStore(TOKEN_KEY, found);
      env.history.replaceState(null, "", env.location.pathname + env.location.search);
    }
    st.token = readStore(TOKEN_KEY) || null;
  }

  function headers() {
    var h = {};
    if (st.token) h.Authorization = "Bearer " + st.token;
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

  function streamHeaders() {
    var h = {};
    if (st.token) h.Authorization = "Bearer " + st.token;   /* never in the URL */
    return h;
  }

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

  function onVisibility() {
    if (hidden()) {
      clearPoll();
      clearTick();
      clearLive();                           /* a hidden tab holds no client slot */
      lv.mode = "off";
      return;
    }
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
    takeFragmentToken();
    st.startedAt = st.visibleSince = env.now();
    doc.addEventListener("visibilitychange", onVisibility);
    chrome();
    poll();
    armTick();
    if (env.live) openStream();
  }

  return { start: start, poll: poll, idle: function () { return st.inflight || Promise.resolve(); },
           liveMode: function () { return lv.mode; }, rev: function () { return st.rev; } };
}

if (typeof window !== "undefined") {
  createApp({
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
    abort: function () { return new window.AbortController(); }
  }).start();
} else {
  module.exports = { createApp: createApp, createSseParser: createSseParser, healthLevel: healthLevel, displayAge: displayAge, formatAge: formatAge, formatUsd: formatUsd };
}
