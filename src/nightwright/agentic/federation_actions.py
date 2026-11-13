"""SPEC-0533: run the `park` / `resume` actions that `POST /v1/mail` committed to
`federation/actions.jsonl`, from the host runner tick, and answer the conductor.

The journal is one JSON object per line, the last line per `msg_uid` wins:
`pending` (written by `federation_host.post_mail`), `started` (optionally with `wait: PARKED`),
`done` (with `outcome`). Every append is fsynced before the verb or the answer that follows it.
This module is the only one that calls the queue's park/resume functions; it never takes
`mail.lock`."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import decisions as DEC
from . import events as E
from . import mailbox as MB
from . import store
from .federation_host import _append_durable, actions_path

log = logging.getLogger("federation_actions")

_FIELDS = {"verb", "campaign", "reason"}
_UNKNOWN = "unknown: host restarted during the verb"


def _lock(cfg) -> Path:
    return actions_path(cfg).with_name("actions.lock")


def _append(cfg, rec: dict) -> None:
    with E._locked(_lock(cfg)):
        _append_durable(actions_path(cfg), (json.dumps(rec) + "\n").encode("utf-8"))


def _journal(cfg, uid: str, state: str, **extra) -> None:
    _append(cfg, {"msg_uid": uid, "state": state, "ts": store.now_iso(), **extra})


def _read(cfg) -> dict[str, dict]:
    """`{msg_uid: {"to", "action", "last"}}` in order of first appearance."""
    out: dict[str, dict] = {}
    with E._locked(_lock(cfg)):
        try:
            f = open(actions_path(cfg), "rb")
        except FileNotFoundError:
            return out
        with f:
            for raw in f:
                if not raw.endswith(b"\n"):
                    break
                try:
                    rec = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                uid = rec.get("msg_uid") if isinstance(rec, dict) else None
                if not isinstance(uid, str):
                    continue
                item = out.setdefault(uid, {"to": None, "action": None, "last": None})
                if "action" in rec and item["last"] is None:
                    item["to"], item["action"] = rec.get("to"), rec["action"]
                item["last"] = rec
    return out


def _campaign_str(action) -> str:
    c = action.get("campaign") if isinstance(action, dict) else None
    return c if isinstance(c, str) else ""


def _read_state(cfg, campaign: str):
    """The campaign's state, or None when it is unreadable or `campaign` is not a plain id: a
    remote string is never joined into a store path unless it is one path component."""
    if (not campaign or campaign in (".", "..") or campaign != Path(campaign).name
            or "\\" in campaign or "\0" in campaign):
        return None
    try:
        return store.read_state(cfg, campaign)
    except ValueError:
        return None


def _refusal(cfg, to, action) -> str | None:
    """The first failing pending check's outcome, or None when the action may run."""
    if (not isinstance(action, dict) or set(action) - _FIELDS
            or ("reason" in action and not isinstance(action["reason"], str))):
        return "refused: fields"
    if action.get("verb") not in ("park", "resume"):
        return "refused: verb"
    project = to[len("steward:"):] if isinstance(to, str) and to.startswith("steward:") else ""
    if not project:
        return "refused: address"
    campaign = action.get("campaign")
    if not isinstance(campaign, str) or not campaign or "@" in campaign:
        return "refused: campaign"
    state = _read_state(cfg, campaign)
    if state is None:
        return "refused: campaign"
    if str(state.get("project") or "").casefold() != project.casefold():
        return "refused: project"
    return None


# ---- answers --------------------------------------------------------------------------------

def _has_re(cfg, addr: str, uid: str) -> bool:
    return any(r.get("re") == uid for r in MB.read_inbox(cfg, addr))


def _answer(cfg, uid: str, to, action, outcome: str) -> None:
    """Send `outcome` to the host conductor (and a refusal to the host human) until the inbox
    holds a record whose `re` is `uid`. A failed send is logged and tried again by the next drain."""
    try:
        frm = MB.validate_address(cfg, to)
    except Exception:                                        # noqa: BLE001
        frm = "human"
    campaign = _campaign_str(action)
    if not campaign or "@" in campaign or _read_state(cfg, campaign) is None:
        campaign = None
    targets = [("conductor", "answer")]
    if outcome.startswith("refused:"):
        targets.append(("human", "info"))
    for addr, kind in targets:
        try:
            if _has_re(cfg, addr, uid):
                continue
            MB.send(cfg, to=addr, frm=frm, kind=kind, re=uid, campaign=campaign,
                    text=outcome, needs_reply=False)
        except Exception as e:                               # noqa: BLE001
            log.warning("action %s: answer to %s failed: %s: %s", uid, addr, type(e).__name__, e)


