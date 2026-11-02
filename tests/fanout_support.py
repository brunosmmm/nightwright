"""Local copies of the end-to-end helpers of `test_agentic_e2e` (SPEC-0176) and the reviewer
fan-out helpers of `test_agentic_reviewers` (SPEC-0201) that the SPEC-0345 tests drive. Copies,
not imports: `tools/affected_tests.py` treats every importer of a test module as affected, and
those two modules reference the project config, so importing them made `test_remote_persona_limits`
a hit of every project-config change. The temp repo's config path comes from `P.CONFIG_RELPATH`."""
import dataclasses
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright.agentic import personas as PS
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import store as S
from nightwright.agentic.runner import FakeRunner
from nightwright.cli import cli

ENV = {
    "GIT_AUTHOR_NAME": "nightwright test", "GIT_AUTHOR_EMAIL": "nightwright@test",
    "GIT_COMMITTER_NAME": "nightwright test", "GIT_COMMITTER_EMAIL": "nightwright@test",
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

PROJECT_YAML = """schema: nw.project.v1
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
  commit_trailer: "Co-Authored-By: nightwright test <nightwright@test>"
"""


def _git(repo, *args):
    # hermetic: the identity / no-global-config env is applied here, not only by the autouse
    # fixture, so modules that import make_repo don't depend on the host's git config
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env={**os.environ, **ENV}).stdout.strip()


def _write(repo, rel, text):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def write_review(cfg, spec_path, spec_id, verdict="approve", *, sha=None, findings=None,
                 reviewed_at="2026-10-25T00:00:00+00:00", **extra):
    """SPEC-0188/0210: the stored review `nw queue add` demands, keyed by the spec's sha."""
    from nightwright.agentic import spec_review as SR
    return SR.save_review(cfg, {
        "schema": SR.SCHEMA, "spec_id": spec_id, "verdict": verdict,
        "spec_sha256": sha or SR.spec_sha256(spec_path),
        "reviewed_at": reviewed_at, "model": "sonnet",
        "cost_usd": 0.0, "findings": findings or [], "suggested_splits": [], "repo_root": "", **extra})


def make_repo(tmp_path, *, spec_id="SPEC-0001", policy="merge", lint_required="true",
              test_gate="test -f feature.txt", lint_gate="true", with_origin=False, extra_fm="",
              spec_rel="docs/specs/0001-trivial.md", review=True):
    """A git repo that doubles as a nightwright checkout (src/nightwright + pyproject) with an accepted spec."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "src" / "nightwright").mkdir(parents=True)
    _write(repo, "pyproject.toml", "[project]\nname='nw'\n")
    spec_path = _write(repo, spec_rel, SPEC_ACCEPTED.format(id=spec_id, extra=extra_fm))
    if review:
        write_review({"data_dir": str(tmp_path / "data")}, spec_path, spec_id)
    _write(repo, "gates/test.sh", f"{test_gate}\n")
    _write(repo, "gates/lint.sh", f"{lint_gate}\n")
    _write(repo, P.CONFIG_RELPATH, PROJECT_YAML.format(policy=policy,
                                                        lint_required=lint_required))
    _write(repo, ".gitignore", ".setup-ran\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    if with_origin:
        origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True,
                       env={**os.environ, **ENV})
        _git(repo, "remote", "add", "origin", str(origin))
        _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


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
        "nw_checkout": str(repo),
        "outbox_dir": str(tmp_path / "outbox"),
        "outbox_targets": outbox or {},
        "agentic": {"runner": "fake", "fake_script": str(script_path), "default_profile": "balanced",
                    "poll_interval_s": 0.01, "lease_ttl_s": 120,
                    "profiles": {"balanced": {"reviewers": ["conformance_reviewer"], **(profile or {})}}},
    }
    return cfg


def run_cli(cfg, monkeypatch, args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args)


def added(cfg, monkeypatch, spec_id="SPEC-0001", *extra):
    r = run_cli(cfg, monkeypatch, ["queue", "add", spec_id, "--yes", "--json", *extra])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "nw.queue.add.v1" and d["written"] is True
    return d["campaign"]



# ---- reviewer fan-out ------------------------------------------------------------------------

A, B = "conformance_reviewer", "second_reviewer"
B_SETTINGS = {"model": "sonnet", "effort": "low", "max_budget_usd": 1.0,
              "permission_mode": "dontAsk", "read_only": True}
DELTA = {"reviewers": [A, B], "review": {"triager": False}, "personas": {B: B_SETTINGS}}


@pytest.fixture
def second_reviewer(monkeypatch):
    """A test-only second reviewer: the conformance entry under another name."""
    monkeypatch.setitem(PS.PERSONAS, B, dataclasses.replace(PS.PERSONAS[A], name=B))
    monkeypatch.setattr(PS, "REVIEWER_PERSONAS", (A, B))


class PersonaRunner(FakeRunner):
    """FakeRunner whose script is a `{persona: [entry, ...]}` map consumed per persona under a
    lock, so concurrent reviewers cannot race on the shared cursor. An entry may carry `raise`
    (the worker raises), `stop` (sets the tick's stop event) and `sleep_s`; `barrier` makes
    every reviewer wait for its siblings."""

    def __init__(self, scripts, barrier=None, stop=None):
        self.scripts = {p: list(v) for p, v in scripts.items()}
        self.lock = threading.Lock()
        self.barrier, self.stop, self.calls, self.done = barrier, stop, [], set()

    def _next_entry(self, req):
        with self.lock:
            self.calls.append(req.persona)
            q = self.scripts.get(req.persona)
            if not q:
                raise ValueError(f"script exhausted for {req.persona}")
            return q.pop(0)

    def run(self, req):
        if self.barrier is not None and req.persona in (A, B):
            self.barrier.wait(timeout=10)
        with self.lock:
            peek = (self.scripts.get(req.persona) or [{}])[0]
        if peek.get("raise"):
            self._next_entry(req)
            raise RuntimeError("worker blew up")
        if peek.get("stop"):             # interrupt once the sibling reviewer has finished
            deadline = time.monotonic() + 10
            while A not in self.done and time.monotonic() < deadline:
                time.sleep(0.01)
            self.stop.set()
        result = super().run(req)
        self.done.add(req.persona)
        return result


def b_entry(verdict="pass", **kw):
    return dict(review(verdict), persona=B, **kw)


def drive(cfg, runner, *, stop=None, ticks=12):
    reports = []
    for _ in range(ticks):
        rep = Q.tick(cfg, runner=runner, stop=stop)
        reports.append(rep)
        states = {s["state"] for s in S.list_campaigns(cfg)}
        if states <= {"MERGED", "PARKED", "ABANDONED", "HANDED_OFF"}:
            break
    return reports


@pytest.fixture
def e2e(tmp_path, monkeypatch, second_reviewer):
    def build(scripts, profile=None, **kw):
        repo = make_repo(tmp_path)
        delta = dict(DELTA, **(profile or {}))
        cfg = make_cfg(tmp_path, repo, script(tmp_path, []), profile=delta)
        cid = added(cfg, monkeypatch)
        return cfg, cid, repo, PersonaRunner(scripts, **kw)
    return build


def starts(cfg, cid):
    return [e["run_id"] for e in S.read_ledger(cfg, cid, types=["run.start"])]
