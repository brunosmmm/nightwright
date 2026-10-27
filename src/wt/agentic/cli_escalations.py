"""`wt escalations list|show|answer|withdraw` (SPEC-0232, SPEC-0235): the human's view of escalation records."""
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
    wt escalations answer ESC-0001 --choose 2 --yes     (or --text, or --waive/--attest)
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
    if rec.get("answer"):
        a = rec["answer"]
        console.print(f"\nanswered by {rec.get('answered_by')} at {rec.get('answered_at')}: "
                      f"{ESC.summarize(a)}", highlight=False)
    if rec.get("withdrawn"):
        w = rec["withdrawn"]
        console.print(f"\nwithdrawn by {w.get('by')}: {w.get('reason')}", highlight=False)


@escalations_group.command("answer")
@click.argument("esc_id", metavar="ID", shell_complete=complete_escalation_id)
@click.option("--choose", default=None, help="an option: its number or its label")
@click.option("--text", default=None, help="a free-text answer")
@click.option("--waive", "waive", multiple=True,
              help="override: a finding id of the stored review (any severity); repeatable")
@click.option("--attest", "attest", multiple=True,
              help="override: an AC finding id (<persona>#ac<j>) or text of an unmet AC "
                   "as met; repeatable")
@click.option("--reason", default=None, help="why (required with --waive / --attest)")
@click.option("--note", default=None, help="optional free text kept with the answer")
@click.option("--by", default=None, help="who answers (default: human; the steward passes "
                                         "steward:<Project>)")
@click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
@click.option("--json", "as_json", is_flag=True, help="emit wt.escalation.answer.v1 JSON")
@click.pass_obj
def escalations_answer(cfg, esc_id, choose, text, waive, attest, reason, note, by, do_write,
                       as_json):
    """Answer an open escalation. Records the answer and wakes the conductor, which applies it;
    the campaign stays PARKED until then. Exactly one form: --choose, --text, or --waive /
    --attest with --reason."""
    try:
        plan = ESC.plan_answer(cfg, esc_id, choose=choose, text=text, waive=waive,
                               attest=attest, reason=reason, note=note, by=by)
        done = ESC.apply_answer(cfg, plan) if do_write else {}
    except ESC.AnswerFormError as e:
        raise click.UsageError(str(e))
    except (ValueError, OSError) as e:
        raise click.ClickException(str(e))
    ev_err = done.get("event_error")
    if ev_err:
        R.warn(f"escalation_answered event not written: {ev_err}; the conductor was not woken")
    if as_json:
        _dump_json({"schema": "wt.escalation.answer.v1", "id": esc_id,
                    "campaign": plan["campaign"], "by": plan["by"],
                    "answer": plan["answer"], "summary": plan["summary"],
                    "override": plan["override"], "written": bool(do_write),
                    **({"event_error": ev_err} if ev_err else {})})
        return
    console.print(f"{esc_id}: {plan['question']}", highlight=False)
    for o in plan["options"]:
        console.print(f"  {o['n']}. {o['label']}", highlight=False)
    rows = {"campaign": plan["campaign"], "by": plan["by"], "answer": plan["summary"]}
    ov = plan["override"]
    if ov:
        for w in ov["waive"]:
            rows[f"waive {w['id']}"] = f"{w['severity']} {w['title']}"
        for a in ov["attest"]:
            rows[f"attest {len(rows)}"] = f"{a}  (covers: {'; '.join(ov['reach'][a])})"
        for a in ov["implicitly_attested"]:
            rows[f"implicitly attested {len(rows)}"] = a
    console.print(R.plan(f"answer {esc_id}", rows), highlight=False)
    if not do_write:
        console.print("[yellow]not written — re-run with --yes to commit[/]")
        return
    console.print(f"[green]✓[/] {esc_id} answered; the conductor applies it")


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
    try:
        plan = ESC.plan_withdraw(cfg, esc_id, reason=reason,
                                 by=frm or os.environ.get("WT_MAILBOX_ADDR") or "conductor")
        by, rec = plan["by"], ESC.read(cfg, esc_id)
        if do_write:
            rec = ESC.apply_withdraw(cfg, plan)
            rec.pop("campaign_state", None)
    except ValueError as e:
        raise click.ClickException(str(e))
    cid = rec.get("campaign")
    try:
        st = store.read_state(cfg, cid) if cid else {}
    except (ValueError, OSError):
        st = {}
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
