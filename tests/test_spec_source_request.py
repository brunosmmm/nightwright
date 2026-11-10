"""SPEC-0535: specs cite the public request they come from."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
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
    fixture = ROOT / "docs/specs/9998-source-request-fixture.md"
    try:
        fixture.write_text(_valid_spec_text("REQ-01234567").replace("SPEC-0999", "SPEC-9998")
                           .replace("0999-fixture", "9998-source-request-fixture"))
        subprocess.run([sys.executable, "tools/spec_lint.py", "--write-ledger"], cwd=ROOT,
                       check=True, capture_output=True, text=True)
        r = subprocess.run([sys.executable, "tools/spec_lint.py"], cwd=ROOT,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr or r.stdout
    finally:
        fixture.unlink(missing_ok=True)
        subprocess.run([sys.executable, "tools/spec_lint.py", "--write-ledger"], cwd=ROOT,
                       check=False, capture_output=True, text=True)


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
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    blob_before = _show_backlog(repo, f"{req}.md")
    created_line = [ln for ln in blob_before.decode().splitlines() if ln.startswith("created:")][0]
    tip_before = _backlog_tip(repo)
    sibling = _seed_idea(monkeypatch, cfg, text="Sibling", summary="other")
    _publish(monkeypatch, cfg, sibling)
    sib_id = W.resolve_selector(cfg, sibling.properties["ID"]).properties["REQ"]
    sib_sha = _git(repo, "rev-parse", f"refs/heads/nw-backlog:{sib_id}.md").strip()
    head_before = _git(repo, "symbolic-ref", "HEAD").strip()
    git_calls = []

    def spy(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and Path(cmd[2]).resolve() == repo.resolve():
            git_calls.append(cmd[3:])
        return _REAL_SUBPROCESS_RUN(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    path, spec_id = S.scaffold_from_idea(cfg, idea.properties["ID"])
    text = path.read_text()
    assert f"source_request: {req}" in text
    assert "secret log" not in text
    assert "Promoted from request" in text and "Public words" in text
    req_base = W.resolve_selector(cfg, idea.properties["ID"]).properties["REQ_BASE"]
    assert W.resolve_selector(cfg, idea.properties["ID"]).properties.get("REQ_BASE") == req_base
    assert W.resolve_selector(cfg, idea.properties["ID"]).state == "SPECCED"
    cited = _show_backlog(repo, f"{req}.md")
    fm, body = split_frontmatter(cited.decode())
    assert fm["spec"] == spec_id
    assert [ln for ln in cited.decode().splitlines() if ln.startswith("created:")][0] == created_line
    assert body == "Public words"
    assert _git(repo, "rev-parse", f"refs/heads/nw-backlog:{sib_id}.md").strip() == sib_sha
    path_epic, _spec_epic = S.scaffold_from_idea(cfg, sibling.properties["ID"], epic=True)
    assert f"source_request: {sib_id}" in path_epic.read_text()
    assert _git(repo, "symbolic-ref", "HEAD").strip() == head_before
    cite_cmds = [c for c in git_calls if c and c[0] in ("hash-object", "mktree", "commit-tree", "update-ref")]
    assert cite_cmds
    assert f"cite {req} {spec_id}" in _git(repo, "log", "--format=%s", "refs/heads/nw-backlog").splitlines()
    agreed = L.load_agreed_request(cfg, W.resolve_selector(cfg, idea.properties["ID"]))
    assert agreed and agreed.spec == spec_id
    L.cite_spec(cfg, agreed, spec_id)
    assert _backlog_tip(repo) == _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()


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

    subprocess.run(["git", "-C", str(repo), "config", "--unset", "user.name"], check=True)
    refuse("user.name")

    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
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
    with pytest.raises(ValueError, match="already cites"):
        S.scaffold_from_idea(cfg, idea.properties["ID"])


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
    idea = _publish(monkeypatch, cfg, idea)
    req = idea.properties["REQ"]
    blob_before = _show_backlog(nw, f"{req}.md")
    path, _, _kind = S.promote_idea(cfg, idea.properties["ID"], internal=True)
    assert f"source_request: {req}" in path.read_text()
    assert _show_backlog(nw, f"{req}.md") == blob_before
    load_calls = []
    real = L.load_agreed_request

    def spy_load(cfg, task):
        load_calls.append(1)
        return real(cfg, task)

    monkeypatch.setattr(L, "load_agreed_request", spy_load)
    acme = _seed_idea(monkeypatch, cfg, text="Out", project="acme", summary="out")
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

    def boom_state(cfg_a, task, state):
        raise OSError("disk full")

    monkeypatch.setattr(W, "set_state", boom_state)
    with pytest.raises(ValueError, match="restored refs/heads/nw-backlog"):
        S.scaffold_from_idea(cfg, idea_id)
    after = W.resolve_selector(cfg, idea_id)
    assert not (after.properties.get("SPEC") or "").strip()
    assert _backlog_tip(repo) == tip

    idea = _publish(monkeypatch, cfg, _seed_idea(monkeypatch, cfg, text="Cas"))
    tip_old = _backlog_tip(repo)
    main = _git(repo, "rev-parse", "main").strip()

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


def test_write_rules_and_front_matter_require_source_request():
    idea = {"id": "IDEA-001", "source_request": "REQ-01234567", "summary": "pub"}
    rules = __import__("nightwright.agentic.spec_write", fromlist=["write_rules"]).write_rules(
        "write", {"path": "docs/specs/0002-x.md", "number": 2}, idea, None)
    assert "`source_request: REQ-01234567`" in rules
    assert "public summary" in rules
    from nightwright.agentic import spec_write as SW

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
