"""`nw workers` (SPEC-0337): the coordinator's registry of remote gate workers. The logic is in
`workers.py`; `nw worker` (SPEC-0336) is the other side, run on the worker host."""
from __future__ import annotations

import click
from rich.table import Table
from rich.text import Text

from ..console import console
from ..render import err_console, warn
from ..report import _dump_json
from . import gate_slots as GS
from . import workers as WK
from . import workers_bootstrap as WB
from . import workers_update as WU
from .store import now_iso

KINDS = ("gate", "persona")
RPS_MIN, RPS_MAX = 1, 16                        # remote_personas.{MIN,MAX}_PERSONA_SLOTS


@click.group("workers")
def workers_group():
    """Registry of remote gate workers (SPEC-0337), kept on the coordinator.

    This is not `nw worker` (SPEC-0336), the commands that run on the worker host itself.

    \b
    nw workers add box --ssh me@box    register a worker (runs `check` first)
    nw workers bootstrap box --ssh nw@box --install-key
                                       install nw, the forced key line, register (SPEC-0354)
    nw workers list                    name, destination, slots, enabled, cooldown, held
    nw workers check [NAME]            probe over SSH
    nw workers set NAME [--slots N] [--kinds gate,persona] [--persona-slots N]
                                       gate slots (SPEC-0390) / job kinds / persona slots
    nw workers on|off NAME             enable (clears cooldown) / disable
    nw workers clear-cooldown NAME     clear the cooldown only (enabled/drain untouched; SPEC-0387)
    nw workers drain NAME              no new gates; disabled once idle (SPEC-0343)
    nw workers update NAME [--force] [--dry-run]
                                       upgrade the worker to this checkout's build (SPEC-0385)
    nw workers update NAME --when-idle drain; the queue updates it once idle, then resumes (SPEC-0386)
    nw workers rm NAME                 unregister (held slots are untouched)
    """


def _say(msg: str, style: str = "") -> None:
    """A plain line on stdout; Text so worker names and ssh output are never read as markup."""
    console.print(Text(msg, style=style))


def _find(records, name):
    for r in records:
        if r.get("name") == name:
            return r
    raise click.ClickException(f"no worker named {name!r}")


def _load_or_fail(cfg) -> list:
    try:
        return WK.read_strict(cfg)
    except (OSError, ValueError) as e:
        raise click.ClickException(f"{WK.path(cfg)} is unreadable: {e}")


def _update(cfg, fn):
    try:
        return WK.update(cfg, fn)
    except (OSError, ValueError) as e:
        raise click.ClickException(str(e))


@workers_group.command("add")
@click.argument("name")
@click.option("--ssh", "dest", required=True, help="ssh destination, e.g. me@box")
@click.option("--slots", type=click.IntRange(WK.MIN_SLOTS, WK.MAX_SLOTS), default=1,
              show_default=True, help="gate slots on the worker")
@click.option("--nw", "nw_cmd", default="nw", show_default=True,
              help="first word of the remote command: `nw` or a path ending in /nw, as a forced "
                   "command requires (it runs its own nw whatever is sent)")
@click.option("--identity", "identity", default=None, metavar="PATH",
              help="absolute path of the worker's dedicated private key; every connection to "
                   "the worker then uses only that key (SPEC-0354)")
@click.option("--no-check", is_flag=True, help="register without probing the worker")
@click.pass_obj
def workers_add(cfg, name, dest, slots, nw_cmd, identity, no_check):
    """Register a worker. Refuses a duplicate name and, unless --no-check, an unreachable one."""
    if not WK.NAME_RE.fullmatch(name) or name == "local":
        raise click.UsageError(f"NAME must match {WK.NAME_RE.pattern} and not be 'local'")
    if not WK.DEST_RE.fullmatch(dest) or dest.startswith("-"):
        raise click.UsageError(f"--ssh must match {WK.DEST_RE.pattern} and not start with '-'")
    if not WK.NW_CMD_RE.fullmatch(nw_cmd) or nw_cmd.startswith("-"):
        raise click.UsageError(f"--nw must match {WK.NW_CMD_RE.pattern}")
    if identity is not None and not WK.identity_ok(identity):
        raise click.UsageError(f"--identity must be an absolute path matching "
                               f"{WK.IDENTITY_RE.pattern}")
    if any(r["name"] == name for r in _load_or_fail(cfg)):
        raise click.ClickException(f"worker {name!r} is already registered (use `rm` first)")
    rec = {"name": name, "ssh": dest, "slots": slots, "kinds": list(WK.DEFAULT_KINDS),
           "nw": nw_cmd, "enabled": True, "cooldown_until": None, "added_at": now_iso()}
    if identity is not None:
        rec["identity"] = identity
    if not no_check:
        res = WK.check(cfg, rec)
        for w in res["warnings"]:
            warn(f"warning: {w}")
        if not res["ok"]:
            for e in res["errors"]:
                err_console.print(Text(f"error: {e}", style="red"))
            raise click.ClickException(f"{name} failed its check; nothing registered "
                                       "(--no-check to register anyway)")
        if isinstance(res.get("info"), dict):   # SPEC-0341: the cached `info` (features)
            rec["info"] = res["info"]

    def add(records):
        if any(r["name"] == name for r in records):
            raise click.ClickException(f"worker {name!r} is already registered (use `rm` first)")
        records.append(rec)

    _update(cfg, add)
    _say(f"registered {name} ({dest}, {slots} slot{'s' if slots != 1 else ''})")


