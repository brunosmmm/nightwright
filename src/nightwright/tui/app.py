"""Textual ideas desk (SPEC-0099 entry, SPEC-0100 read-only panes).

Imports Textual at module scope on purpose — only reachable through `nightwright.tui.run()`, which
probes for the optional extra first. All data comes from `nightwright.tui.model`; this file is a
renderer plus SPEC-0102 mutation wiring; every write goes through `model.apply_mutation`.
"""
import asyncio
import functools

from rich.markup import escape
from rich.text import Text

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (DataTable, Footer, Header, Input, Label, Static, TabbedContent,
                             TabPane, TextArea, Tree)

from .. import report as R
from . import model as M
from .campaign_detail import CampaignDetail, OpenCampaign
from .events_tab import EventsView
from .mailbox_tab import MailboxView
from .queue_tab import QueueView

# Half the terminal, with a floor: proportional so a wide terminal spends the extra columns on
# headings, floored so the metadata columns never get squeezed out (at 35% a 100-col terminal
# silently dropped the last column). Detail takes the rest (`1fr`).
LIST_WIDTH = "50%"
LIST_MIN_WIDTH = 40
# SPEC-0110: the desk plans columns with `report._idea_table_widths` — the same planner the CLI
# uses — instead of the hardcoded budget that used to live here. That is what lets `project`
# come back (SPEC-0100 dropped it; SPEC-0109 made project something you *set* from the desk, so
# hiding it became wrong) without starving the heading: capped columns squeeze first.
DESK_COLUMNS = ("id", "state", "pri", "q", "kind", "project", "epic", "updated")
# SPEC-0110: when even a fully-squeezed plan cannot give the heading its floor, columns are
# *dropped* in this order — least informative first. `kind` is a glyph the detail pane repeats,
# `pri` is usually blank, `state` is partly implied by colour; `project` and `q` earn their keep
# because they are the two things you cannot recover without opening the idea. `id` and
# `updated` never drop: `id` is how you refer to a row and `updated` drives the default sort.
# SPEC-0116: `epic` drops *after* `project` — an epic is more specific and less recoverable at a
# glance than the project, which the detail pane repeats anyway.
DROP_ORDER = ("kind", "pri", "state", "project", "epic", "q")
HEADING_FLOOR = 12


