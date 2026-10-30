"""Per-project configuration `<repo>/.wt/project.yaml` (SPEC-0170).

Gates, merge policy and worktree setup are facts about a project's code, so they are
committed with it. wt finds the file through the project registry
(`rules.resolve_research_context`: outbox `repo_path` or the wt checkout for Meta-Tools) —
never from the current directory alone (SPEC-0079).

Validation is hand-rolled (jsonschema is dev-only), strict like `spec.schema.json`
(`additionalProperties: false`): unknown keys fail, every problem is reported by key path in
one ValueError. Defaults are filled per section after validation."""
import copy
import os
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

SCHEMA = "wt.project.v1"
CONFIG_RELPATH = Path(".wt") / "project.yaml"

MERGE_POLICIES = ("merge", "pr", "draft-pr")
CLEANUP_MODES = ("on_merge", "on_close", "never")
PERMISSION_MODES = ("bypassPermissions", "dontAsk", "acceptEdits")
PLACEHOLDERS = ("spec_id", "campaign", "worktree", "base")

PROJECT_DEFAULTS = {
    "schema": SCHEMA,
    "project": None,
    "base": "main",
    "gates": [],
    "merge": {
        "policy": "merge",
        "base": None,            # None ⇒ top-level base
        "remote": "origin",
        "push_after_merge": False,
        "delete_branch": True,
        "post_cmds": [],
        "regenerate": [],        # [{path, cmd}]: generated files resolved by regeneration at rebase
        "docs_only_paths": [],   # SPEC-0234: paths no gate reads; [] ⇒ `wt queue merge --docs-only` off
        "resolver": True,        # SPEC-0221: a conflict_resolver persona run before parking rebase_conflict
        "pr": {"labels": [], "reviewers": [], "title_prefix": ""},
    },
    "worktrees": {
        "root": None,            # None ⇒ <repo>/../.wt-worktrees/<project>
        "setup": [],
        "cleanup": "on_merge",
    },
    "agents": {
        "context_files": [],
        "permission_mode": "bypassPermissions",
        "allowed_tools": [],
        "commit_trailer": "Co-Authored-By: Claude <noreply@anthropic.com>",
    },
}

GATE_DEFAULTS = {"cwd": ".", "timeout_s": 1200, "required": True, "retries": 0}
ON_FAIL_MODES = ("block", "warn")

# What `wt projects init` writes: the defaults made explicit, with one example gate.
STARTER = """\
# wt per-project configuration (SPEC-0170). Committed with the project.
schema: wt.project.v1
project: {project}
base: main
gates:                         # run in order, all of them, inside the campaign worktree
  - name: tests
    cmd: echo "replace with your test command"
    cwd: .
    timeout_s: 1200
    required: true
merge:
  policy: pr                   # merge | pr | draft-pr
  base: main
  remote: origin
  push_after_merge: false
  delete_branch: true
  post_cmds: []
  docs_only_paths: []          # paths no gate reads; enables wt queue merge --docs-only
  pr:
    labels: []
    reviewers: []
    title_prefix: ""
worktrees:
  root: null                   # null ⇒ <repo>/../.wt-worktrees/<project>; else absolute or repo-relative
  setup: []
  cleanup: on_merge            # on_merge | on_close | never
agents:
  context_files: []
  permission_mode: bypassPermissions   # bypassPermissions | dontAsk | acceptEdits
  allowed_tools: []
  commit_trailer: "Co-Authored-By: Claude <noreply@anthropic.com>"
"""


# ---- validation ------------------------------------------------------------------------

def _is_str(v):
    return isinstance(v, str) and v.strip() != ""


def _check_mapping(problems, value, path, allowed):
    if not isinstance(value, dict):
        problems.append(f"{path}: must be a mapping")
        return False
    for key in value:
        if key not in allowed:
            problems.append(f"{path}.{key}: unknown key (allowed: {', '.join(allowed)})")
    return True