@workers_group.command("bootstrap")
@click.argument("name")
@click.option("--ssh", "dest", required=True, metavar="USER@HOST",
              help="the dedicated worker account, never root, e.g. nw@box")
@click.option("--slots", type=click.IntRange(WK.MIN_SLOTS, WK.MAX_SLOTS), default=1,
              show_default=True, help="gate slots on the worker")
@click.option("--install-key", is_flag=True,
              help="append the forced-command line for the worker key (agentic.workers.key) "
                   "to the worker's authorized_keys, then register with a check")
@click.option("--admin", metavar="USER@HOST", default=None,
              help="an account with passwordless sudo: creates the worker user when missing, "
                   "and every worker command runs through it as `sudo -n -i -u USER`")
@click.option("--reinstall", is_flag=True,
              help="rebuild and reinstall even when the worker reports this checkout's build")
@click.option("--dry-run", is_flag=True, help="print every step and command; run nothing")
@click.pass_obj
def workers_bootstrap(cfg, name, dest, slots, install_key, admin, reinstall, dry_run):
    """Install nightwright on a worker, install its dedicated key's forced-command line and
    register it (SPEC-0354). Steps already done are skipped; exits 1 if a step failed.

    The worker key is `agentic.workers.key` (default ~/.ssh/nw_worker_ed25519.pub), a key
    of its own: never the one your ssh login uses."""
    try:
        plan = WB.prepare(cfg, name, dest, slots, install_key, admin, reinstall)
    except WB.StepFailed as e:
        status = {s: ("not run", "") for s in WB.STEPS}
        status[getattr(e, "step", "install")] = ("failed", str(e))
        _bootstrap_table(name, status)
        raise SystemExit(1)
    if dry_run:
        for step, cmds in WB.dry_plan(plan).items():
            if cmds:
                _say(f"{step}:", "bold")
                for c in cmds:
                    console.print(Text(f"  {c}"), soft_wrap=True)
        _bootstrap_table(name, plan.status)
        return
    ok = WB.run(cfg, plan)
    _bootstrap_table(name, plan.status)
    for line in plan.notes:
        console.print(Text(line), soft_wrap=True)
    if not ok:
        raise SystemExit(1)


def _bootstrap_table(name, status) -> None:
    t = Table(title=f"bootstrap {name}")
    for col in ("step", "status", "detail"):
        t.add_column(col)
    style = {"done": "green", "failed": "bold red", "would run": "cyan"}
    for step in WB.STEPS:
        st, detail = status.get(step, ("not run", ""))
        t.add_row(step, Text(st, style=style.get(st, "dim" if st.startswith("not") else "")),
                  Text(detail))
    console.print(t)
    for step in WB.STEPS:                       # the table may truncate it: in full, unwrapped
        st, detail = status.get(step, ("not run", ""))
        if st == "failed":
            console.print(Text(f"error: {step}: {detail}", style="red"), soft_wrap=True)


