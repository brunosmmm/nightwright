"""SPEC-0318: a park, resume or abandon by the conductor carries its decision record."""
import datetime as dt
import json

import pytest
from click.testing import CliRunner

from nightwright.agentic import decisions as D
from nightwright.agentic import mvp_report as MR
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.cli import cli

pytestmark = pytest.mark.fast

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    c = {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
         "_root": str(tmp_path / "projects"), "_tz": UTC, "timezone": "UTC", "agentic": {}}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: c)
    return c


@pytest.fixture
def clock(monkeypatch):
    """One tick per second would make seq-vs-ts ordering invisible; this clock is FROZEN, so a
    record written before the park has the same ts: only the ledger seq can tell."""
    orig = S.now_iso
    monkeypatch.setattr(S, "now_iso", lambda now=None: orig(now) if now is not None else orig(T0))


def mk(cfg, cid="C1", state="IMPLEMENTING"):
    S.create_campaign(cfg, {"id": cid, "spec_id": f"SPEC-{cid}", "project": "P", "kind": "spec",
                            "profile": "x", "state": state, "cost": {"total_usd": 1.0},
                            "counters": {}})
    return cid


def wt(*args):
    return CliRunner().invoke(cli, ["queue", *args])


def snapshot(cfg, cid):
    return (S.read_state(cfg, cid), S.read_ledger(cfg, cid),
            D.log_path(cfg).read_text() if D.log_path(cfg).exists() else "")


CONDUCTOR = ["--by", "conductor"]
GOOD = ["--decision", "why", "--rule", "message.request"]


@pytest.mark.parametrize("extra", [[], ["--decision", "why"], ["--rule", "message.request"],
                                   ["--decision", "why", "--rule", "BAD RULE"],
                                   ["--decision", "why", "--rule", "message.request",
                                    "--kind", "nope"]])
@pytest.mark.parametrize("yes", [[], ["--yes"]])
def test_conductor_park_requires_decision(cfg, clock, extra, yes):
    cid = mk(cfg)
    before = snapshot(cfg, cid)
    r = wt("park", cid, "--reason", "R", *CONDUCTOR, *extra, *yes)
    assert r.exit_code == 1, r.output
    assert snapshot(cfg, cid) == before


@pytest.mark.parametrize("verb,state", [("park", "IMPLEMENTING"), ("resume", "PARKED"),
                                        ("abandon", "PARKED")])
@pytest.mark.parametrize("extra", [["--decision", "why"], ["--rule", "message.request"],
                                   ["--kind", "wait"]])
def test_human_with_record_options_is_refused(cfg, clock, verb, state, extra):
    cid = mk(cfg, state=state)
    if state == "PARKED":
        st = S.read_state(cfg, cid)
        st["park"] = {"reason": "human", "detail": "d", "since": S.now_iso(),
                      "resume_to": "IMPLEMENTING"}
        S.write_state(cfg, cid, st)
    before = snapshot(cfg, cid)
    args = [verb, cid, "--by", "human", *extra, "--yes"]
    if verb == "park":
        args += ["--reason", "R"]
    r = wt(*args)
    assert r.exit_code == 1 and "needs no decision record" in r.output
    assert snapshot(cfg, cid) == before
    assert all(x["by"] != "human" for x in D.read(cfg))


def test_human_park_unchanged(cfg, clock):
    cid = mk(cfg)
    r = wt("park", cid, "--reason", "R", "--by", "human", "--yes")
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and "decision_request" not in st["park"]
    assert D.read(cfg) == []


def _park_events(cfg, cid):
    return S.read_ledger(cfg, cid, types=["park"])


def _decision_note(cfg, cid, rec):
    return next(e for e in S.read_ledger(cfg, cid, types=["note"])
                if (e.get("decision") or {}).get("seq") == rec["seq"])


