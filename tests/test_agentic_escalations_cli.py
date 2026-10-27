"""SPEC-0232: `wt queue escalate` and `wt escalations list|show|withdraw` through the CLI."""
import json

import pytest
from click.testing import CliRunner

from test_agentic_escalations import CID, _camp, _cfg, _parked, calls  # noqa: F401 (autouse)
from wt.agentic import escalations as ESC
from wt.agentic import mailbox as M
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
    infos = [m for m in M.read_inbox(cfg, "human") if m["kind"] == "info"]
    assert len(infos) == 1 and "ESC-0001" in infos[0]["text"] and "settled" in infos[0]["text"]
    r = run(cfg, monkeypatch, "escalations", "withdraw", "ESC-0001", "--reason", "again",
            "--yes", "--json")
    assert r.exit_code != 0 and "not open" in r.output
    r = run(cfg, monkeypatch, "escalations", "withdraw", "ESC-0099", "--reason", "x", "--yes")
    assert r.exit_code != 0


# ---- SPEC-0235: wt escalations answer ----------------------------------------------------

def test_answer_dry_run_then_yes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    run(cfg, monkeypatch, *ESCALATE, "--yes")
    r = run(cfg, monkeypatch, "escalations", "answer", "ESC-0001", "--choose", "no", "--json")
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["schema"] == "wt.escalation.answer.v1" and d["written"] is False
    assert d["answer"]["choice"] == 2 and d["by"] == "human"
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"
    r = run(cfg, monkeypatch, "escalations", "answer", "ESC-0001", "--choose", "no")
    assert r.exit_code == 0 and "not written" in r.output
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"
    r = run(cfg, monkeypatch, "escalations", "answer", "ESC-0001", "--choose", "2", "--yes",
            "--json")
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["written"] is True
    assert ESC.read(cfg, "ESC-0001")["status"] == "answered"
    r = run(cfg, monkeypatch, "escalations", "answer", "ESC-0001", "--text", "again", "--yes")
    assert r.exit_code != 0 and "answered" in r.output


def test_answer_mixed_forms_and_missing_reason_are_usage_errors(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    run(cfg, monkeypatch, *ESCALATE, "--yes")
    for args, msg in ((["--choose", "1", "--text", "x"], "exactly one"),
                      (["--waive", "a#0"], "non-empty --reason"),
                      (["--choose", "1", "--reason", "r"], "--reason belongs"),
                      ([], "exactly one")):
        r = run(cfg, monkeypatch, "escalations", "answer", "ESC-0001", *args, "--yes")
        assert r.exit_code == 2, (args, r.output)
        assert "Usage:" in r.output and msg in r.output, (args, r.output)
    assert ESC.read(cfg, "ESC-0001")["status"] == "open"
    r = run(cfg, monkeypatch, "escalations", "answer", "ESC-0099", "--choose", "1", "--yes")
    assert r.exit_code == 1 and "no escalation ESC-0099" in r.output       # not a usage error


from test_agentic_escalations import AC4, RV  # noqa: E402


def test_answer_override_plan_output_json_and_show(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    from test_agentic_escalations import _reviewed
    _reviewed(cfg)
    st = S.read_state(cfg, CID)
    st["last_review"]["verdicts"][RV]["findings"][0]["ac"] = AC4
    S.write_state(cfg, CID, st)
    base = ["escalations", "answer", "ESC-0001", "--waive", f"{RV}#0", "--attest", AC4,
            "--reason", "checked by hand"]
    r = run(cfg, monkeypatch, *base)
    assert r.exit_code == 0, r.output
    assert f"waive {RV}#0" in r.output and "major Wrong order" in r.output
    assert f"covers: {AC4}" in r.output and "implicitly attested" in r.output
    assert "not written" in r.output and ESC.read(cfg, "ESC-0001")["status"] == "open"
    assert "human_overrides" not in S.read_state(cfg, CID)
    r = run(cfg, monkeypatch, *base, "--json")
    d = json.loads(r.output)
    assert d["schema"] == "wt.escalation.answer.v1" and d["written"] is False
    ov = d["override"]
    assert [w["id"] for w in ov["waive"]] == [f"{RV}#0"] and ov["attest"] == [AC4]
    assert ov["implicitly_attested"] == [AC4] and ov["reach"] == {AC4: [AC4]}
    assert d["answer"]["kind"] == "override" and d["answer"]["reason"] == "checked by hand"
    r = run(cfg, monkeypatch, *base, "--yes", "--json")
    assert r.exit_code == 0 and json.loads(r.output)["written"] is True
    assert [w["id"] for w in S.read_state(cfg, CID)["human_overrides"]["waive"]] == [f"{RV}#0"]
    r = run(cfg, monkeypatch, "escalations", "show", "ESC-0001")
    assert f"answered by human at" in r.output and f"waived {RV}#0" in r.output
    assert "reason: checked by hand" in r.output


def test_answer_reports_failed_wake_event(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _parked(cfg)
    run(cfg, monkeypatch, *ESCALATE, "--yes")

    def boom(*a, **kw):
        raise OSError("events log unwritable")
    monkeypatch.setattr("wt.agentic.escalations.E.append", boom)
    r = run(cfg, monkeypatch, "escalations", "answer", "ESC-0001", "--choose", "1", "--yes",
            "--json")
    assert r.exit_code == 0, r.output
    assert "conductor was not woken" in r.output           # stderr is mixed into the output
    d = json.loads(r.output[r.output.index("{"):])
    assert d["written"] is True and d["event_error"] == "events log unwritable"
    rec = ESC.read(cfg, "ESC-0001")
    assert rec["status"] == "answered" and "event_error" not in rec