@workers_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="emit nw.workers.list.v1 JSON")
@click.pass_obj
def workers_list(cfg, as_json):
    """The registered workers, in preference order."""
    _load_or_fail(cfg)
    ws = WK.load(cfg)
    occ = GS.occupancy(cfg).get("workers") or {}
    # a disabled worker is not draining, even with a stale `draining` key (a hand edit)
    drain = {w["name"] for w in ws if w["draining"] and w["enabled"]}
    jobs = WK.draining_jobs(cfg) if drain else {}
    rows = [{"name": w["name"], "ssh": w["ssh"], "slots": w["slots"], "kinds": w["kinds"],
             "enabled": w["enabled"], "cooldown_until": w["cooldown_until"]
             if WK.in_cooldown(w) else None,
             "held": len((occ.get(w["name"]) or {}).get("held") or []),
             "identity": w.get("identity"), "draining": w["name"] in drain,
             "updating": w["name"] in drain and bool(WK.update_pending(w)),
             "update_last": w.get("update_last"),
             "draining_jobs": jobs.get(w["name"], 0) if w["name"] in drain else None}
            for w in ws]
    if as_json:
        _dump_json({"schema": "nw.workers.list.v1", "workers": rows})
        return
    if not rows:
        console.print("[dim]no workers registered[/]")
        return
    t = Table(title="Workers")
    for col in ("name", "ssh", "slots", "enabled", "cooldown", "held"):
        t.add_column(col)
    for r in rows:
        enabled = "yes" if r["enabled"] else "no"
        if r["draining"]:
            enabled = (f"updating (draining, {r['draining_jobs']} jobs)" if r["updating"]
                       else f"draining ({r['draining_jobs']} jobs)")
        t.add_row(r["name"], r["ssh"], str(r["slots"]), enabled,
                  r["cooldown_until"] or "—", f"{r['held']}/{r['slots']}")
    console.print(t)


@workers_group.command("check")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="emit a list of {name, ok, errors, "
              "warnings, info}")
@click.option("--auth", is_flag=True, help="persona workers: also run a 1-token `claude -p` "
              "auth probe on the worker (costs money; SPEC-0344)")
@click.pass_obj
def workers_check(cfg, name, as_json, auth):
    """Probe workers with `nw worker info --json` over SSH. Without NAME, every registered
    worker; exits 1 if any fails (a version or sha mismatch only warns). A worker whose
    `kinds` contain `persona` must also have a working `claude` (SPEC-0344)."""
    from . import remote_personas as RPS
    records = _load_or_fail(cfg)
    if name is not None:
        records = [_find(records, name)]
    results = []
    for r in records:
        res = WK.check(cfg, r)
        w = WK.normalize(cfg, r)
        if "persona" in w["kinds"] and not res["errors"]:
            res["errors"] += RPS.claude_check(cfg, w, auth)
            res["ok"] = not res["errors"]
        results.append(res)
    if as_json:
        _dump_json(results)
    else:
        if not results:
            _say("no workers registered", "dim")
        for r in results:
            _say(f"{r['name']}: {'ok' if r['ok'] else 'FAILED'}",
                 "green" if r["ok"] else "bold red")
            for e in r["errors"]:
                _say(f"  error: {e}", "red")
            for w in r["warnings"]:
                _say(f"  warning: {w}", "yellow")
    if WK.auto_update(cfg):                      # SPEC-0386: schedule stale workers
        for r in results:
            if any(w.startswith("worker stale:") for w in r["warnings"]):
                try:
                    _schedule_update(cfg, r["name"])
                except click.ClickException as e:
                    _say(f"{r['name']}: auto_update: {e.message}", "yellow")
    if not all(r["ok"] for r in results):
        raise SystemExit(1)


@workers_group.command("set")
@click.argument("name")
@click.option("--slots", type=click.IntRange(WK.MIN_SLOTS, WK.MAX_SLOTS), default=None,
              help="gate slots on the worker; raising applies on the next tick, lowering "
                   "lets held slots drain (SPEC-0390)")
@click.option("--kinds", default=None, metavar="KIND[,KIND]",
              help="job kinds the worker takes: gate, persona (SPEC-0344)")
@click.option("--persona-slots", type=click.IntRange(RPS_MIN, RPS_MAX), default=None,
              help="concurrent persona jobs on the worker (default 1)")
