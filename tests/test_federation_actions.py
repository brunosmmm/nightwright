"""SPEC-0533: remote park and resume."""
from __future__ import annotations

import ast
import datetime as dt
import fcntl
import inspect
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import decisions as DEC
from nightwright.agentic import events as E
from nightwright.agentic import federation as F
from nightwright.agentic import federation_actions as FA
from nightwright.agentic import federation_host as FH
from nightwright.agentic import hosts as H
from nightwright.agentic import lease as L
from nightwright.agentic import mailbox as M
from nightwright.agentic import queue as Q
from nightwright.agentic import quiet as QT
from nightwright.agentic import store
from nightwright.cli import cli

from test_federation_channel import _cfg, _queue_import_violations
from test_skills import REPO_SKILLS_DIR, _conduct_row

pytestmark = pytest.mark.fast

UID = "b" * 32
ACT = {"verb": "park", "campaign": "C-1", "reason": "hold"}


def _lines(path):
    p = Path(path)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _mk(tmp_path, name="host"):
    cfg = _cfg(tmp_path, name)
    (Path(cfg["config_dir"]) / "mappings.yaml").write_text("{}\n")
    cfg["outbox_targets"] = {"Demo": {"repo_path": str(tmp_path)}}
    cfg["project_axis"] = "bucket"
    sec = Path(cfg["config_dir"]) / "secrets"
    sec.mkdir(parents=True, exist_ok=True)
    (sec / "federation.json").write_text("{}\n")
    return cfg


def _campaign(cfg, cid="C-1", state="QUEUED", project="Demo"):
    store.create_campaign(cfg, {"id": cid, "spec_id": "SPEC-0001", "project": project,
                                "state": state, "repo": {"base": "main"}})
    return cid


def _journal_path(cfg):
    return store.agentic_dir(cfg) / "federation" / "actions.jsonl"


def _pending(cfg, uid, action, to="steward:Demo"):
    p = _journal_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps({"msg_uid": uid, "state": "pending", "to": to, "ts": store.now_iso(),
                            "action": action}) + "\n")


def _journal(cfg):
    return _lines(_journal_path(cfg))


def _answers(cfg, uid, addr="conductor"):
    return [r for r in M.read_inbox(cfg, addr) if r.get("re") == uid]


def _uid(n):
    return f"{n:032x}"


class Spy:
    def __init__(self, monkeypatch, name, raises=None, on_call=None):
        self.calls = []
        real = getattr(Q, name)

        def wrapper(*a, **kw):
            self.calls.append((a, kw))
            if on_call:
                on_call()
            if raises:
                raise raises
            return real(*a, **kw)
        monkeypatch.setattr(Q, name, wrapper)


# ---- post_mail: the action line ---------------------------------------------------------------

def _msg(uid=UID, action=ACT, **kw):
    m = {"msg_uid": uid, "to": "steward:demo", "from": "conductor@hub", "kind": "request",
         "text": "park it", "campaign": "C-1", "re": None}
    if action is not None:
        m["action"] = action
    m.update(kw)
    return m


def test_action_line_fsyncs_before_dedupe(tmp_path, monkeypatch):
    host = _mk(tmp_path)
    _campaign(host)
    state = store.campaign_dir(host, "C-1")
    before = {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    synced = []
    real = os.fsync
    fail = []

    def fsync(fd):
        name = os.readlink(f"/proc/self/fd/{fd}")
        synced.append(Path(name).name)
        if fail and Path(name).name == "actions.jsonl":
            fail.pop()
            raise OSError("disk")
        return real(fd)
    monkeypatch.setattr(os, "fsync", fsync)
    assert FH.post_mail(host, [_msg()])[0]["result"] == "delivered"
    assert synced == [M._inbox_path(host, "steward:Demo").name, "actions.jsonl",
                      "received.jsonl"]
    (line,) = _journal(host)
    assert line["state"] == "pending" and line["action"] == ACT and line["to"] == "steward:Demo"
    assert before == {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}
    # an action-file fsync that raises: 500 commit_failed, no received line, one inbox line
    uid2 = _uid(2)
    fail.append(1)
    with pytest.raises(FH.CommitFailed):
        FH.post_mail(host, [_msg(uid2)])
    rpath = store.agentic_dir(host) / "federation" / "received.jsonl"
    assert uid2 not in [r["msg_uid"] for r in _lines(rpath)]
    assert len(M.read_inbox(host, "steward:Demo")) == 2
    assert FH.post_mail(host, [_msg(uid2)])[0]["result"] == "dup"
    assert len(M.read_inbox(host, "steward:Demo")) == 2
    assert [j["msg_uid"] for j in _journal(host)].count(uid2) == 1
    assert uid2 in [r["msg_uid"] for r in _lines(rpath)]


def test_dup_repairs_a_missing_action(tmp_path):
    host = _mk(tmp_path)
    fed = store.agentic_dir(host) / "federation"
    assert FH.post_mail(host, [_msg()])[0]["result"] == "delivered"
    apath, rpath = fed / "actions.jsonl", fed / "received.jsonl"
    apath.unlink()
    assert FH.post_mail(host, [_msg()])[0]["result"] == "dup"
    assert len(M.read_inbox(host, "steward:Demo")) == 1
    assert len(_lines(rpath)) == 1 and len(_lines(apath)) == 1
    rpath.unlink()
    assert FH.post_mail(host, [_msg()])[0]["result"] == "dup"
    assert len(_lines(rpath)) == 1 and len(_lines(apath)) == 1
    assert FH.post_mail(host, [_msg()])[0]["result"] == "dup"
    assert len(_lines(rpath)) == 1 and len(_lines(apath)) == 1
    # a non-dict action creates no journal
    other = _mk(tmp_path, "other")
    FH.post_mail(other, [_msg(action="park")])
    FH.post_mail(other, [_msg(action="park")])
    assert not (store.agentic_dir(other) / "federation" / "actions.jsonl").exists()
    tree = ast.parse(inspect.getsource(FH.post_mail))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"drain_actions", "plan_park", "apply_park", "plan_resume", "apply_resume"}


