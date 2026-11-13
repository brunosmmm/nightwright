"""`nw mailbox send|follow|list|thread` (SPEC-0183): conductor <-> steward messaging."""
from __future__ import annotations

import os

import click
from rich.table import Table

from .. import completion as C
from .. import render as R
from ..console import console
from ..report import _dump_json
from . import federation as FED
from . import hosts as H
from . import mailbox as M
from . import store as S


@C._soft
def complete_address(ctx, param, incomplete):
    """Mailbox addresses: conductor, human, steward:<each project>."""
    return C._items(M.known_addresses(C._cfg_from_ctx(ctx)), incomplete)


@click.group("mailbox")
def mailbox_group():
    """Conductor <-> steward mailbox (SPEC-0183): file-based inboxes with cursors.

    \b
    nw mailbox send --to steward:Meta-Tools --kind question "why parked?"
    nw mailbox follow --to conductor        one JSON line per new message (Monitor)
    nw mailbox list --to conductor --unanswered
    """


@mailbox_group.command("send")
@click.option("--to", "to", required=True, shell_complete=complete_address,
              help="recipient: conductor | human | steward:<Project> "
                   "(with hosts enrolled: human@<host> | steward:<Project>@<host>)")
@click.option("--from", "frm", default=None, shell_complete=complete_address,
              help="sender (default: $NW_MAILBOX_ADDR)")
@click.option("--kind", type=click.Choice(M.KINDS), default="info", show_default=True)
@click.option("--campaign", default=None, shell_complete=C.complete_campaign_id,
              help="campaign this message is about")
@click.option("--no-campaign", is_flag=True,
              help="do not infer a campaign from --re or the text")
@click.option("--re", "re_", default=None, help="message id this replies to")
@click.option("--no-reply", is_flag=True, help="no reply expected (default for answer/info)")
@click.option("--json", "as_json", is_flag=True, help="emit the nw.message.v1 envelope")
@click.argument("text", nargs=-1, required=True)
@click.pass_obj
def mailbox_send(cfg, to, frm, kind, campaign, no_campaign, re_, no_reply, as_json, text):
    """Append a message to TO's inbox (mail to the conductor also wakes its event stream).
    Without --campaign the campaign of the --re message, or the one campaign the text names, is
    stamped (SPEC-0431). An explicit --campaign, including an empty one, is stored as given.
    --no-campaign skips inference and conflicts with --campaign."""
    frm = frm or os.environ.get("NW_MAILBOX_ADDR")
    if not frm:
        raise click.UsageError("sender required: pass --from or set NW_MAILBOX_ADDR")
    if campaign is not None and no_campaign:
        raise click.UsageError("--no-campaign conflicts with --campaign")
    body = " ".join(text)
    if "@" in to and H.load(cfg):          # SPEC-0532: only an explicit @<host> leaves this machine
        try:
            frm = M.validate_address(cfg, frm)
            rec = FED.enqueue_remote(cfg, to=to, frm=frm, text=body, kind=kind,
                                     campaign=campaign, re=re_,
                                     needs_reply=False if no_reply else None)
        except (ValueError, OSError) as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json(rec)
            return
        console.print(f"queued {rec['msg_uid']} → {rec['to']}@{to.rpartition('@')[2]}",
                      highlight=False)
        return
    campaign_from = None
    if campaign is None and not no_campaign:
        replied = M.find_message(cfg, re_) if re_ else None
        re_campaign = replied.get("campaign") if replied else None
        # inherit only a campaign whose state is a JSON object inside the campaigns root
        # (missing, unreadable, non-object, or a path outside the store falls through)
        if S.campaign_state(cfg, re_campaign) is None:
            re_campaign = None
        if re_campaign:
            campaign, campaign_from = re_campaign, "re"
        else:
            campaign = S.sole_campaign_named(cfg, body)
            campaign_from = "text" if campaign else None
    try:
        rec = M.send(cfg, to=to, frm=frm, text=body, kind=kind, campaign=campaign,
                     re=re_, needs_reply=False if no_reply else None,
                     campaign_from=campaign_from)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json(rec)
        return
    console.print(f"[green]✓[/] {rec['id']}  {rec['kind']} → {rec['to']}"
                  + (f" \\[{campaign}, inferred from {campaign_from}]" if campaign_from else ""),
                  highlight=False)


@mailbox_group.command("follow")
@click.option("--to", "to", required=True, shell_complete=complete_address,
              help="inbox to follow")
@click.option("--json/--text", "as_json", default=True,
              help="JSON lines (default) or one-line summaries")
@click.option("--from-start", is_flag=True, help="new reader: start at the first message")
@click.option("--poll", "poll_s", type=float, default=2.0, show_default=True)
@click.option("--no-code-watch", is_flag=True,
              help="never exit when nightwright's code changes (default: print `code_changed` and "
                   "exit 0 so the Monitor owner re-arms)")
@click.pass_obj
def mailbox_follow(cfg, to, as_json, from_start, poll_s, no_code_watch):
    """Stream new messages for TO forever (one line each; cursor persisted; one follower
    per inbox). Exits with a `code_changed` line when nightwright's code changes."""
    try:
        M.follow(cfg, to, as_json=as_json, poll_s=poll_s, from_start=from_start,
                 **({"code_root": None} if no_code_watch else {}))
    except ValueError as e:
        raise click.ClickException(str(e))
    except KeyboardInterrupt:
        pass


@mailbox_group.command("list")
@click.option("--to", "to", required=True, shell_complete=complete_address,
              help="inbox to list")
@click.option("--unanswered", is_flag=True, help="only messages still needing a reply")
@click.option("--json", "as_json", is_flag=True, help="emit nw.mailbox.list.v1 JSON")
@click.pass_obj
def mailbox_list(cfg, to, unanswered, as_json):
    """Messages in TO's inbox."""
    try:
        addr = M.validate_address(cfg, to)
        msgs = M.unanswered(cfg, addr) if unanswered else M.read_inbox(cfg, addr)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json({"schema": "nw.mailbox.list.v1", "to": addr, "unanswered": unanswered,
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
@click.option("--json", "as_json", is_flag=True, help="emit nw.mailbox.thread.v1 JSON")
@click.pass_obj
def mailbox_thread(cfg, msg_id, as_json):
    """A message and all its replies."""
    try:
        msgs = M.thread(cfg, msg_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json({"schema": "nw.mailbox.thread.v1", "id": msg_id, "messages": msgs})
        return
    for m in msgs:
        console.print(R.message(m), highlight=False)
