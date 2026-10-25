"""wt sheet plan/record (SPEC-0125): a pure, local, JSON-in/out sync-diff computation over the
idea corpus + a SPEC-0124 `SheetManifest`. Never touches a network — that is the whole point
(SPEC-0123's transport-agnostic invariant): the driving agent fetches/writes the actual sheet
via its own tools; `wt` only computes what to push, flags unknown rows to pull, and records
the outcome back onto ideas as `Ext <project>` bookkeeping (SPEC-0119/0122)."""
from __future__ import annotations

import hashlib
import json

from .render import warn
from . import idea_ext as IX
from . import org_write as OW
from .org import filter_tasks, load_tasks
from .sheet_schemes import SheetColumn, SheetManifest, resolve_sheet_manifest


def _raw_row_value(col: SheetColumn, task, ext: dict) -> str:
    if col.source == "id":
        return ext.get("sheet_row", "")
    if col.source == "title":
        return task.heading
    if col.source == "status":
        return task.state or ""
    return ext.get(col.source, "")


def _apply_value_map(col: SheetColumn, raw: str) -> str:
    """Translate a raw wt value through the column's declared value_map (SPEC-0127), so wt's
    internal vocabulary never has to leak verbatim into an external sheet. An unmapped value
    still renders (as itself) with a stderr warning, rather than blocking the whole push."""
    if not col.value_map:
        return raw
    if raw in col.value_map:
        return col.value_map[raw]
    if raw:
        warn(f"! sheet column {col.header!r}: no value_map entry for {raw!r}; "
              f"using raw value")
    return raw


def _row_value(col: SheetColumn, task, ext: dict) -> str:
    return _apply_value_map(col, _raw_row_value(col, task, ext))


def render_row(manifest: SheetManifest, task, ext: dict) -> dict[str, str]:
    """header -> current value (value_map applied), for every wt-owned column."""
    return {col.header: _row_value(col, task, ext) for col in manifest.wt_owned()}


def content_hash(manifest: SheetManifest, task, ext: dict) -> str:
    """sha256 hex over the wt-owned row, excluding the id column (bookkeeping, not content)."""
    row = render_row(manifest, task, ext)
    row.pop(manifest.id_column, None)
    payload = json.dumps(row, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _synced_ideas(cfg, project: str):
    """Ideas with :PROJECT: == project and Ext <project>.sheet_sync == 'yes' (SPEC-0123
    scope invariant). Yields (task, ext) pairs."""
    tasks = filter_tasks(load_tasks(cfg), project=project, is_idea=True)
    for task in tasks:
        ext = IX.read_idea_extension(cfg, task, project)
        if (ext.get("sheet_sync") or "").strip().lower() == "yes":
            yield task, ext


def plan_push(cfg, project: str) -> list[dict]:
    """Ideas needing push: never pushed (no sheet_row) or changed since the last recorded
    sheet_hash. Skips ideas whose content is unchanged."""
    _, manifest = resolve_sheet_manifest(cfg, project)
    out = []
    for task, ext in _synced_ideas(cfg, project):
        idea_id = task.properties.get("ID") or task.id
        row_id = ext.get("sheet_row") or None
        new_hash = content_hash(manifest, task, ext)
        if not row_id:
            action = "new"
        elif ext.get("sheet_hash") != new_hash:
            action = "update"
        else:
            continue
        out.append({
            "idea": idea_id,
            "row_id": row_id,
            "action": action,
            "row": render_row(manifest, task, ext),
        })
    return out


def plan_pull(cfg, project: str, sheet_rows: list[dict]) -> list[dict]:
    """Sheet rows (already fetched by the caller) whose id-column value matches no known
    idea's Ext sheet_row — candidates to adopt as new ideas."""
    _, manifest = resolve_sheet_manifest(cfg, project)
    known_ids = set()
    for _task, ext in _synced_ideas(cfg, project):
        row_id = ext.get("sheet_row")
        if row_id:
            known_ids.add(row_id)
    out = []
    for row in sheet_rows:
        row_id = (row.get(manifest.id_column) or "").strip()
        if row_id and row_id in known_ids:
            continue
        out.append({"row": row, "reason": "unknown-id"})
    return out


def make_plan(cfg, project: str, sheet_rows: list[dict] | None = None) -> dict:
    _, manifest = resolve_sheet_manifest(cfg, project)
    return {
        "project": project,
        "manifest": manifest.name,
        "push": plan_push(cfg, project),
        "pull": plan_pull(cfg, project, sheet_rows) if sheet_rows is not None else [],
    }


def _record_pushed(cfg, project: str, manifest: SheetManifest, entry: dict) -> str:
    idea_id = entry["idea"]
    row_id = entry["row_id"]
    if not idea_id or not row_id:
        raise ValueError(f"pushed entry requires 'idea' and 'row_id': {entry!r}")
    task = OW.resolve_selector(cfg, idea_id)
    IX.set_idea_extension(cfg, idea_id, "sheet_row", row_id, project=project)
    ext = IX.read_idea_extension(cfg, task, project)
    ext["sheet_row"] = row_id
    new_hash = content_hash(manifest, task, ext)
    IX.set_idea_extension(cfg, idea_id, "sheet_hash", new_hash, project=project)
    return idea_id


def _record_adopted(cfg, project: str, manifest: SheetManifest, entry: dict) -> str:
    row = entry.get("row") or {}
    row_id = (row.get(manifest.id_column) or entry.get("row_id") or "").strip()
    title_col = next((c for c in manifest.wt_owned() if c.source == "title"), None)
    title = row.get(title_col.header) if title_col else None
    title = (title or entry.get("title") or row_id or "untitled").strip()
    if not row_id:
        raise ValueError(f"adopted entry requires a row id: {entry!r}")
    _path, _headline, idea_id = OW.add_idea(cfg, title, project=project)
    IX.set_idea_extension(cfg, idea_id, "sheet_sync", "yes", project=project)
    IX.set_idea_extension(cfg, idea_id, "sheet_row", row_id, project=project)
    task = OW.resolve_selector(cfg, idea_id)
    ext = IX.read_idea_extension(cfg, task, project)
    new_hash = content_hash(manifest, task, ext)
    IX.set_idea_extension(cfg, idea_id, "sheet_hash", new_hash, project=project)
    return idea_id


def record(cfg, project: str, payload: dict) -> dict:
    """Persist a completed sync run's outcome. payload:
      {"pushed": [{"idea": "IDEA-127", "row_id": "EXAMPLE-0009"}, ...],
       "adopted": [{"row": {<header>: <value>, ...}}, ...]}
    Returns {"pushed": [idea_ids...], "adopted": [new_idea_ids...]}."""
    _, manifest = resolve_sheet_manifest(cfg, project)
    pushed_ids = [_record_pushed(cfg, project, manifest, e) for e in payload.get("pushed", [])]
    adopted_ids = [_record_adopted(cfg, project, manifest, e) for e in payload.get("adopted", [])]
    return {"pushed": pushed_ids, "adopted": adopted_ids}