# ---- the tick ---------------------------------------------------------------------------------

def test_drain_runs_in_the_tick(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    order = []
    monkeypatch.setattr(FA, "drain_actions", lambda c: order.append("drain"))
    monkeypatch.setattr(F, "relay_tick", lambda c, **kw: order.append("tick"))
    monkeypatch.setattr(F, "relay_mail", lambda c, **kw: order.append("mail"))
    Q.tick(cfg)
    assert order == ["drain", "tick", "mail"]
    order.clear()

    def boom(c):
        order.append("drain")
        raise RuntimeError("x")
    monkeypatch.setattr(FA, "drain_actions", boom)
    report = Q.tick(cfg)
    assert report is not None and order == ["drain", "tick", "mail"]
    monkeypatch.undo()
    # no federation.json: the real drain is inert
    cfg2 = _cfg(tmp_path, "bare")
    spy = Spy(monkeypatch, "plan_park")
    FA.drain_actions(cfg2)
    assert not _journal_path(cfg2).exists() and not spy.calls


# ---- the verbs --------------------------------------------------------------------------------

def test_immediate_park_and_resume_by_human(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    park, apark = Spy(monkeypatch, "plan_park"), Spy(monkeypatch, "apply_park")
    resume, aresume = Spy(monkeypatch, "plan_resume"), Spy(monkeypatch, "apply_resume")
    _pending(cfg, UID, ACT)
    FA.drain_actions(cfg)
    st = store.read_state(cfg, "C-1")
    assert st["state"] == "PARKED" and st["park"]["reason"] == "human"
    assert st["park"]["detail"] == "hold"
    assert park.calls[0][1] == {"by": "human"} and apark.calls[0][0][1]["by"] == "human"
    assert apark.calls[0][0][1].get("decision") is None
    assert store.read_ledger(cfg, "C-1", types=["park"])[-1]["by"] == "human"
    (ans,) = _answers(cfg, UID)
    assert ans["kind"] == "answer" and ans["text"] == "ok" and ans["from"] == "steward:Demo"
    FA.drain_actions(cfg)
    assert len(apark.calls) == 1 and len(_answers(cfg, UID)) == 1
    assert not M.read_inbox(cfg, "human")
    # resume
    rid = _uid(7)
    _pending(cfg, rid, {"verb": "resume", "campaign": "C-1", "reason": ""})
    FA.drain_actions(cfg)
    assert store.read_state(cfg, "C-1")["state"] != "PARKED"
    assert resume.calls[0][1]["by"] == "human" and "decision" not in resume.calls[0][1]
    assert resume.calls[0][1]["note"] is None and aresume.calls
    assert store.read_ledger(cfg, "C-1", types=["resume"])[-1]["by"] == "human"
    assert DEC.read(cfg) == []
    (ans,) = _answers(cfg, rid)
    assert ans["text"] == "ok"
    FA.drain_actions(cfg)
    assert len(_answers(cfg, rid)) == 1 and len(aresume.calls) == 1


def test_deferred_park_answers_ok_once(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg, state="IMPLEMENTING")
    apark = Spy(monkeypatch, "apply_park")
    _pending(cfg, UID, ACT)
    lock = os.open(L.lock_path(cfg), os.O_RDWR | os.O_CREAT)
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        FA.drain_actions(cfg)
        last = _journal(cfg)[-1]
        assert last["state"] == "started" and last["wait"] == "PARKED"
        assert len(apark.calls) == 1 and apark.calls[0][0][1]["by"] == "human"
        st = store.read_state(cfg, "C-1")
        assert st["state"] == "IMPLEMENTING"
        assert st["park_requested"]["by"] == "human" and st["park_requested"]["reason"] == "human"
        assert st["park_requested"]["detail"] == "hold"
        assert not _answers(cfg, UID)
        FA.drain_actions(cfg)
        assert len(apark.calls) == 1 and not _answers(cfg, UID)
        # park_requested lost while still started/wait: ask again, still no answer
        st.pop("park_requested")
        store.write_state(cfg, "C-1", st)
        FA.drain_actions(cfg)
        assert len(apark.calls) == 2 and not _answers(cfg, UID)
        st = store.read_state(cfg, "C-1")
        st["state"] = "PARKED"
        store.write_state(cfg, "C-1", st)
        FA.drain_actions(cfg)
        assert [j.get("outcome") for j in _journal(cfg) if j["state"] == "done"] == ["ok"]
        assert [a["text"] for a in _answers(cfg, UID)] == ["ok"]
        FA.drain_actions(cfg)
        assert len(apark.calls) == 2 and len(_answers(cfg, UID)) == 1
    finally:
        os.close(lock)


def test_failed_apply_is_not_rerun(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    apark = Spy(monkeypatch, "apply_park", raises=RuntimeError("boom"))
    _pending(cfg, UID, ACT)
    FA.drain_actions(cfg)
    assert _journal(cfg)[-1]["outcome"] == "failed: RuntimeError: boom"
    assert [a["text"] for a in _answers(cfg, UID)] == ["failed: RuntimeError: boom"]
    FA.drain_actions(cfg)
    assert len(apark.calls) == 1 and len(_answers(cfg, UID)) == 1
    assert not M.read_inbox(cfg, "human")


def test_started_without_wait_is_unknown(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg, state="PARKED")
    plan, apark = Spy(monkeypatch, "plan_park"), Spy(monkeypatch, "apply_park")
    _pending(cfg, UID, ACT)
    with open(_journal_path(cfg), "a") as f:
        f.write(json.dumps({"msg_uid": UID, "state": "started", "ts": store.now_iso()}) + "\n")
    FA.drain_actions(cfg)
    assert not plan.calls and not apark.calls
    text = "unknown: host restarted during the verb"
    assert _journal(cfg)[-1]["outcome"] == text
    assert [a["text"] for a in _answers(cfg, UID)] == [text]
    FA.drain_actions(cfg)
    assert len(_answers(cfg, UID)) == 1 and not M.read_inbox(cfg, "human")


def test_refused_actions_copy_the_human(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    plan, resume = Spy(monkeypatch, "plan_park"), Spy(monkeypatch, "plan_resume")
    state_file = store.campaign_dir(cfg, "C-1") / "state.json"
    before = state_file.read_bytes()
    cases = [({"verb": "rebase", "campaign": "C-1", "reason": ""}, "steward:Demo", "verb"),
             ({"verb": "park", "campaign": "SPEC-1@box", "reason": ""}, "steward:Demo", "campaign"),
             ({"verb": "park", "campaign": "C-1", "reason": ""}, "steward:Other", "project"),
             ({"verb": "park", "campaign": "C-1", "reason": "", "note": "x"}, "steward:Demo",
              "fields"),
             ({"verb": "park", "campaign": "C-1", "reason": ""}, "human", "address")]
    uids = {}
    for i, (action, to, why) in enumerate(cases):
        uids[why] = _uid(100 + i)
        _pending(cfg, uids[why], action, to=to)
    FA.drain_actions(cfg)
    FA.drain_actions(cfg)
    for why, uid in uids.items():
        text = f"refused: {why}"
        assert [a["text"] for a in _answers(cfg, uid)] == [text], why
        assert [a["text"] for a in _answers(cfg, uid, "human")] == [text], why
    assert not plan.calls and not resume.calls
    assert before == state_file.read_bytes()


def test_answers_retry_until_re_is_present(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    _pending(cfg, UID, ACT)
    with open(_journal_path(cfg), "a") as f:
        f.write(json.dumps({"msg_uid": UID, "state": "done", "outcome": "ok",
                            "ts": store.now_iso()}) + "\n")
    real = M.send
    monkeypatch.setattr(M, "send", lambda *a, **kw: (_ for _ in ()).throw(OSError("down")))
    FA.drain_actions(cfg)
    assert not _answers(cfg, UID)
    monkeypatch.setattr(M, "send", real)
    FA.drain_actions(cfg)
    FA.drain_actions(cfg)
    assert len(_answers(cfg, UID)) == 1 and not M.read_inbox(cfg, "human")
    # a pre-seeded record makes drain skip send
    uid2 = _uid(2)
    _pending(cfg, uid2, ACT)
    with open(_journal_path(cfg), "a") as f:
        f.write(json.dumps({"msg_uid": uid2, "state": "done", "outcome": "ok",
                            "ts": store.now_iso()}) + "\n")
    real(cfg, to="conductor", frm="steward:Demo", kind="answer", re=uid2, text="ok")
    calls = []
    monkeypatch.setattr(M, "send", lambda *a, **kw: calls.append(kw))
    FA.drain_actions(cfg)
    assert not calls
    # a refused outcome whose human send raises is retried, and not copied twice
    uid3 = _uid(3)
    _pending(cfg, uid3, {"verb": "rebase", "campaign": "C-1", "reason": ""})
    monkeypatch.setattr(M, "send", lambda *a, **kw: (
        real(*a, **kw) if kw["to"] == "conductor" else (_ for _ in ()).throw(OSError("x"))))
    FA.drain_actions(cfg)
    assert len(_answers(cfg, uid3)) == 1 and not _answers(cfg, uid3, "human")
    monkeypatch.setattr(M, "send", real)
    FA.drain_actions(cfg)
    FA.drain_actions(cfg)
    assert len(_answers(cfg, uid3)) == 1 and len(_answers(cfg, uid3, "human")) == 1


# ---- the CLI ----------------------------------------------------------------------------------

def _send(args):
    return CliRunner().invoke(cli, ["mailbox", "send", "--from", "conductor", *args, "park"])


def test_mailbox_send_action_flag(tmp_path, monkeypatch):
    hub = _mk(tmp_path, "hub")
    H.update(hub, lambda rs: rs.append({"name": "box", "url": "http://127.0.0.1:1",
                                        "enabled": True, "status_cache": {"projects": ["Demo"]}}))
    monkeypatch.setenv("NW_DATA_DIR", hub["data_dir"])
    monkeypatch.setenv("NW_CONFIG_DIR", hub["config_dir"])
    out = F.outbox_path(hub, "box")
    r = _send(["--to", "steward:Demo@box", "--action", "park", "--campaign", "SPEC-1",
               "--reason", "hold"])
    assert r.exit_code == 0, r.output
    (rec,) = _lines(out)
    assert rec["action"] == {"verb": "park", "campaign": "SPEC-1", "reason": "hold"}
    assert list(rec["action"]) == ["verb", "campaign", "reason"] and rec["campaign"] == "SPEC-1"
    r = _send(["--to", "steward:Demo@box"])
    assert r.exit_code == 0, r.output
    assert "action" not in _lines(out)[-1]
    size = out.stat().st_size
    for args in (["--to", "steward:Demo@box", "--action", "resume"],
                 ["--to", "steward:Demo@box", "--reason", "hold"],
                 ["--to", "steward:Demo@box", "--action", "park", "--campaign", "C",
                  "--no-campaign"],
                 ["--to", "steward:Demo@box", "--action", "park", "--campaign", ""],
                 ["--to", "steward:Demo", "--action", "park", "--campaign", "SPEC-1"]):
        assert _send(args).exit_code != 0, args
    assert out.stat().st_size == size
    assert not M._inbox_path(hub, "steward:Demo").exists()
    bare = _mk(tmp_path, "bare")
    monkeypatch.setenv("NW_DATA_DIR", bare["data_dir"])
    monkeypatch.setenv("NW_CONFIG_DIR", bare["config_dir"])
    assert _send(["--to", "steward:Demo@box", "--action", "park", "--campaign", "C"]).exit_code != 0
    assert not F.outbox_path(bare, "box").exists()
    assert not M._inbox_path(bare, "steward:Demo").exists()


# ---- the 30 minute row ------------------------------------------------------------------------

def test_federation_ping_delivers_at_30_min(tmp_path):
    cfg = _cfg(tmp_path, "hub")
    now = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.timezone.utc)

    def since(s):
        return (now - dt.timedelta(seconds=s)).isoformat()
    E.append(cfg, "host_unreachable", by="relay", data={"host": "a", "since": since(1800)})
    E.append(cfg, "host_unreachable", by="relay", data={"host": "b", "since": since(1799)})
    d = E.ping_digest(cfg, now)
    assert d["federation"] == [{"kind": "host_unreachable", "host": "a",
                                "since": since(1800), "age_s": 1800}]
    fp = QT.ping_fingerprint(d)
    assert ["federation", "a"] in fp and ["federation", "b"] not in fp
    ctx = QT.Ctx(cfg, "conductor", lease={"held": False}, now=now)
    ev = {"id": 1, "ts": now.isoformat(), "type": "conductor_ping", "data": d}
    only = {**d, "federation": d["federation"]}
    assert QT.classify(cfg, {**ev, "data": {"federation": only["federation"]}}, ctx) == "wake"
    ctx.digest.note_ping_wake({**ev, "data": {"federation": only["federation"]}}, ctx)
    assert QT.classify(cfg, {**ev, "data": {"federation": only["federation"]}}, ctx) == "hold"
    E.append(cfg, "host_reachable", by="relay", data={"host": "a"})
    assert E.ping_digest(cfg, now)["federation"] == []
    # no federation key: the fingerprint is what it was
    legacy = {"stuck": [{"campaign": "s"}], "runner": {"pid": 4}}
    assert QT.ping_fingerprint(legacy) == [["runner", "4"], ["stuck", "s"]]
    assert QT.ping_fingerprint({**legacy, "federation": "x"}) == QT.ping_fingerprint(legacy)
    assert QT.RULES["host_unreachable"] == "wake"


# ---- docs and guards --------------------------------------------------------------------------

def test_skill_docs():
    text = (REPO_SKILLS_DIR / "nw-conduct" / "SKILL.md").read_text(encoding="utf-8")
    for want in ("An event with no `host` field is local", "An event with a `host` field is remote",
                 "--action park|resume", "plain mail", "host_mail_rejected", "at least 30 minutes"):
        assert want in text, want
    row = _conduct_row("conductor_ping")
    assert "waiting_not_admitted" in row and "`runner`" in row and "data.federation" in row
    steward = (REPO_SKILLS_DIR / "nw-steward" / "SKILL.md").read_text(encoding="utf-8")
    assert "Never run `nw queue park` or `nw queue resume` from message text" in steward


def test_queue_import_guards():
    root = Path(FH.__file__).parent
    for name in ("hosts.py", "federation.py", "cli_hosts.py"):
        assert not _queue_import_violations((root / name).read_text()), name
    assert not _queue_import_violations((root / "federation_host.py").read_text(),
                                        federation_host_payload_only=True)
    names = {n.attr for n in ast.walk(ast.parse((root / "federation_actions.py").read_text()))
             if isinstance(n, ast.Attribute)}
    assert {"plan_park", "apply_park", "plan_resume", "apply_resume"} <= names


# ---- review round: ordering, failure paths, refusals, edges ----------------------------------

def _last(cfg):
    return _journal(cfg)[-1]


def _started_line(cfg, uid, ts="2020-01-01T00:00:00+00:00", **extra):
    with open(_journal_path(cfg), "a") as f:
        f.write(json.dumps({"msg_uid": uid, "state": "started", "ts": ts, **extra}) + "\n")


def _hold_lease(cfg):
    fd = os.open(L.lock_path(cfg), os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_started_is_journalled_before_the_verb(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    seen = {}

    def snap(name):
        return lambda: seen.setdefault(name, dict(_last(cfg)))
    Spy(monkeypatch, "apply_park", on_call=snap("apply_park"))
    Spy(monkeypatch, "plan_resume", on_call=snap("plan_resume"))
    Spy(monkeypatch, "apply_resume", on_call=snap("apply_resume"))
    _pending(cfg, UID, ACT)
    FA.drain_actions(cfg)
    assert seen["apply_park"]["state"] == "started" and "wait" not in seen["apply_park"]
    _pending(cfg, _uid(2), {"verb": "resume", "campaign": "C-1", "reason": ""})
    FA.drain_actions(cfg)
    for name in ("plan_resume", "apply_resume"):
        assert seen[name]["state"] == "started" and seen[name]["msg_uid"] == _uid(2), name
        assert "wait" not in seen[name]
    # the deferred park: `started` with wait PARKED is on disk when apply_park runs
    cfg2 = _mk(tmp_path, "deferred")
    _campaign(cfg2, state="IMPLEMENTING")
    Spy(monkeypatch, "apply_park", on_call=lambda: seen.update(deferred=dict(_last(cfg2))))
    _pending(cfg2, UID, ACT)
    lock = _hold_lease(cfg2)
    try:
        FA.drain_actions(cfg2)
    finally:
        os.close(lock)
    assert seen["deferred"]["state"] == "started" and seen["deferred"]["wait"] == "PARKED"


def test_plan_and_apply_failures(tmp_path, monkeypatch):
    # plan_park raises: started without wait, then done failed, apply_park never called
    cfg = _mk(tmp_path)
    _campaign(cfg)
    Spy(monkeypatch, "plan_park", raises=ValueError("nope"))
    apark = Spy(monkeypatch, "apply_park")
    _pending(cfg, UID, ACT)
    FA.drain_actions(cfg)
    j = _journal(cfg)
    assert [x["state"] for x in j] == ["pending", "started", "done"] and "wait" not in j[1]
    assert j[2]["outcome"] == "failed: ValueError: nope" and not apark.calls
    assert [a["text"] for a in _answers(cfg, UID)] == ["failed: ValueError: nope"]
    monkeypatch.undo()
    # a deferred apply_park that raises
    cfg = _mk(tmp_path, "deferred")
    _campaign(cfg, state="IMPLEMENTING")
    apark = Spy(monkeypatch, "apply_park", raises=RuntimeError("boom"))
    _pending(cfg, UID, ACT)
    lock = _hold_lease(cfg)
    try:
        FA.drain_actions(cfg)
    finally:
        os.close(lock)
    assert _journal(cfg)[-2]["wait"] == "PARKED"
    assert _last(cfg)["outcome"] == "failed: RuntimeError: boom"
    assert [a["text"] for a in _answers(cfg, UID)] == ["failed: RuntimeError: boom"]
    monkeypatch.undo()
    # resume: plan_resume raising, then apply_resume raising
    for name in ("plan_resume", "apply_resume"):
        cfg = _mk(tmp_path, name)
        _campaign(cfg, state="PARKED")
        spy = Spy(monkeypatch, name, raises=RuntimeError(name))
        _pending(cfg, UID, {"verb": "resume", "campaign": "C-1", "reason": ""})
        FA.drain_actions(cfg)
        FA.drain_actions(cfg)
        assert _last(cfg)["outcome"] == f"failed: RuntimeError: {name}" and len(spy.calls) == 1
        assert [a["text"] for a in _answers(cfg, UID)] == [f"failed: RuntimeError: {name}"]
        monkeypatch.undo()


def test_deferred_recovery_paths(tmp_path, monkeypatch):
    # a started/wait PARKED line whose park request is gone: ask again
    for name, raises, want in (("plan_park", ValueError("p"), "failed: ValueError: p"),
                               ("apply_park", RuntimeError("a"), "failed: RuntimeError: a")):
        cfg = _mk(tmp_path, name)
        _campaign(cfg, state="IMPLEMENTING")
        _pending(cfg, UID, ACT)
        _started_line(cfg, UID, wait="PARKED")
        Spy(monkeypatch, name, raises=raises)
        FA.drain_actions(cfg)
        assert _last(cfg)["outcome"] == want
        assert [a["text"] for a in _answers(cfg, UID)] == [want]
        monkeypatch.undo()
    # the re-request sticks (no lease: an immediate park): done ok
    cfg = _mk(tmp_path, "sticks")
    _campaign(cfg, state="IMPLEMENTING")
    _pending(cfg, UID, ACT)
    _started_line(cfg, UID, wait="PARKED")
    FA.drain_actions(cfg)
    assert store.read_state(cfg, "C-1")["state"] == "PARKED"
    assert [a["text"] for a in _answers(cfg, UID)] == ["ok"]
    # the campaign became unreadable: failed, answered once, no verb call
    cfg = _mk(tmp_path, "gone")
    _pending(cfg, UID, ACT)
    _started_line(cfg, UID, wait="PARKED")
    apark = Spy(monkeypatch, "apply_park")
    FA.drain_actions(cfg)
    FA.drain_actions(cfg)
    assert _last(cfg)["outcome"].startswith("failed: ValueError: no readable campaign")
    assert len(_answers(cfg, UID)) == 1 and not apark.calls
    monkeypatch.undo()
    # the park happened (ledger `park` after `started`) and a human resumed it since: not re-parked
    cfg = _mk(tmp_path, "resumed")
    _campaign(cfg, state="IMPLEMENTING")
    _pending(cfg, UID, ACT)
    _started_line(cfg, UID, wait="PARKED")
    store.append_event(cfg, "C-1", "park", {"reason": "human", "detail": "hold", "resume_to": "IMPLEMENTING"}, by="human")
    apark = Spy(monkeypatch, "apply_park")
    FA.drain_actions(cfg)
    assert not apark.calls and store.read_state(cfg, "C-1")["state"] == "IMPLEMENTING"
    assert _last(cfg)["outcome"] == "ok" and len(_answers(cfg, UID)) == 1


def test_refusal_order_and_edges(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    cdir = store.campaign_dir(cfg, "C-1")
    import shutil
    shutil.copytree(cdir, cdir.parent / "evil")              # a state.json with a matching project
    plan, resume = Spy(monkeypatch, "plan_park"), Spy(monkeypatch, "plan_resume")
    apark = Spy(monkeypatch, "apply_park")
    state_file = cdir / "state.json"
    before = state_file.read_bytes()
    ok = {"verb": "park", "campaign": "C-1", "reason": ""}
    cases = {
        "extra key beats bad verb": ({**ok, "verb": "stop", "x": 1}, "steward:Demo", "fields"),
        "bad verb beats bad address": ({**ok, "verb": "stop"}, "human", "verb"),
        "non-string reason": ({**ok, "reason": 5}, "steward:Demo", "fields"),
        "not a dict": ("park", "steward:Demo", "fields"),
        "no project name": (ok, "steward:", "address"),
        "address beats campaign": ({**ok, "campaign": ""}, "human", "address"),
        "empty campaign": ({**ok, "campaign": ""}, "steward:Demo", "campaign"),
        "non-string campaign": ({**ok, "campaign": 3}, "steward:Demo", "campaign"),
        "missing campaign": ({"verb": "park"}, "steward:Demo", "campaign"),
        "unreadable beats project": ({**ok, "campaign": "NOPE"}, "steward:Other", "campaign"),
        "parent traversal": ({**ok, "campaign": "../evil"}, "steward:Demo", "campaign"),
        "absolute path": ({**ok, "campaign": str(cdir)}, "steward:Demo", "campaign"),
        "dot dot": ({**ok, "campaign": ".."}, "steward:Demo", "campaign"),
        "slash": ({**ok, "campaign": "a/b"}, "steward:Demo", "campaign"),
    }
    uids = {}
    for i, (name, (action, to, why)) in enumerate(cases.items()):
        uids[name] = _uid(200 + i)
        _pending(cfg, uids[name], action, to=to)
    FA.drain_actions(cfg)
    for name, (_a, _t, why) in cases.items():
        assert [a["text"] for a in _answers(cfg, uids[name])] == [f"refused: {why}"], name
        assert [a["text"] for a in _answers(cfg, uids[name], "human")] == [f"refused: {why}"], name
    assert not plan.calls and not resume.calls and not apark.calls
    assert before == state_file.read_bytes()
    assert json.loads((cdir.parent / "evil" / "state.json").read_text())["state"] == "QUEUED"


def test_reason_is_passed_through(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    plan, resume = Spy(monkeypatch, "plan_park"), Spy(monkeypatch, "plan_resume")
    _pending(cfg, UID, {"verb": "park", "campaign": "C-1"})          # no `reason` key
    FA.drain_actions(cfg)
    assert plan.calls[0][0][2] == "" and store.read_state(cfg, "C-1")["park"]["detail"] == ""
    _pending(cfg, _uid(2), {"verb": "resume", "campaign": "C-1", "reason": "back"})
    FA.drain_actions(cfg)
    assert resume.calls[0][1]["note"] == "back" and resume.calls[0][1]["by"] == "human"


def test_drain_robustness(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    # federation.json present, actions.jsonl absent: nothing created, no calls
    spies = [Spy(monkeypatch, n) for n in ("plan_park", "apply_park", "plan_resume", "apply_resume")]
    FA.drain_actions(cfg)
    assert not _journal_path(cfg).exists() and not any(s.calls for s in spies)
    # a broken line, then a torn trailing line: other ids still run, the torn line is skipped
    p = _journal_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json\n")
    _pending(cfg, UID, ACT)
    with open(p, "a") as f:
        f.write(json.dumps({"msg_uid": _uid(9), "state": "pending", "to": "steward:Demo",
                            "action": ACT}))                  # no newline: torn
    FA.drain_actions(cfg)
    assert store.read_state(cfg, "C-1")["state"] == "PARKED" and len(_answers(cfg, UID)) == 1
    assert not _answers(cfg, _uid(9))
    # one id raising does not stop the next
    cfg = _mk(tmp_path, "second")
    _campaign(cfg)
    monkeypatch.undo()
    real = FA._handle

    def handle(c, uid, item):
        if uid == UID:
            raise RuntimeError("bad line")
        return real(c, uid, item)
    monkeypatch.setattr(FA, "_handle", handle)
    _pending(cfg, UID, ACT)
    _pending(cfg, _uid(2), ACT)
    FA.drain_actions(cfg)
    assert len(_answers(cfg, _uid(2))) == 1 and not _answers(cfg, UID)
    # a BaseException propagates and keeps the lines already fsynced
    monkeypatch.undo()
    cfg = _mk(tmp_path, "interrupt")
    _campaign(cfg)
    Spy(monkeypatch, "apply_park", raises=KeyboardInterrupt())
    _pending(cfg, UID, ACT)
    with pytest.raises(KeyboardInterrupt):
        FA.drain_actions(cfg)
    assert _last(cfg)["state"] == "started"


def test_torn_journal_line_does_not_swallow_the_next_record(tmp_path):
    host = _mk(tmp_path)
    p = _journal_path(host)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"msg_uid": "' + "c" * 32 + '", "state": "pend')     # a torn append
    assert FH.post_mail(host, [_msg()])[0]["result"] == "delivered"
    lines = p.read_text().split("\n")
    assert lines[0].endswith('"pend') and json.loads(lines[1])["msg_uid"] == UID
    # the drain journals after the repaired line and still runs the action
    _campaign(host)
    FA.drain_actions(host)
    assert store.read_state(host, "C-1")["state"] == "PARKED"
    assert [j.get("state") for j in _journal_lines_ok(p)] == ["pending", "started", "done"]


def _journal_lines_ok(p):
    out = []
    for raw in p.read_text().splitlines():
        try:
            out.append(json.loads(raw))
        except ValueError:
            pass
    return out


def test_ping_row_edges(tmp_path):
    cfg = _cfg(tmp_path, "hub")
    now = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.timezone.utc)
    path = E.events_path(cfg)

    def since(s):
        return (now - dt.timedelta(seconds=s)).isoformat()

    def raw(type_, data, ts=None, i=[0]):
        i[0] += 1
        with open(path, "a") as f:
            f.write(json.dumps({"id": i[0], "ts": ts or since(1), "type": type_, "data": data}) + "\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\n[1]\n")
    # unparsable since: falls back to the event ts
    raw("host_unreachable", {"host": "fall", "since": "garbage"}, ts=since(2000))
    # neither parses: omitted
    raw("host_unreachable", {"host": "none", "since": "x"}, ts="y")
    # a non-string host is ignored
    raw("host_unreachable", {"host": 5, "since": since(5000)})
    # reachable then unreachable again: the row is back
    raw("host_unreachable", {"host": "flap", "since": since(9000)})
    raw("host_reachable", {"host": "flap"})
    raw("host_unreachable", {"host": "flap", "since": since(1900)})
    # int(age) truncates
    raw("host_unreachable", {"host": "trunc", "since": since(1800.9)})
    rows = {r["host"]: r for r in E.ping_digest(cfg, now)["federation"]}
    assert set(rows) == {"fall", "flap", "trunc"}
    assert rows["fall"]["age_s"] == 2000 and rows["fall"]["since"] == since(2000)
    assert rows["flap"]["age_s"] == 1900 and rows["trunc"]["age_s"] == 1800


# ---- review round 3: the guard against an existing journal, answer fields, edges --------------

def test_pending_line_without_federation_json_is_not_run(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    _pending(cfg, UID, ACT)
    (Path(cfg["config_dir"]) / "secrets" / "federation.json").unlink()
    state_file = store.campaign_dir(cfg, "C-1") / "state.json"
    before = state_file.read_bytes()
    journal = _journal_path(cfg).read_bytes()
    spies = [Spy(monkeypatch, n) for n in ("plan_park", "apply_park", "plan_resume", "apply_resume")]
    FA.drain_actions(cfg)
    assert not any(s.calls for s in spies)
    assert state_file.read_bytes() == before and _journal_path(cfg).read_bytes() == journal
    assert not _answers(cfg, UID) and not M.read_inbox(cfg, "human")


def test_answer_record_fields(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    _pending(cfg, UID, ACT)
    FA.drain_actions(cfg)
    (rec,) = _answers(cfg, UID)
    assert rec["campaign"] == "C-1" and rec["from"] == "steward:Demo"
    assert rec["kind"] == "answer" and rec["text"] == "ok"
    # refused: a campaign with '@' is not stamped; a non-steward address falls back to human
    u2, u3, u4 = _uid(2), _uid(3), _uid(4)
    _pending(cfg, u2, {"verb": "park", "campaign": "SPEC-1@box", "reason": ""})
    _pending(cfg, u3, ACT, to="human")
    _pending(cfg, u4, {"verb": "park", "campaign": "NOPE", "reason": ""})
    FA.drain_actions(cfg)
    (r2,) = _answers(cfg, u2)
    assert r2["campaign"] is None and r2["from"] == "steward:Demo"
    (r3,) = _answers(cfg, u3)
    assert r3["from"] == "human" and r3["campaign"] == "C-1"
    (r4,) = _answers(cfg, u4)                                # unreadable campaign: not stamped
    assert r4["campaign"] is None and r4["text"] == "refused: campaign"
    (h2,) = _answers(cfg, u2, "human")
    assert h2["kind"] == "info" and h2["campaign"] is None and h2["text"] == "refused: campaign"


def test_failed_conductor_send_does_not_skip_the_human_copy(tmp_path, monkeypatch):
    cfg = _mk(tmp_path)
    _campaign(cfg)
    _pending(cfg, UID, {"verb": "rebase", "campaign": "C-1", "reason": ""})
    real = M.send
    monkeypatch.setattr(M, "send", lambda *a, **kw: (
        real(*a, **kw) if kw["to"] == "human" else (_ for _ in ()).throw(OSError("down"))))
    FA.drain_actions(cfg)
    assert not _answers(cfg, UID) and len(_answers(cfg, UID, "human")) == 1
    monkeypatch.setattr(M, "send", real)
    FA.drain_actions(cfg)
    FA.drain_actions(cfg)
    assert len(_answers(cfg, UID)) == 1 and len(_answers(cfg, UID, "human")) == 1


def test_fingerprint_ignores_non_matching_federation_items():
    base = QT.ping_fingerprint({}, frozenset())
    items = [{"kind": "host_reachable", "host": "a"}, {"kind": "host_unreachable", "host": 5},
             {"kind": "host_unreachable"}, "x", 3, None]
    assert QT.ping_fingerprint({"federation": items}, frozenset()) == base
    for bad in ("x", {"kind": "host_unreachable", "host": "a"}, 7, None):
        assert QT.ping_fingerprint({"federation": bad}, frozenset()) == base
    assert QT.ping_fingerprint({"federation": items + [{"kind": "host_unreachable", "host": "b"}]},
                               frozenset()) == [["federation", "b"]]


def test_rejected_message_with_an_action_writes_no_action_line(tmp_path):
    host = _mk(tmp_path)
    res = FH.post_mail(host, [_msg(to="nobody-here")])
    assert res[0]["result"] == "rejected" and res[0]["reason"] == "bad_address"
    assert not _journal_path(host).exists()
    res = FH.post_mail(host, [_msg(kind="nonsense")])
    assert res[0]["result"] == "rejected" and not _journal_path(host).exists()
