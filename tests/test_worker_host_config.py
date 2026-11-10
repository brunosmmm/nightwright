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
if "info" in args and "--json" in args:
    info = json.loads(open(S + "/info.json").read())
    print(json.dumps(info))
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
if scn == "info_fail_after":
    if tail[0] == "set":
        print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True,
                          "op": "set", "key": "executor", "value": "slurm",
                          "effective": "slurm", "unchanged": False}}))
        sys.exit(0)
if tail[0] == "get":
    if len(tail) == 1:
        print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "get",
                          "keys": {{"executor": {{"value": None, "effective": "local"}}}}}}))
    else:
        print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "get",
                          "key": "executor", "value": None, "effective": "local"}}))
    sys.exit(0)
if tail == ["set", "executor", "slurm"]:
    print(json.dumps({{"schema": "nw.worker.config_set.result.v1", "ok": True, "op": "set",
                      "key": "executor", "value": "slurm", "effective": "slurm",
                      "unchanged": False}}))
    sys.exit(0)
sys.exit(8)
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


def test_symlink_config_dir_refused(cfg_home, tmp_path):
    cdir, cfg = cfg_home
    real = tmp_path / "real"
    real.mkdir()
    cdir.symlink_to(real)
    code = WHC.run(["set", "executor", "slurm"], cfg)
    assert code == 3
    assert not (real / "config.yaml").exists()


def test_preserve_siblings_and_unchanged(cfg_home):
    cdir, cfg = cfg_home
    cdir.mkdir()
    body = {"agentic": {"slurm": {"conf": "x"}, "workers": {"executor": "local", "key": 1}}}
    p = cdir / "config.yaml"
    p.write_text(yaml.dump(body))
    os.chmod(p, 0o600)
    before = p.read_bytes()
    mtime = p.stat().st_mtime
    code = WHC.run(["set", "executor", "local"], cfg)
    assert code == 0
    assert p.read_bytes() == before and p.stat().st_mtime == mtime
    WHC.run(["set", "executor", "slurm"], cfg)
    doc = yaml.safe_load(p.read_text())
    assert doc["agentic"]["slurm"]["conf"] == "x"
    assert doc["agentic"]["workers"]["key"] == 1


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
    cdir, cfg = cfg_home
    cdir.mkdir()
    p = cdir / "config.yaml"
    p.write_text("agentic:\n  workers:\n    executor: local\n")
    raw = p.read_bytes()
    for tail in (["set", "bogus", "slurm"], ["set", "executor", "bogus"]):
        rc = WHC.run(tail, cfg)
        assert rc == 2
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
    st = tmp_path / "state"
    st.mkdir()
    ssh = tmp_path / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_STATE", str(st))
    cfg = {"data_dir": str(tmp_path / "data"), "agentic": {}}
    (st / "info.json").write_text(json.dumps({
        "protocol": "nw.job.v1", "git": "g", "version": "0", "build_id": "b",
        "executor": "slurm", "features": ["async", "executor:slurm", "lease:none"]}))
    p = WK.path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "box", "ssh": "nw@box", "slots": 1, "nw": "/b/nw", "ssh_argv": [str(ssh)],
         "info": {"executor": "local", "features": ["async", "executor:local"]}}]}))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    (st / "scenario").write_text("ok")
    return st, cfg


def test_workers_config_set_refreshes_info(coord_world):
    st, cfg = coord_world
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["pushed"] is True
    rec = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert rec["executor"] == "slurm"
    assert rec["info"]["executor"] == "slurm"
    assert "executor:slurm" in rec["info"]["features"]
    argv = [json.loads(x) for x in (st / "argv").read_text().splitlines()]
    assert any("config-set" in a for a in argv)
    assert sum("info" in a and "--json" in a for a in argv) >= 1


def test_failed_push_keeps_old_info(coord_world):
    st, _cfg = coord_world
    (st / "scenario").write_text("config_fail")
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm"])
    assert r.exit_code == 1
    rec = json.loads(WK.path(_cfg).read_text())["workers"][0]
    assert rec["executor"] == "slurm"
    assert rec["info"]["executor"] == "local"
    assert [x for x in (st / "argv").read_text().splitlines() if "info" in x] == []


def test_info_fail_after_push(coord_world, monkeypatch):
    st, cfg = coord_world
    (st / "scenario").write_text("info_fail_after")
    bad = json.loads((st / "info.json").read_text())
    bad["executor"] = "local"
    (st / "info.json").write_text(json.dumps(bad))
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "slurm"])
    assert r.exit_code == 1
    rec = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert rec["info"]["executor"] == "local"


def test_ensure_ssh_dir_before_ssh(coord_world, monkeypatch):
    st, cfg = coord_world

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
    st, cfg = coord_world
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
    st, cfg = coord_world
    records = json.loads(WK.path(cfg).read_text())["workers"]
    records[0]["executor"] = "slurm"
    WK.path(cfg).write_text(json.dumps({"schema": WK.SCHEMA, "workers": records}))
    r = CliRunner().invoke(cli, ["workers", "config", "box", "get", "executor", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert set(payload) == {"name", "op", "desired", "keys"}
    assert payload["op"] == "get" and payload["desired"] == "slurm"
    r = CliRunner().invoke(cli, ["workers", "config", "box", "get", "executor"])
    assert r.exit_code == 0
    assert "box executor live=(unset) effective=local desired=slurm" in r.output


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
    st, cfg = coord_world

    def boom(c, w):
        raise OSError("nope")

    monkeypatch.setattr(WK, "ensure_record_ssh_dir", boom)
    before = WK.path(cfg).read_bytes()
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "bogus"])
    assert r.exit_code == 2
    assert WK.path(cfg).read_bytes() == before


def test_pull_and_unknown(coord_world):
    st, cfg = coord_world
    records = json.loads(WK.path(cfg).read_text())["workers"]
    pull = {"name": "pull", "mode": "pull", "token_sha256": "a" * 64, "slots": 1}
    records.append(pull)
    p = WK.path(cfg)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": records}))
    before = p.read_bytes()
    r = CliRunner().invoke(cli, ["workers", "config", "pull", "get"])
    assert r.exit_code == 1 and before == p.read_bytes()
    r = CliRunner().invoke(cli, ["workers", "config", "nope", "get"])
    assert r.exit_code == 1
    r = CliRunner().invoke(cli, ["workers", "config", "box", "set", "executor", "bogus"])
    assert r.exit_code == 2


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
    r = CliRunner().invoke(cli, ["workers", "check", "box"])
    assert r.exit_code == 0, r.output
    assert "executor: slurm" in r.output
