"""SPEC-0553 through the queue with a FakeRunner: `agentic.lean` pinned at open, tier models on
every run, the size/tone cap at add and at open (the spec on base), the hand-raised campaign's
one deep adversarial pass at close, `cap_ignored`, and the export columns."""
from pathlib import Path

import pytest

from test_agentic_e2e import (SPEC_ACCEPTED, adv_review, added, impl_done, make_cfg, make_repo,
                              review, script, write_review, _PerPersona)
from nightwright.agentic import campaign_export as CE
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.agentic.runner import RunResult

pytestmark = pytest.mark.integration

CONF, ADV = "conformance_reviewer", "adversarial_reviewer"
SPEC_REL = "docs/specs/0001-trivial.md"


class _Recording(_PerPersona):
    def __init__(self, scripts):
        super().__init__(scripts)
        self.reqs = []

    def run(self, req, *, hooks=None):
        with self.lock:
            self.reqs.append(req)
        return super().run(req, hooks=hooks)


def _setup(tmp_path, *, lean=True, extra_fm="", sized=None):
    repo = make_repo(tmp_path, extra_fm=extra_fm, review=sized is None)
    if sized is not None:
        write_review({"data_dir": str(tmp_path / "data")}, repo / SPEC_REL, "SPEC-0001",
                     size=sized[0], tone=sized[1], rationale="r")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile={"reviewers": [CONF, ADV]})
    cfg["agentic"]["profiles"]["thorough"] = {"reviewers": [CONF, ADV]}
    if lean:
        cfg["agentic"]["lean"] = {"tiers": True}
    return repo, cfg


def _drive(cfg, cid, runner, ticks=20):
    for _ in range(ticks):
        Q.tick(cfg, runner=runner)
        if S.read_state(cfg, cid)["state"] in ("MERGED", "PARKED"):
            break
    return S.read_state(cfg, cid)


def _starts(cfg, cid):
    return [(e["persona"], e["model"], e.get("tier"))
            for e in S.read_ledger(cfg, cid, types=["run.start"])]


