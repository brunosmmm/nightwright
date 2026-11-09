"""SPEC-0427: the dashboard service: the `nw-dashboard.service` systemd user unit, `nw dashboard
install | uninstall | status`, `--tailnet-wait`, and the conduct-status / doctor lines.

systemctl and loginctl are faked here (a fake executable on PATH for the real-process CLI
cases, an injected `run` for the pure ones); the real `systemctl --user` run is the manual
verification recorded in the spec."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from fixtures.dashboard_data import seed
from nightwright import doctor as D
from nightwright.agentic import cli_conduct as CC
from nightwright.agentic.dashboard import lifecycle as LC
from test_dashboard_cli import nw_argv, nw_env
from test_dashboard_lifecycle import commit, git, repo, serve_once, tailscale_env  # noqa: F401
from test_dashboard_serve_cli import dash

pytestmark = pytest.mark.fast

NW = "/opt/nw tools/bin/nw"


@pytest.fixture
def fx(tmp_path):
    return seed(tmp_path / "seed")


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    """A private `$XDG_CONFIG_HOME`, so `unit_dir()` never names the real ~/.config."""
    d = tmp_path / "xdg"
    d.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    return d


def _proc(cmd, code=0, out=""):
    return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")


class FakeSystemd:
    """An injected `run`: answers `systemctl --user show` from `props` and `loginctl show-user`
    from `linger`; records every call."""

    def __init__(self, props=None, linger="yes", reachable=True):
        self.props, self.linger, self.reachable = props or {}, linger, reachable
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if not self.reachable:
            return _proc(cmd, 1)
        if cmd[:3] == ["systemctl", "--user", "show"]:
            return _proc(cmd, 0, "".join(f"{k}={v}\n" for k, v in self.props.items()))
        if cmd[:2] == ["loginctl", "show-user"]:
            return _proc(cmd, 0, f"Linger={self.linger}\n")
        return _proc(cmd, 1)


def active_props(pid=4242, **kw):
    return {"LoadState": "loaded", "UnitFileState": "enabled", "ActiveState": "active",
            "SubState": "running", "MainPID": str(pid), "NRestarts": "2", "Result": "success",
            "ExecMainStatus": "0", **kw}


def live_record(cfg, **kw):
    me = os.getpid()
    rec = {"schema": LC.SCHEMA, "pid": me, "ticks": LC.start_ticks(me),
           "url": "http://127.0.0.1:8791", "bind": "127.0.0.1:8791", "started_at": "t",
           "code_watch": False, "code_sha": None, "clients": 0, **kw}
    return LC.write_server_record(cfg, rec)


# ---- the unit text (AC 1, 2, 3) ------------------------------------------------------------

def test_loopback_unit_restarts_on_exit_0_and_stops_on_exit_2():
    text = LC.render_unit(nw_bin="/home/u/.local/share/uv/tools/nightwright/bin/nw",
                          bind="127.0.0.1:8791")
    lines = text.splitlines()
    assert ("ExecStart=/home/u/.local/share/uv/tools/nightwright/bin/nw serve --dashboard "
            "--bind 127.0.0.1:8791") in lines
    for want in ("Restart=always", f"RestartSec={LC.RESTART_SEC}", "RestartPreventExitStatus=2",
                 f"StartLimitIntervalSec={LC.START_LIMIT_INTERVAL_S}",
                 f"StartLimitBurst={LC.START_LIMIT_BURST}", "StandardOutput=journal",
                 "StandardError=journal", "WantedBy=default.target", "Type=simple"):
        assert want in lines, want
    assert LC.RESTART_SEC <= 10
    assert not any(ln.startswith(("After=", "Requires=", "Wants=", "BindsTo=")) for ln in lines)
    assert "--tailnet-wait" not in text and "NW_CONFIG_DIR" not in text
    # the StartLimit keys belong to [Unit] (systemd ignores them in [Service])
    unit_sec = text.split("[Service]")[0]
    assert "StartLimitIntervalSec" in unit_sec and "StartLimitBurst" in unit_sec


def test_exit_3_cadence_never_reaches_the_start_limit():
    """One exit 3 per TAILNET_WAIT_S + RESTART_SEC: fewer starts per interval than the burst,
    so a Tailscale outage never puts the unit into `start-limit-hit`."""
    per_interval = LC.START_LIMIT_INTERVAL_S / (LC.TAILNET_WAIT_S + LC.RESTART_SEC) + 1
    assert per_interval < LC.START_LIMIT_BURST
    # a crash loop (exit 1 at once) does reach it, within a few minutes
    assert LC.START_LIMIT_BURST * LC.RESTART_SEC < LC.START_LIMIT_INTERVAL_S


def test_tailscale_unit_waits_for_the_tailnet_and_orders_softly():
    text = LC.render_unit(nw_bin="/x/nw", bind="tailscale:9000", after_tailscaled=True)
    lines = text.splitlines()
    assert ("ExecStart=/x/nw serve --dashboard --bind tailscale:9000 --tailnet-wait "
            f"{LC.TAILNET_WAIT_S}") in lines
    assert "After=tailscaled.service" in lines
    assert not any(ln.startswith(("Requires=", "Wants=", "BindsTo=", "Requisite="))
                   for ln in lines)
    assert "After=" not in LC.render_unit(nw_bin="/x/nw", bind="tailscale:9000")


def test_unit_quotes_paths_and_escapes_specifiers():
    text = LC.render_unit(nw_bin=NW, bind="127.0.0.1:1", env={"NW_DATA_DIR": "/d/100%$x"},
                          code_root="/c r")
    assert 'ExecStart="/opt/nw tools/bin/nw" serve --dashboard --bind 127.0.0.1:1 ' \
           '--code-root "/c r"' in text
    assert "Environment=NW_DATA_DIR=/d/100%%$$x" in text.splitlines()
    assert 'Environment="NW_DATA_DIR=/a b"' in LC.render_unit(
        nw_bin="/x", bind="127.0.0.1:1", env={"NW_DATA_DIR": "/a b"}).splitlines()


def test_env_overrides_only_non_default_dirs(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for k in ("NW_CONFIG_DIR", "NW_DATA_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(k, raising=False)
    assert LC.env_overrides() == {}
    monkeypatch.setenv("NW_CONFIG_DIR", str(home / ".config/nightwright"))   # = the default
    assert LC.env_overrides() == {}
    monkeypatch.setenv("NW_DATA_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xc"))   # NW_CONFIG_DIR still wins
    assert LC.env_overrides() == {"NW_DATA_DIR": str(tmp_path / "scratch")}
    monkeypatch.delenv("NW_CONFIG_DIR")
    assert LC.env_overrides()["NW_CONFIG_DIR"] == str(tmp_path / "xc" / "nightwright")


def test_resolve_nw_bin_prefers_the_uv_tool_install(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    tool_nw = tools / "nightwright" / "bin" / "nw"
    other = tmp_path / "venv" / "bin" / "nw"
    for p in (tool_nw, other):
        p.parent.mkdir(parents=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    link = tmp_path / "bin" / "nw"
    link.parent.mkdir()
    link.symlink_to(other)
    monkeypatch.setenv("UV_TOOL_DIR", str(tools))
    assert LC.resolve_nw_bin(which=lambda n: str(link)) == str(tool_nw)
    assert LC.resolve_nw_bin(str(other), which=lambda n: None) == str(other)
    with pytest.raises(ValueError):
        LC.resolve_nw_bin(str(tmp_path / "nope"))
    tool_nw.unlink()
    assert LC.resolve_nw_bin(which=lambda n: str(link)) == str(other)      # PATH, resolved


def test_unit_bind_and_name():
    assert LC.unit_bind() == "auto:8791"                     # SPEC-0432: the default
    assert LC.unit_bind("auto", 9002) == "auto:9002"
    assert LC.unit_bind("loopback", 9001) == "127.0.0.1:9001"
    assert LC.unit_bind("tailscale") == "tailscale:8791"
    with pytest.raises(ValueError):
        LC.unit_bind("lan")
    assert LC.unit_name("nw-dashboard-test.service") == "nw-dashboard-test"
    assert LC.unit_name("") == LC.unit_name(None) == "nw-dashboard"
    for bad in ("../x", "a b", "-x", "x;y"):
        with pytest.raises(ValueError):
            LC.unit_name(bad)


# ---- status (AC 5, 6) ----------------------------------------------------------------------

def test_status_reports_an_active_unit_with_a_live_current_server(fx, xdg, repo):
    cfg = fx["cfg"]
    LC.unit_path().parent.mkdir(parents=True)
    LC.unit_path().write_text("[Unit]\n")
    sha = LC.head_sha(repo)
    live_record(cfg, code_watch=True, code_sha=sha, code_root=str(repo))
    run = FakeSystemd(active_props(pid=os.getpid()))
    st = LC.status(cfg, run=run)
    assert st["schema"] == "nw.dashboard.status.v1"
    u = st["unit"]
    assert (u["installed"], u["enabled"], u["active"], u["main_pid"], u["n_restarts"]) == \
        (True, True, True, os.getpid(), 2)
    assert st["running"] and st["server"]["url"] == "http://127.0.0.1:8791"
    assert st["code"] == {"watched": True, "running_sha": sha, "head_sha": sha, "current": True,
                          "root": str(repo)}
    assert st["linger"] is True and st["linger_fix"] is None
    assert st["logs"] == "journalctl --user -u nw-dashboard -f"
    assert st["ok"] and st["problems"] == [] and st["warnings"] == []
    commit(repo, "src/nightwright/a.py", "x = 2\n")                 # the checkout moved on
    st = LC.status(cfg, run=run)
    assert st["code"]["current"] is False and not st["ok"]
    assert "is not the checkout's" in st["problems"][0]


def test_status_problems_and_linger_hint(fx, xdg):
    cfg = fx["cfg"]
    LC.unit_path().parent.mkdir(parents=True)
    LC.unit_path().write_text("[Unit]\n")
    failed = FakeSystemd(active_props(ActiveState="failed", SubState="failed", MainPID="0",
                                      Result="exit-code", ExecMainStatus="2"), linger="no")
    st = LC.status(cfg, run=failed)
    assert not st["ok"] and st["unit"]["active"] is False and st["unit"]["exit_status"] == 2
    assert st["problems"][0].startswith("installed but not active (failed, result exit-code, "
                                        "last exit 2)")
    assert st["linger"] is False and st["linger_fix"] == f"loginctl enable-linger " \
                                                         f"{LC.current_user()}"
    assert any("loginctl enable-linger" in w for w in st["warnings"])
    st = LC.status(cfg, run=FakeSystemd(active_props()))             # active, no server.json
    assert "has not written server.json yet (starting up)" in st["problems"][0]
    st = LC.status(cfg, run=FakeSystemd(reachable=False))
    assert "cannot query `systemctl --user`" in st["problems"][0]


def test_status_without_a_unit_runs_nothing_for_doctor_and_conduct(fx, xdg):
    cfg = fx["cfg"]
    run = FakeSystemd()
    assert LC.brief(cfg, run=run) is None and run.calls == []
    assert CC._dashboard_status(cfg) is None
    c = D.check_dashboard(cfg, run_cmd=run)
    assert c["status"] == "skip" and run.calls == []
    live_record(cfg)                                                   # run by hand
    b = LC.brief(cfg, run=run)
    assert b["installed"] is False and b["running"] and run.calls == []
    assert LC.brief_line(b).startswith("dashboard: running by hand at http://127.0.0.1:8791")
    assert D.check_dashboard(cfg, run_cmd=run)["status"] == "ok"


def test_doctor_and_conduct_status_dashboard_line(fx, xdg, monkeypatch):
    cfg = fx["cfg"]
    LC.unit_path().parent.mkdir(parents=True)
    LC.unit_path().write_text("[Unit]\n")
    live_record(cfg)
    c = D.check_dashboard(cfg, run_cmd=FakeSystemd(active_props(pid=os.getpid())))
    assert c["id"] == "agentic.dashboard" and c["status"] == "ok"
    assert "dashboard active at http://127.0.0.1:8791" in c["summary"]
    c = D.check_dashboard(cfg, run_cmd=FakeSystemd(active_props(), linger="no"))
    assert c["status"] == "warn" and c["fix"].startswith("loginctl enable-linger")
    LC.remove_server_record(cfg)
    dead = FakeSystemd(active_props(ActiveState="failed", MainPID="0"))
    c = D.check_dashboard(cfg, run_cmd=dead)
    assert c["status"] == "warn" and "installed but not active" in c["summary"]
    assert "systemctl --user restart nw-dashboard.service" in c["fix"]
    # conduct-status carries the same view under `dashboard`
    monkeypatch.setattr(LC._subprocess, "run", dead)
    d = CC.conduct_status(cfg)["dashboard"]
    assert d["installed"] and d["active"] is False and not d["ok"]
    assert LC.brief_line(d) == ("dashboard: installed but NOT active (failed) · "
                                "nw dashboard status")
    ids = [c["id"] for c in D.run(cfg, which=lambda n: None, run_cmd=dead)]
    assert "agentic.dashboard" in ids


# ---- the CLI against a fake systemctl (AC 1, 4, 5) ----------------------------------------

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$0 $*" >> "$NW_FAKE_LOG"
case "$*" in
  *show*nw-dash-t.service*) cat "$NW_FAKE_SHOW" 2>/dev/null ;;
  *show*tailscaled*) echo not-found ;;
esac
exit 0
"""
FAKE_LOGINCTL = """#!/bin/sh
echo "$0 $*" >> "$NW_FAKE_LOG"
echo Linger=no
"""


