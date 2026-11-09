"""Next-step workflow hints (SPEC-0023): next_step_for_idea + nw next / nw ideas column."""
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import Task
from nightwright.workflow import next_step_for_idea

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        (specs_dir / name).write_text((ROOT / "docs" / "specs" / name).read_text())
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs_dir),
        "outbox_dir": str(outbox),
        "config_dir": str(tmp_path / "config"),
    }


def _idea(**kw):
    defaults = dict(
        id="IDEA-001", heading="test idea", state="IDEA", is_done=False,
        tags=frozenset(), priority=None, properties={"ID": "IDEA-001"},
        level=1, project="", file="", line=1, topic_key=None, is_idea=True, epic=None,
    )
    defaults.update(kw)
    if "properties" in kw and "ID" not in kw["properties"]:
        defaults["properties"] = {**kw["properties"], "ID": defaults["id"]}
    return Task(**defaults)


def _write_spec(specs_dir, spec_id, status, num=None):
    num = num or spec_id.split("-", 1)[1]
    path = Path(specs_dir) / f"{num}-test.md"
    path.write_text(
        f"---\nid: {spec_id}\ntitle: Test\nstatus: {status}\nowner: t\n"
        f"created: 2026-07-22\n---\n\n## Context\n\nx\n",
        encoding="utf-8",
    )
    return path


# ---- next_step_for_idea ---------------------------------------------------------------

def test_next_idea_no_spec(tmp_path):
    cfg = _cfg(tmp_path)
    assert next_step_for_idea(cfg, _idea()) == "nw idea log IDEA-001"


def test_next_incubate_no_spec(tmp_path):
    cfg = _cfg(tmp_path)
    assert next_step_for_idea(cfg, _idea(state="INCUBATE")) == "nw idea log IDEA-001"


def test_next_specced_draft(tmp_path):
    cfg = _cfg(tmp_path)
    _write_spec(cfg["specs_dir"], "SPEC-0099", "draft", num="0099")
    idea = _idea(state="SPECCED", properties={"ID": "IDEA-001", "SPEC": "SPEC-0099"})
    assert next_step_for_idea(cfg, idea) == "authxx SPEC-0099 → accepted"


def test_next_specced_accepted(tmp_path):
    cfg = _cfg(tmp_path)
    _write_spec(cfg["specs_dir"], "SPEC-0099", "accepted", num="0099")
    idea = _idea(state="SPECCED", properties={"ID": "IDEA-001", "SPEC": "SPEC-0099"})
    assert next_step_for_idea(cfg, idea) == "nw spec generate SPEC-0099"


def test_next_promoted(tmp_path):
    cfg = _cfg(tmp_path)
    assert next_step_for_idea(cfg, _idea(state="PROMOTED", is_done=True)) == "nw tasks"


def test_next_dropped_empty(tmp_path):
    cfg = _cfg(tmp_path)
    assert next_step_for_idea(cfg, _idea(state="DROPPED", is_done=True)) == ""


