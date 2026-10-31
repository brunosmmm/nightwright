"""SPEC-0301: `wt queue conductor-brief` — contents, caps, the text bound, compaction signal."""
import datetime as dt
import json

import pytest

from test_agentic_conductor_cost import line, write_transcript
from test_agentic_decisions import (UTC, cfg, clock, decide, mk, park, resume, run)  # noqa: F401
from wt.agentic import brief as BR
from wt.agentic import conductor_cost as CC
from wt.agentic import decisions as D
from wt.agentic import escalations as ESC
from wt.agentic import mailbox as M
from wt.agentic import queue as Q
from wt.agentic import store as S


def brief(cfg):
    return BR.conductor_brief(cfg, now=dt.datetime.now().astimezone())


def test_brief_contents_and_caps(cfg, clock):
    for i in range(23):
        mk(cfg, f"P{i:02d}")
        park(cfg, f"P{i:02d}", detail="x" * 300)
    decide(cfg, "P22", why="r" * 300)
    for i in range(22):
        mk(cfg, f"A{i:02d}")
    mk(cfg, "Q1", state="QUEUED")
    decide(cfg, "A00", kind="dispatch", rule="run_overdue", why="check the run")
    for _ in range(12):
        decide(cfg, None, kind="no_action", rule="message.answer", why="fine")
    b = brief(cfg)
    assert b["schema"] == BR.SCHEMA
    for key in ("runner", "conductor", "counts", "parked", "active", "open_dispatches",
                "recent_decisions", "unanswered_mail", "escalations_open", "standing", "more"):
        assert key in b, key
    assert b["counts"] == {"active": 22, "queued": 1, "parked": 23}
    assert len(b["parked"]) == 20 and len(b["active"]) == 20
    assert b["more"] == {"parked": 3, "active": 2, "recent_decisions": 4}
    assert len(b["recent_decisions"]) == 10
    assert [d["campaign"] for d in b["open_dispatches"]] == ["A00"]
    p = next(x for x in b["parked"] if x["campaign"] == "P22")
    assert p["last_decision"]["rule"] == "parked.loop_exhausted"
    assert len(p["last_decision"]["rationale"]) <= 200 and len(p["detail"]) <= 200
    assert p["decided"] is True and p["overdue"] is False and p["owner"] == "conductor"
    assert next(x for x in b["parked"] if x["campaign"] == "P21")["last_decision"] is None
    assert b["standing"]["memory_paths"] == [] and b["standing"]["standing_rules"] == []
    assert b["standing"]["skill"].endswith("wt-conduct/SKILL.md")
    assert "+3 more" in BR.render_text(b)


def test_brief_last_decision_is_scoped_to_the_current_park(cfg, clock):
    mk(cfg, "X")
    park(cfg, "X")
    decide(cfg, "X", why="old decision")
    resume(cfg, "X")
    park(cfg, "X")
    item = brief(cfg)["parked"][0]
    assert item["last_decision"] is None and item["decided"] is False
    decide(cfg, "X", why="new decision")
    item = brief(cfg)["parked"][0]
    assert item["last_decision"]["rationale"] == "new decision" and item["decided"] is True


def test_brief_text_size_bound(cfg, clock):
    for i in range(60):
        cid = f"P{i:02d}-{'c' * 30}"
        mk(cfg, cid)
        park(cfg, cid, reason="loop_exhausted", detail="d" * 200)
        decide(cfg, cid, why="w" * 200)
    for i in range(60):
        mk(cfg, f"A{i:02d}-{'c' * 30}")
    for _ in range(440):
        decide(cfg, None, kind="no_action", rule="message.answer", why="n" * 200)
    assert len(D.read(cfg)) == 500
    b = brief(cfg)
    text = BR.render_text(b)
    assert len(text) <= 12000
    lines = text.splitlines()
    assert lines[0].startswith("runner:") and any(l.startswith("conductor:") for l in lines)
    parked = lines[lines.index("parked:") + 1:]
    assert parked[0].startswith("- P")
    # the lower-priority sections go first: active, recent decisions and standing are empty
    # before any parked item is lost
    n_parked = sum(1 for l in parked if l.startswith("- P"))

    def items(header):
        out = []
        for l in lines[lines.index(header) + 1:]:
            if not l.startswith("- "):
                break
            out.append(l)
        return out

    assert "standing:" in lines and "recent decisions:" in lines
    assert items("standing:") == []
    assert n_parked == 20 or (items("recent decisions:") == [] and not items("active:"))
    # a limit that cannot hold all 20 parked items: everything below parked is already gone
    tight = BR.render_text(b, limit=6000)
    tl = tight.splitlines()
    t_parked = [l for l in tl[tl.index("parked:") + 1:] if l.startswith("- P")]
    assert 1 <= len(t_parked) < 20 and len(tight) <= 6000
    lines = tl
    assert items("recent decisions:") == [] and items("standing:") == []
    assert "active:" not in lines or items("active:") == []
    lines = text.splitlines()
    assert not [l for l in text.splitlines() if l.startswith("- A")] or n_parked == 20
    full = BR.render_text(b, limit=10 ** 9)
    assert len(full) > 12000 and "- skill" in full and "recent decisions:" in full
    assert "- skill" not in text
    tiny = BR.render_text(b, limit=800)
    assert len(tiny) <= 800 and tiny.startswith("runner:")


