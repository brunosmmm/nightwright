"""SPEC-0449: orphan ``nw-backlog`` add, edit, close, and reopen."""
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright import backlog as B
from nightwright.cli import cli

REF = "refs/heads/nw-backlog"
ALLOWED = {
    "rev-parse", "rev-list", "ls-tree", "show", "config", "worktree",
    "hash-object", "read-tree", "update-index", "write-tree", "commit-tree", "update-ref",
}
_BASE_LINES = [
    "id: REQ-K7H3MQ2P",
    'title: "Public backlog"',
    "status: open",
    "created: 2026-11-07T15:41:00Z",
    "updated: 2026-11-07T15:41:00Z",
    'created_by: "Ada Lovelace <ada@example.com>"',
    'updated_by: "Ada Lovelace <ada@example.com>"',
]


def git(repo, *args, input=None, check=True, env=None):
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], input=input, capture_output=True, env=env)
    if check and proc.returncode != 0:
        raise AssertionError(
            f"{args} -> {proc.returncode}\n{proc.stderr.decode(errors='replace')}")
    return proc


def init_repo(path, *, commit=True, name="Ada Lovelace", email="ada@example.com"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    if name is not None:
        git(path, "config", "user.name", name)
        git(path, "config", "user.email", email)
    if commit:
        (path / "README").write_text("product\n")
        (path / "src.txt").write_text("src\n")
        git(path, "add", "README", "src.txt")
        git(path, "commit", "-q", "-m", "product")
    return path


def nw(repo, args):
    return CliRunner().invoke(cli, ["backlog", *args, "--repo", str(repo)])


def added(proc):
    assert proc.exit_code == 0, (proc.stdout, proc.stderr, proc.exception)
    assert proc.stderr == ""
    ident = proc.stdout.strip()
    assert proc.stdout == ident + "\n"
    assert ident.startswith("REQ-")
    return ident


def rev(repo, ref="HEAD"):
    proc = git(repo, "rev-parse", "--verify", "--quiet", ref, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def cat(repo, path, ref="nw-backlog"):
    return git(repo, "show", f"{ref}:{path}").stdout


def names(repo, ref="nw-backlog"):
    text = git(repo, "ls-tree", "-r", "--name-only", ref).stdout.decode()
    return [line for line in text.splitlines() if line]


def doc(lines, tail="\nThe summary.\n"):
    return ("---\n" + "\n".join(lines) + "\n---\n" + tail).encode()


def replace(prefix, new, lines=_BASE_LINES):
    return [new if line.startswith(prefix) else line for line in lines]


def without(prefix, lines=_BASE_LINES):
    return [line for line in lines if not line.startswith(prefix)]


VALID = doc(_BASE_LINES)


def commit_tree(repo, files, parent=None, message="plant"):
    """A commit built in a temporary index. ``files`` maps a path to bytes or ``(mode, bytes)``."""
    env = os.environ.copy()
    with tempfile.TemporaryDirectory() as td:
        env["GIT_INDEX_FILE"] = str(Path(td) / "index")
        git(repo, "read-tree", "--empty", env=env)
        for path, spec in files.items():
            if isinstance(spec, tuple):
                mode, data = spec
            else:
                mode, data = "100644", spec
            if isinstance(data, str):
                data = data.encode()
            blob = git(repo, "hash-object", "-w", "--stdin", input=data, env=env).stdout.decode().strip()
            git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
        tree = git(repo, "write-tree", env=env).stdout.decode().strip()
    env.pop("GIT_INDEX_FILE", None)
    args = ["commit-tree", tree]
    if parent:
        args += ["-p", parent]
    args += ["-m", message]
    return git(repo, *args, env=env).stdout.decode().strip()


def point(repo, sha):
    git(repo, "update-ref", REF, sha)


def log_format(repo, fmt, ref="nw-backlog"):
    proc = git(repo, "log", "-1", f"--format={fmt}", ref)
    text = proc.stdout.decode()
    return text[:-1] if text.endswith("\n") else text


def head_state(repo):
    sym = git(repo, "symbolic-ref", "-q", "HEAD", check=False)
    got = git(repo, "rev-parse", "HEAD", check=False)
    if sym.returncode == 0:
        name = sym.stdout.decode().strip()
        if got.returncode != 0:
            return ("unborn", name)
        return ("branch", name, got.stdout.decode().strip())
    return ("detached", got.stdout.decode().strip())


def index_bytes(repo):
    raw = git(repo, "rev-parse", "--git-path", "index").stdout.decode().strip()
    path = Path(raw)
    if not path.is_absolute():
        path = repo / raw
    if not path.is_file():
        return None
    return path.read_bytes()


def git_dir(repo):
    raw = git(repo, "rev-parse", "--git-dir").stdout.decode().strip()
    path = Path(raw)
    return path if path.is_absolute() else (repo / raw)


def extras(repo):
    gd = git_dir(repo)
    return tuple((gd / name).exists() for name in ("MERGE_HEAD", "MERGE_MSG", "CHERRY_PICK_HEAD"))


def worktree_files(repo):
    found = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        if ".git" in rel.parts:
            continue
        found.append(rel.as_posix())
    return sorted(found)


def snapshot(repo):
    """Settle ``git status`` first so a stat refresh is not mistaken for a command write."""
    git(repo, "status", "--porcelain")
    return {
        "head": head_state(repo),
        "index": index_bytes(repo),
        "status": git(repo, "status", "--porcelain").stdout,
        "extras": extras(repo),
        "files": worktree_files(repo),
    }


def assert_allowlist(log):
    for args in log:
        assert args[0] in ALLOWED, args
        assert "-d" not in args
        if args[0] == "update-ref":
            assert args[0:2] == ["update-ref", REF]
            assert len(args) == 4
            assert len(args[2]) == 40 and len(args[3]) == 40
        elif args[0] == "config":
            assert args in (["config", "--get", "user.name"], ["config", "--get", "user.email"])
        elif args[0] == "worktree":
            assert args == ["worktree", "list", "--porcelain", "-z"]


def freeze(monkeypatch, text="2026-11-07T15:41:00.900000+00:00"):
    moment = datetime.fromisoformat(text)
    monkeypatch.setattr(B, "current_time", lambda: moment)
    return moment


@pytest.fixture
def git_log(monkeypatch):
    real = B._git
    log = []

    def wrapped(repo, args, **kwargs):
        log.append(list(args))
        return real(repo, args, **kwargs)

    monkeypatch.setattr(B, "_git", wrapped)
    return log


def _cases():
    """One fixture per validation row, plus the two instants the spec names."""
    valid_tail = "\nThe summary.\n"
    return [
        ("bom", "REQ-K7H3MQ2P.md", b"\xef\xbb\xbf" + VALID, "bom"),
        ("not utf-8", "REQ-K7H3MQ2P.md", b"\xff", "not utf-8"),
        ("cr", "REQ-K7H3MQ2P.md", VALID.replace(b"\n", b"\r\n"), "cr"),
        ("no frontmatter", "REQ-K7H3MQ2P.md", b"hello\n", "no frontmatter"),
        ("invalid yaml", "REQ-K7H3MQ2P.md", b"---\nyes: 1\n---\n", "invalid yaml"),
        ("duplicate key", "REQ-K7H3MQ2P.md",
         doc(["id: REQ-K7H3MQ2P", * _BASE_LINES]), "duplicate key id"),
        ("not a mapping", "REQ-K7H3MQ2P.md", b"---\n- one\n---\n", "not a mapping"),
        ("missing title", "REQ-K7H3MQ2P.md", doc(without("title:")), "missing title"),
        ("bad id", "REQ-K7H3MQ2P.md", doc(replace("id: ", "id: NOPE")), "bad id"),
        ("id mismatch", "REQ-NF6H4D2P.md", VALID, "id mismatch"),
        ("title", "REQ-K7H3MQ2P.md", doc(replace("title: ", 'title: "   "')), "title"),
        ("status", "REQ-K7H3MQ2P.md", doc(replace("status: ", "status: yes")), "status"),
        ("created offset", "REQ-K7H3MQ2P.md",
         doc(replace("created: ", "created: 2026-11-07T10:41:00-05:00")), "created"),
        ("created impossible day", "REQ-K7H3MQ2P.md",
         doc(replace("created: ", "created: 2026-02-31T00:00:00Z")), "created"),
        ("updated", "REQ-K7H3MQ2P.md",
         doc(replace("updated: ", "updated: 2026-11-07T10:41:00-05:00")), "updated"),
        ("created_by", "REQ-K7H3MQ2P.md",
         doc(replace("created_by: ", 'created_by: "   "')), "created_by"),
        ("updated_by", "REQ-K7H3MQ2P.md",
         doc(replace("updated_by: ", 'updated_by: ""')), "updated_by"),
        ("spec", "REQ-K7H3MQ2P.md", doc([*_BASE_LINES, "spec: SPEC-12"], valid_tail), "spec"),
    ]


@pytest.mark.parametrize("name,filename,blob,token", _cases(), ids=[c[0] for c in _cases()])
def test_parse_item_reason_tokens(name, filename, blob, token):
    assert B.parse_item(filename, blob) == token


def test_parse_item_accepts_a_canonical_file():
    parsed = B.parse_item("REQ-K7H3MQ2P.md", VALID)
    assert parsed["summary"] == "The summary.\n"
    assert parsed["spec"] is None
    assert parsed["status"] == "open"


def test_add_writes_orphan_readme_and_req_file(tmp_path, monkeypatch):
    moment = freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    before = snapshot(repo)
    proc = nw(repo, ["add", "--title", "Public backlog", "--summary", "The summary."])
    ident = added(proc)
    assert len(ident) == 12
    assert all(ch in B.ALPHABET for ch in ident[4:])
    assert rev(repo, "nw-backlog^") is None
    assert log_format(repo, "%P") == ""
    assert log_format(repo, "%an") == "Ada Lovelace"
    assert log_format(repo, "%ae") == "ada@example.com"
    assert log_format(repo, "%cn") == "Ada Lovelace"
    assert log_format(repo, "%ce") == "ada@example.com"
    unix = str(int(moment.timestamp()))
    assert log_format(repo, "%at") == unix
    assert log_format(repo, "%ct") == unix
    assert git(repo, "log", "-1", "--format=%B", "nw-backlog").stdout == f"backlog: add {ident}\n".encode()
    assert set(names(repo)) == {"README.md", f"{ident}.md"}
    assert cat(repo, "README.md") == B.README_TEXT.encode()
    blob = cat(repo, f"{ident}.md")
    assert blob.decode() == (
        f"---\n"
        f"id: {ident}\n"
        f"title: \"Public backlog\"\n"
        f"status: open\n"
        f"created: 2026-11-07T15:41:00Z\n"
        f"updated: 2026-11-07T15:41:00Z\n"
        f"created_by: \"Ada Lovelace <ada@example.com>\"\n"
        f"updated_by: \"Ada Lovelace <ada@example.com>\"\n"
        f"---\n"
        f"\n"
        f"The summary.\n"
    )
    assert "spec:" not in blob.decode()
    assert snapshot(repo) == before


def test_second_add_does_not_add_an_index(tmp_path, monkeypatch):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    first = added(nw(repo, ["add", "--title", "One", "--summary", "A"]))
    second = added(nw(repo, ["add", "--title", "Two", "--summary", "B"]))
    assert set(names(repo)) == {"README.md", f"{first}.md", f"{second}.md"}
    diff = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "nw-backlog").stdout.decode().split()
    assert diff == [f"{second}.md"]
    assert f"{first}.md" in names(repo)


def test_existing_readme_bytes_are_left_alone(tmp_path, monkeypatch):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    custom = b"# custom\nnot the fixed text\n"
    sha = commit_tree(repo, {"README.md": custom, "REQ-K7H3MQ2P.md": VALID})
    point(repo, sha)
    before = rev(repo, "nw-backlog:README.md")
    added(nw(repo, ["add", "--title", "Two", "--summary", "B"]))
    assert rev(repo, "nw-backlog:README.md") == before
    assert cat(repo, "README.md") == custom


def test_edit_restores_missing_readme_only(tmp_path, monkeypatch):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    first = added(nw(repo, ["add", "--title", "One", "--summary", "A"]))
    second = added(nw(repo, ["add", "--title", "Two", "--summary", "B"]))
    kept = rev(repo, f"nw-backlog:{second}.md")
    child = commit_tree(repo, {
        f"{first}.md": cat(repo, f"{first}.md"),
        f"{second}.md": cat(repo, f"{second}.md"),
    }, parent=rev(repo, "nw-backlog"), message="drop readme")
    point(repo, child)
    proc = nw(repo, ["edit", first, "--title", "Next"])
    assert proc.exit_code == 0, (proc.stdout, proc.stderr, proc.exception)
    diff = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "nw-backlog")
    assert set(diff.stdout.decode().split()) == {"README.md", f"{first}.md"}
    assert cat(repo, "README.md") == B.README_TEXT.encode()
    assert rev(repo, f"nw-backlog:{second}.md") == kept
    assert f"{first}.md" in names(repo)


