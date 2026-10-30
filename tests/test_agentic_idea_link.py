"""SPEC-0292: a spec campaign links its source idea (`:CAMPAIGN:`), every terminal state clears
the link it still owns, and the schema accepts the new events. Uses the SPEC-0176 end-to-end
harness (temp repo as project, fake runner, temp org)."""
import pytest

from wt import org as ORG
from wt import org_write as OW
from wt.agentic import queue as Q
from wt.agentic import schemas as SC
from wt.agentic import store as S
from wt.agentic.close import CloseOutcome

from test_agentic_e2e import (_git_env, added, impl_done, make_cfg, make_repo,  # noqa: F401
                              run_cli, script)

OLD = "[2026-10-20 Tue 10:00]"        # before the fixture spec's `created: 2026-10-25`


def _world(tmp_path, monkeypatch, *, old=True, with_source=True):
    repo = make_repo(tmp_path, extra_fm="source_idea: IDEA-001\n" if with_source else "")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()] * 6))
    _, _, iid = OW.add_idea(cfg, "link me")
    assert iid == "IDEA-001"
    if old:
        OW.set_property(cfg, idea(cfg, iid), "CREATED", OLD)
    return cfg, repo, iid


def idea(cfg, iid):
    ORG.invalidate_tasks_cache()
    return Q._idea_task(cfg, iid)


def prop(cfg, iid):
    return idea(cfg, iid).properties.get("CAMPAIGN")


def types(cfg, cid, *t):
    return list(S.read_ledger(cfg, cid, types=list(t)))


def _merge(cfg, monkeypatch, cid, next_state="MERGED"):
    """Open the campaign, then close it as `next_state` through `_apply`."""
    run_cli(cfg, monkeypatch, ["queue", "tick"])
    ctx = Q._Ctx(cfg)
    S.write_state(cfg, cid, dict(S.read_state(cfg, cid), state="CLOSING"))
    oc = CloseOutcome(next_state, close={"mode": "merge", "merged_sha": "abc", "pr_url": None,
                                         "gates_seq": 3, "target_sha": "def"},
                      events=[("close", {"mode": "merge"})])
    st = S.read_state(cfg, cid)
    res = Q.StepResult(cid, Q.Step("close"), st["state"], close_outcome=oc)
    Q._apply(ctx, st, res)
    return S.read_state(cfg, cid)


def _live_campaign(cfg, cid, state="IMPLEMENTING"):
    S.create_campaign(cfg, {"id": cid, "kind": "idea", "state": state, "spec_id": None,
                            "project": "Meta-Tools", "profile": "balanced"})


# ---- link at add -----------------------------------------------------------------------------

def test_spec_add_links_idea(tmp_path, monkeypatch):
    cfg, _, iid = _world(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch)
    assert prop(cfg, iid) == cid
    assert S.read_state(cfg, cid)["idea_link"] == {"id": iid}          # the second write
    (ev,) = types(cfg, cid, "idea_linked")
    assert ev["idea_id"] == iid
    assert not types(cfg, cid, "idea_link_skipped")


def test_spec_add_mismatch_no_write(tmp_path, monkeypatch):
    cfg, _, iid = _world(tmp_path, monkeypatch, old=False)             # idea created after the spec
    cid = added(cfg, monkeypatch)
    assert prop(cfg, iid) is None
    (ev,) = types(cfg, cid, "idea_link_skipped")
    assert ev["spec_id"] == "SPEC-0001" and "flag-mismatch" in ev["reason"]
    assert "idea_link" not in S.read_state(cfg, cid) and not types(cfg, cid, "idea_linked")


def test_spec_add_without_source_idea_is_silent(tmp_path, monkeypatch):
    cfg, _, iid = _world(tmp_path, monkeypatch, with_source=False)
    cid = added(cfg, monkeypatch)
    assert prop(cfg, iid) is None and not types(cfg, cid, "idea_link_skipped")


