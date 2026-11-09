"""SPEC-0422 real verification: real `nw queue run` / `nw queue tick` on a real git fixture repo
with a real data dir. Only the persona runs (and their `cost_usd`) are scripted."""
import hashlib
import json

import pytest

from test_agentic_e2e import _finding, added, impl_done, make_cfg, make_repo, review, run_cli, \
    script, triage
from nightwright.agentic import cost_policy as CP
from nightwright.agentic import decisions as D
from nightwright.agentic import escalations as ESC
from nightwright.agentic import store as S

pytestmark = pytest.mark.integration

PROFILE = {"campaign_cap_usd": 2.0, "review": {"triager": False},
           "personas": {"implementer": {"max_budget_usd": 1.0},
                        "conformance_reviewer": {"max_budget_usd": 0.5}}}


@pytest.fixture(autouse=True)
def _quiet_notify(monkeypatch):
    monkeypatch.setattr(ESC, "_run_notify", lambda argv: type("R", (), {"returncode": 0})())


def _minors(n, nits=0):
    return [_finding("minor", f"m{i}") for i in range(n)] + \
        [_finding("nit", f"n{i}") for i in range(nits)]


def _setup(tmp_path, monkeypatch, entries, profile=PROFILE, policy=None):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries), profile=profile)
    if policy is not None:
        cfg["agentic"]["cost_cap_policy"] = policy
    return cfg, added(cfg, monkeypatch)


def _run(cfg, monkeypatch, *args):
    r = run_cli(cfg, monkeypatch, ["queue", *(args or ("run", "--until-idle"))])
    assert r.exit_code == 0, r.output
    return r