def test_commands_do_not_delete_or_rename_items(tmp_path, monkeypatch):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    first = added(nw(repo, ["add", "--title", "One", "--summary", "A"]))
    added(nw(repo, ["add", "--title", "Two", "--summary", "B"]))
    assert nw(repo, ["edit", first, "--title", "Next"]).exit_code == 0
    assert nw(repo, ["close", first]).exit_code == 0
    assert nw(repo, ["reopen", first]).exit_code == 0
    assert git(repo, "log", "--diff-filter=R", "--name-only", "--pretty=format:",
               "nw-backlog").stdout.strip() == b""
    for sha in git(repo, "log", "--format=%H", "nw-backlog").stdout.decode().split():
        status = git(repo, "diff-tree", "--no-commit-id", "--name-status", "-r", "--root", "-M",
                     sha).stdout.decode()
        for line in status.splitlines():
            assert line[:1] not in {"D", "R"}


def test_add_summary_contract(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    missing = nw(repo, ["add", "--title", "T"])
    assert missing.exit_code == 1
    assert missing.stderr == "summary is required\n"
    assert rev(repo, "nw-backlog") is None
    assert not any(args[0] == "hash-object" for args in git_log)

    def tail_of(ident):
        text = cat(repo, f"{ident}.md").decode()
        return text.rpartition("---\n")[2]

    empty = added(nw(repo, ["add", "--title", "Empty", "--summary", ""]))
    assert tail_of(empty) == ""
    blank = added(nw(repo, ["add", "--title", "Blank", "--summary", "\n\n"]))
    assert tail_of(blank) == ""
    spaces = added(nw(repo, ["add", "--title", "Spaces", "--summary", "  "]))
    assert tail_of(spaces) == "\n  \n"
    hello = added(nw(repo, ["add", "--title", "Hello", "--summary", "Hello\r\n"]))
    assert tail_of(hello) == "\nHello\n"
    quoted = added(nw(repo, ["add", "--title", 'a"b\\c', "--summary", "S"]))
    text = cat(repo, f"{quoted}.md").decode()
    assert 'title: "a\\"b\\\\c"' in text
    assert B.parse_item(f"{quoted}.md", text)["title"] == 'a"b\\c'
    padded = added(nw(repo, ["add", "--title", "  padded  ", "--summary", "S"]))
    assert B.parse_item(f"{padded}.md", cat(repo, f"{padded}.md"))["title"] == "  padded  "
    leading = added(nw(repo, ["add", "--title", "Lead", "--summary", "\nAfter newline"]))
    assert tail_of(leading) == "\n\nAfter newline\n"
    linked = added(nw(repo, ["add", "--title", "Spec", "--summary", "S", "--spec", "SPEC-0449"]))
    linked_text = cat(repo, f"{linked}.md").decode()
    keys = [line.split(":", 1)[0] for line in linked_text.split("\n---\n", 1)[0].splitlines()
            if line and line != "---"]
    assert keys[-1] == "spec"
    assert "spec: SPEC-0449" in linked_text
    assert nw(repo, ["edit", hello, "--summary", ""]).exit_code == 0
    assert tail_of(hello) == ""


def test_rejected_arguments_write_nothing(tmp_path, git_log):
    repo = init_repo(tmp_path / "repo")
    seeded = commit_tree(repo, {"README.md": B.README_TEXT, "REQ-K7H3MQ2P.md": VALID})
    point(repo, seeded)
    missing = tmp_path / "missing"
    cases = [
        (["add", "--summary", "S"], "title is required\n"),
        (["add", "--title", "  \t", "--summary", "S"], "title is empty\n"),
        (["add", "--title", "a\nb", "--summary", "S"], "title must be one line\n"),
        (["add", "--title", "a\rb", "--summary", "S"], "title must be one line\n"),
        (["add", "--title", "   "], "summary is required\n"),
        (["add", "--title", "T", "--summary", "S", "--spec", "SPEC-449"], "invalid spec id\n"),
        (["edit", "REQ-K7H3MQ2P", "--spec", "SPEC-449"], "invalid spec id\n"),
        (["edit", "nope"], "invalid id\n"),
        (["edit", "REQ-IIIIIIII"], "invalid id\n"),
        (["edit", "req-k7h3mq2p"], "invalid id\n"),
        (["close", "REQ-K7H3MQ2P.txt"], "invalid id\n"),
        (["edit", "REQ-K7H3MQ2P"], "nothing to change\n"),
        (["edit", "REQ-K7H3MQ2P", "--spec", "SPEC-0449", "--clear-spec", "--repo", str(missing)],
         "spec and clear-spec are mutually exclusive\n"),
    ]
    # The last case carries its own --repo; nw() would append another.
    own_repo = cases[-1]
    for args, message in cases[:-1]:
        git_log.clear()
        proc = nw(repo, args)
        assert proc.exit_code == 1, (args, proc.stdout, proc.stderr, proc.exception)
        assert proc.stderr == message
        assert proc.stdout == ""
        assert git_log == []
        assert rev(repo, "nw-backlog") == seeded
    git_log.clear()
    proc = CliRunner().invoke(cli, ["backlog", *own_repo[0]])
    assert proc.exit_code == 1
    assert proc.stderr == own_repo[1]
    assert git_log == []
    assert rev(repo, "nw-backlog") == seeded


def test_edit_equal_values_do_not_write(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    with_idea = doc([
        "idea: keep-me",
        *_BASE_LINES,
        "spec: SPEC-0449",
    ])
    other_lines = replace("id: ", "id: REQ-NF6H4D2P")
    without_spec = doc(other_lines)
    sha = commit_tree(repo, {
        "README.md": B.README_TEXT,
        "REQ-K7H3MQ2P.md": with_idea,
        "REQ-NF6H4D2P.md": without_spec,
    })
    point(repo, sha)
    same = nw(repo, ["edit", "REQ-K7H3MQ2P", "--title", "Public backlog",
                     "--summary", "The summary.", "--spec", "SPEC-0449"])
    assert same.exit_code == 0, (same.stdout, same.stderr, same.exception)
    assert same.stdout == "" and same.stderr == ""
    cleared = nw(repo, ["edit", "REQ-NF6H4D2P", "--clear-spec"])
    assert cleared.exit_code == 0, (cleared.stdout, cleared.stderr, cleared.exception)
    assert cat(repo, "REQ-K7H3MQ2P.md") == with_idea
    assert b"idea: keep-me" in cat(repo, "REQ-K7H3MQ2P.md")
    assert cat(repo, "REQ-NF6H4D2P.md") == without_spec
    assert rev(repo, "nw-backlog") == sha
    assert not any(args[0] == "hash-object" for args in git_log)


def test_edit_spec_write_keep_and_clear(tmp_path, monkeypatch):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    ident = added(nw(repo, ["add", "--title", "One", "--summary", "S"]))
    assert "spec:" not in cat(repo, f"{ident}.md").decode()
    assert nw(repo, ["edit", ident, "--spec", "SPEC-0449"]).exit_code == 0
    with_spec = cat(repo, f"{ident}.md").decode()
    assert "spec: SPEC-0449" in with_spec
    assert nw(repo, ["edit", ident, "--title", "Two"]).exit_code == 0
    kept = cat(repo, f"{ident}.md").decode()
    assert 'title: "Two"' in kept and "spec: SPEC-0449" in kept
    assert nw(repo, ["edit", ident, "--spec", "SPEC-0001"]).exit_code == 0
    assert "spec: SPEC-0001" in cat(repo, f"{ident}.md").decode()
    assert nw(repo, ["edit", ident, "--clear-spec"]).exit_code == 0
    cleared = cat(repo, f"{ident}.md").decode()
    assert "spec:" not in cleared


def test_edit_drops_unknown_keys_when_a_field_changes(tmp_path, monkeypatch):
    freeze(monkeypatch, "2026-11-08T00:00:00+00:00")
    repo = init_repo(tmp_path / "repo")
    original = doc([
        "idea: keep",
        "IDEA-1019: also",
        "id: REQ-K7H3MQ2P",
        'title: "Public backlog"',
        "status: open",
        "updated: 2026-11-01T00:00:00Z",
        "created: 2026-11-07T15:41:00Z",
        'created_by: "Grace Hopper <grace@example.com>"',
        'updated_by: "Grace Hopper <grace@example.com>"',
    ])
    point(repo, commit_tree(repo, {"README.md": B.README_TEXT, "REQ-K7H3MQ2P.md": original}))
    proc = nw(repo, ["edit", "REQ-K7H3MQ2P.md", "--title", "Next"])
    assert proc.exit_code == 0, (proc.stdout, proc.stderr, proc.exception)
    assert proc.stdout == "" and proc.stderr == ""
    text = cat(repo, "REQ-K7H3MQ2P.md").decode()
    assert "idea" not in text and "IDEA-1019" not in text
    assert text == (
        "---\n"
        "id: REQ-K7H3MQ2P\n"
        'title: "Next"\n'
        "status: open\n"
        "created: 2026-11-07T15:41:00Z\n"
        "updated: 2026-11-08T00:00:00Z\n"
        'created_by: "Grace Hopper <grace@example.com>"\n'
        'updated_by: "Ada Lovelace <ada@example.com>"\n'
        "---\n"
        "\n"
        "The summary.\n"
    )


def test_mutation_refuses_each_invalid_form(tmp_path, git_log):
    repo = init_repo(tmp_path / "repo")
    for _name, filename, blob, token in _cases():
        sha = commit_tree(repo, {"README.md": b"r\n", filename: blob})
        point(repo, sha)
        ident = filename[:-3]
        git_log.clear()
        for args in (["edit", ident, "--title", "Changed"], ["close", ident], ["reopen", ident]):
            proc = nw(repo, args)
            assert proc.exit_code == 1, (args, proc.stderr, proc.exception)
            assert proc.stderr == f"invalid backlog item {ident}: {token}\n"
            assert rev(repo, "nw-backlog") == sha
        assert not any(args[0] == "hash-object" for args in git_log)


def test_malformed_sibling_does_not_block_other_writes(tmp_path, monkeypatch):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    bad = b"not an item\n"
    sha = commit_tree(repo, {
        "README.md": B.README_TEXT,
        "REQ-K7H3MQ2P.md": VALID,
        "REQ-NF6H4D2P.md": bad,
    })
    point(repo, sha)
    sibling = rev(repo, "nw-backlog:REQ-NF6H4D2P.md")
    kept = rev(repo, "nw-backlog:REQ-K7H3MQ2P.md")
    added(nw(repo, ["add", "--title", "More", "--summary", "Body"]))
    assert rev(repo, "nw-backlog:REQ-NF6H4D2P.md") == sibling
    assert rev(repo, "nw-backlog:REQ-K7H3MQ2P.md") == kept
    assert nw(repo, ["edit", "REQ-K7H3MQ2P", "--title", "Next"]).exit_code == 0
    assert rev(repo, "nw-backlog:REQ-NF6H4D2P.md") == sibling
    assert rev(repo, "nw-backlog:REQ-K7H3MQ2P.md") != kept


def test_close_and_reopen_are_single_file_commits(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch, "2026-11-08T00:00:00+00:00")
    repo = init_repo(tmp_path / "repo")
    original = doc([
        "summary: not-the-body",
        "idea: drop-me",
        "status: open",
        'title: "Kept title"',
        'updated_by: "Old <old@example.com>"',
        "id: REQ-K7H3MQ2P",
        'created_by: "Grace Hopper <grace@example.com>"',
        "updated: 2026-11-01T00:00:00Z",
        "created: 2026-11-07T15:41:00Z",
        "spec: SPEC-0449",
    ], tail="\nKept body.\n")
    point(repo, commit_tree(repo, {"README.md": B.README_TEXT, "REQ-K7H3MQ2P.md": original}))
    closed = nw(repo, ["close", "REQ-K7H3MQ2P.md"])
    assert closed.exit_code == 0, (closed.stdout, closed.stderr, closed.exception)
    assert closed.stdout == "" and closed.stderr == ""
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
               "nw-backlog").stdout.decode().split() == ["REQ-K7H3MQ2P.md"]
    assert git(repo, "log", "-1", "--format=%B", "nw-backlog").stdout == b"backlog: close REQ-K7H3MQ2P\n"
    assert cat(repo, "REQ-K7H3MQ2P.md").decode() == (
        "---\n"
        "id: REQ-K7H3MQ2P\n"
        'title: "Kept title"\n'
        "status: closed\n"
        "created: 2026-11-07T15:41:00Z\n"
        "updated: 2026-11-08T00:00:00Z\n"
        'created_by: "Grace Hopper <grace@example.com>"\n'
        'updated_by: "Ada Lovelace <ada@example.com>"\n'
        "spec: SPEC-0449\n"
        "---\n"
        "\n"
        "Kept body.\n"
    )
    tip = rev(repo, "nw-backlog")
    mark = len(git_log)
    again = nw(repo, ["close", "REQ-K7H3MQ2P"])
    assert again.exit_code == 1
    assert again.stderr == "REQ-K7H3MQ2P is already closed\n"
    assert rev(repo, "nw-backlog") == tip
    assert not any(args[0] == "hash-object" for args in git_log[mark:])
    opened = nw(repo, ["reopen", "REQ-K7H3MQ2P"])
    assert opened.exit_code == 0, (opened.stdout, opened.stderr, opened.exception)
    assert cat(repo, "REQ-K7H3MQ2P.md").decode() == (
        "---\n"
        "id: REQ-K7H3MQ2P\n"
        'title: "Kept title"\n'
        "status: open\n"
        "created: 2026-11-07T15:41:00Z\n"
        "updated: 2026-11-08T00:00:00Z\n"
        'created_by: "Grace Hopper <grace@example.com>"\n'
        'updated_by: "Ada Lovelace <ada@example.com>"\n'
        "spec: SPEC-0449\n"
        "---\n"
        "\n"
        "Kept body.\n"
    )
    assert git(repo, "log", "-1", "--format=%B", "nw-backlog").stdout == b"backlog: reopen REQ-K7H3MQ2P\n"
    assert git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r",
               "nw-backlog").stdout.decode().split() == ["REQ-K7H3MQ2P.md"]
    tip = rev(repo, "nw-backlog")
    mark = len(git_log)
    second = nw(repo, ["reopen", "REQ-K7H3MQ2P"])
    assert second.exit_code == 1
    assert second.stderr == "REQ-K7H3MQ2P is already open\n"
    assert rev(repo, "nw-backlog") == tip
    assert not any(args[0] == "hash-object" for args in git_log[mark:])
    missing = nw(repo, ["close", "REQ-NF6H4D2P"])
    assert missing.exit_code == 1
    assert missing.stderr == "REQ-NF6H4D2P is not on nw-backlog\n"
    assert rev(repo, "nw-backlog") == tip


