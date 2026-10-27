"""Mailbox tab widgets (SPEC-0249): inboxes, threads unanswered-first, reply and send as human.
The first inbox row, `escalations` (SPEC-0250), lists the escalation records: choose, text-answer
or withdraw one through `escalations.plan_*` / `apply_*` as `human`.

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
from ..agentic import escalations as ESC
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


class EscalationTable(_MailboxTable):
    BINDINGS = [
        Binding("c", "choose", "choose"),
        Binding("t", "text", "text answer"),
        Binding("w", "withdraw", "withdraw"),
        Binding("enter", "open", "campaign", show=False),
    ]

    def action_choose(self) -> None:
        self.view.start_escalation("choose")

    def action_text(self) -> None:
        self.view.start_escalation("text")

    def action_withdraw(self) -> None:
        self.view.start_escalation("withdraw")

    def action_open(self) -> None:
        self.view.open_escalation()


class ThreadPane(VerticalScroll):
    BINDINGS = [Binding("escape,tab,shift+tab", "back", "threads", show=False)]

    def __init__(self, view, **kw):
        super().__init__(**kw)
        self.view = view

    def action_back(self) -> None:
        self.view.right_table.focus()


class MailboxView(Horizontal):
    DEFAULT_CSS = """
    MailboxView { height: 1fr; }
    MailboxView #mb-inboxes { width: 28; }
    MailboxView #mb-right { width: 1fr; }
    MailboxView #mb-threads { height: 1fr; }
    MailboxView #mb-escalations { height: 1fr; display: none; }
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
        self.escalations: list[AM.EscRow] = []
        self.esc_id: str | None = None          # selected escalation record
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
            yield EscalationTable(self, id="mb-escalations")
            with ThreadPane(self, id="mb-pane"):
                yield Static("", id="mb-thread")

    @property
    def inboxes_table(self) -> InboxTable:
        return self.query_one("#mb-inboxes", InboxTable)

    @property
    def threads_table(self) -> ThreadTable:
        return self.query_one("#mb-threads", ThreadTable)

    @property
    def esc_table(self) -> EscalationTable:
        return self.query_one("#mb-escalations", EscalationTable)

    @property
    def in_escalations(self) -> bool:
        return self.inbox == AM.ESCALATIONS

    @property
    def right_table(self) -> _MailboxTable:
        return self.esc_table if self.in_escalations else self.threads_table

    @property
    def pane(self) -> ThreadPane:
        return self.query_one("#mb-pane", ThreadPane)

    def on_mount(self) -> None:
        self.inboxes_table.add_columns("address", "open/total")
        self.threads_table.add_columns("!", "peer", "subject", "campaign", "n", "last")
        self.esc_table.add_columns("status", "id", "campaign", "question", "age")
        self._timer = self.set_interval(POLL_S, self._poll, pause=True)

    def on_resize(self, event) -> None:
        if not (self.in_escalations or self.threads):
            return
        try:
            if self.in_escalations:
                self.paint_escalations()
            else:
                self.paint_threads()
        except Exception as e:  # bad data the reload already reported must not end the app
            self.app.notify(f"mailbox failed: {escape(str(e) or repr(e))}", severity="error")

    # --- lifecycle -----------------------------------------------------------------------

    def activate(self) -> None:
        self._timer.resume()
        self.right_table.focus()
        self.reload()

    def deactivate(self) -> None:
        self._timer.pause()

    def focus_other(self, table) -> None:
        (self.right_table if table is self.inboxes_table else self.inboxes_table).focus()

    # --- loading -------------------------------------------------------------------------

    def _poll(self) -> None:
        if self._current_signature() != self._signature:
            self.reload()

    def _current_signature(self) -> tuple:
        return AM.mailbox_signature(self.cfg), AM.escalations_signature(self.cfg)

    def reload(self) -> None:
        if self._loading:
            self._again = True                  # the state asked for may have moved on
            return
        self._loading = True
        self.run_worker(self._reload_async(), name="mailbox-reload", group="mailbox-reload")

    async def _reload_async(self) -> None:
        try:
            # Signature first: an append that lands during the load triggers another reload.
            self._signature = self._current_signature()
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
            self.msgs, self.inboxes, self.inbox, self.threads, self.escalations = (
                snap.msgs, snap.inboxes, snap.inbox, snap.threads, snap.escalations)
            self.paint_inboxes()
            self.paint_right()
        except Exception as e:      # a malformed record must not end the app
            self._signature = None              # retried by the next poll, not only on new mail
            self.app.notify(f"mailbox failed: {escape(str(e) or repr(e))}", severity="error")
        finally:
            self._loading = False
            if self._again:
                self._again = False
                self.reload()

    def paint_inboxes(self) -> None:
        table = self.inboxes_table
        table.clear()
        n_open = sum(1 for r in self.escalations if r.status == "open")
        table.add_row(Text(AM.ESCALATIONS, style="bold" if n_open else ""),
                      f"{n_open}/{len(self.escalations)}", key=AM.ESCALATIONS)
        for r in self.inboxes:
            table.add_row(Text(r.address, style="bold" if r.unanswered else ""),
                          f"{r.unanswered}/{r.total}", key=r.address)
        keys = [AM.ESCALATIONS] + [r.address for r in self.inboxes]
        if self.inbox in keys:
            table.move_cursor(row=keys.index(self.inbox))

    def paint_right(self) -> None:
        """Show the threads or the escalations table for the selected inbox, and paint it."""
        had_focus = self.threads_table.has_focus or self.esc_table.has_focus
        self.esc_table.display = self.in_escalations
        self.threads_table.display = not self.in_escalations
        if had_focus:
            self.right_table.focus()
        if self.in_escalations:
            self.paint_escalations()
        else:
            self.paint_threads()

    def paint_escalations(self) -> None:
        table = self.esc_table
        table.clear()
        question_w = max(20, (table.size.width or 100) - 50)
        for r in self.escalations:
            table.add_row(Text(r.status, style="bold yellow" if r.status == "open" else "dim"),
                          Text(r.id), Text(R._clip(str(r.campaign or "-"), 24), style="dim"),
                          Text(R._clip(str(r.question), question_w)),
                          Text(_ago(r.age_s), style="dim"), key=r.id)
        ids = [r.id for r in self.escalations]
        if self.esc_id not in ids:
            self.esc_id = ids[0] if ids else None
        if self.esc_id is not None:
            table.move_cursor(row=ids.index(self.esc_id))
        self.paint_pane()

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
        if self.in_escalations:
            self.pane_text = self._escalation_pane()
            self.query_one("#mb-thread", Static).update(self.pane_text)
            return
        self.pane_text = (AM.thread_text(self.cfg, self.root, self.msgs)
                          if self.root else Text("(no conversations in this inbox)", style="dim"))
        self.query_one("#mb-thread", Static).update(self.pane_text)

    def _escalation_pane(self) -> Text:
        if self.esc_id is None:
            return Text("(no escalations)", style="dim")
        try:
            return AM.escalation_text(self.cfg, ESC.read(self.cfg, self.esc_id))
        except ValueError as e:
            return Text(f"{self.esc_id}: {e}", style="red")

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
        elif event.control is self.esc_table:
            key = self._cursor_key(self.esc_table)
            if key is not None and key != self.esc_id:
                self.esc_id = key
                self.paint_pane()
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

    # --- escalations: choose, text answer, withdraw --------------------------------------

    def current_escalation(self) -> AM.EscRow | None:
        key = self._cursor_key(self.esc_table)
        return next((r for r in self.escalations if r.id == key), None)

    def open_escalation(self) -> None:
        row = self.current_escalation()
        if row is not None and row.campaign:
            self.post_message(QueueView.CampaignSelected(row.campaign))

    def start_escalation(self, action) -> None:
        row = self.current_escalation()
        if row is None or self._busy:
            return
        if row.status != "open":
            self.app.notify(f"{row.id} is {escape(row.status)}", severity="warning")
        elif action == "choose" and not row.options_n:
            self.app.notify(f"{row.id} has no options", severity="warning")
        else:
            self._busy = True
            self.run_worker(self._guarded(self._escalation_flow(row, action)),
                            name="escalation-act")

    async def _escalation_flow(self, row: AM.EscRow, action) -> None:
        from .app import BodyPrompt, ChoiceScreen, TextPrompt
        cfg, eid = self.cfg, row.id
        if action == "choose":
            rec = await asyncio.to_thread(ESC.read, cfg, eid)
            picks = {f"{o['n']}. {o['label']}": o["n"] for o in rec.get("options") or []}
            rec_pick = next((k for k, n in picks.items() if n == rec.get("recommendation")), "")
            pick = await self.app.push_screen_wait(
                ChoiceScreen(f"answer {eid}", list(picks), rec_pick))
            if pick is None:
                return
            plan = await asyncio.to_thread(ESC.plan_answer, cfg, eid, choose=picks[pick],
                                           by=AM.HUMAN)
            ans = plan["answer"]
            title = f"answer {eid}: {ans['choice']}. {ans['label']}"
        elif action == "text":
            text = await self.app.push_screen_wait(BodyPrompt(f"answer {eid}"))
            if text is None:
                return
            if not text.strip():
                self.app.notify("empty answer", severity="warning")
                return
            plan = await asyncio.to_thread(ESC.plan_answer, cfg, eid, text=text, by=AM.HUMAN)
            title = f'answer {eid}: "{AM._first_line(plan["answer"]["text"])}"'
        else:
            reason = await self.app.push_screen_wait(TextPrompt("withdraw reason"))
            if reason is None:
                return
            if not reason.strip():
                self.app.notify("empty reason", severity="warning")
                return
            plan = await asyncio.to_thread(ESC.plan_withdraw, cfg, eid, reason=reason.strip(),
                                           by=AM.HUMAN)
            title = f"withdraw {eid}: {plan['reason']}"
        if await self.app.push_screen_wait(ChoiceScreen(escape(title), ["yes", "cancel"])) != "yes":
            return
        if action == "withdraw":
            rec = await asyncio.to_thread(ESC.apply_withdraw, cfg, plan)
            msg = f"{eid} withdrawn"
            if rec.get("campaign") and rec.get("campaign_state") == "PARKED" \
                    and await asyncio.to_thread(self._parked_for_decision, rec["campaign"]):
                msg += (f"; {rec['campaign']} is still PARKED (human_decision); "
                        f"resume it from the queue tab (u)")
        else:
            await asyncio.to_thread(ESC.apply_answer, cfg, plan)
            msg = f"{eid} answered"
        self.app.notify(escape(msg))
        self.reload()

    def _parked_for_decision(self, cid) -> bool:
        """The CLI's hint condition: the campaign's park reason is still `human_decision`."""
        try:
            st = store.read_state(self.cfg, cid)
        except (ValueError, OSError):
            return False
        return (st.get("park") or {}).get("reason") == "human_decision"

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
        except Exception as e:      # an unexpected error must not end the app
            self.app.notify(f"mailbox action failed: {escape(repr(e))}", severity="error")
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
