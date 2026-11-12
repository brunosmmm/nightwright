"""SPEC-0406 history and records CLI."""
import json
import hashlib
import os
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.agentic import schemas as SC
from nightwright.cli import cli

pytestmark = pytest.mark.fast


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
    run.tmp = tmp_path
    return run


def test_history_argument_rules_unknown_and_json(invoke):
    assert invoke("history").exit_code == 2
    assert invoke("history", "x", "--list").exit_code == 2
    assert invoke("history", "--list", "--rebuild-cache").exit_code == 2
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


def snapshot(root):
    """Every file (content hash) and directory under `root` (SPEC-0566)."""
    return {path.relative_to(root).as_posix(): (
        "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(root.rglob("*"))}


def test_history_clis_do_not_write(invoke):
    before = snapshot(invoke.tmp)
    assert invoke("history", "SPEC-0394-20261102-ae5b", "--json").exit_code == 0
    assert invoke("history", "--list", "--json").exit_code == 0
    assert invoke("records", "--json").exit_code == 0
    assert snapshot(invoke.tmp) == before


def test_snapshot_detects_writes_anywhere_under_tmp(invoke):
    before = snapshot(invoke.tmp)
    (invoke.tmp / "stray.txt").write_text("x")
    assert snapshot(invoke.tmp) != before
    (invoke.tmp / "stray.txt").unlink()
    assert snapshot(invoke.tmp) == before
    (invoke.tmp / "empty-dir").mkdir()
    assert snapshot(invoke.tmp) != before
    (invoke.tmp / "empty-dir").rmdir()
    changed = next(invoke.root.rglob("state.json"))
    changed.write_text(changed.read_text() + " ")
    assert snapshot(invoke.tmp) != before


def test_history_rejects_escaping_campaign_ids(invoke):
    outside = invoke.tmp / "outside"
    outside.mkdir()
    (outside / "state.json").write_text(json.dumps({"id": "outside", "state": "merged"}))
    (outside / "ledger.jsonl").write_text("")
    campaigns = invoke.root / "agentic" / "campaigns"
    relative = os.path.relpath(outside, campaigns)
    assert relative.startswith("..")
    for cid in (str(outside), relative):
        result = invoke("history", cid)
        assert result.exit_code == 1, result.output
        assert f"campaign {cid} not found" in result.stderr
        assert "schema" not in result.stdout
        assert invoke("history", cid, "--json").stdout.strip() == ""


def test_note_positional_probe_ignores_absolute_paths(invoke, monkeypatch):
    monkeypatch.delenv("NW_CAMPAIGN", raising=False)
    outside = invoke.tmp / "outside"
    outside.mkdir()
    (outside / "state.json").write_text(json.dumps({"id": "outside", "state": "merged"}))
    (outside / "ledger.jsonl").write_text("")
    before = sorted(p.name for p in outside.iterdir())
    result = invoke("note", str(outside), "hello")
    assert result.exit_code == 2, result.output
    assert "campaign required" in result.output
    assert sorted(p.name for p in outside.iterdir()) == before
    assert (outside / "ledger.jsonl").read_text() == ""
    assert not (outside / "notes.jsonl").exists()


def test_each_records_filter_and_or_group_is_non_vacuous(invoke):
    all_rows = json.loads(invoke("records", "--json").output)["records"]
    assert all_rows

    cid = "SPEC-0394-20261102-ae5b"
    campaign = json.loads(invoke("records", "--campaign", cid, "--json").output)["records"]
    assert campaign and all(row["campaign"] == cid for row in campaign)

    for option, field in (
        ("--kind", "kind"), ("--agent", "agent"), ("--severity", "severity"),
        ("--status", "status"),
    ):
        values = list(dict.fromkeys(row[field] for row in all_rows if row.get(field)))[:2]
        assert len(values) == 2
        args = [part for value in values for part in (option, value)]
        rows = json.loads(invoke("records", *args, "--json").output)["records"]
        assert rows and {row[field] for row in rows} == set(values)

    finding = next(row for row in all_rows if row["kind"] == "finding" and row["title"])
    query = finding["title"][:12]
    queried = json.loads(invoke("records", "--query", query, "--json").output)["records"]
    assert queried and finding["id"] in {row["id"] for row in queried}

    newest = json.loads(invoke("records", "--limit", "2", "--json").output)["records"]
    assert newest == all_rows[:2]
    assert json.loads(invoke("records", "--limit", "0", "--json").output)["records"] == []
    assert invoke("records", "--since", "not-a-date").exit_code == 2
    assert invoke("history", "--list", "--until", "not-a-date").exit_code == 2


def test_rebuild_cache_cli(tmp_path, monkeypatch):
    root = tmp_path / "fixture"
    shutil.copytree(FIXTURE, root)
    cache = tmp_path / "cache"
    cache.mkdir()
    cfg = {
        "data_dir": str(root),
        "cache_dir": str(cache),
        "agentic": {"default_profile": "balanced", "history": {"cache": True}},
    }
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    runner = CliRunner()

    assert runner.invoke(cli, ["campaign", "history", "--rebuild-cache", "--list"]).exit_code == 2
    assert runner.invoke(
        cli, ["campaign", "history", "SPEC-0394-20261102-ae5b", "--rebuild-cache"],
    ).exit_code == 2
    assert runner.invoke(cli, ["campaign", "history", "--rebuild-cache", "--state", "merged"]).exit_code == 2
    assert runner.invoke(cli, ["campaign", "history", "--rebuild-cache", "--spec", "SPEC-0394"]).exit_code == 2
    assert runner.invoke(cli, ["campaign", "history", "--rebuild-cache", "--since", "2026-11-01"]).exit_code == 2
    assert runner.invoke(cli, ["campaign", "history", "--rebuild-cache", "--until", "2026-11-08"]).exit_code == 2
    assert runner.invoke(cli, ["campaign", "history", "--rebuild-cache", "--min-cost", "1"]).exit_code == 2
    cfg_no_cache = {"data_dir": str(root), "agentic": {"default_profile": "balanced"}}
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg_no_cache)
    bad = runner.invoke(cli, ["campaign", "history", "--rebuild-cache"])
    assert bad.exit_code == 1
    assert "cache: not writable" in bad.stderr

    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    good = runner.invoke(cli, ["campaign", "history", "--rebuild-cache"])
    assert good.exit_code == 0, good.output
    assert good.output.strip().startswith("history cache rebuilt:")
    doc = json.loads(runner.invoke(cli, ["campaign", "history", "--rebuild-cache", "--json"]).output)
    assert "campaigns" in doc and "records" in doc
    ok_state = runner.invoke(cli, ["campaign", "history", "--rebuild-cache", "--state", "all"])
    assert ok_state.exit_code == 0, ok_state.output


def test_records_text_default_limit_and_json_is_unlimited(invoke):
    path = invoke.root / "agentic" / "conductor" / "decisions.jsonl"
    with path.open("a") as stream:
        for index in range(205):
            stream.write(json.dumps({
                "event_id": f"bulk-{index}", "ts": f"2026-11-04T00:{index // 60:02d}:"
                f"{index % 60:02d}+00:00", "text": f"bulk-default {index}",
            }) + "\n")
    text = invoke("records", "--query", "bulk-default")
    assert text.exit_code == 0
    assert len(text.output.splitlines()) == 200
    rows = json.loads(invoke(
        "records", "--query", "bulk-default", "--json").output)["records"]
    assert len(rows) == 205
