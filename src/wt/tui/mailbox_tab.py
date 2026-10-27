"""Mailbox tab widgets (SPEC-0249): inboxes, threads unanswered-first, reply and send as human.

Read-only until a confirmed send, and a send goes through `mailbox.send` with `frm="human"`.
Nothing here follows a stream, takes a reader lock or syncs a cursor: a steward's own follower
holds `mailbox:<addr>` for its whole life and must never be displaced. A `stat()` signature poll
catches appends from other processes.
"""
import asyncio

from rich.markup import escape
from rich.text import Text

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Static

from .. import report as R
from ..agentic import mailbox as MB
from ..agentic import store
from ..agentic.cli_queue import _ago
from . import agentic_model as AM
from .queue_tab import QueueView

POLL_S = 2.0
NO_CAMPAIGN = "(none)"
REPLY_KINDS = ["answer", "info", "request", "question"]


class _MailboxTable(DataTable):
    """Shared keys: its bindings shadow the desk's (`r`, `j`, `k`, `tab`)."""

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("n", "new_message", "new"),
        Binding("tab,shift+tab", "other_table", "inbox/threads", show=False),
    ]

    def __init__(self, view, **kw):
        super().__init__(cursor_type="row", zebra_stripes=False, **kw)
        self.view = view

    def action_new_message(self) -> None:
        self.view.start_new()

    def action_other_table(self) -> None:
        self.view.focus_other(self)


class InboxTable(_MailboxTable):
    pass


class ThreadTable(_MailboxTable):
    BINDINGS = [
        Binding("r", "reply", "reply"),
        Binding("enter", "open", "open", show=False),
    ]

    def action_reply(self) -> None:
        self.view.start_reply()

    def action_open(self) -> None:
        self.view.open_selected()


class ThreadPane(VerticalScroll):
    BINDINGS = [Binding("escape,tab,shift+tab", "back", "threads", show=False)]

    def __init__(self, view, **kw):
        super().__init__(**kw)
        self.view = view

    def action_back(self) -> None:
        self.view.threads_table.focus()


