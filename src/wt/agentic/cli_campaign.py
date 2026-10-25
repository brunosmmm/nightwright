"""`wt campaign list|show|log|note` (SPEC-0171) and the hidden `prompt` (SPEC-0174). `attach`
is called by `wt.agentic.cli.register` with the `campaign` group."""
from __future__ import annotations

import os

import click
from rich.table import Table

from .. import completion as C
from ..console import console
from ..report import _dump_json
from . import store


def _summary(st: dict) -> dict:
    counters = st.get("counters") or {}
    cost = st.get("cost") or {}
    return {
        "id": st.get("id"), "spec_id": st.get("spec_id"), "project": st.get("project"),
        "profile": st.get("profile"), "state": st.get("state"),
        "loops": counters.get("loops", 0), "runs": counters.get("runs", 0),
        "cost_usd": cost.get("total_usd", 0.0), "cap_usd": cost.get("cap_usd"),
        "park": st.get("park"), "created_at": st.get("created_at"),
        "updated_at": st.get("updated_at"),
    }


def _event_line(ev: dict) -> str:
    skip = {"ts", "seq", "campaign", "type", "by"}
    bits = []
    for k, v in ev.items():
        if k in skip:
            continue
        txt = v if isinstance(v, str) else str(v)
        if len(txt) > 60:
            txt = txt[:57] + "..."
        bits.append(f"{k}={txt}")
    return (f"{ev.get('seq', '?'):>4}  {ev.get('ts', '')}  {ev.get('type', '?'):<12} "
            f"{ev.get('by', ''):<16} {' '.join(bits)}")


def attach(campaign_group):
    """Register the campaign subcommands on `campaign_group` (SPEC-0171)."""

    @campaign_group.command("list")
    @click.option("--all", "include_terminal", is_flag=True,
                  help="include MERGED / HANDED_OFF / ABANDONED campaigns")
    @click.option("--project", shell_complete=C.complete_project, help="filter by project")
    @click.option("--spec", "spec_id", shell_complete=C.complete_any_spec_id,
                  help="filter by spec id")
    @click.option("--state", shell_complete=C.complete_campaign_state, help="filter by state")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.campaigns.v1 JSON")
    @click.pass_obj
    def campaign_list(cfg, include_terminal, project, spec_id, state, as_json):
        """Campaigns, newest first (non-terminal unless --all)."""
        rows = store.list_campaigns(cfg, include_terminal=include_terminal, project=project,
                                    spec_id=spec_id, state=state)
        if as_json:
            _dump_json({"schema": "wt.campaigns.v1", "campaigns": [_summary(s) for s in rows]})
            return
        scope = "all" if include_terminal else "open"
        table = Table(title=f"Campaigns  ({scope}; {len(rows)} shown)")
        for col in ("id", "spec", "project", "state", "loops", "cost", "updated"):
            table.add_column(col)
        for s in rows:
            r = _summary(s)
            cost = f"${r['cost_usd']:.2f}" + (f"/{r['cap_usd']:.0f}" if r["cap_usd"] else "")
            table.add_row(str(r["id"]), str(r["spec_id"]), str(r["project"] or ""),
                          str(r["state"]), str(r["loops"]), cost, str(r["updated_at"] or ""))
        console.print(table)

    @campaign_group.command("show")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--json", "as_json", is_flag=True, help="emit wt.campaign.v1 JSON")
    @click.pass_obj
    def campaign_show(cfg, campaign, as_json):
        """State of one campaign plus its last events."""
        try:
            st = dict(store.read_state(cfg, campaign))
        except ValueError as e:
            raise click.ClickException(str(e))
        st["schema"] = "wt.campaign.v1"
        st["last_events"] = store.read_ledger(cfg, campaign, tail=5)
        wt_path = (st.get("repo") or {}).get("worktree")
        st["worktree_present"] = bool(wt_path and os.path.isdir(wt_path))
        if as_json:
            _dump_json(st)
            return
        r = _summary(st)
        console.print(f"[bold]{r['id']}[/]  {r['state']}  spec {r['spec_id']}  "
                      f"project {r['project'] or '-'}  profile {r['profile'] or '-'}")
        cap = f" / ${r['cap_usd']:.2f}" if r["cap_usd"] else ""
        console.print(f"  loops {r['loops']}  runs {r['runs']}  cost ${r['cost_usd']:.2f}{cap}")
        repo = st.get("repo") or {}
        if repo:
            missing = "" if st["worktree_present"] else " (missing)"
            console.print(f"  branch {repo.get('branch') or '-'}  worktree {wt_path or '-'}{missing}")
        if st.get("park"):
            p = st["park"]
            console.print(f"  [yellow]parked[/] {p.get('reason')}: {p.get('detail', '')}  "
                          f"→ resume to {p.get('resume_to')}")
        for ev in st["last_events"]:
            console.print("  " + _event_line(ev), markup=False, highlight=False)

    @campaign_group.command("log")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--type", "types", multiple=True, help="only these event types (repeatable)")
    @click.option("--tail", type=int, default=None, help="last N events")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.campaign.log.v1 JSON")
    @click.pass_obj
    def campaign_log(cfg, campaign, types, tail, as_json):
        """The campaign's append-only ledger."""
        try:
            store.read_state(cfg, campaign)
        except ValueError as e:
            raise click.ClickException(str(e))
        events = store.read_ledger(cfg, campaign, types=types or None, tail=tail)
        if as_json:
            _dump_json({"schema": "wt.campaign.log.v1", "campaign": campaign, "events": events})
            return
        for ev in events:
            console.print(_event_line(ev), markup=False, highlight=False)
        if not events:
            console.print("[dim]no events[/]")

    @campaign_group.command("note")
    @click.argument("args", nargs=-1, required=True)
    @click.option("--author", default=None,
                  help="persona or agent name; recorded as by=agent:<author>")
    @click.pass_obj
    def campaign_note(cfg, args, author):
        """Append a note. CAMPAIGN defaults to $WT_CAMPAIGN (set inside persona runs).

        \b
        wt campaign note SPEC-0170-20261026-a3f9 "checked the fixture"
        WT_CAMPAIGN=… wt campaign note "found the cause" --author implementer
        """
        env_id = os.environ.get("WT_CAMPAIGN")
        args = list(args)
        if len(args) >= 2 and store.campaign_dir(cfg, args[0]).is_dir():
            campaign, text = args[0], " ".join(args[1:])
        elif env_id:
            campaign, text = env_id, " ".join(args)
        else:
            raise click.UsageError(
                "campaign required: pass CAMPAIGN before the text or set WT_CAMPAIGN")
        try:
            store.read_state(cfg, campaign)
        except ValueError as e:
            raise click.ClickException(str(e))
        by = f"agent:{author}" if author else "human"
        rec = store.append_event(cfg, campaign, "note",
                                 {"text": text, "author": author or "human"}, by=by)
        console.print(f"[green]✓[/] note #{rec['seq']} ← {campaign}")

    return campaign_group
