"use strict";
// SPEC-0413 test support: a minimal DOM for src/nightwright/agentic/dashboard/static/app.js.
// The markup setters throw, every created element is recorded, and replaceChildren calls are
// counted per element id so a test can tell a data re-render from an age tick.

class TextNode {
  constructor(text) { this.nodeType = 3; this.data = String(text); }
  get textContent() { return this.data; }
}

class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
  toString() { return [...this.set].join(" "); }
}

class Element {
  constructor(doc, tag) {
    this.nodeType = 1;
    this.doc = doc;
    this.tag = tag;
    this.id = null;
    this.children = [];
    this.attrs = {};
    this.classList = new ClassList();
    this.replaced = 0;
    this.listeners = {};
  }
  // SPEC-0429: the inbox controls listen for clicks / input; `fire` stands in for the browser
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type) { (this.listeners[type] || []).forEach((fn) => fn({ type, preventDefault() {} })); }
  click() { this.fire("click"); }
  // every element below this one (depth first), for tests to find controls
  all() { return this.children.filter((c) => c.nodeType === 1).flatMap((c) => [c, ...c.all()]); }
  find(pred) { return this.all().filter(pred); }
  appendChild(n) { this.children.push(n); return n; }
  replaceChildren(...nodes) { this.children = nodes; this.replaced += 1; }
  setAttribute(k, v) { this.attrs[k] = String(v); this.doc.attrWrites.push([this.tag, k, String(v)]); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  set textContent(v) {
    const s = String(v);
    this.children = s === "" ? [] : [new TextNode(s)];
  }
  get innerHTML() { throw new Error("innerHTML read"); }
  set innerHTML(v) { throw new Error("innerHTML write"); }
  get outerHTML() { throw new Error("outerHTML read"); }
  set outerHTML(v) { throw new Error("outerHTML write"); }
  insertAdjacentHTML() { throw new Error("insertAdjacentHTML"); }
  // a stable text dump of this subtree, for "the tree did not change" assertions
  dump() {
    const kids = this.children.map((c) => (c.nodeType === 3 ? JSON.stringify(c.data) : c.dump())).join(",");
    return `<${this.tag}.${this.classList}${JSON.stringify(this.attrs)}${this.value !== undefined ? "=" + this.value : ""}>[${kids}]`;
  }
}

class Document {
  constructor(ids) {
    this.created = [];
    this.attrWrites = [];
    this.listeners = {};
    this.byId = {};
    this.hidden = false;
    for (const id of ids) {
      const e = new Element(this, "div");
      e.id = id;
      this.byId[id] = e;
    }
  }
  createElement(tag) { const e = new Element(this, tag); this.created.push(e); return e; }
  createTextNode(t) { return new TextNode(t); }
  getElementById(id) { return this.byId[id] || null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  get visibilityState() { return this.hidden ? "hidden" : "visible"; }
  setHidden(h) {
    this.hidden = h;
    (this.listeners.visibilitychange || []).forEach((fn) => fn());
  }
  get innerHTML() { throw new Error("document innerHTML"); }
  write() { throw new Error("document.write"); }
}

// A manual clock plus setTimeout queue: nothing runs until the test advances time.
class Clock {
  constructor(t0) { this.t = t0; this.timers = new Map(); this.seq = 0; }
  now() { return this.t; }
  setTimeout(fn, ms) { const id = ++this.seq; this.timers.set(id, { at: this.t + ms, fn, ms }); return id; }
  clearTimeout(id) { this.timers.delete(id); }
  get armed() { return [...this.timers.values()]; }
  // advance `ms`, firing due timers in order; `settle` runs after each so requests can finish
  async advance(ms, settle) {
    const end = this.t + ms;
    for (;;) {
      let next = null;
      let nid = null;
      for (const [id, tm] of this.timers) {
        if (tm.at <= end && (next === null || tm.at < next.at)) { next = tm; nid = id; }
      }
      if (next === null) break;
      this.timers.delete(nid);
      this.t = Math.max(this.t, next.at);
      next.fn();
      if (settle) await settle();
    }
    this.t = end;
  }
}

module.exports = { Document, Element, Clock };