def cli_env(fx, tmp_path):
    """The real PATH (the fakes need `cat`; `nw_env` drops /usr/bin where tailscale lives)
    behind a dir holding the fake systemctl / loginctl."""
    env = {**os.environ, **fx["env"]}
    bindir = tmp_path / "fakesys"
    bindir.mkdir()
    for name, body in (("systemctl", FAKE_SYSTEMCTL), ("loginctl", FAKE_LOGINCTL)):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", XDG_CONFIG_HOME=str(tmp_path / "xdg"),
               NW_FAKE_LOG=str(tmp_path / "calls.log"), NW_FAKE_SHOW=str(tmp_path / "show"))
    return env


def fake_nw(tmp_path) -> Path:
    p = tmp_path / "nwbin"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return p


def nw(env, *args):
    return subprocess.run([*nw_argv(), "dashboard", *args], env=env, capture_output=True,
                          text=True, timeout=120)


def calls(tmp_path):
    p = tmp_path / "calls.log"
    return [ln.split(" ", 1)[1] for ln in p.read_text().splitlines()] if p.exists() else []


def test_install_dry_run_prints_the_unit_and_changes_nothing(fx, tmp_path):
    env = cli_env(fx, tmp_path)
    r = nw(env, "install", "--dry-run", "--unit-name", "nw-dash-t", "--port", "9123",
           "--nw-bin", str(fake_nw(tmp_path)))
    assert r.returncode == 0, r.stderr
    unit = tmp_path / "xdg/systemd/user/nw-dash-t.service"
    assert f"# {unit}" in r.stdout and "Restart=always" in r.stdout
    assert "--bind auto:9123" in r.stdout and "--tailnet-wait" not in r.stdout   # SPEC-0432
    assert "systemctl --user daemon-reload\nsystemctl --user enable --now nw-dash-t.service" \
        in r.stdout
    # the scratch data / config dirs are not the defaults, so the unit carries them
    assert f"Environment=NW_DATA_DIR={fx['env']['NW_DATA_DIR']}" in r.stdout
    assert not unit.exists() and not any("enable" in c or "reload" in c for c in calls(tmp_path))


