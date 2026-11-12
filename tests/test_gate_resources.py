"""SPEC-0504: per-gate `resources` in project config, the per-kind defaults and the job request."""
import pytest

from nightwright.agentic import gate_resources as GR
from nightwright.agentic import project as P

pytestmark = pytest.mark.fast


def _doc(**gate):
    return {"schema": P.SCHEMA, "gates": [{"name": "g", "cmd": "true", **gate}]}


def test_config_accepts_a_full_resources_block():
    P.validate(_doc(resources={"cpus": 3, "mem": "3G", "licenses": ["jerome_target"]}))
    P.validate(_doc(resources={"mem": "512M"}))
    P.validate(_doc(resources={"cpus": 1, "mem": "1.5G", "licenses": ["x:2"]}))


@pytest.mark.parametrize("res, needle", [
    ({"cpus": 0}, "cpus: must be an integer >= 1"),
    ({"cpus": True}, "cpus: must be an integer >= 1"),
    ({"mem": "3GB"}, "mem: must be a size"),
    ({"mem": 3}, "mem: must be a size"),
    ({"mem": "0G"}, "mem: must be a size"),
    ({"licenses": ["Bad-Name"]}, "licenses: must be a list"),
    ({"licenses": "jerome_target"}, "licenses: must be a list"),
    ({"gpus": 1}, "resources.gpus: unknown key"),
    ("3G", "resources: must be a mapping"),
])
def test_config_rejects_bad_resources(res, needle):
    with pytest.raises(ValueError) as e:
        P.validate(_doc(resources=res))
    assert needle in str(e.value)


def test_defaults_per_kind():
    full = {"name": "pytest", "cmd": "x", "results": {"format": "pytest"}}
    aff = {"name": "pytest-affected", "cmd": "x", "results": {"format": "pytest"}}
    lint = {"name": "spec-lint", "cmd": "x"}
    assert GR.gate_request(full) == {"cpus": 5, "mem_mb": 5120, "licenses": {}}
    assert GR.gate_request(aff) == {"cpus": 3, "mem_mb": 3072, "licenses": {}}
    assert GR.gate_request(lint) == {"cpus": 1, "mem_mb": 1024, "licenses": {}}
    assert GR.persona_request() == {"cpus": 1, "mem_mb": 2048, "licenses": {}}
    # a declared value wins per key; the rest stays the kind's default
    assert GR.gate_request({**lint, "resources": {"mem": "512M"}}) == \
        {"cpus": 1, "mem_mb": 512, "licenses": {}}


def test_host_overrides_the_defaults():
    cfg = {"agentic": {"gates": {"resource_defaults": {
        "default": {"cpus": 2, "mem": "2G"}, "persona": {"mem": "bad"}}}}}
    assert GR.gate_request({"name": "x", "cmd": "x"}, cfg) == \
        {"cpus": 2, "mem_mb": 2048, "licenses": {}}
    assert GR.persona_request(cfg)["mem_mb"] == 2048      # a malformed override is ignored


def test_job_request_is_max_union_and_the_job_timeout():
    from nightwright.agentic import remote_gates as RG
    gates = [{"name": "lint", "cmd": "x", "timeout_s": 120},
             {"name": "pytest", "cmd": "x", "results": {"format": "pytest"}, "timeout_s": 600,
              "retries": 1},
             {"name": "cargo", "cmd": "x", "timeout_s": 300,
              "resources": {"cpus": 6, "mem": "8G", "licenses": ["jerome_target", "x:2"]}},
             {"name": "cargo2", "cmd": "x", "timeout_s": 60,
              "resources": {"licenses": ["x:3"]}}]
    req = GR.job_request(gates, {})
    assert req["cpus"] == 6 and req["mem_mb"] == 8192
    assert req["licenses"] == {"jerome_target": 1, "x": 3}
    assert req["time_s"] == int(RG.job_timeout_s({}, gates) + RG.RUN_TIMEOUT_MARGIN_S)
    assert set(req["gates"]) == {"lint", "pytest", "cargo", "cargo2"}
    assert req["gates"]["pytest"] == {"cpus": 5, "mem_mb": 5120, "licenses": {}}
    assert GR.job_request([], {}, time_s=10)["cpus"] == 1


def test_a_resolved_request_wins():
    g = {"name": "x", "cmd": "x", GR.REQUEST_KEY: {"cpus": 7, "mem_mb": 700,
                                                   "licenses": {"a": 1}}}
    assert GR.gate_request(g) == {"cpus": 7, "mem_mb": 700, "licenses": {"a": 1}}


def test_payload_bounds_check():
    ok = GR.job_request([{"name": "x", "cmd": "x"}], {}, time_s=5)
    assert GR.check_payload(ok) is None
    assert GR.check_payload([]) is not None
    assert GR.check_payload({**ok, "cpus": 0}) is not None
    assert GR.check_payload({**ok, "mem_mb": "1G"}) is not None
    assert GR.check_payload({**ok, "licenses": {"Bad": 1}}) is not None
    assert GR.check_payload({**ok, "time_s": -1}) is not None
    assert GR.check_payload({**ok, "gates": {"x": {"cpus": 1}}}) is not None


def test_a_header_with_and_without_payload_resources_validates():
    from nightwright.agentic import worker as W
    h = {"schema": W.JOB_SCHEMA, "kind": "gate", "job_id": "j", "repo_key": "r",
         "campaign": "c", "spec_id": "s", "commit": "a" * 40,
         "base": {"name": "main", "sha": "b" * 40}, "bundle_bytes": 0, "env": {},
         "payload": {"seq": 1, "gates": [{"name": "x", "cmd": "true"}]}}
    W.validate_header(h)                                    # an older coordinator: no block
    h["payload"]["resources"] = GR.job_request(h["payload"]["gates"], {}, time_s=60)
    W.validate_header(h)
    h["payload"]["resources"]["cpus"] = 0
    with pytest.raises(W.WorkerError) as e:
        W.validate_header(h)
    assert e.value.stage == "validate" and "payload.resources" in e.value.error


def test_the_dogfood_config_declares_resources():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    cfg = P.load_project_config(root / ".nw" / "project.yaml")
    by = {g["name"]: g.get("resources") for g in cfg["gates"]}
    assert by["pytest-affected"] == {"cpus": 3, "mem": "3G"}
    assert by["pytest"] == {"cpus": 5, "mem": "5G"}
    assert by["docs-tests"] == {"cpus": 3, "mem": "3G"}


def test_adhoc_resources_mapping_drives_the_job_request():
    """SPEC-0571: the `resources` the `nw gate run --cmd` flags set ride the gate dict."""
    pytest_gate = {"name": "cmd-1", "cmd": "x", "results": {"format": "pytest"}}
    full = {**pytest_gate, "resources": {"cpus": 8, "mem": "12G", "licenses": ["lic:2"]}}
    req = GR.job_request([full], {}, time_s=60)
    assert (req["cpus"], req["mem_mb"], req["licenses"]) == (8, 12288, {"lic": 2})
    part = GR.job_request([{**pytest_gate, "resources": {"cpus": 8}}], {}, time_s=60)
    assert (part["cpus"], part["mem_mb"]) == (8, 3072)
    part = GR.job_request([{**pytest_gate, "resources": {"mem": "8G"}}], {}, time_s=60)
    assert (part["cpus"], part["mem_mb"]) == (3, 8192)
    none = GR.job_request([pytest_gate], {}, time_s=60)
    assert (none["cpus"], none["mem_mb"], none["licenses"]) == (3, 3072, {})
