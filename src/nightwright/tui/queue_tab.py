"""Queue tab widgets (SPEC-0241): a live DataTable over `agentic_model.load_queue`.

Read-only by construction: nothing here syncs an event cursor, takes a reader lock or follows
a stream. A `stat()` signature poll catches state writes; the conductor's `conductor` reader is
never competed with. Writes happen only through `agentic_model.queue_action` after a confirm.
"""
import asyncio

from rich.markup import escape
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Static

from .. import report as R
from . import agentic_model as AM

POLL_S = 2.0
RELOAD_S = 15.0
_ID_W = 30             # campaign / spec column
_STATE_W = 26
_LOOP_W = 5
_COST_W = 15           # `$12.34 ████████`
_TIME_W = 6
_FIXED_COST = 3 + _ID_W + _STATE_W + _LOOP_W + _COST_W + _TIME_W + 2 * 8 + 2   # + padding
_MIN_WHY = 12
_GROUP_STYLE = {"needs you": "bold red", "conductor": "bold yellow", "closing": "bold green",
                "reviewing": "bold cyan", "gating": "bold magenta", "building": "bold blue",
                "waiting": "bold", "queued": "bold", "delivered": "bold dim"}


from .campaign_detail import DetailOpener, OpenCampaign
from .queue_actions import QueueActions


class QueueTable(DetailOpener, DataTable):
    """The focused widget of the tab: its bindings shadow the desk's (`p`, `r`, `j`/`k`, …)."""

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("p", "act('park')", "park"),
        Binding("u", "act('resume')", "resume"),
        Binding("P", "act('prioritize')", "priority"),
        Binding("A", "act('abandon')", "abandon"),
        Binding("r", "refresh_queue", "refresh"),
        Binding("enter", "open_selected", "open", show=False),
    ]

    def __init__(self, view, **kw):
        super().__init__(cursor_type="row", zebra_stripes=False, **kw)
        self.view = view

    def action_act(self, action: str) -> None:
        self.view.start_action(action)

    def action_refresh_queue(self) -> None:
        self.view.reload()

    def action_open_selected(self) -> None:
        self.view.open_selected()

    def detail_target(self, row_key):
        key = str(row_key or "")
        if not key or key.startswith("#"):
            return None
        for r in self.view.rows:
            if r.kind == "campaign" and f"{r.group}|{r.campaign}" == key:
                return (r.campaign, None)
        return None

    # --- header rows are never the cursor row (SPEC-0410) ----------------------------------

    def _data_indexes(self) -> list:
        return [i for i, r in enumerate(self.view.rows) if r.kind == "campaign"]

    def _go(self, target: int, direction: int) -> None:
        """Move to the first data row at or beyond `target` in `direction`; if there is none,
        to the last data row on that side (only when it is past the cursor)."""
        data = self._data_indexes()
        here = self.cursor_row
        ahead = [i for i in data if (i >= target if direction > 0 else i <= target)]
        pick = (min(ahead) if direction > 0 else max(ahead)) if ahead else None
        if pick is None:
            beyond = [i for i in data if (i > here if direction > 0 else i < here)]
            pick = (max(beyond) if direction > 0 else min(beyond)) if beyond else None
        if pick is not None and pick != here:
            self.move_cursor(row=pick)

    def action_cursor_down(self) -> None:
        self._go(self.cursor_row + 1, 1)

    def action_cursor_up(self) -> None:
        self._go(self.cursor_row - 1, -1)

    def action_page_down(self) -> None:
        self._go(self.cursor_row + max(1, self.size.height - 2), 1)

    def action_page_up(self) -> None:
        self._go(self.cursor_row - max(1, self.size.height - 2), -1)

    def action_scroll_home(self) -> None:
        self._go(0, 1)

    def action_scroll_end(self) -> None:
        self._go(len(self.view.rows) - 1, -1)

    action_scroll_top = action_scroll_home
    action_scroll_bottom = action_scroll_end

    def on_data_table_row_highlighted(self, event) -> None:
        """A click or a repaint that lands on a header row snaps to the nearest data row."""
        rows = self.view.rows
        i = self.cursor_row                         # the live cursor, not a stale event's
        if not rows or not (0 <= i < len(rows)) or rows[i].kind == "campaign":
            return
        data = self._data_indexes()
        if not data:
            return
        after = [d for d in data if d > i]
        self.move_cursor(row=min(after) if after else max(data))


