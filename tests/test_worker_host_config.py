"""SPEC-0538: nw worker config-set and nw workers config."""
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nightwright.agentic import worker as W
from nightwright.agentic import worker_host_config as WHC
from nightwright.agentic import workers as WK
from nightwright.cli import cli

pytestmark = pytest.mark.fast

FAKE_SSH = f"""#!{sys.executable}
import json, os, sys
S = os.environ["FAKE_STATE"]
args = sys.argv[1:]
open(S + "/argv", "a").write(json.dumps(args) + "\\n")
HOST = S + "/host_config.yaml"
LIVE = S + "/live_executor.txt"

def _live():
    if os.path.isfile(LIVE):
        return open(LIVE).read().strip()
    return open(S + "/scenario_live").read().strip() if os.path.isfile(S + "/scenario_live") else "distinctive-remote"

def _info_payload(executor):
    base = json.loads(open(S + "/info.json").read())
    base["executor"] = executor
    feats = ["async", f"executor:{{executor}}"]
    if executor == "slurm":
        feats.append("lease:none")
    base["features"] = feats
    return base

if "info" in args and "--json" in args:
    scn = open(S + "/scenario").read().strip()
    if scn == "info_bad_json":
        print("not-json", flush=True)
        sys.exit(3)
    if scn == "info_bad_exit":
        print(json.dumps(_info_payload("distinctive-remote")))
        sys.exit(5)
    ex = _live()
    if scn == "info_fail_after":
        ex = "local"
    print(json.dumps(_info_payload(ex)))
    sys.exit(0)
verb = args[args.index("worker") + 1] if "worker" in args else None
if verb != "config-set":
    sys.exit(9)
tail = args[args.index("config-set") + 1:]
open(S + "/config_argv", "a").write(json.dumps(tail) + "\\n")
scn = open(S + "/scenario").read().strip()
if scn == "config_fail":
    print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": False, "error": "nope"}}))
    sys.exit(1)
if scn == "ok_bad_exit":
    ex = _live()
    if tail[0] == "get":
        if len(tail) == 1:
            print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "get",
                              "keys": {{"executor": {{"value": ex, "effective": ex}}}}}}))
        else:
            print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "get",
                              "key": "executor", "value": ex, "effective": ex}}))
        sys.exit(7)
    if tail == ["set", "executor", "slurm"]:
        print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "set",
                          "key": "executor", "value": "slurm", "effective": "slurm",
                          "unchanged": False}}))
        sys.exit(7)
if scn == "info_fail_after" and tail[0] == "set":
    open(HOST, "w").write("agentic:\\n  workers:\\n    executor: slurm\\n")
    open(LIVE, "w").write("slurm")
    print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True,
                      "op": "set", "key": "executor", "value": "slurm",
                      "effective": "slurm", "unchanged": False}}))
    sys.exit(0)
if scn == "banner":
    print("Welcome to the box", flush=True)
if tail[0] == "get":
    ex = _live()
    eff = ex if ex != "distinctive-remote" else "local"
    val = None if ex == "distinctive-remote" else ex
    if len(tail) == 1:
        print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "get",
                          "keys": {{"executor": {{"value": val, "effective": eff}}}}}}))
    else:
        print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "get",
                          "key": "executor", "value": val, "effective": eff}}))
    sys.exit(0)
if tail == ["set", "executor", "slurm"]:
    open(HOST, "w").write("agentic:\\n  workers:\\n    executor: slurm\\n")
    open(LIVE, "w").write("slurm")
    print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "set",
                      "key": "executor", "value": "slurm", "effective": "slurm",
                      "unchanged": False}}))
    sys.exit(0)
print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": False, "error": "refused"}}))
sys.exit(3)
"""


@pytest.fixture
def cfg_home(tmp_path, monkeypatch):
    cdir = tmp_path / "cfg"
    monkeypatch.setenv("NW_CONFIG_DIR", str(cdir))
    return cdir, {"data_dir": str(tmp_path / "data"), "agentic": {}}


def test_ssh_grammar_and_refusals():
    assert "config-set" in W.SSH_SUBCOMMANDS
    assert W.check_ssh_command("nw worker config-set get") == ["config-set", "get"]
    assert W.check_ssh_command("nw worker config-set get executor") == ["config-set", "get", "executor"]
    assert W.check_ssh_command("nw worker config-set set executor slurm") == \
        ["config-set", "set", "executor", "slurm"]
    for cmd in ("nw worker config-set", "nw worker config-set set executor SLURM",
                "nw worker config-set set slots 3", "nw worker config-set set executor local; id",
                "nw worker config-set set executor slurm extra",
                "nw worker config-set get executor extra"):
        with pytest.raises(ValueError):
            W.check_ssh_command(cmd)


