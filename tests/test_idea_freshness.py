"""SPEC-0077: the `updated` column on `nw ideas` and freshness ordering.

Closes the third ask of IDEA-092: SPEC-0075 widened the headline, SPEC-0076 produced the stamps,
and this makes them visible and makes them decide the order.
"""
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import pytest

from nightwright import explore as E
from nightwright import org_write as W
from nightwright import report as R
from nightwright.console import console

pytestmark = pytest.mark.fast

TZ = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 7, 27, 12, 0)


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                                  "DROPPED"],
            "timezone": "America/New_York", "_tz": TZ,
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ai" / "ideas.org"),
            # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
            "specs_dir": str(tmp_path / "specs"),
            "org_ideas_archive_file": str(org / "ai" / "ideas-archive.org"),
            "config_dir": str(tmp_path / "cfg"), "project_axis": "bucket"}


def _set_updated(cfg, idea_id, value):
    """Force one idea's `:UPDATED:` (None removes it), so ages/order are deterministic."""
    path = cfg["org_ideas_file"]
    lines = open(path).read().splitlines(keepends=True)
    out, in_target = [], False
    for ln in lines:
        if ln.startswith("* "):
            in_target = False
        if ln.strip() == f":ID: {idea_id}":
            in_target = True
        if in_target and ln.strip().startswith(":UPDATED:"):
            if value is not None:
                out.append(ln.split(":UPDATED:")[0] + f":UPDATED: {value}\n")
            continue
        out.append(ln)
    with open(path, "w") as f:
        f.writelines(out)


def _render(fn, cfg, width=200, **kw):
    prev_w, prev_h = console._width, console._height
    console._width, console._height = width, 80
    try:
        with console.capture() as cap:
            fn(cfg, **kw)
    finally:
        console._width, console._height = prev_w, prev_h
    return cap.get()


def _row_ids(out):
    ids = []
    for ln in out.splitlines():
        for tok in ln.split():
            if tok.startswith("IDEA-"):
                ids.append(tok)
                break
    return ids


def _header(out):
    return next(ln for ln in out.splitlines() if " id " in ln and "idea" in ln)


# ---- the column -------------------------------------------------------------------