def test_conductor_park_writes_record_after_park(cfg, clock):
    cid = mk(cfg)
    r = wt("park", cid, "--reason", "R", *CONDUCTOR, *GOOD, "--yes", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["decision"]["rule"] == "message.request"
    (rec,) = D.read(cfg, campaign=cid)
    assert (rec["by"], rec["kind"], rec["rule"], rec["rationale"]) == \
        ("conductor", "wait", "message.request", "why")
    park, note = _park_events(cfg, cid)[-1], _decision_note(cfg, cid, rec)
    assert note["seq"] > park["seq"]               # ts has one-second resolution: compare seq
    assert D.parse_ts(rec["ts"]) >= D.parse_ts(park["ts"])
    assert "decision_request" not in park
    assert "decision_request" not in S.read_state(cfg, cid)["park"]
    cov = D.park_coverage(cfg, since=T0 - dt.timedelta(days=1))
    assert (cov["parks"], cov["covered"]) == (1, 1)


def test_deferred_park_records_after_tick_applies_it(cfg, clock, monkeypatch):
    cid = mk(cfg)
    monkeypatch.setattr(Q.L, "is_held", lambda c: True)
    r = wt("park", cid, "--reason", "R", *CONDUCTOR, *GOOD, "--yes")
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and st["park_requested"]["decision_request"]["by"] \
        == "conductor"
    assert D.read(cfg) == []
    step = Q.plan_step(S.read_state(cfg, cid), {})
    Q._park_row(cfg, S.read_state(cfg, cid), step)           # the tick applies the park
    assert D.read(cfg) == []                                  # recorded later in the same tick
    assert Q.settle_decision_requests(cfg) == 1
    (rec,) = D.read(cfg, campaign=cid)
    park = _park_events(cfg, cid)[-1]
    assert _decision_note(cfg, cid, rec)["seq"] > park["seq"]
    assert D.park_coverage(cfg, since=T0 - dt.timedelta(days=1))["covered"] == 1


def test_crash_between_park_and_record_is_repaired(cfg, clock, monkeypatch):
    cid = mk(cfg)
    real = D.record
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise OSError("disk full")
    monkeypatch.setattr(D, "record", boom)
    assert wt("park", cid, "--reason", "R", *CONDUCTOR, *GOOD, "--yes").exit_code == 0
    assert calls and S.read_state(cfg, cid)["park"]["decision_request"]["rule"] == "message.request"
    monkeypatch.setattr(D, "record", real)
    assert Q.settle_decision_requests(cfg, "other-campaign") == 0     # scoped
    assert Q.settle_decision_requests(cfg) == 1
    assert len(D.read(cfg, campaign=cid)) == 1
    assert "decision_request" not in S.read_state(cfg, cid)["park"]
    assert Q.settle_decision_requests(cfg) == 0


def test_request_dropped_when_resumed_meanwhile_by_human(cfg, clock, monkeypatch):
    cid = mk(cfg)
    real = D.record
    monkeypatch.setattr(D, "record", lambda *a, **kw: (_ for _ in ()).throw(OSError("x")))
    assert wt("park", cid, "--reason", "R", *CONDUCTOR, *GOOD, "--yes").exit_code == 0
    monkeypatch.setattr(D, "record", real)
    assert wt("resume", cid, "--by", "human", "--yes").exit_code == 0
    assert Q.settle_decision_requests(cfg) == 0
    assert D.read(cfg) == []
    assert "decision_request_dropped" in Q.runner_journal_path(cfg).read_text()


def _parked(cfg, cid="C1", **park):
    mk(cfg, cid)
    st = S.read_state(cfg, cid)
    ev = S.append_event(cfg, cid, "park", {"reason": "loop_exhausted", "detail": "d",
                                           "resume_to": "IMPLEMENTING"})
    st.update(state="PARKED", park={"reason": "loop_exhausted", "detail": "d",
                                    "resume_to": "IMPLEMENTING", "since": ev["ts"], **park})
    S.write_state(cfg, cid, st)
    return ev


@pytest.mark.parametrize("verb", ["resume", "abandon"])
@pytest.mark.parametrize("actor", ["conductor", "steward:P"])
def test_resume_abandon_need_covering_decision(cfg, monkeypatch, verb, actor):
    now = [T0]
    orig = S.now_iso
    monkeypatch.setattr(S, "now_iso", lambda n=None: orig(n) if n else orig(now[0]))
    monkeypatch.setattr("nightwright.agentic.cli_queue.M.validate_address", lambda c, a: a)
    final = {"resume": "IMPLEMENTING", "abandon": "ABANDONED"}[verb]
    old = D.record(cfg, kind="wait", rule="wt_broken", rationale="older", by="conductor")
    now[0] += dt.timedelta(seconds=10)
    _parked(cfg)
    before = snapshot(cfg, "C1")
    for dry in ([], ["--yes"]):
        r = wt(verb, "C1", "--by", actor, *dry)
        assert r.exit_code == 1 and "decision" in r.output, r.output
    assert snapshot(cfg, "C1") == before
    D.record(cfg, kind="wait", rule="wt_broken", rationale="before", by="conductor",
             campaign="C1")                                   # same ts as the park: covers it
    # a record older than the park does not cover it
    st = S.read_state(cfg, "C1")
    now[0] += dt.timedelta(seconds=10)
    st["park"]["since"] = S.now_iso()
    S.write_state(cfg, "C1", st)
    S.append_event(cfg, "C1", "park", {"reason": "loop_exhausted", "detail": "d2",
                                       "resume_to": "IMPLEMENTING"})
    assert wt(verb, "C1", "--by", actor, "--yes").exit_code == 1
    assert S.read_state(cfg, "C1")["state"] == "PARKED"
    # --decision --rule: record first, then the transition
    r = wt(verb, "C1", "--by", actor, "--decision", "go", "--rule", "parked.loop_exhausted",
           "--yes")
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, "C1")["state"] == final
    recs = D.read(cfg, campaign="C1")
    assert recs[-1]["kind"] == verb and recs[-1]["by"] == actor and old["seq"] < recs[-1]["seq"]
    # a covering record already present: no options needed
    now[0] += dt.timedelta(seconds=10)
    _parked(cfg, "C2")
    D.record(cfg, kind="wait", rule="wt_broken", rationale="decided", by=actor, campaign="C2")
    assert wt(verb, "C2", "--by", actor, "--yes").exit_code == 0
    # human and park.auto are exempt
    _parked(cfg, "C3")
    assert wt(verb, "C3", "--by", "human", "--yes").exit_code == 0
    _parked(cfg, "C4", auto={"rule": "parked.base_moving"})
    assert wt(verb, "C4", "--by", actor, "--yes").exit_code == 0