def test_brief_escalation_and_mail_parts(cfg, clock, monkeypatch):
    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(ESC, "_run_notify", lambda argv: Done())
    mk(cfg, "E1")
    park(cfg, "E1")
    mk(cfg, "E2")
    park(cfg, "E2")
    rec = ESC.apply_escalate(cfg, ESC.plan_escalate(cfg, "E1", question="ship it? " + "q" * 300,
                                                    options=["yes", "no"], recommend=1))
    for i in range(13):
        M.send(cfg, to="conductor", frm="human", kind="question", text=f"m{i} " + "t" * 300)
    M.send(cfg, to="conductor", frm="human", kind="info", text="fyi, no reply needed")
    b = brief(cfg)
    by_cid = {p["campaign"]: p for p in b["parked"]}
    assert by_cid["E1"]["escalation"] == rec["id"] and by_cid["E2"]["escalation"] is None
    assert [e["id"] for e in b["escalations_open"]] == [rec["id"]]
    assert b["escalations_open"][0]["campaign"] == "E1"
    assert len(b["escalations_open"][0]["question"]) <= 160
    assert len(b["unanswered_mail"]) == 10 and b["more"]["unanswered_mail"] == 3
    assert all(len(m["text"]) <= 160 and m["id"].startswith("conductor:")
               for m in b["unanswered_mail"])
    text = BR.render_text(b)
    assert f"esc={rec['id']}" in text and "open escalations:" in text and "+3 more" in text


def test_brief_context_from_transcript(cfg, clock):
    mk(cfg, "A")
    b = brief(cfg)
    assert b["conductor"]["context_tokens"] is None and b["conductor"]["compact_now"] is False
    assert b["conductor"]["session_id"] is None and b["conductor"]["compact_at"] == 150000
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="sess")
    b = brief(cfg)                                             # registered, no transcript
    assert b["conductor"]["session_id"] == "sess" and b["conductor"]["context_tokens"] is None
    write_transcript(cfg, "sess", [
        line("m1", "2026-10-30T10:00:00.000Z", usage={"input_tokens": 90_000}),
        line("m2", "2026-10-30T11:00:00.000Z", usage={"input_tokens": 100_000,
                                                      "cache_read_input_tokens": 60_000})])
    b = brief(cfg)
    assert b["conductor"]["context_tokens"] == 160_000 and b["conductor"]["compact_now"] is True
    write_transcript(cfg, "sess", [
        line("m1", "2026-10-30T10:00:00.000Z", usage={"input_tokens": 90_000}),
        line("m2", "2026-10-30T11:00:00.000Z", usage={"input_tokens": 140_000})])
    assert brief(cfg)["conductor"]["compact_now"] is False
    cfg["agentic"] = {"conductor": {"compact_at_tokens": 100_000, "memory_paths": ["/m.md"],
                                    "standing_rules": ["never X"]}}
    b = brief(cfg)
    assert b["conductor"]["compact_now"] is True and b["conductor"]["compact_at"] == 100_000
    assert b["standing"]["memory_paths"] == ["/m.md"] and b["standing"]["standing_rules"] == ["never X"]
    cfg["agentic"] = {"conductor": {"compact_at_tokens": "lots"}}
    assert brief(cfg)["conductor"]["compact_at"] == 150000


def test_brief_follows_the_newest_session_and_the_threshold_edge(cfg, clock):
    mk(cfg, "A")
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="old")
    CC.record_session(cfg, "conductor", {}, source="manual", session_id="new")
    write_transcript(cfg, "old", [line("o1", "2026-10-30T10:00:00.000Z",
                                       usage={"input_tokens": 160_000})])
    write_transcript(cfg, "new", [line("n1", "2026-10-30T09:00:00.000Z",
                                       usage={"input_tokens": 140_000})])
    c = brief(cfg)["conductor"]
    assert (c["session_id"], c["context_tokens"], c["compact_now"]) == ("new", 140_000, False)
    cfg["agentic"] = {"conductor": {"compact_at_tokens": 140_000}}      # context == compact_at
    assert brief(cfg)["conductor"]["compact_now"] is False
    cfg["agentic"] = {"conductor": {"compact_at_tokens": 139_999}}
    assert brief(cfg)["conductor"]["compact_now"] is True


