"""`nw queue conductor-launch` / `steward-launch` (SPEC-0319 / SPEC-0437): start the conductor
or a steward as a Claude Code or cursor-agent session with forced context limits, a pinned
model and a session id nightwright registered (SPEC-0299) before the binary starts.

`settings` and `build_plan` are pure; `run` is the only function with process side effects."""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import uuid

import click

from . import conductor_cost as CC
from .runner import CLAUDE_PERMISSION_MODES, canonical_permission_mode

CONDUCTOR_PROMPT = "Run the /nw-orient skill, then the /nw-conduct skill."
STEWARD_PROMPT = "Run the /nw-orient skill, then the /nw-steward skill for project {project}."
# SPEC-0437: Cursor has no --allowedTools/--disallowedTools. Short policy, not the glob explosion.
CONDUCTOR_CURSOR_POLICY = (
    "Cursor has no tool allow/deny flags. You MUST follow these without waiting for the human:\n"
    "- Do not use AskUserQuestion.\n"
    "- Do not push to main or master; do not force-push except origin nw/* or origin wt/* "
    "with --force-with-lease.\n"
    "- Do not run `gh pr merge`.\n"
    "- Do not edit the main checkout; write only under `.nw-worktrees/`.\n"
    "- A main_dirty clear of the trivial class runs `uv run nw queue main-dirty-apply "
    "--trivial-only`; never run it otherwise. Never write the main checkout with raw git (add, "
    "commit, rm, checkout, switch, restore, merge, rebase, reset, mv, cherry-pick, revert, "
    "stash, apply, am, hash-object, update-index, worktree add outside `.nw-worktrees/`) or cp; "
    "in a worktree under `.nw-worktrees/` add and commit as usual. "
    "No reset --hard, clean, branch -D, or push to the base.\n"
    "- You may Read/Grep/Glob, Edit/Write under `.nw-worktrees/`, and run `uv run nw`, "
    "`uv run pytest`, spec_lint.")
STEWARD_CURSOR_POLICY = (
    "Cursor has no tool allow/deny flags. You MUST follow these without waiting for the human:\n"
    "- Do not push to main or master; do not force-push except origin nw/* or origin wt/* "
    "with --force-with-lease.\n"
    "- Do not run `gh pr merge`.\n"
    "- Do not edit the main checkout; write only under `.nw-worktrees/`.\n"
    "- Git in worktrees: fetch, rebase, add, commit, restore. "
    "No reset --hard, clean, branch -D.\n"
    "- You may Read/Grep/Glob, Edit/Write under `.nw-worktrees/`, and run `uv run nw`, "
    "`uv run pytest`, spec_lint.")

# SPEC-0551: the conductor's verb deny. Anchored (no `*` between `nw` and `queue`): `*` matches
# spaces, so `* queue main-dirty-apply` would also deny the conductor's own
# `queue escalate --fact 'nw queue main-dirty-apply --root R'`. Every option of the verb but
# `--trivial-only` gets a tail and every option of the root group a lead; any other spelling is
# the git-guard hook's job (SPEC-0550 step 7). A launch test keeps these in step with click.
_VERB_PREFIXES = ("uv run nw", "nw")
_VERB_LEADS = ("", "--split ", "-h ", "--help ")
_VERB_TAILS = (" --root*", " --plan*", " --base*", " --json*", " --help*", " -h*")


def _verb_deny():
    return [f"Bash({p} {lead}queue main-dirty-apply{tail})" for p in _VERB_PREFIXES
            for lead in _VERB_LEADS for tail in ("", *_VERB_TAILS)]


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
_PUSH_DENY += ("* nw/*:**", "* wt/*:**", "*:nw/*:**", "*:wt/*:**", "*heads/main*", "*heads/master*",
               "--force-with-lease=* * origin *")
