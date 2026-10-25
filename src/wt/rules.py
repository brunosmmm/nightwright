"""The human-owned rules layer: mappings.yaml (base topic -> facets) and overrides.yaml
(session + time range -> topic). Read at report time; the parser never writes these."""
import os
from pathlib import Path

import yaml

from .render import warn
from .config import _load_yaml
from .dates import _parse_iso


def _mappings_path(cfg):
    return os.path.join(cfg["config_dir"], "mappings.yaml")


def _overrides_path(cfg):
    return os.path.join(cfg["config_dir"], "overrides.yaml")


def load_mappings(cfg):
    """base topic -> {axis: value} facets. A bare string is shorthand for {bucket: str}."""
    raw = _load_yaml(_mappings_path(cfg), {}) or {}
    return {k: ({"bucket": v} if isinstance(v, str) else dict(v or {})) for k, v in raw.items()}


def resolve_facets(cfg, topic, mappings=None) -> dict:
    """Effective facets for a base topic (SPEC-0027).

    Exact mapping wins; otherwise inherit from the prefix before the first `:`, if that
    parent key is mapped. Returns a shallow copy (never the live mappings entry).
    """
    m = mappings if mappings is not None else load_mappings(cfg)
    if topic in m:
        return dict(m[topic])
    if ":" in topic:
        parent = topic.split(":", 1)[0]
        if parent and parent != topic and parent in m:
            return dict(m[parent])
    return {}


def known_projects(cfg):
    """Sorted distinct values of cfg['project_axis'] (default 'bucket') across
    load_mappings(cfg) — the de-facto project registry (SPEC-0018)."""
    axis = cfg.get("project_axis", "bucket")
    values = {facets[axis] for facets in load_mappings(cfg).values() if axis in facets}
    return sorted(values)


def _outbound_flag(entry) -> bool:
    """True when outbox entry has a non-empty repo_path (SPEC-0137)."""
    if not isinstance(entry, dict):
        return False
    return bool((entry.get("repo_path") or "").strip())


def projects_payload(cfg) -> dict:
    """Agent-facing project map for `wt projects --json` (SPEC-0095 / SPEC-0137).

    Union of mapping buckets, `outbox_targets` keys, and Meta-Tools — each with topic count,
    outbound flag (non-empty repo_path), and `resolve_research_context` (never invents paths).
    """
    axis = cfg.get("project_axis", "bucket")
    counts = {}
    for facets in load_mappings(cfg).values():
        val = facets.get(axis)
        if val:
            counts[val] = counts.get(val, 0) + 1
    outbox = cfg.get("outbox_targets") or {}
    names = set(known_projects(cfg)) | set(outbox.keys()) | {"Meta-Tools"}
    projects = []
    for name in sorted(names):
        entry = outbox.get(name) or {}
        projects.append({
            "name": name,
            "topics": counts.get(name, 0),
            "outbound": _outbound_flag(entry),
            "research": resolve_research_context(cfg, name),
        })
    return {"schema": "wt.projects.v1", "projects": projects}


