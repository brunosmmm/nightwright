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

from test_agentic_e2e import make_cfg, make_repo, write_review
from test_reconcile import TZ, _actions, _bytes, _cfg, _idea, _spec, _state_of
from nightwright import org_write as W
from nightwright import specs as S
from nightwright import spec_scope as SS
from nightwright import verify as V
from nightwright.agentic import queue as Q
from nightwright.agentic import store
from nightwright.cli import cli
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast

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
    (detail,) = [d for i, _s, a, d in plan if i == other and a == "flag-foreign"]
    assert PROJ in detail and "Meta-Tools" in detail          # the row names both projects
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


def test_write_source_idea_idea_not_found_raises_and_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    path = _spec(cfg, "SPEC-0001", "accepted", source_idea=None, created="2026-01-01")
    before = Path(path).read_bytes()
    with pytest.raises(ValueError, match="IDEA-999: idea not found; SPEC-0001 not linked"):
        S._write_source_idea(cfg, "SPEC-0001", "IDEA-999")
    assert Path(path).read_bytes() == before


def test_outbound_id_hand_off_ignores_project_scoping(tmp_path):
    """Outbound PROJ-NNNN ids are exempt: an idea promoted `--target` has no :PROJECT:, yet the
    campaign (project = the target) must still mark / clear it."""
    cfg = _cfg(tmp_path)
    iid = _idea(cfg, "outbound idea", state="SPECCED", spec_id="PROJ-0001")
    events, findings = [], []
    Q._mark_handed_off(cfg, {"project": PROJ, "close": {"pr_url": "https://x/pr/2"}}, events,
                       "PROJ-0001", lambda *a, **k: findings.append(a))
    assert [e[1]["idea_id"] for e in events] == [iid] and not findings
    held = lambda: W.resolve_selector(cfg, iid).properties.get("HANDED_OFF")  # noqa: E731
    assert held() == "https://x/pr/2"
    Q._clear_handed_off_on_requeue(cfg, "PROJ-0001", "cid", "t", project=PROJ)
    assert held() is None


def _promoted_internal(cfg, text):
    """A foreign-:PROJECT: idea promoted `--internal`: wt's SPEC-0001 names it as source_idea."""
    iid = _foreign(cfg, text)
    path = _spec(cfg, "SPEC-0001", "done", source_idea=iid)
    return iid, path


def test_foreign_project_idea_promoted_internal_still_reconciles(tmp_path):
    cfg = _cfg(tmp_path)
    iid, path = _promoted_internal(cfg, "home-lab idea promoted internal")
    before = Path(path).read_bytes()
    for apply in (False, True):
        plan = S.reconcile_ideas(cfg, apply=apply)
        assert (iid, "advance") in _actions(plan)
        assert "flag-foreign" not in {a for _i, _s, a, _d in plan}
    assert _state_of(cfg, iid) in ("PROMOTED", "SHIPPED")
    assert Path(path).read_bytes() == before


def test_hand_off_marks_foreign_project_idea_promoted_internal(tmp_path):
    cfg = _cfg(tmp_path)
    iid, _path = _promoted_internal(cfg, "home-lab idea promoted internal")
    W.set_state_by_selector(cfg, iid, "SPECCED")
    events = []
    Q._mark_handed_off(cfg, {"project": "Meta-Tools", "close": {"pr_url": "u"}}, events,
                       "SPEC-0001", lambda *a, **k: None)
    assert [e[1]["idea_id"] for e in events] == [iid]


def test_foreign_idea_hitting_unclaiming_wt_spec_is_flagged_without_write(tmp_path):
    cfg = _cfg(tmp_path)
    mine = _idea(cfg, "internal idea", state="SPECCED", spec_id="SPEC-0009")
    other = _foreign(cfg, "foreign idea")
    path = _spec(cfg, "SPEC-0001", "done", source_idea=mine)    # claims another idea, not `other`
    before = Path(path).read_bytes()
    for apply in (False, True):
        plan = S.reconcile_ideas(cfg, apply=apply)
        assert (other, "flag-foreign") in _actions(plan)
        assert not any(i == other and a != "flag-foreign" for i, _s, a, _d in plan)
        assert Path(path).read_bytes() == before
    assert _state_of(cfg, other) == "SPECCED"


