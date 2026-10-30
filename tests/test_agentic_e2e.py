"""SPEC-0176 end-to-end: the whole loop offline. A temp repo is both the project and (for the
internal case) the `wt_checkout`; gates are `sh` scripts; personas are the FakeRunner; `gh`
is a fake script on PATH. Nothing touches the real data dir, the real `claude` or `gh`."""
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.runner import FakeRunner
from wt.cli import cli

ENV = {
    "GIT_AUTHOR_NAME": "wt test", "GIT_AUTHOR_EMAIL": "wt@test",
    "GIT_COMMITTER_NAME": "wt test", "GIT_COMMITTER_EMAIL": "wt@test",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

SPEC_ACCEPTED = """---
id: {id}
title: "Trivial feature"
status: accepted
owner: t
created: 2026-10-25
{extra}---

## Context

c

## Decision

Ship `feature.txt`.

## Acceptance criteria

- [ ] feature.txt exists

## Test plan

- run the gates

## Definition of done

- [ ] done
"""

SPEC_DONE = SPEC_ACCEPTED.replace("status: accepted", "status: done").replace("- [ ]", "- [x]")

PROJECT_YAML = """schema: wt.project.v1
base: main
gates:
  - {{name: test, cmd: 'sh gates/test.sh', timeout_s: 30, required: true}}
  - {{name: lint, cmd: 'sh gates/lint.sh', timeout_s: 30, required: {lint_required}}}
merge:
  policy: {policy}
  base: main
  remote: origin
worktrees:
  setup: ["touch .setup-ran"]
agents:
  commit_trailer: "Co-Authored-By: wt test <wt@test>"
"""


@pytest.fixture(autouse=True)
def _git_env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("WT_AGENTIC_RUNNER", raising=False)
    monkeypatch.delenv("WT_AGENTIC_FAKE_SCRIPT", raising=False)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _write(repo, rel, text):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def write_review(cfg, spec_path, spec_id, verdict="approve", *, sha=None, findings=None,
                 reviewed_at="2026-10-25T00:00:00+00:00", **extra):
    """SPEC-0188/0210: the stored review `wt queue add` demands, keyed by the spec's sha."""
    from wt.agentic import spec_review as SR
    return SR.save_review(cfg, {
        "schema": SR.SCHEMA, "spec_id": spec_id, "verdict": verdict,
        "spec_sha256": sha or SR.spec_sha256(spec_path),
        "reviewed_at": reviewed_at, "model": "sonnet",
        "cost_usd": 0.0, "findings": findings or [], "suggested_splits": [], "repo_root": "", **extra})


def make_repo(tmp_path, *, spec_id="SPEC-0001", policy="merge", lint_required="true",
              test_gate="test -f feature.txt", lint_gate="true", with_origin=False, extra_fm="",
              spec_rel="docs/specs/0001-trivial.md", review=True):
    """A git repo that doubles as a wt checkout (src/wt + pyproject) with an accepted spec."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "wt").mkdir(parents=True)
    _write(repo, "pyproject.toml", "[project]\nname='wt'\n")
    spec_path = _write(repo, spec_rel, SPEC_ACCEPTED.format(id=spec_id, extra=extra_fm))
    if review:
        write_review({"data_dir": str(tmp_path / "data")}, spec_path, spec_id)
    _write(repo, "gates/test.sh", f"{test_gate}\n")
    _write(repo, "gates/lint.sh", f"{lint_gate}\n")
    _write(repo, ".wt/project.yaml", PROJECT_YAML.format(policy=policy,
                                                          lint_required=lint_required))
    _write(repo, ".gitignore", ".setup-ran\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    if with_origin:
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        _git(repo, "remote", "add", "origin", str(origin))
        _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def fake_gh(tmp_path, monkeypatch, *, fail=False):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    gh = d / "gh"
    log = tmp_path / "gh.log"
    body = ("#!/bin/sh\necho \"$@\" >> " + str(log) + "\n"
            + ("echo nope >&2\nexit 1\n" if fail else "echo https://example.test/pr/7\n"))
    gh.write_text(body)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{d}:{os.environ['PATH']}")
    return log


def script(tmp_path, entries, name="script.json"):
    p = tmp_path / name
    p.write_text(json.dumps(entries))
    return p


def impl_done(spec_id="SPEC-0001", content="hello\n", extra="", spec_rel="docs/specs/0001-trivial.md",
              **more):
    """FakeRunner entry: the implementer writes the feature and sets the spec done."""
    return {"persona": "implementer", "edits": [
        {"path": "feature.txt", "write": content},
        {"path": spec_rel, "write": SPEC_DONE.format(id=spec_id, extra=extra)}], **more,
        "verdict": {"status": "done", "summary": "wrote feature.txt", "changed_files": ["feature.txt"],
                    "tests_run": ["sh gates/test.sh"], "ac_status": [], "blockers": [],
                    "followup_ideas": []}, "cost_usd": 0.5}


def triage(disposition="fix", **kw):
    v = {"disposition": disposition, "confidence": "high", "suspected_flaky": False,
         "must_fix": [{"gate": "test", "title": "feature missing", "detail": "write it",
                       "files": ["feature.txt"]}] if disposition == "fix" else [],
         "waived": [], "fix_brief": "write feature.txt", "followup_ideas": []}
    v.update(kw)
    return {"persona": "gate_triager", "verdict": v, "cost_usd": 0.1}


def review(verdict="pass", **kw):
    """FakeRunner entry: the conformance reviewer (SPEC-0187). `fix` carries one must_fix finding."""
    fix = verdict == "fix"
    v = {"verdict": verdict, "score": 40 if fix else 95,
         "findings": [{"severity": "must_fix", "ac": "feature.txt exists",
                       "title": "feature.txt is wrong", "detail": "write the right text",
                       "suggested_fix": "write the right text into feature.txt",
                       "files": ["feature.txt"]}] if fix else [],
         "ac_assessment": [{"ac": "feature.txt exists", "met": not fix, "evidence": "feature.txt"}],
         "fix_brief": "rewrite feature.txt" if fix else ""}
    v.update(kw)
    return {"persona": "conformance_reviewer", "verdict": v, "cost_usd": 0.2}


def tr_review(verdict="pass", **kw):
    """FakeRunner entry: the test reviewer (SPEC-0205). `fix` carries one must_fix finding."""
    fix = verdict == "fix"
    v = {"verdict": verdict, "score": 30 if fix else 90,
         "findings": [{"severity": "must_fix", "ac": "feature.txt exists",
                       "title": "no test for feature.txt", "detail": "add one",
                       "files": ["tests/test_feature.py"]}] if fix else [],
         "coverage_assessment": [{"ac": "feature.txt exists", "covered": not fix,
                                  "tests": [] if fix else ["tests/test_feature.py::test_exists"]}],
         "fix_brief": "add a test for feature.txt" if fix else ""}
    v.update(kw)
    return {"persona": "test_reviewer", "verdict": v, "cost_usd": 0.2}


def make_cfg(tmp_path, repo, script_path, *, profile=None, outbox=None):
    org = tmp_path / "org"
    org.mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "cfg").mkdir(exist_ok=True)
    cfg = {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "cfg"),
        "org_capture_file": str(org / "inbox.org"), "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(repo / "docs" / "specs"),
        "wt_checkout": str(repo),
        "outbox_dir": str(tmp_path / "outbox"),
        "outbox_targets": outbox or {},
        "agentic": {"runner": "fake", "fake_script": str(script_path), "default_profile": "balanced",
                    "poll_interval_s": 0.01, "lease_ttl_s": 120,
                    "profiles": {"balanced": {"reviewers": ["conformance_reviewer"], **(profile or {})}}},
    }
    return cfg


def run_cli(cfg, monkeypatch, args):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args)


def added(cfg, monkeypatch, spec_id="SPEC-0001", *extra):
    r = run_cli(cfg, monkeypatch, ["queue", "add", spec_id, "--yes", "--json", *extra])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.add.v1" and d["written"] is True
    return d["campaign"]


def ledger_types(cfg, cid):
    return [e["type"] for e in S.read_ledger(cfg, cid)]


# ---- internal happy path ----------------------------------------------------------------------

def test_e2e_internal_merges(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    st = S.read_state(cfg, cid)
    assert st["state"] == "QUEUED" and st["spec"]["rel_path"] == "docs/specs/0001-trivial.md"

    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle", "--json-lines"])
    assert r.exit_code == 0, r.output
    ticks = [json.loads(line) for line in r.output.splitlines() if line.startswith("{")]
    assert ticks and ticks[0]["schema"] == "wt.queue.tick.v1"

    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert st["close"]["mode"] == "merge" and st["close"]["merged_sha"] == _git(repo, "rev-parse", "HEAD")
    assert (repo / "feature.txt").read_text() == "hello\n"
    assert not Path(st["repo"]["worktree"]).exists()
    assert st["counters"] == {"loops": 0, "runs": 2, "gate_runs": 2, "gate_retries": 0,
                              "triage_retries": 0, "blocked_streak": 0, "close_attempts": 1,
                              "parks": 0, "review_retries": 0}
    assert st["cost"]["total_usd"] == 0.7
    assert st["cost"]["by_persona"] == {"implementer": 0.5, "conformance_reviewer": 0.2}
    assert st["last_review"]["verdict"] == "pass" and st["last_review"]["score"] == 100
    types = ledger_types(cfg, cid)
    for t in ("open", "transition", "run.start", "run.end", "commit", "gate", "cost", "close"):
        assert t in types, (t, types)
    assert types.count("gate") == 2
    trans = [(e["from"], e["to"]) for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert trans == [("QUEUED", "OPENING"), ("OPENING", "IMPLEMENTING"), ("IMPLEMENTING", "GATING"),
                     ("GATING", "REVIEWING"), ("REVIEWING", "CLOSING"), ("CLOSING", "MERGED")]
    # the runner committed with the trailer; the setup command ran in the worktree
    assert "Co-Authored-By: wt test" in _git(repo, "log", "-1", "--format=%B")
    assert "wt(" in _git(repo, "log", "-1", "--format=%s")
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.v1" and d["items"] == [] and d["lease"]["held"] is False


def test_e2e_dependent_spec_admitted_only_after_dependency_merges(tmp_path, monkeypatch):
    """SPEC-0180: queue X and Y (Y depends on X) together; Y opens only once X is done on main."""
    repo = make_repo(tmp_path)
    spec2 = "docs/specs/0002-second.md"
    write_review(make_cfg(tmp_path, repo, None),
                 _write(repo, spec2, SPEC_ACCEPTED.format(id="SPEC-0002",
                                                          extra="depends_on: [SPEC-0001]\n")),
                 "SPEC-0002")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "second spec")
    # the fake script cursor is per campaign, so each spec gets its own script
    runners = {"SPEC-0001": FakeRunner(script(tmp_path, [impl_done(), review()], "x.json")),
               "SPEC-0002": FakeRunner(script(tmp_path, [
                   impl_done("SPEC-0002", "second\n", extra="depends_on: [SPEC-0001]\n",
                             spec_rel=spec2), review()], "y.json"))}

    class ByCampaign:
        def run(self, req):
            return next(r for k, r in runners.items() if k in str(req.cwd)).run(req)

    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    y = added(cfg, monkeypatch, "SPEC-0002", "--priority", "1")      # higher priority, but blocked
    x = added(cfg, monkeypatch, "SPEC-0001")

    tick = lambda: Q.tick(cfg, runner=ByCampaign())  # noqa: E731
    report = tick()                              # only X is admitted; Y waits on it
    assert report.admitted == 1
    assert S.read_state(cfg, x)["state"] == "IMPLEMENTING"
    assert S.read_state(cfg, y)["state"] == "QUEUED"
    for _ in range(3):                           # X implements, gates, is reviewed; Y waits
        tick()
        assert S.read_state(cfg, y)["state"] == "QUEUED"
    assert S.read_state(cfg, x)["state"] == "CLOSING"
    assert S.read_ledger(cfg, y, types=["backlog"])[-1]["blocked_by"] == [
        "waiting on SPEC-0001 (accepted)"]

    tick()                                       # X merges; the base now has SPEC-0001 done
    assert S.read_state(cfg, x)["state"] == "MERGED"
    assert S.read_state(cfg, y)["state"] == "QUEUED"
    report = tick()                              # first tick after: Y is admitted
    assert report.admitted == 1 and S.read_state(cfg, y)["state"] == "IMPLEMENTING"
    assert S.read_ledger(cfg, y, types=["backlog"])[-1]["ready"] is True
    while not tick().idle:
        pass
    assert S.read_state(cfg, y)["state"] == "MERGED"
    assert (repo / "feature.txt").read_text() == "second\n"


# ---- outbound: policy pr → HANDED_OFF -----------------------------------------------------------

def test_e2e_outbound_pr_hands_off(tmp_path, monkeypatch):
    rel = "docs/specs/ACME-0001-trivial.md"
    extra = "target_project: acme\n"
    repo = make_repo(tmp_path, spec_id="ACME-0001", policy="pr", with_origin=True, spec_rel=rel,
                     extra_fm=extra)
    gh_log = fake_gh(tmp_path, monkeypatch)
    outbox = tmp_path / "outbox" / "acme"
    outbox.mkdir(parents=True)
    (outbox / "ACME-0001-trivial.md").write_text(SPEC_ACCEPTED.format(
        id="ACME-0001", extra=f"target_project: acme\ntarget_repo: {repo}\n"
                              f"target_spec_path: {repo / rel}\n"))
    write_review({"data_dir": str(tmp_path / "data")}, outbox / "ACME-0001-trivial.md", "ACME-0001")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done("ACME-0001", spec_rel=rel, extra=extra), review()]),
                   outbox={"acme": {"repo_path": str(repo)}})
    r = run_cli(cfg, monkeypatch, ["queue", "add", "ACME-0001", "--json"])
    assert r.exit_code == 0, r.output
    plan = json.loads(r.output)
    assert plan["project"] == "acme" and plan["written"] is False and plan["internal"] is False
    assert not list(S.campaigns_dir(cfg).glob("*")) if S.campaigns_dir(cfg).exists() else True
    cid = added(cfg, monkeypatch, "ACME-0001")
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "HANDED_OFF", st
    assert st["close"]["pr_url"] == "https://example.test/pr/7"
    assert "--draft" not in gh_log.read_text() and "--base main" in gh_log.read_text()
    # branch pushed to origin; main untouched
    assert _git(repo, "ls-remote", "--heads", "origin", st["repo"]["branch"])
    assert not (repo / "feature.txt").exists()


# ---- park variants ---------------------------------------------------------------------------

def test_e2e_loop_exhausted(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, test_gate="exit 1")
    entries = [impl_done(), triage("fix"), impl_done(), triage("fix")]
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries), profile={"loop_max": 1})
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "loop_exhausted", st
    assert st["park"]["resume_to"] == "IMPLEMENTING"
    assert st["counters"]["loops"] == 1 and st["counters"]["runs"] == 4
    assert st["pending_brief"] is None or st["pending_brief"]["source"] == "gate_triager"
    findings = S.read_ledger(cfg, cid, types=["finding"])
    assert any(f["severity"] == "must_fix" for f in findings)
    # cost: 2 implementer runs + 2 triager runs
    assert st["cost"]["total_usd"] == pytest.approx(1.2)
    # the second implementer saw the brief from the first triage
    prompts = sorted(S.runs_dir(cfg, cid).glob("*/prompt.md"))
    assert "write feature.txt" in prompts[2].read_text()
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    item = json.loads(r.output)["items"][0]
    assert item["state"] == "PARKED" and item["park"]["reason"] == "loop_exhausted"


def test_e2e_review_fix_then_pass_merges(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    entries = [impl_done(), review("fix"), impl_done(content="better\n"), review("pass")]
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert (repo / "feature.txt").read_text() == "better\n"
    assert st["counters"]["loops"] == 1 and st["counters"]["runs"] == 4
    assert st["last_review"]["verdict"] == "pass" and st["last_review"]["loop"] == 1
    runs = S.read_ledger(cfg, cid, types=["run.start"])
    assert [e["persona"] for e in runs] == ["implementer", "conformance_reviewer",
                                            "implementer", "conformance_reviewer"]
    findings = [f for f in S.read_ledger(cfg, cid, types=["finding"])
                if f["persona"] == "conformance_reviewer"]
    assert [(f["severity"], f["title"]) for f in findings] == [
        ("must_fix", "feature.txt is wrong")]
    assert "Fix: write the right text into feature.txt" in findings[0]["detail"]   # SPEC-0295
    trans = [(e["from"], e["to"], e["reason"]) for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert ("REVIEWING", "IMPLEMENTING", "review_fix") in trans
    assert ("REVIEWING", "CLOSING", "review_pass") in trans
    end = [e for e in S.read_ledger(cfg, cid, types=["run.end"])
           if e["persona"] == "conformance_reviewer"]
    assert [e["verdict"]["score"] for e in end] == [40, 95]
    # the second implementer saw the reviewer's brief
    prompt = (S.runs_dir(cfg, cid) / "003-implementer" / "prompt.md").read_text()
    assert "From: conformance_reviewer" in prompt and "rewrite feature.txt" in prompt
    assert "Fix: write the right text into feature.txt" in prompt


def _finding(sev, title, **kw):
    return dict({"severity": sev, "title": title, "detail": "d", "suggested_fix": "do f",
                 "files": ["feature.txt"]}, **kw)


def _triage_run(tmp_path, monkeypatch, entries, **kw):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries), **kw)
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    return cfg, cid, S.read_state(cfg, cid)


def test_e2e_triage_pass_with_minor_findings_merges(tmp_path, monkeypatch):      # SPEC-0203 (a)
    rv = review("pass", findings=[_finding("minor", "naming"), _finding("nit", "typo")])
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, [impl_done(), rv])
    assert st["state"] == "MERGED", st
    lr = st["last_review"]
    assert (lr["verdict"], lr["score"], lr["findings"]) == ("pass", 95, 2)
    assert [a["severity"] for a in lr["advisory"]] == ["minor", "nit"]
    notes = [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]
    assert "review score 95/80 pass" in notes


def test_e2e_triage_fix_over_minor_findings_needs_no_loop(tmp_path, monkeypatch):  # (b)
    rv = review("fix", findings=[_finding("minor", "naming")], fix_brief="rename it",
                ac_assessment=[{"ac": "feature.txt exists", "met": True}])
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, [impl_done(), rv])
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 0, st
    assert st["counters"]["runs"] == 2 and st["last_review"]["score"] == 96


def test_e2e_triage_must_fix_loops_once_then_passes(tmp_path, monkeypatch):      # (c)
    entries = [impl_done(), review("fix"), impl_done(content="better\n"), review("pass")]
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, entries)
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 1, st
    assert [e["reason"] for e in S.read_ledger(cfg, cid, types=["transition"])
            if e["from"] == "REVIEWING"].count("review_fix") == 1
    notes = [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]
    assert "review score 0/80 fix" in notes and "review score 100/80 pass" in notes


def test_e2e_triage_low_score_without_blocking_cause_loops(tmp_path, monkeypatch):  # (d)
    low = review("pass", findings=[_finding("minor", f"m{i}") for i in range(6)])
    entries = [impl_done(), low, impl_done(content="better\n"), review("pass")]
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, entries,
                               profile={"review": {"triager": False}})     # SPEC-0206 off
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 1, st
    prompt = (S.runs_dir(cfg, cid) / "003-implementer" / "prompt.md").read_text()
    assert "Review score 76 is below the pass score 80" in prompt
    assert "- [minor] m0 (feature.txt): d" in prompt


def test_e2e_review_fix_until_loop_max_parks(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    entries = [impl_done(), review("fix"), impl_done(), review("fix")]
    cfg = make_cfg(tmp_path, repo, script(tmp_path, entries), profile={"loop_max": 1})
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "loop_exhausted", st
    assert st["park"]["resume_to"] == "IMPLEMENTING" and st["counters"]["loops"] == 1
    assert not (repo / "feature.txt").exists()


def test_e2e_review_malformed_twice_escalates(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    bad = dict(review(), malformed=True)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), bad, bad]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "review_escalated", st
    assert st["park"]["resume_to"] == "REVIEWING" and st["counters"]["review_retries"] == 1
    assert st["counters"]["runs"] == 3 and st["counters"]["loops"] == 0


def test_e2e_main_dirty_then_resume(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]))
    cid = added(cfg, monkeypatch)
    (repo / "gates" / "lint.sh").write_text("true # edited\n")          # tracked, uncommitted
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "main_dirty", st
    assert st["park"]["resume_to"] == "CLOSING"
    assert not (repo / "feature.txt").exists()
    # resume without cleaning: parks again (re-plans against live state)
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--yes", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["to"] == "CLOSING"
    _git(repo, "checkout", "--", "gates/lint.sh")
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert (repo / "feature.txt").exists()
    types = ledger_types(cfg, cid)
    assert "resume" in types and types.count("park") == 1
    assert st["counters"]["parks"] == 1 and st["counters"]["close_attempts"] == 2


def test_e2e_cost_cap_parks_before_spawn(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), review()]),
                   profile={"campaign_cap_usd": 0.01})
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "cost_cap", st
    assert st["counters"]["runs"] == 0 and "run.start" not in ledger_types(cfg, cid)
    assert st["park"]["resume_to"] == "IMPLEMENTING"
    # raise the cap and resume → merges
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--raise-cap", "5", "--yes"])
    assert r.exit_code == 0, r.output
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    ev = S.read_ledger(cfg, cid, types=["resume"])[0]
    assert ev["cap_usd"] == 5.0


def test_e2e_rebase_conflict_parks(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(content="branch\n"), review()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "tick", "--json"])          # admit + open only
    assert r.exit_code == 0, r.output
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    _write(repo, "feature.txt", "main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "conflicting change on main")
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "rebase_conflict", st
    assert "feature.txt" in st["park"]["detail"]
    assert (repo / "feature.txt").read_text() == "main\n"


# ---- human commands against the loop ------------------------------------------------------------

def test_park_immediate_when_no_loop_then_abandon(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "tick"])                      # open it
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    r = run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "looking"])
    assert r.exit_code == 0 and "not written" in r.output
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    r = run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "looking", "--yes", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.park.v1" and d["mode"] == "immediate" and d["written"]
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "human"
    assert st["park"]["resume_to"] == "IMPLEMENTING"
    wt_path = Path(st["repo"]["worktree"])
    assert wt_path.is_dir()
    r = run_cli(cfg, monkeypatch, ["queue", "abandon", cid, "--yes", "--json"])
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.abandon.v1" and d["remove_worktree"] is True
    assert S.read_state(cfg, cid)["state"] == "ABANDONED"
    assert not wt_path.exists()
    assert _git(repo, "branch", "--list", st["repo"]["branch"])          # branch kept
    r = run_cli(cfg, monkeypatch, ["queue", "list", "--json"])
    assert json.loads(r.output)["items"] == []
    # a new campaign for the same spec is admissible again
    cid2 = added(cfg, monkeypatch)
    assert cid2 != cid


def test_park_deferred_while_loop_running(tmp_path, monkeypatch):
    """With a loop holding the lease, `park --yes` only requests the park. Requested between
    ticks ⇒ the next tick parks without running a step; requested mid-step ⇒ the step is
    recorded first, then the park applies."""
    from wt.agentic import lease as L
    from wt.agentic.runner import select_runner
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), impl_done()]))
    cid = added(cfg, monkeypatch)
    run_cli(cfg, monkeypatch, ["queue", "tick"])
    lease = L.acquire(cfg, ["test"])                                      # simulate a live loop
    try:
        r = run_cli(cfg, monkeypatch, ["queue", "park", cid, "--reason", "wait", "--yes", "--json"])
        assert json.loads(r.output)["mode"] == "deferred"
        st = S.read_state(cfg, cid)
        assert st["state"] == "IMPLEMENTING" and st["park_requested"]["detail"] == "wait"
        report = Q.tick(cfg)                                              # between ticks: no run
        st = S.read_state(cfg, cid)
        assert st["state"] == "PARKED" and st["park"]["reason"] == "human", st
        assert st["counters"]["runs"] == 0 and st["park"]["resume_to"] == "IMPLEMENTING"
        assert report.steps[0]["to"] == "PARKED" and report.steps[0]["reason"] == "human"

        r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--yes"])
        assert r.exit_code == 0, r.output

        class ParkMidRun:                                                 # a human parks mid-step
            def __init__(self):
                self.inner = select_runner(cfg)

            def run(self, req):
                with S.item_lock(cfg, cid):
                    on_disk = S.read_state(cfg, cid)
                    on_disk["park_requested"] = {"reason": "human", "detail": "mid-run"}
                    S.write_state(cfg, cid, on_disk)
                return self.inner.run(req)

        report = Q.tick(cfg, runner=ParkMidRun())
    finally:
        lease.release()
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["detail"] == "mid-run", st
    assert st["counters"]["runs"] == 1 and "run.end" in ledger_types(cfg, cid)
    assert st["park"]["resume_to"] == "GATING"                            # the implementer finished
    assert report.steps[0]["to"] == "PARKED" and report.steps[0]["run_id"] == "001-implementer"


def test_watch_renders(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()]))
    added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "watch", "--interval", "0.01", "--iterations", "2"])
    assert r.exit_code == 0, r.output
    assert "Active" in r.output and "Backlog (1)" in r.output


# ---- signals: a real process --------------------------------------------------------------------

def test_sigterm_mid_run_leaves_state_and_releases_lease(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    sc = script(tmp_path, [dict(impl_done(), sleep_s=4)])
    cfg = make_cfg(tmp_path, repo, sc)
    cid = added(cfg, monkeypatch)
    run_cli(cfg, monkeypatch, ["queue", "tick"])
    assert S.read_state(cfg, cid)["state"] == "IMPLEMENTING"
    (tmp_path / "cfg" / "config.yaml").write_text(json.dumps({
        "wt_checkout": str(repo), "specs_dir": cfg["specs_dir"], "outbox_dir": cfg["outbox_dir"],
        "org_files": cfg["org_files"], "org_ideas_file": cfg["org_ideas_file"],
        "org_capture_file": cfg["org_capture_file"], "agentic": cfg["agentic"]}))
    env = {**os.environ, "WT_CONFIG_DIR": str(tmp_path / "cfg"), "WT_DATA_DIR": cfg["data_dir"]}
    proc = subprocess.Popen([sys.executable, "-m", "wt.cli", "queue", "run", "--poll-interval", "0.1"],
                            env=env, start_new_session=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if "run.start" in ledger_types(cfg, cid):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("run never started: " + proc.communicate(timeout=5)[1])
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    st = S.read_state(cfg, cid)
    assert st["state"] == "IMPLEMENTING", st
    ends = S.read_ledger(cfg, cid, types=["run.end"])
    assert len(ends) == 1 and ends[0]["interrupted"] is True
    assert st["counters"]["runs"] == 1 and st["cost"]["total_usd"] == 0.5
    from wt.agentic import lease as L
    assert L.lease_status(cfg)["held"] is False


# ---- SPEC-0205: the test reviewer on the default balanced reviewers ---------------------------

BOTH = ["conformance_reviewer", "test_reviewer"]


def _both(tmp_path, monkeypatch, scripts, **profile):
    """cfg + campaign with the conformance and test reviewers (both in the balanced default,
    with SPEC-0202's adversarial reviewer) and a per-persona scripted runner."""
    from test_agentic_reviewers import PersonaRunner
    from wt.agentic.profiles import resolve_profile
    assert set(BOTH) <= set(resolve_profile({}, "balanced")["reviewers"])
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile=dict(reviewers=BOTH, **profile))
    return cfg, added(cfg, monkeypatch), repo, PersonaRunner(scripts)


@pytest.mark.parametrize("slow", ["conformance_reviewer", "test_reviewer"])
def test_e2e_test_reviewer_fix_then_pass_merges(tmp_path, monkeypatch, slow):
    from test_agentic_reviewers import drive
    scripts = {"implementer": [impl_done(), impl_done(content="better\n")],
               "conformance_reviewer": [review("pass"), review("pass")],
               "test_reviewer": [tr_review("fix"), tr_review("pass")]}
    scripts[slow][0]["sleep_s"] = 0.3                      # test_reviewer finishes first / last
    cfg, cid, repo, runner = _both(tmp_path, monkeypatch, scripts)
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert runner.calls.count("implementer") == 2 and st["counters"]["loops"] == 1
    starts = [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.start"])]
    assert starts == ["implementer", *BOTH, "implementer", *BOTH]
    findings = [f for f in S.read_ledger(cfg, cid, types=["finding"])
                if f["persona"] == "test_reviewer"]
    assert [(f["severity"], f["ac"], f["title"]) for f in findings] == [
        ("must_fix", "feature.txt exists", "no test for feature.txt")]
    assert st["last_review"]["verdicts"]["test_reviewer"]["coverage_assessment"][0]["covered"]
    prompt = (S.runs_dir(cfg, cid) / "004-implementer" / "prompt.md").read_text()
    assert "From: review" in prompt and "[test_reviewer] add a test for feature.txt" in prompt
    reviewer_prompt = (S.runs_dir(cfg, cid) / "003-test_reviewer" / "prompt.md").read_text()
    assert '"path": "feature.txt"' not in reviewer_prompt     # not a test file: not listed


def test_e2e_both_reviewers_pass_merges(tmp_path, monkeypatch):
    from test_agentic_reviewers import drive
    cfg, cid, repo, runner = _both(tmp_path, monkeypatch, {
        "implementer": [impl_done()], "conformance_reviewer": [review("pass")],
        "test_reviewer": [tr_review("pass")]})
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert set(st["last_review"]["verdicts"]) == set(BOTH) and st["last_review"]["verdict"] == "pass"
    assert st["cost"]["by_persona"]["test_reviewer"] == 0.2


def test_e2e_test_reviewer_malformed_twice_escalates(tmp_path, monkeypatch):
    from test_agentic_reviewers import drive
    bad = dict(tr_review("pass"), malformed=True)
    cfg, cid, repo, runner = _both(tmp_path, monkeypatch, {
        "implementer": [impl_done()], "conformance_reviewer": [review("pass")],
        "test_reviewer": [bad, dict(bad)]})
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "review_escalated", st
    assert "test_reviewer" in st["park"]["detail"] and "conformance" not in st["park"]["detail"]
    assert runner.calls.count("conformance_reviewer") == 1


def test_e2e_cost_cap_sums_the_test_reviewer_budget(tmp_path, monkeypatch):
    from test_agentic_reviewers import drive
    # after the implementer 0.5 is spent: 0.5 + 1.5 (conformance alone) fits 2.0, + 1.0 does not
    cfg, cid, repo, runner = _both(
        tmp_path, monkeypatch,
        {"implementer": [impl_done()], "conformance_reviewer": [review("pass")],
         "test_reviewer": [tr_review("pass")]},
        campaign_cap_usd=2.0, personas={"implementer": {"max_budget_usd": 1.0}})
    drive(cfg, runner)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "cost_cap", st
    assert st["park"]["resume_to"] == "REVIEWING" and st["counters"]["runs"] == 1
    assert runner.calls == ["implementer"]                   # no reviewer spawned, no partial fan-out


# ---- SPEC-0206: review triager -----------------------------------------------------------------

def triager_entry(*ids, reason="subjective", **kw):
    """FakeRunner entry: the review triager waives `ids`."""
    return dict({"persona": "review_triager", "cost_usd": 0.1, "verdict": {
        "waive": [{"id": i, "reason": reason} for i in ids], "note": "n"}}, **kw)


def _minors_review(n):
    return review("pass", findings=[_finding("minor", f"m{i}") for i in range(n)])


def _run_personas(cfg, cid):
    return [e["persona"] for e in S.read_ledger(cfg, cid, types=["run.start"])]


def test_e2e_triager_not_run_for_a_clean_pass_or_a_must_fix(tmp_path, monkeypatch):    # (a)
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, [impl_done(), review("pass")])
    assert st["state"] == "MERGED" and "review_triager" not in _run_personas(cfg, cid)
    assert "triage" not in st["last_review"]
    sub = tmp_path / "second"
    sub.mkdir()
    entries = [impl_done(), review("fix"), impl_done(content="better\n"), review("pass")]
    cfg, cid, st = _triage_run(sub, monkeypatch, entries)
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 1
    assert "review_triager" not in _run_personas(cfg, cid)


def test_e2e_triager_waives_minors_and_the_campaign_merges(tmp_path, monkeypatch):     # (b)
    entries = [impl_done(), _minors_review(6),
               triager_entry("conformance_reviewer#0", "conformance_reviewer#1",
                             "conformance_reviewer#2")]
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, entries)
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 0, st
    assert _run_personas(cfg, cid) == ["implementer", "conformance_reviewer", "review_triager"]
    lr = st["last_review"]
    assert lr["triage"] == "done" and lr["triager_run"] == "003-review_triager"
    assert [w["id"] for w in lr["waived"]] == [f"conformance_reviewer#{i}" for i in range(3)]
    assert (lr["verdict"], lr["score"]) == ("pass", 88)
    notes = [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]
    assert "waived conformance_reviewer#0: subjective" in notes
    assert notes.count("review score 88/80 pass") == 1
    trans = [(e["from"], e["to"], e["reason"]) for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert ("REVIEWING", "CLOSING", "review_pass") in trans
    assert st["cost"]["by_persona"]["review_triager"] == 0.1
    prompt = (S.runs_dir(cfg, cid) / "003-review_triager" / "prompt.md").read_text()
    assert "conformance_reviewer#5" in prompt and "waivable" in prompt


def test_e2e_triager_waives_too_little_so_one_loop_follows(tmp_path, monkeypatch):      # (c)
    entries = [impl_done(), _minors_review(8), triager_entry("conformance_reviewer#0"),
               impl_done(content="better\n"), review("pass")]
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, entries)
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 1, st
    assert _run_personas(cfg, cid).count("review_triager") == 1
    assert [e["reason"] for e in S.read_ledger(cfg, cid, types=["transition"])
            if e["from"] == "REVIEWING"].count("review_fix") == 1
    prompt = (S.runs_dir(cfg, cid) / "004-implementer" / "prompt.md").read_text()
    assert "Review score 72 is below the pass score 80" in prompt
    assert "- [minor] m0" not in prompt and "- [minor] m1 (feature.txt): d" in prompt


