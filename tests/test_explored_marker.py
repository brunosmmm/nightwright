"""SPEC-0081: `:EXPLORED:` / `:EXPLORED_AT:` — recording that an idea was actually explored.

The bug: `/nw-seed` writes explore-quality content and captures as INCUBATE, so a seeded idea was
indistinguishable on disk from an explored one. The fix only works if the marker is written
*exclusively* by the explicit command — seeding performs the same summary/questions/log writes, so
any implicit bump would immediately re-create the ambiguity. Hence the per-mutator matrix below.
"""
import datetime as dt
import os
from zoneinfo import ZoneInfo

import pytest

from nightwright import explore as E
from nightwright import org_write as W
from nightwright import report as R
from nightwright.console import console

pytestmark = pytest.mark.fast

TZ = ZoneInfo("America/New_York")


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                                  "DROPPED", "RESEARCHED"],
            "timezone": "America/New_York", "_tz": TZ,
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ai" / "ideas.org"),
            "org_ideas_archive_file": str(org / "ai" / "ideas-archive.org"),
            "specs_dir": str(tmp_path / "specs"),
            "config_dir": str(tmp_path / "cfg"), "project_axis": "bucket"}


def _task(cfg, sel="IDEA-001"):
    return W.resolve_selector(cfg, sel)


def _marker(cfg, sel="IDEA-001"):
    p = _task(cfg, sel).properties
    return (p.get("EXPLORED") or "").strip(), (p.get("EXPLORED_AT") or "").strip()


def _seed(cfg, text="an idea"):
    W.add_idea(cfg, text)
    return "IDEA-001"


# ---- the marker itself ------------------------------------------------------------

