"""Textual ideas desk (SPEC-0099 entry; SPEC-0490 rail + bucket board replaced SPEC-0100's table).

Imports Textual at module scope on purpose — only reachable through `nightwright.tui.run()`, which
probes for the optional extra first. All data comes from `nightwright.tui.model`; this file is a
renderer plus SPEC-0102 mutation wiring; every write goes through `model.apply_mutation`.
"""
import asyncio

from rich.markup import escape
from rich.text import Text

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (DataTable, Footer, Header, Input, Label, OptionList, Static,
                             TabbedContent, TabPane, TextArea, Tree)
from textual.widgets.option_list import Option

from .. import report as R
from . import ideas_board as B
from . import model as M
from .campaign_detail import CampaignDetail, OpenCampaign
from .events_tab import EventsView
from .mailbox_tab import MailboxView
from .queue_tab import QueueView

# SPEC-0490: the ideas tab is rail | board | detail. The rail is fixed-width (project names are
# short and the counts right-align in it); the detail pane keeps the old split's proportional
# share so Summary prose still wraps readably, and the board takes what is left.
RAIL_WIDTH = 26
DETAIL_WIDTH = "36%"
_BUCKET_STYLE = {B.OPEN: "yellow", B.IN_FLIGHT: "cyan", B.BUILT: "green", B.DROPPED: "dim"}
# Spec status colour: live work cyan, approved green, still being written yellow; a link that
# does not resolve (missing / foreign / no link) red, because it is an anomaly to fix.
_STATUS_STYLE = {"in-progress": "cyan", "accepted": "green", "done": "green dim",
                 "proposed": "yellow", "draft": "yellow", "superseded": "dim",
                 "missing": "red", "foreign": "red"}

# `section` + `id` columns + four cells of DataTable padding on the hub tab.
_HUB_META_COST = 34
# SPEC-0105: `search_idea_tasks` costs ~300ms over the live 43-idea corpus, so live filtering
# runs on a restarting timer rather than per keystroke. Roughly one search duration: a burst of
# typing collapses to a single search, and the desk never queues work faster than it finishes.
SEARCH_DEBOUNCE = 0.3
# SPEC-0106: `reverse` inverts whatever the terminal already renders, so a match mark cannot
# vanish against any background. SPEC-0083/0084 shipped a themed palette that was invisible on
# a dark terminal and had to be reverted — no hardcoded colour here.
MATCH_STYLE = "reverse"
# SPEC-0109: the explicit "no project" entry in a chooser. A sentinel rather than an empty row,
# so "clear it" is a visible choice instead of a blank line the user has to guess at.
NONE_CHOICE = "(none)"
# SPEC-0107: ANSI-named syntax themes, chosen from Textual's light/dark. Their colours are
# resolved by the terminal from its own palette, so they cannot be invisible — a hex Pygments
# theme (nord, monokai) is what got SPEC-0083/0084 reverted.
ANSI_DARK = "ansi_dark"
ANSI_LIGHT = "ansi_light"


