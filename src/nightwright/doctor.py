"""`nw doctor` (SPEC-0349): a read-only, offline health check of a nightwright install.

One `check_*` function per check, each taking only what it needs and returning one Check (or a
list for the per-project checks): `{id, group, status, summary, detail, fix}` with `status` one of
`ok | warn | fail | skip`. `run` calls them in a fixed order; a check that raises becomes a
`fail` carrying the exception text, so a doctor bug never hides the other checks.

Read-only and offline: no check writes a file, opens a socket or runs ssh / `gh auth status`.
Subprocess tools go through an injectable `which` / `run_cmd` so tests never use the real PATH.
The logic each check needs lives with its feature (`launch.missing_prices`,
`profiles.check_all`, `skills.status`, `lease.lease_status`, ...) and is called, not copied."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMA = "nw.doctor.v1"
STATUSES = ("ok", "warn", "fail", "skip")
TOOL_TIMEOUT_S = 5.0
RETENTION_SAFE_DAYS = 90          # cleanupPeriodDays at or above this is safe without snapshots
RETENTION_MARGIN_DAYS = 7         # the newest snapshot must be this much younger than the cutoff
CLAUDE_DEFAULT_CLEANUP_DAYS = 30
PR_POLICIES = ("pr", "draft-pr")
SKILL_OK_STATES = ("linked", "copied")
NO_CONFIG = "config did not load"

_NULLABLE_STR = {"type": ["string", "null"]}
# JSON Schema of `nw doctor --json`. A plain dict: the runtime does not import the dev-only
# `jsonschema`; tests validate real output against it. Fields are only ever added.
DOCTOR_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": SCHEMA,
    "type": "object",
    "required": ["schema", "ok", "counts", "checks"],
    "properties": {
        "schema": {"const": SCHEMA},
        "ok": {"type": "boolean"},
        "counts": {
            "type": "object",
            "required": list(STATUSES),
            "properties": {s: {"type": "integer", "minimum": 0} for s in STATUSES},
        },
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "group", "status", "summary", "detail", "fix"],
                "properties": {
                    "id": {"type": "string", "pattern": r"^[A-Za-z0-9_-]+\..+$"},
                    "group": {"type": "string", "minLength": 1},
                    "status": {"enum": list(STATUSES)},
                    "summary": {"type": "string"},
                    "detail": _NULLABLE_STR,
                    "fix": _NULLABLE_STR,
                },
            },
        },
    },
}


def check(id, status, summary, detail=None, fix=None) -> dict:
    """One Check. `group` is the id's first segment; `fix` is dropped for ok / skip."""
    assert status in STATUSES, status
    return {"id": id, "group": id.split(".", 1)[0], "status": status, "summary": summary,
            "detail": detail, "fix": None if status in ("ok", "skip") else fix}


def _skip_no_config(id) -> dict:
    return check(id, "skip", NO_CONFIG)


def _nearest_existing(path) -> Path | None:
    p = Path(path)
    for cand in (p, *p.parents):
        if cand.exists():
            return cand
    return None


def _writable(path) -> bool:
    return os.access(path, os.W_OK)


# ---- tools ---------------------------------------------------------------------------------

