"""Post-hoc Definition-of-Done verification for internal specs (SPEC-0117).

Mechanical checks only — never mutates status or checkboxes. Agents still attest judgment
items (AC actually true, tests executed, body matches shipped) via the `wt-verify` skill.
"""
from __future__ import annotations

import re
from pathlib import Path

from .specmeta import sections, split_frontmatter
from .specs import _find_spec, _is_outbound_spec_id, _specs_dir

_CHECKED = re.compile(r"^- \[[xX]\]", re.M)
_UNCHECKED = re.compile(r"^- \[[ ]\]", re.M)
_PLACEHOLDER = re.compile(r"^<[^>]+>$")


def _checkbox_counts(text: str) -> tuple[int, int]:
    """(unchecked, total) for markdown task bullets."""
    text = text or ""
    unchecked = len(_UNCHECKED.findall(text))
    checked = len(_CHECKED.findall(text))
    return unchecked, unchecked + checked


def _child_frontmatters(specs_dir: Path, parent_id: str) -> list[dict]:
    children = []
    if not specs_dir.is_dir():
        return children
    for path in sorted(specs_dir.glob("[0-9]*.md")):
        try:
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not fm:
            continue
        if str(fm.get("parent") or "").strip() == parent_id:
            children.append(fm)
    return children


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def verify_spec(cfg, spec_id: str) -> dict:
    """Return wt.spec.verify.v1 audit for an internal SPEC-NNNN.

    Raises ValueError for outbound ids or missing files. Resolves the path, then delegates to
    `verify_spec_file` (SPEC-0175 factoring) so the same checks run on a spec inside a
    campaign worktree.
    """
    if _is_outbound_spec_id(spec_id):
        raise ValueError(
            f"{spec_id} is outbound — `wt spec verify` is internal SPEC-NNNN only. "
            f"In the target repo, run /wt-verify (portable / Procedure B) on the "
            f"exported markdown; optional later: wt spec pull-status from wt (SPEC-0117)"
        )

    specs_dir = Path(_specs_dir(cfg))
    path = _find_spec(specs_dir, spec_id)
    return verify_spec_file(path, specs_dir=specs_dir, spec_id=spec_id)


