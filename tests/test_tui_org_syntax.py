"""Org syntax highlighting in the desk detail pane (SPEC-0107).

Two invariants matter more than the prettiness: **structure never reaches the lexer** (so no
theme can eat a section rule or a glyph), and **no hardcoded hex** ever enters the styles —
that is the failure that got SPEC-0083/0084 reverted.
"""
import asyncio
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import tui as T
from nightwright.cli import cli
from nightwright.tui import model as M

ROOT = Path(__file__).resolve().parent.parent

needs_textual = pytest.mark.skipif(not T.textual_available(),
                                   reason="optional tui extra not installed")

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"],
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(tmp_path),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
    }


def _detail(**kw):
    base = dict(id="IDEA-001", state="SPECCED", heading="a lexed idea",
                meta=["kind improvement", "Meta-Tools"],
                summary="Wire ~append_log~ and =verbatim= together.",
                open_questions=["is ~textual~ pinned?"],
                resolved_questions=["pin >=8,<9"],
                log=["[2026-07-30 Thu 16:25]\nnote with ~code~"])
    base.update(kw)
    return M.DeskDetail(**base)


def _styled(text):
    """(slice, style) for spans that actually carry a visible style."""
    return [(text.plain[s.start:s.end], str(s.style)) for s in text.spans
            if text.plain[s.start:s.end].strip() and str(s.style) != "on default"]


# --- what the lexer actually styles -------------------------------------------------------

@needs_textual
def test_code_and_verbatim_are_styled():
    from nightwright.tui.app import lex_org

    styled = dict(_styled(lex_org("prose with ~code~ and =verb= inside")))
    assert styled.get("~code~")
    assert styled.get("=verb=")


@needs_textual
def test_emphasis_mid_prose_is_not_styled_by_orglexer():
    """Documented limitation, asserted so it is a known state rather than a surprise.

    Pygments' OrgLexer does not mark `*bold*` / `/italic/` inside a prose line — only at
    headline level, and headlines are hand-built structure the desk deliberately never lexes.
    The delivered win is `~code~` / `=verbatim=` / links, which is what `nw` prose is full of
    (SPEC-0030 normalises Markdown backticks to tildes).
    """
    from nightwright.tui.app import lex_org

    assert _styled(lex_org("prose with *bold* inside")) == []
    assert _styled(lex_org("prose with /italic/ inside")) == []


@needs_textual
def test_lexing_empty_prose_is_safe():
    from nightwright.tui.app import lex_org

    assert lex_org("").plain == ""


@needs_textual
def test_lexed_prose_keeps_its_text_verbatim():
    """Styling must not rewrite content — the org source is what the user typed."""
    from nightwright.tui.app import lex_org

    src = "Wire ~append_log~ and [[IDEA-131]] with *emphasis* and a :tag:"
    assert lex_org(src).plain.rstrip("\n") == src


# --- structure never reaches the lexer -----------------------------------------------------

@needs_textual
def test_structure_survives_lexing():
    from nightwright.tui.app import _render_detail

    plain = _render_detail(_detail(), width=70).plain
    assert "Summary " in plain and "─" in plain      # section rule
    assert "○ " in plain and "✓ " in plain           # question glyphs
    assert "1 open · 1 resolved" in plain
    assert "Log " in plain
    assert "a lexed idea" in plain


@needs_textual
def test_clock_and_meta_lines_are_not_lexed():
    from nightwright.tui.app import _render_detail

    d = _detail(clock_open=True, clocked_hours=1.5)
    plain = _render_detail(d, width=70).plain
    assert "◕ clocked in" in plain
    assert "1.50h logged" in plain
    assert "kind improvement · Meta-Tools" in plain


@needs_textual
def test_org_prose_with_markup_characters_is_not_eaten():
    """`[#A]` / `[[links]]` / `[2026-07-30 Thu]` must survive — they look like Rich markup."""
    from nightwright.tui.app import _render_detail

    d = _detail(summary="see [#A] and [bold]not bold[/bold]",
                open_questions=["[#B] which [[IDEA-042]] first?"],
                log=["[2026-07-30 Thu 17:00]\nbody"])
    plain = _render_detail(d, width=70).plain
    assert "[#A]" in plain and "[bold]not bold[/bold]" in plain
    assert "[#B] which [[IDEA-042]] first?" in plain
    assert "[2026-07-30 Thu 17:00]" in plain


# --- no hardcoded colour -------------------------------------------------------------------

@needs_textual
@pytest.mark.parametrize("theme", ["ansi_dark", "ansi_light"])
def test_no_hex_colours_anywhere(theme):
    """SPEC-0083/0084 were reverted for a hex palette invisible on a dark terminal."""
    from nightwright.tui.app import _render_detail

    for style in {str(s.style) for s in _render_detail(_detail(), width=70, theme=theme).spans}:
        assert not HEX_RE.search(style), f"hardcoded colour {style!r} under {theme}"


def test_the_desk_names_no_hex_theme():
    """Checks *usage* (a string literal), not mentions — the module comments name nord and
    monokai deliberately, to record why they were rejected."""
    app_src = (ROOT / "src" / "nightwright" / "tui" / "app.py").read_text()
    for banned in ("nord", "monokai", "idea_show_theme"):
        for literal in (f'"{banned}"', f"'{banned}'"):
            assert literal not in app_src, f"{literal} is used as a theme in the desk"
    assert '"ansi_dark"' in app_src and '"ansi_light"' in app_src


@needs_textual
def test_theme_follows_the_apps_light_dark_state(tmp_path):
    from nightwright.tui.app import ANSI_DARK, ANSI_LIGHT, IdeaDesk

    cfg = _cfg(tmp_path)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.theme = "textual-dark"
            await pilot.pause()
            dark = app.syntax_theme
            app.theme = "textual-light"
            await pilot.pause()
            light = app.syntax_theme
            return dark, light

    dark, light = asyncio.run(drive())
    assert dark == ANSI_DARK
    assert light == ANSI_LIGHT


# --- composition with search marks (SPEC-0106) ----------------------------------------------

@needs_textual
def test_search_marks_compose_on_top_of_lexed_prose(tmp_path, monkeypatch):
    """The reason this design was viable at all: lexing returns a Text, so marks still apply."""
    from nightwright.tui.app import IdeaDesk, _render_detail

    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    app = IdeaDesk(cfg)
    app.query = "append_log"

    body = _render_detail(_detail(), width=70)
    lexer_spans = len(body.spans)
    hits = app._mark_into(body)
    assert hits >= 1
    assert len(body.spans) > lexer_spans
    marked = [body.plain[s.start:s.end] for s in body.spans if str(s.style) == "reverse"]
    assert marked and all("append_log" in m for m in marked)


# --- the classic CLI is untouched -------------------------------------------------------------

def test_nw_idea_show_is_unchanged(tmp_path, monkeypatch):
    """SPEC-0107 is a desk-only change; `nw idea show` keeps its own renderer."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "an idea"]).exit_code == 0
    assert run(cli, ["idea", "summary", "IDEA-001", "--set", "with ~code~"]).exit_code == 0
    r = run(cli, ["idea", "show", "IDEA-001"])
    assert r.exit_code == 0
    assert "with ~code~" in r.output

    from nightwright.explore import format_idea_show
    from nightwright.org import load_tasks
    task = [t for t in load_tasks(cfg) if t.is_idea][0]
    assert isinstance(format_idea_show(cfg, task), str)   # still plain text, not a Text
