"""SPEC-0416, SPEC-0417: self-test of the fake `tailscale` fixture."""
import json
import subprocess

import pytest

from fixtures import fake_tailscale as FT

pytestmark = pytest.mark.fast


def run(env, *args, timeout=30):
    return subprocess.run(["tailscale", *args], env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_ip_prints_own_ips_and_logs_argv(tmp_path):
    env = FT.install(tmp_path, {"own_ips": ["100.64.0.5", "fd7a:115c:a1e0::5"]})
    r = run(env, "ip")
    assert r.returncode == 0 and r.stdout.split() == ["100.64.0.5", "fd7a:115c:a1e0::5"]
    assert FT.calls(env) == [["ip"]]


def test_logged_out_is_the_real_binarys_output(tmp_path):
    env = FT.install(tmp_path, {"logged_out": True})
    r = run(env, "ip")
    assert r.returncode == 1 and "no current Tailscale IPs; state: NeedsLogin" in r.stderr


def test_modes(tmp_path):
    assert run(FT.install(tmp_path, mode="fail"), "ip").returncode == 1
    r = run(FT.install(tmp_path, mode="garbage"), "ip")
    assert r.returncode == 0 and "100." not in r.stdout


def test_hang_mode_outlasts_a_short_timeout(tmp_path):
    env = FT.install(tmp_path, mode="hang")
    with pytest.raises(subprocess.TimeoutExpired):
        run(env, "ip", timeout=0.5)


@pytest.mark.parametrize("sub", [["up"], ["set", "--operator=x"], ["serve"], ["cert", "h"],
                                 ["login"], ["status"], ["whois", "100.64.0.9"],
                                 ["whois", "--json"], ["status", "--json", "-x"],
                                 ["ip", "-4"], []])
def test_any_other_subcommand_exits_2(tmp_path, sub):
    env = FT.install(tmp_path)
    r = run(env, *sub)
    assert r.returncode == 2
    assert FT.calls(env) == [sub]


def js(env, *args):
    r = run(env, *args)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_status_prints_self_and_user_in_the_real_shape(tmp_path):
    env = FT.install(tmp_path)
    d = js(env, "status", "--json")
    assert d["Self"]["UserID"] == 7 and d["Self"]["DNSName"] == "host.tail1234.ts.net."
    assert d["Self"]["Tags"] == [] and d["User"]["7"]["LoginName"] == "me@example.com"
    assert FT.calls(env) == [["status", "--json"]]


def test_status_logged_out_has_no_self_and_exit_0(tmp_path):
    d = js(FT.install(tmp_path, {"logged_out": True}), "status", "--json")
    assert d == {"BackendState": "NeedsLogin"}


def test_status_tagged_self(tmp_path):
    env = FT.install(tmp_path, {"self": {"user_id": 9, "login": "tagged-devices",
                                         "dns_name": "h.ts.net.", "tags": ["tag:server"]}})
    assert js(env, "status", "--json")["Self"]["Tags"] == ["tag:server"]


def test_whois_known_peer(tmp_path):
    env = FT.install(tmp_path)
    d = js(env, "whois", "--json", "100.64.0.20")
    assert d["Node"]["ComputedName"] == "phone" and d["Node"]["Name"] == "phone.tail1234.ts.net."
    assert d["UserProfile"]["LoginName"] == "me@example.com" and not d["Node"].get("Tags")
    assert FT.calls(env) == [["whois", "--json", "100.64.0.20"]]


def test_whois_unknown_peer_exits_1(tmp_path):
    assert run(FT.install(tmp_path), "whois", "--json", "100.64.0.99").returncode == 1


def test_whois_tagged_peer(tmp_path):
    env = FT.install(tmp_path, {"peers": {"100.64.0.30": {
        "login": "tagged-devices", "name": "srv.ts.net.", "computed_name": "srv",
        "tags": ["tag:server"]}}})
    d = js(env, "whois", "--json", "100.64.0.30")
    assert d["Node"]["Tags"] == ["tag:server"]
    assert d["UserProfile"]["LoginName"] == "tagged-devices"


def test_whois_own_address_is_the_host_with_its_login(tmp_path):
    d = js(FT.install(tmp_path), "whois", "--json", "100.64.0.5")
    assert d["Node"]["ComputedName"] == "host" and d["Node"]["Name"] == "host.tail1234.ts.net."
    assert d["UserProfile"]["LoginName"] == "me@example.com"


def test_whois_logged_out_exits_1(tmp_path):
    env = FT.install(tmp_path, {"logged_out": True, "own_ips": ["100.64.0.5"]})
    assert run(env, "whois", "--json", "100.64.0.5").returncode == 1


def test_modes_apply_to_status_and_whois(tmp_path):
    assert run(FT.install(tmp_path, mode="fail"), "status", "--json").returncode == 1
    r = run(FT.install(tmp_path, mode="garbage"), "whois", "--json", "100.64.0.20")
    assert r.returncode == 0
    with pytest.raises(ValueError):
        json.loads(r.stdout)
    with pytest.raises(subprocess.TimeoutExpired):
        run(FT.install(tmp_path, mode="hang"), "status", "--json", timeout=0.5)


def test_per_subcommand_modes_override_the_global_mode(tmp_path):
    env = FT.install(tmp_path, {"own_ips": ["100.64.0.5"], "modes": {"whois": "fail"}})
    assert run(env, "whois", "--json", "100.64.0.20").returncode == 1
    assert run(env, "status", "--json").returncode == 0
    assert run(env, "ip").returncode == 0
    env = FT.install(tmp_path, {"own_ips": ["100.64.0.5"], "modes": {"status": "ok"}},
                     mode="fail")
    assert run(env, "status", "--json").returncode == 0
    assert run(env, "ip").returncode == 1
