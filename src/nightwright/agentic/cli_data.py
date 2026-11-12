"""`nw data usage [--json]` (SPEC-0580): the read-only per-source size / count / retention report
of the agentic data dir. The report itself is `data_usage.usage`; this module only renders it."""
from __future__ import annotations

import json

import click
from rich.table import Table

from ..console import console, stdout_is_tty


@click.group("data")
def data_group():
    """Agentic data dir (SPEC-0580): what is stored, how big, and what prunes it.

    \b
    nw data usage                      size, count, age and retention per data source
    nw data usage --json               the nw.data.usage.v1 document
    """


@data_group.command("usage")
@click.option("--json", "as_json", is_flag=True, help="emit the nw.data.usage.v1 document")
@click.pass_obj
def data_usage_cmd(cfg, as_json):
    """Per-source size, file count, oldest/newest file and retention rule of the agentic data
    dir. Read-only; the rows add up to the dir's total. Cleanup is `nw queue disk-clean`."""
    from . import data_usage as DU
    doc = DU.usage(cfg)
    if as_json:
        click.echo(json.dumps(doc, indent=2))
        return
    table = Table(box=None, pad_edge=False)
    table.add_column("source", no_wrap=True)
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("files", justify="right", no_wrap=True)
    table.add_column("count", no_wrap=True)
    table.add_column("oldest", no_wrap=True)
    table.add_column("newest", no_wrap=True)
    table.add_column("retention")
    for r in DU.render_rows(doc):
        table.add_row(r["id"], DU.human_size(r["bytes"]), str(r["files"]),
                      f"{r['count']} {r['count_unit']}", (r["oldest"] or "-")[:10],
                      (r["newest"] or "-")[:10], r["retention"]["text"])
    # piped output stays one row per line (the retention text is the long column)
    console.print(table, width=None if stdout_is_tty(console) else 220)
    camp = next(r for r in doc["sources"] if r["id"] == "campaigns")
    if camp["by_state"]:
        console.print("campaigns by state: " + ", ".join(
            f"{k} {v['count']}" for k, v in camp["by_state"].items()), highlight=False)
    console.print(f"total  {DU.human_size(doc['total']['bytes'])}  {doc['total']['files']} files"
                  f"  in {doc['root']}" + (f"  ({doc['skipped']} skipped)" if doc["skipped"] else ""),
                  highlight=False)
