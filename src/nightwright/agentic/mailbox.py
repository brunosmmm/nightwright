"""File-based mailbox between the conductor and stewards (SPEC-0183).

Addresses: `conductor`, `human`, `steward:<Project>` (project names from the registry). Each
address has an append-only inbox `<data_dir>/agentic/mailbox/<addr>.jsonl` of `nw.message.v1`
records with a monotonic per-inbox `seq` assigned under a per-inbox flock. Followers stream new
messages with a per-reader cursor (same model as the event stream). A message to the conductor
is mirrored into `events.jsonl` as a `message` event, so the conductor's one Monitor on the
event stream wakes on mail too."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import codewatch as CW
from . import events as E
from . import store

SCHEMA = "nw.message.v1"
KINDS = ("question", "answer", "request", "info")
_STEWARD = "steward:"


def mailbox_dir(cfg) -> Path:
    return store.agentic_dir(cfg) / "mailbox"


def _inbox_path(cfg, addr) -> Path:
    return mailbox_dir(cfg) / f"{E._safe(addr)}.jsonl"


def _inbox_lock(cfg, addr) -> Path:
    return mailbox_dir(cfg) / f"{E._safe(addr)}.lock"


def known_addresses(cfg) -> list[str]:
    from ..rules import projects_payload
    try:
        names = [p["name"] for p in projects_payload(cfg)["projects"]]
    except Exception:
        names = []
    return ["conductor", "human"] + [f"{_STEWARD}{n}" for n in names]


def validate_address(cfg, addr) -> str:
    """Canonical address or ValueError. Steward project names match case-insensitively."""
    a = (addr or "").strip()
    if a in ("conductor", "human"):
        return a
    if a.lower().startswith(_STEWARD):
        name = a[len(_STEWARD):].strip()
        from ..rules import projects_payload
        known = [p["name"] for p in projects_payload(cfg)["projects"]]
        hit = {n.casefold(): n for n in known}.get(name.casefold())
        if not hit:
            raise ValueError(f"unknown steward project {name!r}; known: "
                             f"{', '.join(known) or '(none)'}")
        return f"{_STEWARD}{hit}"
    raise ValueError(f"unknown address {addr!r} (conductor | human | steward:<Project>)")


def _last_seq(path: Path) -> int:
    if not path.is_file():
        return 0
    last = 0
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("seq"), int):
                last = max(last, rec["seq"])
    return last


def send(cfg, *, to, frm, text, kind="info", campaign=None, re=None, needs_reply=None,
         campaign_from=None) -> dict:
    """Append one message to `to`'s inbox; mirror conductor-bound mail into the event stream.
    `campaign_from` (SPEC-0431) marks an inferred stamp: `"text"` or `"re"`, only with a campaign."""
    if campaign_from is not None and (campaign is None or campaign_from not in ("text", "re")):
        raise ValueError(f"campaign_from {campaign_from!r} needs a campaign and must be "
                         "'text' or 're'")
    to = validate_address(cfg, to)
    frm = validate_address(cfg, frm)
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r} ({' | '.join(KINDS)})")
    if not (text or "").strip():
        raise ValueError("empty message text")
    if needs_reply is None:
        needs_reply = kind in ("question", "request")
    path = _inbox_path(cfg, to)
    with E._locked(_inbox_lock(cfg, to)):
        seq = _last_seq(path) + 1
        rec = {"schema": SCHEMA, "id": f"{to}:{seq}", "seq": seq, "ts": store.now_iso(),
               "from": frm, "to": to, "kind": kind, "campaign": campaign, "re": re,
               "text": text, "needs_reply": bool(needs_reply)}
        if campaign_from:
            rec["campaign_from"] = campaign_from
        line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    if to == "conductor":
        st = {}
        if campaign:
            try:
                st = store.read_state(cfg, campaign)
            except ValueError:
                st = {}
        E.append(cfg, "message", campaign=campaign, spec_id=st.get("spec_id"),
                 project=st.get("project"), by=frm,
                 data={"msg_id": rec["id"], "from": frm, "kind": kind, "text": text, "re": re})
    return rec


def read_inbox(cfg, addr, after_seq=0) -> list[dict]:
    path = _inbox_path(cfg, addr)
    if not path.is_file():
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict) and isinstance(rec.get("seq"), int) \
                    and rec["seq"] > after_seq:
                out.append(rec)
    return out


def _all_messages(cfg) -> list[dict]:
    d = mailbox_dir(cfg)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.jsonl")):
        with open(p, encoding="utf-8") as f:
            for raw in f:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("id"):
                    out.append(rec)
    return out


# A reply seq is a short decimal. Longer than this, `int()` raises ValueError once the
# digit string passes the interpreter's limit, so the id is treated as missing.
_REPLY_SEQ_MAX_DIGITS = 18


def find_message(cfg, msg_id) -> dict | None:
    """The message with this id, or None. The id is `<addr>:<seq>` (the address may itself
    contain colons, as in `steward:P:5`), so only that inbox is streamed; a missing or
    malformed id is not an error."""
    if not isinstance(msg_id, str):
        return None
    addr, sep, seq_s = msg_id.rpartition(":")
    if not sep or not addr or not seq_s.isascii() or not seq_s.isdigit():
        return None
    if len(seq_s) > _REPLY_SEQ_MAX_DIGITS:
        return None
    path = _inbox_path(cfg, addr)
    if not path.is_file():
        return None
    try:
        want = int(seq_s)
    except ValueError:
        return None
    try:
        f = open(path, encoding="utf-8")
    except OSError:
        return None
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("id") == msg_id:
                return rec
            seq = rec.get("seq")
            if isinstance(seq, int) and seq > want:    # append-only; later lines are newer
                return None
    return None


def unanswered(cfg, addr) -> list[dict]:
    """Messages in `addr`'s inbox that need a reply and have none (no message `re` = id)."""
    addr = validate_address(cfg, addr)
    replied = {m.get("re") for m in _all_messages(cfg) if m.get("re")}
    return [m for m in read_inbox(cfg, addr) if m.get("needs_reply") and m["id"] not in replied]


