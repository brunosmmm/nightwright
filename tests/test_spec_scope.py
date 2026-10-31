"""SPEC-0327: spec ids are per project. A second project's bare `SPEC-NNNN` never resolves against
wt's own `docs/specs`: idea reconcile, hand-off marks, active-campaign checks and the spec CLI
all scope by project. Offline, temp dirs and git repos."""
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from test_agentic_e2e import make_cfg, make_repo
from test_reconcile import TZ, _actions, _bytes, _cfg, _idea, _spec, _state_of
from nightwright import org_write as W
from nightwright import specs as S
from nightwright import spec_scope as SS
from nightwright import verify as V
from nightwright.agentic import queue as Q
from nightwright.agentic import store
from nightwright.cli import cli
from nightwright.org import load_tasks

PROJ = "comfort-assistant"
TODAY = dt.datetime.now(TZ).strftime("%Y-%m-%d")


def _foreign(cfg, text, spec_id="SPEC-0001"):
    """An idea of the second project linking a bare spec id."""
    iid = _idea(cfg, text, state="SPECCED", spec_id=spec_id)
    W.set_property(cfg, W.resolve_selector(cfg, iid), "PROJECT", PROJ)
    return iid


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _project_repo(tmp_path):
    """The second project's repo: its own accepted SPEC-0001 (wt's lives elsewhere)."""
    sub = tmp_path / "p2"
    sub.mkdir()
    repo = make_repo(sub, review=False, policy="pr")
    return repo


# ---- reconcile ---------------------------------------------------------------------------

def test_reconcile_never_writes_foreign_backlink(tmp_path):
    cfg = _cfg(tmp_path)
    path = _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    mine = _idea(cfg, "internal idea", state="SPECCED", spec_id="SPEC-0001")
    other = _foreign(cfg, "foreign idea")
    # the foreign idea is older: without the filter it would be one of two claimants
    before = Path(path).read_bytes()
    plan = S.reconcile_ideas(cfg, apply=True)
    assert (other, "flag-foreign") in _actions(plan)
    assert not any(i == other and a != "flag-foreign" for i, _s, a, _d in plan)
    # the internal idea is the sole claimant again and is back-linked as before
    assert f"source_idea: {mine}\n" in Path(path).read_text()
    assert f"source_idea: {other}" not in Path(path).read_text()
    assert Path(path).read_bytes() != before


def test_reconcile_foreign_only_leaves_wt_spec_byte_identical(tmp_path):
    cfg = _cfg(tmp_path)
    path = _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    other = _foreign(cfg, "foreign idea")
    before = Path(path).read_bytes()
    plan = S.reconcile_ideas(cfg, apply=True)
    assert Path(path).read_bytes() == before
    assert (other, "flag-foreign") in _actions(plan) and "flag-no-backlink" not in {
        a for _i, _s, a, _d in plan}
    assert _state_of(cfg, other) == "SPECCED"


def test_wt_spec_naming_foreign_idea_is_flagged_foreign(tmp_path):
    cfg = _cfg(tmp_path)
    other = _foreign(cfg, "foreign idea", spec_id="SPEC-0009")
    path = _spec(cfg, "SPEC-0001", "done", source_idea=other)
    before, org_before = Path(path).read_bytes(), _bytes(cfg)
    for apply in (False, True):
        plan = S.reconcile_ideas(cfg, apply=apply)
        assert (other, "flag-foreign") in _actions(plan)
        assert any(i == other and s == "SPEC-0001" and "belongs to" in d
                   for i, s, a, d in plan if a == "flag-foreign")
        assert not {a for _i, _s, a, _d in plan} & {"advance", "link", "flag-no-backlink"}
        assert Path(path).read_bytes() == before and _bytes(cfg) == org_before