def test_spec_add_does_not_steal_live_link(tmp_path, monkeypatch):
    cfg, _, iid = _world(tmp_path, monkeypatch)
    _live_campaign(cfg, "IDEA-001-live")
    OW.set_property(cfg, idea(cfg, iid), "CAMPAIGN", "IDEA-001-live")
    cid = added(cfg, monkeypatch)
    assert prop(cfg, iid) == "IDEA-001-live"
    (ev,) = types(cfg, cid, "idea_link_skipped")
    assert ev["reason"] == "held by IDEA-001-live"


@pytest.mark.parametrize("held", ["ABANDONED", None])
def test_spec_add_replaces_dead_link(tmp_path, monkeypatch, held):
    cfg, _, iid = _world(tmp_path, monkeypatch)
    if held:
        _live_campaign(cfg, "dead-1", state=held)
    OW.set_property(cfg, idea(cfg, iid), "CAMPAIGN", "dead-1")        # None: no such campaign
    cid = added(cfg, monkeypatch)
    assert prop(cfg, iid) == cid and types(cfg, cid, "idea_linked")


def test_link_failure_never_refuses_add(tmp_path, monkeypatch):
    cfg, _, iid = _world(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("org parse exploded")
    monkeypatch.setattr(Q.SP, "source_idea_task", boom)
    cid = added(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["state"] == "QUEUED" and prop(cfg, iid) is None
    (ev,) = types(cfg, cid, "idea_link_skipped")
    assert "RuntimeError" in ev["reason"] and "org parse exploded" in ev["reason"]


# ---- release at terminal states --------------------------------------------------------------

@pytest.mark.parametrize("outcome", ["MERGED", "HANDED_OFF"])
def test_merged_and_handed_off_clear_own_link(tmp_path, monkeypatch, outcome):
    cfg, _, iid = _world(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch)
    assert prop(cfg, iid) == cid
    st = _merge(cfg, monkeypatch, cid, outcome)
    assert st["state"] == outcome and prop(cfg, iid) is None
    (ev,) = types(cfg, cid, "idea_unlinked")
    assert ev["idea_id"] == iid


def test_terminal_keeps_foreign_link(tmp_path, monkeypatch):
    cfg, _, iid = _world(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch)
    OW.set_property(cfg, idea(cfg, iid), "CAMPAIGN", "someone-else")
    st = _merge(cfg, monkeypatch, cid)
    assert st["state"] == "MERGED" and prop(cfg, iid) == "someone-else"
    assert not types(cfg, cid, "idea_unlinked")


def test_release_runs_when_reconcile_skips(tmp_path, monkeypatch):
    """The early returns of `_reconcile_closed` (spec store outside the repo, no spec id) must
    not skip the release."""
    for variant in ("outbound", "no_spec_id"):
        sub = tmp_path / variant
        sub.mkdir()
        cfg, _, iid = _world(sub, monkeypatch)
        cid = added(cfg, monkeypatch)
        assert prop(cfg, iid) == cid
        run_cli(cfg, monkeypatch, ["queue", "tick"])
        st = S.read_state(cfg, cid)
        if variant == "outbound":
            other = sub / "other-project"
            other.mkdir()
            st["repo"] = dict(st["repo"], root=str(other))
        else:
            st.pop("spec_id", None)
            st["spec"] = None
        S.write_state(cfg, cid, st)
        out = _merge(cfg, monkeypatch, cid)
        assert out["state"] == "MERGED", variant
        assert prop(cfg, iid) is None, variant
        assert types(cfg, cid, "idea_unlinked"), variant


def test_release_clears_reused_id_twin(tmp_path, monkeypatch):
    cfg, _, iid = _world(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch)
    _, _, other = OW.add_idea(cfg, "reused id twin")
    OW.set_property(cfg, idea(cfg, other), "ID", iid)               # same :ID:, later in the file
    # the twin comes first in file order: move the link to the *second* carrier of the id
    twins = [t for t in ORG.filter_tasks(ORG.load_tasks(cfg), is_idea=True)
             if t.properties.get("ID") == iid]
    assert len(twins) == 2
    OW.clear_property(cfg, twins[0], "CAMPAIGN")
    ORG.invalidate_tasks_cache()
    twins = [t for t in ORG.filter_tasks(ORG.load_tasks(cfg), is_idea=True)
             if t.properties.get("ID") == iid]
    OW.set_property(cfg, twins[1], "CAMPAIGN", cid)
    st = _merge(cfg, monkeypatch, cid)
    assert st["state"] == "MERGED"
    ORG.invalidate_tasks_cache()
    left = [t for t in ORG.filter_tasks(ORG.load_tasks(cfg), is_idea=True)
            if t.properties.get("ID") == iid]
    assert left and all(not t.properties.get("CAMPAIGN") for t in left)
    assert len(types(cfg, cid, "idea_unlinked")) == 1


def test_abandon_spec_campaign_releases_link(tmp_path, monkeypatch):
    cfg, _, iid = _world(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--yes"])
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["state"] == "ABANDONED" and prop(cfg, iid) is None
    assert types(cfg, cid, "idea_unlinked")


@pytest.mark.parametrize("terminal", ["MERGED", "ABANDONED"])
def test_release_error_is_bookkeeping_finding(tmp_path, monkeypatch, terminal):
    cfg, _, iid = _world(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(OW, "clear_property", boom)
    if terminal == "MERGED":
        st = _merge(cfg, monkeypatch, cid)
    else:
        r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--yes"])
        assert r.exit_code == 0, r.output
        st = S.read_state(cfg, cid)
    assert st["state"] == terminal
    (fnd,) = [f for f in types(cfg, cid, "finding") if f["gate"] == "bookkeeping"
              and "unlink" in f["title"]]
    assert fnd["severity"] == "minor" and "disk full" in fnd["detail"] and iid in fnd["title"]
    assert not types(cfg, cid, "idea_unlinked")


def test_abandon_idea_campaign_ownership_check(tmp_path, monkeypatch):
    cfg, _, _ = _world(tmp_path, monkeypatch)
    _, _, iid = OW.add_idea(cfg, "an idea campaign", epic="SPEC-0169")
    r = run_cli(cfg, monkeypatch, ["queue", "add", iid, "--yes", "--json"])
    assert r.exit_code == 0, r.output
    import json
    cid = json.loads(r.output)["campaign"]
    assert prop(cfg, iid) == cid
    OW.set_property(cfg, idea(cfg, iid), "CAMPAIGN", "taken-over")
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--yes"])
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["state"] == "ABANDONED" and prop(cfg, iid) == "taken-over"


def test_link_events_validate():
    for etype, payload in (("idea_linked", {"idea_id": "IDEA-001"}),
                           ("idea_link_skipped", {"spec_id": "SPEC-1", "reason": "held by x"}),
                           ("idea_unlinked", {"idea_id": "IDEA-001"})):
        rec = {"schema": SC.LEDGER_SCHEMA, "ts": "t", "seq": 1, "campaign": "c", "type": etype,
               "by": "tick", **payload}
        assert SC.ledger_problems(rec) == [], etype
        assert SC.ledger_problems({k: v for k, v in rec.items() if k not in payload}), etype


def test_merged_done_spec_clears_link_on_the_promoted_idea(tmp_path, monkeypatch):
    """The real merge: the reconcile advances the idea to PROMOTED (archiving it) and the release
    still finds the archived idea by its `:CAMPAIGN:`."""
    cfg, repo, iid = _world(tmp_path, monkeypatch)
    cid = added(cfg, monkeypatch)
    spec = repo / "docs" / "specs" / "0001-trivial.md"
    spec.write_text(spec.read_text().replace("status: accepted", "status: done"))
    st = _merge(cfg, monkeypatch, cid)
    assert st["state"] == "MERGED"
    assert [e["to"] for e in types(cfg, cid, "idea_reconciled")] == ["PROMOTED"]
    ORG.invalidate_tasks_cache()
    left = [t for t in ORG.load_tasks(cfg) if t.is_idea and t.properties.get("ID") == iid]
    assert left and all(not t.properties.get("CAMPAIGN") for t in left)
    assert len(types(cfg, cid, "idea_unlinked")) == 1