def verify_spec_file(path, *, specs_dir=None, spec_id: str | None = None) -> dict:
    """The mechanical DoD audit for one spec file (SPEC-0175): same checks and payload as
    `verify_spec`, computed from a path — e.g. a spec inside a campaign worktree, or an
    outbound portable file. Epic-children checks need `specs_dir`; without it they are
    skipped. Raises ValueError when the file is missing."""
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"spec file not found: {path}")
    specs_dir = Path(specs_dir) if specs_dir else None
    text = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(text)
    fm = fm or {}
    secs = sections(body or "")
    kind = str(fm.get("kind") or "feature").lower()
    status = str(fm.get("status") or "").lower()
    checks: list[dict] = []

    if status:
        checks.append(_check("status", "pass", f"status={status}"))
    else:
        checks.append(_check("status", "fail", "frontmatter status missing"))

    ac = secs.get("Acceptance criteria", "") or ""
    ac_unchecked, ac_total = _checkbox_counts(ac)
    if kind == "epic":
        if ac_total == 0 and not ac.strip():
            checks.append(_check(
                "acceptance", "skip",
                "epic may use Breakdown / sub-specs instead of AC checkboxes"))
        elif ac_unchecked and status in ("done", "in-progress"):
            checks.append(_check(
                "acceptance", "fail",
                f"{ac_unchecked}/{ac_total} Acceptance criteria unchecked"))
        else:
            checks.append(_check(
                "acceptance", "pass",
                f"{ac_total - ac_unchecked}/{ac_total} AC checked"
                if ac_total else "AC section present"))
    else:
        if not ac.strip():
            checks.append(_check("acceptance", "fail", "missing Acceptance criteria section"))
        elif ac_total == 0:
            checks.append(_check(
                "acceptance", "warn",
                "Acceptance criteria has no checkbox bullets"))
        elif ac_unchecked and status in ("done", "in-progress"):
            checks.append(_check(
                "acceptance", "fail",
                f"{ac_unchecked}/{ac_total} Acceptance criteria unchecked"))
        else:
            checks.append(_check(
                "acceptance", "pass",
                f"{ac_total - ac_unchecked}/{ac_total} AC checked"))

    tp = (secs.get("Test plan", "") or "").strip()
    if not tp:
        checks.append(_check("test_plan", "fail", "missing or empty Test plan"))
    elif _PLACEHOLDER.match(tp.splitlines()[0].strip()):
        checks.append(_check("test_plan", "fail", "Test plan still a template placeholder"))
    elif "N/A" in tp[:80] and kind == "feature":
        checks.append(_check(
            "test_plan", "warn",
            "Test plan is N/A — confirm policy/spike exemption"))
    else:
        checks.append(_check("test_plan", "pass", "Test plan section present"))

    oq = (secs.get("Open questions", "") or "").strip()
    if not oq or oq.lower().startswith("(none"):
        checks.append(_check("open_questions", "pass", "Open questions clear"))
    elif _UNCHECKED.search(oq) or re.search(r"(?im)^-\s+(?!\[)", oq):
        checks.append(_check(
            "open_questions", "warn",
            "Open questions section still has bullets — resolve before done"))
    else:
        checks.append(_check("open_questions", "pass", "Open questions present but no open boxes"))

    if kind == "epic" and specs_dir is None:
        checks.append(_check("epic_children", "skip", "no specs_dir given for children lookup"))
        checks.append(_check("epic_breakdown", "skip", "no specs_dir given for children lookup"))
    elif kind == "epic":
        children = _child_frontmatters(specs_dir, fm.get("id") or spec_id or "")
        incomplete = [
            f"{c.get('id')}={c.get('status')}"
            for c in children
            if str(c.get("status") or "").lower() not in ("done", "superseded")
        ]
        if not children:
            checks.append(_check(
                "epic_children", "warn",
                "epic has no parent: children on disk"))
        elif incomplete and status == "done":
            checks.append(_check(
                "epic_children", "fail",
                f"unfinished children: {', '.join(incomplete)}"))
        elif incomplete:
            checks.append(_check(
                "epic_children", "warn",
                f"unfinished children: {', '.join(incomplete)}"))
        else:
            checks.append(_check(
                "epic_children", "pass",
                f"{len(children)} children done/superseded"))

        bd = secs.get("Breakdown / sub-specs", "") or ""
        bd_unchecked, bd_total = _checkbox_counts(bd)
        if bd_total and bd_unchecked and status == "done":
            checks.append(_check(
                "epic_breakdown", "fail",
                f"{bd_unchecked}/{bd_total} Breakdown boxes unchecked"))
        elif bd_total and bd_unchecked:
            checks.append(_check(
                "epic_breakdown", "warn",
                f"{bd_unchecked}/{bd_total} Breakdown boxes unchecked"))
        elif bd_total:
            checks.append(_check(
                "epic_breakdown", "pass",
                f"{bd_total} Breakdown boxes checked or none open"))
        else:
            checks.append(_check(
                "epic_breakdown", "skip",
                "no Breakdown checkboxes"))
    else:
        checks.append(_check("epic_children", "skip", "not an epic"))
        checks.append(_check("epic_breakdown", "skip", "not an epic"))

    failed = [c for c in checks if c["status"] == "fail"]
    judgment = [
        "Every Acceptance criterion is actually true (not merely checked).",
        "Test plan executed: automated tests written/passing; manual steps done and noted.",
        "No regressions: full pre-existing suite still passes (uv run pytest).",
        "Spec body reflects what shipped (Decision/Design not stale).",
        "docs/LEDGER.md regenerated; python3 tools/spec_lint.py exits 0.",
    ]
    if kind == "epic":
        judgment.append("All child specs are done or superseded (linter also gates epic done).")

    return {
        "schema": "wt.spec.verify.v1",
        "id": fm.get("id") or spec_id or path.stem,
        "path": str(path),
        "kind": kind,
        "status": status,
        "ok": not failed,
        "checks": checks,
        "judgment": judgment,
    }
