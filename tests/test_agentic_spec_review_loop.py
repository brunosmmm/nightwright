"""SPEC-0214: the spec review loop inside the campaign (SPEC_REVIEW, in-loop acceptance). Built on
SPEC-0215's FakeRunner helpers and ledger fixture repo (`test_agentic_spec_writer`)."""
import itertools
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from test_agentic_e2e import _git, impl_done, review as conformance_pass, script
from test_agentic_idea_campaign import add_idea_campaign, capture
from test_agentic_spec_writer import (Hooked, in_review, ledger, parked, prompt, resume,
                                      spec_written, start, wtree)
from wt.agentic import gitops
from wt.agentic import personas as PS
from wt.agentic import profiles as PR
from wt.agentic import queue as Q
from wt.agentic import schemas as SC
from wt.agentic import spec_review as SR
from wt.agentic import store as S
from wt.agentic.gitops import GitError

SPEC = "docs/specs/0002-add-a-thing.md"


def reviewed(verdict="approve", *, findings=None, splits=None, **kw):
    """FakeRunner entry: the spec reviewer."""
    v = {"verdict": verdict, "size": "s", "tone": "cost-effective", "rationale": f"{verdict} it",
         "findings": findings or [], "suggested_splits": splits or []}
    return {"persona": "spec_reviewer", "verdict": v, "cost_usd": kw.pop("cost_usd", 0.3), **kw}


def finding(sev="must_fix", title="AC 1 is untestable"):
    return {"severity": sev, "section": "Acceptance criteria", "title": title,
            "detail": f"detail of {title}"}


_REVISIONS = itertools.count(1)


def writes(i, mode="revise"):
    """A revise entry; every call writes a different body, so the file always changes."""
    return spec_written(2, i, mode=mode, extra_body=f"Revision {next(_REVISIONS)}.")


def begin(tmp_path, monkeypatch, entries, **kw):
    """A campaign at its first SPEC_REVIEW (the write done); the rest of `entries` is scripted."""
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [
        spec_written(2, i), *[e(i) if callable(e) else e for e in entries]], **kw)
    Q.tick(cfg)
    in_review(cfg, cid)
    return cfg, repo, iid, cid


def tick_until(cfg, cid, states, n=30):
    for _ in range(n):
        if S.read_state(cfg, cid)["state"] in states:
            return
        Q.tick(cfg)
    raise AssertionError(S.read_state(cfg, cid)["state"])


def reviews(cfg, cid):
    return ledger(cfg, cid, "spec_review")


def subjects(wt, n=5):
    return _git(wt, "log", f"-{n}", "--format=%s").splitlines()


# ---- approve ---------------------------------------------------------------------------------------

def finish_impl(i):
    """The implementer for the written SPEC-0002: the feature plus the spec set done."""
    e = impl_done("SPEC-0002", spec_rel=SPEC)
    e["edits"] = [e["edits"][0]]
    e["shell"] = [f"sed -i 's/^status: accepted/status: done/; s/- \\[ \\]/- [x]/' {SPEC}",
                  f"{sys.executable} tools/spec_lint.py --write-ledger"]
    return e


