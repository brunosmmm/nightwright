"""SPEC-0337: the worker registry, `nw workers`, `gate_pools`, and the runner's report of an
unreadable `workers.json`. Offline: the SSH transport is a local script (`ssh_argv`) that runs
the remote command on this host against a temp data dir."""
import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import events as E
from nightwright.agentic import lease as L
from nightwright.agentic import queue as Q
from nightwright.agentic import workers as WK
from nightwright.cli import cli
from waits import join

FUTURE = "2999-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _short_runtime_dir(monkeypatch):
    """A long pytest tmp path makes the ssh socket dir fall back to $XDG_RUNTIME_DIR: keep
    those dirs out of the real one."""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="nwr")
    monkeypatch.setenv("XDG_RUNTIME_DIR", d)
    yield
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"gates": {"slots": 3}}}


@pytest.fixture
def invoke(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)

    def run(*args):
        return CliRunner().invoke(cli, ["workers", *args])
    return run


def _script(tmp_path, name, body):
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


@pytest.fixture
def loopback(tmp_path):
    """`[ssh_argv, nw]`: the ssh stand-in drops the destination and runs the rest locally; the
    `nw` it runs is the real `nw worker info` against a temp data dir."""
    ssh = _script(tmp_path, "fake-ssh", 'shift\nexec "$@"')
    nw = _script(tmp_path, "fake-nw", f'NW_DATA_DIR={tmp_path / "wdata"} NW_CONFIG_DIR='
                 f'{tmp_path / "wcfg"} exec {sys.executable} -m nightwright "$@"')
    return [ssh], nw


def _seed(cfg, *records):
    p = WK.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": list(records)}))


def _w(name, **kw):
    return {"name": name, "ssh": f"me@{name}", "slots": 1, "kinds": ["gate"], "nw": "nw",
            "enabled": True, **kw}


def _names(cfg):
    return [w["name"] for w in WK.load(cfg)]


# ---- registry -------------------------------------------------------------------------------

def test_missing_or_empty_registry_is_no_workers(cfg):
    assert WK.load(cfg) == [] and WK.gate_pools(cfg) == [(None, 3)]
    WK.path(cfg).parent.mkdir(parents=True)
    WK.path(cfg).write_text("")
    assert WK.load(cfg) == []


@pytest.mark.parametrize("text", ["{not json", "[]", '{"schema": "other", "workers": []}',
                                  '{"schema": "nw.workers.v1", "workers": {}}',
                                  '{"schema": "nw.workers.v1", "workers": [{"name": "Bad!"}]}'])
def test_unreadable_registry_loads_no_workers_and_writes_nothing(cfg, text):
    WK.path(cfg).parent.mkdir(parents=True)
    WK.path(cfg).write_text(text)
    assert WK.load(cfg) == []
    with pytest.raises(ValueError):
        WK.read_strict(cfg)
    with pytest.raises(ValueError):                  # an update never overwrites what it can't read
        WK.update(cfg, lambda recs: recs.append(_w("a")))
    assert WK.path(cfg).read_text() == text
    assert not list(WK.path(cfg).parent.glob("events*"))


def test_load_fills_defaults_including_ssh_argv(cfg):
    _seed(cfg, {"name": "box", "ssh": "me@box"})
    (w,) = WK.load(cfg)
    assert w["slots"] == 1 and w["kinds"] == ["gate"] and w["enabled"] and w["nw"] == "nw"
    assert w["ssh_argv"] == WK.default_ssh_argv(cfg)
    assert "ConnectTimeout=10" in w["ssh_argv"]
    assert f"ControlPath={WK.ssh_dir(cfg)}/%C" in w["ssh_argv"]
    _seed(cfg, {"name": "box", "ssh": "me@box", "ssh_argv": ["x", "-y"]})
    assert WK.load(cfg)[0]["ssh_argv"] == ["x", "-y"]


