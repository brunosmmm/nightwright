"""SPEC-0075: the `nw ideas` / `nw next` triage tables. `nw ideas` drops the `next` column
(the hint lives on `nw next`), and both give the headline whatever width the terminal leaves
over instead of a hard-coded 40 chars — without letting a column collapse or a truncation go
unmarked."""
import json
import os
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest

from nightwright import org_write as W
from nightwright import report as R
from nightwright.console import console

LONG = ("Unified SpecRef resolver: stop outbound ids from being parsed as internal "
        "SPEC-NNNN everywhere in the codebase")


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                                  "DROPPED"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ai" / "ideas.org"),
            # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
            "specs_dir": str(tmp_path / "specs"),
            "org_ideas_archive_file": str(org / "ai" / "ideas-archive.org"),
            # `project=` on capture validates against the rules layer, which reads config_dir.
            "config_dir": str(tmp_path / "cfg"), "project_axis": "bucket"}


def _seeded(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, LONG, project="Meta-Tools", kind="idea")
    W.add_idea(cfg, "Cascading faceted filters for the report view",
               project="AI-workstreams", kind="improvement")
    return cfg


def _render(fn, cfg, width, **kw):
    """Render a report function at an exact console width (Rich 15 needs both dims set)."""
    prev_w, prev_h = console._width, console._height
    console._width, console._height = width, 80
    try:
        with console.capture() as cap:
            fn(cfg, **kw)
    finally:
        console._width, console._height = prev_w, prev_h
    return cap.get()


def _rows(out):
    return [ln for ln in out.splitlines() if "IDEA-" in ln]


WIDTHS = [200, 120, 80, 60]


# ---- the `next` column moves to `nw next` -----------------------------------------

def test_ideas_table_has_no_next_column(tmp_path):
    out = _render(R.ideas, _seeded(tmp_path), 200)
    header = next(ln for ln in out.splitlines() if " id " in ln and "idea" in ln)
    assert "next" not in header
    # …and the headline is no longer capped at 40 chars.
    assert LONG[:60] in out


def test_next_table_keeps_next_column(tmp_path):
    out = _render(R.next_ideas, _seeded(tmp_path), 200)
    header = next(ln for ln in out.splitlines() if " id " in ln and "idea" in ln)
    assert "next" in header
    assert LONG[:60] in out


def test_headline_beats_forty_chars_in_both_tables(tmp_path):
    """The bug this spec fixes: 40 chars regardless of terminal width."""
    cfg = _seeded(tmp_path)
    for fn in (R.ideas, R.next_ideas):
        out = _render(fn, cfg, 200)
        shown = max((len(ln) for ln in _rows(out)), default=0)
        assert LONG[:80] in out, f"{fn.__name__} still truncating early ({shown} cols)"


# ---- JSON is untouched (display-only removal) --------------------------------------

def test_json_still_carries_next(tmp_path, capsys):
    cfg = _seeded(tmp_path)
    R.ideas(cfg, as_json=True)
    ideas = json.loads(capsys.readouterr().out)
    assert ideas["schema"] == "nw.ideas.v1"
    assert all("next" in row for row in ideas["ideas"])

    R.next_ideas(cfg, as_json=True)
    nxt = json.loads(capsys.readouterr().out)
    assert nxt["schema"] == "nw.next.v1"
    assert all("next" in row for row in nxt["ideas"])


# ---- no column may collapse, no line may overflow ---------------------------------

@pytest.mark.parametrize("width", WIDTHS)
def test_ideas_keeps_all_columns_when_narrow(tmp_path, width):
    """Regression guard for the rejected Rich-flex alternative, where `kind` vanished at <=80."""
    out = _render(R.ideas, _seeded(tmp_path), width)
    header = next(ln for ln in out.splitlines() if " id " in ln and "idea" in ln)
    for col in ("id", "state", "kind", "project", "idea"):
        assert col in header, f"column {col!r} missing at width {width}: {header!r}"


@pytest.mark.parametrize("width", WIDTHS)
def test_next_keeps_all_columns_when_narrow(tmp_path, width):
    out = _render(R.next_ideas, _seeded(tmp_path), width)
    header = next(ln for ln in out.splitlines() if " id " in ln and "idea" in ln)
    for col in ("id", "state", "kind", "project", "next", "idea"):
        assert col in header, f"column {col!r} missing at width {width}: {header!r}"


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fn", [R.ideas, R.next_ideas], ids=["ideas", "next"])
def test_tables_never_exceed_console_width(tmp_path, width, fn):
    out = _render(fn, _seeded(tmp_path), width)
    for ln in out.splitlines():
        assert len(ln) <= width, f"{len(ln)} > {width}: {ln!r}"


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("fn", [R.ideas, R.next_ideas], ids=["ideas", "next"])
def test_one_line_per_idea_nothing_wraps(tmp_path, width, fn):
    out = _render(fn, _seeded(tmp_path), width)
    assert len(_rows(out)) == 2, f"expected 2 rows at width {width}, got {len(_rows(out))}"


