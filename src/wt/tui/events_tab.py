"""Events tab widgets (SPEC-0251): the queue event stream, the watchdog conditions true now, the
latest conductor ping and the followers, over `agentic_model`.

Read-only by construction: nothing here syncs the stream, sets a cursor, takes a reader lock or
follows. The tab would otherwise become one of the followers it monitors. A `stat()` signature
poll catches appended events; the heavier panels (`watchdog_checks` reads every active ledger
tail) refresh on a slower timer.
"""
import asyncio

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
        self._dupes = 0                     # duplicate ids skipped since the last reset
        self._dirty = False                 # a failed paint left the table unreliable
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
            st = AM.events_stat(self.cfg)           # one stat: size, mtime and inode agree
            sig = st[:2] if st else None
            size = st[0] if st else 0
            inode = st[2] if st else None
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
            except Exception as e:  # a bad line must not take the desk down
                self.query_one("#events-status", Static).update(
                    f"[red]events failed: {escape(str(e))}[/]")
                return
            before = (list(self.loaded), list(self.shown), self._dupes, self._last_id)
            try:
                if reset:
                    self.loaded, self.shown, self._dupes, self._last_id = [], [], 0, 0
                    self.table.clear()
                self._append(rows)
            except Exception as e:  # a paint failure must not consume the rows: retry next poll
                self.loaded, self.shown, self._dupes, self._last_id = before
                self._dirty = True              # the table may be partial: rebuild it next time
                self.query_one("#events-status", Static).update(
                    f"[red]events failed: {escape(str(e) or repr(e))}[/]")
                return
            self._signature, self._file_size, self._inode = sig, size, inode
        except Exception as e:      # nothing here may end the app
            self.query_one("#events-status", Static).update(
                f"[red]events failed: {escape(str(e) or repr(e))}[/]")
        finally:
            self._loading = False

    def _append(self, rows) -> None:
        table = self.table
        first = not self.loaded or self._dirty
        selected = self.current_row()       # before `shown` changes: the cursor indexes the old list
        at_tail = first or table.row_count == 0 or table.cursor_row >= table.row_count - 1
        last_id = max([r.id for r in rows], default=self._last_id)
        # Dedupe in the data, so table, `shown` and `loaded` stay one-to-one (a repeated id would
        # raise DuplicateKey, and a row left out of the table only would shift `current_row`).
        seen = {r.id for r in self.loaded}
        fresh = []
        for r in rows:
            if r.id in seen:
                self._dupes += 1
            else:
                seen.add(r.id)
                fresh.append(r)
        rows = fresh
        self.loaded.extend(rows)
        trimmed = len(self.loaded) > AM.EVENT_LIMIT
        if trimmed:
            self.loaded = self.loaded[-AM.EVENT_LIMIT:]
        if trimmed or first:
            self.shown = [r for r in self.loaded if AM.matches(r, self.event_filter)]
            self._repaint(tail=at_tail, keep=selected)
        else:
            new = [r for r in rows if AM.matches(r, self.event_filter)]
            width = self._text_width()
            for r in new:
                table.add_row(*self._cells(r, width), key=str(r.id))
            self.shown.extend(new)
            if new and at_tail:
                table.move_cursor(row=len(self.shown) - 1)
        self._last_id = last_id             # only once painted: a failed paint is retried
        self._dirty = False
        self._render_status()
        self._render_ping()

    def _text_width(self) -> int:
        return max(20, (self.table.size.width or 100) - _FIXED_COST)

    def _cells(self, r, width):
        # Text, never str: DataTable parses a str cell as markup, and event text is untrusted.
        return (Text(str(r.id)), Text(AM.event_time(r.ts)), Text(r.type, style=r.style),
                Text(R._clip(r.campaign, _CAMPAIGN_W)), Text(R._clip(r.text, width)))

    def _repaint(self, *, tail=False, keep=None) -> None:
        """Rebuild the table from `self.shown`; `keep` (an EventRow, or True for the one under
        the cursor) holds the cursor on that event, or on the tail when it is gone."""
        table = self.table
        row = self.current_row() if keep is True else keep
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
        if self._dupes:
            shown += f" · {self._dupes} duplicate ids skipped"
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
            except Exception as e:  # a bad line must not take the desk down
                conditions = f"conditions failed: {e}"
            try:
                followers = await asyncio.to_thread(AM.load_followers, self.cfg)
                self.health = AM.stream_health(self.cfg, followers)
            except Exception as e:  # a bad line must not take the desk down
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
        try:
            body = AM.ping_text(self.loaded, self.cfg)
        except Exception as e:  # a malformed ping must not take the desk down
            body = Text(f"ping unreadable: {e}", style="red")
        self.query_one("#events-ping", Static).update(body)
