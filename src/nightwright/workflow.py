"""Next-step hints for the idea→spec→task workflow (SPEC-0023).

`next_step_for_idea` maps an idea Task (+ optional linked spec frontmatter) to a short
command string humans (and `nw next` / `nw ideas`) can act on. Fail soft: missing/unloadable
specs yield a clear fallback hint, never an exception to the CLI.

SPEC-0033: incomplete linked epics hint `/nw-rework {idea_id}` instead of premature
`nw tasks` / bare generate-after-export.
"""
from __future__ import annotations

import re
from pathlib import Path

from .specmeta import sections, split_frontmatter
from .specs import _find_spec, _specs_dir, outbox_dir

# Idea states that mean "still capturing / incubating" (no promote yet).
_CAPTURE = frozenset({"IDEA", "INCUBATE"})
_AUthxx = frozenset({"draft", "proposed"})
_READY = frozenset({"accepted", "in-progress"})
_DONE = frozenset({"done"})
_CHILD_COMPLETE = frozenset({"done", "superseded"})

_UNCHECKED_RE = re.compile(r"^- \[[ ]\]", re.M)
_CHECKED_RE = re.compile(r"^- \[[xX]\]", re.M)


def _is_outbound_id(spec_id: str) -> bool:
    """Outbound ids are PROJ-NNNN (not SPEC-NNNN)."""
    return bool(spec_id) and not str(spec_id).upper().startswith("SPEC-")


def _resolve_spec_path(cfg, spec_id: str) -> Path | None:
    """Internal or outbound spec path for `spec_id`, or None if missing."""
    if not spec_id:
        return None
    try:
        if _is_outbound_id(spec_id):
            root = outbox_dir(cfg)
            matches = sorted(root.glob(f"*/{spec_id}-*.md")) if root.exists() else []
            return matches[0] if matches else None
        return _find_spec(_specs_dir(cfg), spec_id)
    except Exception:
        return None


def _load_spec_fm(cfg, spec_id: str) -> dict | None:
    """Return frontmatter for an internal or outbound spec id, or None if missing/unloadable."""
    path = _resolve_spec_path(cfg, spec_id)
    if path is None:
        return None
    try:
        fm, _ = split_frontmatter(Path(path).read_text(encoding="utf-8"))
        return fm or {}
    except Exception:
        return None


def _outbound_exported(cfg, outbound_id: str) -> bool:
    """True if any outbox PROVENANCE.md mentions this outbound id (best-effort)."""
    try:
        root = outbox_dir(cfg)
        if not root.exists():
            return False
        for prov in root.glob("*/PROVENANCE.md"):
            if outbound_id in prov.read_text(encoding="utf-8"):
                return True
    except Exception:
        return False
    return False


def _child_specs(cfg, epic_id: str, epic_path: Path) -> list[dict]:
    """Frontmatter dicts for specs with `parent: epic_id` in the epic's namespace."""
    if _is_outbound_id(epic_id):
        paths = sorted(epic_path.parent.glob("*.md"))
    else:
        specs_dir = _specs_dir(cfg)
        paths = sorted(specs_dir.glob("[0-9]*.md")) if specs_dir.exists() else []
    children = []
    for path in paths:
        if path.resolve() == epic_path.resolve():
            continue
        try:
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not fm:
            continue
        if str(fm.get("parent") or "").strip() == epic_id:
            children.append(fm)
    return children


def _breakdown_checkbox_counts(body: str) -> tuple[int, int]:
    """(unchecked, total) checkbox counts under `## Breakdown / sub-specs`."""
    text = sections(body).get("Breakdown / sub-specs", "") or ""
    unchecked = len(_UNCHECKED_RE.findall(text))
    checked = len(_CHECKED_RE.findall(text))
    return unchecked, unchecked + checked