def test_ensure_ssh_dir_is_private(cfg):
    d = WK.ensure_ssh_dir(cfg)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def _bound(argv):
    (cp,) = [a for a in argv if a.startswith("ControlPath=")]
    return len(os.fsencode(cp.removeprefix("ControlPath="))) - len("%C") + 40 + 17


def test_default_control_path_fits_a_unix_socket(monkeypatch):
    argv = WK.default_ssh_argv({"data_dir": "/tmp/nwd"})
    assert "ControlPath=/tmp/nwd/agentic/ssh/%C" in argv and _bound(argv) <= WK.SOCK_MAX
    long_cfg = {"data_dir": "/home/some-long-username/work/nightwright-data-dir/with/more/parts"}
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    argv = WK.default_ssh_argv(long_cfg)
    assert _bound(argv) <= WK.SOCK_MAX
    assert f"ControlPath=/tmp/nw-ssh-{os.getuid()}-" in " ".join(argv)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert "ControlPath=/run/user/1000/nw-ssh-" in " ".join(WK.default_ssh_argv(long_cfg))
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/x" * 40)     # too long itself: /tmp again
    assert "ControlPath=/tmp/nw-ssh-" in " ".join(WK.default_ssh_argv(long_cfg))


def test_no_control_options_when_nothing_fits(monkeypatch):
    monkeypatch.setattr(WK, "SOCK_MAX", 10)
    argv = WK.default_ssh_argv({"data_dir": "/tmp/x"})
    assert not any(a.startswith("Control") for a in argv)
    assert WK.ensure_ssh_dir({"data_dir": "/tmp/x"}) is None


def test_fallback_ssh_dir_is_private_and_refused_when_foreign(tmp_path, monkeypatch):
    long_cfg = {"data_dir": str(tmp_path / ("x" * 90))}
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    d = WK.ensure_ssh_dir(long_cfg)                      # /tmp/nw-ssh-<uid>-<hash>, unique per tmp_path
    try:
        assert d.parent == Path("/tmp") and stat.S_IMODE(d.stat().st_mode) == 0o700
        d.rmdir()
        d.symlink_to(tmp_path)                           # a planted symlink is refused
        with pytest.raises(OSError):
            WK.ensure_ssh_dir(long_cfg)
    finally:
        d.unlink(missing_ok=True)
        if d.exists():
            d.rmdir()


@pytest.mark.parametrize("rec", [
    {"name": "a", "ssh": "-oFoo"}, {"name": "a", "ssh": "h h"},
    {"name": "a", "ssh": "h", "nw": "-x"}, {"name": "a", "ssh": "h", "nw": "nw; id"}])
def test_hand_edited_option_like_values_make_the_registry_unreadable(cfg, rec):
    _seed(cfg, rec)
    assert WK.load(cfg) == []
    with pytest.raises(ValueError):
        WK.read_strict(cfg)


def test_unparseable_cooldown_counts_as_not_cooling_and_naive_now_works(cfg):
    _seed(cfg, {"name": "a", "ssh": "h", "cooldown_until": "not a time"})
    assert WK.gate_pools(cfg) == [("a", 1)]
    import datetime as dt
    assert WK.in_cooldown({"cooldown_until": FUTURE}, dt.datetime.now())
    assert not WK.in_cooldown({"cooldown_until": PAST}, dt.datetime.now())


def test_concurrent_updates_both_persist(cfg):
    names = [f"w{i}" for i in range(12)]
    threads = [threading.Thread(target=WK.update, args=(cfg, lambda recs, n=n: recs.append(_w(n))))
               for n in names]
    for t in threads:
        t.start()
    for t in threads:
        join(t)
    assert sorted(_names(cfg)) == sorted(names)


def test_update_that_raises_leaves_the_file_alone(cfg):
    _seed(cfg, _w("a"))
    before = WK.path(cfg).read_text()

    def boom(recs):
        recs.clear()
        raise RuntimeError("no")
    with pytest.raises(RuntimeError):
        WK.update(cfg, boom)
    assert WK.path(cfg).read_text() == before


