"""SPEC-0241: the TUI queue tab's view model (no Textual needed)."""
import os

import pytest

from test_agentic_dashboard import _campaign, _fixture, _iso, _park_fixture
from test_agentic_e2e import make_cfg, make_repo, run_cli, script, impl_done
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.tui import agentic_model as AM


def _by(rows):
    return {(r.section, r.campaign): r for r in rows}


def test_load_queue_sections_order_and_meta(tmp_path):
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
    order = []
    for r in rows:
        if not order or order[-1] != r.section:
            order.append(r.section)
    assert order == ["needs you", "conductor", "active", "waiting", "backlog", "delivered"]
    by = _by(rows)
    assert by[("needs you", "SPEC-0001-dirty")].meta.startswith("main_dirty · ")
    assert by[("backlog", "SPEC-0001-sized")].meta.endswith(" · m/high-end")
    assert "/" not in by[("backlog", ids["ready"])].meta
    pr = by[("delivered", "SPEC-0001-pr")]
    assert pr.meta.startswith("MERGED https://x/pull/7 · $1.25 · ")
    park = by[("conductor", ids["parked"])]
    assert park.meta.startswith("rate_limited · ") and park.meta.endswith("429 from the API")
    assert park.actions == ("resume", "prioritize", "abandon")
    impl = by[("active", ids["impl"])]
    assert impl.meta.startswith("IMPLEMENTING · implementer ")
    assert " · loops 1/" in impl.meta and impl.meta.endswith(" · $1.25")
    assert impl.title == "Trivial feature" and impl.spec == "SPEC-0001"
    assert by[("active", ids["gating"])].meta.startswith("GATING · gates · loops")
    wait = by[("waiting", "SPEC-0001-wait")]
    assert wait.meta.endswith("→ GATING") and wait.meta.startswith("waiting ")
    ready, blocked = by[("backlog", ids["ready"])], by[("backlog", ids["blocked"])]
    assert " · ready" in ready.meta and ready.meta.startswith("#")
    assert " p3 " in ready.meta
    assert "blocked by" in blocked.meta and "SPEC-0009" in blocked.meta
    merged = by[("delivered", ids["merged"])]
    assert merged.meta.startswith("MERGED abcdef1 · $1.25 · ") and merged.meta.endswith(" ago")
    assert merged.actions == ()
    assert header.startswith("runner down · hb -s · slots 2/")


def test_load_queue_needs_you_and_overdue(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    by = _by(AM.load_queue(cfg)[0])
    assert by[("needs you", ids["dirty"])].meta.startswith("main_dirty · ")
    assert by[("needs you", ids["dirty"])].meta.endswith("main_dirty detail")
    assert by[("conductor", ids["moving"])].meta.endswith(" · overdue")
    assert not by[("conductor", ids["rebase"])].meta.endswith("overdue")


def test_deferred_park_is_listed_twice_and_offers_no_park(tmp_path):
    repo, cfg, ids = _park_fixture(tmp_path)
    rows, _ = AM.load_queue(cfg)
    keys = [(r.section, r.campaign) for r in rows]
    assert len(keys) == len(set(keys))                       # row keys stay unique
    assert ("conductor", ids["req"]) in keys and ("active", ids["req"]) in keys
    by = _by(rows)
    assert by[("conductor", ids["req"])].actions == ()        # no park, no resume (not PARKED)
    assert by[("active", ids["req"])].actions == ()


def test_empty_queue_has_one_placeholder_row(tmp_path):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    rows, header = AM.load_queue(cfg)
    assert len(rows) == 1 and rows[0].section == "" and rows[0].meta == "queue is empty"
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
    from wt.tui.queue_tab import QueueView
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
        from wt.tui.queue_tab import QueueView
        assert QueueView._summary(action, cid, plan).startswith(
            {"prioritize": "priority"}.get(action, action))