def _isolate(monkeypatch, directory, text=""):
    global_cfg = directory / "global.gitconfig"
    system_cfg = directory / "system.gitconfig"
    global_cfg.write_text(text)
    system_cfg.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(system_cfg))


def test_refuses_unset_or_illegal_identity(tmp_path, monkeypatch, git_log):
    _isolate(monkeypatch, tmp_path)
    cases = [
        ({"user.name": None, "user.email": None},
         "user.name is unset\nuser.email is unset\n"),
        ({"user.name": "   ", "user.email": "ada@example.com"}, "user.name is unset\n"),
        ({"user.name": "Ada Lovelace", "user.email": "   "}, "user.email is unset\n"),
        ({"user.name": "Ada\nLovelace", "user.email": "ada@example.com"},
         "user.name contains a line break\n"),
        ({"user.name": "Ada Lovelace", "user.email": "a\nb@example.com"},
         "user.email contains a line break\n"),
        ({"user.name": "Ada <Lovelace", "user.email": "ada@example.com"},
         "user.name contains '<'\n"),
        ({"user.name": "Ada Lovelace>", "user.email": "ada@example.com"},
         "user.name contains '>'\n"),
        ({"user.name": "Ada <x>", "user.email": "a b@example.com"},
         "user.name contains '<'\nuser.email contains whitespace\n"),
        ({"user.name": "Ada Lovelace", "user.email": 'a"b@example.com'},
         "user.email contains '\"'\n"),
    ]
    for number, (values, message) in enumerate(cases):
        repo = init_repo(tmp_path / f"repo-{number}")
        seeded = commit_tree(repo, {"README.md": b"r\n"})
        point(repo, seeded)
        for key, value in values.items():
            git(repo, "config", "--unset-all", key, check=False)
            if value is not None:
                git(repo, "config", key, value)
        git_log.clear()
        proc = nw(repo, ["add", "--title", "T", "--summary", "S"])
        assert proc.exit_code == 1, (values, proc.stderr, proc.exception)
        assert proc.stderr == message
        assert rev(repo, "nw-backlog") == seeded
        assert not any(args[0] == "hash-object" for args in git_log)


