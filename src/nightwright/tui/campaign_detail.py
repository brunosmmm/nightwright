"""Campaign detail popup (SPEC-0407): modal shell, history timeline, queue actions, polling.

Replaces SPEC-0246's pushed `CampaignScreen` with a near-full modal and five tabs: Overview,
Timeline, Runs (SPEC-0476), Findings & decisions and Mail (SPEC-0477).
"""
import asyncio
from collections import deque
from datetime import datetime

from rich.markup import escape
from rich.text import Text

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.events import Click, Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, Static, TabbedContent, TabPane

from .. import report as R
from ..agentic import escalations as ESC
from ..agentic import store
from . import agentic_model as AM
from . import campaign_overview as CO
from . import campaign_records as CR
from .escalation_flow import answer_escalation, guarded
from .queue_actions import QueueActions

POLL_S = 2.0
_RUN_DETAIL_DEBOUNCE_S = 0.05
_FIXED_COST = 52          # seq + time + phase + by + kind + padding
_TAB_IDS = ("tab-overview", "tab-timeline", "tab-runs", "tab-records", "tab-mail")
_PANE_BY_DIGIT = {
    "1": "tab-overview", "2": "tab-timeline", "3": "tab-runs",
    "4": "tab-records", "5": "tab-mail",
}
_GROUP_STYLE = {
    "transitions": "cyan", "runs": "", "gates": "magenta", "findings": "yellow",
    "notes": "dim", "decisions": "green",
}


class _TimelineTable(DataTable):
    """Forwards digit keys to pane switches while the table has focus (timeline and runs)."""

    BINDINGS = [
        Binding("1", "go_pane('tab-overview')", show=False, priority=True),
        Binding("2", "go_pane('tab-timeline')", show=False, priority=True),
        Binding("3", "go_pane('tab-runs')", show=False, priority=True),
        Binding("4", "go_pane('tab-records')", show=False, priority=True),
        Binding("5", "go_pane('tab-mail')", show=False, priority=True),
    ]

    def go_pane(self, tab_id: str) -> None:
        screen = self.app.screen
        if isinstance(screen, CampaignDetail):
            screen.action_tab(tab_id)

    def on_key(self, event: Key) -> None:
        pane = _PANE_BY_DIGIT.get(event.character or event.key or "")
        screen = self.app.screen
        if pane and isinstance(screen, CampaignDetail):
            screen.action_tab(pane)
            event.prevent_default()
            event.stop()


class _RecordsTable(_TimelineTable):
    """Findings & decisions table: the jump and escalation keys apply only while it has focus."""

    BINDINGS = [
        Binding("r", "jump_run", show=False),
        Binding("d", "jump_decision", show=False),
        Binding("i", "show_followup", show=False),
        Binding("c", "escalation('choose')", show=False),
        Binding("t", "escalation('text')", show=False),
        Binding("w", "escalation('withdraw')", show=False),
    ]

    def action_jump_run(self) -> None:
        self.screen.records_jump_run()

    def action_jump_decision(self) -> None:
        self.screen.records_jump_decision()

    def action_show_followup(self) -> None:
        self.screen.records_show_followup()

    def action_escalation(self, action: str) -> None:
        self.screen.records_escalation(action)


class _FilterInput(Input):
    """`escape` leaves the filter for the table instead of dismissing the popup."""

    BINDINGS = [Binding("escape", "to_table", show=False)]

    def action_to_table(self) -> None:
        self.screen.focus_records_table()


class OpenCampaign(Message):
    """Queue table (or tests) request opening the campaign detail popup."""

    def __init__(self, campaign_id: str, focus=None, origin=None) -> None:
        super().__init__()
        self.campaign_id = campaign_id
        self.focus = focus
        self.origin = origin


