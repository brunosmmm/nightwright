"""SPEC-0085: glyphs for the idea `kind` column.

Chosen over colour because a glyph is *content* — it survives a pipe, a file and a paste, whereas
colour is stripped for a non-tty (which is why SPEC-0083/0084 were reverted). It is also
width-positive: the column shrinks from 11 cells (`improvement`) to 4, handing 7 to the headline.
"""
import io
import json
import re

import pytest
from rich.cells import cell_len
from rich.console import Console

from nightwright import org_write as W
from nightwright import report as R
from nightwright.org_write import IDEA_KINDS

from zoneinfo import ZoneInfo

pytestmark = pytest.mark.fast

TZ = ZoneInfo("America/New_York")
LONG = "a deliberately long headline so that the width difference is measurable at any width"


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


def _seeded(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, LONG, kind="improvement")
    W.add_idea(cfg, "a bug idea", kind="bug")
    W.add_idea(cfg, "a chore idea", kind="chore")
    W.add_idea(cfg, "a plain idea", kind="idea")
    return cfg


def _render(fn, cfg, width=130, **kw):
    buf = io.StringIO()
    real = R.console
    R.console = Console(file=buf, width=width, force_terminal=False, color_system=None)
    try:
        fn(cfg, **kw)
    finally:
        R.console = real
    return buf.getvalue()


# ---- the glyph map ----------------------------------------------------------------

@pytest.mark.parametrize("kind", IDEA_KINDS)
def test_map_covers_every_kind(kind):
    """Adding a kind must not silently fall back to text."""
    assert kind in R._KIND_GLYPHS


def test_map_has_no_extra_keys():
    assert set(R._KIND_GLYPHS) == set(IDEA_KINDS)


@pytest.mark.parametrize("kind", IDEA_KINDS)
def test_glyphs_have_no_variation_selectors(kind):
    """`⚠` is 1 cell; `⚠️` (same glyph + U+FE0F) is 2 chars and 2 cells. A stray selector would
    silently change the column width, and is invisible on inspection — so assert it."""
    g = R._KIND_GLYPHS[kind]
    assert len(g) == 1, f"{kind}: {len(g)} chars — variation selector?"
    assert cell_len(g) == 2, f"{kind}: cell_len {cell_len(g)}"
    assert "️" not in g


def test_glyphs_are_distinct():
    assert len(set(R._KIND_GLYPHS.values())) == len(IDEA_KINDS)


def test_kind_cell_plain_vs_glyph():
    assert R._kind_cell("bug", plain=True) == "bug"
    assert R._kind_cell("bug", plain=False) == R._KIND_GLYPHS["bug"]


def test_unknown_kind_falls_back_to_text():
    assert R._kind_cell("banana", plain=False) == "banana"


def test_legend_lists_every_kind():
    legend = R._kind_legend()
    for kind, glyph in R._KIND_GLYPHS.items():
        assert kind in legend and glyph in legend


# ---- rendering --------------------------------------------------------------------

@pytest.mark.parametrize("fn_name", ["ideas", "next_ideas"])
def test_table_shows_glyphs(tmp_path, fn_name):
    out = _render(getattr(R, fn_name), _seeded(tmp_path))
    for glyph in R._KIND_GLYPHS.values():
        assert glyph in out
    # the word is replaced, not preceded — checked on the rows, since the legend spells kinds out
    rows = [l for l in out.splitlines() if "IDEA-" in l]
    assert rows and not any("improvement" in l for l in rows)


@pytest.mark.parametrize("fn_name", ["ideas", "next_ideas"])
def test_plain_shows_words(tmp_path, fn_name):
    out = _render(getattr(R, fn_name), _seeded(tmp_path), plain=True)
    assert "improvement" in out and "chore" in out
    for glyph in R._KIND_GLYPHS.values():
        assert glyph not in out


def test_legend_shown_only_with_glyphs(tmp_path):
    cfg = _seeded(tmp_path)
    assert R._kind_legend() in _render(R.ideas, cfg)
    assert R._kind_legend() not in _render(R.ideas, cfg, plain=True)