# ---- gate_pools -----------------------------------------------------------------------------

def test_gate_pools_filters_and_orders(cfg):
    _seed(cfg, _w("first", slots=2), _w("off", enabled=False),
          _w("cooling", cooldown_until=FUTURE), _w("nogate", kinds=["persona"]),
          _w("cooled", cooldown_until=PAST), _w("last"))
    assert WK.gate_pools(cfg) == [("first", 2), ("cooled", 1), ("last", 1)]   # SPEC-0374


def test_gate_pools_local_only_without_a_registry(cfg):
    assert WK.gate_pools(cfg) == [(None, 3)]
    assert WK.gate_pools({"data_dir": cfg["data_dir"]}) == [(None, 2)]


# ---- the CLI --------------------------------------------------------------------------------

def test_add_list_off_on_rm_cycle(cfg, invoke):
    res = invoke("add", "box", "--ssh", "me@box", "--slots", "2", "--no-check")
    assert res.exit_code == 0, res.output
    raw = json.loads(WK.path(cfg).read_text())
    assert raw["schema"] == "nw.workers.v1"
    (rec,) = raw["workers"]
    assert rec["name"] == "box" and rec["ssh"] == "me@box" and rec["slots"] == 2
    assert rec["kinds"] == ["gate"] and rec["enabled"] is True and rec["added_at"]
    assert "ssh_argv" not in rec                        # the default is applied at load
    assert invoke("add", "two", "--ssh", "b", "--no-check").exit_code == 0
    out = json.loads(invoke("list", "--json").output)
    assert out["schema"] == "nw.workers.list.v1"
    assert [(w["name"], w["slots"], w["enabled"], w["held"]) for w in out["workers"]] \
        == [("box", 2, True, 0), ("two", 1, True, 0)]
    assert "box" in invoke("list").output
    assert invoke("off", "box").exit_code == 0
    assert _names(cfg) == ["box", "two"] and not WK.load(cfg)[0]["enabled"]
    assert WK.gate_pools(cfg) == [("two", 1)]
    assert invoke("on", "box").exit_code == 0 and WK.load(cfg)[0]["enabled"]
    assert invoke("rm", "box").exit_code == 0
    assert _names(cfg) == ["two"]
    for cmd in ("on", "off", "rm", "check"):
        assert invoke(cmd, "nope").exit_code != 0


def test_on_clears_the_cooldown(cfg, invoke):
    _seed(cfg, _w("box", cooldown_until=FUTURE))
    assert WK.gate_pools(cfg) == [(None, 3)]
    assert invoke("list", "--json").output and \
        json.loads(invoke("list", "--json").output)["workers"][0]["cooldown_until"] == FUTURE
    assert invoke("on", "box").exit_code == 0
    assert WK.load(cfg)[0]["cooldown_until"] is None
    assert WK.gate_pools(cfg) == [("box", 1)]


@pytest.mark.parametrize("args", [
    ["Bad", "--ssh", "h"], ["local", "--ssh", "h"], ["-x", "--ssh", "h"], ["a" * 32, "--ssh", "h"],
    ["ok", "--ssh", "-oProxyCommand=x"], ["ok", "--ssh", "-oFoo"], ["ok", "--ssh", "-F/tmp/x"], ["ok", "--ssh", "h h"], ["ok", "--ssh", "h;rm"],
    ["ok", "--ssh", "h", "--slots", "0"], ["ok", "--ssh", "h", "--slots", "17"],
    ["ok", "--ssh", "h", "--nw", "-x"], ["ok", "--ssh", "h", "--nw", "nw; id"]])
def test_add_refuses_bad_input(cfg, invoke, args):
    res = invoke("add", *args, "--no-check")
    assert res.exit_code != 0
    assert WK.load(cfg) == []


