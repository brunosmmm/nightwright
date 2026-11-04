"""SPEC-0416: tailnet address classes, `own_tailnet_ips` and the `resolve_bind` table (unit, no
sockets)."""
import pytest

from fixtures import fake_tailscale as FT
from nightwright.agentic.dashboard import auth as A

OWN = ["100.64.0.5"]


@pytest.mark.parametrize("addr,want", [
    ("100.63.255.255", False), ("100.64.0.0", True), ("100.127.255.255", True),
    ("100.128.0.0", False), ("fd7a:115c:a1e0::1", True), ("fd7a:115c:a1e0:ffff::1", True),
    ("fd7a:115c:a1e1::1", False), ("::ffff:100.64.0.9", True), ("::ffff:100.128.0.0", False),
    ("192.168.1.20", False), ("127.0.0.1", False), ("tailscale", False), ("", False)])
def test_is_tailnet_ip(addr, want):
    assert A.is_tailnet_ip(addr) is want


@pytest.fixture
def ts(tmp_path, monkeypatch):
    def use(scenario=None, mode="ok"):
        env = FT.install(tmp_path, scenario, mode)
        monkeypatch.setenv("PATH", env["PATH"])
        for k in ("FAKE_TAILSCALE_STATE", "FAKE_TAILSCALE_MODE", "FAKE_TAILSCALE_LOG"):
            monkeypatch.setenv(k, env[k])
        return env
    return use


def quick(args):
    return A.run_tailscale(args, timeout=0.5)


def test_own_tailnet_ips_ipv4_first_deduplicated_and_filtered(ts):
    ts({"own_ips": ["fd7a:115c:a1e0::5", "100.64.0.5", "8.8.8.8", "100.64.0.5", "junk"]})
    assert A.own_tailnet_ips() == ["100.64.0.5", "fd7a:115c:a1e0::5"]


def test_own_tailnet_ips_ipv6_only(ts):
    ts({"own_ips": ["fd7a:115c:a1e0::5"]})
    assert A.own_tailnet_ips() == ["fd7a:115c:a1e0::5"]


def test_own_tailnet_ips_only_runs_tailscale_ip(ts):
    env = ts()
    A.own_tailnet_ips()
    assert FT.calls(env) == [["ip"]]


@pytest.mark.parametrize("scenario,mode", [
    ({"logged_out": True}, "ok"), (None, "fail"), (None, "garbage"), ({"own_ips": []}, "ok"),
    ({"own_ips": ["192.168.1.2", "8.8.8.8"]}, "ok"), (None, "hang")])
def test_own_tailnet_ips_failure_modes_are_tailnet_missing(ts, scenario, mode):
    ts(scenario, mode)
    with pytest.raises(A.TailnetMissing, match="tailscale up"):
        A.own_tailnet_ips(run=quick)


def test_own_tailnet_ips_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(A.TailnetMissing, match="tailscale up"):
        A.own_tailnet_ips()


def test_run_tailscale_non_utf8_output_is_a_tailscale_error(monkeypatch):
    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")
    monkeypatch.setattr(A.subprocess, "run", boom)
    with pytest.raises(A.TailscaleError):
        A.run_tailscale(["ip"])
    with pytest.raises(A.TailnetMissing):
        A.own_tailnet_ips()


def test_run_tailscale_uses_an_argv_list_without_a_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return A.subprocess.CompletedProcess(argv, 0, "100.64.0.5\n", "")
    monkeypatch.setattr(A.subprocess, "run", fake_run)
    assert A.own_tailnet_ips() == ["100.64.0.5"]
    assert seen["argv"] == ["tailscale", "ip"] and seen["timeout"] == 5.0
    assert not seen.get("shell")


def test_tailnet_missing_is_not_a_bind_refused():
    assert not issubclass(A.TailnetMissing, A.BindRefused)
    assert not issubclass(A.BindRefused, A.TailnetMissing)


