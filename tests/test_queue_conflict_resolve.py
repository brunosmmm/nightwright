"""SPEC-0463: resolve_dependency_conflicts, worktree resume, conflict mail."""
import hashlib
import os
from pathlib import Path

import pytest

from nightwright.agentic import close as CL
from nightwright.agentic import gitops as git
from nightwright.agentic import mailbox as M
from nightwright.agentic import queue as Q
from nightwright.agentic import quiet as QT
from nightwright.agentic import spec_review as SR
from nightwright.agentic import store as S

from test_agentic_idea_campaign import add_idea_campaign, capture, env  # noqa: F401

ACCEPTED = """---
id: {sid}
title: t
status: accepted
owner: user
created: 2026-11-07
---

body
"""

PROPOSED = ACCEPTED.replace("status: accepted", "status: proposed")
DRAFT = ACCEPTED.replace("status: accepted", "status: draft")
DONE = ACCEPTED.replace("status: accepted", "status: done")


def _state(cfg, cid):
    return S.read_state(cfg, cid)


def _no_campaign_waiter(cfg, monkeypatch):
    missing = capture(cfg, "missing prerequisite")
    waiter = capture(cfg, "waiter")
    cid = add_idea_campaign(cfg, monkeypatch, waiter, "--after", missing)
    return missing, cid, _state(cfg, cid)


def _ctx_blocked_long(cfg, cid, blocked_by):
    proj = _state(cfg, cid)["project"]
    ev = {"type": "backlog_blocked_long", "campaign": cid, "project": proj,
          "data": {"blocked_by": blocked_by}}
    return QT.Ctx(cfg, "conductor"), ev


# ---- AC4 / AC5 ------------------------------------------------------------------------------

def test_resolve_dependency_conflicts_mails_once(env, monkeypatch):
    cfg, _repo = env
    idea, cid, st = _no_campaign_waiter(cfg, monkeypatch)
    n_before = len([c for c in S.list_campaigns(cfg)
                    if (c.get("idea") or {}).get("id") == idea])
    Q.resolve_dependency_conflicts(cfg, [st])
    Q.resolve_dependency_conflicts(cfg, [st])
    reqs = [m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]
    assert len(reqs) == 1
    assert reqs[0]["needs_reply"] and reqs[0]["campaign"] == cid
    assert reqs[0]["text"] == (f"{cid} waits on {idea}. {idea} has no campaign and no spec. "
                               f"Do not queue {idea}.")
    assert len([c for c in S.list_campaigns(cfg)
                if (c.get("idea") or {}).get("id") == idea]) == n_before
    assert idea in st["conflict_mail"]
    ready, blocked = Q.backlog_readiness(cfg, st)
    assert not ready
    line = f"mailed conductor: {idea} has no campaign ({cid})"
    assert blocked == [line]
    ctx, ev = _ctx_blocked_long(cfg, cid, blocked)
    assert QT.dependency_wait(blocked, ctx.live_specs, ev["project"])
    assert QT.classify(cfg, ev, ctx) == "hold"
    pre = [f"waiting on {idea} (after: no campaign queued for {idea})"]
    ev_pre = {**ev, "data": {"blocked_by": pre}}
    assert QT.classify(cfg, ev_pre, ctx) == "wake"