# `git -C *` can swallow any subcommand, so ` push ` must be the 4th token (`git -C <dir> push`)
_PUSH_DENY_C_ONLY = ("* push *",)
# SPEC-0382: the conductor's git verbs, read-only. Not `Bash(git *)`: that would allow a push to
# any branch outside the denied main/master set and the rebase set SPEC-0353 keeps off the conductor
_CONDUCTOR_GIT_VERBS = ("status", "status *", "log *", "diff *", "show *", "rev-parse *",
                        "rev-list *", "merge-base *", "ls-files *", "ls-files", "branch --list *",
                        "branch", "branch -a", "branch -vv")
# what the nw-conduct rows for `main_dirty` / `main_not_on_base` / `wt_broken` need to move
# in-place WIP onto its own worktree branch. Deliberately not the steward's `add *` / `commit *`
# strings (SPEC-0353 keeps the steward's rebase set off the conductor): narrower forms only. The
# steward guard denies (`reset --hard`, `clean`, `branch -D`, pushes) still win. Nothing here
# discards uncommitted work: no `checkout -- *`, and `switch` only creates (`--create`; the short
# `-c` is already denied by the config-injection guard `git * -c *`), with the force / discard
# forms denied below; dropping the moved WIP from main is left to the steward or the human.
_CONDUCTOR_WIP_VERBS = ("worktree add *", "worktree list", "switch --create *",
                        "add -A", "add -A *", "add -- *", "commit -m *", "commit -am *")
# `switch -f` / `--force` / `--discard-changes` / `-C` throw away or reset local state
_CONDUCTOR_SWITCH_DENY = ("-f*", "* -f*", "-C*", "* -C*", "*--force*", "*--discard-changes*")
# conductor verbs `git -C <dir> …` must not swallow ahead of the verb (see _steward_guard)
_CONDUCTOR_GUARD_KEYWORDS = ("status", "log", "diff", "show", "rev-parse", "rev-list", "merge-base",
                             "ls-files", "branch", "switch")
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


def _conductor_guard():
    rules = []
    for x in _CONDUCTOR_SWITCH_DENY:
        rules += [f"Bash(git switch {x})", f"Bash(git * switch {x})"]
    for kw in _CONDUCTOR_GUARD_KEYWORDS:
        rules += [f"Bash(git -C * * {kw} *)", f"Bash(git -C * * {kw})"]
    return rules


def _dedup(items):
    return list(dict.fromkeys(items))


