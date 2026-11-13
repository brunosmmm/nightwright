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
    def __init__(self, monkeypatch, name, raises=None):
        self.calls = []
        real = getattr(Q, name)

        def wrapper(*a, **kw):
            self.calls.append((a, kw))
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
    assert synced.index("actions.jsonl") < synced.index("received.jsonl")
    assert synced.index("actions.jsonl") > 0
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
