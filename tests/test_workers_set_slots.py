"""SPEC-0390: `nw workers set NAME --slots N`, temp data dir only."""
import json

import pytest
from click.testing import CliRunner

from nightwright.agentic import gate_slots as GS
from nightwright.agentic import workers as WK
from nightwright.cli import cli

pytestmark = pytest.mark.fast


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "agentic": {"gates": {"slots": 3}}}


@pytest.fixture
def invoke(cfg, monkeypatch):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return lambda *a: CliRunner().invoke(cli, ["workers", *a])


def _seed(cfg, **kw):
    p = WK.path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"name": "box", "ssh": "me@box", "slots": 2, "kinds": ["gate"], "nw": "nw",
           "enabled": True, **kw}
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [rec]}))


def test_set_slots_keeps_other_state(cfg, invoke):
    extra = {"draining": True, "update_pending": {"since": "2026-11-02T00:00:00+00:00"},
             "cooldown_until": "2999-01-01T00:00:00+00:00"}
    _seed(cfg, **extra)
    before = json.loads(WK.path(cfg).read_text())["workers"][0]
    res = invoke("set", "box", "--slots", "10")
    assert res.exit_code == 0 and "gate slots 10 (was 2)" in res.output
    after = json.loads(WK.path(cfg).read_text())["workers"][0]
    assert after["slots"] == 10
    assert {k: v for k, v in after.items() if k != "slots"} == \
        {k: v for k, v in before.items() if k != "slots"}


@pytest.mark.parametrize("n", ["0", "17"])
def test_set_slots_out_of_range_refused(cfg, invoke, n):
    _seed(cfg)
    assert invoke("set", "box", "--slots", n).exit_code != 0
    assert WK.load(cfg)[0]["slots"] == 2


def test_lowering_keeps_a_held_slot_and_grants_no_more(cfg, invoke):
    _seed(cfg, slots=3)
    with GS.acquire(cfg, "a", pools=[("box", 3)]), GS.acquire(cfg, "b", pools=[("box", 3)]), \
            GS.acquire(cfg, "c", pools=[("box", 3)]):
        assert invoke("set", "box", "--slots", "1").exit_code == 0
        held = GS.occupancy(cfg)["workers"]["box"]
        assert held["slots"] == 1 and len(held["held"]) == 3     # nothing killed or hidden
        assert GS._try_take(cfg, [("box", WK.load(cfg)[0]["slots"])], "d", 0.0) is None
    assert GS.occupancy(cfg)["workers"]["box"]["held"] == []
    assert invoke("set", "box", "--slots", "4").exit_code == 0
    got = GS._try_take(cfg, [("box", 4)], "e", 0.0)
    assert got is not None
    GS._release(got[1])