def test_brief_overdue_and_runner_fields(cfg, clock, monkeypatch):
    lease = {"held": True, "pid": 4242, "heartbeat_age_s": 7.5, "draining": {"reason": "code"}}
    monkeypatch.setattr(Q.L, "lease_status", lambda cfg, now=None: dict(lease))
    mk(cfg, "U")
    park(cfg, "U")
    mk(cfg, "V")
    park(cfg, "V")
    decide(cfg, "V")
    soon = dt.datetime(2026, 10, 30, 12, 1, tzinfo=UTC)
    late = dt.datetime(2026, 10, 30, 12, 30, tzinfo=UTC)              # past park_decide_s (300)
    by = {p["campaign"]: p for p in BR.conductor_brief(cfg, now=soon)["parked"]}
    assert by["U"]["overdue"] is False
    b = BR.conductor_brief(cfg, now=late)
    by = {p["campaign"]: p for p in b["parked"]}
    assert by["U"]["overdue"] is True and by["U"]["decided"] is False
    assert by["V"]["overdue"] is False and by["V"]["decided"] is True
    assert "overdue=true" in BR.render_text(b)
    assert b["runner"] == {"held": True, "pid": 4242, "heartbeat_age_s": 7.5, "draining": "code"}
    assert "runner: held pid 4242, heartbeat 7.5s, draining code" in BR.render_text(b)
    lease.update(held=False, pid=None, heartbeat_age_s=None, draining=None)
    b = BR.conductor_brief(cfg, now=late)
    assert b["runner"] == {"held": False, "pid": None, "heartbeat_age_s": None, "draining": None}
    assert BR.render_text(b).startswith("runner: NOT RUNNING")


def test_brief_caps_open_dispatches_and_escalations(cfg, clock, monkeypatch):
    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(ESC, "_run_notify", lambda argv: Done())
    for i in range(12):
        cid = mk(cfg, f"D{i:02d}")
        park(cfg, cid)
        decide(cfg, cid, kind="dispatch", rule="run_overdue", why=f"check {i}")
        ESC.apply_escalate(cfg, ESC.plan_escalate(cfg, cid, question=f"q{i}",
                                                  options=["yes", "no"], recommend=1))
    b = brief(cfg)
    assert len(b["open_dispatches"]) == 10 and b["more"]["open_dispatches"] == 2
    assert len(b["escalations_open"]) == 10 and b["more"]["escalations_open"] == 2
    text = BR.render_text(b)
    assert text.count("+2 more") >= 2


def test_cli_decision_decisions_brief(cfg, clock, run):
    mk(cfg, "C1")
    park(cfg, "C1", detail="needs a look")
    r = run("conductor-brief")
    assert r.exit_code == 0, r.output
    assert "runner:" in r.output and "C1" in r.output and "last: none" in r.output
    r = run("decision", "C1", "--kind", "resume", "--rule", "parked.loop_exhausted",
            "--action", "wt queue resume C1", "transient failures")
    assert r.exit_code == 0 and "decision #1 resume" in r.output
    b = json.loads(run("conductor-brief", "--json").output)
    assert b["schema"] == BR.SCHEMA and b["parked"][0]["decided"] is True
    assert b["parked"][0]["last_decision"]["kind"] == "resume"
    r = run("conductor-brief")
    assert "resume/parked.loop_exhausted" in r.output and "last: none" not in r.output
    r = run("decision", "C1", "--kind", "resume", "--rule", "nope!", "x")
    assert r.exit_code == 1 and "bad rule id" in r.output


def test_brief_shows_auto(cfg, clock):
    """SPEC-0302: a park the queue decided shows its rule and resume time."""
    mk(cfg, "AUTO")
    mk(cfg, "PLAIN")
    park(cfg, "AUTO", reason="base_moving")
    park(cfg, "PLAIN")
    st = S.read_state(cfg, "AUTO")
    st["park"]["auto"] = {"rule": "parked.base_moving", "resume_at": "2026-10-30T10:01:00+00:00"}
    S.write_state(cfg, "AUTO", st)
    b = brief(cfg)
    by = {p["campaign"]: p for p in b["parked"]}
    assert by["AUTO"]["auto"] == {"rule": "parked.base_moving",
                                  "resume_at": "2026-10-30T10:01:00+00:00"}
    assert by["PLAIN"]["auto"] is None
    assert "auto=parked.base_moving@2026-10-30T10:01:00+00:00" in BR.render_text(b)


def test_cli_conductor_brief_json_shows_auto(cfg, clock, run):
    mk(cfg, "AUTO")
    park(cfg, "AUTO", reason="base_moving")
    st = S.read_state(cfg, "AUTO")
    st["park"]["auto"] = {"rule": "parked.base_moving", "resume_at": "2026-10-30T10:01:00+00:00"}
    S.write_state(cfg, "AUTO", st)
    b = json.loads(run("conductor-brief", "--json").output)
    assert b["parked"][0]["auto"] == {"rule": "parked.base_moving",
                                      "resume_at": "2026-10-30T10:01:00+00:00"}
