"""Shared fixtures."""
import json
import os
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

    _real_run = subprocess.run
    _Popen = subprocess.Popen

    def _is_notify_send(cmd):
        if isinstance(cmd, (list, tuple)):
            return bool(cmd) and os.path.basename(str(cmd[0])) == "notify-send"
        if isinstance(cmd, str):
            part = cmd.strip().split(None, 1)
            return bool(part) and os.path.basename(part[0]) == "notify-send"
        return False

    def _guarded_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if cmd is not None and _is_notify_send(cmd):
            raise AssertionError(f"notify-send blocked in tests: {cmd!r}")
        return _real_run(*args, **kwargs)

    class _GuardedPopen(_Popen):
        def __init__(self, args, *a, **kw):
            if _is_notify_send(args):
                raise AssertionError(f"notify-send blocked in tests: {args!r}")
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", _guarded_run)
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)


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
