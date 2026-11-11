"""SPEC-0215: the spec_writer persona and the SPEC_WRITING step (write and revise). Uses the
SPEC-0176 end-to-end harness (temp repo as project, FakeRunner) and SPEC-0209's idea helpers.
The fixture repo carries the real linter, schema and TEMPLATE/SCHEMA text, a LEDGER.md and a
`merge.regenerate` entry, so a written spec is checked by the same tooling as in nightwright itself."""
import dataclasses
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_agentic_e2e import _git, _write, make_cfg, make_repo, script
from test_agentic_idea_campaign import add_idea_campaign, capture, idea
from nightwright import org_write as OW
from nightwright import spec_numbers as SN
from nightwright.agentic import personas as PS
from nightwright.agentic import profiles as PR
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import spec_write as SW
from nightwright.agentic import store as S
from nightwright.agentic.gitops import GitError
from nightwright.agentic.runner import FakeRunner
from nightwright import specmeta

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import spec_lint as L  # noqa: E402

REGEN = f"{sys.executable} tools/spec_lint.py --write-ledger"
NUM = {False: 2, True: 101}          # the number nightwright allocates: SPEC-0001 exists / SPEC-0100 (epic)


# ---- fixtures and builders ---------------------------------------------------------------------

def make_project(tmp_path, *, regenerate=REGEN, with_epic=False):
    repo = make_repo(tmp_path)
    for rel in ("docs/specs/TEMPLATE.md", "docs/specs/SCHEMA.md", "docs/specs/spec.schema.json",
                "tools/spec_lint.py", "docs/LEDGER.md"):
        _write(repo, rel, (ROOT / rel).read_text())
    if with_epic:
        _write(repo, "docs/specs/0100-the-epic.md", _spec_md(100, None, title="The big epic",
                                                              status="accepted", kind="epic"))
        _write(repo, "docs/specs/0099-sibling.md", _spec_md(
            99, None, title="Sibling child", status="accepted", epic="SPEC-0100"))
    yaml = (repo / ".nw" / "project.yaml").read_text()
    if regenerate:
        yaml = yaml.replace("  remote: origin\n", "  remote: origin\n  regenerate:\n"
                            f"    - {{path: docs/LEDGER.md, cmd: '{regenerate}'}}\n")
    yaml = yaml.replace('setup: ["touch .setup-ran"]',
                        'setup: ["touch .setup-ran", "touch setup-out.txt"]')
    _write(repo, ".nw/project.yaml", yaml)
    subprocess.run([sys.executable, "tools/spec_lint.py", "--write-ledger"], cwd=repo, check=True,
                   capture_output=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "spec writer fixture")
    return repo


def _spec_md(number, iid, *, title="Add a thing", status="proposed", kind="feature", epic=None,
             fm=None, drop_fm=(), drop_sections=(), extra_body=""):
    head = {"id": f"SPEC-{number:04d}", "title": f'"{title}"', "status": status, "owner": "user",
            "created": "2026-10-25", "kind": kind}
    if iid:
        head["source_idea"] = iid
    if epic:
        head["parent"] = epic
    head.update(fm or {})
    for k in drop_fm:
        head.pop(k)
    body = {"Context": "Why.", "Goals / Non-goals": "- goal", "Decision": "Do it.",
            "Design": "How.", "Acceptance criteria": "- [ ] it works",
            "Test plan": "- `tests/test_x.py`", "Definition of done": "- [ ] done"}
    for k in drop_sections:
        body.pop(k)
    out = "---\n" + "".join(f"{k}: {v}\n" for k, v in head.items()) + "---\n"
    out += "".join(f"\n## {h}\n\n{t}\n" for h, t in body.items())
    return out + (f"\n{extra_body}\n" if extra_body else "")


def spec_written(number, iid, *, mode="write", slug="add-a-thing", path=None, extra_files=None,
                 deletes=(), verdict=None, no_edit=False, **spec_kw):
    """FakeRunner entry for the spec writer: writes the spec at nightwright's path (or `path`)."""
    real = f"docs/specs/{number:04d}-{slug}.md"
    path = path or real
    edits = [] if no_edit else [{"path": path, "write": _spec_md(number, iid, **spec_kw)}]
    edits += [{"path": p, "write": t} for p, t in (extra_files or {}).items()]
    edits += [{"path": p, "delete": True} for p in deletes]
    v = {"status": "written", "spec_path": real, "title": "Add a thing",
         "summary": f"{mode} the spec", "blockers": []}
    v.update(verdict or {})
    return {"persona": "spec_writer", "edits": edits, "verdict": v, "cost_usd": 0.4}


def start(tmp_path, monkeypatch, entries, *, epic=None, profile=None, **proj):
    """A project, an idea campaign opened into SPEC_WRITING. `entries` may be a callable of the
    idea id."""
    repo = make_project(tmp_path, with_epic=bool(epic), **proj)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile=profile)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg, epic=epic)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"
    script(tmp_path, entries(iid) if callable(entries) else entries)
    return cfg, repo, iid, cid


def wtree(cfg, cid) -> Path:
    return Path(S.read_state(cfg, cid)["repo"]["worktree"])


def prompt(cfg, cid, n=1) -> str:
    return (S.runs_dir(cfg, cid) / f"{n:03d}-spec_writer" / "prompt.md").read_text()


def parked(cfg, cid):
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED", st["state"]
    return st["park"]


def in_review(cfg, cid):
    """SPEC-0214: a committed write goes on to SPEC_REVIEW (it used to park)."""
    assert S.read_state(cfg, cid)["state"] == "SPEC_REVIEW"


def to_writer_park(cfg, cid):
    """Park a campaign in SPEC_REVIEW the way the old `spec_review_unavailable` park did."""
    st = S.read_state(cfg, cid)
    Q._park(cfg, st, "spec_review_unavailable", "test", "SPEC_WRITING")


def ledger(cfg, cid, *types):
    return S.read_ledger(cfg, cid, types=list(types))


def resume(cfg, cid, **kw):
    return Q.apply_resume(cfg, Q.plan_resume(cfg, cid, **kw))


def wrote(tmp_path, monkeypatch, extra_entries=(), **kw):
    """Run the first write; the campaign is then in SPEC_REVIEW."""
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [
        spec_written(2, i), *[e(i) if callable(e) else e for e in extra_entries]], **kw)
    Q.tick(cfg)
    in_review(cfg, cid)
    return cfg, repo, iid, cid


class Hooked:
    """A runner that runs `before(req)` / `after(req)` around the inner FakeRunner."""

    def __init__(self, tmp_path, before=None, after=None):
        self.inner, self.before, self.after = FakeRunner(tmp_path / "script.json"), before, after

    def run(self, req):
        if self.before:
            self.before(req)
        out = self.inner.run(req)
        if self.after:
            self.after(req)
        return out


# ---- AC1: write mode ---------------------------------------------------------------------------

def test_write_mode_commits_spec_and_ledger(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    wt = wtree(cfg, cid)
    path = "docs/specs/0002-add-a-thing.md"
    head_before = st["spec_write"]["head_before"]
    in_review(cfg, cid)
    changed = _git(wt, "diff", "--name-only", head_before, "HEAD").splitlines()
    assert sorted(changed) == sorted([path, "docs/LEDGER.md"])
    assert _git(wt, "rev-list", "--count", f"{head_before}..HEAD") == "1"
    assert "SPEC-0002" in _git(wt, "show", "-s", "--format=%s", "HEAD")
    assert "spec write" in _git(wt, "show", "-s", "--format=%s", "HEAD")
    assert "SPEC-0002" in (wt / "docs/LEDGER.md").read_text()
    assert subprocess.run([sys.executable, "tools/spec_lint.py"], cwd=wt,
                          capture_output=True, text=True).returncode == 0
    assert st["spec"] == {"id": "SPEC-0002", "rel_path": path, "internal": True}
    assert st["spec_id"] == "SPEC-0002" and st["spec_write"]["mode"] == "write"
    assert st["spec_write"]["number"] == 2 and st["pending_brief"] is None
    assert st["counters"].get("spec_write_retries", 0) == 0
    t = idea(cfg, iid)
    assert t.properties["SPEC"] == "SPEC-0002" and t.state == "SPECCED"
    (ev,) = ledger(cfg, cid, "spec_written")
    assert ev["mode"] == "write" and ev["spec_id"] == "SPEC-0002" and ev["rel_path"] == path
    assert SC.ledger_problems(ev) == []
    assert "setup-out.txt" not in _git(wt, "ls-files")        # setup output is never committed
    assert (wt / "setup-out.txt").exists()
    assert st["cost"]["by_persona"]["spec_writer"] == 0.4


def test_state_survives_finish_but_idea_is_not_overwritten(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    real = SW.commit_staged

    def commit(worktree, paths, message):
        with S.item_lock(cfg, cid):
            st = S.read_state(cfg, cid)
            st["idea"]["heading"] = "edited meanwhile"
            S.write_state(cfg, cid, st)
        return real(worktree, paths, message)

    monkeypatch.setattr(SW, "commit_staged", commit)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["idea"]["heading"] == "edited meanwhile"
    assert st["spec_id"] == "SPEC-0002" and st["spec_write"]["path"].endswith("add-a-thing.md")
    assert st["spec"]["id"] == "SPEC-0002"


def test_writer_committed_itself_and_nothing_to_regenerate(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)],
                                regenerate="true")

    def commit_all(req):
        env = {"GIT_AUTHOR_NAME": "w", "GIT_AUTHOR_EMAIL": "w@x", "GIT_COMMITTER_NAME": "w",
               "GIT_COMMITTER_EMAIL": "w@x"}
        import os
        e = {**os.environ, **env}
        subprocess.run(["git", "add", "docs/specs"], cwd=req.cwd, check=True, env=e)
        subprocess.run(["git", "commit", "-q", "-m", "writer's own commit"], cwd=req.cwd,
                       check=True, env=e)

    Q.tick(cfg, runner=Hooked(tmp_path, after=commit_all))
    st = S.read_state(cfg, cid)
    wt = wtree(cfg, cid)
    in_review(cfg, cid)
    assert _git(wt, "rev-list", "--count", f"{st['spec_write']['head_before']}..HEAD") == "1"
    assert _git(wt, "show", "-s", "--format=%s", "HEAD") == "writer's own commit"
    assert st["spec_id"] == "SPEC-0002"


def test_no_regenerate_entries_commits_spec_alone(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)],
                                regenerate=None)
    Q.tick(cfg)
    st, wt = S.read_state(cfg, cid), wtree(cfg, cid)
    assert _git(wt, "diff", "--name-only", st["spec_write"]["head_before"], "HEAD") \
        == "docs/specs/0002-add-a-thing.md"
    in_review(cfg, cid)


# ---- AC2: revise mode --------------------------------------------------------------------------

BRIEF = {"source": "spec_reviewer", "fix_brief": "tighten the spec",
         "must_fix": [{"title": "AC 1 is untestable", "detail": "name the exit code",
                       "section": "Acceptance criteria", "severity": "must_fix"}],
         "human_notes": []}