def _check_results(problems, value, path):
    """SPEC-0284: a gate's `results:` is `{format: pytest}` or `{format: junit, path: <rel>}`."""
    if not _check_mapping(problems, value, path, ("format", "path")):
        return
    fmt = value.get("format")
    if fmt not in ("pytest", "junit"):
        problems.append(f"{path}.format: must be one of pytest, junit (got {fmt!r})")
        return
    rel = value.get("path")
    if fmt == "pytest":
        if rel is not None:
            problems.append(f"{path}.path: only valid with format: junit")
    elif not _is_str(rel):
        problems.append(f"{path}.path: required non-empty relative path for format: junit")
    elif rel.startswith(("/", "\\")) or PureWindowsPath(rel).is_absolute() \
            or ".." in PurePosixPath(rel.replace("\\", "/")).parts:
        problems.append(f"{path}.path: must be relative and must not contain '..' (got {rel!r})")


def _check_str_list(problems, value, path):
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        problems.append(f"{path}: must be a list of strings")


def _check_regenerate(problems, value):
    if value is None:
        return
    if not isinstance(value, list):
        problems.append("merge.regenerate: must be a list of {path, cmd} mappings")
        return
    for i, item in enumerate(value):
        where = f"merge.regenerate[{i}]"
        if not _check_mapping(problems, item, where, ("path", "cmd")):
            continue
        for key in ("path", "cmd"):
            if not _is_str(item.get(key)):
                problems.append(f"{where}.{key}: must be a non-empty string")


def _check_docs_only_paths(problems, value):
    """SPEC-0234: exact repo-relative paths or `/`-terminated directory prefixes, no globs."""
    if value is None:
        return
    if not isinstance(value, list):
        problems.append("merge.docs_only_paths: must be a list of repo-relative paths")
        return
    for i, item in enumerate(value):
        where = f"merge.docs_only_paths[{i}]"
        if not isinstance(item, str):
            problems.append(f"{where}: must be a string")
        elif not item.strip():
            problems.append(f"{where}: must not be empty")
        elif item.startswith("/"):
            problems.append(f"{where}: must be repo-relative, not absolute")
        elif ".." in item:
            problems.append(f"{where}: must not contain '..'")
        elif any(ch in item for ch in "*?["):
            problems.append(f"{where}: globs are not supported (exact path or dir/ prefix)")


def _check_bool(problems, value, path):
    if value is not None and not isinstance(value, bool):
        problems.append(f"{path}: must be true or false")


def _check_enum(problems, value, path, allowed):
    if value is not None and value not in allowed:
        problems.append(f"{path}: must be one of {', '.join(allowed)} (got {value!r})")


def _check_gate_retry(problems, gate, p):
    """SPEC-0289: `retries` (0-3), `on_fail` (block|warn) and `retry_cmd` (pytest only)."""
    r = gate.get("retries")
    if r is not None and (isinstance(r, bool) or not isinstance(r, int) or not 0 <= r <= 3):
        problems.append(f"{p}.retries: must be an integer from 0 to 3 (got {r!r})")
    on_fail = gate.get("on_fail")
    _check_enum(problems, on_fail, f"{p}.on_fail", ON_FAIL_MODES)
    required = gate.get("required")
    if on_fail == "warn" and required is True:
        problems.append(f"{p}.on_fail: warn contradicts required: true")
    if on_fail == "block" and required is False:
        problems.append(f"{p}.on_fail: block contradicts required: false")
    rc = gate.get("retry_cmd")
    if rc is not None:
        if not _is_str(rc) or "{failing}" not in rc:
            problems.append(f"{p}.retry_cmd: must be a string containing {{failing}}")
        res = gate.get("results")
        if not (isinstance(res, dict) and res.get("format") == "pytest"):
            problems.append(f"{p}.retry_cmd: only valid with results: {{format: pytest}}")


