"""SPEC-0406 history and records CLI."""
import json
import hashlib
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import schemas as SC
from nightwright.cli import cli


FIXTURE = Path(__file__).parent / "fixtures" / "agentic_history"


@pytest.fixture
def invoke(tmp_path, monkeypatch):
    root = tmp_path / "fixture"
    shutil.copytree(FIXTURE, root)
    cfg = {"data_dir": str(root), "agentic": {"default_profile": "balanced"}}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)

    def run(*args):
        return CliRunner().invoke(cli, ["campaign", *args])
    run.root = root
    return run


def test_history_argument_rules_unknown_and_json(invoke):
    assert invoke("history").exit_code == 2
    assert invoke("history", "x", "--list").exit_code == 2
    missing = invoke("history", "NOPE")
    assert missing.exit_code == 1
    assert "campaign NOPE not found" in missing.stderr
    result = invoke("history", "SPEC-0394-20261102-ae5b", "--json")
    assert result.exit_code == 0, result.output
    assert SC.history_problems(json.loads(result.output)) == []


def test_list_json_and_filters(invoke):
    result = invoke("history", "--list", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["schema"] == SC.HISTORY_INDEX_SCHEMA
    assert len(doc["rows"]) == 8
    assert all(not SC.index_problems(row) for row in doc["rows"])
    merged = json.loads(invoke("history", "--list", "--state", "merged", "--json").output)["rows"]
    assert merged and all(row["outcome"] == "merged" for row in merged)
    assert json.loads(invoke("history", "--list", "--project", "none", "--json").output)["rows"] == []
    assert json.loads(invoke("history", "--list", "--spec", "SPEC-0394", "--json").output)[
        "rows"][0]["campaign"] == "SPEC-0394-20261102-ae5b"
    assert json.loads(invoke("history", "--list", "--min-cost", "10000", "--json").output)[
        "rows"] == []
    recent = json.loads(invoke(
        "history", "--list", "--since", "2026-11-02", "--json").output)["rows"]
    assert recent and all((row["closed_at"] or row["created_at"])[:10] >= "2026-11-02"
                          for row in recent)
    old = json.loads(invoke(
        "history", "--list", "--until", "2026-10-25", "--json").output)["rows"]
    assert old and all((row["closed_at"] or row["created_at"])[:10] <= "2026-10-25"
                       for row in old)


def test_records_json_repeated_filters_query_limit_and_literal_text(invoke):
    result = invoke("records", "--kind", "finding", "--kind", "decision", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["schema"] == SC.HISTORY_RECORD_SCHEMA
    assert doc["records"]
    assert {row["kind"] for row in doc["records"]} <= {"finding", "decision"}
    cid = "SPEC-0394-20261102-ae5b"
    rows = json.loads(invoke(
        "records", "--campaign", cid, "--agent", "conformance_reviewer",
        "--agent", "test_reviewer", "--severity", "must_fix", "--severity", "major",
        "--status", "open", "--status", "fixed", "--query", "test", "--limit", "2", "--json",
    ).output)["records"]
    assert len(rows) <= 2
    assert all(row["campaign"] == cid for row in rows)
    project = json.loads(invoke(
        "records", "--project", "Meta-Tools", "--since", "2026-11-01",
        "--until", "2026-11-03", "--json").output)["records"]
    assert project and all(row["project"] == "Meta-Tools" for row in project)
    assert all("2026-11-01" <= row["ts"][:10] <= "2026-11-03" for row in project)
    assert json.loads(invoke("records", "--project", "none", "--json").output)["records"] == []
    ledger = invoke.root / "agentic" / "campaigns" / cid / "ledger.jsonl"
    last = json.loads(ledger.read_text().splitlines()[-1])
    planted = {**last, "seq": last["seq"] + 1, "type": "finding",
               "persona": "test_reviewer", "severity": "minor", "gate": None,
               "title": "[bold]x[/]", "detail": "literal", "files": [], "run_id": None,
               "key": "planted"}
    with ledger.open("a") as stream:
        stream.write(json.dumps(planted) + "\n")
    literal = invoke("records", "--query", "[bold]")
    assert literal.exit_code == 0 and "[bold]x[/]" in literal.output


def test_history_clis_do_not_write(invoke):
    def hashes():
        return {str(path.relative_to(invoke.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in invoke.root.rglob("*") if path.is_file()}
    before = hashes()
    assert invoke("history", "SPEC-0394-20261102-ae5b", "--json").exit_code == 0
    assert invoke("history", "--list", "--json").exit_code == 0
    assert invoke("records", "--json").exit_code == 0
    assert hashes() == before