def test_e2e_triager_malformed_leaves_the_deterministic_fix(tmp_path, monkeypatch):     # (d)
    entries = [impl_done(), _minors_review(6), dict(triager_entry(), malformed=True),
               impl_done(content="better\n"), review("pass")]
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, entries)
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 1, st
    assert "review_escalated" not in [e["reason"] for e in S.read_ledger(cfg, cid, types=["park"])]
    notes = [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]
    assert any("malformed verdict" in n for n in notes)
    assert "review score 76/80 fix" in notes
    assert st["cost"]["by_persona"]["review_triager"] == 0.1      # charged though unusable


def test_e2e_cap_too_small_skips_the_triager(tmp_path, monkeypatch):                    # (e)
    profile = {"campaign_cap_usd": 1.0, "personas": {"implementer": {"max_budget_usd": 0.5},
                                                      "conformance_reviewer": {"max_budget_usd": 0.3}}}
    entries = [impl_done(), _minors_review(6)]
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, entries, profile=profile)
    assert _run_personas(cfg, cid) == ["implementer", "conformance_reviewer"]
    lr = st["last_review"]
    assert lr["triage"] == "skipped" and (lr["verdict"], lr["score"]) == ("fix", 76)
    notes = [n["text"] for n in S.read_ledger(cfg, cid, types=["note"])]
    assert "triager skipped: cost cap" in notes and "review score 76/80 fix" in notes
    trans = [(e["from"], e["to"], e["reason"]) for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert ("REVIEWING", "IMPLEMENTING", "review_fix") in trans      # the deterministic outcome
    assert st["state"] == "PARKED" and st["park"]["reason"] == "cost_cap"


# ---- SPEC-0207: follow-up ideas, end to end ---------------------------------------------------

def _fu_ideas(cfg):
    from wt import org as ORG
    ORG.invalidate_tasks_cache()
    return [t for t in ORG.load_tasks(cfg) if t.is_idea]


def test_e2e_followups_clean_pass_files_nothing(tmp_path, monkeypatch):               # AC7 (a)
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, [impl_done(), review("pass")])
    assert st["state"] == "MERGED" and _fu_ideas(cfg) == []
    assert st["close"]["followups"] == {"filed": [], "unfiled": [], "waived": [],
                                        "capped": [], "idea_id": None}
    assert "followup" not in ledger_types(cfg, cid)