def test_plan_spec_link_accepts_promoted_internal_foreign_idea(tmp_path):
    cfg = _cfg(tmp_path)
    iid = _foreign(cfg, "home-lab idea", spec_id="SPEC-0009")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=iid)
    assert S.plan_spec_link(cfg, iid, "SPEC-0001")["idea_id"] == iid


def test_reused_id_does_not_make_a_foreign_idea_wts_claimed_idea(tmp_path):
    """SPEC-0267 on the claim path: wt's old SPEC-0001 names IDEA-00X; a newer foreign idea that
    reuses the `:ID:` and links a bare SPEC-0001 is not that spec's idea."""
    cfg = _cfg(tmp_path)
    iid = _foreign(cfg, "newer idea of another project")      # created today, id reused
    path = _spec(cfg, "SPEC-0001", "done", source_idea=iid, created="2026-01-01")
    before = Path(path).read_bytes()
    task = W.resolve_selector(cfg, iid)
    assert SS.owns_idea(cfg, task) is False
    assert S._sole_claimant(load_tasks(cfg), "SPEC-0001", project=SS.INTERNAL_PROJECT,
                            cfg=cfg) is None
    for apply in (False, True):
        plan = S.reconcile_ideas(cfg, apply=apply)
        assert (iid, "flag-foreign") in _actions(plan)
        assert not any(i == iid and a != "flag-foreign" for i, _s, a, _d in plan)
    assert _state_of(cfg, iid) == "SPECCED"
    assert Path(path).read_bytes() == before
    # no :SPEC: link: the same rule applies to the claims-only branch
    W.set_property(cfg, W.resolve_selector(cfg, iid), "SPEC", "")
    assert SS.owns_idea(cfg, W.resolve_selector(cfg, iid)) is False


def _foreign_twin(cfg, idea_id, stamp="[2026-10-25 Sun 09:00]"):
    """A foreign idea carrying the same `:ID:` as an internal one, in a second org file."""
    path = Path(cfg["org_ideas_file"]).with_name("ideas-20261025.org")
    path.write_text("#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED SHIPPED DROPPED RESEARCHED\n"
                    f"* IDEA foreign twin\n:PROPERTIES:\n:ID: {idea_id}\n:PROJECT: {PROJ}\n"
                    f":CREATED: {stamp}\n:END:\n")


def test_internal_idea_sharing_an_id_with_a_foreign_one_advances_without_raising(tmp_path):
    cfg = _cfg(tmp_path)
    mine = _idea(cfg, "internal idea", state="SPECCED", spec_id="SPEC-0005")
    # no source_idea: the spec claims neither twin, so the foreign one is filtered from the walk
    _spec(cfg, "SPEC-0005", "done", source_idea=None, created="2026-10-30")
    W.set_property(cfg, W.resolve_selector(cfg, mine), "CREATED", "[2026-09-01 Tue 09:00]")
    _foreign_twin(cfg, mine)
    plan = S.reconcile_ideas(cfg, apply=True)            # an unpinned write would raise 'ambiguous'
    assert (mine, "advance") in _actions(plan)
    task = [t for t in load_tasks(cfg) if t.is_idea and t.heading == "internal idea"][0]
    assert (task.state or "").upper() == "PROMOTED"
    twin = [t for t in load_tasks(cfg) if t.is_idea and t.heading == "foreign twin"][0]
    assert (twin.state or "").upper() == "IDEA"


