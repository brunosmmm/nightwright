"""SPEC-0082: hue on the idea `state` cell.

Dim carries "closed", hue carries which path it closed down — so PROMOTED / EXPORTED / DROPPED /
RESEARCHED stop rendering identically. The headline keeps its existing open-vs-done style, which is
the part easiest to regress by reusing one local for both cells.
"""
import re
from zoneinfo import ZoneInfo

import pytest
from rich.console import Console
from rich.style import Style

from nightwright import org_write as W
from nightwright import report as R
from nightwright.console import console

pytestmark = pytest.mark.fast

TZ = ZoneInfo("America/New_York")

IDEA_STATES = ["IDEA", "INCUBATE", "SPECCED", "PROMOTED", "EXPORTED", "SHIPPED",
               "DROPPED", "RESEARCHED"]
TERMINAL = ["PROMOTED", "EXPORTED", "SHIPPED", "DROPPED", "RESEARCHED"]


class _T:
    """Minimal stand-in for a Task, for the pure style helpers."""

    def __init__(self, state, is_done=False):
        self.state, self.is_done = state, is_done


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


def _render(fn, cfg, width=200, **kw):
    """Render with colour forced on, so the style spans are inspectable.

    The module-level console has no colour system under pytest (stdout is not a tty), so a fresh
    forced-terminal Console is swapped into `report` for the duration."""
    import io

    buf = io.StringIO()
    forced = Console(
        file=buf,
        width=width,
        height=80,
        force_terminal=True,
        color_system="standard",
        no_color=False,
    )
    real = R.console
    R.console = forced
    try:
        fn(cfg, **kw)
    finally:
        R.console = real
    return buf.getvalue()


# ---- the palette is the spec ------------------------------------------------------

EXPECTED = {
    "IDEA": "",
    "INCUBATE": "cyan",
    "SPECCED": "green",
    "PROMOTED": "dim green",
    "EXPORTED": "dim cyan",
    "SHIPPED": "bold green",
    "DROPPED": "dim red",
    "RESEARCHED": "dim",
}


@pytest.mark.parametrize("state", IDEA_STATES)
def test_palette_maps_every_idea_state(state):
    assert R._idea_state_style(_T(state, state in TERMINAL)) == EXPECTED[state]


def test_all_styles_parse():
    """A typo like `dim gren` would otherwise surface only at render time."""
    for state, style in R._IDEA_STATE_STYLES.items():
        if style:
            Style.parse(style)


def test_terminal_states_are_mutually_distinct():
    """The criterion that motivated the spec: finished / handed-off / abandoned must differ."""
    styles = [R._idea_state_style(_T(s, True)) for s in TERMINAL]
    assert len(set(styles)) == len(TERMINAL), styles


def test_idea_is_not_dim():
    """`dim` is nightwright's established 'done' marker; the newest state must not wear it."""
    assert "dim" not in R._idea_state_style(_T("IDEA"))


def test_yellow_is_not_reused():
    """`_state_style` gives yellow to BLOCKED/TRIAGE in `nw tasks`; one colour, one meaning."""
    assert not any("yellow" in s for s in R._IDEA_STATE_STYLES.values())


def test_unknown_state_falls_back_to_state_style():
    t = _T("TRIAGE")
    assert R._idea_state_style(t) == R._state_style(t)


# ---- the headline must not take the hue -------------------------------------------

def _spans(out, text):
    """Style codes immediately preceding `text` in rendered output."""
    return re.findall(r"\x1b\[([0-9;]*)m(?=" + re.escape(text) + ")", out)


def test_state_cell_is_hued_but_headline_is_not(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "a distinctive headline for span checking")
    W.set_state_by_selector(cfg, "IDEA-001", "INCUBATE")

    out = _render(R.ideas, cfg)
    state_spans = _spans(out, "INCUBATE")
    head_spans = _spans(out, "a distinctive headline")
    assert state_spans and head_spans
    assert state_spans != head_spans, (state_spans, head_spans)
    assert "36" in ";".join(state_spans)             # cyan on the state cell
    assert "36" not in ";".join(head_spans)          # …and not on the headline


def test_next_table_uses_the_same_palette(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "another headline")
    W.set_state_by_selector(cfg, "IDEA-001", "INCUBATE")
    out = _render(R.next_ideas, cfg)
    assert "36" in ";".join(_spans(out, "INCUBATE"))


# ---- collateral the spec forbids --------------------------------------------------

def test_tasks_and_agenda_still_use_the_shared_helper():
    """`nw tasks` / `nw agenda` / the review timeline must keep `_state_style`; restyling them
    is the collateral this spec exists to avoid."""
    import inspect

    src = inspect.getsource(R)
    for fn_name in ("def tasks(", "def agenda(", "def review("):
        body = src[src.index(fn_name):]
        body = body[:body.index("\ndef ", 1)]
        assert "_idea_state_style" not in body, fn_name


def test_tasks_output_has_no_idea_palette(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "an ordinary task")
    out = _render(R.tasks, cfg)
    assert "an ordinary task" in out
    # cyan (36) is the idea palette's live colour; the tasks table must not use it for a state
    assert "36" not in ";".join(_spans(out, "TODO"))


# ---- nothing but styling changed --------------------------------------------------

def test_plain_text_is_unchanged_without_colour(tmp_path):
    """Piped output carries no styling, so the visible text must be exactly as before."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "text should be identical")
    W.set_state_by_selector(cfg, "IDEA-001", "SPECCED")

    prev_w, prev_h = console._width, console._height
    console._width, console._height = 200, 80
    try:
        with console.capture() as cap:
            R.ideas(cfg)
        out = cap.get()
    finally:
        console._width, console._height = prev_w, prev_h
    assert "\x1b[" not in out
    assert "SPECCED" in out and "text should be identical" in out


@pytest.mark.parametrize("width", [200, 120, 80, 60])
def test_spec_0075_width_invariants_hold(tmp_path, width):
    """Width accounting measures the unstyled cell, so the palette must not disturb it."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "a fairly long headline that will need clipping at the narrow widths",
               project="Meta-Tools", kind="improvement")
    W.add_idea(cfg, "second idea", project="Example", kind="bug")

    prev_w, prev_h = console._width, console._height
    console._width, console._height = width, 80
    try:
        with console.capture() as cap:
            R.ideas(cfg)
        out = cap.get()
    finally:
        console._width, console._height = prev_w, prev_h

    header = next(ln for ln in out.splitlines() if " id " in ln and "idea" in ln)
    for col in ("id", "state", "kind", "project", "updated", "idea"):
        assert col in header, f"{col} missing at {width}"
    for ln in out.splitlines():
        assert len(ln) <= width, f"{len(ln)} > {width}: {ln!r}"

