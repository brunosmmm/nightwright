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
from ..agentic import store
from . import agentic_model as AM

POLL_S = 2.0
RELOAD_S = 15.0
_FIXED_COST = 44       # section + campaign + spec + padding columns
_META_W = 40


class QueueActions:
    """The prompt → plan → confirm → apply flow of the four queue actions (SPEC-0241), shared by
    the queue table's view and the campaign screen (SPEC-0246). The host provides `cfg`,
    `run_worker`, `app` and `reload()`."""

    _busy = False                           # an action flow is open

    def run_queue_action(self, action: str, campaign_id: str, state: str, actions) -> None:
        if action not in actions:
            self.app.notify(f"{action} not available for {state}")
            return
        if self._busy:
            return
        self._busy = True
        self.run_worker(self._action_flow(action, campaign_id), name="queue-action")

    async def _action_flow(self, action: str, cid: str) -> None:
        try:
            await self._flow(action, cid)
        except (ValueError, OSError) as e:
            self.app.notify(str(e), severity="error")
        except Exception as e:      # an unexpected error must not end the app
            self.app.notify(f"queue action failed: {escape(repr(e))}", severity="error")
        finally:
            self._busy = False

    async def _flow(self, action: str, cid: str) -> None:
        from .app import ChoiceScreen, TextPrompt
        cfg = self.cfg
        kw: dict = {}
        if action == "park":
            reason = await self.app.push_screen_wait(TextPrompt("park reason"))
            if reason is None:
                return
            if not reason.strip():
                self.app.notify("park needs a reason", severity="error")
                return
            kw["reason"] = reason.strip()
        elif action == "resume":
            note = await self.app.push_screen_wait(TextPrompt("resume note (optional)"))
            if note is None:
                return
            kw["note"] = note.strip() or None
        elif action == "prioritize":
            current = store.read_state(cfg, cid).get("priority")
            pick = await self.app.push_screen_wait(ChoiceScreen(
                "priority", [str(n) for n in range(1, 6)], str(current) if current else ""))
            if pick is None:
                return
            kw["priority"] = int(pick)
        plan = await asyncio.to_thread(AM.plan_queue_action, cfg, action, cid, **kw)
        choices = ["yes", "cancel"]
        if action == "abandon" and plan.get("worktree_present"):
            choices = ["abandon", "abandon, keep worktree", "cancel"]
        elif action == "abandon":
            choices = ["abandon", "cancel"]
        pick = await self.app.push_screen_wait(
            ChoiceScreen(self._summary(action, cid, plan), choices))
        if pick is None or pick == "cancel":
            return
        if pick == "abandon, keep worktree":
            kw["keep"] = True               # queue_action re-plans with keep_worktree=True
        result = await asyncio.to_thread(AM.queue_action, cfg, action, cid, **kw)
        if action == "park" and result.get("state") != "PARKED":
            self.app.notify(f"park requested for {cid} (applies after the running step)")
        else:
            self.app.notify(f"{action} {cid} done")
        self.reload()

    @staticmethod
    def _summary(action: str, cid: str, plan: dict) -> str:
        if action == "park":
            return f"park {cid} ({plan['mode']}): {plan['note']}"
        if action == "resume":
            return (f"resume {cid} → {plan['to']}"
                    + (" (waits for a slot)" if plan.get("waits_for_slot") else ""))
        if action == "prioritize":
            return f"priority {plan['from']} → {plan['to']}"
        return (f"abandon {cid}"
                + (f", removes worktree {plan['worktree']}" if plan.get("remove_worktree") else ""))


class QueueTable(DataTable):
    """The focused widget of the tab: its bindings shadow the desk's (`p`, `r`, `j`/`k`, …)."""

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("p", "act('park')", "park"),
        Binding("u", "act('resume')", "resume"),
        Binding("P", "act('prioritize')", "priority"),
        Binding("A", "act('abandon')", "abandon"),
        Binding("r", "refresh_queue", "refresh"),
        Binding("enter", "open", "open", show=False),
    ]

    def __init__(self, view, **kw):
        super().__init__(cursor_type="row", zebra_stripes=False, **kw)
        self.view = view

    def action_act(self, action: str) -> None:
        self.view.start_action(action)

    def action_refresh_queue(self) -> None:
        self.view.reload()

    def action_open(self) -> None:
        self.view.open_selected()


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
        self.table.add_columns("section", "campaign", "spec", "what", "meta")
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
        what_w = max(20, (table.size.width or 100) - _FIXED_COST - _META_W)
        last = None
        for r in self.rows:
            campaign = r.campaign
            if r.spec and campaign.startswith(r.spec + "-"):
                campaign = campaign[len(r.spec) + 1:]
            table.add_row(Text(r.section if r.section != last else "", style="dim"),
                          Text(R._clip(campaign, 24)), Text(r.spec or ""),
                          Text(R._clip(r.title, what_w)),
                          Text(R._clip(r.meta, _META_W), style="dim"),
                          key=f"{r.section}|{r.campaign}")
            last = r.section
        if self.rows:
            table.move_cursor(row=self._restore_index(previous))
        self.query_one("#queue-status", Static).update(f"[dim]{escape(self.header)}[/]")

    def _cursor_target(self):
        row = self.current_row()
        index = self.table.cursor_row if self.rows else 0
        return (row.section, row.campaign, index) if row else (None, None, index)

    def _restore_index(self, previous) -> int:
        """Same (section, campaign), else the campaign's first row, else the old index clamped."""
        section, campaign, index = previous
        if campaign:
            for i, r in enumerate(self.rows):
                if (r.section, r.campaign) == (section, campaign):
                    return i
            for i, r in enumerate(self.rows):
                if r.campaign == campaign:
                    return i
        return max(0, min(index, len(self.rows) - 1))

    def current_row(self) -> AM.QueueRow | None:
        i = self.table.cursor_row
        if self.rows and 0 <= i < len(self.rows) and self.rows[i].campaign:
            return self.rows[i]
        return None

    # --- enter ---------------------------------------------------------------------------

    def open_selected(self) -> None:
        row = self.current_row()
        if row:
            self.post_message(self.CampaignSelected(row.campaign, row.title))

    # --- actions -------------------------------------------------------------------------

    def start_action(self, action: str) -> None:
        row = self.current_row()
        if row is not None:
            self.run_queue_action(action, row.campaign, row.state, row.actions)
