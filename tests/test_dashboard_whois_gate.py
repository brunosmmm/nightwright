"""SPEC-0417: the dashboard whois `Gate` / `Verdict`, `_peer_ip`, `Gate.magic_name`, `bind_mode`
(unit: a fake `run` and an injected clock; plus the real `run_tailscale` through the fake
binary). Nothing here opens a socket."""
import ast
import json
import threading
from pathlib import Path

import pytest

import waits
from fixtures import fake_tailscale as FT
from nightwright.agentic.dashboard import auth as A

pytestmark = pytest.mark.fast

ME = "me@example.com"
PEER = "100.64.0.20"


def whois_out(login=ME, name="phone.tail1234.ts.net.", computed="phone", tags=None):
    node = {"Name": name, "Addresses": [f"{PEER}/32"]}
    if computed is not None:
        node["ComputedName"] = computed
    if tags is not None:
        node["Tags"] = tags
    prof = {} if login is None else {"LoginName": login}
    return json.dumps({"Node": node, "UserProfile": prof})


def status_out(login=ME, uid=7, dns="host.tail1234.ts.net.", tags=None, self_=True, users=True):
    d = {"BackendState": "Running"}
    if self_:
        d["Self"] = {"UserID": uid, "DNSName": dns, "Tags": tags or []}
        d["User"] = {str(uid): {"LoginName": login}} if users else {}
    return json.dumps(d)