@pytest.mark.parametrize("width", [120, 80, 60])
def test_truncation_is_marked(tmp_path, width):
    """A cut headline must end in an ellipsis — silent cropping is the failure mode that
    rejected letting Rich flex the column."""
    out = _render(R.ideas, _seeded(tmp_path), width)
    # Match by id: at narrow widths the word "Unified" itself may be clipped to "Unifie…".
    long_row = next(ln for ln in _rows(out) if "IDEA-001" in ln)
    assert "…" in long_row, f"headline cropped without a marker at {width}: {long_row!r}"


# ---- the budget helper ------------------------------------------------------------

def test_headline_budget_reserves_only_what_data_needs():
    """Short kind/project values must leave the headline more room than capped-out ones."""
    keys = ("id", "state", "kind", "project")
    short = [{"id": "IDEA-001", "state": "IDEA", "kind": "idea", "project": "X"}]
    wide = [{"id": "IDEA-001", "state": "INCUBATE", "kind": "improvement",
             "project": "AI-workstreams-long"}]
    prev_w, prev_h = console._width, console._height
    console._width, console._height = 200, 80
    try:
        _, budget_short = R._idea_table_widths(short, keys)
        _, budget_wide = R._idea_table_widths(wide, keys)
    finally:
        console._width, console._height = prev_w, prev_h
    assert budget_short > budget_wide


def test_headline_budget_tracks_console_width():
    keys = ("id", "state", "kind", "project")
    rows = [{"id": "IDEA-001", "state": "IDEA", "kind": "idea", "project": "Meta-Tools"}]
    prev_w, prev_h = console._width, console._height
    try:
        console._width, console._height = 100, 80
        _, narrow = R._idea_table_widths(rows, keys)
        console._width, console._height = 200, 80
        _, wide = R._idea_table_widths(rows, keys)
    finally:
        console._width, console._height = prev_w, prev_h
    assert wide - narrow == 100


def test_headline_budget_reclaims_from_capped_columns_when_cramped():
    """When the terminal cannot give the headline its floor, width comes out of the capped
    metadata columns rather than being silently cropped by Rich."""
    keys = ("id", "state", "kind", "project")
    rows = [{"id": "IDEA-001", "state": "INCUBATE", "kind": "improvement",
             "project": "AI-workstreams"}]
    prev_w, prev_h = console._width, console._height
    console._width, console._height = 58, 80
    try:
        widths, budget = R._idea_table_widths(rows, keys)
    finally:
        console._width, console._height = prev_w, prev_h
    assert budget >= 12
    assert widths["project"] < len("AI-workstreams") or widths["kind"] < len("improvement")
    # id/state are fixed-shape keys and must never be squeezed.
    assert widths["id"] >= 8 and widths["state"] >= 8


# ---- $COLUMNS (Rich behaviour this spec depends on but does not own) ---------------

def test_columns_env_var_controls_width_when_piped(tmp_path):
    """`COLUMNS=N nw ideas | cat` must use N columns. Pins Rich's own $COLUMNS support: if a
    future Rich drops it, piping silently falls back to 80 and this fails loudly instead."""
    cfg = _seeded(tmp_path)
    plain = {k: v for k, v in cfg.items() if k != "_tz"}   # ZoneInfo has no eval-able repr
    env = dict(os.environ, COLUMNS="200")
    code = (
        "from zoneinfo import ZoneInfo\n"
        "from nightwright import report as R\n"
        f"cfg = {plain!r}\n"
        "cfg['_tz'] = ZoneInfo(cfg['timezone'])\n"
        "R.ideas(cfg)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert max(len(ln) for ln in proc.stdout.splitlines()) > 80


# ---- open-clock glyph on id (SPEC-0140) ---------------------------------------------

def test_idea_id_label_marks_open_clock():
    assert R._idea_id_label({"id": "IDEA-001", "clock_open": True}) == "◕ IDEA-001"
    assert R._idea_id_label({"id": "IDEA-002", "clock_open": False}) == "  IDEA-002"


def test_ideas_and_next_tables_show_open_clock_glyph(tmp_path):
    cfg = _seeded(tmp_path)
    W.clock_in(cfg, "IDEA-001")
    ideas_out = _render(R.ideas, cfg, 200)
    next_out = _render(R.next_ideas, cfg, 200)
    for out in (ideas_out, next_out):
        rows = _rows(out)
        clocked = [ln for ln in rows if "IDEA-001" in ln]
        idle = [ln for ln in rows if "IDEA-002" in ln]
        assert clocked and "◕" in clocked[0]
        assert idle and "◕" not in idle[0]
    # JSON id stays bare (agents use clock_open)
    from nightwright.report import idea_row, collect_idea_tasks
    by_id = {r["id"]: r for r in (idea_row(cfg, t) for t in collect_idea_tasks(cfg))}
    assert by_id["IDEA-001"]["clock_open"] is True
    assert by_id["IDEA-001"]["id"] == "IDEA-001"