@click.pass_obj
def workers_set(cfg, name, slots, kinds, persona_slots):
    """Change a registered worker's gate slots, job kinds or persona slots (SPEC-0344/0390).

    Only the named fields change: draining, a pending update, enabled and cooldown are kept.
    Lowering --slots never kills a running job: slots held beyond the new count drain
    naturally and are not granted again."""
    if slots is None and kinds is None and persona_slots is None:
        raise click.UsageError("nothing to set: give --slots, --kinds and/or --persona-slots")
    parsed = None
    if kinds is not None:
        parsed = [k.strip() for k in kinds.split(",") if k.strip()]
        bad = [k for k in parsed if k not in KINDS]
        if not parsed or bad:
            raise click.UsageError(f"--kinds must list some of {', '.join(KINDS)}"
                                   + (f" (not {', '.join(bad)})" if bad else ""))
        parsed = list(dict.fromkeys(parsed))

    old = []

    def fn(records):
        r = _find(records, name)
        old[:] = [WK.normalize(cfg, dict(r))["slots"]]
        if slots is not None:
            r["slots"] = slots
        if parsed is not None:
            r["kinds"] = parsed
        if persona_slots is not None:
            r["persona_slots"] = persona_slots
    _update(cfg, fn)
    w = WK.normalize(cfg, _find(_load_or_fail(cfg), name))
    from . import remote_personas as RPS
    was = f" (was {old[0]})" if slots is not None and old[0] != w["slots"] else ""
    _say(f"{name}: gate slots {w['slots']}{was}, kinds {','.join(w['kinds'])}, "
         f"persona slots {RPS.persona_slots(w)}")


@workers_group.command("on")
@click.argument("name")
@click.pass_obj
def workers_on(cfg, name):
    """Enable a worker and clear its cooldown (and a drain: SPEC-0343)."""
    def fn(records):
        r = _find(records, name)
        r["enabled"] = True
        r["cooldown_until"] = None
        r.pop("draining", None)
        r.pop("update_pending", None)
    _update(cfg, fn)
    _say(f"{name}: enabled")


@workers_group.command("clear-cooldown")
@click.argument("name")
@click.pass_obj
def workers_clear_cooldown(cfg, name):
    """Clear a worker's cooldown only; enabled, drain and update state stay as they are
    (SPEC-0387)."""
    def fn(records):
        _find(records, name)["cooldown_until"] = None
    _update(cfg, fn)
    _say(f"{name}: cooldown cleared")


@workers_group.command("off")
@click.argument("name")
@click.pass_obj
def workers_off(cfg, name):
    """Disable a worker now (clears a drain). Held slots are untouched; a waiter that then
    takes one of its slots runs the gate locally (SPEC-0343)."""
    def fn(records):
        r = _find(records, name)
        r["enabled"] = False
        r.pop("draining", None)
        r.pop("update_pending", None)
    _update(cfg, fn)
    _say(f"{name}: disabled")


class _DrainNoop(Exception):
    """Raised inside `drain`'s update so the registry is not rewritten."""


@workers_group.command("drain")
@click.argument("name")
@click.pass_obj
def workers_drain(cfg, name):
    """Take a worker out of service without losing its running jobs (SPEC-0343): no new gate
    is placed on it, jobs already on it (adopted ones included) finish there.

    The worker stays draining until a queue tick sees it idle and disables it.
    If no runner is ticking, it stays draining until one runs. `on` cancels the drain;
    `off` disables the worker at once."""
    def state(r):
        if r.get("enabled") is False:
            return f"{name}: already disabled; nothing to drain"
        if r.get("draining") is True:
            return f"{name}: already draining ({_jobs_on(cfg, name)} jobs)"
        return None

    rec = next((r for r in _load_or_fail(cfg) if r.get("name") == name), None)
    if rec is None:
        err_console.print(Text(f"no worker named {name}"))
        raise SystemExit(2)
    if (msg := state(rec)) is not None:
        _say(msg)
        return

    def fn(records):
        r = next((r for r in records if r.get("name") == name), None)
        if r is None:
            raise _DrainNoop(None)
        if (m := state(r)) is not None:
            raise _DrainNoop(m)
        r["draining"] = True
    try:
        _update(cfg, fn)
    except _DrainNoop as e:                     # changed under us: nothing written
        if e.args[0] is None:
            err_console.print(Text(f"no worker named {name}"))
            raise SystemExit(2)
        _say(e.args[0])
        return
    _say(f"{name}: draining ({_jobs_on(cfg, name)} jobs)")


