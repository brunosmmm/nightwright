"""SPEC-0483: `agentic.credentials` resolved through `pass`, never written or logged. A fake
`pass` on PATH stands in for the store (the real one is never called)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nightwright import doctor
from nightwright.agentic import credentials as CR
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import persona_gate as PG
from nightwright.agentic import queue as Q
from nightwright.agentic import runproc as RP

pytestmark = pytest.mark.fast

SECRET = "sk-cursor-SECRET-0123456789"

FAKE_PASS = """#!/bin/sh
# records how it was called, then behaves per $MODE_FILE
{ echo "args=$*"; echo "gpg_tty=${GPG_TTY-unset}"; echo "opts=$PASSWORD_STORE_GPG_OPTS";
  if [ -t 0 ]; then echo stdin=tty; else echo stdin=notty; fi; } >> "%(calls)s"
mode=$(cat "%(mode)s" 2>/dev/null)
case "$mode" in
  ok) printf '%%s\\nline two\\n' "%(secret)s"; exit 0 ;;
  slow) sleep 5; exit 0 ;;
  *) echo "gpg: decryption failed: No pinentry" >&2; exit 2 ;;
esac
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    mode, calls = tmp_path / "mode", tmp_path / "calls"
    p = bin_ / "pass"
    p.write_text(FAKE_PASS % {"calls": calls, "mode": mode, "secret": SECRET})
    p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GPG_TTY", "/dev/pts/9")
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    cfg = {"data_dir": str(tmp_path / "d"),
           "agentic": {"runner": "cursor",
                       "credentials": {"CURSOR_API_KEY": "pass:cursor/sdk", "BAD var": "x",
                                       "OPENAI_API_KEY": "env"}}}
    return cfg, mode, calls


def _no_secret_anywhere(cfg):
    root = Path(cfg["data_dir"])
    for f in root.rglob("*"):
        if f.is_file():
            assert SECRET not in f.read_text(errors="replace"), f


def test_settings_keeps_valid_sources_only(store):
    cfg, _, _ = store
    assert CR.settings(cfg) == {"CURSOR_API_KEY": "pass:cursor/sdk", "OPENAI_API_KEY": "env"}


def test_resolves_non_interactively_and_never_logs_the_value(store, monkeypatch):
    cfg, mode, calls = store
    mode.write_text("ok")
    env = dict(os.environ)
    out = CR.ensure(cfg, environ=env)
    assert out == {"CURSOR_API_KEY": "resolved", "OPENAI_API_KEY": "missing"}
    assert env["CURSOR_API_KEY"] == SECRET                     # first line only
    log = calls.read_text()
    assert "args=show cursor/sdk" in log and "gpg_tty=unset" in log and "stdin=notty" in log
    assert "--pinentry-mode error" in log and "--batch" in log
    rec = [r for r in Q.read_journal(cfg) if r["kind"] == "credential_resolved"]
    assert rec and rec[0]["var"] == "CURSOR_API_KEY" and rec[0]["source"] == "pass:cursor/sdk"
    _no_secret_anywhere(cfg)


def test_process_env_wins(store):
    cfg, mode, calls = store
    mode.write_text("ok")
    env = {**os.environ, "CURSOR_API_KEY": "from-shell"}
    assert CR.ensure(cfg, environ=env)["CURSOR_API_KEY"] == "set"
    assert env["CURSOR_API_KEY"] == "from-shell" and not calls.exists()


def test_locked_then_unlocked_holds_then_resumes(store, monkeypatch):
    """A locked agent: no raise, one escalation, the cursor gate holds; once `pass` works the
    next tick's `ensure` fills os.environ, withdraws the escalation and the gate admits."""
    cfg, mode, _ = store
    monkeypatch.setattr(RP, "probe_cursor_sdk", lambda environ=None: (
        {"ok": True} if (environ or os.environ).get("CURSOR_API_KEY")
        else {"ok": False, "error": "CURSOR_API_KEY is not set"}))
    monkeypatch.setenv("CURSOR_API_KEY", "placeholder")          # restored after the test
    monkeypatch.delenv("CURSOR_API_KEY")
    mode.write_text("locked")
    st, step = {"id": "c1", "state": "GATING"}, Q.Step("triage", persona="gate_triager")
    for _ in range(2):
        assert CR.ensure(cfg)["CURSOR_API_KEY"] == "locked"
        assert PG.Gate(cfg, ["c1"]).admit(st, step) is None
    esc = [r for r in ESC.list_all(cfg, "open") if r["kind"] == CR.ESC_KIND]
    assert len(esc) == 1 and "CURSOR_API_KEY" in esc[0]["question"]
    assert "pass show cursor/sdk" in esc[0]["options"][0]["label"]
    # the backend preflight does not file a second escalation for a mapped var
    assert not [r for r in ESC.list_all(cfg, "open") if r["kind"] == "backend_unavailable"]
    unres = [r for r in Q.read_journal(cfg)
             if r["kind"] == "credential_unresolved" and r["var"] == "CURSOR_API_KEY"]
    assert len(unres) == 1 and "No pinentry" in unres[0]["why"]
    mode.write_text("ok")
    assert CR.ensure(cfg)["CURSOR_API_KEY"] == "resolved"
    assert os.environ["CURSOR_API_KEY"] == SECRET
    assert PG.Gate(cfg, ["c1"]).admit(st, step) is step
    assert not [r for r in ESC.list_all(cfg, "open") if r["kind"] == CR.ESC_KIND]
    E.sync(cfg)
    _no_secret_anywhere(cfg)


def test_timeout_is_locked_not_a_hang(store):
    cfg, mode, _ = store
    mode.write_text("slow")
    value, st, why = CR.fetch("pass:cursor/sdk", timeout=0.5)
    assert value is None and st == "locked" and "timed out" in why


def test_missing_pass_binary(store, monkeypatch, tmp_path):
    cfg, _, _ = store
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert CR.fetch("pass:cursor/sdk")[1] == "missing"


def test_doctor_reports_status_not_value(store, monkeypatch):
    cfg, mode, _ = store
    monkeypatch.setattr(RP, "probe_cursor_sdk",
                        lambda environ=None: {"ok": False, "error": "CURSOR_API_KEY is not set"})
    mode.write_text("ok")
    c = doctor.check_cursor_agent(cfg)
    assert c["status"] == "ok" and "resolves" in c["summary"] and SECRET not in json.dumps(c)
    assert "CURSOR_API_KEY" not in os.environ                   # doctor only probes
    mode.write_text("locked")
    c = doctor.check_cursor_agent(cfg)
    assert c["status"] == "fail" and "locked" in c["summary"] and "unlock gpg" in c["fix"]