def validate(doc):
    """Collect every problem in `doc` and raise one ValueError naming them by key path."""
    problems = []
    if not isinstance(doc, dict):
        raise ValueError("project config: top level must be a mapping")
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema: must be {SCHEMA!r} (got {doc.get('schema')!r})")
    _check_mapping(problems, doc, "project config", tuple(PROJECT_DEFAULTS))

    if "project" in doc and doc["project"] is not None and not _is_str(doc["project"]):
        problems.append("project: must be a non-empty string")
    if "base" in doc and not _is_str(doc["base"]):
        problems.append("base: must be a non-empty string")

    gates = doc.get("gates", [])
    if not isinstance(gates, list):
        problems.append("gates: must be a list")
        gates = []
    seen = set()
    for i, gate in enumerate(gates):
        p = f"gates[{i}]"
        if not _check_mapping(problems, gate, p, ("name", "cmd", "cwd", "timeout_s", "required",
                                                  "results", "retries", "on_fail", "retry_cmd")):
            continue
        name = gate.get("name")
        if not _is_str(name):
            problems.append(f"{p}.name: required non-empty string")
        elif name in seen:
            problems.append(f"{p}.name: duplicate gate name {name!r}")
        else:
            seen.add(name)
        if not _is_str(gate.get("cmd")):
            problems.append(f"{p}.cmd: required non-empty string")
        if "cwd" in gate and not _is_str(gate["cwd"]):
            problems.append(f"{p}.cwd: must be a non-empty string")
        t = gate.get("timeout_s")
        if t is not None and (isinstance(t, bool) or not isinstance(t, int) or t <= 0):
            problems.append(f"{p}.timeout_s: must be a positive integer")
        _check_bool(problems, gate.get("required"), f"{p}.required")
        if gate.get("results") is not None:
            _check_results(problems, gate["results"], f"{p}.results")
        _check_gate_retry(problems, gate, p)

    merge = doc.get("merge")
    if merge is not None and _check_mapping(problems, merge, "merge",
                                            tuple(PROJECT_DEFAULTS["merge"])):
        _check_enum(problems, merge.get("policy"), "merge.policy", MERGE_POLICIES)
        if "base" in merge and merge["base"] is not None and not _is_str(merge["base"]):
            problems.append("merge.base: must be a non-empty string")
        if "remote" in merge and not _is_str(merge["remote"]):
            problems.append("merge.remote: must be a non-empty string")
        _check_bool(problems, merge.get("push_after_merge"), "merge.push_after_merge")
        _check_bool(problems, merge.get("delete_branch"), "merge.delete_branch")
        _check_bool(problems, merge.get("resolver"), "merge.resolver")
        _check_str_list(problems, merge.get("post_cmds"), "merge.post_cmds")
        _check_docs_only_paths(problems, merge.get("docs_only_paths"))
        _check_regenerate(problems, merge.get("regenerate"))
        pr = merge.get("pr")
        if pr is not None and _check_mapping(problems, pr, "merge.pr",
                                             tuple(PROJECT_DEFAULTS["merge"]["pr"])):
            _check_str_list(problems, pr.get("labels"), "merge.pr.labels")
            _check_str_list(problems, pr.get("reviewers"), "merge.pr.reviewers")
            if "title_prefix" in pr and not isinstance(pr["title_prefix"], str):
                problems.append("merge.pr.title_prefix: must be a string")

    wts = doc.get("worktrees")
    if wts is not None and _check_mapping(problems, wts, "worktrees",
                                          tuple(PROJECT_DEFAULTS["worktrees"])):
        if "root" in wts and wts["root"] is not None and not _is_str(wts["root"]):
            problems.append("worktrees.root: must be null or a non-empty string")
        _check_str_list(problems, wts.get("setup"), "worktrees.setup")
        _check_enum(problems, wts.get("cleanup"), "worktrees.cleanup", CLEANUP_MODES)

    agents = doc.get("agents")
    if agents is not None and _check_mapping(problems, agents, "agents",
                                             tuple(PROJECT_DEFAULTS["agents"])):
        _check_str_list(problems, agents.get("context_files"), "agents.context_files")
        _check_enum(problems, agents.get("permission_mode"), "agents.permission_mode",
                    PERMISSION_MODES)
        _check_str_list(problems, agents.get("allowed_tools"), "agents.allowed_tools")
        if "commit_trailer" in agents and not isinstance(agents["commit_trailer"], str):
            problems.append("agents.commit_trailer: must be a string")

    if problems:
        raise ValueError("invalid project config:\n  - " + "\n  - ".join(problems))