def _version(name, which, run_cmd, timeout=TOOL_TIMEOUT_S):
    """`(path, first line of --version, error)`; path is None when `name` is not on PATH."""
    path = which(name)
    if not path:
        return None, None, None
    try:
        r = run_cmd([path, "--version"], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return path, None, f"`{name} --version` took over {timeout:g} s"
    except OSError as e:
        return path, None, f"`{name} --version` could not run: {e}"
    if r.returncode != 0:
        return path, None, f"`{name} --version` exited {r.returncode}"
    out = (r.stdout or r.stderr or "").strip().splitlines()
    return path, (out[0].strip() if out else name), None


_TOOL_FIX = {
    "uv": "install uv: https://docs.astral.sh/uv/getting-started/installation/",
    "git": "install git with your system package manager",
    "claude": "install Claude Code: https://docs.anthropic.com/en/docs/claude-code",
}


def check_tool(name, *, which, run_cmd) -> dict:
    """`tools.<name>` for a required tool (uv, git, claude): on PATH with a working `--version`."""
    id = f"tools.{name}"
    path, version, err = _version(name, which, run_cmd)
    if path is None:
        return check(id, "fail", f"{name} not on PATH", fix=_TOOL_FIX.get(name))
    if err:
        return check(id, "fail", err, detail=path, fix=_TOOL_FIX.get(name))
    return check(id, "ok", version, detail=path)


def check_gh(*, which, run_cmd) -> dict:
    """`tools.gh`: optional (only the `pr` / `draft-pr` merge policies need it)."""
    path, version, err = _version("gh", which, run_cmd)
    if path is None:
        return check("tools.gh", "warn", "gh not on PATH",
                     detail="only projects with merge.policy pr / draft-pr need it",
                     fix="install the GitHub CLI: https://cli.github.com")
    if err:
        return check("tools.gh", "warn", err, detail=path, fix="reinstall the GitHub CLI")
    return check("tools.gh", "ok", version, detail=path)


def check_gh_auth(pr_projects, *, which, run_cmd) -> dict:
    """`tools.gh_auth`: a stored gh token when a project merges by PR. Uses `gh auth token`
    (local credential read), never `gh auth status` (which calls the GitHub API)."""
    id = "tools.gh_auth"
    if not pr_projects:
        return check(id, "skip", "no project merges by PR")
    needs = ", ".join(pr_projects)
    path = which("gh")
    if not path:
        return check(id, "fail", f"gh not on PATH; needed by {needs}",
                     fix="install the GitHub CLI (https://cli.github.com), then `gh auth login`")
    try:
        r = run_cmd([path, "auth", "token"], capture_output=True, text=True,
                    timeout=TOOL_TIMEOUT_S)
        code = r.returncode
    except (subprocess.TimeoutExpired, OSError):
        code = None
    if code == 0:
        return check(id, "ok", "gh has a stored token",
                     detail=f"needed by {needs}; a stored token is not proof it is still valid "
                            "(`gh auth status` checks online)")
    return check(id, "fail", f"gh has no stored token; needed by {needs}", fix="gh auth login")


# ---- install / config ----------------------------------------------------------------------

def _entry_points(argv0=None) -> set:
    """Paths the running `nw` may be: argv[0] when it is an `nw` script, and the `nw` next to
    the running interpreter (a venv or `uv tool` install)."""
    out = set()
    argv0 = sys.argv[0] if argv0 is None else argv0
    if argv0 and Path(argv0).name == "nw":
        out.add(Path(argv0).resolve())
    out.add((Path(sys.executable).parent / "nw").resolve())
    return out


def check_nw_on_path(*, which, entry_points=None) -> dict:
    """`install.nw_on_path`: `nw` on PATH is the running nightwright (skills and agents call `nw`)."""
    id = "install.nw_on_path"
    found = which("nw")
    if not found:
        return check(id, "fail", "nw is not on PATH",
                     detail="skills and campaign agents run `nw`",
                     fix="uv tool install --editable <nightwright checkout>")
    entry_points = _entry_points() if entry_points is None else {Path(p).resolve()
                                                                 for p in entry_points}
    resolved = Path(found).resolve()
    if resolved in entry_points:
        return check(id, "ok", f"nw → {found}")
    running = ", ".join(sorted(str(p) for p in entry_points))
    return check(id, "warn", "nw on PATH is not the running nightwright",
                 detail=f"PATH: {found} ({resolved}); running: {running}",
                 fix="reinstall with `uv tool install --editable <checkout>`, or put the "
                     "intended nw first on PATH")


def check_checkout(cfg) -> dict:
    """`install.checkout`: nightwright's checkout resolves (SPEC-0166)."""
    from . import rules
    id = "install.checkout"
    try:
        configured = rules.configured_nw_checkout(cfg)
    except ValueError as e:
        return check(id, "fail", "nw_checkout is set but invalid", detail=str(e),
                     fix="fix or remove `nw_checkout:` in config.yaml")
    if configured:
        return check(id, "ok", str(configured), detail="from config `nw_checkout:`")
    found = rules.discover_nw_checkout()
    if found:
        return check(id, "ok", found, detail="editable / source install")
    return check(id, "warn", "nightwright checkout not found",
                 detail="skills, specs and the Meta-Tools project need it", fix=rules.CHECKOUT_HINT)


def config_file() -> Path:
    from . import paths
    return Path(paths.config_dir()) / "config.yaml"


def check_config_parse(config_error=None, *, path=None) -> dict:
    """`config.parse`: config.yaml exists and loaded (`config_error` is the load failure)."""
    path = Path(path) if path else config_file()
    if config_error:
        return check("config.parse", "fail", f"{path} did not load", detail=str(config_error),
                     fix=f"fix the YAML / timezone in {path}")
    if not path.is_file():
        return check("config.parse", "warn", "no config.yaml; every setting is a default",
                     detail=str(path), fix=f"create {path} (at least `timezone:`)")
    return check("config.parse", "ok", str(path))


def check_timezone(cfg, *, path=None) -> dict:
    """`config.timezone`: set in the user's YAML, not the America/New_York default."""
    import yaml
    if cfg is None:
        return _skip_no_config("config.timezone")
    path = Path(path) if path else config_file()
    raw = {}
    if path.is_file():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict) and raw.get("timezone"):
        return check("config.timezone", "ok", str(raw["timezone"]))
    return check("config.timezone", "warn",
                 f"timezone unset; defaulting to {cfg.get('timezone') or 'America/New_York'}",
                 detail="day boundaries and reports use it",
                 fix=f"set `timezone: <Area/City>` in {path}")