def test_approve_accepts_in_the_loop_and_runs_to_merged(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed(), finish_impl, conformance_pass()])
    wt = wtree(cfg, cid)
    Q.tick(cfg)                                     # the review: approve, accept, IMPLEMENTING
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING"
    assert "status: accepted" in (wt / SPEC).read_text()
    head = _git(wt, "rev-parse", "HEAD")
    assert _git(wt, "log", "-1", "--format=%s") == f"wt({cid}): accept SPEC-0002 (round 1)"
    assert sorted(_git(wt, "show", "--name-only", "--format=", "HEAD").splitlines()) \
        == sorted([SPEC, "docs/LEDGER.md"])
    assert (wt / "setup-out.txt").exists() and "setup-out.txt" not in _git(wt, "ls-files")
    assert _git(wt, "status", "--porcelain", "--untracked-files=no") == ""
    assert subprocess.run([sys.executable, "tools/spec_lint.py"], cwd=wt,
                          capture_output=True, text=True).returncode == 0
    at_open = S.campaign_dir(cfg, cid) / "spec_at_open.md"
    assert at_open.read_text() == (wt / SPEC).read_text()
    assert st["spec"]["sha256_at_open"] == SR.spec_sha256(wt / SPEC)
    assert st["repo"]["head"] == head and st["pending_brief"] is None
    sha = st["spec_review"]["sha"]
    assert SR.load_review(cfg, "SPEC-0002", sha)["verdict"] == "approve"
    (ev,) = reviews(cfg, cid)
    assert ev["round"] == 1 and ev["verdict"] == "approve" and ev["spec_sha256"] == sha
    assert ev["run_id"] == "002-spec_reviewer"
    assert ev["counts"] == {"must_fix": 0, "major": 0, "minor": 0, "nit": 0}
    (acc,) = ledger(cfg, cid, "spec_accepted")
    assert acc["spec_id"] == "SPEC-0002" and acc["round"] == 1
    assert SC.ledger_problems(ev) == [] and SC.ledger_problems(acc) == []
    assert st["spec_review"] == {"round": 1, "verdict": "approve", "sha": sha}
    assert "spec_rounds" not in st["counters"]
    trans = [(e["from"], e["to"], e["reason"]) for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert ("SPEC_WRITING", "SPEC_REVIEW", "spec_written") in trans
    assert ("SPEC_REVIEW", "IMPLEMENTING", "spec_accepted") in trans
    assert st["cost"]["by_persona"]["spec_reviewer"] == 0.3
    assert st["cost"]["total_usd"] == 0.7
    tick_until(cfg, cid, {"MERGED", "PARKED"})
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st.get("park")
    assert "status: done" in (repo / SPEC).read_text()
    assert (repo / "feature.txt").exists()
    assert any(s.startswith(f"wt({cid}): accept SPEC-0002") for s in subjects(repo, 12))


# ---- revise, split ---------------------------------------------------------------------------------

def test_revise_then_accept_after_revision_without_a_second_review(tmp_path, monkeypatch):
    """SPEC-0230: one review per campaign; a revise is applied by the writer and accepted."""
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed("revise", findings=[finding(), finding("nit", "a nit")]), writes,
        reviewed("approve")])                       # a second review would consume this entry
    Q.tick(cfg)                                     # the one review: revise
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and "spec_rounds" not in st["counters"]
    brief = st["pending_brief"]
    assert brief["source"] == "spec_reviewer" and brief["fix_brief"] == "revise it"
    assert [m["title"] for m in brief["must_fix"]] == ["[must_fix] AC 1 is untestable (Acceptance criteria)"]
    review_sha = reviews(cfg, cid)[0]["spec_sha256"]
    Q.tick(cfg)                                     # the writer revises
    assert "AC 1 is untestable" in prompt(cfg, cid, 3) and "a nit" not in prompt(cfg, cid, 3)
    in_review(cfg, cid)
    Q.tick(cfg)                                     # no reviewer: accept after revision
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING"
    assert [e["round"] for e in reviews(cfg, cid)] == [1]
    assert not (S.runs_dir(cfg, cid) / "004-spec_reviewer").exists()
    assert "status: accepted" in (wtree(cfg, cid) / SPEC).read_text()
    assert reviews(cfg, cid)[0]["counts"] == {"must_fix": 1, "major": 0, "minor": 0, "nit": 1}
    assert _git(wtree(cfg, cid), "log", "-1", "--format=%s").endswith(
        "accept SPEC-0002 (round 1) (after revision)")
    stored = SR.load_review(cfg, "SPEC-0002", review_sha)
    assert st["pending_brief"] == SR.revision_brief(stored)
    assert [m["title"] for m in st["pending_brief"]["must_fix"]] == [
        "[must_fix] AC 1 is untestable (Acceptance criteria)", "[nit] a nit (Acceptance criteria)"]
    (acc,) = ledger(cfg, cid, "spec_accepted")
    assert acc["round"] == 1 and SC.ledger_problems(acc) == []