class QueueView(QueueActions, Vertical):
    """A `QueueTable` plus a status line (header_line, or the last load error)."""

    class CampaignSelected(Message):
        def __init__(self, campaign_id: str, title: str = "") -> None:
            super().__init__()
            self.campaign_id = campaign_id
            self.title = title

    def __init__(self, cfg, **kw):
        super().__init__(**kw)
        self.cfg = cfg
        self.rows: list[AM.QueueRow] = []
        self.header = ""
        self._signature = None
        self._loading = False
        self._refresh_timers: list = []

    def compose(self) -> ComposeResult:
        yield QueueTable(self, id="queue")
        yield Static("", id="queue-status")

    @property
    def table(self) -> QueueTable:
        return self.query_one("#queue", QueueTable)

    def on_mount(self) -> None:
        self.table.add_columns("pri", "campaign / spec", "title", "state", "loop", "cost", "time",
                               "why")
        self._refresh_timers = [self.set_interval(POLL_S, self._poll, pause=True),
                        self.set_interval(RELOAD_S, self.reload, pause=True)]

    def on_resize(self, event) -> None:
        if self.rows:
            self._safe_paint()

    def _safe_paint(self) -> None:
        try:
            self.paint()
        except Exception as e:
            self._show_error(e)

    # --- lifecycle -----------------------------------------------------------------------

    def activate(self) -> None:
        """Load, focus, and start the refresh timers."""
        for t in self._refresh_timers:
            t.resume()
        self.table.focus()
        self.reload()

    def deactivate(self) -> None:
        for t in self._refresh_timers:
            t.pause()

    # --- loading -------------------------------------------------------------------------

    def _poll(self) -> None:
        try:
            changed = AM.queue_signature(self.cfg) != self._signature
        except Exception:
            changed = True                  # reload() reports the error in the status line
        if changed:
            self.reload()

    def reload(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.run_worker(self._reload_async(), name="queue-reload", group="queue-reload")

    async def _reload_async(self) -> None:
        try:
            # Signature first: a write that lands during the load triggers another reload.
            self._signature = AM.queue_signature(self.cfg)
            rows, header = await asyncio.to_thread(AM.load_queue, self.cfg)
            previous = self._cursor_target()        # against the rows the cursor is on now
            self.rows, self.header = rows, header
            self.paint(previous)
        except Exception as e:      # any failure shows in the status line
            self._signature = None          # forget the on-disk state so the next poll retries
            self._show_error(e)
        finally:
            self._loading = False

    def _show_error(self, e: Exception) -> None:
        self.query_one("#queue-status", Static).update(
            f"[red]queue failed: {escape(str(e) or repr(e))}[/]")

    def paint(self, previous=None) -> None:
        table = self.table
        previous = previous or self._cursor_target()
        table.clear()
        data = [r for r in self.rows if r.kind == "campaign"]
        avail = max(2 * _MIN_WHY, (table.size.width or 100) - _FIXED_COST)
        title_w = max(10, min(max((len(r.title) for r in data), default=0), avail - _MIN_WHY))
        why_w = max(_MIN_WHY, avail - title_w)          # `why` is clipped first, then `title`
        for r in self.rows:
            if r.kind == "header":
                label = f"{r.group} ({r.count})"
                table.add_row(Text(""), Text(label, style=_GROUP_STYLE.get(r.group, "bold")),
                              *[Text("")] * 6, key=f"#{r.group}")
            elif r.kind == "placeholder":
                table.add_row(Text(""), Text(""), Text(r.title, style="dim"), *[Text("")] * 5,
                              key="#empty")
            else:
                table.add_row(*self._cells(r, title_w, why_w), key=f"{r.group}|{r.campaign}")
        if self.rows:
            table.move_cursor(row=self._restore_index(previous))
        self.query_one("#queue-status", Static).update(f"[dim]{escape(self.header)}[/]")

    @staticmethod
    def _cells(r, title_w, why_w) -> list:
        campaign = r.campaign
        if r.spec and campaign.startswith(r.spec + "-"):
            campaign = campaign[len(r.spec) + 1:]
        ident = f"{r.spec} {campaign}".strip()
        state = f"{r.state} {r.activity}".strip()
        cost = f"${r.cost_usd or 0:.2f}"
        try:
            over = bool(r.cap_usd) and (r.cost_usd or 0) > float(r.cap_usd)
        except (TypeError, ValueError):         # a hand-edited cap: no flag, never a failed paint
            over = False
        if r.group != "delivered":
            cost = f"{cost} {AM.cost_bar(r.cost_usd, r.cap_usd)}".strip()
        return [Text(r.pri), Text(R._clip(ident, _ID_W)), Text(R._clip(r.title, title_w)),
                Text(R._clip(state, _STATE_W)), Text(r.loops),
                Text(cost, style="bold red" if over else ""), Text(r.time_text, style="dim"),
                Text(R._clip(r.why, why_w), style="dim")]

    def _cursor_target(self):
        row = self.current_row()
        index = self.table.cursor_row if self.rows else 0
        return (row.group, row.campaign, index) if row else (None, None, index)

    def _restore_index(self, previous) -> int:
        """Same (group, campaign), else the campaign's first row, else the nearest data row at
        the old index. Never a header row; with no data row, the first row."""
        group, campaign, index = previous
        if campaign:
            for i, r in enumerate(self.rows):
                if r.kind == "campaign" and (r.group, r.campaign) == (group, campaign):
                    return i
            for i, r in enumerate(self.rows):
                if r.kind == "campaign" and r.campaign == campaign:
                    return i
        data = [i for i, r in enumerate(self.rows) if r.kind == "campaign"]
        if not data:
            return 0
        index = max(0, min(index, len(self.rows) - 1))
        return min(data, key=lambda i: (abs(i - index), i))

    def current_row(self) -> AM.QueueRow | None:
        i = self.table.cursor_row
        if self.rows and 0 <= i < len(self.rows) and self.rows[i].kind == "campaign":
            return self.rows[i]
        return None

    # --- enter ---------------------------------------------------------------------------

    def select_campaign(self, cid: str) -> None:
        for i, r in enumerate(self.rows):
            if r.kind == "campaign" and r.campaign == cid:
                self.table.move_cursor(row=i)
                return

    def open_selected(self) -> None:
        row = self.current_row()
        if row:
            self.post_message(OpenCampaign(row.campaign, None, self))

    # --- actions -------------------------------------------------------------------------

    def start_action(self, action: str) -> None:
        row = self.current_row()
        if row is not None:
            self.run_queue_action(action, row.campaign, row.state, row.actions)
