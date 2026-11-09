"""Idea epic association (SPEC-0307): `set_idea_epic` and `nw idea epic`, modelled on
`tests/test_idea_project.py` (SPEC-0109). Covers the mutator, the CLI verb, and the readers that
make `:EPIC:` matter (`nw ideas --epic`, `nw spec new --from-idea`)."""
import json
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import completion as C
from nightwright import org_write as W
from nightwright.cli import cli, idea_epic_cmd
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
EPIC = "SPEC-0298"


def _cfg(tmp_path, epics=()):
    org = tmp_path / "org"
    org.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        shutil.copy(ROOT / "docs" / "specs" / name, specs / name)
    for eid in epics:
        n = eid.split("-")[1]
        (specs / f"{n}-an-epic.md").write_text(
            f"---\nid: {eid}\ntitle: An epic\nstatus: accepted\nkind: epic\n---\n\n## Context\n")
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path),
        "config_dir": str(tmp_path),
        "project_axis": "bucket",
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
    }


def _seed(tmp_path, monkeypatch, epics=(EPIC,)):
    cfg = _cfg(tmp_path, epics)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    assert CliRunner().invoke(cli, ["idea", "an unlinked idea"]).exit_code == 0
    return cfg


def _idea(cfg, idea_id="IDEA-001"):
    return [t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id][0]


def _epic(cfg, idea_id="IDEA-001"):
    return _idea(cfg, idea_id).properties.get("EPIC")


def _body(cfg):
    return Path(cfg["org_ideas_file"]).read_text()


# --- the mutator ---------------------------------------------------------------------------

def test_set_idea_epic_sets_and_changes(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch, epics=(EPIC, "SPEC-0116"))
    assert _epic(cfg) in (None, "")
    path = W.set_idea_epic(cfg, "IDEA-001", EPIC)
    assert Path(path) == Path(cfg["org_ideas_file"])
    assert _epic(cfg) == EPIC
    W.set_idea_epic(cfg, "IDEA-001", "SPEC-0116")
    assert _epic(cfg) == "SPEC-0116"
    assert _body(cfg).count(":EPIC:") == 1


