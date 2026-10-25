"""Project-namespaced idea extensions (SPEC-0119/0121): `** Ext <Project>` drawers."""
from __future__ import annotations

import re

from .explore import _idea_span, _skip_meta
from .extension_schema import validate_extension_map
from .org import filter_tasks, load_tasks
from .org_write import (_HEADLINE_RE, _PLANNING_RE, _atomic_backup_write, _inject_property,
                        org_writer,
                        _remove_property, resolve_selector, stamp_updated)

_EXT_HEADLINE_RE = re.compile(r"^\*+\s+Ext\s+(.+?)\s*$")
_PROP_LINE_RE = re.compile(r"^\s*:([^:]+):\s*(.*)$")


def _require_idea(cfg, selector):
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    return task


def _stars(lines, idx) -> str:
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    return m.group(1) if m else "**"


def _read_drawer(lines, idx) -> dict[str, str]:
    props: dict[str, str] = {}
    nxt = idx + 1
    while nxt < len(lines) and _PLANNING_RE.match(lines[nxt]):
        nxt += 1
    if nxt >= len(lines) or lines[nxt].strip() != ":PROPERTIES:":
        return props
    nxt += 1
    while nxt < len(lines) and lines[nxt].strip() != ":END:":
        m = _PROP_LINE_RE.match(lines[nxt].rstrip("\n"))
        if m:
            props[m.group(1)] = m.group(2)
        nxt += 1
    return props


def _iter_ext_headlines(lines, start, end):
    for j in range(start, end):
        m = _EXT_HEADLINE_RE.match(lines[j].rstrip("\n"))
        if m:
            yield j, m.group(1).strip()


def _idea_content_start(lines, task) -> tuple[int, int, int]:
    head_idx, body_start, body_end = _idea_span(lines, task)
    content_start = _skip_meta(lines, body_start, body_end)
    return head_idx, content_start, body_end


def read_idea_extensions(cfg, task) -> dict[str, dict[str, str]]:
    """All Ext namespaces on an idea: `{project: {KEY: value}}`."""
    lines = open(task.file).read().splitlines(keepends=True)
    _, content_start, body_end = _idea_content_start(lines, task)
    out: dict[str, dict[str, str]] = {}
    for idx, project in _iter_ext_headlines(lines, content_start, body_end):
        drawer = _read_drawer(lines, idx)
        if drawer:
            out[project] = drawer
    return out


def read_idea_extension(cfg, task, project: str) -> dict[str, str]:
    """One project's Ext map (empty dict when absent)."""
    return dict(read_idea_extensions(cfg, task).get(project) or {})


def resolve_ext_project(task, project: str | None) -> str:
    """Public wrapper for CLI/desk: default to :PROJECT:, enforce namespace match."""
    project = (project or task.properties.get("PROJECT") or "").strip()
    if not project:
        raise ValueError("idea has no :PROJECT: — set one first (wt idea project) or pass --project")
    idea_project = (task.properties.get("PROJECT") or "").strip()
    if idea_project and project != idea_project:
        raise ValueError(f"Ext namespace {project!r} does not match idea :PROJECT: {idea_project!r}")
    return project


def _ensure_ext_headline(lines, task, project: str) -> int:
    head_idx, content_start, body_end = _idea_content_start(lines, task)
    for idx, name in _iter_ext_headlines(lines, content_start, body_end):
        if name == project:
            return idx
    block = [f"** Ext {project}\n", "   :PROPERTIES:\n", "   :END:\n"]
    lines[content_start:content_start] = block
    return content_start


def _validate_write(cfg, project: str, mapping: dict[str, str]) -> None:
    problems = validate_extension_map(cfg, project, mapping, updating=True)
    if problems:
        raise ValueError("; ".join(problems))


