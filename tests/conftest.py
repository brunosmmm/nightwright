"""Shared fixtures."""
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
def _git_identity(monkeypatch):
    """Tests build throwaway repos and `git commit` in them; a host with no global
    user.name/user.email (the remote gate worker) would fail with "Author identity unknown"
    (exit 128). Give every test the same identity as test_agentic_e2e's ENV. Global config
    itself is left alone: other tests may rely on it."""
    for k, v in (("GIT_AUTHOR_NAME", "nightwright test"), ("GIT_AUTHOR_EMAIL", "nightwright@test"),
                 ("GIT_COMMITTER_NAME", "nightwright test"),
                 ("GIT_COMMITTER_EMAIL", "nightwright@test")):
        monkeypatch.setenv(k, v)
