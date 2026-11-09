"""SPEC-0483: nw-runner.service — rendering, systemctl delegation, the foreground refusal."""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from click.testing import CliRunner

from nightwright.cli import cli
from nightwright.agentic import escalations as ESC
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q
from nightwright.agentic import restart as RS
from nightwright.agentic import runner_unit as RU


def _cfg(tmp_path):
    return {"data_dir": str(tmp_path / "d"), "agentic": {"poll_interval_s": 0.01}}


def _install(text="[Unit]\n"):
    RU.unit_path().parent.mkdir(parents=True, exist_ok=True)
    RU.unit_path().write_text(text)


class FakeSystemctl:
    """`systemctl --user …`: `start` takes the lease in-process, `stop` releases it."""

    def __init__(self, cfg, active="active"):
        self.cfg, self.calls, self.lease, self.state = cfg, [], None, active

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        verb = argv[2]
        out = ""
        if verb == "start":
            self.lease = L.acquire(self.cfg, ["nw", "queue", "run"])
            self.state = "active"
        elif verb == "stop":
            if self.lease:
                self.lease.release()
            self.state = "inactive"
        elif verb == "show":
            out = f"LoadState=loaded\nActiveState={self.state}\nNRestarts=5\nResult=start-limit-hit\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


def _never(*a, **k):
    pytest.fail("spawned or signalled a runner outside the unit")


def test_render_unit_has_supervision_and_no_credentials(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "secret-value")
    monkeypatch.setenv("GNUPGHOME", "/home/x/.gnupg")
    monkeypatch.delenv("PASSWORD_STORE_DIR", raising=False)
    env = RU.unit_env({**os.environ, "PATH": "/w/.venv/bin:/home/x/.local/bin"})
    text = RU.render_unit(nw_bin="/opt/nw", env=env)
    for line in ("ExecStart=/opt/nw queue run", "Restart=always", "KillMode=process",
                 "OnFailure=nw-runner-failed.service", f"StartLimitBurst={RU.START_LIMIT_BURST}",
                 "Environment=NW_RUNNER_UNIT=nw-runner.service", "WantedBy=default.target",
                 "Environment=GNUPGHOME=/home/x/.gnupg", "StandardOutput=journal"):
        assert line in text.splitlines(), line
    path = next(ln for ln in text.splitlines() if ln.startswith("Environment=PATH="))
    assert "/usr/bin" in path and "/home/x/.local/bin" in path and ".venv" not in path
    assert "EnvironmentFile" not in text and "secret-value" not in text
    assert "CURSOR_API_KEY" not in text and "PASSWORD_STORE_DIR" not in text
    failed = RU.render_failed_unit(nw_bin="/opt/nw", env=env)
    assert "ExecStart=/opt/nw queue unit-failed --unit nw-runner.service" in failed
    assert "Type=oneshot" in failed


