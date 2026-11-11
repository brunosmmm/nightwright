"""SPEC-0550: the conductor git-guard PreToolUse hook (`nw-conductor-git-guard`)."""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nightwright.agentic import git_guard, git_guard_parse as GP
from nightwright.agentic import launch as L

from _permission_rules import decide as rule_decide

pytestmark = pytest.mark.fast

CONDUCTOR = "conductor-session-1"
HEREDOC = 'git commit -m "$(cat <<\'EOF\'\nx; y\nEOF\n)"'


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """M (primary), W (sibling .nw-worktrees), W2 and W3 (linked trees nested inside M)."""
    parent = tmp_path_factory.mktemp("guard").resolve()
    m = parent / "repo"
    m.mkdir()
    env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(m)], check=True)
    subprocess.run(["git", "-C", str(m), *env_args, "commit", "-q", "--allow-empty", "-m", "i"],
                   check=True)
    (m / "sub").mkdir()
    (m / "sub" / "f").write_text("x")
    paths = {"P": parent, "M": m, "MSUB": m / "sub", "W": parent / ".nw-worktrees" / "P" / "x",
             "W2": m / ".nw-worktrees" / "y", "W3": m / ".claude" / "worktrees" / "w"}
    for key, b in (("W", "bw"), ("W2", "bw2"), ("W3", "bw3")):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
        _git(m, "worktree", "add", "-q", "-b", b, str(paths[key]))
    return {k: str(v) for k, v in paths.items()}


@pytest.fixture(autouse=True)
def registry(tmp_path, monkeypatch):
    data = tmp_path / "data"
    reg = data / "agentic" / "conductor"
    reg.mkdir(parents=True)
    (reg / "sessions.jsonl").write_text(
        json.dumps({"reader": "conductor", "session_id": CONDUCTOR}) + "\n")
    monkeypatch.setenv("NW_DATA_DIR", str(data))
    return reg / "sessions.jsonl"


def expand(text, tree):
    for k in sorted(tree, key=len, reverse=True):
        text = text.replace(f"@{k}@", tree[k])
    return text


def run_hook(monkeypatch, capsys, payload, raw=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw if raw is not None else json.dumps(payload)))
    git_guard.main()
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out.strip() else None


def call(monkeypatch, capsys, tree, cwd, command, session=CONDUCTOR):
    payload = {"session_id": session, "tool_input": {"command": expand(command, tree)}}
    if cwd is not None:
        payload["cwd"] = tree[cwd]
    return run_hook(monkeypatch, capsys, payload)


def assert_silent(monkeypatch, capsys, tree, cwd, command):
    assert call(monkeypatch, capsys, tree, cwd, command) is None, command


def assert_denied(monkeypatch, capsys, tree, cwd, command, reason=None):
    got = call(monkeypatch, capsys, tree, cwd, command)
    assert got is not None, command
    assert got["permissionDecision"] == "deny"
    if reason:
        assert reason in got["permissionDecisionReason"], got
    return got


# ---- AC1 ---------------------------------------------------------------------------------

WORKTREE_COMMANDS = ["git add a", "git commit -m x", 'git commit -m "fix: a; b && c | d"',
                     "git add a#b", HEREDOC, "git commit -F - <<EOF\nx\nEOF"]


@pytest.mark.parametrize("cwd", ["W", "W2"])
@pytest.mark.parametrize("command", WORKTREE_COMMANDS)
def test_ac1_worktree_commits_stay_silent(monkeypatch, capsys, tree, cwd, command):
    assert_silent(monkeypatch, capsys, tree, cwd, command)


@pytest.mark.parametrize("command", [
    "git -C @W@ add -- a", "git -C @W2@ add -- a", "git -C @W@ commit -m x",
    "git -C @W@ rm -f -- a", "git status", "git log -1 | head", "git log 2>&1 | head",
    "git -C @W3@ commit -m x"])
def test_ac1_main_cwd_targeting_a_worktree_is_silent(monkeypatch, capsys, tree, command):
    assert_silent(monkeypatch, capsys, tree, "M", command)


def test_ac1_static_rules_unchanged():
    s = L.settings({"data_dir": "/tmp/x"}, "conductor")
    allow = s["allowed_tools"] + s["conductor_allowed_tools"]
    deny = s["disallowed_tools"] + s["conductor_disallowed_tools"]
    assert rule_decide("git -C /w add -- a", allow, deny) == "allow"
    assert rule_decide("git -C /w commit -m x", allow, deny) == "allow"
    assert rule_decide("git -C /w rm -f -- a", allow, deny) == "deny"
    settings = json.loads((Path(__file__).parent.parent / ".claude" / "settings.json").read_text())
    proj = settings["permissions"]["allow"]
    assert rule_decide("git add a", proj, []) == "allow"
    assert rule_decide("git commit -m x", proj, []) == "allow"


