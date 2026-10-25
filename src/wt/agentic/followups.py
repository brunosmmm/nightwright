"""Follow-up ideas for non-blocking review findings (SPEC-0207).

At the close step the advisory and waived findings of a campaign that passed review are filed
as ideas, idempotently and without ever blocking the merge. A finding's `key` (stable across
loops: spec id + normalized title + cited files) is what carry-over, the `followup` ledger
event and the idea marker `wtfu:<key12>` use. Pure helpers here; `queue._merge_carry` and
`queue._do_close` call them."""
from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path

import orgparse
from orgparse.node import OrgEnv

from .. import org as ORG
from .. import org_write as OW
from ..specmeta import split_frontmatter
from . import store
from .profiles import SEVERITIES, followup_params

MARKER = "wtfu:"
TITLE_MAX, DETAIL_MAX, SLOT_MAX, FILES_MAX = 120, 200, 80, 5
FILED_SEVERITIES = ("major", "minor")

_RANK = {s: i for i, s in enumerate(SEVERITIES)}          # lower = more severe


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


def find_marker(cfg, marker: str) -> str | None:
    """The `:ID:` of the idea whose headline contains `marker`, looked up in the ideas file
    and its archive through the org reader; None when absent."""
    todos, dones = ORG._split_keywords(cfg.get("org_idea_keywords") or [
        "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED"])
    for path in (os.path.expanduser(cfg["org_ideas_file"]), OW.ideas_archive_path(cfg)):
        if not os.path.exists(path):
            continue
        root = orgparse.load(path, env=OrgEnv(todos=todos, dones=dones, filename=path))
        for node in root[1:]:
            if marker in (node.heading or ""):
                return node.properties.get("ID") or f"{path}:{node.linenumber}"
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


def _note(text: str):
    return ("note", {"text": text, "author": "queue"})


def file_followups(cfg, state: dict, profile: dict) -> tuple[list, dict]:
    """File the follow-up ideas of the campaign about to close. Returns `(events, summary)`:
    the `followup` / `note` events for `StepResult.events_before` and the
    `{filed, unfiled, waived}` summary for `StepResult.followups`. Never raises: any failure
    becomes a `note` event and the affected findings are `unfiled`."""
    events: list = []
    summary: dict = {"filed": [], "unfiled": [], "waived": []}
    try:
        _file(cfg, state, profile or {}, events, summary)
    except Exception as e:                                  # the filer never blocks a merge
        events.append(_note(f"follow-ups: filer failed: {e!r}"))
    return events, summary


def _file(cfg, state, profile, events, summary) -> None:
    spec_id = state.get("spec_id")
    enabled, nits = followup_params(profile)
    items = considered(spec_id, state.get("review_carry"), state.get("last_review") or {})
    summary["waived"] = [{"severity": e["severity"], "title": e["title"],
                          "waive_reason": e.get("waive_reason")}
                         for e in items if e["origin"] == "waived"]
    if not enabled:
        return
    wanted = FILED_SEVERITIES + (("nit",) if nits else ())
    items = [e for e in items if e["severity"] in wanted]
    if not items:
        return
    known = {e["key"]: e for e in store.read_ledger(cfg, state["id"], types=["followup"])}
    epic = parent_epic(state)
    for e in items:
        base = {"severity": e["severity"], "title": e["title"]}
        try:
            prior = known.get(e["key"])
            if prior is not None:                           # 1: already filed by an earlier run
                summary["filed"].append(dict(base, idea_id=prior.get("idea_id"),
                                             origin=prior.get("origin") or e["origin"]))
                continue
            marker = f"{MARKER}{e['key'][:12]}"
            idea_id = find_marker(cfg, marker)              # 2: adopt (crash after add_idea)
            if idea_id is None:                             # 3: capture
                _, headline, idea_id = OW.add_idea(
                    cfg, idea_text(spec_id, e), project=state.get("project"), epic=epic,
                    kind="improvement")
                if marker not in (headline or ""):
                    events.append(_note(f"follow-up {idea_id}: headline lost its {marker} "
                                        f"marker"))
            events.append(("followup", {
                "persona": (e["personas"] or [None])[0], "severity": e["severity"],
                "title": e["title"], "key": e["key"], "idea_id": idea_id,
                "origin": e["origin"]}))
            summary["filed"].append(dict(base, idea_id=idea_id, origin=e["origin"]))
        except Exception as err:                            # 4: capture error, merge goes on
            events.append(_note(f"follow-up not filed: [{e['severity']}] {e['title']}: "
                                f"{err!r}"))
            summary["unfiled"].append(dict(base, error=repr(err)))