def test_get_missing_home(cfg_home):
    cdir, cfg = cfg_home
    assert not cdir.exists()
    rec = WHC.get_key(cfg, "executor")
    assert rec["value"] is None and rec["effective"] == "local"
    assert not cdir.exists()


def test_set_creates_dir_and_file(cfg_home):
    cdir, cfg = cfg_home
    code = WHC.run(["set", "executor", "slurm"], cfg)
    assert code == 0
    assert (cdir.stat().st_mode & 0o777) == 0o700
    p = cdir / "config.yaml"
    assert (p.stat().st_mode & 0o777) == 0o600
    doc = yaml.safe_load(p.read_text())
    assert doc["agentic"]["workers"]["executor"] == "slurm"


def test_symlink_parent_allows_missing_config_dir(tmp_path, monkeypatch):
    real = tmp_path / "real_xdg"
    real.mkdir()
    sym = tmp_path / ".config"
    sym.symlink_to(real)
    cdir = sym / "nightwright"
    cfg = {"data_dir": str(tmp_path / "data"), "agentic": {}}
    monkeypatch.setenv("NW_CONFIG_DIR", str(cdir))
    rc = WHC.run(["set", "executor", "slurm"], cfg)
    assert rc == 0
    assert cdir.is_dir()
    assert (cdir.stat().st_mode & 0o777) == 0o700
    assert (cdir / "config.yaml").is_file()


def test_symlink_config_dir_refused(cfg_home, tmp_path):
    cdir, cfg = cfg_home
    real = tmp_path / "real"
    real.mkdir()
    cdir.symlink_to(real)
    code = WHC.run(["set", "executor", "slurm"], cfg)
    assert code == 3
    assert not (real / "config.yaml").exists()


def test_preserve_siblings_and_unchanged(cfg_home):
    import contextlib
    import io

    cdir, cfg = cfg_home
    cdir.mkdir()
    body = {"agentic": {"slurm": {"conf": "x"}, "workers": {"executor": "local", "key": 1}}}
    p = cdir / "config.yaml"
    p.write_text(yaml.dump(body))
    os.chmod(p, 0o600)
    before = p.read_bytes()
    mtime = p.stat().st_mtime
    assert list(cdir.glob("config.yaml.bak.*")) == []
    cap = io.StringIO()
    with contextlib.redirect_stdout(cap):
        code = WHC.run(["set", "executor", "local"], cfg)
    assert code == 0
    assert json.loads(cap.getvalue().strip()).get("unchanged") is True
    assert p.read_bytes() == before and p.stat().st_mtime == mtime
    assert list(cdir.glob("config.yaml.bak.*")) == []
    cap = io.StringIO()
    with contextlib.redirect_stdout(cap):
        code2 = WHC.run(["set", "executor", "slurm"], cfg)
    assert code2 == 0
    assert json.loads(cap.getvalue().strip()).get("unchanged") is False
    doc = yaml.safe_load(p.read_text())
    assert doc["agentic"]["slurm"]["conf"] == "x"
    assert doc["agentic"]["workers"]["key"] == 1
    assert doc["agentic"]["workers"]["executor"] == "slurm"


