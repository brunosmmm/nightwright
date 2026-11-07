"""SPEC-0301: the conductor decision log, `nw queue decision` / `decisions`, park coverage."""
import datetime as dt
import json
import os

import pytest
from click.testing import CliRunner

from nightwright.agentic import decisions as D
from nightwright.agentic import events as E
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.cli import cli

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
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return lambda *args: CliRunner().invoke(cli, ["queue", *args])


# ---- record ----------------------------------------------------------------------------------

@pytest.mark.parametrize("campaign, flags", [
    (None, ["--kind", "bogus", "--rule", "runner_dead"]),
    (None, ["--kind", "resume", "--rule", "Bad-Rule"]),
    (None, ["--kind", "resume", "--rule", "a.b.c.d"]),
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
            "see [/nw-conduct] row and [x] more")
    assert r.exit_code == 0, r.output
    r = run("decisions")
    assert r.exit_code == 0, r.output
    assert "see [/nw-conduct] row and [x] more" in r.output
    r = run("decisions", "--parks", "--since", "2026-10-30")
    assert r.exit_code == 0 and "1/1 parks decided (100%)" in r.output


def test_decision_with_only_a_campaign_id_is_rejected(cfg, run):
    mk(cfg, "SPEC-0123-20261030-abcd")
    r = run("decision", "SPEC-0123-20261030-abcd", "--kind", "resume", "--rule", "parked.cost_cap")
    assert r.exit_code == 1 and "is a campaign id, not a rationale" in r.output
    assert not D.log_path(cfg).exists()
    r = run("decision", "--kind", "resume", "--rule", "parked.cost_cap", "a plain rationale")
    assert r.exit_code == 0 and D.read(cfg)[0]["campaign"] is None


# ---- SPEC-0431: campaign stamps ---------------------------------------------------------------

CA = "SPEC-0001-20261101-abcd"
CB = "SPEC-0002-20261102-0123"


def _dec(run, *words, kind="wait", rule="parked.x", extra=()):
    return run("decision", "--kind", kind, "--rule", rule, *extra, *words)


def test_record_campaign_from_validation(cfg):
    mk(cfg, "C1")
    rec = decide(cfg, "C1", campaign_from="text")
    assert rec["campaign_from"] == "text"
    assert "campaign_from" not in decide(cfg, "C1")
    assert decide(cfg, "C1", campaign_from="event")["campaign_from"] == "event"
    before = D.log_path(cfg).read_bytes()
    for cid, cf in (("C1", "txt"), ("C1", "re"), (None, "text")):
        with pytest.raises(ValueError):
            decide(cfg, cid, campaign_from=cf)
    assert D.log_path(cfg).read_bytes() == before
    assert len([e for e in S.read_ledger(cfg, "C1") if e["type"] == "note"]) == 3


def test_cli_text_inference_stamps_and_notes(cfg, run):
    mk(cfg, CA)
    r = _dec(run, f"waiting on {CA} to land")
    assert r.exit_code == 0, r.output
    assert f"on {CA} (campaign inferred from text)" in r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CA and rec["spec_id"] == f"SPEC-{CA}"
    assert rec["campaign_from"] == "text"
    notes = [e for e in S.read_ledger(cfg, CA) if e["type"] == "note"]
    assert notes[0]["decision"]["seq"] == rec["seq"]


def test_cli_text_inference_from_action_only(cfg, run):
    mk(cfg, CA)
    r = _dec(run, "no campaign in the rationale", extra=("--action", f"nudged {CA}"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CA and rec["campaign_from"] == "text"
    assert rec["spec_id"] == f"SPEC-{CA}"
    notes = [e for e in S.read_ledger(cfg, CA) if e["type"] == "note"]
    assert notes[0]["decision"]["seq"] == rec["seq"]


def test_cli_explicit_campaign_wins_and_has_no_campaign_from(cfg, run):
    mk(cfg, CA)
    mk(cfg, CB)
    r = _dec(run, CA, f"mentions {CB}")
    assert r.exit_code == 0, r.output
    assert "inferred" not in r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CA and "campaign_from" not in rec
    assert not [e for e in S.read_ledger(cfg, CB) if e["type"] == "note"]


def test_cli_rationale_and_action_naming_different_campaigns(cfg, run):
    """Campaign A in the rationale and campaign B in --action is ambiguous: stamp neither."""
    mk(cfg, CA)
    mk(cfg, CB)
    r = _dec(run, f"waiting on {CA}", extra=("--action", f"nudged {CB}"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] is None and "campaign_from" not in rec
    for c in (CA, CB):
        assert not [e for e in S.read_ledger(cfg, c) if e["type"] == "note"]


def test_cli_digit_leading_outbound_campaign_is_stamped(cfg, run):
    from nightwright.specs import _proj_code
    code = _proj_code("3M")
    cid = S.new_campaign_id(cfg, f"{code}-0001", now=dt.datetime(2026, 11, 6))
    mk(cfg, cid)
    r = _dec(run, f"waiting on {cid} to land")
    assert r.exit_code == 0, r.output
    assert f"on {cid} (campaign inferred from text)" in r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == cid and rec["campaign_from"] == "text"
    assert [e for e in S.read_ledger(cfg, cid) if e["type"] == "note"]


def test_cli_ambiguous_or_none_is_queue_level(cfg, run):
    mk(cfg, CA)
    mk(cfg, CB)
    assert _dec(run, f"{CA} and {CB}").exit_code == 0
    assert _dec(run, "no ids at all").exit_code == 0
    for rec in D.read(cfg):
        assert rec["campaign"] is None and "campaign_from" not in rec
    for c in (CA, CB):
        assert not [e for e in S.read_ledger(cfg, c) if e["type"] == "note"]


def test_cli_empty_explicit_campaign_conflicts_with_no_campaign(cfg, run):
    """An explicit empty CAMPAIGN is still explicit: `--no-campaign` is a usage error."""
    mk(cfg, CA)
    assert _dec(run, "seed, no campaign").exit_code == 0
    before = D.log_path(cfg).read_bytes()
    ledger = (S.campaign_dir(cfg, CA) / "ledger.jsonl").read_bytes()
    r = _dec(run, "", f"about {CA}", extra=("--no-campaign",))
    assert r.exit_code == 2 and "conflicts" in r.output
    assert D.log_path(cfg).read_bytes() == before
    assert (S.campaign_dir(cfg, CA) / "ledger.jsonl").read_bytes() == ledger


def test_cli_no_campaign_flag(cfg, run):
    mk(cfg, CA)
    r = _dec(run, f"about {CA}", extra=("--no-campaign",))
    assert r.exit_code == 0, r.output
    assert D.read(cfg)[0]["campaign"] is None
    n = len(D.read(cfg))
    r = _dec(run, CA, "x", extra=("--no-campaign",))
    assert r.exit_code != 0 and "--no-campaign" in r.output
    assert len(D.read(cfg)) == n


def test_cli_event_campaign_outranks_text(cfg, run):
    mk(cfg, CA)
    mk(cfg, CB)
    E.append(cfg, "message", campaign=CA, by="t",
             data={"msg_id": "1", "from": "x", "kind": "info", "text": "t"})
    r = _dec(run, f"this is really about {CB}", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    assert "(campaign inferred from event)" in r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CA and rec["campaign_from"] == "event" and rec["event_id"] == 1
    assert [e for e in S.read_ledger(cfg, CA) if e["type"] == "note"]
    assert not [e for e in S.read_ledger(cfg, CB) if e["type"] == "note"]
    # an explicit CAMPAIGN still wins over the event
    r = _dec(run, CB, "explicit", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[1]
    assert rec["campaign"] == CB and "campaign_from" not in rec


def test_cli_event_without_campaign_falls_through_to_text(cfg, run):
    mk(cfg, CB)
    E.append(cfg, "message", by="t", data={"msg_id": "1", "from": "x", "kind": "info",
                                             "text": "t"})
    r = _dec(run, f"about {CB}", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CB and rec["campaign_from"] == "text"


def test_cli_no_campaign_flag_blocks_event_inheritance(cfg, run):
    mk(cfg, CA)
    E.append(cfg, "message", campaign=CA, by="t",
             data={"msg_id": "1", "from": "x", "kind": "info", "text": "t"})
    r = _dec(run, f"about {CA}", extra=("--event", "1", "--no-campaign"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] is None and "campaign_from" not in rec and rec["event_id"] == 1
    assert not [e for e in S.read_ledger(cfg, CA) if e["type"] == "note"]


def _msg_event(cfg, n, campaign=None):
    return E.append(cfg, "message", campaign=campaign, by="t",
                    data={"msg_id": str(n), "from": "x", "kind": "info", "text": "t"})


def test_cli_event_lookup_picks_event_n_among_several(cfg, run):
    mk(cfg, CA)
    mk(cfg, CB)
    _msg_event(cfg, 1, CB)
    _msg_event(cfg, 2, CA)
    _msg_event(cfg, 3)
    r = _dec(run, "nothing named", extra=("--event", "2"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CA and rec["campaign_from"] == "event" and rec["event_id"] == 2
    # neighbours: event 1 stamps CB, event 3 has no campaign and falls through
    r = _dec(run, "nothing named", extra=("--event", "1"))
    assert D.read(cfg)[1]["campaign"] == CB
    r = _dec(run, "nothing named", extra=("--event", "3"))
    assert r.exit_code == 0, r.output
    assert D.read(cfg)[2]["campaign"] is None


def test_cli_missing_event_with_text_campaign_writes_nothing(cfg, run):
    mk(cfg, CA)
    _msg_event(cfg, 1, CA)
    r = _dec(run, f"about {CA}", extra=("--event", "99"))
    assert r.exit_code != 0
    assert D.read(cfg) == []
    assert not [e for e in S.read_ledger(cfg, CA) if e["type"] == "note"]


def test_cli_event_with_non_string_campaign_falls_through(cfg, run):
    mk(cfg, CB)
    _msg_event(cfg, 1, 123)
    _msg_event(cfg, 2, ["x"])
    for n in ("1", "2"):
        r = _dec(run, f"about {CB}", extra=("--event", n))
        assert r.exit_code == 0, r.output
    recs = D.read(cfg)
    assert [(x["campaign"], x["campaign_from"]) for x in recs] == [(CB, "text")] * 2


def test_cli_event_campaign_with_non_object_state_falls_through(cfg, run):
    mk(cfg, CA)
    mk(cfg, CB)
    S.campaign_dir(cfg, CA).joinpath("state.json").write_text("[1, 2]\n")
    _msg_event(cfg, 1, CA)
    r = _dec(run, f"about {CB}", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CB and rec["campaign_from"] == "text"
    assert not [e for e in S.read_ledger(cfg, CA) if e["type"] == "note"]


def test_cli_event_oserror_falls_through_to_text(cfg, run, monkeypatch):
    mk(cfg, CA)
    mk(cfg, CB)
    real = S.read_state

    def flaky(cfg, cid):
        if cid == CA:
            raise OSError("unreadable")
        return real(cfg, cid)

    monkeypatch.setattr(S, "read_state", flaky)
    _msg_event(cfg, 1, CA)
    r = _dec(run, f"about {CB}", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CB and rec["campaign_from"] == "text"
    assert rec["spec_id"] == f"SPEC-{CB}"


def test_cli_event_campaign_outside_the_store_is_not_inherited(cfg, run, tmp_path):
    """Absolute, traversal, and symlink campaign values do not resolve or get a note."""
    mk(cfg, CB)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "state.json").write_text(
        json.dumps({"id": "evil", "spec_id": "SPEC-9", "project": "P"}) + "\n")
    root = S.campaigns_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    (root / "LINKED").symlink_to(outside, target_is_directory=True)
    escaped = os.path.relpath(outside, root)
    assert escaped.startswith("..")
    for n, bad in enumerate((str(outside), escaped, "LINKED"), start=1):
        _msg_event(cfg, n, bad)
        before = sorted(p.name for p in outside.iterdir())
        r = _dec(run, f"about {CB}", extra=("--event", str(n)))
        assert r.exit_code == 0, r.output
        rec = D.read(cfg)[-1]
        assert rec["campaign"] == CB and rec["campaign_from"] == "text"
        assert sorted(p.name for p in outside.iterdir()) == before
    r = _dec(run, "nothing named here", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[-1]
    assert rec["campaign"] is None and "campaign_from" not in rec
    assert sorted(p.name for p in outside.iterdir()) == ["state.json"]


def test_event_lookup_is_once_and_not_the_whole_suffix(cfg, run, monkeypatch):
    mk(cfg, CA)
    mk(cfg, CB)
    _msg_event(cfg, 1, CA)
    _msg_event(cfg, 2, CB)
    calls = []
    real = E.read_id

    def spy(cfg, event_id):
        calls.append(event_id)
        return real(cfg, event_id)

    monkeypatch.setattr(E, "read_id", spy)
    monkeypatch.setattr(E, "read_since", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("read_since materializes the suffix")))
    r = _dec(run, f"about {CB}", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    assert calls == [1]
    assert D.read(cfg)[0]["campaign"] == CA and D.read(cfg)[0]["campaign_from"] == "event"


def test_cli_event_with_unknown_campaign_falls_through(cfg, run):
    mk(cfg, CB)
    E.append(cfg, "message", campaign="SPEC-0123", by="t",
             data={"msg_id": "1", "from": "x", "kind": "info", "text": "t"})
    r = _dec(run, f"about {CB}", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[0]
    assert rec["campaign"] == CB and rec["campaign_from"] == "text"
    r = _dec(run, "nothing named", extra=("--event", "1"))
    assert r.exit_code == 0, r.output
    rec = D.read(cfg)[1]
    assert rec["campaign"] is None and "campaign_from" not in rec


def test_inferred_decision_covers_park_and_dispatch(cfg, clock, run):
    decide(cfg, None, kind="no_action", rule="wt_broken", why="log starts")
    mk(cfg, CA)
    park(cfg, CA)
    r = _dec(run, f"waiting on {CA}")
    assert r.exit_code == 0, r.output
    cov = D.park_coverage(cfg)
    assert cov["parks"] == 1 and cov["covered"] == 1
    ev = [e for e in S.read_ledger(cfg, CA) if e["type"] == "park"][0]
    assert D.covering(cfg, CA, ev["ts"])["campaign_from"] == "text"
    r = _dec(run, f"dispatch {CA}", kind="dispatch", rule="parked.y")
    assert r.exit_code == 0, r.output
    assert [x["campaign"] for x in D.open_dispatches(D.read(cfg))] == [CA]


def test_inferred_write_is_append_only(cfg, run):
    mk(cfg, CA)
    assert _dec(run, "first, no campaign").exit_code == 0
    before = D.log_path(cfg).read_bytes()
    assert _dec(run, f"second {CA}").exit_code == 0
    assert D.log_path(cfg).read_bytes().startswith(before)
