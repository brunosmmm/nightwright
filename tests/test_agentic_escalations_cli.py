"""SPEC-0232: `wt queue escalate` and `wt escalations list|show|withdraw` through the CLI."""
import json

import pytest
from click.testing import CliRunner

from test_agentic_escalations import CID, _camp, _cfg, _parked, calls  # noqa: F401 (autouse)
from wt.agentic import escalations as ESC
from wt.agentic import store as S
from wt.cli import cli


def run(cfg, monkeypatch, *args):
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, list(args))


ESCALATE = ["queue", "escalate", CID, "--question", "ship it?", "--option", "yes",
            "--option", "no", "--recommend", "1", "--fact", "gate green"]


def test_escalate_dry_run_then_yes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    r = run(cfg, monkeypatch, *ESCALATE, "--json")
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "wt.queue.escalate.v1" and d["written"] is False
    assert d["park"]["escalated_from"] == "loop_exhausted"
    assert ESC.list_all(cfg) == [] and S.read_state(cfg, CID)["park"]["reason"] == "loop_exhausted"
    r = run(cfg, monkeypatch, *ESCALATE, "--yes", "--json")
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["written"] is True and d["escalation"]["id"] == "ESC-0001"
    assert S.read_state(cfg, CID)["park"]["reason"] == "human_decision"
    r = run(cfg, monkeypatch, *ESCALATE, "--yes")
    assert r.exit_code != 0 and "ESC-0001" in r.output


def test_escalate_text_output_and_bad_recommend(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    r = run(cfg, monkeypatch, "queue", "escalate", "--question", "q?", "--option", "a",
            "--recommend", "3", "--yes")
    assert r.exit_code != 0 and "not an option" in r.output
    r = run(cfg, monkeypatch, "queue", "escalate", "--question", "q?", "--option", "a")
    assert r.exit_code == 0 and "not written" in r.output
    r = run(cfg, monkeypatch, "queue", "escalate", "--question", "q?", "--option", "a", "--yes")
    assert r.exit_code == 0 and "ESC-0001 opened" in r.output


@pytest.fixture
def two(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    assert run(cfg, monkeypatch, *ESCALATE, "--yes").exit_code == 0
    assert run(cfg, monkeypatch, "queue", "escalate", "--question", "global?", "--yes",
               ).exit_code == 0
    return cfg


def test_list_all_and_json(two, monkeypatch):
    cfg = two
    ESC.withdraw(cfg, "ESC-0002", "n/a", "conductor")
    r = run(cfg, monkeypatch, "escalations", "list")
    assert "ESC-0001" in r.output and "ESC-0002" not in r.output
    r = run(cfg, monkeypatch, "escalations", "list", "--all")
    assert "ESC-0001" in r.output and "ESC-0002" in r.output and "withdrawn" in r.output
    d = json.loads(run(cfg, monkeypatch, "escalations", "list", "--all", "--json").output)
    assert d["schema"] == "wt.escalation.list.v1"
    assert [e["id"] for e in d["escalations"]] == ["ESC-0002", "ESC-0001"]
    d = json.loads(run(cfg, monkeypatch, "escalations", "list", "--json").output)
    assert [e["id"] for e in d["escalations"]] == ["ESC-0001"]


def test_show_text_and_json_and_unknown(two, monkeypatch):
    cfg = two
    r = run(cfg, monkeypatch, "escalations", "show", "ESC-0001")
    assert r.exit_code == 0 and "1. yes  (recommended)" in r.output
    assert "2. no" in r.output and "gate green" in r.output
    assert "loop_exhausted → IMPLEMENTING" in r.output
    rec = json.loads(run(cfg, monkeypatch, "escalations", "show", "ESC-0001", "--json").output)
    assert rec == ESC.read(cfg, "ESC-0001")
    r = run(cfg, monkeypatch, "escalations", "show", "ESC-0099")
    assert r.exit_code != 0 and "no escalation ESC-0099" in r.output
    assert run(cfg, monkeypatch, "escalations", "show", "bogus").exit_code != 0


def test_withdraw_dry_run_then_yes_leaves_campaign_parked(two, monkeypatch):
    cfg = two
    r = run(cfg, monkeypatch, "escalations", "withdraw", "ESC-0001", "--reason", "settled")
    assert r.exit_code == 0 and "not written" in r.output
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"
    r = run(cfg, monkeypatch, "escalations", "withdraw", "ESC-0001", "--reason", "settled",
            "--yes")
    assert r.exit_code == 0, r.output
    assert f"wt queue resume {CID} --yes" in r.output
    rec = ESC.read(cfg, "ESC-0001")
    assert rec["status"] == "withdrawn" and rec["withdrawn"]["reason"] == "settled"
    st = S.read_state(cfg, CID)
    assert st["state"] == "PARKED" and st["park"]["reason"] == "human_decision"
    assert any("ESC-0001 withdrawn: settled" in e["text"]
               for e in S.read_ledger(cfg, CID, types=["note"]))
    r = run(cfg, monkeypatch, "escalations", "withdraw", "ESC-0001", "--reason", "again",
            "--yes", "--json")
    assert r.exit_code != 0 and "not open" in r.output
    r = run(cfg, monkeypatch, "escalations", "withdraw", "ESC-0099", "--reason", "x", "--yes")
    assert r.exit_code != 0
