"""SPEC-0425: escalation topics, the 30-min timeout (`timeouts_due` / `escalation_timeout`), the
conductor's `answer --auto`, its reversal by the human or a steward, and the CLI around them.
Nothing real is notified (the `calls` fixture of test_agentic_escalations)."""
import datetime as dt
import json
import threading

import waits

import pytest
from click.testing import CliRunner

from test_agentic_escalations import CID, _camp, _cfg, _parked, calls  # noqa: F401 (autouse)
from nightwright.agentic import brief as B
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import mailbox as M
from nightwright.agentic import quiet as QT
from nightwright.agentic import store as S
from nightwright.cli import cli

pytestmark = pytest.mark.fast


def _esc(cfg, cid=CID, **kw):
    kw.setdefault("question", "rebase onto the fixed base?")
    kw.setdefault("options", ["resume to CLOSING", "abandon"])
    return ESC.apply_escalate(cfg, ESC.plan_escalate(cfg, cid, **kw))


def _created(rec):
    return dt.datetime.fromisoformat(rec["created"])


def _due(rec, extra=1):
    return dt.datetime.fromisoformat(rec["auto"]["due_at"]) + dt.timedelta(seconds=extra)


def _timeouts(cfg):
    return [e for e in E.read_since(cfg, 0) if e["type"] == "escalation_timeout"]


def _backdate(cfg, esc_id, s=3600):
    """Move `created` and `auto.due_at` `s` seconds into the past (no real waiting)."""
    def upd(r):
        r["created"] = S.now_iso(dt.datetime.fromisoformat(r["created"]) - dt.timedelta(seconds=s))
        if r["auto"].get("due_at"):
            r["auto"]["due_at"] = S.now_iso(dt.datetime.fromisoformat(r["auto"]["due_at"])
                                            - dt.timedelta(seconds=s))
    ESC._mutate(cfg, esc_id, upd)


# ---- topics and eligibility (AC1) ------------------------------------------------------------

def test_topic_sets():
    assert set(ESC.HUMAN_ONLY_TOPICS) == set(ESC.TOPICS) - {"recovery", "judgment"}
    assert {"scope", "product", "spend_ceiling", "spend_diverging", "manual_ac", "push_main",
            "pr_merge", "human_checkout", "human_settings"} == set(ESC.HUMAN_ONLY_TOPICS)


def test_recommend_needs_a_topic_and_a_bad_topic_is_refused(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="a recommended escalation needs --topic"):
        ESC.plan_escalate(cfg, None, question="q", options=["a", "b"], recommend=1)
    with pytest.raises(ValueError, match="not a topic"):
        ESC.plan_escalate(cfg, None, question="q", options=["a"], recommend=1, topic="vibes")
    # a recovery kind counts as `recovery`; no recommendation needs no topic
    assert ESC.plan_escalate(cfg, None, question="q", options=["off", "keep"], recommend=1,
                             kind="worker_lost")["topic"] is None
    assert ESC.plan_escalate(cfg, None, question="q", options=["a"])["topic"] is None


def test_recovery_topic_is_eligible_and_due_after_1800s(tmp_path):
    cfg = _cfg(tmp_path)
    rec = _esc(cfg, None, recommend=1, topic="recovery")
    assert rec["topic"] == "recovery"
    assert rec["auto"]["eligible"] is True
    assert (dt.datetime.fromisoformat(rec["auto"]["due_at"]) - _created(rec)).total_seconds() \
        == 1800
    assert ESC.read(cfg, rec["id"])["auto"] == rec["auto"]


@pytest.mark.parametrize("topic", ESC.TOPICS)
@pytest.mark.parametrize("recommend", [1, None])
@pytest.mark.parametrize("kind", [None, "cost", "red_main", "worker_lost"])
def test_eligibility_matrix(topic, recommend, kind):
    rec = {"topic": topic, "recommendation": recommend, "kind": kind}
    want = recommend is not None and kind != "cost" and topic in ("recovery", "judgment")
    assert ESC.eligible(rec) is want
    rec["topic"] = None                         # topic-less: only a recovery kind counts
    assert ESC.eligible(rec) is (recommend is not None and kind in ESC.RECOVERY_KINDS)


@pytest.mark.parametrize("kw", [dict(recommend=1, topic="scope"), dict(recommend=1, topic="manual_ac"),
                                dict(recommend=1, topic="judgment", kind="cost"),
                                dict(recommend=None, topic="recovery"), dict()])
def test_ineligible_records_have_no_due_at(tmp_path, kw):
    cfg = _cfg(tmp_path)
    rec = _esc(cfg, None, **kw)
    assert rec["auto"]["eligible"] is False and rec["auto"]["due_at"] is None
    assert rec["auto"]["why"]