class TextPrompt(ModalScreen[str | None]):
    """One-line prompt. Dismisses with the text, or None on Esc."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=False)]

    def __init__(self, title: str, value: str = ""):
        super().__init__()
        self.title_text = title
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self.title_text)
            yield Input(value=self.value, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BodyPrompt(ModalScreen[str | None]):
    """Multi-line prompt (Summary). Ctrl+S saves, Esc cancels — Enter must stay a newline."""

    BINDINGS = [
        Binding("ctrl+s", "save", "save"),
        Binding("escape", "cancel", "cancel", show=False),
    ]

    def __init__(self, title: str, value: str = ""):
        super().__init__()
        self.title_text = title
        self.value = value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(f"{self.title_text}  (ctrl+s save · esc cancel)")
            yield TextArea(self.value, id="prompt-body")

    def on_mount(self) -> None:
        area = self.query_one("#prompt-body", TextArea)
        area.focus()
        # Cursor to the end, not position 0: a pre-filled editor is opened to *continue* the
        # text far more often than to prepend to it. Found while editing a rationale, where
        # typing appended to the front and produced "sobecause".
        area.move_cursor(area.document.end)

    def action_save(self) -> None:
        self.dismiss(self.query_one("#prompt-body", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class QuestionsScreen(ModalScreen[bool]):
    """Manage one idea's questions by 1-based index (SPEC-0101 addressing).

    A dedicated screen rather than inline keys on the detail pane: resolve / unresolve / edit /
    delete all need a *selected question*, and giving the main desk a second cursor would cost
    more chrome than it saves. Dismisses True when anything was written.
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "down", show=False),
        Binding("k,up", "cursor_up", "up", show=False),
        # `enter` is *not* a Binding here: the focused DataTable consumes it for its own
        # select action, so the screen binding would never fire. `RowSelected` is the event
        # that keypress actually produces — the status line advertises it.
        Binding("a", "add", "add"),
        Binding("e", "edit", "edit"),
        Binding("d", "delete", "delete"),
        Binding("r", "rationale", "rationale"),   # SPEC-0112
        Binding("escape,q", "close", "close"),
    ]

    def __init__(self, cfg, selector: str):
        super().__init__()
        self.cfg = cfg
        self.selector = selector
        self.items: list[dict] = []
        self.dirty = False

    def compose(self) -> ComposeResult:
        with Vertical(id="questions-box"):
            yield Label(f"questions · {self.selector}", id="questions-title")
            yield DataTable(id="questions", cursor_type="row")
            yield Static("", id="questions-status")
            yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#questions", DataTable)
        table.add_columns("#", "state", "question")
        table.cursor_type = "row"
        table.focus()
        self.refresh_items()

    def refresh_items(self) -> None:
        from .. import explore as EX
        from ..org_write import resolve_selector

        table = self.query_one("#questions", DataTable)
        keep = table.cursor_row
        try:
            task = resolve_selector(self.cfg, self.selector)
            self.items = EX.read_idea_questions(self.cfg, task)
        except (ValueError, OSError) as e:
            self.items = []
            self.query_one("#questions-status", Static).update(f"[red]{escape(str(e))}[/]")
        table.clear()
        for i, item in enumerate(self.items, 1):
            resolved = (item.get("state") or "").lower() == "resolved"
            cookie = f"[#{item['priority']}] " if item.get("priority") else ""
            # SPEC-0112: the rationale rides under its question, indented and dim, so the
            # list still scans as a list of questions rather than a wall of prose.
            cell = Text(cookie + (item.get("text") or ""), style="dim" if resolved else "")
            body = (item.get("body") or "").strip()
            if body:
                first = body.splitlines()[0]
                more = " …" if len(body.splitlines()) > 1 else ""
                cell.append("\n  └ " + first + more, style="dim italic")
            table.add_row(str(i),
                          Text("done" if resolved else "open",
                               style="green" if resolved else "yellow"),
                          cell, height=2 if body else 1)
        if self.items:
            table.move_cursor(row=min(keep or 0, len(self.items) - 1))
        self.query_one("#questions-status", Static).update(
            f"[dim]{len(self.items)} question(s) · enter toggle · r rationale · "
            f"a add · e edit · d delete[/]"
            if self.items else "[dim]no questions yet — a to add[/]")

    # --- helpers ---------------------------------------------------------------------

    @property
    def index(self) -> int | None:
        """1-based index of the highlighted question, or None."""
        table = self.query_one("#questions", DataTable)
        row = table.cursor_row
        if not self.items or row is None or not (0 <= row < len(self.items)):
            return None
        return row + 1

    def mutate(self, action: str, **kw) -> None:
        try:
            M.apply_mutation(self.cfg, action, self.selector, **kw)
        except (ValueError, OSError) as e:
            self.query_one("#questions-status", Static).update(f"[red]{escape(str(e))}[/]")
            self.refresh_items()
            return
        self.dirty = True
        self.refresh_items()

    # --- actions ---------------------------------------------------------------------

    @on(DataTable.RowSelected, "#questions")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_toggle()

    def action_cursor_down(self) -> None:
        self.query_one("#questions", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#questions", DataTable).action_cursor_up()

    def action_toggle(self) -> None:
        i = self.index
        if i is None:
            return
        resolved = (self.items[i - 1].get("state") or "").lower() == "resolved"
        self.mutate("question_unresolve" if resolved else "question_resolve", index=i)

    def action_add(self) -> None:
        def done(text):
            if text and text.strip():
                self.mutate("question_add", text=text.strip())
        self.app.push_screen(TextPrompt("new question"), done)

    def action_edit(self) -> None:
        i = self.index
        if i is None:
            return

        def done(text):
            if text and text.strip():
                self.mutate("question_edit", index=i, text=text.strip())
        self.app.push_screen(TextPrompt(f"edit question #{i}",
                                        self.items[i - 1].get("text") or ""), done)

    def action_delete(self) -> None:
        i = self.index
        if i is not None:
            self.mutate("question_delete", index=i)

    def action_rationale(self) -> None:
        """Write or edit the *why* (SPEC-0112). Deliberately not folded into `enter`: bulk
        triage must not pay a prompt per resolve, and this also reaches a question that was
        resolved days ago."""
        i = self.index
        if i is None:
            return
        current = (self.items[i - 1].get("body") or "")

        def done(text):
            if text is not None:
                self.mutate("question_body", index=i, text=text)
        self.app.push_screen(BodyPrompt(f"rationale · question #{i}", current), done)

    def action_close(self) -> None:
        self.dismiss(self.dirty)


class ChoiceScreen(ModalScreen[str | None]):
    """Pick one value from a short closed list (kind / priority / workstream / close outcome).

    One screen for every enumerated field instead of a binding per value: the desk stays a
    two-pane browser, and adding a field later costs a call site, not more chrome.
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "down", show=False),
        Binding("k,up", "cursor_up", "up", show=False),
        Binding("escape,q", "cancel", "cancel", show=False),
    ]

    def __init__(self, title: str, choices: list[str], current: str = ""):
        super().__init__()
        self.title_text = title
        self.choices = choices
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(f"{self.title_text}  (enter pick · esc cancel)", markup=False)
            yield DataTable(id="choices", cursor_type="row", show_header=False)

    def on_mount(self) -> None:
        table = self.query_one("#choices", DataTable)
        table.add_columns("value")
        for c in self.choices:
            table.add_row(Text(c, style="bold" if c == self.current else ""))
        table.focus()
        if self.current in self.choices:
            table.move_cursor(row=self.choices.index(self.current))

    @on(DataTable.RowSelected, "#choices")
    def _picked(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        self.dismiss(self.choices[row] if 0 <= row < len(self.choices) else None)

    def action_cursor_down(self) -> None:
        self.query_one("#choices", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#choices", DataTable).action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss(None)


class IdeaDesk(App):
    """Ideas board (SPEC-0490): project rail · Open / In flight / Built buckets · detail, plus
    the explore mutations (SPEC-0102) and metadata + clock (SPEC-0103) on the selected idea."""

    TITLE = "nightwright — ideas desk"
    # Substitution, not %-formatting: CSS is full of literal `%` (and so are comments about
    # it), which %-formatting reads as conversion specs — that broke the module twice.
    CSS = """
    #panes { height: 1fr; }
    #hub { height: 1fr; }
    #hub-status { color: $text-muted; padding: 0 1; }
    #queue-view { height: 1fr; }
    #queue { height: 1fr; }
    #queue-status { color: $text-muted; padding: 0 1; }
    #rail { width: RAIL_W; height: 1fr; border: none; border-right: solid $panel; }
    #board-pane { width: 1fr; }
    #scoreboard { height: auto; padding: 0 1; display: none; }
    #scoreboard.visible { display: block; }
    #board { height: 1fr; }
    #status { color: $text-muted; padding: 0 1; }
    /* The bottom strip is explicit. Left in flow, `#tabs` at 1fr ate every remaining row and
       the search box, hint and Footer all landed on the same line with Footer painted on top —
       the box collected keystrokes no terminal ever showed. An Input is also 3 rows tall by
       default, hence the explicit height/border reset. */
    #tabs { height: 1fr; }
    #search { display: none; height: 1; border: none; padding: 0 1; background: $panel; }
    #search.visible { display: block; }
    #detail-pane { width: DETAIL_W; border-left: solid $panel; }
    #detail { padding: 0 1; }
    #hint { color: $text-muted; padding: 0 1; height: 1; }
    TextPrompt, BodyPrompt { align: center middle; }
    #prompt-box {
        width: 80%; height: auto; padding: 1 2;
        background: $surface; border: solid $panel;
    }
    #prompt-body { height: 12; }
    QuestionsScreen { align: center middle; }
    /* auto-height so a two-question idea does not open a mostly-empty tall box */
    #questions-box {
        width: 80%; height: auto; max-height: 80%; padding: 1 2;
        background: $surface; border: solid $panel;
    }
    #questions { height: auto; max-height: 20; }
    #questions-status { color: $text-muted; }
    """.replace("RAIL_W", str(RAIL_WIDTH)).replace("DETAIL_W", DETAIL_WIDTH)

    BINDINGS = [
        Binding("j,down", "cursor_down", "down", show=False),
        Binding("k,up", "cursor_up", "up", show=False),
        Binding("slash", "search", "search"),
        # SPEC-0490: the rail replaces SPEC-0110's filter bar / sort keys / wide mode and
        # SPEC-0115's tree mode; `[`/`]` walk it from the board without moving focus.
        Binding("left_square_bracket", "project_prev", "prev project", show=False),
        Binding("right_square_bracket", "project_next", "next project", show=False),
        Binding("d", "toggle_dropped", "dropped"),
        # A 250-idea OPEN bucket pushes IN FLIGHT off screen: `{`/`}` hop between bucket
        # headers (space folds the one under the cursor).
        Binding("left_curly_bracket", "bucket_prev", "prev bucket", show=False),
        Binding("right_curly_bracket", "bucket_next", "next bucket", show=False),
        Binding("r", "refresh_desk", "refresh", show=False),
        # SPEC-0102 mutations
        Binding("l", "log", "log"),
        Binding("s", "summary", "summary"),
        Binding("question_mark", "questions", "questions"),
        Binding("c", "capture", "capture"),
        Binding("t", "retitle", "retitle", show=False),
        Binding("x", "explored", "explored", show=False),
        # SPEC-0103 metadata + clock
        Binding("m", "metadata", "meta"),
        Binding("h", "toggle_hub", "hub"),
        # SPEC-0241: number keys select tabs; later agentic tabs extend the digits.
        Binding("1", "tab('tab-ideas')", "ideas", show=False),
        Binding("2", "tab('tab-hub')", "hub", show=False),
        Binding("3", "tab('tab-queue')", "queue", show=False),
        Binding("5", "tab('tab-events')", "events", show=False),  # SPEC-0251
        Binding("4", "tab('tab-mailbox')", "mailbox", show=False),
        Binding("i", "clock_toggle", "clock"),
        Binding("X", "close_idea", "close", show=False),
        Binding("q", "quit", "quit"),
        Binding("escape", "clear_bars", "clear", show=False),
    ]

    def __init__(self, cfg, *, all_done=False, query=None, filters=None, tab="ideas",
                 campaign=None):
        super().__init__()
        self.cfg = cfg
        self.campaign = campaign           # SPEC-0246: open this campaign's screen on mount
        self.tab = "queue" if campaign else (tab or "ideas")
        self._active_tab = "tab-ideas"
        self.query = query or ""
        opening = dict(filters or {})
        # SPEC-0490: bucket order is fixed by design, so `--sort/--desc` no longer apply;
        # `--project` selects a rail entry; the rest narrow what the board loads.
        opening.pop("sort", None)
        opening.pop("desc", None)
        self._hub_filters = {k: v for k, v in opening.items() if v}
        project = (opening.pop("project", None) or "").strip()
        self.filters = {k: v for k, v in opening.items() if v}
        self.project = project or B.ALL
        self.show_dropped = bool(all_done)      # `--all` opens with DROPPED revealed
        self._board = B.Board()
        self._pairs: list[tuple[M.DeskRow, object]] = []
        self._hits: list[M.DeskRow] = []
        self._rail_keys: list[str] = []
        # Fold state per bucket survives repaints: BUILT is folded until the user opens it.
        self._expanded = {B.OPEN: True, B.IN_FLIGHT: True, B.BUILT: False, B.DROPPED: True}
        self._hub_lines: list[M.HubLine] = []
        self._search_timer = None
        self._shown_id: str | None = None
        self._org_signature = None          # SPEC-0128: external-change poll baseline
        self._reload_in_flight = False      # SPEC-0129: guards overlapping poll reloads
        self._search_in_flight = False

    # --- layout ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs", initial=f"tab-{self.tab}"):
            with TabPane("ideas", id="tab-ideas"):
                with Horizontal(id="panes"):
                    yield OptionList(id="rail")
                    with Vertical(id="board-pane"):
                        yield Static("", id="scoreboard")
                        yield Tree("ideas", id="board")
                        yield Static("", id="status")
                    with VerticalScroll(id="detail-pane"):
                        yield Static("", id="detail", markup=True)
            # SPEC-0104: read-only triage over the same `hub_payload` agents get.
            with TabPane("hub", id="tab-hub"):
                yield DataTable(id="hub", cursor_type="row")
                yield Static("", id="hub-status")
            # SPEC-0241: live view of the agentic queue (nw.queue.dashboard.v1).
            with TabPane("queue", id="tab-queue"):
                yield QueueView(self.cfg, id="queue-view")
            # SPEC-0251: event stream, watchdog conditions, ping digest, followers.
            with TabPane("events", id="tab-events"):
                yield EventsView(self.cfg, id="events-view")
            # SPEC-0249: inboxes, threads and the human's replies.
            with TabPane("mailbox", id="tab-mailbox"):
                yield MailboxView(self.cfg, id="mailbox-view")
        yield Input(placeholder="search all projects…  (enter: browse hits · esc: clear)",
                    id="search")
        yield Static("", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#board", Tree)
        tree.show_root = False
        tree.guide_depth = 2
        if self.tab == "ideas":
            tree.focus()        # focusing a hidden pane's widget would switch back to its tab
        self.call_after_refresh(self.reload)
        self.set_interval(1.5, self._check_external_changes)   # SPEC-0128
        if self.campaign:
            self.open_campaign_detail(self.campaign)

    def on_resize(self, event) -> None:
        if self._hub_lines:
            self.load_hub()

    # --- data --------------------------------------------------------------------------

    def _load(self):
        """Board + (when a query is active) search hits. Runs off the event loop."""
        board = B.load_board(self.cfg, filters=self.filters)
        hits = self._load_hits(self.query) if self.query else []
        return board, hits

    def _load_hits(self, query: str) -> list[M.DeskRow]:
        """Ranked hits across every project (SPEC-0086 searcher; never the board's own)."""
        return [row for row, _t in M.load_rows(self.cfg, query=query, **self.filters)]

    def _adopt(self, board, hits) -> None:
        self._board = board
        self._pairs = [(i.row, i.task) for i in board.items]
        self._hits = hits

    def reload(self, *, keep_id: str | None = None) -> None:
        """Re-read org + specs, then repaint (SPEC-0134: non-blocking when a loop is running)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._reload_blocking(keep_id=keep_id)
            return
        self.run_worker(self._reload_async(keep_id=keep_id), name="desk-reload")

    def _reload_blocking(self, *, keep_id: str | None = None) -> None:
        keep_id = keep_id or self.selected_id
        try:
            self._adopt(*self._load())
        except (ValueError, OSError) as e:                 # unreadable org / bad filter
            self._adopt(B.Board(), [])
            self.set_hint(f"load failed: {e}")
        self.paint(keep_id=keep_id)
        from ..org import org_files_mtime_signature
        self._org_signature = org_files_mtime_signature(self.cfg)

    async def _reload_async(self, *, keep_id: str | None = None) -> None:
        """Load off-thread, then paint on the event-loop thread (SPEC-0129/0134)."""
        if self._reload_in_flight:
            return
        self._reload_in_flight = True
        try:
            keep_id = keep_id or self.selected_id
            loop = asyncio.get_running_loop()
            try:
                board, hits = await loop.run_in_executor(None, self._load)
            except (ValueError, OSError) as e:
                self._adopt(B.Board(), [])
                self.set_hint(f"load failed: {e}")
                self.paint(keep_id=keep_id)
                return
            self._adopt(board, hits)
            self.paint(keep_id=keep_id)
            from ..org import org_files_mtime_signature
            self._org_signature = org_files_mtime_signature(self.cfg)
        finally:
            self._reload_in_flight = False

    def refresh_search(self) -> None:
        """Re-run only the search (the board is already loaded), off the event loop."""
        if not self.query:
            self._hits = []
            self.paint()
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._hits = self._load_hits(self.query)
            self.paint()
            return
        self.run_worker(self._search_async(self.query), name="desk-search")

    async def _search_async(self, query: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            hits = await loop.run_in_executor(None, self._load_hits, query)
        except (ValueError, OSError) as e:
            hits = []
            self.set_hint(f"search failed: {e}")
        if query != self.query:             # a newer query (or Esc) overtook this one
            return
        self._hits = hits
        self.paint()

    def _resolve_fresh(self, idea_id: str):
        """Re-resolve a Task fresh from disk by id (SPEC-0128) — never reuse a cached Task,
        since an external edit since the last `reload()` can shift its line number or state
        out from under it, tripping `explore._idea_span`'s drift guard."""
        from ..org_write import resolve_selector
        return resolve_selector(self.cfg, idea_id)

    async def _check_external_changes(self) -> None:
        """Poll (SPEC-0128): cheap `stat()`-only signature compare, no reparse unless
        something actually changed; the reparse runs off the loop (SPEC-0129/0134)."""
        from textual.css.query import NoMatches

        from ..org import org_files_mtime_signature
        sig = org_files_mtime_signature(self.cfg)
        if sig == self._org_signature or self._reload_in_flight:
            return
        try:
            watched_id = self.selected_id
            old = self._board.by_id.get(watched_id) if watched_id else None
            old = (old.row.heading, old.row.state) if old else None
            await self._reload_async(keep_id=watched_id)
        except NoMatches:       # the timer fired while the app tears its DOM down: nothing to do
            return
        new = self._board.by_id.get(watched_id) if watched_id else None
        if old and new and old != (new.row.heading, new.row.state):
            self.set_hint(f"↻ {new.id} changed externally")

    # --- painting ----------------------------------------------------------------------

    def paint(self, *, keep_id: str | None = None, keep: bool = True) -> None:
        """Render the loaded board into rail, main pane and detail. No org reads.

        With `keep`, the cursor stays on `keep_id` (default: the selected idea) — following it
        into its project when it moved (a capture into another project) — or, when that idea
        left the view, near the line it was on. `keep=False` starts at the first idea."""
        tree = self.query_one("#board", Tree)
        if keep:
            keep_id = keep_id or self.selected_id
        prev_line = tree.cursor_line if keep else -1
        item = self._board.by_id.get(keep_id) if keep_id else None
        if item is not None and not self.query and self.project not in (B.ALL, item.project_key):
            self.project = item.project_key          # e.g. capture into another project
        if self._board.entry(self.project) is None and self._board.rail:
            if self.project == B.NO_PROJECT:
                self.project = B.ALL
            else:
                # `--project P` with no ideas yet (or one just emptied): keep it on the rail
                # with zero counts, so capture still pre-selects it.
                self._board.rail.append(B.RailEntry(self.project, B.project_label(self.project)))
        if item is not None and item.project_key == self.project:
            self._expanded[item.bucket] = True       # never strand the cursor in a fold
        self.paint_rail()
        scoreboard = self.query_one("#scoreboard", Static)
        on_all = self.project == B.ALL and not self.query
        scoreboard.set_class(on_all, "visible")
        if on_all:
            scoreboard.update(self._scoreboard_table())

        tree.clear()
        if self.query:
            self._paint_hits(tree)
        elif self.project == B.ALL:
            self._paint_activity(tree)
        else:
            self._paint_buckets(tree)
        self.query_one("#status", Static).update(escape(self._status_text()))
        _ = tree.last_line          # build the line cache so `node._line` is valid below

        target = self._find_leaf(tree.root, keep_id) if keep_id else None
        if target is None:
            leaves = self._leaves(tree.root)
            if not leaves:
                tree.move_cursor(None)
                self._shown_id = None
                self.query_one("#detail", Static).update(
                    "[dim]no ideas here[/]" if not self.query else "[dim]no hits[/]")
                self.set_hint("")
                return
            # A fresh view, or a search (its hits are a new list), starts on the first idea.
            fresh = prev_line is None or prev_line < 0 or bool(self.query)
            target = leaves[0] if fresh else None
        if target is not None:
            tree.move_cursor(target)
            self.show_detail(target.data)
        else:
            # The kept idea left this view (closed/dropped): stay near where the cursor was.
            tree.cursor_line = max(0, min(prev_line, tree.last_line))
            node = tree.cursor_node
            if node is not None and isinstance(node.data, str):
                self.show_detail(node.data)

    def paint_rail(self) -> None:
        rail = self.query_one("#rail", OptionList)
        keys = [e.key for e in self._board.rail]
        width = RAIL_WIDTH - 3
        prompts = []
        for e in self._board.rail:
            counts = e.counts
            label = R._clip(e.label, max(4, width - len(counts) - 1))
            text = Text(label, style="bold" if e.key == B.ALL else "")
            text.append(" " * max(1, width - len(label) - len(counts)))
            text.append(f"{e.open}", style="yellow" if e.open else "dim")
            text.append("·", style="dim")
            text.append(f"{e.in_flight}", style="cyan" if e.in_flight else "dim")
            prompts.append(text)
        if keys != self._rail_keys or rail.option_count != len(keys):
            rail.clear_options()
            rail.add_options([Option(p, id=f"rail-{i}") for i, p in enumerate(prompts)])
            self._rail_keys = keys
        else:
            for i, p in enumerate(prompts):
                rail.replace_option_prompt_at_index(i, p)
        if self.project in keys:
            index = keys.index(self.project)
            if rail.highlighted != index:
                rail.highlighted = index

    def _scoreboard_table(self):
        from rich.table import Table

        table = Table(box=None, padding=(0, 1), show_edge=False, header_style="bold dim",
                      expand=False)
        table.add_column("PROJECT")
        for col in ("OPEN", "IN FLIGHT", "BUILT"):
            table.add_column(col, justify="right")
        table.add_column(f"LAST {B.MOVEMENT_DAYS}D")
        rows = self._board.scoreboard
        for row in rows:
            total = row.key == B.ALL
            style = "bold" if total else ""
            table.add_row(Text(row.label, style=style or "green"),
                          Text(str(row.open), style="yellow" if row.open else "dim"),
                          Text(str(row.in_flight), style="cyan" if row.in_flight else "dim"),
                          Text(str(row.built), style="dim"),
                          Text(row.movement, style="dim" if row.movement == "—" else ""),
                          end_section=(rows.index(row) == len(rows) - 2))
        return table

    def _paint_activity(self, tree) -> None:
        feed = tree.root.add(Text(f"RECENT ACTIVITY  ({len(self._board.activity)} newest)",
                                  style="bold"), data=("section", "activity"), expand=True)
        style = {"captured": "", "specced": "cyan", "built": "green", "updated": "dim"}
        for a in self._board.activity:
            label = Text()
            label.append(a.when.strftime("%Y-%m-%d") + "  ", style="dim")
            label.append(f"{a.text:<20.20} ", style=style[a.kind])
            label.append(f"{a.item.id:<10}", style="dim")
            label.append(R._clip(a.item.row.heading, 60) + "  ")
            label.append(B.project_label(a.item.project_key), style="green dim")
            feed.add_leaf(label, data=a.item.id)

    def _paint_buckets(self, tree) -> None:
        for bucket, items in self._board.buckets(self.project, show_dropped=self.show_dropped):
            head = Text(f"{B.BUCKET_LABELS[bucket]}  ({len(items)})",
                        style="bold " + _BUCKET_STYLE[bucket])
            node = tree.root.add(head, data=("bucket", bucket),
                                 expand=self._expanded.get(bucket, True),
                                 allow_expand=bool(items))
            spec_w = min(34, max((len(i.spec_label) for i in items), default=0))
            for item in items:
                node.add_leaf(self._item_label(item, spec_w), data=item.id)

    def _item_label(self, item, spec_w: int) -> Text:
        """One idea line: id, state, [spec + status + roll-up], heading. The spec sits before
        the heading so a narrow pane crops the prose, never the status."""
        row = item.row
        text = Text()
        text.append("◕ " if row.clock_open else "  ", style="cyan")
        text.append(f"{row.id:<10}", style="dim")
        text.append(f"{(row.state or '·'):<10} ", style=row.state_style or "")
        if item.bucket == B.OPEN:
            text.append(f"[#{row.priority}] " if row.priority else "     ", style="bold")
            text.append(f"{row.open_questions}q " if row.open_questions else "   ",
                        style="yellow")
        else:
            status = item.link.status if item.link else ""
            text.append(f"{item.spec_label:<{spec_w}.{spec_w}} ",
                        style=_STATUS_STYLE.get(status, "red" if not item.link else ""))
            if item.rollup_label:
                text.append(f"{item.rollup_label} ", style="magenta")
            if item.bucket == B.BUILT and item.built_at is not None:
                text.append(item.built_at.strftime("%Y-%m-%d") + " ", style="dim")
        text.append(row.heading or "", style="dim" if item.bucket == B.DROPPED else "")
        return text

    def _paint_hits(self, tree) -> None:
        hits = [h for h in self._hits if h.id in self._board.by_id]
        node = tree.root.add(Text(f"SEARCH {self.query!r}  ({len(hits)} hits, all projects)",
                                  style="bold"), data=("section", "search"), expand=True)
        for row in hits:
            item = self._board.by_id[row.id]
            label = Text()
            label.append(f"{row.id:<10}", style="dim")
            label.append(f"{B.BUCKET_LABELS[item.bucket]:<10}",
                         style=_BUCKET_STYLE[item.bucket])
            label.append(f"{B.project_label(item.project_key):<18.18}", style="green")
            label.append_text(self._marked(row.heading or ""))
            node.add_leaf(label, data=row.id)

    def _status_text(self) -> str:
        parts = []
        if self.query:
            parts.append(f"{len(self._hits)} hit(s) for {self.query!r} across all projects")
            parts.append("enter on a hit jumps to it · esc clears")
        else:
            e = self._board.entry(self.project)
            if e is not None:
                parts.append("all projects" if self.project == B.ALL else e.label)
                parts.append(f"{e.open} open · {e.in_flight} in flight · {e.built} built")
                if self.project != B.ALL:
                    parts.append(f"{e.dropped} dropped " +
                                 ("shown (d hides)" if self.show_dropped else "hidden (d shows)"))
        if self.filters:
            parts.append(" ".join(f"{k}={v}" for k, v in self.filters.items()))
        return " · ".join(parts)

    @staticmethod
    def _leaves(root) -> list:
        out = []
        for child in root.children:
            if isinstance(child.data, str):
                out.append(child)
            elif child.is_expanded:
                out.extend(c for c in child.children if isinstance(c.data, str))
        return out

    @staticmethod
    def _find_leaf(root, idea_id):
        for child in root.children:
            if child.data == idea_id:
                return child
            for leaf in child.children:
                if leaf.data == idea_id and child.is_expanded:
                    return leaf
        return None

    @property
    def selected_id(self) -> str | None:
        node = self.query_one("#board", Tree).cursor_node
        data = getattr(node, "data", None) if node is not None else None
        return data if isinstance(data, str) else None

    def show_detail(self, idea_id: str | None) -> None:
        item = self._board.by_id.get(idea_id) if idea_id else None
        if item is None:
            return
        try:
            task = self._resolve_fresh(item.id)        # SPEC-0128: never read via a stale Task
            detail = M.load_detail(self.cfg, task)
        except (ValueError, OSError) as e:
            self.query_one("#detail", Static).update(
                f"[red]could not read idea:[/] {escape(str(e))}")
            return
        self._shown_id = item.id
        width = self.query_one("#detail-pane", VerticalScroll).size.width
        snippet = next((h.snippet for h in self._hits if h.id == item.id), "")
        body = _render_detail(detail, snippet=snippet, width=width, theme=self.syntax_theme,
                              spec=_spec_lines(item))
        self._mark_into(body)
        self.query_one("#detail", Static).update(body)
        self.query_one("#detail-pane", VerticalScroll).scroll_home(animate=False)
        self.set_hint(f"› next: {detail.next_hint}" if detail.next_hint else "")

    # --- search-term highlighting (SPEC-0106) -------------------------------------------

    @property
    def syntax_theme(self) -> str:
        """ANSI syntax theme matching the app's light/dark state (SPEC-0107)."""
        try:
            dark = bool(getattr(self.current_theme, "dark", True))
        except Exception:                       # theme not resolved yet (pre-mount)
            dark = True
        return ANSI_DARK if dark else ANSI_LIGHT

    def _mark_into(self, text: Text) -> int:
        """Mark the active query's tokens in `text` in place; returns the number of hits.

        `Text.highlight_words` is case-insensitive substring matching — exactly SPEC-0086's
        rule — so the marks show precisely what the search matched. No query means no marks.
        """
        tokens = M.search_tokens(self.query)
        if not tokens:
            return 0
        return text.highlight_words(tokens, MATCH_STYLE, case_sensitive=False)

    def _marked(self, plain: str, *, style: str = "") -> Text:
        """A `Text` for `plain` with the active query marked."""
        text = Text(plain, style=style)
        self._mark_into(text)
        return text

    def set_hint(self, text: str) -> None:
        self.query_one("#hint", Static).update(escape(text))

    # --- events ------------------------------------------------------------------------

    @on(Tree.NodeHighlighted, "#board")
    def _board_highlighted(self, event) -> None:
        data = getattr(event.node, "data", None)
        if isinstance(data, str) and data != self._shown_id:
            self.show_detail(data)

    @on(Tree.NodeSelected, "#board")
    def _board_selected(self, event) -> None:
        """Enter on a search hit jumps to it in its project (SPEC-0490)."""
        data = getattr(event.node, "data", None)
        if self.query and isinstance(data, str):
            self.jump_to(data)

    @on(Tree.NodeExpanded, "#board")
    @on(Tree.NodeCollapsed, "#board")
    def _fold_changed(self, event) -> None:
        data = getattr(event.node, "data", None)
        if isinstance(data, tuple) and data[0] == "bucket":
            self._expanded[data[1]] = event.node.is_expanded

    @on(OptionList.OptionHighlighted, "#rail")
    def _rail_highlighted(self, event) -> None:
        # Only the user's own rail moves (keys or a click — both focus the rail) select a
        # project. `paint_rail` sets `highlighted` itself, and that queued echo must not land
        # after a newer `select_project` / `jump_to` and drag the view back.
        if not self.query_one("#rail", OptionList).has_focus:
            return
        index = event.option_index
        if 0 <= index < len(self._rail_keys):
            self.select_project(self._rail_keys[index])

    @on(Input.Submitted, "#search")
    def _search_submitted(self, event: Input.Submitted) -> None:
        """Enter closes the box and hands the hit list to the cursor; Enter on a hit jumps."""
        self._cancel_search_timer()
        wanted = event.value.strip()
        self._hide_search()
        if wanted != self.query:
            self.query = wanted
            self.refresh_search()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        """Search as you type, debounced (SPEC-0105). Clearing the box restores the board."""
        self._cancel_search_timer()
        wanted = event.value.strip()

        def apply() -> None:
            self._search_timer = None
            if wanted != self.query:
                self.query = wanted
                self.refresh_search()

        self._search_timer = self.set_timer(SEARCH_DEBOUNCE, apply)

    def _cancel_search_timer(self) -> None:
        if getattr(self, "_search_timer", None) is not None:
            self._search_timer.stop()
            self._search_timer = None

    # --- actions -----------------------------------------------------------------------

    def _cursor_widget(self):
        """j/k move whichever of rail / board has focus (the board otherwise)."""
        rail = self.query_one("#rail", OptionList)
        return rail if rail.has_focus else self.query_one("#board", Tree)

    def action_cursor_down(self) -> None:
        self._cursor_widget().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._cursor_widget().action_cursor_up()

    def select_project(self, key: str) -> None:
        """Show one rail entry. A pure repaint: the board already holds every project."""
        if key == self.project:
            return
        if self.query:
            self._clear_query()
        self.project = key
        self.paint(keep=False)

    def _step_project(self, delta: int) -> None:
        keys = self._rail_keys
        if not keys:
            return
        index = keys.index(self.project) if self.project in keys else 0
        self.select_project(keys[(index + delta) % len(keys)])

    def action_project_prev(self) -> None:
        self._step_project(-1)

    def action_project_next(self) -> None:
        self._step_project(1)

    def _hop_bucket(self, delta: int) -> None:
        tree = self.query_one("#board", Tree)
        heads = list(tree.root.children)
        if not heads:
            return
        node = tree.cursor_node
        current = node if node in heads else (node.parent if node is not None else None)
        index = heads.index(current) if current in heads else -1
        target = heads[(index + delta) % len(heads)] if index >= 0 else heads[0]
        tree.move_cursor(target)
        tree.scroll_to_node(target, animate=False)

    def action_bucket_prev(self) -> None:
        self._hop_bucket(-1)

    def action_bucket_next(self) -> None:
        self._hop_bucket(1)

    def action_toggle_dropped(self) -> None:
        self.show_dropped = not self.show_dropped
        self.paint()

    def jump_to(self, idea_id: str) -> None:
        """Select `idea_id` in its own project's bucket, unfolding (or revealing) it."""
        item = self._board.by_id.get(idea_id)
        if item is None:
            self.notify(f"{idea_id} is not on the board", severity="warning")
            return
        self._clear_query()
        self.project = item.project_key
        if item.bucket == B.DROPPED:
            self.show_dropped = True
        self._expanded[item.bucket] = True
        self.paint(keep_id=idea_id)
        self.query_one("#board", Tree).focus()

    def _clear_query(self) -> None:
        self._cancel_search_timer()
        self.query = ""
        self._hits = []
        box = self.query_one("#search", Input)
        box.remove_class("visible")
        box.value = ""

    def action_search(self) -> None:
        box = self.query_one("#search", Input)
        box.add_class("visible")
        box.value = self.query
        box.focus()

    def action_clear_bars(self) -> None:
        self.action_clear_search()

    def action_clear_search(self) -> None:
        """Esc: close the box; if a query was applied, drop it and show the board again."""
        self._cancel_search_timer()
        self._hide_search()
        if self.query:
            self.query = ""
            self._hits = []
            self.paint()

    def action_refresh_desk(self) -> None:
        self.reload()

    # --- mutations (SPEC-0102) ---------------------------------------------------------

    def mutate(self, action: str, **kw) -> None:
        """Run one library mutation on the selected idea, then always refresh.

        Drift (`ValueError` from the writers' expected-state guard) is non-fatal: the message
        is shown and the desk reloads from disk, which is exactly the recovery the user needs.
        """
        selector = self.selected_id
        if not selector:
            return
        try:
            M.apply_mutation(self.cfg, action, selector, **kw)
        except (ValueError, OSError) as e:
            self.notify(str(e), severity="error", title="write failed")
            self.reload(keep_id=selector)          # re-read: the file may have moved under us
            return
        self.reload(keep_id=selector)

    def action_log(self) -> None:
        def done(note):
            if note and note.strip():
                self.mutate("log", note=note.strip())
        self.push_screen(TextPrompt("log note"), done)

    def action_summary(self) -> None:
        selector = self.selected_id
        if not selector:
            return
        current = ""
        try:
            current = M.load_detail(self.cfg, self._resolve_fresh(selector)).summary
        except (ValueError, OSError):
            current = ""

        def done(text):
            if text is not None:
                self.mutate("summary", text=text)
        self.push_screen(BodyPrompt(f"summary · {selector}", current), done)

    def _selected_row(self):
        item = self._board.by_id.get(self.selected_id or "")
        return item.row if item is not None else None

    def action_retitle(self) -> None:
        selector = self.selected_id
        if not selector:
            return
        row = self._selected_row()
        current = row.heading if row is not None else ""

        def done(title):
            if title and title.strip():
                self.mutate("retitle", title=title.strip())
        self.push_screen(TextPrompt(f"retitle · {selector}", current), done)

    def action_explored(self) -> None:
        self.mutate("explored")

    def action_questions(self) -> None:
        selector = self.selected_id
        if not selector:
            return

        def done(dirty):
            if dirty:
                self.reload(keep_id=selector)
        self.push_screen(QuestionsScreen(self.cfg, selector), done)

    # --- hub tab (SPEC-0104) -------------------------------------------------------------

    def action_toggle_hub(self) -> None:
        """Switch tabs and nothing else (SPEC-0108).

        Loading and focus deliberately do **not** live here. They hang off `TabActivated`, so
        this binding, a mouse click and Textual's own tab navigation all converge on one path
        and cannot drift apart — which is exactly how the click route came to show a blank pane.
        """
        tabs = self.query_one("#tabs", TabbedContent)
        tabs.active = "tab-ideas" if tabs.active == "tab-hub" else "tab-hub"

    def action_tab(self, name: str) -> None:
        """Select a tab by id (`1`..`5`); loading and focus hang off `TabActivated`."""
        if isinstance(self.screen, CampaignDetail) and name in self._DESK_TAB_KEYS:
            self.screen.action_tab(self._DESK_TAB_KEYS[name])
            return
        self.query_one("#tabs", TabbedContent).active = name

    # Idea-scoped actions act on the selected idea of the ideas list, which is hidden on any
    # other tab. Disabled there so e.g. `x` on the queue tab cannot change org state.
    _IDEA_ACTIONS = frozenset({
        "search", "project_prev", "project_next", "bucket_prev", "bucket_next", "toggle_dropped", "refresh_desk", "log",
        "summary", "questions", "capture", "retitle", "explored", "metadata", "clock_toggle",
        "close_idea"})

    _DESK_TAB_KEYS = {
        "tab-ideas": "tab-overview",
        "tab-hub": "tab-timeline",
        "tab-queue": "tab-runs",
        "tab-mailbox": "tab-records",
        "tab-events": "tab-mail",
    }

    def _campaign_detail_on_stack(self) -> bool:
        return any(isinstance(screen, CampaignDetail) for screen in self.screen_stack)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        on_detail = self._campaign_detail_on_stack()
        if action in self._IDEA_ACTIONS and (
                self._active_tab != "tab-ideas" or on_detail):
            return False
        return True

    @on(TabbedContent.TabActivated, "#tabs")
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Load + focus for whichever tab just became active, by whatever route."""
        active = self.query_one("#tabs", TabbedContent).active
        self._active_tab = active
        queue = self.query_one("#queue-view", QueueView)
        events = self.query_one("#events-view", EventsView)
        mailbox = self.query_one("#mailbox-view", MailboxView)
        if active == "tab-queue":
            events.deactivate()
            mailbox.deactivate()
            queue.activate()
            return
        queue.deactivate()
        if active == "tab-events":
            mailbox.deactivate()
            events.activate()
            return
        if active == "tab-mailbox":
            events.deactivate()
            mailbox.activate()
            return
        events.deactivate()
        mailbox.deactivate()
        if active == "tab-hub":
            # Reload every activation: the hub is triage over ideas/specs/outbox that this very
            # desk mutates, so a stale hub is worse than a cheap re-read. load_hub never writes.
            self.load_hub()
            self.query_one("#hub", DataTable).focus()
        else:
            self.query_one("#board", Tree).focus()

    @on(OpenCampaign)
    def _open_campaign(self, event: OpenCampaign) -> None:
        self.open_campaign_detail(event.campaign_id, focus=event.focus, origin=event.origin)

    @on(QueueView.CampaignSelected)
    def _campaign_selected(self, event: QueueView.CampaignSelected) -> None:
        """Legacy queue message: same modal as `OpenCampaign` (SPEC-0407)."""
        queue = self.query_one("#queue-view", QueueView)
        self.open_campaign_detail(event.campaign_id, origin=queue)

    def open_campaign_detail(self, cid, focus=None, origin=None) -> None:
        """Push `CampaignDetail` and restore the opener's queue cursor on dismiss."""

        def done(_result) -> None:
            if origin is not None and hasattr(origin, "select_campaign"):
                origin.select_campaign(cid)

        self.push_screen(CampaignDetail(self.cfg, cid, focus=focus), done)

    def load_hub(self) -> None:
        """Fill the hub tab from `hub_payload`. Read-only: no writer is reachable from here."""
        table = self.query_one("#hub", DataTable)
        if not table.columns:
            table.add_columns("section", "id", "item", "meta")
        try:
            self._hub_lines, summary = M.load_hub(self.cfg, **self.filters_for_hub())
        except (ValueError, OSError) as e:
            self._hub_lines, summary = [], f"hub failed: {e}"
        table.clear()
        # Same measured-budget rule as the ideas list: unclipped titles push `meta` clean off
        # the right edge, which silently hides the column rather than shrinking it.
        meta_w = 30
        item_w = max(20, (table.size.width or 100) - _HUB_META_COST - meta_w)
        last_section = None
        for line in self._hub_lines:
            table.add_row(
                Text(line.section if line.section != last_section else "", style="dim"),
                line.left,
                R._clip(line.text, item_w),
                Text(R._clip(line.meta, meta_w), style="dim"),
            )
            last_section = line.section
        self.query_one("#hub-status", Static).update(f"[dim]{escape(summary)}[/]")

    def filters_for_hub(self) -> dict:
        """Hub takes the same filters the desk was opened with (`--project` included), minus
        the retired sort keys."""
        return dict(self._hub_filters)

    @on(DataTable.RowSelected, "#hub")
    def _hub_row_selected(self, event: DataTable.RowSelected) -> None:
        """Drill an idea row into the ideas desk; a spec row only offers a CLI hint."""
        row = event.cursor_row
        if not (0 <= row < len(self._hub_lines)):
            return
        line = self._hub_lines[row]
        if line.idea_id:
            self.query_one("#tabs", TabbedContent).active = "tab-ideas"
            self.query_one("#board", Tree).focus()
            if line.idea_id in self._board.by_id:
                self.jump_to(line.idea_id)      # reveals a fold / DROPPED when it has to
            else:
                self.reload(keep_id=line.idea_id)
        elif line.hint:
            self.notify(line.hint, title="run this in a shell")

    # --- metadata + clock (SPEC-0103) ---------------------------------------------------

    def _selected_detail(self):
        selected = self.selected_id
        if selected is None:
            return None
        try:
            return M.load_detail(self.cfg, self._resolve_fresh(selected))   # SPEC-0128
        except (ValueError, OSError):
            return None

    def action_metadata(self) -> None:
        """Pick a field, then a value — two short lists beat six bindings."""
        from ..org_write import IDEA_KINDS, configured_workstreams

        if not self.selected_id:
            return
        fields = ["state", "kind", "priority", "project", "workstream", "ext"]

        def field_picked(field):
            if not field:
                return
            if field == "ext":                                       # SPEC-0122
                selected = self.selected_id
                if selected is None:
                    return
                try:
                    task = self._resolve_fresh(selected)             # SPEC-0128
                except (ValueError, OSError) as e:
                    self.notify(f"could not read idea: {e}", severity="warning")
                    return
                project = (task.properties.get("PROJECT") or "").strip()
                if not project:
                    self.notify("set :PROJECT: first (metadata → project)",
                                severity="warning")
                    return
                from ..idea_ext import read_idea_extension
                from ..extension_schema import enum_values_for, extension_keys_for
                existing = read_idea_extension(self.cfg, task, project)
                keys = extension_keys_for(self.cfg, project, existing)

                def key_picked(key):
                    if not key:
                        return
                    current = existing.get(key, "")
                    enum_vals = enum_values_for(self.cfg, project, key)
                    if enum_vals:
                        def enum_picked(value):
                            if value is None:
                                return
                            self.mutate("ext_set", key=key, value=value)
                        self.push_screen(ChoiceScreen(f"ext · {key}", enum_vals, current),
                                         enum_picked)
                    else:
                        def got_value(value):
                            if value is None or not value.strip():
                                return
                            self.mutate("ext_set", key=key, value=value.strip())
                        self.push_screen(TextPrompt(f"ext · {key}", current or ""),
                                         got_value)
                self.push_screen(ChoiceScreen(f"ext · {self.selected_id}", keys), key_picked)
                return
            if field == "state":                                     # SPEC-0135
                from ..org import _split_keywords
                todos, dones = _split_keywords(
                    self.cfg.get("org_idea_keywords")
                    or ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                        "SHIPPED", "DROPPED", "RESEARCHED"])
                choices = list(todos) + list(dones)
            elif field == "kind":
                choices = list(IDEA_KINDS)
            elif field == "priority":
                choices = ["A", "B", "C"]
            elif field == "project":                      # SPEC-0109 / SPEC-0139
                # NONE_CHOICE first: clearing a mis-set project must be as easy as setting one.
                names = M.known_project_choices(self.cfg)
                if not names:
                    self.notify("no projects configured (mappings / outbox_targets)",
                                severity="warning")
                    return
                choices = [NONE_CHOICE] + names
            else:
                choices = list(configured_workstreams(self.cfg) or [])
                if not choices:
                    self.notify("no workstreams configured (cfg['workstreams'])",
                                severity="warning")
                    return

            current = ""
            row = self._selected_row()
            if row is not None and field in ("project", "state"):
                current = getattr(row, field) or ""

            def value_picked(value):
                if value is None:
                    return
                self.mutate(field, value="" if value == NONE_CHOICE else value)
            self.push_screen(ChoiceScreen(f"{field} · {self.selected_id}", choices, current),
                             value_picked)

        self.push_screen(ChoiceScreen(f"metadata · {self.selected_id}", fields), field_picked)

    def action_clock_toggle(self) -> None:
        """One key for both directions — the payload already says which way it goes."""
        detail = self._selected_detail()
        if detail is None:
            return
        self.mutate("clock_out" if detail.clock_open else "clock_in")

    def action_close_idea(self) -> None:
        if not self.selected_id:
            return

        def picked(outcome):
            if outcome:
                self.mutate("close", outcome=outcome)
        self.push_screen(ChoiceScreen(f"close · {self.selected_id}", ["drop", "researched"]),
                         picked)

    def action_capture(self) -> None:
        """Capture text, then offer a project (SPEC-0109).

        The project step is offered rather than required — an idea with no project is a valid
        state and capture has to stay fast. When the desk was opened with `--project P`, that
        entry is pre-selected: the filter you are working under is almost certainly the project
        you are capturing into. Pre-selected, never silently applied.
        """
        def write(text, project):
            try:
                new_id = M.capture_idea(self.cfg, text, project=project)
            except (ValueError, OSError) as e:
                self.notify(str(e), severity="error", title="capture failed")
                return
            self._clear_query()                    # the new idea is what to look at next
            self.reload(keep_id=new_id or None)

        def got_text(text):
            if not (text and text.strip()):
                return
            choices = [NONE_CHOICE] + M.known_project_choices(self.cfg)
            if len(choices) == 1:                  # nothing configured — don't ask a dead question
                self.notify("no projects configured (mappings / outbox_targets)",
                            severity="warning")
                write(text, None)
                return

            def got_project(choice):
                if choice is None:                 # Esc on the project step still captures
                    write(text, None)
                    return
                write(text, None if choice == NONE_CHOICE else choice)

            self.push_screen(
                ChoiceScreen("project for the new idea", choices,
                             self.project if self.project not in (B.ALL, B.NO_PROJECT)
                             else NONE_CHOICE),
                got_project)

        self.push_screen(TextPrompt("new idea"), got_text)

    def _hide_search(self) -> None:
        box = self.query_one("#search", Input)
        box.remove_class("visible")
        self.query_one("#board", Tree).focus()