def test_next_has_no_legend(tmp_path):
    """`nw next` has no footer line; adding one would reshape a view whose point is `next`."""
    out = _render(R.next_ideas, _seeded(tmp_path))
    assert R._kind_legend() not in out


# ---- the width justification ------------------------------------------------------

def test_glyph_mode_gives_the_headline_more_width(tmp_path):
    """The whole case for this change, so it is measured rather than assumed: 11 cells for
    `improvement` become 4, and the 7 saved go to the headline."""
    cfg = _seeded(tmp_path)

    def shown(plain):
        out = _render(R.ideas, cfg, plain=plain)
        row = next(l for l in out.splitlines() if LONG[:20] in l)
        return len(row[row.index(LONG[:20]):].rstrip().rstrip("…"))

    assert shown(plain=False) - shown(plain=True) == 7


@pytest.mark.parametrize("width", [200, 120, 80, 60])
@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.parametrize("fn_name", ["ideas", "next_ideas"])
def test_no_line_exceeds_width(tmp_path, width, plain, fn_name):
    """Measured with `cell_len`: a 2-cell glyph counts as one character, so `len` would pass here
    while the real terminal wrapped."""
    out = _render(getattr(R, fn_name), _seeded(tmp_path), width=width, plain=plain)
    for ln in out.splitlines():
        assert cell_len(ln) <= width, f"{cell_len(ln)} > {width}: {ln!r}"


@pytest.mark.parametrize("width", [200, 120, 80, 60])
@pytest.mark.parametrize("plain", [False, True])
def test_all_columns_present(tmp_path, width, plain):
    out = _render(R.ideas, _seeded(tmp_path), width=width, plain=plain)
    header = next(ln for ln in out.splitlines() if " id " in ln and "idea" in ln)
    for col in ("id", "state", "kind", "project", "updated", "idea"):
        assert col in header, f"{col} missing at {width} (plain={plain})"


@pytest.mark.parametrize("plain", [False, True])
def test_one_row_per_idea(tmp_path, plain):
    out = _render(R.ideas, _seeded(tmp_path), plain=plain)
    assert len([l for l in out.splitlines() if "IDEA-" in l]) == 4


# ---- display-width accounting -----------------------------------------------------

def test_meta_widths_uses_display_width(monkeypatch):
    """`_meta_widths` must reserve *cells*, not characters. The real `kind` header is 4 chars so it
    masks the difference; this shrinks it to 1 to expose the arithmetic directly."""
    monkeypatch.setitem(R._IDEA_META_COLS, "kind", ("k", 1, 12))
    prows = [{"_kind": R._KIND_GLYPHS["bug"]}]
    assert R._meta_widths(prows, ("kind",))["kind"] == 2      # len() would say 1


def test_meta_widths_measures_the_rendered_kind(tmp_path):
    """The column is sized from `_kind` (what is shown), not `kind` (the word) — the same trick
    SPEC-0077 used for the age column."""
    prows = [{"kind": "improvement", "_kind": R._KIND_GLYPHS["improvement"]}]
    assert R._meta_widths(prows, ("kind",))["kind"] == 4      # header 'kind', not 11


# ---- json is untouched ------------------------------------------------------------

def test_json_unaffected_by_plain(tmp_path, capsys):
    cfg = _seeded(tmp_path)
    R.ideas(cfg, as_json=True)
    a = capsys.readouterr().out
    R.ideas(cfg, as_json=True, plain=True)
    b = capsys.readouterr().out
    assert json.loads(a) == json.loads(b)
    assert all(r["kind"] in IDEA_KINDS for r in json.loads(a)["ideas"])


def test_json_carries_words_not_glyphs(tmp_path, capsys):
    R.ideas(_seeded(tmp_path), as_json=True)
    payload = capsys.readouterr().out
    for glyph in R._KIND_GLYPHS.values():
        assert glyph not in payload


# ---- glyphs survive the medium (the reason they beat colour) ----------------------

def test_glyphs_survive_a_colourless_render(tmp_path):
    """`_render` uses color_system=None — i.e. a pipe. Colour would vanish here; a glyph must not."""
    out = _render(R.ideas, _seeded(tmp_path))
    assert "\x1b[" not in out
    for glyph in R._KIND_GLYPHS.values():
        assert glyph in out

