"""`nw profiles list|show|check` (SPEC-0238): every workflow profile visible from a project, the
value each key resolves to and the file that set it. The group is registered by
`nightwright.agentic.cli.register`."""
from __future__ import annotations

import json

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .. import completion as C
from ..console import console
from ..report import _dump_json
from . import profiles as PR
from . import project as P


@click.group("profiles")
def profiles_group():
    """Workflow profiles (SPEC-0238): what each resolves to and which file set each value.

    \b
    nw profiles list                   every profile visible from the project, ok or broken
    nw profiles show NAME              each resolved value next to the file that set it
    nw profiles check                  exit 1 when any profile (or the default) fails
    """


def _project(cfg, project):
    """`(name, root)` of the project layer: `--project`, else the cwd's project, else none."""
    try:
        if project:
            return project, P.project_root(cfg, project)
        name = P.find_project_for_cwd(cfg)
        return (name, P.project_root(cfg, name)) if name else (None, None)
    except ValueError as e:
        raise click.ClickException(str(e))


def _default(cfg) -> str:
    return PR._agentic(cfg).get("default_profile") or "balanced"


def _header(project, root):
    where = f"{project} ({root})" if project else "- (no project layer: not in a known project repo)"
    console.print(f"[dim]project {escape(where)}[/]", highlight=False)


def _first_error(msg: str) -> str:
    return msg.split("; ")[0]


def _scope_of(layers, name) -> str:
    return next((layer.scope for layer in reversed(layers) if name in layer.defs), "-")


def _entry(cfg, layers, name, default) -> dict:
    top = next((layer.defs[name] for layer in reversed(layers) if name in layer.defs), None)
    out = {"name": name, "scope": _scope_of(layers, name), "default": name == default,
           "extends": (top.doc.get("extends") if top else None), "sources": [], "ok": True,
           "error": None, "slots": None, "loop_max": None, "campaign_cap_usd": None,
           "implementer": None, "reviewers": None, "description": ""}
    try:
        prof = PR._resolve(layers, name)
    except ValueError as e:
        return {**out, "ok": False, "error": str(e)}
    impl = (prof.get("personas") or {}).get("implementer") or {}
    return {**out, "sources": prof["sources"], "slots": prof["slots"],
            "loop_max": prof["loop_max"], "campaign_cap_usd": prof["campaign_cap_usd"],
            "implementer": "/".join(str(x) for x in (impl.get("model"), impl.get("effort")) if x),
            "reviewers": list(prof["reviewers"]), "description": prof["description"]}


def _cap(value) -> str:
    return "-" if value is None else f"${value:g}"


def _value_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "[]"
    return str(value)


def _lookup(profile, path):
    cur = profile
    for part in path.split("."):
        cur = cur[part]
    return cur


@profiles_group.command("list")
@click.option("--project", help="project whose .nw/profiles layer applies (default: the cwd's)",
              shell_complete=C.complete_project)
@click.option("--json", "as_json", is_flag=True, help="emit nw.profiles.v1 JSON")
@click.pass_obj
def profiles_list(cfg, project, as_json):
    """List every profile with its scope, key settings and ok / error status.

    Exits 0 even with broken profiles; `nw profiles check` is the gate.
    """
    project, root = _project(cfg, project)
    layers = PR.load_layers(cfg, project_root=root)
    default = _default(cfg)
    entries = [_entry(cfg, layers, n, default) for n in PR._known(layers)]
    if as_json:
        _dump_json({"schema": "nw.profiles.v1", "project": project, "default": default,
                    "profiles": [{k: e[k] for k in ("name", "scope", "sources", "ok", "error",
                                                    "slots", "loop_max", "campaign_cap_usd",
                                                    "reviewers", "description")}
                                 for e in entries]})
        return
    _header(project, root)
    table = Table(show_header=True, header_style="bold")
    for col in ("name", "from", "extends", "slots", "loops", "cap", "implementer", "reviewers",
                "status"):
        table.add_column(col, no_wrap=col not in ("reviewers", "status"), min_width=len(col))
    for e in entries:
        status = Text("ok", style="green") if e["ok"] else \
            Text(_first_error(e["error"]), style="red", overflow="fold")
        table.add_row(e["name"] + (" *" if e["default"] else ""), e["scope"], e["extends"] or "-",
                      "-" if e["slots"] is None else str(e["slots"]),
                      "-" if e["loop_max"] is None else str(e["loop_max"]),
                      _cap(e["campaign_cap_usd"]) if e["ok"] else "-", e["implementer"] or "-",
                      ", ".join(r.removesuffix("_reviewer") for r in e["reviewers"])
                      if e["reviewers"] else "-", status)
    console.print(table)
    console.print(f"[dim]* = agentic.default_profile ({default})[/]", highlight=False)


@profiles_group.command("show")
@click.argument("name", shell_complete=lambda ctx, param, inc: [
    n for n in PR.known_profiles((ctx.obj or {}), project_root=None) if n.startswith(inc)])
@click.option("--project", help="project whose .nw/profiles layer applies (default: the cwd's)",
              shell_complete=C.complete_project)
@click.option("--json", "as_json", is_flag=True, help="emit nw.profile.show.v1 JSON")
@click.pass_obj
def profiles_show(cfg, name, project, as_json):
    """Show every resolved value of profile NAME next to the file that set it."""
    project, root = _project(cfg, project)
    try:
        profile = PR.resolve_profile(cfg, name, project_root=root)
        prov = PR.profile_provenance(cfg, name, project_root=root)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json({"schema": "nw.profile.show.v1", "name": name, "project": project,
                    "profile": profile, "provenance": prov, "sources": profile["sources"]})
        return
    _header(project, root)
    console.print(f"[bold]{name}[/]  {profile['description']}", highlight=False)
    table = Table(show_header=True, header_style="bold")
    table.add_column("key", no_wrap=True)
    table.add_column("value", overflow="fold")
    table.add_column("source", no_wrap=True)
    for path, origin in prov.items():
        table.add_row(path, _value_text(_lookup(profile, path)),
                      PR.short_origin(cfg, origin, project=project))
    console.print(table)
    console.print("sources (lowest first): " + " → ".join(
        PR.short_origin(cfg, o, project=project) for o in profile["sources"]), highlight=False)


@profiles_group.command("check")
@click.option("--project", help="project whose .nw/profiles layer applies (default: the cwd's)",
              shell_complete=C.complete_project)
@click.option("--json", "as_json", is_flag=True, help="emit nw.profiles.check.v1 JSON")
@click.pass_context
def profiles_check(ctx, project, as_json):
    """Resolve every profile and `agentic.default_profile`; exit 1 when any fails."""
    cfg = ctx.obj
    project, root = _project(cfg, project)
    results = PR.check_all(cfg, project_root=root)
    ok = all(r["ok"] for r in results)
    if as_json:
        _dump_json({"schema": "nw.profiles.check.v1", "ok": ok, "results": results})
    else:
        _header(project, root)
        for r in results:
            if r["ok"]:
                console.print(f"[green]✓[/] {r['name']}", highlight=False)
            else:
                console.print(Text(f"✗ {r['name']}: {r['error']}", style="red"), highlight=False)
    if not ok:
        ctx.exit(1)
