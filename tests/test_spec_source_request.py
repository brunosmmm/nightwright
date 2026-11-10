"""SPEC-0535: specs cite the public request they come from."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import jsonschema
import pytest
from click.testing import CliRunner

from nightwright import idea_link as L
from nightwright import org_write as W
from nightwright import spec_numbers as SN
from nightwright import specs as S
from nightwright.agentic import queue as Q
from nightwright.cli import cli
from nightwright.org import load_tasks
from nightwright.specmeta import split_frontmatter

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
PINNED = "9700eaa1f712c11423e0542ae8882ba96653f4575be89e3f481d05df79eff208"
_REAL_SUBPROCESS_RUN = subprocess.run
_SCHEMA = json.loads((ROOT / "docs/specs/spec.schema.json").read_text())
_LINT = ROOT / "tools/spec_lint.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def _nw_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "nw-checkout"
    (root / "src" / "nightwright").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='nightwright'\n")
    return _init_repo(root)


def _project_yaml(repo: Path, project: str = "Example"):
    (repo / ".nw").mkdir(exist_ok=True)
    (repo / ".nw/project.yaml").write_text(
        f"schema: nw.project.v1\nproject: {project}\nbase: main\n")


def _cfg(tmp_path, *, repo: Path, nw_checkout: Path | None = None, project: str = "Example",
         specs_dir: Path | None = None):
    org = tmp_path / "org"
    org.mkdir(exist_ok=True)
    conf = tmp_path / "config"
    conf.mkdir(exist_ok=True)
    (conf / "mappings.yaml").write_text(
        f"{project}:\n  bucket: {project}\nmeta:\n  bucket: Meta-Tools\n"
    )
    sd = specs_dir or (tmp_path / "specs")
    sd.mkdir(parents=True, exist_ok=True)
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        shutil.copy(ROOT / "docs/specs" / name, sd / name)
    cfg = {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"),
        "config_dir": str(conf),
        "project_axis": "bucket",
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(sd),
        "outbox_targets": {
            project: {"repo_path": str(repo)},
            "Meta-Tools": {"repo_path": str(tmp_path / "wrong-meta")},
            "acme": {"repo_path": str(repo)},
        },
    }
    if nw_checkout is not None:
        cfg["nw_checkout"] = str(nw_checkout)
    _project_yaml(repo, project)
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


def _cfg_citing_repo(tmp_path, repo: Path) -> dict:
    specs = repo / "docs" / "specs"
    specs.mkdir(parents=True)
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        shutil.copy(ROOT / "docs/specs" / name, specs / name)
    return _cfg(tmp_path, repo=repo, specs_dir=specs)


def _seed_idea(monkeypatch, cfg, *, text="Link", state="INCUBATE", project="Example",
               summary="Hello", tags=("session-triage",)):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    args = ["idea", "capture", text, "--state", state, "--project", project]
    for t in tags:
        args.extend(["--tag", t])
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == 0, r.output
    idea = [t for t in load_tasks(cfg) if t.is_idea][-1]
    if summary is not None:
        sr = CliRunner().invoke(cli, ["idea", "summary", idea.properties["ID"], "--set", summary])
        assert sr.exit_code == 0, sr.output
        idea = W.resolve_selector(cfg, idea.properties["ID"])
    return idea


def _publish(monkeypatch, cfg, idea):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code == 0, r.output
    return W.resolve_selector(cfg, idea.properties["ID"])


def _show_backlog(repo: Path, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/nw-backlog:{path}"],
        check=True, capture_output=True,
    ).stdout


def _backlog_tip(repo: Path) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "refs/heads/nw-backlog"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


_FORBIDDEN_GIT = frozenset({"checkout", "switch", "add", "fetch", "push"})


def _git_spy(monkeypatch, repo: Path):
    """Record git subcommands run in ``repo``; forbid index/worktree mutators."""
    calls: list[tuple] = []
    real_run = _REAL_SUBPROCESS_RUN

    def spy(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and Path(cmd[2]).resolve() == repo.resolve():
            tail = tuple(cmd[3:])
            calls.append(tail)
            if tail and tail[0] in _FORBIDDEN_GIT:
                raise AssertionError(f"unexpected git {tail[0]!r}")
        return real_run(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    return calls


def _assert_cite_plumbing(calls: list[tuple], *, tip_before: str):
    kinds = {c[0] for c in calls if c}
    assert kinds >= {"hash-object", "mktree", "commit-tree", "update-ref"}
    upd = [c for c in calls if c and c[0] == "update-ref"]
    assert upd and upd[-1][-1] == tip_before
    ct = [c for c in calls if c and c[0] == "commit-tree"]
    assert ct and "-p" in ct[0]


def _fm_lines_after_source_idea(text: str) -> list[str]:
    fm = text.split("---\n", 2)[1]
    lines = fm.splitlines()
    ix = next(i for i, ln in enumerate(lines) if ln.startswith("source_idea:"))
    return lines[ix + 1 : ix + 3]


def _backlog_root_entries(repo: Path) -> list[tuple[str, str, str]]:
    out = _git(repo, "ls-tree", "refs/heads/nw-backlog").strip()
    if not out:
        return []
    rows = []
    for line in out.split("\n"):
        meta, path = line.split("\t", 1)
        mode, _typ, sha = meta.split()
        rows.append((mode, path, sha))
    return sorted(rows, key=lambda row: row[1])


def _linter():
    spec = importlib.util.spec_from_file_location("spec_lint", _LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _valid_spec_text(req="REQ-01234567"):
    return f"""---