def _revise(cfg, cid, brief):
    if S.read_state(cfg, cid)["state"] != "PARKED":
        to_writer_park(cfg, cid)
    with S.item_lock(cfg, cid):
        st = S.read_state(cfg, cid)
        st["pending_brief"] = brief
        S.write_state(cfg, cid, st)
    resume(cfg, cid, to="SPEC_WRITING")
    Q.tick(cfg)


def test_revise_mode_edits_commits_and_clears_brief(tmp_path, monkeypatch):
    cfg, repo, iid, cid = wrote(tmp_path, monkeypatch, [
        lambda i: spec_written(2, i, mode="revise", extra_body="Revised: exit code 2.")])
    wt = wtree(cfg, cid)
    before = _git(wt, "rev-parse", "HEAD")
    _revise(cfg, cid, BRIEF)
    st = S.read_state(cfg, cid)
    p = prompt(cfg, cid, 2)
    assert "AC 1 is untestable" in p and "name the exit code" in p and "Acceptance criteria" in p
    assert "Revise mode: edit only `docs/specs/0002-add-a-thing.md`" in p
    assert "narrow the spec" not in p
    assert st["spec_write"]["mode"] == "revise" and st["pending_brief"] is None
    assert "Revised: exit code 2." in (wt / "docs/specs/0002-add-a-thing.md").read_text()
    assert _git(wt, "diff", "--name-only", before, "HEAD") == "docs/specs/0002-add-a-thing.md"
    in_review(cfg, cid)
    modes = [e["mode"] for e in ledger(cfg, cid, "spec_written")]
    assert modes == ["write", "revise"]


def test_revise_unchanged_file_is_a_problem(tmp_path, monkeypatch):
    cfg, repo, iid, cid = wrote(tmp_path, monkeypatch, [
        lambda i: spec_written(2, i, mode="revise", no_edit=True)] * 2)
    _revise(cfg, cid, BRIEF)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and "no change made" in park["detail"]


def test_revise_deleting_the_spec_and_changing_another_file_is_a_problem(tmp_path, monkeypatch):
    cfg, repo, iid, cid = wrote(tmp_path, monkeypatch, [
        lambda i: spec_written(2, i, mode="revise", no_edit=True, deletes=[
            "docs/specs/0002-add-a-thing.md"], extra_files={"other.txt": "x\n"})] * 2)
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    _revise(cfg, cid, BRIEF)
    assert _git(wt, "rev-parse", "HEAD") == head and (wt / "docs/specs/0002-add-a-thing.md").exists()
    assert not (wt / "other.txt").exists()
    st = S.read_state(cfg, cid)
    problems = st["pending_brief"]["write_problems"]
    spec = "docs/specs/0002-add-a-thing.md"
    assert f"{spec} was deleted" in problems
    assert f"other.txt was changed (only {spec} may change)" in problems
    assert st["counters"]["spec_write_retries"] == 1


def test_revise_with_suggested_splits_carries_the_narrowing_rule(tmp_path, monkeypatch):
    cfg, repo, iid, cid = wrote(tmp_path, monkeypatch, [
        lambda i: spec_written(2, i, mode="revise", extra_body="narrowed")])
    _revise(cfg, cid, {**BRIEF, "suggested_splits": [{"title": "Part A", "scope": "the core"},
                                                      {"title": "Part B"}]})
    p = prompt(cfg, cid, 2)
    assert "narrow the spec to the first suggested split" in p
    assert "list the rest under Non-goals" in p and "Part A" in p and "Part B" in p


# ---- AC3: invalid output is rolled back, retried once, then parked ---------------------------------

def _case_regen_fail(n, iid):
    return spec_written(n, iid)


CASES = {
    "wrong-number": lambda n, i: spec_written(n, i, path=f"docs/specs/{n + 5:04d}-add-a-thing.md"),
    "wrong-path": lambda n, i: spec_written(n, i, path="docs/specs/add-a-thing.md"),
    "second-file": lambda n, i: spec_written(n, i, extra_files={"docs/specs/extra.md": "x\n"}),
    "edited-ledger": lambda n, i: spec_written(n, i, extra_files={"docs/LEDGER.md": "x\n"}),
    "deletion": lambda n, i: spec_written(n, i, deletes=["gates/lint.sh"]),
    "status-accepted": lambda n, i: spec_written(n, i, status="accepted"),
    "no-source-idea": lambda n, i: spec_written(n, i, drop_fm=["source_idea"]),
    "no-created": lambda n, i: spec_written(n, i, drop_fm=["created"]),
    "unknown-key": lambda n, i: spec_written(n, i, fm={"bogus": "1"}),
    "wrong-owner": lambda n, i: spec_written(n, i, fm={"owner": "somebody"}),
    "wrong-parent": lambda n, i: spec_written(n, i, epic="SPEC-0001"),
    "non-string-title": lambda n, i: spec_written(n, i, fm={"title": "[1, 2]"}),
    "bad-depends-on": lambda n, i: spec_written(n, i, fm={"depends_on": "[nope]"}),
    "no-test-plan": lambda n, i: spec_written(n, i, drop_sections=["Test plan"]),
    "placeholder": lambda n, i: spec_written(n, i, extra_body="- …"),
    "outside-specs": lambda n, i: spec_written(n, i, extra_files={"src/x.py": "x = 1\n"}),
    "runner-error": lambda n, i: {**spec_written(n, i), "exit_code": 1},
    "spec-path-mismatch": lambda n, i: spec_written(n, i, verdict={"spec_path": "docs/other.md"}),
    "timed-out": lambda n, i: {**spec_written(n, i), "timed_out": True},
    "unparseable-verdict": lambda n, i: spec_written(n, i, verdict={"status": "maybe"}),
}


EXPECT = {
    "wrong-number": "0007-add-a-thing.md was changed", "wrong-path": "add-a-thing.md was changed",
    "second-file": "docs/specs/extra.md was changed", "edited-ledger": "docs/LEDGER.md was changed",
    "deletion": "gates/lint.sh was deleted", "status-accepted": "status must be proposed",
    "no-source-idea": "source_idea must be", "no-created": "'created' is a required property",
    "unknown-key": "'bogus' was unexpected", "wrong-owner": "owner must be user",
    "wrong-parent": "parent must be absent", "non-string-title": "title: [1, 2] is not of type",
    "bad-depends-on": "depends_on.0: 'nope' does not match",
    "no-test-plan": "missing section '## Test plan'", "placeholder": "template placeholder",
    "outside-specs": "src/x.py was changed", "runner-error": "run failed (exit 1)",
    "spec-path-mismatch": "verdict spec_path 'docs/other.md' is not",
    "timed-out": "run timed out", "unparseable-verdict": "no parseable verdict",
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_invalid_output_rolled_back_retried_once_then_parked(tmp_path, monkeypatch, name):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [CASES[name](2, i)] * 2)
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    Q.tick(cfg)                                       # first attempt: rejected, retried
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and st["counters"]["spec_write_retries"] == 1
    assert st["pending_brief"]["write_problems"]
    assert _git(wt, "rev-parse", "HEAD") == head
    Q.tick(cfg)                                       # second attempt: parked
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert EXPECT[name] in park["detail"], park["detail"]
    st = S.read_state(cfg, cid)
    assert st["counters"]["spec_write_retries"] == 2
    assert prompt(cfg, cid, 1).count("docs/specs/0002-add-a-thing.md") \
        and "Your last attempt was rejected" in prompt(cfg, cid, 2)
    assert re.search(r"Spec file: `([^`]+)`", prompt(cfg, cid, 1)).group(1) \
        == re.search(r"Spec file: `([^`]+)`", prompt(cfg, cid, 2)).group(1)
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain").splitlines() == ["?? setup-out.txt"]
    assert [r["number"] for r in SN.list_reservations(cfg, specs_dir=wt / "docs/specs")] == [2]
    assert any(f["gate"] == "spec-write" and f["severity"] == "major"
               for f in ledger(cfg, cid, "finding"))
    assert st["spec"]["id"] is None and "SPEC" not in idea(cfg, iid).properties


def test_rejected_revise_keeps_the_review_findings_next_to_the_problems(tmp_path, monkeypatch):
    cfg, repo, iid, cid = wrote(tmp_path, monkeypatch, [
        lambda i: spec_written(2, i, mode="revise", no_edit=True),
        lambda i: spec_written(2, i, mode="revise", extra_body="Revised.")])
    brief = {**BRIEF, "suggested_splits": [{"title": "Part A", "scope": "the core"}]}
    _revise(cfg, cid, brief)                          # attempt 1: nothing changed, rejected
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and st["counters"]["spec_write_retries"] == 1
    kept = st["pending_brief"]
    assert kept["must_fix"] == BRIEF["must_fix"] and kept["suggested_splits"] == brief["suggested_splits"]
    assert kept["write_problems"] and "no change made" in kept["write_problems"][0]
    Q.tick(cfg)                                       # attempt 2: valid
    retry = prompt(cfg, cid, 3)
    assert "Your last attempt was rejected" in retry and "AC 1 is untestable" in retry
    assert "narrow the spec to the first suggested split" in retry
    assert S.read_state(cfg, cid)["pending_brief"] is None
    in_review(cfg, cid)


def test_epic_child_with_a_different_parent_is_rejected(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch,
                                lambda i: [spec_written(101, i, epic="SPEC-0099")] * 2,
                                epic="SPEC-0100")
    Q.tick(cfg)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and "parent must be SPEC-0100" in park["detail"]


def test_non_git_exception_in_the_worker_rolls_back_and_parks(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)] * 2)
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    real = SW.regenerate
    monkeypatch.setattr(SW, "regenerate", lambda *a, **k: {}["boom"])
    report = Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert "KeyError" in park["detail"] and any("KeyError" in e for e in report.errors)
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain").splitlines() == ["?? setup-out.txt"]
    monkeypatch.setattr(SW, "regenerate", real)
    resume(cfg, cid)                                  # no leftover file to wedge the next run
    Q.tick(cfg)
    in_review(cfg, cid)
    assert S.read_state(cfg, cid)["spec_id"] == "SPEC-0002"


def test_a_leftover_spec_file_is_removed_and_never_hides_the_new_write(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)] * 2)
    wt = wtree(cfg, cid)
    real_roll, real_regen = SW.rollback, SW.regenerate
    monkeypatch.setattr(SW, "rollback", lambda *a, **k: None)       # a rollback that left it behind
    monkeypatch.setattr(SW, "regenerate", lambda *a, **k: {}["boom"])
    Q.tick(cfg)
    leftover = wt / "docs/specs/0002-add-a-thing.md"
    assert parked(cfg, cid)["reason"] == "spec_write_failed" and leftover.exists()
    monkeypatch.setattr(SW, "rollback", real_roll)
    monkeypatch.setattr(SW, "regenerate", real_regen)
    resume(cfg, cid)
    Q.tick(cfg)
    in_review(cfg, cid)
    st = S.read_state(cfg, cid)
    assert st["spec_id"] == "SPEC-0002"
    assert "docs/specs/0002-add-a-thing.md" not in st["spec_write"]["untracked_before"]