class FakeRun:
    """A `run` double: `peers` maps ip -> whois output (or an exception to raise); `status` is
    the `status --json` output (or an exception). Records argv."""

    def __init__(self, peers=None, status=None):
        self.peers = {PEER: whois_out()} if peers is None else peers
        self.status = status_out() if status is None else status
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args == ["status", "--json"]:
            out = self.status
        elif args[:2] == ["whois", "--json"] and len(args) == 3:
            out = self.peers.get(args[2], A.TailscaleError("no match"))
        else:
            raise AssertionError(f"unexpected tailscale form {args!r}")
        if isinstance(out, Exception):
            raise out
        return out

    def n(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def gate(run=None, allowed=(), clock=None):
    run = run or FakeRun()
    return A.Gate(run=run, allowed_logins=allowed, clock=clock or Clock()), run


# --- criterion 1: the verdict table ---------------------------------------------------------
def test_allowed_login_verdict_carries_login_node_and_peer():
    g, run = gate()
    v = g.check(PEER)
    assert v == A.Verdict(True, "ok", login=ME, node="phone", node_ip=PEER, fresh=True)
    assert run.calls[-1] == ["whois", "--json", PEER]


def test_node_name_falls_back_to_name_without_trailing_dot():
    g, _ = gate(FakeRun({PEER: whois_out(computed=None)}))
    assert g.check(PEER).node == "phone.tail1234.ts.net"


def test_other_login_is_not_allowed():
    g, _ = gate(FakeRun({PEER: whois_out(login="eve@example.com")}))
    v = g.check(PEER)
    assert (v.allowed, v.reason, v.login) == (False, "login_not_allowed", "eve@example.com")


@pytest.mark.parametrize("out", [
    whois_out(tags=["tag:server"]), whois_out(login=None), whois_out(login=""),
    whois_out(login="tagged-devices"), whois_out(login="  "), whois_out(tags="tag:x")])
def test_tagged_or_userless_node_is_tagged_node(out):
    g, _ = gate(FakeRun({PEER: out}))
    v = g.check(PEER)
    assert (v.allowed, v.reason) == (False, "tagged_node")


@pytest.mark.parametrize("out", [
    A.TailscaleError("exit 1"), "<<not json>>", "", "[1, 2]", "null", '"str"', "{}",
    json.dumps({"Node": "x"})])
def test_failing_or_unreadable_whois_is_whois_failed(out):
    g, _ = gate(FakeRun({PEER: out}))
    v = g.check(PEER)
    assert (v.allowed, v.reason) == (False, "whois_failed")


def test_absurdly_nested_output_is_a_denied_verdict():
    g, _ = gate(FakeRun({PEER: "[" * 100000}))
    v = g.check(PEER)
    assert not v.allowed and v.reason in ("whois_failed", "gate_error")


def test_exception_raising_run_is_gate_error_never_an_exception():
    def boom(args):
        if args[0] == "status":
            return status_out()
        raise KeyError("x")
    g = A.Gate(run=boom, clock=Clock())
    v = g.check(PEER)
    assert (v.allowed, v.reason) == (False, "gate_error")


def test_a_run_raising_for_everything_is_still_a_denied_verdict():
    def boom(args):
        raise RuntimeError("x")
    v = A.Gate(run=boom, clock=Clock()).check(PEER)
    assert not v.allowed


@pytest.mark.parametrize("peer", ["127.0.0.1", "192.168.1.5", "8.8.8.8", "garbage", "", None,
                                  "::1", "::ffff:8.8.8.8", 12])
def test_non_tailnet_peer_never_runs_tailscale(peer):
    g, run = gate()
    v = g.check(peer)
    assert (v.allowed, v.reason) == (False, "not_tailnet_peer")
    assert run.calls == []


def test_ipv4_mapped_tailnet_peer_is_unmapped_before_whois():
    g, run = gate()
    v = g.check("::ffff:" + PEER)
    assert v.allowed and v.node_ip == PEER
    assert run.calls == [["status", "--json"], ["whois", "--json", PEER]]


def test_login_comparison_is_case_insensitive():
    g, _ = gate(FakeRun({PEER: whois_out(login="Me@Example.COM")}, status_out(login="ME@example.com")))
    assert g.check(PEER).allowed
    g, _ = gate(FakeRun({PEER: whois_out(login="Me@Example.COM")}), allowed=[" me@EXAMPLE.com "])
    assert g.check(PEER).allowed


def test_the_hosts_own_node_and_login_is_allowed_like_any_peer():
    own = "100.64.0.5"
    run = FakeRun({own: whois_out(name="host.tail1234.ts.net.", computed="host")})
    v = A.Gate(run=run, clock=Clock()).check(own)
    assert v.allowed and v.node == "host"


# --- criterion 2: the allowed set -----------------------------------------------------------
def test_empty_allowed_logins_means_the_own_login_only():
    g, run = gate(FakeRun({PEER: whois_out(login="eve@example.com"), "100.64.0.21": whois_out()}))
    assert g.check("100.64.0.21").allowed
    assert g.check(PEER).reason == "login_not_allowed"
    assert run.n("status") == 1


@pytest.mark.parametrize("status", [
    status_out(tags=["tag:server"]), status_out(self_=False), A.TailscaleError("x"),
    status_out(users=False), "<<junk>>", "[]", status_out(uid=0), "{}"])
def test_unknown_own_login_denies_every_peer(status):
    g, _ = gate(FakeRun(status=status))
    v = g.check(PEER)
    assert (v.allowed, v.reason) == (False, "self_login_unknown")


def test_explicit_list_is_exactly_the_set_and_status_is_never_run():
    run = FakeRun({PEER: whois_out(login=ME), "100.64.0.21": whois_out(login="bob@example.com")})
    g, _ = gate(run, allowed=["bob@example.com"])
    assert g.check("100.64.0.21").allowed
    assert g.check(PEER).reason == "login_not_allowed"      # the own login is not added
    assert run.n("status") == 0


def test_a_list_of_blanks_behaves_as_empty():
    g, run = gate(allowed=["", "   "])
    assert g.check(PEER).allowed and run.n("status") == 1


# --- criterion 3: the cache -----------------------------------------------------------------
def test_allowed_verdict_is_cached_for_60s():
    clock = Clock()
    g, run = gate(clock=clock)
    assert g.check(PEER).fresh
    clock.t += 59.9
    v = g.check(PEER)
    assert v.allowed and not v.fresh and run.n("whois") == 1
    clock.t += 0.1
    assert g.check(PEER).fresh and run.n("whois") == 2


def test_denied_verdict_is_cached_for_5s():
    clock = Clock()
    g, run = gate(FakeRun({PEER: A.TailscaleError("x")}), clock=clock)
    assert g.check(PEER).reason == "whois_failed"
    clock.t += 4.9
    v = g.check(PEER)
    assert v.reason == "whois_failed" and not v.fresh and run.n("whois") == 1
    clock.t += 0.1
    assert g.check(PEER).fresh and run.n("whois") == 2


def test_a_recovered_whois_is_allowed_after_the_denied_ttl():
    clock = Clock()
    run = FakeRun({PEER: A.TailscaleError("x")})
    g, _ = gate(run, clock=clock)
    assert not g.check(PEER).allowed
    run.peers[PEER] = whois_out()
    clock.t += 5
    assert g.check(PEER).allowed


def test_20_threads_for_one_peer_cause_one_whois_call():
    g, run = gate()
    release = threading.Event()
    inner = run.__call__

    def slow(args):
        if args[0] == "whois":
            release.wait(waits.WAIT_S)
        return inner(args)
    g._run = slow
    out = []
    threads = [threading.Thread(target=lambda: out.append(g.check(PEER))) for _ in range(20)]
    for t in threads:
        t.start()
    assert waits.until(lambda: g._locks.get(PEER, [None, 0])[1] == 20)
    release.set()
    for t in threads:
        waits.join(t)
    assert run.n("whois") == 1 and len(out) == 20 and all(v.allowed for v in out)
    assert sum(v.fresh for v in out) == 1
    assert g._locks == {}


def test_cache_never_holds_more_than_512_peers_and_evicts_the_oldest():
    peers = {f"100.64.{i // 250}.{i % 250 + 1}": whois_out() for i in range(600)}
    g, run = gate(FakeRun(peers))
    for ip in peers:
        g.check(ip)
        assert len(g._cache) <= A.CACHE_MAX
    assert len(g._cache) == 512
    first = next(iter(peers))
    n = run.n("whois")
    assert g.check(first).fresh and run.n("whois") == n + 1     # the oldest was evicted
    last = list(peers)[-1]
    assert not g.check(last).fresh


def test_expired_entries_are_evicted_before_live_ones():
    clock = Clock()
    peers = {f"100.64.{i // 250}.{i % 250 + 1}": whois_out() for i in range(520)}
    ips = list(peers)
    g, run = gate(FakeRun(peers), clock=clock)
    g.check(ips[0])                                 # allowed, 60 s
    for ip in ips[1:512]:
        g.check(ip)
    # make the 2nd..512th entries denied-short by swapping them in the cache expiry
    for ip in ips[1:512]:
        g._cache[ip] = (clock.t + 1, g._cache[ip][1])
    clock.t += 2
    g.check(ips[512])
    assert ips[0] in g._cache and len(g._cache) == 2


def test_a_login_that_stops_being_allowed_is_denied_once_its_entry_expires():
    clock = Clock()
    run = FakeRun()
    g, _ = gate(run, clock=clock)
    assert g.check(PEER).allowed
    run.peers[PEER] = whois_out(login="eve@example.com")
    clock.t += 59.9
    assert g.check(PEER).allowed                    # not before
    clock.t += 0.1
    assert g.check(PEER).reason == "login_not_allowed"


def test_an_own_login_that_changes_applies_after_the_60s_reread():
    clock = Clock()
    run = FakeRun()
    g, _ = gate(run, clock=clock)
    assert g.check(PEER).allowed
    run.status = status_out(login="other@example.com")
    clock.t += 60
    assert g.check(PEER).reason == "login_not_allowed"
    assert run.n("status") == 2


def test_failed_own_login_read_is_retried_after_5s_and_a_good_one_after_60s():
    clock = Clock()
    run = FakeRun(status=A.TailscaleError("down"))
    g, _ = gate(run, clock=clock)
    assert g.check(PEER).reason == "self_login_unknown"
    clock.t += 4.9
    assert g.check(PEER).reason == "self_login_unknown" and run.n("status") == 1
    run.status = status_out()
    clock.t += 0.1
    assert g.check(PEER).allowed and run.n("status") == 2
    # a second peer within 60 s does not re-read the good login
    run.peers["100.64.0.21"] = whois_out()
    clock.t += 59
    assert g.check("100.64.0.21").allowed and run.n("status") == 2
    clock.t += 1
    g.check("100.64.0.22")
    assert run.n("status") == 3


# --- criterion 4: helpers -------------------------------------------------------------------
@pytest.mark.parametrize("addr,want", [
    ("::ffff:100.64.0.9", "100.64.0.9"), ("100.64.0.9", "100.64.0.9"),
    ("fd7a:115c:a1e0::5%tailscale0", "fd7a:115c:a1e0::5"),
    ("fe80::1%eth0", "fe80::1"), ("::ffff:100.64.0.9%lo", "100.64.0.9"),
    ("FD7A:115C:A1E0:0:0:0:0:5", "fd7a:115c:a1e0::5"),
    ("garbage", None), ("", None), (None, None), ("100.64.0.9%", "100.64.0.9"), ("%x", None)])
def test_peer_ip(addr, want):
    assert A._peer_ip(addr) == want


@pytest.mark.parametrize("status,want", [
    (status_out(dns="Host.Tail1234.ts.net."), "host.tail1234.ts.net"),
    (A.TailscaleError("x"), None), (status_out(self_=False), None), (status_out(dns=""), None),
    ("<<junk>>", None)])
def test_magic_name(status, want):
    assert gate(FakeRun(status=status))[0].magic_name() == want


def test_magic_name_never_raises_and_shares_the_status_read():
    def boom(args):
        raise KeyError("x")
    assert A.Gate(run=boom, clock=Clock()).magic_name() is None
    g, run = gate()
    assert g.magic_name() == "host.tail1234.ts.net"
    g.check(PEER)
    assert run.n("status") == 1


@pytest.mark.parametrize("host,want", [
    ("127.0.0.1", "local"), ("::1", "local"), ("127.0.0.2", "local"),
    ("::ffff:127.0.0.1", "local"), ("100.64.0.5", "whois"), ("fd7a:115c:a1e0::1", "whois"),
    ("0.0.0.0", "whois"), ("tailscale", "whois"), ("", "whois")])
def test_bind_mode(host, want):
    assert A.bind_mode(host) == want


# --- criterion 5: the real run_tailscale through the fake binary -----------------------------
@pytest.fixture
def ts(tmp_path, monkeypatch):
    def use(scenario=None, mode="ok"):
        env = FT.install(tmp_path, scenario, mode)
        for k in ("PATH", "FAKE_TAILSCALE_STATE", "FAKE_TAILSCALE_MODE", "FAKE_TAILSCALE_LOG"):
            monkeypatch.setenv(k, env[k])
        return env
    return use


def test_real_run_tailscale_ok_allows_a_known_peer(ts):
    env = ts()
    g = A.Gate()
    v = g.check(PEER)
    assert v.allowed and v.login == ME and v.node == "phone"
    assert g.magic_name() == "host.tail1234.ts.net"
    assert not g.check("100.64.0.99").allowed                    # unknown peer: whois exit 1
    assert not g.check("8.8.8.8").allowed
    assert FT.calls(env) == [["status", "--json"], ["whois", "--json", PEER],
                             ["whois", "--json", "100.64.0.99"]]


@pytest.mark.parametrize("mode", ["fail", "garbage"])
def test_real_run_tailscale_failing_modes_deny(ts, mode):
    env = ts(mode=mode)
    g = A.Gate(allowed_logins=[ME])
    v = g.check(PEER)
    assert not v.allowed and v.reason == "whois_failed"
    assert g.magic_name() is None
    assert all(c in (["status", "--json"], ["whois", "--json", PEER]) for c in FT.calls(env))


def test_real_run_tailscale_hang_denies(ts, monkeypatch):
    import functools
    env = ts(mode="hang")
    g = A.Gate(run=functools.partial(A.run_tailscale, timeout=0.5))
    v = g.check(PEER)
    assert not v.allowed and v.reason == "self_login_unknown"
    assert FT.calls(env) == [["status", "--json"]]
    g = A.Gate(run=functools.partial(A.run_tailscale, timeout=0.5), allowed_logins=[ME])
    assert g.check(PEER).reason == "whois_failed"


def test_real_run_tailscale_logged_out_and_tagged_host_deny(ts):
    ts({"logged_out": True})
    v = A.Gate().check(PEER)
    assert (v.allowed, v.reason) == (False, "self_login_unknown")
    ts({"self": {"user_id": 9, "login": "tagged-devices", "dns_name": "h.ts.net.",
                 "tags": ["tag:server"]}, "own_ips": ["100.64.0.5"]})
    assert A.Gate().check(PEER).reason == "self_login_unknown"


def test_real_run_tailscale_same_host_connection_is_allowed_like_any_peer(ts):
    ts()
    v = A.Gate().check("100.64.0.5")
    assert v.allowed and v.login == ME and v.node == "host"


def test_gate_only_ever_runs_status_and_whois_forms(ts):
    env = ts()
    g = A.Gate()
    for p in ("100.64.0.20", "100.64.0.5", "127.0.0.1", "192.168.1.5", "::ffff:100.64.0.20",
              "100.64.0.77", "garbage"):
        g.check(p)
    g.magic_name()
    for c in FT.calls(env):
        assert c == ["status", "--json"] or (c[:2] == ["whois", "--json"] and len(c) == 3
                                             and A.is_tailnet_ip(c[2]))


# --- SPEC-0428: the gate is wired into the server, not the CLI -------------------------------
SRC = Path(A.__file__).parent


def _names(mod):
    tree = ast.parse((SRC / mod).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    return names


def test_the_server_wires_the_gate_and_the_cli_does_not():
    assert {"Gate", "bind_mode"} <= _names("server.py")
    assert not _names("../cli_worker.py") & {"Gate", "Verdict", "bind_mode"}


def test_resolve_bind_accepts_an_own_tailnet_address():
    assert A.resolve_bind("tailscale", own_fn=lambda: ["100.64.0.5"]) == ("100.64.0.5", 8791)