def test_tick_persists_conflict_mail(env, monkeypatch):
    cfg, _repo = env
    _, cid, _st = _no_campaign_waiter(cfg, monkeypatch)
    Q.tick(cfg)
    st = _state(cfg, cid)
    idea = st["after"][0]["ref"]
    assert idea in (st.get("conflict_mail") or [])
    assert any("mailed conductor:" in b for b in (st.get("backlog") or {}).get("blocked_by") or [])
    n = len([m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"])
    Q.tick(cfg)
    assert len([m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]) == n


# ---- AC6: worktree resume -------------------------------------------------------------------

def _prep_worktree(cfg, repo, cid, spec_text, *, spec_id="SPEC-0999", rel="docs/specs/0999-x.md",
                    extra_state=None):
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    base = st["repo"].get("base") or "main"
    base_sha = git.rev_parse(root, base)
    git.worktree_add(root, wt, st["repo"]["branch"], base_sha)
    spec_path = wt / rel
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(spec_text.format(sid=spec_id), encoding="utf-8")
    st["spec"] = {"id": spec_id, "rel_path": rel, "internal": True}
    st["spec_id"] = spec_id
    st["repo"]["base_sha_at_open"] = base_sha
    if extra_state:
        st.update(extra_state)
    S.write_state(cfg, cid, st)
    return _state(cfg, cid)


def _open_resume(cfg, monkeypatch, st):
    head_before = git.rev_parse(Path(st["repo"]["root"]), st["repo"]["branch"])
    monkeypatch.setattr(git, "worktree_add",
                        lambda *a, **k: pytest.fail("worktree_add should not run"))
    monkeypatch.setattr(Q, "_run_worktree_setup",
                        lambda *a, **k: pytest.fail("setup should not run"))
    Q._transition(cfg, st, "OPENING", "test_admit")
    st = _state(cfg, st["id"])
    ctx = Q._Ctx(cfg)
    res = Q._do_open(ctx, st)
    row = Q._apply(ctx, st, res)
    head_after = git.rev_parse(Path(st["repo"]["root"]), st["repo"]["branch"])
    assert head_before == head_after
    return row, _state(cfg, st["id"])


@pytest.mark.parametrize("status,text", [
    ("accepted", ACCEPTED),
    ("in-progress", ACCEPTED.replace("status: accepted", "status: in-progress")),
])
def test_resume_accepted_spec_to_implementing(env, monkeypatch, status, text):
    cfg, repo = env
    iid = capture(cfg, "impl")
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    st = _prep_worktree(cfg, repo, cid, text)
    row, after = _open_resume(cfg, monkeypatch, st)
    assert row["reason"] == "resumed_worktree" and after["state"] == "IMPLEMENTING"
    notes = [e["text"] for e in S.read_ledger(cfg, cid, types=["note"])]
    assert any(n.startswith("resumed into existing worktree → IMPLEMENTING") for n in notes)


def test_resume_proposed_spec_to_spec_review(env, monkeypatch):
    cfg, repo = env
    iid = capture(cfg, "review")
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    st = _prep_worktree(cfg, repo, cid, PROPOSED, extra_state={"pending_brief": ""})
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "SPEC_REVIEW" and row["reason"] == "resumed_worktree"
    assert Q.plan_step(after, Q._Ctx(cfg).profile(after)).kind == "spec_review"


def test_resume_draft_to_spec_writing(env, monkeypatch):
    cfg, repo = env
    iid = capture(cfg, "write")
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    st = _prep_worktree(cfg, repo, cid, DRAFT, spec_id=None, extra_state={"spec_id": None})
    st["spec"]["id"] = None
    S.write_state(cfg, cid, st)
    st = _state(cfg, cid)
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"
    assert Q.plan_step(after, Q._Ctx(cfg).profile(after)).kind == "write_spec"


def test_resume_draft_with_spec_id_parks(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "bad draft"))
    st = _prep_worktree(cfg, repo, cid, DRAFT)
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "worktree:draft" not in (after.get("conflict_mail") or [])
    assert not [m for m in M.read_inbox(cfg, "conductor")
                if m.get("kind") == "request" and cid in (m.get("text") or "")]


def test_resume_missing_spec_with_id_parks(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "no file"))
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    st["spec"] = {"id": "SPEC-0999", "rel_path": "docs/specs/missing.md", "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "missing" in after["park"]["detail"]
    assert "docs/specs/missing.md" in after["park"]["detail"]
    assert wt.is_dir()


def test_fresh_open_keeps_trusted_spec_snapshot(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "trusted fresh"))
    st = _state(cfg, cid)
    rel = "docs/specs/0999-x.md"
    st["spec"] = {"rel_path": rel, "id": "SPEC-0999", "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["state"] = "OPENING"
    snap_text = "old trusted open snapshot\n"
    digest = hashlib.sha256(snap_text.encode()).hexdigest()
    st["spec"]["sha256_at_open"] = digest
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / CL.SPEC_AT_OPEN).write_text(snap_text, encoding="utf-8")
    S.write_state(cfg, cid, st)
    wt_text = ACCEPTED.format(sid="SPEC-0999")
    real_add = git.worktree_add

    def add_and_seed(root, path, branch, base):
        real_add(root, path, branch, base)
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(wt_text, encoding="utf-8")

    monkeypatch.setattr(git, "worktree_add", add_and_seed)
    monkeypatch.setattr(Q, "_run_worktree_setup", lambda *a, **k: None)
    ctx = Q._Ctx(cfg)
    res = Q._do_open(ctx, _state(cfg, cid))
    assert res.error is None
    Q._apply(ctx, _state(cfg, cid), res)
    after = _state(cfg, cid)
    assert (cdir / CL.SPEC_AT_OPEN).read_text(encoding="utf-8") == snap_text
    assert after["spec"]["sha256_at_open"] == digest
    assert after["state"] == "IMPLEMENTING"


def test_adopt_accepted_without_spec_id_stores_open_snapshot(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "idea no id"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED, spec_id=None)
    st["spec"].pop("id", None)
    st["spec_id"] = "SPEC-0999"
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "IMPLEMENTING" and row["reason"] == "resumed_worktree"
    snap = S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN
    assert snap.is_file()
    wt_spec = Path(after["repo"]["worktree"]) / after["spec"]["rel_path"]
    assert after["spec"]["sha256_at_open"] == SR.spec_sha256(wt_spec)


def test_replace_spec_snapshot_validation_failure_keeps_trusted_hash(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "val fail"))
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    snap = cdir / CL.SPEC_AT_OPEN
    baseline = b"trusted baseline bytes"
    snap.write_bytes(baseline)
    spec = {"sha256_at_open": hashlib.sha256(baseline).hexdigest()}
    err = Q._replace_spec_at_open_snapshot(cdir, snap, b"\xff\xfe", spec)
    assert err and "not storable" in err
    assert spec["sha256_at_open"] == hashlib.sha256(baseline).hexdigest()
    assert snap.read_bytes() == baseline


def test_replace_spec_snapshot_io_failure_keeps_trusted_hash(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "io fail"))
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    snap = cdir / CL.SPEC_AT_OPEN
    baseline = ACCEPTED.format(sid="SPEC-0999").encode()
    snap.write_bytes(baseline)
    digest = hashlib.sha256(baseline).hexdigest()
    spec = {"sha256_at_open": digest}
    real_replace = os.replace

    def boom(src, dst):
        if Path(dst).name == CL.SPEC_AT_OPEN:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    wt = ACCEPTED.format(sid="SPEC-0999") + "\nworktree drift\n"
    err = Q._replace_spec_at_open_snapshot(cdir, snap, wt.encode(), spec)
    assert err and "could not write" in err
    assert spec["sha256_at_open"] == digest
    assert snap.read_bytes() == baseline


