"""SPEC-0192: every log the agentic layer saves is JSON/JSONL with a declared schema — the
ledger (validated on append), `run.json` beside each persona run, a `.json` sidecar beside each
raw gate log, and the runner's own `runner.jsonl` (+ `wt queue runner-log`)."""
import json

import pytest

from test_agentic_e2e import (added, impl_done, make_cfg, make_repo, review, run_cli, script,
                              tr_review, triage)
from wt.agentic import queue as Q
from wt.agentic import schemas as SC
from wt.agentic import store as S


def _run_to_idle(tmp_path, monkeypatch, entries):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    return cfg, cid


def test_every_saved_record_matches_its_schema(tmp_path, monkeypatch):
    cfg, cid = _run_to_idle(tmp_path, monkeypatch, [impl_done(), review()])
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    cdir = S.campaign_dir(cfg, cid)

    ledger = [json.loads(line) for line in (cdir / "ledger.jsonl").read_text().splitlines()]
    assert ledger and all(rec["schema"] == SC.LEDGER_SCHEMA for rec in ledger)
    assert [p for rec in ledger for p in SC.ledger_problems(rec)] == []

    runs = sorted(d for d in (cdir / "runs").iterdir() if d.is_dir())   # skip .fake-cursor
    assert runs
    ends = {e["run_id"]: e for e in S.read_ledger(cfg, cid, types=["run.end"])}
    for d in runs:
        rec = json.loads((d / "run.json").read_text())
        assert SC.record_problems(rec, SC.RUN_KEYS, SC.RUN_SCHEMA) == []
        assert rec["campaign"] == cid and rec["run_id"] == d.name
        assert rec["cost_usd"] == ends[d.name]["cost_usd"]
        assert rec["verdict_ok"] == ends[d.name]["verdict_ok"]
        for f in rec["files"].values():
            assert (d / f).is_file(), f

    logs = sorted((cdir / "gates").glob("*.log"))
    assert logs
    for log in logs:
        side = json.loads(log.with_suffix(".json").read_text())
        assert SC.record_problems(side, SC.GATE_KEYS, SC.GATE_SCHEMA) == []
        assert side["log"] == log.name and side["cmd"]
        assert not log.read_text().startswith("# ")              # no wt text in raw output

    journal = Q.read_journal(cfg)
    assert [p for rec in journal for p in SC.runner_problems(rec)] == []
    kinds = [rec["kind"] for rec in journal]
    assert kinds[0] == "start" and kinds[-1] == "stop" and "tick" in kinds
    assert journal[-1]["reason"] == "idle"
    steps = [s for rec in journal if rec["kind"] == "tick" for s in rec["steps"]]
    assert ("CLOSING", "MERGED") in [(s["from"], s["to"]) for s in steps]


