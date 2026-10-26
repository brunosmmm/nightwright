"""`wt campaign list|show|log|note|runs|run` (SPEC-0171, SPEC-0199) and the hidden `prompt` (SPEC-0174). `attach`
is called by `wt.agentic.cli.register` with the `campaign` group."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import click
from rich.table import Table
from rich.text import Text

from .. import completion as C
from .. import render as R
from ..console import console
from ..report import _dump_json
from . import queue as Q
from . import campaign_export as CE
from . import runs as RN
from . import store


def _summary(st: dict) -> dict:
    counters = st.get("counters") or {}
    cost = st.get("cost") or {}
    return {
        "id": st.get("id"), "spec_id": st.get("spec_id"), "project": st.get("project"),
        "label": Q.campaign_label(st),
        "kind": st.get("kind"), "idea_id": (st.get("idea") or {}).get("id"),
        "profile": st.get("profile"), "state": st.get("state"),
        "loops": counters.get("loops", 0), "runs": counters.get("runs", 0),
        "cost_usd": cost.get("total_usd", 0.0), "cap_usd": cost.get("cap_usd"),
        "estimated_usd": cost.get("estimated_usd") or 0.0,
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


def _sources_suffix(cfg, st: dict) -> str:
    """` (builtin + config.yaml)`: the files that shaped the campaign's profile (SPEC-0238);
    empty for a campaign from before they were recorded."""
    from . import profiles as PR
    sources = st.get("profile_sources") or []
    if not sources:
        return ""
    return " (" + " + ".join(PR.short_origin(cfg, o, project=st.get("project"))
                              for o in sources) + ")"


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


def _export_dir(cfg, out, now) -> Path:
    """The directory an export goes into, created. An explicit `--out` must be missing or empty;
    the default is timestamped, with `-2`, `-3`, … when it exists. Never reuses an export."""
    if out:
        path = Path(out).expanduser()
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            raise click.ClickException(f"--out {out} is not empty")
        path.mkdir(parents=True, exist_ok=True)
        return path
    root = store.agentic_dir(cfg) / "exports"
    root.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    for n in range(1, 1000):
        path = root / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise click.ClickException(f"could not find a free export directory under {root}")


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
            table.add_row(str(r["id"]), r["label"], str(r["project"] or ""),
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
        console.print(f"[bold]{r['id']}[/]  {r['state']}  spec {r['label']}  "
                      f"project {r['project'] or '-'}  profile {r['profile'] or '-'}"
                      + _sources_suffix(cfg, st), highlight=False)
        cap = f" / ${r['cap_usd']:.2f}" if r["cap_usd"] else ""
        est = f" (~${r['estimated_usd']:.2f} est.)" if r["estimated_usd"] > 0 else ""
        console.print(f"  loops {r['loops']}  runs {r['runs']}  cost ${r['cost_usd']:.2f}{est}{cap}")
        sizing = st.get("sizing") or {}
        if sizing:                                                     # SPEC-0219
            src = f" ({sizing['source']})" if sizing.get("source") else ""
            console.print(f"  size {sizing.get('size') or '-'}  tone {sizing.get('tone') or '-'}  "
                          f"profile {r['profile'] or '-'}{src}", highlight=False)
            console.print(f"  sizing: {sizing.get('reason') or '-'}"
                          + (f" — {sizing['rationale']}" if sizing.get("rationale") else ""),
                          highlight=False)
        repo = st.get("repo") or {}
        if repo:
            missing = "" if st["worktree_present"] else " (missing)"
            console.print(f"  branch {repo.get('branch') or '-'}  worktree {wt_path or '-'}{missing}")
        merge = st.get("merge") or {}
        if st.get("kind") == "merge":                                  # SPEC-0233
            console.print(f"  merge {merge.get('branch')} (requested by "
                          f"{merge.get('requested_by')}; head at request "
                          f"{str(merge.get('head_at_request') or '-')[:10]}; worktree "
                          f"{'created' if merge.get('own_worktree') else 'adopted'})",
                          highlight=False)
        lr = st.get("last_review") or {}
        if lr.get("verdict") is not None:
            adv = lr.get("advisory") or []
            majors = sum(1 for f in adv if f.get("severity") == "major")
            console.print(f"  review: score {lr.get('score')}  {lr['verdict']}  "
                          f"advisory {len(adv)} ({majors} major)")
        fu = (st.get("close") or {}).get("followups") or {}
        if fu.get("idea_id"):
            console.print(f"  follow-ups {fu['idea_id']} "
                          f"({len(fu.get('filed') or [])} listed, {len(fu.get('capped') or [])} "
                          f"capped)", highlight=False)
        for f in fu.get("capped") or []:
            console.print(Text(f"  [capped] {f.get('severity')} {f.get('title')}",
                               style="yellow"), highlight=False)
        for f in fu.get("filed") or []:
            console.print(Text(f"  follow-up {f.get('idea_id')} {f.get('severity')} "
                               f"{f.get('title')}"), highlight=False)
        for f in fu.get("unfiled") or []:
            console.print(Text(f"  [unfiled] {f.get('severity')} {f.get('title')}: "
                               f"{f.get('error')}", style="yellow"), highlight=False)
        for f in fu.get("waived") or []:
            console.print(Text(f"  [waived] {f.get('severity')} {f.get('title')}: "
                               f"{f.get('waive_reason')}"), highlight=False)
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

    @campaign_group.command("export")
    @click.option("--format", "fmt", type=click.Choice(CE.FORMATS), default="jsonl",
                  show_default=True, help="jsonl and csv always; parquet needs pyarrow")
    @click.option("--out", "out", type=click.Path(file_okay=False), default=None,
                  help="export directory (must not exist or be empty); default "
                       "<data_dir>/agentic/exports/<YYYYMMDD-HHMMSS>/")
    @click.option("--project", shell_complete=C.complete_project, help="only this project")
    @click.option("--since", type=click.DateTime(["%Y-%m-%d"]), default=None,
                  help="only campaigns created on or after YYYY-MM-DD (configured timezone)")
    @click.option("--terminal-only", is_flag=True, help="only MERGED / HANDED_OFF / ABANDONED")
    @click.option("--table", type=click.Choice(CE.TABLES), default=None,
                  help="with --stdout: the one table to print")
    @click.option("--stdout", "to_stdout", is_flag=True,
                  help="print --table as jsonl or csv instead of writing files")
    @click.option("--json", "as_json", is_flag=True, help="emit the manifest as JSON")
    @click.pass_obj
    def campaign_export(cfg, fmt, out, project, since, terminal_only, table, to_stdout, as_json):
        """Export campaigns, runs and findings as flat tables (wt.campaign.export.v1) for
        analysing how well specs were sized. Read-only; every run is a full snapshot.

        \b
        wt campaign export                          jsonl files under <data_dir>/agentic/exports/
        wt campaign export --format csv --out /tmp/x --since 2026-10-01
        wt campaign export --table campaigns --stdout | duckdb -c "select * from read_json_auto('/dev/stdin')"
        """
        if to_stdout and not table:
            raise click.ClickException("--stdout needs --table campaigns|runs|findings")
        if table and not to_stdout:
            raise click.ClickException("--table is only used with --stdout")
        if to_stdout and fmt == "parquet":
            raise click.ClickException("--stdout supports jsonl or csv")
        if to_stdout and out:
            raise click.ClickException("--out and --stdout are exclusive")
        if fmt == "parquet" and not CE.have_pyarrow():
            raise click.ClickException(
                "--format parquet needs pyarrow (pip install pyarrow); use jsonl or csv")
        now = dt.datetime.now().astimezone()
        tables = CE.export_tables(cfg, project=project, since=since.date() if since else None,
                                  terminal_only=terminal_only, now=now)
        if to_stdout:
            sys.stdout.write(CE.render(tables[table], CE.COLUMNS[table], fmt))
            return
        existed = bool(out) and Path(out).expanduser().exists()
        out_dir = _export_dir(cfg, out, now)
        filters = {"project": project, "since": since.date().isoformat() if since else None,
                   "terminal_only": terminal_only}
        try:
            manifest = CE.write_export(tables, out_dir, fmt, filters=filters)
        except Exception as e:
            # never leave a half-written export: the directory was new or empty before.
            # Best effort and limited to our own files, so cleanup cannot mask `e`.
            for p in (*(out_dir / f"{n}.{fmt}" for n in CE.TABLES), out_dir / "manifest.json",
                      *out_dir.glob(".wt-export-*")):
                try:
                    p.unlink()
                except OSError:
                    pass
            if not existed:
                try:
                    out_dir.rmdir()
                except OSError:
                    pass
            raise click.ClickException(f"export failed, nothing written: {e}")
        if as_json:
            _dump_json({**manifest, "out": str(out_dir)})
            return
        console.print(f"[green]✓[/] exported to {out_dir}", highlight=False)
        for name, t in manifest["tables"].items():
            console.print(f"  {t['file']:<16} {t['rows']} rows", highlight=False)

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
            profile = resolve_profile(cfg, st.get("profile"),
                                      project_root=(st.get("repo") or {}).get("root"))
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
                               previous=previous,
                               extra=PS.persona_extra(st, persona, project_cfg))
        import sys
        sys.stdout.write(PS.build_prompt(persona, ctx))

    return campaign_group


def _persona_names():
    from .personas import PERSONAS
    return list(PERSONAS)