def test_value_is_stripped(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_epic(cfg, "IDEA-001", f"  {EPIC}  ")
    assert _epic(cfg) == EPIC


def test_clearing_removes_the_property_rather_than_blanking_it(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    for empty in ("", None, "   "):
        W.set_idea_epic(cfg, "IDEA-001", EPIC)
        W.set_idea_epic(cfg, "IDEA-001", empty)
        assert ":EPIC:" not in _body(cfg)
        assert _epic(cfg) in (None, "")


def test_clearing_when_absent_does_not_raise(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_epic(cfg, "IDEA-001", "")
    assert ":EPIC:" not in _body(cfg)


def test_unknown_epic_warns_but_still_writes(tmp_path, monkeypatch, capsys):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_epic(cfg, "IDEA-001", "SPEC-9999")
    assert _epic(cfg) == "SPEC-9999"
    err = capsys.readouterr().err
    assert "SPEC-9999" in err and EPIC in err          # warns, naming the known epics

    W.set_idea_epic(cfg, "IDEA-001", EPIC)
    assert capsys.readouterr().err == ""               # a known epic is silent


def test_set_idea_epic_rejects_a_non_idea(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    assert CliRunner().invoke(cli, ["add", "a plain task"]).exit_code == 0
    task = [t for t in load_tasks(cfg) if not t.is_idea][0]
    with pytest.raises(ValueError, match="not an idea"):
        W.set_idea_epic(cfg, task.properties.get("ID") or task.heading, EPIC)


OLD_STAMP = "[2000-01-01 Sat 00:00]"


def _age_updated(cfg):
    """Rewrite the idea's :UPDATED: to an old stamp, so a later bump is observable."""
    path = Path(cfg["org_ideas_file"])
    lines = path.read_text().splitlines(keepends=True)
    assert any(l.strip().startswith(":UPDATED:") for l in lines)
    path.write_text("".join(
        f":UPDATED: {OLD_STAMP}\n" if l.strip().startswith(":UPDATED:") else l for l in lines))
    assert _idea(cfg).properties["UPDATED"] == OLD_STAMP


def test_setting_an_epic_stamps_updated(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    _age_updated(cfg)
    W.set_idea_epic(cfg, "IDEA-001", EPIC)
    assert _idea(cfg).properties["UPDATED"] != OLD_STAMP


def test_clearing_an_epic_stamps_updated(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_epic(cfg, "IDEA-001", EPIC)
    _age_updated(cfg)
    W.set_idea_epic(cfg, "IDEA-001", "")
    assert _idea(cfg).properties["UPDATED"] != OLD_STAMP


def test_other_properties_survive_an_epic_change(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    W.set_idea_kind(cfg, "IDEA-001", "bug")
    W.set_idea_project(cfg, "IDEA-001", "Example")
    W.set_idea_epic(cfg, "IDEA-001", EPIC)
    W.set_idea_epic(cfg, "IDEA-001", "")
    idea = _idea(cfg)
    assert idea.properties.get("KIND") == "bug"
    assert idea.properties.get("PROJECT") == "Example"
    assert idea.properties.get("ID") == "IDEA-001"
    assert idea.heading == "an unlinked idea"


# --- CLI verb ------------------------------------------------------------------------------

def test_cli_epic_set_and_clear(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    r = run(cli, ["idea", "epic", "IDEA-001", "--set", EPIC])
    assert r.exit_code == 0
    assert "epic" in r.output and "IDEA-001" in r.output and EPIC in r.output
    assert _epic(cfg) == EPIC
    r = run(cli, ["idea", "epic", "IDEA-001", "--set", ""])
    assert r.exit_code == 0
    assert "(none)" in r.output
    assert ":EPIC:" not in _body(cfg)


def test_cli_epic_requires_set(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["idea", "epic", "IDEA-001"])
    assert r.exit_code != 0
    assert "--set" in r.output


def test_cli_epic_on_a_non_idea_is_a_clean_error(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    r = CliRunner().invoke(cli, ["idea", "epic", "IDEA-999", "--set", EPIC])
    assert r.exit_code != 0
    assert "Traceback" not in r.output

    assert CliRunner().invoke(cli, ["add", "a plain task"]).exit_code == 0
    r = CliRunner().invoke(cli, ["idea", "epic", "a plain task", "--set", EPIC])
    assert r.exit_code != 0
    assert "not an idea" in r.output
    assert "Traceback" not in r.output
    assert r.exception is None or isinstance(r.exception, SystemExit)


def test_epic_option_has_completion():
    opt = next(p for p in idea_epic_cmd.params if p.name == "epic")
    assert opt.opts == ["--set"]
    assert opt._custom_shell_complete is C.complete_epic


# --- the consequences that made this matter --------------------------------------------------

def test_epic_makes_the_idea_visible_to_epic_filtering(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke

    def listed():
        r = run(cli, ["ideas", "--epic", EPIC, "--json"])
        assert r.exit_code == 0
        return [i["id"] for i in json.loads(r.output)["ideas"]]

    assert listed() == []
    W.set_idea_epic(cfg, "IDEA-001", EPIC)
    assert listed() == ["IDEA-001"]
    W.set_idea_epic(cfg, "IDEA-001", "")
    assert listed() == []


def test_spec_new_from_idea_uses_the_set_epic_as_parent(tmp_path, monkeypatch):
    cfg = _seed(tmp_path, monkeypatch)
    run = CliRunner().invoke
    assert run(cli, ["idea", "epic", "IDEA-001", "--set", EPIC]).exit_code == 0
    r = run(cli, ["spec", "new", "--from-idea", "IDEA-001", "--internal"])
    assert r.exit_code == 0, r.output
    new = [p for p in Path(cfg["specs_dir"]).glob("[0-9]*.md") if "an-epic" not in p.name]
    assert len(new) == 1
    assert f"parent: {EPIC}" in new[0].read_text()

