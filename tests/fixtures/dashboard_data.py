"""SPEC-0401: a seeded data dir for the dashboard tests (snapshot, cache, CLI, read-only; the
server and page specs reuse it). One active campaign with cost and cap, a PARKED
`human_decision` campaign with an open ESC-0001 (plus an answered ESC-0002 and a withdrawn
ESC-0003, which the inbox must leave out), a `main_dirty` park, a `main_not_on_base` park, a
delivered (merged) campaign, a lease file, and an `events.jsonl` larger than 256 KiB whose tail
holds the recent events. Everything is written directly (no notifier, no mail, no follower)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import yaml

from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import lease as L
from nightwright.agentic import store as S

ACTIVE = "SPEC-0901-20261103-aaaa"
DECISION = "SPEC-0902-20261103-bbbb"
DIRTY = "SPEC-0903-20261103-cccc"
MERGED = "SPEC-0904-20261103-dddd"
OFF_BASE = "SPEC-0905-20261103-ffff"
AGENTIC = {"lease_ttl_s": 120}
# event types in the tail, in file order (the old `merged` before the window must not show)
TAIL_TYPES = ["parked", "note", "escalation", "cost_warning", "merged", "admitted",
              "run_failed", "parked"]


def _iso(now, minutes_ago=0):
    return (now - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def make_cfg(data_dir: Path, config_dir: Path) -> dict:
    return {"data_dir": str(data_dir), "config_dir": str(config_dir),
            "agentic": dict(AGENTIC), "outbox_targets": {}, "specs_dir": str(data_dir / "specs")}


def _campaign(cfg, cid, state, now, *, updated_min=1, created_min=30, **extra):
    st = {"id": cid, "kind": "implement", "spec_id": cid[:9], "project": "Meta-Tools",
          "profile": "balanced", "state": state, "created_at": _iso(now, created_min),
          "repo": {"base": "main", "branch": f"nw/{cid}"}, "park": None,
          "counters": {"loops": 1, "runs": 1, "parks": 0},
          "cost": {"total_usd": 1.25, "cap_usd": 10.0}, **extra}
    S.create_campaign(cfg, st)
    p = S.campaign_dir(cfg, cid) / "state.json"          # control updated_at (write_state stamps it)
    raw = json.loads(p.read_text())
    raw["updated_at"] = _iso(now, updated_min)
    p.write_text(json.dumps(raw))
    return cid


REVIEWER = "conformance_reviewer"
UNMET_AC = "AC 4 output is sorted"
LONG_DETAIL = "d" * 400


def review(unmet=(UNMET_AC,)) -> dict:
    """A stored `last_review` (SPEC-0403): one reviewer's `fix` verdict with two blocking
    (`major`) findings, the second with a 400-character detail, and `unmet` ACs."""
    return {"verdicts": {REVIEWER: {
        "verdict": "fix", "score": 40, "fix_brief": "x", "loop": 0, "run": "002-r",
        "findings": [{"severity": "major", "title": "Wrong order", "files": ["a.py"]},
                     {"severity": "major", "title": "Missing check", "files": ["b.py"],
                      "detail": LONG_DETAIL}],
        "ac_assessment": [*[{"ac": a, "met": False} for a in unmet],
                          {"ac": "AC 5 other", "met": True}]}}}


def _event(i, type_, now, campaign=None, **data):
    return {"schema": E.SCHEMA, "id": i, "ts": _iso(now), "type": type_, "campaign": campaign,
            "spec_id": campaign[:9] if campaign else None, "project": "Meta-Tools",
            "by": "queue", "data": data}


def seed(tmp_path: Path, now=None) -> dict:
    """Seed `tmp_path/data` (+ `tmp_path/config/config.yaml` for a subprocess `nw`). Returns
    `cfg` with `NW_DATA_DIR` / `NW_CONFIG_DIR` env for a subprocess under `env`."""
    now = now or dt.datetime.now().astimezone()
    data, conf = tmp_path / "data", tmp_path / "config"
    data.mkdir(parents=True, exist_ok=True)
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "config.yaml").write_text(yaml.safe_dump({"agentic": AGENTIC}))
    cfg = make_cfg(data, conf)
    _campaign(cfg, ACTIVE, "IMPLEMENTING", now)
    _campaign(cfg, DECISION, "PARKED", now, park={
        "reason": "human_decision", "detail": "ESC-0001: ship it?", "since": _iso(now, 5),
        "resume_to": "IMPLEMENTING", "escalated_from": "loop_exhausted"},
        last_review=review())                          # SPEC-0403: the inbox reads it
    _campaign(cfg, DIRTY, "PARKED", now, park={
        "reason": "main_dirty", "detail": "main checkout has uncommitted tracked changes",
        "since": _iso(now, 3), "resume_to": "CLOSING"})
    _campaign(cfg, OFF_BASE, "PARKED", now, park={
        "reason": "main_not_on_base", "detail": "main checkout is on branch feat-x",
        "since": _iso(now, 2), "resume_to": "CLOSING"})
    _campaign(cfg, MERGED, "MERGED", now, updated_min=10, created_min=60,
              close={"mode": "merge", "merged_sha": "abcdef1234567", "pr_url": None})
    ESC._create(cfg, {"by": "conductor", "campaign": DECISION, "spec_id": DECISION[:9],
                      "project": "Meta-Tools", "question": "ship it?\nmore context",
                      "options": [{"n": 1, "label": "yes"}, {"n": 2, "label": "no"}],
                      "recommendation": 1, "facts": [], "park": None, "kind": None})
    for status, extra in (("answered", {"answer": 1, "answered_by": "human",
                                        "answered_at": _iso(now, 1)}),
                          ("withdrawn", {"withdrawn": {"by": "conductor", "reason": "moot"}})):
        rec = ESC._create(cfg, {"by": "conductor", "campaign": None, "spec_id": None,
                                "project": "Meta-Tools", "question": f"{status} one?",
                                "options": [], "recommendation": None, "facts": [],
                                "park": None, "kind": None})
        ESC._mutate(cfg, rec["id"], lambda r, s=status, x=extra: r.update(status=s, **x))
    L.lease_path(cfg).write_text(json.dumps({"pid": 999999, "host": "nowhere",
                                             "heartbeat_at": _iso(now, 10)}))
    lines, i = [], 0
    i += 1
    lines.append(_event(i, "merged", now, MERGED, merged_sha="old0000"))   # outside the window
    filler = "x" * 400
    size = 0
    while size < 300 * 1024:
        i += 1
        ev = _event(i, "note", now, text=filler)
        size += len(json.dumps(ev)) + 1
        lines.append(ev)
    reasons = {"parked": {"reason": "main_dirty", "detail": "d", "resume_to": "CLOSING"}}
    for t in TAIL_TYPES:
        i += 1
        cid = {"merged": MERGED, "escalation": DECISION}.get(t, DIRTY if t == "parked" else ACTIVE)
        lines.append(_event(i, t, now, cid, **reasons.get(t, {})))
    with open(E.events_path(cfg), "w", encoding="utf-8") as f:
        for ev in lines:
            f.write(json.dumps(ev) + "\n")
    env = {"NW_DATA_DIR": str(data), "NW_CONFIG_DIR": str(conf)}
    return {"cfg": cfg, "env": env, "now": now, "data": data}


# ---- SPEC-0403: the decisions inbox fixture ----------------------------------------------

I_DEC = "SPEC-0911-20261103-aaaa"        # PARKED human_decision, ESC-0001 with a review
I_PEND = "SPEC-0912-20261103-bbbb"       # park requested only, ESC-0002
I_RUN = "SPEC-0913-20261103-cccc"        # IMPLEMENTING, ESC-0003
I_BAD = "SPEC-0915-20261103-eeee"        # corrupt state.json, ESC-0005
I_DIRTY = "SPEC-0916-20261103-ffff"      # main_dirty, repo.root set
I_OFF = "SPEC-0917-20261103-a1a1"        # main_not_on_base, no repo.root
I_ANS = "SPEC-0918-20261103-b2b2"        # human_decision, latest escalation answered
I_WD = "SPEC-0919-20261103-c3c3"         # human_decision, latest escalation withdrawn
DIRTY_ROOT = "/srv/repos/meta-tools"
DEC_QUESTION = "Waive the review?\nThe loop is exhausted.\nTwo findings remain."


def _hd_park(now, esc_id, minutes=5):
    return {"reason": "human_decision", "detail": f"{esc_id}: q", "since": _iso(now, minutes),
            "resume_to": "IMPLEMENTING", "escalated_from": "loop_exhausted"}


def _esc(cfg, now, minutes, cid, question, status="open", **fields):
    rec = ESC._create(cfg, {"by": "conductor", "campaign": cid,
                            "spec_id": cid[:9] if cid else None, "project": "Meta-Tools",
                            "question": question, "options": fields.pop("options", []),
                            "recommendation": fields.pop("recommendation", None),
                            "facts": fields.pop("facts", []), "park": None,
                            "kind": fields.pop("kind", None)})
    extra = {"created": _iso(now, minutes), "status": status, **fields}
    ESC._mutate(cfg, rec["id"], lambda r: r.update(extra))
    return rec["id"]


def seed_inbox(tmp_path: Path, now=None) -> dict:
    """A data dir with one of each inbox case (Test plan of SPEC-0403). `created` times give
    the inbox order ESC-0004, ESC-0001, ESC-0002, ESC-0003 (tie with ESC-0002, broken by id),
    ESC-0005, ESC-0006."""
    now = now or dt.datetime.now().astimezone()
    data, conf = tmp_path / "data", tmp_path / "config"
    data.mkdir(parents=True, exist_ok=True)
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "config.yaml").write_text(yaml.safe_dump({"agentic": AGENTIC}))
    cfg = make_cfg(data, conf)
    _campaign(cfg, I_DEC, "PARKED", now, park=_hd_park(now, "ESC-0001"), last_review=review())
    _campaign(cfg, I_PEND, "IMPLEMENTING", now, park_requested={
        "reason": "human_decision", "detail": "ESC-0002: q", "at": _iso(now, 4),
        "by": "conductor"})
    _campaign(cfg, I_RUN, "IMPLEMENTING", now)
    _campaign(cfg, I_BAD, "PARKED", now, park=_hd_park(now, "ESC-0005"))
    _campaign(cfg, I_DIRTY, "PARKED", now, repo={"base": "main", "root": DIRTY_ROOT}, park={
        "reason": "main_dirty", "detail": "main checkout has uncommitted tracked changes",
        "since": _iso(now, 3), "resume_to": "CLOSING"})
    _campaign(cfg, I_OFF, "PARKED", now, park={
        "reason": "main_not_on_base", "detail": "main checkout is on branch feat-x",
        "since": _iso(now, 2), "resume_to": "CLOSING"})
    _campaign(cfg, I_ANS, "PARKED", now, park=_hd_park(now, "ESC-0007"))
    _campaign(cfg, I_WD, "PARKED", now, park=_hd_park(now, "ESC-0009"))
    _esc(cfg, now, 50, I_DEC, DEC_QUESTION,
         options=[{"n": 1, "label": "Waive and resume"}, {"n": 2, "label": "Abandon"}],
         recommendation=1, facts=["loops: 3", "cost: $4.10"])
    _esc(cfg, now, 40, I_PEND, "Pending one?", options=[{"n": 1, "label": "go"}])
    _esc(cfg, now, 40, I_RUN, "Running one?")
    _esc(cfg, now, 60, None, "Campaign-less one?", facts="one fact")
    _esc(cfg, now, 30, I_BAD, "Corrupt one?")
    _esc(cfg, now, 20, None, "Worker box lost 3 jobs; pause it?", kind="worker_lost",
         options=[{"n": 1, "label": "pause"}, {"n": 2, "label": "keep"}])
    _esc(cfg, now, 15, I_ANS, "Answered one?", status="answered",
         answer={"kind": "text", "text": "go"}, answered_by="human",
         answered_at=_iso(now, 10))
    _esc(cfg, now, 25, I_WD, "Old answered one?", status="answered",
         answer={"kind": "text", "text": "go"}, answered_by="human",
         answered_at=_iso(now, 24))
    _esc(cfg, now, 12, I_WD, "Withdrawn one?", status="withdrawn",
         withdrawn={"by": "conductor", "reason": "moot", "at": _iso(now, 11)})
    (S.campaign_dir(cfg, I_BAD) / "state.json").write_text("{not json")
    env = {"NW_DATA_DIR": str(data), "NW_CONFIG_DIR": str(conf)}
    return {"cfg": cfg, "env": env, "now": now, "data": data}