def test_writer_leaving_the_branch_parks_without_a_reset(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    wt = wtree(cfg, cid)
    Q.tick(cfg, runner=Hooked(tmp_path, after=lambda req: _git(wt, "checkout", "-q", "--detach")))
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed"
    assert park["detail"].startswith("HEAD is no longer on ")
    assert "clean the worktree, then resume" not in park["detail"]
    assert (wt / "docs/specs/0002-add-a-thing.md").exists()


def _switch_away(wt):
    """The writer hops to another branch and commits there."""
    def hop(req):
        _git(wt, "checkout", "-q", "-b", "other")
        (wt / "other.txt").write_text("work\n")
        _git(wt, "add", "other.txt")
        _git(wt, "commit", "-q", "-m", "other work")
    return hop


@pytest.mark.parametrize("how", ["blocked", "interrupted", "crash"])
def test_off_branch_rollback_never_resets_the_other_branch(tmp_path, monkeypatch, how):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [
        spec_written(2, i, no_edit=True, verdict={"status": "blocked", "blockers": ["no"]})
        if how == "blocked" else spec_written(2, i)])
    wt = wtree(cfg, cid)
    stop = threading.Event()
    hop = _switch_away(wt)
    if how == "crash":
        monkeypatch.setattr(SW, "validate_spec_change", lambda *a, **k: {}["boom"])
    Q.tick(cfg, stop=stop, runner=Hooked(tmp_path, after=lambda req: (
        hop(req), stop.set() if how == "interrupted" else None)))
    other = _git(wt, "rev-parse", "other")
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed"
    assert park["detail"].startswith("HEAD is no longer on ")
    assert "clean the worktree, then resume" not in park["detail"]
    assert _git(wt, "rev-parse", "other") == other and (wt / "other.txt").exists()
    assert _git(wt, "log", "-1", "--format=%s", "other") == "other work"


def test_file_committed_then_deleted_by_the_writer_is_a_problem(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)] * 2)
    wt = wtree(cfg, cid)

    def commit_and_delete(req):
        (wt / "stray.txt").write_text("x\n")
        _git(wt, "add", "stray.txt")
        _git(wt, "commit", "-q", "-m", "stray")
        (wt / "stray.txt").unlink()
    head = _git(wt, "rev-parse", "HEAD")
    Q.tick(cfg, runner=Hooked(tmp_path, after=commit_and_delete))
    assert _git(wt, "rev-parse", "HEAD") == head            # rolled back, not kept on the branch
    st = S.read_state(cfg, cid)
    assert st["counters"]["spec_write_retries"] == 1
    assert st["pending_brief"]["write_problems"] == [
        "stray.txt was deleted (only docs/specs/0002-add-a-thing.md may change)"]


def test_amended_base_commit_is_a_problem(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)] * 2)
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    Q.tick(cfg, runner=Hooked(tmp_path, after=lambda req: _git(
        wt, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")))
    assert _git(wt, "rev-parse", "HEAD") == head
    st = S.read_state(cfg, cid)
    assert st["counters"]["spec_write_retries"] == 1
    assert any("rewrote history" in p for p in st["pending_brief"]["write_problems"])


# ---- SPEC-0256: the idea link is best-effort ---------------------------------------------------

def _no_specced_keyword(cfg):
    path = Path(cfg["org_ideas_file"])
    path.write_text(path.read_text().replace(" SPECCED", "", 1))


def _link_failure_checks(cfg, iid, cid):
    st = S.read_state(cfg, cid)
    in_review(cfg, cid)         # SPEC-0214: the committed write goes on to its review
    assert st["spec_id"] == "SPEC-0002" and st["spec"]["id"] == "SPEC-0002"
    assert st["counters"].get("spec_write_retries", 0) == 0 and st["pending_brief"] is None
    (ev,) = ledger(cfg, cid, "spec_written")
    assert ev["spec_id"] == "SPEC-0002"
    assert ledger(cfg, cid, "commit") and ledger(cfg, cid, "run.end") and ledger(cfg, cid, "cost")
    (fail,) = ledger(cfg, cid, "idea_link_failed")
    assert fail["spec_id"] == "SPEC-0002" and fail["idea"] == iid and fail["error"]
    notes = [n for n in ledger(cfg, cid, "note")
             if n["text"].startswith("could not link idea to SPEC-0002:")]
    assert len(notes) == 1 and notes[0]["author"] == "queue" and fail["error"] in notes[0]["text"]
    assert all(SC.ledger_problems(r) == [] for r in S.read_ledger(cfg, cid))
    return fail


@pytest.mark.parametrize("which", ["set_property", "set_state"])
def test_idea_link_drift_is_best_effort_not_a_tick_abort(tmp_path, monkeypatch, which):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    real = getattr(OW, which)
    calls = []

    def drift(*a, **k):
        calls.append(which)
        raise ValueError("drift")
    monkeypatch.setattr(OW, which, drift)
    report = Q.tick(cfg)
    assert calls and not report.errors
    assert _link_failure_checks(cfg, iid, cid)["error"] == "drift"
    monkeypatch.setattr(OW, which, real)


def test_idea_link_without_a_specced_keyword_is_best_effort(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    _no_specced_keyword(cfg)
    Q.tick(cfg)
    _link_failure_checks(cfg, iid, cid)


def test_idea_link_oserror_reading_the_ideas_file_is_swallowed(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    real = Q._idea_task
    state = {"armed": False}

    def moved(cfg_, idea_id):
        if state["armed"]:
            raise FileNotFoundError("ideas.org moved")
        return real(cfg_, idea_id)
    monkeypatch.setattr(Q, "_idea_task", moved)
    # the writer run is the last thing before the link: arm once the worker has finished
    monkeypatch.setattr(SW, "commit_staged", (lambda orig: lambda *a, **k: (
        state.__setitem__("armed", True), orig(*a, **k))[1])(SW.commit_staged))
    Q.tick(cfg)
    assert "ideas.org moved" in _link_failure_checks(cfg, iid, cid)["error"]


def test_link_idea_returns_none_on_success(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [])
    st = S.read_state(cfg, cid)
    assert Q._link_idea(cfg, st, "SPEC-0002") is None
    t = idea(cfg, iid)
    assert t.properties["SPEC"] == "SPEC-0002" and t.state == "SPECCED"
    assert Q._link_idea(cfg, st, "SPEC-0002") is None                # idempotent


def test_exception_escaping_do_write_spec_is_a_step_error(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])

    def boom(*a, **k):
        raise RuntimeError("worker died")
    monkeypatch.setattr(Q, "_do_write_spec", boom)
    report = Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert "worker died" in park["detail"] and any("worker died" in e for e in report.errors)
    st = S.read_state(cfg, cid)
    assert st["spec_write"]["mode"] == "write" and not st.get("spec_id")
    (start_ev,) = ledger(cfg, cid, "run.start")                     # the submitted step is on record
    assert start_ev["step"]["kind"] == "write_spec" and start_ev["persona"] == "spec_writer"
    assert st["counters"]["runs"] == 1


def test_missing_parent_for_epic_idea_is_rejected(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(101, i)] * 2,
                                epic="SPEC-0100")
    Q.tick(cfg)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and "parent must be SPEC-0100" in park["detail"]


def test_failing_regenerate_command_rolls_back_and_removes_its_file(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(
        tmp_path, monkeypatch, lambda i: [spec_written(2, i)] * 2,
        regenerate="touch leftover.txt; echo boom >&2; exit 3")
    wt = wtree(cfg, cid)
    Q.tick(cfg)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and "exited 3" in park["detail"]
    assert not (wt / "leftover.txt").exists()
    assert not (wt / "docs/specs/0002-add-a-thing.md").exists()
    assert (wt / "setup-out.txt").exists()
    assert _git(wt, "status", "--porcelain").splitlines() == ["?? setup-out.txt"]


def test_undeclared_changed_path_from_regeneration_is_a_problem(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(
        tmp_path, monkeypatch, lambda i: [spec_written(2, i)] * 2,
        regenerate=f"{REGEN} && touch stray.txt")
    Q.tick(cfg)
    Q.tick(cfg)
    assert "stray.txt" in parked(cfg, cid)["detail"]
    assert not (wtree(cfg, cid) / "stray.txt").exists()


# ---- AC4: blocked, interrupted ---------------------------------------------------------------------

def test_blocked_verdict_parks_with_blockers(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(
        2, i, no_edit=True, verdict={"status": "blocked", "blockers": ["needs two specs"]})])
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["detail"] == "blocked: needs two specs"
    assert park["resume_to"] == "SPEC_WRITING"


def test_interrupted_run_rolled_back_without_counting_a_retry(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    wt = wtree(cfg, cid)
    head = _git(wt, "rev-parse", "HEAD")
    stop = threading.Event()
    report = Q.tick(cfg, stop=stop, runner=Hooked(tmp_path, after=lambda req: stop.set()))
    st = S.read_state(cfg, cid)
    assert report.steps[-1]["reason"] == "interrupted"
    assert st["state"] == "SPEC_WRITING" and st["counters"].get("spec_write_retries", 0) == 0
    assert _git(wt, "rev-parse", "HEAD") == head
    assert not (wt / "docs/specs/0002-add-a-thing.md").exists()
    assert (wt / "setup-out.txt").exists()


# ---- AC5: generic errors ---------------------------------------------------------------------------

def test_git_error_in_commit_parks_and_a_resume_writes(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)] * 2)
    real = SW.commit_staged
    calls = []

    def flaky(worktree, paths, message):
        calls.append(1)
        if len(calls) == 1:
            raise GitError(["git", "commit"], 1, "injected")
        return real(worktree, paths, message)

    monkeypatch.setattr(SW, "commit_staged", flaky)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert park["detail"].startswith("write_spec step failed:") and "injected" in park["detail"]
    wt = wtree(cfg, cid)
    assert _git(wt, "status", "--porcelain").splitlines() == ["?? setup-out.txt"]   # rolled back
    resume(cfg, cid)
    Q.tick(cfg)
    in_review(cfg, cid)
    assert S.read_state(cfg, cid)["spec_id"] == "SPEC-0002"
    assert _git(wt, "show", "-s", "--format=%s", "HEAD").startswith("nw(")
    assert [e["type"] for e in S.read_ledger(cfg, cid) if e["type"] == "run.end"] \
        .__len__() == 2                                  # the failed attempt was still recorded


def test_failed_rollback_parks_at_once_and_a_dirty_worktree_parks_again(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [
        spec_written(2, i, extra_files={"gates/test.sh": "changed\n"})] * 2)
    real = SW.rollback

    def broken(worktree, sw):
        raise GitError(["git", "reset"], 1, "nope")

    monkeypatch.setattr(SW, "rollback", broken)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed"
    assert park["detail"] == "rollback failed; clean the worktree, then resume"
    assert S.read_state(cfg, cid)["counters"].get("spec_write_retries", 0) == 0
    resume(cfg, cid)
    Q.tick(cfg)                                           # still dirty: parked again, no run
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and "uncommitted changes" in park["detail"]
    assert S.read_state(cfg, cid)["counters"]["runs"] == 1
    monkeypatch.setattr(SW, "rollback", real)
    _git(wtree(cfg, cid), "checkout", "--", "gates/test.sh")
    resume(cfg, cid)
    Q.tick(cfg)                                           # clean again: the writer runs
    assert S.read_state(cfg, cid)["counters"]["runs"] == 2


# ---- AC6: cost cap ---------------------------------------------------------------------------------

def test_cost_cap_parks_without_a_run_and_raise_cap_resumes(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    with S.item_lock(cfg, cid):
        st = S.read_state(cfg, cid)
        st["cost"]["cap_usd"] = 1.0                       # balanced spec_writer budget is 1.50
        S.write_state(cfg, cid, st)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "cost_cap" and park["resume_to"] == "SPEC_WRITING"
    assert S.read_state(cfg, cid)["counters"]["runs"] == 0
    resume(cfg, cid, raise_cap=10.0)
    Q.tick(cfg)
    in_review(cfg, cid)


def test_empty_brief_in_revise_mode_wins_over_the_cap():
    profile = PR.resolve_profile({}, "balanced")
    state = {"state": "SPEC_WRITING", "spec": {"id": "SPEC-0002"}, "pending_brief": None,
             "cost": {"cap_usd": 0.1, "total_usd": 0.0}}
    assert Q.plan_step(state, profile).reason == "spec_review_unavailable"
    state["spec"] = {"id": None}
    assert Q.plan_step(state, profile).reason == "cost_cap"
    state["cost"]["cap_usd"] = 50
    step = Q.plan_step(state, profile)
    assert step.kind == "write_spec" and step.persona == "spec_writer"


# ---- AC7/9/10: resumes -------------------------------------------------------------------------------

def test_resume_spec_write_failed_with_note_refreshes_idea_and_resets_retries(tmp_path, monkeypatch):
    from nightwright import explore as EX
    bad = lambda i: spec_written(2, i, status="accepted")        # noqa: E731
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [bad(i), bad(i),
                                                                  spec_written(2, i)])
    Q.tick(cfg)
    Q.tick(cfg)
    assert parked(cfg, cid)["reason"] == "spec_write_failed"
    assert S.read_state(cfg, cid)["idea"]["state_before"] == "IDEA"
    EX.set_summary(cfg, iid, "A brand new summary about frobnicators.")
    resume(cfg, cid, note="please keep it small")
    st = S.read_state(cfg, cid)
    assert st["counters"]["spec_write_retries"] == 0
    assert "frobnicators" in st["idea"]["summary"] and st["idea"]["state_before"] == "IDEA"
    Q.tick(cfg)
    p = prompt(cfg, cid, 3)
    assert "please keep it small" in p and "frobnicators" in p
    in_review(cfg, cid)


def test_resume_review_unavailable_without_note_reviews_with_note_revises(tmp_path, monkeypatch):
    cfg, repo, iid, cid = wrote(tmp_path, monkeypatch, [
        lambda i: spec_written(2, i, mode="revise", extra_body="noted")])
    to_writer_park(cfg, cid)
    resume(cfg, cid)                                  # SPEC-0214: no note goes to the review
    assert S.read_state(cfg, cid)["state"] == "SPEC_REVIEW"
    assert S.read_state(cfg, cid)["counters"]["runs"] == 1
    to_writer_park(cfg, cid)
    resume(cfg, cid, note="mention the exit code")
    Q.tick(cfg)
    in_review(cfg, cid)
    st = S.read_state(cfg, cid)
    assert st["counters"]["runs"] == 2 and st["spec_write"]["mode"] == "revise"
    assert "mention the exit code" in prompt(cfg, cid, 2)


def test_legacy_spec_writer_unavailable_park_resumes_into_the_writer(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    Q._park(cfg, S.read_state(cfg, cid), "spec_writer_unavailable", "old", "SPEC_WRITING")
    resume(cfg, cid)
    Q.tick(cfg)
    in_review(cfg, cid)
    assert S.read_state(cfg, cid)["spec_id"] == "SPEC-0002"


# ---- epic child --------------------------------------------------------------------------------------

def test_epic_idea_naming_no_epic_spec_parks_without_a_run(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)],
                                epic=None)
    # re-point the campaign's idea at an :EPIC: that does not exist
    with S.item_lock(cfg, cid):
        st = S.read_state(cfg, cid)
        st["idea"]["epic"] = "SPEC-0777"
        S.write_state(cfg, cid, st)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert ":EPIC: SPEC-0777 is not an epic spec" in park["detail"]
    st = S.read_state(cfg, cid)
    assert st["counters"]["runs"] == 0 and not (S.runs_dir(cfg, cid) / "001-spec_writer").exists()
    assert SN.list_reservations(cfg, specs_dir=wtree(cfg, cid) / "docs/specs") == []


def test_epic_child_is_written_with_the_parent_and_the_prompt_names_the_epic(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch,
                                lambda i: [spec_written(101, i, epic="SPEC-0100")],
                                epic="SPEC-0100")
    Q.tick(cfg)
    in_review(cfg, cid)
    p = prompt(cfg, cid)
    assert "The big epic" in p and "SPEC-0099: Sibling child" in p and "parent: SPEC-0100" in p
    text = (wtree(cfg, cid) / "docs/specs/0101-add-a-thing.md").read_text()
    assert "parent: SPEC-0100" in text


# ---- human changed the state during the write ---------------------------------------------------------

@pytest.mark.parametrize("to,linked", [("PARKED", True), ("ABANDONED", False)])
def test_human_state_change_during_write_keeps_bookkeeping_matching_the_commit(
        tmp_path, monkeypatch, to, linked):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])

    def human(req):
        with S.item_lock(cfg, cid):
            st = S.read_state(cfg, cid)
            st["state"] = to
            if to == "PARKED":
                st["park"] = {"reason": "human", "detail": "x", "since": S.now_iso(),
                              "resume_to": "SPEC_WRITING"}
            S.write_state(cfg, cid, st)

    Q.tick(cfg, runner=Hooked(tmp_path, after=human))
    st = S.read_state(cfg, cid)
    assert st["state"] == to
    assert st["spec"]["id"] == "SPEC-0002" and st["spec_id"] == "SPEC-0002"
    assert st["spec"]["rel_path"] == "docs/specs/0002-add-a-thing.md"
    assert (idea(cfg, iid).properties.get("SPEC") == "SPEC-0002") is linked


