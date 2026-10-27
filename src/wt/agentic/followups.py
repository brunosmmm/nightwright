"""Follow-up ideas for non-blocking review findings (SPEC-0207, grouped by SPEC-0239).

At the close step the advisory and waived findings of a campaign that passed review are filed,
idempotently and without ever blocking the merge, as ONE grouped idea per campaign: a checklist
with one `wtfu:<key12>` line per finding, found again through the heading marker
`wtfug:<campaign id>`. A finding's `key` (stable across loops: spec id + normalized title +
cited files) is what carry-over, the `followup` ledger event and the checklist line use. Pure
helpers here; `queue._merge_carry` and `queue._do_close` call them. `group_legacy` is the
one-off migration of the old one-idea-per-finding singles."""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path

import orgparse
from orgparse.node import OrgEnv

from .. import explore as EX
from .. import org as ORG
from .. import org_write as OW
from ..specmeta import split_frontmatter
from . import store
from .profiles import SEVERITIES, followup_cap, followup_params

MARKER = "wtfu:"
GROUP_MARKER = "wtfug:"                                     # does not contain MARKER
TITLE_MAX, DETAIL_MAX, SLOT_MAX, FILES_MAX = 120, 200, 80, 5
FILED_SEVERITIES = ("major", "minor")

_RANK = {s: i for i, s in enumerate(SEVERITIES)}          # lower = more severe
_LINE_RE = re.compile(r"^- \[[ Xx]\] wtfu:([0-9a-f]{12})\b", re.M)   # a checklist line
_OVERFLOW_RE = re.compile(r"^- \+\d+ more findings not listed")
_SINGLE_RE = re.compile(r"^wtfu:([0-9a-f]{12}) \[(SPEC-\d{4})\] (.+)$")   # a legacy single


def finding_key(spec_id, title, files) -> str:
    """`sha1(spec_id | norm_finding_title(title) | ",".join(sorted(files)))` (hex)."""
    from .queue import norm_finding_title                   # queue imports this module
    raw = f"{spec_id}|{norm_finding_title(title)}|{','.join(sorted(files or []))}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def make_entry(spec_id, finding: dict, origin: str) -> dict:
    """The working entry of one `last_review.advisory` / `waived` finding."""
    files = list(finding.get("files") or [])
    entry = {"key": finding_key(spec_id, finding.get("title"), files),
             "personas": list(finding.get("personas") or []),
             "severity": finding.get("severity") or "minor",
             "title": finding.get("title") or "", "detail": finding.get("detail"),
             "files": files, "ac": finding.get("ac"), "category": finding.get("category"),
             "origin": origin}
    if origin == "waived":
        entry["waive_reason"] = finding.get("reason")
    return entry


def merge_entry(entries: list, new: dict) -> None:
    """Add `new` to `entries` (a list de-duplicated by key). On a collision the highest
    severity wins, `personas` are unioned and `waived` wins over `advisory`."""
    for e in entries:
        if e["key"] != new["key"]:
            continue
        for p in new.get("personas") or []:
            if p not in e["personas"]:
                e["personas"].append(p)
        if _RANK.get(new["severity"], 9) < _RANK.get(e["severity"], 9):
            e.update(severity=new["severity"], detail=new.get("detail"), ac=new.get("ac"),
                     category=new.get("category"))
        if new["origin"] == "waived" and e["origin"] != "waived":
            e.update(origin="waived", waive_reason=new.get("waive_reason"))
        return
    entries.append(dict(new, personas=list(new.get("personas") or [])))


def considered(spec_id, carry, review: dict) -> list:
    """The carry ∪ the final review's advisory ∪ waived findings, de-duplicated by key."""
    out: list = []
    for e in carry or []:
        merge_entry(out, e)
    for origin, items in (("advisory", review.get("advisory")), ("waived", review.get("waived"))):
        for f in items or []:
            merge_entry(out, make_entry(spec_id, f, origin))
    return out


def clean(text) -> str:
    """Org-safe single-line text: control characters become spaces, `[`/`]` are dropped,
    `—` is written `-`, `*` and `:` become spaces (no tag suffix or heading marker forms)."""
    s = "".join(" " if unicodedata.category(c).startswith("C") else c for c in str(text or ""))
    s = s.replace("[", "").replace("]", "").replace("—", "-").replace("*", " ").replace(":", " ")
    return " ".join(s.split())