def check_org_ideas_file(cfg) -> dict:
    """`config.org_ideas_file`: the ideas file exists and can be read."""
    id = "config.org_ideas_file"
    if cfg is None:
        return _skip_no_config(id)
    raw = cfg.get("org_ideas_file")
    if not raw:
        return check(id, "warn", "org_ideas_file is unset", fix="set `org_ideas_file:` in config.yaml")
    p = Path(os.path.expanduser(str(raw)))
    if not p.exists():
        return check(id, "warn", f"{p} does not exist yet", detail="the first capture creates it",
                     fix='nw idea "<first idea>"')
    if not p.is_file() or not os.access(p, os.R_OK):
        return check(id, "fail", f"{p} cannot be read", fix=f"fix the permissions of {p}")
    return check(id, "ok", str(p))


def check_data_dir(data_dir) -> dict:
    """`data.dir`: the data dir is writable (or can be created under a writable parent)."""
    p = Path(data_dir)
    if p.exists():
        if p.is_dir() and _writable(p):
            return check("data.dir", "ok", str(p))
        return check("data.dir", "fail", f"{p} is not writable", fix=f"chmod u+w {p}")
    parent = _nearest_existing(p)
    if parent is not None and parent.is_dir() and _writable(parent):
        return check("data.dir", "warn", f"{p} does not exist yet",
                     detail=f"{parent} is writable; nw creates it on first use",
                     fix="nw snapshot (or any command that records data)")
    return check("data.dir", "fail", f"{p} cannot be created",
                 detail=f"nearest existing parent {parent} is not writable",
                 fix="set NW_DATA_DIR to a writable directory")


# ---- skills / completion -------------------------------------------------------------------

def check_skills(cfg, *, dest=None) -> dict:
    """`skills.installed`: every repo skill is linked or copied (and current) at `dest`."""
    from . import skills
    id = "skills.installed"
    try:
        source = skills.repo_skills_dir(cfg)
    except ValueError:
        source = None
    if source is None or not source.is_dir():
        return check(id, "skip", "no nightwright checkout, so no skill source")
    entries = skills.status(dest=dest, source=source)
    if not entries:
        return check(id, "skip", f"no skills under {source}")
    bad = [e for e in entries if e["state"] not in SKILL_OK_STATES]
    if not bad:
        return check(id, "ok", f"{len(entries)} skills installed")
    return check(id, "warn", f"{len(bad)} of {len(entries)} skills not installed or out of date",
                 detail="; ".join(f"{e['name']}: {e['state']}" for e in bad),
                 fix="nw skills install")


