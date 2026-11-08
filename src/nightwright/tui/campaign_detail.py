"""Campaign detail popup (SPEC-0407): modal shell, history timeline, queue actions, polling.

Replaces SPEC-0246's pushed `CampaignScreen` with a near-full modal and five tabs. Overview and
Timeline are live; Runs, Findings & decisions, and Mail are placeholders until follow-on specs.
"""
import asyncio
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
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

from .. import report as R
from . import agentic_model as AM
from .queue_actions import QueueActions

POLL_S = 2.0
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
    """Forwards digit keys to pane switches while the table has focus."""

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
        return str(ts)


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
    """96% × 94% modal over the desk: overview, timeline, and three placeholder panes."""

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
    #overview-text { padding: 0 1; }
    #timeline-pane { height: 1fr; layout: vertical; }
    #campaign-timeline { height: 1fr; }
    #campaign-status { height: 1; color: $text-muted; padding: 0 1; }
    #campaign-detail { height: 40%; border-top: solid $primary; padding: 0 1; }
    #placeholder-text { padding: 1 2; color: $text-muted; }
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
        self._placeholder_runs = "placeholder: runs"
        self._placeholder_records = "placeholder: records"
        self._placeholder_mail = "placeholder: mail"
        self._cursor_direction = 0
        self._detail_token = 0
        self._tab_from_action = False
        self._paint_retry_pending = False

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
                    yield Static(self._placeholder_runs, id="placeholder-text")
                with TabPane("Findings & decisions", id="tab-records"):
                    yield Static(self._placeholder_records, id="placeholder-records")
                with TabPane("Mail", id="tab-mail"):
                    yield Static(self._placeholder_mail, id="placeholder-mail")
            yield Footer()

    @property
    def table(self) -> DataTable:
        return self.query_one("#campaign-timeline", DataTable)

    def on_mount(self) -> None:
        self.query_one("#load-error", Static).update(
            Text(f"loading {self.campaign_id}…", style="dim"))
        self.table.add_columns("seq", "time", "phase", "by", "kind", "summary")
        self.query_one("#campaign-detail", VerticalScroll).can_focus = False
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
        try:
            self.paint(self._cursor_target())
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
            self._placeholder_runs = f"placeholder: runs ({extra})"
            self.query_one("#placeholder-text", Static).update(Text(self._placeholder_runs))
        elif self._focus_tab == "tab-records" and extra:
            self._placeholder_records = f"placeholder: records ({extra})"
            self.query_one("#placeholder-records", Static).update(Text(self._placeholder_records))

    def _worker_alive(self) -> bool:
        if not self.is_mounted:
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
            self.query_one("#overview-text", Static).update(
                Text("\n").join(detail.header_lines))
            if initial:
                self._apply_focus()
            try:
                self.paint(previous)
            except Exception as e:
                if previous_view is not None and not initial:
                    self.view = previous_view
                    self.query_one("#overview-text", Static).update(
                        Text("\n").join(previous_view.header_lines))
                if self._worker_alive():
                    self._handle_paint_failure(e)
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
        row = self.current_row()
        if row is None or row.get("seq") != seq or row.get("ref") != ref:
            return
        self.query_one("#campaign-detail-text", Static).update(text)

    def record_detail(self, history, record_id) -> Text:
        return AM.record_detail(history, record_id)

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
        self.table.can_focus = on_timeline
        if not on_timeline:
            self.table.blur()
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
            self.query_one("#placeholder-text", Static).focus()
        elif tab_id == "tab-records":
            self.query_one("#placeholder-records", Static).focus()
        elif tab_id == "tab-mail":
            self.query_one("#placeholder-mail", Static).focus()
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
        if self._active_tab() != "tab-timeline":
            return
        try:
            scroller = self.query_one("#campaign-detail", VerticalScroll)
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
        if self._active_tab() != "tab-timeline":
            return
        self._cursor_direction = -1
        data = self._data_indexes()
        if data:
            self.table.move_cursor(row=data[0], scroll=True)

    def action_last(self) -> None:
        if self._active_tab() != "tab-timeline":
            return
        self._cursor_direction = 1
        data = self._data_indexes()
        if data:
            self.table.move_cursor(row=data[-1], scroll=True)

    def action_down(self) -> None:
        if self._active_tab() != "tab-timeline":
            return
        self._cursor_direction = 1
        data = self._data_indexes()
        here = self.table.cursor_row
        after = [i for i in data if i > here]
        if after:
            self.table.move_cursor(row=after[0])

    def action_up(self) -> None:
        if self._active_tab() != "tab-timeline":
            return
        self._cursor_direction = -1
        data = self._data_indexes()
        here = self.table.cursor_row
        before = [i for i in data if i < here]
        if before:
            self.table.move_cursor(row=before[-1])

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
