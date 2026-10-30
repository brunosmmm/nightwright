"""SPEC-0301: the conductor decision log, `wt queue decision` / `decisions`, park coverage."""
import datetime as dt
import json

import pytest
from click.testing import CliRunner

from wt.agentic import decisions as D
from wt.agentic import events as E
from wt.agentic import queue as Q
from wt.agentic import schemas as SC
from wt.agentic import store as S
from wt.cli import cli

UTC = dt.timezone.utc


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "data").mkdir()
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "_root": str(tmp_path / "projects"), "_tz": UTC, "timezone": "UTC", "agentic": {}}


@pytest.fixture
def clock(monkeypatch):
    """`store.now_iso` ticks one second per call, so ledger and log stamps are ordered."""
    t = [dt.datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)]
    orig = S.now_iso

    def tick(now=None):
        if now is not None:
            return orig(now)
        t[0] += dt.timedelta(seconds=1)
        return orig(t[0])

    monkeypatch.setattr(S, "now_iso", tick)
    return t


def mk(cfg, cid, state="IMPLEMENTING", **extra):
    S.create_campaign(cfg, {"id": cid, "spec_id": f"SPEC-{cid}", "project": "P", "kind": "spec",
                            "profile": "x", "state": state, "cost": {"total_usd": 1.0},
                            "counters": {}, **extra})
    return cid


def park(cfg, cid, reason="loop_exhausted", detail="d"):
    """Park `cid` (state and ledger event)."""
    ev = S.append_event(cfg, cid, "park", SC.with_defaults(
        "park", {"reason": reason, "detail": detail, "resume_to": "IMPLEMENTING"}))
    st = S.read_state(cfg, cid)
    st.update(state="PARKED", park={"reason": reason, "detail": detail,
                                    "resume_to": "IMPLEMENTING", "since": ev["ts"]})
    S.write_state(cfg, cid, st)
    return ev


def resume(cfg, cid):
    S.append_event(cfg, cid, "resume", SC.with_defaults("resume", {"to": "IMPLEMENTING"}))
    st = S.read_state(cfg, cid)
    st.update(state="IMPLEMENTING", park=None)
    S.write_state(cfg, cid, st)


def decide(cfg, cid=None, kind="resume", rule="parked.loop_exhausted", why="because", **kw):
    return D.record(cfg, kind=kind, rule=rule, rationale=why, campaign=cid, **kw)


def view(cfg, cid):
    import datetime
    return Q.park_view(cfg, S.read_state(cfg, cid), datetime.datetime.now().astimezone(),
                       E.settings(cfg))


@pytest.fixture
def run(cfg, monkeypatch):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    return lambda *args: CliRunner().invoke(cli, ["queue", *args])


# ---- record ----------------------------------------------------------------------------------

@pytest.mark.parametrize("campaign, flags", [
    (None, ["--kind", "bogus", "--rule", "runner_dead"]),
    (None, ["--kind", "resume", "--rule", "Bad-Rule"]),
    (None, ["--kind", "resume", "--rule", "a.b.c"]),
    (None, ["--kind", "resume", "--rule", "runner_dead", "--by", "nobody"]),
    (None, ["--kind", "resume", "--rule", "runner_dead", "--event", "999"]),
    ("ghost", ["--kind", "resume", "--rule", "runner_dead"]),
])
def test_decision_validation(cfg, run, campaign, flags):
    mk(cfg, "C1")
    r = run("decision", *([campaign] if campaign else []), *flags, "why")
    assert r.exit_code == 1, r.output
    assert not D.log_path(cfg).exists()
    assert not [e for e in S.read_ledger(cfg, "C1") if e["type"] == "note"]


def test_decision_writes_log_and_ledger_note(cfg, clock):
    mk(cfg, "C1")
    park(cfg, "C1")
    assert view(cfg, "C1")["decided"] is False
    a = decide(cfg, "C1", why="first", action="resume it")
    b = decide(cfg, "C1", kind="wait", rule="run_overdue", why="second")
    assert (a["seq"], b["seq"]) == (1, 2) and a["schema"] == D.SCHEMA
    assert a["spec_id"] == "SPEC-C1" and a["action"] == "resume it" and a["by"] == "conductor"
    notes = [e for e in S.read_ledger(cfg, "C1") if e["type"] == "note"]
    assert notes[0]["decision"] == {"seq": 1, "kind": "resume", "rule": "parked.loop_exhausted"}
    assert notes[0]["text"] == "decision resume (parked.loop_exhausted): first"
    assert notes[0]["author"] == "conductor"
    assert view(cfg, "C1")["decided"] is True
    assert [r["seq"] for r in D.read(cfg)] == [1, 2]
    assert [r["seq"] for r in D.read(cfg, campaign="C1")] == [1, 2]


