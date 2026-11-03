"""`nw personas list|show|validate` (SPEC-0370, SPEC-0371): the persona registry across its builtin,
user and project layers, with the file each definition came from, and the check of declared
personas. Registered by `nightwright.agentic.cli.register`."""
from __future__ import annotations

from pathlib import Path

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .. import completion as C
from ..console import console
from ..report import _dump_json
from . import persona_defs as PD
from . import persona_registry as R
from . import personas as PS
from .cli_profiles import _first_error, _header, _project


@click.group("personas")
def personas_group():
    """Persona registry (SPEC-0370): every persona definition and the file it came from.

    \b
    nw personas list                   every definition across layers, ok or broken
    nw personas show NAME              all resolved fields, the template and any problems
    nw personas validate               exit 1 when any declared persona is broken
    """


def _registry(cfg, project):
    """The registry with SPEC-0371's declared-persona rules (role placeholder subsets, the
    user/project duplicate rule) applied on top of the loader's own checks to user/project
    entries, so `list`/`show` agree with `validate`."""
    project, root = _project(cfg, project)
    try:
        reg = R.load_registry(cfg, project_root=root, known=PS.KNOWN)
    except ValueError as e:
        raise click.ClickException(str(e))
    declared = {(d.layer, d.path.name): d
                for d in PD.scan(root, user_dir=PD.user_personas_dir(cfg)) if not d.path.is_dir()}
    for e in reg.entries:
        d = declared.get((e.scope, Path(e.path).name))
        if d is not None and d.problems and not e.errors:
            e.errors = list(d.problems)
        if d is not None and d.persona is not None and not e.errors:
            e.declared = d.persona.declared
    return project, root, reg


def _declared_fields(entry) -> dict:
    """SPEC-0394: `settings` (given keys only), `required` / `when` for a reviewer and
    `template_sha256` of a valid user/project entry; nothing for a builtin or a broken one."""
    decl = entry.declared
    if decl is None:
        return {}
    out = {"settings": dict(decl["settings"])}
    if decl["required"] is not None:
        out["required"] = decl["required"]
        out["when"] = {k: list(v) for k, v in decl["when"].items()}
    out["template_sha256"] = decl["template_sha256"]
    return out


def _adopt_text(adopt) -> str:
    return "-" if adopt is None else f"{adopt[0]} {list(adopt[1])}"


def _text(value):
    """A raw YAML value as display text: a string as is, any other value (int, list, date) via
    `repr`, so a typo in a broken file cannot crash the table or the JSON dump."""
    return value if value is None or isinstance(value, str) else repr(value)


def _summary(entry, active) -> dict:
    d = entry.defn
    return {"name": entry.name, "scope": entry.scope, "layer": entry.scope,
            "origin": entry.origin, "active": active, "ok": not entry.errors,
            "role": d.role if d else _text(entry.doc.get("role")),
            "reviewer": bool(d and d.role == "reviewer"),
            "read_only": d.read_only if d else None,
            "category": list(d.category) if d and d.category else None,
            "template": d.template if d else _text(entry.doc.get("template")),
            "description": d.description if d else None,
            "error": entry.errors[0] if entry.errors else None, **_declared_fields(entry)}


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
    for col in ("name", "role", "from", "reviewer", "category", "template", "origin", "status"):
        table.add_column(col, no_wrap=col in ("name", "category", "template"), min_width=len(col))
    for r in rows:
        status = Text("ok" if r["active"] else "ok (overridden)", style="green") if r["ok"] \
            else Text("invalid: " + _first_error(r["error"]), style="red", overflow="fold")
        table.add_row(Text(r["name"]), Text(r["role"] or "-"), r["scope"],
                      "yes" if r["reviewer"] else "-",
                      Text(", ".join(r["category"]) if r["category"] else "-"),
                      Text(r["template"] or "-"),
                      Text(r["origin"]), status)
    console.print(table)


@personas_group.command("show")
@click.argument("name", shell_complete=C.complete_persona)
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
        decl = _declared_fields(entry)
        if decl:
            pairs.append(("settings", ", ".join(f"{k}={v}" for k, v in decl["settings"].items())
                          or "-"))
            if "required" in decl:
                pairs += [("required", "true" if decl["required"] else "false"),
                          ("when", "; ".join(f"{k}: {', '.join(v)}"
                                             for k, v in decl["when"].items()) or "always")]
            pairs.append(("template_sha256", decl["template_sha256"]))
        if d.scope == "builtin":
            pairs += [("order", str(d.order)), ("output_schema", d.output_schema),
                      ("invariant", d.invariant), ("adopt", _adopt_text(d.adopt))]
    pairs.append(("status", "ok" if not entry.errors else "invalid"))
    for key, value in pairs:
        console.print(f"[bold]{key}[/]: {escape(str(value))}", highlight=False)
    if entry.errors:
        console.print("[red]problems:[/]", highlight=False)
        for e in entry.errors:
            console.print(f"  - {e}", highlight=False, markup=False)


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
