"""`wt campaign list|show|log|note|runs|run` (SPEC-0171, SPEC-0199) and the hidden `prompt` (SPEC-0174). `attach`
is called by `wt.agentic.cli.register` with the `campaign` group."""
from __future__ import annotations

import json
import os

import click
from rich.table import Table
from rich.text import Text

from .. import completion as C
from .. import render as R
from ..console import console
from ..report import _dump_json
from . import runs as RN
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


_VERDICT_STYLES = {"done": "green", "pass": "green", "approve": "green", "blocked": "red",
                   "fail": "red", "escalate": "red", "invalid": "dim", "—": "dim"}


def _cost_text(rec: dict) -> str:
    if rec.get("cost_usd") is not None:
        return f"${rec['cost_usd']:.2f}"
    if rec.get("charged_usd") is not None:
        return f"${rec['charged_usd']:.2f}*"
    return ""


def _flag(value, style) -> Text:
    return Text("yes", style=style) if value else Text("")


def _model_text(rec: dict) -> str:
    return "/".join(str(x) for x in (rec.get("model"), rec.get("effort")) if x)


def _duration_text(rec: dict) -> str:
    d = rec.get("duration_s")
    return f"{d:.0f}s" if isinstance(d, (int, float)) else ""


def _runs_table(cid: str, runs: list[dict]) -> Table:
    table = R.section_table(f"Runs  ({cid}; {len(runs)})")
    for col in ("run", "persona", "model/effort", "started", "duration", "cost", "exit",
                "is_error", "timed_out", "transient", "verdict"):
        table.add_column(col)
    for r in runs:
        vs = r["verdict_status"]
        status = "" if r["status"] == "done" else f" ({r['status']})"
        table.add_row(
            r["run_id"] + status, str(r["persona"] or ""), _model_text(r),
            str(r["started_at"] or ""), _duration_text(r), _cost_text(r),
            "" if r["exit_code"] is None else str(r["exit_code"]),
            _flag(r["is_error"], "red"), _flag(r["timed_out"], "red"),
            _flag(r["transient"], "yellow"), Text(vs, style=_VERDICT_STYLES.get(vs, "")))
    return table


def _print_run(rec: dict) -> None:
    def kv(key, value):
        console.print(f"  [dim]{key:<14}[/] {value}", highlight=False)

    vs = rec["verdict_status"]
    verdict = rec["verdict"] if isinstance(rec["verdict"], dict) else {}
    console.print(f"[bold]{rec['run_id']}[/]  {rec['campaign']}  "
                  f"[{_VERDICT_STYLES.get(vs, '')}]{vs}[/]  ({rec['source']}, {rec['status']})",
                  highlight=False)
    kv("persona", f"{rec['persona'] or '-'}  {_model_text(rec)}")
    kv("started", f"{rec['started_at'] or '-'}  ({_duration_text(rec) or '-'})")
    kv("exit", f"{rec['exit_code']}  is_error={rec['is_error']}  timed_out={rec['timed_out']}  "
               f"interrupted={rec['interrupted']}  transient={rec['transient']}")
    kv("cost", f"{_cost_text(rec) or '-'}  (budget {rec['max_budget_usd']})")
    if verdict.get("summary"):
        kv("summary", verdict["summary"])
    for key in ("must_fix", "blockers"):
        for item in rec[key]:
            kv(key, item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
    if rec["result_tail"]:
        kv("error tail", rec["result_tail"])
    if rec["permission_denials"]:
        kv("denials", json.dumps(rec["permission_denials"], ensure_ascii=False)[:800])
    if rec["stderr_tail"]:
        console.print("  [dim]stderr tail[/]")
        for line in rec["stderr_tail"]:
            console.print(Text("    " + line), highlight=False)
    for key, path in rec["paths"].items():
        kv(key, path)


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
            console.print(Text("  ") + R.campaign_event(ev), highlight=False)

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
            console.print(R.campaign_event(ev), highlight=False)
        if not events:
            console.print("[dim]no events[/]")

    @campaign_group.command("runs")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--json", "as_json", is_flag=True, help="emit wt.campaign.runs.v1 JSON")
    @click.pass_obj
    def campaign_runs(cfg, campaign, as_json):
        """One row per persona run: cost, exit, errors, transient?, verdict status."""
        try:
            runs = RN.campaign_runs(cfg, campaign)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.campaign.runs.v1", "campaign": campaign, "runs": runs})
            return
        if not runs:
            console.print("[dim]no runs yet[/]")
            return
        console.print(_runs_table(campaign, runs))

    @campaign_group.command("run")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.argument("run")
    @click.option("--json", "as_json", is_flag=True, help="emit wt.campaign.run.v1 JSON")
    @click.pass_obj
    def campaign_run(cfg, campaign, run, as_json):
        """One run in full: verdict, must_fix / blockers, error / stderr tails, file paths.
        RUN is a run id (001-implementer) or a unique prefix (001)."""
        try:
            rec = RN.campaign_run(cfg, campaign, run)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "wt.campaign.run.v1", "campaign": campaign, "run": rec})
            return
        _print_run(rec)

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

    @campaign_group.command("prompt", hidden=True)
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.argument("persona", type=click.Choice(sorted(_persona_names())))
    @click.pass_obj
    def campaign_prompt(cfg, campaign, persona):
        """Print the prompt a persona would get for this campaign, without running it
        (SPEC-0174). Debugging aid; byte-identical to what the runner saves as prompt.md."""
        from . import personas as PS
        from .profiles import resolve_profile

        try:
            st = store.read_state(cfg, campaign)
            profile = resolve_profile(cfg, st.get("profile"))
        except ValueError as e:
            raise click.ClickException(str(e))
        project_cfg = None
        if st.get("project"):
            try:
                from . import project as P
                project_cfg = P.project_config(cfg, st["project"])
            except ValueError:
                project_cfg = None       # no .wt/project.yaml yet: render without context files
        previous = store.read_ledger(cfg, campaign, types=["run.end"], tail=2)
        ctx = PS.build_context(cfg, st, project_cfg, loop_max=profile.get("loop_max"),
                               previous=previous)
        import sys
        sys.stdout.write(PS.build_prompt(persona, ctx))

    return campaign_group


def _persona_names():
    from .personas import PERSONAS
    return list(PERSONAS)
