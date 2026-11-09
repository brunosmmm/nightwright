"""nw hub --json triage (SPEC-0052)."""
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import org_write as W
from nightwright import specs as S
from nightwright.cli import cli
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        (specs / name).write_text((ROOT / "docs" / "specs" / name).read_text())
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED",
        ],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
        "config_dir": str(tmp_path / "config"),
        "outbox_dir": str(tmp_path / "outbox"),
        "outbox_targets": {},
        "project_axis": "bucket",
        "hub_noise_states": ["CATEGORIZE"],
        "hub_tasks_cap": 40,
    }


def _patch(monkeypatch, cfg):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)


def test_hub_requires_json(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["hub"])
    assert r.exit_code != 0
    assert "--json" in r.output


def test_hub_json_envelope(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "hub idea", state="IDEA")

    # active internal
    (Path(cfg["specs_dir"]) / "0001-active.md").write_text(
        "---\nid: SPEC-0001\ntitle: \"Active\"\nstatus: draft\n"
        "kind: feature\nmilestone: M1\ncreated: 2026-07-01\nupdated: 2026-07-01\n---\n\n"
        "## Summary\n\nx\n",
        encoding="utf-8",
    )
    # done — must be omitted
    (Path(cfg["specs_dir"]) / "0002-done.md").write_text(
        "---\nid: SPEC-0002\ntitle: \"Done\"\nstatus: done\n"
        "kind: feature\nmilestone: M1\ncreated: 2026-07-01\nupdated: 2026-07-01\n---\n\n"
        "## Summary\n\nx\n",
        encoding="utf-8",
    )

    out_path, oid = S.scaffold_outbound(cfg, "acme", title="Outbound stub")
    ledger = ROOT / "docs" / "LEDGER.md"
    before = ledger.read_text(encoding="utf-8") if ledger.exists() else None

    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["hub", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["schema"] == "nw.hub.v1"
    assert {i["id"] for i in data["ideas"]} == {"IDEA-001"}
    assert {s["id"] for s in data["internal"]} == {"SPEC-0001"}
    assert data["internal"][0]["status"] == "draft"
    assert len(data["outbound"]) == 1
    assert data["outbound"][0]["id"] == oid
    assert data["outbound"][0]["project"] == "acme"
    assert data["outbound"][0]["path"] == str(out_path)
    assert "today" in data
    assert "date" in data["today"]
    assert "overdue" in data["today"] and "tasks" in data["today"]

    if before is not None:
        assert ledger.read_text(encoding="utf-8") == before


def test_hub_today_slice_membership_and_noise(tmp_path, monkeypatch):
    import datetime as dt

    cfg = _cfg(tmp_path)
    cfg["org_todo_keywords"] = ["TODO", "CATEGORIZE", "|", "DONE"]
    today = dt.datetime.now(cfg["_tz"]).date().isoformat()
    W.add_task(cfg, "due today", scheduled=today)
    W.add_task(cfg, "late work", deadline="2020-01-01")
    W.add_task(cfg, "noise dated", state="CATEGORIZE", scheduled=today)
    W.add_idea(cfg, "idea scheduled", scheduled=today)
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["hub", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["today"]["date"] == today
    heads = {t["heading"] for t in data["today"]["tasks"]}
    assert "due today" in heads
    assert "noise dated" not in heads
    assert "idea scheduled" not in heads
    overdue_heads = {t["heading"] for t in data["today"]["overdue"]}
    assert "late work" in overdue_heads


def test_hub_tasks_slice_open_and_none(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg["hub_tasks_cap"] = 2
    cfg["org_todo_keywords"] = ["TODO", "CATEGORIZE", "|", "DONE"]
    W.add_task(cfg, "a one")
    W.add_task(cfg, "b two")
    W.add_task(cfg, "c three")
    W.add_task(cfg, "noise", state="CATEGORIZE")
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["hub", "--json", "--tasks-slice", "open"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["today"]["slice"] == "open"
    assert len(data["today"]["tasks"]) == 2
    assert all(t["heading"] != "noise" for t in data["today"]["tasks"])
    r = CliRunner().invoke(cli, ["hub", "--json", "--tasks-slice", "none"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert "today" not in data


def test_hub_help_documents_json():
    r = CliRunner().invoke(cli, ["hub", "--help"])
    assert r.exit_code == 0
    assert "--json" in r.output
    assert "--tasks-slice" in r.output


# ---- SPEC-0292: agentic slice + idea_row.campaign ---------------------------------

def _campaign(cfg, cid, project, state, *, idea=None, link=None, cost=1.5, cap=10.0, park=None):
    from nightwright.agentic import store
    st = {"id": cid, "kind": "idea" if idea else "implement", "state": state,
          "spec_id": None if idea else "SPEC-0001", "project": project, "priority": 3,
          "cost": {"total_usd": cost, "cap_usd": cap}, "park": park}
    if idea:
        st["idea"] = {"id": idea}
    if link:
        st["idea_link"] = {"id": link}
    store.create_campaign(cfg, st)


def _hub(cfg, monkeypatch, *args):
    _patch(monkeypatch, cfg)
    r = CliRunner().invoke(cli, ["hub", "--json", *args])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def test_hub_agentic_slice(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "hub idea", state="IDEA", project="Meta-Tools")
    _campaign(cfg, "C-1", "Meta-Tools", "IMPLEMENTING", link="IDEA-001")
    _campaign(cfg, "C-2", "Other", "PARKED", idea="IDEA-002", park={"reason": "cost_cap"})
    _campaign(cfg, "C-3", "Other", "MERGED", link="IDEA-001")              # terminal: hidden
    _campaign(cfg, "C-4", "Meta-Tools", "QUEUED")                          # no idea at all
    ag = _hub(cfg, monkeypatch)["agentic"]
    rows = {c["id"]: c for c in ag["campaigns"]}
    assert set(rows) == {"C-1", "C-2", "C-4"}
    assert rows["C-1"]["idea_id"] == "IDEA-001" and rows["C-1"]["cost_usd"] == 1.5
    assert rows["C-1"]["cap_usd"] == 10.0 and rows["C-1"]["state"] == "IMPLEMENTING"
    assert rows["C-2"]["park_reason"] == "cost_cap" and rows["C-2"]["idea_id"] == "IDEA-002"
    assert rows["C-4"]["idea_id"] is None and rows["C-4"]["park_reason"] is None
    assert ag["by_idea"] == {"IDEA-001": ["C-1"], "IDEA-002": ["C-2"]}
    assert ag["counts"] == {"IMPLEMENTING": 1, "PARKED": 1, "QUEUED": 1}
    # --project filters the slice; the idea filters do not
    only = _hub(cfg, monkeypatch, "--project", "Other")["agentic"]
    assert [c["id"] for c in only["campaigns"]] == ["C-2"]
    assert only["counts"] == {"PARKED": 1}
    filtered = _hub(cfg, monkeypatch, "--tag", "nope", "--state", "INCUBATE")
    assert filtered["ideas"] == [] and filtered["agentic"] == ag


def test_hub_agentic_spec_id_falls_back_to_written_spec(tmp_path, monkeypatch):
    """An idea campaign that wrote a spec carries the id in `spec.id`, not `spec_id`."""
    from nightwright.agentic import store
    cfg = _cfg(tmp_path)
    store.create_campaign(cfg, {"id": "C-9", "kind": "idea", "state": "IMPLEMENTING",
                                "spec_id": None, "spec": {"id": "SPEC-0042"},
                                "idea": {"id": "IDEA-009"}, "project": "Meta-Tools"})
    (row,) = _hub(cfg, monkeypatch)["agentic"]["campaigns"]
    assert row["spec_id"] == "SPEC-0042" and row["idea_id"] == "IDEA-009"


def test_hub_agentic_error_isolated(tmp_path, monkeypatch):
    from nightwright.agentic import store

    def boom(*a, **k):
        raise RuntimeError("campaigns unreadable")
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "hub idea", state="IDEA")
    monkeypatch.setattr(store, "list_campaigns", boom)
    data = _hub(cfg, monkeypatch)
    assert data["agentic"] == {"error": "campaigns unreadable"}
    assert [i["id"] for i in data["ideas"]] == ["IDEA-001"]


def test_idea_row_campaign_field(tmp_path):
    from nightwright import report as RP
    from nightwright.org import load_tasks
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "plain")
    W.add_idea(cfg, "running")
    tasks = {t.heading: t for t in load_tasks(cfg) if t.is_idea}
    W.set_property(cfg, tasks["running"], "CAMPAIGN", "SPEC-0001-x")
    tasks = {t.heading: t for t in load_tasks(cfg) if t.is_idea}
    assert RP.idea_row(cfg, tasks["running"])["campaign"] == "SPEC-0001-x"
    assert "campaign" not in RP.idea_row(cfg, tasks["plain"])
