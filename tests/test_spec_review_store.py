"""SPEC-0210: the data-dir review store, any-repo reviews, the standalone review journal and the
legacy-sidecar fallback of the `wt queue add` gate."""
import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from test_agentic_e2e import SPEC_ACCEPTED, make_cfg, make_repo, run_cli, script, write_review
from wt.agentic import spec_review as SR

APPROVE = {"verdict": "approve", "findings": [{"severity": "nit", "section": "Context",
                                               "title": "wording"}], "suggested_splits": []}
MUST_FIX = {"verdict": "approve", "findings": [{"severity": "must_fix", "title": "t"}]}


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
    assert stored == json.loads(r.output)
    assert stored["repo_root"] == str(repo.resolve()) and stored["spec_sha256"] == sha
    assert not (repo / "docs" / "specs" / "reviews").exists()
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                          text=True).stdout.strip() == ""


def test_queue_add_gate_store_legacy_stale_and_revise(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    spec = _spec(repo)
    write_review(cfg, spec, "SPEC-0001", verdict="revise")
    r = _add(cfg, monkeypatch)
    assert r.exit_code != 0 and "no approving review" in r.output
    write_review(cfg, spec, "SPEC-0001")                              # stored approve
    assert _add(cfg, monkeypatch).exit_code == 0
    # a stored review for an older text does not count for the new text
    spec.write_text(spec.read_text() + "\nedited\n")
    r = _add(cfg, monkeypatch)
    assert r.exit_code != 0 and "no approving review" in r.output
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
    out = json.loads(r.output)
    assert out["repo_root"] == str(other.resolve()) and out["spec_id"] == "SPEC-0007"
    assert SR.load_review(cfg, "SPEC-0007", SR.spec_sha256(spec)) == out
    assert not (other / "docs" / "specs" / "reviews").exists()


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
        assert r.exit_code != 0 and "no approving review for its current text" in r.output
        assert not isinstance(r.exception, (KeyError, TypeError, AttributeError))


def test_outbound_spec_uses_meta_tools_root(tmp_path, monkeypatch):
    repo, cfg = _setup(tmp_path, [])
    outbox = tmp_path / "outbox" / "acme"
    outbox.mkdir(parents=True)
    spec = outbox / "ACME-0001-thing.md"
    spec.write_text(SPEC_ACCEPTED.format(id="ACME-0001", extra=""))
    monkeypatch.setattr(SR.P, "project_config", lambda cfg, name: {"_root": str(repo)})
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