def test_author_follows_local_config_not_environment_or_global(tmp_path, monkeypatch):
    moment = freeze(monkeypatch)
    _isolate(monkeypatch, tmp_path, "[user]\n\tname = Grace Hopper\n\temail = grace@example.com\n")
    for key, value in (
        ("GIT_AUTHOR_NAME", "Env Author"),
        ("GIT_AUTHOR_EMAIL", "env@example.com"),
        ("GIT_AUTHOR_DATE", "1000000000 +0000"),
        ("GIT_COMMITTER_NAME", "Env Committer"),
        ("GIT_COMMITTER_EMAIL", "envc@example.com"),
        ("GIT_COMMITTER_DATE", "1000000000 +0000"),
    ):
        monkeypatch.setenv(key, value)
    repo = init_repo(tmp_path / "local", name='Ada "Lovelace" X', email="ada@example.com")
    ident = added(nw(repo, ["add", "--title", "T", "--summary", "S"]))
    assert log_format(repo, "%an") == 'Ada "Lovelace" X'
    assert log_format(repo, "%ae") == "ada@example.com"
    assert log_format(repo, "%cn") == 'Ada "Lovelace" X'
    assert log_format(repo, "%ce") == "ada@example.com"
    assert log_format(repo, "%at") == str(int(moment.timestamp()))
    assert log_format(repo, "%at") != "1000000000"
    assert log_format(repo, "%ct") == str(int(moment.timestamp()))
    assert log_format(repo, "%ct") != "1000000000"
    assert cat(repo, f"{ident}.md").decode().count(
        'created_by: "Ada \\"Lovelace\\" X <ada@example.com>"') == 1

    other = init_repo(tmp_path / "global", name=None)
    git(other, "config", "--unset-all", "user.name", check=False)
    git(other, "config", "--unset-all", "user.email", check=False)
    ident = added(nw(other, ["add", "--title", "T", "--summary", "S"]))
    assert log_format(other, "%an") == "Grace Hopper"
    assert log_format(other, "%ae") == "grace@example.com"
    assert b'created_by: "Grace Hopper <grace@example.com>"' in cat(other, f"{ident}.md")


