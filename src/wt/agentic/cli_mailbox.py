"""`wt mailbox send|follow|list|thread` (SPEC-0183): conductor <-> steward messaging."""
from __future__ import annotations

import os

import click
from rich.table import Table

from .. import completion as C
from .. import render as R
from ..console import console
from ..report import _dump_json
from . import mailbox as M


@C._soft
def complete_address(ctx, param, incomplete):
    """Mailbox addresses: conductor, human, steward:<each project>."""
    return C._items(M.known_addresses(C._cfg_from_ctx(ctx)), incomplete)


@click.group("mailbox")
def mailbox_group():
    """Conductor <-> steward mailbox (SPEC-0183): file-based inboxes with cursors.

    \b
    wt mailbox send --to steward:Meta-Tools --kind question "why parked?"
    wt mailbox follow --to conductor        one JSON line per new message (Monitor)
    wt mailbox list --to conductor --unanswered
    """


@mailbox_group.command("send")
@click.option("--to", "to", required=True, shell_complete=complete_address,
              help="recipient: conductor | human | steward:<Project>")
@click.option("--from", "frm", default=None, shell_complete=complete_address,
              help="sender (default: $WT_MAILBOX_ADDR)")
@click.option("--kind", type=click.Choice(M.KINDS), default="info", show_default=True)
@click.option("--campaign", default=None, shell_complete=C.complete_campaign_id,
              help="campaign this message is about")
@click.option("--re", "re_", default=None, help="message id this replies to")
@click.option("--no-reply", is_flag=True, help="no reply expected (default for answer/info)")
@click.option("--json", "as_json", is_flag=True, help="emit the wt.message.v1 envelope")
@click.argument("text", nargs=-1, required=True)
@click.pass_obj
def mailbox_send(cfg, to, frm, kind, campaign, re_, no_reply, as_json, text):
    """Append a message to TO's inbox (mail to the conductor also wakes its event stream)."""
    frm = frm or os.environ.get("WT_MAILBOX_ADDR")
    if not frm:
        raise click.UsageError("sender required: pass --from or set WT_MAILBOX_ADDR")
    try:
        rec = M.send(cfg, to=to, frm=frm, text=" ".join(text), kind=kind, campaign=campaign,
                     re=re_, needs_reply=False if no_reply else None)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json(rec)
        return
    console.print(f"[green]✓[/] {rec['id']}  {rec['kind']} → {rec['to']}", highlight=False)


@mailbox_group.command("follow")
@click.option("--to", "to", required=True, shell_complete=complete_address,
              help="inbox to follow")
@click.option("--json/--text", "as_json", default=True,
              help="JSON lines (default) or one-line summaries")
@click.option("--from-start", is_flag=True, help="new reader: start at the first message")
@click.option("--poll", "poll_s", type=float, default=2.0, show_default=True)
@click.pass_obj
def mailbox_follow(cfg, to, as_json, from_start, poll_s):
    """Stream new messages for TO forever (one line each; cursor persisted; one follower
    per inbox)."""
    try:
        M.follow(cfg, to, as_json=as_json, poll_s=poll_s, from_start=from_start)
    except ValueError as e:
        raise click.ClickException(str(e))
    except KeyboardInterrupt:
        pass


@mailbox_group.command("list")
@click.option("--to", "to", required=True, shell_complete=complete_address,
              help="inbox to list")
@click.option("--unanswered", is_flag=True, help="only messages still needing a reply")
@click.option("--json", "as_json", is_flag=True, help="emit wt.mailbox.list.v1 JSON")
@click.pass_obj
def mailbox_list(cfg, to, unanswered, as_json):
    """Messages in TO's inbox."""
    try:
        addr = M.validate_address(cfg, to)
        msgs = M.unanswered(cfg, addr) if unanswered else M.read_inbox(cfg, addr)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json({"schema": "wt.mailbox.list.v1", "to": addr, "unanswered": unanswered,
                    "messages": msgs})
        return
    table = Table(title=f"Mailbox {addr}  ({len(msgs)} shown)")
    for col in ("id", "kind", "from", "campaign", "re", "text"):
        table.add_column(col)
    for m in msgs:
        table.add_row(m["id"], m["kind"], m["from"], m.get("campaign") or "",
                      m.get("re") or "", m["text"])
    console.print(table)


@mailbox_group.command("thread")
@click.argument("msg_id")
@click.option("--json", "as_json", is_flag=True, help="emit wt.mailbox.thread.v1 JSON")
@click.pass_obj
def mailbox_thread(cfg, msg_id, as_json):
    """A message and all its replies."""
    try:
        msgs = M.thread(cfg, msg_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json({"schema": "wt.mailbox.thread.v1", "id": msg_id, "messages": msgs})
        return
    for m in msgs:
        console.print(R.message(m), highlight=False)