def _rule(label: str, width: int, *, note: str = "") -> str:
    """`Summary ─────────` — a section header that carries its own divider.

    The dashes encode where a section ends, which is information the reader needs when Summary
    and Log are both wrapped prose; they are not decoration. Degrades to a bare label when the
    pane is too narrow to spend columns on a rule.
    """
    plain_len = len(label) + (len(note) + 1 if note else 0)
    dashes = width - plain_len - 3
    if dashes < 2:
        return f"[bold dim]{escape(label)}[/]" + (f" [dim]{escape(note)}[/]" if note else "")
    rule = f"[bold dim]{escape(label)}[/] [dim]{'─' * dashes}[/]"
    return f"{rule} [dim]{escape(note)}[/]" if note else rule


def lex_org(prose: str, theme: str = ANSI_DARK) -> Text:
    """Org prose as a styled `Text` (SPEC-0107).

    `Syntax.highlight` returns a `Text` rather than an opaque renderable, which is what lets
    lexing, SPEC-0106's search marks and the hand-built structure all coexist. `OrgLexer`
    distinguishes headings, `*bold*`, `/italic/`, `~code~`, `=verbatim=`, tags and property
    keys. `background_color="default"` keeps the terminal's own background.
    """
    from rich.syntax import Syntax

    if not prose:
        return Text("")
    lexed = Syntax(prose, "org", theme=theme, background_color="default").highlight(prose)
    lexed.rstrip()                       # highlight() appends a trailing newline
    return lexed


