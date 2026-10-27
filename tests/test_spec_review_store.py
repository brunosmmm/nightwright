"""SPEC-0210: the data-dir review store, any-repo reviews, the standalone review journal and the
legacy-sidecar fallback of the `wt queue add` gate."""
import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from test_agentic_e2e import SPEC_ACCEPTED, make_cfg, make_repo, run_cli, script, write_review
from wt.agentic import spec_review as SR

SIZED = {"size": "m", "tone": "high-end", "rationale": "r"}            # SPEC-0219
APPROVE = {"verdict": "approve", **SIZED, "findings": [{"severity": "nit", "section": "Context",
                                               "title": "wording"}], "suggested_splits": []}
MUST_FIX = {"verdict": "approve", **SIZED, "findings": [{"severity": "must_fix", "title": "t"}]}


def _entry(verdict=None, **kw):
    return {"persona": "spec_reviewer", "verdict": verdict or APPROVE, "cost_usd": 0.42, **kw}


def _setup(tmp_path, entries):
    repo = make_repo(tmp_path, review=False)
    return repo, make_cfg(tmp_path, repo, script(tmp_path, entries))


def _spec(repo):
    return repo / "docs" / "specs" / "0001-trivial.md"


def _add(cfg, monkeypatch):
    return run_cli(cfg, monkeypatch, ["queue", "add", "SPEC-0001", "--json"])


def _second_repo(tmp_path, git=True):
    other = tmp_path / "other"
    (other / "docs" / "specs").mkdir(parents=True)
    (other / "docs" / "specs" / "SCHEMA.md").write_text("OTHER-REPO-SCHEMA-MARKER\n")
    spec = other / "docs" / "specs" / "0007-thing.md"
    spec.write_text(SPEC_ACCEPTED.format(id="SPEC-0007", extra=""))
    if git:
        subprocess.run(["git", "-C", str(other), "init", "-q", "-b", "main"], check=True)
    return other, spec


class Spy:
    def __init__(self, inner=None):
        self.inner, self.seen = inner, []

    def run(self, req):
        self.seen.append(req)
        if self.inner:
            return self.inner.run(req)
        raise ValueError("stop")


def test_review_writes_store_with_repo_root_and_nothing_in_repo(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_entry()])
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--json"])
    assert r.exit_code == 0, r.output
    sha = SR.spec_sha256(_spec(repo))
    path = Path(cfg["data_dir"]) / "agentic" / "reviews" / "SPEC-0001" / f"{sha}.json"
    assert path == SR.store_path(cfg, "SPEC-0001", sha)
    stored = json.loads(path.read_text())
    out = json.loads(r.stdout)
    assert out.pop("profile") == "balanced" and stored == out      # SPEC-0219: not stored
    assert stored["repo_root"] == str(repo.resolve()) and stored["spec_sha256"] == sha
    assert not (repo / "docs" / "specs" / "reviews").exists()
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True).stdout.strip() == ""