def _finish(cfg, uid: str, item: dict, outcome: str) -> None:
    _journal(cfg, uid, "done", outcome=outcome)
    _answer(cfg, uid, item["to"], item["action"], outcome)


def _failed(exc: Exception) -> str:
    return f"failed: {type(exc).__name__}: {exc}"


# ---- verbs ----------------------------------------------------------------------------------

def _park(cfg, uid: str, item: dict) -> None:
    from . import queue as Q
    action = item["action"]
    campaign, reason = action["campaign"], action.get("reason") or ""
    try:
        plan = Q.plan_park(cfg, campaign, reason, by="human")
    except Exception as e:                                   # noqa: BLE001
        _journal(cfg, uid, "started")
        _finish(cfg, uid, item, _failed(e))
        return
    deferred = plan.get("mode") == "deferred"
    _journal(cfg, uid, "started", **({"wait": "PARKED"} if deferred else {}))
    try:
        Q.apply_park(cfg, plan)
    except Exception as e:                                   # noqa: BLE001
        _finish(cfg, uid, item, _failed(e))
        return
    if not deferred:
        _finish(cfg, uid, item, "ok")


def _resume(cfg, uid: str, item: dict) -> None:
    from . import queue as Q
    action = item["action"]
    campaign, reason = action["campaign"], action.get("reason") or ""
    _journal(cfg, uid, "started")
    try:
        plan = Q.plan_resume(cfg, campaign, note=reason or None, by="human")
        Q.apply_resume(cfg, plan)
    except Exception as e:                                   # noqa: BLE001
        _finish(cfg, uid, item, _failed(e))
        return
    _finish(cfg, uid, item, "ok")


def _parked_since(cfg, campaign: str, since) -> bool:
    """True when the campaign ledger has a `park` event at or after `since` (the `started` line)."""
    t0 = DEC.parse_ts(since)
    if t0 is None:
        return False
    return any((t := DEC.parse_ts(e.get("ts"))) is not None and t >= t0
               for e in store.read_ledger(cfg, campaign, types=["park"]))


def _await_parked(cfg, uid: str, item: dict) -> None:
    """The latest line is `started` with `wait: PARKED`: answer `ok` once the state is PARKED,
    re-request a park that did not stick, otherwise wait."""
    from . import queue as Q
    action = item["action"] if isinstance(item["action"], dict) else {}
    campaign = _campaign_str(action)
    state = _read_state(cfg, campaign) if campaign else None
    if state is None:
        _finish(cfg, uid, item, f"failed: ValueError: no readable campaign {campaign!r}")
        return
    if state.get("state") == "PARKED":
        _finish(cfg, uid, item, "ok")
        return
    if isinstance(state.get("park_requested"), dict):
        return
    if _parked_since(cfg, campaign, item["last"].get("ts")):
        _finish(cfg, uid, item, "ok")                        # parked, then resumed by someone else
        return
    try:
        Q.apply_park(cfg, Q.plan_park(cfg, campaign, action.get("reason") or "", by="human"))
    except Exception as e:                                   # noqa: BLE001
        _finish(cfg, uid, item, _failed(e))
        return
    state = _read_state(cfg, campaign)
    if state is not None and state.get("state") == "PARKED":
        _finish(cfg, uid, item, "ok")


def _handle(cfg, uid: str, item: dict) -> None:
    last = item["last"]
    state = last.get("state")
    if state == "done":
        outcome = last.get("outcome")
        if isinstance(outcome, str):
            _answer(cfg, uid, item["to"], item["action"], outcome)
    elif state == "started":
        if last.get("wait") == "PARKED":
            _await_parked(cfg, uid, item)
        else:
            _finish(cfg, uid, item, _UNKNOWN)
    elif state == "pending":
        why = _refusal(cfg, item["to"], item["action"])
        if why:
            _finish(cfg, uid, item, why)
        elif item["action"]["verb"] == "park":
            _park(cfg, uid, item)
        else:
            _resume(cfg, uid, item)


def drain_actions(cfg) -> None:
    """Run every unfinished action in the journal once. Inert without `federation.json` or
    `actions.jsonl`. An `Exception` of one verb is journalled as `failed`; `BaseException`s
    propagate and leave the lines already fsynced."""
    if not (Path(cfg["config_dir"]) / "secrets" / "federation.json").is_file():
        return
    if not actions_path(cfg).is_file():
        return
    for uid, item in _read(cfg).items():
        if item["last"] is None:
            continue
        try:
            _handle(cfg, uid, item)
        except Exception as e:                               # noqa: BLE001
            log.warning("action %s: %s: %s", uid, type(e).__name__, e)