# ---- the saved prompt and the request -------------------------------------------------------------------

def test_write_mode_prompt_and_request(tmp_path, monkeypatch):
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    Q.tick(cfg)
    p = prompt(cfg, cid)
    assert "{{" not in p
    assert "Add a thing" in p                                         # the idea snapshot
    assert (ROOT / "docs/specs/TEMPLATE.md").read_text().strip() in p
    assert (ROOT / "docs/specs/SCHEMA.md").read_text().strip() in p
    assert "Spec id: `SPEC-0002`" in p and "Spec file: `docs/specs/0002-add-a-thing.md`" in p
    assert "create exactly `docs/specs/0002-add-a-thing.md`" in p
    assert "answer `blocked`" in p and "docs/LEDGER.md" in p
    assert "parent:" not in p.split("## Pending brief")[0].split("## Your task")[1]
    # the request carries the writer invariant, not the implementer's
    profile = PR.resolve_profile(cfg, "balanced")
    req = PS.build_request(cfg, profile, {}, S.read_state(cfg, cid), "spec_writer",
                           run_dir=tmp_path / "r", extra={"spec_id": "SPEC-0002"})
    assert req.system_append.startswith("## nw spec writer invariants")
    assert f"campaign {cid}" in req.system_append and not req.read_only


def test_two_campaigns_in_one_tick_get_different_numbers(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile={"slots": 2})
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    cids = []
    for text in ("First thing", "Second thing"):
        iid = capture(cfg, text)
        cids.append(add_idea_campaign(cfg, monkeypatch, iid))
    Q.tick(cfg)
    assert {S.read_state(cfg, c)["state"] for c in cids} == {"SPEC_WRITING"}

    class Smart:
        """Writes a valid spec for whatever its prompt names."""

        def run(self, req):
            path = re.search(r"Spec file: `([^`]+)`", req.prompt).group(1)
            number = int(re.search(r"Spec id: `SPEC-(\d{4})`", req.prompt).group(1))
            iid = re.search(r'"id": "(IDEA-\d+)"', req.prompt).group(1)
            entry = spec_written(number, iid, path=path, verdict={"spec_path": path})
            p = req.run_dir.parent.parent / f"{number}.json"
            p.write_text(json.dumps([entry]))
            return FakeRunner(p).run(req)

    Q.tick(cfg, runner=Smart())
    ids = {S.read_state(cfg, c)["spec_id"] for c in cids}
    assert len(ids) == 2 and None not in ids
    assert {S.read_state(cfg, c)["state"] for c in cids} == {"SPEC_REVIEW"}


# ---- profiles, placeholder parity --------------------------------------------------------------------------

def test_every_builtin_profile_has_a_spec_writer_entry():
    for name, prof in PR.builtin_profiles().items():
        entry = prof["personas"]["spec_writer"]
        assert entry["permission_mode"] == "bypassPermissions" and not entry["read_only"], name
        assert entry["max_budget_usd"] > 0 and entry["model"] and entry["effort"], name
    assert PR.builtin_profiles()["balanced"]["personas"]["spec_writer"]["max_budget_usd"] == 1.5
    assert "spec_written" in SC.EVENT_KEYS
    assert Q.SPEC_WRITE_RETRY_MAX == 1
    assert {"spec_write_failed", "spec_review_unavailable", "spec_writer_unavailable"} \
        <= Q.PARK_REASONS


_PLACEHOLDER_INPUTS = [
    "a\n<The chosen approach in a few sentences.>\n", "x\ny\n- …\n", "  - …  \n",
    "- **<option>** — why\n", '---\n# milestone: "M?: <phase>"\n---\n',
    "Mention `<option>` and `- …` inline.\n```\n- …\n<option>\n```\n- a real bullet … with text\n",
    (ROOT / "docs/specs/TEMPLATE.md").read_text(), "nothing here\n",
]


@pytest.mark.parametrize("text", _PLACEHOLDER_INPUTS)
def test_specmeta_placeholder_rule_matches_the_linter(text):
    assert specmeta.first_placeholder(text) == L.first_placeholder(text)


def test_specmeta_normalize_matches_the_linter():
    import datetime as dt
    doc = {"created": dt.date(2026, 10, 25), "x": [dt.datetime(2026, 1, 2, 3, 4, 5), "s", 1]}
    assert SW._normalize(doc) == L._normalize(doc)


# ---- SPEC-0279: amend mode ---------------------------------------------------------------------

AMEND_FINDINGS = [{"severity": "major", "section": "Design", "title": "gap in step two",
                   "detail": "state the timeout behaviour"},
                  {"severity": "nit", "title": "loose wording"}]
AMEND_PATH = "docs/specs/0005-add-a-thing.md"


def amend_fx(tmp_path, monkeypatch, entries):
    """A project whose accepted SPEC-0005 has a `revise` review for its current text, queued and
    opened into SPEC_WRITING (the spec writer amends it)."""
    from test_agentic_e2e import added, write_review
    repo = make_project(tmp_path)
    _write(repo, AMEND_PATH, _spec_md(5, None, status="accepted"))
    subprocess.run([sys.executable, "tools/spec_lint.py", "--write-ledger"], cwd=repo, check=True,
                   capture_output=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "an accepted spec")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    write_review(cfg, repo / AMEND_PATH, "SPEC-0005", "revise", findings=AMEND_FINDINGS)
    cid = added(cfg, monkeypatch, "SPEC-0005")
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and st["admission"] == "revise_pending"
    script(tmp_path, entries)
    return cfg, repo, cid