def test_queue_level_decision_no_campaign(cfg, run):
    r = run("decision", "--kind", "restart_runner", "--rule", "runner_dead", "--by", "queue",
            "runner dead")
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] is None and rec["spec_id"] is None and rec["by"] == "queue"


def test_decision_with_known_event_and_positional_campaign(cfg, run):
    mk(cfg, "C1")
    E.append(cfg, "message", by="t", data={"msg_id": "1", "from": "x", "kind": "info",
                                             "text": "t"})
    r = run("decision", "C1", "--kind", "no_action", "--rule", "message.answer", "--event", "1",
            "--by", "steward:Meta", "applied")
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["event_id"] == 1 and rec["by"] == "steward:Meta" and rec["campaign"] == "C1"


def test_record_write_order_and_by(cfg, monkeypatch):
    mk(cfg, "C1")
    decide(cfg, "C1")
    before = D.log_path(cfg).read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(S, "append_event", boom)
    with pytest.raises(OSError):
        decide(cfg, "C1")
    assert D.log_path(cfg).read_text() == before
    monkeypatch.undo()
    for bad in ("someone", "steward:", "Conductor"):
        with pytest.raises(ValueError):
            decide(cfg, "C1", by=bad)
    for ok in ("queue", "steward:Meta-Tools"):
        decide(cfg, "C1", by=ok)
    now = dt.datetime(2026, 10, 30, 12, tzinfo=UTC)
    assert D.parse_since("2h", now) == now - dt.timedelta(hours=2)
    assert D.parse_since("1d", now) == now - dt.timedelta(days=1)
    assert D.parse_since("2026-10-29").date() == dt.date(2026, 10, 29)
    assert D.parse_since("2026-10-29T10:00:00+00:00").hour == 10
    with pytest.raises(ValueError):
        D.parse_since("yesterday")


def test_open_dispatches(cfg, clock):
    mk(cfg, "C1")
    mk(cfg, "C2")
    decide(cfg, "C1", kind="dispatch", rule="run_overdue", why="check run")
    decide(cfg, "C1", kind="resume", rule="run_overdue", why="done")
    decide(cfg, "C2", kind="dispatch", rule="run_overdue", why="check run")
    decide(cfg, None, kind="dispatch", rule="runner_dead", why="look")
    assert [(r["campaign"], r["seq"]) for r in D.open_dispatches(D.read(cfg))] == [("C2", 3),
                                                                                   (None, 4)]
    decide(cfg, None, kind="restart_runner", rule="runner_dead", why="restarted")
    assert [r["campaign"] for r in D.open_dispatches(D.read(cfg))] == ["C2"]


# ---- park coverage ---------------------------------------------------------------------------

def test_park_coverage(cfg, clock):
    decide(cfg, None, kind="no_action", rule="wt_broken", why="log starts")
    for cid in ("A", "B", "C"):
        mk(cfg, cid)
    park(cfg, "A")
    decide(cfg, "A")
    park(cfg, "B")
    decide(cfg, "B")
    decide(cfg, "C", why="decided before the park")
    park(cfg, "C")
    cov = D.park_coverage(cfg)
    assert cov["parks"] == 3 and cov["covered"] == 2
    assert [m["campaign"] for m in cov["missing"]] == ["C"]
    assert cov["ratio"] == pytest.approx(2 / 3)


def test_park_coverage_one_decision_covers_one_park(cfg, clock):
    decide(cfg, None, kind="no_action", rule="wt_broken", why="log starts")
    mk(cfg, "D")
    park(cfg, "D")
    resume(cfg, "D")
    park(cfg, "D")
    d = decide(cfg, "D")
    cov = D.park_coverage(cfg)
    assert cov["parks"] == 2 and cov["covered"] == 1 and len(cov["missing"]) == 1
    assert [x["decision"] and x["decision"]["seq"] for x in cov["items"]] == [None, d["seq"]]


