"""Escalation records (SPEC-0232): `ESC-NNNN` question-to-the-human files under
`<agentic_dir>/escalations/`, created by `nw queue escalate`, which also parks the campaign as
`human_decision` (the human-owned reason of SPEC-0222), appends an `escalation` event, mails
`human` and runs a configurable desktop notifier. Answering (SPEC-0235) only records the answer; the conductor applies it.

Lock order is always the campaign's `item_lock`, then the escalations lock."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import shutil
import subprocess

from ..export import _atomic_write
from . import events as E
from . import mailbox as M
from . import store

SCHEMA = "nw.escalation.v1"
STATUSES = ("open", "answered", "withdrawn")
DEFAULTS = {"notify": "auto", "remind_s": 3600}
NOTIFY_TIMEOUT_S = 5
_ID_RE = re.compile(r"^ESC-(\d{4,})$")
_SUBST_RE = re.compile(r"\{(id|campaign|question|recommendation)\}")


def settings(cfg) -> dict:
    ag = cfg.get("agentic")
    esc = ag.get("escalations") if isinstance(ag, dict) else None
    return {**DEFAULTS, **(esc if isinstance(esc, dict) else {})}      # malformed: defaults


# ---- storage ---------------------------------------------------------------------------------

def escalations_dir(cfg):
    return store.agentic_dir(cfg) / "escalations"


def _lock_path(cfg):
    return escalations_dir(cfg) / ".lock"


def _path(cfg, esc_id):
    return escalations_dir(cfg) / f"{esc_id}.json"


def _ids(cfg) -> list[int]:
    d = escalations_dir(cfg)
    if not d.is_dir():
        return []
    return sorted(int(m.group(1)) for p in d.glob("ESC-*.json")
                  if (m := _ID_RE.match(p.stem)))


def _write(cfg, rec: dict) -> None:
    _atomic_write(_path(cfg, rec["id"]), json.dumps(rec, indent=2, ensure_ascii=False) + "\n")


def read(cfg, esc_id) -> dict:
    """The record, or ValueError for an unknown or unreadable id."""
    if not _ID_RE.match(esc_id or ""):
        raise ValueError(f"not an escalation id: {esc_id!r} (ESC-NNNN)")
    p = _path(cfg, esc_id)
    if not p.is_file():
        raise ValueError(f"no escalation {esc_id}")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise ValueError(f"escalation {esc_id} is unreadable: {e}")
    if not isinstance(rec, dict):
        raise ValueError(f"escalation {esc_id} is unreadable: not an object")
    return rec


def list_all(cfg, status=None) -> list[dict]:
    """Records, newest first; only those with `status` when given."""
    out = []
    for n in reversed(_ids(cfg)):
        try:
            rec = read(cfg, f"ESC-{n:04d}")
        except ValueError:
            continue
        if status is None or rec.get("status") == status:
            out.append(rec)
    return out


def _campaign_state(cfg, cid) -> dict | None:
    try:
        return store.read_state(cfg, cid)
    except (ValueError, OSError):
        return None


def _live(cfg, rec: dict) -> bool:
    """An open record is live while its campaign is PARKED `human_decision` or holds a
    `human_decision` park request (a campaign-less record is always live)."""
    cid = rec.get("campaign")
    if not cid:
        return True
    st = _campaign_state(cfg, cid)
    if st is None:
        return False
    if st.get("state") == "PARKED" and (st.get("park") or {}).get("reason") == "human_decision":
        return True
    return (st.get("park_requested") or {}).get("reason") == "human_decision"


def _open_records(cfg, campaign) -> list[dict]:
    return [r for r in list_all(cfg, "open") if r.get("campaign") == campaign]


def open_for(cfg, campaign) -> dict | None:
    """The live open escalation of `campaign`, or None. A record whose campaign left the
    `human_decision` park by another route does not count."""
    for rec in _open_records(cfg, campaign):
        if _live(cfg, rec):
            return rec
    return None


def open_live(cfg) -> list[dict]:
    return [r for r in list_all(cfg, "open") if _live(cfg, r)]


def _mutate(cfg, esc_id, fn) -> dict:
    """Re-read `esc_id` under the escalations lock, apply `fn(rec)` and write it back."""
    with E._locked(_lock_path(cfg)):
        rec = read(cfg, esc_id)
        fn(rec)
        _write(cfg, rec)
    return rec


def _create(cfg, fields: dict) -> dict:
    """Allocate the next id and write the `open` record, both under the escalations lock."""
    with E._locked(_lock_path(cfg)):
        ids = _ids(cfg)
        esc_id = f"ESC-{(ids[-1] if ids else 0) + 1:04d}"
        rec = {"schema": SCHEMA, "id": esc_id, "created": store.now_iso(), **fields,
               "status": "open", "mail_id": None, "notified": None, "reminded_at": None,
               "answer": None, "answered_by": None, "answered_at": None, "withdrawn": None}
        _write(cfg, rec)
    return rec


# ---- escalate --------------------------------------------------------------------------------

def plan_escalate(cfg, campaign, *, question, options=(), recommend=None, facts=(),
                  frm=None, kind=None) -> dict:
    """Validate and describe an escalation (pure: writes nothing). `kind` (SPEC-0342) says
    what a record is about (`worker_lost`); None for an ordinary question."""
    from . import queue as Q
    frm = M.validate_address(cfg, frm or os.environ.get("NW_MAILBOX_ADDR") or "conductor")
    question = (question or "").strip()
    if not question:
        raise ValueError("an escalation needs a --question")
    opts = [{"n": i, "label": str(o).strip()} for i, o in enumerate(options, 1)]
    if any(not o["label"] for o in opts):
        raise ValueError("empty --option")
    if recommend is not None:
        if not opts:
            raise ValueError("--recommend needs at least one --option")
        if not 1 <= recommend <= len(opts):
            raise ValueError(f"--recommend {recommend} is not an option (1-{len(opts)})")
    facts = [str(f) for f in facts]
    plan = {"campaign": campaign, "spec_id": None, "project": None, "state": None,
            "park_reason_now": None, "by": frm, "question": question, "options": opts,
            "recommendation": recommend, "facts": facts, "park": None, "kind": kind}
    if not campaign:
        return plan
    st = store.read_state(cfg, campaign)           # ValueError when unknown
    s = st.get("state")
    if s in Q.TERMINAL_STATES:
        raise ValueError(f"{campaign} is {s}; nothing to escalate")
    other = open_for(cfg, campaign)
    if other:
        raise ValueError(f"{campaign} already has an open escalation, {other['id']}; "
                         f"withdraw it first (nw escalations withdraw {other['id']})")
    plan.update(spec_id=st.get("spec_id"), project=st.get("project"), state=s)
    if s == "PARKED":
        park = st.get("park") or {}
        reason = park.get("reason")
        plan["park_reason_now"] = reason
        # a re-park keeps what an earlier escalation recorded: the situation did not change
        origin = park.get("escalated_from") if reason == "human_decision" else reason
        plan["park"] = {"escalated_from": origin, "resume_to": park.get("resume_to"),
                        "mode": "immediate"}
        return plan
    pp = Q.plan_park(cfg, campaign, question)
    plan["park"] = {"escalated_from": None, "resume_to": pp["resume_to"], "mode": pp["mode"]}
    earlier = st.get("park_requested") or {}
    plan["request_now"] = earlier.get("reason")
    if pp["mode"] == "deferred" and earlier.get("reason") == "human":
        plan["facts"] = facts + [f"park requested earlier: {earlier.get('detail')}"]
    return plan


def _revalidate(cfg, plan: dict, st: dict) -> None:
    from . import queue as Q
    cid = plan["campaign"]
    moved = st.get("state") != plan["state"] or st.get("state") in Q.TERMINAL_STATES
    if not moved and plan["state"] == "PARKED":
        moved = (st.get("park") or {}).get("reason") != plan["park_reason_now"]
    if not moved and plan["state"] != "PARKED":
        moved = (st.get("park_requested") or {}).get("reason") != plan.get("request_now")
    if moved or open_for(cfg, cid):
        raise ValueError(f"{cid} changed since the plan; re-run")


def apply_escalate(cfg, plan: dict) -> dict:
    """Write the record, park the campaign, then announce (event, mail, notification). The
    record is removed again when the park fails, so no orphan `open` record blocks a retry."""
    from . import queue as Q
    cid = plan["campaign"]
    fields = {"by": plan["by"], "campaign": cid, "spec_id": plan["spec_id"],
              "project": plan["project"], "question": plan["question"],
              "options": plan["options"], "recommendation": plan["recommendation"],
              "facts": plan["facts"], "park": plan["park"], "kind": plan.get("kind")}
    if not cid:
        rec = _create(cfg, fields)
    else:
        with store.item_lock(cfg, cid):
            st = store.read_state(cfg, cid)
            _revalidate(cfg, plan, st)
            for stale in _open_records(cfg, cid):      # not live: open_for found none
                _withdraw_stale(cfg, stale["id"])
            rec = _create(cfg, fields)
            detail = f"{rec['id']}: {plan['question']}"
            park = plan["park"]
            try:
                if park["mode"] == "deferred":
                    earlier = st.get("park_requested")
                    st["park_requested"] = {"reason": "human_decision", "detail": detail,
                                            "at": store.now_iso(), "by": plan["by"]}
                    if (earlier or {}).get("reason") == "human":
                        # kept so a withdrawal gives the human's own request back
                        st["park_requested"]["replaces"] = earlier
                    store.append_event(cfg, cid, "note", {
                        "text": f"escalation {rec['id']} requested; parks after the running "
                                f"step", "author": plan["by"]}, by=plan["by"])
                    store.write_state(cfg, cid, st)
                else:
                    Q._park(cfg, st, "human_decision", detail, park["resume_to"],
                            by=plan["by"], extra={"escalated_from": park["escalated_from"]},
                            count=plan["state"] != "PARKED")
            except BaseException:
                with contextlib.suppress(OSError):
                    _path(cfg, rec["id"]).unlink()
                raise
    return _announce(cfg, rec)


def _mail_text(rec: dict) -> str:
    lines = [rec["question"]]
    for o in rec["options"]:
        mark = " (recommended)" if o["n"] == rec.get("recommendation") else ""
        lines.append(f"  {o['n']}. {o['label']}{mark}")
    lines.append(f"answer: nw escalations show {rec['id']}")
    return "\n".join(lines)


def _announce(cfg, rec: dict) -> dict:
    """Event, human mail, notification (after the locks are released)."""
    by = rec["by"]
    errors = []
    try:
        E.append(cfg, "escalation", campaign=rec["campaign"], spec_id=rec["spec_id"],
                 project=rec["project"], by=by,
                 data={"escalation_id": rec["id"], "question": rec["question"],
                       "options": rec["options"], "recommendation": rec["recommendation"]})
    except (ValueError, OSError) as e:
        errors.append(f"event: {e}")
    mail_id = None
    try:
        mail_id = M.send(cfg, to="human", frm=by, kind="question", campaign=rec["campaign"],
                         needs_reply=False, text=_mail_text(rec))["id"]
    except (ValueError, OSError) as e:
        errors.append(f"mail: {e}")
    outcome = notify(cfg, rec)
    if errors:                    # the record is already parked: report, do not fail the apply
        outcome = {**outcome, "error": "; ".join([*errors, outcome.get("error") or ""]).strip("; ")[:300]}
    return _mutate(cfg, rec["id"], lambda r: r.update(mail_id=mail_id, notified=outcome))


# ---- answer (SPEC-0235) ----------------------------------------------------------------------

_NOTE_AC_CHARS, _NOTE_TEXT_CHARS, _NOTE_NOTE_CHARS = 60, 200, 200


class AnswerFormError(ValueError):
    """A wrong combination of answer options (mixed, absent or reason-less forms): the CLI shows
    it as a usage error."""


def _cut(text, n) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n] + "…"


def _check_parked(esc_id, st) -> None:
    """A campaign-bound record is answerable only once its campaign is PARKED `human_decision`; a
    deferred park request (SPEC-0232) has not parked yet and would park later, unanswered."""
    if st is None:
        raise ValueError(f"{esc_id}: its campaign's state is missing or unreadable; "
                         f"nothing to answer")
    if st.get("state") == "PARKED" and (st.get("park") or {}).get("reason") == "human_decision":
        return
    if (st.get("park_requested") or {}).get("reason") == "human_decision":
        raise ValueError(f"{esc_id} is pending: the campaign has not parked yet; answer once "
                         f"it parks")
    raise ValueError(f"{esc_id}: its campaign is not parked human_decision "
                     f"(state {st.get('state')}); nothing to answer")


def _review_findings(cfg, st: dict, overridden: bool = True) -> tuple[list, list, set]:
    """`(findings, unmet, synthetic ids)` of the review that led to the park: every `blocking` / `advisory`
    finding of `triage_reviews` over all stored verdicts (any loop), minus what the campaign's
    `human_overrides` already settled (unless `overridden=False`), and the distinct `ac` texts of
    the `met: false` `ac_assessment` entries."""
    from . import profiles as PR
    from . import queue as Q
    stored = (st.get("last_review") or {}).get("verdicts") or {}
    profile = PR.resolve_profile(cfg, st.get("profile"))
    valid = {p: stored[p] for p in Q.reviewers_of(profile)
             if isinstance(stored.get(p), dict)}
    human_waived, attested = Q.human_overrides_of(st) if overridden else ((), ())
    tri = Q.triage_reviews(valid, profile, human_waived=human_waived, attested=attested)
    unmet = []
    for v in valid.values():
        for e in v.get("ac_assessment") or []:
            if isinstance(e, dict) and e.get("met") is False and e.get("ac") \
                    and e["ac"] not in unmet:
                unmet.append(e["ac"])
    return tri["blocking"] + tri["advisory"], unmet, set(tri["synthetic"])


def answerable_findings(cfg, state: dict, esc_id=None) -> list[dict]:
    """The findings `plan_answer` validates `--waive` ids against (SPEC-0250 lists them too):
    `blocking` + `advisory` of the stored review, synthetic ones included, already-waived ones
    left out. With `esc_id`, that escalation's own (half-written) overrides do not count as
    waived, the same view its retry is planned against (SPEC-0286)."""
    if esc_id is not None:
        state = _without_overrides_of(state, esc_id)
    return _review_findings(cfg, state)[0]


def _ids_hint(ids) -> str:
    ids = list(ids)
    return ", ".join(ids[:10]) + (f", … ({len(ids)} in all)" if len(ids) > 10 else "") \
        if ids else "(none)"


def _unmet_reach(text, unmet, what) -> list:
    """The distinct unmet ACs `text` covers (`queue._covers`): only the exact one when `text` is
    one of them (SPEC-0286); ValueError when it covers more than one and is not exactly one of
    them, because the substring rule would quietly widen it."""
    from . import queue as Q
    hits = [u for u in unmet if Q._covers(text, u)]
    exact = [u for u in hits if Q.norm_finding_title(u) == Q.norm_finding_title(text)]
    if len(hits) > 1 and not exact:
        raise ValueError(f"{what} covers {len(hits)} ACs ({_ids_hint(hits)}); "
                         f"use a longer text or a <persona>#ac<j> id")
    return exact or hits


def _strip_overrides_of(h: dict, esc_id) -> int:
    """Drop the `human_overrides` waive/attest entries tagged `esc_id` from `h` in place; the
    number removed."""
    n = 0
    for k in ("waive", "attest"):
        if isinstance(h.get(k), list):
            kept = [o for o in h[k]
                    if not (isinstance(o, dict) and o.get("escalation") == esc_id)]
            n += len(h[k]) - len(kept)
            h[k] = kept
    return n


def _without_overrides_of(st: dict, esc_id) -> dict:
    """A shallow copy of `st` whose `human_overrides` leave out the entries tagged `esc_id`."""
    h = st.get("human_overrides")
    if not isinstance(h, dict):
        return st
    h = dict(h)
    _strip_overrides_of(h, esc_id)
    return {**st, "human_overrides": h}


def _plan_override(cfg, rec, waive, attest, reason) -> dict:
    from . import queue as Q
    cid = rec.get("campaign")
    if not cid:
        raise ValueError(f"{rec['id']} has no campaign; there is no review to override")
    if not (reason or "").strip():
        raise AnswerFormError("an override needs a non-empty --reason")
    if not waive and not attest:
        raise AnswerFormError("an override needs --waive and/or --attest")
    # validate against the review without this escalation's own (half-written) overrides: a
    # retry of the same answer replaces them in `apply_answer` (SPEC-0286)
    st = _without_overrides_of(store.read_state(cfg, cid) or {}, rec["id"])
    findings, unmet, synthetic = _review_findings(cfg, st)
    by_id = {f["id"]: f for f in findings}
    waived, implicit = [], []
    for fid in waive:
        f = by_id.get(fid)
        if f is None:
            raise ValueError(f"--waive {fid!r} is not a finding of the stored review; "
                             f"valid ids: {_ids_hint(by_id)}")
        if fid in [w["id"] for w in waived]:
            continue
        # `resolved_ac`: the full unmet AC texts this waiver clears (the reviewer's own `ac` is
        # often a partial quote; triage matches these exactly, SPEC-0264)
        resolved = (_unmet_reach(f["ac"], unmet, f"--waive {fid!r} (its ac {f['ac']!r})")
                    if f.get("ac") else [])
        waived.append({"id": fid, "title": f["title"], "files": f["files"],
                       "severity": f["severity"], "ac": f.get("ac"),
                       "resolved_ac": list(resolved), "synthetic": fid in synthetic})
        for u in resolved:
            if u not in implicit:
                implicit.append(u)
    attested, reach, ac_by_id = [], {}, None
    for a in attest:
        a = (a or "").strip()
        if not a:
            raise ValueError("empty --attest")
        m = re.match(r"^(\w+)#ac\d+$", a)
        if m:
            if ac_by_id is None:      # an AC already attested still has its `#ac<j>` id, and
                # one a waived finding covered gets its own once the waiver applies (SPEC-0286)
                ac_by_id = {f["id"]: f for f in _review_findings(cfg, st, overridden=False)[0]
                            + findings}
            f = ac_by_id.get(a)
            if f is None or not f.get("ac"):
                ids = [i for i in ac_by_id if re.search(r"#ac\d+$", i)]
                raise ValueError(f"--attest {a!r} is not an AC finding of the stored review; "
                                 f"valid ids: {_ids_hint(ids)}")
            text, hits = f["ac"], [f["ac"]]
        else:
            hits = _unmet_reach(a, unmet, f"--attest {a!r}")
            if not hits:
                raise ValueError(f"--attest {a!r} matches no unmet AC of the stored review; "
                                 f"unmet ACs: {_ids_hint(unmet)}")
            # store the resolved full AC, not the typed text: triage matches it exactly
            text = hits[0] if len(hits) == 1 else next(
                u for u in hits if Q.norm_finding_title(u) == Q.norm_finding_title(a))
        if text not in attested:
            attested.append(text)
            reach[text] = hits
    return {"waive": waived, "attest": attested, "reason": reason.strip(),
            "implicitly_attested": implicit, "reach": reach}


def plan_answer(cfg, esc_id, *, choose=None, text=None, waive=(), attest=(), reason=None,
                note=None, by=None) -> dict:
    """Validate and describe an answer (pure: writes nothing). Exactly one form: `choose`, `text`
    or an override (`waive` / `attest` with `reason`)."""
    waive, attest = list(waive or ()), list(attest or ())
    forms = [k for k, on in (("choose", choose is not None), ("text", text is not None),
                             ("override", bool(waive or attest))) if on]
    if len(forms) != 1:
        raise AnswerFormError("give exactly one answer form: --choose, --text, or "
                         "--waive/--attest with --reason" +
                         (f" (got {', '.join(forms)})" if forms else ""))
    if reason is not None and forms != ["override"]:
        raise AnswerFormError("--reason belongs to --waive/--attest")
    by = M.validate_address(cfg, by or "human")
    rec = read(cfg, esc_id)
    if rec.get("status") != "open":
        when = (rec.get("answered_at") if rec.get("status") == "answered"
                else (rec.get("withdrawn") or {}).get("at"))
        raise ValueError(f"{esc_id} is {rec.get('status')} since {when}; only an open "
                         f"escalation can be answered")
    if rec.get("campaign"):
        _check_parked(esc_id, _campaign_state(cfg, rec["campaign"]))
    kind = forms[0]
    answer = {"kind": kind, "choice": None, "label": None, "text": None, "waive": [],
              "attest": [], "reason": None, "note": (note or "").strip() or None}
    plan = {"id": esc_id, "campaign": rec.get("campaign"), "by": by,
            "question": rec["question"], "options": rec.get("options") or [],
            "answer": answer, "override": None}
    if kind == "choose":
        opts = rec.get("options") or []
        if not opts:
            raise ValueError(f"{esc_id} has no options to choose from; use --text")
        v = str(choose).strip()
        hit = None
        if v.isdigit():
            hit = next((o for o in opts if o["n"] == int(v)), None)
        if hit is None and not v.isdigit():
            hit = next((o for o in opts if o["label"].strip().casefold() == v.casefold()), None)
        if hit is None:
            raise ValueError(f"--choose {v!r} is not an option of {esc_id} "
                             f"(1-{len(opts)} or a label: {', '.join(o['label'] for o in opts)})")
        answer.update(choice=hit["n"], label=hit["label"])
    elif kind == "text":
        if not str(text).strip():
            raise ValueError("empty --text")
        answer["text"] = str(text).strip()
    else:
        ov = _plan_override(cfg, rec, waive, attest, reason)
        answer.update(waive=[w["id"] for w in ov["waive"]], attest=ov["attest"],
                      reason=ov["reason"])
        plan["override"] = ov
    plan["summary"] = summarize(answer)
    return plan


def summarize(answer: dict) -> str:
    """The pinned one-line summary of an answer (ledger note, `escalation_answered`)."""
    kind = answer["kind"]
    if kind == "choose":
        out = f"chose {answer['choice']} ({answer['label']})"
    elif kind == "text":
        out = f"text: {_cut(answer['text'], _NOTE_TEXT_CHARS)}"
    else:
        sections = []
        if answer["waive"]:
            sections.append("waived " + ", ".join(answer["waive"]))
        if answer["attest"]:
            sections.append("attested " + ", ".join(
                f'"{_cut(a, _NOTE_AC_CHARS)}"' for a in answer["attest"]))
        sections.append(f"reason: {answer['reason']}")
        out = "; ".join(sections)
    if answer.get("note"):
        out += f" — note: {_cut(answer['note'], _NOTE_NOTE_CHARS)}"
    return out


def answer_summary(rec: dict) -> str:
    """`summarize` of a stored record's answer, '' when it has none (SPEC-0250's tab uses it)."""
    return summarize(rec["answer"]) if rec.get("answer") else ""


def apply_answer(cfg, plan: dict) -> dict:
    """Ledger the answer, store the overrides, close the record, then append
    `escalation_answered`. Lock order: the campaign's `item_lock`, then the escalations lock,
    held across the whole read-check-write; `open` is re-checked inside it. A failure before the
    record closes leaves it open to retry (ledger events may repeat). A failed wake-up append is
    returned as `event_error` (not persisted). Never moves the campaign."""
    esc_id, cid, by, answer = plan["id"], plan["campaign"], plan["by"], plan["answer"]
    known = bool(cid)
    if known:
        _check_parked(esc_id, _campaign_state(cfg, cid))   # unreadable ⇒ refused before any lock
    now = store.now_iso()
    with store.item_lock(cfg, cid) if known else contextlib.nullcontext():
        if known:
            _check_parked(esc_id, _campaign_state(cfg, cid))
        with E._locked(_lock_path(cfg)):
            rec = read(cfg, esc_id)
            if rec.get("status") != "open":
                when = rec.get("answered_at") or (rec.get("withdrawn") or {}).get("at")
                raise ValueError(f"{esc_id} is {rec.get('status')} since {when}")
            # the record closes last: a failure before it leaves the escalation open to retry
            if known:
                summary = plan["summary"]
                store.append_event(cfg, cid, "human_decision",
                                   {"escalation": esc_id, **answer}, by=by)
                store.append_event(cfg, cid, "note", {"text": f"{by}: {esc_id} {summary}",
                                                      "author": by}, by=by)
                ov = plan.get("override")
                st = store.read_state(cfg, cid)
                h = st.get("human_overrides") if isinstance(st.get("human_overrides"),
                                                            dict) else None
                # any answer replaces this escalation's earlier (half-written) overrides: a
                # retry stores one copy, a --choose/--text answer leaves none (SPEC-0286)
                removed = _strip_overrides_of(h, esc_id) if h is not None else 0
                if ov:
                    h = st.setdefault("human_overrides", {"waive": [], "attest": []})
                    for k in ("waive", "attest"):
                        if not isinstance(h.get(k), list):
                            h[k] = []
                    for w in ov["waive"]:
                        h["waive"].append({**w, "reason": ov["reason"], "escalation": esc_id,
                                           "at": now})
                    for a in ov["attest"]:
                        h["attest"].append({"ac": a, "reason": ov["reason"],
                                            "escalation": esc_id, "at": now})
                if ov or removed:
                    # a failed withdraw's count goes from disk before this answer changes the
                    # overrides, so a later withdraw never reports removals it did not do
                    if rec.pop("overrides_removed", None) is not None:
                        _write(cfg, rec)
                    store.write_state(cfg, cid, st)
            rec.pop("overrides_removed", None)   # a failed withdraw's count no longer holds
            rec.update(status="answered", answer=answer, answered_by=by, answered_at=now)
            _write(cfg, rec)
    try:
        E.append(cfg, "escalation_answered", campaign=cid, spec_id=rec.get("spec_id"),
                 project=rec.get("project"), by=by,
                 data={"escalation_id": esc_id, "kind": answer["kind"],
                       "summary": plan["summary"], "answer": answer})
    except (ValueError, OSError) as e:
        return {**rec, "event_error": str(e)}     # returned only, never persisted
    return rec


# ---- notification ----------------------------------------------------------------------------

def _run_notify(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=NOTIFY_TIMEOUT_S)


def _notify_argv(cfg, rec: dict, title: str) -> tuple[str, list | None, str | None]:
    """`(via, argv, error)`; `argv` None when nothing is to run."""
    mode = settings(cfg)["notify"]
    if mode == "none":
        return "none", None, None
    if mode == "auto":
        exe = shutil.which("notify-send")
        if not exe:
            return "none", None, "notify-send not found"
        return "notify-send", [exe, "--app-name=nightwright", "--urgency=critical", "--",
                               title, rec["question"]], None
    if isinstance(mode, list) and mode and all(isinstance(a, str) for a in mode):
        vals = {"id": rec["id"], "campaign": rec.get("campaign") or "",
                "question": rec["question"],
                "recommendation": "" if rec.get("recommendation") is None
                else str(rec["recommendation"])}
        return "command", [_SUBST_RE.sub(lambda m: vals[m.group(1)], a) for a in mode], None
    return "none", None, f"bad agentic.escalations.notify: {mode!r}"


def notify(cfg, rec: dict, *, title: str | None = None) -> dict:
    """Run the configured notifier; never raises. The outcome is `notified`."""
    title = title or f"nw: {rec['id']} needs you"
    try:
        via, argv, error = _notify_argv(cfg, rec, title)
    except Exception as e:                             # a malformed setting
        via, argv, error = "none", None, f"{type(e).__name__}: {e}"[:300]
    ok = argv is not None
    if argv is not None:
        try:
            r = _run_notify(argv)
            if r.returncode != 0:
                ok = False
                error = (f"exit {r.returncode}: " + (r.stderr or r.stdout or "").strip())[:300]
        except Exception as e:                         # OSError, timeout, a broken notifier
            ok, error = False, f"{type(e).__name__}: {e}"[:300]
    elif via == "none" and error is None:
        ok = True
    return {"via": via, "ok": ok, "error": error, "at": store.now_iso()}


def remind_due(cfg, now=None) -> list[dict]:
    """Re-notify each open record last notified (`reminded_at` or `created`) over `remind_s`
    ago; 0 disables. A due record is claimed under the escalations lock (`reminded_at` is
    written there) before the notifier runs outside it, so concurrent followers remind once.
    Open records whose campaign left the park by another route are withdrawn instead."""
    try:
        remind_s = float(settings(cfg)["remind_s"] or 0)
    except (TypeError, ValueError):
        remind_s = float(DEFAULTS["remind_s"])
    if remind_s <= 0:
        return []
    now = now or dt.datetime.now().astimezone()
    out = []
    for rec in list_all(cfg, "open"):
        try:
            if not _live(cfg, rec):
                _withdraw_stale(cfg, rec["id"])
                continue
            if not _claim(cfg, rec["id"], now, remind_s):
                continue
            claimed = read(cfg, rec["id"])
        except (ValueError, OSError):                  # one bad record must not stop the pass
            continue
        outcome = notify(cfg, claimed, title=f"nw: {claimed['id']} still needs you")
        with contextlib.suppress(ValueError, OSError):
            _mutate(cfg, claimed["id"], lambda r: r.update(notified=outcome))
        out.append({"id": claimed["id"], "notified": outcome})
    return out


def _claim(cfg, esc_id, now, remind_s) -> bool:
    with E._locked(_lock_path(cfg)):
        try:
            rec = read(cfg, esc_id)
        except ValueError:
            return False
        if rec.get("status") != "open":
            return False
        age = E._age_s(rec.get("reminded_at") or rec.get("created"), now)
        if age is None or age < remind_s:
            return False
        rec["reminded_at"] = store.now_iso(now)
        _write(cfg, rec)
    return True


# ---- withdrawal ------------------------------------------------------------------------------

def withdraw(cfg, esc_id, reason, by, *, clear_request=True) -> dict:
    """Close an `open` record as `withdrawn`: ledger note, `escalation_withdrawn` event and a
    `human` info mail. A deferred `human_decision` park request that has not parked yet is
    dropped (`clear_request`; callers that own the campaign state pass False). Never resumes
    the campaign. ValueError when the record is not open."""
    rec = read(cfg, esc_id)
    cid = rec.get("campaign")
    known = bool(cid) and _campaign_state(cfg, cid) is not None   # item_lock would mkdir
    with store.item_lock(cfg, cid) if known else contextlib.nullcontext():
        def close(r):
            if r.get("status") != "open":
                raise ValueError(f"{esc_id} is {r.get('status')}, not open")
            r["status"] = "withdrawn"
            r["withdrawn"] = {"reason": reason, "by": by, "at": store.now_iso()}
            if not clear_request:          # this withdraw keeps the overrides
                r.pop("overrides_removed", None)
        removed = 0
        if known:
            close(dict(read(cfg, esc_id)))     # refuse before touching the state
            # the state is written before the record closes: a failed write leaves it open,
            # so the withdraw can be retried (answers hold this lock too)
            st = _campaign_state(cfg, cid)
            dirty = False
            if clear_request and st and st.get("state") != "PARKED" \
                    and (st.get("park_requested") or {}).get("reason") == "human_decision":
                back = st["park_requested"].get("replaces")
                if back:
                    st["park_requested"] = back
                else:
                    st.pop("park_requested")
                dirty = True
            # overrides a half-written answer stored no longer stand (SPEC-0286)
            h = (st or {}).get("human_overrides")
            if clear_request and isinstance(h, dict):
                removed = _strip_overrides_of(h, esc_id)
                dirty = dirty or bool(removed)
            if removed:
                # the count goes on the open record first: a retry after the state write finds
                # nothing left to strip, but its note still says how many went (SPEC-0286)
                def count(r):
                    close(dict(r))
                    r["overrides_removed"] = removed
                _mutate(cfg, esc_id, count)
            if dirty:
                store.write_state(cfg, cid, st)
        rec = _mutate(cfg, esc_id, close)
        if known:
            removed = (rec.get("overrides_removed") or 0) if clear_request else 0
            more = f"; {removed} override(s) removed" if removed else ""
            with contextlib.suppress(ValueError, OSError):
                store.append_event(cfg, cid, "note", {
                    "text": f"escalation {esc_id} withdrawn: {reason}{more}", "author": by},
                    by=by)
    try:
        frm = M.validate_address(cfg, by)
    except ValueError:                    # `tick`, `nw`: not a mailbox address
        frm = "conductor"
    with contextlib.suppress(ValueError, OSError):
        E.append(cfg, "escalation_withdrawn", campaign=cid, spec_id=rec.get("spec_id"),
                 project=rec.get("project"), by=by,
                 data={"escalation_id": esc_id, "reason": reason})
    with contextlib.suppress(ValueError, OSError):
        M.send(cfg, to="human", frm=frm, kind="info", campaign=cid,
               text=f"{esc_id} withdrawn by {by}: {reason}")
    return rec


def plan_withdraw(cfg, esc_id, *, reason, by) -> dict:
    """Validate a withdrawal (pure: writes nothing): the record must be `open` and `by` a
    mailbox address. The logic behind `nw escalations withdraw ID --reason … --from ADDR`."""
    by = M.validate_address(cfg, by)
    rec = read(cfg, esc_id)
    if rec.get("status") != "open":
        raise ValueError(f"{esc_id} is {rec.get('status')}, not open")
    return {"id": esc_id, "campaign": rec.get("campaign"), "reason": reason, "by": by}


def apply_withdraw(cfg, plan: dict) -> dict:
    """Withdraw per `plan_withdraw`; `open` is re-checked under the escalations lock (ValueError
    otherwise). Returns the record plus `campaign_state` (the campaign's state afterwards, or
    None)."""
    rec = withdraw(cfg, plan["id"], plan["reason"], plan["by"])
    cid = rec.get("campaign")
    st = _campaign_state(cfg, cid) if cid else None
    return {**rec, "campaign_state": (st or {}).get("state") if st else None}


def withdraw_for_campaign(cfg, cid, reason, by) -> dict | None:
    """Withdraw the campaign's `open` record, if any (the campaign left the park by a resume,
    an abandon or a terminal transition). The caller owns the campaign state; `answered`
    records are never touched. Never raises."""
    out = None
    try:
        for rec in _open_records(cfg, cid):
            out = withdraw(cfg, rec["id"], reason, by, clear_request=False)
    except (ValueError, OSError):
        pass
    return out


def _withdraw_stale(cfg, esc_id) -> None:
    """Backstop: close a record whose campaign is no longer in the `human_decision` park."""
    cid = read(cfg, esc_id).get("campaign")
    known = _campaign_state(cfg, cid) is not None
    with store.item_lock(cfg, cid) if known else contextlib.nullcontext():
        rec = read(cfg, esc_id)
        if rec.get("status") == "open" and not _live(cfg, rec):
            with contextlib.suppress(ValueError, OSError):
                withdraw(cfg, esc_id, "campaign left the park", "nw", clear_request=False)


def note_park(cfg, cid, *, escalated_from, resume_to) -> None:
    """Keep the open record's `park` in step with a park the tick resolved differently from
    the plan (the running step parked by itself)."""
    for rec in _open_records(cfg, cid):
        def upd(r):
            r["park"] = {**(r.get("park") or {}), "escalated_from": escalated_from,
                         "resume_to": resume_to}
        with contextlib.suppress(ValueError, OSError):
            _mutate(cfg, rec["id"], upd)


def digest_rows(cfg, now) -> list[dict]:
    """`ping_digest.escalations_open`: live open records."""
    return [{"id": r["id"], "campaign": r.get("campaign"),
             "age_s": round(E._age_s(r.get("created"), now) or 0)} for r in open_live(cfg)]