def test_zero_disables_timeouts(tmp_path):
    cfg = _cfg(tmp_path, auto_apply_after_s=0)
    rec = _esc(cfg, None, recommend=1, topic="recovery")
    assert rec["auto"]["eligible"] is True and rec["auto"]["due_at"] is None
    assert ESC.timeouts_due(cfg, _created(rec) + dt.timedelta(days=3)) == []


@pytest.mark.parametrize("bad", ["soon", -5, None])
def test_malformed_after_is_the_default(tmp_path, bad):
    assert ESC.auto_apply_after_s(_cfg(tmp_path, auto_apply_after_s=bad)) == 1800


# ---- timeouts_due (AC2) ---------------------------------------------------------------------

def test_timeout_once_after_due(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg, recommend=2, topic="judgment")
    assert ESC.timeouts_due(cfg, _due(rec, -1)) == []
    out = ESC.timeouts_due(cfg, _due(rec))
    assert [e["type"] for e in out] == ["escalation_timeout"]
    d = out[0]["data"]
    assert d == {"escalation_id": rec["id"], "campaign": CID, "recommendation": 2,
                 "label": "abandon", "topic": "judgment"}
    assert out[0]["campaign"] == CID and out[0]["by"] == "watchdog"
    assert ESC.read(cfg, rec["id"])["auto"]["announced_at"]
    assert ESC.timeouts_due(cfg, _due(rec, 600)) == []          # a second sync
    assert len(_timeouts(cfg)) == 1


