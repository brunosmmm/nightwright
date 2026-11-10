"""SPEC-0511: Slurm job classes. The coordinator stamps `payload.job_class` per caller (campaign
gate, `nw gate run`, sentinel, cadence heavy tier, persona; an old header has none = gatejob);
the worker maps class -> `{partition, qos}` (`agentic.slurm.classes`) into sbatch's argv, and a
mapping Slurm rejects falls back once to the plain partition without a QOS, with a warning."""
import io
import json
import types

import pytest

from nightwright.agentic import cadence as CD
from nightwright.agentic import executor_slurm as ES
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import remote_personas as RPS
from nightwright.agentic import worker as W
from nightwright.agentic import worker_job as WJ
from slurm_fake import SlurmFake
from test_worker import _gate, _header, _job_dir, _no_finish_gc, wenv  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
def fake(tmp_path, monkeypatch):
    f = SlurmFake(tmp_path / "slurm").install(monkeypatch)
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()
    yield f
    f.close()
    ES.forget()
    ES._LICENSES.clear()
    ES._LIMITS.clear()


def _cfg(env, slurm=None):
    return {"data_dir": env["NW_DATA_DIR"], "config_dir": env["NW_CONFIG_DIR"],
            "agentic": {"workers": {"min_free_gb": 0, "abort_free_gb": 0, "executor": "slurm"},
                        "slurm": {"cache_s": 0, **(slurm or {})}}}


def _plain(job_id, job_class=None):
    h = _header(job_id, "a" * 40, "b" * 40, [_gate("x", "true")], bundle_bytes=0)
    h["payload"]["resources"] = {"cpus": 2, "mem_mb": 1024, "licenses": {}, "time_s": 60,
                                 "gates": {}}
    if job_class is not None:
        h["payload"]["job_class"] = job_class
    return h


def _submit(cfg, h):
    out = io.BytesIO()
    rc = WJ.submit(cfg, io.BytesIO((json.dumps(h) + "\n").encode()), out)
    return rc, [json.loads(ln) for ln in out.getvalue().splitlines()]


def _opts(argv):
    return {a.split("=", 1)[0]: a.split("=", 1)[1] for a in argv if "=" in a}


def _read(path):
    return json.loads(path.read_text())


# ---- class assignment (coordinator) ------------------------------------------------------------

@pytest.mark.parametrize("state,cls", [
    ({"id": "SPEC-0511-20261110-ab12"}, "campaign"),             # campaign loop / close gates
    ({"id": "GATEJOB-0042"}, "gatejob"),                          # `nw gate run`
    ({"id": "sentinel-nightwright"}, "bulk"),                     # full-suite sentinel
    ({"id": CD.campaign_id("nightwright", "slow-tier")}, "bulk"),  # cadence heavy tier
    ({"id": "C-1", "job_class": "bulk"}, "bulk"),                 # an explicit class wins
    ({"id": "C-1", "job_class": "nonsense"}, "campaign"),
])
def test_class_per_caller(state, cls):
    assert RG.job_class(state) == cls


def _gate_header(tmp_path, cid):
    cfg = {"data_dir": str(tmp_path / "d"), "config_dir": str(tmp_path / "c")}
    y = tmp_path / "p" / ".nw" / "project.yaml"
    y.parent.mkdir(parents=True, exist_ok=True)
    y.write_text("schema: nw.project.v1\nproject: p\nbase: main\ngates: []\n")
    st = {"id": cid, "project": "p", "repo": {"worktree": str(tmp_path / "p"), "base": "main"}}
    return RG._header({"gates": []}, st, cfg, 1, f"{cid}-gate-001-a0", "k", "0" * 40, "0" * 40,
                      [], 0)


@pytest.mark.parametrize("cid,cls", [("C-1", "campaign"), ("GATEJOB-0001", "gatejob"),
                                     ("sentinel-p", "bulk"), ("sentinel-p-heavy", "bulk")])
def test_every_gate_header_carries_its_class(tmp_path, cid, cls):
    h = _gate_header(tmp_path, cid)
    W.validate_header(h)
    assert h["payload"]["job_class"] == cls


def test_a_persona_header_is_class_persona(tmp_path, monkeypatch):
    monkeypatch.setattr(RG, "cut", lambda *a, **k: ("k", "0" * 40, "0" * 40, [], 0))
    monkeypatch.setattr(RPS.R, "request_to_job", lambda req, wt: {})
    monkeypatch.setattr(RPS.W, "validate_header", lambda h: None)
    req = types.SimpleNamespace(runner="claude", timeout_s=60, run_dir=str(tmp_path / "r1"),
                                persona="implementer", json_schema=None, read_only=False,
                                max_budget_usd=1.0)
    state = {"id": "C-1", "spec_id": "SPEC-1", "repo": {"worktree": str(tmp_path), "base": "main"}}
    h = RPS._header({"data_dir": str(tmp_path / "d")}, {"name": "box"}, state, req,
                    "C-1-persona-r1-a1", tmp_path / "b")
    assert h["kind"] == "persona" and h["payload"]["job_class"] == "persona"


# ---- the mapping (worker) ------------------------------------------------------------------------

def test_an_old_header_is_gatejob_and_unknown_classes_warn():
    assert ES.job_class({"payload": {}}) == ("gatejob", None)
    cls, warn = ES.job_class({"payload": {"job_class": "vip"}})
    assert cls == "gatejob" and "vip" in warn