DEFAULTS = {"model": "claude-sonnet-5-5", "effort": "medium", "autocompact": "150k",
            "idle_compact_min_tokens": 120000, "bash_max_output_length": 15000,
            "file_read_max_output_tokens": 10000,
            "allowed_tools": [*_push_allow(), "Bash(gh pr create *)", "Bash(gh pr edit *)",
                              "Bash(gh pr comment *)"],
            # SPEC-0424: the steward runs `dontAsk` too, so its allow-list covers its whole job
            # (read anything, every `nw` command, tests and spec lint, fix branches in worktrees
            # under .nw-worktrees/); the deny list (pushes to the base, `gh pr merge`, the guard)
            # still wins. No `AskUserQuestion` rule: claude 2.1.318 denies it under dontAsk even
            # when allow-listed (SPEC-0424 real-binary check), so the steward asks the human in
            # plain text in its attended session; it is never in the steward's deny list either.
            "steward_allowed_tools": [*[f"Bash({p} {v})" for p in _PREFIXES
                                        for v in _STEWARD_VERBS],
                                      "Read", "Grep", "Glob",
                                      f"Edit(//**/{WORKTREES_DIR}/**)",
                                      f"Write(//**/{WORKTREES_DIR}/**)",
                                      "Bash(nw *)", "Bash(uv run nw *)", "Bash(uv run pytest *)",
                                      "Bash(uv sync*)", "Bash(python3 tools/spec_lint.py*)",
                                      "Bash(python3 tools/affected_tests.py*)",
                                      *[f"Bash({p} {v})" for p in _PREFIXES
                                        for v in _CONDUCTOR_GIT_VERBS],
                                      f"Bash(git worktree add ../{WORKTREES_DIR}/*)",
                                      "Bash(git worktree list)",
                                      "Bash(gh pr view *)", "Bash(gh pr checks *)",
                                      "Bash(gh pr list *)",
                                      "Bash(ls *)", "Bash(mkdir *)", "Bash(ps *)", "Bash(tail *)"],
            "steward_permission_mode": "dontAsk",
            "disallowed_tools": [*_push_deny(), *_steward_guard(), *_conductor_guard(),
                                 "Bash(gh pr merge *)"],
            # SPEC-0382: the conductor never blocks on the human; under `dontAsk` (which applies
            # to its sub-agents too) the allow-list keeps their normal work (rebase conflicts in
            # campaign worktrees, looking into stuck runs) from being refused
            # no `cp` in the allow-list below: under dontAsk it would overwrite the main checkout's
            # uncommitted files
            # SPEC-0548/0551: the verb is denied at the subcommand position only, in every form
            # but `--trivial-only` (see `_verb_deny`)
            "conductor_disallowed_tools": ["AskUserQuestion", *_verb_deny()],
            "conductor_permission_mode": "dontAsk",
            "conductor_allowed_tools": ["Read", "Grep", "Glob",
                                        f"Edit(//**/{WORKTREES_DIR}/**)",
                                        f"Write(//**/{WORKTREES_DIR}/**)",
                                        *[f"Bash({p} {v})" for p in _PREFIXES
                                          for v in (*_CONDUCTOR_GIT_VERBS, *_CONDUCTOR_WIP_VERBS)],
                                        "Bash(uv run pytest *)", "Bash(uv run nw *)",
                                        # documentary: `uv run nw *` already allows it; what
                                        # grants the flagged verb is the absence of a blanket deny
                                        "Bash(uv run nw queue main-dirty-apply --trivial-only *)",
                                        "Bash(python3 tools/spec_lint.py*)",
                                        "Bash(mkdir *)", "Bash(ps *)", "Bash(tail *)"]}
TOOL_FLAGS = {"allowed_tools": "--allowedTools", "disallowed_tools": "--disallowedTools"}
# list-valued settings; `steward_allowed_tools` is merged into `--allowedTools`, never its own flag
LIST_KEYS = {*TOOL_FLAGS, "steward_allowed_tools", "conductor_disallowed_tools",
             "conductor_allowed_tools"}
PERMISSION_MODES = CLAUDE_PERMISSION_MODES          # SPEC-0593; `default` is a deprecated alias of `manual`
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
    if key in ("conductor_permission_mode", "steward_permission_mode"):
        return canonical_permission_mode(v)       # SPEC-0593: `default` -> `manual`
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


def _session_kind(cfg, role, project=None, *, runner=None, resume=False, last=None) -> str:
    """SPEC-0438: `--runner` wins; else resume uses the last launch record; else SPEC-0437."""
    from .runner import resolve_runner
    if runner in ("claude", "cursor"):
        return runner
    if runner not in (None,):
        raise ValueError(f"unknown runner {runner!r} (expected claude or cursor)")
    stored = (last or {}).get("runner") if resume else None
    if stored in ("claude", "cursor"):
        return stored
    return resolve_runner(cfg, project=project if role == "steward" else None)


def _with_resume(argv, sid) -> list:
    out = list(argv)
    for i, a in enumerate(out[:-1]):
        if a == "--resume":
            out[i + 1] = sid
            break
    return out