def amended(extra_body="Amended: timeout is 30s.", **kw):
    kw.setdefault("fm", {"updated": "2026-10-27"})
    return spec_written(5, None, mode="amend", status="accepted", extra_body=extra_body, **kw)


def test_amend_mode_rules_and_frontmatter_guard(tmp_path, monkeypatch):
    bad = [amended(fm={"status": "proposed"})] * 2
    cfg, repo, cid = amend_fx(tmp_path, monkeypatch, bad)
    Q.tick(cfg)                                                  # rejected, rolled back, retried
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and st["counters"]["spec_write_retries"] == 1
    assert "front matter: status was changed from 'accepted' to 'proposed'" \
        in st["pending_brief"]["write_problems"][0]
    assert "Amended" not in (wtree(cfg, cid) / AMEND_PATH).read_text()
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and "an amend may change only `updated`" \
        in park["detail"]
    p = prompt(cfg, cid, 1)
    assert "Amend mode: edit only `docs/specs/0005-add-a-thing.md`" in p
    assert "gap in step two" in p and "state the timeout behaviour" in p
    assert "Revise mode" not in p
    # a removed and an added key are problems too
    fm = {"id": "SPEC-0005", "status": "accepted", "owner": "user", "kind": "feature",
          "title": "x", "created": "2026-10-25", "source_idea": "IDEA-1"}
    before = {k: v for k, v in fm.items() if k != "source_idea"}
    probs = SW._amend_problems(before, {**fm, "updated": "2026-10-27"})
    assert probs == ["front matter: source_idea was added (an amend may change only `updated`)"]
    probs = SW._amend_problems(fm, {k: v for k, v in fm.items() if k != "owner"})
    assert probs == ["front matter: owner was removed (an amend may change only `updated`)"]


def test_amend_body_and_updated_only_is_valid(tmp_path, monkeypatch):
    cfg, repo, cid = amend_fx(tmp_path, monkeypatch, [amended()])
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING", st.get("park")
    assert "updated: 2026-10-27" in (wtree(cfg, cid) / AMEND_PATH).read_text()


def test_amend_snapshot_failure_keeps_writer_brief(tmp_path, monkeypatch):
    cfg, repo, cid = amend_fx(tmp_path, monkeypatch, [amended()])
    st = S.read_state(cfg, cid)
    brief_before = st["pending_brief"]
    real = Q._replace_spec_at_open_snapshot
    monkeypatch.setattr(Q, "_replace_spec_at_open_snapshot", lambda *a, **k: "disk full")
    Q.tick(cfg)
    monkeypatch.setattr(Q, "_replace_spec_at_open_snapshot", real)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    st = S.read_state(cfg, cid)
    assert st["admission"] == "revise_pending"
    assert st["pending_brief"] == brief_before
    assert "spec amend" in _git(wtree(cfg, cid), "show", "-s", "--format=%s", "HEAD")


def test_amend_success_goes_to_implementing(tmp_path, monkeypatch):
    from nightwright.agentic import spec_review as SR
    cfg, repo, cid = amend_fx(tmp_path, monkeypatch, [amended()])
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    wt = wtree(cfg, cid)
    assert st["state"] == "IMPLEMENTING" and st["park"] is None
    assert st["admission"] == "after_revision" and st["spec_write"]["mode"] == "amend"
    spec_file = wt / AMEND_PATH
    assert "Amended: timeout is 30s." in spec_file.read_text()
    assert st["spec"]["sha256_at_open"] == SR.spec_sha256(spec_file)
    baseline = S.campaign_dir(cfg, cid) / "spec_at_open.md"
    assert baseline.read_text() == spec_file.read_text()
    brief = st["pending_brief"]
    assert brief["source"] == "spec_review"
    assert [m["title"] for m in brief["must_fix"]] == ["[major] gap in step two (Design)",
                                                       "[nit] loose wording"]
    (tr,) = [e for e in ledger(cfg, cid, "transition") if e["to"] == "IMPLEMENTING"]
    assert tr["from"] == "SPEC_WRITING" and tr["reason"] == "spec_amended"
    (ev,) = ledger(cfg, cid, "spec_written")
    assert ev["mode"] == "amend" and ev["spec_id"] == "SPEC-0005" and SC.ledger_problems(ev) == []
    assert not [e for e in ledger(cfg, cid, "run.start") if e["persona"] == "spec_reviewer"]
    assert "spec amend" in _git(wt, "show", "-s", "--format=%s", "HEAD")
    assert st["counters"].get("spec_write_retries", 0) == 0


def test_amend_number_and_spec_id_flow(tmp_path, monkeypatch):
    cfg, repo, cid = amend_fx(tmp_path, monkeypatch, [amended(no_edit=True)] * 2)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    sw = st["spec_write"]
    assert sw["number"] == 5 and sw["path"] == AMEND_PATH and sw["mode"] == "amend"
    assert sw["fm_before"]["status"] == "accepted" and "updated" not in sw["fm_before"]
    assert SW.validate_spec_change(wtree(cfg, cid), sw, None) == [f"no change made to {AMEND_PATH}"]
    Q.tick(cfg)
    assert f"no change made to {AMEND_PATH}" in parked(cfg, cid)["detail"]


def test_amend_precedes_revise_branch(tmp_path, monkeypatch):
    cfg, repo, cid = amend_fx(tmp_path, monkeypatch, [amended()])
    st = S.read_state(cfg, cid)
    assert st["spec"]["id"] == "SPEC-0005"             # the revise branch would match on this
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["spec_write"]["mode"] == "amend"


# ---- SPEC-0290: split mode ---------------------------------------------------------------------

SPLIT_FM = {"id": "SPEC-0102", "status": "accepted", "kind": "feature",
            "supersedes": ["SPEC-0005"], "depends_on": ["SPEC-0101"], "parent": "SPEC-0100"}


@pytest.mark.parametrize("key,value", [
    ("id", "SPEC-0109"), ("status", "proposed"), ("kind", "epic"), ("supersedes", []),
    ("depends_on", ["SPEC-0001"]), ("depends_on", []), ("parent", "SPEC-0099"), ("parent", None)])
def test_split_problems_reports_each_wrong_key(key, value):
    fm = {**SPLIT_FM, key: value}
    if value is None:
        fm.pop(key)
    (problem,) = SW._split_problems(fm, SPLIT_FM)
    assert problem.startswith(f"front matter: {key} must be")
    assert SW._split_problems(SPLIT_FM, SPLIT_FM) == []


def test_split_problems_expects_no_parent_when_the_original_has_none():
    expected = {k: v for k, v in SPLIT_FM.items() if k != "parent"}
    assert SW._split_problems(expected, expected) == []
    (problem,) = SW._split_problems(SPLIT_FM, expected)
    assert "parent must be absent" in problem


def test_split_write_rules_name_scope_other_parts_and_front_matter():
    parts = [{"title": "A", "scope": "alpha"}, {"title": "B", "scope": "beta"},
             {"title": "C", "scope": "gamma"}]
    sw = {"path": "docs/specs/0102-b.md", "expected_fm": SPLIT_FM,
          "split": {"k": 2, "n": 3, "original": "SPEC-0005", "parts": parts,
                    "source_idea": "IDEA-065"}}
    rules = SW.write_rules("split", sw, None, None)
    assert rules.startswith("Split mode: create exactly `docs/specs/0102-b.md`")
    assert "part 2 of 3" in rules and "Your scope: beta" in rules
    assert "- part 1: A — alpha" in rules and "- part 3: C — gamma" in rules
    assert "part 2: B" not in rules
    assert "`id: SPEC-0102`" in rules and "`status: accepted`" in rules
    assert '`supersedes: ["SPEC-0005"]`' in rules and '`depends_on: ["SPEC-0101"]`' in rules
    assert "`parent: SPEC-0100`" in rules and "`source_idea: IDEA-065`" in rules
    assert "Carry over only this part's Context" in rules


def test_split_snapshot_validates_like_write(tmp_path):
    repo = make_project(tmp_path)
    sw = SW.snapshot(repo, mode="split", number=102, path="docs/specs/0102-b.md")
    assert sw["mode"] == "split" and "fm_before" not in sw


def test_rate_limited_spec_writer_starts_the_backoff_and_holds_the_rewrite(tmp_path,
                                                                           monkeypatch):
    """SPEC-0345: a spec writer run's 429 API error is folded into the account backoff; the
    SPEC_WRITING step then waits for it (no new writer run)."""
    from nightwright.agentic import events as EV
    from nightwright.agentic import persona_gate as PG
    hit = {"persona": "spec_writer", "edits": [], "verdict": None, "cost_usd": 0.1,
           "api_error": {"status": 429, "message": "API Error: 429 rate limit"}}
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [hit, spec_written(2, i)])
    Q.tick(cfg)
    assert len(ledger(cfg, cid, "run.end")) == 1
    assert PG.read_rate_limit(cfg)["backoff_s"] == 60
    assert PG.backoff_until(cfg) is not None
    assert [e["campaign"] for e in EV.read_since(cfg, 0) if e["type"] == "rate_limited"] == [cid]
    for _ in range(3):
        Q.tick(cfg)
    assert len(ledger(cfg, cid, "run.start")) == 1
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"


def test_a_backoff_begun_after_the_gate_holds_the_spec_writer_at_its_start(tmp_path,
                                                                           monkeypatch):
    """SPEC-0345: a backoff that starts after the tick's gate admitted the write_spec step
    (another run's 429) holds the writer at its start: the run ends interrupted, no retry is
    charged, and the writer runs once the backoff is over."""
    from nightwright.agentic import persona_gate as PG
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])

    class StaleGate(PG.Gate):                       # the gate read the backoff before it began
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.until = None

    real_gate = PG.Gate
    monkeypatch.setattr(PG, "Gate", StaleGate)
    PG._write_json(PG.rate_limit_path(cfg),
                   {"until": PG._iso(PG._now_epoch(None) + 900), "backoff_s": 60})
    cost0 = S.read_state(cfg, cid)["cost"]["total_usd"]
    Q.tick(cfg)
    end, = ledger(cfg, cid, "run.end")
    assert end["interrupted"] is True
    assert end["charged_usd"] == 0.0 and end["cost_estimated"] is False    # never started
    assert S.read_state(cfg, cid)["cost"]["total_usd"] == cost0
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"
    assert not (wtree(cfg, cid) / "docs/specs/0002-add-a-thing.md").exists()
    monkeypatch.setattr(PG, "Gate", real_gate)
    PG.rate_limit_path(cfg).unlink()
    Q.tick(cfg)
    assert len(ledger(cfg, cid, "run.end")) == 2
    in_review(cfg, cid)


# ---- SPEC-0441: an adopted spec write must not kill the runner -------------------------------------