def test_refused_backlink_write_is_flag_mismatch_and_pass_continues(tmp_path):
    cfg = _cfg(tmp_path)
    old = _spec(cfg, "SPEC-0001", "accepted", source_idea=None, created="2026-01-01")
    fine = _spec(cfg, "SPEC-0002", "accepted", source_idea=None)
    newer = _idea(cfg, "created after SPEC-0001", state="SPECCED", spec_id="SPEC-0001")
    good = _idea(cfg, "fine idea", state="SPECCED", spec_id="SPEC-0002")
    before = Path(old).read_bytes()
    plan = S.reconcile_ideas(cfg, apply=True, backlinks=True)     # must not raise
    rows = [(i, s, a) for i, s, a, _d in plan]
    assert (newer, "SPEC-0001", "flag-mismatch") in rows
    assert Path(old).read_bytes() == before                       # the refused write left no trace
    assert f"source_idea: {good}\n" in Path(fine).read_text()    # the pass went on to the next one


def test_reconcile_foreign_idea_with_outbound_id_still_advances(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["outbox_dir"] = str(tmp_path / "outbox")
    (tmp_path / "outbox" / PROJ).mkdir(parents=True)
    (tmp_path / "outbox" / PROJ / "PROJ-0001-x.md").write_text(
        f"---\nid: PROJ-0001\ntitle: x\nstatus: done\ncreated: {TODAY}\n---\n\nbody\n")
    iid = _idea(cfg, "outbound idea", state="IDEA", spec_id="PROJ-0001")
    W.set_property(cfg, W.resolve_selector(cfg, iid), "PROJECT", PROJ)
    W.set_state_by_selector(cfg, iid, "EXPORTED")
    plan = S.reconcile_ideas(cfg, apply=True)
    assert (iid, "advance") in _actions(plan)
    assert "flag-foreign" not in {a for _i, _s, a, _d in plan}


def test_write_source_idea_refuses_foreign_and_newer_idea(tmp_path):
    cfg = _cfg(tmp_path)
    old = _spec(cfg, "SPEC-0001", "accepted", source_idea=None, created="2026-01-01")
    new = _spec(cfg, "SPEC-0002", "accepted", source_idea=None)
    newer = _idea(cfg, "created after SPEC-0001", state="SPECCED", spec_id="SPEC-0001")
    other = _foreign(cfg, "foreign idea", spec_id="SPEC-0002")
    snap = {p: Path(p).read_bytes() for p in (old, new)}
    with pytest.raises(ValueError, match="after spec created"):
        S._write_source_idea(cfg, "SPEC-0001", newer)
    with pytest.raises(ValueError, match=f"belongs to {PROJ}"):
        S._write_source_idea(cfg, "SPEC-0002", other)
    assert {p: Path(p).read_bytes() for p in (old, new)} == snap


def test_plan_spec_link_refuses_foreign_idea(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    iid = _idea(cfg, "foreign idea", state="IDEA", spec_id="SPEC-0009")
    W.set_property(cfg, W.resolve_selector(cfg, iid), "PROJECT", PROJ)
    with pytest.raises(ValueError, match=f"{iid} belongs to {PROJ}"):
        S.plan_spec_link(cfg, iid, "SPEC-0001")


# ---- queue bookkeeping keyed by a bare id ------------------------------------------------

def test_mark_and_clear_handed_off_are_project_scoped(tmp_path):
    cfg = _cfg(tmp_path)
    mine = _idea(cfg, "internal idea", state="SPECCED", spec_id="SPEC-0001")
    other = _foreign(cfg, "foreign idea")
    for iid in (mine, other):
        W.set_property(cfg, W.resolve_selector(cfg, iid), "HANDED_OFF", "old")
    events, findings = [], []
    Q._mark_handed_off(cfg, {"project": PROJ, "close": {"pr_url": "https://x/pr/1"}}, events,
                       "SPEC-0001", lambda *a, **k: findings.append(a))
    assert [e[1]["idea_id"] for e in events] == [other]
    held = lambda i: W.resolve_selector(cfg, i).properties.get("HANDED_OFF")  # noqa: E731
    assert held(other) == "https://x/pr/1" and held(mine) == "old"
    Q._clear_handed_off_on_requeue(cfg, "SPEC-0001", "cid", "t", project=PROJ)
    assert held(other) is None and held(mine) == "old"


def _state_file(cfg, cid, **over):
    st = {"id": cid, "spec_id": "SPEC-0001", "project": "Meta-Tools", "state": "IMPLEMENTING",
          "created_at": "2026-10-31T00:00:00Z"}
    st.update(over)
    d = store.campaigns_dir(cfg) / cid
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(st))
    return st


def test_active_campaign_for_spec_is_project_scoped(tmp_path):
    cfg = _cfg(tmp_path)
    _state_file(cfg, "c-wt")
    assert store.active_campaign_for_spec(cfg, "SPEC-0001")["id"] == "c-wt"
    assert store.active_campaign_for_spec(cfg, "SPEC-0001", PROJ) is None
    _state_file(cfg, "c-p2", project=PROJ)
    assert store.active_campaign_for_spec(cfg, "SPEC-0001", PROJ)["id"] == "c-p2"
    assert store.active_campaign_for_spec(cfg, "SPEC-0001", "Meta-Tools")["id"] == "c-wt"


def test_plan_add_same_spec_id_two_projects(tmp_path):
    wt_repo = make_repo(tmp_path, review=False)
    p2 = _project_repo(tmp_path)
    cfg = make_cfg(tmp_path, wt_repo, tmp_path / "script.json",
                   outbox={PROJ: {"repo_path": str(p2)}})
    _state_file(cfg, "c-wt")                                  # wt's SPEC-0001 is active
    plan = Q.plan_add(cfg, "SPEC-0001", project=PROJ, force=True)
    assert Path(plan["worktree"]).parent.name == PROJ
    assert plan["spec_rel_path"] == "docs/specs/0001-trivial.md"
    assert plan["repo_root"] == str(p2.resolve())
    # the reverse: the second project's campaign does not block wt's own
    Q.apply_add(cfg, plan)
    store.write_state(cfg, "c-wt", dict(store.read_state(cfg, "c-wt"), state="ABANDONED"))
    _state_file(cfg, "c-wt2")
    with pytest.raises(ValueError, match="already has an active campaign c-wt2"):
        Q.plan_add(cfg, "SPEC-0001", force=True)
    store.write_state(cfg, "c-wt2", dict(store.read_state(cfg, "c-wt2"), state="ABANDONED"))
    assert Q.plan_add(cfg, "SPEC-0001", force=True)["repo_root"] == str(wt_repo.resolve())


# ---- resolve_in_scope --------------------------------------------------------------------

@pytest.fixture
def scoped(tmp_path):
    """wt (with a SPEC-0001) and a second project (with its own SPEC-0001 and a SPEC-0003)."""
    wt_repo = make_repo(tmp_path, review=False)
    p2 = _project_repo(tmp_path)
    (p2 / "docs" / "specs" / "0003-only-here.md").write_text("---\nid: SPEC-0003\n---\n")
    cfg = make_cfg(tmp_path, wt_repo, tmp_path / "script.json",
                   outbox={PROJ: {"repo_path": str(p2)}})
    return cfg, wt_repo, p2


def _campaign(cfg, p2, tmp_path, with_worktree=True):
    wtree = tmp_path / "wtrees" / "cid"
    if with_worktree:
        _git(p2, "worktree", "add", "-q", "-b", "wt/cid", str(wtree))
    return _state_file(cfg, "cid", project=PROJ, repo={"root": str(p2), "worktree": str(wtree)},
                       spec={"rel_path": "docs/specs/0001-trivial.md"}), wtree


def test_resolve_in_scope_campaign_env(scoped, tmp_path, monkeypatch):
    cfg, wt_repo, p2 = scoped
    _st, wtree = _campaign(cfg, p2, tmp_path)
    monkeypatch.setenv("NW_CAMPAIGN", "cid")
    got = SS.resolve_in_scope(cfg, "SPEC-0001", cwd=tmp_path)
    assert got == wtree / "docs/specs/0001-trivial.md"
    # another id of the same project resolves in its repo; one absent there is refused, never wt's
    assert SS.resolve_in_scope(cfg, "SPEC-0003", cwd=tmp_path).parent == p2 / "docs/specs"
    (wt_repo / "docs/specs/0002-wt-only.md").write_text("---\nid: SPEC-0002\n---\n")
    with pytest.raises(ValueError, match="ambiguous in a comfort-assistant campaign"):
        SS.resolve_in_scope(cfg, "SPEC-0002", cwd=tmp_path)
    monkeypatch.delenv("NW_CAMPAIGN")
    assert SS.resolve_in_scope(cfg, "SPEC-0002", cwd=wt_repo) == wt_repo / "docs/specs/0002-wt-only.md"


def test_spec_status_set_in_campaign_leaves_wt_spec_untouched(scoped, tmp_path, monkeypatch):
    cfg, wt_repo, p2 = scoped
    _st, wtree = _campaign(cfg, p2, tmp_path)
    wt_spec = wt_repo / "docs/specs/0001-trivial.md"
    before = wt_spec.read_bytes()
    monkeypatch.setenv("NW_CAMPAIGN", "cid")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "status", "SPEC-0001", "--set", "in-progress"])
    assert r.exit_code == 0, r.output
    assert "status: in-progress" in (wtree / "docs/specs/0001-trivial.md").read_text()
    assert wt_spec.read_bytes() == before
    r = CliRunner().invoke(cli, ["spec", "status", "SPEC-0002", "--set", "done"])
    assert r.exit_code != 0 and "ambiguous in a comfort-assistant campaign" in r.output
    assert wt_spec.read_bytes() == before