def test_queue_add_gate_store_legacy_stale_and_revise(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    write_review(cfg, spec, "SPEC-0001", verdict="revise")           # SPEC-0279: amended first
    r = _add(cfg, monkeypatch)
    assert r.exit_code == 0 and json.loads(r.stdout)["review"] == "revise_pending"
    write_review(cfg, spec, "SPEC-0001")                              # stored approve
    assert _add(cfg, monkeypatch).exit_code == 0
    # a stored review for an older text does not count for the new text
    spec.write_text(spec.read_text() + "\nedited\n")
    r = _add(cfg, monkeypatch)
    assert r.exit_code != 0 and "edited after its approving review" in r.output
    # a legacy sidecar with a matching sha does
    side = SR.sidecar_path(spec, "SPEC-0001")
    side.parent.mkdir(parents=True)
    side.write_text(json.dumps({"verdict": "approve", "spec_sha256": SR.spec_sha256(spec)}))
    assert _add(cfg, monkeypatch).exit_code == 0


def test_legacy_sidecar_stale_or_not_approving_refuses(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    side = SR.sidecar_path(spec, "SPEC-0001")
    side.parent.mkdir(parents=True)
    side.write_text(json.dumps({"verdict": "approve", "spec_sha256": "0" * 64}))
    assert _add(cfg, monkeypatch).exit_code != 0
    side.write_text(json.dumps({"verdict": "revise", "spec_sha256": SR.spec_sha256(spec)}))
    assert _add(cfg, monkeypatch).exit_code != 0


def test_review_path_in_second_repo_uses_that_root_and_schema(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_entry()])
    other, spec = _second_repo(tmp_path)
    spy = Spy()
    monkeypatch.setattr(SR, "select_runner", lambda cfg: spy)
    with pytest.raises(ValueError):
        SR.run_spec_review(cfg, spec, "SPEC-0007")
    req = spy.seen[0]
    assert req.cwd == other.resolve() and req.add_dirs == (other.resolve(),)
    assert "OTHER-REPO-SCHEMA-MARKER" in req.prompt
    monkeypatch.undo()
    r = run_cli(cfg, monkeypatch, ["spec", "review", "--path", str(spec), "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["repo_root"] == str(other.resolve()) and out["spec_id"] == "SPEC-0007"
    assert SR.load_review(cfg, "SPEC-0007", SR.spec_sha256(spec)) == {k: v for k, v in out.items() if k != "profile"}
    assert not (other / "docs" / "specs" / "reviews").exists()


def test_nested_spec_uses_git_toplevel_not_grandparent_or_meta_tools_root(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_entry()])
    other = tmp_path / "other"
    nested = other / "sub" / "docs" / "specs"
    nested.mkdir(parents=True)
    (other / "docs" / "specs").mkdir(parents=True)
    (other / "docs" / "specs" / "SCHEMA.md").write_text("OTHER-TOPLEVEL-SCHEMA-MARKER\n")
    (nested / "SCHEMA.md").write_text("GRANDPARENT-SCHEMA-MARKER\n")
    spec = nested / "0007-thing.md"
    spec.write_text(SPEC_ACCEPTED.format(id="SPEC-0007", extra=""))
    subprocess.run(["git", "-C", str(other), "init", "-q", "-b", "main"], check=True)
    assert spec.parent.parent.parent != other  # the grandparent fallback would differ
    r = run_cli(cfg, monkeypatch, ["spec", "review", "--path", str(spec), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["repo_root"] == str(other.resolve())
    # a configured Meta-Tools root must not leak into a non-outbound spec's root
    monkeypatch.setattr(SR.P, "project_config", lambda cfg, name: {"_root": str(repo)})
    assert SR.spec_root(cfg, spec) == other.resolve()
    spy = Spy()
    monkeypatch.setattr(SR, "select_runner", lambda cfg: spy)
    with pytest.raises(ValueError):
        SR.run_spec_review(cfg, spec, "SPEC-0007")
    req = spy.seen[0]
    assert req.cwd == other.resolve() and req.add_dirs == (other.resolve(),)
    assert "OTHER-TOPLEVEL-SCHEMA-MARKER" in req.prompt
    assert "GRANDPARENT-SCHEMA-MARKER" not in req.prompt
    ends = [j for j in SR.read_journal(cfg) if j["kind"] == "end"]
    assert ends and ends[-1]["error"]


def test_spec_root_outside_git_falls_back_to_grandparent(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    other, spec = _second_repo(tmp_path, git=False)
    assert SR.spec_root(cfg, spec) == other.resolve()


def test_journal_start_end_distinct_run_ids_and_failure_ends(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_entry(), _entry(), _entry(MUST_FIX), _entry(exit_code=1)])
    monkeypatch.setattr(SR.store, "now_iso", lambda now=None: "2026-10-25T10:00:00+00:00")
    SR.review_spec(cfg, "SPEC-0001")
    SR.review_spec(cfg, "SPEC-0001")                                  # same second
    recs = SR.read_journal(cfg)
    assert [r["kind"] for r in recs] == ["start", "end", "start", "end"]
    assert recs[0]["run_id"] == recs[1]["run_id"] and recs[2]["run_id"] == recs[3]["run_id"]
    assert recs[0]["run_id"] != recs[2]["run_id"]
    assert all(r["schema"] == "wt.spec.review.run.v1" and r["spec_id"] == "SPEC-0001"
               for r in recs)
    assert recs[1]["verdict"] == "approve" and recs[1]["counts"] == {"nit": 1}
    assert recs[1]["error"] is None and recs[1]["cost_usd"] == 0.42
    for _ in range(2):                                                 # must_fix, runner error
        with pytest.raises(ValueError):
            SR.review_spec(cfg, "SPEC-0001")
    recs = SR.read_journal(cfg)
    assert [r["kind"] for r in recs[4:]] == ["start", "end", "start", "end"]
    ends = [r for r in recs[4:] if r["kind"] == "end"]
    assert all(e["error"] and e["verdict"] is None for e in ends)
    assert "must_fix" in ends[0]["error"]
    assert recs[4]["run_id"] == ends[0]["run_id"]


def test_read_journal_tail_and_since(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    p = SR.journal_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    base = dt.datetime(2026, 10, 25, 10, 0, 0, tzinfo=dt.timezone.utc)
    lines = [json.dumps({"kind": "start", "run_id": f"r{i}", "spec_id": "SPEC-1",
                         "ts": (base + dt.timedelta(minutes=i)).isoformat()}) for i in range(10)]
    p.write_text("\n".join(lines) + "\n{torn")
    assert [r["run_id"] for r in SR.read_journal(cfg, tail=3)] == ["r8", "r9"]   # torn line eats 1
    assert len(SR.read_journal(cfg)) == 10
    since = (base + dt.timedelta(minutes=7)).isoformat()
    assert [r["run_id"] for r in SR.read_journal(cfg, since_ts=since)] == ["r7", "r8", "r9"]
    assert [r["run_id"] for r in SR.read_journal(cfg, since_ts=since, tail=3)] == ["r8", "r9"]
    assert SR.read_journal({"data_dir": str(tmp_path / "nowhere")}) == []


def test_review_cli_usage_errors_and_missing_id(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [_entry()])
    r = run_cli(cfg, monkeypatch, ["spec", "review"])
    assert r.exit_code == 2 and "exactly one" in r.output
    r = run_cli(cfg, monkeypatch, ["spec", "review", "SPEC-0001", "--path", str(_spec(repo))])
    assert r.exit_code == 2 and "exactly one" in r.output
    noid = tmp_path / "noid.md"
    noid.write_text("---\ntitle: x\n---\nbody\n")
    r = run_cli(cfg, monkeypatch, ["spec", "review", "--path", str(noid)])
    assert r.exit_code != 0 and "noid.md" in r.output and "`id`" in r.output


def test_corrupt_store_file_refuses_admission_without_crash(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    p = SR.store_path(cfg, "SPEC-0001", SR.spec_sha256(_spec(repo)))
    p.parent.mkdir(parents=True)
    for junk in ("{not json", "[1, 2]", ""):
        p.write_text(junk)
        assert SR.load_review(cfg, "SPEC-0001", SR.spec_sha256(_spec(repo))) is None
        r = _add(cfg, monkeypatch)
        assert r.exit_code != 0 and "no spec review" in r.output
        assert not isinstance(r.exception, (KeyError, TypeError, AttributeError))


def test_outbound_spec_unloadable_project_falls_back_to_meta_tools_root(   # SPEC-0243: was the rule
        tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    outbox = tmp_path / "outbox" / "acme"
    outbox.mkdir(parents=True)
    spec = outbox / "ACME-0001-thing.md"
    spec.write_text(SPEC_ACCEPTED.format(id="ACME-0001", extra=""))
    def project_config(cfg, name):
        if name != "Meta-Tools":
            raise ValueError(f"no project {name}")
        return {"_root": str(repo)}
    monkeypatch.setattr(SR.P, "project_config", project_config)
    assert SR.spec_root(cfg, spec) == repo
    spy = Spy()
    monkeypatch.setattr(SR, "select_runner", lambda cfg: spy)
    (repo / "docs" / "specs" / "SCHEMA.md").write_text("META-SCHEMA-MARKER\n")
    with pytest.raises(ValueError):
        SR.review_spec(cfg, "ACME-0001")
    assert spy.seen[0].cwd == repo and "META-SCHEMA-MARKER" in spy.seen[0].prompt


def test_review_context_keys_and_payload_rejects_must_fix(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    ctx = SR.review_context(_spec(repo), "SPEC-0001", idea_text="idea", root=repo)
    assert set(ctx) == {"spec_id", "spec_rel_path", "spec_text", "schema_text", "idea_text"}
    assert ctx["spec_rel_path"] == "docs/specs/0001-trivial.md" and ctx["idea_text"] == "idea"
    with pytest.raises(ValueError, match="must_fix"):
        SR.review_payload("SPEC-0001", "abc", model="m", cost_usd=0.1, verdict=MUST_FIX, root=repo)
    p = SR.review_payload("SPEC-0001", "abc", model="m", cost_usd=0.1, verdict=APPROVE, root=repo)
    assert p["schema"] == "wt.spec.review.v1" and p["repo_root"] == str(repo)


# ---- SPEC-0230: the latest review governs admission ----------------------------------------------

OLD, OLDER = "a" * 64, "b" * 64


def _store(cfg, spec, verdict, at, sha=None, findings=None):
    return write_review(cfg, spec, "SPEC-0001", verdict, sha=sha, findings=findings,
                        reviewed_at=f"2026-10-25T00:00:{at:02d}+00:00")


def test_list_and_latest_review_order_and_skip_junk(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    assert SR.list_reviews(cfg, "SPEC-0001") == [] and SR.latest_review(cfg, "SPEC-0001") is None
    _store(cfg, spec, "revise", 20, sha=OLD)
    _store(cfg, spec, "split", 5, sha=OLDER)
    _store(cfg, spec, "approve", 30)
    d = SR.store_path(cfg, "SPEC-0001", OLD).parent
    (d / f"{'c' * 64}.json").write_text("{not json")
    (d / "d.json").write_text("[1]")
    (d / f"{'e' * 64}.json.xyz.tmp").write_text(json.dumps({"reviewed_at": "2099", "verdict": "x"}))
    got = SR.list_reviews(cfg, "SPEC-0001")
    assert [r["verdict"] for r in got] == ["split", "revise", "approve"]
    assert SR.latest_review(cfg, "SPEC-0001")["verdict"] == "approve"


def test_check_admissible_cases(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    sha = SR.spec_sha256(spec)
    with pytest.raises(ValueError, match="SPEC-0001 has no spec review; run wt spec review"):
        SR.check_admissible(cfg, spec, "SPEC-0001")
    _store(cfg, spec, "split", 1, sha=OLD)                       # split, earlier sha
    with pytest.raises(ValueError, match=r"recommends a split \(aaaaaaaaaaaa\); split the spec"):
        SR.check_admissible(cfg, spec, "SPEC-0001")
    _store(cfg, spec, "revise", 2, sha=OLDER)                    # older revise, then the latest
    got = SR.check_admissible(cfg, spec, "SPEC-0001")
    assert got["admission"] == "after_revision" and got["spec_sha256"] == OLDER
    _store(cfg, spec, "revise", 3, sha=sha)                      # revise for the current text
    got = SR.check_admissible(cfg, spec, "SPEC-0001")                # SPEC-0279
    assert got["admission"] == "revise_pending" and got["spec_sha256"] == sha
    assert got["current_sha"] == sha
    _store(cfg, spec, "split", 4, sha=sha)                       # split, current sha
    with pytest.raises(ValueError, match="recommends a split"):
        SR.check_admissible(cfg, spec, "SPEC-0001")
    _store(cfg, spec, "approve", 5, sha=OLD)                     # approve for an earlier sha
    with pytest.raises(ValueError, match=r"edited after its approving review \(aaaaaaaaaaaa\); "
                                         r"run wt spec review SPEC-0001 --force"):
        SR.check_admissible(cfg, spec, "SPEC-0001")
    _store(cfg, spec, "approve", 6)                              # approve for the current sha
    got = SR.check_admissible(cfg, spec, "SPEC-0001")
    assert got["admission"] == "approved" and got["spec_sha256"] == sha


def test_check_admissible_legacy_approve_and_corrupt_file(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    side = SR.sidecar_path(spec, "SPEC-0001")
    side.parent.mkdir(parents=True)
    side.write_text(json.dumps({"verdict": "approve", "spec_sha256": SR.spec_sha256(spec)}))
    assert SR.check_admissible(cfg, spec, "SPEC-0001")["admission"] == "approved"
    side.unlink()
    p = SR.store_path(cfg, "SPEC-0001", OLD)
    p.parent.mkdir(parents=True)
    p.write_text("{corrupt")
    with pytest.raises(ValueError, match="no spec review"):
        SR.check_admissible(cfg, spec, "SPEC-0001")
    _store(cfg, spec, "revise", 1, sha=OLDER)
    assert SR.check_admissible(cfg, spec, "SPEC-0001")["admission"] == "after_revision"


def test_check_admissible_after_several_revises_latest_governs(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    _store(cfg, spec, "revise", 1, sha=OLDER)
    _store(cfg, spec, "revise", 2, sha=OLD)
    got = SR.check_admissible(cfg, spec, "SPEC-0001")
    assert got["admission"] == "after_revision" and got["spec_sha256"] == OLD
    assert len(SR.list_reviews(cfg, "SPEC-0001")) == 2


def test_check_admissible_current_approve_wins_over_later_review(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    _store(cfg, spec, "approve", 1)                              # approve for the current sha
    _store(cfg, spec, "split", 2, sha=OLD)                       # a later review for another sha
    got = SR.check_admissible(cfg, spec, "SPEC-0001")
    assert got["admission"] == "approved" and got["spec_sha256"] == SR.spec_sha256(spec)
    _store(cfg, spec, "revise", 3, sha=OLDER)
    assert SR.check_admissible(cfg, spec, "SPEC-0001")["admission"] == "approved"


def _foreign(cfg, spec, verdict, at, root, sha=None):
    """A review stored under the same spec id but recorded for another repository."""
    p = _store(cfg, spec, verdict, at, sha=sha)
    data = json.loads(p.read_text())
    data["repo_root"] = str(root)
    p.write_text(json.dumps(data))


def test_foreign_repo_reviews_neither_admit_nor_block(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    other, _ = _second_repo(tmp_path)
    _foreign(cfg, spec, "revise", 1, other, sha=OLD)
    assert SR.latest_review(cfg, "SPEC-0001") is not None            # unfiltered lookup sees it
    assert SR.list_reviews(cfg, "SPEC-0001", spec) == []
    with pytest.raises(ValueError, match="has no spec review"):
        SR.check_admissible(cfg, spec, "SPEC-0001")
    # a local review still governs, whatever the foreign one says
    _foreign(cfg, spec, "split", 9, other, sha=OLDER)
    _foreign(cfg, spec, "revise", 2, repo, sha=OLD)
    got = SR.check_admissible(cfg, spec, "SPEC-0001")
    assert got["admission"] == "after_revision" and got["spec_sha256"] == OLD
    assert got["current_sha"] == SR.spec_sha256(spec)


def test_linked_worktree_reviews_count_as_same_repo(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    wt = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    _foreign(cfg, spec, "revise", 1, wt, sha=OLD)                  # reviewed from the worktree
    assert SR.check_admissible(cfg, spec, "SPEC-0001")["admission"] == "after_revision"


def test_review_from_removed_worktree_or_moved_checkout_still_counts(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    wt = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)], check=True)
    _foreign(cfg, spec, "revise", 1, wt, sha=OLD)                  # reviewed from the worktree
    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True)
    assert not wt.exists()
    spec.write_text(spec.read_text() + "\nrevised\n")              # the revision was applied
    assert SR.check_admissible(cfg, spec, "SPEC-0001")["admission"] == "after_revision"
    assert SR.latest_review(cfg, "SPEC-0001", spec)["spec_sha256"] == OLD
    # a root that never existed (moved checkout) matches too; a foreign existing repo does not
    _foreign(cfg, spec, "split", 2, tmp_path / "gone", sha=OLDER)
    assert SR.latest_review(cfg, "SPEC-0001", spec)["verdict"] == "split"
    other, _ = _second_repo(tmp_path)
    _foreign(cfg, spec, "approve", 3, other, sha="c" * 64)
    assert SR.latest_review(cfg, "SPEC-0001", spec)["verdict"] == "split"


def test_reviews_sort_by_parsed_time_not_string(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    write_review(cfg, spec, "SPEC-0001", "split", sha=OLD, reviewed_at="2026-10-25T10:00:00+00:00")
    write_review(cfg, spec, "SPEC-0001", "revise", sha=OLDER,
                 reviewed_at="2026-10-25T09:30:00-03:00")           # = 12:30Z, later
    write_review(cfg, spec, "SPEC-0001", "approve", sha="c" * 64, reviewed_at="garbage")
    assert [r["verdict"] for r in SR.list_reviews(cfg, "SPEC-0001")] == ["approve", "split", "revise"]


def test_check_admissible_revise_pending_internal(tmp_path):         # SPEC-0279
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    sha = SR.spec_sha256(spec)
    _store(cfg, spec, "revise", 1, sha=sha)
    got = SR.check_admissible(cfg, spec, "SPEC-0001")
    assert got["admission"] == "revise_pending" and got["current_sha"] == sha
    assert got["spec_sha256"] == sha and got["verdict"] == "revise"


def test_check_admissible_revise_unapplied_outbound_still_refused(tmp_path):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    write_review(cfg, spec, "PROJ-0001", "revise")
    with pytest.raises(ValueError, match=r"PROJ-0001: .*not applied yet \(the text is unchanged"):
        SR.check_admissible(cfg, spec, "PROJ-0001")
