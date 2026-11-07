"""Shared queue-action prompt flow (SPEC-0241), used by the queue tab and campaign detail."""
import asyncio

from rich.markup import escape

from ..agentic import store
from . import agentic_model as AM


class QueueActions:
    """The prompt → plan → confirm → apply flow of the four queue actions (SPEC-0241)."""

    _busy = False

    def run_queue_action(self, action: str, campaign_id: str, state: str, actions) -> None:
        if action not in actions:
            self.app.notify(f"{action} not available in {state}", markup=False)
            return
        if self._busy:
            return
        self._busy = True
        self.run_worker(self._action_flow(action, campaign_id), name="queue-action")

    async def _action_flow(self, action: str, cid: str) -> None:
        try:
            await self._flow(action, cid)
        except (ValueError, OSError) as e:
            self.app.notify(escape(str(e)), severity="error")
        except Exception as e:
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
            kw["keep"] = True
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
