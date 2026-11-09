"""Search-term highlighting in the desk (SPEC-0106).

Assertions are span-level — "some styling exists" would pass on a highlighter that marked the
wrong text. The point of this feature is *fidelity*: the marks must cover exactly what
SPEC-0086's matcher matched.
"""
import asyncio
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import report as R
from nightwright.cli import cli
from nightwright.tui import model as M

ROOT = Path(__file__).resolve().parent.parent


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


def _seed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    run = CliRunner().invoke
    assert run(cli, ["idea", "Textual packaging is intuitive"]).exit_code == 0
    assert run(cli, ["idea", "summary", "IDEA-001", "--set",
                     "A textual desk, TEXTUAL in caps too"]).exit_code == 0
    assert run(cli, ["idea", "unrelated aardvark"]).exit_code == 0
    return cfg


def _marks(text):
    """(slice, style) for every span the highlighter added."""
    return [(text.plain[s.start:s.end], str(s.style)) for s in text.spans]


# --- tokens come from the searcher --------------------------------------------------------

def test_search_tokens_delegates_to_the_searcher():
    assert M.search_tokens("Textual  TUI") == R.tokenize_idea_query("Textual  TUI")
    assert M.search_tokens("Textual  TUI") == ["textual", "tui"]
    assert M.search_tokens("") == []
    assert M.search_tokens(None) == []


def test_the_desk_defines_no_tokeniser_of_its_own():
    """The UI must route through `search_tokens`, never re-derive tokens or re-rank."""
    app_src = (ROOT / "src" / "nightwright" / "tui" / "app.py").read_text()
    assert "search_tokens" in app_src
    for banned in ("tokenize_idea_query", "score_idea_match", "idea_match_snippet",
                   "_QUERY_SPLIT_RE", ".lower().split"):
        assert banned not in app_src, f"{banned} reimplemented in the desk UI"


# --- span fidelity -------------------------------------------------------------------------

def test_marks_cover_exactly_the_token_occurrences(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    app = IdeaDesk(cfg)
    app.query = "textual"
    marked = app._marked("Textual packaging is intuitive")
    assert _marks(marked) == [("Textual", "reverse")]


def test_marks_are_case_insensitive(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    app = IdeaDesk(cfg)
    app.query = "textual"
    marked = app._marked("textual Textual TEXTUAL")
    assert [s for s, _ in _marks(marked)] == ["textual", "Textual", "TEXTUAL"]


def test_a_token_inside_a_longer_word_is_marked(tmp_path, monkeypatch):
    """SPEC-0086 matches substrings, so `tui` inside `intuitive` is *why* the idea matched.
    Marking less than the search used would misrepresent the result set."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    app = IdeaDesk(cfg)
    app.query = "tui"
    marked = app._marked("Textual packaging is intuitive")
    assert [s for s, _ in _marks(marked)] == ["tui"]
    start = marked.spans[0].start
    assert marked.plain[start - 2:start + 5] == "intuiti"   # inside the word, as matched


def test_every_token_of_a_multi_token_query_is_marked(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    app = IdeaDesk(cfg)
    app.query = "textual packaging"
    marked = app._marked("Textual packaging is intuitive")
    assert sorted(s.lower() for s, _ in _marks(marked)) == ["packaging", "textual"]


def test_no_query_means_no_marks(tmp_path, monkeypatch):
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    app = IdeaDesk(cfg)
    app.query = ""
    assert _marks(app._marked("Textual packaging is intuitive")) == []


def test_marks_agree_with_what_the_matcher_matched(tmp_path, monkeypatch):
    """Fidelity: anything marked must be a real reason `score_idea_match` accepted the idea."""
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)
    app = IdeaDesk(cfg)
    heading = "Textual packaging is intuitive"
    for query in ("textual", "tui", "textual packaging", "TEXTUAL"):
        app.query = query
        tokens = R.tokenize_idea_query(query)
        assert R.score_idea_match(tokens, heading, "", "", "") is not None
        for slice_, _style in _marks(app._marked(heading)):
            assert slice_.lower() in tokens


# --- detail pane ----------------------------------------------------------------------------

def test_detail_body_is_marked_without_corrupting_markup(tmp_path, monkeypatch):
    """Marks are applied to a Text, not to the markup string whose indices `escape()` shifts.

    Since SPEC-0107 `_render_detail` returns a `Text` directly (structure hand-built, prose
    lexed), so the mark step needs no markup round-trip at all.
    """
    from nightwright.tui.app import IdeaDesk, _render_detail

    cfg = _seed(tmp_path, monkeypatch)
    app = IdeaDesk(cfg)
    app.query = "textual"
    detail = M.DeskDetail(id="IDEA-001", state="IDEA",
                          heading="Textual packaging is intuitive",
                          summary="A textual desk, TEXTUAL in caps too",
                          open_questions=["[#A] is textual pinned?"])
    body = _render_detail(detail, width=70)
    hits = app._mark_into(body)
    assert hits >= 4
    assert {s.lower() for s, _ in _marks(body) if _ == "reverse"} == {"textual"}
    assert "[#A]" in body.plain          # markup survived the round-trip


# --- live behaviour ---------------------------------------------------------------------------

def _leaf_labels(app):
    """Every idea leaf's label on the board (SPEC-0495 replaced the DataTable)."""
    root = app.query_one("#board").root
    return [leaf.label for head in root.children for leaf in head.children]


def _reversed(text):
    return [s.lower() for s, style in _marks(text) if style == "reverse"]


def test_list_cells_gain_and_lose_marks_with_the_query(tmp_path, monkeypatch):
    from waits import pilot_until

    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(140, 20)) as pilot:
            assert await pilot_until(pilot, lambda: _leaf_labels(app))
            assert all(_reversed(label) == [] for label in _leaf_labels(app))

            await pilot.press("slash")
            await pilot.pause()
            for ch in "textual":
                await pilot.press(ch)
            assert await pilot_until(pilot, lambda: app._hits)
            await pilot.pause()
            labels = _leaf_labels(app)
            assert [_reversed(label) for label in labels] == [["textual"]]

            await pilot.press("escape")          # clearing removes the marks
            await pilot.pause()
            assert all(_reversed(label) == [] for label in _leaf_labels(app))

    asyncio.run(drive())


def test_the_mark_is_visibly_different_on_screen(tmp_path, monkeypatch):
    """End-to-end: the matched run must render with a different fill than ordinary body text.

    `reverse` is emitted by Rich's SVG as the text taking the *background* colour, so a match
    and its surrounding prose cannot share a style class.
    """
    from nightwright.tui.app import IdeaDesk

    cfg = _seed(tmp_path, monkeypatch)

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(140, 20)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            for ch in "textual":
                await pilot.press(ch)
            await pilot.pause(1.0)
            await pilot.pause()
            return app.export_screenshot()

    svg = asyncio.run(drive())
    fills = dict(re.findall(r'\.(\S+?)\s*\{\s*fill:\s*(#[0-9a-fA-F]{6})', svg))
    matched, plain = set(), set()
    for cls, body in re.findall(r'<text[^>]*class="([^"]*)"[^>]*>(.*?)</text>', svg, re.S):
        content = re.sub(r"<[^>]+>", "", body)
        key = cls.split()[-1]
        if key not in fills:
            continue
        (matched if re.search("textual", content, re.I) else plain).add(fills[key])
    assert matched, "no styled run contained the search term"
    assert matched - plain, f"match uses no fill distinct from ordinary text: {matched} vs {plain}"