def test_scalar_agentic_refused(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text("agentic: hello\n")
    raw = p.read_bytes()
    code = WHC.run(["set", "executor", "slurm"], cfg)
    assert code == 3
    assert p.read_bytes() == raw


def test_comments_only_set(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text("# just a comment\n")
    code = WHC.run(["set", "executor", "local"], cfg)
    assert code == 0
    doc = yaml.safe_load(p.read_text())
    assert list(doc.keys()) == ["agentic"]
    assert doc["agentic"]["workers"]["executor"] == "local"


def test_invalid_yaml_refused(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text("agentic: {\n")
    raw = p.read_bytes()
    assert WHC.run(["set", "executor", "slurm"], cfg) == 3
    assert p.read_bytes() == raw


def test_symlink_config_refused(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    tgt = cfg_home[0].parent / "target.yaml"
    tgt.write_text("agentic:\n  workers:\n    executor: local\n")
    p = cdir / "config.yaml"
    p.symlink_to(tgt)
    raw = tgt.read_bytes()
    assert WHC.run(["set", "executor", "slurm"], cfg) == 3
    assert tgt.read_bytes() == raw


def test_dangling_symlink_config_refused(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.symlink_to("/no/such/config.yaml")
    assert p.is_symlink() and not p.exists()
    code = WHC.run(["set", "executor", "slurm"], cfg)
    assert code == 3
    assert p.is_symlink()
    assert os.readlink(p) == "/no/such/config.yaml"
    assert list(cdir.glob("config.yaml.bak.*")) == []
    assert list(cdir.glob("config.yaml.tmp*")) == []


def test_dangling_symlink_config_dir_refused(cfg_home):
    cdir, cfg = cfg_home
    dangling = cfg_home[0].parent / "gone"
    cdir.symlink_to(dangling)
    assert cdir.is_symlink() and not cdir.exists()
    code = WHC.run(["set", "executor", "slurm"], cfg)
    assert code == 3
    assert cdir.is_symlink()
    assert not dangling.exists()
    assert not (cfg_home[0].parent / "config.yaml").exists()


def test_worker_run_refuses_bad_key_and_value(cfg_home):
    import contextlib
    import io

    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text("agentic:\n  workers:\n    executor: local\n")
    raw = p.read_bytes()
    for tail in (["set", "bogus", "slurm"], ["set", "executor", "bogus"]):
        cap = io.StringIO()
        with contextlib.redirect_stdout(cap):
            rc = WHC.run(tail, cfg)
        assert rc == 2
        rec = json.loads(cap.getvalue().strip())
        assert rec["schema"] == "nw.worker.config_set.result.v1" and rec["ok"] is False
        assert p.read_bytes() == raw


def test_backup_on_change(cfg_home):
    cdir, cfg = cfg_home
    WHC.run(["set", "executor", "local"], cfg)
    p = cdir / "config.yaml"
    WHC.run(["set", "executor", "slurm"], cfg)
    baks = list(cdir.glob("config.yaml.bak.*"))
    assert len(baks) == 1
    assert (baks[0].stat().st_mode & 0o777) == 0o600


@pytest.fixture
def coord_world(tmp_path, monkeypatch):
    from nightwright.config import load_config

    st = tmp_path / "state"
    st.mkdir()
    ssh = tmp_path / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_STATE", str(st))
    coord_cfg = tmp_path / "coord_cfg"
    coord_cfg.mkdir()
    sentinel_bytes = b"coordinator: sentinel\n"
    sentinel = coord_cfg / "config.yaml"
    sentinel.write_bytes(sentinel_bytes)
    monkeypatch.setenv("NW_CONFIG_DIR", str(coord_cfg))
    monkeypatch.setenv("NW_DATA_DIR", str(tmp_path / "data"))
    cfg = load_config()
    seeded_info = {"executor": "local", "features": ["async", "executor:local"]}
    (st / "info.json").write_text(json.dumps({
        "protocol": "nw.job.v1", "git": "g", "version": "0", "build_id": "b",
        "executor": "slurm", "features": ["async", "executor:slurm", "lease:none"]}))
    p = WK.path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "box", "ssh": "nw@box", "slots": 1, "nw": "/b/nw", "ssh_argv": [str(ssh)],
         "info": dict(seeded_info)}]}))
    (st / "scenario").write_text("ok")
    (st / "scenario_live").write_text("distinctive-remote")
    host_seed = st / "host_config.yaml"
    host_seed.write_text("agentic:\n  workers:\n    executor: local\n")
    return st, cfg, sentinel, sentinel_bytes, host_seed, seeded_info


def test_workers_config_set_refreshes_info(coord_world):
    st, cfg, *_ = coord_world
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["pushed"] is True
    rec = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert rec["executor"] == "slurm"
    assert rec["info"]["executor"] == "slurm"
    assert "executor:slurm" in rec["info"]["features"]
    assert "lease:none" in rec["info"]["features"]
    argv = [json.loads(x) for x in (st / "argv").read_text().splitlines()]
    assert any("config-set" in a for a in argv)
    info_idx = next(i for i, a in enumerate(argv) if "info" in a and "--json" in a)
    set_idx = next(i for i, a in enumerate(argv) if "config-set" in a and "set" in a)
    assert info_idx > set_idx


