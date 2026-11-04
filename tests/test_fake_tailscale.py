"""SPEC-0416: self-test of the fake `tailscale` fixture."""
import subprocess

import pytest

from fixtures import fake_tailscale as FT


def run(env, *args):
    return subprocess.run(["tailscale", *args], env=env, capture_output=True, text=True,
                          timeout=30)


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


@pytest.mark.parametrize("sub", [["up"], ["set", "--operator=x"], ["serve"], ["cert", "h"],
                                 ["status", "--json"], ["whois", "--json", "100.64.0.9"],
                                 ["ip", "-4"], []])
def test_any_other_subcommand_exits_2(tmp_path, sub):
    env = FT.install(tmp_path)
    r = run(env, *sub)
    assert r.returncode == 2
    assert FT.calls(env) == [sub]
