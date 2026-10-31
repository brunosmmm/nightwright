"""Spec ids are per project (SPEC-0327): a bare `SPEC-NNNN` of another project's repo must never
resolve against wt's own `docs/specs`. Small helpers shared by reconcile, the queue and the spec CLI.
"""
from __future__ import annotations

import os
from pathlib import Path

from .rules import INTERNAL_PROJECT

SPEC_DIR_REL = "docs/specs"      # the fixed per-repo convention (spec_review.py, campaign spec paths)


def idea_project(task) -> str:
    """The idea's `:PROJECT:`, or `INTERNAL_PROJECT` when empty."""
    return ((task.properties.get("PROJECT") or "").strip()) or INTERNAL_PROJECT


def is_internal_idea(task) -> bool:
    return idea_project(task) == INTERNAL_PROJECT


def owns_idea(cfg, task, spec_id=None) -> bool:
    """Does the idea belong to wt's store (matches its bare `SPEC-NNNN` ids)? True when the idea
    is internal, or when wt's spec at `spec_id` (default: the idea's own `:SPEC:`) already names
    it as `source_idea`: an explicit claim, e.g. an idea promoted `--internal` from another
    project's `:PROJECT:`. Anything else is foreign (SPEC-0327 rule 1)."""
    if is_internal_idea(task):
        return True
    from .workflow import _is_outbound_id, _load_spec_fm
    sid = (spec_id or task.properties.get("SPEC") or "").strip()
    idea_id = (task.properties.get("ID") or "").strip()
    if not sid or not idea_id or _is_outbound_id(sid):
        return False
    try:
        fm = _load_spec_fm(cfg, sid)
    except (ValueError, KeyError, OSError):
        return False
    return str((fm or {}).get("source_idea") or "").strip() == idea_id


def is_bare_spec_id(spec_id) -> bool:
    return str(spec_id or "").strip().upper().startswith("SPEC-")


def campaign_spec_path(cfg, state) -> Path:
    """The campaign's own spec file: the worktree copy when the worktree exists, else the repo
    root's. Raises ValueError when neither exists."""
    repo, spec = state.get("repo") or {}, state.get("spec") or {}
    rel = spec.get("rel_path")
    if not rel:
        raise ValueError(f"campaign {state.get('id')} has no spec rel_path")
    for base in (repo.get("worktree"), repo.get("root")):
        if base and (Path(base) / rel).is_file():
            return Path(base) / rel
    raise ValueError(f"campaign {state.get('id')}: spec file {rel} not found in its worktree "
                     f"or repo")


def _refusal(spec_id, project, why="it would resolve against wt's own specs; use --path"):
    return ValueError(f"{spec_id} is ambiguous in a {project} campaign: {why}")


def project_checkout_for_cwd(cfg, cwd):
    """`(project, toplevel)` when `cwd` is in a git checkout (main or linked worktree) of a
    registered non-internal project's `research.root`, else None. Matched on `--git-common-dir`
    (`specs._git_checkout`), not on the toplevel, so campaign worktrees count."""
    from . import specs as SP
    from .agentic import project as P
    from .rules import registered_projects

    cwd = Path(cwd)
    if not cwd.is_dir():
        return None
    cur = SP._git_checkout(cwd)
    if cur is None:
        return None
    try:
        reg_dir = SP._specs_dir(cfg)
        wt = SP._git_checkout(reg_dir) if Path(reg_dir).is_dir() else None
        if wt and wt[1] == cur[1]:              # wt's own repository: the store applies
            return None
        names = registered_projects(cfg)
    except (ValueError, KeyError, OSError):    # no specs dir / registry configured
        return None
    for name in names:
        if name == INTERNAL_PROJECT:
            continue
        try:
            root = P.project_root(cfg, name)
        except ValueError:
            continue
        reg = SP._git_checkout(root) if Path(root).is_dir() else None
        if reg and reg[1] == cur[1]:
            return name, cur[0]
    return None


def foreign_scope_path(cfg, spec_id, *, campaign=None, cwd=None):
    """`(path, origin)` when a bare `spec_id` is scoped to a non-wt project (a non-wt campaign in
    `$NW_CAMPAIGN`, or `cwd` in a checkout of a non-wt project), `None` when wt's own store
    applies. Raises ValueError when the scope cannot supply the id; never reads wt's store."""
    from . import specs as SP
    from .agentic import store

    if not is_bare_spec_id(spec_id):
        return None
    spec_id = spec_id.strip()
    cid = campaign if campaign is not None else os.environ.get("NW_CAMPAIGN", "")
    if cid:
        try:
            state = store.read_state(cfg, cid)
        except (ValueError, KeyError, OSError):    # unknown campaign / no data dir configured
            state = None
        project = (state or {}).get("project") or INTERNAL_PROJECT
        if state and project != INTERNAL_PROJECT:
            if str(state.get("spec_id") or "").upper() == spec_id.upper():
                return campaign_spec_path(cfg, state), f"campaign {cid}"
            repo = state.get("repo") or {}
            for base in (repo.get("worktree"), repo.get("root")):
                if base and Path(base).is_dir():
                    try:
                        return SP._find_spec(Path(base) / SPEC_DIR_REL, spec_id), f"campaign {cid}"
                    except ValueError:
                        continue
            raise _refusal(spec_id, project)
    found = project_checkout_for_cwd(cfg, cwd if cwd is not None else Path.cwd())
    if found:
        project, top = found
        try:
            return SP._find_spec(top / SPEC_DIR_REL, spec_id), f"{project} checkout"
        except ValueError:
            raise _refusal(spec_id, project, f"no such spec in {top / SPEC_DIR_REL}; wt's own "
                           "specs are not consulted from a project checkout")
    return None


def resolve_in_scope(cfg, spec_id, *, campaign=None, cwd=None) -> Path:
    """Path of `spec_id` for a command that takes a bare id: scoped to the campaign / project
    checkout when there is one (`foreign_scope_path`), else today's `resolve_spec_path`."""
    from . import specs as SP

    if not spec_id:
        raise ValueError("spec id is required")
    hit = foreign_scope_path(cfg, spec_id, campaign=campaign, cwd=cwd)
    return hit[0] if hit else SP.resolve_spec_path(cfg, spec_id)