class Own:
    def __init__(self, ips=OWN, exc=None):
        self.ips, self.exc, self.calls = ips, exc, 0

    def __call__(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return list(self.ips)


# (spec, rule word, own_fn calls)
REFUSED = [
    ("0.0.0.0:0", "wildcard", 0), ("[::]:0", "wildcard", 0), ("::", "wildcard", 0),
    ("192.168.1.20:0", "not a loopback or tailnet address", 0),
    ("8.8.8.8:0", "not a loopback or tailnet address", 0),
    ("169.254.1.1:0", "not a loopback or tailnet address", 0),
    ("[fe80::1]:0", "not a loopback or tailnet address", 0),
    ("example.com:8791", "not an IP", 0),
    ("100.64.0.9:0", "foreign tailnet address", 1),
    ("[fd7a:115c:a1e0::9]:0", "foreign tailnet address", 1),
    ("tailscale", "tailnet serving needs the identity gate", 1),
    ("TAILSCALE", "tailnet serving needs the identity gate", 1),
    ("tailscale:9000", "tailnet serving needs the identity gate", 1),
    ("tailscale:0", "tailnet serving needs the identity gate", 1),
    ("100.64.0.5:0", "tailnet serving needs the identity gate", 1),
]


@pytest.mark.parametrize("spec,word,calls", REFUSED)
def test_resolve_bind_refuses_with_the_rule(spec, word, calls):
    own = Own()
    with pytest.raises(A.BindRefused, match="refusing to bind") as e:
        A.resolve_bind(spec, own_fn=own)
    assert word in str(e.value)
    assert own.calls == calls


@pytest.mark.parametrize("spec,want", [("127.0.0.1:0", ("127.0.0.1", 0)),
                                       ("localhost:8791", ("127.0.0.1", 8791)),
                                       ("[::1]:9", ("::1", 9)), ("::ffff:127.0.0.1", None)])
def test_resolve_bind_loopback_never_calls_own_fn(spec, want):
    own = Own(exc=A.TailnetMissing("x"))
    if want:
        assert A.resolve_bind(spec, own_fn=own) == want
    else:
        assert A.resolve_bind(spec, own_fn=own)[1] == A.DEFAULT_PORT
    assert own.calls == 0


def test_tailscale_resolves_to_the_first_own_address_and_port():
    for spec, port in (("tailscale", 8791), ("tailscale:9000", 9000)):
        with pytest.raises(A.BindRefused, match=r"bind 100\.64\.0\.5: tailnet serving"):
            A.resolve_bind(spec, own_fn=Own(["100.64.0.5", "fd7a:115c:a1e0::5"]))
    with pytest.raises(A.BindRefused, match=r"bind fd7a:115c:a1e0::5: tailnet"):
        A.resolve_bind("tailscale", own_fn=Own(["fd7a:115c:a1e0::5"]))


@pytest.mark.parametrize("spec", ["tailscale", "TAILSCALE:9", "100.64.0.9:0", "100.64.0.5:0",
                                  "[fd7a:115c:a1e0::9]:0"])
def test_no_tailnet_is_tailnet_missing_even_for_an_explicit_address(spec):
    own = Own(exc=A.TailnetMissing("no tailnet address: run `tailscale up`"))
    with pytest.raises(A.TailnetMissing, match="tailscale up"):
        A.resolve_bind(spec, own_fn=own)
    assert own.calls == 1


@pytest.mark.parametrize("host,word", [
    ("127.0.0.1", None), ("0.0.0.0", "wildcard"), ("192.168.1.20", "loopback or tailnet"),
    ("100.64.0.9", "foreign tailnet address"), ("fd7a:115c:a1e0::1", "foreign tailnet address"),
    ("tailscale", "not an IP")])
def test_check_bind_default_own_makes_every_tailnet_address_foreign(host, word):
    why = A.check_bind(host)
    assert (why is None) if word is None else (why and word in why)


def test_check_bind_accepts_an_own_tailnet_address_in_any_spelling():
    assert A.check_bind("100.64.0.5", own=["100.64.0.5"]) is None
    assert A.check_bind("::ffff:100.64.0.5", own=["100.64.0.5"]) is None
    assert A.check_bind("100.64.0.6", own=["100.64.0.5"])


def test_validate_bind_stays_loopback_only_with_a_loopback_message():
    with pytest.raises(A.BindRefused, match="loopback"):
        A.validate_bind("100.64.0.5:0")