def test_fresh_open_stores_crlf_worktree_spec_snapshot(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "crlf snap"))
    st = _state(cfg, cid)
    rel = "docs/specs/0999-x.md"
    st["spec"] = {"rel_path": rel, "id": "SPEC-0999", "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["state"] = "OPENING"
    S.write_state(cfg, cid, st)
    crlf = ACCEPTED.format(sid="SPEC-0999").replace("\n", "\r\n")
    real_add = git.worktree_add

    def add_and_seed(root, path, branch, base):
        real_add(root, path, branch, base)
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(crlf.encode("utf-8"))

    monkeypatch.setattr(git, "worktree_add", add_and_seed)
    monkeypatch.setattr(Q, "_run_worktree_setup", lambda *a, **k: None)
    ctx = Q._Ctx(cfg)
    st_open = _state(cfg, cid)
    res = Q._do_open(ctx, st_open)
    assert res.error is None
    Q._apply(ctx, st_open, res)
    after = _state(cfg, cid)
    snap = S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN
    assert snap.is_file()
    assert after["spec"]["sha256_at_open"] == hashlib.sha256(crlf.encode()).hexdigest()


def test_fresh_open_still_calls_worktree_add(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "fresh"))
    st = _state(cfg, cid)
    st["state"] = "OPENING"
    calls = []
    monkeypatch.setattr(git, "worktree_add", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(Q, "_run_worktree_setup", lambda *a, **k: None)
    ctx = Q._Ctx(cfg)
    res = Q._do_open(ctx, st)
    assert calls == [1]
    assert res.resumed_to is None


def test_resume_no_spec_idea_to_spec_writing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "no spec yet"))
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"


# ---- AC7: terminal worktree spec ----------------------------------------------------------

def test_resume_done_spec_parks_and_mails_once(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "done spec"))
    st = _prep_worktree(cfg, repo, cid, DONE)
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "done" in after["park"]["detail"]
    wt = Path(st["repo"]["worktree"])
    assert wt.is_dir()
    assert "worktree:done" in (after.get("conflict_mail") or [])
    reqs = [m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]
    assert len(reqs) == 1 and reqs[0]["needs_reply"]
    assert cid in reqs[0]["text"] and "done" in reqs[0]["text"]
    assert str(wt) in reqs[0]["text"]
    st2 = _state(cfg, cid)
    st2.pop("park", None)
    S.write_state(cfg, cid, st2)
    row2, after2 = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after2["state"] == "PARKED"
    assert len([m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]) == 1


def test_resume_proposed_with_pending_to_spec_writing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "prop brief"))
    st = _prep_worktree(cfg, repo, cid, PROPOSED, extra_state={"pending_brief": "revise this"})
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"
    assert Q.plan_step(after, Q._Ctx(cfg).profile(after)).kind == "write_spec"


def test_resume_proposed_no_spec_id_to_spec_writing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "prop new"))
    st = _prep_worktree(cfg, repo, cid, PROPOSED, spec_id=None, extra_state={"spec_id": None})
    st["spec"]["id"] = None
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"


def test_resume_missing_spec_revise_pending_to_spec_writing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "revise"))
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    st["spec"] = {"id": "SPEC-0999", "rel_path": "docs/specs/missing.md", "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    st["admission"] = "revise_pending"
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"


def test_resume_unreadable_yaml_parks_and_mails(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "bad yaml"))
    rel = "docs/specs/bad-yaml.md"
    st = _prep_worktree(cfg, repo, cid, "placeholder\n", rel=rel)
    (Path(st["repo"]["worktree"]) / rel).write_text("---\n: [invalid\n...\n\nbody\n", encoding="utf-8")
    st = _state(cfg, cid)
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "unreadable" in after["park"]["detail"]
    wt = Path(st["repo"]["worktree"])
    assert wt.is_dir()
    assert "worktree:unreadable" in (after.get("conflict_mail") or [])
    reqs = [m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]
    assert len(reqs) == 1 and "unreadable" in reqs[0]["text"] and str(wt) in reqs[0]["text"]
    st2 = _state(cfg, cid)
    st2.pop("park", None)
    S.write_state(cfg, cid, st2)
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert len([m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]) == 1


def test_resume_non_mapping_frontmatter_parks_and_mails(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "list fm"))
    rel = "docs/specs/list-fm.md"
    st = _prep_worktree(cfg, repo, cid, "---\n- item\n---\n\nbody\n", spec_id="SPEC-0999", rel=rel)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "PARKED" and "unreadable" in after["park"]["detail"]
    assert "worktree:unreadable" in (after.get("conflict_mail") or [])


