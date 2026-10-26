"""Events tab widgets (SPEC-0251): the queue event stream, the watchdog conditions true now, the
latest conductor ping and the followers, over `agentic_model`.

Read-only by construction: nothing here syncs the stream, sets a cursor, takes a reader lock or
follows. The tab would otherwise become one of the followers it monitors. A `stat()` signature
poll catches appended events; the heavier panels (`watchdog_checks` reads every active ledger
tail) refresh on a slower timer.
"""
import asyncio
import os

from rich.markup import escape
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Static

from .. import report as R
from . import agentic_model as AM
from .queue_tab import QueueView

POLL_S = 2.0
SIDE_S = 5.0
_FIXED_COST = 62       # id + time + type + campaign + padding columns
_CAMPAIGN_W = 24


class EventsTable(DataTable):
    """The focused widget of the tab: its bindings shadow the desk's (`c`, `x`, `f`, …)."""

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("f", "filter", "filter"),
        Binding("x", "alerts", "alerts only"),
        Binding("c", "campaign", "campaign"),
        Binding("G", "tail", "tail"),
        Binding("enter", "open", "open", show=False),
    ]

    def __init__(self, view, **kw):
        super().__init__(cursor_type="row", zebra_stripes=False, **kw)
        self.view = view

    def action_filter(self) -> None:
        self.view.open_filter()

    def action_alerts(self) -> None:
        self.view.toggle_alerts()

    def action_campaign(self) -> None:
        self.view.toggle_campaign()

    def action_tail(self) -> None:
        self.view.jump_to_tail()

    def action_open(self) -> None:
        self.view.open_selected()


class EventFilterInput(Input):
    BINDINGS = [Binding("escape", "close", "close", show=False)]

    def action_close(self) -> None:
        self.screen.query_one(EventsView).close_filter()


