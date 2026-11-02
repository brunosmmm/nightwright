"""`nw queue conductor-launch` / `steward-launch` (SPEC-0319): start the conductor or a steward
as a Claude Code session with forced context limits, a pinned model and a session id nightwright
registered (SPEC-0299) before `claude` starts.

`settings` and `build_plan` are pure; `run` is the only function with process side effects."""
from __future__ import annotations

import math
import os
import re
import shutil
import uuid

import click

from . import conductor_cost as CC

CONDUCTOR_PROMPT = "Run the /nw-orient skill, then the /nw-conduct skill."
STEWARD_PROMPT = "Run the /nw-orient skill, then the /nw-steward skill for project {project}."

# Rule generators (SPEC-0353). `*` matches any characters, spaces included, so allow rules that end
# in a wildcard are guarded by a deny rule for every argument that can follow the ref.
WORKTREES_DIR = ".nw-worktrees"
_PREFIXES = ("git", "git -C *")
_NAMESPACES = ("nw/*", "wt/*")
_FORCE_FORMS = ("", "--force-with-lease ", "--force-with-lease=* ")
_PUSH_DENY = (
    "* main", "* master", "* main *", "* master *",
    "*:main", "*:master", "*:main *", "*:master *",
    "*refs/heads/main", "*refs/heads/master", "*refs/heads/main *", "*refs/heads/master *",
    "*+*",
    "--force *", "-f *", "--force", "-f", "* --force", "* --force *", "* -f", "* -f *",
    "* nw/* *", "* wt/* *", "*:nw/* *", "*:wt/* *")
# Added after review: `nw/x:<dst>` (any destination, incl. partly qualified `heads/main`, tags),
# `heads/main` spellings, and tokens swallowed by `--force-with-lease=*` before `origin`.
_PUSH_DENY += ("* nw/*:*", "* wt/*:*", "*:nw/*:*", "*:wt/*:*", "*heads/main*", "*heads/master*",
               "--force-with-lease=* * origin *")
# `git -C *` can swallow any subcommand, so ` push ` must be the 4th token (`git -C <dir> push`)
_PUSH_DENY_C_ONLY = ("* push *",)
# SPEC-0382: the conductor's git verbs, read-only. Not `Bash(git *)`: that would allow a push to
# any branch outside the denied main/master set and the rebase set SPEC-0353 keeps off the conductor
_CONDUCTOR_GIT_VERBS = ("status", "status *", "log *", "diff *", "show *", "rev-parse *",
                        "rev-list *", "merge-base *", "ls-files *", "ls-files", "branch --list *",
                        "branch", "branch -a", "branch -vv")
_STEWARD_VERBS = ("fetch *", "rebase *", "rebase --continue", "rebase --abort", "add *",
                  "commit *", "restore *", "checkout --ours *", "checkout --theirs *")
# `git <x>` and `git * <x>` (the latter covers `git -C <dir> <x>`)
_GUARD_SUBCOMMANDS = ("reset --hard*", "branch -D*", "branch -d*", "branch --delete*",
                      "clean *", "clean")
# flags that run shell commands or inject config; `git *<x>` covers any position. git accepts any
# unambiguous abbreviation of a long option (`--exe=`, `--upload=`) and a stuck short-option value
# (`-x'cmd'`), so these are the shortest unambiguous prefixes that do not hit legitimate flags
# (`--update`, `--recurse-submodules`, `--continue`), and ` -x*` has no space after the flag.
_GUARD_FLAGS = ("--ex*", " -x*", "--upl*", "--rece*", "--config-e*", "--git-d*", "--wo*",
                "--open-f*", "--out*", "--templ*")
# `git grep -O<cmd>` runs <cmd> through the shell (stuck short value, so `-O*` after `grep`)
_GUARD_GREP_O = ("grep *-O*",)
# `-x` combined behind other short flags (`rebase -ix'cmd'`): any dash option followed by an `x`
_GUARD_REBASE_X = ("rebase -*x*", "rebase * -*x*")
# subcommands that edit config/refs, run commands or destroy data; reachable only by `-C *`
# swallowing a quoted argument that ends in an allowed tail (`config k "x # add y"`)
_GUARD_SWALLOWED = ("config", "rm", "update-ref", "symbolic-ref", "worktree", "stash", "remote",
                    "submodule", "filter-branch", "filter-repo", "gc", "reflog", "bisect",
                    "difftool", "mergetool", "hook", "maintenance", "replace")
