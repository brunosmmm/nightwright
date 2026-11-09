"""`nw agenda` date views (SPEC-0007): SCHEDULED/DEADLINE bucketing over a scope + overdue."""
import os

from click.testing import CliRunner

from nightwright import report as R
from nightwright.cli import cli
from nightwright.console import console
from nightwright.dates import week_days
import datetime as dt
from zoneinfo import ZoneInfo
from nightwright.org import load_tasks
import pytest

pytestmark = pytest.mark.fast

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "org")


def _cfg():
    return {"org_files": [FIXTURES], "org_todo_keywords": ["TODO", "|", "DONE"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York")}


def _planned():
    return [t for t in load_tasks(_cfg()) if t.file.endswith("planned.org")]


WEEK = [d.isoformat() for d in week_days(dt.date(2026, 7, 15))]   # Mon 07-13 .. Sun 07-19


def test_buckets_scheduled_and_deadline_in_scope():
    by_day, overdue = R._agenda_buckets(_planned(), WEEK)
    kinds = {(k, t.heading) for day in by_day.values() for k, t in day}
    assert ("SCHEDULED", "Scheduled inside the week") in kinds
    assert ("DEADLINE", "Deadline inside the week") in kinds
    assert by_day["2026-07-15"][0][0] == "SCHEDULED"
    assert by_day["2026-07-16"][0][0] == "DEADLINE"


def test_overdue_excludes_done_and_undated():
    _, overdue = R._agenda_buckets(_planned(), WEEK)
    heads = {t.heading for t in overdue}
    assert "Overdue deadline before the week" in heads
    assert "Past deadline but done (not overdue)" not in heads   # done -> not overdue
    assert "No dates at all" not in heads                        # undated -> absent


def test_undated_task_never_appears():
    by_day, overdue = R._agenda_buckets(_planned(), WEEK)
    all_heads = {t.heading for day in by_day.values() for _, t in day} | {t.heading for t in overdue}
    assert "No dates at all" not in all_heads


def test_render_and_empty_scope():
    prev = console._width
    console._width = 240
    try:
        with console.capture() as cap:
            R.agenda(_cfg(), week="2026-07-15")
        out = cap.get()
        # a scope with no scheduling and no overdue -> friendly line
        with console.capture() as cap2:
            R.agenda(_cfg(), day="2020-01-01")   # before all deadlines: no overdue, nothing scheduled
        empty = cap2.get()
    finally:
        console._width = prev
    assert "Scheduled inside the week" in out and "Overdue" in out
    assert "nothing scheduled in range" in empty


def test_cli_agenda_runs(monkeypatch):
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", _cfg)
    r = CliRunner().invoke(cli, ["agenda", "-w", "2026-07-15"])
    assert r.exit_code == 0, r.output


# ---- SPEC-0088: default excludes ideas; --ideas opt-in --------------------------------

def _mixed_cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    ideas = org / "ideas.org"
    inbox = org / "inbox.org"
    ideas.write_text(
        "#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED DROPPED RESEARCHED\n"
        "* IDEA Dated idea in week\n"
        "  SCHEDULED: <2026-07-15 Wed>\n"
        "* IDEA Overdue idea\n"
        "  DEADLINE: <2026-07-01 Wed>\n"
        "* PROMOTED Done dated idea still in week\n"
        "  SCHEDULED: <2026-07-16 Thu>\n"
        "  CLOSED: [2026-07-16 Thu]\n"
    )
    inbox.write_text(
        "#+TODO: TODO | DONE\n"
        "* TODO Task in week\n"
        "  SCHEDULED: <2026-07-15 Wed>\n"
        "* TODO Overdue task\n"
        "  DEADLINE: <2026-07-01 Wed>\n"
    )
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED", "RESEARCHED",
        ],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path / "data"),
        "org_capture_file": str(inbox),
        "org_ideas_file": str(ideas),
        "specs_dir": str(tmp_path / "specs"),
    }


def test_agenda_default_excludes_ideas(tmp_path):
    cfg = _mixed_cfg(tmp_path)
    prev = console._width
    console._width = 240
    try:
        with console.capture() as cap:
            R.agenda(cfg, week="2026-07-15")
        out = cap.get()
    finally:
        console._width = prev
    assert "Task in week" in out
    assert "Overdue task" in out
    assert "Dated idea in week" not in out
    assert "Overdue idea" not in out
    assert "Done dated idea" not in out


def test_agenda_ideas_flag_includes_union(tmp_path):
    cfg = _mixed_cfg(tmp_path)
    prev = console._width
    console._width = 240
    try:
        with console.capture() as cap:
            R.agenda(cfg, week="2026-07-15", include_ideas=True)
        out = cap.get()
    finally:
        console._width = prev
    assert "Task in week" in out
    assert "Dated idea in week" in out
    assert "Overdue idea" in out
    # PROMOTED is done → not overdue; still in-scope SCHEDULED day → shows like tasks
    assert "Done dated idea" in out


def test_cli_agenda_ideas_flag(tmp_path, monkeypatch):
    cfg = _mixed_cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["agenda", "-w", "2026-07-15", "--ideas"])
    assert r.exit_code == 0, r.output
    assert "Dated idea in week" in r.output
    r2 = CliRunner().invoke(cli, ["agenda", "-w", "2026-07-15"])
    assert r2.exit_code == 0, r2.output
    assert "Dated idea in week" not in r2.output