def test_ok_true_nonzero_ssh_exit_set(coord_world):
    st, cfg, *_ = coord_world
    (st / "scenario").write_text("ok_bad_exit")
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm"])
    assert r.exit_code == 1
    assert "box executor desired=slurm; host not updated:" in r.output
    assert "ssh exit 7" in r.output
    rec = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert rec["executor"] == "slurm"
    assert rec["info"]["executor"] == "local"
    assert [x for x in (st / "argv").read_text().splitlines() if "info" in x] == []


def test_ok_true_nonzero_ssh_exit_get(coord_world):
    st, cfg, *_ = coord_world
    records = json.loads(WK.path(cfg).read_text())["workers"]
    records[0]["executor"] = "slurm"
    WK.path(cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": records}))
    (st / "scenario").write_text("ok_bad_exit")
    r = CliRunner().invoke(cli, ["workers", "config", "box", "get", "executor"])
    assert r.exit_code == 1
    assert "desired=slurm" in r.output
    assert "live=" not in r.output


def test_failed_push_keeps_old_info(coord_world):
    st, _cfg, sentinel, sentinel_bytes, host_seed, seeded_info = coord_world
    host_before = host_seed.read_bytes()
    (st / "scenario").write_text("config_fail")
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm"])
    assert r.exit_code == 1
    rec = json.loads(WK.path(_cfg).read_text())["workers"][0]
    assert rec["executor"] == "slurm"
    assert rec["info"] == seeded_info
    assert host_seed.read_bytes() == host_before
    assert sentinel.read_bytes() == sentinel_bytes
    assert [x for x in (st / "argv").read_text().splitlines() if "info" in x] == []


def test_info_fail_after_push(coord_world, monkeypatch):
    st, cfg, sentinel, sentinel_bytes, host_seed, seeded_info = coord_world
    (st / "scenario").write_text("info_fail_after")
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm"])
    assert r.exit_code == 1
    rec = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert rec["info"] == seeded_info
    assert sentinel.read_bytes() == sentinel_bytes
    assert "executor: slurm" in host_seed.read_text()


def test_ensure_ssh_dir_before_ssh(coord_world, monkeypatch):
    st, cfg, *_ = coord_world

    def boom(c, w):
        raise OSError("nope")

    monkeypatch.setattr(WK, "ensure_record_ssh_dir", boom)
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm"])
    assert r.exit_code == 1
    rec = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert rec.get("executor") == "slurm"
    assert rec["info"]["executor"] == "local"
    assert not (st / "argv").exists()
    assert "desired=slurm" in r.output
    assert "host not updated: ssh socket directory unusable: nope" in r.output


def test_ensure_ssh_dir_get_shows_desired(coord_world, monkeypatch):
    st, cfg, *_ = coord_world
    records = json.loads(WK.path(cfg).read_text())["workers"]
    records[0]["executor"] = "slurm"
    WK.path(cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": records}))

    def boom(c, w):
        raise OSError("nope")

    monkeypatch.setattr(WK, "ensure_record_ssh_dir", boom)
    r = CliRunner().invoke(cli, ["workers", "config", "box", "get", "--json"])
    assert r.exit_code == 1
    payload = json.loads(r.output)
    assert payload == {"name": "box", "op": "get", "desired": "slurm", "error": "ssh socket directory unusable: nope"}
    assert not (st / "argv").exists()


def test_workers_config_get_success(coord_world):
    st, cfg, sentinel, sentinel_bytes, _host, seeded_info = coord_world
    records = json.loads(WK.path(cfg).read_text())["workers"]
    records[0]["executor"] = "slurm"
    WK.path(cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": records}))
    r = CliRunner().invoke(cli, ["workers", "config", "box", "get", "executor", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert set(payload) == {"name", "op", "desired", "keys"}
    assert payload["op"] == "get" and payload["desired"] == "slurm"
    assert payload["keys"]["executor"]["value"] is None
    assert payload["keys"]["executor"]["effective"] == "local"
    rec_after = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert rec_after["info"] == seeded_info
    r = CliRunner().invoke(cli, ["workers", "config", "box", "get", "executor"])
    assert r.exit_code == 0
    assert "box executor live=(unset) effective=local desired=slurm" in r.output
    assert sentinel.read_bytes() == sentinel_bytes


def test_remote_config_set_surfaces_stderr(coord_world, monkeypatch):
    from nightwright.agentic import workers_host_config as WC

    def fake_ssh(w, tail):
        return 255, "", "refused: subcommand"

    monkeypatch.setattr(WC, "_ssh_run", fake_ssh)
    w = WK.normalize({"data_dir": "/x", "agentic": {}},
                      json.loads(WK.path(coord_world[1]).read_text())["workers"][0])
    rec, reason, code = WC.remote_config_set(w, ["get"])
    assert rec is None and code == 1
    assert reason == "refused: subcommand"


def test_bogus_set_exits_2_before_ensure(coord_world, monkeypatch):
    st, cfg, *_ = coord_world

    def boom(c, w):
        raise OSError("nope")

    monkeypatch.setattr(WK, "ensure_record_ssh_dir", boom)
    before = WK.path(cfg).read_bytes()
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "bogus"])
    assert r.exit_code == 2
    assert WK.path(cfg).read_bytes() == before


def test_pull_and_unknown(coord_world):
    st, cfg, *_ = coord_world
    records = json.loads(WK.path(cfg).read_text())["workers"]
    pull = {"name": "pull", "mode": "pull", "token_sha256": "a" * 64, "slots": 1}
    records.append(pull)
    p = WK.path(cfg)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": records}))
    before = p.read_bytes()
    r = CliRunner().invoke(cli, ["workers", "config", "pull", "get"])
    assert r.exit_code == 1 and before == p.read_bytes()
    assert not (st / "argv").exists()
    r = CliRunner().invoke(cli, ["workers", "config", "nope", "get"])
    assert r.exit_code == 1 and before == p.read_bytes()
    assert not (st / "argv").exists()
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "slots", "3"])
    assert r.exit_code == 2 and before == p.read_bytes()
    assert not (st / "argv").exists()
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "bogus"])
    assert r.exit_code == 2 and before == p.read_bytes()


