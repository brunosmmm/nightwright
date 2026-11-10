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

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
PINNED = "9700eaa1f712c11423e0542ae8882ba96653f4575be89e3f481d05df79eff208"
VEC2 = "a0d1682323b9536aa06d0ce3c5a21ab22ac8f0be7cf24906aa88df17bc50c45d"
VEC3 = "f8e7c032715c4691e8f0e4bfa4fae7b80f449f9fcae4c077b00d1bb1dd9fa654"
_REQ_RE = re.compile(r"^REQ-[0-9A-HJKMNP-TV-Z]{8}$")
_NUL = "0000000000000000000000000000000000000000"
_REAL_SUBPROCESS_RUN = subprocess.run


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

    import nightwright.agentic.project as AP

    real_project_root = AP.project_root
    monkeypatch.setattr(AP, "project_root", spy_root)
    with pytest.raises(ValueError, match="project is required"):
        L.resolve_linked_repo(cfg, "")
    assert root_calls == []
    monkeypatch.setattr(AP, "project_root", real_project_root)

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
    hash_calls = []

    def spy_hash(cmd, **kw):
        if cmd[:3] == ["git", "-C", str(bad)] and len(cmd) > 3 and cmd[3] == "hash-object":
            hash_calls.append(cmd)
        return _REAL_SUBPROCESS_RUN(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy_hash)
    r = CliRunner().invoke(cli, ["idea", "publish", idea3.properties["ID"]])
    assert r.exit_code != 0
    assert "user.name" in r.output
    assert _backlog_tip(bad) is None
    assert not hash_calls

    subprocess.run(["git", "-C", str(bad), "config", "--local", "user.name", "Test User"],
                   check=True)
    subprocess.run(["git", "-C", str(bad), "config", "--local", "--unset", "user.email"],
                   check=True)
    hash_calls.clear()
    r = CliRunner().invoke(cli, ["idea", "publish", idea3.properties["ID"]])
    assert r.exit_code != 0
    assert "user.email" in r.output
    assert _backlog_tip(bad) is None
    assert not hash_calls

    checkout_repo = _init_repo(tmp_path / "checked-out")
    cfg4 = _cfg(tmp_path, repo=checkout_repo)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg4)
    idea4 = _seed_idea(monkeypatch, cfg4)
    tip_before = _git(checkout_repo, "rev-parse", "main").strip()
    subprocess.run(["git", "-C", str(checkout_repo), "branch", "nw-backlog", "main"], check=True)
    subprocess.run(["git", "-C", str(checkout_repo), "symbolic-ref", "HEAD",
                      "refs/heads/nw-backlog"], check=True)
    git_calls = []

    def spy(cmd, **kw):
        if cmd[:3] == ["git", "-C", str(checkout_repo)] and cmd[3] == "hash-object":
            git_calls.append(cmd)
        return _REAL_SUBPROCESS_RUN(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["idea", "publish", idea4.properties["ID"]])
    assert r.exit_code != 0
    assert "refusing to update nw-backlog while it is checked out" in r.output
    assert not git_calls
    assert _git(checkout_repo, "rev-parse", "refs/heads/nw-backlog").strip() == tip_before

    linked_repo = _init_repo(tmp_path / "linked-wt")
    cfg5 = _cfg(tmp_path, repo=linked_repo)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg5)
    idea5 = _seed_idea(monkeypatch, cfg5)
    wt = tmp_path / "linked-wt-other"
    subprocess.run(
        ["git", "-C", str(linked_repo), "worktree", "add", "-q", "-b", "nw-backlog", str(wt)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(linked_repo), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    hash_calls2 = []

    def spy_wt(cmd, **kw):
        if cmd[:3] == ["git", "-C", str(linked_repo)] and len(cmd) > 3 and cmd[3] == "hash-object":
            hash_calls2.append(cmd)
        return _REAL_SUBPROCESS_RUN(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy_wt)
    r = CliRunner().invoke(cli, ["idea", "publish", idea5.properties["ID"]])
    assert r.exit_code != 0
    assert "refusing to update nw-backlog while it is checked out" in r.output
    assert not hash_calls2


def test_publish_links_without_changing_state_or_sections(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, state="INCUBATE")
    assert CliRunner().invoke(cli, ["idea", "clock-in", idea.properties["ID"]]).exit_code == 0
    idea = W.resolve_selector(cfg, idea.properties["ID"])
    headline_before = idea.heading
    org_before = Path(cfg["org_ideas_file"]).read_text()
    wt_before = _git(repo, "status", "--porcelain").strip()
    index_before = (repo / ".git" / "index").read_bytes()
    git_calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if cmd[:3] == ["git", "-C", str(repo)]:
            git_calls.append(cmd[3:])
        return real_run(cmd, **kw)

    org_writes = []
    real_atomic = W._atomic_backup_write

    def spy_atomic(cfg, path, content):
        org_writes.append(content)
        return real_atomic(cfg, path, content)

    monkeypatch.setattr("nightwright.org_write._atomic_backup_write", spy_atomic)
    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code == 0
    assert r.output == f"{idea.properties['ID']}\n"
    assert len(org_writes) == 1
    assert ":REQ:" in org_writes[0] and ":REQ_BASE:" in org_writes[0]
    fresh = W.resolve_selector(cfg, idea.properties["ID"])
    assert fresh.state == "INCUBATE"
    assert fresh.heading == headline_before
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
    assert "CLOCK:" in after
    assert f"* INCUBATE {headline_before} :session-triage:" in after
    blob = _show_backlog(repo, f"{req}.md")
    fm, body = split_frontmatter(blob.decode())
    assert fm["status"] == "open"
    assert fm["title"] == "Link"
    assert fm["created_by"] == "Test User <test@example.com>"
    assert fm["updated_by"] == "Test User <test@example.com>"
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
    assert _git(repo, "status", "--porcelain").strip() == wt_before
    assert (repo / ".git" / "index").read_bytes() == index_before
    ct = [c for c in git_calls if c and c[0] == "commit-tree"]
    assert ct and "-p" not in ct[0]
    tip_sha = _git(repo, "rev-parse", "refs/heads/nw-backlog").strip()
    parents_line = _git(repo, "rev-list", "--parents", "-n", "1", tip_sha).strip()
    assert parents_line.split() == [tip_sha]


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


def test_second_publish_preserves_backlog_when_linked_path_is_subdir(tmp_path, monkeypatch):
    """repo_path under the work tree must not drop existing root REQ files on mktree (loop-3)."""
    repo = _init_repo(tmp_path / "proj")
    sub = repo / "packages" / "app"
    sub.mkdir(parents=True)
    cfg = _cfg(tmp_path, repo=sub)
    monkeypatch.chdir(sub)
    a = _seed_idea(monkeypatch, cfg, text="First", summary="one")
    b = _seed_idea(monkeypatch, cfg, text="Second", summary="two")
    assert CliRunner().invoke(cli, ["idea", "publish", a.properties["ID"]]).exit_code == 0
    first = W.resolve_selector(cfg, a.properties["ID"]).properties["REQ"]
    first_sha = _git(repo, "rev-parse", f"refs/heads/nw-backlog:{first}.md").strip()
    ls_calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if len(cmd) > 5 and cmd[0] == "git" and str(repo) in cmd[2] and cmd[3] == "ls-tree":
            ls_calls.append(list(cmd[3:]))
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    assert CliRunner().invoke(cli, ["idea", "publish", b.properties["ID"]]).exit_code == 0
    assert any(c[:2] == ["ls-tree", "--full-tree"] for c in ls_calls)
    assert _git(repo, "rev-parse", f"refs/heads/nw-backlog:{first}.md").strip() == first_sha
    second = W.resolve_selector(cfg, b.properties["ID"]).properties["REQ"]
    assert _show_backlog(repo, f"{second}.md").decode()


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
    org_before = Path(cfg["org_ideas_file"]).read_text()
    r = CliRunner().invoke(cli, ["idea", "publish", idea2.properties["ID"]])
    assert r.exit_code != 0
    assert "no longer points at" in r.output
    assert _git(repo, "rev-parse", "refs/heads/nw-backlog").strip() == main
    assert Path(cfg["org_ideas_file"]).read_text() == org_before


def test_publish_refuses_when_summary_read_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, text="Drift")
    org_path = Path(cfg["org_ideas_file"])
    org_path.chmod(0)
    try:
        r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    finally:
        org_path.chmod(0o600)
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

    lines_dup = [
        "* INCUBATE  Link :tag:\n",
        ":PROPERTIES:\n",
        ":ID: IDEA-OTHER\n",
        ":END:\n",
        "* INCUBATE  Link :tag:\n",
        ":PROPERTIES:\n",
        ":ID: IDEA-001\n",
        ":END:\n",
    ]
    assert not W._is_same_headline(task, lines_dup, 4)
    assert W._locate_headline(task, lines_dup) == 0

    task_wrong_line = Task(
        id="IDEA-001",
        heading="Link",
        state="INCUBATE",
        is_done=False,
        tags=frozenset(),
        priority=None,
        properties={"ID": "IDEA-001"},
        level=None,
        file="f.org",
        line=6,
        is_idea=True,
    )
    with pytest.raises(ValueError, match="not found"):
        W._locate_headline(task_wrong_line, lines_dup)

    lines_same_heading = [
        "* INCUBATE  Link :a:\n",
        ":PROPERTIES:\n",
        ":ID: IDEA-A\n",
        ":END:\n",
        "* INCUBATE  Link :b:\n",
        ":PROPERTIES:\n",
        ":ID: IDEA-B\n",
        ":END:\n",
    ]
    task_a = Task(
        id="IDEA-A",
        heading="Link",
        state="INCUBATE",
        is_done=False,
        tags=frozenset(),
        priority=None,
        properties={"ID": "IDEA-A"},
        level=1,
        file="f.org",
        line=1,
        is_idea=True,
    )
    task_b = Task(
        id="IDEA-B",
        heading="Link",
        state="INCUBATE",
        is_done=False,
        tags=frozenset(),
        priority=None,
        properties={"ID": "IDEA-B"},
        level=1,
        file="f.org",
        line=5,
        is_idea=True,
    )
    assert W._is_same_headline(task_a, lines_same_heading, 0)
    assert not W._is_same_headline(task_a, lines_same_heading, 4)
    assert W._is_same_headline(task_b, lines_same_heading, 4)
    assert not W._is_same_headline(task_b, lines_same_heading, 0)

    task_empty_heading = Task(
        id="IDEA-001",
        heading="",
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
    assert not W._is_same_headline(task_empty_heading, lines, 0)

    task_miss = Task(
        id="IDEA-001",
        heading="NoMatch",
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
    assert not W._is_same_headline(task_miss, lines, 0)


def test_ideas_and_hub_keep_idea_row(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    assert CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]]).exit_code == 0
    git_calls = []
    real_run = subprocess.run
    idea_row_calls = []
    from nightwright import report as RP

    real_idea_row = RP.idea_row

    def track_idea_row(cfg, task):
        idea_row_calls.append(task.id)
        return real_idea_row(cfg, task)

    monkeypatch.setattr("nightwright.report.idea_row", track_idea_row)

    def spy(cmd, **kw):
        if cmd[:2] == ["git", "-C"] and "nw-backlog" in " ".join(cmd):
            git_calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["ideas", "--json"])
    assert r.exit_code == 0
    assert idea_row_calls
    assert not git_calls
    idea_row_calls.clear()
    r2 = CliRunner().invoke(cli, ["hub", "--json"])
    assert r2.exit_code == 0
    assert idea_row_calls
    assert not git_calls

    import ast
    import inspect

    from nightwright.explore import idea_show_payload

    assert len(inspect.signature(idea_show_payload).parameters) == 2
    from nightwright.tui import model as TM
    from nightwright.agentic import queue as Q
    from nightwright.agentic import spec_review as SR

    tm_tree = ast.parse(inspect.getsource(TM))

    def _func_body_names(tree, name):
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == name:
                return {
                    (x.id if isinstance(x, ast.Name) else x.attr if isinstance(x, ast.Attribute) else None)
                    for x in ast.walk(n)
                    if isinstance(x, (ast.Name, ast.Attribute))
                }
        return set()

    row_names = _func_body_names(tm_tree, "_row_for")
    assert "idea_show_payload" not in row_names
    assert "next_step_for_idea" not in row_names

    def _show_payload_calls(tree):
        out = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = f.attr if isinstance(f, ast.Attribute) else (
                f.id if isinstance(f, ast.Name) else None
            )
            if name == "idea_show_payload":
                out.append(n)
        return out

    for mod in (Q, SR):
        tree = ast.parse(inspect.getsource(mod))
        calls = _show_payload_calls(tree)
        assert calls, f"expected idea_show_payload call in {mod.__name__}"
        assert all(len(c.args) == 2 and not c.keywords for c in calls)

    load_calls = _show_payload_calls(tm_tree)
    assert load_calls, "expected idea_show_payload call in load_detail"
    assert all(len(c.args) == 2 and not c.keywords for c in load_calls)

    load_names = _func_body_names(tm_tree, "load_detail")
    assert "next_step_for_idea" in load_names