def test_split_verdict_gives_the_writer_the_narrowing_rule(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed("split", findings=[finding("major", "too big")],
                 splits=[{"title": "Part A", "scope": "the core"}, {"title": "Part B"}]),
        writes])
    Q.tick(cfg)
    Q.tick(cfg)
    p = prompt(cfg, cid, 3)
    assert "narrow the spec to the first suggested split" in p
    assert "Part A" in p and "Part B" in p and "too big" in p
    Q.tick(cfg)                                     # the narrowing is the revision: accepted
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    assert len(reviews(cfg, cid)) == 1


# ---- unchanged revision ----------------------------------------------------------------------------

def unchanged(tmp_path, monkeypatch, more=(), clear_brief=False):
    """A revise review, then back in SPEC_REVIEW with the spec text unchanged. `clear_brief`
    drops the writer's brief first, as a successful writer run would."""
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed("revise", findings=[finding(title="round one")]), *more])
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING"
    st["state"] = "SPEC_REVIEW"
    if clear_brief:
        st["pending_brief"] = None
    S.write_state(cfg, cid, st)
    Q.tick(cfg)
    return cfg, repo, iid, cid


def test_an_unchanged_revision_parks_and_runs_no_second_review(tmp_path, monkeypatch):
    cfg, repo, iid, cid = unchanged(tmp_path, monkeypatch, [reviewed("approve")])
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_WRITING"
    assert park["detail"] == "the revision did not change the spec"
    assert len(reviews(cfg, cid)) == 1
    assert "status: accepted" not in (wtree(cfg, cid) / SPEC).read_text()


def test_resume_with_a_note_revises_then_accepts(tmp_path, monkeypatch):
    cfg, repo, iid, cid = unchanged(tmp_path, monkeypatch, [writes])
    resume(cfg, cid, note="keep it to the CLI")
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"
    Q.tick(cfg)
    p = prompt(cfg, cid, 3)
    assert "keep it to the CLI" in p and "round one" in p
    in_review(cfg, cid)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    assert len(reviews(cfg, cid)) == 1


def test_a_plain_resume_of_the_unchanged_park_runs_the_writer_with_the_stored_findings(
        tmp_path, monkeypatch):
    cfg, repo, iid, cid = unchanged(tmp_path, monkeypatch, [writes], clear_brief=True)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_WRITING"
    brief = S.read_state(cfg, cid)["pending_brief"]
    assert [m["title"] for m in brief["must_fix"]] == ["[must_fix] round one (Acceptance criteria)"]
    resume(cfg, cid)                                # no note
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"
    Q.tick(cfg)
    assert "round one" in prompt(cfg, cid, 3)
    in_review(cfg, cid)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    assert len(reviews(cfg, cid)) == 1


# ---- reviewer failure ------------------------------------------------------------------------------

def test_runner_error_retries_once_then_parks_and_a_resume_costs_no_round(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed(exit_code=1), reviewed(exit_code=1), reviewed("approve")])
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_REVIEW" and st["counters"]["spec_review_retries"] == 1
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_REVIEW"
    assert park["detail"].startswith("reviewer failed: ")
    assert not reviews(cfg, cid)
    resume(cfg, cid)
    assert S.read_state(cfg, cid)["counters"]["spec_review_retries"] == 0
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    assert [e["round"] for e in reviews(cfg, cid)] == [1]


def test_malformed_verdict_is_a_failed_run(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed(malformed=True), reviewed(malformed=True)])
    Q.tick(cfg)
    Q.tick(cfg)
    assert "does not match the schema" in parked(cfg, cid)["detail"]


def test_approve_with_a_must_fix_is_a_failed_run(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed("approve", findings=[finding()]), reviewed("approve", findings=[finding()])])
    Q.tick(cfg)
    Q.tick(cfg)
    assert "must_fix" in parked(cfg, cid)["detail"] and not reviews(cfg, cid)