def test_resume_null_base_sha_runs_setup_then_records_sha(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "null sha"))
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    rel = "docs/specs/0999-x.md"
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt / rel).write_text(ACCEPTED.format(sid="SPEC-0999"), encoding="utf-8")
    st["spec"] = {"id": "SPEC-0999", "rel_path": rel, "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["repo"].pop("base_sha_at_open", None)
    S.write_state(cfg, cid, st)
    setup_calls = []
    monkeypatch.setattr(git, "worktree_add",
                        lambda *a, **k: pytest.fail("worktree_add should not run"))
    monkeypatch.setattr(Q, "_run_worktree_setup", lambda *a, **k: setup_calls.append(1))
    st = _state(cfg, cid)
    Q._transition(cfg, st, "OPENING", "test")
    st = _state(cfg, cid)
    ctx = Q._Ctx(cfg)
    res = Q._do_open(ctx, st)
    row = Q._apply(ctx, st, res)
    after = _state(cfg, cid)
    assert setup_calls == [1]
    assert after["repo"]["base_sha_at_open"]
    assert after["state"] == "IMPLEMENTING" and row["reason"] == "resumed_worktree"


def test_adopt_setup_failure_does_not_persist_base_sha(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "setup fail"))
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    rel = "docs/specs/0999-x.md"
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt / rel).write_text(ACCEPTED.format(sid="SPEC-0999"), encoding="utf-8")
    st["spec"] = {"id": "SPEC-0999", "rel_path": rel, "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["repo"].pop("base_sha_at_open", None)
    S.write_state(cfg, cid, st)
    calls = {"setup": 0}
    def fail_setup(*a, **k):
        calls["setup"] += 1
        if calls["setup"] == 1:
            raise git.GitError(["uv sync"], 1, "uv sync exited 1")
    monkeypatch.setattr(git, "worktree_add",
                        lambda *a, **k: pytest.fail("worktree_add should not run"))
    monkeypatch.setattr(Q, "_run_worktree_setup", fail_setup)
    st = _state(cfg, cid)
    Q._transition(cfg, st, "OPENING", "test")
    st = _state(cfg, cid)
    ctx = Q._Ctx(cfg)
    res = Q._do_open(ctx, st)
    parked = Q._apply(ctx, st, res)
    after = _state(cfg, cid)
    assert after["state"] == "PARKED"
    assert not after.get("repo", {}).get("base_sha_at_open")
    def ok_setup(*a, **k):
        calls["setup"] += 1
    monkeypatch.setattr(Q, "_run_worktree_setup", ok_setup)
    st2 = _state(cfg, cid)
    st2.pop("park", None)
    S.write_state(cfg, cid, st2)
    monkeypatch.setattr(git, "worktree_add",
                        lambda *a, **k: pytest.fail("worktree_add should not run"))
    st3 = _state(cfg, cid)
    Q._transition(cfg, st3, "OPENING", "retry")
    st3 = _state(cfg, cid)
    ctx = Q._Ctx(cfg)
    res2 = Q._do_open(ctx, st3)
    row = Q._apply(ctx, st3, res2)
    after2 = _state(cfg, cid)
    assert after2["state"] == "IMPLEMENTING" and row["reason"] == "resumed_worktree"
    assert calls["setup"] == 2


def test_mismatched_worktree_path_still_calls_worktree_add(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "wrong path"))
    st = _state(cfg, cid)
    root = Path(repo)
    wrong = st["repo"]["worktree"] + "-other"
    git.worktree_add(root, Path(wrong), st["repo"]["branch"], git.rev_parse(root, "main"))
    calls = []
    monkeypatch.setattr(git, "worktree_add", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(Q, "_run_worktree_setup", lambda *a, **k: None)
    st["state"] = "OPENING"
    S.write_state(cfg, cid, st)
    Q._do_open(Q._Ctx(cfg), _state(cfg, cid))
    assert calls == [1]


def test_resolve_dependency_conflicts_send_failure_retries(env, monkeypatch):
    cfg, _repo = env
    idea, cid, st = _no_campaign_waiter(cfg, monkeypatch)
    n = {"calls": 0}
    real_send = M.send
    def flaky_send(*a, **k):
        n["calls"] += 1
        if n["calls"] == 1:
            raise OSError("mailbox down")
        return real_send(*a, **k)
    monkeypatch.setattr(M, "send", flaky_send)
    Q.resolve_dependency_conflicts(cfg, [st])
    assert idea not in (st.get("conflict_mail") or [])
    raw = [f"waiting on {idea} (after: no campaign queued for {idea})"]
    assert not QT.dependency_wait(raw, QT.Ctx(cfg, "conductor").live_specs, st["project"])
    assert Q.backlog_readiness(cfg, st) == (False, raw)
    Q.resolve_dependency_conflicts(cfg, [st])
    assert st["conflict_mail"] == [st["after"][0]["ref"]]
    assert len([m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]) == 1


def test_worktree_conflict_mail_send_failure_retries(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "mail retry"))
    st = _prep_worktree(cfg, repo, cid, DONE)
    n = {"calls": 0}
    real_send = M.send
    def flaky_send(*a, **k):
        n["calls"] += 1
        if n["calls"] == 1:
            raise OSError("mailbox down")
        return real_send(*a, **k)
    monkeypatch.setattr(M, "send", flaky_send)
    st = _state(cfg, cid)
    Q._transition(cfg, st, "OPENING", "t")
    st = _state(cfg, cid)
    ctx = Q._Ctx(cfg)
    res = Q._do_open(ctx, st)
    Q._apply(ctx, st, res)
    after = _state(cfg, cid)
    assert after["state"] == "PARKED"
    assert "worktree:done" not in (after.get("conflict_mail") or [])
    st2 = _state(cfg, cid)
    st2.pop("park", None)
    S.write_state(cfg, cid, st2)
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert "worktree:done" in (_state(cfg, cid).get("conflict_mail") or [])


