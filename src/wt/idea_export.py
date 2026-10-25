"""Read-only filtered idea subtree export (SPEC-0132)."""
from __future__ import annotations

import os
import re

from .org_write import _HEADLINE_RE, _locate_headline
from .report import collect_idea_tasks

_TOP_LEVEL_RE = re.compile(r"^\*\s")


def idea_subtree_lines(task) -> list[str]:
    """Return the top-level idea subtree lines (headline through next `*` peer / EOF).

    Same span as `archive_idea`, but read-only — never mutates the source file.
    """
    if not task.is_idea:
        raise ValueError(f"{task.id} is not an idea")
    lines = open(task.file, encoding="utf-8").read().splitlines(keepends=True)
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} in {task.file} is not a headline: {lines[idx]!r}")
    if len(m.group(1)) != 1:
        raise ValueError(f"{task.file}:{task.line}: expected a top-level idea headline")
    first = m.group(3).split(None, 1)[0] if m.group(3).split() else ""
    if task.state and first != task.state:
        raise ValueError(f"drift: {task.file}:{task.line} expected state {task.state!r}, "
                         f"found {first!r} — re-run after a fresh parse")
    end = idx + 1
    while end < len(lines) and not _TOP_LEVEL_RE.match(lines[end]):
        end += 1
    return lines[idx:end]


def _idea_id(task) -> str:
    return task.properties.get("ID") or task.id or ""


def export_idea_subtrees(cfg, path: str | None = None, *, dry_run: bool = False,
                         state=None, all_done=False, kind=None, tag=None, project=None,
                         epic=None, workstream=None, priority=None) -> list[str]:
    """Export filtered idea subtrees to `path` (or dry-run). Returns idea ids in write order.

    Selection uses `collect_idea_tasks` — the same filter vocabulary as `wt ideas`.
    """
    tasks = collect_idea_tasks(
        cfg, state=state, all_done=all_done, kind=kind, tag=tag, project=project,
        epic=epic, workstream=workstream, priority=priority, sort="id",
    )
    # Stable id order; dedupe by :ID: (first wins — prefer fresher collect order then sort)
    seen: set[str] = set()
    ordered = []
    for task in sorted(tasks, key=lambda t: _idea_id(t)):
        iid = _idea_id(task)
        if not iid or iid in seen:
            continue
        seen.add(iid)
        ordered.append(task)

    ids = [_idea_id(t) for t in ordered]
    if dry_run or path is None:
        return ids

    keywords = cfg.get("org_idea_keywords") or [
        "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED",
        "RESEARCHED",
    ]
    parts = [f"#+TITLE: wt idea export\n", f"#+TODO: {' '.join(keywords)}\n\n"]
    for task in ordered:
        parts.extend(idea_subtree_lines(task))
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")
        parts.append("\n")

    out = os.path.expanduser(path)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    return ids