def test_foreign_id_twin_does_not_make_dry_run_and_apply_disagree(tmp_path):
    cfg = _cfg(tmp_path)
    path = _spec(cfg, "SPEC-0001", "accepted", source_idea=None, created="2026-10-01")
    mine = _idea(cfg, "internal idea", state="SPECCED", spec_id="SPEC-0001")
    W.set_property(cfg, W.resolve_selector(cfg, mine), "CREATED", "[2026-09-01 Tue 09:00]")
    _foreign_twin(cfg, mine, stamp="[2026-10-20 Tue 09:00]")   # newer than the spec: would win _pick_idea
    dry = S.reconcile_ideas(cfg, apply=False)
    applied = S.reconcile_ideas(cfg, apply=True)
    assert sorted(dry) == sorted(applied)
    assert f"source_idea: {mine}\n" in Path(path).read_text()


def test_dry_run_matches_apply_for_foreign_backlink(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    other = _foreign(cfg, "foreign idea")
    dry = S.reconcile_ideas(cfg, apply=False)
    applied = S.reconcile_ideas(cfg, apply=True)
    assert sorted(dry) == sorted(applied)
    assert "flag-no-backlink" not in {a for _i, _s, a, _d in dry}


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


def _spec_file(root, num, status="accepted", deps=()):
    d = Path(root) / "docs" / "specs"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"{num:04d}-*.md"):
        old.unlink()
    body = (f"---\nid: SPEC-{num:04d}\ntitle: t{num}\nstatus: {status}\ncreated: 2026-10-31\n"
            f"depends_on: [{', '.join(deps)}]\n---\n\n## Context\nx\n")
    (d / f"{num:04d}-s{num}.md").write_text(body)


def test_batch_add_follows_the_projects_own_specs(tmp_path):
    wt_repo = make_repo(tmp_path, review=False)
    p2 = _project_repo(tmp_path)
    cfg = make_cfg(tmp_path, wt_repo, tmp_path / "script.json",
                   outbox={PROJ: {"repo_path": str(p2)}})
    # wt: 0002 depends on 0001. project: 0001 depends on 0002 (the opposite) and on 0003
    _spec_file(wt_repo, 1)
    _spec_file(wt_repo, 2, deps=["SPEC-0001"])
    _spec_file(p2, 1, deps=["SPEC-0002", "SPEC-0003"])
    _spec_file(p2, 2)
    _spec_file(p2, 3, status="draft")
    for n in (1, 2):
        path = next((p2 / "docs" / "specs").glob(f"{n:04d}-*.md"))
        write_review(cfg, path, f"SPEC-{n:04d}")
    batch = Q.plan_add_batch(cfg, ["SPEC-0001", "SPEC-0002"], project=PROJ)
    assert [i["id"] for i in batch["items"]] == ["SPEC-0002", "SPEC-0001"]
    assert [i.get("error") for i in batch["items"]] == [None, None]
    first = {i["id"]: i for i in batch["items"]}["SPEC-0001"]
    assert first["would_block"] == ["SPEC-0003 is draft and not queued"]
    # wt's own batch still follows wt's files
    wt_batch = Q.plan_add_batch(cfg, ["SPEC-0002", "SPEC-0001"])
    assert [i["id"] for i in wt_batch["items"]] == ["SPEC-0001", "SPEC-0002"]
    # an active wt campaign for the same id does not satisfy the project's dependency ...
    _state_file(cfg, "c-wt3", spec_id="SPEC-0003")
    b = Q.plan_add_batch(cfg, ["SPEC-0001", "SPEC-0002"], project=PROJ)
    assert {i["id"]: i for i in b["items"]}["SPEC-0001"]["would_block"] == [
        "SPEC-0003 is draft and not queued"]
    # ... the project's own does
    _state_file(cfg, "c-p3", spec_id="SPEC-0003", project=PROJ)
    b = Q.plan_add_batch(cfg, ["SPEC-0001", "SPEC-0002"], project=PROJ)
    assert {i["id"]: i for i in b["items"]}["SPEC-0001"]["would_block"] == []
    # applying re-reads the project's order too
    Q.apply_add_batch(cfg, b)
    assert store.active_campaign_for_spec(cfg, "SPEC-0002", PROJ)


