"""Org-side clock-in/clock-out primitive (SPEC-0061): clock_in/clock_out writers, Task.clock
reader, no-crash on an open (still-clocked-in) entry."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import load_tasks


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
        "specs_dir": str(tmp_path / "specs"),
    }


def _idea(cfg):
    return [t for t in load_tasks(cfg) if t.is_idea][0]


def test_clock_in_creates_logbook_with_open_clock(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "clock me")
    W.clock_in(cfg, "IDEA-001")
    raw = open(cfg["org_ideas_file"]).read()
    assert ":LOGBOOK:" in raw
    assert ":END:" in raw
    import re
    assert re.search(r"CLOCK: \[\d{4}-\d{2}-\d{2} \w{3} \d{2}:\d{2}\]\n", raw)
    assert "--" not in raw.split("CLOCK:")[1].split("\n")[0]  # open: no end/duration yet


def test_clock_in_while_already_clocked_in_errors_no_write(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "double clock")
    W.clock_in(cfg, "IDEA-001")
    before = open(cfg["org_ideas_file"]).read()
    with pytest.raises(ValueError, match="already clocked in"):
        W.clock_in(cfg, "IDEA-001")
    assert open(cfg["org_ideas_file"]).read() == before


def test_clock_out_closes_line_with_correct_duration(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "timed session")
    start = dt.datetime(2026, 7, 25, 10, 0, tzinfo=cfg["_tz"])
    end = dt.datetime(2026, 7, 25, 11, 30, tzinfo=cfg["_tz"])
    W.clock_in(cfg, "IDEA-001", when=start)
    W.clock_out(cfg, "IDEA-001", when=end)
    raw = open(cfg["org_ideas_file"]).read()
    assert "CLOCK: [2026-07-25 Sat 10:00]--[2026-07-25 Sat 11:30] => 1:30" in raw


def test_clock_out_with_nothing_open_errors_no_write(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "never clocked in")
    before = open(cfg["org_ideas_file"]).read()
    with pytest.raises(ValueError, match="not clocked in"):
        W.clock_out(cfg, "IDEA-001")
    assert open(cfg["org_ideas_file"]).read() == before


def test_task_clock_round_trips_open_and_closed(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "round trip")
    start1 = dt.datetime(2026, 7, 25, 9, 0, tzinfo=cfg["_tz"])
    end1 = dt.datetime(2026, 7, 25, 9, 45, tzinfo=cfg["_tz"])
    W.clock_in(cfg, "IDEA-001", when=start1)
    W.clock_out(cfg, "IDEA-001", when=end1)
    start2 = dt.datetime(2026, 7, 25, 14, 0, tzinfo=cfg["_tz"])
    W.clock_in(cfg, "IDEA-001", when=start2)

    t = _idea(cfg)
    assert len(t.clock) == 2
    (s1, e1), (s2, e2) = t.clock
    assert s1 == dt.datetime(2026, 7, 25, 9, 0) and e1 == dt.datetime(2026, 7, 25, 9, 45)
    assert s2 == dt.datetime(2026, 7, 25, 14, 0) and e2 is None    # still open, no crash


def test_multiple_clock_cycles_accumulate_in_same_drawer(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "many cycles")
    for h in (9, 11, 15):
        start = dt.datetime(2026, 7, 25, h, 0, tzinfo=cfg["_tz"])
        end = dt.datetime(2026, 7, 25, h, 30, tzinfo=cfg["_tz"])
        W.clock_in(cfg, "IDEA-001", when=start)
        W.clock_out(cfg, "IDEA-001", when=end)
    raw = open(cfg["org_ideas_file"]).read()
    assert raw.count("CLOCK:") == 3
    assert raw.count(":LOGBOOK:") == 1
    assert raw.count(":END:") == 2                          # one for LOGBOOK, one for PROPERTIES
    t = _idea(cfg)
    assert len(t.clock) == 3
    assert all(e is not None for _, e in t.clock)


def test_cli_clock_in_and_out(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    W.add_idea(cfg, "cli clock")
    r_in = CliRunner().invoke(cli, ["idea", "clock-in", "IDEA-001"])
    assert r_in.exit_code == 0, r_in.output
    assert "clocked in" in r_in.output
    r_out = CliRunner().invoke(cli, ["idea", "clock-out", "IDEA-001"])
    assert r_out.exit_code == 0, r_out.output
    assert "clocked out" in r_out.output
    t = _idea(cfg)
    assert len(t.clock) == 1
    assert t.clock[0][1] is not None


def test_default_task_clock_is_empty_tuple(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "no clock at all")
    t = _idea(cfg)
    assert t.clock == ()


# --- Markdown Clock Log parser + add_clock_entry (SPEC-0063) ---------------------------

def test_parse_clock_log_pairs_complete_entries():
    from nightwright.explore import parse_clock_log
    text = (
        "CLOCK-IN: [2026-07-25T09:00:00]\n"
        "CLOCK-OUT: [2026-07-25T09:30:00]\n"
        "CLOCK-IN: [2026-07-25T14:00:00]\n"
        "CLOCK-OUT: [2026-07-25T15:15:00]\n"
    )
    pairs = parse_clock_log(text)
    assert len(pairs) == 2
    assert pairs[0][0] == dt.datetime(2026, 7, 25, 9, 0)
    assert pairs[0][1] == dt.datetime(2026, 7, 25, 9, 30)
    assert pairs[1][1] == dt.datetime(2026, 7, 25, 15, 15)


def test_parse_clock_log_skips_unmatched_trailing_clock_in():
    from nightwright.explore import parse_clock_log
    text = (
        "CLOCK-IN: [2026-07-25T09:00:00]\n"
        "CLOCK-OUT: [2026-07-25T09:30:00]\n"
        "CLOCK-IN: [2026-07-25T16:00:00]\n"           # session still open, no CLOCK-OUT
    )
    pairs = parse_clock_log(text)
    assert len(pairs) == 1


def test_parse_clock_log_accepts_unbracketed_timestamps():
    """Bug found against a real portable spec (DEMO-0001): agents in practice also
    write plain, unbracketed timestamps — the parser silently returned zero pairs for these
    before this fix, though the guidance's own example shows brackets."""
    from nightwright.explore import parse_clock_log
    text = (
        "CLOCK-IN: 2026-07-26 12:01\n"
        "CLOCK-OUT: 2026-07-26 12:07\n"
        "CLOCK-IN: 2026-07-26 12:07\n"
        "CLOCK-OUT: 2026-07-26 12:18\n"
    )
    pairs = parse_clock_log(text)
    assert len(pairs) == 2
    assert pairs[0][0] == dt.datetime(2026, 7, 26, 12, 1)
    assert pairs[0][1] == dt.datetime(2026, 7, 26, 12, 7)
    assert pairs[1][1] == dt.datetime(2026, 7, 26, 12, 18)


