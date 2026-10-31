"""Time→task join & digests (SPEC-0008): join_time mapping, the join invariant, untracked
bucket, and the digest rendering/CLI."""
import os
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import report as R
from nightwright.cli import cli
from nightwright.console import console
from nightwright.org import join_time, load_tasks

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "org")


def _cfg():
    return {"org_files": [FIXTURES], "org_todo_keywords": ["TODO", "|", "DONE"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York")}


def test_join_maps_keys_and_untracked():
    tasks = load_tasks(_cfg())
    # DEMO-504/506 are task keys; some-repo:main matches no task
    topic_secs = {"DEMO-504": 3600.0, "DEMO-506": 1800.0, "some-repo:main": 900.0}
    hours, key_to_tasks, untracked = join_time(tasks, topic_secs)
    assert hours["DEMO-504"] == 3600.0
    assert hours["DEMO-506"] == 1800.0
    assert untracked == {"some-repo:main": 900.0}
    assert {t.topic_key for t in key_to_tasks["DEMO-504"]} == {"DEMO-504"}


def test_join_invariant_key_hours_equal_tracked():
    tasks = load_tasks(_cfg())
    topic_secs = {"DEMO-504": 7200.0}
    hours, _, _ = join_time(tasks, topic_secs)
    # a base topic that IS a task key lands verbatim -> join hours == tracked hours
    assert hours["DEMO-504"] == topic_secs["DEMO-504"]


def test_join_embedded_key_in_repo_branch_topic():
    tasks = load_tasks(_cfg())
    # a repo:branch topic whose branch carries the JIRA key still joins
    hours, _, untracked = join_time(tasks, {"repo:DEMO-506-fix": 600.0})
    assert hours["DEMO-506"] == 600.0
    assert not untracked


def test_key_mapping_to_multiple_tasks():
    # two tasks sharing one key -> key_to_tasks has both; hours summed once per topic
    from nightwright.org import Task
    t1 = Task(id="a", heading="DEMO-1 one", state="TODO", is_done=False, tags=frozenset(),
              priority=None, topic_key="DEMO-1", project="p")
    t2 = Task(id="b", heading="DEMO-1 two", state="TODO", is_done=False, tags=frozenset(),
              priority=None, topic_key="DEMO-1", project="q")
    hours, key_to_tasks, _ = join_time([t1, t2], {"DEMO-1": 1200.0})
    assert hours["DEMO-1"] == 1200.0
    assert len(key_to_tasks["DEMO-1"]) == 2


def _render_digest(monkeypatch, topic_secs, **kw):
    monkeypatch.setattr(R, "_scope_topic_secs", lambda cfg, days: topic_secs)
    prev = console._width
    console._width = 240
    try:
        with console.capture() as cap:
            R.digest(_cfg(), **kw)
    finally:
        console._width = prev
    return cap.get()


def test_digest_by_task_and_untracked(monkeypatch):
    out = _render_digest(monkeypatch, {"DEMO-504": 3600.0, "mystery-repo": 900.0},
                         day="2026-07-15")
    assert "DEMO-504" in out
    assert "Untracked in org" in out and "mystery-repo" in out


def test_digest_by_project(monkeypatch):
    out = _render_digest(monkeypatch, {"DEMO-504": 3600.0}, week="2026-07-15", by="project")
    assert "estimations" in out       # DEMO-504's project (file stem)


def test_cli_digest_runs(monkeypatch):
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", _cfg)
    monkeypatch.setattr(R, "_scope_topic_secs", lambda cfg, days: {"DEMO-504": 3600.0})
    r = CliRunner().invoke(cli, ["digest", "-w", "2026-07-15"])
    assert r.exit_code == 0, r.output
    assert "DEMO-504" in r.output


# ---- SPEC-0063: labeled supplemental clock panel in nw digest --------------------------

def test_digest_shows_supplemental_clocked_hours(monkeypatch, tmp_path):
    from zoneinfo import ZoneInfo as _ZI
    from nightwright import org_write as W

    org = tmp_path / "org"
    org.mkdir()
    cfg = {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
           "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
           "timezone": "America/New_York", "_tz": _ZI("America/New_York"),
           "data_dir": str(tmp_path / "data"), "org_capture_file": str(org / "inbox.org"),
           "org_ideas_file": str(org / "ideas.org"),
           # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
           "specs_dir": str(tmp_path / "specs")}
    W.add_idea(cfg, "clocked idea")
    import datetime as _dt
    W.add_clock_entry(cfg, "IDEA-001",
                      _dt.datetime(2026, 7, 25, 9, 0, tzinfo=cfg["_tz"]),
                      _dt.datetime(2026, 7, 25, 10, 30, tzinfo=cfg["_tz"]))

    monkeypatch.setattr(R, "_scope_topic_secs", lambda c, days: {})
    prev = console._width
    console._width = 240
    try:
        with console.capture() as cap:
            R.digest(cfg, day="2026-07-25")
        out = cap.get()
    finally:
        console._width = prev
    assert "Clocked (supplemental)" in out
    assert "IDEA-001" in out
    assert "1.50h" in out


def test_digest_omits_clock_panel_when_no_clock_data(monkeypatch):
    out = _render_digest(monkeypatch, {}, day="2026-07-15")
    assert "Clocked (supplemental)" not in out
