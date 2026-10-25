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


def make_repo(tmp_path, *, spec_id="SPEC-0001", policy="merge", lint_required="true",
              test_gate="test -f feature.txt", lint_gate="true", with_origin=False, extra_fm="",
              spec_rel="docs/specs/0001-trivial.md"):
    """A git repo that doubles as a wt checkout (src/wt + pyproject) with an accepted spec."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "wt").mkdir(parents=True)
    _write(repo, "pyproject.toml", "[project]\nname='wt'\n")
    _write(repo, spec_rel, SPEC_ACCEPTED.format(id=spec_id, extra=extra_fm))
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
                       "files": ["feature.txt"]}] if fix else [],
         "ac_assessment": [{"ac": "feature.txt exists", "met": not fix, "evidence": "feature.txt"}],
         "fix_brief": "rewrite feature.txt" if fix else ""}
    v.update(kw)
    return {"persona": "conformance_reviewer", "verdict": v, "cost_usd": 0.2}


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
                    "profiles": {"balanced": profile or {}}},
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
    assert st["last_review"]["verdict"] == "pass" and st["last_review"]["score"] == 95
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
    _write(repo, spec2, SPEC_ACCEPTED.format(id="SPEC-0002", extra="depends_on: [SPEC-0001]\n"))
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
    trans = [(e["from"], e["to"], e["reason"]) for e in S.read_ledger(cfg, cid, types=["transition"])]
    assert ("REVIEWING", "IMPLEMENTING", "review_fix") in trans
    assert ("REVIEWING", "CLOSING", "review_pass") in trans
    end = [e for e in S.read_ledger(cfg, cid, types=["run.end"])
           if e["persona"] == "conformance_reviewer"]
    assert [e["verdict"]["score"] for e in end] == [40, 95]
    # the second implementer saw the reviewer's brief
    prompt = (S.runs_dir(cfg, cid) / "003-implementer" / "prompt.md").read_text()
    assert "From: conformance_reviewer" in prompt and "rewrite feature.txt" in prompt


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
