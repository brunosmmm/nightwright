"""SPEC-0404: the dashboard server lifecycle: `server.json`, liveness, code-change exit 0, the
`code_check_s` setting and the import graph. Real subprocesses for the process-level cases."""
import ast
import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import nightwright
from fixtures.dashboard_data import AGENTIC, seed
from nightwright.agentic import codewatch as CW
from nightwright.agentic.dashboard import lifecycle as LC
from test_dashboard_cli import nw_argv, nw_env
from test_dashboard_serve_cli import dash, stop, wait_server
from test_dashboard_serve_cli import start as _start
from waits import join, until


@pytest.fixture
def fx(tmp_path):
    return seed(tmp_path / "seed")


def set_dashboard_cfg(fx, **dash_cfg):
    path = Path(fx["env"]["NW_CONFIG_DIR"]) / "config.yaml"
    path.write_text(yaml.safe_dump({"agentic": {**AGENTIC, "dashboard": dash_cfg}}))


def start(fx, *args, pythonpath=None):
    """`nw serve --dashboard` with `git` reachable (`nw_env` drops the PATH dirs that hold a
    `tailscale`, and on a host with tailscale installed that is /usr/bin)."""
    env = nw_env(fx)
    if pythonpath:
        env["PYTHONPATH"] = str(pythonpath)
    bindir = Path(fx["data"]).parent / "gitbin"
    bindir.mkdir(exist_ok=True)
    link = bindir / "git"
    if not link.exists():
        link.symlink_to(shutil.which("git"))
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return _start(fx, *args, env=env)


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "checkout"
    (root / "src" / "nightwright").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "nightwright" / "a.py").write_text("x = 1\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


def commit(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-qm", f"change {rel}")


# ---- code-change exit (AC 1, 2, 5) ---------------------------------------------------------

def test_server_exits_0_on_code_change(fx, repo):
    set_dashboard_cfg(fx, code_check_s=1)
    sj = dash(fx) / "server.json"
    proc = start(fx, "--bind", "127.0.0.1:0", "--code-root", str(repo))
    try:
        rec = wait_server(fx, proc)
        assert rec["code_watch"] is True and len(rec["code_sha"]) == 40
        commit(repo, "src/nightwright/a.py", "x = 2\n")
        assert until(lambda: proc.poll() is not None)
        _, err = proc.communicate(timeout=30)
    finally:
        stop(proc)
    assert proc.returncode == 0
    assert "code_changed" in err
    assert not sj.exists()


def test_docs_only_commit_keeps_running_and_sigterm_exits_0(fx, repo):
    set_dashboard_cfg(fx, code_check_s=1)
    sj = dash(fx) / "server.json"
    proc = start(fx, "--bind", "127.0.0.1:0", "--code-root", str(repo))
    try:
        assert wait_server(fx, proc)["code_watch"] is True
        commit(repo, "docs/note.md", "hello\n")
        # several poll intervals pass; HEAD moved, so the watcher has looked at the docs commit
        assert not until(lambda: proc.poll() is not None, timeout=4)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
    finally:
        stop(proc)
    assert proc.returncode == 0 and "code_changed" not in err
    assert not sj.exists()


NO_CHECKOUT = """
from nightwright.agentic import codewatch as CW
CW.code_checkout = lambda: None
"""


@pytest.mark.parametrize("how", ["flag", "zero", "not_git", "auto_no_checkout"])
def test_watcher_can_be_disabled(fx, repo, tmp_path, how):
    args, hook = ["--bind", "127.0.0.1:0"], None
    if how == "zero":
        set_dashboard_cfg(fx, code_check_s=0)
        args += ["--code-root", str(repo)]
    elif how == "flag":
        set_dashboard_cfg(fx, code_check_s=1)
        args += ["--code-root", str(repo), "--no-code-watch"]
    elif how == "not_git":                  # a directory that is not a git checkout
        set_dashboard_cfg(fx, code_check_s=1)
        plain = tmp_path / "plain"
        plain.mkdir()
        args += ["--code-root", str(plain)]
    else:                                   # no --code-root: `code_checkout()` is None (AUTO path)
        set_dashboard_cfg(fx, code_check_s=1)
        hook = tmp_path / "nocheckout"
        hook.mkdir()
        (hook / "sitecustomize.py").write_text(NO_CHECKOUT)
    proc = start(fx, *args, pythonpath=hook)
    try:
        rec = wait_server(fx, proc)
        assert rec["code_watch"] is False and rec["code_sha"] is None
        if how in ("flag", "zero"):         # a watched checkout exists: the commit must not stop it
            commit(repo, "src/nightwright/a.py", "x = 3\n")
            assert not until(lambda: proc.poll() is not None, timeout=4)
    finally:
        stop(proc)
    assert proc.returncode == 0


def test_no_git_checkout_means_no_watcher(monkeypatch):
    monkeypatch.setattr(CW, "code_checkout", lambda: None)
    t = LC.CodeWatchThread(__import__("threading").Event(), lambda c: None, 1)
    assert t.root is None and not t.enabled and LC.head_sha(t.root) is None


def test_code_watch_thread_calls_on_change_once(repo):
    import threading
    stop_ev, seen = threading.Event(), []
    t = LC.CodeWatchThread(stop_ev, seen.append, 0.05, repo, poll_s=0.05)
    t.start()
    commit(repo, "src/nightwright/a.py", "x = 9\n")
    assert until(lambda: seen)
    join(t)
    assert not t.is_alive() and len(seen) == 1 and seen[0]["paths"] == ["src/nightwright/a.py"]
    stop_ev.set()


# ---- exit 3 without a tailnet address (AC 3) -----------------------------------------------

def tailscale_env(fx, tmp_path, script):
    """`nw_env` with a fake `tailscale` first on PATH (`script` is its shell body), or none
    when `script` is None. `nw_env` drops every PATH dir that holds a real `tailscale`."""
    env = nw_env(fx)
    if script is not None:
        bindir = tmp_path / "fakebin"
        bindir.mkdir(exist_ok=True)
        fake = bindir / "tailscale"
        fake.write_text(f"#!/bin/sh\n{script}\n")
        fake.chmod(0o755)
        env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return env


def serve_once(fx, env, *args):
    return subprocess.run([*nw_argv(), "serve", "--dashboard", *args], env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("script", ["exit 1", "exit 0", None], ids=["fails", "silent", "missing"])
def test_server_exits_3_without_tailnet_address(fx, tmp_path, script):
    env = tailscale_env(fx, tmp_path, script)
    for args in (["--bind", "tailscale:0"], ["--bind", "tailscale"], ["--bind", "100.64.0.9:0"]):
        r = serve_once(fx, env, *args)
        assert r.returncode == 3, (args, r.stderr)
        assert "no tailnet address" in r.stderr and "tailscale up" in r.stderr, r.stderr
        assert not (dash(fx) / "server.json").exists()
        # exit 3 came before the bind: the process is gone and never announced a URL
        assert "dashboard on http" not in r.stderr and "job API" not in r.stderr


def test_tailnet_gate_keeps_exit_2_and_loopback_starts(fx, tmp_path):
    env = tailscale_env(fx, tmp_path, "[ \"$1\" = ip ] && echo 100.64.0.1")
    for bind in ("0.0.0.0:0", "100.64.0.9:0", "192.168.1.5:0"):   # wildcard, foreign tailnet, LAN
        r = serve_once(fx, env, "--bind", bind)
        assert r.returncode == 2, (bind, r.stderr)
        assert not (dash(fx) / "server.json").exists()
    # the same failing fake never stops a loopback bind (no tailscale is run for it)
    env = tailscale_env(fx, tmp_path, "exit 1")
    p = _start(fx, "--bind", "127.0.0.1:0", env=env)
    try:
        assert wait_server(fx, p)["port"] > 0
    finally:
        stop(p)
    assert p.returncode == 0


# ---- server.json (AC 4, 5) -----------------------------------------------------------------

def test_server_record_roundtrip_and_liveness(fx, tmp_path):
    cfg = fx["cfg"]
    assert LC.read_server_record(cfg) is None
    me = os.getpid()
    rec = {"schema": LC.SCHEMA, "pid": me, "ticks": LC.start_ticks(me), "clients": 0}
    LC.write_server_record(cfg, rec)
    assert LC.read_server_record(cfg) == rec
    assert LC.server_json_path(cfg) == fx["data"] / "agentic/dashboard/server.json"
    assert LC.record_live(rec)
    assert not LC.record_live({**rec, "ticks": (rec["ticks"] or 0) + 1})   # reused pid
    assert not LC.record_live({**rec, "pid": 999999})                      # dead pid
    if LC.start_ticks(me) is not None:                                     # /proc readable
        assert not LC.record_live({**rec, "ticks": None})
        assert not LC.record_live({k: v for k, v in rec.items() if k != "ticks"})
    assert not LC.record_live(None) and not LC.record_live({"pid": "x"})
    LC.remove_server_record(cfg)
    assert LC.read_server_record(cfg) is None
    LC.server_json_path(cfg).write_text("{not json")
    assert LC.read_server_record(cfg) is None
    LC.server_json_path(cfg).write_text("[1]")
    assert LC.read_server_record(cfg) is None
    LC.write_server_record(cfg, {**rec, "pid": me + 100000})               # foreign: kept
    LC.remove_server_record(cfg)
    assert LC.read_server_record(cfg) is not None


def test_server_record_writes_are_atomic(fx):
    import threading
    cfg, done, bad = fx["cfg"], threading.Event(), []
    LC.write_server_record(cfg, {"pid": 1, "n": 0})

    def reader():
        while not done.is_set():
            if LC.read_server_record(cfg) is None:
                bad.append("partial or empty")

    t = threading.Thread(target=reader)
    t.start()
    for n in range(300):
        LC.write_server_record(cfg, {"pid": 1, "n": n, "pad": "x" * 4000})
    done.set()
    join(t)
    assert not bad


def test_real_server_record_and_sigkill_reads_not_running(fx):
    proc = start(fx, "--bind", "127.0.0.1:0")
    try:
        wait_server(fx, proc)
        rec = LC.read_server_record(fx["cfg"])
        assert set(rec) >= {"schema", "pid", "ticks", "url", "host", "port", "bind", "scheme",
                            "started_at", "code_sha", "code_watch", "clients", "updated_at"}
        assert rec["pid"] == proc.pid and rec["clients"] == 0 and rec["scheme"] == "http"
        assert rec["ticks"] == LC.start_ticks(proc.pid) and LC.record_live(rec)
        proc.kill()
        proc.wait(30)
    finally:
        stop(proc)
    stale = LC.read_server_record(fx["cfg"])
    assert stale is not None and not LC.record_live(stale)


# ---- clients (AC 6) ------------------------------------------------------------------------

def test_clients_count_written_only_on_change(fx):
    cfg, me = fx["cfg"], os.getpid()
    base = {"schema": LC.SCHEMA, "pid": me, "ticks": LC.start_ticks(me), "clients": 0,
            "updated_at": "t0"}
    LC.write_server_record(cfg, base)
    now, count = [100.0], [0]
    r = LC.RecordRefresher(cfg, base, lambda: count[0], clock=lambda: now[0])
    now[0] += 60
    assert not r.tick() and r.writes == 0                  # idle: nothing rewritten
    count[0] = 2
    assert r.tick() and LC.read_server_record(cfg)["clients"] == 2
    count[0] = 0
    now[0] += 4.9
    assert not r.tick() and LC.read_server_record(cfg)["clients"] == 2   # under 5 s: held back
    now[0] += 0.2
    assert r.tick() and LC.read_server_record(cfg)["clients"] == 0
    stamp = LC.read_server_record(cfg)["updated_at"]
    now[0] += 60
    assert not r.tick() and LC.read_server_record(cfg)["updated_at"] == stamp
    assert r.writes == 2


def test_dashboard_server_client_count_is_the_open_streams(fx):
    """`clients` in `server.json` is the number of open live streams (the hub's slot pool)."""
    from nightwright.agentic.dashboard import server as DS
    srv = DS.make_server(fx["cfg"], "127.0.0.1:0")
    try:
        assert srv.client_count() == 0
        assert srv.hub.pool.acquire() and srv.hub.pool.acquire()
        assert srv.client_count() == 2
        srv.hub.pool.release()
        assert srv.client_count() == 1
    finally:
        srv.server_close()


SITECUSTOMIZE = """
from nightwright.agentic.dashboard import lifecycle as LC, server as DS
DS.DashboardServer.client_count = lambda self: 2
_init = LC.RecordRefresher.__init__
def init(self, cfg, rec, client_count, **kw):
    _init(self, cfg, rec, client_count, **{**kw, "min_s": 0.5})
LC.RecordRefresher.__init__ = init
"""


def test_real_serve_loop_feeds_client_count_into_the_record(fx, tmp_path):
    """The serve loop must hand `DashboardServer.client_count` to the refresher and tick it:
    a subprocess server whose hook returns 2 (sitecustomize) rewrites `clients` to 2, once."""
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(SITECUSTOMIZE)
    proc = start(fx, "--bind", "127.0.0.1:0", "--no-code-watch", pythonpath=hook)
    try:
        first = wait_server(fx, proc)
        assert until(lambda: (LC.read_server_record(fx["cfg"]) or {}).get("clients") == 2)
        rec = LC.read_server_record(fx["cfg"])
        assert rec["updated_at"] >= first["updated_at"] and rec["pid"] == proc.pid
        assert until(lambda: proc.poll() is not None, timeout=1.5) is False   # idle: no rewrite
        assert LC.read_server_record(fx["cfg"])["updated_at"] == rec["updated_at"]
    finally:
        stop(proc)


def test_clients_refresh_leaves_a_foreign_record_alone(fx):
    cfg, me = fx["cfg"], os.getpid()
    LC.write_server_record(cfg, {"pid": me + 100000, "clients": 0})
    now = [0.0]
    r = LC.RecordRefresher(cfg, {"pid": me, "clients": 0}, lambda: 3, clock=lambda: now[0])
    now[0] += 60
    assert not r.tick() and LC.read_server_record(cfg)["clients"] == 0


def test_clients_refresh_does_not_recreate_a_removed_record(fx):
    cfg = fx["cfg"]
    LC.write_server_record(cfg, {"pid": os.getpid(), "clients": 0})
    now = [0.0]
    r = LC.RecordRefresher(cfg, {"pid": os.getpid(), "clients": 0}, lambda: 3,
                           clock=lambda: now[0])
    LC.remove_server_record(cfg)
    assert LC.read_server_record(cfg) is None
    now[0] += 60
    assert not r.tick() and not LC.read_path(cfg).exists() and r.writes == 0


def test_reading_a_record_creates_nothing(tmp_path):
    cfg = {"data_dir": str(tmp_path / "never-run")}
    assert LC.read_server_record(cfg) is None
    assert not (tmp_path / "never-run").exists()


# ---- config (AC 7) -------------------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    (None, 10), (0, 0), (3, 3), (2.5, 2.5), ("fast", 10), (-1, 10), (True, 10),
    (float("inf"), 10), ([1], 10)])
def test_dashboard_config_code_check_s(raw, want):
    dash_cfg = {} if raw is None else {"code_check_s": raw}
    assert LC.code_check_s({"agentic": {"dashboard": dash_cfg}}) == want


def test_dashboard_config_code_check_s_missing_blocks():
    assert LC.code_check_s({}) == 10
    assert LC.code_check_s({"agentic": {"dashboard": "x"}}) == 10
    assert LC.code_check_s({"agentic": None}) == 10


# ---- import graph and writes (AC 8) --------------------------------------------------------

def test_lifecycle_import_graph():
    path = Path(nightwright.__file__).parent / "agentic" / "dashboard" / "lifecycle.py"
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            names |= {base} | {f"{base}.{a.name}".lstrip(".") if base else a.name
                               for a in node.names}
    banned = ("queue", "runner", "runproc", "events", "escalations", "lease", "mailbox",
              "herdr_autowatch")
    bad = [n for n in names if n.split(".")[-1] in banned]
    assert not bad, bad
    code = ("import sys, nightwright.agentic.dashboard.lifecycle;"
            "print(sorted(m for m in sys.modules if m.rsplit('.', 1)[-1] in "
            f"{banned!r} and m.startswith('nightwright.agentic.')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=120).stdout.strip()
    assert out == "[]"


def test_server_run_writes_only_under_the_dashboard_dir(fx, repo):
    def snap():
        return {str(p): p.read_bytes() for p in fx["data"].rglob("*")
                if p.is_file() and "agentic/dashboard" not in str(p)}

    before = snap()
    proc = start(fx, "--bind", "127.0.0.1:0", "--code-root", str(repo))
    try:
        wait_server(fx, proc)
    finally:
        stop(proc)
    assert snap() == before