def test_updated_column_present_in_ideas_not_next(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea")
    assert "updated" in _header(_render(R.ideas, cfg))
    assert "updated" not in _header(_render(R.next_ideas, cfg))


def test_unstamped_renders_placeholder(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "never touched")
    _set_updated(cfg, "IDEA-001", None)
    out = _render(R.ideas, cfg)
    row = next(ln for ln in out.splitlines() if "IDEA-001" in ln)
    assert "—" in row


def test_fresh_capture_shows_minutes(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "just captured")
    row = next(ln for ln in _render(R.ideas, cfg).splitlines() if "IDEA-001" in ln)
    assert "0m" in row


# ---- _age boundaries --------------------------------------------------------------

def _stamp(when):
    return when.strftime("[%Y-%m-%d %a %H:%M]")


@pytest.mark.parametrize("delta,expected", [
    (dt.timedelta(0), "0m"),
    (dt.timedelta(minutes=1), "1m"),
    (dt.timedelta(minutes=59), "59m"),
    (dt.timedelta(minutes=60), "1h"),
    (dt.timedelta(hours=23), "23h"),
    (dt.timedelta(hours=24), "1d"),
    (dt.timedelta(days=6), "6d"),
    (dt.timedelta(days=7), "1w"),
    (dt.timedelta(days=364), "52w"),
    (dt.timedelta(days=365), "1y"),
    (dt.timedelta(days=800), "2y"),
])
def test_age_rendering_boundaries(delta, expected):
    assert R._age(_stamp(NOW - delta), now=NOW) == expected


@pytest.mark.parametrize("value", [None, "", "not a stamp", "[nope]", "[2026-13-45 Xxx]",
                                  "[2026-07-27 Mon 99:99]"])
def test_age_of_missing_or_malformed_is_placeholder(value):
    assert R._age(value, now=NOW) == "—"


def test_future_stamp_floors_at_zero():
    assert R._age(_stamp(NOW + dt.timedelta(days=3)), now=NOW) == "0m"


def test_age_accepts_aware_now():
    """`ideas()` passes a tz-aware now; org stamps are naive local."""
    aware = NOW.replace(tzinfo=TZ)
    assert R._age(_stamp(NOW - dt.timedelta(hours=2)), now=aware) == "2h"


# ---- ordering ---------------------------------------------------------------------

def _three(cfg):
    W.add_idea(cfg, "aaa oldest")
    W.add_idea(cfg, "bbb newest")
    W.add_idea(cfg, "ccc middle")
    _set_updated(cfg, "IDEA-001", "[2026-01-01 Thu 09:00]")
    _set_updated(cfg, "IDEA-002", "[2026-07-01 Wed 09:00]")
    _set_updated(cfg, "IDEA-003", "[2026-04-01 Wed 09:00]")


def test_ideas_sorted_newest_first(tmp_path):
    cfg = _cfg(tmp_path)
    _three(cfg)
    assert _row_ids(_render(R.ideas, cfg)) == ["IDEA-002", "IDEA-003", "IDEA-001"]


def test_unstamped_sort_last(tmp_path):
    cfg = _cfg(tmp_path)
    _three(cfg)
    W.add_idea(cfg, "ddd unstamped")
    _set_updated(cfg, "IDEA-004", None)
    ids = _row_ids(_render(R.ideas, cfg))
    assert ids[-1] == "IDEA-004"
    assert ids == ["IDEA-002", "IDEA-003", "IDEA-001", "IDEA-004"]


def test_equal_stamps_break_ties_by_heading_deterministically(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "zzz last alphabetically")
    W.add_idea(cfg, "aaa first alphabetically")
    same = "[2026-05-05 Tue 09:00]"
    _set_updated(cfg, "IDEA-001", same)
    _set_updated(cfg, "IDEA-002", same)
    first = _row_ids(_render(R.ideas, cfg))
    assert first == ["IDEA-002", "IDEA-001"]          # aaa before zzz
    assert _row_ids(_render(R.ideas, cfg)) == first   # stable across runs


def test_done_ideas_stay_below_open_with_all(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "open but ancient")
    W.add_idea(cfg, "done but touched a second ago")
    _set_updated(cfg, "IDEA-001", "[2026-01-01 Thu 09:00]")
    W.set_state_by_selector(cfg, "IDEA-002", "PROMOTED")   # auto-archives (SPEC-0073)

    ids = _row_ids(_render(R.ideas, cfg, all_done=True))
    assert ids.index("IDEA-001") < ids.index("IDEA-002"), ids


def test_next_ordering_unchanged(tmp_path):
    """`nw next` keeps `_task_sort_key`; only the ideas listing is freshness-ordered."""
    cfg = _cfg(tmp_path)
    _three(cfg)
    tasks = R.collect_next_tasks(cfg)
    expected = [t.properties.get("ID") for t in sorted(tasks, key=R._task_sort_key)]
    assert [t.properties.get("ID") for t in tasks] == expected
    assert _row_ids(_render(R.next_ideas, cfg)) == expected


def test_json_order_matches_table(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _three(cfg)
    table_ids = _row_ids(_render(R.ideas, cfg))
    R.ideas(cfg, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert [r["id"] for r in payload["ideas"]] == table_ids
    assert payload["schema"] == "nw.ideas.v1"
    for row in payload["ideas"]:                      # no schema change
        assert {"id", "state", "heading", "tags", "kind", "next"} <= set(row)


# ---- SPEC-0075 invariants still hold with the extra column ------------------------

@pytest.mark.parametrize("width", [200, 120, 80, 60])
def test_spec_0075_invariants_hold_with_new_column(tmp_path, width):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "a fairly long headline that will need clipping at narrow widths indeed",
               project="Meta-Tools", kind="improvement")
    W.add_idea(cfg, "another idea entirely", project="Example", kind="bug")
    out = _render(R.ideas, cfg, width=width)

    header = _header(out)
    for col in ("id", "state", "kind", "project", "updated", "idea"):
        assert col in header, f"{col} missing at {width}: {header!r}"
    for ln in out.splitlines():
        assert len(ln) <= width, f"{len(ln)} > {width}: {ln!r}"
    assert len(_row_ids(out)) == 2


def test_updated_column_costs_little_width(tmp_path):
    """The age is 2-4 chars; the column must not reserve the raw stamp's ~20."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea")
    prows = [dict(R.idea_row(cfg, W.resolve_selector(cfg, "IDEA-001")), _age="3d")]
    prev_w, prev_h = console._width, console._height
    console._width, console._height = 200, 80
    try:
        widths, _ = R._idea_table_widths(prows, ("id", "state", "updated"))
    finally:
        console._width, console._height = prev_w, prev_h
    assert widths["updated"] <= 8


# ---- the shared stamp parser ------------------------------------------------------

@pytest.mark.parametrize("with_time", [True, False])
def test_parse_inactive_stamp_round_trip(tmp_path, with_time):
    cfg = _cfg(tmp_path)
    text = W._inactive_stamp(cfg, with_time=with_time)
    parsed = W.parse_inactive_stamp(text)
    assert parsed is not None
    now = dt.datetime.now(TZ)
    assert parsed.date() == now.date()
    if with_time:
        assert (parsed.hour, parsed.minute) == (now.hour, now.minute)


@pytest.mark.parametrize("junk", [None, "", "no stamp", "[]", "[2026-02-30 Fri]"])
def test_parse_inactive_stamp_rejects_junk(junk):
    assert W.parse_inactive_stamp(junk) is None


def test_newest_log_stamp_uses_shared_parser():
    """SPEC-0077 folded explore's own regex into `parse_inactive_stamp`."""
    log = "*** [2026-01-02 Fri 08:00]\nolder\n*** [2026-04-05 Sun 17:45]\nnewer\n"
    assert E.newest_log_stamp(log) == dt.datetime(2026, 4, 5, 17, 45)