def test_e2e_followups_advisory_findings_become_ideas(tmp_path, monkeypatch):         # AC7 (b)
    rv = review("pass", findings=[_finding("major", "risky parse"), _finding("minor", "naming"),
                                  _finding("nit", "typo")])
    repo = make_repo(tmp_path, extra_fm="parent: SPEC-0169\n")
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(extra="parent: SPEC-0169\n"), rv]))
    cid = added(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    [group] = _fu_ideas(cfg)                                 # ONE idea; the nit is not listed
    assert group.heading == f"wtfug:{cid} [SPEC-0001] review follow-ups (2 findings)"
    assert group.properties["KIND"] == "improvement" and group.epic == "SPEC-0169"
    assert group.project == st["project"]
    from wt import explore as EX
    summary = EX.read_idea_enrichment(cfg, group)["summary"].splitlines()
    assert [ln.split()[4] for ln in summary] == ["major", "minor"]
    assert st["close"]["followups"]["idea_id"] == group.properties["ID"]
    assert {f["idea_id"] for f in st["close"]["followups"]["filed"]} == {group.properties["ID"]}
    evs = S.read_ledger(cfg, cid, types=["followup"])
    assert [(e["severity"], e["title"], e["origin"]) for e in evs] == [
        ("major", "risky parse", "advisory"), ("minor", "naming", "advisory")]
    assert _git(repo, "status", "--porcelain") == ""         # the ideas live outside the repo
    r = run_cli(cfg, monkeypatch, ["campaign", "show", cid])
    assert "follow-up IDEA-" in r.output and "risky parse" in r.output
    assert f"follow-ups {group.properties['ID']} (2 listed, 0 capped)" in r.output


def test_e2e_followups_fixed_finding_is_not_filed_but_earlier_advisory_is(tmp_path, monkeypatch):
    first = review("fix", findings=[
        _finding("must_fix", "wrong text", ac="feature.txt exists"),
        _finding("minor", "naming", files=["feature.txt", "x.py"])], fix_brief="rewrite")
    second = review("pass", findings=[_finding("nit", "typo")])          # a nit: not filed
    entries = [impl_done(), first, impl_done(content="better\n"), second]
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, entries)                   # AC7 (c)
    assert st["state"] == "MERGED" and st["counters"]["loops"] == 1, st
    [group] = _fu_ideas(cfg)
    from wt import explore as EX
    body = EX.read_idea_enrichment(cfg, group)["summary"]
    assert "(1 finding)" in group.heading and "naming" in body and "wrong text" not in body
    assert [f["title"] for f in st["close"]["followups"]["filed"]] == ["naming"]
    assert [e["title"] for e in st["review_carry"]] == ["naming"]


