"""`wt escalations list|show|withdraw` (SPEC-0232): the human's view of escalation records."""
from __future__ import annotations

import os

import click
from rich.table import Table
from rich.text import Text

from .. import completion as C
from .. import render as R
from ..console import console
from ..report import _dump_json
from . import escalations as ESC
from . import events as E
from . import store


@C._soft
def complete_escalation_id(ctx, param, incomplete):
    return C._items([r["id"] for r in ESC.list_all(C._cfg_from_ctx(ctx))], incomplete)


@click.group("escalations")
def escalations_group():
    """Escalations to the human (SPEC-0232): questions the conductor could not decide.

    \b
    wt escalations list                open escalations
    wt escalations show ESC-0001       the question, options, recommendation and facts
    wt escalations withdraw ESC-0001 --reason "…" --yes
    """


def _age(rec, now) -> str:
    s = E._age_s(rec.get("created"), now)
    if s is None:
        return "?"
    return f"{int(s // 3600)}h" if s >= 3600 else f"{int(s // 60)}m" if s >= 60 else f"{int(s)}s"


@escalations_group.command("list")
@click.option("--all", "show_all", is_flag=True, help="every status, not only open")
@click.option("--json", "as_json", is_flag=True, help="emit wt.escalation.list.v1 JSON")
@click.pass_obj
def escalations_list(cfg, show_all, as_json):
    """Escalations, newest first (open ones unless --all)."""
    recs = ESC.list_all(cfg, None if show_all else "open")
    if as_json:
        _dump_json({"schema": "wt.escalation.list.v1", "escalations": recs})
        return
    if not recs:
        console.print("[dim]no escalations[/]" if show_all else "[dim]no open escalations[/]")
        return
    import datetime as dt
    now = dt.datetime.now().astimezone()
    table = Table(title=f"Escalations  ({len(recs)} shown)")
    for col in ("id", "age", "campaign", "status", "question", "options"):
        table.add_column(col, max_width=60 if col == "question" else None,
                         overflow="fold" if col == "question" else None)
    for r in recs:
        table.add_row(r["id"], _age(r, now), r.get("campaign") or "-", r.get("status", "?"),
                      r.get("question", ""), str(len(r.get("options") or [])))
    console.print(table)


@escalations_group.command("show")
@click.argument("esc_id", metavar="ID", shell_complete=complete_escalation_id)
@click.option("--json", "as_json", is_flag=True, help="emit the wt.escalation.v1 record")
@click.pass_obj
def escalations_show(cfg, esc_id, as_json):
    """One escalation in full."""
    try:
        rec = ESC.read(cfg, esc_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json(rec)
        return
    console.print(Text.assemble((f"{rec['id']}  ", "bold"), (rec.get("status", "?"), "cyan"),
                                f"  by {rec.get('by')}  {rec.get('created')}"), highlight=False)
    if rec.get("campaign"):
        console.print(f"campaign: {rec['campaign']}", highlight=False)
    console.print(f"\n{rec['question']}\n", highlight=False)
    for o in rec.get("options") or []:
        mark = "  (recommended)" if o["n"] == rec.get("recommendation") else ""
        console.print(f"  {o['n']}. {o['label']}{mark}", highlight=False)
    if rec.get("facts"):
        console.print("\nfacts:")
        for f in rec["facts"]:
            console.print(f"  - {f}", highlight=False)
    park = rec.get("park")
    if park:
        console.print(f"\npark: {park.get('escalated_from') or '(none)'} → "
                      f"{park.get('resume_to')}  [{park.get('mode')}]", highlight=False)
    if rec.get("withdrawn"):
        w = rec["withdrawn"]
        console.print(f"\nwithdrawn by {w.get('by')}: {w.get('reason')}", highlight=False)


@escalations_group.command("withdraw")
@click.argument("esc_id", metavar="ID", shell_complete=complete_escalation_id)
@click.option("--reason", required=True, help="why the escalation is no longer needed")
@click.option("--from", "frm", default=None,
              help="who withdraws (default: $WT_MAILBOX_ADDR, then conductor)")
@click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
@click.option("--json", "as_json", is_flag=True, help="emit wt.escalation.withdraw.v1 JSON")
@click.pass_obj
def escalations_withdraw(cfg, esc_id, reason, frm, do_write, as_json):
    """Withdraw an open escalation. The campaign stays PARKED (human_decision): resume it
    with `wt queue resume`."""
    from . import mailbox as M
    try:
        rec = ESC.read(cfg, esc_id)
        by = M.validate_address(cfg, frm or os.environ.get("WT_MAILBOX_ADDR") or "conductor")
        if rec.get("status") != "open":
            raise ValueError(f"{esc_id} is {rec.get('status')}, not open")
        if do_write:
            rec = ESC.withdraw(cfg, esc_id, reason, by)
    except ValueError as e:
        raise click.ClickException(str(e))
    cid = rec.get("campaign")
    st = store.read_state(cfg, cid) if cid else {}
    cstate = st.get("state")
    hint = None
    if cid and cstate == "PARKED" and (st.get("park") or {}).get("reason") == "human_decision":
        hint = f"{cid} is still PARKED (human_decision); resume it: wt queue resume {cid} --yes"
    if as_json:
        _dump_json({"schema": "wt.escalation.withdraw.v1", "escalation": rec, "reason": reason,
                    "by": by, "campaign_state": cstate, "hint": hint,
                    "written": bool(do_write)})
        return
    console.print(R.plan(f"withdraw {esc_id}", {"campaign": cid, "reason": reason, "by": by}),
                  highlight=False)
    if not do_write:
        console.print("[yellow]not written — re-run with --yes to commit[/]")
        return
    console.print(f"[green]✓[/] {esc_id} withdrawn")
    if hint:
        console.print(hint, highlight=False)