def idea_text(spec_id, entry: dict) -> str:
    """`wtfu:<key12> [<spec>] <persona>/<slot>: <title> — <detail> (files: …)`; the marker
    leads (alphanumeric, so neither cap nor sanitizing can alter it)."""
    persona = (entry.get("personas") or ["review"])[0]
    slot = clean(entry.get("ac") or entry.get("category"))[:SLOT_MAX].strip()
    head = f"{MARKER}{entry['key'][:12]} [{spec_id}] {clean(persona)}"
    head += f"/{slot}" if slot else ""
    text = f"{head}: {clean(entry.get('title'))[:TITLE_MAX].strip()}"
    detail = clean(entry.get("detail"))[:DETAIL_MAX].strip()
    if detail:
        text += f" — {detail}"
    files = [clean(f) for f in entry.get("files") or [] if clean(f)]
    if files:
        shown = ", ".join(files[:FILES_MAX])
        text += f" (files: {shown}{f', +{len(files) - FILES_MAX} more' if len(files) > FILES_MAX else ''})"
    return text


def find_marker(cfg, marker: str, *, open_only=False) -> str | None:
    """The `:ID:` of the idea whose headline contains `marker`, looked up in the ideas file
    and its archive through the org reader; None when absent. With `open_only` (SPEC-0266) a
    closed idea counts as absent and only the ideas file is read."""
    todos, dones = ORG._split_keywords(cfg.get("org_idea_keywords") or [
        "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED"])
    paths = [os.path.expanduser(cfg["org_ideas_file"])]
    if not open_only:
        paths.append(OW.ideas_archive_path(cfg))
    for path in paths:
        if not os.path.exists(path):
            continue
        root = orgparse.load(path, env=OrgEnv(todos=todos, dones=dones, filename=path))
        for node in root[1:]:
            if open_only and node.todo in dones:
                continue
            if marker in (node.heading or ""):
                idea_id = node.properties.get("ID")
                if not idea_id:                             # cannot adopt it; never double-file
                    raise ValueError(f"idea {marker} at {path}:{node.linenumber} has no :ID:")
                return idea_id
    return None