def check_completion(shell_env=None) -> dict:
    """`completion.installed`: the completion file for `$SHELL` is ours and current (SPEC-0022)."""
    from . import completion as C
    id = "completion.installed"
    shell = Path(shell_env).name if shell_env else ""
    if shell not in C.SHELLS:
        return check(id, "skip", f"shell {shell or '(unset)'} has no nw completion")
    path = C.default_install_path(shell)
    fix = f"nw completion install --shell {shell}"
    if not path.is_file():
        return check(id, "warn", f"{shell} completion not installed", detail=str(path), fix=fix)
    text = path.read_text(encoding="utf-8", errors="replace")
    if text == C.source_script(shell):
        return check(id, "ok", f"{shell}: {path}")
    if C._MARKER.rstrip("\n") in text[:80]:
        return check(id, "warn", f"{shell} completion is stale", detail=str(path), fix=fix)
    return check(id, "warn", f"{path} is not nightwright's completion file", detail=str(path),
                 fix=fix + " --force")


# ---- agentic -------------------------------------------------------------------------------

def check_prices(cfg) -> dict:
    """`agentic.prices`: every launch model has a price (the launch commands exit 1 without)."""
    from .agentic import launch
    id = "agentic.prices"
    if cfg is None:
        return _skip_no_config(id)
    missing = launch.missing_prices(cfg)
    if not missing:
        return check(id, "ok", "conductor and steward models are priced")
    models = sorted({m for _, m in missing})
    return check(id, "fail", "unpriced launch model: " + ", ".join(models),
                 detail="; ".join(f"{role}: {model}" for role, model in missing)
                        + " (conductor-launch / steward-launch exit 1)",
                 fix="; ".join(f"set agentic.conductor.prices.{m} (a model prefix) in config.yaml"
                               for m in models))


def check_agentic_dirs(cfg) -> dict:
    """`agentic.dirs`: the agentic and mailbox dirs (or their nearest existing parents) are
    writable."""
    from .agentic import mailbox, store
    id = "agentic.dirs"
    if cfg is None:
        return _skip_no_config(id)
    dirs = [store.agentic_dir(cfg), mailbox.mailbox_dir(cfg)]
    bad = []
    for d in dirs:
        near = _nearest_existing(d)
        if near is None or not near.is_dir() or not _writable(near):
            bad.append(f"{d} (nearest existing: {near})")
    if bad:
        return check(id, "fail", "agentic dirs not writable", detail="; ".join(bad),
                     fix="make the data dir writable, or set NW_DATA_DIR")
    return check(id, "ok", str(dirs[0]))


def _active_campaigns(cfg) -> list:
    from .agentic import store
    return store.list_campaigns(cfg)


def check_runner(cfg, *, now=None) -> dict:
    """`agentic.runner`: the queue runner's lease is held and fresh when campaigns need it."""
    from .agentic import lease
    id = "agentic.runner"
    if cfg is None:
        return _skip_no_config(id)
    st = lease.lease_status(cfg, now=now)
    if st["held"] and st["stale"]:
        return check(id, "warn", f"runner lease is stale (heartbeat {st['heartbeat_age_s']} s ago)",
                     detail=f"pid {st['pid']} on {st['host']}; ttl {st['ttl_s']} s",
                     fix="nw queue status; restart the runner with `nw queue run --detach`")
    if st["held"]:
        return check(id, "ok", f"runner pid {st['pid']} on {st['host']}",
                     detail=f"heartbeat {st['heartbeat_age_s']} s ago")
    active = _active_campaigns(cfg)
    if active:
        return check(id, "warn", f"{len(active)} active campaign(s) and no runner",
                     fix="nw queue run --detach")
    return check(id, "skip", "no campaigns")


def _unguarded_launch(sessions):
    """The newest registered conductor record when it is a `source: launch` one whose launch
    lacked the AskUserQuestion deny (older records carry no argv fields, so they count as
    lacking it; a `--resume` relaunch refreshes the field), else None. A newer hand-started
    conductor (follower or manual record) is guarded by the hook instead."""
    last = sessions[-1] if sessions else None
    if last and last.get("source") == "launch" \
            and "AskUserQuestion" not in (last.get("disallowed_tools") or []):
        return last
    return None