def test_resume_proposed_top_level_spec_id_only_to_spec_writing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "prop id on state"))
    rel = "docs/specs/0999-x.md"
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt / rel).write_text(PROPOSED.format(sid="SPEC-0999"), encoding="utf-8")
    st["spec"] = {"rel_path": rel, "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    st["pending_brief"] = ""
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"
    assert Q.plan_step(after, Q._Ctx(cfg).profile(after)).kind == "write_spec"


def test_resume_draft_top_level_spec_id_only_to_spec_writing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "draft id on state"))
    rel = "docs/specs/0999-x.md"
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    (wt / rel).write_text(DRAFT.format(sid="SPEC-0999"), encoding="utf-8")
    st["spec"] = {"rel_path": rel, "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    st["pending_brief"] = ""
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"
    assert Q.plan_step(after, Q._Ctx(cfg).profile(after)).kind == "write_spec"


def test_adopt_records_head_and_spec_snapshot(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "adopt snap"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    st["spec"].pop("sha256_at_open", None)
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert row["reason"] == "resumed_worktree"
    wt_head = git.rev_parse(Path(after["repo"]["worktree"]))
    assert after["repo"]["head"] == wt_head
    snap = S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN
    assert snap.is_file()
    assert after["spec"]["sha256_at_open"] == SR.spec_sha256(
        Path(after["repo"]["worktree"]) / after["spec"]["rel_path"])
    wt_spec = Path(after["repo"]["worktree"]) / after["spec"]["rel_path"]
    assert snap.read_bytes() == wt_spec.read_bytes()
    assert snap.read_text(encoding="utf-8") == wt_spec.read_text(encoding="utf-8")
    notes = S.read_ledger(cfg, cid, types=["note"])
    assert any(n.get("author") == "queue" and "resumed into existing worktree" in n["text"]
               for n in notes)


def test_adopt_park_draft_with_id_leaves_snapshot_unset(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "park snap"))
    st = _prep_worktree(cfg, repo, cid, DRAFT)
    _open_resume(cfg, monkeypatch, st)
    after = _state(cfg, cid)
    assert after["state"] == "PARKED"
    assert not after.get("spec", {}).get("sha256_at_open")
    assert not (S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN).is_file()


def test_adopt_spec_snapshot_write_failure_leaves_hash_unset(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "snap fail"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    real_replace = os.replace
    fail_once = []

    def boom(src, dst):
        if Path(dst).name == CL.SPEC_AT_OPEN and not fail_once:
            fail_once.append(1)
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "resumed_worktree" not in (row.get("reason") or "")
    assert not after.get("spec", {}).get("sha256_at_open")
    assert not (S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN).is_file()
    Q._transition(cfg, after, "QUEUED", "retry")
    Q._transition(cfg, _state(cfg, cid), "OPENING", "retry")
    row2, after2 = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after2["state"] == "IMPLEMENTING"
    assert after2["spec"].get("sha256_at_open")
    assert (S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN).is_file()


def test_resume_whitespace_status_parks_without_crash(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "ws status"))
    bad = "---\nid: SPEC-0999\nstatus: ' '\n---\n\nbody\n"
    st = _prep_worktree(cfg, repo, cid, bad)
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "worktree:empty" in (after.get("conflict_mail") or [])


def test_resolve_on_same_queued_dict_then_record_backlog(env, monkeypatch):
    cfg, _repo = env
    idea, cid, st = _no_campaign_waiter(cfg, monkeypatch)
    queued = [st]
    Q.resolve_dependency_conflicts(cfg, queued)
    ready, blocked = Q.backlog_readiness(cfg, st)
    Q._record_backlog(cfg, st, ready, blocked)
    after = _state(cfg, cid)
    assert idea in (after.get("conflict_mail") or [])
    assert any("mailed conductor:" in b for b in after["backlog"]["blocked_by"])


def _open_kind_at_existing_worktree(cfg, repo, monkeypatch, *, kind, extra=None,
                                   revert_result="revert failed for test"):
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, kind))
    st = _state(cfg, cid)
    st["kind"] = kind
    if kind == "split":
        st["spec_id"] = "SPEC-0001"
        st["spec"] = {"id": "SPEC-0001", "rel_path": "docs/specs/0001-trivial.md",
                      "internal": True}
    if extra:
        st.update(extra)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    S.write_state(cfg, cid, st)
    add_calls, revert_calls = [], []
    monkeypatch.setattr(git, "worktree_add", lambda *a, **k: add_calls.append(1))
    monkeypatch.setattr(Q, "_run_worktree_setup", lambda *a, **k: None)
    monkeypatch.setattr(CL, "revert_campaign_range",
                        lambda *a, **k: revert_calls.append(1) or revert_result)
    Q._transition(cfg, _state(cfg, cid), "OPENING", "t")
    st = _state(cfg, cid)
    res = Q._do_open(Q._Ctx(cfg), st)
    row = Q._apply(Q._Ctx(cfg), st, res)
    return add_calls, revert_calls, row


@pytest.mark.parametrize("kind", ["revert", "split", "fixforward"])
def test_non_adopt_kinds_still_call_worktree_add_at_existing_path(env, monkeypatch, kind):
    cfg, repo = env
    head = git.rev_parse(repo, "main")
    extra = {"revert": {"target_sha": head, "merged_sha": head}} if kind == "revert" else {}
    if kind == "fixforward":
        extra = {"fixes_red": True, "red_episode": 1}
    revert_result = None if kind == "revert" else "revert failed for test"
    add_calls, revert_calls, row = _open_kind_at_existing_worktree(
        cfg, repo, monkeypatch, kind=kind, extra=extra, revert_result=revert_result)
    assert add_calls == [1]
    assert row["reason"] != "resumed_worktree"
    if kind == "revert":
        assert revert_calls == [1]
        assert row["to"] == "CLOSING" and row["reason"] == "opened"
    if kind == "split":
        assert row["to"] == "SPEC_WRITING" and row["reason"] == "opened"


def test_revert_open_conflict_parks_revert_conflict(env, monkeypatch):
    cfg, repo = env
    head = git.rev_parse(repo, "main")
    add_calls, revert_calls, row = _open_kind_at_existing_worktree(
        cfg, repo, monkeypatch, kind="revert",
        extra={"revert": {"target_sha": head, "merged_sha": head}})
    assert add_calls == [1] and revert_calls == [1]
    assert row["to"] == "PARKED" and row["reason"] == "revert_conflict"
    st = _state(cfg, row["campaign"])
    assert st["state"] == "PARKED" and st["park"]["reason"] == "revert_conflict"


def test_mixed_blocked_by_wakes_when_not_all_benign(env, monkeypatch):
    cfg, _repo = env
    idea, cid, st = _no_campaign_waiter(cfg, monkeypatch)
    Q.resolve_dependency_conflicts(cfg, [st])
    mailed = Q.backlog_readiness(cfg, st)[1]
    mixed = mailed + ["after IDEA-404: idea not found"]
    ctx = QT.Ctx(cfg, "conductor")
    assert QT.dependency_wait(mixed, ctx.live_specs, st["project"]) is False
    handed = [f"waiting on {cid} (after: campaign {cid} is HANDED_OFF)"]
    assert QT.dependency_wait(handed + ["after IDEA-404: idea not found"],
                              ctx.live_specs, st["project"]) is False