@pytest.mark.parametrize("how", ["edit_spec", "edit_other", "commit"])
def test_reviewer_modifying_the_worktree_is_reset_retried_and_parked(tmp_path, monkeypatch, how):
    bad = {"edit_spec": {"edits": [{"path": SPEC, "append": "\nsneaky\n"}]},
           "edit_other": {"edits": [{"path": "docs/LEDGER.md", "append": "\nsneaky\n"}]},
           "commit": {"edits": [{"path": "other.txt", "write": "x"}],
                      "shell": ["git add other.txt", "git commit -q -m sneaky"]}}[how]
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed("approve", **bad), reviewed("approve", **bad)])
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "SPEC_REVIEW"
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain", "--untracked-files=no") == ""
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["detail"] == "reviewer failed: reviewer modified the worktree"
    assert park["resume_to"] == "SPEC_REVIEW"
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain", "--untracked-files=no") == ""
    assert (wt / "setup-out.txt").exists() and not reviews(cfg, cid)
    assert "sneaky" not in (wt / SPEC).read_text()


def test_resuming_the_worktree_modifying_reviewer_park_costs_no_round(tmp_path, monkeypatch):
    bad = {"edits": [{"path": SPEC, "append": "\nsneaky\n"}]}
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed("approve", **bad), reviewed("approve", **bad), reviewed("approve")])
    Q.tick(cfg)
    Q.tick(cfg)
    assert parked(cfg, cid)["detail"] == "reviewer failed: reviewer modified the worktree"
    assert not reviews(cfg, cid)
    resume(cfg, cid)
    assert S.read_state(cfg, cid)["state"] == "SPEC_REVIEW"
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING"
    assert [e["round"] for e in reviews(cfg, cid)] == [1]
    assert "sneaky" not in (wtree(cfg, cid) / SPEC).read_text()


def test_a_timed_out_review_is_a_failed_run(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed(timed_out=True), reviewed(timed_out=True)])
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "SPEC_REVIEW"
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["detail"] == "reviewer failed: the run timed out" and park["resume_to"] == "SPEC_REVIEW"
    assert not reviews(cfg, cid)


def test_an_interrupted_review_stays_in_spec_review_and_counts_no_round(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed("approve")])
    stop = threading.Event()
    report = Q.tick(cfg, stop=stop, runner=Hooked(tmp_path, after=lambda req: stop.set()))
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_REVIEW" and report.steps[-1]["reason"] == "interrupted"
    assert not reviews(cfg, cid)
    assert "status: accepted" not in (wtree(cfg, cid) / SPEC).read_text()


@pytest.mark.parametrize("exc", [RuntimeError("bug"), OSError("disk full")])
def test_a_runner_that_raises_after_editing_is_reset_and_its_run_closed(tmp_path, monkeypatch, exc):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed("approve")])
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")

    def edit_then_raise(req):
        with open(wt / SPEC, "a") as f:
            f.write("\nsneaky\n")
        raise exc

    Q.tick(cfg, runner=Hooked(tmp_path, before=edit_then_raise))
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_REVIEW"
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain", "--untracked-files=no") == ""
    assert "sneaky" not in (wt / SPEC).read_text()
    starts = [e["run_id"] for e in ledger(cfg, cid, "run.start") if e["run_id"].endswith("spec_reviewer")]
    ends = [e["run_id"] for e in ledger(cfg, cid, "run.end") if e["run_id"].endswith("spec_reviewer")]
    assert starts and starts == ends
    resume(cfg, cid)                                # the clean worktree needs no manual recovery
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"


def test_tracked_uncommitted_edit_parks_before_any_run(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed("approve")])
    wt = wtree(cfg, cid)
    with open(wt / SPEC, "a") as f:
        f.write("\nhand edit\n")
    runs = S.read_state(cfg, cid)["counters"]["runs"]
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_REVIEW"
    assert park["detail"] == "commit or discard the worktree changes, then resume"
    assert S.read_state(cfg, cid)["counters"]["runs"] == runs
    _git(wt, "checkout", "--", SPEC)
    resume(cfg, cid)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    assert [e["round"] for e in reviews(cfg, cid)] == [1]