class MailboxView(Horizontal):
    DEFAULT_CSS = """
    MailboxView { height: 1fr; }
    MailboxView #mb-inboxes { width: 28; }
    MailboxView #mb-right { width: 1fr; }
    MailboxView #mb-threads { height: 1fr; }
    MailboxView #mb-pane { height: 1fr; border-top: solid $panel; padding: 0 1; }
    """

    def __init__(self, cfg, **kw):
        super().__init__(**kw)
        self.cfg = cfg
        self.inbox: str | None = None           # selected address
        self.root: str | None = None            # selected thread root id
        self.msgs: list = []
        self.inboxes: list[AM.InboxRow] = []
        self.threads: list[AM.ThreadRow] = []
        self.pane_text = Text()
        self._signature = None
        self._loading = False
        self._again = False
        self._busy = False                      # a reply / new-message flow is open
        self._timer = None

    def compose(self) -> ComposeResult:
        yield InboxTable(self, id="mb-inboxes")
        with Vertical(id="mb-right"):
            yield ThreadTable(self, id="mb-threads")
            with ThreadPane(self, id="mb-pane"):
                yield Static("", id="mb-thread")

    @property
    def inboxes_table(self) -> InboxTable:
        return self.query_one("#mb-inboxes", InboxTable)

    @property
    def threads_table(self) -> ThreadTable:
        return self.query_one("#mb-threads", ThreadTable)

    @property
    def pane(self) -> ThreadPane:
        return self.query_one("#mb-pane", ThreadPane)

    def on_mount(self) -> None:
        self.inboxes_table.add_columns("address", "open/total")
        self.threads_table.add_columns("!", "peer", "subject", "campaign", "n", "last")
        self._timer = self.set_interval(POLL_S, self._poll, pause=True)

    def on_resize(self, event) -> None:
        if self.threads:
            self.paint_threads()

    # --- lifecycle -----------------------------------------------------------------------

    def activate(self) -> None:
        self._timer.resume()
        self.threads_table.focus()
        self.reload()

    def deactivate(self) -> None:
        self._timer.pause()

    def focus_other(self, table) -> None:
        (self.threads_table if table is self.inboxes_table else self.inboxes_table).focus()

    # --- loading -------------------------------------------------------------------------

    def _poll(self) -> None:
        if AM.mailbox_signature(self.cfg) != self._signature:
            self.reload()

    def reload(self) -> None:
        if self._loading:
            self._again = True                  # the state asked for may have moved on
            return
        self._loading = True
        self.run_worker(self._reload_async(), name="mailbox-reload", group="mailbox-reload")

    async def _reload_async(self) -> None:
        try:
            # Signature first: an append that lands during the load triggers another reload.
            self._signature = AM.mailbox_signature(self.cfg)
            asked, asked_root = self.inbox, self.root
            try:
                snap = await asyncio.to_thread(AM.load_mailbox, self.cfg, asked)
            except (OSError, ValueError, TypeError, KeyError) as e:
                self.app.notify(f"mailbox failed: {escape(str(e))}", severity="error")
                return
            if asked is not None and self.inbox != asked:
                return                          # the selection moved on: the _again reload
            if self._again and self.root != asked_root:
                return                          # a new thread was selected: reload keeps it
            self.msgs, self.inboxes, self.inbox, self.threads = (
                snap.msgs, snap.inboxes, snap.inbox, snap.threads)
            self.paint_inboxes()
            self.paint_threads()
        finally:
            self._loading = False
            if self._again:
                self._again = False
                self.reload()

    def paint_inboxes(self) -> None:
        table = self.inboxes_table
        table.clear()
        for r in self.inboxes:
            table.add_row(Text(r.address, style="bold" if r.unanswered else ""),
                          f"{r.unanswered}/{r.total}", key=r.address)
        if self.inbox in [r.address for r in self.inboxes]:
            table.move_cursor(row=[r.address for r in self.inboxes].index(self.inbox))

    def paint_threads(self) -> None:
        table = self.threads_table
        table.clear()
        subject_w = max(20, (table.size.width or 100) - 40)
        for r in self.threads:
            age = AM.age_s(r.last_ts)
            table.add_row(Text("!", style="bold yellow") if r.unanswered else "",
                          Text(R._clip(str(r.peer), 22)),     # Text: no markup parsing of
                          Text(R._clip(str(r.subject), subject_w)),   # agent-written cells
                          Text(R._clip(str(r.campaign or ""), 24), style="dim"), str(r.count),
                          Text(_ago(age), style="dim"), key=r.root_id)
        roots = [r.root_id for r in self.threads]
        if self.root not in roots:
            self.root = roots[0] if roots else None
        if self.root is not None:
            table.move_cursor(row=roots.index(self.root))
        self.paint_pane()

    def paint_pane(self) -> None:
        self.pane_text = (AM.thread_text(self.cfg, self.root, self.msgs)
                          if self.root else Text("(no conversations in this inbox)", style="dim"))
        self.query_one("#mb-thread", Static).update(self.pane_text)

    # --- cursor events -------------------------------------------------------------------
    # The row is read from the table's cursor, not from the event: painting posts a highlight
    # for the transient row 0 before the one we move to, and those stale events must be no-ops.

    @staticmethod
    def _cursor_key(table):
        if 0 <= table.cursor_row < len(table.ordered_rows):
            return table.ordered_rows[table.cursor_row].key.value
        return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.control is self.inboxes_table:
            key = self._cursor_key(self.inboxes_table)
            if key is not None and key != self.inbox:
                self.inbox, self.root = key, None
                self.reload()
        elif event.control is self.threads_table:
            key = self._cursor_key(self.threads_table)
            if key is not None and key != self.root:
                self.root = key
                self.paint_pane()

    def current_thread(self) -> AM.ThreadRow | None:
        key = self._cursor_key(self.threads_table)
        return next((r for r in self.threads if r.root_id == key), None)

    # --- enter ---------------------------------------------------------------------------

    def open_selected(self) -> None:
        row = self.current_thread()
        if row is None:
            return
        if row.campaign:
            self.post_message(QueueView.CampaignSelected(row.campaign))
        else:
            self.pane.focus()

    # --- reply / new message -------------------------------------------------------------

    def start_reply(self) -> None:
        row = self.current_thread()
        if row is not None and not self._busy:
            self._busy = True
            self.run_worker(self._guarded(self._reply_flow(row)), name="mailbox-send")

    def start_new(self) -> None:
        if not self._busy:
            self._busy = True
            self.run_worker(self._guarded(self._new_flow()), name="mailbox-send")

    async def _guarded(self, flow) -> None:
        try:
            await flow
        except (ValueError, OSError) as e:
            self.app.notify(escape(str(e)), severity="error")
        finally:
            self._busy = False

    async def _reply_flow(self, row: AM.ThreadRow) -> None:
        from .app import BodyPrompt, ChoiceScreen
        reply_to = row.reply_to
        to, re = AM.reply_target(reply_to)
        kind = await self.app.push_screen_wait(
            ChoiceScreen("kind", REPLY_KINDS, AM.default_reply_kind(reply_to)))
        if kind is None:
            return
        text = await self.app.push_screen_wait(BodyPrompt(f"{kind} to {to} re {re or '-'}"))
        if text is None:
            return
        plan = AM.send_plan(self.cfg, to=to, kind=kind, text=text, campaign=reply_to.get("campaign"),
                            re=re)
        title = f"send {kind} to {to} re {re or '-'}?" + AM.answers_notice(self.msgs, re)
        if await self.app.push_screen_wait(ChoiceScreen(title, ["send", "cancel"])) != "send":
            return
        await asyncio.to_thread(AM.send_apply, self.cfg, plan)
        self.app.notify(f"sent {kind} to {to}")
        self.reload()                           # inbox and thread stay selected

    async def _new_flow(self) -> None:
        from .app import BodyPrompt, ChoiceScreen
        cfg = self.cfg
        to = await self.app.push_screen_wait(ChoiceScreen(
            "to", [a for a in MB.known_addresses(cfg) if a != AM.HUMAN]))
        if to is None:
            return
        kind = await self.app.push_screen_wait(ChoiceScreen("kind", list(MB.KINDS), "request"))
        if kind is None:
            return
        labels = {f"{c['id']}  {c.get('spec_id') or ''}".rstrip(): c["id"]
                  for c in store.list_campaigns(cfg)}
        pick = await self.app.push_screen_wait(
            ChoiceScreen("campaign", [NO_CAMPAIGN, *labels], NO_CAMPAIGN))
        if pick is None:
            return
        campaign = labels.get(pick)             # (none) is not a label: no campaign
        text = await self.app.push_screen_wait(BodyPrompt(f"{kind} to {to}"))
        if text is None:
            return
        plan = AM.send_plan(cfg, to=to, kind=kind, text=text, campaign=campaign)
        title = f"send {kind} to {plan['to']}" + (f" [{campaign}]" if campaign else "") + "?"
        if await self.app.push_screen_wait(ChoiceScreen(title, ["send", "cancel"])) != "send":
            return
        rec = await asyncio.to_thread(AM.send_apply, cfg, plan)
        self.app.notify(f"sent {kind} to {plan['to']}")
        self.inbox, self.root = rec["to"], rec["id"]     # the new thread is its own root
        self.reload()