@pytest.mark.parametrize("prereq_id", [
    "MERGE-20261107-a1b2",
    "3M-0001-20261107-c3d4",
    "FIX-acme.io-20261107-e5f6",
])
def test_dependency_wait_holds_handed_off_for_minted_campaign_ids(env, monkeypatch, prereq_id):
    cfg, _repo = env
    waiter = capture(cfg, "after minted id")
    wcid = add_idea_campaign(cfg, monkeypatch, waiter)
    st = _state(cfg, wcid)
    st["after"] = [{"ref": prereq_id, "kind": "campaign"}]
    S.write_state(cfg, wcid, st)
    root = S.campaign_dir(cfg, prereq_id)
    root.mkdir(parents=True)
    S.write_state(cfg, prereq_id, {
        "id": prereq_id, "state": "HANDED_OFF", "project": st["project"], "kind": "merge",
        "created_at": "2026-11-07T00:00:00",
    })
    handed = f"waiting on {prereq_id} (after: campaign {prereq_id} is HANDED_OFF)"
    blocked = Q.backlog_readiness(cfg, _state(cfg, wcid))[1]
    assert blocked == [handed]
    ctx = QT.Ctx(cfg, "conductor")
    live = {x[1] for x in ctx.live_specs if x[0] == st["project"]}
    assert prereq_id not in live
    assert QT.dependency_wait(blocked, ctx.live_specs, st["project"])


@pytest.mark.parametrize("prereq_id", [
    "MERGE-20261107-a1b2",
    "3M-0001-20261107-c3d4",
    "FIX-acme.io-20261107-e5f6",
])
def test_dependency_wait_holds_live_campaign_wait_for_minted_ids(env, monkeypatch, prereq_id):
    cfg, _repo = env
    waiter = capture(cfg, "after live minted")
    wcid = add_idea_campaign(cfg, monkeypatch, waiter)
    st = _state(cfg, wcid)
    st["after"] = [{"ref": prereq_id, "kind": "campaign"}]
    S.write_state(cfg, wcid, st)
    root = S.campaign_dir(cfg, prereq_id)
    root.mkdir(parents=True)
    S.write_state(cfg, prereq_id, {
        "id": prereq_id, "state": "REVIEWING", "project": st["project"], "kind": "merge",
        "created_at": "2026-11-07T00:00:00",
    })
    wait = f"waiting on {prereq_id} (after: campaign {prereq_id} is REVIEWING)"
    blocked = Q.backlog_readiness(cfg, _state(cfg, wcid))[1]
    assert blocked == [wait]
    ctx = QT.Ctx(cfg, "conductor")
    assert QT.dependency_wait(blocked, ctx.live_specs, st["project"])


def test_mailed_wait_replaced_when_prerequisite_campaign_appears(env, monkeypatch):
    cfg, _repo = env
    idea, cid, st = _no_campaign_waiter(cfg, monkeypatch)
    Q.resolve_dependency_conflicts(cfg, [st])
    S.write_state(cfg, cid, st)
    assert Q.backlog_readiness(cfg, st)[1] == [f"mailed conductor: {idea} has no campaign ({cid})"]
    prereq_cid = add_idea_campaign(cfg, monkeypatch, idea)
    wait = f"waiting on {idea} (after: campaign {prereq_cid} is QUEUED)"
    blocked = Q.backlog_readiness(cfg, _state(cfg, cid))[1]
    assert blocked == [wait]
    assert idea in (_state(cfg, cid).get("conflict_mail") or [])
    assert QT.dependency_wait(blocked, QT.Ctx(cfg, "conductor").live_specs, st["project"])


def test_tick_conflict_mail_on_config_recovered_waiter(env, monkeypatch):
    cfg, repo = env
    idea_w, cid_w, _st_w = _no_campaign_waiter(cfg, monkeypatch)
    idea_o, cid_o, st_o = _no_campaign_waiter(cfg, monkeypatch)
    S.write_state(cfg, cid_o, dict(st_o, state="PARKED", park={
        "reason": "config_invalid", "detail": "bogus", "resume_to": "QUEUED"}))
    Q.tick(cfg)
    st_w = _state(cfg, cid_w)
    st_o = _state(cfg, cid_o)
    assert st_o["state"] == "QUEUED"
    assert idea_w in (st_w.get("conflict_mail") or [])
    assert idea_o in (st_o.get("conflict_mail") or [])
    assert any("mailed conductor:" in b for b in (st_w.get("backlog") or {}).get("blocked_by") or [])
    assert any("mailed conductor:" in b for b in (st_o.get("backlog") or {}).get("blocked_by") or [])


def test_adopt_rejects_spec_path_outside_worktree(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "escape"))
    outside = Path(repo) / "docs/specs/outsider.md"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text(ACCEPTED.format(sid="SPEC-0001"), encoding="utf-8")
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    st["spec"] = {"id": "SPEC-0001", "rel_path": "../docs/specs/outsider.md", "internal": True}
    st["spec_id"] = "SPEC-0001"
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "escapes worktree" in after["park"]["detail"]
    assert row.get("reason") != "resumed_worktree"