def test_next_missing_spec_soft_fail(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _idea(state="SPECCED", properties={"ID": "IDEA-001", "SPEC": "SPEC-9999"})
    assert next_step_for_idea(cfg, idea) == "spec SPEC-9999 missing"


def test_next_outbound_export(tmp_path):
    cfg = _cfg(tmp_path)
    proj = Path(cfg["outbox_dir"]) / "acme"
    proj.mkdir()
    (proj / "ACME-0001-x.md").write_text(
        "---\nid: ACME-0001\ntitle: X\nstatus: accepted\nowner: t\n"
        "created: 2026-07-22\ntarget_project: acme\ntarget_repo: /tmp/t\n---\n\n## Context\n\n",
        encoding="utf-8",
    )
    idea = _idea(state="SPECCED", properties={"ID": "IDEA-001", "SPEC": "ACME-0001"})
    assert next_step_for_idea(cfg, idea) == "nw spec export ACME-0001"


def test_next_outbound_exported_implement(tmp_path):
    """SPEC-0041: exported + accepted → implement in target, not nw tasks."""
    cfg = _cfg(tmp_path)
    proj = Path(cfg["outbox_dir"]) / "acme"
    proj.mkdir()
    (proj / "ACME-0001-x.md").write_text(
        "---\nid: ACME-0001\ntitle: X\nstatus: accepted\nowner: t\n"
        "created: 2026-07-22\ntarget_project: acme\ntarget_repo: ~/work/acme\n---\n\n"
        "## Context\n\n",
        encoding="utf-8",
    )
    (proj / "PROVENANCE.md").write_text("exported ACME-0001\n", encoding="utf-8")
    idea = _idea(state="EXPORTED", is_done=True,
                 properties={"ID": "IDEA-001", "SPEC": "ACME-0001"})
    assert next_step_for_idea(cfg, idea) == "implement in ~/work/acme"


def test_next_outbound_exported_pull_status(tmp_path):
    """SPEC-0068: the in-progress hint mentions both pull-status and pull-clock."""
    cfg = _cfg(tmp_path)
    proj = Path(cfg["outbox_dir"]) / "acme"
    proj.mkdir()
    (proj / "ACME-0001-x.md").write_text(
        "---\nid: ACME-0001\ntitle: X\nstatus: in-progress\nowner: t\n"
        "created: 2026-07-22\ntarget_project: acme\ntarget_repo: ~/work/acme\n---\n\n"
        "## Context\n\n",
        encoding="utf-8",
    )
    (proj / "PROVENANCE.md").write_text("exported ACME-0001\n", encoding="utf-8")
    idea = _idea(state="SPECCED", properties={"ID": "IDEA-001", "SPEC": "ACME-0001"})
    assert next_step_for_idea(cfg, idea) == "nw spec pull-status/pull-clock ACME-0001"


def test_next_outbound_exported_done_quiet(tmp_path):
    cfg = _cfg(tmp_path)
    proj = Path(cfg["outbox_dir"]) / "acme"
    proj.mkdir()
    (proj / "ACME-0001-x.md").write_text(
        "---\nid: ACME-0001\ntitle: X\nstatus: done\nowner: t\n"
        "created: 2026-07-22\ntarget_project: acme\ntarget_repo: ~/work/acme\n---\n\n"
        "## Context\n\n",
        encoding="utf-8",
    )
    (proj / "PROVENANCE.md").write_text("exported ACME-0001\n", encoding="utf-8")
    idea = _idea(state="EXPORTED", is_done=True,
                 properties={"ID": "IDEA-001", "SPEC": "ACME-0001"})
    assert next_step_for_idea(cfg, idea) == ""


def _write_outbound_epic(cfg, *, exported=False, breakdown_open=True, children=None):
    """Outbound epic DEMO-0001; optional PROVENANCE export + child specs."""
    proj = Path(cfg["outbox_dir"]) / "AI-workstreams"
    proj.mkdir(parents=True, exist_ok=True)
    boxes = "- [ ] child A\n- [ ] child B\n" if breakdown_open else "- [x] child A\n- [x] child B\n"
    (proj / "DEMO-0001-epic.md").write_text(
        "---\nid: DEMO-0001\ntitle: Epic\nstatus: accepted\nowner: t\n"
        "created: 2026-07-22\nkind: epic\n"
        "target_project: AI-workstreams\ntarget_repo: /tmp/t\n---\n\n"
        "## Context\n\nx\n\n## Breakdown / sub-specs\n\n"
        f"{boxes}\n## Decision\n\nx\n",
        encoding="utf-8",
    )
    if exported:
        (proj / "PROVENANCE.md").write_text(
            "exported DEMO-0001\n", encoding="utf-8",
        )
    for child in children or []:
        cid, cstatus = child["id"], child["status"]
        (proj / f"{cid}-child.md").write_text(
            f"---\nid: {cid}\ntitle: Child\nstatus: {cstatus}\nowner: t\n"
            f"created: 2026-07-22\nparent: DEMO-0001\n"
            f"target_project: AI-workstreams\ntarget_repo: /tmp/t\n---\n\n"
            "## Context\n\nx\n",
            encoding="utf-8",
        )


def test_next_incomplete_outbound_epic_exported(tmp_path):
    """Motivating bug: accepted+exported epic with open Breakdown → /nw-rework, not nw tasks."""
    cfg = _cfg(tmp_path)
    _write_outbound_epic(cfg, exported=True, breakdown_open=True, children=[])
    idea = _idea(state="PROMOTED", is_done=True,
                 properties={"ID": "IDEA-001", "SPEC": "DEMO-0001"})
    assert next_step_for_idea(cfg, idea) == "/nw-rework IDEA-001"


def test_next_incomplete_outbound_epic_specced(tmp_path):
    cfg = _cfg(tmp_path)
    _write_outbound_epic(cfg, exported=True, breakdown_open=True, children=[])
    idea = _idea(state="SPECCED",
                 properties={"ID": "IDEA-001", "SPEC": "DEMO-0001"})
    assert next_step_for_idea(cfg, idea) == "/nw-rework IDEA-001"


def test_next_complete_outbound_epic_exported(tmp_path):
    cfg = _cfg(tmp_path)
    _write_outbound_epic(
        cfg, exported=True, breakdown_open=False,
        children=[
            {"id": "DEMO-0001", "status": "done"},
            {"id": "DEMO-0001", "status": "superseded"},
        ],
    )
    idea = _idea(state="PROMOTED", is_done=True,
                 properties={"ID": "IDEA-001", "SPEC": "DEMO-0001"})
    assert next_step_for_idea(cfg, idea) == "nw tasks"


def test_next_incomplete_internal_epic_open_child(tmp_path):
    cfg = _cfg(tmp_path)
    path = Path(cfg["specs_dir"]) / "0098-epic.md"
    path.write_text(
        "---\nid: SPEC-0098\ntitle: Epic\nstatus: accepted\nowner: t\n"
        "created: 2026-07-22\nkind: epic\n---\n\n## Context\n\nx\n\n"
        "## Breakdown / sub-specs\n\n- [x] done bit\n- [ ] open bit\n",
        encoding="utf-8",
    )
    (Path(cfg["specs_dir"]) / "0097-child.md").write_text(
        "---\nid: SPEC-0097\ntitle: Child\nstatus: in-progress\nowner: t\n"
        "created: 2026-07-22\nparent: SPEC-0098\n---\n\n## Context\n\nx\n",
        encoding="utf-8",
    )
    idea = _idea(state="SPECCED",
                 properties={"ID": "IDEA-002", "SPEC": "SPEC-0098"})
    assert next_step_for_idea(cfg, idea) == "/nw-rework IDEA-002"


def test_next_complete_internal_epic(tmp_path):
    cfg = _cfg(tmp_path)
    (Path(cfg["specs_dir"]) / "0096-epic.md").write_text(
        "---\nid: SPEC-0096\ntitle: Epic\nstatus: done\nowner: t\n"
        "created: 2026-07-22\nkind: epic\n---\n\n## Context\n\nx\n\n"
        "## Breakdown / sub-specs\n\n- [x] all done\n",
        encoding="utf-8",
    )
    (Path(cfg["specs_dir"]) / "0095-child.md").write_text(
        "---\nid: SPEC-0095\ntitle: Child\nstatus: done\nowner: t\n"
        "created: 2026-07-22\nparent: SPEC-0096\n---\n\n## Context\n\nx\n",
        encoding="utf-8",
    )
    idea = _idea(state="PROMOTED", is_done=True,
                 properties={"ID": "IDEA-003", "SPEC": "SPEC-0096"})
    assert next_step_for_idea(cfg, idea) == "nw tasks"


# ---- CLI ------------------------------------------------------------------------------

def test_cli_idea_help_mentions_capture_and_log():
    r = CliRunner().invoke(cli, ["idea", "--help"])
    assert r.exit_code == 0
    assert "log" in r.output
    assert "show" in r.output
    assert "Bare TEXT" in r.output or "capture" in r.output.lower()
    # `explore` is a hidden alias for `log` — must not be listed as its own command.
    # Checked per-line rather than as a substring: `mark-explored` (SPEC-0081) legitimately
    # contains those letters, so a substring test would forbid any command named after
    # exploration at all.
    listed = {ln.split()[0] for ln in r.output.splitlines() if ln.startswith("  ") and ln.split()}
    assert "explore" not in listed, listed
    assert "mark-explored" in listed


def test_cli_next_shows_hint_and_ideas_does_not(tmp_path, monkeypatch):
    """SPEC-0075: the next-step hint lives on `nw next`; `nw ideas` dropped the column to give
    the headline that width back (the hint is still in `nw ideas --json`)."""
    from nightwright.console import console
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "parked thought for next")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    prev_w, prev_h = console._width, console._height
    console._width, console._height = 320, 80
    try:
        r_next = CliRunner().invoke(cli, ["next"])
        assert r_next.exit_code == 0, r_next.output
        assert "nw idea log" in r_next.output
        assert "Next" in r_next.output

        r_ideas = CliRunner().invoke(cli, ["ideas"])
        assert r_ideas.exit_code == 0, r_ideas.output
        assert "nw idea log" not in r_ideas.output      # no next column any more
        assert "parked thought for next" in r_ideas.output

        r_json = CliRunner().invoke(cli, ["ideas", "--json"])
        assert r_json.exit_code == 0, r_json.output
        assert "nw idea log" in r_json.output           # …but the hint stays in JSON
    finally:
        console._width, console._height = prev_w, prev_h
