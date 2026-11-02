"""`nw personas list|show|validate` (SPEC-0371): the builtin personas plus the ones declared in
the user config dir and in a project's `.nw/personas/`. The group is registered by
`nightwright.agentic.cli.register`."""
from __future__ import annotations

import click
from rich.table import Table
from rich.text import Text

from .. import completion as C
from ..console import console
from ..report import _dump_json
from . import persona_defs as PD
from . import personas as PS
from .cli_profiles import _header, _project


@click.group("personas")
def personas_group():
    """Personas (SPEC-0371): builtin and declared ones, and whether the declarations are valid.

    \b
    nw personas list                   every persona visible from the project
    nw personas show NAME              role, schema, layer and template of one persona
    nw personas validate               exit 1 when any declared persona is broken
    """


def _builtin_role(name):
    return "reviewer" if name in PS.REVIEWER_PERSONAS else \
        {"implementer": "implementer", "gate_triager": "triager"}.get(name)


def _rows(cfg, root) -> list:
    """Builtin rows then declared ones (valid or not), each a dict."""
    rows = [{"name": n, "layer": "builtin", "role": _builtin_role(n), "category": None,
             "read_only": p.read_only, "template": str(PS._TEMPLATES_DIR / p.template_file),
             "description": "", "ok": True, "problems": []} for n, p in PS.PERSONAS.items()]
    for d in PD.scan(root, user_dir=PD.user_personas_dir(cfg)):
        if d.path.is_dir():                     # an unreadable directory
            rows.append({"name": f"({d.layer})", "layer": d.layer, "role": None,
                         "category": None, "read_only": None, "template": None,
                         "description": "", "ok": False, "problems": d.problems})
            continue
        rows.append({"name": d.name, "layer": d.layer, "role": d.role,
                     "category": list(d.categories) or None,
                     "read_only": d.persona.read_only if d.persona else None,
                     "template": str(d.template_path) if d.template_path else None,
                     "description": d.description, "ok": d.ok, "problems": d.problems})
    return rows


def _row_status(r) -> Text:
    return Text("ok", style="green") if r["ok"] else Text("invalid", style="red")


@personas_group.command("list")
@click.option("--project", help="project whose .nw/personas layer applies (default: the cwd's)",
              shell_complete=C.complete_project)
@click.option("--json", "as_json", is_flag=True, help="emit nw.personas.v1 JSON")
@click.pass_obj
def personas_list(cfg, project, as_json):
    """List every persona with its layer and role; broken declarations are marked invalid.

    Exits 0 even with broken declarations; `nw personas validate` is the gate.
    """
    project, root = _project(cfg, project)
    rows = _rows(cfg, root)
    if as_json:
        _dump_json({"schema": "nw.personas.v1", "project": project, "personas": rows})
        return
    _header(project, root)
    table = Table(show_header=True, header_style="bold")
    for col in ("name", "layer", "role", "read-only", "category", "status"):
        table.add_column(col, no_wrap=col != "category", min_width=len(col))
    for r in rows:
        table.add_row(r["name"], r["layer"], r["role"] or "-",
                      "-" if r["read_only"] is None else ("yes" if r["read_only"] else "no"),
                      ", ".join(r["category"]) if r["category"] else "-", _row_status(r))
    console.print(table)
    for r in rows:
        for msg in r["problems"]:
            console.print(Text(msg, style="red"), highlight=False)


@personas_group.command("show")
@click.argument("name", shell_complete=C.complete_persona)
@click.option("--project", help="project whose .nw/personas layer applies (default: the cwd's)",
              shell_complete=C.complete_project)
@click.option("--json", "as_json", is_flag=True, help="emit nw.persona.show.v1 JSON")
@click.pass_obj
def personas_show(cfg, name, project, as_json):
    """Show persona NAME: layer, role, read-only, categories and template path."""
    project, root = _project(cfg, project)
    row = next((r for r in _rows(cfg, root) if r["name"] == name), None)
    if row is None:
        raise click.ClickException(f"unknown persona {name!r}")
    if as_json:
        _dump_json({"schema": "nw.persona.show.v1", "project": project, "persona": row})
        return
    _header(project, root)
    console.print(f"[bold]{name}[/]  {row['description']}", highlight=False)
    for key in ("layer", "role", "read_only", "category", "template"):
        value = row[key]
        if isinstance(value, list):
            value = ", ".join(value)
        console.print(f"{key.replace('_', '-'):<10} {'-' if value is None else value}",
                      highlight=False)
    console.print(Text("status     ok" if row["ok"] else "status     invalid",
                       style="green" if row["ok"] else "red"), highlight=False)
    for msg in row["problems"]:
        console.print(Text(msg, style="red"), highlight=False)


@personas_group.command("validate")
@click.option("--project", help="project whose .nw/personas layer applies (default: the cwd's)",
              shell_complete=C.complete_project)
@click.option("--json", "as_json", is_flag=True, help="emit nw.personas.validate.v1 JSON")
@click.pass_context
def personas_validate(ctx, project, as_json):
    """Check every declared persona (user layer, then the project's); exit 1 on any problem."""
    cfg = ctx.obj
    project, root = _project(cfg, project)
    declared = PD.scan(root, user_dir=PD.user_personas_dir(cfg))
    problems = [m for d in declared for m in d.problems]
    if as_json:
        _dump_json({"ok": not problems, "problems": problems,
                    "personas": [{"name": d.name, "layer": d.layer, "role": d.role,
                                  "ok": d.ok} for d in declared if not d.path.is_dir()]})
    else:
        _header(project, root)
        for msg in problems:
            console.print(Text(msg, style="red"), highlight=False)
        if not problems:
            console.print("[green]ok[/]", highlight=False)
    if problems:
        ctx.exit(1)