@pytest.mark.parametrize("linked", [False, True])
def test_cwd_in_foreign_project_does_not_fall_back_to_wt_store(scoped, tmp_path, linked,
                                                                monkeypatch):
    cfg, wt_repo, p2 = scoped
    monkeypatch.delenv("NW_CAMPAIGN", raising=False)
    cwd = p2
    if linked:
        cwd = tmp_path / "wtrees" / "other"
        _git(p2, "worktree", "add", "-q", "-b", "wt/other", str(cwd))
    top = cwd.resolve()
    assert SS.resolve_in_scope(cfg, "SPEC-0001", cwd=cwd) == top / "docs/specs/0001-trivial.md"
    (wt_repo / "docs/specs/0002-wt-only.md").write_text("---\nid: SPEC-0002\n---\n")
    with pytest.raises(ValueError, match="ambiguous in a comfort-assistant"):
        SS.resolve_in_scope(cfg, "SPEC-0002", cwd=cwd)
    # `wt spec review` resolves through the same scope
    path, origin = S.resolve_spec_path_for_cwd(cfg, "SPEC-0001", cwd=cwd)
    assert path == top / "docs/specs/0001-trivial.md" and PROJ in origin
    with pytest.raises(ValueError, match="ambiguous"):
        S.resolve_spec_path_for_cwd(cfg, "SPEC-0002", cwd=cwd)
    # `wt spec verify` too: the project's file is audited, wt's store is never consulted
    monkeypatch.chdir(cwd)
    assert V.verify_spec(cfg, "SPEC-0001")["path"] == str(top / "docs/specs/0001-trivial.md")
    with pytest.raises(ValueError, match="ambiguous in a comfort-assistant"):
        V.verify_spec(cfg, "SPEC-0002")


def test_spec_verify_in_campaign_audits_the_campaign_copy(scoped, tmp_path, monkeypatch):
    cfg, wt_repo, p2 = scoped
    _st, wtree = _campaign(cfg, p2, tmp_path)
    (wt_repo / "docs/specs/0002-wt-only.md").write_text("---\nid: SPEC-0002\n---\n")
    monkeypatch.setenv("NW_CAMPAIGN", "cid")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.chdir(tmp_path)
    assert V.verify_spec(cfg, "SPEC-0001")["path"] == str(wtree / "docs/specs/0001-trivial.md")
    with pytest.raises(ValueError, match="ambiguous in a comfort-assistant campaign"):
        V.verify_spec(cfg, "SPEC-0002")
    r = CliRunner().invoke(cli, ["spec", "verify", "SPEC-0002"])
    assert r.exit_code != 0 and "ambiguous in a comfort-assistant campaign" in r.output