# ---- AC2 / AC3 ---------------------------------------------------------------------------

MAIN_DENIED = [
    ("M", "git add a"), ("M", "git commit -m x"), ("M", "git commit -am x"),
    ("M", "git add -A"), ("M", 'git commit -m "a; b"'),
    ("MSUB", "git add a"), ("MSUB", "git commit -m x"),
    ("W", "git -C @M@ commit -m x"), ("W", "cd @M@ && git commit -m x"),
    ("W", "git -C @W@ -C ../../../repo commit -m x"),
    ("W", "cd @M@\n\ngit commit -m x"), ("M", "git status &&\ngit add a"),
    ("W", "git add a#b; git -C @M@ commit -m x"),
    ("W2", "git -C @M@ commit -m x"), ("W2", "git -C ../.. commit -m x"),
    ("W2", "cd @M@ && git commit -m x"), ("W3", "git -C ../../.. commit -m x"),
]


@pytest.mark.parametrize("cwd,command", MAIN_DENIED)
def test_ac2_same_commands_against_main_are_denied(monkeypatch, capsys, tree, cwd, command):
    assert_denied(monkeypatch, capsys, tree, cwd, command, "no raw git writes in the main checkout")


def test_ac2_backslash_newline_with_dash_c_is_denied(monkeypatch, capsys, tree):
    assert_denied(monkeypatch, capsys, tree, "W", "git \\\n-C @M@ commit -m x", "cannot verify")


AC3_DENIED = [
    ("M", "git rm -f -- a"), ("M", "git checkout -- ."), ("M", "git checkout HEAD -- a"),
    ("M", "git checkout HEAD a"), ("M", "git checkout other"), ("M", "git checkout -b x"),
    ("M", "git switch -c x"), ("M", "git switch -f main"),
    ("M", "git switch --discard-changes main"), ("M", "git restore a"),
    ("M", "git restore --staged a"), ("M", "git merge --ff-only x"), ("M", "git rebase x"),
    ("M", "git reset --soft HEAD~1"), ("M", "git mv a b"), ("M", "git stash"),
    ("M", "git apply p"), ("M", "git hash-object -w -- a"),
    ("M", "git update-index --add --cacheinfo 100644,0,a"), ("M", "git worktree add ../x"),
    ("M", "git worktree add -B x ../.nw-worktrees/P/y"),
    ("M", "git worktree add -f ../.nw-worktrees/P/y"),
    ("W", "git --work-tree=@M@ add a"), ("W", "git --wo=@M@ add a"),
    ("W", "GIT_DIR=@M@/.git git add a"), ("W", "env GIT_WORK_TREE=@M@ git add a"),
    ("W", "export GIT_DIR=@M@/.git; git add a"),
    ("M", "/usr/bin/git add a"), ("M", "command git commit -m x"),
    ("W", "(cd @M@ && git commit -m x)"), ("W", "{ cd @M@; git commit -m x; }"),
    ("W", "cd $HOME && git add a"), ("W", "cd ~ && git add a"), ("W", "cd - && git add a"),
    ("W", "cd @M@ | git add a"), ("W", 'git -C "$MAIN" commit -m x'),
    ("W", "git -C ~/repo add a"), ("W", "git -C /par* add a"),
    ("M", 'git commit -m "x'), ("M", "git commit -m `x`"),
    ("M", 'git commit -m "$(git -C @M@ add .)"'),
    ("W", 'git commit -m "$(git -C @M@ add .)"'),
    ("W", "cp a @M@/b"), ("W", "cp -t @M@ a"), ("W", "cp -rt @M@ a"), ("W", "cp a $M/b"),
    ("W", "git show HEAD:a > @M@/a"),
    ("W2", "git -C @M@ add a"), ("W2", "cp a @M@/b"),
    ("M", "if true; then git commit -m x; fi"), ("M", "for f in a; do git add $f; done"),
    ("M", "! git add a"), ("M", "time git add a"),
    ("W", "uv run nw queue main-dirty-apply --root @M@"),
    ("W", "uv run --project @M@ nw queue main-dirty-apply --root @M@"),
    ("W", "/home/u/.local/bin/nw queue main-dirty-apply --root @M@"),
    ("W", "(uv run nw queue main-dirty-apply --root @M@)"),
    ("W", "nw queue main-dirty-apply --trivial-only-not --root @M@"),
]


@pytest.mark.parametrize("cwd,command", AC3_DENIED)
def test_ac3_denied(monkeypatch, capsys, tree, cwd, command):
    assert_denied(monkeypatch, capsys, tree, cwd, command)