def build_plan(cfg, role, project=None, *, resume=False, session_id=None, runner=None) -> dict:
    """The launch plan; touches nothing. With `resume`, the id is the newest launched session
    of the reader (a ClickException when there is none or its transcript is gone)."""
    from ..rules import INTERNAL_PROJECT
    from .cursor_map import launch_model_arg, map_launch_model
    if role not in ("conductor", "steward"):
        raise ValueError(role)
    sett = settings(cfg, role)
    name = reader_for(role, project)
    cwd = _project_root(cfg, INTERNAL_PROJECT if role == "conductor" else project)
    launched = ([r for r in CC.sessions(cfg, name) if r.get("source") == "launch"]
                if resume else [])
    last = launched[-1] if launched else None
    try:
        kind = _session_kind(cfg, role, project, runner=runner, resume=resume, last=last)
    except ValueError as e:
        raise click.ClickException(str(e))
    if resume:
        sid = last["session_id"] if last else None
        if not sid:
            raise click.ClickException("no launched session to resume; run without --resume")
        stored = last.get("runner")
        if stored in ("claude", "cursor") and stored != kind:
            raise click.ClickException(
                f"newest launch was {stored}; start fresh without --resume "
                f"or pass --runner {stored}")
        if kind == "cursor" and stored != "cursor":
            raise click.ClickException(
                "newest launch was not cursor-agent; start fresh without --resume")
        if kind == "claude" and CC.find_transcript(cfg, sid) is None:
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
    mode = sett[f"{role}_permission_mode"]                 # SPEC-0424: both roles
    if kind == "cursor":
        model = launch_model_arg(map_launch_model(sett["model"], sett["effort"], cfg))
        policy = CONDUCTOR_CURSOR_POLICY if role == "conductor" else STEWARD_CURSOR_POLICY
        prompt = prompt + "\n\n" + policy
        force = [] if mode == "plan" else ["--force"]
        plan_mode = ["--mode", "plan"] if mode == "plan" else []
        argv = ["cursor-agent", "--resume", sid, *force, *plan_mode, "--trust", "--approve-mcps",
                "--workspace", str(cwd), "--model", model, prompt]
        env = {}
        price = None
    else:
        argv = ["claude", "-n", name, "--resume" if resume else "--session-id", sid, *perms,
                *(["--permission-mode", mode] if mode else []),
                "--model", sett["model"], "--effort", sett["effort"],
                "--autocompact", str(sett["autocompact"]), prompt]
        env = {var: str(sett[key]) for key, var in ENV_KEYS.items()}
        env[LAUNCH_KEYS_VAR] = ",".join(ENV_KEYS.values())
        model = sett["model"]
        price = CC.price_for(cfg, sett["model"])
    plan = {"role": role, "name": name, "cwd": str(cwd), "session_id": sid, "resumed": resume,
            "runner": kind, "argv": argv, "env": env, "env_remove": list(ENV_REMOVE),
            "prompt": prompt, "model": model, "permission_mode": mode,
            "allowed_tools": list(eff["allowed_tools"]),
            "disallowed_tools": list(eff["disallowed_tools"]), "price": price}
    plan["launch_hash"] = launch_hash(plan)
    return plan


_SESSION_TOKEN = "<session>"


def launch_hash(plan) -> str:
    """SPEC-0424: `sha256:<hex16>` of the canonical JSON of the plan's effective launch (role,
    name, cwd, argv, env, env_remove). The `--session-id` / `--resume` flag and its value are
    replaced by a fixed token, so a `--resume` launch hashes equal to a fresh one."""
    argv = list(plan["argv"])
    for i, a in enumerate(argv[:-1]):
        if a in ("--session-id", "--resume"):
            argv[i] = argv[i + 1] = _SESSION_TOKEN
            break
    doc = {"role": plan["role"], "name": plan["name"], "cwd": plan["cwd"], "argv": argv,
           "env": plan["env"], "env_remove": plan["env_remove"]}
    raw = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def missing_prices(cfg) -> list:
    """`[(role, model)]` for each launch role (conductor, steward) whose model has no price in
    `agentic.conductor.prices`: the same lookup `build_plan` makes, without a project root.
    `nw doctor` reports these (SPEC-0349); `check_price` refuses the launch.
    SPEC-0437: empty when the host default is cursor (no USD table)."""
    from .runner import resolve_runner
    if resolve_runner(cfg) == "cursor":
        return []
    out = []
    for role in ("conductor", "steward"):
        model = settings(cfg, role)["model"]
        if CC.price_for(cfg, model) is None:
            out.append((role, model))
    return out