class _Blocked(FakeRunner):
    """The 'dead runner' of SPEC-0441: its writer never finishes, so the run stays open."""

    def __init__(self, path, release):
        super().__init__(path)
        self.release = release

    def run(self, req, *, hooks=None):
        self.release.wait(30)
        raise ValueError("the old runner's writer was killed")


class _Adopter(FakeRunner):
    """`detachable` lets the first tick of a new pool adopt open runs. An adopted request is
    rebuilt without a `cwd` (FakeRunner leaves no `request.json`), so the writer's worktree is
    given here, as the real runner's request record would."""
    detachable = True

    def __init__(self, path, cwd):
        super().__init__(path)
        self.cwd = cwd

    def run(self, req, *, hooks=None):
        return super().run(dataclasses.replace(req, cwd=self.cwd), hooks=hooks)


def test_adopted_open_write_spec_run_finishes_through_the_spec_writer_path(tmp_path, monkeypatch):
    """SPEC-0441: a new runner adopts the open `write_spec` run and applies it like a freshly
    submitted one. It used to go through `_do_persona`, which never sets `res.write`, and the
    unguarded harvest died on `w["blocked"]`."""
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    release, old = threading.Event(), Q.StepPool()
    try:
        Q.tick(cfg, runner=_Blocked(tmp_path / "script.json", release), pool=old)
        assert [e["persona"] for e in Q._open_runs(cfg, cid)] == ["spec_writer"]
        assert not ledger(cfg, cid, "run.end")
        new, adopter = Q.StepPool(), _Adopter(tmp_path / "script.json", wtree(cfg, cid))
        report = Q.tick(cfg, runner=adopter, pool=new)
        deadline = time.monotonic() + 30
        while not ledger(cfg, cid, "run.end") and time.monotonic() < deadline:
            new.wait_all()
            report = Q.tick(cfg, runner=adopter, pool=new)
        assert not report.errors
        (end,) = ledger(cfg, cid, "run.end")
        assert end["persona"] == "spec_writer"
        in_review(cfg, cid)                                  # the normal outcome of a good write
        assert S.read_state(cfg, cid)["spec_id"] == "SPEC-0002"
        assert (wtree(cfg, cid) / "docs/specs/0002-add-a-thing.md").exists()
        new.shutdown()
    finally:
        release.set()
        old.shutdown()


def test_spec_write_without_a_write_result_parks_instead_of_raising(tmp_path, monkeypatch):
    """SPEC-0441 backstop: a result with no `write` records the run and parks."""
    cfg, repo, iid, cid = start(tmp_path, monkeypatch, lambda i: [spec_written(2, i)])
    real = Q._do_write_spec

    def no_write(*a, **k):
        res = real(*a, **k)
        res.write = None
        return res

    monkeypatch.setattr(Q, "_do_write_spec", no_write)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed" and park["resume_to"] == "SPEC_WRITING"
    assert park["detail"] == "spec writer returned no write result"
    assert len(ledger(cfg, cid, "run.end")) == 1


# ---- SPEC-0535: source_request cite -----------------------------------------------------------

def _publish_for_campaign(cfg, monkeypatch, repo, iid, summary="Public body"):
    from test_agentic_e2e import run_cli
    _git(repo, "config", "user.name", "nightwright test")
    _git(repo, "config", "user.email", "nightwright@test")
    run_cli(cfg, monkeypatch, ["idea", "summary", iid, "--set", summary])
    t = idea(cfg, iid)
    if not (t.properties.get("PROJECT") or "").strip():
        OW.set_property(cfg, t, "PROJECT", "Meta-Tools")
    r = run_cli(cfg, monkeypatch, ["idea", "publish", iid])
    assert r.exit_code == 0, r.output
    return idea(cfg, iid).properties["REQ"]


def spec_with_req(number, iid, req, **kw):
    return spec_written(number, iid, fm={"source_request": req}, **kw)