def test_e2e_followups_close_run_twice_files_each_once(tmp_path, monkeypatch):         # AC7 (d)
    rv = review("pass", findings=[_finding("minor", "naming")])
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done(), rv]))
    cid = added(cfg, monkeypatch)
    (repo / "gates" / "lint.sh").write_text("true # edited\n")          # main dirty ⇒ close parks
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["resume_to"] == "CLOSING", st
    assert len(_fu_ideas(cfg)) == 1 and len(S.read_ledger(cfg, cid, types=["followup"])) == 1
    r = run_cli(cfg, monkeypatch, ["queue", "resume", cid, "--yes", "--json"])
    assert r.exit_code == 0, r.output
    _git(repo, "checkout", "--", "gates/lint.sh")
    r = run_cli(cfg, monkeypatch, ["queue", "run", "--until-idle"])
    assert r.exit_code == 0, r.output
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert len(_fu_ideas(cfg)) == 1 and len(S.read_ledger(cfg, cid, types=["followup"])) == 1
    [f] = st["close"]["followups"]["filed"]                  # the second run still reports it
    assert f["idea_id"] == _fu_ideas(cfg)[0].properties["ID"]


def test_e2e_followups_disabled_by_profile_files_nothing(tmp_path, monkeypatch):
    rv = review("pass", findings=[_finding("major", "risky"), _finding("minor", "naming")])
    cfg, cid, st = _triage_run(tmp_path, monkeypatch, [impl_done(), rv],
                               profile={"review": {"followups": False}})
    assert st["state"] == "MERGED" and _fu_ideas(cfg) == []
    assert st["close"]["followups"]["filed"] == [] and "followup" not in ledger_types(cfg, cid)