def test_systemd_analyze_verifies_the_rendered_units(tmp_path):
    exe = shutil.which("systemd-analyze")
    if not exe or not os.environ.get("XDG_RUNTIME_DIR"):
        pytest.skip("no systemd-analyze or no user session (XDG_RUNTIME_DIR) on this host")
    nw = tmp_path / "nw"
    nw.write_text("#!/bin/sh\n")
    nw.chmod(0o755)
    env = RU.unit_env()
    (tmp_path / "nw-runner.service").write_text(RU.render_unit(nw_bin=str(nw), env=env))
    (tmp_path / "nw-runner-failed.service").write_text(RU.render_failed_unit(nw_bin=str(nw), env=env))
    p = subprocess.run([exe, "--user", "verify", str(tmp_path / "nw-runner.service"),
                        str(tmp_path / "nw-runner-failed.service")],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr


def test_detach_delegates_to_systemctl_start(tmp_path):
    cfg = _cfg(tmp_path)
    _install()
    sc = FakeSystemctl(cfg, active="inactive")
    try:
        d = Q.detach_runner(cfg, ["nw", "queue", "run", "--detach"], tmp_path / "log",
                            popen=_never, systemctl_run=sc)
        assert d["pid"] == os.getpid() and d["unit"] == "nw-runner.service"
        assert sc.calls == [["systemctl", "--user", "start", "nw-runner.service"]]
    finally:
        sc.lease and sc.lease.release()


def test_stop_delegates_to_systemctl_stop(tmp_path):
    cfg = _cfg(tmp_path)
    _install()
    sc = FakeSystemctl(cfg)
    sc.lease = L.acquire(cfg, ["nw", "queue", "run"])
    out = Q.stop_runner(cfg, timeout_s=5, kill=_never, systemctl_run=sc)
    assert out["stopped"] is True and not L.is_held(cfg)
    assert ["systemctl", "--user", "stop", "--no-block", "nw-runner.service"] in sc.calls


def test_stop_without_unit_still_signals(tmp_path):
    cfg = _cfg(tmp_path)
    lease = L.acquire(cfg, ["nw", "queue", "run"])
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        lease.release()
    assert Q.stop_runner(cfg, timeout_s=5, kill=kill)["stopped"] is True
    assert sent and sent[0][0] == os.getpid()


def test_restart_start_phase_uses_the_unit(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _install()
    sc = FakeSystemctl(cfg, active="inactive")
    real = Q.detach_runner
    monkeypatch.setattr(Q, "detach_runner",
                        lambda *a, **k: real(*a, **{**k, "systemctl_run": sc}))
    plan = {"command": {"argv": ["nw", "queue", "run"], "env_source": "proc"},
            "log": str(tmp_path / "log")}
    try:
        RS._start(cfg, plan, env={"CURSOR_API_KEY": "x"}, popen=_never,
                  clock=__import__("time").monotonic, timeout_s=5)
        assert plan["start"]["started"] and plan["command"]["env_used"] == "unit"
        assert ["systemctl", "--user", "start", "nw-runner.service"] in sc.calls
    finally:
        sc.lease and sc.lease.release()


def test_foreground_run_refuses_while_the_unit_is_active(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _install()
    monkeypatch.setattr(RU, "active", lambda run=None: True)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["queue", "run", "--once"])
    assert r.exit_code == 1 and "nw-runner.service" in r.output
    assert "systemctl --user restart nw-runner.service" in r.output
    assert RU.foreground_refusal() is not None
    monkeypatch.setenv(RU.UNIT_ENV, "nw-runner.service")      # the unit's own process
    assert RU.foreground_refusal() is None
    monkeypatch.delenv(RU.UNIT_ENV)
    RU.unit_path().unlink()
    assert RU.foreground_refusal() is None


def test_unit_failed_escalates_once(tmp_path):
    cfg = _cfg(tmp_path)
    sc = FakeSystemctl(cfg, active="failed")
    rec = RU.escalate_failed(cfg, run=sc)
    assert rec and rec["kind"] == "runner_unit_failed"
    assert "result: start-limit-hit" in rec["facts"]
    assert RU.escalate_failed(cfg, run=sc) is None
    assert len([r for r in ESC.list_all(cfg, "open") if r["kind"] == "runner_unit_failed"]) == 1


def test_install_dry_run_prints_both_units(tmp_path, monkeypatch):
    nw = tmp_path / "nw"
    nw.write_text("#!/bin/sh\n")
    nw.chmod(0o755)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: _cfg(tmp_path))
    r = CliRunner().invoke(cli, ["queue", "install", "--dry-run", "--nw-bin", str(nw)])
    assert r.exit_code == 0, r.output
    assert f"ExecStart={nw} queue run" in r.output and "nw-runner-failed.service" in r.output
    assert "systemctl --user enable --now nw-runner.service" in r.output
    assert not RU.installed()                                  # dry run wrote nothing


def test_install_refuses_while_a_stray_runner_holds_the_lease(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    nw = tmp_path / "nw"
    nw.write_text("#!/bin/sh\n")
    nw.chmod(0o755)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.setattr(RU, "show", lambda run=None: None)
    monkeypatch.setattr(RU, "systemctl", _never)
    lease = L.acquire(cfg, ["nw", "queue", "run"])
    try:
        r = CliRunner().invoke(cli, ["queue", "install", "--nw-bin", str(nw)])
    finally:
        lease.release()
    if shutil.which("systemctl"):
        assert r.exit_code == 1 and "nw queue stop --yes" in r.output, r.output
    assert not RU.installed()