id: SPEC-0999
title: Fixture
status: draft
owner: user
created: 2026-11-10
source_idea: IDEA-0001
source_request: {req}
---

## Context

c

## Goals / Non-goals

- g

## Decision

d

## Design

d

## Acceptance criteria

- [ ] x

## Test plan

- t

## Definition of done

- [ ] d
"""


def test_schema_accepts_source_request(tmp_path):
    good = {"id": "SPEC-0001", "title": "T", "status": "draft", "owner": "user",
            "created": "2026-11-10", "source_request": "REQ-01234567"}
    jsonschema.validate(good, _SCHEMA)
    for bad_val in ("REQ-IIIIIIII", "REQ-123", "hello"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**good, "source_request": bad_val}, _SCHEMA)
    jsonschema.validate({k: v for k, v in good.items() if k != "source_request"}, _SCHEMA)
    assert "source_request" in (ROOT / "docs/specs/SCHEMA.md").read_text()
    lint_root = tmp_path / "lint-tree"
    (lint_root / "docs" / "specs").mkdir(parents=True)
    shutil.copy(ROOT / "docs/LEDGER.md", lint_root / "docs/LEDGER.md")
    for name in ("SCHEMA.md", "spec.schema.json", "TEMPLATE.md", "0000-how-we-write-specs.md"):
        shutil.copy(ROOT / "docs/specs" / name, lint_root / "docs/specs" / name)
    (lint_root / "tools").mkdir()
    shutil.copy(_LINT, lint_root / "tools/spec_lint.py")
    fixture = lint_root / "docs/specs/9998-source-request-fixture.md"
    fixture.write_text(_valid_spec_text("REQ-01234567").replace("SPEC-0999", "SPEC-9998")
                       .replace("0999-fixture", "9998-source-request-fixture"))
    subprocess.run([sys.executable, "tools/spec_lint.py", "--write-ledger"], cwd=lint_root,
                   check=True, capture_output=True, text=True)
    r = subprocess.run([sys.executable, "tools/spec_lint.py"], cwd=lint_root,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


def test_unlinked_idea_does_not_read_the_backlog(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    task = W.resolve_selector(cfg, idea.properties["ID"])
    assert L.load_agreed_request(cfg, task) is None
    resolve_calls, backlog_reads = [], []
    real_resolve = L.resolve_linked_repo
    real_run = subprocess.run

    def spy_resolve(cfg, project):
        resolve_calls.append(project)
        return real_resolve(cfg, project)

    def spy_run(cmd, **kw):
        if cmd[:2] == ["git", "-C"] and any("nw-backlog" in str(x) for x in cmd):
            backlog_reads.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(L, "resolve_linked_repo", spy_resolve)
    monkeypatch.setattr(subprocess, "run", spy_run)
    Q.plan_add_idea(cfg, idea.properties["ID"])
    assert resolve_calls == [] and backlog_reads == []
    resolve_calls.clear()
    path, _spec_id = S.scaffold_from_idea(cfg, idea.properties["ID"])
    text = path.read_text()
    assert text.startswith("---\n") and "source_idea:" in text
    assert "source_request:" not in text.split("---\n", 2)[1]
    assert "Promoted from idea" in text
    assert resolve_calls == [] and backlog_reads == []


def test_scaffold_cites_the_public_summary(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg, summary="Public words")
    CliRunner().invoke(cli, ["idea", "log", idea.properties["ID"], "--note", "secret log"])
    CliRunner().invoke(cli, ["idea", "questions", idea.properties["ID"], "--add", "Still open?"])
    CliRunner().invoke(cli, ["idea", "clock-in", idea.properties["ID"]])
    idea = _publish(monkeypatch, cfg, idea)
    org_after_publish = Path(cfg["org_ideas_file"]).read_text()
    assert "Public words" in org_after_publish
    assert "session-triage" in org_after_publish
    assert "Still open?" in org_after_publish
    req = idea.properties["REQ"]
    title = L.load_agreed_request(cfg, idea).title
    blob_before = _show_backlog(repo, f"{req}.md")
    blob_text = blob_before.decode()
    created_line = [ln for ln in blob_text.splitlines() if ln.startswith("created:")][0]
    status_line = [ln for ln in blob_text.splitlines() if ln.startswith("status:")][0]
    org_before_text = Path(cfg["org_ideas_file"]).read_text()
    task_before = W.resolve_selector(cfg, idea.properties["ID"])
    req_base_before = task_before.properties["REQ_BASE"]
    req_prop_before = task_before.properties["REQ"]
    headline_before = task_before.heading
    sibling = _seed_idea(monkeypatch, cfg, text="Sibling", summary="other")
    _publish(monkeypatch, cfg, sibling)
    sib_id = W.resolve_selector(cfg, sibling.properties["ID"]).properties["REQ"]
    sib_sha = _git(repo, "rev-parse", f"refs/heads/nw-backlog:{sib_id}.md").strip()
    tip_before = _backlog_tip(repo)
    head_before = _git(repo, "symbolic-ref", "HEAD").strip()
    git_calls = _git_spy(monkeypatch, repo)
    path, spec_id = S.scaffold_from_idea(cfg, idea.properties["ID"])
    text = path.read_text()
    after_si = _fm_lines_after_source_idea(text)
    assert after_si[0] == f"source_request: {req}"
    assert "secret log" not in text
    ctx = text.split("## Context", 1)[1].split("## Goals", 1)[0]
    assert ctx.strip().startswith(f"Promoted from request `{req}` ({title}).")
    assert "Public words" in ctx
    fresh = W.resolve_selector(cfg, idea.properties["ID"])
    assert fresh.state == "SPECCED"
    assert fresh.properties["SPEC"] == spec_id
    assert fresh.properties["REQ_BASE"] == req_base_before
    assert fresh.properties["REQ"] == req_prop_before
    assert fresh.heading == headline_before
    org_after = Path(cfg["org_ideas_file"]).read_text()
    assert "secret log" in org_after
    assert "Public words" in org_after
    assert "session-triage" in org_after
    assert "Still open?" in org_after
    assert "clock" in org_after.lower() or "CLOCK" in org_after
    assert fresh.properties.get("REQ_BASE") == req_base_before
    assert fresh.properties.get("REQ") == req_prop_before
    assert f":SPEC: {spec_id}" in org_after
    for ln in org_before_text.splitlines():
        if ln.startswith(":REQ:") or ln.startswith(":REQ_BASE:") or ln.startswith("* INCUBATE"):
            assert ln in org_after or ln.replace("INCUBATE", "SPECCED") in org_after
    cited = _show_backlog(repo, f"{req}.md")
    cited_text = cited.decode()
    fm, body = split_frontmatter(cited_text)
    assert fm["spec"] == spec_id
    assert idea.properties["ID"] not in cited_text
    ub = cited_text.splitlines()
    i_ub = next(i for i, ln in enumerate(ub) if ln.startswith("updated_by:"))
    i_spec = next(i for i, ln in enumerate(ub) if ln.startswith("spec:"))
    assert i_ub < i_spec
    assert [ln for ln in cited_text.splitlines() if ln.startswith("created:")][0] == created_line
    assert [ln for ln in cited_text.splitlines() if ln.startswith("status:")][0] == status_line
    assert body == "Public words"
    assert _git(repo, "rev-parse", f"refs/heads/nw-backlog:{sib_id}.md").strip() == sib_sha
    _assert_cite_plumbing(git_calls, tip_before=tip_before)
    log_row = _git(repo, "log", "-1", "--format=%s|%an|%ae", "refs/heads/nw-backlog").strip()
    subj, an, ae = log_row.split("|")
    assert subj == f"cite {req} {spec_id}"
    assert an == "Test User" and ae == "test@example.com"
    assert _git(repo, "symbolic-ref", "HEAD").strip() == head_before

    empty = _seed_idea(monkeypatch, cfg, text="Empty pub", summary="")
    empty = _publish(monkeypatch, cfg, empty)
    ereq = empty.properties["REQ"]
    p_empty, _ = S.scaffold_from_idea(cfg, empty.properties["ID"])
    ectx = p_empty.read_text().split("## Context", 1)[1].split("## Goals", 1)[0].strip()
    assert ectx == f"Promoted from request `{ereq}` (Empty pub)."
    assert "Public words" not in ectx

    path_epic, _spec_epic = S.scaffold_from_idea(cfg, sibling.properties["ID"], epic=True)
    epic_si = _fm_lines_after_source_idea(path_epic.read_text())
    assert epic_si[0] == f"source_request: {sib_id}"
    agreed = L.load_agreed_request(cfg, W.resolve_selector(cfg, idea.properties["ID"]))
    assert agreed and agreed.spec == spec_id
    tip_before_redo = _backlog_tip(repo)
    n_commits = int(_git(repo, "rev-list", "--count", "refs/heads/nw-backlog"))
    L.cite_spec(cfg, agreed, spec_id)
    assert _backlog_tip(repo) == tip_before_redo
    assert int(_git(repo, "rev-list", "--count", "refs/heads/nw-backlog")) == n_commits


def test_scaffold_refuses_drift_and_bad_files(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    req_base = idea.properties["REQ_BASE"]
    org_path = Path(cfg["org_ideas_file"])

    def refuse(msg_part: str):
        org_before = org_path.read_bytes()
        tip = _backlog_tip(repo)
        with pytest.raises(ValueError, match=re.escape(msg_part) if "match" not in msg_part
                           else msg_part):
            S.scaffold_from_idea(cfg, idea.properties["ID"])
        assert org_path.read_bytes() == org_before
        assert _backlog_tip(repo) == tip
        assert not list(Path(cfg["specs_dir"]).glob("0*.md"))

    CliRunner().invoke(cli, ["idea", "summary", idea.properties["ID"], "--set", "Drifted"])
    refuse("do not match :REQ_BASE:")

    idea = W.resolve_selector(cfg, idea.properties["ID"])
    CliRunner().invoke(cli, ["idea", "summary", idea.properties["ID"], "--set", "Hello"])
    published = _show_backlog(repo, f"{req}.md").decode()
    fm_pub, _ = split_frontmatter(published)
    drift_pub = L.render_request(
        req_id=req, title=fm_pub["title"], summary="Public drift", status=fm_pub["status"],
        created=[ln.split(": ", 1)[1] for ln in published.splitlines() if ln.startswith("created:")][0],
        updated=fm_pub["updated"], created_by=fm_pub["created_by"], updated_by=fm_pub["updated_by"],
    )
    L._publish_commit(repo, rel_path=f"{req}.md", body=drift_pub,
                      message="pub drift", name="Test User", email="test@example.com")
    refuse("do not match :REQ_BASE:")
    L._publish_commit(repo, rel_path=f"{req}.md", body=published.encode(),
                      message="restore pub", name="Test User", email="test@example.com")

    matched = "Matched wrong base"
    CliRunner().invoke(cli, ["idea", "summary", idea.properties["ID"], "--set", matched])
    L._publish_commit(
        repo, rel_path=f"{req}.md",
        body=L.render_request(
            req_id=req, title="Link", summary=matched, status="open",
            created="2026-11-08T12:00:00Z", updated="2026-11-08T12:00:00Z",
            created_by="Test User <test@example.com>", updated_by="Test User <test@example.com>",
        ),
        message="both drift", name="Test User", email="test@example.com",
    )
    refuse("do not match :REQ_BASE:")
    CliRunner().invoke(cli, ["idea", "summary", idea.properties["ID"], "--set", "Hello"])
    restore_good = lambda: L._publish_commit(
        repo, rel_path=f"{req}.md", body=published.encode(),
        message="restore", name="Test User", email="test@example.com")
    restore_good()

    idea = W.resolve_selector(cfg, idea.properties["ID"])
    CliRunner().invoke(cli, ["idea", "summary", idea.properties["ID"], "--set", "Hello"])
    idea = W.resolve_selector(cfg, idea.properties["ID"])
    W.clear_property(cfg, idea, "REQ_BASE")
    refuse("but no :REQ_BASE:")

    idea = W.resolve_selector(cfg, idea.properties["ID"])
    W.set_property(cfg, idea, "REQ_BASE", req_base)
    subprocess.run(["git", "-C", str(repo), "update-ref", "-d", "refs/heads/nw-backlog"], check=True)
    refuse("is not on refs/heads/nw-backlog")

    good = L.render_request(
        req_id=req, title="Link", summary="Hello", status="open",
        created="2026-11-08T12:00:00Z", updated="2026-11-08T12:00:00Z",
        created_by="Test User <test@example.com>", updated_by="Test User <test@example.com>",
    )
    L._publish_commit(repo, rel_path=f"{req}.md", body=good,
                      message="restore", name="Test User", email="test@example.com")
    idea = W.resolve_selector(cfg, idea.properties["ID"])
    published = _show_backlog(repo, f"{req}.md").decode()
    good_text = published
    head = published[:published.index("---\n", 4) + 4]

    def restore_good():
        L._publish_commit(repo, rel_path=f"{req}.md", body=good,
                          message="restore", name="Test User", email="test@example.com")

    L._publish_commit(repo, rel_path=f"{req}.md", body=(head + "Hello\n").encode(),
                      message="bad", name="Test User", email="test@example.com")
    refuse("summary is not canonical")
    restore_good()

    bad2_text = good_text.replace(f"id: {req}", "id: WRONG0001", 1)
    L._publish_commit(repo, rel_path=f"{req}.md", body=bad2_text.encode(),
                      message="bad2", name="Test User", email="test@example.com")
    refuse("file id is")
    restore_good()

    extra_text = good_text.replace("updated_by:", "foo: bar\nupdated_by:", 1)
    L._publish_commit(repo, rel_path=f"{req}.md", body=extra_text.encode(),
                      message="bad3", name="Test User", email="test@example.com")
    refuse("unexpected key")
    restore_good()

    L.cite_spec(cfg, L.load_agreed_request(cfg, idea), "SPEC-0099")
    refuse("already cites")
    restore_good()

    bad_spec = L.render_request(
        req_id=req, title="Link", summary="Hello", status="open",
        created="2026-11-08T12:00:00Z", updated="2026-11-08T12:00:00Z",
        created_by="Test User <test@example.com>", updated_by="Test User <test@example.com>",
        spec="SPEC-ABCD",
    )
    L._publish_commit(repo, rel_path=f"{req}.md", body=bad_spec,
                      message="bad spec", name="Test User", email="test@example.com")
    refuse("file spec is")
    restore_good()

    bad_title_text = good_text.replace('title: "Link"', 'title: "Link "', 1)
    L._publish_commit(repo, rel_path=f"{req}.md", body=bad_title_text.encode(),
                      message="bad title", name="Test User", email="test@example.com")
    refuse("title is not canonical")
    restore_good()

    idea = W.resolve_selector(cfg, idea.properties["ID"])
    W.set_property(cfg, idea, "REQ", "REQ-123")
    refuse("invalid :REQ:")
    idea = W.resolve_selector(cfg, idea.properties["ID"])
    W.set_property(cfg, idea, "REQ", req)

    subprocess.run(["git", "-C", str(repo), "config", "--unset", "user.name"], check=True)
    refuse("user.name")

    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "--unset", "user.email"], check=True)
    refuse("user.email")
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)

    wt = tmp_path / "backlog-wt"
    if _backlog_tip(repo):
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "nw-backlog"], check=True)
    else:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "nw-backlog", str(wt)],
            check=True,
        )
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    hash_calls = []

    def spy_hash(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and Path(cmd[2]).resolve() == repo.resolve():
            if cmd[3] == "hash-object":
                hash_calls.append(cmd)
        return _REAL_SUBPROCESS_RUN(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy_hash)
    with pytest.raises(ValueError, match="checked out"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert not hash_calls

    subprocess.run(["git", "-C", str(repo), "worktree", "remove", "-f", str(wt)], check=True)
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    L._publish_commit(repo, rel_path=f"{req}.md", body=good,
                      message="clear spec", name="Test User", email="test@example.com")

    calls = [0]
    real_load = L.load_agreed_request

    def second_load(cfg, task):
        calls[0] += 1
        ag = real_load(cfg, task)
        if calls[0] >= 2 and ag:
            return dataclasses.replace(ag, spec="SPEC-0099")
        return ag

    monkeypatch.setattr(L, "load_agreed_request", second_load)
    tip_pin = _backlog_tip(repo)
    res_n = len(SN.list_reservations(cfg))
    with pytest.raises(ValueError, match="already cites"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert not list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))
    assert _backlog_tip(repo) == tip_pin
    assert len(SN.list_reservations(cfg)) == res_n


def test_force_replaces_an_existing_spec_cite(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    agreed = L.load_agreed_request(cfg, idea)
    L.cite_spec(cfg, agreed, "SPEC-0099")
    sha_before = _git(repo, "rev-parse", f"refs/heads/nw-backlog:{req}.md").strip()
    with pytest.raises(ValueError, match="already cites"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert _git(repo, "rev-parse", f"refs/heads/nw-backlog:{req}.md").strip() == sha_before
    path, sid = S.scaffold_from_idea(cfg, idea.properties["ID"], force=True)
    fm, _ = split_frontmatter(_show_backlog(repo, f"{req}.md").decode())
    assert fm["spec"] == sid and sid != "SPEC-0099"
    assert path.exists()


def test_internal_into_another_checkout_records_source_request_only(tmp_path, monkeypatch):
    linked = _init_repo(tmp_path / "linked")
    foreign = _init_repo(tmp_path / "foreign")
    specs = foreign / "docs" / "specs"
    specs.mkdir(parents=True)
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        shutil.copy(ROOT / "docs/specs" / name, specs / name)
    nw = _nw_checkout(tmp_path)
    _project_yaml(nw, "Meta-Tools")
    cfg = _cfg(tmp_path, repo=linked, nw_checkout=nw, project="Meta-Tools", specs_dir=specs)
    cfg["outbox_targets"]["Meta-Tools"]["repo_path"] = str(linked)
    idea = _seed_idea(monkeypatch, cfg, project="Meta-Tools")
    CliRunner().invoke(cli, ["idea", "log", idea.properties["ID"], "--note", "private only"])
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    blob_before = _show_backlog(nw, f"{req}.md")
    CliRunner().invoke(cli, ["idea", "summary", idea.properties["ID"], "--set", "Drifted"])
    with pytest.raises(ValueError, match="do not match :REQ_BASE:"):
        S.promote_idea(cfg, idea.properties["ID"], internal=True)
    CliRunner().invoke(cli, ["idea", "summary", idea.properties["ID"], "--set", "Hello"])
    path, _, _kind = S.promote_idea(cfg, idea.properties["ID"], internal=True)
    body = path.read_text()
    assert f"source_request: {req}" in body
    assert "private only" not in body
    assert "Promoted from request" in body
    assert _show_backlog(nw, f"{req}.md") == blob_before
    idea2 = _publish(monkeypatch, cfg, _seed_idea(monkeypatch, cfg, project="Meta-Tools", text="No preflight"))
    subprocess.run(["git", "-C", str(nw), "config", "--unset", "user.name"], check=True)
    path2, _, _ = S.promote_idea(cfg, idea2.properties["ID"], internal=True)
    assert path2.exists()
    subprocess.run(["git", "-C", str(nw), "config", "user.name", "Test User"], check=True)
    idea3 = _publish(monkeypatch, cfg, _seed_idea(monkeypatch, cfg, project="Meta-Tools", text="No checkout guard"))
    wt = tmp_path / "nw-bk-wt"
    if _backlog_tip(nw):
        subprocess.run(["git", "-C", str(nw), "worktree", "add", "-q", str(wt), "nw-backlog"],
                       check=True)
    else:
        subprocess.run(
            ["git", "-C", str(nw), "worktree", "add", "-q", "-b", "nw-backlog", str(wt)],
            check=True,
        )
    hash_calls = []

    def spy_hash(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and Path(cmd[2]).resolve() == nw.resolve():
            if cmd[3] == "hash-object":
                hash_calls.append(cmd)
        return _REAL_SUBPROCESS_RUN(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy_hash)
    path3, _, _ = S.promote_idea(cfg, idea3.properties["ID"], internal=True)
    assert path3.exists() and not hash_calls
    subprocess.run(["git", "-C", str(nw), "worktree", "remove", "-f", str(wt)], check=True)
    monkeypatch.setattr(subprocess, "run", _REAL_SUBPROCESS_RUN)
    idea4 = _publish(monkeypatch, cfg, _seed_idea(monkeypatch, cfg, project="Meta-Tools", text="Both unset"))
    subprocess.run(["git", "-C", str(nw), "config", "--unset", "user.name"], check=True)
    subprocess.run(["git", "-C", str(nw), "config", "--unset", "user.email"], check=True)
    blob4 = _show_backlog(nw, f"{idea4.properties['REQ']}.md")
    path4, _, _ = S.promote_idea(cfg, idea4.properties["ID"], internal=True)
    assert path4.exists()
    assert _show_backlog(nw, f"{idea4.properties['REQ']}.md") == blob4
    subprocess.run(["git", "-C", str(nw), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(nw), "config", "user.email", "test@example.com"], check=True)
    load_calls = []
    real = L.load_agreed_request

    def spy_load(cfg, task):
        load_calls.append(1)
        return real(cfg, task)

    monkeypatch.setattr(L, "load_agreed_request", spy_load)
    acme = _seed_idea(monkeypatch, cfg, text="Out", project="acme", summary="out")
    _publish(monkeypatch, cfg, acme)
    S.promote_idea(cfg, acme.properties["ID"])
    assert load_calls == []


def test_cite_org_write_failure_restores_the_tip(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    idea = _publish(monkeypatch, cfg, idea)
    tip_publish = _backlog_tip(repo)
    org_before = Path(cfg["org_ideas_file"]).read_bytes()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(W, "set_property", boom)
    with pytest.raises(ValueError, match="restored refs/heads/nw-backlog"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert not list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_before
    assert _backlog_tip(repo) == tip_publish

    from nightwright.org_write import set_property as real_set_property

    monkeypatch.setattr(W, "set_property", real_set_property)
    idea = _publish(monkeypatch, cfg, _seed_idea(monkeypatch, cfg, text="Specced fail"))
    idea_id = idea.properties["ID"]
    tip = _backlog_tip(repo)
    org_before2 = Path(cfg["org_ideas_file"]).read_bytes()
    res_n = len(SN.list_reservations(cfg))

    def boom_state(cfg_a, task, state):
        raise OSError("disk full")

    monkeypatch.setattr(W, "set_state", boom_state)
    with pytest.raises(ValueError, match="restored refs/heads/nw-backlog"):
        S.scaffold_from_idea(cfg, idea_id)
    after = W.resolve_selector(cfg, idea_id)
    assert not (after.properties.get("SPEC") or "").strip()
    assert _backlog_tip(repo) == tip
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_before2
    assert not list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))
    assert len(SN.list_reservations(cfg)) == res_n

    idea = _publish(monkeypatch, cfg, _seed_idea(monkeypatch, cfg, text="Cas"))
    tip_old = _backlog_tip(repo)
    main = _git(repo, "rev-parse", "main").strip()
    org_cas = Path(cfg["org_ideas_file"]).read_bytes()
    res_cas = len(SN.list_reservations(cfg))

    def boom_move(*_a, **_k):
        subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/nw-backlog", main],
                       check=True)
        raise OSError("disk full")

    monkeypatch.setattr(W, "set_property", boom_move)
    with pytest.raises(ValueError, match="no longer points at"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))
    assert _git(repo, "rev-parse", "refs/heads/nw-backlog").strip() == main
    assert _backlog_tip(repo) != tip_old
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_cas
    assert len(SN.list_reservations(cfg)) == res_cas + 1

    from nightwright.org_write import set_property as real_sp

    monkeypatch.setattr(W, "set_property", real_sp)
    idea = _publish(monkeypatch, cfg, _seed_idea(monkeypatch, cfg, text="Restore fail"))
    org_restore = Path(cfg["org_ideas_file"]).read_bytes()
    res_restore = len(SN.list_reservations(cfg))
    tip_restore = _backlog_tip(repo)
    backlog_ref_updates = []
    real_run = _REAL_SUBPROCESS_RUN

    def fail_restore(cmd, **kw):
        if (
            len(cmd) >= 6
            and cmd[0] == "git"
            and str(repo) in cmd[2]
            and cmd[3] == "update-ref"
            and "refs/heads/nw-backlog" in cmd
        ):
            backlog_ref_updates.append(cmd)
            if len(backlog_ref_updates) >= 2:
                return mock.Mock(returncode=1, stdout=b"", stderr=b"")
        return real_run(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", fail_restore)

    def boom_sp(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(W, "set_property", boom_sp)
    with pytest.raises(ValueError, match="restore failed") as exc:
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert "restored refs/heads/nw-backlog" not in str(exc.value)
    assert list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_restore
    assert len(SN.list_reservations(cfg)) == res_restore + 1
    assert _backlog_tip(repo) != tip_restore


def _move_idea_to_sibling_org_file(cfg, idea_id: str, alt_name: str = "ideas-rotated.org") -> Path:
    """Put one idea in a second org file under the same directory (rotation-style)."""
    main_org = Path(cfg["org_ideas_file"])
    alt_org = main_org.parent / alt_name
    task = W.resolve_selector(cfg, idea_id)
    lines = main_org.read_text().splitlines(keepends=True)
    start = task.line - 1
    depth = task.level
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if ln.startswith("*") and not ln.startswith("*" * (depth + 1)):
            break
        end += 1
    block = lines[start:end]
    first_idea = next(i for i, ln in enumerate(lines) if ln.startswith("* "))
    header = lines[:first_idea]
    alt_org.write_text("".join(header + block))
    main_org.write_text("".join(header + lines[end:]))
    return alt_org


def test_cite_org_rollback_restores_task_file(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg, text="Rotated file")
    idea = _publish(monkeypatch, cfg, idea)
    iid = idea.properties["ID"]
    alt_org = _move_idea_to_sibling_org_file(cfg, iid)
    task = W.resolve_selector(cfg, iid)
    assert Path(task.file).resolve() == alt_org.resolve()
    tip = _backlog_tip(repo)
    org_snapshot = alt_org.read_bytes()
    res_n = len(SN.list_reservations(cfg))

    def boom_sp(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(W, "set_property", boom_sp)
    with pytest.raises(ValueError, match="restored refs/heads/nw-backlog"):
        S.scaffold_from_idea(cfg, iid)
    assert alt_org.read_bytes() == org_snapshot
    assert Path(cfg["org_ideas_file"]).read_bytes() != org_snapshot
    assert not list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))
    assert _backlog_tip(repo) == tip
    assert len(SN.list_reservations(cfg)) == res_n


def test_cite_org_rollback_replace_failure_keeps_spec(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg, text="Replace fail")
    idea = _publish(monkeypatch, cfg, idea)
    tip = _backlog_tip(repo)
    res_n = len(SN.list_reservations(cfg))
    real_replace = os.replace

    def fail_replace(src, dst, **kw):
        if ".nw-org-" in str(src):
            raise OSError("replace failed")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_replace)

    def boom_sp(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(W, "set_property", boom_sp)
    with pytest.raises(OSError, match="replace failed"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))
    assert _backlog_tip(repo) == tip
    assert len(SN.list_reservations(cfg)) == res_n + 1


def test_cite_pins_backlog_tip_at_in_lock_load(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    org_before = Path(cfg["org_ideas_file"]).read_bytes()
    tip_at_publish = _backlog_tip(repo)
    bumped = []
    real_run = _REAL_SUBPROCESS_RUN
    bump_body = (
        b"---\n"
        b"id: REQ-11111111\n"
        b'title: "Bump"\n'
        b"status: open\n"
        b"created: 2020-01-01T00:00:00Z\n"
        b'created_by: "a"\n'
        b"updated: 2020-01-01T00:00:00Z\n"
        b'updated_by: "a"\n'
        b"---\n"
        b"\n"
    )

    def spy(cmd, **kw):
        if (
            len(cmd) >= 6
            and cmd[0] == "git"
            and Path(cmd[2]).resolve() == repo.resolve()
            and cmd[3] == "update-ref"
            and cmd[4] == "refs/heads/nw-backlog"
            and not bumped
        ):
            bumped.append(1)
            L._publish_commit(
                repo, rel_path="BUMP.md", body=bump_body, message="bump",
                name="Test User", email="test@example.com",
            )
        return real_run(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    with pytest.raises(ValueError, match="was not cited; refs/heads/nw-backlog unchanged"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    cited = _show_backlog(repo, f"{req}.md").decode()
    assert "spec:" not in cited
    assert _backlog_tip(repo) != tip_at_publish
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_before
    assert not list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))


def test_scaffold_rollback_when_cite_update_ref_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    idea = _publish(monkeypatch, cfg, idea)
    org_before = Path(cfg["org_ideas_file"]).read_bytes()
    tip = _backlog_tip(repo)
    res_n = len(SN.list_reservations(cfg))
    real_run = _REAL_SUBPROCESS_RUN

    def fail_upd(cmd, **kw):
        if (
            len(cmd) >= 5
            and cmd[0] == "git"
            and Path(cmd[2]).resolve() == repo.resolve()
            and cmd[3] == "update-ref"
            and cmd[4] == "refs/heads/nw-backlog"
        ):
            return mock.Mock(returncode=1, stdout=b"", stderr=b"")
        return real_run(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", fail_upd)
    with pytest.raises(ValueError, match="was not cited; refs/heads/nw-backlog unchanged"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert not list(Path(cfg["specs_dir"]).glob("[0-9]*.md"))
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_before
    assert _backlog_tip(repo) == tip
    assert len(SN.list_reservations(cfg)) == res_n
    fresh = W.resolve_selector(cfg, idea.properties["ID"])
    assert not (fresh.properties.get("SPEC") or "").strip()


def test_load_agreed_request_rejects_bad_request_files(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    task = W.resolve_selector(cfg, idea.properties["ID"])
    good = _show_backlog(repo, f"{req}.md")

    def publish_body(body: bytes):
        L._publish_commit(repo, rel_path=f"{req}.md", body=body,
                          message="mutate", name="Test User", email="test@example.com")

    def restore():
        publish_body(good)

    publish_body(b"\xff\xfe".ljust(20, b"\x00"))
    with pytest.raises(ValueError, match="is not a request"):
        L.load_agreed_request(cfg, task)
    restore()

    publish_body(b"---\n- not-a-dict\n---\n\n")
    with pytest.raises(ValueError, match="is not a request"):
        L.load_agreed_request(cfg, task)
    restore()

    txt = good.decode()
    missing_title = txt.replace('title: "Link"\n', "", 1)
    publish_body(missing_title.encode())
    with pytest.raises(ValueError, match="is not a request"):
        L.load_agreed_request(cfg, task)
    restore()

    real_summary = L.idea_summary_text

    def boom_summary(cfg_a, task_a, **kw):
        raise OSError("no summary drawer")

    monkeypatch.setattr(L, "idea_summary_text", boom_summary)
    with pytest.raises(ValueError, match="could not read Summary"):
        L.load_agreed_request(cfg, task)
    monkeypatch.setattr(L, "idea_summary_text", real_summary)


def test_cite_preserves_bare_yaml_status(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    pub = _show_backlog(repo, f"{req}.md").decode()
    fm, body = split_frontmatter(pub)
    bare = (
        f"---\n"
        f"id: {req}\n"
        f'title: "{fm["title"]}"\n'
        f"status: yes\n"
        f"created: {[ln.split(': ', 1)[1] for ln in pub.splitlines() if ln.startswith('created:')][0]}\n"
        f"updated: {fm['updated']}\n"
        f'created_by: "{fm["created_by"]}"\n'
        f'updated_by: "{fm["updated_by"]}"\n'
        f"---\n{body}"
    )
    L._publish_commit(repo, rel_path=f"{req}.md", body=bare.encode(),
                      message="bare yes", name="Test User", email="test@example.com")
    path, spec_id = S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert path.exists()
    blob = _show_backlog(repo, f"{req}.md").decode()
    assert re.search(r"^status: yes\n", blob, re.M)
    assert "status: True" not in blob and "status: false" not in blob.lower()
    assert split_frontmatter(blob)[0]["spec"] == spec_id


def test_cite_preserves_quoted_status(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg_citing_repo(tmp_path, repo)
    idea = _seed_idea(monkeypatch, cfg)
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    yes = L.render_request(
        req_id=req, title="Link", summary="Hello", status="yes",
        created="2026-11-08T12:00:00Z", updated="2026-11-08T12:00:00Z",
        created_by="Test User <test@example.com>", updated_by="Test User <test@example.com>",
    )
    L._publish_commit(repo, rel_path=f"{req}.md", body=yes,
                      message="yes status", name="Test User", email="test@example.com")
    path, spec_id = S.scaffold_from_idea(cfg, idea.properties["ID"])
    assert path.exists()
    blob = _show_backlog(repo, f"{req}.md").decode()
    assert 'status: "yes"' in blob
    fm, _ = split_frontmatter(blob)
    assert fm["status"] == "yes"
    assert fm["spec"] == spec_id


def test_write_rules_and_front_matter_require_source_request():
    import nightwright.agentic.spec_write as SW

    idea = {"id": "IDEA-001", "source_request": "REQ-01234567", "summary": "pub"}
    rules = SW.write_rules(
        "write", {"path": "docs/specs/0002-x.md", "number": 2}, idea, None)
    assert "`source_request: REQ-01234567`" in rules
    assert "public summary" in rules
    rev = SW.write_rules("revise", {"path": "docs/specs/0002-x.md", "number": 2}, idea, None)
    assert "source_request" in rev and "Keep" in rev
    plain_rules = SW.write_rules(
        "write", {"path": "docs/specs/0002-x.md", "number": 2}, {"id": "IDEA-002"}, None)
    assert "no `source_request` key" in plain_rules

    fm = {"id": "SPEC-0002", "status": "proposed", "source_idea": "IDEA-001",
          "source_request": "REQ-01234567", "owner": "user", "kind": "feature"}
    assert SW._front_matter_problems(fm, "SPEC-0002", idea) == []
    assert "source_request must be 'REQ-01234567'" in "".join(
        SW._front_matter_problems({**fm, "source_request": "REQ-00000000"}, "SPEC-0002", idea))
    assert "source_request must be" in "".join(
        SW._front_matter_problems({k: v for k, v in fm.items() if k != "source_request"},
                                  "SPEC-0002", idea))
    plain = {"id": "IDEA-002"}
    assert "must be absent" in "".join(
        SW._front_matter_problems({**fm, "source_request": "REQ-01234567"}, "SPEC-0002", plain))