def test_list_campaigns_treats_a_state_without_project_as_internal(tmp_path):
    cfg = _cfg(tmp_path)
    _state_file(cfg, "c-old")
    p = store.campaigns_dir(cfg) / "c-old" / "state.json"
    st = json.loads(p.read_text())
    del st["project"]
    p.write_text(json.dumps(st))
    assert store.active_campaign_for_spec(cfg, "SPEC-0001", "Meta-Tools")["id"] == "c-old"
    assert store.active_campaign_for_spec(cfg, "SPEC-0001", PROJ) is None


def test_owns_idea_accepts_an_idea_named_by_a_wt_spec_without_a_spec_link(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "home-lab idea")                       # no :SPEC: yet
    iid = [t for t in load_tasks(cfg) if t.is_idea and t.heading == "home-lab idea"][0] \
        .properties.get("ID")
    W.set_property(cfg, W.resolve_selector(cfg, iid), "PROJECT", PROJ)
    task = W.resolve_selector(cfg, iid)
    assert SS.owns_idea(cfg, task) is False
    _spec(cfg, "SPEC-0001", "accepted", source_idea=iid)
    assert SS.owns_idea(cfg, task) is True


def test_foreign_scope_ignores_a_terminal_campaign_in_the_env(scoped, tmp_path, monkeypatch):
    cfg, _wt_repo, p2 = scoped
    st, _wtree = _campaign(cfg, p2, tmp_path)
    monkeypatch.setenv("NW_CAMPAIGN", "cid")
    monkeypatch.chdir(tmp_path)
    assert SS.foreign_scope_path(cfg, "SPEC-0001") is not None
    store.write_state(cfg, "cid", dict(st, state="MERGED"))
    assert SS.foreign_scope_path(cfg, "SPEC-0001") is None      # a stale NW_CAMPAIGN scopes nothing


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


# ---- review fixes: root fallback, claims parsed once, dry run == apply, presentation ----------

def test_campaign_spec_path_falls_back_to_repo_root_without_a_worktree(scoped, tmp_path):
    cfg, _wt_repo, p2 = scoped
    st, wtree = _campaign(cfg, p2, tmp_path, with_worktree=False)
    assert not wtree.exists()
    assert SS.campaign_spec_path(cfg, st) == p2 / "docs/specs/0001-trivial.md"
    assert SS.foreign_scope_path(cfg, "SPEC-0001", campaign="cid")[0] == \
        p2 / "docs/specs/0001-trivial.md"
    # another id of the project: the worktree is skipped, the repo root answers
    assert SS.foreign_scope_path(cfg, "SPEC-0003", campaign="cid")[0].parent == p2 / "docs/specs"
    missing = dict(st, spec={"rel_path": "docs/specs/0099-nope.md"})
    with pytest.raises(ValueError, match="not found in its worktree or repo"):
        SS.campaign_spec_path(cfg, missing)
    with pytest.raises(ValueError, match="ambiguous in a comfort-assistant campaign") as e:
        SS.foreign_scope_path(cfg, "SPEC-0099", campaign="cid")
    assert "Meta-Tools" in str(e.value)                       # names both projects


def test_campaign_spec_path_prefers_the_worktree_copy(scoped, tmp_path):
    cfg, _wt_repo, p2 = scoped
    st, wtree = _campaign(cfg, p2, tmp_path)
    assert SS.campaign_spec_path(cfg, st) == wtree / "docs/specs/0001-trivial.md"
    (wtree / "docs/specs/0001-trivial.md").unlink()           # gone there: the root's copy answers
    assert SS.campaign_spec_path(cfg, st) == p2 / "docs/specs/0001-trivial.md"