def test_list_json_counts_slots_held_in_a_workers_pool(cfg, invoke):
    from nightwright.agentic import gate_slots as GS
    assert invoke("add", "box", "--ssh", "me@box", "--no-check").exit_code == 0
    with GS.acquire(cfg, "camp", pools=[("box", 1)]):
        rows = json.loads(invoke("list", "--json").output)["workers"]
    assert rows[0]["name"] == "box" and rows[0]["held"] == 1


def test_list_on_a_corrupt_registry_leaves_it_alone(cfg, invoke):
    WK.path(cfg).parent.mkdir(parents=True, exist_ok=True)
    WK.path(cfg).write_text("{not json")
    res = invoke("list")
    assert res.exit_code != 0
    assert WK.path(cfg).read_text() == "{not json"


def test_add_refuses_a_duplicate_name(cfg, invoke):
    assert invoke("add", "box", "--ssh", "a", "--no-check").exit_code == 0
    res = invoke("add", "box", "--ssh", "b", "--no-check")
    assert res.exit_code != 0 and "already registered" in res.output
    assert [w["ssh"] for w in WK.load(cfg)] == ["a"]


def test_add_checks_the_worker_unless_no_check(cfg, invoke, loopback, monkeypatch, tmp_path):
    ssh, nw = loopback
    monkeypatch.setattr(WK, "default_ssh_argv", lambda c: ssh)
    res = invoke("add", "box", "--ssh", "me@box", "--nw", nw)
    assert res.exit_code == 0, res.output
    assert WK.load(cfg)[0]["nw"] == nw
    fail = _script(tmp_path, "fail-ssh", "exit 255")
    monkeypatch.setattr(WK, "default_ssh_argv", lambda c: [fail])
    res = invoke("add", "dead", "--ssh", "me@dead")
    assert res.exit_code != 0 and "failed its check" in res.output
    assert _names(cfg) == ["box"]
    assert invoke("add", "dead", "--ssh", "me@dead", "--no-check").exit_code == 0
    assert _names(cfg) == ["box", "dead"]


def _info_script(tmp_path, name, info):
    f = tmp_path / f"{name}.json"
    f.write_text(json.dumps(info))
    return ["sh", "-c", 'cat "$0"', str(f)]


def _good_info(**kw):
    from nightwright.agentic import worker as W
    return {**W.info({"data_dir": str(Path("/tmp"))}), **kw}


def test_check_loopback_ok(cfg, loopback, invoke):
    ssh, nw = loopback
    _seed(cfg, _w("box", nw=nw, ssh_argv=ssh))
    res = invoke("check", "box", "--json")
    assert res.exit_code == 0, res.output
    (r,) = json.loads(res.output)
    assert r["name"] == "box" and r["ok"] and r["errors"] == [] and r["warnings"] == []
    assert r["info"]["protocol"] == "nw.job.v1"
    assert "box: ok" in invoke("check").output


def test_check_failures(cfg, tmp_path, invoke):
    fail = _script(tmp_path, "fail-ssh", "echo 'ssh: connect refused' >&2\nexit 255")
    cases = {
        "sshfail": [fail],
        "proto": _info_script(tmp_path, "proto", _good_info(protocol="nw.job.v2")),
        "nogit": _info_script(tmp_path, "nogit", _good_info(git=None)),
        "garbage": ["sh", "-c", "echo not-json"],
    }
    _seed(cfg, *[_w(n, ssh_argv=argv) for n, argv in cases.items()])
    res = invoke("check", "--json")
    assert res.exit_code == 1
    got = {r["name"]: r for r in json.loads(res.output)}
    assert all(not r["ok"] and r["errors"] for r in got.values())
    assert "connect refused" in got["sshfail"]["errors"][0]
    assert "protocol" in got["proto"]["errors"][0]
    assert "git" in got["nogit"]["errors"][0]