# ---- SPEC-0202: adversarial reviewer in the REVIEWING fan-out ---------------------------------

CONF, ADV = "conformance_reviewer", "adversarial_reviewer"


def adv_review(verdict="pass", *, category="path_traversal", malformed=False, **kw):
    """FakeRunner entry: the adversarial reviewer. `fix` carries one must_fix finding."""
    fix = verdict == "fix"
    findings = [{"severity": "must_fix", "category": category, "title": "name escapes the root",
                 "detail": "joined unchecked", "trigger": "when `name` contains `../`",
                 "files": ["feature.txt"], "lines": ["feature.txt:1"]}] if fix else []
    v = {"verdict": verdict, "score": 30 if fix else 92, "findings": findings,
         "fix_brief": "reject names with .." if fix else ""}
    v.update(kw)
    return {"persona": ADV, "verdict": v, "cost_usd": 0.3, "malformed": malformed}


class _PerPersona(FakeRunner):
    """`{persona: [entry, ...]}` scripts consumed per persona (reviewers run concurrently)."""

    def __init__(self, scripts):
        import threading
        self.scripts = {p: list(v) for p, v in scripts.items()}
        self.lock, self.calls = threading.Lock(), []

    def _next_entry(self, req):
        with self.lock:
            self.calls.append(req.persona)
            return self.scripts[req.persona].pop(0)


