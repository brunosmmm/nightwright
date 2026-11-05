"""SPEC-0389: one `claude` resolver for `claude-check` and the job's exec. Offline: a stub
`claude` script in a temp HOME; never the real box."""
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from nightwright.agentic import remote_personas as RPS
from nightwright.agentic import runner as R
from nightwright.agentic import runproc as RP
from nightwright.agentic import worker as W


def _stub(home: Path) -> Path:
    d = home / ".local" / "bin"
    d.mkdir(parents=True)
    c = d / "claude"
    c.write_text("#!/bin/sh\necho 9.9.9 stub\n")
    c.chmod(c.stat().st_mode | stat.S_IXUSR)
    return c


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A HOME with claude only in ~/.local/bin, and a PATH without it (a non-login ssh)."""
    stub = _stub(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin-nonexistent:/bin-nonexistent")
    return stub


def test_resolver_finds_local_bin_when_path_lacks_it(bare):
    assert RP.resolve_claude() == str(bare)


def test_resolver_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(RP, "_CLAUDE_SYSTEM", ())
    assert RP.resolve_claude({"PATH": "", "HOME": str(tmp_path)}) is None


def test_claude_check_uses_resolver(bare):
    out = W.claude_check()
    assert out["ok"] is True and out["version"] == "9.9.9 stub"


def test_claude_check_fails_when_unresolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(RP, "resolve_claude", lambda environ=None: None)
    out = W.claude_check()
    assert out["ok"] is False and out["error"]


def test_with_claude_makes_argv_absolute_and_extends_path(bare):
    argv, env = RP.with_claude(["claude", "-p"], {"PATH": "/x"})
    assert argv == [str(bare), "-p"] and env["PATH"].split(os.pathsep)[-1] == str(bare.parent)
    assert RP.with_claude(["git", "x"], {"PATH": "/x"}) == (["git", "x"], {"PATH": "/x"})


def test_with_claude_raises_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(RP, "resolve_claude", lambda environ=None: None)
    with pytest.raises(FileNotFoundError):
        RP.with_claude(["claude"], {"PATH": ""})


def test_run_job_raises_job_level_error_without_claude(tmp_path, monkeypatch):
    """Unresolvable claude is an exception out of run_job (worker_job turns it into a
    `job_error` frame), raised before `started`, not a persona verdict."""
    monkeypatch.setattr(RP, "resolve_claude", lambda environ=None: None)
    monkeypatch.setattr(R, "select_runner", lambda cfg, runner=None: R.ClaudeRunner())
    monkeypatch.setattr(R, "request_from_job", lambda *a, **k: object())
    monkeypatch.setattr(RPS, "_tree_git", lambda *a, **k: "")
    monkeypatch.setattr(W, "_git", lambda *a, **k: type("P", (), {"returncode": 0})())

    class Frames:
        def emit(self, kind, **kw):
            raise AssertionError(f"unexpected frame {kind}")

    job = tmp_path / "job"
    job.mkdir()
    h = {"job_id": "j", "payload": {"request": {}, "read_only": True}}
    cfg = {"data_dir": tmp_path}
    with pytest.raises(FileNotFoundError):
        RPS.run_job(cfg, h, job, Frames(), threading.Event())
    detail = W.error_detail(cfg, FileNotFoundError(2, "x", "claude"), job)
    assert detail["job_error"] is True and detail["filename"] == "claude"