def _spec_lines(item) -> list[str]:
    """Markup lines naming the idea's spec and its real status (SPEC-0490), always present:
    an idea with no spec says so, so the pane never leaves "is there a spec?" to guesswork."""
    bucket = B.BUCKET_LABELS[item.bucket]
    style = _BUCKET_STYLE[item.bucket]
    link = item.link
    if link is None:
        what = escape(item.spec_label or "no spec yet")
        lines = [f"[bold dim]spec[/] [dim]{what}[/]  [{style}]{bucket}[/]"]
    else:
        status = escape(link.status or "?")
        sstyle = _STATUS_STYLE.get(link.status, "")
        via = f" [dim]→ superseded by[/] {escape(link.via)}" if link.via else ""
        lines = [(f"[bold dim]spec[/] [bold]{escape(link.id)}[/]{via}  "
                  f"[{sstyle or 'white'}]{status}[/]  [{style}]{bucket}[/]")]
        info = link.info
        extra = []
        if info is not None and info.title:
            extra.append(escape(info.title))
        if info is not None and info.outbound:
            extra.append("outbound · exported" if info.exported else "outbound · not exported")
        if extra:
            lines.append(f"[dim]{' · '.join(extra)}[/]")
    if item.rollup_label:
        lines.append(f"[magenta]epic roll-up {escape(item.rollup_label)}[/]")
    return lines