def test_mutations_reject_stray_and_bad_mode(tmp_path, git_log):
    repo = init_repo(tmp_path / "repo")
    sha = commit_tree(repo, {
        "notes.txt": b"notes\n",
        "REQ-IIIIIIII.md": b"nope\n",
        "src/main.c": b"int main(){return 0;}\n",
        "run.sh": ("100755", b"#!/bin/sh\n"),
    })
    point(repo, sha)
    problems = [
        "stray: REQ-IIIIIIII.md",
        "stray: notes.txt",
        "mode: run.sh",
        "stray: run.sh",
        "stray: src/main.c",
    ]
    body = "\n".join(problems + [f"root {sha} {line}" for line in problems])
    expected = "refs/heads/nw-backlog is not an orphan backlog\n" + body + "\n"
    commands = [
        ["add", "--title", "T", "--summary", "S"],
        ["edit", "REQ-K7H3MQ2P", "--title", "T"],
        ["close", "REQ-K7H3MQ2P"],
        ["reopen", "REQ-K7H3MQ2P"],
    ]
    for args in commands:
        git_log.clear()
        proc = nw(repo, args)
        assert proc.exit_code == 1, (args, proc.stderr, proc.exception)
        assert proc.stderr == expected
        assert rev(repo, "nw-backlog") == sha
        assert not any(call[0] == "hash-object" for call in git_log)


