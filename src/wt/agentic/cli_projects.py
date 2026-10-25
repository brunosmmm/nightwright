"""`wt projects config|init` (SPEC-0170). `attach` is called by `wt.agentic.cli.register`
with the existing `projects` group, so the commands sit next to `add` / `rename` and share
the project completer."""
import json
import sys
from pathlib import Path

import click
import yaml

from . import project as P
from .. import completion as C
from ..console import console


def _project_or_cwd(cfg, project):
    if project:
        return project
    found = P.find_project_for_cwd(cfg)
    if not found:
        raise click.UsageError("no project given and the current directory is not a known "
                               "project's repo (see `wt projects --json`)")
    return found


def attach(projects_group):
    """Register the per-project config subcommands on `projects_group` (SPEC-0170)."""

    @projects_group.command("config")
    @click.argument("project", required=False, shell_complete=C.complete_project)
    @click.option("--json", "as_json", is_flag=True, help="emit wt.project.config.v1 JSON")
    @click.pass_obj
    def projects_config_cmd(cfg, project, as_json):
        """Show a project's resolved `.wt/project.yaml` (defaults filled).

        PROJECT defaults to the project whose repo contains the current directory.
        """
        try:
            project = _project_or_cwd(cfg, project)
            doc = P.project_config(cfg, project)
        except ValueError as e:
            raise click.ClickException(str(e))
        root, path = doc.pop("_root"), doc.pop("_path")
        if as_json:
            payload = {"schema": "wt.project.config.v1", "project": project, "root": root,
                       "path": path, "config": doc}
            json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
            sys.stdout.write("\n")
            return
        sys.stdout.write(f"# {path}\n")
        sys.stdout.write(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))

    @projects_group.command("init")
    @click.argument("project", required=False, shell_complete=C.complete_project)
    @click.option("--yes", "do_write", is_flag=True, help="write the file (default is dry-run)")
    @click.option("--force", is_flag=True, help="overwrite an existing .wt/project.yaml")
    @click.pass_obj
    def projects_init_cmd(cfg, project, do_write, force):
        """Write a starter `.wt/project.yaml` into the project's repo (dry-run by default)."""
        try:
            project = _project_or_cwd(cfg, project)
            root = P.project_root(cfg, project)
        except ValueError as e:
            raise click.ClickException(str(e))
        path = P.config_path(root)
        text = P.STARTER.format(project=project)
        if path.exists() and not force:
            raise click.ClickException(f"{path} already exists (pass --force to overwrite)")
        if not do_write:
            console.print(f"[dim]# would write {path}[/]")
            sys.stdout.write(text)
            console.print("[yellow]not written — re-run with --yes to commit[/]")
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        console.print(f"[green]✓[/] wrote {path}")

    return projects_group