class EventsView(Horizontal):
    """Stream table + status line + filter bar on the left, three panels on the right."""

    DEFAULT_CSS = """
    EventsView { height: 1fr; }
    #events-main { width: 65%; }
    #events-table { height: 1fr; }
    #events-status { color: $text-muted; padding: 0 1; height: 1; }
    #events-filter { display: none; height: 1; border: none; padding: 0 1; background: $panel; }
    #events-filter.visible { display: block; }
    #events-side { width: 1fr; }
    .events-panel { height: auto; border: round $panel; padding: 0 1; margin: 0 0 0 1; }
    """

    def __init__(self, cfg, **kw):
        super().__init__(**kw)
        self.cfg = cfg
        self.loaded: list[AM.EventRow] = []
        self.shown: list[AM.EventRow] = []
        self.event_filter = AM.EventFilter()
        self.health = ""
        self._signature = None
        self._file_size = 0
        self._inode = None
        self._last_id = 0
        self._loading = False
        self._side_loading = False
        self._refresh_timers: list = []

    def compose(self) -> ComposeResult:
        with Vertical(id="events-main"):
            yield EventsTable(self, id="events-table")
            yield EventFilterInput(placeholder="type=parked,merged campaign=SPEC-0230 alerts",
                                   id="events-filter")
            yield Static("", id="events-status")
        with VerticalScroll(id="events-side"):
            for pid, title in (("conditions", "conditions"), ("ping", "conductor ping"),
                               ("followers", "followers")):
                panel = Static("", id=f"events-{pid}", classes="events-panel")
                panel.border_title = title
                yield panel

    @property
    def table(self) -> EventsTable:
        return self.query_one("#events-table", EventsTable)

    def on_mount(self) -> None:
        self.table.add_columns("#id", "time", "type", "campaign", "text")
        self._refresh_timers = [self.set_interval(POLL_S, self._poll, pause=True),
                        self.set_interval(SIDE_S, self.reload_side, pause=True)]

    def on_resize(self, event) -> None:
        if self.shown:
            self._repaint(keep=True)

    # --- lifecycle -----------------------------------------------------------------------

    def activate(self) -> None:
        """Load, focus, and start the timers."""
        for t in self._refresh_timers:
            t.resume()
        self.table.focus()
        self._signature = None              # force a look: the file may have moved while away
        self._poll()
        self.reload_side()

    def deactivate(self) -> None:
        for t in self._refresh_timers:
            t.pause()

    # --- stream --------------------------------------------------------------------------

    def _poll(self) -> None:
        if AM.events_signature(self.cfg) != self._signature and not self._loading:
            self._loading = True
            self.run_worker(self._load_async(), name="events-load", group="events-load")

    async def _load_async(self) -> None:
        try:
            # Signature first: a write that lands during the load triggers another load.
            sig = AM.events_signature(self.cfg)
            size = sig[0] if sig else 0
            inode = self._events_inode()
            # Shrunk, gone, or swapped for another file (an atomic replace keeps ids monotonic
            # per file, so a bigger replacement would otherwise look like an append).
            reset = (size < self._file_size or (sig is None and self._last_id > 0)
                     or (self._inode is not None and inode != self._inode))
            try:
                rows = await asyncio.to_thread(
                    AM.load_events, self.cfg, 0 if reset else self._last_id,
                    AM.EVENT_LIMIT)
                if not rows and self._last_id and not reset:
                    # Changed without growing the id range: replaced by a file with older ids?
                    full = await asyncio.to_thread(AM.load_events, self.cfg, 0, AM.EVENT_LIMIT)
                    if not full or full[-1].id < self._last_id:
                        reset, rows = True, full
            except (OSError, ValueError) as e:
                self.query_one("#events-status", Static).update(
                    f"[red]events failed: {escape(str(e))}[/]")
                return
            self._signature, self._file_size, self._inode = sig, size, inode
            if reset:
                self.loaded, self.shown, self._last_id = [], [], 0
                self.table.clear()
            self._append(rows)
        finally:
            self._loading = False

    def _events_inode(self):
        try:
            return os.stat(AM.EV.events_path(self.cfg)).st_ino
        except OSError:
            return None

    def _append(self, rows) -> None:
        table = self.table
        first = not self.loaded
        at_tail = first or table.row_count == 0 or table.cursor_row >= table.row_count - 1
        if rows:
            self._last_id = rows[-1].id
        self.loaded.extend(rows)
        trimmed = len(self.loaded) > AM.EVENT_LIMIT
        if trimmed:
            self.loaded = self.loaded[-AM.EVENT_LIMIT:]
        if trimmed or first:
            self.shown = [r for r in self.loaded if AM.matches(r, self.event_filter)]
            self._repaint(tail=at_tail, keep=not at_tail)
        else:
            new = [r for r in rows if AM.matches(r, self.event_filter)]
            width = self._text_width()
            for r in new:
                table.add_row(*self._cells(r, width), key=str(r.id))
            self.shown.extend(new)
            if new and at_tail:
                table.move_cursor(row=len(self.shown) - 1)
        self._render_status()
        self._render_ping()

    def _text_width(self) -> int:
        return max(20, (self.table.size.width or 100) - _FIXED_COST)

    def _cells(self, r, width):
        return (str(r.id), AM.event_time(r.ts), Text(r.type, style=r.style),
                R._clip(r.campaign, _CAMPAIGN_W), R._clip(r.text, width))

    def _repaint(self, *, tail=False, keep=False) -> None:
        """Rebuild the table from `self.shown`; `keep` holds the cursor on the same event."""
        table = self.table
        row = self.current_row() if keep else None
        table.clear()
        width = self._text_width()
        for r in self.shown:
            table.add_row(*self._cells(r, width), key=str(r.id))
        if not self.shown:
            return
        index = len(self.shown) - 1
        if row is not None and not tail:
            index = next((i for i, r in enumerate(self.shown) if r.id == row.id), index)
        table.move_cursor(row=index)

    def current_row(self) -> AM.EventRow | None:
        i = self.table.cursor_row
        return self.shown[i] if 0 <= i < len(self.shown) else None

    def _render_status(self) -> None:
        shown = f"{len(self.shown)}/{len(self.loaded)}"
        active = AM.format_event_filter(self.event_filter) or "none"
        self.query_one("#events-status", Static).update(
            f"[dim]{escape(self.health or 'stream health loading…')} · filter: "
            f"{escape(active)} · {shown}[/]")

    # --- filters -------------------------------------------------------------------------

    def set_filter(self, f) -> None:
        self.event_filter = f
        self.shown = [r for r in self.loaded if AM.matches(r, f)]
        self._repaint(tail=True)
        self._render_status()

    def open_filter(self) -> None:
        box = self.query_one("#events-filter", Input)
        box.add_class("visible")
        box.value = AM.format_event_filter(self.event_filter)
        box.focus()

    def close_filter(self) -> None:
        self.query_one("#events-filter", Input).remove_class("visible")
        self.table.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "events-filter":
            return
        event.stop()
        try:
            f = AM.parse_event_filter(event.value)
        except ValueError as e:
            self.app.set_hint(f"filter: {e}")          # the old filter stays in force
            return
        self.app.set_hint("")
        self.close_filter()
        self.set_filter(f)

    def toggle_alerts(self) -> None:
        f = self.event_filter
        self.set_filter(AM.EventFilter(f.types, f.campaign, not f.alerts_only))

    def toggle_campaign(self) -> None:
        f = self.event_filter
        if f.campaign:
            self.set_filter(AM.EventFilter(f.types, None, f.alerts_only))
            return
        row = self.current_row()
        if row is None or not row.campaign:
            self.app.notify("no campaign on this event")
            return
        self.set_filter(AM.EventFilter(f.types, row.campaign, f.alerts_only))

    def jump_to_tail(self) -> None:
        if self.shown:
            self.table.move_cursor(row=len(self.shown) - 1)

    def open_selected(self) -> None:
        row = self.current_row()
        if row and row.campaign:
            self.post_message(QueueView.CampaignSelected(row.campaign, ""))

    # --- side panels ---------------------------------------------------------------------

    def reload_side(self) -> None:
        if self._side_loading:
            return
        self._side_loading = True
        self.run_worker(self._side_async(), name="events-side", group="events-side")

    async def _side_async(self) -> None:
        try:
            try:
                conditions = await asyncio.to_thread(AM.load_conditions, self.cfg)
            except (OSError, ValueError) as e:
                conditions = f"conditions failed: {e}"
            try:
                followers = await asyncio.to_thread(AM.load_followers, self.cfg)
                self.health = AM.stream_health(self.cfg, followers)
            except (OSError, ValueError) as e:
                followers = f"followers failed: {e}"
            self._render_conditions(conditions)
            self._render_followers(followers)
            self._render_status()
            self._render_ping()
        finally:
            self._side_loading = False

    def _render_conditions(self, rows) -> None:
        panel = self.query_one("#events-conditions", Static)
        if isinstance(rows, str):
            panel.update(Text(rows, style="red"))
        elif not rows:
            panel.update(Text(AM.NO_ALERTS, style="green"))
        else:
            out = Text()
            for i, r in enumerate(rows):
                out.append(("\n" if i else "") + f"{r.type} {r.campaign or '-'} {r.text}".strip(),
                           style=r.style)
            panel.update(out)

    def _render_followers(self, rows) -> None:
        panel = self.query_one("#events-followers", Static)
        if isinstance(rows, str):
            panel.update(Text(rows, style="red"))
        elif not rows:
            panel.update(Text("no followers", style="dim"))
        else:
            out = Text()
            for i, r in enumerate(rows):
                age = "-" if r.cursor_age_s is None else AM._ago(r.cursor_age_s)
                out.append(("\n" if i else "") + f"{r.reader} {r.kind} pid {r.pid} lag {r.lag} "
                           f"cursor {age}" + (f" {r.flag}" if r.flag else ""),
                           style={"dead": "red", "lagging": "yellow"}.get(r.flag, ""))
            panel.update(out)

    def _render_ping(self) -> None:
        self.query_one("#events-ping", Static).update(
            AM.ping_text(self.loaded, self.cfg))