def test_wt_campaign_naming_an_internal_or_unknown_campaign_scopes_nothing(
        scoped, tmp_path, monkeypatch):
    cfg, wt_repo, p2 = scoped
    _state_file(cfg, "c-wt")                                  # a Meta-Tools campaign
    monkeypatch.chdir(wt_repo)
    for cid in ("c-wt", "no-such-campaign"):
        monkeypatch.setenv("NW_CAMPAIGN", cid)
        assert SS.foreign_scope_path(cfg, "SPEC-0001") is None
        assert SS.resolve_in_scope(cfg, "SPEC-0001") == wt_repo / "docs/specs/0001-trivial.md"
    # an internal campaign does not hide the cwd: inside a project checkout the project answers
    monkeypatch.setenv("NW_CAMPAIGN", "c-wt")
    path, origin = SS.foreign_scope_path(cfg, "SPEC-0001", cwd=p2)
    assert path == p2.resolve() / "docs/specs/0001-trivial.md" and PROJ in origin


def test_foreign_scope_matches_the_campaign_spec_id_case_insensitively(scoped, tmp_path):
    cfg, _wt_repo, p2 = scoped
    _st, wtree = _campaign(cfg, p2, tmp_path)
    path, origin = SS.foreign_scope_path(cfg, "spec-0001", campaign="cid")
    assert path == wtree / "docs/specs/0001-trivial.md" and "cid" in origin


def test_plan_add_missing_bare_id_of_a_second_project_never_reads_wt_store(tmp_path):
    wt_repo = make_repo(tmp_path, review=False)
    p2 = _project_repo(tmp_path)
    cfg = make_cfg(tmp_path, wt_repo, tmp_path / "script.json",
                   outbox={PROJ: {"repo_path": str(p2)}})
    (wt_repo / "docs/specs/0099-wt-only.md").write_text(
        "---\nid: SPEC-0099\ntitle: x\nstatus: accepted\n---\n")
    assert Q.plan_add(cfg, "SPEC-0099", force=True)["repo_root"] == str(wt_repo.resolve())
    with pytest.raises(ValueError, match=f"SPEC-0099 not found in project {PROJ}"):
        Q.plan_add(cfg, "SPEC-0099", project=PROJ, force=True)


def test_apply_add_forwards_the_plans_project_to_the_hand_off_clear(tmp_path, monkeypatch):
    wt_repo = make_repo(tmp_path, review=False)
    p2 = _project_repo(tmp_path)
    cfg = make_cfg(tmp_path, wt_repo, tmp_path / "script.json",
                   outbox={PROJ: {"repo_path": str(p2)}})
    plan = Q.plan_add(cfg, "SPEC-0001", project=PROJ, force=True)
    assert plan["project"] == PROJ
    seen = []
    monkeypatch.setattr(Q, "_clear_handed_off_on_requeue",
                        lambda c, spec_id, campaign, by, project=None: seen.append((spec_id, project)))
    Q.apply_add(cfg, plan)
    assert seen == [("SPEC-0001", PROJ)]


def test_internal_campaign_marks_only_the_internal_idea(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)     # claims neither idea
    mine = _idea(cfg, "internal idea", state="SPECCED", spec_id="SPEC-0001")
    other = _foreign(cfg, "foreign idea")
    for iid in (mine, other):
        W.set_property(cfg, W.resolve_selector(cfg, iid), "HANDED_OFF", "old")
    events = []
    Q._mark_handed_off(cfg, {"project": "Meta-Tools", "close": {"pr_url": "https://x/pr/3"}},
                       events, "SPEC-0001", lambda *a, **k: None)
    held = lambda i: W.resolve_selector(cfg, i).properties.get("HANDED_OFF")  # noqa: E731
    assert [e[1]["idea_id"] for e in events] == [mine]
    assert held(mine) == "https://x/pr/3" and held(other) == "old"
    Q._clear_handed_off_on_requeue(cfg, "SPEC-0001", "cid", "t", project="Meta-Tools")
    assert held(mine) is None and held(other) == "old"