def _schedule_update(cfg, name) -> None:
    """SPEC-0386: mark `name` draining for an update (`draining` + `update_pending`)."""
    def state(r):
        if r.get("enabled") is False:
            return f"{name}: disabled; nothing to drain (run `nw workers update {name}` now)"
        if WK.update_pending(r):
            return f"{name}: update already scheduled ({_jobs_on(cfg, name)} jobs)"
        return None

    def fn(records):
        r = _find(records, name)
        if (m := state(r)) is not None:
            raise _DrainNoop(m)
        WK.mark_update(r, now_iso())
    try:
        _update(cfg, fn)
    except _DrainNoop as e:
        _say(e.args[0])
        return
    _say(f"{name}: update scheduled, draining ({_jobs_on(cfg, name)} jobs); a queue tick "
         f"updates it once idle")


def _jobs_on(cfg, name) -> int:
    return WK.draining_jobs(cfg).get(name, 0)


@workers_group.command("rm")
@click.argument("name")
@click.pass_obj
def workers_rm(cfg, name):
    """Unregister a worker. Held slots are untouched."""
    def fn(records):
        records.remove(_find(records, name))
    _update(cfg, fn)
    _say(f"{name}: removed")


@workers_group.command("update")
@click.argument("name")
@click.option("--force", is_flag=True,
              help="install even with live jobs (they are not killed); passed to `nw worker "
                   "update --force`")
@click.option("--dry-run", is_flag=True,
              help="build the wheel, print the plan and its size; send nothing")
@click.option("--when-idle", is_flag=True,
              help="schedule instead: drain NAME now (no new gates), and when the queue sees it "
                   "idle it runs this update (never --force) and returns it to service "
                   "(SPEC-0386)")
@click.pass_obj
def workers_update(cfg, name, force, dry_run, when_idle):
    """Upgrade a worker to this checkout's build over its restricted key (SPEC-0385): builds a
    wheel and streams it to the worker's forced-command verb `nw worker update` (SPEC-0369),
    then runs `check`. Exits 0 updated or unchanged, 5 worker busy (live jobs), 1 failed.

    With --when-idle the queue does it for you: the worker drains (as `nw workers drain`), and
    a queue tick that finds it idle updates it, verifies the build id, and puts it back in
    service; on failure or after `agentic.workers.update_drain_timeout_h` (default 6) it is
    returned to service anyway and the conductor is told. Needs a ticking runner."""
    if when_idle:
        if force or dry_run:
            raise click.UsageError("--when-idle cannot be combined with --force or --dry-run")
        _schedule_update(cfg, name)
        return
    rec = _find(_load_or_fail(cfg), name)
    try:
        plan, res, code, err = WU.send(cfg, rec, force, dry_run)
    except WU.UpdateError as e:
        raise click.ClickException(str(e))
    _say(f"{name}: {plan.wheel} ({plan.size} bytes) build {plan.build_id[:12]}"
         + (" --force" if force else ""))
    if dry_run:
        _say(f"would run: {plan.shown()}")
        _say("dry run: nothing sent", "dim")
        return
    if res is None:
        why = err.splitlines()[-1] if err else "no result line"
        hint = (" (an older worker without the update verb? bootstrap it once)"
                if "not allowed" in err else "")
        _say(f"{name}: update failed (ssh exit {code}): {why}{hint}", "bold red")
        raise SystemExit(1)
    state = res.get("state")
    if state == "busy":
        jobs = ", ".join(res.get("live_jobs") or [])
        _say(f"{name}: " + WU.BUSY_MSG.format(name=name)
             + (f" (live: {jobs})" if jobs else ""), "bold yellow")
        raise SystemExit(WU.EXIT_BUSY)
    if state == "failed":
        _say(f"{name}: update failed at {res.get('stage')}: {res.get('error')}", "bold red")
        if res.get("stage") in ("install", "verify"):
            _say(f"  see {WU.UV_LOG} on the worker for uv's output", "red")
        raise SystemExit(1)
    _say(f"{name}: {state} (build {str(res.get('build_id_before'))[:14]} -> "
         f"{str(res.get('build_id_after'))[:14]}, version {res.get('version')})", "green")
    chk = WK.check(cfg, rec)
    got = (chk.get("info") or {}).get("build_id")
    if not chk["ok"] or got != plan.build_id:
        for e in chk["errors"]:
            _say(f"  error: {e}", "red")
        _say(f"{name}: check after update: build {got} != {plan.build_id}", "bold red")
        raise SystemExit(1)
    for w in chk["warnings"]:
        _say(f"  warning: {w}", "yellow")
    _say(f"{name}: check ok, build {got[:12]} matches", "green")
