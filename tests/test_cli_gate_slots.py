"""SPEC-0309: `wt queue gate-slots` shows holders and waiters."""
import json
import os

from click.testing import CliRunner

from wt.agentic import gate_slots as GS
from wt.cli import cli


def test_gate_slots_cli_text_and_json(tmp_path, monkeypatch):
    cfg = {"data_dir": str(tmp_path / "data"), "agentic": {"gates": {"slots": 1}}}
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    runner = CliRunner()
    with GS.acquire(cfg, "SPEC-1-held", poll_s=0.05):
        GS.slots_dir(cfg).mkdir(parents=True, exist_ok=True)
        (GS.slots_dir(cfg) / "waiting-SPEC-2-wait.json").write_text(json.dumps(
            {"campaign": "SPEC-2-wait", "pid": os.getpid(), "since": "2026-10-30T10:00:00-04:00"}))
        res = runner.invoke(cli, ["queue", "gate-slots", "--json"])
        assert res.exit_code == 0, res.output
        out = json.loads(res.output)
        assert out["schema"] == "wt.queue.gate_slots.v1" and out["slots"] == 1
        assert [h["campaign"] for h in out["held"]] == ["SPEC-1-held"]
        assert [w["campaign"] for w in out["waiting"]] == ["SPEC-2-wait"]
        res = runner.invoke(cli, ["queue", "gate-slots"])
        assert res.exit_code == 0, res.output
        assert "SPEC-1-held" in res.output and "SPEC-2-wait" in res.output
        assert "1/1 held" in res.output
    res = runner.invoke(cli, ["queue", "gate-slots"])
    assert "0/1 held" in res.output