AC3_SILENT = [
    "git branch --list", "git worktree list", "git worktree add ../.nw-worktrees/P/y",
    "git worktree add -b x ../.nw-worktrees/P/y", "cp a @W@/b",
    "cp a @M@/.nw-worktrees/y/b", "cp a @M@/.claude/worktrees/w/b", "echo x > @M@/f",
    "uv run nw queue main-dirty-apply --trivial-only --root @M@",
    'ls; echo "git add a"', "(ls)", "git log --oneline | head", "git fetch origin",
    "git branch -vv",
]


@pytest.mark.parametrize("cwd", ["M", "W"])
@pytest.mark.parametrize("command", AC3_SILENT)
def test_ac3_silent(monkeypatch, capsys, tree, cwd, command):
    assert_silent(monkeypatch, capsys, tree, cwd, command)


def test_push_is_denied_against_main_and_silent_in_a_linked_worktree(monkeypatch, capsys, tree):
    # Steward review: `git push` from the main checkout can push a local branch state; only
    # `fetch` stays allowed there.
    assert_denied(monkeypatch, capsys, tree, "M", "git push origin main")
    assert_silent(monkeypatch, capsys, tree, "W", "git push origin main")


# ---- AC4 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("cwd,command", MAIN_DENIED)
def test_ac4_non_conductor_session_is_silent(monkeypatch, capsys, tree, cwd, command):
    assert call(monkeypatch, capsys, tree, cwd, command, session="someone-else") is None


def test_ac4_unreadable_json_and_empty_command_are_silent(monkeypatch, capsys, tree):
    assert run_hook(monkeypatch, capsys, None, raw="{not json") is None
    assert run_hook(monkeypatch, capsys, None, raw="") is None
    assert run_hook(monkeypatch, capsys,
                    {"session_id": CONDUCTOR, "cwd": tree["M"], "tool_input": {"command": ""}}
                    ) is None


def test_ac4_missing_directories_fail_silent_only_where_git_fails_too(monkeypatch, capsys, tree,
                                                                       tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert_silent(monkeypatch, capsys, tree, "M", f"git -C {plain} add a")
    assert_silent(monkeypatch, capsys, tree, "M", "git -C /missing add a")
    assert_silent(monkeypatch, capsys, tree, "M", "cd /missing && git add a")
    assert_denied(monkeypatch, capsys, tree, "M", "cd /missing; git add a")
    assert_denied(monkeypatch, capsys, tree, "M", "cd /missing\ngit add a")
    assert_denied(monkeypatch, capsys, tree, "M", "cd /missing || git add a")


def _fake_run(returncode=1, stderr="fatal: boom", timeout=False):
    def run(*a, **kw):
        if timeout:
            raise subprocess.TimeoutExpired(a[0], 2)
        return subprocess.CompletedProcess(a[0], returncode, "", stderr)
    return run


def test_ac4_rev_parse_failures_deny(monkeypatch, capsys, tree):
    monkeypatch.setattr(GP.subprocess, "run", _fake_run())
    assert_denied(monkeypatch, capsys, tree, "M", "git add a", "cannot verify")
    monkeypatch.setattr(GP.subprocess, "run", _fake_run(timeout=True))
    assert_denied(monkeypatch, capsys, tree, "M", "git add a", "cannot verify")
    monkeypatch.setattr(GP.subprocess, "run", _fake_run(returncode=0, stderr=""))
    assert_denied(monkeypatch, capsys, tree, "M", "git add a", "cannot verify")


def test_ac4_no_cwd_denies(monkeypatch, capsys, tree):
    assert_denied(monkeypatch, capsys, tree, None, "git add a", "cannot verify")


def test_ac4_internal_error_denies_a_conductor_only(monkeypatch, capsys, tree):
    def boom(command):
        raise RuntimeError("boom")
    monkeypatch.setattr(GP, "tokenize", boom)
    got = assert_denied(monkeypatch, capsys, tree, "W", "git add a", "cannot verify internal error")
    assert got["hookEventName"] == "PreToolUse"
    assert call(monkeypatch, capsys, tree, "W", "git add a", session="someone-else") is None


# ---- AC6 ---------------------------------------------------------------------------------

def _modules(env_data, payload):
    code = ("import sys, json\nfrom nightwright.agentic import git_guard\ngit_guard.main()\n"
            "print('MODULES=' + json.dumps(sorted(sys.modules)))\n")
    r = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload),
                       capture_output=True, text=True,
                       env={**__import__("os").environ, "NW_DATA_DIR": env_data})
    assert r.returncode == 0, r.stderr
    line = [ln for ln in r.stdout.splitlines() if ln.startswith("MODULES=")][0]
    return json.loads(line[len("MODULES="):]), r.stdout