def check_price(plan):
    if plan.get("runner") == "cursor":
        return
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
    """Pre-register the session in the SPEC-0299 registry (one row per reader and id; a resumed
    launch refreshes the row's `disallowed_tools`, `launch_hash` and `nw_build` to the launch it
    now runs with, SPEC-0382 / SPEC-0424)."""
    from .. import buildid
    try:
        build = buildid.local()
    except Exception:          # the build id is informational; never block a launch on it
        build = None
    try:
        CC.record_session(cfg, plan["name"], {}, source="launch", session_id=plan["session_id"],
                          extra={"disallowed_tools": plan["disallowed_tools"],
                                 "launch_hash": plan.get("launch_hash") or launch_hash(plan),
                                 "nw_build": build,
                                 "runner": plan.get("runner") or "claude"},
                          refresh=bool(plan.get("resumed")))
    except OSError as e:
        raise click.ClickException(f"cannot register the session: {e}")


def run(cfg, plan):
    """Check the price and the binary, chdir to the plan's cwd, register, exec the backend."""
    from . import runproc as RP
    kind = plan.get("runner") or "claude"
    check_price(plan)
    if kind == "cursor":
        exe = RP.resolve_cursor_agent()
        if exe is None:
            raise click.ClickException(
                "`cursor-agent` not found on PATH or in the standard install dirs")
        if not plan.get("resumed"):
            try:
                sid = RP.create_cursor_chat(exe)
            except (OSError, RuntimeError, FileNotFoundError) as e:
                raise click.ClickException(f"cursor-agent create-chat failed: {e}")
            plan["session_id"] = sid
            plan["argv"] = _with_resume(plan["argv"], sid)
    elif shutil.which("claude") is None:
        raise click.ClickException("`claude` not found on PATH")
    try:
        os.chdir(plan["cwd"])
    except OSError as e:
        raise click.ClickException(f"cannot enter {plan['cwd']}: {e}")
    register(cfg, plan)
    try:
        if kind == "cursor":
            argv, env = RP.with_cursor_agent(plan["argv"], exec_env(plan))
            os.execvpe(argv[0], argv, env)
        else:
            os.execvpe("claude", plan["argv"], exec_env(plan))
    except OSError as e:   # e.g. a broken wrapper: an error, not a traceback
        name = "cursor-agent" if kind == "cursor" else "claude"
        raise click.ClickException(f"cannot start {name}: {e}; the registered launch record "
                                   "has no session, so start fresh (without --resume)")


# ---- launch freshness (SPEC-0424) ----------------------------------------------------------

FIXED_SESSION = "<fixed>"
RELAUNCH_CHECK_S = 60          # the follower checks freshness at most this often


def fix_for(reader) -> str:
    if reader == "conductor":
        return "nw queue conductor-launch --resume"
    return f"nw queue steward-launch {reader.split(':', 1)[1]} --resume"


def _readers(cfg) -> list[str]:
    """`conductor` plus every `steward:<P>` with a registered session, first seen first."""
    path = CC._sessions_path(cfg)
    found = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    except OSError:
        text = ""
    for raw in text.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        r = rec.get("reader") if isinstance(rec, dict) else None
        if isinstance(r, str) and r.startswith("steward:") and len(r) > 8 and r not in found:
            found.append(r)
    return ["conductor", *found]