def _render_detail(d: M.DeskDetail, *, snippet: str = "", width: int = 60,
                   theme: str = ANSI_DARK, spec: list[str] | None = None) -> Text:
    """The detail pane as a `Text`.

    **Structure is hand-built; only prose is lexed** (SPEC-0107). Section rules, the ○/✓ question
    glyphs, the clock and meta lines never reach the lexer, so no theme can eat them — they are
    structure, not decoration. Everything interpolated into markup is still escaped: org prose
    routinely contains `[#A]`, `[[links]]` and `[2026-07-30 Thu]`, which Rich would otherwise
    read as markup.
    """
    inner = max(20, width - 2)                     # the Static's own `padding: 0 1`
    body = Text()

    def add_markup(markup: str) -> None:
        body.append_text(Text.from_markup(markup))
        body.append("\n")

    def add_prose(prose: str, *, indent: str = "") -> None:
        lexed = lex_org(prose, theme)
        if indent:
            body.append(indent)
        body.append_text(lexed)
        body.append("\n")

    state = escape(d.state or "·")
    style = d.state_style or "white"
    add_markup(f"[{style}]{state}[/]  [bold]{escape(d.heading)}[/]")
    if d.meta:
        add_markup(f"[dim]{escape(' · '.join(d.meta))}[/]")
    for line in spec or ():                                           # SPEC-0490
        add_markup(line)
    if d.clock_open or d.clocked_hours:                               # SPEC-0103
        running = "[cyan]◕ clocked in[/]" if d.clock_open else "[dim]clock idle[/]"
        add_markup(f"{running} [dim]· {d.clocked_hours:.2f}h logged[/]")
    if snippet:
        add_markup(f"[dim]match:[/] {escape(snippet)}")

    add_markup("")
    add_markup(_rule("Summary", inner))
    if d.summary:
        add_prose(d.summary)
    else:
        add_markup("[dim](empty)[/]")

    add_markup("")
    add_markup(_rule("Questions", inner,
                     note=f"{d.open_count} open · {d.resolved_count} resolved"))
    if not (d.open_questions or d.resolved_questions):
        add_markup("[dim](none)[/]")
    for q in d.open_questions:
        body.append_text(Text.from_markup("[yellow]○[/] "))
        add_prose(q)
    for q in d.resolved_questions:
        body.append_text(Text.from_markup("[green]✓[/] "))
        add_prose(q)

    add_markup("")
    add_markup(_rule("Log", inner))
    if not d.log:
        add_markup("[dim](empty)[/]")
    for entry in d.log:
        head, _, rest = entry.partition("\n")
        add_markup(f"[cyan]{escape(head)}[/]")
        if rest.strip():
            add_prose(rest.strip())
    body.rstrip()
    return body


def run_app(cfg, **kwargs):
    """Run the desk to completion (blocks until the user quits)."""
    return IdeaDesk(cfg, **kwargs).run()