def test_parse_clock_log_mixed_bracketed_and_unbracketed():
    from nightwright.explore import parse_clock_log
    text = (
        "CLOCK-IN: [2026-07-25T09:00:00]\n"
        "CLOCK-OUT: 2026-07-25 09:30:00\n"
    )
    pairs = parse_clock_log(text)
    assert len(pairs) == 1
    assert pairs[0] == (dt.datetime(2026, 7, 25, 9, 0), dt.datetime(2026, 7, 25, 9, 30))


def test_parse_clock_log_accepts_org_weekday_timestamps():
    """Bug found against a real portable spec (EXAMPLE-0026/IDEA-212): agents following
    /nw-implement-spec's `CLOCK-IN: [timestamp]` guidance wrote org-mode-style timestamps
    with a weekday abbreviation, which `datetime.fromisoformat` rejects outright — the
    parser raised, and `nw spec sweep` silently swallowed it as `+0 clock` instead of
    surfacing the failure."""
    from nightwright.explore import parse_clock_log
    text = (
        "CLOCK-IN: [2026-08-05 Wed 19:30]\n"
        "CLOCK-OUT: [2026-08-05 Wed 19:33]\n"
    )
    pairs = parse_clock_log(text)
    assert len(pairs) == 1
    assert pairs[0] == (dt.datetime(2026, 8, 5, 19, 30), dt.datetime(2026, 8, 5, 19, 33))


def test_parse_clock_log_skips_unfilled_placeholder():
    """Bug found against real portable specs (e.g. DEMO-0001): the guidance's own
    literal example `CLOCK-IN: [timestamp]` / `CLOCK-OUT: [timestamp]` was sometimes left
    verbatim, unfilled, in the Clock Log section — that's an unused template, not real clock
    data, and must not raise or be counted as a pair."""
    from nightwright.explore import parse_clock_log
    text = (
        "CLOCK-IN: [timestamp]\n"
        "CLOCK-OUT: [timestamp]\n"
    )
    assert parse_clock_log(text) == []


def test_add_clock_entry_appends_closed_line(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "reconciled")
    start = dt.datetime(2026, 7, 25, 9, 0, tzinfo=cfg["_tz"])
    end = dt.datetime(2026, 7, 25, 9, 45, tzinfo=cfg["_tz"])
    path, added = W.add_clock_entry(cfg, "IDEA-001", start, end)
    assert added is True
    raw = open(cfg["org_ideas_file"]).read()
    assert "CLOCK: [2026-07-25 Sat 09:00]--[2026-07-25 Sat 09:45] => 0:45" in raw
    t = _idea(cfg)
    assert t.clock == ((dt.datetime(2026, 7, 25, 9, 0), dt.datetime(2026, 7, 25, 9, 45)),)


def test_add_clock_entry_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "dedup")
    start = dt.datetime(2026, 7, 25, 9, 0, tzinfo=cfg["_tz"])
    end = dt.datetime(2026, 7, 25, 9, 45, tzinfo=cfg["_tz"])
    _p1, added1 = W.add_clock_entry(cfg, "IDEA-001", start, end)
    _p2, added2 = W.add_clock_entry(cfg, "IDEA-001", start, end)
    assert added1 is True and added2 is False
    t = _idea(cfg)
    assert len(t.clock) == 1
