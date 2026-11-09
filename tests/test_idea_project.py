"""Idea project association (SPEC-0109).

The gap was library-wide, not TUI-specific: `--project` existed only at capture, so an idea
captured without one could never be given a project from anywhere. These cover the mutator, the
CLI verb, and the desk wiring — plus the *consequences* (`--project` filtering and SPEC-0042
research context), which is what made the missing field matter.
"""
import asyncio
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import load_tasks
from nightwright.tui import model as M

pytestmark = pytest.mark.tui

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    conf = tmp_path / "config"
    conf.mkdir()
    (conf / "mappings.yaml").write_text(
        "example:\n  bucket: Example\nmeta:\n  bucket: Meta-Tools\n")
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path),
        "config_dir": str(conf),
        "project_axis": "bucket",
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


def _seed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    assert CliRunner().invoke(cli, ["idea", "an unassociated idea"]).exit_code == 0
    return cfg


def _idea(cfg, idea_id="IDEA-001"):
    return [t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id][0]


def _project(cfg, idea_id="IDEA-001"):
    return _idea(cfg, idea_id).properties.get("PROJECT")


# --- the library gap ---------------------------------------------------------------------

def test_set_idea_project_sets_and_changes(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert _project(cfg) in (None, "")
    W.set_idea_project(cfg, "IDEA-001", "Example")
    assert _project(cfg) == "Example"
    W.set_idea_project(cfg, "IDEA-001", "Meta-Tools")
    assert _project(cfg) == "Meta-Tools"


def test_clearing_removes_the_property_rather_than_blanking_it(tmp_path, monkeypatch):
    """`ideas.org` is hand-edited: a dangling `:PROJECT:` with no value is a wart."""
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_project(cfg, "IDEA-001", "Example")
    W.set_idea_project(cfg, "IDEA-001", "")
    body = Path(cfg["org_ideas_file"]).read_text()
    assert ":PROJECT:" not in body
    assert _project(cfg) in (None, "")


def test_unknown_project_warns_but_still_writes(tmp_path, monkeypatch, capsys):
    """SPEC-0018 is deliberately permissive; a chooser must not turn it into a gate."""
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_project(cfg, "IDEA-001", "NotAKnownProject")
    assert _project(cfg) == "NotAKnownProject"
    assert "NotAKnownProject" in capsys.readouterr().err


def test_set_idea_project_rejects_a_non_idea(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert CliRunner().invoke(cli, ["add", "a plain task"]).exit_code == 0
    task = [t for t in load_tasks(cfg) if not t.is_idea][0]
    with pytest.raises(ValueError, match="not an idea"):
        W.set_idea_project(cfg, task.properties.get("ID") or task.heading, "Example")


def test_setting_a_project_stamps_updated(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_project(cfg, "IDEA-001", "Example")
    assert (_idea(cfg).properties.get("UPDATED") or "").strip()


def test_other_properties_survive_a_project_change(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_kind(cfg, "IDEA-001", "bug")
    W.set_idea_project(cfg, "IDEA-001", "Example")
    W.set_idea_project(cfg, "IDEA-001", "")
    idea = _idea(cfg)
    assert idea.properties.get("KIND") == "bug"
    assert idea.properties.get("ID") == "IDEA-001"
    assert idea.heading == "an unassociated idea"


# --- CLI verb ------------------------------------------------------------------------------

def test_cli_project_set_and_clear(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    assert run(cli, ["idea", "project", "IDEA-001", "--set", "Example"]).exit_code == 0
    assert _project(cfg) == "Example"
    assert run(cli, ["idea", "project", "IDEA-001", "--set", ""]).exit_code == 0
    assert _project(cfg) in (None, "")
    assert run(cli, ["idea", "project", "IDEA-001"]).exit_code != 0     # --set required


def test_cli_project_on_a_non_idea_is_a_clean_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["idea", "project", "IDEA-999", "--set", "Example"])
    assert r.exit_code != 0
    assert "Traceback" not in r.output


# --- the consequences that made this matter -------------------------------------------------

def test_project_makes_the_idea_visible_to_project_filtering(tmp_path, monkeypatch):
    """SPEC-0089/0096: a project-less idea is invisible to project-scoped triage."""
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    r = run(cli, ["ideas", "--project", "Example", "--json"])
    assert "IDEA-001" not in r.output

    W.set_idea_project(cfg, "IDEA-001", "Example")
    r = run(cli, ["ideas", "--project", "Example", "--json"])
    assert r.exit_code == 0
    assert "IDEA-001" in r.output


def test_project_resolves_research_context(tmp_path, monkeypatch):
    """SPEC-0042 keys research context off `:PROJECT:` — the `(none)` line users kept seeing."""
    cfg = _seed(tmp_path, monkeypatch)
    before = json.loads(CliRunner().invoke(
        cli, ["idea", "show", "IDEA-001", "--json"]).output)["research"]
    assert not before.get("root")

    W.set_idea_project(cfg, "IDEA-001", "Example")
    after = json.loads(CliRunner().invoke(
        cli, ["idea", "show", "IDEA-001", "--json"]).output)["research"]
    assert after != before


# --- desk wiring -----------------------------------------------------------------------------

def test_capture_passes_the_project_to_add_idea_not_a_second_write(tmp_path, monkeypatch):
    """The project reaches `add_idea` as a kwarg, so the idea is *written* associated — there is
    never a moment where it exists without its project."""
    import nightwright.org_write as OW

    cfg = _seed(tmp_path, monkeypatch)
    seen = {}
    real = OW.add_idea

    def spy(cfg_, text, **kw):
        seen.update(kw)
        return real(cfg_, text, **kw)

    monkeypatch.setattr(OW, "add_idea", spy)
    new_id = M.capture_idea(cfg, "born with a project", project="Example")
    assert seen.get("project") == "Example"
    assert _project(cfg, new_id) == "Example"
    assert _idea(cfg, new_id).heading == "born with a project"


def test_capture_without_a_project_still_works(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    for value in (None, "", "   "):
        new_id = M.capture_idea(cfg, f"no project {value!r}", project=value)
        assert _project(cfg, new_id) in (None, "")


def test_apply_mutation_sets_and_clears_project(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    M.apply_mutation(cfg, "project", "IDEA-001", value="Meta-Tools")
    assert _project(cfg) == "Meta-Tools"
    M.apply_mutation(cfg, "project", "IDEA-001", value="")
    assert _project(cfg) in (None, "")


def test_known_project_choices_comes_from_the_registry(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert M.known_project_choices(cfg) == ["Example", "Meta-Tools"]


def test_known_project_choices_includes_outbox_and_meta_tools_without_mappings(tmp_path):
    """SPEC-0139: desk choices follow projects_payload, not mappings alone."""
    cfg = _cfg(tmp_path)
    (Path(cfg["config_dir"]) / "mappings.yaml").write_text("")
    cfg["outbox_targets"] = {
        "DemoBrew": {"repo_path": str(tmp_path / "demobrew")},
    }
    assert M.known_project_choices(cfg) == ["DemoBrew", "Meta-Tools"]


def test_known_project_choices_degrades_instead_of_crashing(tmp_path):
    """A config with no mappings must give an empty chooser, never break capture."""
    cfg = _cfg(tmp_path)
    del cfg["config_dir"]
    assert M.known_project_choices(cfg) == []


def test_desk_owns_no_project_write_rule():
    app_src = (ROOT / "src" / "nightwright" / "tui" / "app.py").read_text()
    assert "set_property" not in app_src
    assert "set_idea_project" not in app_src      # goes through apply_mutation


def test_capture_flow_offers_a_project_and_can_decline(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            for ch in "with project":
                await pilot.press("space" if ch == " " else ch)
            await pilot.press("enter")             # -> project chooser
            await pilot.pause()
            await pilot.press("j")                 # (none) -> first real project (sorted)
            await pilot.press("enter")
            await pilot.pause()
            assert _project(cfg, "IDEA-002") == "Example"

            await pilot.press("c")                 # second capture, decline the project
            await pilot.pause()
            for ch in "no project":
                await pilot.press("space" if ch == " " else ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")             # (none) is pre-selected
            await pilot.pause()
            assert _project(cfg, "IDEA-003") in (None, "")

    asyncio.run(drive())


def test_metadata_chooser_sets_and_clears_project(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await pilot.press("m")  # state / kind / priority / project / …
            await pilot.pause()
            for _ in range(3):                     # state → kind → priority → project
                await pilot.press("j")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("j")                 # (none) → Example (first real project, sorted)
            await pilot.press("enter")
            await pilot.pause()
            assert _project(cfg) == "Example"

            await pilot.press("m")                 # now clear it
            await pilot.pause()
            for _ in range(3):
                await pilot.press("j")             # → project
            await pilot.press("enter")
            await pilot.pause()
            # the chooser opens on the *current* value, so walk back up to (none)
            for _ in range(4):
                await pilot.press("k")
            await pilot.press("enter")
            await pilot.pause()
            assert _project(cfg) in (None, ""), "clearing via the chooser did not clear"

    asyncio.run(drive())


def test_desk_opened_with_a_project_preselects_it(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg, filters={"project": "Example"})
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            for ch in "inherits":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")             # accept whatever is pre-selected
            await pilot.pause()
            ids = [t.properties.get("ID") for t in load_tasks(cfg) if t.is_idea]
            newest = sorted(i for i in ids if i)[-1]
            assert _project(cfg, newest) == "Example"

    asyncio.run(drive())