def _timeline_time(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%m-%d %H:%M:%S")
    except Exception:
        return AM._clean(str(ts))


class DetailOpener:
    """Mixin for a `DataTable`: Enter and double-click on a data row post `OpenCampaign`."""

    def detail_target(self, row_key):
        """`(cid, focus) | None` for `row_key` (subclass implements)."""
        raise NotImplementedError

    def _open_row(self, row_key) -> None:
        if hasattr(row_key, "value"):
            row_key = row_key.value
        target = self.detail_target(row_key)
        if target is None:
            return
        cid, focus = target
        origin = getattr(self, "view", None) or getattr(self, "_detail_origin", None)
        self.post_message(OpenCampaign(cid, focus, origin))

    def action_open_detail(self) -> None:
        row = self.cursor_row
        if row is None or row < 0 or row >= self.row_count:
            return
        self._open_row(self.ordered_rows[row].key)

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            self.action_open_detail()
            event.prevent_default()
            event.stop()

    def on_click(self, event: Click) -> None:
        if event.widget is not self:
            return
        if event.chain != 2:
            return
        meta = getattr(event.style, "meta", None) or {}
        row = meta.get("row")
        if row is None or row < 0 or row >= self.row_count:
            return
        self.move_cursor(row=row)
        self._open_row(self.ordered_rows[row].key)


class CampaignDetail(QueueActions, ModalScreen):
    """96% × 94% modal over the desk: overview, timeline, runs, findings & decisions, and mail."""

    DEFAULT_CSS = """
    CampaignDetail {
        align: center middle;
        background: $background 60%;
    }
    CampaignDetail > #detail-frame {
        width: 96%;
        height: 94%;
        border: solid $primary;
        background: $surface;
        layout: vertical;
    }
    #detail-body { height: 1fr; }
    #tab-overview { height: 1fr; overflow-y: auto; }
    #overview-text { padding: 0 1; }
    #timeline-pane { height: 1fr; layout: vertical; }
    #campaign-timeline { height: 1fr; }
    #campaign-status { height: 1; color: $text-muted; padding: 0 1; }
    #campaign-detail { height: 40%; border-top: solid $primary; padding: 0 1; }
    #runs-pane { height: 1fr; layout: vertical; }
    #campaign-runs { height: 1fr; }
    #runs-detail { height: 40%; border-top: solid $primary; padding: 0 1; }
    #records-pane, #mail-pane { height: 1fr; layout: vertical; }
    #records-filter { height: 3; }
    #records-filter-status { height: auto; color: $text-muted; padding: 0 1; }
    #campaign-records, #campaign-mail { height: 1fr; }
    #records-detail, #mail-detail { height: 40%; border-top: solid $primary; padding: 0 1; }
    #load-error { padding: 1 2; }
    """

    BINDINGS = [
        Binding("1", "tab('tab-overview')", "overview", show=False, priority=True),
        Binding("2", "tab('tab-timeline')", "timeline", show=False, priority=True),
        Binding("3", "tab('tab-runs')", "runs", show=False, priority=True),
        Binding("4", "tab('tab-records')", "records", show=False, priority=True),
        Binding("5", "tab('tab-mail')", "mail", show=False, priority=True),
        Binding("tab", "next_tab", "tab", show=False),
        Binding("shift+tab", "prev_tab", "tab", show=False),
        Binding("f", "cycle_filter", "filter"),
        Binding("v", "toggle_review", "review"),
        Binding("left_square_bracket", "prev_loop", "loop", show=False),
        Binding("right_square_bracket", "next_loop", "loop", show=False),
        Binding("g", "first", "first", show=False),
        Binding("G", "last", "last", show=False),
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
        Binding("up", "up", "up", show=False, priority=True),
        Binding("down", "down", "down", show=False, priority=True),
        Binding("pagedown,ctrl+j", "detail_scroll_down", "detail scroll", show=False,
                priority=True),
        Binding("pageup,ctrl+k", "detail_scroll_up", "detail scroll", show=False,
                priority=True),
        Binding("slash", "focus_filter", "filter", show=False),
        Binding("escape", "close", "back"),
        Binding("q", "close", "back", show=False),
        Binding("p", "act('park')", "park"),
        Binding("u", "act('resume')", "resume"),
        Binding("P", "act('prioritize')", "priority"),
        Binding("A", "act('abandon')", "abandon"),
    ]

    def __init__(self, cfg, campaign_id: str, *, focus=None, **kw):
        super().__init__(**kw)
        self.cfg = cfg
        self.campaign_id = campaign_id
        self.focus = focus
        self.view: AM.DetailView | None = None
        self._load_error: Text | None = None
        self.filter = "all"
        self.review_mode = False
        self.shown: list = []
        self._signature = None
        self._loading = False
        self._reload_pending = False
        self._first_paint = True
        self._last_highlight_row = None
        self._poll_timer = None
        self._focus_tab = "tab-overview"
        self._focus_extra: str | None = None
        self._busy = False                      # an escalation flow is open
        self._rec_rows: list = []               # Findings rows on screen (filtered, sorted)
        self._selected_record_id: str | None = None
        self._rec_index = 0
        self._followup: tuple | None = None     # (record id, line) once `i` was pressed
        self._mail_rows: list = []              # CR.MailThread, sorted
        self._selected_thread: str | None = None
        self._mail_index = 0
        self._runs_rows: list = []
        self._selected_run_id: str | None = None
        self._pending_run_select: str | None = None
        self._run_renderer = None
        self._run_offset = 0
        self._run_has_stream = False
        self._run_follow_done = False
        self._run_head_done = False
        self._run_tail_lock = asyncio.Lock()
        self._run_loaded_id: str | None = None
        self._run_detail_stale = False
        self._run_head = Text()
        self._run_body: deque = deque(maxlen=AM.RUNS_FOLLOW_MAX_LINES)
        self._run_detail_loading_run_id: str | None = None
        self._run_detail_token = 0
        self._cursor_direction = 0
        self._detail_token = 0
        self._tab_from_action = False
        self._paint_retry_pending = False
        self._pending_cursor = None

    def compose(self) -> ComposeResult:
        with Vertical(id="detail-frame"):
            yield Static("", id="load-error")
            with TabbedContent(id="detail-body", initial="tab-overview"):
                with TabPane("Overview", id="tab-overview"):
                    yield Static("", id="overview-text")
                with TabPane("Timeline", id="tab-timeline"):
                    with Vertical(id="timeline-pane"):
                        yield _TimelineTable(id="campaign-timeline", cursor_type="row",
                                        zebra_stripes=False)
                        yield Static("", id="campaign-status")
                        with VerticalScroll(id="campaign-detail"):
                            yield Static("", id="campaign-detail-text")
                with TabPane("Runs", id="tab-runs"):
                    with Vertical(id="runs-pane"):
                        yield _TimelineTable(id="campaign-runs", cursor_type="row",
                                         zebra_stripes=False)
                        with VerticalScroll(id="runs-detail"):
                            yield Static("", id="runs-detail-text")
                with TabPane("Findings & decisions", id="tab-records"):
                    with Vertical(id="records-pane"):
                        yield _FilterInput(placeholder="filter: agent:… kind:… severity:… "
                                           "status:… or words  (/ to focus)",
                                           id="records-filter")
                        yield Static("", id="records-filter-status")
                        yield _RecordsTable(id="campaign-records", cursor_type="row",
                                            zebra_stripes=False)
                        with VerticalScroll(id="records-detail"):
                            yield Static("", id="record-detail")
                with TabPane("Mail", id="tab-mail"):
                    with Vertical(id="mail-pane"):
                        yield _TimelineTable(id="campaign-mail", cursor_type="row",
                                             zebra_stripes=False)
                        with VerticalScroll(id="mail-detail"):
                            yield Static("", id="mail-detail-text")
            yield Footer()

    @property
    def table(self) -> DataTable:
        return self.query_one("#campaign-timeline", DataTable)

    @property
    def runs_table(self) -> DataTable:
        return self.query_one("#campaign-runs", DataTable)

    @property
    def records_table(self) -> DataTable:
        return self.query_one("#campaign-records", DataTable)

    @property
    def mail_table(self) -> DataTable:
        return self.query_one("#campaign-mail", DataTable)

    def on_mount(self) -> None:
        self.query_one("#load-error", Static).update(
            Text(f"loading {self.campaign_id}…", style="dim"))
        self.table.add_columns("seq", "time", "phase", "by", "kind", "summary")
        self.runs_table.add_columns(*AM.RUNS_TABLE_COLUMNS)
        self.records_table.add_columns("time", "agent", "kind", "severity", "status", "title")
        self.mail_table.add_columns("time", "from", "status", "text")
        for pane in ("#campaign-detail", "#runs-detail", "#records-detail", "#mail-detail"):
            self.query_one(pane, VerticalScroll).can_focus = False
        self.reload(initial=True)

    def on_key(self, event: Key) -> None:
        pane = _PANE_BY_DIGIT.get(event.character or event.key or "")
        if pane:
            self.action_tab(pane)
            event.prevent_default()
            event.stop()

    def on_resize(self, event) -> None:
        if self.view and self._active_tab() == "tab-timeline":
            self._safe_paint()

    def _active_tab(self) -> str:
        try:
            return self.query_one("#detail-body", TabbedContent).active
        except Exception:
            return self._focus_tab

    def _safe_paint(self) -> None:
        try:
            self.paint()
        except Exception as e:
            self._handle_paint_failure(e)

    def _show_timeline_error(self, e: Exception) -> None:
        self.query_one("#campaign-status", Static).update(
            Text(f"campaign failed: {escape(str(e) or repr(e))}", style="red"))

    def _clear_timeline_error(self) -> None:
        if self.view is None:
            return
        if self._active_tab() == "tab-timeline" and self._timeline_ready_to_paint():
            self._paint_status()
        else:
            try:
                self.query_one("#campaign-status", Static).update(Text(""))
            except Exception:
                pass

    def _should_retry_terminal_paint(self) -> bool:
        if not self.is_mounted or self.view is None or not self.view.terminal:
            return False
        return self._poll_timer is None

    def _schedule_terminal_paint_retry(self) -> None:
        if self._paint_retry_pending or not self._should_retry_terminal_paint():
            return
        self._paint_retry_pending = True
        self.call_after_refresh(self._retry_after_paint_failure)

    def _handle_paint_failure(self, e: Exception) -> None:
        self._signature = None
        self._show_timeline_error(e)
        self._schedule_terminal_paint_retry()

    def _retry_after_paint_failure(self) -> None:
        if self.view is not None and self.is_mounted:
            self.reload()

    def _timeline_ready_to_paint(self) -> bool:
        if self._active_tab() != "tab-timeline":
            return False
        table = self.table
        if not table.display:
            return False
        return (table.size.width or 0) > 0

    def _paint_after_layout(self) -> None:
        if not self.is_mounted or self.view is None:
            return
        if not self._timeline_ready_to_paint():
            return
        target = self._pending_cursor
        if target is None:
            target = self._cursor_target()
        try:
            self.paint(target)
        except Exception as e:
            self._handle_paint_failure(e)

    def _apply_focus(self) -> None:
        if self.view is None:
            return
        tab, extra = AM.resolve_focus(self.view.history, self.focus)
        self._focus_tab = {
            "overview": "tab-overview", "timeline": "tab-timeline", "runs": "tab-runs",
            "records": "tab-records", "mail": "tab-mail",
        }.get(tab, "tab-overview")
        self._focus_extra = extra
        tabs = self.query_one("#detail-body", TabbedContent)
        tabs.active = self._focus_tab
        if extra == "not found":
            missing = self.focus.get("run") or self.focus.get("record") or "?"
            self.notify(f"not found: {missing}", markup=False)
        elif self._focus_tab == "tab-runs" and extra:
            self._pending_run_select = extra
        elif self._focus_tab == "tab-records" and extra:
            self._selected_record_id = extra    # the first paint selects it; later reloads keep

    def _worker_alive(self) -> bool:
        """May a worker's tail touch this screen's widgets? Not once the screen is unmounted,
        nor while its widgets are not (or no longer) in the DOM — a dismiss tears the children
        down before `is_mounted` drops, and a worker resuming in that window used to raise
        `NoMatches('#campaign-timeline')` (SPEC-0494 resilience fix)."""
        if not self.is_mounted:
            return False
        from textual.css.query import NoMatches
        try:
            self.query_one("#campaign-timeline", DataTable)
        except NoMatches:
            return False
        from textual.worker import get_current_worker
        try:
            worker = get_current_worker()
        except Exception:
            return True
        return not worker.is_cancelled

    def _start_poll(self) -> None:
        self._stop_poll()
        if self.view is not None and not self.view.terminal:
            self._poll_timer = self.set_interval(POLL_S, self._poll)

    def _stop_poll(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None

    def _poll(self) -> None:
        if self.view is None or self.view.terminal:
            self._stop_poll()
            return
        if self._run_tail_lock.locked():
            return
        if (self._run_detail_loading_run_id is not None
                and self._run_detail_loading_run_id == self._selected_run_id):
            return
        self.run_worker(self._poll_async(), name="campaign-detail-poll",
                        group="campaign-detail-poll", exclusive=True)

    async def _poll_async(self) -> None:
        if self.view is None or self.view.terminal:
            return
        try:
            sig = await asyncio.to_thread(AM.detail_signature, self.cfg, self.campaign_id)
            if not self._worker_alive():
                return
            changed = sig != self._signature
        except Exception:
            if not self._worker_alive():
                return
            changed = True
        if changed:
            self.reload()
        else:
            await self._runs_follow_poll()

    def reload(self, *, initial=False) -> None:
        if self._loading:
            self._reload_pending = True
            return
        self._loading = True
        self.run_worker(self._reload_async(initial=initial), name="campaign-detail-reload",
                        group="campaign-detail-reload")

    async def _reload_async(self, *, initial=False) -> None:
        previous_view = self.view
        try:
            detail = await asyncio.to_thread(AM.load_detail, self.cfg, self.campaign_id)
            if not self._worker_alive():
                return
            self._signature = detail.signature
            was_terminal = self.view.terminal if self.view else False
            previous = self._cursor_target()
            previous_view = self.view
            self.view = detail
            self._load_error = None
            self.query_one("#load-error", Static).update(Text(""))
            self.query_one("#overview-text", Static).update(CO.overview(detail))
            if initial:
                self._apply_focus()
            try:
                self.paint(previous)
            except Exception as e:
                if previous_view is not None and not initial:
                    self.view = previous_view
                    self.query_one("#overview-text", Static).update(
                        CO.overview(previous_view))
                if self._worker_alive():
                    self._handle_paint_failure(e)
            self._refresh_runs_pane()
            self._refresh_records_pane()
            self._refresh_mail_pane()
            live = self.view is not None and not self.view.terminal
            if not live:
                self._stop_poll()
            elif not was_terminal or initial:
                self._start_poll()
        except Exception as e:
            if not self._worker_alive():
                return
            if previous_view is not None and not initial:
                self.view = previous_view
                self._signature = previous_view.signature
                self._show_timeline_error(e)
                if not previous_view.terminal:
                    self._start_poll()
            else:
                self._signature = None
                self.view = None
                self._load_error = Text(f"history unavailable: {e}", style="red")
                try:
                    self.query_one("#load-error", Static).update(self._load_error)
                except Exception:
                    pass
                self._stop_poll()
        finally:
            self._loading = False
            pending = self._reload_pending
            self._reload_pending = False
            if pending and self._worker_alive():
                self.reload()

    def _event_rows(self) -> list:
        """Timeline rows that are not separators."""
        return [r for r in self.shown if not r.get("separator")]

    def _data_indexes(self) -> list[int]:
        return [i for i, r in enumerate(self.shown) if not r.get("separator")]

    def _cursor_target(self):
        data = self._data_indexes()
        i = self.table.cursor_row
        if not data or i not in range(len(self.shown)):
            return (None, None, True)
        row = self.shown[i]
        if row.get("separator"):
            if self._cursor_direction < 0:
                before = [d for d in data if d < i]
                pick = before[-1] if before else data[0]
            else:
                after = [d for d in data if d > i]
                pick = after[0] if after else data[-1]
            row = self.shown[pick]
            i = pick
        on_last = data and i == data[-1]
        return (row.get("seq"), row.get("ref"), on_last)

    def _filter_row(self, row: dict) -> bool:
        if row.get("separator"):
            return True
        if self.filter == "all":
            return True
        return AM.timeline_group(row) == self.filter

    def paint(self, previous=None) -> None:
        detail = self.view
        if detail is None:
            return
        seq, ref, on_last = previous or self._cursor_target()
        grouped = detail.timeline
        visible: list = []
        for row in grouped:
            if row.get("separator"):
                continue
            if self.filter != "all" and AM.timeline_group(row) != self.filter:
                continue
            if visible and not visible[-1].get("separator"):
                prev_loop = visible[-1].get("loop", 0)
                loop = row.get("loop", 0)
                if loop != prev_loop:
                    visible.append({"separator": True, "loop": loop,
                                    "label": f"── loop {loop} ──"})
            visible.append(row)
        while visible and visible[-1].get("separator"):
            visible.pop()
        self.shown = visible
        if not self._timeline_ready_to_paint():
            # The table cursor still points at the pre-append row. Keep the
            # follow-the-end flag so the next visible paint does not treat that
            # row as a pinned older event.
            self._pending_cursor = (seq, ref, on_last)
            self._paint_retry_pending = False
            if self._active_tab() == "tab-timeline":
                self.call_after_refresh(self._paint_after_layout)
            else:
                self._clear_timeline_error()
            return
        table = self.table
        text_w = max(20, table.size.width - _FIXED_COST)
        table.clear()
        for i, row in enumerate(self.shown):
            if row.get("separator"):
                table.add_row(Text(""), Text(""), Text(""), Text(""),
                              Text(row.get("label", ""), style="bold dim"), Text(""),
                              key=f"sep:{i}")
                continue
            seq_cell = "" if row.get("seq") is None else str(row["seq"])
            group = AM.timeline_group(row)
            table.add_row(
                Text(seq_cell),
                Text(_timeline_time(row.get("ts") or "")),
                Text(str(row.get("phase") or "")),
                Text(str(row.get("by") or "")),
                Text(str(row.get("kind") or ""), style=_GROUP_STYLE.get(group, "")),
                Text(R._clip(str(row.get("summary") or "").replace("\n", " "), text_w)),
                key=f"e:{row.get('seq')}:{row.get('ref')}:{i}")
        data = self._data_indexes()
        if data:
            table.move_cursor(row=self._restore_index(seq, ref, on_last), scroll=True)
        self._paint_status()
        self._first_paint = False
        self._paint_retry_pending = False
        self._pending_cursor = None
        self.show_detail()

    def _paint_status(self) -> None:
        detail = self.view
        if detail is None:
            return
        total = len(detail.history.get("timeline") or [])
        shown_n = len(self._event_rows())
        loop_n = 0
        i = self.table.cursor_row
        if 0 <= i < len(self.shown) and not self.shown[i].get("separator"):
            loop_n = self.shown[i].get("loop", 0)
        loops = (detail.history.get("header") or {}).get("loops", 0)
        text = (f"filter {self.filter} · {shown_n}/{total} events · loop {loop_n}/{loops}")
        self.query_one("#campaign-status", Static).update(Text(text, style="dim"))

    def _restore_index(self, seq, ref, on_last) -> int:
        data = self._data_indexes()
        if not data:
            return 0
        if self._first_paint or on_last:
            return data[-1]
        if seq is not None:
            for i in data:
                if self.shown[i].get("seq") == seq:
                    return i
        elif ref is not None:
            for i in data:
                if self.shown[i].get("seq") is None and self.shown[i].get("ref") == ref:
                    return i
        return data[-1]

    def current_row(self) -> dict | None:
        i = self.table.cursor_row
        if self.shown and 0 <= i < len(self.shown) and not self.shown[i].get("separator"):
            return self.shown[i]
        return None

    def show_detail(self) -> None:
        if self._active_tab() != "tab-timeline":
            return
        pane = self.query_one("#campaign-detail-text", Static)
        if self.view is None:
            return
        if self.review_mode:
            self._detail_token += 1
            pane.update(self.view.review)
            return
        row = self.current_row()
        if row is None:
            pane.update(Text("no events", style="dim"))
            return
        try:
            if row.get("seq") is None:
                pane.update(AM.record_detail(self.view.history, row.get("ref")))
                return
            entry = AM.entry_from_timeline_row(row, self.view.events_by_seq)
            if not entry:
                pane.update(Text("(event missing)", style="yellow"))
                return
            self._detail_token += 1
            token = self._detail_token
            seq, ref = row.get("seq"), row.get("ref")
            async def work() -> None:
                await self._entry_detail_async(entry, seq, ref, token)
            self.run_worker(work, name="campaign-entry-detail",
                            group="campaign-entry-detail", exclusive=True)
        except Exception as e:
            pane.update(Text(f"(detail unavailable: {e})", style="red"))

    async def _entry_detail_async(self, entry, seq, ref, token: int) -> None:
        try:
            text = await asyncio.to_thread(AM.entry_detail, self.cfg, self.campaign_id, entry)
        except Exception as e:
            text = Text(f"(detail unavailable: {e})", style="red")
        if token != self._detail_token or self.review_mode:
            return
        if not self._worker_alive():
            return
        from textual.css.query import NoMatches
        try:
            row = self.current_row()
            if row is None or row.get("seq") != seq or row.get("ref") != ref:
                return
            self.query_one("#campaign-detail-text", Static).update(text)
        except NoMatches:                   # torn down between the check and the paint
            return

    def record_detail(self, history, record_id) -> Text:
        return AM.record_detail(history, record_id)

    def select_run(self, run_id: str) -> None:
        if self.view is None:
            return
        runs = self.view.history.get("runs") or []
        if not any(r.get("run_id") == run_id for r in runs):
            self.notify(f"not found: {run_id}", markup=False)
            return
        self.action_tab("tab-runs")
        self._select_run_row(run_id)

    def _runs_cell(self, row: dict, key: str) -> str:
        val = row.get(key)
        if val is None:
            return ""
        if key == "cost_usd" and isinstance(val, (int, float)):
            return f"{val:.4f}"
        return str(val)

    def _refresh_runs_pane(self) -> None:
        if self.view is None:
            return
        self._fill_runs_table()
        if self._pending_run_select:
            self._select_run_row(self._pending_run_select)
            self._pending_run_select = None
            return
        if not self._selected_run_id:
            return
        self._select_run_row(self._selected_run_id, keep_detail=True)
        if self._active_tab() != "tab-runs":
            # A live run's detail went stale while hidden; reload it when the tab returns.
            if (self._run_loaded_id == self._selected_run_id
                    and not (self._run_follow_done and self._run_head_done)):
                self._run_detail_stale = True
        elif (self.view.terminal and self._run_loaded_id == self._selected_run_id
              and (self._run_renderer is not None and not self._run_follow_done
                   or not self._run_head_done)):
            # The campaign ended: no later poll tick will drain the followed run or re-read
            # its verdict.
            self.run_worker(self._runs_follow_poll(final=True), name="campaign-run-final",
                            group="campaign-run-final")

    def _fill_runs_table(self) -> None:
        detail = self.view
        if detail is None:
            return
        rows = list(detail.history.get("runs") or [])
        self._runs_rows = rows
        table = self.runs_table
        table.clear()
        for row in rows:
            rid = row.get("run_id") or ""
            table.add_row(
                *(Text(AM._clean(self._runs_cell(row, k))) for k in AM.RUNS_TABLE_COLUMNS),
                key=rid)
        if rows and self._selected_run_id:
            for i, row in enumerate(rows):
                if row.get("run_id") == self._selected_run_id:
                    table.move_cursor(row=i, scroll=True)
                    break

    def _select_run_row(self, run_id: str, *, keep_detail: bool = False) -> None:
        self._selected_run_id = run_id
        for i, row in enumerate(self._runs_rows):
            if row.get("run_id") == run_id:
                self.runs_table.move_cursor(row=i, scroll=True)
                break
        if keep_detail:
            return
        self._reset_run_follow()
        if self._active_tab() == "tab-runs":
            self._load_run_detail(run_id)
        else:
            # Parsing a stream nobody is looking at is wasted work; load on tab activation.
            self._run_detail_stale = True

    def _reset_run_follow(self) -> None:
        self._run_renderer = None
        self._run_offset = 0
        self._run_has_stream = False
        self._run_follow_done = False
        self._run_head_done = False
        self._run_loaded_id = None

    def _load_run_detail(self, run_id: str) -> None:
        self._run_detail_stale = False
        self._run_detail_token += 1
        token = self._run_detail_token

        async def work() -> None:
            await self._load_run_detail_async(run_id, token)

        self.run_worker(work, name="campaign-run-detail", group="campaign-run-detail",
                        exclusive=True)

    async def _load_run_detail_async(self, run_id: str, token: int) -> None:
        self._run_detail_loading_run_id = run_id
        try:
            # Held cursor keys cancel this worker here instead of starting a parse per row.
            await asyncio.sleep(_RUN_DETAIL_DEBOUNCE_S)
            try:
                # A terminal campaign will not tick again: settle the run (flush its fragment).
                extra = (True,) if self.view is not None and self.view.terminal else ()
                snap = await asyncio.to_thread(AM.load_run_pane_detail, self.cfg, self.campaign_id,
                                               run_id, *extra)
            except Exception as e:
                snap = AM.RunPaneDetail(
                    Text(f"(run detail unavailable: {AM._clean(e)})\n", style="red"),
                    [], None, 0, False, run_id)
            if token != self._run_detail_token or not self._worker_alive():
                return
            if self._selected_run_id != run_id:
                return
            self._run_head_done = snap.has_run_json or self.view.terminal
            if self.view.terminal:
                self._run_follow_done = True
            elif snap.has_stream:
                # `ended` was read in the worker before the stream, so the latch matches it.
                self._run_follow_done = snap.ended
            else:
                self._run_follow_done = False
            self._run_renderer = snap.renderer
            self._run_offset = snap.offset
            self._run_has_stream = snap.has_stream
            self._run_loaded_id = run_id
            self._run_head = snap.head
            self._run_body = deque(snap.body, maxlen=AM.RUNS_FOLLOW_MAX_LINES)
            # A live run has no verdict yet: follow its tail. A finished one opens on its verdict.
            self._paint_runs_detail("home" if snap.ended or self.view.terminal else "end")
        finally:
            if (token == self._run_detail_token
                    and self._run_detail_loading_run_id == run_id):
                self._run_detail_loading_run_id = None

    def _paint_runs_detail(self, scroll: str | None) -> None:
        t = Text()
        t.append_text(self._run_head)
        for ln in self._run_body:
            t.append(ln)
            t.append("\n")
        self.query_one("#runs-detail-text", Static).update(t)
        if scroll and self._active_tab() == "tab-runs":
            self.call_after_refresh(self._scroll_runs_detail, scroll)

    def _scroll_runs_detail(self, where: str) -> None:
        try:
            pane = self.query_one("#runs-detail", VerticalScroll)
            if where == "end":
                pane.scroll_end(animate=False)
            else:
                pane.scroll_home(animate=False)
        except Exception:
            pass

    def _runs_detail_at_end(self) -> bool:
        try:
            pane = self.query_one("#runs-detail", VerticalScroll)
        except Exception:
            return True
        return pane.scroll_y >= pane.max_scroll_y - 1

    async def _runs_follow_poll(self, *, final: bool = False) -> None:
        if self.view is None or (self.view.terminal and not final):
            return
        if self._active_tab() != "tab-runs":
            return
        rid = self._selected_run_id
        if rid is None or (self._run_follow_done and self._run_head_done):
            return
        async with self._run_tail_lock:
            if rid != self._selected_run_id or (self._run_follow_done and self._run_head_done):
                return
            try:
                has_stream, ended = await asyncio.to_thread(
                    AM.run_follow_state, self.cfg, self.campaign_id, rid)
            except Exception:
                # An unreadable run directory: skip this tick; the next one retries.
                return
            if rid != self._selected_run_id:
                return
            if has_stream and (not self._run_has_stream or self._run_renderer is None):
                if (self._run_detail_loading_run_id != rid and not self.view.terminal):
                    self._load_run_detail(rid)
                return
            if self._run_loaded_id != rid:
                return
            terminal = self.view.terminal
            stream_pending = has_stream and not self._run_follow_done
            # exit.json lands before run.json (the verdict): keep re-reading the head until
            # run.json exists, with or without a stream.
            head_pending = not self._run_head_done and (ended or terminal)
            if not stream_pending and not head_pending:
                return
            renderer = self._run_renderer
            offset = self._run_offset
            drain = stream_pending and (ended or terminal)
            lines: list[str] = []
            head = body = None
            head_done = self._run_head_done
            try:
                if stream_pending:
                    fn = AM.stream_tail_drain if drain else AM.stream_tail
                    lines, offset = await asyncio.to_thread(
                        fn, self.cfg, self.campaign_id, rid, offset, renderer)
                if has_stream and (drain or head_pending):
                    head, head_done = await asyncio.to_thread(
                        AM.load_run_head, self.cfg, self.campaign_id, rid)
                elif head_pending:
                    snap = await asyncio.to_thread(
                        AM.load_run_pane_detail, self.cfg, self.campaign_id, rid)
                    head, body, head_done = snap.head, snap.body, snap.has_run_json
            except Exception as e:
                lines, offset = [f"(follow stopped: {AM._clean(e)})"], self._run_offset
                drain, head_done = stream_pending, True
            # The row may have moved A->B->A while the thread ran: only the renderer this
            # call started with may advance the offset.
            if (not self._worker_alive() or self._selected_run_id != rid
                    or self._run_renderer is not renderer):
                return
            self._run_offset = offset
            if drain:
                self._run_follow_done = True
            changed = head is not None and head.plain != self._run_head.plain
            if head is not None:
                self._run_head = head
            if body is not None:
                changed = changed or list(body) != list(self._run_body)
                self._run_body = deque(body, maxlen=AM.RUNS_FOLLOW_MAX_LINES)
            if head is not None or head_pending or terminal:
                self._run_head_done = head_done or terminal
            if lines or changed:
                at_end = self._runs_detail_at_end()
                self._run_body.extend(lines)
                self._paint_runs_detail("end" if at_end else None)

    @on(DataTable.RowHighlighted, "#campaign-runs")
    def _runs_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row != self.runs_table.cursor_row:
            return
        i = event.cursor_row
        if not self._runs_rows or i < 0 or i >= len(self._runs_rows):
            return
        rid = self._runs_rows[i].get("run_id")
        if rid and rid != self._selected_run_id:
            self._select_run_row(rid)

    # --- Findings & decisions (SPEC-0477) ---------------------------------------------------

    def _filter_value(self) -> str:
        return self.query_one("#records-filter", Input).value

    def _refresh_records_pane(self) -> None:
        """Rebuild the table from `history.records`; the selected id and the filter survive."""
        if self.view is None:
            return
        keep_id, keep_i = self._selected_record_id, self._rec_index
        rows, unmatched = CR.filter_records(
            CR.sort_records(self.view.history.get("records")), self._filter_value())
        self._rec_rows = rows
        self.query_one("#records-filter-status", Static).update(
            Text("  ".join(f"unmatched: {tok}" for tok in unmatched), style="yellow"))
        table = self.records_table
        table.clear()
        for rec in rows:
            table.add_row(
                Text(_timeline_time(rec.get("ts") or "")), Text(AM._clean(rec.get("agent") or "")),
                Text(AM._clean(rec.get("kind") or "")), Text(AM._clean(rec.get("severity") or "")),
                Text(CR.status_cell(rec)), Text(AM._clean(rec.get("title") or "")),
                key=str(rec["id"]))
        i = CR.nearest_index(rows, keep_id, keep_i)
        if i >= 0:
            table.move_cursor(row=i)
            self._select_record_index(i)
        else:                       # the filter matches nothing: remember the selection for later
            self._show_record_detail()

    def _select_record_index(self, i: int) -> None:
        rec = self._rec_rows[i] if 0 <= i < len(self._rec_rows) else None
        new_id = str(rec["id"]) if rec else None
        if new_id != self._selected_record_id:
            self._followup = None
        self._selected_record_id = new_id
        self._rec_index = max(i, 0)
        self._show_record_detail()

    def _current_record(self) -> dict | None:
        for rec in self._rec_rows:
            if str(rec["id"]) == self._selected_record_id:
                return rec
        return None

    def _show_record_detail(self) -> None:
        pane = self.query_one("#record-detail", Static)
        rec = self._current_record()
        if rec is None:
            pane.update(Text("no records", style="dim"))
            return
        t = CR.detail_text(rec)
        if self._followup and self._followup[0] == self._selected_record_id:
            t.append("\n")
            t.append_text(self._followup[1])
        pane.update(t)

    @on(DataTable.RowHighlighted, "#campaign-records")
    def _records_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row != self.records_table.cursor_row:
            return
        self._select_record_index(event.cursor_row)

    @on(Input.Changed, "#records-filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        if self.view is not None:
            self._refresh_records_pane()

    @on(Input.Submitted, "#records-filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.focus_records_table()

    def focus_records_table(self) -> None:
        self.records_table.can_focus = True
        self.records_table.focus()

    def action_focus_filter(self) -> None:
        if self._active_tab() == "tab-records":
            self.query_one("#records-filter", Input).focus()

    def records_jump_run(self) -> None:
        rec = self._current_record()
        run_id = CR.record_links(rec).get("run_id") if rec else None
        runs = (self.view.history.get("runs") or []) if self.view else []
        if not run_id or not any(r.get("run_id") == run_id for r in runs):
            self.notify("no run", markup=False)
            return
        self.select_run(run_id)

    def records_jump_decision(self) -> None:
        rec = self._current_record()
        target = CR.record_links(rec).get("resolved_by") if rec else None
        for i, row in enumerate(self._rec_rows):
            if target and str(row["id"]) == str(target):
                self.records_table.move_cursor(row=i, scroll=True)
                self._select_record_index(i)
                return
        self.notify("no decision", markup=False)

    def records_show_followup(self) -> None:
        rec = self._current_record()
        idea = CR.record_links(rec).get("followup_idea") if rec else None
        if not idea:
            self.notify("no follow-up", markup=False)
            return
        rid = self._selected_record_id

        async def work() -> None:
            title = await asyncio.to_thread(CR.idea_title, self.cfg, idea)
            if not self._worker_alive() or self._selected_record_id != rid:
                return
            line = Text(f"follow-up: {AM._clean(idea)}")
            if title:
                line.append(f"  {AM._clean(title)}")
            self._followup = (rid, line)
            self._show_record_detail()

        self.run_worker(work, name="campaign-followup", group="campaign-followup",
                        exclusive=True)

    def records_escalation(self, action: str) -> None:
        rec = self._current_record()
        esc_id = CR.record_links(rec).get("escalation") if rec else None
        if (rec is None or rec.get("kind") != "escalation" or rec.get("status") != "open"
                or not esc_id):
            self.notify("not an open escalation", markup=False)
            return
        if self._busy:
            return
        self._busy = True
        self.run_worker(guarded(self, self._escalation_popup_flow(str(esc_id), action)),
                        name="escalation-act")

    async def _escalation_popup_flow(self, esc_id: str, action: str) -> None:
        raw = await asyncio.to_thread(ESC.read, self.cfg, esc_id)
        row = AM.esc_row(raw)
        if row.status != "open":
            self.notify(f"{row.id} is {row.status}", severity="warning", markup=False)
        elif action == "choose" and not row.options_n:
            self.notify(f"{row.id} has no options", severity="warning", markup=False)
        else:
            await answer_escalation(self, self.cfg, row, action)

    # --- Mail (SPEC-0477) ---------------------------------------------------------------------

    def _refresh_mail_pane(self) -> None:
        if self.view is None:
            return
        keep_key, keep_i = self._selected_thread, self._mail_index
        threads = CR.mail_threads(self.view.history.get("records"))
        self._mail_rows = threads
        table = self.mail_table
        table.clear()
        for th in threads:
            m = CR.mail_message(self.cfg, th.latest)
            table.add_row(
                Text(_timeline_time(th.latest.get("ts") or "")), Text(AM._clean(m.frm)),
                Text(AM._clean(th.latest.get("status") or "")),
                Text(AM._clean(AM._first_line(m.text))), key=th.key)
        i = next((n for n, th in enumerate(threads) if th.key == keep_key), -1)
        if i < 0 and threads:
            i = max(0, min(keep_i, len(threads) - 1))
        if i >= 0:
            table.move_cursor(row=i)
        self._select_thread_index(i)

    def _select_thread_index(self, i: int) -> None:
        th = self._mail_rows[i] if 0 <= i < len(self._mail_rows) else None
        self._selected_thread = th.key if th else None
        self._mail_index = max(i, 0)
        pane = self.query_one("#mail-detail-text", Static)
        pane.update(CR.thread_detail(self.cfg, th) if th else Text("no mail", style="dim"))

    @on(DataTable.RowHighlighted, "#campaign-mail")
    def _mail_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.cursor_row != self.mail_table.cursor_row:
            return
        self._select_thread_index(event.cursor_row)

    @on(DataTable.RowHighlighted, "#campaign-timeline")
    def _row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        rows = self.shown
        i = event.cursor_row
        if rows and 0 <= i < len(rows) and rows[i].get("separator"):
            data = self._data_indexes()
            prev = self._last_highlight_row
            if prev is not None and prev != i:
                direction = 1 if i > prev else -1
            else:
                direction = self._cursor_direction
            if direction < 0:
                before = [d for d in data if d < i]
                pick = before[-1] if before else (data[0] if data else i)
            else:
                after = [d for d in data if d > i]
                pick = min(after) if after else (data[-1] if data else i)
            if pick != i:
                self.table.move_cursor(row=pick)
                self._last_highlight_row = pick
            return
        self._last_highlight_row = i
        self.show_detail()

    def _sync_tab_focus(self, tab_id: str) -> None:
        on_timeline = tab_id == "tab-timeline"
        on_runs = tab_id == "tab-runs"
        self.table.can_focus = on_timeline
        self.runs_table.can_focus = on_runs
        self.records_table.can_focus = tab_id == "tab-records"
        self.mail_table.can_focus = tab_id == "tab-mail"
        for hidden, tid in ((self.records_table, "tab-records"), (self.mail_table, "tab-mail")):
            if tab_id != tid:
                hidden.blur()
        if not on_timeline:
            self.table.blur()
        if not on_runs:
            self.runs_table.blur()
            try:
                self.query_one("#campaign-detail").blur()
            except Exception:
                pass
        if on_timeline:
            self.table.focus()
            self.call_after_refresh(self._paint_after_layout)
        elif tab_id == "tab-overview":
            self.query_one("#overview-text", Static).focus()
        elif tab_id == "tab-runs":
            self.runs_table.can_focus = True
            self.runs_table.focus()
            if self._run_detail_stale and self._selected_run_id:
                self._load_run_detail(self._selected_run_id)
        elif tab_id == "tab-records":
            self.records_table.focus()
        elif tab_id == "tab-mail":
            self.mail_table.focus()
        else:
            self.query_one("#detail-body", TabbedContent).focus()

    def action_tab(self, tab_id: str) -> None:
        tabs = self.query_one("#detail-body", TabbedContent)
        if tabs.active != tab_id:
            self._tab_from_action = True
            tabs.active = tab_id
            tabs.refresh(layout=True)
        self._sync_tab_focus(tab_id)

    @on(TabbedContent.TabActivated, "#detail-body")
    def _detail_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if self._tab_from_action:
            self._tab_from_action = False
            return
        pane = event.pane
        if pane is not None and pane.id:
            self._sync_tab_focus(pane.id)

    def action_next_tab(self) -> None:
        tabs = self.query_one("#detail-body", TabbedContent)
        i = _TAB_IDS.index(tabs.active) if tabs.active in _TAB_IDS else 0
        self.action_tab(_TAB_IDS[(i + 1) % len(_TAB_IDS)])

    def action_prev_tab(self) -> None:
        tabs = self.query_one("#detail-body", TabbedContent)
        i = _TAB_IDS.index(tabs.active) if tabs.active in _TAB_IDS else 0
        self.action_tab(_TAB_IDS[(i - 1) % len(_TAB_IDS)])

    def _detail_scroll(self, delta: int) -> None:
        tab = self._active_tab()
        if tab == "tab-timeline":
            scroller_id = "#campaign-detail"
        elif tab == "tab-runs":
            scroller_id = "#runs-detail"
        elif tab == "tab-records":
            scroller_id = "#records-detail"
        elif tab == "tab-mail":
            scroller_id = "#mail-detail"
        else:
            return
        try:
            scroller = self.query_one(scroller_id, VerticalScroll)
        except Exception:
            return
        scroller.scroll_relative(y=delta, animate=False)

    def action_detail_scroll_down(self) -> None:
        self._detail_scroll(3)

    def action_detail_scroll_up(self) -> None:
        self._detail_scroll(-3)

    def action_cycle_filter(self) -> None:
        if self._active_tab() != "tab-timeline":
            return
        filters = AM.DETAIL_FILTERS
        self.filter = filters[(filters.index(self.filter) + 1) % len(filters)]
        self._safe_paint()

    def action_toggle_review(self) -> None:
        if self._active_tab() != "tab-timeline":
            return
        self.review_mode = not self.review_mode
        self.show_detail()

    def _loop_starts(self) -> list[int]:
        """Table indexes of the first event row in each loop (among `shown`)."""
        starts = []
        prev = None
        for i, row in enumerate(self.shown):
            if row.get("separator"):
                continue
            loop = row.get("loop", 0)
            if loop != prev:
                starts.append(i)
                prev = loop
        return starts

    def _loop_group_index(self, starts: list[int], here: int) -> int:
        group = 0
        for i, s in enumerate(starts):
            if s <= here:
                group = i
        return group

    def action_prev_loop(self) -> None:
        if self._active_tab() != "tab-timeline":
            return
        self._cursor_direction = -1
        starts = self._loop_starts()
        if len(starts) < 2:
            return
        here = self.table.cursor_row
        group = self._loop_group_index(starts, here)
        if group > 0:
            self.table.move_cursor(row=starts[group - 1], scroll=True)

    def action_next_loop(self) -> None:
        if self._active_tab() != "tab-timeline":
            return
        self._cursor_direction = 1
        starts = self._loop_starts()
        if len(starts) < 2:
            return
        here = self.table.cursor_row
        group = self._loop_group_index(starts, here)
        if group + 1 < len(starts):
            self.table.move_cursor(row=starts[group + 1], scroll=True)

    def action_first(self) -> None:
        tab = self._active_tab()
        if tab == "tab-timeline":
            self._cursor_direction = -1
            data = self._data_indexes()
            if data:
                self.table.move_cursor(row=data[0], scroll=True)
        elif tab == "tab-runs" and self._runs_rows:
            self.runs_table.move_cursor(row=0, scroll=True)

    def action_last(self) -> None:
        tab = self._active_tab()
        if tab == "tab-timeline":
            self._cursor_direction = 1
            data = self._data_indexes()
            if data:
                self.table.move_cursor(row=data[-1], scroll=True)
        elif tab == "tab-runs" and self._runs_rows:
            self.runs_table.move_cursor(row=len(self._runs_rows) - 1, scroll=True)

    def action_down(self) -> None:
        tab = self._active_tab()
        if tab == "tab-timeline":
            self._cursor_direction = 1
            data = self._data_indexes()
            here = self.table.cursor_row
            after = [i for i in data if i > here]
            if after:
                self.table.move_cursor(row=after[0])
        elif tab == "tab-runs":
            here = self.runs_table.cursor_row
            if here < len(self._runs_rows) - 1:
                self.runs_table.move_cursor(row=here + 1)
        elif tab == "tab-records":
            self.records_table.action_cursor_down()
        elif tab == "tab-mail":
            self.mail_table.action_cursor_down()

    def action_up(self) -> None:
        tab = self._active_tab()
        if tab == "tab-timeline":
            self._cursor_direction = -1
            data = self._data_indexes()
            here = self.table.cursor_row
            before = [i for i in data if i < here]
            if before:
                self.table.move_cursor(row=before[-1])
        elif tab == "tab-runs":
            here = self.runs_table.cursor_row
            if here > 0:
                self.runs_table.move_cursor(row=here - 1)
        elif tab == "tab-records":
            self.records_table.action_cursor_up()
        elif tab == "tab-mail":
            self.mail_table.action_cursor_up()

    def action_close(self) -> None:
        self.dismiss(True)

    def action_act(self, action: str) -> None:
        if self.view is None:
            return
        self.run_queue_action(action, self.campaign_id, self.view.state.get("state"),
                              self.view.actions)

    def reload_after_action(self) -> None:
        """Called from `QueueActions` after an apply."""
        self.reload()


def record_detail(history, record_id) -> Text:
    return AM.record_detail(history, record_id)
