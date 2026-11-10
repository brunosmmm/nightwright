"""SPEC-0510: `nw worker slurm-check` (the forced-command grammar entry and the checks' verdicts,
against the fake Slurm CLI) and `nw workers check --slurm`'s parsing of the worker's answer."""
import json
import subprocess
import sys

import pytest

from nightwright.agentic import executor_slurm as ES
from nightwright.agentic import worker as W
from slurm_fake import SlurmFake

pytestmark = pytest.mark.integration


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = SlurmFake(tmp_path / "slurm").install(monkeypatch)
    ES.forget()
    yield f
    f.close()
    ES.forget()


def _cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"slurm": {"cache_s": 0}}}


def test_the_grammar_allows_slurm_check():
    assert "slurm-check" in W.SSH_SUBCOMMANDS
    assert W.check_ssh_command("nw worker slurm-check") == ["slurm-check"]
    assert W.check_ssh_command("/x/nw worker slurm-check --json") == ["slurm-check", "--json"]
    for bad in ("nw worker slurm-check --wrap x", "nw worker slurm-check --json --json"):
        with pytest.raises(ValueError):
            W.check_ssh_command(bad)


def _judge(rec):
    return {c["name"]: c["ok"] for c in rec["checks"]}


def test_a_healthy_slurm_passes_every_check(tmp_path, fake):
    fake.auto("nw-slurm-check-cgroup", "COMPLETED", f"2\n{1 << 30}\n")
    fake.auto("nw-slurm-check-oom", "OUT_OF_MEMORY", "Killed\n")
    rec = ES.slurm_check(_cfg(tmp_path), wait_s=10)
    assert rec["schema"] == ES.CHECK_SCHEMA and rec["ok"] is True, rec
    assert set(_judge(rec)) == {"version", "node", "config", "node_spec", "licenses",
                                "classes", "cgroup", "oom"}         # SPEC-0511: classes
    cg = next(c for c in rec["checks"] if c["name"] == "cgroup")
    assert cg["detail"]["state"] == "COMPLETED" and cg["detail"]["output"].split() == \
        ["2", str(1 << 30)]
    sb = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in fake.calls("sbatch")[0] if "=" in a}
    assert sb["--cpus-per-task"] == "2" and sb["--mem"] == "1G"


def test_no_memory_enforcement_fails_the_oom_check(tmp_path, fake):
    fake.auto("nw-slurm-check-cgroup", "COMPLETED", "12\nmax\n")
    fake.auto("nw-slurm-check-oom", "COMPLETED", "629145600\n")
    rec = ES.slurm_check(_cfg(tmp_path), wait_s=10)
    assert rec["ok"] is False
    assert _judge(rec)["cgroup"] is False and _judge(rec)["oom"] is False
    assert _judge(rec)["version"] is True


def test_the_cli_prints_one_json_line_and_exits_1_on_failure(tmp_path, fake):
    fake.fail("sbatch", "sbatch: error: Unable to contact slurm controller")
    env = fake.env()
    env.update(NW_DATA_DIR=str(tmp_path / "data"), NW_CONFIG_DIR=str(tmp_path / "conf"))
    p = subprocess.run([sys.executable, "-m", "nightwright", "worker", "slurm-check", "--json"],
                       env=env, capture_output=True, text=True, timeout=120)
    assert p.returncode == 1, p.stderr
    rec = json.loads(p.stdout)
    assert rec["schema"] == ES.CHECK_SCHEMA and rec["ok"] is False
    assert "Unable to contact" in next(c for c in rec["checks"] if c["name"] == "cgroup")["detail"]


def test_remote_check_reports_the_failed_checks(monkeypatch):
    from nightwright.agentic import remote_gates as RG
    rec = {"schema": ES.CHECK_SCHEMA, "ok": False,
           "checks": [{"name": "version", "ok": True, "detail": "slurm 26.05.5"},
                      {"name": "oom", "ok": False, "detail": {"state": "COMPLETED"}}]}
    monkeypatch.setattr(RG, "_call", lambda w, *a, **k: (1, [rec], b""))
    errs, got = ES.remote_check({"name": "box"})
    assert got == rec and len(errs) == 1 and errs[0].startswith("slurm oom:")
    monkeypatch.setattr(RG, "_call", lambda w, *a, **k: None)
    assert ES.remote_check({"name": "box"})[0] == ["`nw worker slurm-check` could not run over SSH"]
