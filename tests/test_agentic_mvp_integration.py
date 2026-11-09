"""SPEC-0298 integration: cost, wakeups, brief and the MVP scorecard agree on one queue.

One campaign runs through the real queue offline (temp git repo, FakeRunner): it parks
`main_dirty`, gets a conductor decision, is resumed and merges. Two more merged campaigns are
written with fixed timestamps around it. A scripted conductor transcript and scripted follower
deliveries supply the rest, and every number is checked against a hand computation."""
import datetime as dt
import json

import pytest

import waits
from test_agentic_e2e import (_git, added, impl_done, make_cfg, make_repo, review, run_cli,
                              script)
from test_agentic_e2e import _git_env  # noqa: F401 — autouse fixture: git identity
from test_agentic_mvp_report import PRICE, asst, camp, conductor, crit, deliveries
from nightwright.agentic import conductor_cost as CC
from nightwright.agentic import quiet
from nightwright.agentic import store as S

pytestmark = pytest.mark.integration


def iso(t):
    return t.isoformat()


def test_cost_wakeups_brief_and_scorecard_agree(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cfg["_root"] = str(tmp_path / "projects")
    cfg["agentic"]["conductor"] = {"prices": {"claude-x": PRICE}}

    # campaign 1: parks main_dirty, the conductor decides, resume, merge
    c1 = added(cfg, monkeypatch)
    (repo / "gates" / "lint.sh").write_text("true # edited\n")
    assert run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"]).exit_code == 0
    assert S.read_state(cfg, c1)["park"]["reason"] == "main_dirty"
    # ledger timestamps have second resolution: let the clock tick past the open event so the
    # campaign's live window (open -> terminal transition, end exclusive) is never empty
    parked_at = S.now_iso()
    r = run_cli(cfg, monkeypatch, ["queue", "decision", c1, "--kind", "wait", "--rule",
                                   "parked.main_dirty", "main is dirty; the human cleans it"])
    assert r.exit_code == 0, r.output
    brief = run_cli(cfg, monkeypatch, ["queue", "conductor-brief"]).output
    assert c1 in brief and "decided=true" in brief and "wait/parked.main_dirty" in brief
    _git(repo, "checkout", "--", "gates/lint.sh")
    assert waits.until(lambda: S.now_iso() != parked_at)
    assert run_cli(cfg, monkeypatch, ["queue", "resume", c1, "--yes"]).exit_code == 0
    assert run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"]).exit_code == 0
    assert S.read_state(cfg, c1)["state"] == "MERGED"

    # campaigns 2 and 3 merge later, at fixed offsets from campaign 1's start
    (w1,) = [w for w in CC.live_windows(cfg) if w["campaign"] == c1]
    s1, e1 = w1["start"], w1["end"]
    hour = dt.timedelta(hours=1)
    c2 = camp(cfg, "C2", iso(s1 + hour), iso(s1 + 2 * hour))
    c3 = camp(cfg, "C3", iso(s1 + 3 * hour), iso(s1 + 4 * hour))

    # the scripted conductor: 100k tokens ($1 at PRICE) while each campaign is live, and one
    # expensive turn two hours before the first campaign opened (outside the interval)
    conductor(cfg, "s1", [
        asst("pre", iso(s1 - 2 * hour), 900_000),
        asst("t1", iso(s1 + (e1 - s1) / 2), 100_000),
        asst("t2", iso(s1 + hour * 1.5), 100_000),
        asst("t3", iso(s1 + hour * 3.5), 100_000)])
    cost = CC.cost_report(cfg)
    share = {c["campaign"]: c["conductor_usd"] for c in cost["campaigns"]}
    assert (share[c1], share[c2], share[c3]) == (1.0, 1.0, 1.0)
    assert cost["idle_usd"] == pytest.approx(9.0) and cost["merges"] == 3

    # scripted follower deliveries: 3 delivered (2 wakes and a digest), 3 held
    deliveries(cfg, [
        (iso(s1 - hour), "parked", "wake"),
        (iso(s1 + hour * 0.2), "parked", "wake"), (iso(s1 + hour * 0.3), "merged", "hold"),
        (iso(s1 + hour * 1.2), "runner_dead", "wake"), (iso(s1 + hour * 1.3), "merged", "hold"),
        (iso(s1 + hour * 2.5), "queue_digest", "digest"), (iso(s1 + hour * 2.6), "merged", "hold")])
    wk = quiet.wakeups_report(cfg, since=iso(s1 - hour)[:10], until=iso(s1 + 4 * hour)[:10])
    assert (wk["delivered"], wk["held"], wk["digests"]) == (4, 3, 1)      # incl. the earlier wake

    # the scorecard over exactly these three campaigns
    r = run_cli(cfg, monkeypatch, ["queue", "mvp-report", "--window", "3", "--json"])
    assert r.exit_code == 0, r.output
    rep = json.loads(r.output)
    assert rep["window"]["campaigns"] == [c1, c2, c3] and rep["window"]["merges"] == 3
    assert rep["window"]["qualifying"] is False
    assert "SPEC-0299" in rep["window"]["qualifying_reason"]
    assert (crit(rep, "E1")["value"], crit(rep, "E1")["status"]) == (1.0, "pass")   # $3 / 3
    assert (crit(rep, "E2")["value"], crit(rep, "E2")["status"]) == (100_000, "pass")
    assert (crit(rep, "E3")["value"], crit(rep, "E3")["status"]) == (1.0, "pass")   # 3 / 3
    assert (crit(rep, "E4")["value"], crit(rep, "E4")["status"]) == (1.0, "pass")   # 1 park, 1 decision
    assert rep["parks_undecided"] == [] and rep["ok"] is False
    assert run_cli(cfg, monkeypatch, ["queue", "mvp-report", "--window", "3",
                                      "--check"]).exit_code == 1