def test_install_status_uninstall_with_a_fake_systemctl(fx, tmp_path):
    env = cli_env(fx, tmp_path)
    nwbin = fake_nw(tmp_path)
    r = nw(env, "install", "--unit-name", "nw-dash-t", "--bind", "tailscale", "--nw-bin",
           str(nwbin))
    assert r.returncode == 0, r.stderr
    unit = tmp_path / "xdg/systemd/user/nw-dash-t.service"
    text = unit.read_text()
    assert f"ExecStart={nwbin} serve --dashboard --bind tailscale:8791 --tailnet-wait 60" in text
    assert "After=tailscaled.service" not in text        # the fake says tailscaled is not-found
    seq = [c for c in calls(tmp_path) if c.startswith("--user") and "show" not in c]
    assert seq == ["--user daemon-reload", "--user enable --now nw-dash-t.service"]
    # status: the unit is active (fake), no server is running; linger off prints the fix
    (tmp_path / "show").write_text("".join(f"{k}={v}\n" for k, v in active_props().items()))
    r = nw(env, "status", "--unit-name", "nw-dash-t")
    assert r.returncode == 1, r.stdout                   # active without a live server.json
    assert "installed" in r.stdout and "active (running)" in r.stdout
    assert f"loginctl enable-linger {LC.current_user()}" in r.stdout
    assert "journalctl --user -u nw-dash-t -f" in r.stdout
    assert not any("enable-linger" in c for c in calls(tmp_path))      # never run, only shown
    doc = json.loads(nw(env, "status", "--unit-name", "nw-dash-t", "--json").stdout)
    assert doc["unit"]["active"] is True and doc["linger"] is False and not doc["ok"]
    # re-install while active also restarts it onto the new unit text
    r = nw(env, "install", "--unit-name", "nw-dash-t", "--nw-bin", str(nwbin))
    assert r.returncode == 0, r.stderr
    assert "--user restart nw-dash-t.service" in calls(tmp_path)
    # uninstall
    open(tmp_path / "calls.log", "w").close()
    r = nw(env, "uninstall", "--unit-name", "nw-dash-t")
    assert r.returncode == 0, r.stderr
    assert not unit.exists() and "removed nw-dash-t.service" in r.stdout
    assert [c for c in calls(tmp_path) if c.startswith("--user")] == [
        "--user disable --now nw-dash-t.service", "--user reset-failed nw-dash-t.service",
        "--user daemon-reload"]
    r = nw(env, "uninstall", "--unit-name", "nw-dash-t")
    assert r.returncode == 0 and "is not installed" in r.stdout