def test_triage_path_records_are_valid_too(tmp_path, monkeypatch):
    """A failing gate → triage → fix loop exercises finding/park-free paths and more runs."""
    repo = make_repo(tmp_path, test_gate="test -f feature.txt && grep -q fixed feature.txt")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [
        impl_done(), triage("fix"), impl_done(content="fixed\n"), review()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    cdir = S.campaign_dir(cfg, cid)
    ledger = [json.loads(line) for line in (cdir / "ledger.jsonl").read_text().splitlines()]
    assert [p for rec in ledger for p in SC.ledger_problems(rec)] == []
    assert len([d for d in (cdir / "runs").iterdir() if d.is_dir()]) == 4
    failed = [json.loads(p.read_text()) for p in (cdir / "gates").glob("*.json")]
    assert any(not g["ok"] for g in failed) and any(g["ok"] for g in failed)


def test_append_event_rejects_unknown_type_and_missing_keys(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    S.create_campaign(cfg, {"id": "C1", "spec_id": "SPEC-0001", "state": "QUEUED"})
    path = S.campaign_dir(cfg, "C1") / "ledger.jsonl"
    before = path.read_text()
    with pytest.raises(ValueError, match="unknown event type 'mystery'"):
        S.append_event(cfg, "C1", "mystery", {})
    with pytest.raises(ValueError, match="transition: missing 'reason'"):
        S.append_event(cfg, "C1", "transition", {"from": "QUEUED", "to": "OPENING"})
    assert path.read_text() == before
    rec = S.append_event(cfg, "C1", "note", {"text": "ok"})
    assert rec["schema"] == SC.LEDGER_SCHEMA and SC.ledger_problems(rec) == []


def test_pre_schema_records_still_read_as_v1():
    old = {"ts": "t", "seq": 1, "campaign": "c", "type": "note", "by": "human", "text": "x"}
    assert SC.ledger_problems(old) == []
    assert SC.ledger_problems({**old, "schema": "wt.ledger.event.v9"}) == [
        "unknown schema 'wt.ledger.event.v9'"]


def test_runner_crash_is_journaled(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))

    def boom(*a, **k):
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(Q, "tick", boom)
    with pytest.raises(RuntimeError):
        Q.run(cfg, once=True)
    recs = Q.read_journal(cfg)
    assert [r["kind"] for r in recs] == ["start", "crash"]
    assert recs[-1]["error"] == "RuntimeError: tick exploded" and "Traceback" in recs[-1]["traceback"]
    assert SC.runner_problems(recs[-1]) == []


def test_runner_log_cli(tmp_path, monkeypatch):
    cfg, cid = _run_to_idle(tmp_path, monkeypatch, [impl_done(), review()])
    r = run_cli(cfg, monkeypatch, ["queue", "runner-log", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.runner.log.v1" and d["records"][-1]["kind"] == "stop"
    r = run_cli(cfg, monkeypatch, ["queue", "runner-log", "--tail", "0"])
    assert r.exit_code == 0 and "start" in r.output and "stop" in r.output and cid in r.output
    r = run_cli(cfg, monkeypatch, ["queue", "runner-log", "--tail", "1", "--json"])
    assert len(json.loads(r.output)["records"]) == 1


def test_runner_log_renders_drain_tick_and_admission_blocked(tmp_path, monkeypatch):
    """SPEC-0268: a draining harvest with a tick reads `tick N (drain)`; `admission_blocked` is
    one line naming the holders."""
    cfg = make_cfg(tmp_path, make_repo(tmp_path), script(tmp_path, []))
    Q.journal(cfg, "tick", tick=90, steps=[], admitted=0, errors=[], started=[], in_flight=[],
              drain=True)
    Q.journal(cfg, "tick", tick=91, steps=[], admitted=0, errors=[], started=[], in_flight=[],
              drain=False)
    Q.journal(cfg, "admission_blocked", tick=92, draining=True, ready=["SPEC-1-a"], slots=2,
              occupied=2, holders=[{"campaign": "SPEC-2-b", "state": "CLOSING", "deferred": True},
                                   {"campaign": "SPEC-3-c", "state": "GATING", "deferred": False}])
    r = run_cli(cfg, monkeypatch, ["queue", "runner-log", "--tail", "0"])
    assert r.exit_code == 0, r.output
    assert "tick 90 (drain)" in r.output and "tick 91" in r.output
    Q.journal(cfg, "tick", drain=True)                  # _drain / crash sweep: no tick number
    r2 = run_cli(cfg, monkeypatch, ["queue", "runner-log", "--tail", "1"])
    assert r2.exit_code == 0 and "drain" in r2.output and "tick None" not in r2.output
    (line,) = [ln for ln in r.output.splitlines() if "admission blocked" in ln]
    assert "1 ready, slots 2/2" in line
    assert "SPEC-2-b CLOSING deferred" in line and "SPEC-3-c GATING" in line


def test_test_reviewer_records_are_valid_and_routed(tmp_path, monkeypatch):
    """SPEC-0205 AC4: the test reviewer's findings are `finding` events with persona and `ac`;
    `pass` + a `must_fix` finding routes as `fix`; the ledger validates."""
    from test_agentic_reviewers import PersonaRunner, drive
    mixed = tr_review("pass")                       # pass, but carries a must_fix finding
    mixed["verdict"]["findings"] = tr_review("fix")["verdict"]["findings"]
    scripts = {"implementer": [impl_done(), impl_done(content="better\n")],
               "conformance_reviewer": [review("pass"), review("pass")],
               "test_reviewer": [mixed, tr_review("pass")]}
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []),
                   profile={"reviewers": ["conformance_reviewer", "test_reviewer"]})
    cid = added(cfg, monkeypatch)
    drive(cfg, PersonaRunner(scripts))
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 1     # pass+must_fix ⇒ fix
    ledger = [json.loads(line)
              for line in (S.campaign_dir(cfg, cid) / "ledger.jsonl").read_text().splitlines()]
    assert [p for rec in ledger for p in SC.ledger_problems(rec)] == []
    found = [r for r in ledger if r["type"] == "finding" and r["persona"] == "test_reviewer"]
    assert [(f["severity"], f["ac"]) for f in found] == [("must_fix", "feature.txt exists")]
    assert st["last_review"]["verdicts"]["test_reviewer"]["coverage_assessment"] == [
        {"ac": "feature.txt exists", "covered": True, "tests": ["tests/test_feature.py::test_exists"]}]
    first = S.runs_dir(cfg, cid) / "003-test_reviewer" / "run.json"
    assert json.loads(first.read_text())["verdict"]["coverage_assessment"][0]["covered"] is True


def test_followup_event_is_registered_and_requires_its_keys(tmp_path):              # SPEC-0207
    keys = ("persona", "severity", "title", "key", "idea_id", "origin")
    assert SC.EVENT_KEYS["followup"] == keys
    rec = {"ts": "t", "seq": 1, "campaign": "c", "type": "followup", "by": "queue",
           **{k: "x" for k in keys}}
    assert SC.ledger_problems(rec) == []
    del rec["idea_id"]
    assert SC.ledger_problems(rec) == ["followup: missing 'idea_id'"]
    assert SC.ledger_problems({**rec, "idea_id": "IDEA-001"}) == []


def test_adversarial_finding_with_category_is_a_valid_ledger_record(tmp_path, monkeypatch):
    """SPEC-0202: a reviewer `finding` carrying `category` still satisfies the ledger schema."""
    adv = {"persona": "adversarial_reviewer", "verdict": {
        "verdict": "fix", "score": 20, "fix_brief": "escape the name",
        "findings": [{"severity": "must_fix", "category": "path_traversal", "title": "escape",
                      "detail": "joined unchecked", "trigger": "when name has ../",
                      "files": ["feature.txt"], "lines": ["feature.txt:1"]}]}, "cost_usd": 0.3}
    ok = dict(adv, verdict=dict(adv["verdict"], verdict="pass", score=90, findings=[],
                                fix_brief=""))
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []),
                   profile={"reviewers": ["adversarial_reviewer"]})
    from wt.agentic.runner import FakeRunner
    cid = added(cfg, monkeypatch)
    entries = [impl_done(), adv, impl_done(content="better\n"), ok]
    runner = FakeRunner(script(tmp_path, entries, "adv.json"))
    for _ in range(12):
        Q.tick(cfg, runner=runner)
        if S.read_state(cfg, cid)["state"] in ("MERGED", "PARKED"):
            break
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    ledger = S.read_ledger(cfg, cid)
    assert [p for rec in ledger for p in SC.ledger_problems(rec)] == []
    f = [e for e in ledger if e["type"] == "finding" and e["persona"] == "adversarial_reviewer"]
    assert len(f) == 1 and f[0]["category"] == "path_traversal"
    assert f[0]["gate"] is None and f[0]["ac"] is None
    assert f[0]["detail"] == "joined unchecked\ntrigger: when name has ../"