@org_writer
def rename_idea_ext_namespace(cfg, selector, old: str, new: str, *, task=None) -> bool:
    """Rename `** Ext <old>` → `** Ext <new>` on an idea (SPEC-0159). Values preserved.

    Returns True when a headline was renamed, False when no Ext <old> was present.
    Raises if Ext <new> already exists (would collide).
    Pass `task=` to skip selector resolve (bulk migrate).
    """
    task = task if task is not None else _require_idea(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new:
        raise ValueError("old and new Ext namespace names must be non-empty")
    if old == new:
        return False
    lines = open(task.file).read().splitlines(keepends=True)
    head_idx, content_start, body_end = _idea_content_start(lines, task)
    old_idx = None
    for idx, name in _iter_ext_headlines(lines, content_start, body_end):
        if name == new:
            raise ValueError(f"Ext namespace {new!r} already exists on idea")
        if name == old:
            old_idx = idx
    if old_idx is None:
        return False
    stars = _stars(lines, old_idx)
    nl = "\n" if lines[old_idx].endswith("\n") else ""
    lines[old_idx] = f"{stars} Ext {new}{nl}"
    stamp_updated(cfg, lines, head_idx, _stars(lines, head_idx))
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return True


@org_writer
def set_idea_extension(cfg, selector, key: str, value: str, *, project: str | None = None):
    """Set one Ext key under `** Ext <project>`. Schema-validates when configured."""
    task = _require_idea(cfg, selector)
    project = resolve_ext_project(task, project)
    key = (key or "").strip()
    if not key:
        raise ValueError("extension key must not be empty")
    value = str(value)
    cur = read_idea_extension(cfg, task, project)
    new_map = {**cur, key: value}
    _validate_write(cfg, project, new_map)

    lines = open(task.file).read().splitlines(keepends=True)
    head_idx, _, _ = _idea_content_start(lines, task)
    ext_idx = _ensure_ext_headline(lines, task, project)
    stars = _stars(lines, ext_idx)
    _inject_property(lines, ext_idx, stars, key, value)
    stamp_updated(cfg, lines, head_idx, _stars(lines, head_idx))
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


@org_writer
def clear_idea_extension(cfg, selector, key: str, *, project: str | None = None):
    """Remove one Ext key. Rejects clearing a required key when schema exists."""
    task = _require_idea(cfg, selector)
    project = resolve_ext_project(task, project)
    key = (key or "").strip()
    if not key:
        raise ValueError("extension key must not be empty")
    cur = read_idea_extension(cfg, task, project)
    if key not in cur:
        raise ValueError(f"no Ext key {key!r} under {project!r}")
    new_map = {k: v for k, v in cur.items() if k != key}
    _validate_write(cfg, project, new_map)

    lines = open(task.file).read().splitlines(keepends=True)
    head_idx, content_start, body_end = _idea_content_start(lines, task)
    ext_idx = None
    for idx, name in _iter_ext_headlines(lines, content_start, body_end):
        if name == project:
            ext_idx = idx
            break
    if ext_idx is None:
        raise ValueError(f"no Ext {project!r} headline")
    stars = _stars(lines, ext_idx)
    if not _remove_property(lines, ext_idx, key):
        raise ValueError(f"no Ext key {key!r} under {project!r}")
    stamp_updated(cfg, lines, head_idx, _stars(lines, head_idx))
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


def lint_extension_entry(cfg, task, project: str, mapping: dict[str, str]) -> list[str]:
    """Lint one Ext namespace (schema + :PROJECT: alignment)."""
    problems: list[str] = []
    idea_project = (task.properties.get("PROJECT") or "").strip()
    if mapping and not idea_project:
        problems.append(f"Ext {project!r} set but idea has no :PROJECT:")
    if idea_project and project != idea_project:
        problems.append(f"Ext namespace {project!r} does not match :PROJECT: {idea_project!r}")
    problems.extend(validate_extension_map(cfg, project, mapping))
    return problems


def lint_idea_extensions(cfg, task) -> list[str]:
    """All lint findings for one idea (empty when clean)."""
    idea_id = task.properties.get("ID") or task.id
    problems: list[str] = []
    for project, mapping in read_idea_extensions(cfg, task).items():
        for msg in lint_extension_entry(cfg, task, project, mapping):
            problems.append(f"{idea_id}: {msg}")
    return problems


def lint_all_idea_extensions(cfg) -> list[str]:
    """Lint every idea in the corpus."""
    problems: list[str] = []
    for task in filter_tasks(load_tasks(cfg), is_idea=True):
        problems.extend(lint_idea_extensions(cfg, task))
    return problems