def _drive_adv(tmp_path, monkeypatch, scripts, **profile):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []),
                   profile={"reviewers": [CONF, ADV], **profile})
    cid = added(cfg, monkeypatch)
    runner = _PerPersona(scripts)
    for _ in range(14):
        Q.tick(cfg, runner=runner)
        if S.read_state(cfg, cid)["state"] in ("MERGED", "PARKED"):
            break
    return cfg, cid, runner


@pytest.mark.parametrize("as_pass", [False, True])
def test_e2e_adversarial_must_fix_routes_fix_then_merges(tmp_path, monkeypatch, as_pass):
    bad = adv_review("fix")
    if as_pass:                                     # `pass` carrying a must_fix still routes fix
        bad["verdict"]["verdict"] = "pass"
    cfg, cid, runner = _drive_adv(tmp_path, monkeypatch, {
        "implementer": [impl_done(), impl_done(content="better\n")],
        CONF: [review(), review()], ADV: [bad, adv_review()]})
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED", st
    assert runner.calls.count("implementer") == 2 and runner.calls.count(ADV) == 2
    brief = (S.runs_dir(cfg, cid) / "004-implementer" / "prompt.md").read_text()
    assert "From: review" in brief and f"[{ADV}] reject names with .." in brief
    assert "- MUST FIX [path_traversal]: name escapes the root (feature.txt)" in brief
    assert "trigger: when `name` contains `../`" in brief
    found = [e for e in S.read_ledger(cfg, cid, types=["finding"]) if e["persona"] == ADV]
    assert len(found) == 1 and found[0]["category"] == "path_traversal"
    assert found[0]["detail"] == "joined unchecked\ntrigger: when `name` contains `../`"
    assert st["last_review"]["verdicts"][ADV]["findings"] == [] and st["last_review"]["loop"] == 1
    first = S.read_ledger(cfg, cid, types=["run.end"])[2]
    assert first["persona"] == ADV and first["verdict"]["findings"][0]["lines"] == ["feature.txt:1"]