def test_stray_on_middle_commit_with_clean_tip_and_root(tmp_path, monkeypatch, git_log):
    """Only the tip tree and parentless roots are checked, not every ancestor."""
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    root = commit_tree(repo, {"README.md": B.README_TEXT, "REQ-K7H3MQ2P.md": VALID},
                        message="clean root")
    middle = commit_tree(repo, {"README.md": B.README_TEXT, "REQ-K7H3MQ2P.md": VALID,
                                "notes.txt": b"stray\n"},
                         parent=root, message="stray middle")
    tip = commit_tree(repo, {"README.md": B.README_TEXT, "REQ-K7H3MQ2P.md": VALID},
                      parent=middle, message="clean tip")
    point(repo, tip)
    ident = added(nw(repo, ["add", "--title", "T", "--summary", "S"]))
    assert f"{ident}.md" in names(repo)
    roots = git(repo, "rev-list", "--max-parents=0", "nw-backlog").stdout.decode().split()
    assert roots == [root]


def test_bad_mode_on_legal_readme_and_req(tmp_path, git_log):
    repo = init_repo(tmp_path / "repo")
    sha = commit_tree(repo, {
        "README.md": ("100755", B.README_TEXT),
        "REQ-K7H3MQ2P.md": VALID,
    })
    point(repo, sha)
    expected = (
        "refs/heads/nw-backlog is not an orphan backlog\n"
        "mode: README.md\n"
        f"root {sha} mode: README.md\n"
    )
    proc = nw(repo, ["add", "--title", "T", "--summary", "S"])
    assert proc.exit_code == 1 and proc.stderr == expected
    assert not any(args[0] == "hash-object" for args in git_log)