def test_park_coverage_window(cfg, clock):
    mk(cfg, "E")
    old = park(cfg, "E")
    resume(cfg, "E")
    assert D.park_coverage(cfg) == {"parks": 0, "covered": 0, "missing": [], "ratio": None,
                                    "items": []}
    decide(cfg, None, kind="no_action", rule="wt_broken", why="log starts")
    park(cfg, "E")
    cov = D.park_coverage(cfg)
    assert cov["parks"] == 1 and old["ts"] not in [m["ts"] for m in cov["missing"]]
    cov = D.park_coverage(cfg, since=D.parse_ts("2026-10-30T00:00:00+00:00"))
    assert cov["parks"] == 2


def test_cli_decisions_listing_and_parks(cfg, clock, run):
    decide(cfg, None, kind="no_action", rule="wt_broken", why="start")
    mk(cfg, "A")
    mk(cfg, "B")
    park(cfg, "A")
    decide(cfg, "A", why="decided a")
    park(cfg, "B")
    r = run("decisions", "--json")
    assert r.exit_code == 0, r.output
    assert [x["seq"] for x in json.loads(r.output)["decisions"]] == [1, 2]
    r = run("decisions", "--campaign", "A", "--json")
    assert [x["campaign"] for x in json.loads(r.output)["decisions"]] == ["A"]
    r = run("decisions", "--since", "2h", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["decisions"] == []   # clock is in the past
    r = run("decisions", "--since", "2026-10-30", "--tail", "1", "--json")
    assert [x["seq"] for x in json.loads(r.output)["decisions"]] == [2]
    r = run("decisions", "--since", "nonsense")
    assert r.exit_code == 1
    r = run("decisions", "--parks", "--json")
    cov = json.loads(r.output)
    assert (cov["parks"], cov["covered"]) == (2, 1) and cov["missing"][0]["campaign"] == "B"
    r = run("decisions", "--parks")
    assert "missing" in r.output and "1/2 parks decided (50%)" in r.output
    r = run("decisions")
    assert "wt_broken" in r.output


def test_cli_decisions_parks_filtered_by_campaign(cfg, clock, run):
    for cid in ("A", "B", "C"):
        mk(cfg, cid)
        park(cfg, cid)
    decide(cfg, "A", why="a")
    decide(cfg, "C", why="c")
    cov = json.loads(run("decisions", "--parks", "--since", "2026-10-30", "--campaign", "B", "--json").output)
    assert (cov["parks"], cov["covered"], cov["ratio"]) == (1, 0, 0.0)
    assert [m["campaign"] for m in cov["missing"]] == ["B"]
    cov = json.loads(run("decisions", "--parks", "--since", "2026-10-30", "--campaign", "A", "--json").output)
    assert (cov["parks"], cov["covered"], cov["ratio"]) == (1, 1, 1.0) and cov["missing"] == []
    cov = json.loads(run("decisions", "--parks", "--since", "2026-10-30", "--campaign", "nobody", "--json").output)
    assert (cov["parks"], cov["covered"], cov["ratio"]) == (0, 0, None)
    r = run("decisions", "--parks", "--since", "2026-10-30", "--campaign", "B")
    assert "0/1 parks decided (0%)" in r.output and "A " not in r.output


def test_cli_decisions_text_does_not_read_rationale_as_markup(cfg, clock, run):
    mk(cfg, "C1")
    park(cfg, "C1")
    r = run("decision", "C1", "--kind", "resume", "--rule", "parked.loop_exhausted",
            "see [/wt-conduct] row and [x] more")
    assert r.exit_code == 0, r.output
    r = run("decisions")
    assert r.exit_code == 0, r.output
    assert "see [/wt-conduct] row and [x] more" in r.output
    r = run("decisions", "--parks", "--since", "2026-10-30")
    assert r.exit_code == 0 and "1/1 parks decided (100%)" in r.output


def test_decision_with_only_a_campaign_id_is_rejected(cfg, run):
    mk(cfg, "SPEC-0123-20261030-abcd")
    r = run("decision", "SPEC-0123-20261030-abcd", "--kind", "resume", "--rule", "parked.cost_cap")
    assert r.exit_code == 1 and "is a campaign id, not a rationale" in r.output
    assert not D.log_path(cfg).exists()
    r = run("decision", "--kind", "resume", "--rule", "parked.cost_cap", "a plain rationale")
    assert r.exit_code == 0 and D.read(cfg)[0]["campaign"] is None
