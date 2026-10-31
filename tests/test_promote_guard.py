"""SPEC-0167: promoting a terminal or archived idea is refused unless --force — a wrong id in a
shared ideas file must not silently rewrite another project's finished idea.

All scaffolding happens in tmp specs/outbox dirs."""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright import specs as S
from nightwright.cli import cli
from nightwright.org import invalidate_tasks_cache, load_tasks

ROOT = Path(__file__).resolve().parent.parent
TERMINAL = ["PROMOTED", "EXPORTED", "SHIPPED", "DROPPED", "RESEARCHED"]


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        (specs / name).write_text((ROOT / "docs" / "specs" / name).read_text())
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                              "SHIPPED", "DROPPED", "RESEARCHED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"),
        "config_dir": str(tmp_path / "config"),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
        "outbox_dir": str(tmp_path / "outbox"),
        "outbox_targets": {"acme": {"repo_path": str(tmp_path / "acme-repo")}},
    }


def _idea(cfg, idea_id="IDEA-001"):
    invalidate_tasks_cache()
    return next(t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id)


def _closed_idea(cfg, state, project=None):
    W.add_idea(cfg, "finished elsewhere", project=project)
    EX.set_summary(cfg, "IDEA-001", "done work")
    EX.set_idea_state(cfg, "IDEA-001", state)
    return _idea(cfg)


def _written(tmp_path):
    specs = sorted(p.name for p in (tmp_path / "specs").glob("[0-9]*.md"))
    outbox = sorted(p.name for p in (tmp_path / "outbox").rglob("*.md")
                    if p.name != "INDEX.md") if (tmp_path / "outbox").exists() else []
    return specs, outbox


def _snapshot(task):
    return task.state, dict(task.properties), task.file


# ---- ideas_archive_path ---------------------------------------------------------------

def test_archive_path_default_and_configured(tmp_path):
    cfg = _cfg(tmp_path)
    assert W.ideas_archive_path(cfg) == str(tmp_path / "org" / "ideas-archive.org")
    cfg["org_ideas_archive_file"] = "~/somewhere/arch.org"
    assert W.ideas_archive_path(cfg) == os.path.expanduser("~/somewhere/arch.org")


# ---- the guard on every entry point ------------------------------------------------------

@pytest.mark.parametrize("state", TERMINAL)
@pytest.mark.parametrize("entry", ["promote", "promote_internal", "scaffold"])
def test_terminal_idea_refused(tmp_path, state, entry):
    cfg = _cfg(tmp_path)
    before = _snapshot(_closed_idea(cfg, state))
    with pytest.raises(ValueError) as exc:
        if entry == "scaffold":
            S.scaffold_from_idea(cfg, "IDEA-001")
        else:
            S.promote_idea(cfg, "IDEA-001", internal=(entry == "promote_internal"))
    msg = str(exc.value)
    assert "IDEA-001" in msg and "finished elsewhere" in msg and state in msg
    assert "--force" in msg
    assert _written(tmp_path) == ([], [])
    assert _snapshot(_idea(cfg)) == before


@pytest.mark.parametrize("state", TERMINAL)
def test_scaffold_outbound_refuses_closed_idea(tmp_path, state):
    cfg = _cfg(tmp_path)
    before = _snapshot(_closed_idea(cfg, state, project="acme"))
    with pytest.raises(ValueError, match="refusing to re-promote"):
        S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001")
    assert _written(tmp_path) == ([], [])
    assert _snapshot(_idea(cfg)) == before


def test_archived_message_names_archive_file(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _closed_idea(cfg, "SHIPPED")
    assert idea.file == W.ideas_archive_path(cfg)          # SPEC-0073 moved it
    with pytest.raises(ValueError, match="archived in .*ideas-archive.org"):
        S.promote_idea(cfg, "IDEA-001", internal=True)


def test_archived_idea_refused_even_if_reopened_in_place(tmp_path):
    """An idea hand-flipped back to an open state inside the archive is still refused."""
    cfg = _cfg(tmp_path)
    archive = Path(_closed_idea(cfg, "SHIPPED").file)
    archive.write_text(archive.read_text().replace("* SHIPPED ", "* INCUBATE ", 1))
    idea = _idea(cfg)
    assert idea.state == "INCUBATE" and not idea.is_done
    with pytest.raises(ValueError, match="archived in"):
        S.promote_idea(cfg, "IDEA-001", internal=True)


@pytest.mark.parametrize("state", ["EXPORTED", "SHIPPED"])
def test_force_bypasses(tmp_path, state):
    cfg = _cfg(tmp_path)
    _closed_idea(cfg, state)
    _path, spec_id, kind = S.promote_idea(cfg, "IDEA-001", internal=True, force=True)
    assert kind == "internal" and spec_id.startswith("SPEC-")
    assert _idea(cfg).properties.get("SPEC") == spec_id


@pytest.mark.parametrize("state", ["IDEA", "INCUBATE"])
def test_open_live_ideas_promote_as_before(tmp_path, state):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "open idea")
    EX.set_summary(cfg, "IDEA-001", "ready")
    if state == "IDEA":
        EX.set_idea_state(cfg, "IDEA-001", "IDEA")
    _path, spec_id, _ = S.promote_idea(cfg, "IDEA-001", internal=True)
    assert _idea(cfg).state == "SPECCED"


# ---- CLI ----------------------------------------------------------------------------

def test_cli_refuses_and_writes_nothing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _closed_idea(cfg, "SHIPPED")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "new", "--from-idea", "IDEA-001", "--internal"])
    assert r.exit_code != 0
    assert "refusing to re-promote" in r.output
    assert _written(tmp_path) == ([], [])


def test_cli_success_line_names_heading(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "the right [bracketed] idea")
    EX.set_summary(cfg, "IDEA-001", "ready")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "new", "--from-idea", "IDEA-001", "--internal"])
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    assert "IDEA-001" in flat and "the right [bracketed] idea" in flat