def test_e2e_adversarial_unknown_category_retries_then_escalates(tmp_path, monkeypatch):
    cfg, cid, runner = _drive_adv(tmp_path, monkeypatch, {
        "implementer": [impl_done()], CONF: [review()],
        ADV: [adv_review("fix", category="vibes"), adv_review("fix", category="vibes")]})
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "review_escalated"
    assert ADV in st["park"]["detail"] and CONF not in st["park"]["detail"]
    assert runner.calls.count(ADV) == 2 and runner.calls.count(CONF) == 1
    ends = [e for e in S.read_ledger(cfg, cid, types=["run.end"]) if e["persona"] == ADV]
    assert [e["verdict_ok"] for e in ends] == [False, False]


def test_e2e_adversarial_malformed_once_then_valid_merges(tmp_path, monkeypatch):
    cfg, cid, runner = _drive_adv(tmp_path, monkeypatch, {
        "implementer": [impl_done()], CONF: [review()],
        ADV: [adv_review(malformed=True), adv_review()]})
    assert S.read_state(cfg, cid)["state"] == "MERGED"
    assert runner.calls.count(ADV) == 2 and runner.calls.count(CONF) == 1


def test_e2e_cost_cap_counts_the_adversarial_reviewer(tmp_path, monkeypatch):
    """After the implementer ($3) conformance alone ($1.50) fits a $5 cap; with the adversarial
    reviewer ($1.50 more) it does not: parked before any reviewer is spawned."""
    impl = impl_done()
    impl["cost_usd"] = 3.0
    cfg, cid, runner = _drive_adv(tmp_path, monkeypatch, {
        "implementer": [impl], CONF: [review()], ADV: [adv_review()]}, campaign_cap_usd=5.0)
    st = S.read_state(cfg, cid)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "cost_cap"
    assert ADV in st["park"]["detail"]
    assert runner.calls == ["implementer"]
