"use strict";
// SPEC-0402 / SPEC-0429 / SPEC-0430 manual verification: drive the dashboard page in a real
// headless Chromium over the DevTools protocol (node's built-in WebSocket, no dependency).
// Not run by pytest (no host in the gate pool has Chromium, IDEA-920); run by hand:
//   node tests/js/browser_drive.js <chrome-headless-shell> <dashboard url> [ESC-id-to-answer] [cid]
// Prints one JSON line per step. Never point it at the real data dir's server when answering.
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const [, , CHROME, URL0, ANSWER_ID, CID] = process.argv;
if (!CHROME || !URL0) { console.error("usage: browser_drive.js <chrome> <url> [esc-id] [cid]"); process.exit(2); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const say = (o) => console.log(JSON.stringify(o));

async function launch() {
  const dir = fs.mkdtempSync(path.join(process.env.TMPDIR || os.tmpdir(), "nw-chrome-"));
  const p = spawn(CHROME, ["--headless", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0",
    `--user-data-dir=${dir}`, "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
  const ws = await new Promise((resolve, reject) => {
    let buf = "";
    p.stderr.on("data", (d) => {
      buf += d;
      const m = /DevTools listening on (ws:\/\/\S+)/.exec(buf);
      if (m) resolve(m[1]);
    });
    p.on("exit", (c) => reject(new Error("chrome exited " + c + ": " + buf)));
  });
  return { p, dir, port: new URL(ws).port };
}

async function connect(port) {
  const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = list.find((t) => t.type === "page");
  const sock = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => sock.addEventListener("open", r));
  let id = 0;
  const waiting = new Map();
  sock.addEventListener("message", (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && waiting.has(msg.id)) { waiting.get(msg.id)(msg); waiting.delete(msg.id); }
  });
  const send = (method, params = {}) => new Promise((resolve) => {
    id += 1;
    waiting.set(id, resolve);
    sock.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expr) => {
    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    if (r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return r.result.result.value;
  };
  return { sock, send, evaluate };
}

async function until(cdp, expr, ms = 20000) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await cdp.evaluate(expr);
    if (v) return v;
    if (Date.now() > end) throw new Error("timed out waiting for: " + expr);
    await sleep(200);
  }
}

async function selftest(cdp, w, h) {
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 3, mobile: true });
  await cdp.send("Page.navigate", { url: URL0.replace(/\/?$/, "/") + "?selftest=1" });
  const raw = await until(cdp, "document.getElementById('selftest') && document.getElementById('selftest').textContent", 30000);
  say({ step: `selftest ${w}x${h}`, result: JSON.parse(raw) });
  if (process.env.NW_SHOT_DIR) {                  // optional full-page screenshot for a human look
    const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
    fs.writeFileSync(path.join(process.env.NW_SHOT_DIR, `dashboard-${w}x${h}.png`), Buffer.from(shot.result.data, "base64"));
  }
}

(async () => {
  const b = await launch();
  try {
    const cdp = await connect(b.port);
    await cdp.send("Runtime.enable");
    await selftest(cdp, 390, 844);
    await selftest(cdp, 360, 740);
    if (ANSWER_ID) {
      const q = `document.querySelector('[data-esc="${ANSWER_ID}"]')`;
      await until(cdp, `!!${q} && ${q}.querySelectorAll('button.opt').length > 0`);
      const opts = await cdp.evaluate(`Array.from(${q}.querySelectorAll('button.opt')).map(b => b.textContent)`);
      await cdp.evaluate(`${q}.querySelector('button.opt').click()`);
      const conf = await until(cdp, `${q} && ${q}.querySelector('.confirm') && ${q}.querySelector('.confirm').textContent`);
      say({ step: "option clicked, confirm panel shown", options: opts, confirm: conf });
      await cdp.evaluate(`${q}.querySelector('.confirm button.primary').click()`);
      const flash = await until(cdp, "document.querySelector('#needs .msg.ok') && document.querySelector('#needs .msg.ok').textContent");
      say({ step: "confirmed", flash });
      const gone = await until(cdp, `!${q}`, 20000);
      say({ step: "escalation left the inbox after the snapshot refresh", gone });
    }
    if (CID) {
      await cdp.evaluate(`location.hash = '#/c/${CID}'`);
      const tl = await until(cdp, "document.getElementById('drill-timeline').children.length");
      const tail = await until(cdp, "document.getElementById('drill-tail').textContent.length > 0 && document.getElementById('drill-tail').textContent");
      const shown = await cdp.evaluate("!document.getElementById('drill').classList.contains('hidden') && document.getElementById('main').classList.contains('hidden')");
      say({ step: "drilldown", timeline_rows: tl, drill_shown: shown, tail: tail.slice(0, 400) });
      await cdp.evaluate("location.hash = ''");
      const back = await until(cdp, "!document.getElementById('main').classList.contains('hidden')");
      say({ step: "back to the main view", back });
    }
    cdp.sock.close();
  } finally {
    b.p.kill();
    fs.rmSync(b.dir, { recursive: true, force: true });
  }
})().catch((e) => { console.error(e); process.exit(1); });