def _personas(cfg, cid):
    return [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.start"])]


def _cost_escalations(cfg, cid):
    return [r for r in ESC.list_all(cfg) if r.get("campaign") == cid and r.get("kind") == "cost"]


def test_converging_campaign_is_raised_in_place_and_merges(tmp_path, monkeypatch):
    # 60/80 fix (minors only) → 76/80 fix (rising, minors only) → the third implementer run is
    # cost-blocked at $1.40 + $1.00 > $2.00: the queue raises the cap and the run happens
    entries = [impl_done(), review("pass", findings=_minors(10)),
               impl_done(content="b\n"), review("pass", findings=_minors(6)),
               impl_done(content="c\n"), review("pass")]
    cfg, cid = _setup(tmp_path, monkeypatch, entries, policy={"enabled": True})
    _run(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert _personas(cfg, cid) == ["implementer", "conformance_reviewer"] * 3
    assert S.read_ledger(cfg, cid, types=["park"]) == []                      # never parked
    raised = S.read_ledger(cfg, cid, types=["cap_raised"])
    assert [(e["from"], e["to"], e["ceiling"], e["by"]) for e in raised] == [(2.0, 3.0, 6.0, "queue")]
    assert "score rose 60→76" in raised[0]["why"]
    assert st["cost"]["cap_usd"] == 3.0 and st["counters"][CP.RAISES] == 1
    recs = [r for r in D.read(cfg, campaign=cid) if r["rule"] == CP.RULE]
    assert [(r["by"], r["kind"], r["action"]) for r in recs] == [("queue", "resume", "raise cap to $3.00")]
    outs = S.read_ledger(cfg, cid, types=["review_outcome"])
    assert [(o["score"], o["outcome"], o["major"]) for o in outs] == [(60, "fix", 0), (76, "fix", 0),
                                                                     (100, "pass", 0)]
    # the queue event mirror, always held for the conductor
    from nightwright.agentic import events as E
    from nightwright.agentic import quiet as QT
    E.sync(cfg)
    evs = E.read_since(cfg, 0, types=["cap_raised"])
    assert [(e["campaign"], e["data"]["to"]) for e in evs] == [(cid, 3.0)]
    assert QT.RULES["cap_raised"] == "hold"


def test_diverging_campaign_parks_with_exactly_one_escalation(tmp_path, monkeypatch):
    major = review("fix", findings=[_finding("major", "lock leaks")] + _minors(2))   # 77: rising, a major
    entries = [impl_done(), review("pass", findings=_minors(10)), impl_done(content="b\n"), major,
               impl_done(content="c\n"), review("pass")]
    cfg, cid = _setup(tmp_path, monkeypatch, entries, policy={"enabled": True})
    _run(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "human_decision", st
    assert st["park"]["escalated_from"] == "cost_cap"
    assert st["cost"]["cap_usd"] == 2.0 and S.read_ledger(cfg, cid, types=["cap_raised"]) == []
    assert _personas(cfg, cid) == ["implementer", "conformance_reviewer"] * 2
    (rec,) = _cost_escalations(cfg, cid)
    assert rec["recommendation"] is None and rec["status"] == "open"
    assert "topic: spend_diverging" in rec["facts"] and "2 major" in rec["question"]
    assert [o["label"] for o in rec["options"]][1:] == [
        "let it finish at the current cap (no more runs)", "abandon",
        "rework the spec (split / narrow)"]
    recs = [r for r in D.read(cfg, campaign=cid) if r["rule"] == CP.RULE]
    assert [(r["by"], r["kind"]) for r in recs] == [("queue", "escalate")]
    for _ in range(2):                                    # later ticks file nothing more
        _run(cfg, monkeypatch, "tick")
    assert len(_cost_escalations(cfg, cid)) == 1 and len(ESC.list_all(cfg)) == 1


def test_ceiling_refuses_with_exactly_one_escalation(tmp_path, monkeypatch):
    # ceiling 1.5 × $2 = $3: one raise to $3, then $2.10 + a $1.00 run would cross it
    entries = [impl_done(), review("pass", findings=_minors(10)),
               impl_done(content="b\n"), review("pass", findings=_minors(6)),
               impl_done(content="c\n"), review("pass", findings=_minors(5, nits=2)),
               impl_done(content="d\n"), review("pass")]
    cfg, cid = _setup(tmp_path, monkeypatch, entries,
                      policy={"enabled": True, "ceiling_multiplier": 1.5})
    _run(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["escalated_from"] == "cost_cap", st
    assert [e["to"] for e in S.read_ledger(cfg, cid, types=["cap_raised"])] == [3.0]
    assert st["cost"]["cap_usd"] == 3.0 and st["cost"]["total_usd"] == pytest.approx(2.1)
    assert _personas(cfg, cid).count("implementer") == 3                 # never past the ceiling
    (rec,) = _cost_escalations(cfg, cid)
    assert "topic: spend_ceiling" in rec["facts"] and rec["recommendation"] is None
    assert "ceiling" in rec["question"]
    _run(cfg, monkeypatch, "tick")
    assert len(_cost_escalations(cfg, cid)) == 1


# ---- the reserve (AC3) -------------------------------------------------------------------------

NO_FEATURE = {"persona": "implementer", "edits": [{"path": "other.txt", "write": "x\n"}],
              "verdict": {"status": "done", "summary": "s", "changed_files": ["other.txt"],
                          "tests_run": [], "ac_status": [], "blockers": [], "followup_ideas": []},
              "cost_usd": 0.5}
RESERVE_PROFILE = {"campaign_cap_usd": 1.5,
                   "personas": {"implementer": {"max_budget_usd": 1.0},
                                "gate_triager": {"max_budget_usd": 0.5}}}


def _seed_stats(cfg):
    from nightwright.agentic.profiles import resolve_profile
    prof = resolve_profile(cfg, "balanced")
    runs = {CP.key("implementer", prof["personas"]["implementer"]["model"]): [0.3] * 8,
            CP.key("gate_triager", prof["personas"]["gate_triager"]["model"]): [0.05] * 8}
    CP.stats_path(cfg).write_text(json.dumps({"schema": CP.STATS_SCHEMA, "runs": runs,
                                              "cursors": {}, "pending": {}}))


def _gate_loop():
    return [NO_FEATURE, triage("fix", cost_usd=0.1)] * 3


def test_old_reserve_parks_at_the_full_budget(tmp_path, monkeypatch):
    cfg, cid = _setup(tmp_path, monkeypatch, _gate_loop(), profile=RESERVE_PROFILE,
                      policy={"enabled": False})
    _seed_stats(cfg)
    _run(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "cost_cap", st
    assert st["cost"]["total_usd"] == pytest.approx(0.6)      # $0.60 + $1.00 > $1.50
    assert _personas(cfg, cid) == ["implementer", "gate_triager"]


def test_new_reserve_parks_only_when_the_median_would_cross_the_cap(tmp_path, monkeypatch):
    cfg, cid = _setup(tmp_path, monkeypatch, _gate_loop(), profile=RESERVE_PROFILE,
                      policy={"enabled": True, "no_review_raises": 0})
    _seed_stats(cfg)
    _run(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    # $0.60 + reserve $0.45 fits; $1.20 + $0.45 > $1.50 blocks, and with no review the policy
    # escalates rather than raising (no_review_raises: 0)
    assert _personas(cfg, cid) == ["implementer", "gate_triager"] * 2
    assert st["cost"]["total_usd"] == pytest.approx(1.2) and st["cost"]["cap_usd"] == 1.5
    assert st["state"] == "PARKED" and st["park"]["escalated_from"] == "cost_cap", st
    park = S.read_ledger(cfg, cid, types=["park"])[0]
    assert park["reason"] == "cost_cap" and "(reserve $0.45 of budget $1.00)" in park["detail"]
    (rec,) = _cost_escalations(cfg, cid)
    assert "no review yet after 0 raises" in rec["question"]
    stats = json.loads(CP.stats_path(cfg).read_text())
    assert len(next(v for k, v in stats["runs"].items() if k.startswith("implementer|"))) == 10


# ---- AC5: a park from before the deploy --------------------------------------------------------

def test_pre_deploy_cost_park_is_resumed_raised_on_the_next_tick(tmp_path, monkeypatch):
    cfg, cid = _setup(tmp_path, monkeypatch, _gate_loop(), profile=RESERVE_PROFILE,
                      policy={"enabled": False})
    _run(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["park"]["reason"] == "cost_cap"
    cfg["agentic"]["cost_cap_policy"] = {"enabled": True}              # the deploy
    r = run_cli(cfg, monkeypatch, ["queue", "tick", "--json"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] != "PARKED" and st["cost"]["cap_usd"] == 2.5, st      # 1.5 + max(0.75, 1.0)
    resume = S.read_ledger(cfg, cid, types=["resume"])[-1]
    assert resume["by"] == "queue" and resume["cap_usd"] == 2.5
    assert [e["to"] for e in S.read_ledger(cfg, cid, types=["cap_raised"])] == [2.5]
    recs = [r for r in D.read(cfg, campaign=cid) if r["rule"] == CP.RULE]
    assert [(r["by"], r["kind"], r["action"]) for r in recs] == [
        ("queue", "resume", "resume to IMPLEMENTING raise cap to $2.50")]
    assert st["counters"][CP.RAISES] == 1 and st["counters"][CP.RAISES_NO_REVIEW] == 1


# ---- AC6: explain writes nothing ---------------------------------------------------------------

def _tree_digest(root):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def test_explain_reports_and_writes_nothing(tmp_path, monkeypatch):
    entries = [impl_done(), review("pass", findings=_minors(10)), impl_done(content="b\n"),
               review("fix", findings=[_finding("major", "lock leaks")] + _minors(2))]
    cfg, cid = _setup(tmp_path, monkeypatch, entries + [impl_done(content="c\n")],
                      policy={"enabled": False})
    _run(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["park"]["reason"] == "cost_cap"
    cfg["agentic"]["cost_cap_policy"] = {"enabled": True}
    data = tmp_path / "data"
    before = _tree_digest(data)
    r = run_cli(cfg, monkeypatch, ["queue", "cost-policy", "explain", cid, "--json"])
    assert r.exit_code == 0, r.output
    assert _tree_digest(data) == before                          # byte for byte
    out = json.loads(r.output)
    assert out["schema"] == "nw.queue.cost_policy.v1"
    (row,) = out["campaigns"]
    assert (row["spent_usd"], row["cap_usd"], row["base_cap_usd"], row["ceiling_usd"]) == \
        (pytest.approx(1.4), 2.0, 2.0, 6.0)
    assert row["reserves"]["implementer"] == {"reserve_usd": 1.0, "budget_usd": 1.0,
                                              "samples": 2, "median_usd": 0.5}
    assert row["converging"] is False and row["converging_why"] == "2 major"
    assert row["decision"]["action"] == "escalate" and row["decision"]["topic"] == "spend_diverging"
    assert row["blocked_now"] is True
    r = run_cli(cfg, monkeypatch, ["queue", "cost-policy", "explain"])         # default selection
    assert r.exit_code == 0 and cid in r.output and "spend_diverging" in r.output
    assert _tree_digest(data) == before