def check_conductor(cfg) -> dict:
    """`agentic.conductor`: a conductor session is registered (SPEC-0299)."""
    from .agentic import conductor_cost
    id = "agentic.conductor"
    if cfg is None:
        return _skip_no_config(id)
    sessions = conductor_cost.sessions(cfg)
    unguarded = _unguarded_launch(sessions)
    if unguarded:   # SPEC-0382: a hand-started conductor is guarded by the hook instead
        return check(id, "warn", "the launched conductor session can call AskUserQuestion",
                     detail=f"session: {unguarded['session_id']}",
                     fix="nw queue conductor-launch --resume")
    if sessions:
        return check(id, "ok", f"{len(sessions)} conductor session(s) registered",
                     detail=f"newest: {sessions[-1]['session_id']}")
    if _active_campaigns(cfg):
        return check(id, "warn", "campaigns exist but no conductor session is registered",
                     fix="nw queue conductor-launch")
    return check(id, "skip", "no campaigns")


def check_workers(cfg) -> dict:
    """`agentic.workers`: the worker registry parses and ssh has a ControlPath dir (SPEC-0337).
    Static only: `nw workers check` does the live SSH probe."""
    from .agentic import workers
    id = "agentic.workers"
    if cfg is None:
        return _skip_no_config(id)
    p = workers.path(cfg)
    if not p.exists():
        return check(id, "skip", "no workers.json")
    try:
        recs = workers.read_strict(cfg)
    except (OSError, ValueError) as e:
        return check(id, "fail", f"{p} does not parse", detail=str(e),
                     fix=f"fix or remove {p} (`nw workers list` shows what is registered)")
    if not recs:
        return check(id, "skip", "no workers registered")
    enabled = sum(1 for r in recs if r.get("enabled") is not False)
    counts = f"{enabled}/{len(recs)} workers enabled"
    d = workers.ssh_dir(cfg)
    near = _nearest_existing(d) if d is not None else None
    if d is None or near is None or not near.is_dir() or not _writable(near):
        return check(id, "warn", f"{counts}; no usable ssh ControlPath dir",
                     detail=f"candidate: {d}; ssh runs without connection sharing",
                     fix="set XDG_RUNTIME_DIR, or shorten NW_DATA_DIR")
    return check(id, "ok", counts,
                 detail=f"ControlPath dir {d}; `nw workers check` probes them over SSH")


# ---- projects ------------------------------------------------------------------------------

def project_roots(cfg, only=None) -> tuple:
    """`([(name, root)], [(name, error)])` for registered projects with a research root (`only`
    limits to one). A project whose root fails to resolve (Meta-Tools under an invalid
    `nw_checkout:`) goes to the error list instead of failing the whole listing."""
    from .rules import registered_projects, resolve_research_context
    roots, errors = [], []
    for name in registered_projects(cfg):
        if only is not None and name != only:
            continue
        try:
            root = (resolve_research_context(cfg, name) or {}).get("root")
        except Exception as e:
            errors.append((name, f"{type(e).__name__}: {e}"))
            continue
        if root:
            roots.append((name, Path(os.path.expanduser(root))))
    return roots, errors


def pr_projects(cfg, roots) -> list:
    """Names of projects whose `.nw/project.yaml` merges by PR (unloadable configs ignored)."""
    from .agentic import project as P
    out = []
    for name, root in roots:
        try:
            doc = P.load_project_config(P.config_path(root))
        except (ValueError, OSError):
            continue
        if (doc.get("merge") or {}).get("policy") in PR_POLICIES:
            out.append(name)
    return out