def test_resume_dry_run_json_carries_decision(cfg, clock):
    _parked(cfg)
    r = wt("resume", "C1", *CONDUCTOR, *GOOD, "--json")
    d = json.loads(r.output)
    assert r.exit_code == 0 and d["written"] is False and d["decision"]["kind"] == "resume"
    assert D.read(cfg) == []


def test_e4_over_conductor_parks(cfg, monkeypatch):
    now = [T0]
    orig = S.now_iso
    monkeypatch.setattr(S, "now_iso", lambda n=None: orig(n) if n else orig(now[0]))
    monkeypatch.setattr(Q.L, "is_held", lambda c: False)
    ids = []
    for i in range(5):
        ids.append(mk(cfg, f"E{i}"))
        now[0] += dt.timedelta(seconds=5)
    # 0: conductor hold; 1: deferred hold; 2: escalated park resumed with --decision;
    # 3: auto park (queue decision); 4: human park
    assert wt("park", ids[0], "--reason", "h", *CONDUCTOR, *GOOD, "--yes").exit_code == 0
    now[0] += dt.timedelta(seconds=5)
    monkeypatch.setattr(Q.L, "is_held", lambda c: True)
    assert wt("park", ids[1], "--reason", "h", *CONDUCTOR, *GOOD, "--yes").exit_code == 0
    Q._park_row(cfg, S.read_state(cfg, ids[1]), Q.plan_step(S.read_state(cfg, ids[1]), {}))
    Q.settle_decision_requests(cfg)
    monkeypatch.setattr(Q.L, "is_held", lambda c: False)
    now[0] += dt.timedelta(seconds=5)
    Q._park(cfg, S.read_state(cfg, ids[2]), "triage_escalated", "d", "IMPLEMENTING")
    now[0] += dt.timedelta(seconds=5)
    assert wt("resume", ids[2], *CONDUCTOR, "--decision", "go", "--rule", "parked.loop_exhausted",
              "--yes").exit_code == 0
    now[0] += dt.timedelta(seconds=5)
    Q._park(cfg, S.read_state(cfg, ids[3]), "cost_cap", "d", "IMPLEMENTING", by="queue")
    D.record(cfg, campaign=ids[3], kind="wait", rule="parked.cost_cap", rationale="r", by="queue")
    assert wt("park", ids[4], "--reason", "h", "--by", "human", "--yes").exit_code == 0
    now[0] += dt.timedelta(seconds=5)
    D.record(cfg, campaign=ids[4], kind="wait", rule="parked.human", rationale="r", by="conductor")
    crit, bad = MR.e4(cfg, [{"campaign": i} for i in ids], T0 - dt.timedelta(days=1),
                      now[0] + dt.timedelta(days=1), {"e4": 1.0})
    assert bad == [], bad