def test_linked_idea_writer_uses_the_public_summary_and_cites(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    req = _publish_for_campaign(cfg, monkeypatch, repo, iid, "Public body")
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING"
    assert st["idea"]["summary"] == "Public body"
    assert st["idea"]["source_request"] == req
    assert "public summary" in SW.write_rules(
        "write", {"path": "docs/specs/0002-x.md", "number": 2}, st["idea"], None)
    script(tmp_path, [spec_written(2, iid), spec_with_req(2, iid, req)])
    Q.tick(cfg)
    probs = (S.read_state(cfg, cid).get("pending_brief") or {}).get("write_problems") or []
    assert any("source_request must be" in p for p in probs)
    tips = []
    real_run = subprocess.run

    forbidden = {"checkout", "switch", "add", "fetch", "push"}
    tip_before = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()

    def spy(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and Path(cmd[2]).resolve() == repo.resolve():
            if len(cmd) > 3:
                tips.append(cmd[3:])
                if cmd[3] in forbidden:
                    raise AssertionError(f"unexpected git {cmd[3]!r}")
        return real_run(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    Q.tick(cfg)
    in_review(cfg, cid)
    kinds = {c[0] for c in tips if c}
    assert kinds >= {"hash-object", "mktree", "commit-tree", "update-ref"}
    upd = [c for c in tips if c and c[0] == "update-ref"]
    assert upd and upd[-1][-1] == tip_before
    assert idea(cfg, iid).state == "SPECCED"
    blob = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/nw-backlog:{req}.md"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "spec: SPEC-0002" in blob
    assert idea(cfg, iid).properties["SPEC"] == "SPEC-0002"
    backlog_n = int(_git(repo, "rev-list", "--count", "refs/heads/nw-backlog"))
    to_writer_park(cfg, cid)
    with S.item_lock(cfg, cid):
        st = S.read_state(cfg, cid)
        st["pending_brief"] = BRIEF
        st["admission"] = "revise_pending"
        S.write_state(cfg, cid, st)
    script(tmp_path, [spec_with_req(2, iid, req, mode="revise")])
    resume(cfg, cid)
    Q.tick(cfg)
    assert int(_git(repo, "rev-list", "--count", "refs/heads/nw-backlog")) == backlog_n


def test_linked_idea_resume_preserves_source_request_for_revise(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    req = _publish_for_campaign(cfg, monkeypatch, repo, iid, "Public body")
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    script(tmp_path, [spec_with_req(2, iid, req)])
    Q.tick(cfg)
    in_review(cfg, cid)
    backlog_n = int(_git(repo, "rev-list", "--count", "refs/heads/nw-backlog"))
    st = S.read_state(cfg, cid)
    Q._park(cfg, st, "spec_review_exhausted", "test", "SPEC_WRITING")
    resume(cfg, cid)
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING"
    assert st.get("admission") != "revise_pending"
    assert st["idea"]["source_request"] == req
    assert st["idea"]["summary"] == "Public body"
    spec_path = wtree(cfg, cid) / "docs/specs/0002-add-a-thing.md"
    from nightwright.specmeta import split_frontmatter
    no_req = "\n".join(
        ln for ln in spec_path.read_text().splitlines() if not ln.startswith("source_request:")
    ) + "\n"
    fm, _ = split_frontmatter(no_req)
    assert any("source_request must be" in p for p in SW._front_matter_problems(
        fm, "SPEC-0002", st["idea"]))
    kept = spec_path.read_text().replace("Why.", "Public still.")
    good = spec_written(2, iid, mode="revise", no_edit=True)
    good["edits"] = [{"path": "docs/specs/0002-add-a-thing.md", "write": kept}]
    script(tmp_path, [good])
    Q.tick(cfg)
    assert int(_git(repo, "rev-list", "--count", "refs/heads/nw-backlog")) == backlog_n


def test_drift_before_write_parks_without_a_cite(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    req = _publish_for_campaign(cfg, monkeypatch, repo, iid)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    script(tmp_path, [spec_with_req(2, iid, req)])
    from nightwright import spec_numbers as SN
    from test_agentic_e2e import run_cli
    run_cli(cfg, monkeypatch, ["idea", "summary", iid, "--set", "Drifted private"])
    res_n = len(SN.list_reservations(cfg))
    hash_calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if len(cmd) > 3 and cmd[3] == "hash-object":
            hash_calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    Q.tick(cfg)
    Q.tick(cfg)
    park = parked(cfg, cid)
    assert park["reason"] == "spec_write_failed"
    assert "do not match :REQ_BASE:" in park["detail"]
    assert not (wtree(cfg, cid) / "docs/specs/0002-add-a-thing.md").exists()
    assert not hash_calls
    assert len(SN.list_reservations(cfg)) == res_n


def test_apply_add_idea_sets_public_summary_and_source_request(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    req = _publish_for_campaign(cfg, monkeypatch, repo, iid, "Public-only body")
    plan = Q.plan_add_idea(cfg, iid)
    st = Q.apply_add_idea(cfg, plan)
    assert st["idea"]["source_request"] == req
    assert st["idea"]["summary"] == "Public-only body"
    assert idea(cfg, iid).properties.get("CAMPAIGN") == plan["campaign"]
    Q.tick(cfg)
    st2 = S.read_state(cfg, plan["campaign"])
    assert st2["idea"]["source_request"] == req
    assert st2["idea"]["summary"] == "Public-only body"


def test_link_idea_restores_backlog_when_org_write_fails(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    req = _publish_for_campaign(cfg, monkeypatch, repo, iid, "Public body")
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    st["spec_write"] = {"mode": "write"}
    tip = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()
    spec_id = "SPEC-0002"
    org_before = Path(cfg["org_ideas_file"]).read_bytes()

    from nightwright import org_write as OW

    def boom_sp(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(OW, "set_property", boom_sp)
    err = Q._link_idea(cfg, st, spec_id)
    assert err and "restored refs/heads/nw-backlog" in err
    assert _git(repo, "rev-parse", "refs/heads/nw-backlog").strip() == tip
    blob = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/nw-backlog:{req}.md"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "spec:" not in blob
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_before
    assert idea(cfg, iid).properties.get("SPEC", "").strip() == ""


def test_link_idea_restores_backlog_when_set_state_fails(tmp_path, monkeypatch):
    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    req = _publish_for_campaign(cfg, monkeypatch, repo, iid, "Public body")
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    st = S.read_state(cfg, cid)
    st["spec_write"] = {"mode": "write"}
    tip = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()
    spec_id = SW.spec_id_of((st.get("spec_write") or {}).get("number") or 2)
    org_before = Path(cfg["org_ideas_file"]).read_bytes()

    from nightwright.org_write import set_state as real_ss

    def boom_st(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(OW, "set_state", boom_st)
    err = Q._link_idea(cfg, st, spec_id)
    assert err and "restored refs/heads/nw-backlog" in err
    assert _git(repo, "rev-parse", "refs/heads/nw-backlog").strip() == tip
    blob = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/nw-backlog:{req}.md"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "spec:" not in blob
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_before
    assert idea(cfg, iid).properties.get("SPEC", "").strip() == ""
    monkeypatch.setattr(OW, "set_state", real_ss)


def test_queue_add_refuses_a_drifted_pair(tmp_path, monkeypatch):
    from test_agentic_e2e import run_cli

    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    _publish_for_campaign(cfg, monkeypatch, repo, iid, "Hello")
    run_cli(cfg, monkeypatch, ["idea", "summary", iid, "--set", "Drifted"])
    with pytest.raises(ValueError, match="do not match :REQ_BASE:"):
        Q.plan_add_idea(cfg, iid)
    assert "CAMPAIGN" not in idea(cfg, iid).properties
    run_cli(cfg, monkeypatch, ["idea", "summary", iid, "--set", "Hello"])
    plan = Q.plan_add_idea(cfg, iid)
    run_cli(cfg, monkeypatch, ["idea", "summary", iid, "--set", "Drifted"])
    with pytest.raises(ValueError, match="do not match :REQ_BASE:"):
        Q.apply_add_idea(cfg, plan)
    assert "CAMPAIGN" not in idea(cfg, iid).properties


def test_apply_add_idea_refuses_when_request_already_cites(tmp_path, monkeypatch):
    from nightwright import idea_link as IL

    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    _publish_for_campaign(cfg, monkeypatch, repo, iid)
    plan = Q.plan_add_idea(cfg, iid)
    agreed = IL.load_agreed_request(cfg, idea(cfg, iid))
    IL.cite_spec(cfg, agreed, "SPEC-0099")
    with pytest.raises(ValueError, match="already cites"):
        Q.apply_add_idea(cfg, plan)
    assert "CAMPAIGN" not in idea(cfg, iid).properties


def test_queue_add_refuses_when_the_request_already_cites(tmp_path, monkeypatch):
    from nightwright import idea_link as IL

    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    _publish_for_campaign(cfg, monkeypatch, repo, iid)
    task = idea(cfg, iid)
    agreed = IL.load_agreed_request(cfg, task)
    IL.cite_spec(cfg, agreed, "SPEC-0099")
    created = []

    def spy(cfg, state, **kw):
        created.append(1)
        return S.create_campaign(cfg, state, **kw)

    monkeypatch.setattr(S, "create_campaign", spy)
    with pytest.raises(ValueError, match="already cites"):
        Q.plan_add_idea(cfg, iid)
    assert not created


def test_link_idea_refuses_a_different_cite(tmp_path, monkeypatch):
    from nightwright import idea_link as IL

    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    req = _publish_for_campaign(cfg, monkeypatch, repo, iid)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    st = S.read_state(cfg, cid)
    st["spec_write"] = {"mode": "write"}
    agreed = IL.load_agreed_request(cfg, idea(cfg, iid))
    IL.cite_spec(cfg, agreed, "SPEC-0099")
    blob_before = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/nw-backlog:{req}.md"],
        check=True, capture_output=True, text=True,
    ).stdout
    cite_calls = []
    real_cite = IL.cite_spec

    def spy_cite(cfg_a, ag, sid):
        cite_calls.append(sid)
        return real_cite(cfg_a, ag, sid)

    monkeypatch.setattr(IL, "cite_spec", spy_cite)
    err = Q._link_idea(cfg, st, "SPEC-0002")
    assert err and "already cites" in err
    assert cite_calls == []
    assert idea(cfg, iid).properties.get("SPEC") != "SPEC-0002"
    assert subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/nw-backlog:{req}.md"],
        check=True, capture_output=True, text=True,
    ).stdout == blob_before
    monkeypatch.setattr(IL, "cite_spec", real_cite)
    agreed = IL.load_agreed_request(cfg, idea(cfg, iid))
    IL.cite_spec(cfg, agreed, "SPEC-0002")
    tip = _git(repo, "rev-parse", "refs/heads/nw-backlog")
    assert Q._link_idea(cfg, st, "SPEC-0002") is None
    assert idea(cfg, iid).properties["SPEC"] == "SPEC-0002"
    assert idea(cfg, iid).state == "SPECCED"
    assert _git(repo, "rev-parse", "refs/heads/nw-backlog") == tip


def test_campaign_cite_follows_resolve_linked_repo(tmp_path, monkeypatch):
    from test_agentic_e2e import _write, make_cfg, make_repo

    base = tmp_path / "split"
    (base / "nw").mkdir(parents=True)
    (base / "out").mkdir(parents=True)
    (base / "data").mkdir(parents=True)
    nw = make_repo(base / "nw")
    out = make_repo(base / "out")
    _git(nw, "config", "user.name", "nightwright test")
    _git(nw, "config", "user.email", "nightwright@test")
    scr = script(base / "data", [])
    cfg = make_cfg(base / "data", out, scr,
                   outbox={"Meta-Tools": {"repo_path": str(out)}})
    cfg["nw_checkout"] = str(nw)
    cfg["specs_dir"] = str(nw / "docs/specs")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    for rel in ("docs/specs/TEMPLATE.md", "docs/specs/SCHEMA.md", "docs/specs/spec.schema.json"):
        _write(nw, rel, (ROOT / rel).read_text())
    iid = capture(cfg)
    OW.set_property(cfg, idea(cfg, iid), "PROJECT", "Meta-Tools")
    req = _publish_for_campaign(cfg, monkeypatch, nw, iid)
    nw_tip = _git(nw, "rev-parse", "refs/heads/nw-backlog")
    out_tip = _backlog_tip(out)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    script(base / "data", [spec_with_req(2, iid, req)])
    Q.tick(cfg)
    Q.tick(cfg)
    assert f"source_request: {req}" in (wtree(cfg, cid) / "docs/specs/0002-add-a-thing.md").read_text()
    assert idea(cfg, iid).properties["SPEC"] == "SPEC-0002"
    assert idea(cfg, iid).state == "SPECCED"
    assert _git(nw, "rev-parse", "refs/heads/nw-backlog") == nw_tip
    assert _backlog_tip(out) == out_tip


def test_link_idea_drifts_after_spec_committed(tmp_path, monkeypatch):
    from test_agentic_e2e import run_cli

    repo = make_project(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    iid = capture(cfg)
    req = _publish_for_campaign(cfg, monkeypatch, repo, iid)
    cid = add_idea_campaign(cfg, monkeypatch, iid)
    Q.tick(cfg)
    script(tmp_path, [spec_with_req(2, iid, req)])
    real_link = Q._link_idea

    def drift_before_link(cfg_a, state, spec_id):
        run_cli(cfg_a, monkeypatch, ["idea", "summary", iid, "--set", "Drifted after write"])
        return real_link(cfg_a, state, spec_id)

    monkeypatch.setattr(Q, "_link_idea", drift_before_link)
    Q.tick(cfg)
    in_review(cfg, cid)
    fail = _link_failure_checks(cfg, iid, cid)
    assert "do not match :REQ_BASE:" in fail["error"]
    assert idea(cfg, iid).properties.get("SPEC") != "SPEC-0002"
    assert (wtree(cfg, cid) / "docs/specs/0002-add-a-thing.md").exists()
    blob = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/nw-backlog:{req}.md"],
        check=True, capture_output=True, text=True,
    ).stdout
    assert "spec: SPEC-0002" not in blob


def _backlog_tip(repo: Path) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "refs/heads/nw-backlog"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


# ---- SPEC-0541: a spec from a public request alone ---------------------------------------------

from test_request_source import (BODY as REQ_BODY, PROJECT as REQ_PROJECT,  # noqa: E402
                                 TITLE as REQ_TITLE, add_request, corrupt_org, linked_idea,
                                 put_request)
from nightwright import idea_link as IL  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from nightwright.cli import cli  # noqa: E402


def _backlog_subjects(repo):
    return _git(repo, "log", "--format=%s", "refs/heads/nw-backlog").splitlines()


def start_request(tmp_path, monkeypatch, entries, *, linked=False):
    """A hand-built `request_only` campaign (SPEC-0541) opened into SPEC_WRITING. The queue has
    no command for it yet: an idea campaign is the vehicle, its payload is then replaced."""
    repo = make_project(tmp_path)
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    if linked:
        task, req = linked_idea(cfg, monkeypatch)
    else:
        req = add_request(repo)
    cid = add_idea_campaign(cfg, monkeypatch, capture(cfg, "Vehicle"))
    with S.item_lock(cfg, cid):
        st = S.read_state(cfg, cid)
        st["idea"] = {"schema": "nw.idea.v1", "id": req, "state": "", "heading": REQ_TITLE,
                      "tags": [], "kind": "idea", "summary": REQ_BODY, "questions": "",
                      "question_items": [], "log": "", "project": REQ_PROJECT,
                      "source_request": req, "request_only": True, "epic": None,
                      "state_before": None}
        S.write_state(cfg, cid, st)
    Q.tick(cfg)
    assert S.read_state(cfg, cid)["state"] == "SPEC_WRITING"
    script(tmp_path, entries(req) if callable(entries) else entries)
    return cfg, repo, req, cid


def _req_spec(number, req, **kw):
    return spec_written(number, None, slug="export-the-thing",
                        fm={"source_request": req, **kw.pop("fm", {})}, **kw)


def test_request_only_writer_uses_the_public_summary_and_cites(tmp_path, monkeypatch):
    bad = lambda r: spec_written(2, "IDEA-001", slug="export-the-thing",       # noqa: E731
                                 fm={"source_request": r})
    cfg, repo, req, cid = start_request(tmp_path, monkeypatch,
                                        lambda r: [bad(r), _req_spec(2, r),
                                                    _req_spec(2, r, mode="revise",
                                                              extra_body="Revised.")],
                                        linked=True)
    org = Path(cfg["org_ideas_file"])
    org_before = org.read_bytes()
    commits_before = _backlog_subjects(repo)
    Q.tick(cfg)                                    # rejected: source_idea is present
    st = S.read_state(cfg, cid)
    assert st["state"] == "SPEC_WRITING" and st["counters"]["spec_write_retries"] == 1
    assert _backlog_subjects(repo) == commits_before
    assert any("source_idea must be absent (the campaign comes from a request alone)" in
               f["detail"] for f in ledger(cfg, cid, "finding"))
    Q.tick(cfg)
    in_review(cfg, cid)
    first = prompt(cfg, cid, 2)
    idea_json = first[first.index("{"):]
    assert REQ_BODY in idea_json and "`source_request: %s`" % req in first
    assert "`source_idea:" not in first and "no `source_idea` key" in first
    wt = wtree(cfg, cid)
    assert subprocess.run([sys.executable, "tools/spec_lint.py"], cwd=wt,
                          capture_output=True, text=True).returncode == 0
    fm, _ = specmeta.split_frontmatter((wt / "docs/specs/0002-export-the-thing.md").read_text())
    assert fm["source_request"] == req and "source_idea" not in fm
    # one cite commit, `spec:` added, nothing else changed
    assert _backlog_subjects(repo) == [f"cite {req} SPEC-0002", *commits_before]
    tip_before = _git(repo, "rev-parse", "refs/heads/nw-backlog~1").strip()
    old = _git(repo, "show", f"{tip_before}:{req}.md").splitlines()
    new = _git(repo, "show", f"refs/heads/nw-backlog:{req}.md").splitlines()
    def bare(lines):                  # the cite refreshes `updated*` and adds `spec:`; nothing else
        return [l for l in lines if not l.startswith(("updated:", "updated_by:", "spec:"))]
    assert bare(new) == bare(old) and len(new) == len(old) + 1 and "spec: SPEC-0002" in new
    assert [l for l in old if l.startswith("spec:")] == []
    assert _git(repo, "diff", "--name-only", tip_before, "refs/heads/nw-backlog").split() \
        == [f"{req}.md"]
    assert org.read_bytes() == org_before                       # no :SPEC:, no SPECCED
    assert not ledger(cfg, cid, "idea_link_failed")
    # a second link for the same spec: None, no commit, no failure event
    assert Q._link_idea(cfg, S.read_state(cfg, cid), "SPEC-0002") is None
    assert _backlog_subjects(repo)[0] == f"cite {req} SPEC-0002" and \
        len(_backlog_subjects(repo)) == len(commits_before) + 1
    assert not ledger(cfg, cid, "idea_link_failed")
    # a revise tick adds no commit, and the campaign's own cite is no drift
    _revise(cfg, cid, BRIEF)
    in_review(cfg, cid)
    assert len(_backlog_subjects(repo)) == len(commits_before) + 1
    assert "Revise mode" in prompt(cfg, cid, 3) and "keep its id, `source_request`" in \
        prompt(cfg, cid, 3)
    assert org.read_bytes() == org_before


def test_request_only_prepare_overwrites_a_stale_snapshot(tmp_path, monkeypatch):
    """`_prepare_spec_write` takes `summary` and `heading` from the public file, not from the
    campaign's hand-built (stale) idea payload, and the spec slug follows the public title."""
    new_title, new_body = "A better public title", "A fresher public summary."
    cfg, repo, req, cid = start_request(
        tmp_path, monkeypatch,
        lambda r: [spec_written(2, None, slug="a-better-public-title",
                                fm={"source_request": r})])
    with S.item_lock(cfg, cid):
        st = S.read_state(cfg, cid)
        st["idea"]["summary"], st["idea"]["heading"] = "OLD summary", "Old title"
        S.write_state(cfg, cid, st)
    put_request(repo, req, title=new_title, summary=new_body)
    Q.tick(cfg)
    in_review(cfg, cid)
    first = prompt(cfg, cid, 1)
    idea_json = first[first.index("{"):]
    assert new_body in idea_json and new_title in idea_json
    assert "OLD summary" not in idea_json and "Old title" not in idea_json
    idea = S.read_state(cfg, cid)["idea"]
    assert idea["summary"] == new_body and idea["heading"] == new_title
    assert idea["request_only"] is True and idea["id"] == req
    assert "docs/specs/0002-a-better-public-title.md" in first
    assert (wtree(cfg, cid) / "docs/specs/0002-a-better-public-title.md").exists()


def test_request_only_front_matter_rules():
    idea = {"id": "REQ-0ABCDEFG", "source_request": "REQ-0ABCDEFG", "request_only": True}
    fm = {"id": "SPEC-0002", "status": "proposed", "source_request": "REQ-0ABCDEFG",
          "owner": "user", "kind": "feature"}
    assert SW._front_matter_problems(fm, "SPEC-0002", idea) == []
    assert "source_idea must be absent (the campaign comes from a request alone), got " \
        "'IDEA-001'" in "".join(SW._front_matter_problems(
            {**fm, "source_idea": "IDEA-001"}, "SPEC-0002", idea))
    assert "source_request must be 'REQ-0ABCDEFG'" in "".join(SW._front_matter_problems(
        {k: v for k, v in fm.items() if k != "source_request"}, "SPEC-0002", idea))
    assert "source_request must be" in "".join(SW._front_matter_problems(
        {**fm, "source_request": "REQ-00000000"}, "SPEC-0002", idea))
    plain = {"id": "IDEA-001"}                                  # SPEC-0535's check is kept
    assert "source_idea must be 'IDEA-001'" in "".join(SW._front_matter_problems(
        {k: v for k, v in fm.items() if k != "source_request"}, "SPEC-0002", plain))
    rules = SW.write_rules("write", {"path": "docs/specs/0002-x.md", "number": 2}, idea, None)
    assert "`source_request: REQ-0ABCDEFG`" in rules and "`source_idea:" not in rules
    assert "only input" in rules


def test_request_only_drift_before_write_parks(tmp_path, monkeypatch):
    cfg, repo, req, cid = start_request(tmp_path, monkeypatch, [_req_spec(2, "x")], linked=True)
    r = CliRunner().invoke(cli, ["idea", "summary", linked_id(cfg), "--set", "Changed privately."])
    assert r.exit_code == 0, r.output
    objects = _git(repo, "count-objects", "-v")
    Q.tick(cfg)
    assert parked(cfg, cid)["reason"] == "spec_write_failed"
    assert "do not match :REQ_BASE:" in parked(cfg, cid)["detail"]
    assert not list(wtree(cfg, cid).glob("docs/specs/0002-*"))
    assert _git(repo, "count-objects", "-v") == objects          # no `hash-object`
    assert not S.read_ledger(cfg, cid, types=["run.start"])


def test_request_only_closed_before_write_parks(tmp_path, monkeypatch):
    cfg, repo, req, cid = start_request(tmp_path, monkeypatch, [_req_spec(2, "x")])
    put_request(repo, req, status="closed")
    Q.tick(cfg)
    assert parked(cfg, cid)["reason"] == "spec_write_failed"
    assert f"{req} is closed" in parked(cfg, cid)["detail"]
    assert not S.read_ledger(cfg, cid, types=["run.start"])


def test_request_only_unreadable_org_file_before_write_parks(tmp_path, monkeypatch):
    cfg, repo, req, cid = start_request(tmp_path, monkeypatch, [_req_spec(2, "x")])
    corrupt_org(cfg, monkeypatch)
    Q.tick(cfg)
    assert parked(cfg, cid)["reason"] == "spec_write_failed"
    assert "could not read the ideas file to check" in parked(cfg, cid)["detail"]
    assert not list(wtree(cfg, cid).glob("docs/specs/0002-*"))


def test_request_only_cited_before_write_parks(tmp_path, monkeypatch):
    cfg, repo, req, cid = start_request(tmp_path, monkeypatch, [_req_spec(2, "x")])
    put_request(repo, req, spec="SPEC-0042")
    objects = _git(repo, "count-objects", "-v")
    Q.tick(cfg)
    assert parked(cfg, cid)["reason"] == "spec_write_failed"
    assert f"{req} already cites SPEC-0042" in parked(cfg, cid)["detail"]
    assert not list(wtree(cfg, cid).glob("docs/specs/0002-*"))
    assert _git(repo, "count-objects", "-v") == objects
    assert not S.read_ledger(cfg, cid, types=["run.start"])


def test_request_only_missing_file_before_write_parks(tmp_path, monkeypatch):
    cfg, repo, req, cid = start_request(tmp_path, monkeypatch, [_req_spec(2, "x")])
    tip = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()
    empty = _git(repo, "hash-object", "-t", "tree", "/dev/null").strip()
    gone = _git(repo, "commit-tree", empty, "-p", tip, "-m", "remove").strip()
    _git(repo, "update-ref", "refs/heads/nw-backlog", gone)
    objects = _git(repo, "count-objects", "-v")
    Q.tick(cfg)
    assert parked(cfg, cid)["reason"] == "spec_write_failed"
    assert not list(wtree(cfg, cid).glob("docs/specs/0002-*"))
    assert _git(repo, "count-objects", "-v") == objects
    assert not S.read_ledger(cfg, cid, types=["run.start"])


def linked_id(cfg):
    ORG_ids = [t for t in Q._ideas(cfg) if (t.properties.get("REQ") or "").strip()]
    return ORG_ids[0].properties["ID"]


@pytest.mark.parametrize("case", ["moved_tip", "other_spec", "drift", "org_unreadable"])
def test_request_only_cite_failure_still_advances(tmp_path, monkeypatch, case):
    cfg, repo, req, cid = start_request(tmp_path, monkeypatch, lambda r: [_req_spec(2, r)],
                                        linked=case in ("drift",))
    runner = None
    if case == "moved_tip":
        real = IL.cite_spec

        def late(cfg_, agreed, spec_id, *, expected_backlog_tip=None):
            put_request(repo, req, summary=REQ_BODY)             # the tip moves before the swap
            return real(cfg_, agreed, spec_id, expected_backlog_tip=expected_backlog_tip)
        monkeypatch.setattr(IL, "cite_spec", late)
    else:
        def after(_req):
            if case == "other_spec":
                put_request(repo, req, spec="SPEC-0042")
            elif case == "drift":
                CliRunner().invoke(cli, ["idea", "summary", linked_id(cfg), "--set", "Moved."])
            else:
                corrupt_org(cfg, monkeypatch)
        runner = Hooked(tmp_path, after=after)
    tip = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()
    Q.tick(cfg, runner=runner) if runner else Q.tick(cfg)
    in_review(cfg, cid)
    (fail,) = ledger(cfg, cid, "idea_link_failed")
    assert f"nw backlog edit {req} --spec SPEC-0002" in fail["error"]
    if case == "other_spec":
        assert _backlog_subjects(repo)[0].startswith("put")
        assert "already cites SPEC-0042" in fail["error"]
    elif case == "moved_tip":
        assert "was not cited" in fail["error"]
        assert _git(repo, "rev-list", "--count", f"{tip}..refs/heads/nw-backlog").strip() == "1"
    else:
        assert not [s for s in _backlog_subjects(repo) if s.startswith("cite")]
    assert (wtree(cfg, cid) / "docs/specs/0002-export-the-thing.md").exists()


def test_request_only_reset_refreshes_the_snapshot(tmp_path, monkeypatch):
    cfg, repo, req, cid = start_request(tmp_path, monkeypatch, lambda r: [_req_spec(2, r)])
    put_request(repo, req, status="closed")
    Q.tick(cfg)
    assert parked(cfg, cid)["reason"] == "spec_write_failed"
    put_request(repo, req, title="A better title", summary="A brand new public summary.")
    resume(cfg, cid)
    st = S.read_state(cfg, cid)
    assert st["idea"]["summary"] == "A brand new public summary."
    assert st["idea"]["heading"] == "A better title" and st["idea"]["request_only"] is True
    assert st["idea"]["id"] == req and st["counters"]["spec_write_retries"] == 0
    # a removed file keeps the old snapshot
    tip = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()
    empty = _git(repo, "hash-object", "-t", "tree", "/dev/null").strip()
    gone = _git(repo, "commit-tree", empty, "-p", tip, "-m", "remove").strip()
    _git(repo, "update-ref", "refs/heads/nw-backlog", gone)
    before = dict(st["idea"])
    Q._reset_spec_write(cfg, st)
    assert st["idea"] == before
    # a campaign that already cited its own spec still resets
    put_request(repo, req, title="Third title", summary="Third summary.", spec="SPEC-0002")
    st["spec"] = {"id": "SPEC-0002", "rel_path": "docs/specs/0002-x.md", "internal": True}
    st["spec_id"] = "SPEC-0002"
    Q._reset_spec_write(cfg, st)
    assert st["idea"]["summary"] == "Third summary." and st["idea"]["heading"] == "Third title"
    # another spec's citation keeps the snapshot
    st["spec"], st["spec_id"] = {"id": "SPEC-0003"}, "SPEC-0003"
    Q._reset_spec_write(cfg, st)
    assert st["idea"]["summary"] == "Third summary."