def check_project_config(cfg, name, root) -> dict:
    """`project.<Name>.config`: `.nw/project.yaml` loads, validates and has loop + close gates."""
    from .agentic import project as P
    id = f"project.{name}.config"
    root = Path(root)
    if not root.is_dir():
        return check(id, "fail", f"registered root {root} is missing",
                     fix=f"nw projects add {name} --repo <path> --yes")
    path = P.config_path(root)
    if not path.is_file():
        return check(id, "skip", "no .nw/project.yaml (outbox-only project)")
    try:
        doc = P.project_config(cfg, name)
    except ValueError as e:
        return check(id, "fail", f"{path} is invalid", detail=str(e), fix=f"fix {path}")
    loop, close = P.gates_for(doc, "loop"), P.gates_for(doc, "close")
    empty = [tier for tier, gates in (("loop", loop), ("close", close)) if not gates]
    if empty:
        return check(id, "warn", "no gates for " + " / ".join(empty),
                     detail=f"{path}: campaigns {'/'.join(empty)} without any check",
                     fix=f"add gates to {path}")
    return check(id, "ok", f"{len(loop)} loop / {len(close)} close gates")


def check_project_profiles(cfg, name, root) -> dict:
    """`project.<Name>.profiles`: every visible profile and the default resolve (SPEC-0237)."""
    from .agentic import profiles
    from .agentic import project as P
    id = f"project.{name}.profiles"
    if not P.config_path(root).is_file():
        return check(id, "skip", "no .nw/project.yaml")
    results = profiles.check_all(cfg, project_root=Path(root))
    bad = [r for r in results if not r["ok"]]
    if bad:
        return check(id, "fail", f"{len(bad)} of {len(results)} profiles do not resolve",
                     detail="; ".join(r["error"] for r in bad),
                     fix=f"nw profiles check --project {name}")
    return check(id, "ok", f"{len(results)} profiles resolve")


# ---- legacy / retention --------------------------------------------------------------------

def check_legacy_wt() -> dict:
    """`legacy.wt_dirs`: no pre-rename `wt` config / data dirs left behind (SPEC-0330)."""
    from . import migrate
    found = [d for d in (migrate.legacy_config_dir(), migrate.legacy_data_dir())
             if migrate._nonempty(Path(d))]
    if found:
        return check("legacy.wt_dirs", "warn", "pre-rename wt dirs present",
                     detail="; ".join(found), fix="nw migrate-from-wt")
    return check("legacy.wt_dirs", "ok", "no wt dirs")


def _newest_history_day(history_file):
    newest = None
    p = Path(history_file)
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            day = dt.date.fromisoformat(json.loads(line)["day"])
        except (ValueError, KeyError, TypeError):
            continue
        newest = day if newest is None or day > newest else newest
    return newest


def check_retention(history_file, *, settings_path=None, today=None) -> dict:
    """`retention.cleanup_period`: Claude Code keeps transcripts long enough for `nw snapshot`."""
    id = "retention.cleanup_period"
    settings_path = Path(settings_path or Path.home() / ".claude" / "settings.json")
    days, note = CLAUDE_DEFAULT_CLEANUP_DAYS, "cleanupPeriodDays unset (Claude default 30)"
    if settings_path.is_file():
        try:
            raw = json.loads(settings_path.read_text(encoding="utf-8")).get("cleanupPeriodDays")
        except (ValueError, AttributeError, OSError):     # OSError: e.g. chmod 000
            raw, note = None, f"{settings_path} does not read / parse; assuming 30"
        if isinstance(raw, int) and not isinstance(raw, bool):
            days, note = raw, f"cleanupPeriodDays {raw}"
    if days >= RETENTION_SAFE_DAYS:
        return check(id, "ok", note)
    today = today or dt.date.today()
    newest = _newest_history_day(history_file)
    if newest is not None and (today - newest).days < days - RETENTION_MARGIN_DAYS:
        return check(id, "ok", f"{note}; newest snapshot {newest}")
    seen = f"newest snapshot {newest}" if newest else "no snapshot yet"
    return check(id, "warn", f"{note}; {seen}",
                 detail="Claude Code deletes transcripts older than cleanupPeriodDays; "
                        "unsnapshotted days are lost",
                 fix=f'set "cleanupPeriodDays": {RETENTION_SAFE_DAYS} (or more) in {settings_path}, '
                     "or run `nw snapshot` regularly")


# ---- run -----------------------------------------------------------------------------------