def test_an_untracked_file_alone_does_not_block(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed("approve")])
    (wtree(cfg, cid) / "scratch.txt").write_text("untracked")
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    assert "scratch.txt" not in _git(wtree(cfg, cid), "ls-files")


# ---- accept failure --------------------------------------------------------------------------------

def test_commit_failure_restores_the_worktree_then_a_resume_re_reviews_and_accepts(
        tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed(), reviewed()])
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    real = gitops.git

    def failing(repo_, *args, **kw):
        if args and args[0] == "commit":
            raise GitError(["git", "commit"], 1, "boom")
        return real(repo_, *args, **kw)

    monkeypatch.setattr(gitops, "git", failing)
    Q.tick(cfg)
    monkeypatch.setattr(gitops, "git", real)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_REVIEW"
    assert park["detail"].startswith("accept failed: ") and "boom" in park["detail"]
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain", "--untracked-files=no") == ""
    assert "status: accepted" not in (wt / SPEC).read_text()
    assert _git(wt, "diff", "--cached", "--name-only") == ""
    resume(cfg, cid)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    assert "status: accepted" in (wt / SPEC).read_text()
    assert len(reviews(cfg, cid)) == 2


def test_regeneration_failure_restores_the_spec_and_the_ledger_file(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed(), reviewed()])
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    ledger_before = (wt / "docs/LEDGER.md").read_text()
    real = Q.SW.regenerate

    def failing(worktree, regen, snap):
        real(worktree, regen, snap)                 # the generated file really changes first
        assert (wt / "docs/LEDGER.md").read_text() != ledger_before
        return ["regenerate exploded"], []

    monkeypatch.setattr(Q.SW, "regenerate", failing)
    Q.tick(cfg)
    monkeypatch.setattr(Q.SW, "regenerate", real)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_REVIEW"
    assert park["detail"].startswith("accept failed: ") and "regenerate exploded" in park["detail"]
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain", "--untracked-files=no") == ""
    assert _git(wt, "diff", "--cached", "--name-only") == ""
    assert "status: accepted" not in (wt / SPEC).read_text()
    assert (wt / "docs/LEDGER.md").read_text() == ledger_before
    resume(cfg, cid)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"


