"""SPEC-0434: the dashboard page outlives a server restart, and a web answer is in the very next
snapshot. Escalations come from the real CLI (`test_dashboard_answers.world`). A page and plan
token served by a real `nw serve --dashboard` process A are used against process B after a
SIGTERM and relaunch on the same port, as systemd does."""
import http.client
import json
import os
import re
import signal
import socket
import stat
import subprocess

import pytest

import waits
from nightwright.agentic import store as S
from nightwright.agentic.dashboard import auth as A
from nightwright.agentic.dashboard import server as DS
from nightwright.agentic.dashboard import snapshot as SN
from test_dashboard_answers import world  # noqa: F401 - the fixture
from test_dashboard_cli import nw_argv

CSRF_RE = re.compile(r'name="nw-csrf" content="([^"]+)"')


# ---- the secret file ---------------------------------------------------------------------

def test_the_secret_is_created_0600_and_reused(tmp_path):
    d = tmp_path / "dash"
    d.mkdir()
    a = A.load_secret(d)
    p = d / A.SECRET_NAME
    assert len(a) == A.SECRET_LEN and p.read_bytes() == a
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert A.load_secret(d) == a                              # the next process reads it back


@pytest.mark.parametrize("bad", ["short", "mode"])
def test_a_malformed_or_exposed_secret_is_replaced(tmp_path, bad):
    d = tmp_path / "dash"
    d.mkdir()
    p = d / A.SECRET_NAME
    if bad == "short":
        p.write_bytes(b"x" * 5)
        p.chmod(0o600)
    else:
        p.write_bytes(b"k" * A.SECRET_LEN)
        p.chmod(0o644)                                        # readable by others: leaked
    new = A.load_secret(d)
    assert len(new) == A.SECRET_LEN and new != b"k" * A.SECRET_LEN
    assert p.read_bytes() == new and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert [x.name for x in d.iterdir()] == [A.SECRET_NAME]    # no temp file left behind


def test_make_server_uses_the_persisted_secret(world):  # noqa: F811
    cfg = world["cfg"]
    a = DS.make_server(cfg, "127.0.0.1:0")
    b = DS.make_server(cfg, "127.0.0.1:0")
    try:
        v = a.csrf.mint("local")
        assert b.csrf.check(v, "local")
        assert (S.agentic_dir(cfg) / "dashboard" / A.SECRET_NAME).exists()
    finally:
        a.server_close()
        b.server_close()


# ---- a web answer is in the next snapshot ----------------------------------------------------

def test_the_snapshot_after_an_answer_is_fresh_at_once(world):  # noqa: F811
    """With a cache that would not re-check its watermark for an hour, the snapshot read right
    after a 200 answer no longer lists the escalation (SPEC-0434 `SnapshotCache.expire`)."""
    s = world["srv"]
    s.srv.cache = SN.SnapshotCache(world["cfg"], refresh_s=3600, max_stale_s=3600)
    first = json.loads(s.req("GET", "/api/snapshot")[2])
    assert "ESC-0001" in [e["id"] for e in first["inbox"]["escalations"]]
    st, out, _ = s.answer("ESC-0001", {"form": "choose", "choose": 1})
    assert st == 200, out
    st, hd, raw = s.req("GET", "/api/snapshot", headers={"If-None-Match": f'"{first["rev"]}"'})
    assert st == 200, "a 304 would leave the answered escalation on the page"
    assert "ESC-0001" not in [e["id"] for e in json.loads(raw)["inbox"]["escalations"]]


# ---- restart: process A's page answers through process B -----------------------------------

def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def env_for(cfg):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NW_DASHBOARD")}
    env.update(NW_DATA_DIR=cfg["data_dir"], NW_CONFIG_DIR=cfg["config_dir"])
    return env


def launch(cfg, port):
    proc = subprocess.Popen([*nw_argv(), "serve", "--dashboard", "--bind", f"127.0.0.1:{port}"],
                            env=env_for(cfg), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    assert waits.until(lambda: proc.poll() is not None or req(port, "GET", "/")[0] == 200), \
        proc.stderr.read() if proc.poll() is not None else "no answer"
    assert proc.poll() is None
    return proc


def stop(proc):
    proc.send_signal(signal.SIGTERM)
    try:
        proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.request(method, path, body=None if body is None else json.dumps(body).encode(),
                  headers={"Host": f"127.0.0.1:{port}", **(headers or {})})
        r = c.getresponse()
        return r.status, r.read()
    except OSError:
        return 0, b""
    finally:
        c.close()


def write(port, csrf, path, body):
    st, raw = req(port, "POST", path, body, {"Content-Type": "application/json",
                                             "Origin": f"http://127.0.0.1:{port}",
                                             "X-NW-CSRF": csrf})
    return st, json.loads(raw) if raw else None


def test_a_page_served_by_process_a_answers_through_process_b(world):  # noqa: F811
    cfg = world["cfg"]
    world["srv"].close()                                      # only the subprocesses serve
    port = free_port()
    a = launch(cfg, port)
    try:
        csrf = CSRF_RE.search(req(port, "GET", "/")[1].decode()).group(1)
        body = {"form": "choose", "choose": 1}
        st, plan = write(port, csrf, "/api/escalations/ESC-0001/plan", body)
        assert st == 200, plan
        st, other = write(port, csrf, "/api/escalations/ESC-0002/plan", body)
        assert st == 200, other
    finally:
        stop(a)
    assert req(port, "GET", "/")[0] == 0                      # A is gone
    b = launch(cfg, port)
    try:
        # the open confirm panel (A's plan token) still confirms
        st, out = write(port, csrf, "/api/escalations/ESC-0001/answer",
                        {**body, "plan_token": plan["plan_token"]})
        assert st == 200, out
        # and the same page plans and answers anew
        st, plan2 = write(port, csrf, "/api/escalations/ESC-0002/plan", body)
        assert st == 200, plan2
        st, out = write(port, csrf, "/api/escalations/ESC-0002/answer",
                        {**body, "plan_token": plan2["plan_token"]})
        assert st == 200, out
        # a forged value is still refused (SPEC-0428 unchanged)
        st, out = write(port, "x" + csrf, "/api/escalations/ESC-0006/plan", body)
        assert st == 403 and out["reason"] == "bad_csrf"
    finally:
        stop(b)
    audit = [json.loads(x) for x in
             (S.agentic_dir(cfg) / "dashboard" / "audit.jsonl").read_text().splitlines()]
    answers = [e for e in audit if e.get("action") == "answer" and e.get("result") == "ok"]
    assert [e["esc_id"] for e in answers] == ["ESC-0001", "ESC-0002"]
