"""Shared fixtures."""
import subprocess

import pytest


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
