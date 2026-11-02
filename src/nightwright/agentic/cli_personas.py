"""`nw personas list|show` (SPEC-0370): the persona registry across its builtin, user and project
layers, with the file each definition came from. Registered by `nightwright.agentic.cli.register`."""
from __future__ import annotations

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .. import completion as C
from ..console import console
from ..report import _dump_json
from . import persona_registry as R
from . import personas as PS
from .cli_profiles import _first_error, _header, _project


@click.group("personas")
def personas_group():
    """Persona registry (SPEC-0370): every persona definition and the file it came from.

    \b
    nw personas list                   every definition across layers, ok or broken
    nw personas show NAME              all resolved fields, the template and any problems
    """


def _registry(cfg, project):
    project, root = _project(cfg, project)
    try:
        return project, root, R.load_registry(cfg, project_root=root, known=PS.KNOWN)
    except ValueError as e:
        raise click.ClickException(str(e))


def _adopt_text(adopt) -> str:
    return "-" if adopt is None else f"{adopt[0]} {list(adopt[1])}"


def _text(value):
    """A raw YAML value as display text: a string as is, any other value (int, list, date) via
    `repr`, so a typo in a broken file cannot crash the table or the JSON dump."""
    return value if value is None or isinstance(value, str) else repr(value)


def _summary(entry, active) -> dict:
    d = entry.defn
    return {"name": entry.name, "scope": entry.scope, "origin": entry.origin,
            "active": active, "ok": not entry.errors,
            "role": d.role if d else _text(entry.doc.get("role")),
            "reviewer": bool(d and d.role == "reviewer"),
            "template": d.template if d else _text(entry.doc.get("template")),
            "description": d.description if d else None,
            "error": entry.errors[0] if entry.errors else None}


def _detail(entry, active) -> dict:
    out = {**_summary(entry, active), "problems": list(entry.errors)}
    d = entry.defn
    if d:
        out.update({"template_path": str(d.template_path), "category": list(d.category),
                    "order": d.order, "output_schema": d.output_schema,
                    "read_only": d.read_only, "invariant": d.invariant,
                    "adopt": None if d.adopt is None
                    else {"step": d.adopt[0], "personas": list(d.adopt[1])},
                    "path": str(d.path)})
    return out


def _names_complete(ctx, param, incomplete):
    return [n for n in PS.PERSONAS if n.startswith(incomplete)]


@personas_group.command("list")
@click.option("--project", help="project whose .nw/personas layer applies (default: the cwd's)",
              shell_complete=C.complete_project)
@click.option("--json", "as_json", is_flag=True, help="emit nw.personas.v1 JSON")
@click.pass_obj
def personas_list(cfg, project, as_json):
    """List every persona definition with its scope, role, template and ok / error status.

    Exits 0 even with broken definitions (listing is not a check).
    """
    project, root, reg = _registry(cfg, project)
    winners = reg.resolved()
    rows = [_summary(e, winners[e.name] is e) for e in reg.entries]
    if as_json:
        _dump_json({"schema": "nw.personas.v1", "project": project, "personas": rows})
        return
    _header(project, root)
    table = Table(show_header=True, header_style="bold")
    for col in ("name", "role", "from", "reviewer", "template", "origin", "status"):
        table.add_column(col, no_wrap=col == "name", min_width=len(col))
    for r in rows:
        status = Text("ok" if r["active"] else "ok (overridden)", style="green") if r["ok"] \
            else Text(_first_error(r["error"]), style="red", overflow="fold")
        table.add_row(Text(r["name"]), Text(r["role"] or "-"), r["scope"],
                      "yes" if r["reviewer"] else "-", Text(r["template"] or "-"),
                      Text(r["origin"]), status)
    console.print(table)


@personas_group.command("show")
@click.argument("name", shell_complete=_names_complete)
@click.option("--project", help="project whose .nw/personas layer applies (default: the cwd's)",
              shell_complete=C.complete_project)
@click.option("--json", "as_json", is_flag=True, help="emit nw.persona.show.v1 JSON")
@click.pass_obj
def personas_show(cfg, name, project, as_json):
    """Show every resolved field of persona NAME, its template and any validation problems."""
    project, root, reg = _registry(cfg, project)
    winners = reg.resolved()
    entry = winners.get(name)
    if entry is None:
        raise click.ClickException(
            f"unknown persona {name!r}; known: {', '.join(sorted(winners))}")
    detail = _detail(entry, True)
    if as_json:
        _dump_json({"schema": "nw.persona.show.v1", "project": project, **detail})
        return
    _header(project, root)
    d = entry.defn
    pairs = [("name", entry.name), ("scope", entry.scope), ("origin", entry.origin),
             ("role", detail["role"] or "-"), ("reviewer", "yes" if detail["reviewer"] else "no")]
    if d:
        pairs += [("description", d.description), ("template", d.template),
                  ("template path", str(d.template_path)),
                  ("category", ", ".join(d.category) or "-"),
                  ("read_only", "true" if d.read_only else "false")]
        if d.scope == "builtin":
            pairs += [("order", str(d.order)), ("output_schema", d.output_schema),
                      ("invariant", d.invariant), ("adopt", _adopt_text(d.adopt))]
    for key, value in pairs:
        console.print(f"[bold]{key}[/]: {escape(str(value))}", highlight=False)
    if entry.errors:
        console.print("[red]problems:[/]", highlight=False)
        for e in entry.errors:
            console.print(f"  - {e}", highlight=False, markup=False)
