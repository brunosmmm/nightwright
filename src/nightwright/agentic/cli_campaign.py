"""`nw campaign list|show|log|note|runs|run|tail` (SPEC-0171, SPEC-0199, SPEC-0362) and the hidden `prompt` (SPEC-0174). `attach`
is called by `nightwright.agentic.cli.register` with the `campaign` group."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .. import completion as C
from .. import render as R
from ..console import console
from ..report import _dump_json
from . import queue as Q
from . import campaign_export as CE
from . import history as H
from . import schemas as SC
from . import persona_gate as PG
from . import runs as RN
from . import store
from . import timeline as TL


def _history_cache_unwritable_warn(cfg) -> None:
    from . import history_cache as HC
    if not HC.cache_wanted(cfg, None):
        return
    if HC._not_writable(cfg, None) or HC.consume_cache_write_fallback():
        R.err_console.print(Text("cache: not writable"))


def _summary(st: dict, cfg=None, backlog=None) -> dict:
    """`nw.campaigns.v1` entry. With `cfg`, the SPEC-0356 row fields (title, priority,
    depends_on, blocked_by, waiting_on, park_reason) ride along; `backlog` is one
    `backlog_view` keyed by campaign (QUEUED readiness), read once per invocation."""
    counters = st.get("counters") or {}
    cost = st.get("cost") or {}
    out = {
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
    if cfg is not None:
        rv = Q.row_view(cfg, st, (backlog or {}).get(st.get("id")))
        out.update({k: rv[k] for k in ("title", "priority", "depends_on", "blocked_by",
                                       "waiting_on", "park_reason")})
    return out


def _list_table(title: str, items: list[dict], width: int, backlog=None) -> "Table":
    """SPEC-0356: `nw campaign list` fitted to `width` (ids and states never cut, a title on
    every row; the `spec` column is gone: the title and the id carry it). `backlog` (keyed by
    campaign) gives a ready QUEUED row its `✓ ready`, as in `nw queue list`."""
    from .cli_queue import _blocked_cell, _campaign_cell, _col
    from . import listview as LV
    rows = []
    for r in items:
        cost = f"${r['cost_usd']:.2f}" + (f"/{r['cap_usd']:.0f}" if r["cap_usd"] else "")
        rows.append({"campaign": _campaign_cell({"campaign": str(r["id"]), "kind": r["kind"],
                                                 "label": r["label"]}),
                     "state": Text(str(r["state"]), style=R.state_style(r["state"])),
                     "title": Text(str(r.get("title") or "—")),
                     "blocked_by": _blocked_cell({**r, "backlog": (backlog or {}).get(r["id"])}),
                     "park": Text(str(r.get("park_reason") or ""), style="yellow"),
                     "pri": str(r.get("priority")), "cost": cost,
                     "project": R.project_text(r["project"]), "loops": str(r["loops"]),
                     "age": str(r["updated_at"] or "")})
    cols = [_col("campaign", "id", fixed=True), _col("state", "state", fixed=True),
            _col("title", "title", flex=True),
            _col("blocked_by", "blocked by", fold="{} ", hide_empty=True),
            _col("park", "park", fold="[{}] ", hide_empty=True),
            _col("pri", "pri", justify="right"), _col("cost", "cost"),
            _col("project", "project"), _col("loops", "loops"), _col("age", "updated")]
    return LV.render(title, cols, rows, width)


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
        console.print(f"  [dim]{key:<14}[/] {escape(str(value))}", highlight=False)

    vs = rec["verdict_status"]
    verdict = rec["verdict"] if isinstance(rec["verdict"], dict) else {}
    style = _VERDICT_STYLES.get(vs, "")
    status_frag = f"[{style}]{escape(str(vs))}[/]" if style else escape(str(vs))
    console.print(f"[bold]{escape(str(rec['run_id']))}[/]  {escape(str(rec['campaign']))}  "
                  f"{status_frag}  ({escape(str(rec['source']))}, {escape(str(rec['status']))})",
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


_NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


_TONE_STYLE = {"head": "bold", "group": "bold cyan", "ok": "green", "bad": "red",
               "warn": "yellow", "info": "blue", "dim": "dim"}


def _print_story(story, expand) -> None:
    """The SPEC-0433 story under the header: collapsed unless `expand`."""
    rows = TL.text_lines(story, expand=expand)
    if not rows:
        console.print("  [dim]no events[/]")
        return
    console.print()
    for indent, text, tone in rows:
        console.print(Text("  " * (indent + 1) + text, style=_TONE_STYLE.get(tone, "")),
                      highlight=False, soft_wrap=False, overflow="fold")
    if not expand:
        console.print(Text("  (-x expands details · --raw shows raw events · "
                           "nw campaign log for the full ledger)", style="dim"), highlight=False)


def _session_reader(cfg, sid) -> str | None:
    """The `conductor` / `steward:<P>` reader the Claude Code session `sid` is registered as in
    `conductor_cost.sessions` (SPEC-0299), or None."""
    from . import conductor_cost as CC
    from . import mailbox as M
    try:
        text = CC._sessions_path(cfg).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in reversed(text.splitlines()):
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("session_id") == sid:
            try:
                reader = M.validate_address(cfg, row.get("reader"))
            except ValueError:
                continue
            if reader != "human":
                return reader
    return None


def note_actor(cfg, env, author=None) -> tuple[str, str]:
    """SPEC-0425: `(by, author)` of a `nw campaign note`, first match wins: `--author X` →
    `agent:X`; `NW_PERSONA` → `agent:<persona>`; `NW_PERSONA_RUN=1` → `agent:persona`; a valid
    `NW_MAILBOX_ADDR` → that address; a `CLAUDE_CODE_SESSION_ID` registered for the conductor or
    a steward → that reader; `CLAUDECODE=1` → `agent:session`; else `human`. `author` is `by`
    without the `agent:` prefix."""
    from . import mailbox as M

    def agent(name):
        return f"agent:{name}", name
    if author:
        return agent(author)
    persona = (env.get("NW_PERSONA") or "").strip()
    if persona and set(persona) <= _NAME_OK:
        return agent(persona)
    if env.get("NW_PERSONA_RUN") == "1":
        return agent("persona")
    addr = (env.get("NW_MAILBOX_ADDR") or "").strip()
    if addr:
        try:
            a = M.validate_address(cfg, addr)
            return a, a
        except ValueError:
            pass
    sid = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if sid:
        reader = _session_reader(cfg, sid)
        if reader:
            return reader, reader
    if env.get("CLAUDECODE") == "1":
        return agent("session")
    return "human", "human"


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
    @click.option("--json", "as_json", is_flag=True, help="emit nw.campaigns.v1 JSON")
    @click.pass_obj
    def campaign_list(cfg, include_terminal, project, spec_id, state, as_json):
        """Campaigns, newest first (non-terminal unless --all)."""
        rows = store.list_campaigns(cfg, include_terminal=include_terminal, project=project,
                                    spec_id=spec_id, state=state)
        backlog = ({b["campaign"]: b for b in Q.backlog_view(cfg)}
                   if any(s.get("state") == "QUEUED" for s in rows) else {})
        items = [_summary(s, cfg, backlog) for s in rows]
        if as_json:
            _dump_json({"schema": "nw.campaigns.v1", "campaigns": items})
            return
        scope = "all" if include_terminal else "open"
        console.print(_list_table(f"Campaigns  ({scope}; {len(rows)} shown)", items,
                                  console.width, backlog))

    @campaign_group.command("show")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--raw", is_flag=True,
                  help="the last raw ledger events instead of the story (SPEC-0433)")
    @click.option("--expand", "-x", is_flag=True,
                  help="also print each story line's details (findings, gate results, raw notes)")
    @click.option("--json", "as_json", is_flag=True,
                  help="emit nw.campaign.v1 JSON (last_events plus the nw.campaign.story.v1 story)")
    @click.pass_obj
    def campaign_show(cfg, campaign, raw, expand, as_json):
        """State of one campaign plus its story: the ledger grouped by loop and phase, one line
        per run, gate, review round and rebase (SPEC-0433). `--raw` shows the last raw events."""
        try:
            st = dict(store.read_state(cfg, campaign))
        except ValueError as e:
            raise click.ClickException(str(e))
        st["schema"] = "nw.campaign.v1"
        st["last_events"] = store.read_ledger(cfg, campaign, tail=5)
        wt_path = (st.get("repo") or {}).get("worktree")
        st["worktree_present"] = bool(wt_path and os.path.isdir(wt_path))
        st["remote_gate"] = Q.remote_gate_view(cfg, st)                # SPEC-0356
        st["cost_by_worker"] = PG.cost_by_worker(                      # SPEC-0345
            store.read_ledger(cfg, campaign, types=["run.end"]))
        story = None
        if as_json or not raw:
            story = TL.story(store.read_ledger(cfg, campaign), campaign=campaign, state=st,
                             now=store.now_iso())
        if as_json:
            st["story"] = story
            _dump_json(st)
            return
        r = _summary(st)
        console.print(f"[bold]{r['id']}[/]  {r['state']}  spec {r['label']}  "
                      f"project {r['project'] or '-'}  profile {r['profile'] or '-'}"
                      + _sources_suffix(cfg, st), highlight=False)
        cap = f" / ${r['cap_usd']:.2f}" if r["cap_usd"] else ""
        est = f" (~${r['estimated_usd']:.2f} est.)" if r["estimated_usd"] > 0 else ""
        console.print(f"  loops {r['loops']}  runs {r['runs']}  cost ${r['cost_usd']:.2f}{est}{cap}")
        if st["cost_by_worker"]:
            console.print("  cost by worker  " + "  ".join(
                f"{k} ${v:.2f}" for k, v in sorted(st["cost_by_worker"].items())), highlight=False)
        rg_line = Q.remote_gate_line(st["remote_gate"])
        if rg_line:
            console.print(Text("  " + rg_line), highlight=False)
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
        split = st.get("split") or {}
        if st.get("kind") == "split":                                  # SPEC-0290
            console.print(f"  split of {split.get('original')} into "
                          f"{len(split.get('parts') or [])} parts", highlight=False)
            for i, part in enumerate(split.get("parts") or [], 1):
                console.print(Text(f"  part {i}: {part.get('spec_id') or '(pending)'} "
                                   f"{part.get('title')}"), highlight=False)
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
        if raw:
            for ev in st["last_events"]:
                console.print(Text("  ") + R.campaign_event(ev), highlight=False)
            return
        _print_story(story, expand)

    @campaign_group.command("log")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--type", "types", multiple=True, help="only these event types (repeatable)")
    @click.option("--tail", type=int, default=None, help="last N events")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.campaign.log.v1 JSON")
    @click.pass_obj
    def campaign_log(cfg, campaign, types, tail, as_json):
        """The campaign's append-only ledger."""
        try:
            store.read_state(cfg, campaign)
        except ValueError as e:
            raise click.ClickException(str(e))
        events = store.read_ledger(cfg, campaign, types=types or None, tail=tail)
        if as_json:
            _dump_json({"schema": "nw.campaign.log.v1", "campaign": campaign, "events": events})
            return
        for ev in events:
            console.print(R.campaign_event(ev), highlight=False)
        if not events:
            console.print("[dim]no events[/]")

    @campaign_group.command("history")
    @click.argument("campaign", required=False, shell_complete=C.complete_campaign_id)
    @click.option("--list", "as_list", is_flag=True, help="list all campaign histories")
    @click.option("--rebuild-cache", "rebuild_cache", is_flag=True, help="rebuild the history cache")
    @click.option("--project", help="filter the list by project")
    @click.option("--state", type=click.Choice(["merged", "handed_off", "abandoned", "live", "all"]),
                  default="all", show_default=True)
    @click.option("--spec", "spec_id", help="filter the list by spec id")
    @click.option("--since", help="closed/created at or after YYYY-MM-DD or ISO-8601")
    @click.option("--until", help="closed/created at or before YYYY-MM-DD or ISO-8601")
    @click.option("--min-cost", type=float, help="minimum campaign cost in USD")
    @click.option("--json", "as_json", is_flag=True)
    @click.pass_context
    def campaign_history(ctx, campaign, as_list, rebuild_cache, project, state, spec_id, since,
                         until, min_cost, as_json):
        cfg = ctx.obj
        """Derived history for exactly one CAMPAIGN, --list, or --rebuild-cache.

        List, records, and CAMPAIGN use cache=None: caching runs when cache_dir
        is set; cache=False is always pure. --rebuild-cache always builds under cache_dir."""
        modes = sum(bool(x) for x in (campaign, as_list, rebuild_cache))
        if modes != 1:
            raise click.UsageError(
                "exactly one of CAMPAIGN, --list, and --rebuild-cache is required")
        if rebuild_cache:
            cmdline = click.core.ParameterSource.COMMANDLINE
            extra = {p for p in (
                "as_list", "rebuild_cache", "project", "state", "spec_id", "since", "until",
                "min_cost", "campaign", "as_json",
            ) if ctx.get_parameter_source(p) == cmdline}
            if "state" in extra and state == "all":
                extra.discard("state")
            if extra - {"as_json", "rebuild_cache"}:
                raise click.UsageError("--rebuild-cache accepts --json only")
            from . import history_cache as HC
            if HC._writable_root(cfg, True) is None:
                raise click.ClickException("cache: not writable")
            def progress(done, total):
                if done % 50 == 0 and sys.stderr.isatty():
                    R.err_console.print(Text(f"history: {done}/{total}"))
            try:
                result = H.rebuild_cache(cfg, progress=progress)
            except OSError:
                raise click.ClickException("cache: not writable")
            if as_json:
                _dump_json(result)
                return
            console.print(
                Text(f"history cache rebuilt: {result['campaigns']} campaigns, "
                     f"{result['records']} records"),
                highlight=False,
            )
            return
        try:
            if as_list:
                def progress(done, total):
                    if done % 50 == 0 and sys.stderr.isatty():
                        R.err_console.print(Text(f"history: {done}/{total}"))
                rows = H.filter_index(
                    H.history_index(cfg, progress=progress), project=project, state=state,
                    spec=spec_id, since=since, until=until, min_cost=min_cost,
                )
                _history_cache_unwritable_warn(cfg)
                if as_json:
                    _dump_json({"schema": SC.HISTORY_INDEX_SCHEMA, "rows": rows})
                    return
                table = Table("campaign", "state", "project", "spec", "cost", "dominant",
                              title=f"Campaign history ({len(rows)})")
                for row in rows:
                    table.add_row(
                        Text(str(row["campaign"])), Text(str(row.get("state") or "")),
                        Text(str(row.get("project") or "")), Text(str(row.get("spec_id") or "")),
                        Text(f"${float(row.get('cost_usd') or 0):.2f}"),
                        Text(str((row.get("dominant") or {}).get("wall") or "")),
                    )
                console.print(table)
                return
            doc = H.campaign_history(cfg, campaign)
            _history_cache_unwritable_warn(cfg)
        except ValueError as exc:
            if campaign and not store.campaign_dir(cfg, campaign).is_dir():
                raise click.ClickException(f"campaign {campaign} not found")
            raise click.UsageError(str(exc))
        if as_json:
            _dump_json(doc)
            return
        header = doc["header"]
        console.print(Text(
            f"{header['campaign']}  {header.get('state') or '-'}  "
            f"{header.get('project') or '-'}  loops {header.get('loops')}/{header.get('loop_max')}"
        ))
        phases = Table("phase", "wall", "share", "bar", "run", "gate", "gate-wait",
                       "runner-down", "other", "cost", "runs")
        wall = max(1, header.get("wall_s") or 0)
        for row in doc["phases"]:
            share = row["wall_s"] / wall
            phases.add_row(
                Text(row["phase"]), Text(str(row["wall_s"])), Text(f"{share:.0%}"),
                Text("█" * round(share * 12)), Text(str(row["run_s"])), Text(str(row["gate_s"])),
                Text(str(row["gate_wait_s"])), Text(str(row["runner_down_s"])),
                Text(str(row["other_s"])), Text(f"${row['cost_usd']:.2f}"),
                Text(str(row["runs"])),
            )
        console.print(phases)
        dominant = doc["dominant"]
        console.print(Text(f"dominant: wall={dominant['wall'] or '-'} "
                           f"cost={dominant['cost'] or '-'} working={dominant['working'] or '-'}"))
        for row in doc["timeline"][-30:]:
            console.print(Text(f"{row.get('ts') or '-'}  {row.get('kind') or '-'}  "
                               f"{row.get('summary') or ''}"))
        if doc["runs"]:
            table = Table("run", "persona", "phase", "status", "cost", title="Runs")
            for row in doc["runs"]:
                table.add_row(*(Text(str(value or "")) for value in (
                    row["run_id"], row["persona"], row["phase"], row["status"],
                    f"${float(row.get('cost_usd') or 0):.2f}",
                )))
            console.print(table)
        if doc["gaps"]:
            console.print(Text("gaps:"))
            for gap in doc["gaps"]:
                console.print(Text(f"  {gap}"))

    @campaign_group.command("records")
    @click.option("--campaign")
    @click.option("--agent", multiple=True)
    @click.option("--kind", multiple=True)
    @click.option("--project")
    @click.option("--severity", multiple=True)
    @click.option("--status", multiple=True)
    @click.option("--since")
    @click.option("--until")
    @click.option("--query")
    @click.option("--limit", type=click.IntRange(min=0), default=None)
    @click.option("--json", "as_json", is_flag=True)
    @click.pass_obj
    def campaign_records(cfg, campaign, agent, kind, project, severity, status, since, until,
                         query, limit, as_json):
        """Search derived findings and decisions across campaigns."""
        try:
            rows = H.history_records(
                cfg, campaign=campaign, agent=agent or None, kind=kind or None, project=project,
                severity=severity or None, status=status or None, since=since, until=until,
                query=query, limit=limit if limit is not None else (None if as_json else 200),
            )
            _history_cache_unwritable_warn(cfg)
        except ValueError as exc:
            raise click.UsageError(str(exc))
        if as_json:
            _dump_json({"schema": SC.HISTORY_RECORD_SCHEMA, "records": rows})
            return
        for row in rows:
            console.print(Text("  ".join(str(row.get(key) or "-") for key in (
                "ts", "source", "kind", "agent", "campaign", "severity", "status", "title"
            ))))

    @campaign_group.command("runs")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--json", "as_json", is_flag=True, help="emit nw.campaign.runs.v1 JSON")
    @click.pass_obj
    def campaign_runs(cfg, campaign, as_json):
        """One row per persona run: cost, exit, errors, transient?, verdict status."""
        try:
            runs = RN.campaign_runs(cfg, campaign)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.campaign.runs.v1", "campaign": campaign, "runs": runs})
            return
        if not runs:
            console.print("[dim]no runs yet[/]")
            return
        console.print(_runs_table(campaign, runs))

    @campaign_group.command("run")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.argument("run")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.campaign.run.v1 JSON")
    @click.pass_obj
    def campaign_run(cfg, campaign, run, as_json):
        """One run in full: verdict, must_fix / blockers, error / stderr tails, file paths.
        RUN is a run id (001-implementer) or a unique prefix (001)."""
        try:
            rec = RN.campaign_run(cfg, campaign, run)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.campaign.run.v1", "campaign": campaign, "run": rec})
            return
        _print_run(rec)

    @campaign_group.command("tail")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--run", "run", default=None,
                  help="run id (001-implementer) or unique prefix; default the newest run")
    @click.option("--follow", "-f", is_flag=True, help="keep reading until the run ends")
    @click.option("--raw", is_flag=True, help="print the stream-json lines as they are")
    @click.option("--follow-campaign", is_flag=True,
                  help="follow the campaign across its runs until interrupted (the herdr run "
                       "pane viewer, SPEC-0366); --run/--follow are ignored")
    @click.pass_obj
    def campaign_tail(cfg, campaign, run, follow, raw, follow_campaign):
        """A persona run as a readable transcript (SPEC-0362): tool calls with a short result
        summary, assistant text, running cost vs budget, the final result. Reads
        runs/<run>/stream.jsonl; a run launched without stream capture shows its result."""
        from . import tail as TL
        if follow_campaign:
            import signal

            from .scoped_signals import scoped

            def _bye(signum, frame):
                raise KeyboardInterrupt
            # SPEC-0528: the handlers end with the command (an in-process run keeps none)
            with scoped({signal.SIGTERM: _bye, signal.SIGHUP: _bye}):
                try:
                    rc = TL.follow_campaign(cfg, campaign, raw=raw, out=console)
                except ValueError as e:
                    raise click.ClickException(str(e))
                except KeyboardInterrupt:
                    rc = 130
            sys.exit(rc)
        try:
            run_dir = TL.resolve(cfg, campaign, run)
        except ValueError as e:
            raise click.ClickException(str(e))
        try:
            sys.exit(TL.tail(cfg, run_dir, follow=follow, raw=raw, out=console))
        except KeyboardInterrupt:
            sys.exit(130)

    @campaign_group.command("note")
    @click.argument("args", nargs=-1, required=True)
    @click.option("--author", default=None,
                  help="persona or agent name; recorded as by=agent:<author>")
    @click.pass_obj
    def campaign_note(cfg, args, author):
        """Append a note. CAMPAIGN defaults to $NW_CAMPAIGN (set inside persona runs).

        \b
        nw campaign note SPEC-0170-20261026-a3f9 "checked the fixture"
        NW_CAMPAIGN=… nw campaign note "found the cause" --author implementer
        """
        env_id = os.environ.get("NW_CAMPAIGN")
        args = list(args)
        if len(args) >= 2 and store.campaign_dir(cfg, args[0]).is_dir():
            campaign, text = args[0], " ".join(args[1:])
        elif env_id:
            campaign, text = env_id, " ".join(args)
        else:
            raise click.UsageError(
                "campaign required: pass CAMPAIGN before the text or set NW_CAMPAIGN")
        try:
            store.read_state(cfg, campaign)
        except ValueError as e:
            raise click.ClickException(str(e))
        by, who = note_actor(cfg, os.environ, author)
        rec = store.append_event(cfg, campaign, "note", {"text": text, "author": who}, by=by)
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
        """Export campaigns, runs and findings as flat tables (nw.campaign.export.v1) for
        analysing how well specs were sized. Read-only; every run is a full snapshot.

        \b
        nw campaign export                          jsonl files under <data_dir>/agentic/exports/
        nw campaign export --format csv --out /tmp/x --since 2026-10-01
        nw campaign export --table campaigns --stdout | duckdb -c "select * from read_json_auto('/dev/stdin')"
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
                      *out_dir.glob(".nw-export-*")):
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
                project_cfg = None       # no .nw/project.yaml yet: render without context files
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