def plan_project_add(cfg, name, *, repo=None, scheme=None, spec_dir=None, force=False):
    """Plan registering/updating outbox_targets[name] (SPEC-0137). Does not write.

    Returns a plan dict: action (create|update|noop), name, before, after, config_path.
    Raises ValueError on empty name, missing path (unless force), or repo_path conflict.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("project name must be non-empty")
    config_path = os.path.join(cfg["config_dir"], "config.yaml")
    outbox = cfg.get("outbox_targets") or {}
    before = dict(outbox[name]) if name in outbox and isinstance(outbox[name], dict) else (
        {} if name in outbox else None)
    after = dict(before or {})

    if repo is not None:
        raw = str(repo).strip()
        expanded = os.path.expanduser(raw)
        if not force:
            if not os.path.exists(expanded):
                raise ValueError(
                    f"repo path does not exist: {expanded} (pass --force to record anyway)")
            if not os.path.isdir(expanded):
                raise ValueError(
                    f"repo path is not a directory: {expanded} (pass --force to record anyway)")
        existing = (after.get("repo_path") or "").strip()
        if existing:
            existing_exp = os.path.expanduser(existing)
            if os.path.normpath(existing_exp) != os.path.normpath(expanded) and not force:
                raise ValueError(
                    f"conflict: outbox_targets[{name!r}].repo_path is {existing!r}; "
                    f"refusing to replace with {expanded!r} (pass --force)")
        after["repo_path"] = expanded

    if scheme is not None:
        after["scheme"] = scheme
    if spec_dir is not None:
        after["spec_dir"] = spec_dir

    if before is None:
        action = "create"
    elif after == before:
        action = "noop"
    else:
        action = "update"

    return {
        "action": action,
        "name": name,
        "before": before,
        "after": after,
        "config_path": config_path,
    }


def apply_project_add(cfg, plan):
    """Commit a plan_project_add result to the user config.yaml (SPEC-0137).

    Loads the on-disk user file only (not merged defaults), merges outbox_targets[name],
    writes a timestamped backup, then atomically replaces config.yaml.
    """
    if plan.get("action") == "noop":
        return
    config_path = plan["config_path"]
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    user = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            user = loaded
        # backup before mutate
        from datetime import datetime
        bak = path.with_name(f"config.yaml.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
        bak.write_bytes(path.read_bytes())
    targets = dict(user.get("outbox_targets") or {})
    targets[plan["name"]] = dict(plan["after"])
    user["outbox_targets"] = targets
    text = yaml.safe_dump(user, sort_keys=False, default_flow_style=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    # keep in-memory cfg in sync for callers that re-use it
    cfg_targets = dict(cfg.get("outbox_targets") or {})
    cfg_targets[plan["name"]] = dict(plan["after"])
    cfg["outbox_targets"] = cfg_targets


def discover_wt_checkout():
    """Filesystem root of this wt tooling repo (SPEC-0042 Meta-Tools research root).

    Walks from the installed `wt` package upward looking for `src/wt` + `pyproject.toml`.
    Returns an absolute path string, or None if not found (e.g. bare wheel without repo).
    """
    import wt as _wt
    here = Path(_wt.__file__).resolve().parent
    for cand in [here, *here.parents]:
        if _is_wt_checkout(cand):
            return str(cand)
    return None


# SPEC-0166: shared fix hint for every caller that needs the checkout and cannot find it.
CHECKOUT_HINT = ("reinstall from your wt checkout with `uv tool install --editable .` "
                 "or set `wt_checkout: /path/to/wt` in config.yaml")


def _is_wt_checkout(path):
    return (path / "src" / "wt").is_dir() and (path / "pyproject.toml").is_file()


def configured_wt_checkout(cfg):
    """The `wt_checkout` config override as a Path, or None when unset. Raises ValueError if
    it is set but does not look like a wt checkout — a typo must not silently fall back."""
    raw = str((cfg or {}).get("wt_checkout") or "").strip()
    if not raw:
        return None
    path = Path(os.path.expanduser(raw))
    if not _is_wt_checkout(path):
        raise ValueError(f"wt_checkout {raw!r} is not a wt checkout (needs src/wt and "
                         "pyproject.toml)")
    return path


def wt_checkout(cfg=None):
    """wt's own checkout root (SPEC-0166): config `wt_checkout` > module-relative walk
    (`discover_wt_checkout`, works for `uv run` / editable installs) > None. Never cwd-relative
    (SPEC-0079)."""
    configured = configured_wt_checkout(cfg)
    if configured:
        return configured
    found = discover_wt_checkout()
    return Path(found) if found else None


def resolve_research_context(cfg, project=None):
    """Where agents should search code for an idea's :PROJECT: (SPEC-0042).

    Returns a dict: root (expanded path or None), source
    ('outbox_targets' | 'internal' | 'none'), optional spec_dir/scheme, optional note.
    Never invents paths from mappings.yaml or ~/work/<slug>.
    """
    project = (project or "").strip() or None
    outbox = cfg.get("outbox_targets") or {}

    if project and project in outbox:
        target = outbox.get(project) or {}
        raw = (target.get("repo_path") or "").strip()
        root = os.path.expanduser(raw) if raw else None
        ctx = {"root": root, "source": "outbox_targets"}
        if target.get("spec_dir"):
            ctx["spec_dir"] = target["spec_dir"]
        if target.get("scheme"):
            ctx["scheme"] = target["scheme"]
        if not root:
            ctx["note"] = f"outbox_targets[{project!r}] has empty repo_path"
        return ctx

    if project == "Meta-Tools":
        root = wt_checkout(cfg)
        if root:
            return {"root": str(root), "source": "internal"}
        return {
            "root": None,
            "source": "none",
            "note": f"could not discover wt checkout for internal Meta-Tools; {CHECKOUT_HINT}",
        }

    if project:
        return {
            "root": None,
            "source": "none",
            "note": (
                f"project {project!r} has no outbox_targets.repo_path; "
                f"run `wt projects add {project} --repo <path> --yes` "
                "(or explore without a tree)"
            ),
        }

    return {
        "root": None,
        "source": "none",
        "note": "no :PROJECT:; explore from conversation only",
    }


def load_overrides(cfg):
    raw = _load_yaml(_overrides_path(cfg), []) or []
    out = []
    for ov in raw:
        try:
            out.append({
                "session": ov["session"],
                "_start": _parse_iso(ov["start"]),
                "_end": _parse_iso(ov["end"]),
                "topic": ov["topic"],
                "note": ov.get("note", ""),
            })
        except Exception as e:
            warn(f"  ! skipping bad override {ov!r}: {e}")
    return out


def set_facets(cfg, raw, facets):
    """Merge {axis: value} facets into a base topic's mapping entry."""
    path = _mappings_path(cfg)
    raw_map = _load_yaml(path, {}) or {}
    cur = raw_map.get(raw)
    cur = {"bucket": cur} if isinstance(cur, str) else dict(cur or {})
    cur.update(facets)
    raw_map[raw] = cur
    with open(path, "w") as f:
        yaml.safe_dump(raw_map, f, sort_keys=True, default_flow_style=False)


def add_override_rec(cfg, session, start, end, topic, note=""):
    path = _overrides_path(cfg)
    lst = _load_yaml(path, []) or []
    lst.append({"session": session, "start": start, "end": end, "topic": topic, "note": note or ""})
    with open(path, "w") as f:
        yaml.safe_dump(lst, f, sort_keys=False, default_flow_style=False)