def parent_epic(state: dict) -> str | None:
    """The `parent` frontmatter of the governing spec, read in the main checkout."""
    rel = (state.get("spec") or {}).get("rel_path")
    root = (state.get("repo") or {}).get("root")
    if not rel or not root:
        return None
    try:
        fm, _ = split_frontmatter(Path(root, rel).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (fm or {}).get("parent") or None


def group_title(token: str, spec_id, n: int) -> str:
    """`wtfug:<token> [<spec>] review follow-ups (<n> findings)`; `n` counts checklist lines."""
    return (f"{GROUP_MARKER}{token} [{spec_id}] review follow-ups "
            f"({n} finding{'' if n == 1 else 's'})")


def checklist_line(spec_id, entry: dict) -> str:
    """`- [ ] wtfu:<key12> <severity> persona/slot: title — detail (files: …)`: `idea_text`'s
    bounds with the `[<spec>] ` part dropped and the severity after the per-finding marker."""
    marker, rest = idea_text(spec_id, entry).split(" ", 1)
    rest = rest.split(" ", 1)[1]                            # drop "[<spec>]"
    return f"- [ ] {marker} {clean(entry.get('severity'))} {rest}"


def summary_keys(summary: str) -> list:
    """The key12 of each checklist line (ticked ones too), in order, de-duplicated."""
    return list(dict.fromkeys(_LINE_RE.findall(summary or "")))


def overflow_line(count: int, campaign_id: str) -> str:
    return f"- +{count} more findings not listed (wt campaign show {campaign_id})"


def _group_task(cfg, idea_id: str):
    """The `Task` of a grouped idea (the enrichment reader needs `task.file`)."""
    return OW.resolve_selector(cfg, idea_id)


def open_group_keys(cfg, spec_id, *, exclude=None) -> dict:
    """`{key12: idea_id}` of the checklist lines of every open grouped idea of `spec_id`
    (`wtfug:` in the heading, an active state), except the idea `exclude`. Only the ideas file
    is read: an archived idea is closed by construction."""
    out: dict = {}
    for t in ORG.load_tasks(cfg):
        if (not t.is_idea or t.is_done or GROUP_MARKER not in t.heading
                or f"[{spec_id}]" not in t.heading):
            continue
        idea_id = t.properties.get("ID")
        if not idea_id or idea_id == exclude:
            continue
        for key in summary_keys(EX.read_idea_enrichment(cfg, t)["summary"]):
            out.setdefault(key, idea_id)
    return out


def _open_group(cfg, marker: str) -> tuple:
    """`(idea_id, Task)` of the open (active) idea whose heading contains `marker`, else
    `(None, None)`; a closed group is never a migration target."""
    for t in ORG.load_tasks(cfg):
        if t.is_idea and not t.is_done and marker in t.heading and t.properties.get("ID"):
            return t.properties["ID"], t
    return None, None


def _note(text: str):
    return ("note", {"text": text, "author": "queue"})


def file_followups(cfg, state: dict, profile: dict) -> tuple[list, dict]:
    """File the follow-up idea of the campaign about to close. Returns `(events, summary)`:
    the `followup` / `note` events for `StepResult.events_before` and the
    `{filed, unfiled, waived, capped, idea_id}` summary for `StepResult.followups`. Never
    raises: any failure becomes a `note` event and the affected findings are `unfiled`."""
    events: list = []
    summary: dict = {"filed": [], "unfiled": [], "waived": [], "capped": [], "idea_id": None}
    pending: list = []                                      # findings still to be handled
    try:
        _file(cfg, state, profile or {}, events, summary, pending)
    except Exception as e:                                  # the filer never blocks a merge
        events.append(_note(f"follow-ups: filer failed: {e!r}"))
        for item in pending:                                # not reported anywhere yet
            summary["unfiled"].append({"severity": item["severity"], "title": item["title"],
                                       "error": repr(e)})
    return events, summary


def _event(e: dict, idea_id) -> tuple:
    return ("followup", {
        "persona": (e["personas"] or [None])[0], "severity": e["severity"],
        "title": e["title"], "key": e["key"], "idea_id": idea_id, "origin": e["origin"]})


def _file(cfg, state, profile, events, summary, pending) -> None:
    spec_id, cid = state.get("spec_id"), state["id"]
    enabled, nits = followup_params(profile)
    items = considered(spec_id, state.get("review_carry"), state.get("last_review") or {})
    summary["waived"] = [{"severity": e["severity"], "title": e["title"],
                          "waive_reason": e.get("waive_reason")}
                         for e in items if e["origin"] == "waived"]
    if not enabled:
        return
    wanted = FILED_SEVERITIES + (("nit",) if nits else ())
    items = sorted((e for e in items if e["severity"] in wanted),
                   key=lambda e: (_RANK.get(e["severity"], 9), e["origin"] != "advisory",
                                  e["title"]))
    if not items:
        return
    pending.extend(items)                                   # the outer guard reports leftovers

    def report(e, idea_id, ev=True):
        pending.remove(e)
        if ev:
            events.append(_event(e, idea_id))
        summary["filed"].append({"severity": e["severity"], "title": e["title"],
                                 "idea_id": idea_id, "origin": e["origin"]})

    known = {e["key"]: e for e in store.read_ledger(cfg, cid, types=["followup"])}
    left = []
    for e in items:                                         # 1: filed by an earlier run
        prior = known.get(e["key"])
        if prior is None:
            left.append(e)
            continue
        pending.remove(e)
        summary["filed"].append({"severity": e["severity"], "title": e["title"],
                                 "idea_id": prior.get("idea_id"),
                                 "origin": prior.get("origin") or e["origin"]})

    # SPEC-0266: the group read-modify-write runs under the (re-entrant) org lock, so another
    # writer's Summary edit waits for it instead of being overwritten with a stale copy
    with OW.org_write_lock(cfg):
        # adopt an open own group (crash after add_idea); a closed one counts as absent
        own = find_marker(cfg, f"{GROUP_MARKER}{cid}", open_only=True)
        own_task = _group_task(cfg, own) if own else None
        own_text = EX.read_idea_enrichment(cfg, own_task)["summary"] if own else ""
        own_keys = summary_keys(own_text)
        summary["idea_id"] = own

        recovered = [e for e in left if e["key"][:12] in own_keys]  # 2: line written, no event
        for e in recovered:
            report(e, own)
        left = [e for e in left if e not in recovered]
        recovered_keys = {e["key"][:12] for e in recovered}

        if left:                                            # 3: listed in another open group
            elsewhere = open_group_keys(cfg, spec_id, exclude=own)
            for e in [e for e in left if e["key"][:12] in elsewhere]:
                report(e, elsewhere[e["key"][:12]])
            left = [e for e in left if e["key"][:12] not in elsewhere]

        cap = followup_cap(profile)                         # 4: sort and cap
        kept, capped = left, []
        if cap > 0:
            lines_before = sum(1 for k in own_keys if k not in recovered_keys)
            room = max(0, cap - lines_before - len(recovered))
            kept, capped = left[:room], left[room:]
        for e in capped:
            pending.remove(e)
            summary["capped"].append({"severity": e["severity"], "title": e["title"]})

        if not kept and not own:
            return
        try:                                                # 5: write the group
            group = own
            if group is None:
                _, headline, group = OW.add_idea(
                    cfg, group_title(cid, spec_id, 0), project=state.get("project"),
                    epic=parent_epic(state), kind="improvement")
                summary["idea_id"] = group
                if f"{GROUP_MARKER}{cid}" not in (headline or ""):
                    events.append(_note(f"follow-up group {group}: headline lost its "
                                        f"{GROUP_MARKER}{cid} marker"))
            lines = [ln for ln in own_text.splitlines() if not _OVERFLOW_RE.match(ln)]
            lines += [checklist_line(spec_id, e) for e in kept if e["key"][:12] not in own_keys]
            if capped:
                lines.append(overflow_line(len(capped), cid))
            text = "\n".join(lines).strip()
            if text != own_text.strip():
                EX.set_summary(cfg, group, text)
            title = group_title(cid, spec_id, len(summary_keys(text)))
            if own_task is None or own_task.heading != title:
                EX.retitle_idea(cfg, group, title)
        except Exception as err:                            # 6: merge goes on
            events.append(_note(f"follow-ups not filed: {err!r}"))
            for e in kept:
                pending.remove(e)
                summary["unfiled"].append({"severity": e["severity"], "title": e["title"],
                                           "error": repr(err)})
            return
    for e in kept:
        report(e, group)


# ---- migration of the SPEC-0207 singles (SPEC-0239) -------------------------------------------

_IDEA_NUM = re.compile(r"IDEA-(\d+)")


def _idea_order(t):
    m = _IDEA_NUM.match(t.properties.get("ID") or "")
    return int(m.group(1)) if m else 0


def group_legacy(cfg, *, apply: bool) -> dict:
    """Fold the open single `wtfu:` ideas into one grouped idea per spec (`legacy-<SPEC>`) and
    close the singles as DROPPED with FOLDED_INTO the group. Returns the
    `wt.ideas.group_followups.v1` report; without `apply` nothing is written. Migrated lines
    (`wtfu:<key12> <rest of heading> (was IDEA-NNN)`) differ in shape from filer lines on
    purpose: dedup reads only the key12 token."""
    ORG.invalidate_tasks_cache()
    by_spec: dict = {}
    for t in sorted((t for t in ORG.load_tasks(cfg) if t.is_idea and not t.is_done),
                    key=_idea_order):
        m = _SINGLE_RE.match(t.heading)
        if m and t.properties.get("ID"):
            by_spec.setdefault(m.group(2), []).append((t, m.group(1), m.group(3)))
    groups = []
    for spec_id in sorted(by_spec):
        singles = by_spec[spec_id]
        token = f"legacy-{spec_id}"
        target, target_task = _open_group(cfg, f"{GROUP_MARKER}{token}")
        listed = open_group_keys(cfg, spec_id)              # includes the target
        text = EX.read_idea_enrichment(cfg, target_task)["summary"] if target else ""
        lines, new_keys, fold = [], set(summary_keys(text)), {}
        for t, key, rest in singles:
            sid = t.properties["ID"]
            if key in listed:
                fold[sid] = listed[key]
            else:                                           # folds into the group itself
                if key not in new_keys:
                    new_keys.add(key)
                    lines.append(f"- [ ] {MARKER}{key} {rest} (was {sid})")
                fold[sid] = None
        groups.append({"spec_id": spec_id, "singles": [t.properties["ID"] for t, _, _ in singles],
                       "target": target or "new", "lines": lines, "_fold": fold, "_text": text, "_first": singles[0][0],
                       "group_id": target})
    if apply:
        for g in groups:
            _apply_group(cfg, g)
    for g in groups:
        for k in ("_fold", "_text", "_first"):
            g.pop(k)
        if not apply:
            g.pop("group_id")
    return {"schema": "wt.ideas.group_followups.v1", "written": apply, "groups": groups,
            "singles": sum(len(g["singles"]) for g in groups)}


def _apply_group(cfg, g: dict) -> None:
    spec_id, token = g["spec_id"], f"legacy-{g['spec_id']}"
    group = g["group_id"]
    if group is None and g["lines"]:
        first = g["_first"]
        _, _, group = OW.add_idea(cfg, group_title(token, spec_id, 0),
                                  project=first.project or None, epic=first.epic,
                                  kind="improvement")
    if group is not None:                                   # also repairs a crashed retitle
        text = "\n".join([*g["_text"].splitlines(), *g["lines"]]).strip()
        if text != g["_text"].strip():
            EX.set_summary(cfg, group, text)
        EX.retitle_idea(cfg, group, group_title(token, spec_id, len(summary_keys(text))))
    g["group_id"] = group
    for sid in g["singles"]:
        EX.close_idea(cfg, sid, "drop", reason="merged into grouped review follow-ups",
                      folded_into=g["_fold"][sid] or group)
