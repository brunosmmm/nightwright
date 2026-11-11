"""Answer or withdraw one open escalation as `human` (SPEC-0250), shared by the mailbox tab and
the campaign detail popup (SPEC-0477).

`owner` is the widget or screen that runs the flow: it needs `.app` and `.reload()`. The flow
plans, asks for a confirm, and only then applies; cancel, empty text and an empty reason write
nothing. The caller wraps the coroutine in its own busy flag and error guard.
"""
import asyncio

from rich.markup import escape

from ..agentic import escalations as ESC
from ..agentic import store
from . import agentic_model as AM


def parked_for_decision(cfg, cid) -> bool:
    """The CLI's hint condition: the campaign's park reason is still `human_decision`."""
    try:
        st = store.read_state(cfg, cid)
    except (ValueError, OSError):
        return False
    return (st.get("park") or {}).get("reason") == "human_decision"


async def answer_escalation(owner, cfg, row: AM.EscRow, action) -> None:
    """`action` is `choose`, `text` or `withdraw`; `row.id` is the `ESC-…` id."""
    from .app import BodyPrompt, ChoiceScreen, TextPrompt
    eid = row.id
    app = owner.app
    if action == "choose":
        rec = await asyncio.to_thread(ESC.read, cfg, eid)
        picks = {f"{o['n']}. {o['label']}": o["n"] for o in rec.get("options") or []}
        rec_pick = next((k for k, n in picks.items() if n == rec.get("recommendation")), "")
        pick = await app.push_screen_wait(
            ChoiceScreen(f"answer {eid}", list(picks), rec_pick))
        if pick is None:
            return
        plan = await asyncio.to_thread(ESC.plan_answer, cfg, eid, choose=picks[pick],
                                       by=AM.HUMAN)
        ans = plan["answer"]
        title = f"answer {eid}: {ans['choice']}. {ans['label']}"
    elif action == "text":
        text = await app.push_screen_wait(BodyPrompt(f"answer {eid}"))
        if text is None:
            return
        if not text.strip():
            app.notify("empty answer", severity="warning")
            return
        plan = await asyncio.to_thread(ESC.plan_answer, cfg, eid, text=text, by=AM.HUMAN)
        title = f'answer {eid}: "{AM._first_line(plan["answer"]["text"])}"'
    else:
        reason = await app.push_screen_wait(TextPrompt("withdraw reason"))
        if reason is None:
            return
        if not reason.strip():
            app.notify("empty reason", severity="warning")
            return
        plan = await asyncio.to_thread(ESC.plan_withdraw, cfg, eid, reason=reason.strip(),
                                       by=AM.HUMAN)
        title = f"withdraw {eid}: {plan['reason']}"
    if await app.push_screen_wait(ChoiceScreen(escape(title), ["yes", "cancel"])) != "yes":
        return
    if action == "withdraw":
        rec = await asyncio.to_thread(ESC.apply_withdraw, cfg, plan)
        msg = f"{eid} withdrawn"
        if rec.get("campaign") and rec.get("campaign_state") == "PARKED" \
                and await asyncio.to_thread(parked_for_decision, cfg, rec["campaign"]):
            msg += (f"; {rec['campaign']} is still PARKED (human_decision); "
                    f"resume it from the queue tab (u)")
    else:
        await asyncio.to_thread(ESC.apply_answer, cfg, plan)
        msg = f"{eid} answered"
    app.notify(escape(msg))
    owner.reload()


async def guarded(owner, flow) -> None:
    """Run `flow` under `owner._busy`: a `ValueError` / `OSError` is an error notify, anything
    else `mailbox action failed: …` (the mailbox tab's `_guarded`, for a screen)."""
    try:
        await flow
    except (ValueError, OSError) as e:
        owner.app.notify(escape(str(e)), severity="error")
    except Exception as e:      # an unexpected error must not end the app
        owner.app.notify(f"mailbox action failed: {escape(repr(e))}", severity="error")
    finally:
        owner._busy = False
