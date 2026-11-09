"""SPEC-0480: publish a private idea to refs/heads/nw-backlog and record :REQ: / :REQ_BASE:."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import idea_link as L
from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import Task, load_tasks
from nightwright.specmeta import split_frontmatter

ROOT = Path(__file__).resolve().parent.parent
PINNED = "9700eaa1f712c11423e0542ae8882ba96653f4575be89e3f481d05df79eff208"
VEC2 = "a0d1682323b9536aa06d0ce3c5a21ab22ac8f0be7cf24906aa88df17bc50c45d"
VEC3 = "f8e7c032715c4691e8f0e4bfa4fae7b80f449f9fcae4c077b00d1bb1dd9fa654"
_REQ_RE = re.compile(r"^REQ-[0-9A-HJKMNP-TV-Z]{8}$")
_NUL = "0000000000000000000000000000000000000000"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
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


def _cfg(tmp_path, *, repo: Path, nw_checkout: Path | None = None, project: str = "Example"):
    org = tmp_path / "org"
    org.mkdir(exist_ok=True)
    conf = tmp_path / "config"
    conf.mkdir(exist_ok=True)
    (conf / "mappings.yaml").write_text(
        f"{project}:\n  bucket: {project}\nmeta:\n  bucket: Meta-Tools\n"
    )
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
        "specs_dir": str(tmp_path / "specs"),
        "outbox_targets": {
            project: {"repo_path": str(repo)},
            "Meta-Tools": {"repo_path": str(tmp_path / "wrong-meta")},
        },
    }
    if nw_checkout is not None:
        cfg["nw_checkout"] = str(nw_checkout)
    Path(cfg["data_dir"]).mkdir(parents=True, exist_ok=True)
    return cfg


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


def _show_backlog(repo: Path, path: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/heads/nw-backlog:{path}"],
        check=True,
        capture_output=True,
    ).stdout


def _backlog_tip(repo: Path) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "refs/heads/nw-backlog"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def test_req_base_known_vectors():
    assert L.req_base("  Link  ", " \n Hello \n ") == PINNED
    assert L.req_base("Link", "Hello") == PINNED
    assert L.req_base("Link", "Hello\n") == PINNED
    assert L.req_base("Link", "Hello\r\nworld\r") == VEC2
    assert L.req_base("Link", " \r\n ") == VEC3
    assert L.canonical_pair("Link  two", "say **bold**") == ("Link  two", "say **bold**")
    digest_bold = L.req_base("Link  two", "say **bold**")
    body_bold = L.render_request(
        req_id="REQ-00000000",
        title="Link  two",
        summary="say **bold**",
        status="open",
        created="2026-11-08T12:00:00Z",
        updated="2026-11-08T12:00:00Z",
        created_by="T <t@t>",
        updated_by="T <t@t>",
    )
    fm_b, rest_b = split_frontmatter(body_bold.decode())
    assert fm_b["title"] == "Link  two"
    assert rest_b == "say **bold**"
    assert digest_bold == L.req_base(fm_b["title"], rest_b)


def test_req_base_on_published_idea(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    monkeypatch.chdir(_init_repo(tmp_path / "cwd"))
    idea = _seed_idea(
        monkeypatch,
        cfg,
        text="Link",
        tags=("session-triage",),
    )
    assert idea.heading == "Link"
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code == 0
    fresh = W.resolve_selector(cfg, idea.properties["ID"])
    assert fresh.properties.get("REQ_BASE") == PINNED


def test_request_file_layout_and_invalid_file():
    body = L.render_request(
        req_id="REQ-01234567",
        title='say "hi"',
        summary="",
        status="open",
        created="2026-11-08T12:00:00Z",
        updated="2026-11-08T12:00:00Z",
        created_by="Test User <test@example.com>",
        updated_by="Test User <test@example.com>",
    )
    text = body.decode("utf-8")
    assert text.startswith("---\n")
    header, summary = text.split("---\n", 2)[1:]
    yaml_lines = header.splitlines()
    assert yaml_lines == [
        "id: REQ-01234567",
        'title: "say \\"hi\\""',
        "status: open",
        "created: 2026-11-08T12:00:00Z",
        "updated: 2026-11-08T12:00:00Z",
        'created_by: "Test User <test@example.com>"',
        'updated_by: "Test User <test@example.com>"',
    ]
    assert "spec:" not in text
    assert summary == ""
    assert text.endswith("---\n")

    fm, rest = split_frontmatter(text)
    assert fm["title"] == 'say "hi"'
    assert fm["created_by"] == "Test User <test@example.com>"
    assert fm["updated_by"] == "Test User <test@example.com>"
    assert rest == ""

    body2 = L.render_request(
        req_id="REQ-01234567",
        title="a\\b",
        summary="x",
        status="open",
        created="2026-11-08T12:00:00Z",
        updated="2026-11-08T12:01:00Z",
        created_by="N <e>",
        updated_by="N <e>",
    )
    fm2, rest2 = split_frontmatter(body2.decode())
    assert fm2["title"] == "a\\b"
    assert rest2 == "x"

    jane = L.render_request(
        req_id="REQ-01234567",
        title="t",
        summary="",
        status="open",
        created="2026-11-08T12:00:00Z",
        updated="2026-11-08T12:00:00Z",
        created_by="Jane #2 <j@example.com>",
        updated_by="Jane: Doe <j@example.com>",
    )
    fm_j, _ = split_frontmatter(jane.decode())
    assert fm_j["created_by"] == "Jane #2 <j@example.com>"
    assert fm_j["updated_by"] == "Jane: Doe <j@example.com>"

    again = L.render_request(
        req_id="REQ-01234567",
        title="t",
        summary="",
        status="open",
        created="2026-11-08T12:00:00Z",
        updated="2026-11-08T12:00:00Z",
        created_by="N <e>",
        updated_by="N <e>",
    )
    assert "created: 2026-11-08T12:00:00Z\n" in again.decode()

    assert split_frontmatter("---\na: b\n---") == (None, "---\na: b\n---")


def test_publish_uses_the_project_repo_not_cwd(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cwd = _init_repo(tmp_path / "cwd")
    nw = _nw_checkout(tmp_path)
    cfg = _cfg(tmp_path, repo=repo, nw_checkout=nw, project="Meta-Tools")
    cfg["outbox_targets"]["Meta-Tools"]["repo_path"] = str(repo)
    idea = _seed_idea(monkeypatch, cfg, project="Meta-Tools")
    monkeypatch.chdir(cwd)
    calls = []

    def fake_root(cfg, project):
        calls.append(project)
        raise AssertionError("project_root should not run for Meta-Tools")

    monkeypatch.setattr("nightwright.agentic.project.project_root", fake_root)
    head_before = _git(cwd, "symbolic-ref", "HEAD").strip()
    index_before = (cwd / ".git" / "index").read_bytes()
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code == 0
    assert _git(nw, "rev-parse", "--verify", "refs/heads/nw-backlog")
    assert _git(cwd, "symbolic-ref", "HEAD").strip() == head_before
    assert (cwd / ".git" / "index").read_bytes() == index_before
    assert calls == []


def test_publish_refuses_without_a_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    W.clear_property(cfg, idea, "PROJECT")
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "project is required" in r.output
    fresh = W.resolve_selector(cfg, idea.properties["ID"])
    assert not fresh.properties.get("REQ")
    assert _backlog_tip(repo) is None

    root_calls = []

    def spy_root(cfg, project):
        root_calls.append(project)
        raise ValueError("project is required")

    with pytest.raises(ValueError, match="project is required"):
        L.resolve_linked_repo(cfg, "")
    assert root_calls == []

    cfg2 = _cfg(tmp_path, repo=repo, project="Unknown")
    cfg2["outbox_targets"] = {"Unknown": {"repo_path": ""}}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg2)
    idea2 = _seed_idea(monkeypatch, cfg2, project="Unknown")
    r = CliRunner().invoke(cli, ["idea", "publish", idea2.properties["ID"]])
    assert r.exit_code != 0
    assert "cannot resolve a repo for project " in r.output and "Unknown" in r.output
    assert not W.resolve_selector(cfg2, idea2.properties["ID"]).properties.get("REQ")

    bad = _init_repo(tmp_path / "bad")
    subprocess.run(["git", "-C", str(bad), "config", "--local", "--unset", "user.name"],
                   check=True)
    cfg3 = _cfg(tmp_path, repo=bad)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg3)
    idea3 = _seed_idea(monkeypatch, cfg3, project="Example")
    r = CliRunner().invoke(cli, ["idea", "publish", idea3.properties["ID"]])
    assert r.exit_code != 0
    assert "user.name" in r.output
    assert _backlog_tip(bad) is None

    subprocess.run(["git", "-C", str(bad), "config", "--local", "user.name", "Test User"],
                   check=True)
    subprocess.run(["git", "-C", str(bad), "config", "--local", "--unset", "user.email"],
                   check=True)
    r = CliRunner().invoke(cli, ["idea", "publish", idea3.properties["ID"]])
    assert r.exit_code != 0
    assert "user.email" in r.output
    assert _backlog_tip(bad) is None

    checkout_repo = _init_repo(tmp_path / "checked-out")
    cfg4 = _cfg(tmp_path, repo=checkout_repo)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg4)
    idea4 = _seed_idea(monkeypatch, cfg4)
    tip_before = _git(checkout_repo, "rev-parse", "main").strip()
    subprocess.run(["git", "-C", str(checkout_repo), "branch", "nw-backlog", "main"], check=True)
    subprocess.run(["git", "-C", str(checkout_repo), "symbolic-ref", "HEAD",
                      "refs/heads/nw-backlog"], check=True)
    git_calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if cmd[:3] == ["git", "-C", str(checkout_repo)] and cmd[3] == "hash-object":
            git_calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["idea", "publish", idea4.properties["ID"]])
    assert r.exit_code != 0
    assert "refusing to update nw-backlog while it is checked out" in r.output
    assert not git_calls
    assert _git(checkout_repo, "rev-parse", "refs/heads/nw-backlog").strip() == tip_before


def test_publish_links_without_changing_state_or_sections(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, state="INCUBATE")
    org_before = Path(cfg["org_ideas_file"]).read_text()
    git_calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if cmd[:3] == ["git", "-C", str(repo)]:
            git_calls.append(cmd[3:])
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code == 0
    assert r.output == f"{idea.properties['ID']}\n"
    fresh = W.resolve_selector(cfg, idea.properties["ID"])
    assert fresh.state == "INCUBATE"
    req = fresh.properties["REQ"]
    assert _REQ_RE.match(req)
    assert fresh.properties.get("REQ_BASE") == PINNED
    after = Path(cfg["org_ideas_file"]).read_text()
    assert ":REQ:" in after and ":REQ:" not in org_before
    assert ":REQ_BASE:" in after and ":REQ_BASE:" not in org_before
    assert "** Summary" in after and "Hello" in after
    assert ":session-triage:" in after
    assert "** Open questions" in after
    assert "** Log" in after
    blob = _show_backlog(repo, f"{req}.md")
    fm, body = split_frontmatter(blob.decode())
    assert fm["status"] == "open"
    assert fm["title"] == "Link"
    assert body == "Hello"
    assert "spec" not in fm
    assert idea.properties["ID"] not in blob.decode()
    upd = [c for c in git_calls if c and c[0] == "update-ref"]
    assert upd and upd[0][-1] == _NUL
    assert any(c[0] == "hash-object" for c in git_calls)
    assert any(c[0] == "mktree" for c in git_calls)
    assert any(c[0] == "commit-tree" for c in git_calls)
    forbidden = {"checkout", "switch", "add", "fetch", "push"}
    assert not any(c[0] in forbidden for c in git_calls)
    log = _git(
        repo,
        "log",
        "-1",
        "--format=%s|%an|%ae|%cn|%ce",
        "refs/heads/nw-backlog",
    ).strip()
    subj, an, ae, cn, ce = log.split("|")
    assert subj == f"publish {req}"
    assert an == "Test User" and ae == "test@example.com"
    assert cn == "Test User" and ce == "test@example.com"
    head_before = _git(repo, "symbolic-ref", "HEAD").strip()
    assert "refs/heads/main" in head_before


def test_second_publish_preserves_existing_requests(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    a = _seed_idea(monkeypatch, cfg, text="First", summary="one")
    b = _seed_idea(monkeypatch, cfg, text="Second", summary="two")
    CliRunner().invoke(cli, ["idea", "publish", a.properties["ID"]])
    first = W.resolve_selector(cfg, a.properties["ID"]).properties["REQ"]
    first_sha = _git(repo, "rev-parse", f"refs/heads/nw-backlog:{first}.md").strip()
    tip1 = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()
    git_calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if len(cmd) > 3 and cmd[0] == "git" and str(repo) in cmd[2]:
            git_calls.append(cmd[3:])
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    CliRunner().invoke(cli, ["idea", "publish", b.properties["ID"]])
    assert _git(repo, "rev-parse", f"refs/heads/nw-backlog:{first}.md").strip() == first_sha
    tip2 = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()
    assert _git(repo, "rev-parse", f"{tip2}^").strip() == tip1
    upd = [c for c in git_calls if c and c[0] == "update-ref" and c[1] == "refs/heads/nw-backlog"]
    assert upd and upd[0][3] == tip1


def test_publish_refuses_empty_title_multiline_title_and_second_link(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    tip_at_start = _backlog_tip(repo)

    CliRunner().invoke(cli, ["add", "plain task"])
    task = [t for t in load_tasks(cfg) if not t.is_idea][0]
    r = CliRunner().invoke(cli, ["idea", "publish", task.heading])
    assert r.exit_code != 0
    assert "is not an idea" in r.output
    assert _backlog_tip(repo) == tip_at_start

    idea_empty = _seed_idea(monkeypatch, cfg, text="Blank")
    empty_task = W.resolve_selector(cfg, idea_empty.properties["ID"])
    empty_task = Task(
        id=empty_task.id,
        heading="   ",
        state=empty_task.state,
        is_done=empty_task.is_done,
        tags=empty_task.tags,
        priority=empty_task.priority,
        properties=dict(empty_task.properties),
        level=empty_task.level,
        file=empty_task.file,
        line=empty_task.line,
        is_idea=True,
    )
    with pytest.raises(ValueError, match="title is empty"):
        L.publish_idea(cfg, empty_task)
    assert _backlog_tip(repo) == tip_at_start

    idea_ml = _seed_idea(monkeypatch, cfg, text="Multi")
    ml_task = W.resolve_selector(cfg, idea_ml.properties["ID"])
    ml_task = Task(
        id=ml_task.id,
        heading="Multi\nline",
        state=ml_task.state,
        is_done=ml_task.is_done,
        tags=ml_task.tags,
        priority=ml_task.priority,
        properties=dict(ml_task.properties),
        level=ml_task.level,
        file=ml_task.file,
        line=ml_task.line,
        is_idea=True,
    )
    with pytest.raises(ValueError, match="title must be one line"):
        L.publish_idea(cfg, ml_task)
    assert _backlog_tip(repo) == tip_at_start

    idea_ok = _seed_idea(monkeypatch, cfg, text="PublishMe")
    tip_before_link = _backlog_tip(repo)
    CliRunner().invoke(cli, ["idea", "publish", idea_ok.properties["ID"]])
    tip_after_first = _backlog_tip(repo)
    assert tip_after_first != tip_before_link
    r = CliRunner().invoke(cli, ["idea", "publish", idea_ok.properties["ID"]])
    assert r.exit_code != 0
    assert "already linked" in r.output
    assert _backlog_tip(repo) == tip_after_first

    idea2 = _seed_idea(monkeypatch, cfg, text="Another")
    real_run = subprocess.run
    tip_before = _backlog_tip(repo)

    def fail_ref(cmd, **kw):
        if (len(cmd) >= 6 and cmd[0] == "git" and cmd[3] == "update-ref"
                and cmd[4] == "refs/heads/nw-backlog"):
            return mock.Mock(returncode=1, stdout=b"", stderr=b"")
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", fail_ref)
    r = CliRunner().invoke(cli, ["idea", "publish", idea2.properties["ID"]])
    assert r.exit_code != 0
    fresh = W.resolve_selector(cfg, idea2.properties["ID"])
    assert not fresh.properties.get("REQ")
    assert not fresh.properties.get("REQ_BASE")
    assert _backlog_tip(repo) == tip_before


def test_publish_org_write_failure_restores_the_old_tip(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, text="Rollback")
    org_before = Path(cfg["org_ideas_file"]).read_text()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("nightwright.org_write.link_idea_request", boom)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "REQ-" in r.output
    assert "restored refs/heads/nw-backlog" in r.output
    assert _backlog_tip(repo) is None
    assert Path(cfg["org_ideas_file"]).read_text() == org_before
    assert not W.resolve_selector(cfg, idea.properties["ID"]).properties.get("REQ")


def test_publish_org_write_failure_restores_existing_tip(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea_a = _seed_idea(monkeypatch, cfg, text="KeepTip")
    assert CliRunner().invoke(cli, ["idea", "publish", idea_a.properties["ID"]]).exit_code == 0
    req_a = W.resolve_selector(cfg, idea_a.properties["ID"]).properties["REQ"]
    tip_old = _backlog_tip(repo)

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("nightwright.org_write.link_idea_request", boom)
    idea_b = _seed_idea(monkeypatch, cfg, text="FailOrg")
    org_mid = Path(cfg["org_ideas_file"]).read_text()
    r = CliRunner().invoke(cli, ["idea", "publish", idea_b.properties["ID"]])
    assert r.exit_code != 0
    assert req_a in r.output or "REQ-" in r.output
    assert "restored refs/heads/nw-backlog" in r.output
    assert _backlog_tip(repo) == tip_old
    assert Path(cfg["org_ideas_file"]).read_text() == org_mid
    assert not W.resolve_selector(cfg, idea_b.properties["ID"]).properties.get("REQ")


def test_publish_org_write_failure_leaves_ref_when_cas_lost(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    main = _git(repo, "rev-parse", "main").strip()

    def boom_and_move(cfg, task, req_id, req_base):
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/heads/nw-backlog", main],
            check=True,
        )
        raise OSError("disk full")

    monkeypatch.setattr("nightwright.org_write.link_idea_request", boom_and_move)
    idea2 = _seed_idea(monkeypatch, cfg, text="Race")
    r = CliRunner().invoke(cli, ["idea", "publish", idea2.properties["ID"]])
    assert r.exit_code != 0
    assert "no longer points at" in r.output
    assert _git(repo, "rev-parse", "refs/heads/nw-backlog").strip() == main


def test_publish_refuses_when_summary_read_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, text="Drift")
    monkeypatch.setattr(
        "nightwright.idea_link.idea_summary_text",
        mock.Mock(side_effect=ValueError("drift")),
    )
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "could not read Summary" in r.output
    assert _backlog_tip(repo) is None
    assert not W.resolve_selector(cfg, idea.properties["ID"]).properties.get("REQ")


def test_heading_without_level_matches_only_task_line():
    lines = [
        "* INCUBATE  Link :tag:\n",
        ":PROPERTIES:\n",
        ":ID: IDEA-OTHER\n",
        ":END:\n",
        "** Summary\n",
        "* INCUBATE  Link :tag:\n",
    ]
    task = Task(
        id="IDEA-001",
        heading="Link",
        state="INCUBATE",
        is_done=False,
        tags=frozenset(),
        priority=None,
        properties={"ID": "IDEA-001"},
        level=None,
        file="f.org",
        line=1,
        is_idea=True,
    )
    assert W._is_same_headline(task, lines, 0)
    assert not W._is_same_headline(task, lines, 5)
    assert W._locate_headline(task, lines) == 0

    lines2 = list(lines)
    lines2[5] = "* INCUBATE  Other :tag:\n"
    task_diff = Task(
        id="IDEA-001",
        heading="Link",
        state="INCUBATE",
        is_done=False,
        tags=frozenset(),
        priority=None,
        properties={"ID": "IDEA-001"},
        level=None,
        file="f.org",
        line=1,
        is_idea=True,
    )
    assert W._locate_headline(task_diff, lines2) == 0

    task2 = Task(
        id="f.org:6",
        heading="Link",
        state="INCUBATE",
        is_done=False,
        tags=frozenset(),
        priority=None,
        properties={},
        level=1,
        file="f.org",
        line=6,
        is_idea=True,
    )
    assert W._is_same_headline(task2, lines, 5)
    task3 = Task(
        id="IDEA-002",
        heading="Link",
        state="INCUBATE",
        is_done=False,
        tags=frozenset(),
        priority=None,
        properties={"ID": "IDEA-002"},
        level=1,
        file="f.org",
        line=6,
        is_idea=True,
    )
    lines3 = list(lines)
    lines3[6:6] = [":PROPERTIES:\n", ":ID: IDEA-002\n", ":END:\n"]
    assert W._is_same_headline(task3, lines3, 5)


def test_ideas_and_hub_keep_idea_row(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    _seed_idea(monkeypatch, cfg)
    git_calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if cmd[:2] == ["git", "-C"] and "nw-backlog" in " ".join(cmd):
            git_calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["ideas", "--json"])
    assert r.exit_code == 0
    assert not git_calls
    r2 = CliRunner().invoke(cli, ["hub", "--json"])
    assert r2.exit_code == 0
    assert not git_calls

    import inspect

    from nightwright.explore import idea_show_payload

    assert len(inspect.signature(idea_show_payload).parameters) == 2
    from nightwright import report as RP
    from nightwright.tui import model as TM
    from nightwright.agentic import queue as Q
    from nightwright.agentic import spec_review as SR

    src_rp = inspect.getsource(RP)
    src_tm = inspect.getsource(TM)
    src_q = inspect.getsource(Q)
    src_sr = inspect.getsource(SR)
    assert "idea_row" in src_rp
    assert "_row_for" in src_tm
    assert "idea_show_payload" in src_tm
    assert re.search(r"idea_show_payload\s*\(", src_q)
    assert re.search(r"idea_show_payload\s*\(", src_sr)
    assert "link=True" not in src_q
    assert "link=True" not in src_sr