def test_failure_after_the_accept_commit_resumes_without_a_review(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    wt = wtree(cfg, cid)
    real = Q.shutil.copyfile
    monkeypatch.setattr(Q.shutil, "copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    Q.tick(cfg)
    monkeypatch.setattr(Q.shutil, "copyfile", real)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_REVIEW"
    assert park["detail"] == "accept failed after commit: disk"
    assert "accept SPEC-0002" in _git(wt, "log", "-1", "--format=%s")
    assert not ledger(cfg, cid, "spec_accepted")
    runs = S.read_state(cfg, cid)["counters"]["runs"]
    resume(cfg, cid)
    Q.tick(cfg)                                     # the script holds no more entries
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and st["counters"]["runs"] == runs
    assert (S.campaign_dir(cfg, cid) / "spec_at_open.md").read_text() == (wt / SPEC).read_text()
    assert st["spec"]["sha256_at_open"] == SR.spec_sha256(wt / SPEC)
    (acc,) = ledger(cfg, cid, "spec_accepted")
    assert acc["round"] == 1
    assert len(reviews(cfg, cid)) == 1


def revised(tmp_path, monkeypatch):
    """A revise review and the writer's revision done: back in SPEC_REVIEW, one accept away."""
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [
        reviewed("revise", findings=[finding(title="round one")]), writes])
    Q.tick(cfg)
    Q.tick(cfg)
    in_review(cfg, cid)
    return cfg, repo, iid, cid


def test_after_revision_commit_failure_resumes_to_the_accept_not_a_second_review(
        tmp_path, monkeypatch):
    cfg, repo, iid, cid = revised(tmp_path, monkeypatch)
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    real = gitops.git

    def failing(repo_, *args, **kw):
        if args and args[0] == "commit":
            raise GitError(["git", "commit"], 1, "boom")
        return real(repo_, *args, **kw)

    monkeypatch.setattr(gitops, "git", failing)
    Q.tick(cfg)
    monkeypatch.setattr(gitops, "git", real)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_REVIEW"
    assert park["detail"].startswith("accept failed: ")
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain", "--untracked-files=no") == ""
    runs = S.read_state(cfg, cid)["counters"]["runs"]
    resume(cfg, cid)
    Q.tick(cfg)                                     # the script holds no more entries
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and st["counters"]["runs"] == runs
    assert len(reviews(cfg, cid)) == 1
    assert _git(wt, "log", "-1", "--format=%s").endswith("(round 1) (after revision)")
    assert "round one" in str(st["pending_brief"])


def test_after_revision_failure_past_the_commit_finishes_with_the_revision_brief(
        tmp_path, monkeypatch):
    cfg, repo, iid, cid = revised(tmp_path, monkeypatch)
    wt = wtree(cfg, cid)
    real = Q.shutil.copyfile
    monkeypatch.setattr(Q.shutil, "copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    Q.tick(cfg)
    monkeypatch.setattr(Q.shutil, "copyfile", real)
    park = parked(cfg, cid)
    assert park["detail"] == "accept failed after commit: disk" and park["resume_to"] == "SPEC_REVIEW"
    assert "accept SPEC-0002" in _git(wt, "log", "-1", "--format=%s")
    runs = S.read_state(cfg, cid)["counters"]["runs"]
    resume(cfg, cid)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and st["counters"]["runs"] == runs
    assert len(reviews(cfg, cid)) == 1
    stored = SR.load_review(cfg, "SPEC-0002", st["spec_review"]["sha"])
    assert st["pending_brief"] == SR.revision_brief(stored)
    (acc,) = ledger(cfg, cid, "spec_accepted")
    assert acc["round"] == 1


def test_a_failed_accept_removes_files_a_regenerate_command_created(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    wt = wtree(cfg, cid)
    real = Q.SW.regenerate

    def failing(worktree, regen, snap):
        (wt / "stray.txt").write_text("made by a command")
        real(worktree, regen, snap)
        return ["regenerate exploded"], []

    monkeypatch.setattr(Q.SW, "regenerate", failing)
    Q.tick(cfg)
    assert parked(cfg, cid)["detail"].startswith("accept failed: ")
    assert not (wt / "stray.txt").exists()
    assert (wt / "setup-out.txt").exists()          # worktree setup output stays


def test_a_dirty_worktree_wins_over_an_accepted_head(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    wt = wtree(cfg, cid)
    real = Q.shutil.copyfile
    monkeypatch.setattr(Q.shutil, "copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    Q.tick(cfg)
    monkeypatch.setattr(Q.shutil, "copyfile", real)
    (wt / SPEC).write_text((wt / SPEC).read_text() + "\nhand edit\n")
    resume(cfg, cid)
    Q.tick(cfg)
    assert parked(cfg, cid)["detail"] == "commit or discard the worktree changes, then resume"


# ---- cost cap, slots, human change -----------------------------------------------------------------

def test_cost_cap_parks_before_the_review_and_raise_cap_runs_it(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    with S.item_lock(cfg, cid):
        st = S.read_state(cfg, cid)
        st["cost"]["cap_usd"] = 1.0                 # 0.4 spent + 0.8 reviewer budget
        S.write_state(cfg, cid, st)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "cost_cap" and park["resume_to"] == "SPEC_REVIEW"
    assert S.read_state(cfg, cid)["counters"]["runs"] == 1
    resume(cfg, cid, raise_cap=10.0)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"


def test_approved_campaign_keeps_its_slot(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()], profile={"slots": 1})
    other = add_idea_campaign(cfg, monkeypatch, capture(cfg, text="Another thing"))
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING"
    assert S.read_state(cfg, other)["state"] == "QUEUED"


def test_a_human_change_during_the_review_skips_the_accept(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])

    def human(req):
        with S.item_lock(cfg, cid):
            st = S.read_state(cfg, cid)
            Q._park(cfg, st, "human", "stop", "SPEC_REVIEW", by="human")

    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    report = Q.tick(cfg, runner=Hooked(tmp_path, after=human))
    assert S.read_state(cfg, cid)["state"] == "PARKED"
    assert _git(wt, "rev-parse", "HEAD") == head and "status: accepted" not in (wt / SPEC).read_text()
    assert not ledger(cfg, cid, "spec_accepted")
    assert report.steps[-1]["reason"] == "superseded_by_human"


# ---- generic errors, legacy park, recording --------------------------------------------------------

def boom(exc):
    def before(req):
        raise exc
    return before


def test_oserror_in_the_write_worker_parks_spec_write_failed(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    Q.tick(cfg, runner=Hooked(tmp_path, before=boom(OSError("disk full"))))
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"


@pytest.mark.parametrize("exc", [OSError("disk full"), RuntimeError("bug")])
def test_exception_in_the_review_worker_parks_spec_review_exhausted(tmp_path, monkeypatch, exc):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [])
    Q.tick(cfg, runner=Hooked(tmp_path, before=boom(exc)))
    park = parked(cfg, cid)
    assert park["reason"] == "spec_review_exhausted" and park["resume_to"] == "SPEC_REVIEW"
    assert park["detail"].startswith("spec_review step failed: ")
    assert ("disk full" if isinstance(exc, OSError) else "bug") in park["detail"]
    assert S.read_state(cfg, cid)["counters"].get("spec_rounds", 0) == 0


def test_legacy_review_unavailable_park_resumes_to_the_review_or_the_writer(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [writes, reviewed()])
    Q._park(cfg, S.read_state(cfg, cid), "spec_review_unavailable", "old", "SPEC_WRITING")
    resume(cfg, cid)
    assert S.read_state(cfg, cid)["state"] == "SPEC_REVIEW"
    assert S.read_state(cfg, cid)["counters"]["runs"] == 1
    Q._park(cfg, S.read_state(cfg, cid), "spec_review_unavailable", "old", "SPEC_WRITING")
    resume(cfg, cid, note="mention the exit code")
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"
    Q.tick(cfg)
    assert "mention the exit code" in prompt(cfg, cid, 2)
    in_review(cfg, cid)


def test_the_request_carries_the_campaign_invariant_and_is_read_only(tmp_path, monkeypatch):
    cfg, repo, iid, cid = begin(tmp_path, monkeypatch, [reviewed()])
    seen = []
    Q.tick(cfg, runner=Hooked(tmp_path, before=seen.append))
    (req,) = seen
    wt = wtree(cfg, cid)
    assert f"the spec-review step of campaign {cid} on SPEC-0002" in req.system_append
    assert "There is no campaign" not in req.system_append
    assert req.read_only and req.cwd == wt and tuple(req.add_dirs) == (wt,)
    assert "Add a thing" in req.prompt and "## Acceptance criteria" in req.prompt
    assert PS.get_persona("spec_reviewer").invariant == PS.SPEC_REVIEW_INVARIANT


def test_no_builtin_profile_has_spec_review_max():
    """SPEC-0230: the round count is fixed at one, not a profile setting."""
    for name, prof in PR.builtin_profiles().items():
        assert "spec_review_max" not in prof, name
    assert "spec_review_exhausted" in Q.PARK_REASONS
    assert "SPEC_REVIEW" in Q.ACTIVE_STATES and "SPEC_REVIEW" in Q.RESUMABLE_STATES
    assert Q._resume_target("SPEC_REVIEW") == "SPEC_REVIEW"


def test_human_park_in_spec_review_resumes_to_it(tmp_path, monkeypatch):
    state = {"state": "SPEC_REVIEW", "park_requested": {"detail": "pause"}}
    step = Q.plan_step(state, PR.builtin_profiles()["balanced"])
    assert step.kind == "park" and step.resume_to == "SPEC_REVIEW"
