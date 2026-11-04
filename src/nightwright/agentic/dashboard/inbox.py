"""The decisions inbox of `nw.dashboard.v1` (SPEC-0403): every open escalation with everything
the human needs to decide, and every human-owned park with no open escalation, with the fix to
run on the host. Read-only: no `events.sync`, no lock, no write (SPEC-0400 invariant 1).

`forms` / `answerable` mirror `escalations.plan_answer`'s structural rules, and `findings` /
`unmet_acs` are the very lists it validates `--waive` / `--attest` against, so the inbox never
offers an answer `plan_answer` refuses for a structural reason."""
from __future__ import annotations

import re

from .. import escalations as ESC
from .. import store

FINDING_TEXT_MAX = 300
FINDING_KEYS = ("id", "personas", "severity", "title", "detail", "files", "synthetic")
SUMMARY_KEYS = ("id", "created", "by", "kind", "campaign", "spec_id", "project")
EFFECTS = {
    "choose": "The conductor does what the chosen option says (resume, abandon, prioritize or "
              "raise the cap) and tells you in one line.",
    "text": "The conductor acts on your text by its decision table; if the text is not "
            "actionable, it asks again with a new question.",
    "override": "The conductor resumes the campaign to REVIEWING; reviewers are told not to "
                "re-raise the waived findings or the attested ACs.",
}
EFFECTS_WORKER_LOST = ("The queue applies this answer itself (SPEC-0342); the conductor is not "
                       "involved.")
HOST_FIX = {
    "main_dirty": "On the host: move the uncommitted changes in `<root>` off main (commit them "
                  "on a branch), then `nw queue resume <cid>`.",
    "main_not_on_base": "On the host: move any work off the current branch of `<root>`, switch "
                        "it back to its base branch, then `nw queue resume <cid>`.",
    "awaiting_conductor": "Answered; the conductor applies it on its next wake "
                          "(`nw conduct-status` on the host if it does not).",
    "needs_decision": "No open question; the conductor re-escalates or resumes it "
                      "(`nw campaign log <cid> --tail 20` on the host).",
    None: "On the host: `nw campaign log <cid> --tail 20`.",
}
NO_ROOT = "the project repo"
# `triage_reviews`' finding records carry no `synthetic` key (it returns the ids apart); a
# synthetic finding's id is `<persona>#fix` or `<persona>#ac<j>`, the rule triage itself uses
_SYNTHETIC_ID = re.compile(r"#(fix|ac\d+)$")


