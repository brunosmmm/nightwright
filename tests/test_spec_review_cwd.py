"""SPEC-0227: `wt spec review SPEC-ID` reviews the copy in the current checkout (main or a linked
worktree) of the registered specs repository; elsewhere, the registered checkout's copy."""
import json
import subprocess
from pathlib import Path

import pytest

from test_agentic_e2e import SPEC_ACCEPTED, _git, make_cfg, make_repo, run_cli, script
from test_spec_review_store import APPROVE, Spy, _entry
from wt.agentic import spec_review as SR
from wt.agentic.runner import FakeRunner

SPEC_REL = "docs/specs/0001-trivial.md"


def _setup(tmp_path, monkeypatch, entries=None):
    repo = make_repo(tmp_path, review=False)
    sp = script(tmp_path, entries if entries is not None else [_entry(), _entry()])
    cfg = make_cfg(tmp_path, repo, sp)
    spy = Spy(FakeRunner(sp))
    monkeypatch.setattr(SR, "select_runner", lambda cfg: spy)
    return repo, cfg, spy


def _worktree(tmp_path, repo, branch="rev", edit="\nrevised\n"):
    wt2 = tmp_path / "wt2"
    _git(repo, "worktree", "add", "-q", str(wt2), "-b", branch)
    if edit:
        spec = wt2 / SPEC_REL
        spec.write_text(spec.read_text() + edit)
    return wt2


def _cursor(cfg):
    return Path(cfg["data_dir"]) / "agentic" / "spec-reviews" / FakeRunner.CURSOR_NAME


def _review(cfg, monkeypatch, *args):
    return run_cli(cfg, monkeypatch, ["spec", "review", *args])


def test_worktree_copy_is_reviewed(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    wt2 = _worktree(tmp_path, repo)
    monkeypatch.chdir(wt2)
    r = _review(cfg, monkeypatch, "SPEC-0001", "--json")
    assert r.exit_code == 0, r.output
    spec = wt2 / SPEC_REL
    out = json.loads(r.stdout)
    assert out["spec_sha256"] == SR.spec_sha256(spec) != SR.spec_sha256(repo / SPEC_REL)
    assert out["origin"] == "worktree rev" and out["spec_path"] == str(spec.resolve())
    assert SR.load_review(cfg, "SPEC-0001", out["spec_sha256"]) == {k: v for k, v in out.items() if k != "profile"}
    assert spy.seen[0].cwd == wt2.resolve()


def test_main_checkout_copy(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    _worktree(tmp_path, repo)
    monkeypatch.chdir(repo)
    r = _review(cfg, monkeypatch, "SPEC-0001", "--json")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["origin"] == "main checkout"
    assert out["spec_sha256"] == SR.spec_sha256(repo / SPEC_REL)
    assert spy.seen[0].cwd == repo.resolve()


def test_outside_repo_and_other_repo_fall_back(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    sha = SR.spec_sha256(repo / SPEC_REL)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], check=True)
    (clone / SPEC_REL).write_text((clone / SPEC_REL).read_text() + "\nclone edit\n")
    for cwd in (elsewhere, clone):
        monkeypatch.chdir(cwd)
        r = _review(cfg, monkeypatch, "SPEC-0001", "--json", "--force")   # SPEC-0230: 2nd round
        assert r.exit_code == 0, r.output
        out = json.loads(r.stdout)
        assert out["origin"] == "registered checkout" and out["spec_sha256"] == sha
        assert out["spec_path"] == str((repo / SPEC_REL).resolve())


def test_missing_in_worktree_errors_no_fallback(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    wt2 = _worktree(tmp_path, repo, edit="")
    (wt2 / SPEC_REL).unlink()
    monkeypatch.chdir(wt2)
    r = _review(cfg, monkeypatch, "SPEC-0001")
    assert r.exit_code != 0
    assert str(wt2.resolve()) in r.output and str((repo / SPEC_REL).resolve()) in r.output
    assert "--path" in r.output
    assert spy.seen == [] and not _cursor(cfg).exists()


def test_reviewed_line_stdout_and_json_stderr(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    wt2 = _worktree(tmp_path, repo)
    monkeypatch.chdir(wt2)
    sha12 = SR.spec_sha256(wt2 / SPEC_REL)[:12]
    line = f"reviewing {SPEC_REL} @ {sha12} (worktree rev)  {wt2.resolve()}"
    r = _review(cfg, monkeypatch, "SPEC-0001")
    assert r.exit_code == 0, r.output
    assert r.stdout.splitlines()[0] == line
    r = _review(cfg, monkeypatch, "SPEC-0001", "--json", "--force")    # SPEC-0230: 2nd round
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["spec_path"] and line in r.stderr and line not in r.stdout


def test_path_option_prints_origin_path(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    spec = repo / SPEC_REL
    r = _review(cfg, monkeypatch, "--path", str(spec))
    assert r.exit_code == 0, r.output
    assert r.stdout.splitlines()[0] == (
        f"reviewing {SPEC_REL} @ {SR.spec_sha256(spec)[:12]} (path)  {repo.resolve()}")
    r = _review(cfg, monkeypatch, "--path", str(spec), "--json", "--force")   # SPEC-0230
    assert json.loads(r.stdout)["origin"] == "path" and "(path)" in r.stderr


def test_outbound_id_unaffected(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    box = Path(cfg["outbox_dir"]) / "Proj"
    box.mkdir(parents=True)
    out_spec = box / "PROJ-0001-thing.md"
    out_spec.write_text(SPEC_ACCEPTED.format(id="PROJ-0001", extra=""))
    from wt import specs as SP
    wt2 = _worktree(tmp_path, repo)
    monkeypatch.chdir(wt2)
    assert SP.resolve_spec_path_for_cwd(cfg, "PROJ-0001") == (out_spec, "outbox")
    with pytest.raises(ValueError):
        SP.resolve_spec_path_for_cwd(cfg, "PROJ-0002")


def test_specs_dir_in_tmp_repo_rule_is_relative_to_that_repo(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    wt2 = _worktree(tmp_path, repo)
    from wt import specs as SP
    path, origin = SP.resolve_spec_path_for_cwd(cfg, "SPEC-0001", cwd=wt2 / "docs")
    assert path == wt2.resolve() / SPEC_REL and origin == "worktree rev"


def test_other_spec_commands_unchanged(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    wt2 = _worktree(tmp_path, repo, edit="")
    (wt2 / SPEC_REL).write_text((wt2 / SPEC_REL).read_text().replace(
        "status: accepted", "status: draft"))
    monkeypatch.chdir(wt2)
    r = run_cli(cfg, monkeypatch, ["spec", "status", "SPEC-0001", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["status"] == "accepted" and out["path"] == str((repo / SPEC_REL))


def test_detached_head_origin(tmp_path, monkeypatch):
    repo, cfg, spy = _setup(tmp_path, monkeypatch)
    wt2 = tmp_path / "wt2"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt2))
    monkeypatch.chdir(wt2)
    r = _review(cfg, monkeypatch, "SPEC-0001", "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["origin"] == "worktree detached"