# ---- loading ---------------------------------------------------------------------------

def _with_defaults(doc):
    out = copy.deepcopy(PROJECT_DEFAULTS)
    out["project"] = doc.get("project")
    out["base"] = doc.get("base", out["base"])
    out["gates"] = [{**GATE_DEFAULTS, **g} for g in doc.get("gates", [])]
    for g in out["gates"]:
        if g.get("on_fail") == "warn":          # SPEC-0289: the spelled-out `required: false`
            g["required"] = False
    for section in ("merge", "worktrees", "agents"):
        out[section].update(doc.get(section) or {})
    out["merge"]["pr"] = {**PROJECT_DEFAULTS["merge"]["pr"], **((doc.get("merge") or {}).get("pr") or {})}
    if out["merge"]["base"] is None:
        out["merge"]["base"] = out["base"]
    return out


def load_project_config(path):
    """Parse, validate and default a `.wt/project.yaml`. Raises ValueError on any problem."""
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"no project config at {path}")
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: not valid YAML ({e})")
    if doc is None:
        doc = {}
    validate(doc)
    return _with_defaults(doc)


def config_path(repo_root):
    return Path(repo_root) / CONFIG_RELPATH


def project_root(cfg, project):
    """Repo root for `project` via the registry, or ValueError carrying the registry note."""
    from ..rules import resolve_research_context

    ctx = resolve_research_context(cfg, project)
    if not ctx.get("root"):
        raise ValueError(f"cannot resolve a repo for project {project!r}: "
                         f"{ctx.get('note') or 'no research root'}")
    return Path(ctx["root"]).resolve()


def project_config(cfg, project):
    """Resolved config for `project` plus `_root` / `_path` (absolute). Errors name the fix."""
    root = project_root(cfg, project)
    path = config_path(root)
    if not path.is_file():
        raise ValueError(f"no {CONFIG_RELPATH} under {root}; run `wt projects init {project}`")
    doc = load_project_config(path)
    if doc.get("project") and doc["project"] != project:
        raise ValueError(f"{path}: project: {doc['project']!r} does not match {project!r}")
    doc["_root"] = str(root)
    doc["_path"] = str(path)
    return doc


def worktree_root(doc, repo_root, project):
    """Where this project's campaign worktrees live (SPEC-0169 decision 7)."""
    repo_root = Path(repo_root)
    raw = (doc.get("worktrees") or {}).get("root")
    if not raw:
        return repo_root.parent / ".wt-worktrees" / project
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else (repo_root / p).resolve()


def expand_cmd(cmd, **values):
    """Replace the known `{placeholder}`s only; other braces are left alone (never format)."""
    out = cmd
    for key in PLACEHOLDERS:
        if key in values and values[key] is not None:
            out = out.replace("{" + key + "}", str(values[key]))
    return out


def _git_toplevel(cwd):
    try:
        r = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


def find_project_for_cwd(cfg, cwd=None):
    """The registry project whose repo root is the git toplevel of `cwd`, else None."""
    from ..rules import projects_payload

    top = _git_toplevel(cwd or os.getcwd())
    if top is None:
        return None
    for p in projects_payload(cfg)["projects"]:
        root = (p.get("research") or {}).get("root")
        if root and Path(root).resolve() == top:
            return p["name"]
    return None