def test_flags_pinned_at_open_and_tiers_on_every_run(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["lean"] == {"tiers": True, "packet": False, "collator": False, "sessions": False}
    assert S.read_ledger(cfg, cid, types=["open"])[0]["lean"] == st["lean"]
    cfg["agentic"]["lean"] = {"tiers": False}             # flipped mid-campaign: no effect
    runner = _Recording({"implementer": [impl_done()], CONF: [review()], ADV: [adv_review()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert sorted(_starts(cfg, cid)) == [
        (ADV, "claude-sonnet-5-5", "judge"), (CONF, "claude-sonnet-5-5", "judge"),
        ("implementer", "claude-sonnet-5-5", "judge")]
    assert st["raised"] is False and "deep_close" not in st              # default: no deep pass
    runs = {r["persona"]: r for r in CE.export_tables(cfg)["runs"]}
    assert runs[CONF]["tier"] == "judge" and runs[CONF]["model"] == "claude-sonnet-5-5"
    camp = CE.export_tables(cfg)["campaigns"][0]
    assert camp["lean"] == st["lean"] and camp["raised"] is False


def test_flag_off_campaign_stays_off_and_unchanged(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, lean=False)
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert "lean" not in st and "flag" not in st["sizing"]
    assert "lean" not in S.read_ledger(cfg, cid, types=["open"])[0]
    cfg["agentic"]["lean"] = {"tiers": True}              # turned on later: still off here
    runner = _Recording({"implementer": [impl_done()], CONF: [review()], ADV: [adv_review()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert sorted(_starts(cfg, cid)) == [(ADV, "opus", None), (CONF, "opus", None),
                                         ("implementer", "sonnet", None)]
    assert not any(k in st for k in ("raised", "deep_close", "cap_ignored"))
    assert "hand" not in st["sizing"]
    camp = CE.export_tables(cfg)["campaigns"][0]
    assert (camp["lean"], camp["raised"]) == (None, False)
    assert {r["tier"] for r in CE.export_tables(cfg)["runs"]} == {None}


def test_review_sizing_is_capped_at_the_default(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, sized=("l", "critical"))
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["profile"] == "balanced" and st["sizing"]["capped_from"] == "thorough"
    assert st["sizing"]["reason"].endswith("(capped at balanced)")
    runner = _Recording({"implementer": [impl_done()], CONF: [review()], ADV: [adv_review()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED" and st["profile"] == "balanced" and st["raised"] is False
    assert runner.calls.count(ADV) == 1
    assert not any("opus" in m for _, m, _ in _starts(cfg, cid))


def test_flag_off_review_sizing_still_picks_freely(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, lean=False, sized=("l", "critical"))
    cid = added(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["profile"] == "thorough"


def test_profile_flag_is_capped(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch, "SPEC-0001", "--profile", "thorough")
    st = S.read_state(cfg, cid)
    assert st["profile"] == "balanced" and st["sizing"]["flag"] == "thorough"


def test_hand_raised_campaign_runs_one_deep_adversarial_pass(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, extra_fm="tone: critical\n")
    cid = added(cfg, monkeypatch)
    runner = _Recording({"implementer": [impl_done(extra="tone: critical\n")],
                         CONF: [review()], ADV: [adv_review(), adv_review()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert st["profile"] == "thorough" and st["raised"] is True
    assert st["sizing"]["source"] == "hand" and st["sizing"]["hand"] == {"tone": "critical"}
    assert S.read_ledger(cfg, cid, types=["lean_sizing"])[0]["raised"] is True
    starts = _starts(cfg, cid)
    assert [s for s in starts if s[2] == "deep"] == [(ADV, "claude-opus-5-5", "deep")]
    assert starts[-1] == (ADV, "claude-opus-5-5", "deep")       # the last run before close
    assert runner.calls.count(ADV) == 2 and runner.calls.count(CONF) == 1
    assert st["deep_close"]["status"] == "done"
    trans = [(e["from"], e["to"], e["reason"]) for e in S.read_ledger(cfg, cid,
                                                                       types=["transition"])]
    assert [(e["status"], e.get("tier")) for e in S.read_ledger(cfg, cid, types=["deep_close"])] \
        == [("pending", "deep"), ("done", None)]
    assert ("REVIEWING", "CLOSING", "review_pass") in trans
    camp = CE.export_tables(cfg)["campaigns"][0]
    assert camp["raised"] is True and camp["profile"] == "thorough"


def test_deep_pass_fix_is_a_normal_review_fix(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, extra_fm="tone: critical\n")
    cid = added(cfg, monkeypatch)
    runner = _Recording({
        "implementer": [impl_done(extra="tone: critical\n"),
                        impl_done(content="better\n", extra="tone: critical\n")],
        CONF: [review(), review()], ADV: [adv_review(), adv_review("fix"), adv_review()]})
    st = _drive(cfg, cid, runner, ticks=30)
    assert st["state"] == "MERGED", st.get("park")
    assert st["counters"]["loops"] == 1                          # the deep fix used one loop
    assert [s for s in _starts(cfg, cid) if s[2] == "deep"] == [(ADV, "claude-opus-5-5", "deep")]
    assert runner.calls.count(ADV) == 3 and runner.calls.count("implementer") == 2
    trans = [(e["from"], e["to"], e["reason"]) for e in S.read_ledger(cfg, cid,
                                                                       types=["transition"])]
    assert ("REVIEWING", "IMPLEMENTING", "review_fix") in trans
    assert [e["status"] for e in S.read_ledger(cfg, cid, types=["deep_close"])] == [
        "pending", "done"]


def test_size_tone_written_inside_the_campaign_is_ignored(tmp_path, monkeypatch):
    """The implementer's commit adds `size`/`tone` to the spec (the spec guard would refuse the
    gate too): the profile is unchanged and one `cap_ignored` event per new value says so."""
    repo, cfg = _setup(tmp_path)
    cid = added(cfg, monkeypatch)
    Q.tick(cfg, runner=_Recording({}))                         # open -> IMPLEMENTING
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and st["sizing"]["hand"] is None
    spec = Path(st["repo"]["worktree"]) / SPEC_REL
    spec.write_text(SPEC_ACCEPTED.format(id="SPEC-0001", extra="size: l\ntone: critical\n"))
    done = {"status": "done", "summary": "s", "changed_files": [], "tests_run": [],
            "ac_status": [], "blockers": [], "followup_ideas": []}
    rr = RunResult(exit_code=0, timed_out=False, interrupted=False, duration_s=0.1,
                   is_error=False, cost_usd=0.1, verdict=done, verdict_ok=True)
    for n in (1, 2):
        cur = S.read_state(cfg, cid)
        cur["state"] = "IMPLEMENTING"
        S.write_state(cfg, cid, cur)
        res = Q.StepResult(cid, Q.Step("implement", persona="implementer"), "IMPLEMENTING",
                           run_id=f"00{n}-implementer", run_result=rr, budget=1.0)
        Q._apply(Q._Ctx(cfg), cur, res)
    st = S.read_state(cfg, cid)
    assert st["profile"] == "balanced" and st["raised"] is False
    ev = S.read_ledger(cfg, cid, types=["cap_ignored"])
    assert len(ev) == 1 and ev[0]["spec"] == {"size": "l", "tone": "critical"}
    assert ev[0]["hand"] is None and ev[0]["step"] == "implement"
    assert st["cap_ignored"] == {"size": "l", "tone": "critical"}


def test_hand_raise_ignored_when_flag_off(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, lean=False, extra_fm="tone: critical\n")
    cid = added(cfg, monkeypatch)
    runner = _Recording({"implementer": [impl_done(extra="tone: critical\n")],
                         CONF: [review()], ADV: [adv_review()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED" and st["profile"] == "balanced"
    assert runner.calls.count(ADV) == 1 and not S.read_ledger(cfg, cid, types=["cap_ignored"])


assert "{extra}" in SPEC_ACCEPTED                                 # the hand keys go there


@pytest.mark.parametrize("lean,want", [(True, "balanced"), (False, "thorough")])
def test_idea_campaign_profile_flag_is_capped(tmp_path, monkeypatch, lean, want):
    from nightwright import org_write as OW
    repo, cfg = _setup(tmp_path, lean=lean)
    _, _, iid = OW.add_idea(cfg, "an idea to build")
    cid = added(cfg, monkeypatch, iid, "--profile", "thorough")
    assert S.read_state(cfg, cid)["profile"] == want


def test_worktree_resnapshot_is_not_a_hand_value(tmp_path, monkeypatch):
    """A missing / untrusted `spec_at_open` is rebuilt from the worktree on adopt; its
    size/tone may be in-campaign edits, so the open reads them as absent and journals it."""
    repo, cfg = _setup(tmp_path, extra_fm="tone: critical\n")
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    wt = Path(st["repo"]["worktree"])
    import subprocess
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", st["repo"]["branch"],
                   str(wt), "main"], check=True, capture_output=True)
    assert Q._adopt_spec_open_snapshot(cfg, st, wt) is None
    assert st["spec"]["snapshot_adopted"] is True
    events = []
    Q._lean_open(Q._Ctx(cfg), st, events)
    assert st["sizing"]["hand"] is None and st["raised"] is False
    assert st["profile"] == "balanced"
    assert events[0][1]["hand_untrusted"] is True and events[0][1]["hand"] is None
    assert "lean_hand_untrusted" in [j["kind"] for j in Q.read_journal(cfg)]


# ---- post-merge fix: an equal-rank `--profile` survives the open (and the spec acceptance) ----

EQUAL = "lean-medium"          # config-only profile: ranks as `balanced`, the default


def _lean_sizing(cfg, cid):
    return S.read_ledger(cfg, cid, types=["lean_sizing"])


@pytest.mark.parametrize("lean", [True, False])
def test_idea_campaign_keeps_an_equal_rank_profile_flag(tmp_path, monkeypatch, lean):
    """IDEA-1322-20261112-f0ea: `nw queue add IDEA --lean --profile lean-medium` opened as the
    default (`lean_sizing` source default) because the idea path never recorded `sizing.flag`.
    The flag is kept at open, through SPEC_WRITING and the in-loop spec acceptance."""
    from test_agentic_idea_campaign import add_idea_campaign, capture
    from test_agentic_spec_review_loop import reviewed
    from test_agentic_spec_writer import make_project, spec_written
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    cfg["agentic"]["profiles"][EQUAL] = {"reviewers": [CONF]}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    cid = add_idea_campaign(cfg, monkeypatch, iid, *(["--lean"] if lean else []),
                            "--profile", EQUAL)
    st = S.read_state(cfg, cid)
    assert st["profile"] == EQUAL
    if lean:
        assert st["sizing"]["flag"] == EQUAL and "capped_from" not in st["sizing"]
        assert S.read_ledger(cfg, cid, types=["open"])[0]["profile_source"] == "flag"
    else:
        assert "sizing" not in st                                  # flag off: as before
    script(tmp_path, [spec_written(2, iid), reviewed()])
    Q.tick(cfg)                                                    # open → SPEC_WRITING
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and st["profile"] == EQUAL
    if lean:
        (ev,) = _lean_sizing(cfg, cid)
        assert (ev["profile"], ev["source"]) == (EQUAL, "flag"), ev
        assert ev["reason"] == f"--profile {EQUAL}"
    else:
        assert not _lean_sizing(cfg, cid)
    for _ in range(4):                                             # write, review + accept
        Q.tick(cfg)
        if S.read_state(cfg, cid)["state"] not in ("SPEC_WRITING", "SPEC_REVIEW"):
            break
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING", st.get("park")
    assert st["profile"] == EQUAL and len(_lean_sizing(cfg, cid)) == (1 if lean else 0)


def test_idea_campaign_stronger_profile_flag_still_capped_at_open(tmp_path, monkeypatch):
    from nightwright import org_write as OW
    repo, cfg = _setup(tmp_path)
    _, _, iid = OW.add_idea(cfg, "an idea to build")
    cid = added(cfg, monkeypatch, iid, "--profile", "thorough")
    st = S.read_state(cfg, cid)
    assert st["profile"] == "balanced"
    assert (st["sizing"]["flag"], st["sizing"]["capped_from"]) == ("thorough", "thorough")
    events = []
    Q._lean_open(Q._Ctx(cfg), st, events)
    assert st["profile"] == "balanced" and events[0][1]["profile"] == "balanced"


def test_spec_campaign_keeps_an_equal_rank_profile_flag(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, lean=False)
    cfg["agentic"]["profiles"][EQUAL] = {"reviewers": [CONF, ADV]}
    cid = added(cfg, monkeypatch, "SPEC-0001", "--lean-flags", "tiers", "--profile", EQUAL)
    st = S.read_state(cfg, cid)
    assert st["profile"] == EQUAL and st["sizing"]["flag"] == EQUAL
    runner = _Recording({"implementer": [impl_done()], CONF: [review()], ADV: [adv_review()]})
    st = _drive(cfg, cid, runner)
    assert st["state"] == "MERGED", st.get("park")
    assert st["profile"] == EQUAL
    (ev,) = _lean_sizing(cfg, cid)
    assert (ev["profile"], ev["source"]) == (EQUAL, "flag"), ev
