"""Campaign detail screen (SPEC-0246): header, filterable ledger timeline, detail pane.

Read-only like the queue tab: nothing here syncs an event cursor or takes a reader lock; a
`stat()` signature poll catches ledger and state writes. Writes happen only through the shared
`QueueActions` flow after a confirm.
"""
import asyncio

from rich.markup import escape
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Static

from .. import report as R
from . import agentic_model as AM
from .queue_tab import QueueActions

POLL_S = 2.0
_FIXED_COST = 30       # seq + time + type columns and padding
FILTERS = ("all",) + AM.GROUPS
GROUP_STYLE = {"transitions": "cyan", "runs": "", "gates": "magenta", "findings": "yellow",
               "notes": "dim"}


class CampaignScreen(QueueActions, Screen):
    DEFAULT_CSS = """
    CampaignScreen { layout: vertical; }
    #campaign-header { height: auto; padding: 0 1; background: $boost; }
    #campaign-timeline { height: 1fr; }
    #campaign-status { height: 1; color: $text-muted; padding: 0 1; }
    #campaign-detail { height: 40%; border-top: solid $primary; padding: 0 1; }
    """

    BINDINGS = [
        Binding("f", "cycle_filter", "filter"),
        Binding("v", "toggle_review", "review"),
        Binding("g", "first", "first", show=False),
        Binding("G", "last", "last", show=False),
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
        Binding("escape", "close", "back"),
        Binding("q", "close", "back", show=False),
        Binding("p", "act('park')", "park"),
        Binding("u", "act('resume')", "resume"),
        Binding("P", "act('prioritize')", "priority"),
        Binding("A", "act('abandon')", "abandon"),
    ]

    def __init__(self, cfg, campaign_id: str, **kw):
        super().__init__(**kw)
        self.cfg = cfg
        self.campaign_id = campaign_id
        self.detail: AM.CampaignDetail | None = None
        self.filter = "all"
        self.review_mode = False
        self.shown: list[AM.TimelineEntry] = []
        self._signature = None
        self._loading = False
        self._first_paint = True

    def compose(self) -> ComposeResult:
        yield Static("", id="campaign-header")
        yield DataTable(id="campaign-timeline", cursor_type="row", zebra_stripes=False)
        yield Static("", id="campaign-status")
        with VerticalScroll(id="campaign-detail"):
            yield Static("", id="campaign-detail-text")

    @property
    def table(self) -> DataTable:
        return self.query_one("#campaign-timeline", DataTable)

    def on_mount(self) -> None:
        self.table.add_columns("seq", "time", "type", "text")
        self.table.focus()
        self.set_interval(POLL_S, self._poll)
        self.reload()

    def on_resize(self, event) -> None:
        if self.detail:
            self._safe_paint()

    def _safe_paint(self) -> None:
        try:
            self.paint()
        except Exception as e:
            self._show_error(e)

    def _show_error(self, e: Exception) -> None:
        self.query_one("#campaign-status", Static).update(
            f"[red]campaign failed: {escape(str(e) or repr(e))}[/]")

    # --- loading -------------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            changed = AM.campaign_signature(self.cfg, self.campaign_id) != self._signature
        except Exception:
            changed = True                  # reload() reports the error in the status line
        if changed:
            self.reload()

    def reload(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.run_worker(self._reload_async(), name="campaign-reload", group="campaign-reload")

    async def _reload_async(self) -> None:
        try:
            detail = await asyncio.to_thread(AM.load_campaign, self.cfg, self.campaign_id)
            self._signature = detail.signature
            previous = self._cursor_target()
            self.detail = detail
            self.paint(previous)
        except Exception as e:      # any failure shows in the status line; the next poll retries
            self._show_error(e)
        finally:
            self._loading = False

    # --- painting ------------------------------------------------------------------------

    def _cursor_target(self):
        """`(seq, on_last_row)` of the row under the cursor, against the rows shown now."""
        i = self.table.cursor_row
        if not self.shown or not 0 <= i < len(self.shown):
            return (None, True)
        return (self.shown[i].seq, i == len(self.shown) - 1)

    def paint(self, previous=None) -> None:
        detail = self.detail
        if detail is None:
            return
        table = self.table
        seq, on_last = previous or self._cursor_target()
        self.query_one("#campaign-header", Static).update(Text("\n").join(detail.header))
        self.shown = [e for e in detail.entries
                        if self.filter == "all" or e.group == self.filter]
        text_w = max(20, (table.size.width or 100) - _FIXED_COST)
        table.clear()
        for e in self.shown:
            table.add_row(Text(str(e.seq)),
                          Text(e.ts[11:19] if len(e.ts) >= 19 else e.ts),
                          Text(e.type, style=GROUP_STYLE.get(e.group, "")),
                          Text(R._clip(e.text.replace("\n", " "), text_w)), key=str(e.seq))
        self.query_one("#campaign-status", Static).update(
            Text(f"filter {self.filter} · {len(self.shown)}/{len(detail.entries)} events",
                 style="dim"))
        if self.shown:
            table.move_cursor(row=self._restore_index(seq, on_last), scroll=True)
        self._first_paint = False
        self.show_detail()

    def _restore_index(self, seq, on_last) -> int:
        if self._first_paint or on_last or seq is None:
            return len(self.shown) - 1
        for i, e in enumerate(self.shown):
            if e.seq == seq:
                return i
        return len(self.shown) - 1

    def current_entry(self) -> AM.TimelineEntry | None:
        i = self.table.cursor_row
        return self.shown[i] if self.shown and 0 <= i < len(self.shown) else None

    def show_detail(self) -> None:
        pane = self.query_one("#campaign-detail-text", Static)
        if self.detail is None:
            return
        if self.review_mode:
            pane.update(self.detail.review)
            return
        try:
            entry = self.current_entry()
            pane.update(AM.entry_detail(self.cfg, self.campaign_id, entry)
                        if entry else Text("no events", style="dim"))
        except Exception as e:
            pane.update(Text(f"(detail unavailable: {e})", style="red"))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "campaign-timeline":
            self.show_detail()

    # --- actions -------------------------------------------------------------------------

    def action_cycle_filter(self) -> None:
        self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
        self._safe_paint()

    def action_toggle_review(self) -> None:
        self.review_mode = not self.review_mode
        self.show_detail()

    def action_first(self) -> None:
        if self.shown:
            self.table.move_cursor(row=0, scroll=True)

    def action_last(self) -> None:
        if self.shown:
            self.table.move_cursor(row=len(self.shown) - 1, scroll=True)

    def action_down(self) -> None:
        self.table.action_cursor_down()

    def action_up(self) -> None:
        self.table.action_cursor_up()

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_act(self, action: str) -> None:
        if self.detail is None:
            return
        self.run_queue_action(action, self.campaign_id, self.detail.state.get("state"),
                              self.detail.actions)