HEAVY = ("click", "yaml", "rich", "nightwright.agentic.conductor_cost",
         "nightwright.agentic.store", "nightwright.config")


def test_ac6_console_script_is_declared():
    text = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert 'nw-conductor-git-guard = "nightwright.agentic.git_guard:main"' in text


def test_ac6_non_conductor_call_imports_nothing_heavy(registry, tree):
    mods, _ = _modules(str(registry.parents[2]), {
        "session_id": "other", "cwd": tree["M"], "tool_input": {"command": "git add a"}})
    for name in (*HEAVY, "nightwright.agentic.git_guard_parse"):
        assert name not in mods, name


def test_ac6_conductor_call_loads_the_parser_but_nothing_heavy(registry, tree):
    mods, out = _modules(str(registry.parents[2]), {
        "session_id": CONDUCTOR, "cwd": tree["W"], "tool_input": {"command": "git status"}})
    assert "nightwright.agentic.git_guard_parse" in mods
    for name in HEAVY:
        assert name not in mods, name
    assert "permissionDecision" not in out


@pytest.mark.parametrize("content", [None, b"\xff\xfe\n", b"{bad\n", b'{"reader":"conductor"}'])
def test_ac6_bad_registry_leaves_the_hook_silent(monkeypatch, capsys, tree, registry, content):
    if content is None:
        registry.unlink()
    else:
        registry.write_bytes(content)
    assert call(monkeypatch, capsys, tree, "M", "git add a") is None


def test_ac6_registry_line_without_newline_is_skipped(monkeypatch, capsys, tree, registry):
    registry.write_text(json.dumps({"reader": "conductor", "session_id": CONDUCTOR}))
    assert call(monkeypatch, capsys, tree, "M", "git add a") is None


# ---- tokenizer table ----------------------------------------------------------------------

def _shape(command):
    return [(s.words, s.redirects, s.term) for s in GP.tokenize(command)]


def test_tokenizer_splits_merged_punctuation():
    assert _shape("cd M\ngit commit -m \"a; b && c\"") == [
        (["cd", "M"], [], "\n"), (["git", "commit", "-m", "a; b && c"], [], None)]
    assert [s.term for s in GP.tokenize("a &&\nb")] == ["&&", "\n", None]
    assert [s.term for s in GP.tokenize("a;\nb")] == [";", "\n", None]
    assert [s.term for s in GP.tokenize("a\n\nb")] == ["\n", "\n", None]
    assert [s.term for s in GP.tokenize("a || b | c ; d")] == ["||", "|", ";", None]


def test_tokenizer_redirects_and_hash():
    assert _shape("git log 2>&1 | head") == [(["git", "log"], [(">&", "1")], "|"),
                                              (["head"], [], None)]
    assert _shape("git show x > o")[0][:2] == (["git", "show", "x"], [(">", "o")])
    assert _shape("git add a#b") == [(["git", "add", "a#b"], [], None)]


@pytest.mark.parametrize("command", [
    'git commit -m "x', "git add a\\\nb", "(ls)", "{ ls; }", "a & b", "a |& b", "a &> f",
    "a;; b", "git commit -F - <<EOF\nx\nEOF", "echo `x`", "echo $(x)", "pushd M", "popd",
    "case a in a) ;; esac", "select x in a; do :; done", "function f", "[[ -n x ]]", "a >",
    "echo $(cat <<'EOF'\nx `y`\nEOF\n)"])
def test_tokenizer_unsupported(command):
    with pytest.raises(GP.Unsupported):
        segs = GP.tokenize(command)
        for s in segs:
            GP.split_prefix(s.words)


@pytest.mark.parametrize("command,program", [
    ("then git commit -m x", "git"), ("do git add a", "git"), ("! git add a", "git"),
    ("time git add a", "git"), ("else git add a", "git"), ("elif git add a", "git"),
    ("while git add a", "git"), ("until git add a", "git"), ("if git add a", "git"),
    ("A=1 B=2 git add", "git"), ("env A=1 git add", "git"), ("command git add", "git"),
    ("exec git add", "git"), ("/usr/bin/git add", "/usr/bin/git")])
def test_tokenizer_skips_leading_words(command, program):
    seg = GP.tokenize(command)[0]
    _, i = GP.split_prefix(seg.words)
    assert seg.words[i] == program


def test_tokenizer_accepts_the_commit_message_heredoc():
    assert _shape(HEREDOC)[0][0][:3] == ["git", "commit", "-m"]


@pytest.mark.parametrize("command", ["env -i git add a", "time -p git add a"])
def test_option_after_env_or_time_is_unsupported(command):
    with pytest.raises(GP.Unsupported):
        GP.split_prefix(GP.tokenize(command)[0].words)
