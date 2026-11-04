"""SPEC-0401: a seeded data dir for the dashboard tests (snapshot, cache, CLI, read-only; the
server and page specs reuse it). One active campaign with cost and cap, a PARKED
`human_decision` campaign with an open ESC-0001, a `main_dirty` park, a delivered (merged)
campaign, a lease file, and an `events.jsonl` larger than 256 KiB whose tail holds the recent
events. Everything is written directly (no notifier, no mail, no follower)."""
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
        "resume_to": "IMPLEMENTING", "escalated_from": "loop_exhausted"})
    _campaign(cfg, DIRTY, "PARKED", now, park={
        "reason": "main_dirty", "detail": "main checkout has uncommitted tracked changes",
        "since": _iso(now, 3), "resume_to": "CLOSING"})
    _campaign(cfg, MERGED, "MERGED", now, updated_min=10, created_min=60,
              close={"mode": "merge", "merged_sha": "abcdef1234567", "pr_url": None})
    ESC._create(cfg, {"by": "conductor", "campaign": DECISION, "spec_id": DECISION[:9],
                      "project": "Meta-Tools", "question": "ship it?\nmore context",
                      "options": [{"n": 1, "label": "yes"}, {"n": 2, "label": "no"}],
                      "recommendation": 1, "facts": [], "park": None, "kind": None})
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