_GUARD_KEYWORDS = ("fetch", "rebase", "add", "commit", "restore", "checkout")


def _push_allow():
    return [f"Bash({p} push {f}origin {r})" for p in _PREFIXES for ns in _NAMESPACES
            for r in (ns, f"HEAD:{ns}") for f in _FORCE_FORMS]


def _push_deny():
    rules = [f"Bash({p} push {x})" for p in _PREFIXES for x in _PUSH_DENY]
    return rules + [f"Bash(git -C * {x})" for x in _PUSH_DENY_C_ONLY]


def _steward_guard():
    rules = []
    for x in _GUARD_SUBCOMMANDS:
        rules += [f"Bash(git {x})", f"Bash(git * {x})"]
    rules += [f"Bash(git *{x})" for x in _GUARD_FLAGS]
    rules += [f"Bash(git {x})" for x in _GUARD_REBASE_X]
    rules += [f"Bash(git * {x})" for x in _GUARD_REBASE_X]
    rules += [f"Bash(git {x})" for x in _GUARD_GREP_O] + [f"Bash(git * {x})" for x in _GUARD_GREP_O]
    rules += ["Bash(git -c *)", "Bash(git * -c *)"]
    for x in _GUARD_SWALLOWED:
        rules += [f"Bash(git * {x})", f"Bash(git * {x} *)"]
    for kw in _GUARD_KEYWORDS:
        rules += [f"Bash(git * push * {kw})", f"Bash(git * push * {kw} *)"]
    # structural: `git -C <dir> <kw> …` has the verb as the 3rd token; these need it to be the
    # 4th or later, so `git -C <dir> <other-sub> … <kw> …` (grep -O, log --output=, clone
    # --template=, whatever the other subcommand is) is denied, as `git -C * * push *` does for push
    for kw in _GUARD_KEYWORDS:
        rules += [f"Bash(git -C * * {kw} *)", f"Bash(git -C * * {kw})"]
    return rules


def _dedup(items):
    return list(dict.fromkeys(items))


DEFAULTS = {"model": "claude-sonnet-5-5", "effort": "medium", "autocompact": "150k",
            "idle_compact_min_tokens": 120000, "bash_max_output_length": 15000,
            "file_read_max_output_tokens": 10000,
            "allowed_tools": [*_push_allow(), "Bash(gh pr create *)", "Bash(gh pr edit *)",
                              "Bash(gh pr comment *)"],
            "steward_allowed_tools": [f"Bash({p} {v})" for p in _PREFIXES for v in _STEWARD_VERBS],
            "disallowed_tools": [*_push_deny(), *_steward_guard(), "Bash(gh pr merge *)"],
            # SPEC-0382: the conductor never blocks on the human; under `dontAsk` (which applies
            # to its sub-agents too) the allow-list keeps their normal work (rebase conflicts in
            # campaign worktrees, looking into stuck runs) from being refused
            "conductor_disallowed_tools": ["AskUserQuestion"],
            "conductor_permission_mode": "dontAsk",
            "conductor_allowed_tools": ["Read", "Grep", "Glob",
                                        f"Edit(//**/{WORKTREES_DIR}/**)",
                                        f"Write(//**/{WORKTREES_DIR}/**)",
                                        *[f"Bash({p} {v})" for p in _PREFIXES
                                          for v in _CONDUCTOR_GIT_VERBS],
                                        "Bash(uv run pytest *)", "Bash(ps *)", "Bash(tail *)"]}
TOOL_FLAGS = {"allowed_tools": "--allowedTools", "disallowed_tools": "--disallowedTools"}
# list-valued settings; `steward_allowed_tools` is merged into `--allowedTools`, never its own flag
LIST_KEYS = {*TOOL_FLAGS, "steward_allowed_tools", "conductor_disallowed_tools",
             "conductor_allowed_tools"}
PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
AUTOCOMPACT_MIN, AUTOCOMPACT_MAX = 100_000, 1_000_000
# settings key -> env var, in the order NW_LAUNCH_KEYS lists them
ENV_KEYS = {"idle_compact_min_tokens": "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS",
            "bash_max_output_length": "BASH_MAX_OUTPUT_LENGTH",
            "file_read_max_output_tokens": "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS"}
ENV_REMOVE = ["CLAUDE_CODE_EFFORT_LEVEL"]
LAUNCH_KEYS_VAR = "NW_LAUNCH_KEYS"
_COUNT_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*$")


def _number(v):
    """`v` as a finite non-negative float, or None (bool, non-numeric, NaN/inf, negative)."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and f >= 0 else None


def _tokens(v):
    """A token count from an int, a numeric string, or an `Nk`/`Nm` string; else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = _number(v)
        return int(f) if f is not None and f.is_integer() else None
    m = _COUNT_RE.match(v) if isinstance(v, str) else None
    if not m:
        return None
    f = float(m.group(1)) * {"": 1, "k": 1000, "m": 1_000_000}[m.group(2).lower()]
    return int(f) if f.is_integer() else None


def _good(key, v):
    """The cleaned value of setting `key`, or None when `v` is a bad value."""
    if key == "model":
        return v.strip() if isinstance(v, str) and v.strip() else None
    if key == "effort":
        return v if v in EFFORTS else None
    if key == "conductor_permission_mode":
        return v if v in PERMISSION_MODES else None
    if key == "autocompact":
        if isinstance(v, str) and v.strip().lower() == "auto":
            return "auto"
        n = _tokens(v)
        return n if n is not None and AUTOCOMPACT_MIN <= n <= AUTOCOMPACT_MAX else None
    if key in LIST_KEYS:
        ok = isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
        return [x.strip() for x in v] if ok else None
    n = _tokens(v)
    floor = 100_000 if key == "idle_compact_min_tokens" else 1000
    return n if n is not None and n >= floor else None


def settings(cfg, role="conductor") -> dict:
    """`DEFAULTS` overlaid with `agentic.conductor.launch` and, for a steward, its `steward`
    sub-mapping. A bad value falls back to the default; `autocompact` becomes a token count
    (or `auto`)."""
    block = (((cfg.get("agentic") or {}).get("conductor") or {}).get("launch")) or {}
    block = block if isinstance(block, dict) else {}
    steward = block.get("steward") if role == "steward" else None
    steward = steward if isinstance(steward, dict) else {}
    out = {}
    for key, default in DEFAULTS.items():
        out[key] = _good(key, default)
        for layer in (block, steward):
            if key in layer:
                good = _good(key, layer[key])
                out[key] = good if good is not None else _good(key, default)
    return out


def _project_root(cfg, project):
    from ..rules import projects_payload
    projects = projects_payload(cfg)["projects"]
    for p in projects:
        if p["name"] == project:
            root = (p.get("research") or {}).get("root")
            if root:
                return root
            raise click.ClickException(
                f"project {project!r} has no repo root; known projects: "
                + ", ".join(q["name"] for q in projects) + " (see `nw projects --json`)")
    raise click.ClickException(
        f"unknown project {project!r}; known projects: "
        + ", ".join(q["name"] for q in projects) + " (see `nw projects --json`)")


def reader_for(role, project=None) -> str:
    return "conductor" if role == "conductor" else f"steward:{project}"