def _epic_needs_extend(cfg, spec_id: str, fm: dict, body: str, path: Path) -> bool:
    """True when a ready/done epic still has open Breakdown work or incomplete children."""
    if str(fm.get("kind") or "").lower() != "epic":
        return False
    status = str(fm.get("status") or "").lower()
    if status not in _READY | _DONE:
        return False
    children = _child_specs(cfg, spec_id, path)
    unchecked, total = _breakdown_checkbox_counts(body)
    if unchecked > 0:
        return True
    if total > 0 and not children:
        return True
    for child in children:
        if str(child.get("status") or "").lower() not in _CHILD_COMPLETE:
            return True
    return False


def next_step_for_idea(cfg, idea) -> str:
    """Return the next recommended command/hint for an idea Task.

    Rules (SPEC-0023 + SPEC-0024 explore gate + SPEC-0033 epic extend):
      - DROPPED → ""
      - no :SPEC:, Summary empty → "nw idea log {id}"
      - no :SPEC:, Summary present → "nw spec new --from-idea {id}"
      - linked spec missing → "spec {id} missing"
      - draft/proposed → "authxx {id} → accepted"
      - incomplete epic (ready/done) → "/nw-rework {idea_id}"
      - PROMOTED (complete / non-epic, internal) → "nw tasks"
      - outbound ready, not exported → "nw spec export {id}"
      - outbound exported / EXPORTED → implement / pull-status+pull-clock / quiet
        (SPEC-0041/SPEC-0068)
      - internal accepted/in-progress → "nw spec generate {id}"
      - done → "nw tasks"
    """
    state = (idea.state or "").upper()
    if state in ("DROPPED", "RESEARCHED", "SHIPPED"):  # terminal closes → no hint
        return ""

    idea_id = idea.properties.get("ID") or idea.id
    spec_id = (idea.properties.get("SPEC") or "").strip()

    handed = (idea.properties.get("HANDED_OFF") or "").strip()        # SPEC-0280
    if handed and state == "SPECCED":
        return (f"PR open ({handed}); after it merges: nw spec reconcile --apply; "
                f"if it was closed: nw queue add {spec_id or '<SPEC>'}")

    if not spec_id:
        if state == "PROMOTED":
            return "nw tasks"
        from .explore import idea_summary_text
        if not idea_summary_text(cfg, idea).strip():
            return f"nw idea log {idea_id}"
        return f"nw spec new --from-idea {idea_id}"

    path = _resolve_spec_path(cfg, spec_id)
    if path is None:
        if state == "PROMOTED":
            return "nw tasks"
        return f"spec {spec_id} missing"

    try:
        fm, body = split_frontmatter(Path(path).read_text(encoding="utf-8"))
        fm = fm or {}
    except Exception:
        if state == "PROMOTED":
            return "nw tasks"
        return f"spec {spec_id} missing"

    # Incomplete epics before PROMOTED / export / generate short-circuits (SPEC-0033).
    if _epic_needs_extend(cfg, spec_id, fm, body, path):
        return f"/nw-rework {idea_id}"

    # PROMOTED = internal generate path only (SPEC-0041). Outbound uses EXPORTED.
    if state == "PROMOTED":
        return "nw tasks"

    status = str(fm.get("status") or "").lower()

    if status in _AUthxx or status == "":
        return f"authxx {spec_id} → accepted"

    if _is_outbound_id(spec_id):
        if status in _READY | _DONE:
            exported = state == "EXPORTED" or _outbound_exported(cfg, spec_id)
            if not exported:
                return f"nw spec export {spec_id}"
            if status == "done":
                return ""
            if status == "in-progress":
                return f"nw spec pull-status/pull-clock {spec_id}"
            target = (fm.get("target_repo") or fm.get("target_project") or "target").strip()
            return f"implement in {target}"
        return f"review {spec_id} ({status or '?'})"

    if status in _READY:
        return f"nw spec generate {spec_id}"
    if status in _DONE:
        return "nw tasks"

    return f"review {spec_id} ({status or '?'})"