def test_open_event_without_sizing_keys_still_validates(tmp_path):      # SPEC-0219
    cfg = {"data_dir": str(tmp_path / "data")}
    S.create_campaign(cfg, {"id": "C1", "spec_id": "SPEC-0001", "state": "QUEUED"})
    ev = S.read_ledger(cfg, "C1")[0]
    assert ev["type"] == "open" and "size" not in ev and SC.ledger_problems(ev) == []
    S.create_campaign(cfg, {"id": "C2", "spec_id": "SPEC-0001", "state": "QUEUED",
                            "sizing": {"size": "m", "tone": "high-end", "source": "review",
                                       "reason": "r"}})
    ev = S.read_ledger(cfg, "C2")[0]
    assert (ev["size"], ev["tone"], ev["profile_source"]) == ("m", "high-end", "review")
    assert SC.ledger_problems(ev) == []
    assert "size" not in SC.EVENT_KEYS["open"]


def test_rebase_resolved_event_is_registered_and_requires_its_keys():             # SPEC-0221
    keys = ("run_id", "target_sha", "conflicts", "resolutions", "summary", "head",
            "extra_commits", "absorbed_commits", "resolver_commit")           # SPEC-0252
    assert SC.EVENT_KEYS["rebase_resolved"] == keys
    rec = {"ts": "t", "seq": 1, "campaign": "c", "type": "rebase_resolved", "by": "queue",
           **{k: "x" for k in keys}}
    assert SC.ledger_problems(rec) == []
    del rec["head"]
    assert SC.ledger_problems(rec) == ["rebase_resolved: missing 'head'"]