def build_plan(cfg, role, project=None, *, resume=False, session_id=None) -> dict:
    """The launch plan; touches nothing. With `resume`, the id is the newest launched session
    of the reader (a ClickException when there is none or its transcript is gone)."""
    from ..rules import INTERNAL_PROJECT
    if role not in ("conductor", "steward"):
        raise ValueError(role)
    sett = settings(cfg, role)
    name = reader_for(role, project)
    cwd = _project_root(cfg, INTERNAL_PROJECT if role == "conductor" else project)
    if resume:
        launched = [r for r in CC.sessions(cfg, name) if r.get("source") == "launch"]
        sid = launched[-1]["session_id"] if launched else None
        if not sid or CC.find_transcript(cfg, sid) is None:
            raise click.ClickException("no launched session to resume; run without --resume")
    else:
        sid = session_id or str(uuid.uuid4())
    prompt = (CONDUCTOR_PROMPT if role == "conductor"
              else STEWARD_PROMPT.format(project=project))
    # --allowedTools/--disallowedTools are variadic: each is followed by another option (never
    # the positional prompt) so the prompt is not swallowed as a rule. Empty list: no flag.
    # The steward's effective allow list is allowed_tools + steward_allowed_tools (SPEC-0353).
    # The conductor's is allowed_tools + conductor_allowed_tools, its deny list gains
    # conductor_disallowed_tools (SPEC-0382); a steward gets neither.
    extra_allow = {"steward": sett["steward_allowed_tools"],
                   "conductor": sett["conductor_allowed_tools"]}[role]
    extra_deny = sett["conductor_disallowed_tools"] if role == "conductor" else []
    eff = {"allowed_tools": _dedup(sett["allowed_tools"] + extra_allow),
           "disallowed_tools": _dedup(sett["disallowed_tools"] + extra_deny)}
    perms = [x for key, flag in TOOL_FLAGS.items() if eff[key] for x in (flag, *eff[key])]
    mode = sett["conductor_permission_mode"] if role == "conductor" else None
    argv = ["claude", "-n", name, "--resume" if resume else "--session-id", sid, *perms,
            *(["--permission-mode", mode] if mode else []),
            "--model", sett["model"], "--effort", sett["effort"],
            "--autocompact", str(sett["autocompact"]), prompt]
    env = {var: str(sett[key]) for key, var in ENV_KEYS.items()}
    env[LAUNCH_KEYS_VAR] = ",".join(ENV_KEYS.values())
    return {"role": role, "name": name, "cwd": str(cwd), "session_id": sid, "resumed": resume,
            "argv": argv, "env": env, "env_remove": list(ENV_REMOVE), "prompt": prompt,
            "model": sett["model"], "permission_mode": mode,
            "allowed_tools": list(eff["allowed_tools"]),
            "disallowed_tools": list(eff["disallowed_tools"]), "price": CC.price_for(cfg, sett["model"])}


def missing_prices(cfg) -> list:
    """`[(role, model)]` for each launch role (conductor, steward) whose model has no price in
    `agentic.conductor.prices`: the same lookup `build_plan` makes, without a project root.
    `nw doctor` reports these (SPEC-0349); `check_price` refuses the launch."""
    out = []
    for role in ("conductor", "steward"):
        model = settings(cfg, role)["model"]
        if CC.price_for(cfg, model) is None:
            out.append((role, model))
    return out


def check_price(plan):
    if plan["price"] is None:
        raise click.ClickException(
            f"no price for model {plan['model']}; set agentic.conductor.prices.<model-prefix> "
            "(SPEC-0299)")


def exec_env(plan, base=None) -> dict:
    """`base` (default `os.environ`) without the `env_remove` keys, overlaid with the plan's env."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if k not in plan.get("env_remove", ())}
    env.update(plan["env"])
    return env


def register(cfg, plan):
    """Pre-register the session in the SPEC-0299 registry (idempotent per reader and id)."""
    try:
        CC.record_session(cfg, plan["name"], {}, source="launch", session_id=plan["session_id"],
                          extra={"disallowed_tools": plan["disallowed_tools"]})
    except OSError as e:
        raise click.ClickException(f"cannot register the session: {e}")


def run(cfg, plan):
    """Check the price and the binary, chdir to the plan's cwd, register, exec `claude`."""
    check_price(plan)
    if shutil.which("claude") is None:
        raise click.ClickException("`claude` not found on PATH")
    try:
        os.chdir(plan["cwd"])
    except OSError as e:
        raise click.ClickException(f"cannot enter {plan['cwd']}: {e}")
    register(cfg, plan)
    try:
        os.execvpe("claude", plan["argv"], exec_env(plan))
    except OSError as e:   # e.g. a broken `claude` wrapper: an error, not a traceback
        raise click.ClickException(f"cannot start claude: {e}; the registered launch record "
                                   "has no session, so start fresh (without --resume)")
