"""Shared fixtures."""
import json
import os
import re
import shlex
import subprocess

import pytest


def pytest_configure(config):
    """SPEC-0409: the TUI runtime budget. Registered here rather than via `pytest_plugins`, which
    pytest rejects in a non-top-level conftest; a run that already loaded it with `-p tui_budget`
    is left alone."""
    if not config.pluginmanager.has_plugin("tui_budget"):
        import tui_budget
        config.pluginmanager.register(tui_budget, "tui_budget")


class CodeRepo:
    """A throwaway git repo shaped like a nightwright checkout (SPEC-0231 follower code watch)."""

    def __init__(self, root):
        self.root = root
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "t")
        self.commit({"src/nightwright/x.py": "x = 1\n", "skills/nw-conduct/SKILL.md": "a\n",
                     "docs/a.md": "a\n"})

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files):
        for rel, text in files.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "c")
        return self.head()

    def head(self):
        return self._git("rev-parse", "HEAD")


@pytest.fixture
def code_repo(tmp_path):
    root = tmp_path / "wtcode"
    root.mkdir()
    return CodeRepo(root)


@pytest.fixture(autouse=True)
def _git_guard_installed(monkeypatch, tmp_path_factory):
    """SPEC-0550: `conductor-launch` refuses a claude conductor without the git-guard hook entry
    in `~/.claude/settings.json` and the `nw-conductor-git-guard` script on PATH. No test may read
    the real settings file or depend on the host's PATH, so every test starts with a tmp settings
    file that already has the entry and a fake executable; the tests of the check override both."""
    from nightwright import agent_permissions as AP
    path = tmp_path_factory.mktemp("claude-home") / "settings.json"
    path.write_text(json.dumps({"hooks": {"PreToolUse": [AP.GIT_GUARD_ENTRY]}}))
    monkeypatch.setattr(AP, "default_settings_path", lambda: path)
    monkeypatch.setattr(AP, "git_guard_executable",
                        lambda env=None: "/fake/bin/nw-conductor-git-guard")


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Hermetic commits: many tests build throwaway repos and `git commit` in them. A sandbox with
    no global user.name/user.email (the queue's gate runner) would exit 128, so give every test an
    identity through the environment (tests that need a different one set their own)."""
    for k, v in (("GIT_AUTHOR_NAME", "nightwright test"), ("GIT_AUTHOR_EMAIL", "nightwright@test"),
                 ("GIT_COMMITTER_NAME", "nightwright test"),
                 ("GIT_COMMITTER_EMAIL", "nightwright@test")):
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def _no_persona_run(monkeypatch):
    """SPEC-0272: pytest run by a campaign agent inherits NW_PERSONA_RUN=1; behave as on a
    human shell. Tests that exercise the guard opt in with monkeypatch.setenv. Likewise
    SPEC-0291's NW_RUNNER_LOG, should pytest run under a detached runner."""
    monkeypatch.delenv("NW_PERSONA_RUN", raising=False)
    monkeypatch.delenv("NW_RUNNER_LOG", raising=False)
    # SPEC-0425: `nw campaign note` attributes from these; a test sets the ones it needs
    for k in ("NW_PERSONA", "NW_MAILBOX_ADDR", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _no_runner_unit(monkeypatch, tmp_path_factory):
    """SPEC-0483: a host with nw-runner.service installed must not make `nw queue run`,
    `--detach`, `stop` or `restart` delegate to the human's systemd in tests: the unit dir is an
    empty temp dir unless a test installs a unit there, and no test runs as the unit."""
    monkeypatch.setenv("NW_SYSTEMD_USER_DIR", str(tmp_path_factory.mktemp("systemd-user")))
    monkeypatch.delenv("NW_RUNNER_UNIT", raising=False)
    from nightwright.agentic import backend_preflight as BP, credentials as CR
    BP.reset_noted()
    CR.reset()


@pytest.fixture(autouse=True)
def _no_ambient_herdr_env(monkeypatch):
    """SPEC-0365: pytest run from a herdr pane inherits HERDR_*; a conductor follower under test
    would then auto-start a real `nw herdr watch` against the human's herdr. Tests that want a
    herdr env set it themselves (the contract tests read their socket from a capture taken at
    import)."""
    import os
    for k in [k for k in os.environ if k.startswith("HERDR_")]:
        monkeypatch.delenv(k)


_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_WRAPPERS = frozenset({"env", "exec", "command", "nohup", "nice", "setsid", "sudo", "time",
                       "timeout", "stdbuf"})
_WRAPPER_FLAGS_WITH_ARG = frozenset({"-s", "-k", "-n", "-u", "-g", "-i", "-o", "-e"})
_SHELL_KEYWORDS = frozenset({"then", "do", "else", "{", "!"})
_SEP_CHARS = frozenset(";&|()\n")
_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"\d+(\.\d+)?[smhd]?\Z")
_SHELL_C_FLAG = re.compile(r"-[A-Za-z]*c[A-Za-z]*\Z")


def _as_text(x):
    if isinstance(x, (bytes, bytearray)):
        return os.fsdecode(bytes(x))
    if isinstance(x, os.PathLike):
        return os.fsdecode(os.fspath(x))
    return str(x)


def _shell_words(line):
    """Words of a shell command line; unquoted `;&|()` and newlines come out as separators."""
    lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()\n<>")
    lex.whitespace_split = True
    lex.whitespace = " \t\r"
    try:
        return list(lex)
    except ValueError:  # unbalanced quote
        return re.sub(r"([;&|()\n])", r" \1 ", line).split()


def _simple_is_notify_send(words, depth):
    """Is the simple command `words` (after env assignments and wrappers) `notify-send`, or a
    shell whose `-c` script is?"""
    i, n = 0, len(words)
    while i < n:
        w = words[i]
        base = os.path.basename(w)
        if _ASSIGN.match(w) or w in _SHELL_KEYWORDS:
            i += 1
        elif base in _WRAPPERS:
            i += 1
            while i < n:
                if words[i] in _WRAPPER_FLAGS_WITH_ARG and base != "env":
                    i += 2
                elif words[i].startswith("-") or _DURATION.match(words[i]):
                    i += 1
                else:
                    break
        else:
            break
    if i >= n:
        return False
    cmd = os.path.basename(words[i])
    if cmd == "notify-send":
        return True
    if cmd in _SHELLS and depth > 0:
        for j in range(i + 1, n - 1):
            if _SHELL_C_FLAG.match(words[j]):
                return _line_is_notify_send(words[j + 1], depth - 1)
    return False


def _line_is_notify_send(line, depth):
    cmds, cur = [], []
    for tok in _shell_words(line):
        if set(tok) <= _SEP_CHARS:
            cmds.append(cur)
            cur = []
        else:
            cur.append(tok)
    cmds.append(cur)
    return any(_simple_is_notify_send(c, depth) for c in cmds)


def _is_notify_send(cmd, depth=3):
    """SPEC-0499/0589: would this `Popen` argv exec `notify-send`? Decodes `bytes`/`PathLike`,
    treats a `str` as a shell command line, and looks through shell wrappers (`sh -c`, `env`,
    `&&`) to the command they run. A small command-position scan, not a shell interpreter."""
    if isinstance(cmd, (list, tuple)):
        return _simple_is_notify_send([_as_text(x) for x in cmd], depth)
    if isinstance(cmd, (str, bytes, bytearray)):
        return _line_is_notify_send(_as_text(cmd), depth)
    if isinstance(cmd, os.PathLike):
        return os.path.basename(_as_text(cmd)) == "notify-send"
    return False


@pytest.fixture(autouse=True)
def _no_desktop_notify(monkeypatch):
    """SPEC-0499: default `notify=auto` must not spawn the session's `notify-send`; any test that
    still reaches `notify-send` through subprocess fails before the process starts."""
    from nightwright.agentic import escalations as ESC

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(ESC, "_run_notify", lambda argv: _Done())

    _real_init = subprocess.Popen.__init__

    def _guarded_init(self, args, *a, **kw):
        # Sets no instance state before raising: `Popen._child_created` is a class attribute,
        # so the half-built object's `__del__` returns at once (SPEC-0589).
        if _is_notify_send(args):
            raise AssertionError(f"notify-send blocked in tests: {args!r}")
        _real_init(self, args, *a, **kw)

    # One class-level patch covers `run`/`call`/`check_output`, every subclass (`gates._MeteredPopen`)
    # and every `popen=subprocess.Popen` default bound at import (SPEC-0589).
    monkeypatch.setattr(subprocess.Popen, "__init__", _guarded_init)


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    """Tests build throwaway repos and `git commit` in them; a host with no global
    user.name/user.email (the remote gate worker) would fail with "Author identity unknown"
    (exit 128). Give every test the same identity as test_agentic_e2e's ENV. Global config
    itself is left alone: other tests may rely on it."""
    for k, v in (("GIT_AUTHOR_NAME", "nightwright test"), ("GIT_AUTHOR_EMAIL", "nightwright@test"),
                 ("GIT_COMMITTER_NAME", "nightwright test"),
                 ("GIT_COMMITTER_EMAIL", "nightwright@test")):
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def _cost_policy_off(monkeypatch):
    """SPEC-0422: the cost-cap policy ships on, but the tests written before it assert today's
    pessimistic cap checks and `cost_cap` parks (AC4). Every test starts with the policy's
    default off; tests of the policy opt in with the `cost_policy_on` fixture (or set
    `agentic.cost_cap_policy.enabled: true` in their config)."""
    from nightwright.agentic import cost_policy as CP
    monkeypatch.setitem(CP.DEFAULTS, "enabled", False)


@pytest.fixture
def cost_policy_on(monkeypatch):
    """SPEC-0422: the shipped default (the policy on)."""
    from nightwright.agentic import cost_policy as CP
    monkeypatch.setitem(CP.DEFAULTS, "enabled", True)