def test_empty_tree_is_valid_empty_backlog(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    empty = commit_tree(repo, {})
    point(repo, empty)
    ident = added(nw(repo, ["add", "--title", "T", "--summary", "S"]))
    assert f"{ident}.md" in names(repo)


def test_refuses_a_root_that_contains_product_history(tmp_path, git_log):
    repo = init_repo(tmp_path / "repo")
    root = commit_tree(repo, {"src/main.c": b"int main(){return 0;}\n"}, message="product")
    tip = commit_tree(repo, {"README.md": B.README_TEXT, "REQ-K7H3MQ2P.md": VALID},
                      parent=root, message="backlog")
    point(repo, tip)
    expected = (
        "refs/heads/nw-backlog is not an orphan backlog\n"
        f"root {root} stray: src/main.c\n"
    )
    for args in (
        ["add", "--title", "T", "--summary", "S"],
        ["edit", "REQ-K7H3MQ2P", "--title", "T"],
        ["close", "REQ-K7H3MQ2P"],
        ["reopen", "REQ-K7H3MQ2P"],
    ):
        git_log.clear()
        proc = nw(repo, args)
        assert proc.exit_code == 1, (args, proc.stderr)
        assert proc.stderr == expected
        assert rev(repo, "nw-backlog") == tip
        assert not any(call[0] == "hash-object" for call in git_log)


def test_dirty_detached_and_unborn_checkout_stay_put(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "dirty")
    (repo / "README").write_text("staged\n")
    git(repo, "add", "README")
    (repo / "src.txt").write_text("unstaged\n")
    (repo / "extra.txt").write_text("untracked\n")
    before = snapshot(repo)
    ident = added(nw(repo, ["add", "--title", "One", "--summary", "A"]))
    assert snapshot(repo) == before
    assert [args for args in git_log if args[0] == "update-ref"][0][3] == "0" * 40
    (repo / f"{ident}.md").write_text("NOT THE BLOB\n")
    dirty_file = snapshot(repo)
    mark = len(git_log)
    proc = nw(repo, ["edit", ident, "--title", "Next"])
    assert proc.exit_code == 0, (proc.stdout, proc.stderr, proc.exception)
    assert snapshot(repo) == dirty_file
    assert (repo / f"{ident}.md").read_text() == "NOT THE BLOB\n"
    branch = cat(repo, f"{ident}.md")
    assert b"NOT THE BLOB" not in branch
    assert b'title: "Next"' in branch
    shows = [args for args in git_log[mark:] if args[0] == "show"]
    assert any(args[1].endswith(f":{ident}.md") for args in shows)
    assert nw(repo, ["close", ident]).exit_code == 0
    assert snapshot(repo) == dirty_file
    assert nw(repo, ["reopen", ident]).exit_code == 0
    assert snapshot(repo) == dirty_file
    noop = nw(repo, ["edit", ident, "--title", "Next"])
    assert noop.exit_code == 0 and noop.stdout == "" and noop.stderr == ""
    assert snapshot(repo) == dirty_file
    stray = commit_tree(repo, {"notes.txt": b"x\n"})
    point(repo, stray)
    mark = len(git_log)
    refused = nw(repo, ["edit", ident, "--title", "Other"])
    assert refused.exit_code == 1
    assert "not an orphan backlog" in refused.stderr
    assert not any(args[0] == "hash-object" for args in git_log[mark:])
    assert snapshot(repo) == dirty_file
    invalid = commit_tree(repo, {"README.md": b"r\n", f"{ident}.md": b"nope\n"})
    point(repo, invalid)
    mark = len(git_log)
    bad = nw(repo, ["close", ident])
    assert bad.exit_code == 1
    assert bad.stderr == f"invalid backlog item {ident}: no frontmatter\n"
    assert not any(args[0] == "hash-object" for args in git_log[mark:])
    assert snapshot(repo) == dirty_file
    assert extras(repo) == (False, False, False)

    detached = init_repo(tmp_path / "detached")
    git(detached, "checkout", "--detach", "-q")
    (detached / "README").write_text("staged\n")
    git(detached, "add", "README")
    held = snapshot(detached)
    added(nw(detached, ["add", "--title", "D", "--summary", "S"]))
    assert snapshot(detached) == held
    assert head_state(detached)[0] == "detached"

    unborn = init_repo(tmp_path / "unborn", commit=False)
    assert head_state(unborn)[0] == "unborn"
    unborn_index = index_bytes(unborn)
    unborn_head = head_state(unborn)
    mark = len(git_log)
    added(nw(unborn, ["add", "--title", "U", "--summary", "S"]))
    assert index_bytes(unborn) == unborn_index
    assert head_state(unborn) == unborn_head
    assert git(unborn, "status", "--porcelain").stdout == b""
    assert extras(unborn) == (False, False, False)
    creates = [args for args in git_log[mark:] if args[0] == "update-ref"]
    assert len(creates) == 1 and creates[0][3] == "0" * 40
    assert_allowlist(git_log)


def test_refuses_when_nw_backlog_is_checked_out(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    ident = added(nw(repo, ["add", "--title", "T", "--summary", "S"]))
    tip = rev(repo, "nw-backlog")
    other = (tmp_path / "other").resolve()
    git(repo, "worktree", "add", str(other), "nw-backlog")
    porcelain = git(repo, "worktree", "list", "--porcelain").stdout.decode().splitlines()
    checked = []
    path = None
    for line in porcelain:
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == "branch refs/heads/nw-backlog":
            checked.append(path)
    assert checked and all(Path(item).is_absolute() for item in checked)
    expected = "".join(f"nw-backlog is checked out at {item}\n" for item in checked)
    for args in (
        ["add", "--title", "U", "--summary", "S"],
        ["edit", ident, "--title", "Next"],
        ["close", ident],
        ["reopen", ident],
    ):
        git_log.clear()
        proc = nw(repo, args)
        assert proc.exit_code == 1, (args, proc.stderr, proc.exception)
        assert proc.stderr == expected
        assert rev(repo, "nw-backlog") == tip
        assert not any(call[0] == "hash-object" for call in git_log)


def test_mint_vectors_and_eight_draw_cap(tmp_path, monkeypatch, git_log):
    assert B.mint_id(set(), draw=lambda n: bytes.fromhex("99e23a5c56")) == "REQ-K7H3MQ2P"
    assert B.mint_id(set(), draw=lambda n: bytes.fromhex("abcd123456")) == "REQ-NF6H4D2P"
    calls = []

    def draw(n):
        calls.append(n)
        return bytes.fromhex("99e23a5c56")

    with pytest.raises(B.MintError):
        B.mint_id({"req-k7h3mq2p.md"}, draw=draw)
    assert calls == [5] * 8

    repo = init_repo(tmp_path / "repo")
    sha = commit_tree(repo, {"README.md": b"r\n", "REQ-K7H3MQ2P.md": b"x\n"})
    point(repo, sha)
    calls.clear()
    monkeypatch.setattr(B.secrets, "token_bytes", draw)
    proc = nw(repo, ["add", "--title", "T", "--summary", "S"])
    assert proc.exit_code == 1
    assert proc.stderr == "could not mint an id\n"
    assert calls == [5] * 8
    assert rev(repo, "nw-backlog") == sha
    assert not any(args[0] == "hash-object" for args in git_log)


def _fail_update(monkeypatch, repo, on_call):
    real = B._git
    calls = []

    def wrapped(target, args, **kwargs):
        if args and args[0] == "update-ref":
            calls.append(list(args))
            failed = on_call(list(args))
            if failed is not None:
                return failed
        return real(target, args, **kwargs)

    monkeypatch.setattr(B, "_git", wrapped)
    return calls


class _Failed:
    returncode = 1
    stdout = b""
    stderr = b"failed\n"


def test_compare_and_swap_gives_up_after_three_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(B.secrets, "token_bytes", lambda n: bytes.fromhex("99e23a5c56"))
    repo = init_repo(tmp_path / "repo")
    winners = []

    def on_call(args):
        if not winners:
            assert args[3] == "0" * 40
        else:
            assert args[3] == winners[-1]
        sha = commit_tree(repo, {"README.md": f"variant {len(winners)}\n".encode()})
        point(repo, sha)
        winners.append(sha)
        return _Failed()

    calls = _fail_update(monkeypatch, repo, on_call)
    proc = nw(repo, ["add", "--title", "T", "--summary", "S"])
    assert proc.exit_code == 1
    assert proc.stderr == "nw-backlog changed\n"
    assert len(calls) == 3
    assert len(winners) == 3 and len(set(winners)) == 3
    assert rev(repo, "nw-backlog") == winners[-1]
    assert winners[-1] not in {args[2] for args in calls}
    assert "REQ-K7H3MQ2P.md" not in names(repo)


def test_update_ref_failure_without_a_moved_ref_does_not_retry(tmp_path, monkeypatch):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    added(nw(repo, ["add", "--title", "T", "--summary", "S"]))
    tip = rev(repo, "nw-backlog")

    def on_call(args):
        assert args[3] == tip
        return _Failed()

    calls = _fail_update(monkeypatch, repo, on_call)
    proc = nw(repo, ["add", "--title", "U", "--summary", "S"])
    assert proc.exit_code == 1
    assert proc.stderr == "update-ref failed\n"
    assert len(calls) == 1
    assert rev(repo, "nw-backlog") == tip


def test_retry_stops_when_the_minted_id_appears(tmp_path, monkeypatch):
    draws = []

    def draw(n):
        draws.append(n)
        return bytes.fromhex("99e23a5c56")

    monkeypatch.setattr(B.secrets, "token_bytes", draw)
    repo = init_repo(tmp_path / "repo")

    def on_call(args):
        sha = commit_tree(repo, {"README.md": b"r\n", "REQ-K7H3MQ2P.md": b"already\n"})
        point(repo, sha)
        return _Failed()

    calls = _fail_update(monkeypatch, repo, on_call)
    proc = nw(repo, ["add", "--title", "T", "--summary", "S"])
    assert proc.exit_code == 1
    assert proc.stderr == "REQ-K7H3MQ2P is already on nw-backlog\n"
    assert len(calls) == 1
    assert draws == [5]
    assert "REQ-K7H3MQ2P.md" in names(repo)


def test_repo_flag_and_command_surface(tmp_path, monkeypatch):
    freeze(monkeypatch)
    here = init_repo(tmp_path / "here")
    there = init_repo(tmp_path / "there")
    (there / "sub").mkdir()
    monkeypatch.chdir(here)
    proc = CliRunner().invoke(cli, ["backlog", "add", "--title", "T", "--summary", "S",
                                    "--repo", str(there / "sub")])
    ident = added(proc)
    assert rev(there, "nw-backlog") is not None
    assert f"{ident}.md" in names(there)
    assert rev(here, "nw-backlog") is None

    home = init_repo(tmp_path / "home")
    monkeypatch.chdir(home)
    added(CliRunner().invoke(cli, ["backlog", "add", "--title", "T", "--summary", "S"]))
    assert rev(home, "nw-backlog") is not None

    plain = tmp_path / "plain"
    plain.mkdir()
    bad = nw(plain, ["add", "--title", "T", "--summary", "S"])
    assert bad.exit_code == 1 and bad.stderr == "not a git work tree\n"
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    bare_proc = nw(bare, ["add", "--title", "T", "--summary", "S"])
    assert bare_proc.exit_code == 1 and bare_proc.stderr == "not a git work tree\n"
    early = nw(plain, ["add"])
    assert early.exit_code == 1 and early.stderr == "title is required\n"

    help_text = CliRunner().invoke(cli, ["backlog", "--help"]).output
    assert set(cli.commands["backlog"].commands) == {"add", "edit", "close", "reopen"}
    for name in ("add", "edit", "close", "reopen"):
        assert name in help_text
    for name in ("list", "show", "fetch", "push", "resolve"):
        assert f"\n  {name} " not in help_text
    add_help = CliRunner().invoke(cli, ["backlog", "add", "--help"]).output
    for flag in ("--title", "--summary", "--spec", "--repo"):
        assert flag in add_help
    assert "--push" not in add_help
    assert "--clear-spec" not in add_help


def test_refuses_non_utf8_git_identity(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo", name=None)
    git(repo, "config", "--unset-all", "user.name", check=False)
    git(repo, "config", "--unset-all", "user.email", check=False)
    cfg = git_dir(repo) / "config"
    cfg.write_bytes(
        cfg.read_bytes()
        + b'\n[user]\n\tname = Jos\xe9\n\temail = ada@example.com\n')
    git_log.clear()
    proc = nw(repo, ["add", "--title", "T", "--summary", "S"])
    assert proc.exit_code == 1
    assert proc.stderr == "user.name is not valid utf-8\n"
    assert rev(repo, "nw-backlog") is None
    assert not any(args[0] == "hash-object" for args in git_log)


def _wrong_git_env(other, var):
    gd = git_dir(other)
    if var == "GIT_DIR":
        return str(gd)
    if var == "GIT_WORK_TREE":
        return str(other)
    if var == "GIT_OBJECT_DIRECTORY":
        return str(gd / "objects")
    if var == "GIT_NAMESPACE":
        return "dummy"
    if var == "GIT_COMMON_DIR":
        return str(gd)
    if var == "GIT_ALTERNATE_OBJECT_DIRECTORIES":
        return str(gd / "objects")
    raise AssertionError(var)


def test_inherited_git_env_does_not_redirect_writes(tmp_path, monkeypatch):
    freeze(monkeypatch)
    target = init_repo(tmp_path / "target")
    other = init_repo(tmp_path / "other")
    cleared = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_NAMESPACE",
        "GIT_COMMON_DIR",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    )
    for var in cleared:
        monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv(var, _wrong_git_env(other, var))
        ident = added(nw(target, ["add", "--title", "T", "--summary", "S"]))
        monkeypatch.delenv(var, raising=False)
        assert f"{ident}.md" in names(target)
        assert rev(other, "nw-backlog") is None
        subprocess.run(["git", "-C", str(target), "update-ref", "-d", "refs/heads/nw-backlog"],
                       check=False, capture_output=True)


def test_refuses_pyyaml_unsafe_title_before_hash_object(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    cases = [
        ("x\u0080y", "title contains control character"),
        ("x\u0085y", "title contains control character"),
        ("\ufffe", "title contains control character"),
        ("\ud800", "title is not valid utf-8"),
    ]
    for title, fragment in cases:
        git_log.clear()
        proc = nw(repo, ["add", "--title", title, "--summary", "S"])
        assert proc.exit_code == 1, (title, proc.stderr, proc.exception)
        assert fragment in proc.stderr
        assert proc.stderr.count("\n") == 1
        assert rev(repo, "nw-backlog") is None
        assert not any(args[0] == "hash-object" for args in git_log)


def test_refuses_surrogate_summary_before_hash_object(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    git_log.clear()
    proc = nw(repo, ["add", "--title", "T", "--summary", "\ud800"])
    assert proc.exit_code == 1
    assert proc.stderr == "summary is not valid utf-8\n"
    assert rev(repo, "nw-backlog") is None
    assert not any(args[0] == "hash-object" for args in git_log)


def test_refuses_pyyaml_unsafe_git_identity(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo", name=None)
    git(repo, "config", "user.name", "x\u0080y")
    git(repo, "config", "user.email", "ada@example.com")
    git_log.clear()
    proc = nw(repo, ["add", "--title", "T", "--summary", "S"])
    assert proc.exit_code == 1
    assert "user.name contains control character" in proc.stderr
    assert rev(repo, "nw-backlog") is None
    assert not any(args[0] == "hash-object" for args in git_log)

    repo2 = init_repo(tmp_path / "repo2", name=None)
    git(repo2, "config", "user.name", "Ada")
    git(repo2, "config", "user.email", "a\u0080@example.com")
    git_log.clear()
    proc2 = nw(repo2, ["add", "--title", "T", "--summary", "S"])
    assert proc2.exit_code == 1
    assert "user.email contains control character" in proc2.stderr
    assert not any(args[0] == "hash-object" for args in git_log)


def test_title_unicode_and_tab_round_trip(tmp_path, monkeypatch):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    for title in ("Café", "a\tb"):
        ident = added(nw(repo, ["add", "--title", title, "--summary", "S"]))
        parsed = B.parse_item(f"{ident}.md", cat(repo, f"{ident}.md"))
        assert parsed["title"] == title
        subprocess.run(["git", "-C", str(repo), "update-ref", "-d", "refs/heads/nw-backlog"],
                       check=False, capture_output=True)


def test_refuses_c0_and_del_in_title(tmp_path, monkeypatch, git_log):
    freeze(monkeypatch)
    repo = init_repo(tmp_path / "repo")
    for ch, esc in (("\x00", "\\x00"), ("\x7f", "\\x7f")):
        git_log.clear()
        proc = nw(repo, ["add", "--title", f"x{ch}y", "--summary", "S"])
        assert proc.exit_code == 1
        assert proc.stderr == f"title contains control character {esc}\n"
        assert not any(args[0] == "hash-object" for args in git_log)


def test_safe_path_backslashreplace_on_stderr():
    text = B._safe_path("pre\ud800post")
    assert "\ud800" not in text
    assert "ud800" in text.lower() or "\\ud800" in text


def test_stray_stderr_uses_safe_path_encoding():
    path = "stray\ud800.txt"
    line = B._problem_lines([("100644", path)])[0]
    assert line.startswith("stray: ")
    assert path not in line
    assert "ud800" in line.lower()


def test_aware_clock_is_stored_as_utc(tmp_path, monkeypatch):
    freeze(monkeypatch, "2026-11-07T10:41:00-05:00")
    repo = init_repo(tmp_path / "repo")
    ident = added(nw(repo, ["add", "--title", "T", "--summary", "S"]))
    text = cat(repo, f"{ident}.md").decode()
    assert "created: 2026-11-07T15:41:00Z" in text
    assert "updated: 2026-11-07T15:41:00Z" in text
    naive, unix = B._freeze(datetime(2026, 11, 7, 15, 41, 0))
    assert naive == "2026-11-07T15:41:00Z"
    assert unix == int(datetime(2026, 11, 7, 15, 41, tzinfo=timezone.utc).timestamp())