def test_reconcile_parses_wts_specs_once_per_pass(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    for n in range(3):                                        # foreign ideas with no :SPEC: yet
        W.add_idea(cfg, f"foreign backlog idea {n}")
        task = [t for t in load_tasks(cfg) if t.is_idea and t.heading == f"foreign backlog idea {n}"][0]
        W.set_property(cfg, task, "PROJECT", PROJ)
    calls = []
    real = S._spec_claims
    monkeypatch.setattr(S, "_spec_claims", lambda c: calls.append(1) or real(c))
    S.reconcile_ideas(cfg, apply=True)
    assert len(calls) == 1
    calls.clear()
    S.reconcile_ideas(cfg, apply=False, spec_ids=["SPEC-0001"])
    assert len(calls) == 1


def test_dry_run_matches_apply_when_the_backlink_is_refused_for_a_newer_idea(tmp_path):
    cfg = _cfg(tmp_path)
    old = _spec(cfg, "SPEC-0001", "accepted", source_idea=None, created="2026-01-01")
    newer = _idea(cfg, "created after SPEC-0001", state="SPECCED", spec_id="SPEC-0001")
    before = Path(old).read_bytes()
    dry = S.reconcile_ideas(cfg, apply=False)
    assert (newer, "flag-mismatch") in _actions(dry)
    applied = S.reconcile_ideas(cfg, apply=True)
    assert sorted(dry) == sorted(applied)
    assert [a for i, _s, a, _d in applied if i == newer].count("flag-mismatch") == 1
    assert Path(old).read_bytes() == before


def test_backlink_writes_share_one_idea_load_per_pass(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for n in (1, 2, 3):
        _spec(cfg, f"SPEC-000{n}", "accepted", source_idea=None)
        _idea(cfg, f"idea {n}", state="SPECCED", spec_id=f"SPEC-000{n}")
    seen = []
    real = S._write_source_idea
    monkeypatch.setattr(S, "_write_source_idea",
                        lambda c, sid, iid, ideas=None: seen.append(ideas is not None)
                        or real(c, sid, iid, ideas=ideas))
    S.reconcile_ideas(cfg, apply=True)
    assert seen == [True, True, True]
    for n in (1, 2, 3):
        assert "source_idea: IDEA-" in (Path(cfg["specs_dir"]) / f"000{n}-fixture.md").read_text()
    # called alone it still loads the org files itself
    _spec(cfg, "SPEC-0004", "accepted", source_idea=None)
    solo = _idea(cfg, "idea 4", state="SPECCED", spec_id="SPEC-0004")
    S._write_source_idea(cfg, "SPEC-0004", solo)
    assert f"source_idea: {solo}" in (Path(cfg["specs_dir"]) / "0004-fixture.md").read_text()


def test_close_hook_reports_a_foreign_idea_as_a_minor_finding(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea=None)
    other = _foreign(cfg, "foreign idea")
    events = []
    Q._reconcile_closed(cfg, {"id": "cid", "spec_id": "SPEC-0001", "project": "Meta-Tools",
                              "repo": {"root": str(tmp_path / "repo")}}, events, "MERGED")
    (fnd,) = [e[1] for e in events if e[0] == "finding" and "foreign" in e[1]["title"]]
    assert fnd["severity"] == "minor" and fnd["gate"] == "bookkeeping"
    assert other in fnd["detail"] and PROJ in fnd["detail"] and "Meta-Tools" in fnd["detail"]
    assert _state_of(cfg, other) == "SPECCED"


def test_spec_reconcile_cli_prints_foreign_rows(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    other = _foreign(cfg, "foreign idea")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "reconcile"])
    out = r.output + capsys.readouterr().out
    assert r.exit_code == 0, out
    line = next(ln for ln in out.splitlines() if other in ln)
    assert "↗" in line and PROJ in line and "Meta-Tools" in line