def test_two_concurrent_followers_announce_once(tmp_path):
    cfg = _cfg(tmp_path)
    rec = _esc(cfg, None, recommend=1, topic="recovery")
    when = _due(rec)
    start, outs = threading.Barrier(4), []

    def follower():
        start.wait()
        outs.append(ESC.timeouts_due(cfg, when))
    ts = [threading.Thread(target=follower) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        waits.join(t)
    assert sum(len(o) for o in outs) == 1 and len(_timeouts(cfg)) == 1


def test_sync_ping_emits_the_timeout(tmp_path):
    cfg = _cfg(tmp_path)
    rec = _esc(cfg, None, recommend=1, topic="recovery")
    E.sync(cfg, now=_due(rec), ping=True)
    assert len(_timeouts(cfg)) == 1
    E.sync(cfg, now=_due(rec, 60), ping=True)
    assert len(_timeouts(cfg)) == 1


def test_human_only_answered_legacy_and_unparked_records_get_none(tmp_path):
    cfg = _cfg(tmp_path)
    scope = _esc(cfg, None, recommend=1, topic="scope")
    none = _esc(cfg, None)                                         # no recommendation
    answered = _esc(cfg, None, recommend=1, topic="recovery")
    ESC.apply_answer(cfg, ESC.plan_answer(cfg, answered["id"], choose="1"))
    legacy = _esc(cfg, None, recommend=1, topic="recovery")
    ESC._mutate(cfg, legacy["id"], lambda r: r.pop("auto"))       # from before SPEC-0425
    later = _created(scope) + dt.timedelta(days=2)
    assert ESC.timeouts_due(cfg, later) == []
    for r in (scope, none, legacy):
        assert ESC.read(cfg, r["id"])["status"] == "open"


def test_deferred_park_is_not_announced_until_it_parks(tmp_path):
    cfg = _cfg(tmp_path)
    _camp(cfg, "IMPLEMENTING")
    st = S.read_state(cfg, CID)
    st["park_requested"] = {"reason": "human_decision", "detail": "ESC-0001: q",
                            "at": S.now_iso(), "by": "conductor"}
    S.write_state(cfg, CID, st)
    rec = ESC._create(cfg, {"by": "conductor", "campaign": CID, "spec_id": None,
                            "project": None, "question": "q",
                            "options": [{"n": 1, "label": "a"}], "recommendation": 1,
                            "facts": [], "park": None, "kind": None, "topic": "recovery"})
    assert ESC.timeouts_due(cfg, _due(rec)) == []
    assert "announced_at" not in ESC.read(cfg, rec["id"])["auto"]


def test_a_failed_append_gives_the_claim_back(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    rec = _esc(cfg, None, recommend=1, topic="recovery")
    real = E.append

    def boom(cfg_, type_, **kw):
        if type_ == "escalation_timeout":
            raise OSError("disk full")
        return real(cfg_, type_, **kw)
    monkeypatch.setattr(E, "append", boom)
    assert ESC.timeouts_due(cfg, _due(rec)) == []
    assert "announced_at" not in ESC.read(cfg, rec["id"])["auto"]
    monkeypatch.setattr(E, "append", real)
    assert len(ESC.timeouts_due(cfg, _due(rec))) == 1


def test_timeouts_due_never_raises(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ESC, "list_all", lambda *a, **k: 1 / 0)
    assert ESC.timeouts_due(cfg) == []


# ---- answer --auto (AC3) --------------------------------------------------------------------

def test_auto_refusals(tmp_path):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg, recommend=1, topic="recovery")
    with pytest.raises(ValueError, match="not due yet"):
        ESC.plan_answer(cfg, rec["id"], auto=True, by="conductor")
    with pytest.raises(ValueError, match="conductor's"):
        ESC.plan_answer(cfg, rec["id"], auto=True, by="human", now=_due(rec))
    with pytest.raises(ESC.AnswerFormError, match="no other answer form"):
        ESC.plan_answer(cfg, rec["id"], auto=True, choose="2", by="conductor")
    loose = _esc(cfg, None, recommend=1, topic="scope")
    with pytest.raises(ValueError, match="topic scope is human-only"):
        ESC.plan_answer(cfg, loose["id"], auto=True, by="conductor",
                        now=_created(loose) + dt.timedelta(days=1))
    legacy = _esc(cfg, None, recommend=1, topic="recovery")
    ESC._mutate(cfg, legacy["id"], lambda r: r.pop("auto"))
    with pytest.raises(ValueError, match="predates"):
        ESC.plan_answer(cfg, legacy["id"], auto=True, by="conductor",
                        now=_created(legacy) + dt.timedelta(days=1))
    assert ESC.read(cfg, rec["id"])["status"] == "open"


def test_auto_success_path(tmp_path, calls):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    rec = _esc(cfg, recommend=1, topic="recovery")
    calls.clear()
    plan = ESC.plan_answer(cfg, rec["id"], auto=True, by="conductor", now=_due(rec))
    done = ESC.apply_answer(cfg, plan)
    assert done["status"] == "answered" and done["answered_by"] == "conductor"
    a = done["answer"]
    assert (a["kind"], a["choice"], a["label"], a["auto"]) == ("choose", 1, "resume to CLOSING",
                                                               True)
    assert a["note"] == "auto-applied after 30 min without an answer (SPEC-0425)"
    ev = [e for e in E.read_since(cfg, 0) if e["type"] == "escalation_answered"]
    assert len(ev) == 1 and ev[0]["data"]["auto"] is True and ev[0]["by"] == "conductor"
    infos = [m for m in M.read_inbox(cfg, "human") if m.get("kind") == "info"]
    assert len(infos) == 1
    assert (f"{rec['id']}: no answer in 30 min; applied option 1 (resume to CLOSING). "
            f"To reverse: nw escalations answer {rec['id']} --choose 2") in infos[0]["text"]
    assert [c[-2] for c in calls] == [f"nw: {rec['id']} auto-applied"]
    stored = ESC.read(cfg, rec["id"])
    assert stored["auto"]["mail_id"] == infos[0]["id"] and stored["auto"]["applied_at"]
    note = [e for e in S.read_ledger(cfg, CID) if e["type"] == "note"][-1]
    assert note["by"] == "conductor" and rec["id"] in note["text"] and note["author"] == "conductor"
    assert ESC.auto_marker(stored, _due(rec)) == "auto-applied"


# ---- reversal (AC4) -------------------------------------------------------------------------

def _auto_answered(cfg):
    _parked(cfg)
    rec = _esc(cfg, recommend=1, topic="recovery")
    ESC.apply_answer(cfg, ESC.plan_answer(cfg, rec["id"], auto=True, by="conductor",
                                          now=_due(rec)))
    st = S.read_state(cfg, CID)                    # the conductor applied it: moved on
    st["state"], st["park"] = "CLOSING", None
    S.write_state(cfg, CID, st)
    return rec["id"]


@pytest.mark.parametrize("by", ["human", "steward:Meta-Tools"])
def test_human_or_steward_reverses_an_auto_answer(tmp_path, monkeypatch, by):
    monkeypatch.setattr("nightwright.rules.projects_payload",
                        lambda cfg: {"projects": [{"name": "Meta-Tools"}]})
    cfg = _cfg(tmp_path)
    esc_id = _auto_answered(cfg)
    plan = ESC.plan_answer(cfg, esc_id, choose="2", by=by)
    assert plan["reverses"]["auto"] is True
    rec = ESC.apply_answer(cfg, plan)
    assert rec["answer"]["choice"] == 2 and not rec["answer"].get("auto")
    assert rec["answered_by"] == by
    hist = rec["answer_history"]
    assert len(hist) == 1 and hist[0]["answer"]["auto"] is True
    assert hist[0]["answered_by"] == "conductor"
    ev = [e for e in E.read_since(cfg, 0) if e["type"] == "escalation_answered"][-1]
    assert ev["data"]["reverses_auto"] is True and ev["data"]["previous"]["choice"] == 1
    assert ESC.auto_marker(rec, dt.datetime.now().astimezone()) == "reversed"
    # a second reversal of a non-auto answer is refused as before
    with pytest.raises(ValueError, match="answered since"):
        ESC.plan_answer(cfg, esc_id, choose="1", by="human")


def test_the_conductor_cannot_reverse_its_own_auto_answer(tmp_path):
    cfg = _cfg(tmp_path)
    esc_id = _auto_answered(cfg)
    with pytest.raises(ValueError, match="answered since"):
        ESC.plan_answer(cfg, esc_id, choose="2", by="conductor")


# ---- quiet, brief (AC6) ---------------------------------------------------------------------

def test_timeout_wakes_the_quiet_follower():
    assert QT.RULES.get("escalation_timeout") == "wake"
    assert "escalation_timeout" in E.EVENT_TYPES


def test_brief_rows_carry_topic_due_and_auto_applied(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    esc_id = _auto_answered(cfg)
    other = "SPEC-0901-20261025-bbbb"
    _camp(cfg, "PARKED", cid=other, park={"reason": "loop_exhausted", "detail": "d",
                                          "since": S.now_iso(), "resume_to": "IMPLEMENTING"})
    open_rec = _esc(cfg, other, recommend=1, topic="judgment")
    b = B.conductor_brief(cfg)
    row = next(e for e in b["escalations_open"] if e["id"] == open_rec["id"])
    assert row["topic"] == "judgment" and row["due_at"] == open_rec["auto"]["due_at"]
    assert [r["id"] for r in b["auto_applied_24h"]] == [esc_id]
    text = B.render_text(b)
    assert "auto-applied escalations (24h):" in text and esc_id in text


# ---- CLI ------------------------------------------------------------------------------------

def run(cfg, monkeypatch, *args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, list(args))


def test_cli_topic_auto_and_list_markers(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    base = ["queue", "escalate", CID, "--question", "resume?", "--option", "resume",
            "--option", "abandon", "--recommend", "1"]
    r = run(cfg, monkeypatch, *base, "--yes")
    assert r.exit_code != 0 and "needs --topic" in r.output
    r = run(cfg, monkeypatch, *base, "--topic", "recovery", "--yes", "--json")
    assert r.exit_code == 0, r.output
    esc_id = json.loads(r.output)["escalation"]["id"]
    run(cfg, monkeypatch, "queue", "escalate", "--question", "split it?", "--option", "a",
        "--recommend", "1", "--topic", "scope", "--yes")
    r = run(cfg, monkeypatch, "escalations", "list")
    assert "due in 29m" in r.output or "due in 30m" in r.output, r.output
    assert "human-only" in r.output and "recovery" in r.output
    r = run(cfg, monkeypatch, "escalations", "answer", esc_id, "--auto", "--by", "conductor",
            "--yes")
    assert r.exit_code != 0 and "not due yet" in r.output
    _backdate(cfg, esc_id)
    r = run(cfg, monkeypatch, "escalations", "answer", esc_id, "--auto", "--by", "conductor")
    assert r.exit_code == 0 and "not written" in r.output
    r = run(cfg, monkeypatch, "escalations", "answer", esc_id, "--auto", "--by", "conductor",
            "--yes")
    assert r.exit_code == 0 and "auto-applied" in r.output, r.output
    r = run(cfg, monkeypatch, "escalations", "list", "--all")
    assert "auto-applied" in r.output
    r = run(cfg, monkeypatch, "escalations", "show", esc_id)
    assert f"reverse with nw escalations answer {esc_id} --choose 2" in r.output
    r = run(cfg, monkeypatch, "escalations", "answer", esc_id, "--choose", "2", "--yes")
    assert r.exit_code == 0 and "reverses the auto answer" in r.output, r.output
    r = run(cfg, monkeypatch, "escalations", "show", esc_id)
    assert "(auto, reversed)" in r.output


def test_conductor_skill_has_the_timeout_row_reversal_and_human_only_topics():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "skills"
    text = (root / "nw-conduct" / "SKILL.md").read_text(encoding="utf-8")
    assert "| `escalation_timeout` | `escalation_timeout` |" in text
    assert "nw escalations answer <id> --auto --by conductor" in text
    assert "reverses_auto" in text
    assert "Every\n  `--recommend` needs `--topic`" in text
    for t in ESC.HUMAN_ONLY_TOPICS:
        assert f"`{t}`" in text, t
    assert "reversed the same way" in (root / "nw-steward" / "SKILL.md").read_text(
        encoding="utf-8")