def test_read_strict_executor(tmp_path):
    cfg = {"data_dir": str(tmp_path / "d"), "agentic": {}}
    p = WK.path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [{"name": "b", "ssh": "u@h"}]}))
    WK.read_strict(cfg)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "b", "ssh": "u@h", "executor": "slurm"}]}))
    WK.read_strict(cfg)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "b", "ssh": "u@h", "executor": "bogus"}]}))
    with pytest.raises(ValueError):
        WK.read_strict(cfg)


def test_workers_check_prints_executor(coord_world):
    st, cfg, *_ = coord_world
    (st / "scenario_live").write_text("slurm")
    r = CliRunner().invoke(cli, ["workers", "check", "box"])
    assert r.exit_code == 0, r.output
    assert "executor: slurm" in r.output
    rj = CliRunner().invoke(cli, ["workers", "check", "box", "--json"])
    payload = json.loads(rj.output)
    assert set(payload[0]) == {"name", "ok", "errors", "warnings", "info"}


def test_workers_check_no_info_mapping(coord_world, tmp_path, monkeypatch):
    st, cfg, *_ = coord_world
    garbage = tmp_path / "garbage-ssh"
    garbage.write_text(f"""#!{sys.executable}
import json, sys
print(json.dumps({{"protocol": "nw.job.v1", "git": "g", "version": "0", "build_id": "b"}}))
""")
    garbage.chmod(garbage.stat().st_mode | stat.S_IXUSR)
    records = json.loads(WK.path(cfg).read_text())["workers"]
    records[0]["ssh_argv"] = [str(garbage)]
    WK.path(cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": records}))
    r = CliRunner().invoke(cli, ["workers", "check", "box"])
    assert r.exit_code == 0, r.output
    assert "executor:" not in r.output


def _cli_config_set_result(runner_output):
    lines = [ln for ln in runner_output.strip().splitlines() if ln.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


@pytest.mark.parametrize("body", ["agentic: {\n", "- a\n"])
def test_cli_worker_config_set_bad_document_refused(tmp_path, monkeypatch, body):
    cdir = tmp_path / "cfg"
    cdir.mkdir()
    target = cdir / "config.yaml"
    if body.startswith("-"):
        target.mkdir()
        inode = target.stat().st_ino
        raw = None
    else:
        target.write_text(body)
        raw = target.read_bytes()
        inode = None
    monkeypatch.setenv("NW_CONFIG_DIR", str(cdir))
    monkeypatch.setattr(sys, "argv", ["nw", "worker", "config-set", "set", "executor", "slurm"])
    r = CliRunner().invoke(cli, ["worker", "config-set", "set", "executor", "slurm"])
    assert r.exit_code == 3, r.output
    rec = _cli_config_set_result(r.output)
    assert rec["ok"] is False and rec["schema"] == "nw.worker.config_set.result.v1"
    if raw is not None:
        assert target.read_bytes() == raw
    else:
        assert target.is_dir() and target.stat().st_ino == inode


def test_top_level_list_refused(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text("- only\n")
    raw = p.read_bytes()
    assert WHC.run(["set", "executor", "slurm"], cfg) == 3
    assert p.read_bytes() == raw


@pytest.mark.parametrize("text", [
    "agentic: [1, 2]\n",
    "agentic:\n  workers: oops\n",
    "agentic:\n  workers: [1]\n",
])
def test_non_mapping_nodes_refused(cfg_home, text):
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text(text)
    raw = p.read_bytes()
    assert WHC.run(["set", "executor", "slurm"], cfg) == 3
    assert p.read_bytes() == raw
    assert list(cdir.glob("config.yaml.bak.*")) == []


def test_list_executor_replaced(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text("agentic:\n  workers:\n    executor: [local]\n    key: 1\n")
    assert WHC.run(["set", "executor", "slurm"], cfg) == 0
    doc = yaml.safe_load(p.read_text())
    assert doc["agentic"]["workers"]["executor"] == "slurm"
    assert doc["agentic"]["workers"]["key"] == 1


def test_bogus_executor_get_roundtrip(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text("agentic:\n  workers:\n    executor: bogus\n")
    raw = p.read_bytes()
    import contextlib
    import io
    cap = io.StringIO()
    with contextlib.redirect_stdout(cap):
        assert WHC.run(["get", "executor"], cfg) == 0
    rec = json.loads(cap.getvalue().strip())
    assert rec["value"] == "bogus" and rec["effective"] == "bogus"
    assert p.read_bytes() == raw


def test_non_regular_config_refused(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    try:
        os.mkfifo(cdir / "config.yaml")
    except PermissionError:
        pytest.skip("cannot create fifo")
    assert WHC.run(["set", "executor", "slurm"], cfg) == 3


def test_get_via_run_missing_file(cfg_home):
    import contextlib
    import io
    cdir, cfg = cfg_home
    assert not cdir.exists()
    cap = io.StringIO()
    with contextlib.redirect_stdout(cap):
        assert WHC.run(["get", "executor"], cfg) == 0
    rec = json.loads(cap.getvalue().strip())
    assert rec["value"] is None and rec["effective"] == "local"


def test_set_success_sentinel_and_jobs_line(coord_world):
    st, cfg, sentinel, sentinel_bytes, *_ = coord_world
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm"])
    assert r.exit_code == 0, r.output
    assert sentinel.read_bytes() == sentinel_bytes
    assert "jobs already running keep the executor they started with" in r.output


def test_info_probe_bad_json_keeps_info(coord_world):
    st, cfg, _, _, _, seeded_info = coord_world
    (st / "scenario").write_text("info_bad_json")
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm"])
    assert r.exit_code == 1
    rec = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert rec["info"] == seeded_info


def test_remote_banner_before_result(coord_world, monkeypatch):
    from nightwright.agentic import workers_host_config as WC

    _, cfg, *_ = coord_world
    rec = json.loads(WK.path(cfg).read_text())["workers"][0]
    w = WK.normalize(cfg, rec)

    def fake_ssh(_w, tail):
        out = "Welcome\n" + json.dumps({"schema": "nw.worker.config_set.result.v1", "ok": True,
                                        "op": "get", "key": "executor", "value": None,
                                        "effective": "local"})
        return 0, out + "\n", ""

    monkeypatch.setattr(WC, "_ssh_run", fake_ssh)
    remote, reason, code = WC.remote_config_set(w, ["get", "executor"])
    assert code == 0 and reason == "" and remote["ok"]


def test_config_set_help_and_workflow_four_forms():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "WORKFLOW.md").read_text()
    assert "config-set get" in doc and "[executor]" in doc
    assert "config-set set executor local" in doc or "set executor local" in doc
    assert "set executor slurm" in doc
    r = CliRunner().invoke(cli, ["worker", "--help"])
    assert r.exit_code == 0
    assert "config-set get" in r.output
    assert "set executor" in r.output and "local" in r.output and "slurm" in r.output
    r = CliRunner().invoke(cli, ["workers", "--help"])
    assert r.exit_code == 0
    assert "workers config" in r.output and "executor" in r.output
    r = CliRunner().invoke(cli, ["worker", "ssh-command", "--help"])
    assert r.exit_code == 0
    assert "config-set" in r.output
