"""Outbox project bucket rename / migrate (SPEC-0159). Dry-run plan; apply with --yes."""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

import yaml

from .idea_ext import read_idea_extensions, rename_idea_ext_namespace
from .org import filter_tasks, invalidate_tasks_cache, load_tasks
from .org_write import resolve_selector, set_property
from .specs import _proj_code, outbox_dir, write_outbox_index

_UNSAFE_NAME_RE = re.compile(r"[/\\]|^\.\.?$")
_TARGET_PROJECT_RE = re.compile(r"^target_project:\s*.*$", re.M)
_FM_RE = re.compile(r"^(---\n)(.*?)(\n---\n)(.*)$", re.S)


def _safe_segment(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("project name must be non-empty")
    if _UNSAFE_NAME_RE.search(name) or "/" in name or "\\" in name:
        raise ValueError(f"unsafe project name {name!r}: must be a single path segment")
    return name


def _ideas_for_project(cfg, project: str):
    return [
        t for t in filter_tasks(load_tasks(cfg), is_idea=True)
        if (t.properties.get("PROJECT") or "").strip() == project
    ]


def _idea_key(task) -> str:
    return (task.properties.get("ID") or task.id or "").strip()


def _find_idea(cfg, iid: str):
    """Locate an idea by exact stored :ID:/task.id (avoids IDEA-N ↔ IDEA-00N pad mismatch)."""
    for task in filter_tasks(load_tasks(cfg), is_idea=True):
        if _idea_key(task) == iid or task.id == iid:
            return task
    return resolve_selector(cfg, iid)


def _ext_hit_count(cfg, ideas, project: str) -> int:
    return sum(1 for task in ideas if project in read_idea_extensions(cfg, task))


def _sample_target_spec_paths(out_dir: Path, limit: int = 3) -> list[str]:
    samples = []
    if not out_dir.is_dir():
        return samples
    for path in sorted(out_dir.glob("*.md")):
        if path.name.upper() == "INDEX.MD":
            continue
        text = path.read_text(encoding="utf-8")
        m = _FM_RE.match(text)
        if not m:
            continue
        fm = yaml.safe_load(m.group(2)) or {}
        tsp = (fm.get("target_spec_path") or "").strip()
        if tsp:
            samples.append(tsp)
            if len(samples) >= limit:
                break
    return samples


def _rewrite_outbox_target_project(path: Path, new_project: str) -> bool:
    text = path.read_text(encoding="utf-8")
    m = _FM_RE.match(text)
    if not m:
        return False
    fm_body = m.group(2)
    if _TARGET_PROJECT_RE.search(fm_body):
        fm2 = _TARGET_PROJECT_RE.sub(f"target_project: {new_project}", fm_body, count=1)
    else:
        fm2 = fm_body.rstrip() + f"\ntarget_project: {new_project}"
    if fm2 == fm_body:
        return False
    path.write_text(m.group(1) + fm2 + m.group(3) + m.group(4), encoding="utf-8")
    return True


def plan_project_rename(cfg, old: str, new: str) -> dict:
    """Plan renaming outbox_targets[old] → [new] (SPEC-0159). Does not write."""
    old = _safe_segment(old)
    new = _safe_segment(new)
    if old == new:
        raise ValueError(f"old and new project names are the same: {old!r}")

    config_path = os.path.join(cfg["config_dir"], "config.yaml")
    targets = cfg.get("outbox_targets") or {}
    if old not in targets:
        raise ValueError(
            f"outbox_targets has no key {old!r}; "
            f"register with `nw projects add` or pick an existing name")
    if new in targets:
        raise ValueError(f"conflict: outbox_targets already has key {new!r}")

    root = outbox_dir(cfg)
    old_dir = root / old
    new_dir = root / new
    if new_dir.exists():
        raise ValueError(f"conflict: outbox directory already exists: {new_dir}")

    ideas = _ideas_for_project(cfg, old)
    idea_ids = [_idea_key(t) for t in ideas if _idea_key(t)]
    ext_hits = _ext_hit_count(cfg, ideas, old)
    extensions = cfg.get("idea_extensions") or {}
    ext_cfg = old in extensions

    warnings: list[str] = []
    entry = targets.get(old) or {}
    if isinstance(entry, dict) and entry.get("sheet"):
        warnings.append(
            f"outbox_targets[{old!r}] has a sheet target — rows are not remapped in v1")
    old_code, new_code = _proj_code(old), _proj_code(new)
    if old_code != new_code:
        warnings.append(
            f"mint prefix will change after rename: {_proj_code(old)} → {_proj_code(new)}; "
            f"existing outbound ids stay as-is (no rekey in v1)")
    for tsp in _sample_target_spec_paths(old_dir):
        warnings.append(f"stale target_spec_path left unchanged (v1): {tsp}")

    return {
        "action": "rename",
        "old": old,
        "new": new,
        "before": dict(entry) if isinstance(entry, dict) else entry,
        "config_path": config_path,
        "outbox_old": str(old_dir),
        "outbox_new": str(new_dir),
        "outbox_exists": old_dir.is_dir(),
        "idea_ids": idea_ids,
        "idea_count": len(idea_ids),
        "ext_headline_hits": ext_hits,
        "idea_extensions_key": ext_cfg,
        "warnings": warnings,
    }


def apply_project_rename(cfg, plan: dict) -> dict:
    """Apply a plan_project_rename result (SPEC-0159)."""
    if plan.get("action") != "rename":
        raise ValueError(f"unexpected plan action {plan.get('action')!r}")
    old, new = plan["old"], plan["new"]
    # Re-validate against live cfg so we never partially write on a stale plan.
    live = plan_project_rename(cfg, old, new)
    if live["idea_ids"] != plan["idea_ids"]:
        plan = {**plan, **{k: live[k] for k in (
            "idea_ids", "idea_count", "ext_headline_hits", "idea_extensions_key",
            "warnings", "outbox_exists", "before")}}

    config_path = Path(plan["config_path"])
    config_path.parent.mkdir(parents=True, exist_ok=True)
    user: dict = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            user = loaded
        bak = config_path.with_name(
            f"config.yaml.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
        bak.write_bytes(config_path.read_bytes())
        plan["config_backup"] = str(bak)

    targets = dict(user.get("outbox_targets") or {})
    if old not in targets:
        raise ValueError(f"on-disk config lost outbox_targets[{old!r}] before apply")
    if new in targets:
        raise ValueError(f"on-disk config already has outbox_targets[{new!r}]")
    targets[new] = targets.pop(old)
    user["outbox_targets"] = targets

    if plan.get("idea_extensions_key"):
        exts = dict(user.get("idea_extensions") or {})
        if old in exts and new not in exts:
            exts[new] = exts.pop(old)
            user["idea_extensions"] = exts

    text = yaml.safe_dump(user, sort_keys=False, default_flow_style=False)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, config_path)

    cfg_targets = dict(cfg.get("outbox_targets") or {})
    if old in cfg_targets:
        cfg_targets[new] = cfg_targets.pop(old)
    cfg["outbox_targets"] = cfg_targets
    if plan.get("idea_extensions_key"):
        cfg_exts = dict(cfg.get("idea_extensions") or {})
        if old in cfg_exts and new not in cfg_exts:
            cfg_exts[new] = cfg_exts.pop(old)
        cfg["idea_extensions"] = cfg_exts

    old_dir = Path(plan["outbox_old"])
    new_dir = Path(plan["outbox_new"])
    outbox_renamed = False
    if old_dir.is_dir():
        if new_dir.exists():
            raise ValueError(f"conflict during apply: {new_dir} appeared")
        old_dir.rename(new_dir)
        outbox_renamed = True

    fm_updated = 0
    if new_dir.is_dir():
        for path in sorted(new_dir.glob("*.md")):
            if path.name.upper() == "INDEX.MD":
                continue
            if _rewrite_outbox_target_project(path, new):
                fm_updated += 1

    ideas_updated = 0
    ext_renamed = 0
    for iid in list(plan["idea_ids"]):
        invalidate_tasks_cache()
        task = _find_idea(cfg, iid)
        if rename_idea_ext_namespace(cfg, iid, old, new, task=task):
            ext_renamed += 1
            invalidate_tasks_cache()
            task = _find_idea(cfg, iid)
        set_property(cfg, task, "PROJECT", new)
        ideas_updated += 1

    write_outbox_index(cfg)

    summary = {
        **plan,
        "written": True,
        "outbox_renamed": outbox_renamed,
        "ideas_updated": ideas_updated,
        "ext_renamed": ext_renamed,
        "fm_target_project_updated": fm_updated,
        "index_refreshed": True,
    }
    return summary