def test_first_pass_sets_count_and_stamp(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    assert _marker(cfg) == ("", "")

    assert E.mark_explored(cfg, idea) == 1
    count, at = _marker(cfg)
    assert count == "1"
    assert at == W._inactive_stamp(cfg, with_time=True)


def test_repeat_passes_increment(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    assert [E.mark_explored(cfg, idea) for _ in range(3)] == [1, 2, 3]
    assert _marker(cfg)[0] == "3"


def test_garbage_count_coerces_to_zero(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    W.set_property(cfg, _task(cfg), "EXPLORED", "banana")
    assert E.mark_explored(cfg, idea) == 1


def test_negative_count_coerces_to_zero(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    W.set_property(cfg, _task(cfg), "EXPLORED", "-4")
    assert E.mark_explored(cfg, idea) == 1


def test_explored_count_helper(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    assert E.explored_count(_task(cfg)) == 0
    E.mark_explored(cfg, "IDEA-001")
    assert E.explored_count(_task(cfg)) == 1


def test_non_idea_is_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "an ordinary task")
    with pytest.raises(ValueError):
        E.mark_explored(cfg, "an ordinary task")


# ---- nothing implicit may bump it -------------------------------------------------

NON_BUMPERS = [
    ("summary", lambda cfg: E.set_summary(cfg, "IDEA-001", "a summary")),
    ("questions --set", lambda cfg: E.set_questions(cfg, "IDEA-001", "a question")),
    ("questions --add", lambda cfg: E.add_question(cfg, "IDEA-001", "another")),
    ("questions --resolve", lambda cfg: (E.set_questions(cfg, "IDEA-001", "q"),
                                         E.resolve_questions(cfg, "IDEA-001", [1]))),
    ("log", lambda cfg: E.append_log(cfg, "IDEA-001", "a finding")),
    ("retitle", lambda cfg: E.retitle_idea(cfg, "IDEA-001", "a new title")),
    ("kind", lambda cfg: W.set_idea_kind(cfg, "IDEA-001", "bug")),
    ("state", lambda cfg: W.set_state_by_selector(cfg, "IDEA-001", "INCUBATE")),
    ("clock-in/out", lambda cfg: (W.clock_in(cfg, "IDEA-001"), W.clock_out(cfg, "IDEA-001"))),
    ("set_property", lambda cfg: W.set_property(cfg, _task(cfg), "SPEC", "SPEC-0001")),
]


@pytest.mark.parametrize("label,mutate", NON_BUMPERS, ids=[n for n, _ in NON_BUMPERS])
def test_nothing_else_bumps_explored(tmp_path, label, mutate):
    """Seeding performs exactly these writes. If any bumped the marker, a seeded idea would read
    as explored and the fix would defeat itself."""
    cfg = _cfg(tmp_path)
    _seed(cfg)
    before = _marker(cfg)
    mutate(cfg)
    assert _marker(cfg) == before, f"{label} touched the explored marker"


def test_capture_does_not_mark(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    assert _marker(cfg) == ("", "")


def test_seeded_idea_is_distinguishable_from_explored(tmp_path):
    """The actual bug, asserted directly: INCUBATE + populated sections + no marker is 'seeded'."""
    cfg = _cfg(tmp_path)
    idea = _seed(cfg, "seeded with context")
    E.set_summary(cfg, idea, "explore-quality summary written straight from conversation")
    E.set_questions(cfg, idea, "a question raised in chat")
    E.append_log(cfg, idea, "a finding from chat")

    task = _task(cfg)
    assert (task.state or "").upper() == "INCUBATE"       # looks explored…
    assert E.idea_summary_text(cfg, task).strip()         # …has content…
    assert E.explored_count(task) == 0                    # …but is provably unexplored

    E.mark_explored(cfg, idea)
    assert E.explored_count(_task(cfg)) == 1


# ---- interaction with SPEC-0076 stamps -------------------------------------------

def test_explored_bumps_updated_but_not_created(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    task = _task(cfg)
    created_before = (task.properties.get("CREATED") or "").strip()
    W.set_property(cfg, task, "UPDATED", "[2019-01-01 Tue 00:00]")

    E.mark_explored(cfg, idea)
    p = _task(cfg).properties
    assert (p.get("UPDATED") or "").strip() != "[2019-01-01 Tue 00:00]"
    assert (p.get("CREATED") or "").strip() == created_before


# ---- surfacing --------------------------------------------------------------------

def test_show_payload_includes_marker_when_present(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    assert "explored" not in E.idea_show_payload(cfg, _task(cfg))

    E.mark_explored(cfg, idea)
    payload = E.idea_show_payload(cfg, _task(cfg))
    assert payload["explored"] == 1 and isinstance(payload["explored"], int)
    assert payload["explored_at"].startswith("[")


def test_pretty_show_prints_explored_line_only_when_marked(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    assert "explored:" not in E.format_idea_show(cfg, _task(cfg))

    E.mark_explored(cfg, idea)
    out = E.format_idea_show(cfg, _task(cfg))
    assert "explored: 1 pass" in out and "latest [" in out

    E.mark_explored(cfg, idea)
    assert "explored: 2 passes" in E.format_idea_show(cfg, _task(cfg))


def test_idea_row_exposes_explored(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    assert "explored" not in R.idea_row(cfg, _task(cfg))
    E.mark_explored(cfg, idea)
    row = R.idea_row(cfg, _task(cfg))
    assert row["explored"] == 1
    for key in ("id", "state", "heading", "tags", "kind", "next"):
        assert key in row


# ---- explicitly unchanged ---------------------------------------------------------

def test_tables_gain_no_column(tmp_path):
    cfg = _cfg(tmp_path)
    E.mark_explored(cfg, _seed(cfg))
    prev_w, prev_h = console._width, console._height
    console._width, console._height = 200, 80
    try:
        for fn in (R.ideas, R.next_ideas):
            with console.capture() as cap:
                fn(cfg)
            header = next(ln for ln in cap.get().splitlines() if " id " in ln and "idea" in ln)
            assert "explored" not in header
    finally:
        console._width, console._height = prev_w, prev_h


def test_next_step_hint_unchanged_by_the_marker(tmp_path):
    """The promote gate is deliberately NOT changing in this spec."""
    from nightwright.workflow import next_step_for_idea

    cfg = _cfg(tmp_path)
    idea = _seed(cfg)
    E.set_summary(cfg, idea, "a summary")
    before = next_step_for_idea(cfg, _task(cfg))
    E.mark_explored(cfg, idea)
    assert next_step_for_idea(cfg, _task(cfg)) == before


def test_skill_documents_explored():
    """Doc drift is the failure mode this family of bugs keeps hitting (cf. IDEA-098)."""
    import pathlib

    text = pathlib.Path("skills/nw-explore/SKILL.md").read_text(encoding="utf-8")
    assert "nw idea mark-explored" in text


# ---- CLI --------------------------------------------------------------------------

def test_cli_explored(tmp_path, monkeypatch):
    from click.testing import CliRunner

    import nightwright.cli as cli_mod
    from nightwright.cli import cli

    cfg = _cfg(tmp_path)
    _seed(cfg)
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "mark-explored", "IDEA-001"])
    assert r.exit_code == 0, r.output
    assert "pass 1" in r.output
    r2 = CliRunner().invoke(cli, ["idea", "mark-explored", "IDEA-001"])
    assert "pass 2" in r2.output


def test_cli_explored_on_missing_idea_errors(tmp_path, monkeypatch):
    from click.testing import CliRunner

    import nightwright.cli as cli_mod
    from nightwright.cli import cli

    cfg = _cfg(tmp_path)
    _seed(cfg)
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "mark-explored", "IDEA-404"])
    assert r.exit_code != 0