def test_adopt_snapshot_hash_unchanged_when_copy_matches(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "snap keep"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    snap_text = "old snapshot"
    digest = hashlib.sha256(snap_text.encode("utf-8")).hexdigest()
    st["spec"]["sha256_at_open"] = digest
    (cdir / CL.SPEC_AT_OPEN).write_text(snap_text, encoding="utf-8")
    S.write_state(cfg, cid, st)
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    after = _state(cfg, cid)
    assert after["spec"]["sha256_at_open"] == digest
    assert (cdir / CL.SPEC_AT_OPEN).read_text(encoding="utf-8") == snap_text


def test_adopt_rewrites_untrusted_snapshot_when_hash_null(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "snap null hash"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    st["spec"].pop("sha256_at_open", None)
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / CL.SPEC_AT_OPEN).write_text("drift baseline", encoding="utf-8")
    S.write_state(cfg, cid, st)
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    after = _state(cfg, cid)
    wt_spec = Path(after["repo"]["worktree"]) / after["spec"]["rel_path"]
    assert after["spec"]["sha256_at_open"] == SR.spec_sha256(wt_spec)
    assert (cdir / CL.SPEC_AT_OPEN).read_text(encoding="utf-8") == wt_spec.read_text(encoding="utf-8")


def test_adopt_rewrites_snapshot_when_hash_mismatches_copy(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "snap mismatch"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    st["spec"]["sha256_at_open"] = "deadbeef"
    (cdir / CL.SPEC_AT_OPEN).write_text("stale bytes", encoding="utf-8")
    S.write_state(cfg, cid, st)
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    after = _state(cfg, cid)
    wt_spec = Path(after["repo"]["worktree"]) / after["spec"]["rel_path"]
    assert after["spec"]["sha256_at_open"] == SR.spec_sha256(wt_spec)
    assert (cdir / CL.SPEC_AT_OPEN).read_text(encoding="utf-8") == wt_spec.read_text(encoding="utf-8")


def test_adopt_rewrites_empty_snapshot_when_hash_null(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "snap empty"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    st["spec"].pop("sha256_at_open", None)
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / CL.SPEC_AT_OPEN).write_text("", encoding="utf-8")
    S.write_state(cfg, cid, st)
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    after = _state(cfg, cid)
    wt_spec = Path(after["repo"]["worktree"]) / after["spec"]["rel_path"]
    assert after["spec"]["sha256_at_open"] == hashlib.sha256(wt_spec.read_bytes()).hexdigest()
    snap = cdir / CL.SPEC_AT_OPEN
    assert snap.read_bytes() == wt_spec.read_bytes()


def test_adopt_rewrites_snapshot_when_hash_set_but_copy_missing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "snap restore"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    st["spec"]["sha256_at_open"] = "stale"
    S.write_state(cfg, cid, st)
    snap = S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN
    if snap.is_file():
        snap.unlink()
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    after = _state(cfg, cid)
    assert snap.is_file()
    assert after["spec"]["sha256_at_open"] != "stale"
    assert after["spec"]["sha256_at_open"] == SR.spec_sha256(
        Path(after["repo"]["worktree"]) / after["spec"]["rel_path"])


@pytest.mark.parametrize("text", [
    ACCEPTED,
    ACCEPTED.replace("status: accepted", "status: in-progress"),
])
def test_resume_revise_pending_accepted_or_in_progress_goes_spec_writing(
        env, monkeypatch, text):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "revise accepted"))
    st = _prep_worktree(cfg, repo, cid, text, extra_state={
        "admission": "revise_pending", "pending_brief": "address review"})
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"
    assert Q.plan_step(after, Q._Ctx(cfg).profile(after)).kind == "write_spec"


def test_resume_after_revision_accepted_goes_implementing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "after revision"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED, extra_state={
        "admission": "after_revision", "pending_brief": "implement these findings"})
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "IMPLEMENTING" and row["reason"] == "resumed_worktree"
    step = Q.plan_step(after, Q._Ctx(cfg).profile(after))
    assert step.kind != "write_spec"


def test_resume_draft_with_pending_brief_to_spec_writing(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "draft brief"))
    st = _prep_worktree(cfg, repo, cid, DRAFT, extra_state={"pending_brief": "write more"})
    row, after = _open_resume(cfg, monkeypatch, st)
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"
    assert Q.plan_step(after, Q._Ctx(cfg).profile(after)).kind == "write_spec"


def test_guard_fails_when_sha256_set_but_snapshot_untrusted(env, monkeypatch):
    cfg, _repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "guard trust"))
    st = _state(cfg, cid)
    rel = "docs/specs/0999-x.md"
    wt = Path(st["repo"]["worktree"])
    wt.mkdir(parents=True, exist_ok=True)
    (wt / rel).parent.mkdir(parents=True, exist_ok=True)
    body = ACCEPTED.format(sid="SPEC-0999")
    (wt / rel).write_text(body, encoding="utf-8")
    st["spec"] = {"rel_path": rel, "id": "SPEC-0999", "sha256_at_open": "wrong"}
    st["repo"]["worktree"] = str(wt)
    S.write_state(cfg, cid, st)
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / CL.SPEC_AT_OPEN).write_text("stale drift baseline", encoding="utf-8")
    ok, gate, events = CL.spec_guard(cfg, _state(cfg, cid))
    assert not ok and gate is not None and gate["name"] == "spec-changed"
    assert events[0][1]["severity"] == "must_fix"


def test_worktree_conflict_mail_tolerates_null_conflict_mail(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "null mail"))
    st = _prep_worktree(cfg, repo, cid, DONE)
    st = _state(cfg, cid)
    st["conflict_mail"] = None
    S.write_state(cfg, cid, st)
    wt = Path(st["repo"]["worktree"])
    Q._worktree_conflict_mail(cfg, st, wt, "done")
    assert st["conflict_mail"] == ["worktree:done"]


def test_resume_oversized_worktree_spec_parks(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "huge spec"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    wt = Path(st["repo"]["worktree"])
    rel = st["spec"]["rel_path"]
    huge = ACCEPTED.format(sid=st["spec_id"]) + ("x" * (Q.MAX_WORKTREE_SPEC_BYTES + 1))
    (wt / rel).write_bytes(huge.encode("utf-8"))
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "exceeds" in after["park"]["detail"]
    assert row.get("reason") != "resumed_worktree"


def test_dependency_wait_wakes_on_non_live_non_handed_off_campaign(env, monkeypatch):
    cfg, _repo = env
    prereq_id = "MERGE-20261107-ffff"
    blocked = [f"waiting on {prereq_id} (after: campaign {prereq_id} is QUEUED)"]
    ctx = QT.Ctx(cfg, "conductor")
    assert QT.dependency_wait(blocked, ctx.live_specs, "Proj") is False


def test_resume_missing_spec_no_spec_id_clears_untrusted_snapshot(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "no id snap"))
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    rel = "docs/specs/missing.md"
    st["spec"] = {"rel_path": rel, "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["spec"].pop("id", None)
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    S.write_state(cfg, cid, st)
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / CL.SPEC_AT_OPEN).write_text("leftover", encoding="utf-8")
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"
    snap = cdir / CL.SPEC_AT_OPEN
    assert not snap.exists()
    assert not after.get("spec", {}).get("sha256_at_open")