def test_settings_default_and_override_the_class_map():
    s = ES.settings({})
    assert {c: s["classes"][c]["partition"] for c in ES.CLASSES} == {
        "campaign": "nw-campaign", "gatejob": "nw-gatejob", "persona": "nw-persona",
        "bulk": "nw-bulk"}
    q = ES.settings({"agentic": {"slurm": {"partition": "nw", "classes": {
        "campaign": {"partition": "nw", "qos": "nw-campaign"}, "bulk": {"partition": None},
        "bogus": {"partition": "x"}, "persona": "not-a-map"}}}})
    assert ES.target(q, "campaign") == {"class": "campaign", "partition": "nw",
                                        "qos": "nw-campaign"}
    assert ES.target(q, "bulk") == {"class": "bulk", "partition": "nw", "qos": None}
    assert ES.target(q, "persona")["partition"] == "nw-persona"
    assert "bogus" not in q["classes"]
    assert ES.target(q, "campaign", fallback=True) == {"class": "campaign", "partition": "nw",
                                                       "qos": None}


@pytest.mark.parametrize("cls,part", [("campaign", "nw-campaign"), ("gatejob", "nw-gatejob"),
                                      ("persona", "nw-persona"), ("bulk", "nw-bulk"),
                                      (None, "nw-gatejob")])
def test_partition_mode_submits_to_the_class_partition(wenv, fake, cls, part):
    assert _submit(_cfg(wenv), _plain("j-cls", cls))[0] == 0
    (argv,) = fake.calls("sbatch")
    opts = _opts(argv)
    assert opts["--partition"] == part and "--qos" not in opts
    rec = _read(_job_dir(wenv, "j-cls") / "job.json")
    assert rec["job_class"] == (cls or "gatejob") and rec["slurm_partition"] == part
    assert fake.job(fake.ids()[0])["partition"] == part


def test_the_class_partition_s_limits_clamp_the_request(wenv, fake):
    h = _plain("j-pers", "persona")
    h["payload"]["resources"]["cpus"] = 6                  # nw-persona: MaxCPUsPerNode=4
    assert _submit(_cfg(wenv), h)[0] == 0
    assert _opts(fake.calls("sbatch")[0])["--cpus-per-task"] == "4"


def test_qos_mode_adds_the_qos(wenv, fake):
    classes = {c: {"partition": "nw", "qos": f"nw-{c}"} for c in ES.CLASSES}
    assert _submit(_cfg(wenv, {"classes": classes}), _plain("j-qos", "campaign"))[0] == 0
    opts = _opts(fake.calls("sbatch")[0])
    assert opts["--partition"] == "nw" and opts["--qos"] == "nw-campaign"


def test_a_rejected_partition_falls_back_once_with_a_warning(wenv, fake, capsys):
    fake.reject("partition", "nw-bulk")
    rc, out = _submit(_cfg(wenv), _plain("j-rej", "bulk"))
    assert rc == 0 and out[0]["state"] == "accepted"
    first, second = fake.calls("sbatch")
    assert _opts(first)["--partition"] == "nw-bulk"
    assert "--partition" not in _opts(second) and "--qos" not in _opts(second)
    rec = _read(_job_dir(wenv, "j-rej") / "job.json")
    assert rec["slurm_partition"] is None and rec["job_class"] == "bulk"
    assert any("rejected partition nw-bulk" in w for w in rec["slurm_warnings"])
    assert "rejected partition nw-bulk" in capsys.readouterr().err


def test_a_rejected_qos_falls_back_to_the_configured_partition(wenv, fake):
    fake.reject("qos", "nw-bulk")
    classes = {"bulk": {"partition": "nw-bulk", "qos": "nw-bulk"}}
    sl = {"partition": "nw-gatejob", "classes": classes}
    assert _submit(_cfg(wenv, sl), _plain("j-rejq", "bulk"))[0] == 0
    first, second = fake.calls("sbatch")
    assert _opts(first)["--qos"] == "nw-bulk"
    assert _opts(second)["--partition"] == "nw-gatejob" and "--qos" not in _opts(second)


def test_a_fallback_that_fails_too_unclaims_and_other_errors_never_fall_back(wenv, fake):
    fake.reject("partition", "nw-bulk")
    fake.reject("partition", "nw-gatejob")
    rc, out = _submit(_cfg(wenv, {"partition": "nw-gatejob"}), _plain("j-both", "bulk"))
    assert rc == W.EXIT_ERROR and out[0]["stage"] == "spawn"
    assert len(fake.calls("sbatch")) == 2 and not _job_dir(wenv, "j-both").exists()
    fake.fail("sbatch", "sbatch: error: Batch job submission failed: Requested node "
                        "configuration is not available")
    rc, _ = _submit(_cfg(wenv), _plain("j-node", "campaign"))
    assert rc == W.EXIT_ERROR and len(fake.calls("sbatch")) == 3     # one call, no fallback


def test_slurm_check_reports_the_class_partitions(tmp_path, fake):
    cfg = {"data_dir": str(tmp_path / "data"), "agentic": {"slurm": {"cache_s": 0}}}
    fake.auto("nw-slurm-check-cgroup", "COMPLETED", f"2\n{1 << 30}\n")
    fake.auto("nw-slurm-check-oom", "OUT_OF_MEMORY", "Killed\n")
    rec = ES.slurm_check(cfg, wait_s=10)
    cl = next(c for c in rec["checks"] if c["name"] == "classes")
    assert cl["ok"] is True and cl["detail"]["bulk"] == {
        "partition": "nw-bulk", "partition_exists": True, "qos": None, "qos_exists": None}
    cfg["agentic"]["slurm"]["classes"] = {"bulk": {"partition": "nw-nope"}}
    cl = next(c for c in ES.slurm_check(cfg, wait_s=10)["checks"] if c["name"] == "classes")
    assert cl["ok"] is False and cl["detail"]["bulk"]["partition_exists"] is False