def freshness(cfg) -> list[dict]:
    """One row per reader (`conductor`, `steward:<P>`) with a registered session, for its newest
    `source: launch` record (else its newest record): is the launch it runs the one `build_plan`
    would produce now? A launch record whose `launch_hash` is missing or differs is `stale`; a
    reader with only hand-started sessions is `managed: false` and never stale; a project that
    no longer resolves is `why: project unknown`, not stale. Pure over the registry and config."""
    out = []
    for reader in _readers(cfg):
        rows = CC.sessions(cfg, reader)
        if not rows:
            continue
        # the newest launched record is the one `--resume` resumes, so it is the one judged even
        # when a hand-registered record (`conductor-session add`, a follower) came after it
        launched = [r for r in rows if r.get("source") == "launch"]
        last = launched[-1] if launched else rows[-1]
        managed = last.get("source") == "launch"
        role, project = (("conductor", None) if reader == "conductor"
                         else ("steward", reader.split(":", 1)[1]))
        recorded = last.get("launch_hash")
        try:
            current = build_plan(cfg, role, project, session_id=FIXED_SESSION)["launch_hash"]
        except (click.ClickException, OSError, ValueError, KeyError, TypeError):
            current = None
        if current is None:
            stale, why = False, "project unknown"
        elif not managed:
            stale, why = False, "not started by nw (hand-started session)"
        elif not recorded:
            stale, why = True, "no launch hash recorded (launched before SPEC-0424)"
        elif recorded != current:
            stale, why = True, "the launch flags changed since this session started"
        else:
            stale, why = False, None
        out.append({"reader": reader, "session_id": last.get("session_id"),
                    "source": last.get("source"), "managed": managed,
                    "recorded_hash": recorded, "current_hash": current, "stale": stale,
                    "why": why, "fix": fix_for(reader)})
    return out


def _requests_path(cfg):
    return CC.conductor_dir(cfg) / "launch_requests.json"


def _load_requests(path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"checked_at": None, "requested": {}}
    if not isinstance(doc, dict):
        return {"checked_at": None, "requested": {}}
    req = doc.get("requested")
    return {"checked_at": doc.get("checked_at"), "requested": req if isinstance(req, dict) else {}}


def _age_s(ts, now):
    try:
        return (now - dt.datetime.fromisoformat(ts)).total_seconds()
    except (TypeError, ValueError):
        return None


def request_relaunch(cfg, now=None) -> list[dict]:
    """The follower's relaunch request (SPEC-0424): for each stale reader, append one
    `launch_stale` event and mail the human once per `(reader, current_hash)`, remembered in
    `<agentic>/conductor/launch_requests.json` under its lock. Never kills or signals anything:
    the session cannot replace itself and the terminal is the human's. Checks at most every
    `RELAUNCH_CHECK_S`. Returns the events appended."""
    from . import events as E
    from . import mailbox as M
    from . import store
    now = now or dt.datetime.now().astimezone()
    path = _requests_path(cfg)
    age = _age_s(_load_requests(path).get("checked_at"), now)
    if age is not None and 0 <= age < RELAUNCH_CHECK_S:
        return []
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    out = []
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        state = _load_requests(path)
        for row in freshness(cfg):
            key = f"{row['reader']}|{row['current_hash']}"
            if not row["stale"] or key in state["requested"]:
                continue
            data = {k: row[k] for k in ("reader", "session_id", "recorded_hash", "current_hash",
                                        "fix")}
            data["why"] = row["why"]
            project = None if row["reader"] == "conductor" else row["reader"].split(":", 1)[1]
            out.append(E.append(cfg, "launch_stale", project=project, by="follower", data=data))
            try:
                M.send(cfg, to="human", frm="conductor", kind="info",
                       text=(f"{row['reader']} runs a stale launch ({row['why']}). In its "
                             f"terminal, when it is idle, run: {row['fix']}"))
            except (OSError, ValueError):
                pass        # the event is the durable request; the mail is a courtesy copy
            state["requested"][key] = store.now_iso(now)
        state["checked_at"] = store.now_iso(now)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return out