def plan_desk_columns(prows, width):
    """`(keys, widths, heading_budget)` for a list pane of `width` (SPEC-0110).

    Extends the shared planner's "squeeze capped columns" with "then drop", so one rule serves
    every terminal size: a wide pane keeps all seven columns, a narrow one sheds the least
    informative until the heading is readable again. Always keeps at least one metadata column
    so a row is still identifiable.
    """
    keys = list(DESK_COLUMNS)
    sample = prows or [{k: "" for k in DESK_COLUMNS}]
    while True:
        widths, budget = R._idea_table_widths(
            sample, tuple(keys), floor=HEADING_FLOOR, width=width,
            overhead=2 * (len(keys) + 1))       # DataTable pads 1 each side, draws no rules
        droppable = [k for k in DROP_ORDER if k in keys]
        if budget >= HEADING_FLOOR or not droppable or len(keys) <= 1:
            return tuple(keys), widths, budget
        keys.remove(droppable[0])


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
            yield Label(f"{self.title_text}  (enter pick · esc cancel)")
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
    """Two-pane ideas desk: browse (SPEC-0100), explore mutations (SPEC-0102),
    metadata + clock (SPEC-0103)."""

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
    #list-pane { width: LIST_W; min-width: LIST_MINW; }
    #list { height: 1fr; }
    #tree { height: 1fr; display: none; }
    #tree.visible { display: block; }
    #status { color: $text-muted; padding: 0 1; }
    /* The bottom strip is explicit. Left in flow, `#tabs` at 1fr ate every remaining row and
       the search box, hint and Footer all landed on the same line with Footer painted on top —
       the box collected keystrokes no terminal ever showed. An Input is also 3 rows tall by
       default, hence the explicit height/border reset. */
    #tabs { height: 1fr; }
    #search { display: none; height: 1; border: none; padding: 0 1; background: $panel; }
    #search.visible { display: block; }
    #filter { display: none; height: 1; border: none; padding: 0 1; background: $panel; }
    #filter.visible { display: block; }
    #detail-pane { border-left: solid $panel; }
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
    """.replace("LIST_MINW", str(LIST_MIN_WIDTH)).replace("LIST_W", LIST_WIDTH)

    BINDINGS = [
        Binding("j,down", "cursor_down", "down", show=False),
        Binding("k,up", "cursor_up", "up", show=False),
        Binding("slash", "search", "search"),
        Binding("a", "toggle_all", "non-active"),
        # SPEC-0110 — in-app filters/sort/layout. SPEC-0100 kept these as flags, appealing to
        # SPEC-0098's "minimal chrome"; that forced quit-and-relaunch to re-filter a triage
        # desk. Restraint re-read as "no sprawl", not "no controls".
        Binding("f", "filter", "filter"),
        Binding("o", "cycle_sort", "sort"),
        Binding("O", "toggle_desc", "reverse", show=False),
        Binding("z", "toggle_wide", "wide"),
        Binding("g", "toggle_tree", "tree"),      # SPEC-0114
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
        Binding("5", "tab('tab-events')", "events", show=False),      # SPEC-0251
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
        self.all_done = all_done
        self.query = query or ""
        self.filters = filters or {}
        self._pairs: list[tuple[M.DeskRow, object]] = []
        self._hub_lines: list[M.HubLine] = []
        self._search_timer = None
        # SPEC-0110: sort/desc arrive as opening flags, then become in-app state. Popped out of
        # `filters` so `filters` stays exactly `collect_idea_tasks`' filter vocabulary.
        self.sort = self.filters.pop("sort", None)
        self.desc = bool(self.filters.pop("desc", False))
        self.wide = False
        # `tree` is taken by Textual's DOMNode.tree (read-only), hence `tree_mode`.
        self.tree_mode = False                     # SPEC-0114
        self._depths: list[tuple] = []
        self._columns: tuple = ()
        self._org_signature = None          # SPEC-0128: external-change poll baseline
        self._reload_in_flight = False      # SPEC-0129: guards overlapping poll reloads

    # --- layout ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs", initial=f"tab-{self.tab}"):
            with TabPane("ideas", id="tab-ideas"):
                with Horizontal(id="panes"):
                    with Vertical(id="list-pane"):
                        yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                        # SPEC-0115: a real collapsible tree, shown instead of the table in tree
                        # mode. A DataTable has no notion of a parent, which is why indented rows
                        # could never expand or collapse.
                        yield Tree("ideas", id="tree")
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
        yield Input(placeholder="search ideas…", id="search")
        yield Input(placeholder="filter: project=X kind=bug priority=A …", id="filter")
        yield Static("", id="hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#list", DataTable)
        # columns are planned per width in `paint`; nothing to add here
        if self.tab == "ideas":
            table.focus()       # focusing a hidden pane's widget would switch back to its tab
        # Paint *after* the first refresh: pane widths don't exist until layout has run, and the
        # heading column is clipped to a measured budget.
        self.call_after_refresh(self.reload)
        self.set_interval(1.5, self._check_external_changes)   # SPEC-0128
        if self.campaign:
            self.open_campaign_detail(self.campaign)

    def on_resize(self, event) -> None:
        """Re-clip to the new width without re-reading org."""
        if self._pairs:
            self.paint()
        if self._hub_lines:
            self.load_hub()

    # --- data --------------------------------------------------------------------------

    def reload(self, *, keep_id: str | None = None) -> None:
        """Re-read org, then repaint (SPEC-0134: non-blocking when an event loop is running).

        Sync call sites (`r`, search, filter, mutate, mount) schedule `_reload_async` so
        `load_rows` never freezes the Textual loop. Without a running loop (rare unit helpers),
        fall back to the blocking path.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._reload_blocking(keep_id=keep_id)
            return
        self.run_worker(self._reload_async(keep_id=keep_id), name="desk-reload")

    def _reload_blocking(self, *, keep_id: str | None = None) -> None:
        """Synchronous load+paint — used only when no event loop is running."""
        keep_id = keep_id or self.selected_id
        try:
            self._pairs = M.load_rows(self.cfg, query=self.query or None,
                                      all_done=self.all_done, sort=self.sort,
                                      desc=self.desc, **self.filters)
            self._depths = []      # SPEC-0115: the Tree widget owns nesting now, not padding
        except (ValueError, OSError) as e:                 # unreadable org / bad filter
            self._pairs = []
            self.set_hint(f"load failed: {e}")
        self.paint(keep_id=keep_id)
        # SPEC-0128: resync the external-change baseline on every reload (not just the poll's
        # own), so the timer never re-fires on a change the desk already knows about.
        from ..org import org_files_mtime_signature
        self._org_signature = org_files_mtime_signature(self.cfg)

    async def _reload_async(self, *, keep_id: str | None = None) -> None:
        """Load rows off-thread, then paint on the event-loop thread (SPEC-0129/0134)."""
        if self._reload_in_flight:
            return
        self._reload_in_flight = True
        try:
            keep_id = keep_id or self.selected_id
            loop = asyncio.get_running_loop()
            try:
                pairs = await loop.run_in_executor(
                    None, functools.partial(
                        M.load_rows, self.cfg, query=self.query or None,
                        all_done=self.all_done, sort=self.sort, desc=self.desc,
                        **self.filters))
            except (ValueError, OSError) as e:
                self._pairs = []
                self.set_hint(f"load failed: {e}")
                self.paint(keep_id=keep_id)
                return
            self._pairs = pairs
            self._depths = []
            self.paint(keep_id=keep_id)
            from ..org import org_files_mtime_signature
            self._org_signature = org_files_mtime_signature(self.cfg)
        finally:
            self._reload_in_flight = False

    def _resolve_fresh(self, idea_id: str):
        """Re-resolve a Task fresh from disk by id (SPEC-0128) — never reuse a Task cached in
        `self._pairs`, since an external edit since the last `reload()` can shift its line
        number or state out from under it, tripping `explore._idea_span`'s drift guard."""
        from ..org_write import resolve_selector
        return resolve_selector(self.cfg, idea_id)

    async def _check_external_changes(self) -> None:
        """Poll (SPEC-0128): cheap `stat()`-only signature compare, no reparse unless
        something actually changed. The reparse itself runs off the event-loop thread
        (SPEC-0129/0134) via `_reload_async`. On a change, reload and — only if the idea
        currently shown in the detail pane itself changed — leave a small non-blocking hint."""
        from textual.css.query import NoMatches

        from ..org import org_files_mtime_signature
        sig = org_files_mtime_signature(self.cfg)
        if sig == self._org_signature or self._reload_in_flight:
            return
        try:
            watched_id = self.selected_id
            old = next((r for r, _ in self._pairs if r.id == watched_id), None)
            await self._reload_async(keep_id=watched_id)
        except NoMatches:       # the timer fired while the app tears its DOM down: nothing to do
            return
        new = next((r for r, _ in self._pairs if r.id == watched_id), None)
        if old and new and (old.heading != new.heading or old.state != new.state):
            self.set_hint(f"↻ {new.id} changed externally")

    def paint(self, *, keep_id: str | None = None) -> None:
        """Render `self._pairs` into the panes at the current width. No org reads."""
        if self.tree_mode:                                    # SPEC-0115
            self.paint_tree()
            self.query_one("#status", Static).update(M.status_line(
                self._pairs, all_done=self.all_done, query=self.query, filters=self.filters,
                sort=self.sort, desc=self.desc,
                mode="tree wide" if self.wide else "tree"))
            if self._pairs:
                self.show_detail(0)
            return
        table = self.query_one("#list", DataTable)
        keep_id = keep_id or self.selected_id
        table.clear()
        # SPEC-0110: plan with the CLI's own planner against the MEASURED pane width. Capped
        # columns (kind/project/q) squeeze toward their minimum before the heading loses its
        # floor, which is what makes a `project` column affordable at all.
        prows = [{"id": "  " + r.id, "state": r.state, "pri": r.priority, "kind": r.kind_glyph,
                  "project": r.project, "epic": r.epic, "updated": r.age,
                  "q": str(r.open_questions) if r.open_questions else ""}
                 for r, _ in self._pairs]
        keys, widths, head_width = plan_desk_columns(
            prows, table.size.width or LIST_MIN_WIDTH)
        if keys != self._columns:                 # rebuild only when the plan actually changes
            self._columns = keys
            table.clear(columns=True)
            table.add_columns(*[R._IDEA_META_COLS[k][0] for k in keys], "idea")
        for idx, ((row, _task), prow) in enumerate(zip(self._pairs, prows)):
            cells = []
            for key in keys:
                w = widths[key]
                if key == "id":
                    # SPEC-0103: a running clock is the one piece of state you can forget you
                    # left on, so it marks the id itself rather than taking its own column.
                    cells.append(Text(("◕ " if row.clock_open else "  ") + row.id,
                                      style="cyan" if row.clock_open else ""))
                elif key == "state":
                    cells.append(Text(R._clip(row.state or "·", w), style=row.state_style or ""))
                elif key == "q":
                    cells.append(Text(prow["q"], style="yellow" if prow["q"] else ""))
                elif key == "project":
                    cells.append(Text(R._clip(row.project, w), style="green" if row.project else ""))
                elif key == "epic":
                    cells.append(Text(R._clip(row.epic, w), style="magenta" if row.epic else ""))
                else:
                    cells.append(R._clip(prow[key], w))
            # SPEC-0114: indentation is charged against the heading budget, so a nested row
            # clips with an ellipsis instead of being cropped by the widget.
            depth, label = self._depth_of(idx)
            pad = "  " * depth
            avail = max(6, head_width - len(pad))
            cells.append(self._marked(pad + R._clip(row.heading, avail),
                                      style="dim" if row.is_done else ""))
            # Row keys are idea ids; if a duplicate still slipped past load_rows dedupe,
            # suffix so Textual DataTable never raises DuplicateKey and crashes the desk.
            row_key = row.id
            try:
                table.add_row(*cells, key=row_key)
            except Exception as e:  # textual.widgets.data_table.DuplicateKey
                if type(e).__name__ != "DuplicateKey":
                    raise
                table.add_row(*cells, key=f"{row.id}#{idx}")
        self.query_one("#status", Static).update(M.status_line(
            self._pairs, all_done=self.all_done, query=self.query, filters=self.filters,
            sort=self.sort, desc=self.desc,
            mode=("tree " if self.tree_mode else "") + ("wide" if self.wide else "split")))

        if self._pairs:
            index = 0
            if keep_id:
                index = next((i for i, (r, _) in enumerate(self._pairs) if r.id == keep_id), 0)
            table.move_cursor(row=index)
            self.show_detail(index)
        else:
            self.query_one("#detail", Static).update("[dim]no ideas match[/]")
            self.set_hint("")

    @property
    def selected_id(self) -> str | None:
        if self.tree_mode:                                    # SPEC-0115
            node = self.query_one("#tree", Tree).cursor_node
            return getattr(node, "data", None) if node else None
        table = self.query_one("#list", DataTable)
        if not self._pairs or table.cursor_row is None or table.cursor_row < 0:
            return None
        if table.cursor_row >= len(self._pairs):
            return None
        return self._pairs[table.cursor_row][0].id

    def show_detail(self, index: int) -> None:
        if not (0 <= index < len(self._pairs)):
            return
        row, _cached_task = self._pairs[index]
        try:
            task = self._resolve_fresh(row.id)         # SPEC-0128: never read via a stale Task
            detail = M.load_detail(self.cfg, task)
        except (ValueError, OSError) as e:
            self.query_one("#detail", Static).update(f"[red]could not read idea:[/] {escape(str(e))}")
            return
        width = self.query_one("#detail-pane", VerticalScroll).size.width
        # `_render_detail` returns a Text already (SPEC-0107), so search marks apply directly.
        # Marking a markup *string* would corrupt it — `escape()` shifts indices and a span
        # could land inside a `[dim]` tag (SPEC-0106).
        body = _render_detail(detail, snippet=row.snippet, width=width,
                              theme=self.syntax_theme)
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
        rule — so the marks show precisely what the search matched, including a token that
        occurs inside a longer word. No query means no marks.
        """
        tokens = M.search_tokens(self.query)
        if not tokens:
            return 0
        return text.highlight_words(tokens, MATCH_STYLE, case_sensitive=False)

    def _depth_of(self, index: int) -> tuple:
        """`(depth, label)` for a painted row — `(0, "")` outside tree mode (SPEC-0114)."""
        if self.tree_mode and 0 <= index < len(self._depths):
            return self._depths[index]
        return (0, "")

    def _marked(self, plain: str, *, style: str = "") -> Text:
        """A `Text` for `plain` with the active query marked."""
        text = Text(plain, style=style)
        self._mark_into(text)
        return text

    def set_hint(self, text: str) -> None:
        self.query_one("#hint", Static).update(escape(text))

    # --- events ------------------------------------------------------------------------

    @on(DataTable.RowHighlighted, "#list")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.show_detail(event.cursor_row)

    @on(Input.Submitted, "#search")
    def _search_submitted(self, event: Input.Submitted) -> None:
        self._cancel_search_timer()
        self.query = event.value.strip()
        self._hide_search()
        self.reload()

    @on(Input.Changed, "#search")
    def _search_changed(self, event: Input.Changed) -> None:
        """Filter as you type, debounced (SPEC-0105). Enter still applies immediately.

        Each keystroke restarts the timer, so a burst of typing costs one search rather than
        one per character. Clearing the box live restores the unfiltered list — backspacing out
        of a query must not strand you on an empty result.
        """
        self._cancel_search_timer()
        wanted = event.value.strip()

        def apply() -> None:
            self._search_timer = None
            if wanted != self.query:
                self.query = wanted
                self.reload()

        self._search_timer = self.set_timer(SEARCH_DEBOUNCE, apply)

    def _cancel_search_timer(self) -> None:
        if getattr(self, "_search_timer", None) is not None:
            self._search_timer.stop()
            self._search_timer = None

    # --- actions -----------------------------------------------------------------------

    def _cursor_widget(self):
        """The widget j/k should move (SPEC-0115).

        Hardcoding the DataTable meant that in tree mode j/k moved the *hidden* table, so the
        detail pane followed a row you could not see and the tree cursor never moved — which also
        made Enter appear not to expand anything.
        """
        return self.query_one("#tree", Tree) if self.tree_mode \
            else self.query_one("#list", DataTable)

    def action_cursor_down(self) -> None:
        self._cursor_widget().action_cursor_down()

    def action_cursor_up(self) -> None:
        self._cursor_widget().action_cursor_up()

    def action_search(self) -> None:
        box = self.query_one("#search", Input)
        box.add_class("visible")
        box.value = self.query
        box.focus()

    def action_clear_bars(self) -> None:
        """Esc closes whichever bottom bar is open (SPEC-0110 added a second one)."""
        if self.query_one("#filter", Input).has_class("visible"):
            self._hide_filter()
            return
        self.action_clear_search()

    def action_clear_search(self) -> None:
        """Esc: close the box; if a query was applied, drop it and reload."""
        self._cancel_search_timer()
        self._hide_search()
        if self.query:
            self.query = ""
            self.reload()

    def action_toggle_all(self) -> None:
        self.all_done = not self.all_done
        self.reload()

    def action_refresh_desk(self) -> None:
        self.reload()

    # --- filters, sort, layout (SPEC-0110) ----------------------------------------------

    def action_filter(self) -> None:
        box = self.query_one("#filter", Input)
        box.add_class("visible")
        box.value = M.format_filter_expr(self.filters)
        box.focus()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        """Apply `key=value` tokens. Problems are reported, never silently dropped — a filter
        that quietly does nothing looks exactly like one that matched nothing."""
        filters, problems = M.parse_filter_expr(event.value)
        if problems:
            self.notify("; ".join(problems), severity="error", title="filter")
            return
        self.filters = filters
        self._hide_filter()
        self.reload()

    def action_clear_filter(self) -> None:
        self._hide_filter()

    def _hide_filter(self) -> None:
        self.query_one("#filter", Input).remove_class("visible")
        self.query_one("#list", DataTable).focus()

    def action_cycle_sort(self) -> None:
        """Cycle the sort field; maps straight to `collect_idea_tasks(sort=…)` (`--sort`)."""
        order = list(M.SORT_FIELDS)
        current = self.sort or order[0]
        self.sort = order[(order.index(current) + 1) % len(order)] if current in order \
            else order[0]
        self.reload()

    def action_toggle_desc(self) -> None:
        self.desc = not self.desc
        self.reload()

    def action_toggle_tree(self) -> None:
        """Swap the flat table for a real collapsible tree (SPEC-0115).

        Nesting needs non-active ideas visible, since children are PROMOTED as they ship — the
        status line says so, and `a` reveals them.
        """
        self.tree_mode = not self.tree_mode
        table, tree = self.query_one("#list", DataTable), self.query_one("#tree", Tree)
        table.display = not self.tree_mode
        tree.set_class(self.tree_mode, "visible")
        # After refresh, not immediately: a widget that was hidden has no size yet, so painting
        # now would plan columns against width 0 and auto-drop half of them (the same trap as
        # SPEC-0100's pre-layout paint).
        self.call_after_refresh(self.reload)
        (tree if self.tree_mode else table).focus()

    def paint_tree(self) -> None:
        """Rebuild the tree from `build_idea_tree` (SPEC-0115). Ideas carry their id in
        `node.data`, so `selected_id` — and therefore every mutation — works unchanged."""
        tree = self.query_one("#tree", Tree)
        tree.clear()
        tree.show_root = False
        matched = {r.id for r, _ in self._pairs}
        groups = R.build_idea_tree(self.cfg, [t for _r, t in self._pairs],
                                  sort=self.sort, desc=self.desc, matched=matched)

        def label(node) -> Text:
            """A node label styled like the flat table's row, not monochrome.

            The first cut styled everything `dim` or not at all, throwing away every colour the
            table carries: SPEC-0082's state hue (which encodes *which way* an idea closed),
            SPEC-0085's kind glyph, and SPEC-0110's yellow open-question count. The same helpers
            are reused here so there is one definition of how an idea looks.
            """
            row, task = node["row"], node["task"]
            if node.get("synthetic"):          # SPEC-0116: an `:EPIC:` spec id, not an idea
                return Text(f"{node['id']}  ({node['count']})", style="magenta")
            context = not node["matched"]
            text = Text()
            if row.get("clock_open"):                       # SPEC-0103
                text.append("◕ ", style="cyan")
            text.append(f"{node['id']}  ", style="dim")
            state = row.get("state") or "·"
            text.append(state, style=R._idea_state_style(task) or "")   # SPEC-0082 hue
            if row.get("priority"):                                     # SPEC-0093
                text.append(f"  [#{row['priority']}]", style="bold")
            if row.get("open_questions"):                               # SPEC-0110
                text.append(f"  {row['open_questions']}q", style="yellow")
            text.append("  " + R._kind_cell(row.get("kind") or "idea", False) + "  ")
            text.append(row.get("heading") or "",
                        style="dim" if (context or task.is_done) else "")
            return text

        def add(parent, node):
            branch = parent.add(label(node), data=node["id"],
                                expand=False, allow_expand=bool(node["children"]))
            for child in node["children"]:
                add(branch, child)

        for group in groups:
            # Projects open, epics closed: the mode exists to show structure without 151 rows,
            # and an epic's children are the detail you drill into.
            g = tree.root.add(Text(f"{group['project']}  ({group['count']})", style="bold"),
                              data=None, expand=True)
            for node in group["nodes"]:
                add(g, node)
        # A fresh Tree has cursor_line == -1, i.e. nothing highlighted, so the detail pane would
        # show a stale idea until the first keypress. Land on the first real idea instead.
        if groups:
            tree.cursor_line = 1 if groups[0]["nodes"] else 0

    @on(Tree.NodeHighlighted, "#tree")
    def _tree_highlighted(self, event) -> None:
        ident = getattr(event.node, "data", None)
        if not ident:
            return
        index = next((i for i, (r, _) in enumerate(self._pairs) if r.id == ident), None)
        if index is not None:
            self.show_detail(index)

    def action_toggle_wide(self) -> None:
        """Full-width list vs split. The table is otherwise stuck fighting for half the
        terminal, which is what makes every extra column feel expensive."""
        self.wide = not self.wide
        self.query_one("#detail-pane", VerticalScroll).display = not self.wide
        self.query_one("#list-pane", Vertical).styles.width = "100%" if self.wide else LIST_WIDTH
        self.call_after_refresh(self.paint)

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

    def action_retitle(self) -> None:
        selector = self.selected_id
        if not selector:
            return
        index = next((i for i, (r, _) in enumerate(self._pairs) if r.id == selector), None)
        current = self._pairs[index][0].heading if index is not None else ""

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
        self.query_one("#tabs", TabbedContent).active = name

    # Idea-scoped actions act on the selected idea of the ideas list, which is hidden on any
    # other tab. Disabled there so e.g. `x` on the queue tab cannot change org state.
    _IDEA_ACTIONS = frozenset({
        "search", "toggle_all", "filter", "cycle_sort", "toggle_desc", "toggle_wide",
        "toggle_tree", "refresh_desk", "log", "summary", "questions", "capture", "retitle",
        "explored", "metadata", "clock_toggle", "close_idea"})

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        on_detail = any(isinstance(s, CampaignDetail) for s in self.screen_stack)
        if (on_detail and action == "tab" and parameters
                and parameters[0] in {"tab-ideas", "tab-hub", "tab-queue",
                                      "tab-mailbox", "tab-events"}):
            return False
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
            self.query_one("#list", DataTable).focus()

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
        """Hub takes the same filters the desk was opened with, minus list-only sort keys."""
        return {k: v for k, v in self.filters.items() if k not in ("sort", "desc")}

    @on(DataTable.RowSelected, "#hub")
    def _hub_row_selected(self, event: DataTable.RowSelected) -> None:
        """Drill an idea row into the ideas desk; a spec row only offers a CLI hint."""
        row = event.cursor_row
        if not (0 <= row < len(self._hub_lines)):
            return
        line = self._hub_lines[row]
        if line.idea_id:
            self.query_one("#tabs", TabbedContent).active = "tab-ideas"
            self.query_one("#list", DataTable).focus()
            if line.idea_id not in [r.id for r, _ in self._pairs]:
                self.all_done = True            # hub can list an idea the current view hides
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
            if field == "project":
                index = next((i for i, (r, _) in enumerate(self._pairs)
                              if r.id == self.selected_id), None)
                current = self._pairs[index][0].project if index is not None else ""
            elif field == "state":
                index = next((i for i, (r, _) in enumerate(self._pairs)
                              if r.id == self.selected_id), None)
                current = self._pairs[index][0].state if index is not None else ""

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
            self.query = ""                        # a new idea is active; clear any filter view
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
                             self.filters.get("project") or NONE_CHOICE),
                got_project)

        self.push_screen(TextPrompt("new idea"), got_text)

    def _hide_search(self) -> None:
        box = self.query_one("#search", Input)
        box.remove_class("visible")
        self.query_one("#list", DataTable).focus()


def _short_state(state: str) -> str:
    """Idea states clipped to 3 chars so the list pane keeps its width for the heading."""
    return (state or "")[:3]


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


def _render_detail(d: M.DeskDetail, *, snippet: str = "", width: int = 60,
                   theme: str = ANSI_DARK) -> Text:
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
