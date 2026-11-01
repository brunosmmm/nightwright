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
from .store import now_iso


@click.group("workers")
def workers_group():
    """Registry of remote gate workers (SPEC-0337), kept on the coordinator.

    This is not `nw worker` (SPEC-0336), the commands that run on the worker host itself.

    \b
    nw workers add box --ssh me@box    register a worker (runs `check` first)
    nw workers list                    name, destination, slots, enabled, cooldown, held
    nw workers check [NAME]            probe over SSH
    nw workers on|off NAME             enable (clears cooldown) / disable
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
              help="first word of the remote command (a forced command ignores it)")
@click.option("--no-check", is_flag=True, help="register without probing the worker")
@click.pass_obj
def workers_add(cfg, name, dest, slots, nw_cmd, no_check):
    """Register a worker. Refuses a duplicate name and, unless --no-check, an unreachable one."""
    if not WK.NAME_RE.fullmatch(name) or name == "local":
        raise click.UsageError(f"NAME must match {WK.NAME_RE.pattern} and not be 'local'")
    if not WK.DEST_RE.fullmatch(dest) or dest.startswith("-"):
        raise click.UsageError(f"--ssh must match {WK.DEST_RE.pattern} and not start with '-'")
    if not WK.NW_CMD_RE.fullmatch(nw_cmd) or nw_cmd.startswith("-"):
        raise click.UsageError(f"--nw must match {WK.NW_CMD_RE.pattern}")
    if any(r["name"] == name for r in _load_or_fail(cfg)):
        raise click.ClickException(f"worker {name!r} is already registered (use `rm` first)")
    rec = {"name": name, "ssh": dest, "slots": slots, "kinds": list(WK.DEFAULT_KINDS),
           "nw": nw_cmd, "enabled": True, "cooldown_until": None, "added_at": now_iso()}
    if not no_check:
        res = WK.check(cfg, rec)
        for w in res["warnings"]:
            warn(f"warning: {w}")
        if not res["ok"]:
            for e in res["errors"]:
                err_console.print(Text(f"error: {e}", style="red"))
            raise click.ClickException(f"{name} failed its check; nothing registered "
                                       "(--no-check to register anyway)")

    def add(records):
        if any(r["name"] == name for r in records):
            raise click.ClickException(f"worker {name!r} is already registered (use `rm` first)")
        records.append(rec)

    _update(cfg, add)
    _say(f"registered {name} ({dest}, {slots} slot{'s' if slots != 1 else ''})")


@workers_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="emit nw.workers.list.v1 JSON")
@click.pass_obj
def workers_list(cfg, as_json):
    """The registered workers, in preference order."""
    _load_or_fail(cfg)
    ws = WK.load(cfg)
    occ = GS.occupancy(cfg).get("workers") or {}
    rows = [{"name": w["name"], "ssh": w["ssh"], "slots": w["slots"], "kinds": w["kinds"],
             "enabled": w["enabled"], "cooldown_until": w["cooldown_until"]
             if WK.in_cooldown(w) else None,
             "held": len((occ.get(w["name"]) or {}).get("held") or [])} for w in ws]
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
        t.add_row(r["name"], r["ssh"], str(r["slots"]), "yes" if r["enabled"] else "no",
                  r["cooldown_until"] or "—", f"{r['held']}/{r['slots']}")
    console.print(t)


@workers_group.command("check")
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, help="emit a list of {name, ok, errors, "
              "warnings, info}")
@click.pass_obj
def workers_check(cfg, name, as_json):
    """Probe workers with `nw worker info --json` over SSH. Without NAME, every registered
    worker; exits 1 if any fails (a version or sha mismatch only warns)."""
    records = _load_or_fail(cfg)
    if name is not None:
        records = [_find(records, name)]
    results = [WK.check(cfg, r) for r in records]
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
    if not all(r["ok"] for r in results):
        raise SystemExit(1)


@workers_group.command("on")
@click.argument("name")
@click.pass_obj
def workers_on(cfg, name):
    """Enable a worker and clear its cooldown."""
    def fn(records):
        r = _find(records, name)
        r["enabled"] = True
        r["cooldown_until"] = None
    _update(cfg, fn)
    _say(f"{name}: enabled")


@workers_group.command("off")
@click.argument("name")
@click.pass_obj
def workers_off(cfg, name):
    """Disable a worker. Held slots are untouched; waiters keep the pools they started with."""
    def fn(records):
        _find(records, name)["enabled"] = False
    _update(cfg, fn)
    _say(f"{name}: disabled")


@workers_group.command("rm")
@click.argument("name")
@click.pass_obj
def workers_rm(cfg, name):
    """Unregister a worker. Held slots are untouched."""
    def fn(records):
        records.remove(_find(records, name))
    _update(cfg, fn)
    _say(f"{name}: removed")