def test_open_event_profile_sources_is_optional(tmp_path):                       # SPEC-0238
    cfg = {"data_dir": str(tmp_path / "data")}
    S.create_campaign(cfg, {"id": "C1", "spec_id": "SPEC-0001", "state": "QUEUED"})
    ev = S.read_ledger(cfg, "C1")[0]
    assert "profile_sources" not in ev and SC.ledger_problems(ev) == []      # an old ledger
    S.create_campaign(cfg, {"id": "C2", "spec_id": "SPEC-0001", "state": "QUEUED",
                            "profile_sources": ["builtin:balanced.yaml"]})
    ev = S.read_ledger(cfg, "C2")[0]
    assert ev["profile_sources"] == ["builtin:balanced.yaml"] and SC.ledger_problems(ev) == []


def test_runs_and_run_start_carry_the_profile(tmp_path, monkeypatch):           # SPEC-0238
    cfg, cid = _run_to_idle(tmp_path, monkeypatch, [impl_done(), review()])
    state = S.read_state(cfg, cid)
    want = ["builtin:balanced.yaml", "config.yaml:agentic.profiles.balanced"]   # make_cfg's delta
    assert state["profile_sources"] == want
    (opened,) = S.read_ledger(cfg, cid, types=["open"])
    assert opened["profile_sources"] == state["profile_sources"]
    starts = S.read_ledger(cfg, cid, types=["run.start"])
    assert starts and all(e["profile"] == "balanced" for e in starts)
    for d in (S.campaign_dir(cfg, cid) / "runs").iterdir():
        if d.is_dir() and (d / "run.json").is_file():
            rec = json.loads((d / "run.json").read_text())
            assert rec["profile"] == "balanced"
            assert rec["profile_sources"] == want
            assert SC.record_problems(rec, SC.RUN_KEYS, SC.RUN_SCHEMA) == []


def test_outbound_pulled_event_is_registered_and_requires_its_keys():            # SPEC-0297
    keys = ("spec_id", "status", "outbox_path", "idea_id", "from", "to")
    assert SC.EVENT_KEYS["outbound_pulled"] == keys
    rec = {"ts": "t", "seq": 1, "campaign": "c", "type": "outbound_pulled", "by": "queue",
           **{k: None for k in keys}}
    assert SC.ledger_problems(rec) == []
    del rec["idea_id"]
    assert SC.ledger_problems(rec) == ["outbound_pulled: missing 'idea_id'"]
