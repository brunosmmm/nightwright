"""SPEC-0432: the default `auto` bind. One process serves loopback always and the host's tailnet
address whenever one exists; the auth mode belongs to the listening socket. Real sockets: the
"tailnet" listener binds a second loopback address (127.0.0.2) in `whois` mode with a fake gate
and an injected peer address (no tailnet in CI); a real `nw serve` subprocess runs against the
fake `tailscale`."""
import ast
import http.client
import json
import socket
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

import waits
from fixtures import fake_tailscale as FT
from fixtures.dashboard_data import seed
from nightwright.agentic import cli_worker
from nightwright.agentic.dashboard import auth as A
from nightwright.agentic.dashboard import lifecycle as LC
from nightwright.agentic.dashboard import server as DS
from nightwright.agentic.dashboard import stream as ST
from nightwright.agentic.dashboard.snapshot import SnapshotCache
from test_dashboard_cli import nw_env
from test_dashboard_serve_cli import dash, nw_argv, start, stop, wait_server

PEER = "100.101.1.2"
TAILNET = "127.0.0.2"           # stands in for this host's tailnet address


def _lo2_ok() -> bool:
    s = socket.socket()
    try:
        s.bind((TAILNET, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


needs_lo2 = pytest.mark.skipif(not _lo2_ok(), reason="127.0.0.2 is not bindable here")


# ---- pure parts ------------------------------------------------------------------------------

def test_auto_port():
    assert DS.auto_port("auto") == 8791 and DS.auto_port("AUTO:9000") == 9000
    assert DS.auto_port("auto:0") == 0
    for other in ("127.0.0.1:8791", "tailscale", "tailscale:1", None, ""):
        assert DS.auto_port(other) is None
    for bad in ("auto:x", "auto:70000", "auto:"):
        with pytest.raises(A.BindRefused):
            DS.auto_port(bad)
    assert DS.DEFAULT_SERVE_BIND == "auto:8791"


def test_tailnet_recheck_s():
    assert DS.tailnet_recheck_s({}) == 15.0
    for v, want in ((2, 2.0), (0.5, 0.5), (300, 300.0), (0, 15.0), (-1, 15.0), (301, 15.0),
                    ("5", 15.0), (True, 15.0)):
        assert DS.tailnet_recheck_s({"agentic": {"dashboard": {"tailnet_recheck_s": v}}}) == want


@pytest.mark.parametrize("state,reason,hint", [
    ("NeedsLogin", "logged out", "sudo tailscale up"),
    ("NoState", "logged out", "sudo tailscale up"),
    ("Stopped", "stopped", "sudo tailscale up"),
    ("Starting", "starting", None),
    ("Running", "no tailnet address", None)])
def test_tailnet_reason_from_backend_state(state, reason, hint):
    assert A.tailnet_reason(lambda args: json.dumps({"BackendState": state})) == (reason, hint)


def test_tailnet_reason_failures():
    def missing(args):
        raise A.TailscaleNotInstalled("cannot run `tailscale`")

    def down(args):
        raise A.TailscaleError("exited 1", "failed to connect to local tailscaled\n")

    def nonzero_json(args):
        raise A.TailscaleError("exited 1", json.dumps({"BackendState": "NeedsLogin"}))
    assert A.tailnet_reason(missing) == ("tailscale not installed", "install Tailscale")
    assert A.tailnet_reason(down) == ("tailscaled not running", "sudo systemctl start tailscaled")
    assert A.tailnet_reason(nonzero_json)[0] == "logged out"
    assert A.tailnet_reason(lambda a: 1 / 0)[0] == "no tailnet address"     # never raises


def test_local_mode_is_refused_off_loopback(tmp_path):
    fx = seed(tmp_path)
    with pytest.raises(ValueError, match="needs a loopback address"):
        DS.DashboardServer(fx["cfg"], ("100.64.0.5", 0), SnapshotCache(fx["cfg"]), mode="local")


def test_the_cli_passes_no_supervisor_test_seam():
    tree = ast.parse(Path(cli_worker.__file__).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "TailnetSupervisor"]
    assert calls
    for c in calls:
        assert not {k.arg for k in c.keywords} & {"own", "reason", "gate", "peer"}


# ---- the supervisor on real sockets ----------------------------------------------------------

class FakeRun:
    """A fake `tailscale` run function for the gate: own login me@example.com; whois says
    `self.login` for every peer."""

    def __init__(self):
        self.login = "me@example.com"

    def __call__(self, args, timeout=5.0):
        if args[:2] == ["status", "--json"]:
            return json.dumps({"Self": {"UserID": 7, "DNSName": "box.tail1234.ts.net."},
                               "User": {"7": {"LoginName": "me@example.com"}}})
        return json.dumps({"Node": {"ComputedName": "phone"},
                           "UserProfile": {"LoginName": self.login}})


@pytest.fixture
def dual(tmp_path):
    fx = seed(tmp_path)
    cfg = fx["cfg"]
    cache = SnapshotCache(cfg)
    hub = ST.Hub(cache, ST.SlotPool(ST.max_clients(cfg)))
    lo = DS.make_server(cfg, "127.0.0.1:0", cache=cache, mode="local", hub=hub)
    lo.heartbeat_s = 0.2
    t = threading.Thread(target=lo.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    port = lo.server_address[1]
    own = {"ips": []}
    ts = FakeRun()
    events, lines = [], []
    sup = DS.TailnetSupervisor(cfg, port, cache=cache, hub=hub, csrf=lo.csrf,
                               on_change=events.append, log=lines.append,
                               own=lambda: list(own["ips"]),
                               reason=lambda: ("logged out", "sudo tailscale up"),
                               gate=lambda: A.Gate(run=ts), peer=lambda addr: PEER)

    def req(addr, path="/api/snapshot", host=None, headers=None):
        c = http.client.HTTPConnection(addr, port, timeout=10)
        try:
            c.request("GET", path, headers={"Host": host or f"{addr}:{port}", **(headers or {})})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def audit():
        p = Path(cfg["data_dir"]) / "agentic" / "dashboard" / "audit.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    yield {"lo": lo, "sup": sup, "own": own, "ts": ts, "events": events, "lines": lines,
           "req": req, "port": port, "hub": hub, "audit": audit, "cfg": cfg}
    sup.stop()
    lo.shutdown()
    waits.join(t, 10)
    lo.server_close()
    hub.close()


def refused(addr, port) -> bool:
    s = socket.socket()
    s.settimeout(2)
    try:
        return s.connect_ex((addr, port)) != 0
    finally:
        s.close()


@needs_lo2
def test_supervisor_waits_binds_and_closes_live(dual):
    sup, own, req, port = dual["sup"], dual["own"], dual["req"], dual["port"]
    e = sup.poll()
    assert e == {"name": "tailnet", "auth": "whois", "state": "waiting", "url": None,
                 "reason": "logged out", "hint": "sudo tailscale up"}
    sup.poll()                                              # unchanged: no second line or event
    assert dual["lines"] == ["tailnet: waiting for a Tailscale address (logged out)"]
    assert len(dual["events"]) == 1
    assert req("127.0.0.1")[0] == 200 and refused(TAILNET, port)
    own["ips"] = [TAILNET]                                  # the address appears
    e = sup.poll()
    assert e["state"] == "listening" and e["url"] == f"http://{TAILNET}:{port}"
    assert dual["lines"][-1] == (f"tailnet: listening on http://{TAILNET}:{port} "
                                 "(tailscale whois identity gate)")
    srv = sup.server
    assert srv.mode == "whois" and srv.cache is dual["lo"].cache and srv.hub is dual["hub"]
    assert srv.csrf is dual["lo"].csrf
    assert req(TAILNET)[0] == 200 and req("127.0.0.1")[0] == 200
    dual["ts"].login = "eve@example.com"                    # an unknown login on the tailnet
    srv.gate._cache.clear()
    assert req(TAILNET)[0] == 403
    assert dual["audit"]()[-1]["reason"] == "login_not_allowed"
    own["ips"] = []                                         # the address goes away
    e = sup.poll()
    assert e["state"] == "waiting" and sup.server is None
    assert "tailnet: address gone, listener closed (loopback keeps serving)" in dual["lines"]
    assert refused(TAILNET, port) and req("127.0.0.1")[0] == 200


@needs_lo2
def test_a_failed_tailnet_bind_is_a_waiting_reason(dual):
    blocker = socket.socket()
    blocker.bind((TAILNET, dual["port"]))
    blocker.listen()
    try:
        dual["own"]["ips"] = [TAILNET]
        e = dual["sup"].poll()
        assert e["state"] == "waiting" and e["reason"].startswith(f"cannot bind {TAILNET}:")
        assert dual["req"]("127.0.0.1")[0] == 200
    finally:
        blocker.close()
    assert dual["sup"].poll()["state"] == "listening"       # retried at the next check


@needs_lo2
def test_modes_belong_to_the_socket_not_the_host_header(dual):
    req, port = dual["req"], dual["port"]
    dual["own"]["ips"] = [TAILNET]
    dual["sup"].poll()
    # the tailnet socket never accepts a loopback Host, even from an allowed peer
    for h in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
        assert req(TAILNET, host=h)[0] == 403
    assert dual["audit"]()[-1]["reason"] == "bad_host"
    assert req(TAILNET, host=f"box.tail1234.ts.net:{port}")[0] == 200     # MagicDNS name
    # the loopback socket never accepts the tailnet Host
    assert req("127.0.0.1", host=f"{TAILNET}:{port}")[0] == 403
    assert req("127.0.0.1", host=f"box.tail1234.ts.net:{port}")[0] == 403
    # and it never runs the gate: a denying whois does not touch loopback
    dual["ts"].login = "eve@example.com"
    assert req("127.0.0.1")[0] == 200
    assert dual["lo"].gate is None and dual["sup"].server.gate is not None


@needs_lo2
def test_closing_the_tailnet_listener_ends_its_streams_not_the_hub(dual):
    port = dual["port"]
    dual["own"]["ips"] = [TAILNET]
    dual["sup"].poll()

    def open_stream(addr):
        s = socket.create_connection((addr, port), timeout=10)
        s.sendall(f"GET /api/stream HTTP/1.1\r\nHost: {addr}:{port}\r\n\r\n".encode())
        buf = b""
        while b"event: snapshot" not in buf:
            chunk = s.recv(65536)
            assert chunk, buf
            buf += chunk
        return s
    tail_s, lo_s = open_stream(TAILNET), open_stream("127.0.0.1")
    dual["own"]["ips"] = []
    dual["sup"].poll()

    def ended():
        try:
            while True:
                chunk = tail_s.recv(65536)
                if not chunk:
                    return True
        except socket.timeout:
            return False
        except OSError:
            return True
    tail_s.settimeout(0.5)
    assert waits.until(ended)
    tail_s.close()
    # the loopback stream is still open and the shared hub still serves new streams
    lo_s.settimeout(0.5)
    try:
        assert lo_s.recv(1) != b""
    except socket.timeout:
        pass                                                # open and quiet: fine
    lo_s.close()
    assert open_stream("127.0.0.1").close() is None


# ---- the record and status -------------------------------------------------------------------

def test_status_lines_and_problems(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))     # no unit installed
    fx = seed(tmp_path)
    cfg = fx["cfg"]
    import os
    base = {"pid": os.getpid(), "ticks": LC.start_ticks(os.getpid()), "bind": "auto:8791",
            "url": "http://127.0.0.1:8791", "auth": "local", "started_at": "now", "clients": 0,
            "code_watch": False}
    local = {"name": "local", "auth": "local", "state": "listening",
             "url": "http://127.0.0.1:8791", "reason": None, "hint": None}
    wait = {"name": "tailnet", "auth": "whois", "state": "waiting", "url": None,
            "reason": "logged out", "hint": "sudo tailscale up"}
    up = {**wait, "state": "listening", "url": "http://100.64.0.5:8791", "reason": None,
          "hint": None}
    LC.write_server_record(cfg, {**base, "listeners": [local, wait]})
    st = LC.status(cfg, run=lambda *a, **k: None)
    assert st["problems"] == []                              # a waiting tailnet is no problem
    assert [LC.listener_line(x) for x in st["server"]["listeners"]] == [
        "http://127.0.0.1:8791/ (no login)",
        "waiting: Tailscale logged out — run: sudo tailscale up"]
    LC.write_server_record(cfg, {**base, "listeners": [local, up]})
    st = LC.status(cfg, run=lambda *a, **k: None)
    assert LC.listener_line(st["server"]["listeners"][1]) == "http://100.64.0.5:8791/ (whois)"
    b = {"installed": True, "active": True, "url": base["url"], "pid": 1, "problems": [],
         "warnings": [], "listeners": [local, wait]}
    assert LC.brief_line(b) == ("dashboard: active · local http://127.0.0.1:8791/ (no login) · "
                                "tailnet waiting: Tailscale logged out — run: sudo tailscale up "
                                "· pid 1")
    # tailnet-only and waiting: nothing is served, and status says why (not "no server.json")
    LC.write_server_record(cfg, {**base, "bind": "tailscale:8791", "url": None,
                                 "listeners": [wait]})
    st = LC.status(cfg, run=lambda *a, **k: None)
    assert st["running"] and len(st["problems"]) == 1
    assert "nothing is served: the tailnet-only bind tailscale:8791 is waiting: Tailscale " \
           "logged out — run: sudo tailscale up" in st["problems"][0]
    # a pre-0432 record still reads as one listener
    LC.write_server_record(cfg, base)
    assert LC.status(cfg, run=lambda *a, **k: None)["server"]["listeners"] == [local]


def test_install_change_report():
    old = LC.render_unit(nw_bin="/x/nw", bind="tailscale:8791")
    new = LC.render_unit(nw_bin="/x/nw", bind="auto:8791")
    rep = LC.unit_change_report(old, new)
    assert rep[0] == "bind tailscale:8791 (tailnet only) -> auto:8791 (loopback + tailnet)"
    assert any(x.startswith("- ExecStart=") and "--tailnet-wait 60" in x for x in rep)
    assert "+ ExecStart=/x/nw serve --dashboard --bind auto:8791" in rep
    assert LC.unit_change_report(new, new) == ["unit unchanged"]
    assert LC.unit_change_report(None, new) == []
    assert "--tailnet-wait" not in new


# ---- a real `nw serve` -----------------------------------------------------------------------

def test_real_serve_with_a_logged_out_tailscale_serves_loopback_and_waits(tmp_path):
    fx = seed(tmp_path)
    conf = Path(fx["env"]["NW_CONFIG_DIR"]) / "config.yaml"
    doc = yaml.safe_load(conf.read_text())
    doc["agentic"]["dashboard"] = {"tailnet_recheck_s": 0.3}
    conf.write_text(yaml.safe_dump(doc))
    env = FT.install(tmp_path, {"logged_out": True}, base_env=nw_env(fx))
    p = start(fx, "--bind", "auto:0", "--no-code-watch", env=env)
    try:
        rec = wait_server(fx, p)
        port = rec["port"]
        assert rec["host"] == "127.0.0.1" and rec["auth"] == "local"
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/api/snapshot")
        assert c.getresponse().status == 200
        c.close()

        def waiting():
            ls = json.loads((dash(fx) / "server.json").read_text()).get("listeners") or []
            return len(ls) == 2 and ls[1]["state"] == "waiting" and \
                ls[1]["reason"] == "logged out"
        assert waits.until(waiting)
        n = len(FT.calls(env))
        assert waits.until(lambda: len(FT.calls(env)) >= n + 4)     # it keeps re-checking
        assert p.poll() is None                                     # and never exits
        r = subprocess.run([*nw_argv(), "dashboard", "status", "--unit-name",
                            "nw-dashboard-test-absent"], env=env, capture_output=True,
                           text=True, timeout=60)
        assert f"local    http://127.0.0.1:{port}/ (no login)" in r.stdout
        assert "tailnet  waiting: Tailscale logged out — run: sudo tailscale up" in r.stdout
        assert "problem:" not in r.stdout and r.returncode == 0, r.stdout
    finally:
        _, err = stop(p)
    assert p.returncode == 0
    assert err.count("tailnet: waiting for a Tailscale address (logged out)") == 1
    assert not (dash(fx) / "server.json").exists()


def test_real_tailnet_only_wait_writes_a_waiting_record(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))     # no unit installed
    fx = seed(tmp_path)
    env = FT.install(tmp_path, {"logged_out": True}, base_env=nw_env(fx))
    p = start(fx, "--bind", "tailscale:0", "--tailnet-wait", "30", env=env)
    try:
        path = dash(fx) / "server.json"
        assert waits.until(path.exists)
        rec = json.loads(path.read_text())
        assert rec["url"] is None and rec["listeners"][0]["state"] == "waiting"
        st = LC.status(fx["cfg"], run=lambda *a, **k: None)
        assert st["running"] and "nothing is served" in st["problems"][0]
    finally:
        stop(p)
    assert not (dash(fx) / "server.json").exists()


@needs_lo2
def test_a_changed_tailnet_address_rebinds(dual):
    other = "127.0.0.3"
    try:
        socket.create_server((other, 0)).close()
    except OSError:
        pytest.skip("127.0.0.3 is not bindable here")
    dual["own"]["ips"] = [TAILNET]
    assert dual["sup"].poll()["url"] == f"http://{TAILNET}:{dual['port']}"
    dual["own"]["ips"] = [other]
    e = dual["sup"].poll()
    assert e["state"] == "listening" and e["url"] == f"http://{other}:{dual['port']}"
    assert refused(TAILNET, dual["port"]) and dual["req"](other)[0] == 200
