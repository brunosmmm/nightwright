"""The findings collator (SPEC-0555, phase 3 of SPEC-0552).

With `state.lean.collator` pinned on (it requires `packet`; SPEC-0553 pins the flags at open),
every review loop whose reviewers all left a valid verdict gets one more REVIEWING sub-step
before the verdicts are routed: the marshal-tier `findings_collator` persona reads the loop's
findings (and the packet, SPEC-0554) and returns, per canonical finding, the ids it merged, its
tie (`AC<n>`, `decision`, `design`, `diff:<file>` or `none`), `scope: in|out` with a one-line
reason, and the reviewer's severity.

Code owns everything else. The collator never writes a finding's text: `collate` joins the
title, detail, files and severity of each record from the reviewers' verdicts **verbatim**, then
applies the hard guards (a finding tied to an AC / the Decision / the Design, citing a file the
campaign changed, a `must_fix` or a finding on an unmet AC is never `out`; a dropped finding is
restored `in` with reason `collator_dropped`; a changed severity is reverted). The result goes to
`<campaign_dir>/findings-loop-N.json` (`nw.collated_findings.v1`) and `last_review.collation`;
`scoped` marks each stored finding `scope` / `tie` for `queue.triage_reviews`, which scores the
in-scope ones only. Out-of-scope findings are filed into the campaign's grouped follow-up idea
by the queue (`followups.file_out_of_scope`).

A failed or invalid collator run, a cost-capped one or any error here falls back to today's
routing (every finding in scope) for that loop, with a `collator_failed` event. Never a park."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import packet as PK
from . import store
from .profiles import SEVERITIES

NAME = "findings_collator"
SCHEMA_ID = "nw.collated_findings.v1"
# The collator's run settings when a profile names none (as `packet.MARSHAL_SETTINGS`: adding
# the persona to the builtin profiles would change every profile's sha for flag-off
# campaigns). With `lean.tiers` the model comes from the marshal tier instead.
SETTINGS = {"model": "haiku", "effort": "medium", "max_budget_usd": 0.40,
            "permission_mode": "dontAsk", "read_only": True}
SCOPES = ("in", "out")
_TIE_RE = re.compile(r"^(AC\d+|decision|design|diff:.+|none)$")
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")
_REASON_MAX = 300
_DETAIL_MAX = 2000                       # prompt input only; the output file keeps it verbatim


# ---- state -----------------------------------------------------------------------------------

def on(state) -> bool:
    """`collator` pinned on (with its `packet` dependency, which `effective_lean` enforces at
    open; checked again so a hand-edited state cannot run the collator alone)."""
    from . import tiers as T
    lean = T.pinned(state)
    return lean.get("collator") is True and lean.get("packet") is True


def name(loop: int) -> str:
    return f"findings-loop-{int(loop)}.json"


def signature(verdicts: dict) -> dict:
    """`{persona: run id}` of a verdict set: a collation belongs to exactly these verdicts (a
    retried reviewer, the deep close pass or a re-entered review is a new set)."""
    return {p: (v or {}).get("run") for p, v in (verdicts or {}).items()}


def _lr(state) -> dict:
    lr = (state or {}).get("last_review")
    return lr if isinstance(lr, dict) else {}


def current(state, verdicts) -> dict | None:
    """`last_review.collation` when it belongs to `verdicts`, else None."""
    c = _lr(state).get("collation")
    if isinstance(c, dict) and c.get("runs") == signature(verdicts):
        return c
    return None


def pending(state, verdicts) -> bool:
    c = current(state, verdicts) if on(state) else None
    return bool(c and c.get("status") == "pending")


def needs_start(state, verdicts) -> bool:
    """The flag is pinned and this verdict set has no collation yet."""
    return on(state) and current(state, verdicts) is None


def scoped_out(state) -> bool:
    """The last applied collation scoped at least one finding out (read while IMPLEMENTING:
    `last_review.collation` stays until the next review starts)."""
    if not on(state):
        return False
    c = _lr(state).get("collation")
    return isinstance(c, dict) and c.get("status") == "done" and int(c.get("out") or 0) > 0


def out_findings(state) -> list:
    """`[{persona, title, files}]` of the stored findings the applied collation of
    `last_review.verdicts` scoped out (for the next loop's packet, SPEC-0554); [] otherwise."""
    if not on(state):
        return []
    verdicts = _lr(state).get("verdicts") or {}
    c = _lr(state).get("collation")
    if not (isinstance(c, dict) and c.get("status") == "done" and isinstance(c.get("runs"), dict)
            and all((verdicts.get(p) or {}).get("run") == r for p, r in c["runs"].items())):
        return []
    out = []
    for i, (scope, _tie) in (c.get("scopes") or {}).items():
        p, _, n = str(i).rpartition("#")
        raw = [f for f in (verdicts.get(p) or {}).get("findings") or [] if isinstance(f, dict)]
        if scope == "out" and n.isdigit() and int(n) < len(raw):
            f = raw[int(n)]
            out.append({"persona": p, "title": f.get("title") or "",
                        "files": list(f.get("files") or [])})
    return out


def review_group(state) -> set:
    """The personas of the last review (its reviewers, the marshal, the triager, the
    collator)."""
    c = _lr(state).get("collation")
    runs = c.get("runs") if isinstance(c, dict) and isinstance(c.get("runs"), dict) else {}
    from .personas import REVIEWER_PERSONAS
    return set(runs) | set(REVIEWER_PERSONAS) | {PK.MARSHAL, "review_triager", NAME}


# ---- input -----------------------------------------------------------------------------------

def _sev(f) -> str:
    return f.get("severity") if f.get("severity") in SEVERITIES else "minor"


def inputs(verdicts: dict) -> list:
    """The findings to collate, `{id, persona, severity, title, detail, files, ac, category}`;
    ids are `queue.triage_reviews`' (`<persona>#<index>` over the stored findings). A finding
    the SPEC-0554 delta rule demoted is advisory already and is left out; a `major` /
    `must_fix` it kept (SPEC-0596, `basis_outside_delta`) is an input like any other."""
    out = []
    for p, v in (verdicts or {}).items():
        raw = [f for f in (v or {}).get("findings") or [] if isinstance(f, dict)]
        for i, f in enumerate(raw):
            if f.get("demoted"):
                continue
            item = {"id": f"{p}#{i}", "persona": p, "severity": _sev(f),
                    "title": f.get("title") or "", "detail": f.get("detail"),
                    "files": [str(x) for x in f.get("files") or [] if x is not None],
                    "ac": f.get("ac"), "category": f.get("category")}
            for k in ("suggested_fix", "acceptance", "trigger", "prior",
                      "basis_outside_delta"):                           # SPEC-0596
                if f.get(k):
                    item[k] = f[k]
            out.append(item)
    return out


def unmet_acs(verdicts: dict) -> list:
    """The `ac` texts the reviewers assessed unmet (`ac_assessment[].met is false`)."""
    out = []
    for v in (verdicts or {}).values():
        for e in (v or {}).get("ac_assessment") or []:
            if isinstance(e, dict) and e.get("met") is False and e.get("ac") \
                    and e["ac"] not in out:
                out.append(e["ac"])
    return out


def norm_path(path, worktree=None) -> str:
    """A cited file as a repo-relative path: a worktree prefix, `./` and a `:line` /
    `:start-end` suffix are dropped."""
    s = str(path or "").strip()
    if worktree:
        wt = str(worktree).rstrip("/") + "/"
        if s.startswith(wt):
            s = s[len(wt):]
    while s.startswith("./"):
        s = s[2:]
    return _LINE_SUFFIX_RE.sub("", s)


def diff_files(cfg, state) -> set:
    """Every path the campaign changed (the packet's diff stat when this loop's packet was
    built, plus `git diff --name-only --no-renames merge-base...HEAD`). Empty when neither can
    be read: the guard then only lacks this one signal."""
    from . import gitops as git
    out = set()
    doc = PK.load(cfg, state) if PK.current_ok(state) else None
    for s in ((doc or {}).get("diff") or {}).get("stat") or []:
        if isinstance(s, dict) and s.get("path"):
            out.add(s["path"])
    repo = state.get("repo") or {}
    if repo.get("worktree") and repo.get("base") and Path(repo["worktree"]).is_dir():
        try:
            out |= set(git.changed_files(repo["worktree"], repo["base"], renames=False))
        except Exception:                   # noqa: BLE001 — the packet stat (if any) stands
            pass
    return out


def acs_of(cfg, state) -> list:
    """The spec's ACs (`[{id, text}]`): the packet's, else parsed from the worktree's spec."""
    doc = PK.load(cfg, state) if PK.current_ok(state) else None
    if doc and doc.get("acs"):
        return doc["acs"]
    repo, spec = state.get("repo") or {}, state.get("spec") or {}
    p = Path(repo.get("worktree") or ".") / (spec.get("rel_path") or "")
    try:
        return PK.parse_acs(p.read_text(encoding="utf-8")) if spec.get("rel_path") else []
    except OSError:
        return []


def input_doc(cfg, state, verdicts: dict, changed=None) -> dict:
    """The collator's input (the `collator_input` prompt value, as JSON)."""
    items = []
    for f in inputs(verdicts):
        g = {k: f.get(k) for k in ("id", "persona", "severity", "title", "files", "ac",
                                   "category") if f.get(k) not in (None, "", [])}
        if f.get("detail"):
            g["detail"] = str(f["detail"])[:_DETAIL_MAX]
        items.append(g)
    changed = sorted(diff_files(cfg, state) if changed is None else changed)
    return {"loop": PK.loop_of(state), "acs": acs_of(cfg, state), "changed_files": changed,
            "unmet_acs": unmet_acs(verdicts), "findings": items}


def collator_values(cfg, state, verdicts: dict) -> dict:
    """The collator's prompt values: `collator_input` (JSON) and `packet_path`."""
    doc = input_doc(cfg, state, verdicts)
    packet = str(PK.path_of(cfg, state)) if PK.current_ok(state) else "(no packet this loop)"
    return {"collator_input": json.dumps(doc, indent=2, ensure_ascii=False),
            "packet_path": packet}


# ---- output schema ---------------------------------------------------------------------------

SCHEMA = {
    "type": "object",
    "required": ["collated"],
    "properties": {
        "collated": {"type": "array", "items": {
            "type": "object", "required": ["id", "merged", "tie", "scope", "reason", "severity"],
            "properties": {"id": {"type": "string"},
                           "merged": {"type": "array", "items": {"type": "string"}},
                           "tie": {"type": "string"},
                           "scope": {"type": "string", "enum": list(SCOPES)},
                           "reason": {"type": "string"},
                           "severity": {"type": "string", "enum": list(SEVERITIES)}}}},
    },
}


# ---- the guards ------------------------------------------------------------------------------

def _covers(finding_ac, unmet) -> bool:
    from .queue import _covers as cov
    return any(cov(finding_ac, u) for u in unmet)


def _tie(raw, ac_ids: set, changed: set) -> str:
    """The collator's tie, kept only when well-formed: an AC id of the spec, `decision`,
    `design`, `diff:<a changed file>` or `none`; anything else is `none`."""
    t = str(raw or "").strip()
    if not _TIE_RE.match(t):
        return "none"
    if t.startswith("AC") and t not in ac_ids:
        return "none"
    if t.startswith("diff:") and t[5:] not in changed:
        return "none"
    return t


def collate(verdict, items: list, *, changed=(), unmet=(), ac_ids=(), worktree=None) -> tuple:
    """Pure: `(records, stats)` from the collator's verdict and the input findings (`inputs`).

    Each record is `{id, merged, persona, severity, tie, scope, reason, title, detail, files}`
    with the text fields copied verbatim from the input finding `id` (the canonical one) and
    `severity` the highest of its members' input severities. Guards: the collator's severity is
    reverted to that (`severity_reverted`); `out` becomes `in` (`guard`) for a tie to an AC /
    `decision` / `design` / a changed hunk, a member citing a changed file, a `must_fix` member,
    a member the delta rule kept (`basis_outside_delta`, SPEC-0596) or a member whose `ac`
    names an unmet AC; an input finding in no record is restored as its
    own `in` record with reason `collator_dropped`. A record naming no known, unused id is
    ignored (`unknown`). Raises ValueError when the verdict has no `collated` list, or names
    none of the input findings (an empty list for a non-empty input is a failed run)."""
    if not isinstance(verdict, dict) or not isinstance(verdict.get("collated"), list):
        raise ValueError("collator returned no collated list")
    changed = {norm_path(c) for c in changed or ()}
    ac_ids = set(ac_ids or ())
    unmet = list(unmet or ())
    by_id = {f["id"]: f for f in items}
    rank = {s: i for i, s in enumerate(SEVERITIES)}
    seen: set = set()
    records = []
    stats = {"dropped": 0, "severity_reverted": 0, "guarded": 0, "unknown": 0}
    for e in verdict["collated"]:
        if not isinstance(e, dict):
            stats["unknown"] += 1
            continue
        ids = [e.get("id")] + [m for m in e.get("merged") or [] if isinstance(m, str)]
        members = []
        for i in ids:
            if i in by_id and i not in seen and i not in members:
                members.append(i)
        if not members:
            stats["unknown"] += 1
            continue
        seen.update(members)
        canon = by_id[members[0]]
        mem = [by_id[m] for m in members]
        severity = min((f["severity"] for f in mem), key=lambda s: rank[s])
        rec = {"id": canon["id"], "merged": members[1:],
               "persona": list(dict.fromkeys(f["persona"] for f in mem)),
               "severity": severity,
               "tie": _tie(e.get("tie"), ac_ids, changed),
               "scope": e.get("scope") if e.get("scope") in SCOPES else "in",
               "reason": str(e.get("reason") or "").strip()[:_REASON_MAX],
               "title": canon["title"], "detail": canon.get("detail"),
               "files": list(canon.get("files") or [])}
        if e.get("severity") != severity:
            rec["severity_reverted"] = e.get("severity")
            stats["severity_reverted"] += 1
        if rec["scope"] == "out":
            guard = None
            if rec["tie"] != "none":
                guard = f"tied to {rec['tie']}"
            elif any(norm_path(x, worktree) in changed for f in mem for x in f.get("files") or []):
                guard = "cites a file this campaign changed"
            elif any(f["severity"] == "must_fix" for f in mem):
                guard = "must_fix"
            elif any(f.get("basis_outside_delta") for f in mem):      # SPEC-0596
                guard = "basis_outside_delta"
            elif any(f.get("ac") and _covers(f["ac"], unmet) for f in mem):
                guard = "on an unmet AC"
            if guard is not None:
                rec["scope"], rec["guard"] = "in", guard
                stats["guarded"] += 1
        records.append(rec)
    if items and not seen:
        raise ValueError("collator returned no input finding")
    for f in items:
        if f["id"] in seen:
            continue
        stats["dropped"] += 1
        ac = str(f.get("ac") or "").strip()
        records.append({"id": f["id"], "merged": [], "persona": [f["persona"]],
                        "severity": f["severity"], "tie": ac if ac in ac_ids else "none",
                        "scope": "in", "reason": "collator_dropped", "title": f["title"],
                        "detail": f.get("detail"), "files": list(f.get("files") or [])})
    return records, stats


def scope_map(records) -> dict:
    """`{input id: (scope, tie)}` for every canonical and merged id."""
    out = {}
    for r in records or []:
        for i in [r["id"], *(r.get("merged") or [])]:
            out[i] = (r["scope"], r["tie"])
    return out


def out_ids(collation) -> list:
    return [i for i, (s, _t) in (collation or {}).get("scopes", {}).items() if s == "out"]


def annotate(verdicts: dict, scopes: dict) -> dict:
    """Pure: a copy of `verdicts` whose findings carry `scope` / `tie` from `scopes`
    (`{id: [scope, tie]}`); the stored verdicts are not touched."""
    out = {}
    for p, v in (verdicts or {}).items():
        raw = [f for f in (v or {}).get("findings") or [] if isinstance(f, dict)]
        fs = []
        for i, f in enumerate(raw):
            hit = scopes.get(f"{p}#{i}")
            fs.append(dict(f, scope=hit[0], tie=hit[1]) if hit else f)
        out[p] = dict(v, findings=fs)
    return out


def scoped(state, verdicts: dict) -> dict:
    """`verdicts` annotated with this verdict set's applied collation; unchanged when the flag is
    off or there is none (pending, failed, skipped or another verdict set's)."""
    if not on(state):
        return verdicts
    c = current(state, verdicts)
    if not c or c.get("status") != "done":
        return verdicts
    return annotate(verdicts, c.get("scopes") or {})


# ---- write -----------------------------------------------------------------------------------

def path_of(cfg, state, loop=None) -> Path:
    return store.campaign_dir(cfg, state["id"]) / name(loop or PK.loop_of(state))


def write(cfg, state, doc: dict) -> int:
    return PK._write(path_of(cfg, state, doc.get("loop")), doc)


def document(state, loop, status, records, items, *, stats=None, run_id=None, error=None) -> dict:
    """The `nw.collated_findings.v1` document of one loop. `inputs` keeps every original
    finding (verbatim), `collated` the records the routing used."""
    doc = {"schema": SCHEMA_ID, "campaign": state.get("id"), "spec_id": state.get("spec_id"),
           "loop": loop, "status": status, "run_id": run_id, "collated": records,
           "inputs": items, "stats": stats or {}}
    if error:
        doc["error"] = str(error)[:400]
    return doc


def fallback_records(items) -> list:
    """Today's routing as records: every finding its own, in scope, tie from its `ac` only."""
    return [{"id": f["id"], "merged": [], "persona": [f["persona"]], "severity": f["severity"],
             "tie": "none", "scope": "in", "reason": "collator_failed", "title": f["title"],
             "detail": f.get("detail"), "files": list(f.get("files") or [])} for f in items]


def followup_entries(spec_id, cid, loop, records, items) -> list:
    """The follow-up entries (`followups.make_entry` shape, origin `out_of_scope`) of the
    out-of-scope records: one per member finding (verbatim title / files, so its key is the
    close-time key), the detail naming the campaign, loop, tie and the collator's reason."""
    from .followups import make_entry, merge_entry
    by_id = {f["id"]: f for f in items}
    out: list = []
    for r in records:
        if r["scope"] != "out":
            continue
        for i in [r["id"], *(r.get("merged") or [])]:
            f = by_id.get(i)
            if f is None:
                continue
            e = make_entry(spec_id, {**f, "personas": [f["persona"]]}, "out_of_scope")
            head = f"out of scope ({cid} loop {loop}, tie {r['tie']}): {r.get('reason') or ''}"
            e["detail"] = head.strip() + (f" | {f['detail']}" if f.get("detail") else "")
            e["loop"], e["tie"] = loop, r["tie"]
            merge_entry(out, e)
    return out