def _safe(id, fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except Exception as e:      # a doctor bug must not hide the other checks
        return check(id, "fail", f"check crashed: {type(e).__name__}", detail=str(e),
                     fix="report this as a nightwright bug")


def run(cfg, *, project=None, config_error=None, which=None, run_cmd=None,
        shell_env=None, skills_dest=None) -> list:
    """Every check in a fixed order. `cfg` is None when config.yaml did not load
    (`config_error` says why); the checks that need it are then `skip`."""
    from . import paths
    which = which or shutil.which
    run_cmd = run_cmd or (lambda *a, **k: subprocess.run(*a, **k))
    shell_env = os.environ.get("SHELL") if shell_env is None else shell_env
    out = []
    for name in ("uv", "git", "claude"):
        out.append(_safe(f"tools.{name}", check_tool, name, which=which, run_cmd=run_cmd))
    out.append(_safe("tools.gh", check_gh, which=which, run_cmd=run_cmd))
    roots, root_errors, listing = [], [], None
    if cfg is None:
        out.append(_skip_no_config("tools.gh_auth"))
    else:
        try:
            roots, root_errors = project_roots(cfg, project)
        except Exception as e:      # e.g. an unparseable mappings.yaml
            listing = check("install.projects", "fail", "could not list projects",
                            detail=f"{type(e).__name__}: {e}",
                            fix="fix mappings.yaml / outbox_targets, then `nw projects`")
            out.append(check("tools.gh_auth", "skip", "projects could not be listed"))
        else:
            out.append(_safe("tools.gh_auth", lambda: check_gh_auth(
                pr_projects(cfg, roots), which=which, run_cmd=run_cmd)))
    out.append(_safe("install.nw_on_path", check_nw_on_path, which=which))
    out.append(_safe("install.checkout", check_checkout, cfg) if cfg is not None
               else _skip_no_config("install.checkout"))
    if listing is not None:
        out.append(listing)
    out.append(_safe("config.parse", check_config_parse, config_error))
    out.append(_safe("config.timezone", check_timezone, cfg))
    out.append(_safe("config.org_ideas_file", check_org_ideas_file, cfg))
    def data_dir():         # computed inside _safe: a bad config value must not escape run()
        return (cfg or {}).get("data_dir") or paths.data_dir()
    out.append(_safe("data.dir", lambda: check_data_dir(data_dir())))
    out.append(_safe("skills.installed", check_skills, cfg, dest=skills_dest) if cfg is not None
               else _skip_no_config("skills.installed"))
    out.append(_safe("completion.installed", check_completion, shell_env))
    out.append(_safe("agentic.prices", check_prices, cfg))
    out.append(_safe("agentic.dirs", check_agentic_dirs, cfg))
    out.append(_safe("agentic.runner", check_runner, cfg))
    out.append(_safe("agentic.conductor", check_conductor, cfg))
    out.append(_safe("agentic.workers", check_workers, cfg))
    for name, root in roots:
        out.append(_safe(f"project.{name}.config", check_project_config, cfg, name, root))
        out.append(_safe(f"project.{name}.profiles", check_project_profiles, cfg, name, root))
    for name, err in root_errors:
        out.append(check(f"project.{name}.config", "fail", "research root did not resolve",
                         detail=err, fix="see install.checkout / `nw projects`"))
        out.append(check(f"project.{name}.profiles", "skip", "research root did not resolve"))
    known = {n for n, _ in roots} | {n for n, _ in root_errors}
    if project and listing is None and cfg is not None and project not in known:
        why = "no research root (bucket / outbox target without a repo path); nothing to check"
        out.append(check(f"project.{project}.config", "skip", why))
        out.append(check(f"project.{project}.profiles", "skip", why))
    out.append(_safe("legacy.wt_dirs", check_legacy_wt))
    out.append(_safe("retention.cleanup_period", lambda: check_retention(os.path.join(
        data_dir(), (cfg or {}).get("history_file") or "history.jsonl"))))
    return out


def payload(checks) -> dict:
    """The `nw.doctor.v1` document for `checks`."""
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in STATUSES}
    return {"schema": SCHEMA, "ok": counts["fail"] == 0, "counts": counts, "checks": list(checks)}