def thread(cfg, msg_id) -> list[dict]:
    """The message and every reply to it (transitively): the root first, then replies in
    reply order (timestamps have one-second resolution, so they cannot order a fast thread)."""
    msgs = _all_messages(cfg)
    by_id = {m["id"]: m for m in msgs}
    if msg_id not in by_id:
        raise ValueError(f"no message {msg_id!r}")
    want, out = {msg_id}, [by_id[msg_id]]
    changed = True
    while changed:
        changed = False
        for m in msgs:
            if m.get("re") in want and m["id"] not in want:
                want.add(m["id"])
                out.append(m)
                changed = True
    return out


def summary_line(m: dict) -> str:
    camp = f" [{m['campaign']}]" if m.get("campaign") else ""
    re = f" re={m['re']}" if m.get("re") else ""
    return f"{m['id']} {m['kind']} from={m['from']}{camp}{re}: {m['text']}".replace("\n", " ")


def follow(cfg, addr, *, as_json=True, poll_s=2, out=None, max_events=None, stop=None,
           from_start=False, code_root=CW.AUTO, code_check_s=None) -> int:
    """Stream new messages for `addr` forever (one line each), holding the reader lock and
    advancing the cursor after each printed message. A new reader starts at the current end
    unless `from_start`. Returns the number printed. When nightwright's code changes (SPEC-0231) it
    prints one `code_changed` envelope and returns; `code_root=None` turns that watch off."""
    addr = validate_address(cfg, addr)
    out = out or sys.stdout
    reader = f"mailbox:{addr}"
    printed = 0
    with E.reader_lock(cfg, reader):
        try:    # SPEC-0361: remember the herdr pane this inbox's owner lives in (env only)
            from . import conductor_cost
            conductor_cost.record_herdr_pane(cfg, addr)
        except Exception:
            pass
        watcher = CW.Watcher(CW.code_checkout() if code_root is CW.AUTO else code_root,
                             E.settings(cfg)["code_check_s"] if code_check_s is None
                             else code_check_s)
        cpath = E.cursors_dir(cfg) / f"{E._safe(reader)}.json"
        if not cpath.is_file():
            E.cursor_set(cfg, reader, 0 if from_start else _last_seq(_inbox_path(cfg, addr)))
        while True:
            after = E.cursor_get(cfg, reader)
            for m in read_inbox(cfg, addr, after):
                out.write((json.dumps(m, ensure_ascii=False) if as_json else summary_line(m))
                          + "\n")
                out.flush()
                E.cursor_set(cfg, reader, m["seq"])
                printed += 1
                if max_events and printed >= max_events:
                    return printed
            change = watcher.poll(time.monotonic())
            if change:
                out.write(E.code_changed_line(reader, change, as_json=as_json))
                out.flush()
                return printed
            if stop is not None and stop():
                return printed
            time.sleep(poll_s)