@pytest.mark.parametrize("snapshot", ["mismatch", "null_hash", "symlink"])
def test_resume_revise_pending_missing_clears_untrusted_snapshot(
        env, monkeypatch, snapshot):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, f"rev snap {snapshot}"))
    st = _state(cfg, cid)
    root = Path(repo)
    wt = Path(st["repo"]["worktree"])
    wt.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(root, wt, st["repo"]["branch"], git.rev_parse(root, "main"))
    rel = "docs/specs/missing.md"
    st["spec"] = {"id": "SPEC-0999", "rel_path": rel, "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["repo"]["base_sha_at_open"] = git.rev_parse(root, "main")
    st["admission"] = "revise_pending"
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    snap = cdir / CL.SPEC_AT_OPEN
    if snapshot == "mismatch":
        st["spec"]["sha256_at_open"] = "deadbeef"
        snap.write_text("stale", encoding="utf-8")
    elif snapshot == "null_hash":
        st["spec"].pop("sha256_at_open", None)
        snap.write_text("stale", encoding="utf-8")
    else:
        target = cdir / "real-snap.md"
        target.write_text("via symlink", encoding="utf-8")
        os.symlink(target, snap)
        st["spec"]["sha256_at_open"] = hashlib.sha256(
            b"via symlink").hexdigest()
    S.write_state(cfg, cid, st)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "SPEC_WRITING" and row["reason"] == "resumed_worktree"
    assert not snap.exists()
    assert not after.get("spec", {}).get("sha256_at_open")


def test_adopt_worktree_spec_symlink_parks(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "spec symlink"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    wt = Path(st["repo"]["worktree"])
    rel = st["spec"]["rel_path"]
    spec_path = wt / rel
    real = spec_path.with_suffix(".real.md")
    real.write_text(spec_path.read_text(encoding="utf-8"), encoding="utf-8")
    spec_path.unlink()
    os.symlink(real, spec_path)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    assert "not a regular file" in after["park"]["detail"]
    assert row.get("reason") != "resumed_worktree"


def test_adopt_snapshot_outside_campaign_dir_is_rewritten(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "snap outside"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    cdir = S.campaign_dir(cfg, cid)
    cdir.mkdir(parents=True, exist_ok=True)
    outside = cdir.parent / "outside-snap.md"
    outside.write_text("escape", encoding="utf-8")
    digest = hashlib.sha256(b"escape").hexdigest()
    st["spec"]["sha256_at_open"] = digest
    snap = cdir / CL.SPEC_AT_OPEN
    if snap.is_file() or snap.is_symlink():
        snap.unlink()
    os.symlink(outside, snap)
    S.write_state(cfg, cid, st)
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    after = _state(cfg, cid)
    wt_spec = Path(after["repo"]["worktree"]) / after["spec"]["rel_path"]
    assert not snap.is_symlink()
    assert after["spec"]["sha256_at_open"] == SR.spec_sha256(wt_spec)


def test_fresh_open_does_not_store_non_utf8_snapshot(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "bad bytes"))
    st = _state(cfg, cid)
    rel = "docs/specs/0999-x.md"
    st["spec"] = {"rel_path": rel, "id": "SPEC-0999", "internal": True}
    st["spec_id"] = "SPEC-0999"
    st["state"] = "OPENING"
    S.write_state(cfg, cid, st)
    real_add = git.worktree_add
    bad = b"\xff\xfe---\nid: SPEC-0999\nstatus: accepted\n---\n\nbody\n"

    def add_and_seed(root, path, branch, base):
        real_add(root, path, branch, base)
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(bad)

    monkeypatch.setattr(git, "worktree_add", add_and_seed)
    monkeypatch.setattr(Q, "_run_worktree_setup", lambda *a, **k: None)
    res = Q._do_open(Q._Ctx(cfg), _state(cfg, cid))
    assert res.error and "not storable" in res.error
    st_after = _state(cfg, cid)
    snap = S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN
    assert not snap.exists()
    assert not st_after.get("spec", {}).get("sha256_at_open")


def test_short_spec_snapshot_write_leaves_no_truncated_baseline(env, monkeypatch):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "short write"))
    st = _prep_worktree(cfg, repo, cid, ACCEPTED)
    real_write = os.write
    calls = []

    def short_write(fd, data):
        if calls:
            return 0
        calls.append(1)
        if len(data) > 1:
            return 1
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", short_write)
    row, after = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after["state"] == "PARKED" and after["park"]["reason"] == "open_failed"
    snap = S.campaign_dir(cfg, cid) / CL.SPEC_AT_OPEN
    assert not snap.exists()
    assert not after.get("spec", {}).get("sha256_at_open")
    assert row.get("reason") != "resumed_worktree"


@pytest.mark.parametrize("status,marker", [
    ("rejected", "worktree:rejected"),
    ("superseded", "worktree:superseded"),
])
def test_resume_rejected_or_superseded_spec_mails_once(env, monkeypatch, status, marker):
    cfg, repo = env
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, status))
    text = ACCEPTED.replace("status: accepted", f"status: {status}")
    st = _prep_worktree(cfg, repo, cid, text)
    _open_resume(cfg, monkeypatch, st)
    after = _state(cfg, cid)
    assert after["state"] == "PARKED"
    assert marker in (after.get("conflict_mail") or [])
    assert len([m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]) == 1
    after.pop("park", None)
    S.write_state(cfg, cid, after)
    _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert len([m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]) == 1