def test_check_version_mismatch_only_warns(cfg, tmp_path, invoke):
    _seed(cfg, _w("old", ssh_argv=_info_script(tmp_path, "old", _good_info(version="0.0.1"))))
    res = invoke("check", "old", "--json")
    assert res.exit_code == 0
    (r,) = json.loads(res.output)
    assert r["ok"] and r["errors"] == [] and "version" in r["warnings"][0]


def _stale(cfg, tmp_path, invoke, name, info):
    _seed(cfg, _w(name, ssh="me@box", ssh_argv=_info_script(tmp_path, name, info)))
    res = invoke("check", name, "--json")
    assert res.exit_code == 0, res.output                # warnings keep exit 0 (SPEC-0337)
    (r,) = json.loads(res.output)
    return r


def test_check_warns_worker_stale_for_a_different_or_missing_build_id(cfg, tmp_path, invoke):
    mine = _good_info()
    r = _stale(cfg, tmp_path, invoke, "other", {**mine, "build_id": "b1:" + "0" * 64})
    assert r["ok"] and r["errors"] == []
    (w,) = r["warnings"]
    assert w.startswith("worker stale: build b1:00000000") and mine["build_id"][:12] in w
    assert "nw workers bootstrap other --ssh me@box --reinstall" in w
    gone = {k: v for k, v in mine.items() if k != "build_id"}
    r = _stale(cfg, tmp_path, invoke, "gone", gone)
    assert r["ok"] and len(r["warnings"]) == 1 and "worker stale: build none differs" in r["warnings"][0]
    res = invoke("check", "gone")
    assert res.exit_code == 0 and "warning: worker stale" in res.output


def test_check_is_silent_for_an_equal_build_id_and_drops_the_sha_warning(cfg, tmp_path, invoke):
    mine = _good_info()
    r = _stale(cfg, tmp_path, invoke, "same", {**mine, "git_sha": "0" * 40})
    assert r["warnings"] == []                           # another sha alone no longer warns


def test_check_without_name_fails_if_any_worker_fails(cfg, tmp_path, invoke):
    good = _info_script(tmp_path, "good", _good_info())
    bad = _script(tmp_path, "bad-ssh", "exit 255")
    _seed(cfg, _w("good", ssh_argv=good), _w("bad", ssh_argv=[bad]))
    res = invoke("check", "--json")
    assert res.exit_code == 1
    assert {r["name"]: r["ok"] for r in json.loads(res.output)} == {"good": True, "bad": False}
    assert invoke("check", "good").exit_code == 0
    assert invoke("check", "bad").exit_code == 1


def test_check_timeout_is_an_ssh_failure(cfg, tmp_path, invoke, monkeypatch):
    monkeypatch.setattr(WK, "CHECK_TIMEOUT_S", 0.3)
    _seed(cfg, _w("slow", ssh_argv=[_script(tmp_path, "slow-ssh", "exec sleep 30")]))
    res = invoke("check", "slow", "--json")
    assert res.exit_code == 1
    (r,) = json.loads(res.output)
    assert not r["ok"] and "timed out" in r["errors"][0]


def test_help_points_at_the_other_group():
    out = CliRunner().invoke(cli, ["workers", "--help"]).output
    assert "nw worker" in out
    assert "nw workers" in CliRunner().invoke(cli, ["worker", "--help"]).output


# ---- the runner reports an unreadable registry -----------------------------------------------

