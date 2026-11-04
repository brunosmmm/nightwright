"""Answering escalations from the dashboard (SPEC-0429): `POST /api/escalations/<id>/plan` and
`/answer`. The only queue-side write the dashboard makes, and it is the CLI's own: every answer
goes through `escalations.plan_answer(..., by="human")` then `escalations.apply_answer(plan)`,
exactly as `nw escalations answer --yes` does.

The confirm step is a plan token (an HMAC over the caller's identity, the record's state and the
exact answer request, valid `PLAN_TTL_S`). Conflicts (answered, withdrawn, campaign no longer
parked for the human) are classified before anything is written and answered with 409.
Not imported by any queue-side module."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import time

from .. import escalations as ESC
from . import inbox as IB

PLAN_TTL_S = 600.0
TEXT_MAX = 4000
NOTE_MAX = 2000
ITEM_MAX = 500
LIST_MAX = 100
ID_RE = re.compile(r"^ESC-\d{4,}$")
FIELDS = {"choose": ("choose",), "text": ("text",), "override": ("waive", "attest", "reason")}
COMMON = ("form", "note")
BY = "human"


@dataclasses.dataclass(frozen=True)
class Identity:
    """Who is asking: `local` on a loopback bind, the whois login / device / node IP on the
    tailnet. `key` is what CSRF values and plan tokens are bound to."""
    login: str | None
    node: str | None
    node_ip: str | None

    @property
    def key(self) -> str:
        if self.login == "local":
            return "local"
        return f"{self.login or ''}|{self.node_ip or ''}"

    def audit(self) -> dict:
        return {"login": self.login, "node": self.node, "node_ip": self.node_ip}


class Refusal(Exception):
    """An answer request refused with `status` and the JSON `body`; nothing was written."""

    def __init__(self, status: int, body: dict, result: str | None = None):
        super().__init__(body.get("error"))
        self.status, self.body = status, body
        self.result = result or str(status)


def _bad(msg: str) -> Refusal:
    return Refusal(400, {"error": msg})


def _str(v, name, n, *, empty_ok=False) -> str:
    if not isinstance(v, str):
        raise _bad(f"{name} must be a string")
    if len(v) > n:
        raise _bad(f"{name} is longer than {n} characters")
    if not empty_ok and not v.strip():
        raise _bad(f"empty {name}")
    return v


def parse_request(body, *, with_token: bool = False) -> tuple[str, dict, str, str | None]:
    """`(form, plan_answer kwargs, canonical request, plan_token)` of a request body, or a 400
    `Refusal`. Strict: a field that does not belong to the form is refused."""
    if not isinstance(body, dict):
        raise _bad("the body must be a JSON object")
    form = body.get("form")
    if form not in FIELDS:
        raise _bad("form must be one of choose, text, override")
    allowed = set(COMMON) | set(FIELDS[form]) | ({"plan_token"} if with_token else set())
    extra = sorted(set(body) - allowed)
    if extra:
        raise _bad(f"fields not allowed with form {form}: {', '.join(extra)}")
    kw: dict = {}
    canon: dict = {"form": form}
    if form == "choose":
        v = body.get("choose")
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise _bad("choose must be an option number or label")
        kw["choose"] = canon["choose"] = _str(str(v), "choose", ITEM_MAX)
    elif form == "text":
        kw["text"] = canon["text"] = _str(body.get("text"), "text", TEXT_MAX)
    else:
        for k in ("waive", "attest"):
            v = body.get(k, [])
            if not isinstance(v, list) or len(v) > LIST_MAX:
                raise _bad(f"{k} must be a list of at most {LIST_MAX} strings")
            kw[k] = canon[k] = [_str(x, k, ITEM_MAX) for x in v]
        r = body.get("reason")
        kw["reason"] = canon["reason"] = None if r is None else _str(r, "reason", TEXT_MAX,
                                                                    empty_ok=True)
    note = body.get("note")
    if note is not None:
        note = _str(note, "note", NOTE_MAX, empty_ok=True)
    kw["note"] = canon["note"] = note
    token = body.get("plan_token") if with_token else None
    if with_token and not isinstance(token, str):
        raise _bad("plan_token is required: plan the answer first, then confirm it")
    return form, kw, json.dumps(canon, sort_keys=True, separators=(",", ":")), token


# ---- conflicts ----------------------------------------------------------------------------

def _conflict(rec: dict, msg: str) -> Refusal:
    status = rec.get("status")
    if status == "withdrawn":
        w = rec.get("withdrawn") or {}
        by, at = w.get("by"), w.get("at")
    else:
        by, at = rec.get("answered_by"), rec.get("answered_at")
    return Refusal(409, {"error": msg, "id": rec.get("id"), "status": status, "by": by,
                         "at": at})


def read_record(cfg, esc_id) -> dict:
    """The record, or a 404 `Refusal`."""
    if not isinstance(esc_id, str) or not ID_RE.match(esc_id):
        raise Refusal(404, {"error": f"not an escalation id: {esc_id!r}"})
    try:
        return ESC.read(cfg, esc_id)
    except ValueError as e:
        raise Refusal(404, {"error": str(e)})


def classify(cfg, rec: dict) -> bool:
    """Raise a 409 `Refusal` when `rec` can no longer take a human answer (the rules of
    `plan_answer` / `apply_answer`, checked first so a conflict is never reported as a form
    error); else whether the answer reverses an auto-applied one (SPEC-0425)."""
    esc_id = rec.get("id")
    reverses = ESC._reversible(rec, BY)
    if rec.get("status") != "open" and not reverses:
        when = rec.get("answered_at") if rec.get("status") == "answered" else \
            (rec.get("withdrawn") or {}).get("at")
        raise _conflict(rec, f"{esc_id} is {rec.get('status')} since {when}; only an open "
                             f"escalation can be answered")
    if rec.get("campaign") and not reverses:
        st = ESC._campaign_state(cfg, rec["campaign"])
        try:
            ESC._check_parked(esc_id, st)
        except ValueError as e:
            raise Refusal(409, {"error": str(e), "id": esc_id, "status": "not_applicable",
                                "campaign_state": (st or {}).get("state"),
                                "by": None, "at": None})
    return reverses


# ---- the plan token -------------------------------------------------------------------------

def _token_parts(identity: Identity, rec: dict, canon: str, exp: str) -> tuple:
    return ("plan", identity.key, str(rec.get("id")), str(rec.get("created")),
            str(rec.get("status")), str(rec.get("answered_at")), canon, exp)


def mint_token(csrf, identity: Identity, rec: dict, canon: str, now: float) -> tuple[str, int]:
    exp = str(int(now + PLAN_TTL_S))
    return f"{exp}.{csrf.sign(*_token_parts(identity, rec, canon, exp))}", int(exp)


def check_token(csrf, token: str, identity: Identity, rec: dict, canon: str,
                now: float) -> None:
    """A 400 `Refusal` for a token that does not match, 409 for an expired one."""
    exp, dot, mac = token.partition(".")
    if not dot or not exp.isdigit() or len(exp) > 12:
        raise Refusal(400, {"error": "bad plan token: plan the answer again"},
                      result="bad_plan_token")
    if not csrf.verify(mac, *_token_parts(identity, rec, canon, exp)):
        raise Refusal(400, {"error": "the plan token does not match this answer, this device "
                                     "or the escalation as it is now: plan the answer again"},
                      result="bad_plan_token")
    if int(exp) < now:
        raise Refusal(409, {"error": "the confirmation expired: plan the answer again",
                            "id": rec.get("id"), "status": "plan_expired",
                            "by": None, "at": None}, result="plan_expired")


# ---- plan and answer ------------------------------------------------------------------------

def _plan(cfg, esc_id, kw) -> dict:
    try:
        return ESC.plan_answer(cfg, esc_id, by=BY, **kw)
    except ESC.AnswerFormError as e:
        raise _bad(str(e))
    except ValueError as e:
        raise _bad(str(e))


def _effect(rec: dict, form: str, reverses: bool) -> str:
    return IB.effects_of(rec.get("kind"), [form], reverses=reverses)[form]


def _plan_view(plan: dict) -> dict:
    ov = plan.get("override")
    out = {k: plan.get(k) for k in ("id", "campaign", "by", "question", "answer", "summary",
                                    "reverses")}
    if ov:
        out["override"] = {
            "waive": [{k: w.get(k) for k in ("id", "severity", "title")} for w in ov["waive"]],
            "attest": list(ov["attest"]), "reason": ov["reason"],
            "implicitly_attested": list(ov["implicitly_attested"]),
            "reach": {k: list(v) for k, v in (ov.get("reach") or {}).items()}}
    else:
        out["override"] = None
    if plan.get("reverses"):
        out["reverses_summary"] = ESC.summarize(plan["reverses"])
    return out


def plan(cfg, esc_id, body, identity: Identity, csrf, now: float | None = None) -> dict:
    """`POST …/plan`: the dry-run plan, the effect text and a plan token. Writes nothing."""
    now = time.time() if now is None else now
    form, kw, canon, _ = parse_request(body)
    rec = read_record(cfg, esc_id)
    classify(cfg, rec)
    p = _plan(cfg, esc_id, kw)
    token, exp = mint_token(csrf, identity, rec, canon, now)
    return {"plan": _plan_view(p), "effect": _effect(rec, form, bool(p.get("reverses"))),
            "plan_token": token,
            "expires_at": dt.datetime.fromtimestamp(exp).astimezone().isoformat(
                timespec="seconds")}


def answer(cfg, esc_id, body, identity: Identity, csrf, now: float | None = None) -> dict:
    """`POST …/answer`: re-plan, then `apply_answer`, the CLI's write. A race lost to another
    answer between the checks and the write is a 409, with nothing written."""
    now = time.time() if now is None else now
    form, kw, canon, token = parse_request(body, with_token=True)
    rec = read_record(cfg, esc_id)
    classify(cfg, rec)
    check_token(csrf, token, identity, rec, canon, now)
    p = _plan(cfg, esc_id, kw)
    try:
        done = ESC.apply_answer(cfg, p)
    except ValueError as e:                 # the record or the park changed under us
        try:
            cur = ESC.read(cfg, esc_id)
        except ValueError:
            cur = rec
        raise _conflict(cur, str(e))
    out = {"record": {k: v for k, v in done.items() if k != "event_error"},
           "summary": p["summary"], "form": form}
    if done.get("event_error"):
        out["event_error"] = done["event_error"]
    return out