def _error(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def _first_line(text) -> str:
    s = str(text or "").strip()
    return s.splitlines()[0] if s else ""


def _cut(text):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= FINDING_TEXT_MAX else text[:FINDING_TEXT_MAX] + "…"


def _summary(rec: dict) -> dict:
    return {**{k: rec.get(k) for k in SUMMARY_KEYS}, "question": _first_line(rec.get("question"))}


def _facts(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [str(raw)]


def _status(st: dict | None) -> tuple[bool, bool, str | None]:
    """`(answerable, pending, why_not)` of a record with campaign state `st` (`_check_parked`'s
    rules). A campaign-less record passes None and is always answerable."""
    if st is None:
        return True, False, None
    if st.get("state") == "PARKED" and (st.get("park") or {}).get("reason") == "human_decision":
        return True, False, None
    if (st.get("park_requested") or {}).get("reason") == "human_decision":
        return False, True, "not parked yet"
    return False, False, f"campaign is {st.get('state')}"


def _park_of(st: dict | None):
    if not st or st.get("state") != "PARKED":
        return None
    park = st.get("park") or {}
    return {"reason": park.get("reason"), "escalated_from": park.get("escalated_from"),
            "resume_to": park.get("resume_to"),
            "parked_at": park.get("parked_at") or park.get("since"), "detail": park.get("detail")}


def _spec_title(st, row):
    if row and row.get("spec_title"):
        return row["spec_title"]
    if not st:
        return None
    from .. import queue as Q
    try:
        return Q._spec_title(st)
    except Exception:                               # noqa: BLE001 - a title is cosmetic
        return None


def _finding(f: dict) -> dict:
    """A finding projected onto `FINDING_KEYS`, `title` / `detail` cut; the id stays exact
    (`--waive` matches on it)."""
    return {**{k: f.get(k) for k in FINDING_KEYS}, "title": _cut(f.get("title")),
            "detail": _cut(f.get("detail")),
            "synthetic": bool(_SYNTHETIC_ID.search(str(f.get("id") or "")))}


def forms_of(options, has_campaign: bool, findings, unmet) -> list[str]:
    """`plan_answer`'s forms for a record: `choose` needs options, `override` a campaign whose
    stored review has something to waive or attest; `text` always applies."""
    out = ["choose"] if options else []
    out.append("text")
    if has_campaign and (findings or unmet):
        out.append("override")
    return out


def effects_of(kind, forms) -> dict:
    if kind == "worker_lost":
        return {f: EFFECTS_WORKER_LOST for f in forms}
    return {f: EFFECTS[f] for f in forms}


def _entry(cfg, rec: dict, rows: dict) -> dict:
    cid = rec.get("campaign")
    base = _summary(rec)
    st = None
    if cid:
        try:
            st = store.read_state(cfg, cid)
            if not isinstance(st, dict):
                raise ValueError(f"state of {cid} is not an object")
        except (OSError, ValueError) as e:
            return {**base, "error": _error(e), "answerable": False, "pending": False,
                    "why_not": "campaign state unreadable"}
    try:
        options = [{"n": o.get("n"), "label": o.get("label")}
                   for o in rec.get("options") or [] if isinstance(o, dict)]
        answerable, pending, why_not = _status(st)
        if st is not None:
            findings = [_finding(f) for f in ESC.answerable_findings(cfg, st, rec.get("id"))]
            unmet = ESC.unmet_acs(cfg, st, rec.get("id"))
        else:
            findings, unmet = [], []
        forms = forms_of(options, bool(cid), findings, unmet)
        return {**base, "question_text": str(rec.get("question") or ""), "options": options,
                "recommendation": rec.get("recommendation"), "facts": _facts(rec.get("facts")),
                "spec_title": _spec_title(st, rows.get(cid)), "park": _park_of(st),
                "answerable": answerable, "pending": pending, "why_not": why_not,
                "forms": forms, "findings": findings, "unmet_acs": unmet,
                "effects": effects_of(rec.get("kind"), forms)}
    except Exception as e:                          # noqa: BLE001 - one entry, not the inbox
        return {**base, "error": _error(e), "answerable": False}


def escalation_entries(cfg, rows: dict) -> list[dict]:
    recs = sorted(ESC.list_all(cfg, "open"),
                  key=lambda r: (str(r.get("created") or ""), str(r.get("id") or "")))
    return [_entry(cfg, r, rows) for r in recs]


def _root(cfg, cid) -> str:
    try:
        st = store.read_state(cfg, cid)
    except (OSError, ValueError):
        return NO_ROOT
    repo = st.get("repo") if isinstance(st, dict) else None
    return (repo.get("root") if isinstance(repo, dict) else None) or NO_ROOT


def park_entries(cfg, needs_you: list[dict]) -> list[dict]:
    """The `needs_you` rows whose campaign has no open escalation, each plus `inbox_state` and
    `fix`. One `list_all(cfg)` per call gives both the open set and the latest record per
    campaign."""
    open_cids, latest = set(), {}
    for r in ESC.list_all(cfg):
        cid = r.get("campaign")
        if not cid:
            continue
        if r.get("status") == "open":
            open_cids.add(cid)
        key = (str(r.get("created") or ""), str(r.get("id") or ""))
        if cid not in latest or key > latest[cid][0]:
            latest[cid] = (key, r)
    out = []
    for row in needs_you:
        cid = row.get("campaign")
        if cid in open_cids:
            continue
        reason = (row.get("park") or {}).get("reason")
        if reason == "human_decision":
            last = latest.get(cid, (None, {}))[1]
            state = "awaiting_conductor" if last.get("status") == "answered" else "needs_decision"
            fix = HOST_FIX[state]
        else:
            state = "needs_host"
            fix = HOST_FIX.get(reason) or HOST_FIX[None]
        if "<root>" in fix:
            fix = fix.replace("<root>", _root(cfg, cid))
        out.append({**row, "inbox_state": state, "fix": fix.replace("<cid>", str(cid))})
    return out


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:                          # noqa: BLE001 - one section, not the doc
        return {"error": _error(e)}


def build_inbox(cfg, needs_you_rows) -> dict:
    """`{"escalations": [...], "parks": [...]}`. `needs_you_rows` is `dashboard_payload`'s
    `needs_you` list, or its `{"error": …}` when the payload failed: then `parks` is that error
    and `escalations` still builds. Each section fails alone, and each escalation entry too."""
    failed = isinstance(needs_you_rows, dict)
    rows = {} if failed else {r.get("campaign"): r for r in needs_you_rows or []}
    return {"escalations": _safe(escalation_entries, cfg, rows),
            "parks": needs_you_rows if failed else _safe(park_entries, cfg, needs_you_rows)}