def test_config_watch_reports_an_unreadable_registry_once(cfg):
    lease = L.acquire(cfg, ["nw", "queue", "run"])
    try:
        watch = Q.ConfigWatch(cfg, lambda: cfg)

        def conditions():
            return [c for c in E.watchdog_checks(cfg) if c[1] == "runner_config_invalid"]

        def errors():
            return (L.read_lease(cfg) or {}).get("config_errors") or {}

        def tick():
            watch.check(cfg, lease, [])

        tick()                                           # no file: nothing
        assert errors() == {} and conditions() == []
        p = WK.path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{broken")
        os.utime(p, ns=(10**18, 10**18))
        tick()
        tick()                                           # same version: still one
        assert list(errors()) == [str(p)] and errors()[str(p)]["scope"] == "workers"
        (cond,) = conditions()
        assert cond[0].startswith(f"runner_config_invalid:{p}:")
        assert cond[3]["scope"] == "workers" and cond[3]["path"] == str(p)
        E.validate("runner_config_invalid", cond[3])
        assert WK.load(cfg) == []
        _seed(cfg, _w("box"))                            # fixed
        os.utime(p, ns=(2 * 10**18, 2 * 10**18))
        tick()
        assert errors() == {} and conditions() == []
        p.write_text("[]")
        os.utime(p, ns=(3 * 10**18, 3 * 10**18))
        tick()
        assert len(errors()) == 1
        p.unlink()                                       # removed: cleared too
        tick()
        assert errors() == {}
    finally:
        lease.release()


# ---- dedicated worker key (SPEC-0354) -------------------------------------------------------

def test_identity_in_ssh_argv(cfg):
    _seed(cfg, _w("box", identity="/home/me/.ssh/nw_worker_ed25519"))
    (w,) = WK.load(cfg)
    tag = WK.identity_tag("/home/me/.ssh/nw_worker_ed25519")
    assert w["ssh_argv"] == WK.default_ssh_argv(cfg, tag) + [
        "-i", "/home/me/.ssh/nw_worker_ed25519", "-o", "IdentitiesOnly=yes"]


def test_identity_appended_to_hand_set_ssh_argv(cfg):
    """A hand-set `ssh_argv` keeps the dedicated key too: `identity` is appended to it."""
    _seed(cfg, _w("box", ssh_argv=["myssh", "-p", "2222"], identity="/k/worker"))
    (w,) = WK.load(cfg)
    assert w["ssh_argv"] == ["myssh", "-p", "2222", "-i", "/k/worker",
                             "-o", "IdentitiesOnly=yes"]


def test_identity_controlpath_is_its_own(cfg):
    """An identity record never shares a ControlMaster with a no-identity (or another
    identity's) record for the same destination: `%C` alone does not hash the key."""
    _seed(cfg, _w("a", ssh="nw@box"), _w("b", ssh="nw@box", identity="/k/one"),
          _w("c", ssh="nw@box", identity="/k/two"))
    cps = [[x for x in w["ssh_argv"] if x.startswith("ControlPath=")] for w in WK.load(cfg)]
    assert all(len(c) == 1 for c in cps) and len({c[0] for c in cps}) == 3
    assert cps[1][0].endswith("/%C-" + WK.identity_tag("/k/one"))
    d = cps[1][0].removeprefix("ControlPath=").rsplit("/", 1)[0]
    assert len(os.fsencode(d)) + WK.CTL_SUFFIX + WK.TAG_LEN <= WK.SOCK_MAX


def test_no_identity_unchanged(cfg):
    _seed(cfg, _w("box"))
    (w,) = WK.load(cfg)
    assert w["ssh_argv"] == WK.default_ssh_argv(cfg) and "identity" not in w


@pytest.mark.parametrize("ident", ["rel/key", "-oProxyCommand=x", "~/.ssh/k", "/a b/k"])
def test_add_identity_validation(cfg, invoke, ident):
    res = invoke("add", "box", "--ssh", "me@box", "--identity", ident, "--no-check")
    assert res.exit_code != 0 and WK.load(cfg) == []
    _seed(cfg, _w("box", identity=ident))               # a hand-edited one is unreadable too
    with pytest.raises(ValueError):
        WK.read_strict(cfg)


def test_add_identity_is_stored_and_listed(cfg, invoke):
    res = invoke("add", "box", "--ssh", "me@box", "--identity", "/k/id", "--no-check")
    assert res.exit_code == 0, res.output
    assert json.loads(WK.path(cfg).read_text())["workers"][0]["identity"] == "/k/id"
    assert json.loads(invoke("list", "--json").output)["workers"][0]["identity"] == "/k/id"