def test_publish_scrubs_git_env_and_writes_project_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    decoy = _init_repo(tmp_path / "decoy")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    git_c_repos = []

    publish_git = {"hash-object", "mktree", "commit-tree", "update-ref",
                   "rev-parse", "config", "symbolic-ref", "worktree", "ls-tree"}

    def spy(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[1] == "-C" and cmd[3] in publish_git:
            assert Path(cmd[2]).resolve() == repo.resolve()
            env = kw.get("env")
            if env is not None:
                for key in (
                    "GIT_DIR",
                    "GIT_WORK_TREE",
                    "GIT_INDEX_FILE",
                    "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                    "GIT_COMMON_DIR",
                    "GIT_NAMESPACE",
                ):
                    assert key not in env
            git_c_repos.append(cmd[3])
        return _REAL_SUBPROCESS_RUN(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code == 0, r.output
    monkeypatch.setattr("subprocess.run", _REAL_SUBPROCESS_RUN)
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    assert _backlog_tip(repo) is not None
    assert _backlog_tip(decoy) is None
    assert "hash-object" in git_c_repos and "update-ref" in git_c_repos


def test_publish_org_write_failure_when_restore_update_ref_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, text="RestoreFail")
    org_before = Path(cfg["org_ideas_file"]).read_text()
    real_run = subprocess.run

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("nightwright.org_write.link_idea_request", boom)

    backlog_ref_updates = []

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

    monkeypatch.setattr("subprocess.run", fail_restore)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "restore failed" in r.output
    assert "restored refs/heads/nw-backlog" not in r.output
    assert Path(cfg["org_ideas_file"]).read_text() == org_before
    tip_after = _backlog_tip(repo)
    assert tip_after is not None
    assert tip_after != _git(repo, "rev-parse", "main").strip()


def test_publish_refuses_control_character_title(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, text="Tabby")
    t = W.resolve_selector(cfg, idea.properties["ID"])
    t = Task(
        id=t.id,
        heading="Tab\x0bhere",
        state=t.state,
        is_done=t.is_done,
        tags=t.tags,
        priority=t.priority,
        properties=dict(t.properties),
        level=t.level,
        file=t.file,
        line=t.line,
        is_idea=True,
    )
    with pytest.raises(ValueError, match="unsupported control character"):
        L.publish_idea(cfg, t)
    assert _backlog_tip(repo) is None


def test_link_idea_request_refuses_when_req_already_set(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, text="HasReq")
    W.set_property(cfg, idea, "REQ", "REQ-ABCDEFGH")
    fresh = W.resolve_selector(cfg, idea.properties["ID"])
    org_before = Path(cfg["org_ideas_file"]).read_bytes()
    with pytest.raises(ValueError, match="already linked"):
        W.link_idea_request(cfg, fresh, "REQ-NEWNEW01", "deadbeef")
    assert Path(cfg["org_ideas_file"]).read_bytes() == org_before
    assert ":REQ_BASE:" not in Path(cfg["org_ideas_file"]).read_text()


def test_publish_restores_backlog_when_link_sees_req_race(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, text="RaceLink")
    tip_before = _backlog_tip(repo)
    real_publish = L._publish_commit

    idea_id = idea.properties["ID"]

    def publish_then_prelink(repo, **kw):
        new_sha, old_tip, blob = real_publish(repo, **kw)
        raced = W.resolve_selector(cfg, idea_id)
        W.set_property(cfg, raced, "REQ", "REQ-RACE0001")
        return new_sha, old_tip, blob

    monkeypatch.setattr("nightwright.idea_link._publish_commit", publish_then_prelink)
    r = CliRunner().invoke(cli, ["idea", "publish", idea_id])
    assert r.exit_code != 0
    assert "restored refs/heads/nw-backlog" in r.output
    assert _backlog_tip(repo) == tip_before
    fresh = W.resolve_selector(cfg, idea_id)
    assert fresh.properties.get("REQ") == "REQ-RACE0001"
    assert ":REQ_BASE:" not in Path(cfg["org_ideas_file"]).read_text()


def test_publish_refuses_when_worktree_list_fails_but_branch_checked_out(
    tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "other-wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "nw-backlog", str(wt)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    real_run = subprocess.run

    def spy(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[3] == "worktree" and cmd[4] == "list":
            return mock.Mock(returncode=1, stdout=b"", stderr=b"")
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "refusing to update nw-backlog while it is checked out" in r.output
    assert _backlog_tip(repo) is None or _git(repo, "rev-parse", "refs/heads/nw-backlog").strip() == (
        _git(repo, "rev-parse", "main").strip()
    )


def test_link_creates_drawer_without_gluing_headline_missing_final_newline(
    tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    org = Path(cfg["org_ideas_file"])
    org.write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED SHIPPED DROPPED\n\n"
        "* INCUBATE NoNL\n"
        "** Summary\n"
        "body\n"
    )
    tasks = load_tasks(cfg)
    idea = next(t for t in tasks if t.is_idea)
    assert ":PROPERTIES:" not in org.read_text()
    W.link_idea_request(cfg, idea, "REQ-NONL0001", "ab" * 32)
    text = org.read_text()
    assert ":PROPERTIES:" in text
    assert "NoNL :PROPERTIES:" not in text
    props_idx = text.index(":PROPERTIES:")
    headline_end = text.index("NoNL") + len("NoNL")
    assert text[headline_end:props_idx].startswith("\n")


def test_publish_refuses_invalid_utf8_git_config(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    hash_calls = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if len(cmd) >= 5 and cmd[0] == "git" and cmd[3] == "config" and cmd[-1] == "user.name":
            return mock.Mock(returncode=0, stdout=b"\xff\xfe", stderr=b"")
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[3] == "hash-object":
            hash_calls.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "not valid UTF-8" in r.output
    assert _backlog_tip(repo) is None
    assert not hash_calls


def test_publish_refuses_control_character_in_user_name(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Bad\tName"],
        check=True,
    )
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "user.name contains unsupported control character" in r.output
    assert _backlog_tip(repo) is None


def test_publish_refuses_del_and_c1_in_title_and_identity(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    for bad, expect in (
        ("Del\x7ftitle", "title contains unsupported control character"),
        ("C1\x90title", "title contains unsupported control character"),
    ):
        idea = _seed_idea(monkeypatch, cfg, text="ok")
        t = W.resolve_selector(cfg, idea.properties["ID"])
        t = Task(
            id=t.id,
            heading=bad,
            state=t.state,
            is_done=t.is_done,
            tags=t.tags,
            priority=t.priority,
            properties=dict(t.properties),
            level=t.level,
            file=t.file,
            line=t.line,
            is_idea=True,
        )
        with pytest.raises(ValueError, match=expect):
            L.publish_idea(cfg, t)
        assert _backlog_tip(repo) is None

    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "bad\x7f@example.com"],
        check=True,
    )
    idea = _seed_idea(monkeypatch, cfg, text="EmailBad")
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "user.email contains unsupported control character" in r.output
    assert _backlog_tip(repo) is None


def test_publish_refuses_multiline_user_identity(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Bad\nName"],
        check=True,
    )
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "user.name must be one line" in r.output
    assert _backlog_tip(repo) is None

    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "bad\n@example.com"],
        check=True,
    )
    idea2 = _seed_idea(monkeypatch, cfg, text="EmailNL")
    r2 = CliRunner().invoke(cli, ["idea", "publish", idea2.properties["ID"]])
    assert r2.exit_code != 0
    assert "user.email must be one line" in r2.output
    assert _backlog_tip(repo) is None


def test_publish_commit_tree_uses_linked_repo_author_env(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    commit_envs = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[3] == "commit-tree":
            commit_envs.append(dict(kw.get("env") or {}))
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Evil Override")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "evil@example.com")
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code == 0, r.output
    assert commit_envs
    env = commit_envs[0]
    assert env.get("GIT_AUTHOR_NAME") == "Test User"
    assert env.get("GIT_AUTHOR_EMAIL") == "test@example.com"
    assert env.get("GIT_COMMITTER_NAME") == "Test User"
    assert env.get("GIT_COMMITTER_EMAIL") == "test@example.com"


def test_publish_org_write_failure_restores_on_keyboard_interrupt(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg, text="KiRestore")
    org_before = Path(cfg["org_ideas_file"]).read_text()

    def ki(*_a, **_k):
        raise KeyboardInterrupt()

    monkeypatch.setattr("nightwright.org_write.link_idea_request", ki)
    task = W.resolve_selector(cfg, idea.properties["ID"])
    with pytest.raises(KeyboardInterrupt):
        L.publish_idea(cfg, task, selector=idea.properties["ID"])
    assert _backlog_tip(repo) is None
    assert Path(cfg["org_ideas_file"]).read_text() == org_before


def test_publish_refuses_when_git_config_read_fails(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    idea = _seed_idea(monkeypatch, cfg)
    real_run = subprocess.run

    def spy(cmd, **kw):
        if len(cmd) >= 6 and cmd[0] == "git" and cmd[3:6] == ["config", "--local", "--get"]:
            return mock.Mock(
                returncode=128,
                stdout=b"",
                stderr=b"fatal: detected dubious ownership",
            )
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    r = CliRunner().invoke(cli, ["idea", "publish", idea.properties["ID"]])
    assert r.exit_code != 0
    assert "git config --local user.name failed" in r.output
    assert _backlog_tip(repo) is None


def test_second_publish_preserves_symlink_and_uses_mktree_missing(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path / "proj")
    cfg = _cfg(tmp_path, repo=repo)
    a = _seed_idea(monkeypatch, cfg, text="First", summary="one")
    b = _seed_idea(monkeypatch, cfg, text="Second", summary="two")
    assert CliRunner().invoke(cli, ["idea", "publish", a.properties["ID"]]).exit_code == 0
    first = W.resolve_selector(cfg, a.properties["ID"]).properties["REQ"]
    tip1 = _backlog_tip(repo)
    target = "README"
    link_sha = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
        input=target + "\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    req_blob = _git(repo, "rev-parse", f"refs/heads/nw-backlog:{first}.md").strip()
    merged = (
        f"100644 blob {req_blob}\t{first}.md\n"
        f"120000 blob {link_sha}\tEXTRA-LINK\n"
    ).encode()
    new_tree = subprocess.run(
        ["git", "-C", str(repo), "mktree"],
        input=merged,
        check=True,
        capture_output=True,
    ).stdout.decode().strip()
    commit = _git(
        repo,
        "commit-tree",
        new_tree,
        "-p",
        tip1,
        "-m",
        "add symlink",
    ).strip()
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/heads/nw-backlog", commit, tip1],
        check=True,
    )
    link_sha_before = _git(repo, "rev-parse", "refs/heads/nw-backlog:EXTRA-LINK").strip()
    mktree_argv = []
    real_run = subprocess.run

    def spy(cmd, **kw):
        if len(cmd) >= 4 and cmd[0] == "git" and cmd[3] == "mktree":
            mktree_argv.append(list(cmd[3:]))
        return real_run(cmd, **kw)

    monkeypatch.setattr("subprocess.run", spy)
    assert CliRunner().invoke(cli, ["idea", "publish", b.properties["ID"]]).exit_code == 0
    assert mktree_argv and "--missing" in mktree_argv[0]
    assert _git(repo, "rev-parse", "refs/heads/nw-backlog:EXTRA-LINK").strip() == link_sha_before
