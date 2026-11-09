"""SPEC-0241: the TUI queue tab's view model (no Textual needed)."""
import os

import pytest

from test_agentic_dashboard import _campaign, _fixture, _iso, _park_fixture
from test_agentic_e2e import make_cfg, make_repo, run_cli, script, impl_done
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.tui import agentic_model as AM

pytestmark = pytest.mark.tui


def _by(rows):
    return {(r.group, r.campaign): r for r in rows if r.kind == "campaign"}


def test_load_queue_groups_order_and_cells(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    _campaign(cfg, repo, "SPEC-0001-wait", "WAITING", waiting={"resume_to": "GATING",
                                                               "since": _iso(5)})
    _campaign(cfg, repo, "SPEC-0001-dirty", "PARKED",
              park={"reason": "main_dirty", "detail": "main_dirty detail", "resume_to": "GATING"})
    _campaign(cfg, repo, "SPEC-0001-sized", "QUEUED",
              sizing={"size": "m", "tone": "high-end", "source": "flag", "reason": "x"})
    _campaign(cfg, repo, "SPEC-0001-pr", "MERGED", created_min=50, updated_min=4,
              close={"mode": "pr", "merged_sha": None, "pr_url": "https://x/pull/7"})
    rows, header = AM.load_queue(cfg)
    assert [r.group for r in rows if r.kind == "header"] == [
        "needs you", "conductor", "gating", "building", "waiting", "queued", "delivered"]
    by = _by(rows)
    assert by[("needs you", "SPEC-0001-dirty")].actions == ("resume", "prioritize", "abandon")
    park = by[("conductor", ids["parked"])]
    assert park.why                                        # the payload's why sentence
    assert park.actions == ("resume", "prioritize", "abandon")
    impl = by[("building", ids["impl"])]
    assert impl.activity.startswith("implementer ") and impl.state == "IMPLEMENTING"
    assert impl.loops.startswith("1/") and impl.cost_usd == 1.25 and impl.cap_usd == 10.0
    assert impl.title == "Trivial feature" and impl.spec == "SPEC-0001" and impl.pri == "3"
    assert impl.time_text and impl.state_since
    assert by[("gating", ids["gating"])].activity == "gates"
    wait = by[("waiting", "SPEC-0001-wait")]
    assert wait.activity == "waiting for a campaign slot" and wait.time_text.endswith("m")
    ready, blocked = by[("queued", ids["ready"])], by[("queued", ids["blocked"])]
    assert ready.why.startswith("ready") and ready.activity == ""
    assert "SPEC-0009" in blocked.activity and "SPEC-0009" in blocked.why
    merged = by[("delivered", ids["merged"])]
    assert merged.time_text.startswith("finished ") and merged.time_text.endswith(" ago")
    assert merged.actions == () and merged.pri == ""
    assert by[("delivered", "SPEC-0001-pr")].state == "MERGED"
    assert header.startswith("runner down · hb -s · slots 1/")      # SPEC-0395: the GATING item holds none


def test_view_uses_recent_20_for_delivered(tmp_path, monkeypatch):
    repo, cfg, ids = _fixture(tmp_path)
    seen = {}
    real = Q.dashboard_payload

    def spy(cfg, **kw):
        seen.update(kw)
        return real(cfg, **kw)
    monkeypatch.setattr(Q, "dashboard_payload", spy)
    AM.load_queue(cfg)
    assert seen["recent"] == 20


def test_load_queue_needs_you_and_overdue(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    rows = AM.load_queue(cfg)[0]
    by = _by(rows)
    assert ("needs you", ids["dirty"]) in by
    assert by[("conductor", ids["moving"])].why.endswith(" · overdue")
    assert not by[("conductor", ids["rebase"])].why.endswith("overdue")
    # parked-at order, so the overdue row does not lead the conductor group
    cond = [r.campaign for r in rows if r.kind == "campaign" and r.group == "conductor"]
    assert cond == [ids["rebase"], ids["moving"], ids["req"]]


def test_deferred_park_is_listed_twice_and_offers_no_park(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    rows, _ = AM.load_queue(cfg)
    keys = [(r.group, r.campaign) for r in rows if r.kind == "campaign"]
    assert len(keys) == len(set(keys))                       # row keys stay unique
    assert ("conductor", ids["req"]) in keys and ("building", ids["req"]) in keys
    by = _by(rows)
    assert by[("conductor", ids["req"])].actions == ()        # no park, no resume (not PARKED)
    assert by[("building", ids["req"])].actions == ()


def test_empty_queue_has_one_placeholder_row(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    rows, header = AM.load_queue(cfg)
    assert len(rows) == 1 and rows[0].kind == "placeholder" and rows[0].title == "queue is empty"
    assert rows[0].actions == () and rows[0].campaign == ""


@pytest.mark.parametrize("state,requested,expect", [
    ("QUEUED", False, ("park", "prioritize", "abandon")),
    ("WAITING", False, ("park", "prioritize", "abandon")),
    ("PARKED", False, ("resume", "prioritize", "abandon")),
    ("IMPLEMENTING", False, ("park",)),
    ("GATING", False, ("park",)),
    ("MERGED", False, ()),
    ("ABANDONED", False, ()),
    ("IMPLEMENTING", True, ()),
    ("QUEUED", True, ("prioritize", "abandon")),
    ("PARKED", True, ("resume", "prioritize", "abandon")),
])
def test_allowed_actions(state, requested, expect):
    assert AM.allowed_actions(state, requested) == expect


def _payload(**lease):
    return {"lease": {"held": True, "pid": 4242, "stale": False, "heartbeat_age_s": 3.0,
                      "draining": None, **lease},
            "slots": {"busy": 1, "total": 2}, "totals": {"spend_today_usd": 1.5}}


def test_header_line_variants():
    fol = [{"reader": "conductor", "held": True, "pid": 77},
           {"reader": "mailbox:steward:Alpha", "held": True, "pid": 5},
           {"reader": "mailbox:steward:Beta", "held": False, "pid": None}]
    assert AM.header_line(_payload(), fol) == (
        "runner 4242 · hb 3s · slots 1/2 · conductor 77 · stewards Alpha · today $1.50")
    assert AM.header_line(_payload(), []) == (
        "runner 4242 · hb 3s · slots 1/2 · conductor none · stewards none · today $1.50")
    assert AM.header_line(_payload(held=False, pid=None, heartbeat_age_s=None), []).startswith(
        "runner down · hb -s")
    assert AM.header_line(_payload(stale=True), []).startswith("runner stale 4242 · hb 3s")
    assert AM.header_line(_payload(draining={"since": "x"}), []).endswith(" · draining")
    # a follower whose lock is not held is not a live conductor
    assert "conductor none" in AM.header_line(
        _payload(), [{"reader": "conductor", "held": False, "pid": None}])


def test_queue_signature_changes_on_state_write_only(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    before = AM.queue_signature(cfg)
    assert AM.queue_signature(cfg) == before
    p = S.campaign_dir(cfg, ids["ready"]) / "state.json"
    os.utime(p, ns=(p.stat().st_atime_ns, p.stat().st_mtime_ns + 5_000_000_000))
    assert AM.queue_signature(cfg) != before


def test_queue_signature_missing_paths_are_none(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    ev, lease, states = AM.queue_signature(cfg)
    assert lease is None and states == ()


def _view(cfg, cid):
    st = S.read_state(cfg, cid)
    wt = (st.get("repo") or {}).get("worktree")
    return {"state": st["state"], "priority": Q._priority(st), "park": st.get("park") and
            {k: v for k, v in st["park"].items() if k != "since"},
            "park_requested": {k: v for k, v in (st.get("park_requested") or {}).items()
                               if k != "at"} or None,
            "pending_brief": st.get("pending_brief"),
            "worktree": bool(wt and os.path.isdir(wt))}


@pytest.mark.parametrize("action,key,kw,cli", [
    ("park", "ready", {"reason": "hold it"}, lambda c: ["park", c, "--reason", "hold it", "--yes"]),
    ("park", "req2", {"reason": "later"}, lambda c: ["park", c, "--reason", "later", "--yes"]),
    ("resume", "dirty", {"note": "go on"}, lambda c: ["resume", c, "--note", "go on", "--yes"]),
    ("resume", "dirty", {}, lambda c: ["resume", c, "--yes"]),
    ("prioritize", "ready", {"priority": 1}, lambda c: ["prioritize", c, "1", "--yes"]),
    ("abandon", "ready", {}, lambda c: ["abandon", c, "--yes"]),
    ("abandon", "dirty", {"keep": True}, lambda c: ["abandon", c, "--keep-worktree", "--yes"]),
])
def test_queue_action_matches_cli(tmp_path, monkeypatch, action, key, kw, cli):
    # one seed per side: the TUI path and the CLI path start from identical data
    results = []
    for side in ("tui", "cli"):
        root = tmp_path / side
        root.mkdir()
        repo, cfg, ids = _fixture(root)
        _campaign(cfg, repo, "SPEC-0001-dirty", "PARKED",
                  park={"reason": "main_dirty", "detail": "x", "resume_to": "GATING"})
        _campaign(cfg, repo, "SPEC-0001-req2", "IMPLEMENTING")
        cid = {"ready": ids["ready"], "dirty": "SPEC-0001-dirty",
               "req2": "SPEC-0001-req2"}[key]
        if side == "tui":
            AM.queue_action(cfg, action, cid, **kw)
        else:
            r = run_cli(cfg, monkeypatch, ["queue", *cli(cid)])
            assert r.exit_code == 0, r.output
        results.append(_view(cfg, cid))
    assert results[0] == results[1]


def test_queue_action_raises_plan_errors_and_writes_nothing(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    before = _view(cfg, ids["merged"])
    for action, kw in (("park", {"reason": "x"}), ("resume", {}), ("prioritize", {"priority": 2}),
                       ("abandon", {})):
        with pytest.raises(ValueError):
            AM.queue_action(cfg, action, ids["merged"], **kw)
    with pytest.raises(ValueError):
        AM.queue_action(cfg, "prioritize", ids["ready"], priority=9)
    assert _view(cfg, ids["merged"]) == before


def test_summary_text_for_each_action():
    from nightwright.tui.queue_tab import QueueView
    s = QueueView._summary
    assert s("park", "C", {"mode": "immediate", "note": "why"}) == "park C (immediate): why"
    assert s("park", "C", {"mode": "deferred", "note": "later"}) == "park C (deferred): later"
    assert s("resume", "C", {"to": "GATING"}) == "resume C → GATING"
    assert s("resume", "C", {"to": "IMPLEMENTING", "waits_for_slot": True}) == (
        "resume C → IMPLEMENTING (waits for a slot)")
    assert s("prioritize", "C", {"from": 3, "to": 1}) == "priority 3 → 1"
    assert s("abandon", "C", {"remove_worktree": False}) == "abandon C"
    assert s("abandon", "C", {"remove_worktree": True, "worktree": "/w/C"}) == (
        "abandon C, removes worktree /w/C")


def test_plans_carry_the_keys_the_summary_reads(tmp_path):
    repo, cfg, ids = _fixture(tmp_path)
    for action, cid, kw in (("park", ids["ready"], {"reason": "r"}),
                            ("resume", ids["parked"], {}),
                            ("prioritize", ids["ready"], {"priority": 1}),
                            ("abandon", ids["ready"], {})):
        plan = AM.plan_queue_action(cfg, action, cid, **kw)
        from nightwright.tui.queue_tab import QueueView
        assert QueueView._summary(action, cid, plan).startswith(
            {"prioritize": "priority"}.get(action, action))


def test_tui_activity_review_chips():                                      # SPEC-0277
    it = {"state": "REVIEWING", "loops": 1, "cost_usd": 1.0, "activity": {"kind": "idle"},
          "reviews": [{"persona": "conformance_reviewer", "status": "pass"},
                      {"persona": "adversarial_reviewer", "status": "fix"},
                      {"persona": "test_reviewer", "status": "running"},
                      {"persona": "extra_reviewer", "status": "not_run"}]}
    assert AM.activity_text(it) == "✓conf ✗adve ▶test ·extr"
    assert AM.activity_text({**it, "reviews": []}) == ""