# ---- --tailnet-wait (AC 3) -----------------------------------------------------------------

def test_tailnet_wait_retries_then_exits_3(fx, tmp_path):
    env = tailscale_env(fx, tmp_path, "exit 1")
    t0 = time.monotonic()
    r = serve_once(fx, env, "--bind", "tailscale:0", "--tailnet-wait", "2")
    assert r.returncode == 3, r.stderr
    assert time.monotonic() - t0 >= 2
    assert "retrying for up to 2s" in r.stderr and r.stderr.count("retrying") == 1
    assert not (dash(fx) / "server.json").exists()


def test_tailnet_wait_ends_as_soon_as_the_tailnet_is_up(fx, tmp_path):
    flag = tmp_path / "up"
    env = tailscale_env(fx, tmp_path, f'[ -e {flag} ] && [ "$1" = ip ] && echo 100.64.0.1')
    p = subprocess.Popen([*nw_argv(), "serve", "--dashboard", "--bind", "tailscale:0",
                          "--tailnet-wait", "120"], env=env, stderr=subprocess.PIPE, text=True)
    try:
        first = p.stderr.readline()
        assert "retrying for up to 120s" in first, first
        t0 = time.monotonic()
        flag.touch()
        rc = p.wait(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    # the wait ended on the first poll after the address appeared (not after 120 s, not exit
    # 3); what follows is the bind gate's own answer for that address
    assert rc != 3 and time.monotonic() - t0 < 30, p.stderr.read()

