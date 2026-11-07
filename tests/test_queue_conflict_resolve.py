"""SPEC-0458: resolve_dependency_conflicts, worktree resume, conflict mail."""
from pathlib import Path

import pytest

from nightwright.agentic import gitops as git
from nightwright.agentic import mailbox as M
from nightwright.agentic import queue as Q
from nightwright.agentic import quiet as QT
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
    assert after["state"] == "PARKED"
    assert "missing" in after["park"]["detail"]
    assert wt.is_dir()


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
    assert after["state"] == "PARKED" and "done" in after["park"]["detail"]
    wt = Path(st["repo"]["worktree"])
    assert wt.is_dir()
    assert "worktree:done" in (after.get("conflict_mail") or [])
    reqs = [m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]
    assert len(reqs) == 1 and cid in reqs[0]["text"] and "done" in reqs[0]["text"]
    st2 = _state(cfg, cid)
    st2.pop("park", None)
    S.write_state(cfg, cid, st2)
    row2, after2 = _open_resume(cfg, monkeypatch, _state(cfg, cid))
    assert after2["state"] == "PARKED"
    assert len([m for m in M.read_inbox(cfg, "conductor") if m.get("kind") == "request"]) == 1
