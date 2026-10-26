"""wt CLI (click + rich). Subcommands:
  report / export / review / topics / snapshot / map / override
  tasks / agenda / digest / add / state / done   (org-mode task management, SPEC-0004/0010)
  idea / ideas                                   (idea capture & lifecycle, SPEC-0012)
  next                                           (next-step hints for open ideas)
  hub                                            (triage JSON across ideas/specs/outbox, SPEC-0052)
  tui                                            (optional interactive ideas desk, SPEC-0099)
  spec new / generate / export / schemes / verify (idea -> spec promotion SPEC-0013, spec ->
                                                   task generation SPEC-0014, outbound export
                                                   SPEC-0015, pluggable export schemes
                                                   SPEC-0016, post-hoc DoD audit SPEC-0117)
  skills install / list                          (agent workflow skills, SPEC-0017)
  completion / completion install                (shell completion, SPEC-0022)
  sheet plan / record                            (JSON-only sheet-sync diff, SPEC-0125;
                                                   wt never touches the network)
  ingest-meetings / refresh-meetings
Run with no command for a today + this-week dashboard.
"""
import datetime as dt
import json
import re
import sys

import click
from rich.markup import escape

from . import report as R
from . import completion as C
from .config import load_config
from .console import console
from .dates import parse_date, week_days
from .history import snapshot
from .meetings import ingest_meetings, refresh_meetings
from .export import export_spec
from .export_schemes import available as available_schemes
from .org_write import (IDEA_KINDS, add_idea, add_task, clock_in, clock_out, mark_done,
                        reindex_ideas, resolve_selector, set_idea_kind, set_idea_priority,
                        set_idea_project, set_idea_tags, set_idea_workstream,
                        set_state_by_selector)
from . import explore as EX
from . import idea_ext as IX
from .extension_schema import enum_values_for
from .idea_ext import resolve_ext_project
from .rules import add_override_rec, known_projects, load_mappings, set_facets
from .specs import (SPEC_STATUSES, generate_from_spec, promote_idea, scaffold_outbound,
                    seed_ideas_from_epic)
from . import skills as SK
from . import sheet_sync as SHS


@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--split", is_flag=True, help="diagnostic: Claude-generating vs your-turn time")
@click.pass_context
def cli(ctx, split):
    """wt — passive work-time tracker. Turns Claude transcripts + meetings into
    per-topic hours. Run with no command for a today + this-week dashboard."""
    ctx.obj = load_config()
    if ctx.invoked_subcommand is None:
        cfg = ctx.obj
        console.rule("[bold cyan]today[/]")
        R.report(cfg, split=split)
        console.rule("[bold cyan]this week[/]")
        R.report(cfg, week="now", split=split)
        console.print("[dim]› wt report --week · wt review --day --fix · wt refresh-meetings[/]")


@cli.command("report")
@click.argument("date", required=False)
@click.option("--week", "-w", is_flag=True, help="weekly view (of DATE, or this week)")
@click.option("--last", type=int, help="last N days")
@click.option("--by", help="group by a facet axis (e.g. project, area); default: topic",
              shell_complete=C.complete_by_axis)
@click.option("--md", is_flag=True, help="also write a markdown file to reports/")
@click.option("--split", is_flag=True, help="diagnostic: Claude-generating vs your-turn time")
@click.pass_obj
def report_cmd(cfg, date, week, last, by, md, split):
    """Per-topic hours. DATE optional (YYYY-MM-DD).

    \b
    wt report                 today
    wt report --week          this week
    wt report 2026-06-01      that day
    wt report -w 2026-06-01   that week
    wt report --last 7        last 7 days
    wt report --week --by project   this week grouped by the 'project' facet
    """
    if last:
        R.report(cfg, last=last, md=md, split=split, by=by)
    elif week:
        R.report(cfg, week=date or "now", md=md, split=split, by=by)
    else:
        R.report(cfg, day=date or "now", md=md, split=split, by=by)


@cli.command("export")
@click.argument("date", required=False)
@click.option("--week", "-w", is_flag=True, help="export the week of DATE (or this week)")
@click.option("--last", type=int, help="export the last N days")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv",
              help="output format (default: csv)")
@click.option("-o", "--out", type=click.Path(),
              help="output path (default: exports/<scope>.<fmt>)")
@click.pass_obj
def export_cmd(cfg, date, week, last, fmt, out):
    """Save a tidy dataset (date, topic, hours, facet columns). DATE/-w/--last optional;
    default exports ALL available days."""
    tz = cfg["_tz"]
    if last:
        today = dt.datetime.now(tz).date()
        days = [(today - dt.timedelta(days=i)).isoformat() for i in range(last - 1, -1, -1)]
        name = f"last{last}"
    elif week:
        wd = week_days(parse_date(date or "now", tz)); days = [x.isoformat() for x in wd]
        name = f"week_{days[0]}"
    elif date:
        days = [parse_date(date, tz).isoformat()]; name = days[0]
    else:
        days = None; name = "all"
    R.export(cfg, days, name, fmt, out)


@cli.command("review")
@click.argument("date", required=False)
@click.option("--week", "-w", is_flag=True, help="weekly view (of DATE, or this week)")
@click.option("--fix", "interactive", is_flag=True, help="interactively map unclassified topics")
@click.pass_obj
def review_cmd(cfg, date, week, interactive):
    """Segment breakdown, unclassified inbox, and orphaned overrides. DATE optional."""
    if week:
        R.review(cfg, week=date or "now", interactive=interactive)
    else:
        R.review(cfg, day=date or "now", interactive=interactive)


@cli.command("topics")
@click.option("--last", type=int, help="restrict to last N days (default: all time)")
@click.option("--unmapped", is_flag=True, help="show only topics with no mapping yet")
@click.option("--by", help="regroup by facet axis (e.g. bucket, project); all-time condensed view",
              shell_complete=C.complete_by_axis)
@click.pass_obj
def topics_cmd(cfg, last, unmapped, by):
    """List discovered base topics (+ hours) and the current mappings/overrides.

    \b
    wt topics                 all-time base topics + facets
    wt topics --unmapped      only topics with no effective mapping
    wt topics --by bucket     all-time hours rolled up by facet axis
    wt topics --by bucket --last 30
    """
    try:
        R.topics(cfg, last, unmapped, by=by)
    except ValueError as e:
        raise click.ClickException(str(e)) from e


@cli.command("tasks")
@click.option("--state", help="only this TODO state (e.g. INPROGRESS)",
              shell_complete=C.complete_todo_state)
@click.option("--tag", help="only tasks with this tag (grouped tags are split)")
@click.option("--project", help="only tasks under this project/category",
              shell_complete=C.complete_project)
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="only this priority (A/B/C)")
@click.option("--workstream", help="only this :WORKSTREAM: (SPEC-0090)",
              shell_complete=C.complete_workstream)
@click.option("--all", "all_done", is_flag=True, help="include done + structural headings")
@click.option("--key", is_flag=True, help="only tasks with a topic_key (JIRA-linked)")
@click.option("--json", "as_json", is_flag=True,
              help="emit wt.tasks.v1 JSON on stdout (SPEC-0092)")
@click.option("--sort", type=click.Choice(list(R.TASK_SORT_NAMES), case_sensitive=False),
              help="override order (SPEC-0094; default: done/pri/project/heading)")
@click.option("--desc", is_flag=True, help="reverse --sort order (SPEC-0094)")
@click.pass_obj
def tasks_cmd(cfg, state, tag, project, priority, workstream, all_done, key, as_json,
              sort, desc):
    """List org tasks. Default: open, actionable items (a TODO state that isn't done).

    \b
    wt tasks                    open tasks
    wt tasks --state INPROGRESS only that state
    wt tasks --tag example         tagged example
    wt tasks --project Example     under that project
    wt tasks --priority A       only [#A]
    wt tasks --workstream ops   only that workstream (SPEC-0090)
    wt tasks --key              only JIRA-linked tasks
    wt tasks --all              include done + structural headings
    wt tasks --json             wt.tasks.v1 for agents (SPEC-0092)
    wt tasks --sort priority    A before B before C (SPEC-0094)
    """
    R.tasks(cfg, state=state, tag=tag, project=project, priority=priority,
            all_done=all_done, key=key, workstream=workstream, as_json=as_json,
            sort=sort, desc=desc)


@cli.command("agenda")
@click.argument("date", required=False)
@click.option("--week", "-w", is_flag=True, help="weekly view (of DATE, or this week)")
@click.option("--last", type=int, help="last N days")
@click.option("--ideas", "include_ideas", is_flag=True,
              help="include dated ideas alongside tasks (SPEC-0088; default: tasks only)")
@click.option("--state", help="only this TODO/idea state (SPEC-0096)",
              shell_complete=C.complete_todo_state)
@click.option("--kind", type=click.Choice(list(IDEA_KINDS), case_sensitive=False),
              help="only this idea kind when --ideas (SPEC-0096)")
@click.option("--tag", help="only this tag (SPEC-0096)")
@click.option("--project", help="only this project (SPEC-0096)",
              shell_complete=C.complete_project)
@click.option("--workstream", help="only this :WORKSTREAM: (SPEC-0096)",
              shell_complete=C.complete_workstream)
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="only this priority (SPEC-0096)")
@click.pass_obj
def agenda_cmd(cfg, date, week, last, include_ideas, state, kind, tag, project, workstream,
               priority):
    """Tasks by SCHEDULED/DEADLINE over a date scope, plus overdue. DATE optional.

    Default excludes ideas (SPEC-0088). Pass --ideas to include dated ideas too.

    \b
    wt agenda                 today (tasks only)
    wt agenda --week          this week
    wt agenda --ideas         today including dated ideas
    wt agenda --project P     only that project (SPEC-0096)
    wt agenda 2026-07-20      that day
    wt agenda -w 2026-07-20   that week
    wt agenda --last 7        last 7 days
    """
    kw = {"include_ideas": include_ideas, "state": state, "kind": kind, "tag": tag,
          "project": project, "workstream": workstream, "priority": priority}
    if last:
        R.agenda(cfg, last=last, **kw)
    elif week:
        R.agenda(cfg, week=date or "now", **kw)
    else:
        R.agenda(cfg, day=date or "now", **kw)


@cli.command("digest")
@click.argument("date", required=False)
@click.option("--week", "-w", is_flag=True, help="weekly view (of DATE, or this week)")
@click.option("--last", type=int, help="last N days")
@click.option("--by", help="group by 'project' instead of per task",
              shell_complete=C.complete_by_axis)
@click.pass_obj
def digest_cmd(cfg, date, week, last, by):
    """Tracked hours joined to org tasks by JIRA key, over a date scope. DATE optional.

    \b
    wt digest                 today: tasks + tracked hours
    wt digest --week          this week
    wt digest -w 2026-07-06   that week
    wt digest --last 7        last 7 days
    wt digest --by project    group by task project
    """
    if last:
        R.digest(cfg, last=last, by=by)
    elif week:
        R.digest(cfg, week=date or "now", by=by)
    else:
        R.digest(cfg, day=date or "now", by=by)


@cli.command("add")
@click.argument("text", nargs=-1, required=True)
@click.option("--state", help="TODO state (default: the file's first active keyword)",
              shell_complete=C.complete_todo_state)
@click.option("--tag", "tags", multiple=True, help="tag (repeatable)")
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="priority cookie (A/B/C)")
@click.option("--scheduled", help="SCHEDULED date (YYYY-MM-DD)")
@click.option("--deadline", help="DEADLINE date (YYYY-MM-DD)")
@click.option("--file", type=click.Path(exists=False),
              help="target .org file (default: org_capture_file)")
@click.option("--project", help="associate a known project/bucket (see `wt projects`)",
              shell_complete=C.complete_project)
@click.option("--epic", help="associate a known epic (SPEC-NNNN or outbound PROJ-NNNN)",
              shell_complete=C.complete_epic)
@click.option("--key", help="JIRA key (e.g. DEMO-100) to set as the task's topic_key")
@click.option("--workstream", help="curated :WORKSTREAM: lane (SPEC-0090)",
              shell_complete=C.complete_workstream)
@click.pass_obj
def add_cmd(cfg, text, state, tags, priority, scheduled, deadline, file, project, epic, key,
            workstream):
    """Capture a new task (append a headline to the capture file). TEXT needs no quotes.

    \b
    wt add DEMO-100 wire up the thing
    wt add "fix the flaky test" --priority A --tag ci --tag flaky
    wt add "prep review" --scheduled 2026-07-20 --file ~/work/org/agenda.org
    wt add "wire the dashboard" --project Demo-ATS --key DEMO-100
    wt add "ops chore" --workstream ops
    """
    try:
        path, headline = add_task(cfg, " ".join(text), state=state, tags=tags,
                                  priority=priority, scheduled=scheduled, deadline=deadline,
                                  file=file, project=project, epic=epic, key=key,
                                  workstream=workstream)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] added  [white]{headline}[/]  [dim]{path}[/]")


class IdeaGroup(click.Group):
    """`wt idea TEXT…` captures; `wt idea log|summary|…` are subcommands (SPEC-0024/0031)."""

    def resolve_command(self, ctx, args):
        args = list(args)
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-h", "--help"):
                break
            if a.startswith("--"):
                name = a.split("=", 1)[0]
                if name in ("--state", "--tag", "--priority", "--project", "--epic",
                            "--key", "--kind", "--workstream"):
                    if "=" not in a and i + 1 < len(args):
                        i += 2
                        continue
                i += 1
                continue
            if a.startswith("-") and len(a) <= 2:
                i += 2 if i + 1 < len(args) else 1
                continue
            break
        if i < len(args) and args[i] in self.commands:
            return super().resolve_command(ctx, args)
        return super().resolve_command(ctx, ["capture", *args])


@cli.group("idea", cls=IdeaGroup)
def idea_group():
    """Capture or enrich ideas. Bare TEXT captures; subcommands log/show/….

    \b
    wt idea Investigate self-assessing AI usage          # capture
    wt idea log IDEA-011 --note "finding…"               # append Log
    wt idea show IDEA-011                                # read enrichment
    """


@idea_group.command("capture")
@click.argument("text", nargs=-1, required=True)
@click.option("--state", help="idea state (default: IDEA)",
              shell_complete=C.complete_idea_state)
@click.option("--tag", "tags", multiple=True, help="tag (repeatable)")
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="priority cookie (A/B/C)")
@click.option("--project", help="associate a known project/bucket (see `wt projects`)",
              shell_complete=C.complete_project)
@click.option("--epic", help="associate a known epic (SPEC-NNNN or outbound PROJ-NNNN)",
              shell_complete=C.complete_epic)
@click.option("--key", help="JIRA key (e.g. DEMO-100) to set as the idea's topic_key")
@click.option("--kind", type=click.Choice(list(IDEA_KINDS), case_sensitive=False),
              help="capture kind label (default: idea; SPEC-0044)")
@click.option("--workstream", help="curated :WORKSTREAM: lane (SPEC-0090)",
              shell_complete=C.complete_workstream)
@click.option("--force-idea", "force_idea", is_flag=True,
              help="keep as idea even with --kind bug; suppress routing warn (SPEC-0091)")
@click.pass_obj
def idea_capture_cmd(cfg, text, state, tags, priority, project, epic, key, kind, workstream,
                     force_idea):
    """Capture an idea (default when you pass bare text after `wt idea`)."""
    joined = " ".join(text)
    if kind is not None and str(kind).strip().lower() == "bug" and not force_idea:
        _warn_bug_as_idea(joined, tags=tags, priority=priority, project=project,
                          epic=epic, key=key, workstream=workstream)
    try:
        path, headline, idea_id = add_idea(cfg, joined, state=state, tags=tags,
                                           priority=priority, project=project, epic=epic,
                                           key=key, kind=kind, workstream=workstream)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] {idea_id}  [white]{headline}[/]  [dim]{path}[/]")


def _warn_bug_as_idea(text, *, tags=(), priority=None, project=None, epic=None, key=None,
                      workstream=None):
    """Soft routing warn for --kind bug (SPEC-0091). Still allows capture."""
    parts = ["wt", "add", _shell_quote(text)]
    for t in tags:
        parts.extend(["--tag", _shell_quote(t)])
    if priority:
        parts.extend(["--priority", priority])
    if project:
        parts.extend(["--project", _shell_quote(project)])
    if epic:
        parts.extend(["--epic", _shell_quote(epic)])
    if key:
        parts.extend(["--key", _shell_quote(key)])
    if workstream:
        parts.extend(["--workstream", _shell_quote(workstream)])
    suggested = " ".join(parts)
    console.print(
        "[yellow]note[/]: `--kind bug` still writes an /idea/. "
        "If this is actionable work, prefer a task instead:")
    console.print(f"  [cyan]{suggested}[/]")
    console.print("[dim]Pass --force-idea to keep a design-needed bug as an idea silently.[/]")


def _shell_quote(value: str) -> str:
    """Minimal quoting for suggested CLI lines (spaces → double quotes)."""
    s = str(value)
    if not s:
        return '""'
    if any(c.isspace() for c in s) or any(c in s for c in "\"'`$"):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _idea_log(cfg, selector, note):
    try:
        path = EX.append_log(cfg, selector, note)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    console.print(f"[green]✓[/] log ← {selector}  [dim]{path}[/]")


@idea_group.command("log")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--note", required=True, help="finding to append under Log (date+time)")
@click.pass_obj
def idea_log_cmd(cfg, selector, note):
    """Append a Log entry (date+time); ensure Summary/questions/Log skeleton; INCUBATE if IDEA."""
    _idea_log(cfg, selector, note)


@idea_group.command("explore", hidden=True)
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--note", required=True, help="finding to append under Log (date+time)")
@click.pass_obj
def idea_explore_cmd(cfg, selector, note):
    """Deprecated alias of `wt idea log` (SPEC-0031)."""
    _idea_log(cfg, selector, note)


@idea_group.command("normalize-logs")
@click.argument("selector", required=False, shell_complete=C.complete_idea_id)
@click.pass_obj
def idea_normalize_logs_cmd(cfg, selector):
    """Rewrite plain Log headlines to inactive org timestamps (SPEC-0032 migration).

    \b
    wt idea normalize-logs              # whole org_ideas_file
    wt idea normalize-logs IDEA-020     # file containing that idea
    """
    try:
        results = EX.normalize_idea_log_stamps(cfg, selector)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    total = 0
    for path, n in results:
        total += n
        console.print(f"[green]✓[/] normalize-logs  {n} rewritten  [dim]{path}[/]")
    if total == 0:
        console.print("[dim]normalize-logs: nothing to rewrite[/]")


@idea_group.command("mark-explored")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.pass_obj
def idea_explored_cmd(cfg, selector):
    """Record an exploration pass on an idea (SPEC-0081).

    Increments `:EXPLORED:` and refreshes `:EXPLORED_AT:`. Nothing else ever writes them, so an
    INCUBATE idea with no marker is one that was seeded with context but never explored.

    Named `mark-explored`, not `explored`, because `wt idea explore` is a hidden alias for
    `log` (SPEC-0031) — one letter apart would mean typing the alias and silently appending a
    Log entry instead of recording the pass.

    \b
    wt idea mark-explored IDEA-105
    """
    try:
        n = EX.mark_explored(cfg, selector)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    console.print(f"[green]✓[/] {selector} explored [dim](pass {n})[/]")


@idea_group.command("backfill-stamps")
@click.pass_obj
def idea_backfill_stamps_cmd(cfg):
    """Give pre-SPEC-0076 ideas a best-effort `:UPDATED:` stamp.

    Derived from the newest Log headline, else the CLOSED: stamp, else skipped — never invented.
    Idempotent: ideas that already have `:UPDATED:` are untouched, so a re-run does nothing.
    New captures get `:CREATED:`/`:UPDATED:` automatically; this is only for the backlog.
    """
    results = EX.backfill_idea_stamps(cfg)
    stamped = [(i, s) for i, s in results if s != "skipped"]
    for idea_id, source in results:
        style = "dim" if source == "skipped" else "green"
        console.print(f"[{style}]{'·' if source == 'skipped' else '✓'}[/] {idea_id}  "
                      f"[dim]{source}[/]")
    if not results:
        console.print("[dim]backfill-stamps: every idea already has :UPDATED:[/]")
    else:
        console.print(f"[dim]{len(stamped)} stamped, "
                      f"{len(results) - len(stamped)} skipped (no Log or CLOSED signal)[/]")


@idea_group.command("summary")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--set", "text", required=True, help="replace Summary body")
@click.pass_obj
def idea_summary_cmd(cfg, selector, text):
    """Replace the idea's Summary section."""
    try:
        path = EX.set_summary(cfg, selector, text)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] summary ← {selector}  [dim]{path}[/]")


@idea_group.command("questions")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--set", "text", default=None, help="replace section (each line → an OPEN question)")
@click.option("--add", "bullet", default=None, help="append one question headline")
@click.option("--state", type=click.Choice(["open", "resolved"]), default="open",
              show_default=True, help="state for --add")
@click.option("--resolve", "resolve_idx", type=int, multiple=True,
              help="flip the Nth question (1-based) to RESOLVED; repeatable")
@click.option("--resolve-all", is_flag=True, help="flip every open question to RESOLVED")
@click.option("--unresolve", "unresolve_idx", type=int, default=None,
              help="flip the Nth question (1-based) back to OPEN (SPEC-0101)")
@click.option("--edit", "edit_idx", type=int, default=None,
              help="replace the Nth question's text; needs --text (SPEC-0101)")
@click.option("--text", "edit_text", default=None, help="new text for --edit")
@click.option("--delete", "delete_idx", type=int, default=None,
              help="remove the Nth question (1-based) (SPEC-0101)")
@click.option("--priority", "prio_idx", type=int, default=None,
              help="set/clear the Nth question's [#A|B|C]; needs --pri (SPEC-0101)")
@click.option("--body", "body_idx", type=int, default=None,
              help="set/clear the Nth question's rationale; needs --text (SPEC-0112)")
@click.option("--pri", type=click.Choice(["A", "B", "C", "none"], case_sensitive=False),
              default=None, help="cookie for --priority ('none' clears it)")
@click.pass_obj
def idea_questions_cmd(cfg, selector, text, bullet, state, resolve_idx, resolve_all,
                       unresolve_idx, edit_idx, edit_text, delete_idx, prio_idx, pri,
                       body_idx):
    """Replace, append, resolve or edit Open questions (`*** OPEN|RESOLVED` headlines,
    SPEC-0054/0101). Every N is a 1-based index into `wt idea show`'s order.

    \b
    wt idea questions IDEA-012 --resolve 1 --resolve 3
    wt idea questions IDEA-012 --resolve-all
    wt idea questions IDEA-012 --unresolve 2
    wt idea questions IDEA-012 --edit 2 --text "sharper wording"
    wt idea questions IDEA-012 --delete 3
    wt idea questions IDEA-012 --priority 1 --pri A
    wt idea questions IDEA-012 --body 2 --text "ANSI: hex was invisible on dark"
    """
    if resolve_idx and resolve_all:
        raise click.UsageError("pass either --resolve or --resolve-all, not both")
    chose = [text is not None and edit_idx is None and body_idx is None,
             bullet is not None, bool(resolve_idx) or resolve_all,
             unresolve_idx is not None, edit_idx is not None, delete_idx is not None,
             prio_idx is not None, body_idx is not None]
    if sum(chose) != 1:
        raise click.UsageError("pass exactly one of --set / --add / --resolve / --resolve-all / "
                               "--unresolve / --edit / --delete / --priority / --body")
    if edit_idx is not None and edit_text is None:
        raise click.UsageError("--edit needs --text")
    if prio_idx is not None and pri is None:
        raise click.UsageError("--priority needs --pri (A/B/C or 'none')")
    if body_idx is not None and edit_text is None:
        raise click.UsageError("--body needs --text (use --text '' to clear)")
    try:
        if text is not None:
            path = EX.set_questions(cfg, selector, text)
            console.print(f"[green]✓[/] questions ← {selector}  [dim]{path}[/]")
        elif bullet is not None:
            path = EX.add_question(cfg, selector, bullet, state=state)
            console.print(f"[green]✓[/] questions ← {selector}  [dim]{path}[/]")
        elif unresolve_idx is not None:
            path = EX.unresolve_question(cfg, selector, unresolve_idx)
            console.print(f"[green]✓[/] question #{unresolve_idx} reopened ← {selector}"
                          f"  [dim]{path}[/]")
        elif edit_idx is not None:
            path = EX.edit_question(cfg, selector, edit_idx, edit_text)
            console.print(f"[green]✓[/] question #{edit_idx} rewritten ← {selector}"
                          f"  [dim]{path}[/]")
        elif delete_idx is not None:
            path = EX.delete_question(cfg, selector, delete_idx)
            console.print(f"[green]✓[/] question #{delete_idx} deleted ← {selector}"
                          f"  [dim]{path}[/]")
        elif body_idx is not None:
            path = EX.set_question_body(cfg, selector, body_idx, edit_text)
            shown = "cleared" if not edit_text.strip() else "set"
            console.print(f"[green]✓[/] question #{body_idx} rationale {shown} ← {selector}"
                          f"  [dim]{path}[/]")
        elif prio_idx is not None:
            want = None if pri.lower() == "none" else pri.upper()
            path = EX.set_question_priority(cfg, selector, prio_idx, want)
            console.print(f"[green]✓[/] question #{prio_idx} priority "
                          f"{'cleared' if want is None else want} ← {selector}  [dim]{path}[/]")
        else:
            n = EX.resolve_questions(cfg, selector, None if resolve_all else resolve_idx)
            console.print(f"[green]✓[/] {n} question(s) resolved ← {selector}"
                          if n else f"[dim]· nothing to resolve on {selector}[/]")
    except ValueError as e:
        raise click.ClickException(str(e))


@idea_group.command("normalize-questions")
@click.argument("selector", required=False, shell_complete=C.complete_idea_id)
@click.pass_obj
def idea_normalize_questions_cmd(cfg, selector):
    """Rewrite legacy Open-questions forms to `*** OPEN|RESOLVED` headlines (SPEC-0054).

    \b
    wt idea normalize-questions           # whole org_ideas_file
    wt idea normalize-questions IDEA-063  # file containing that idea
    """
    try:
        results = EX.normalize_idea_questions(cfg, selector)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    total = 0
    for path, n in results:
        total += n
        if n:
            console.print(f"[green]✓[/] normalize-questions  {n} idea(s)  [dim]{path}[/]")
    if total == 0:
        console.print("[dim]normalize-questions: nothing to rewrite[/]")


@idea_group.command("project")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--set", "project", default=None,
              help="associate a project/bucket (see `wt projects`); empty string clears")
@click.pass_obj
def idea_project_cmd(cfg, selector, project):
    """Set or clear an idea's `:PROJECT:` (SPEC-0109). Before this, `--project` existed only at
    capture, so an idea captured without one could never be given a project.

    Unknown names warn but still write (SPEC-0018 is deliberately permissive).

    \b
    wt idea project IDEA-012 --set Example
    wt idea project IDEA-012 --set ""     # clear
    """
    if project is None:
        raise click.UsageError("pass --set (use --set '' to clear)")
    try:
        path = set_idea_project(cfg, selector, project)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    shown = project.strip() or "(none)"
    console.print(f"[green]✓[/] project ← {selector}  [white]{shown}[/]  [dim]{path}[/]")


@idea_group.command("tags")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--set", "tags", default=None,
              help="replace the headline's tags (comma/space separated; empty string clears)")
@click.pass_obj
def idea_tags_cmd(cfg, selector, tags):
    """Replace the `:tag:tag:` block on an idea headline (SPEC-0101). Whole-set replace, so
    read `wt idea show` first if you mean to add one.

    \b
    wt idea tags IDEA-012 --set "tui,ideas"
    wt idea tags IDEA-012 --set ""        # strip all tags
    """
    if tags is None:
        raise click.UsageError("pass --set (use --set '' to clear)")
    names = [t for t in re.split(r"[,\s]+", tags.strip()) if t]
    try:
        path = set_idea_tags(cfg, selector, names)
    except ValueError as e:
        raise click.ClickException(str(e)) from e
    shown = ":".join(names) or "(none)"
    console.print(f"[green]✓[/] tags ← {selector}  [white]{shown}[/]  [dim]{path}[/]")


@idea_group.command("close")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--as", "outcome", type=click.Choice(["drop", "researched"]), required=True,
              help="terminal outcome: drop (won't-do) or researched (explored, no spec)")
@click.option("--reason", default=None, help="why it's closed (stored as a property + Log line)")
@click.option("--folded-into", "folded_into", default=None,
              help="idea/spec id this idea's value moved into")
@click.option("--superseded-by", "superseded_by", default=None,
              help="idea/spec id that supersedes this one")
@click.pass_obj
def idea_close_cmd(cfg, selector, outcome, reason, folded_into, superseded_by):
    """Close an idea with a terminal outcome + reason/pointers (SPEC-0055/0056/0057).

    \b
    wt idea close IDEA-062 --as researched --reason "…" --folded-into IDEA-059
    wt idea close IDEA-070 --as drop --reason "duplicate of IDEA-071"
    """
    try:
        path = EX.close_idea(cfg, selector, outcome, reason=reason,
                             folded_into=folded_into, superseded_by=superseded_by)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] closed {selector} as [bold]{outcome}[/]  [dim]{path}[/]")


@idea_group.command("state")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.argument("new_state", shell_complete=C.complete_idea_state)
@click.pass_obj
def idea_state_cmd(cfg, selector, new_state):
    """Force an idea's lifecycle keyword (SPEC-0135). Bare flip — no CLOSE_REASON.

    \b
    wt idea state IDEA-161 SHIPPED
    wt idea state IDEA-060 EXPORTED
    wt idea state IDEA-113 INCUBATE
    """
    try:
        path = EX.set_idea_state(cfg, selector, new_state)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(
        f"[green]✓[/] {selector} → [bold]{(new_state or '').strip().upper()}[/]  [dim]{path}[/]")


@idea_group.command("clock-in")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.pass_obj
def idea_clock_in_cmd(cfg, selector):
    """Open an org-native CLOCK: entry on an idea (SPEC-0061)."""
    try:
        path = clock_in(cfg, selector)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] clocked in on {selector}  [dim]{path}[/]")


@idea_group.command("clock-out")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.pass_obj
def idea_clock_out_cmd(cfg, selector):
    """Close the open CLOCK: entry on an idea (SPEC-0061)."""
    try:
        path = clock_out(cfg, selector)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] clocked out on {selector}  [dim]{path}[/]")


@idea_group.command("retitle")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.argument("title", nargs=-1, required=True)
@click.pass_obj
def idea_retitle_cmd(cfg, selector, title):
    """Rewrite the idea headline; keep id/state/properties/tags."""
    try:
        path = EX.retitle_idea(cfg, selector, " ".join(title))
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] retitled {selector}  [dim]{path}[/]")


@idea_group.command("kind")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--set", "kind", required=True,
              type=click.Choice(list(IDEA_KINDS), case_sensitive=False),
              help="capture kind (idea/bug/improvement/chore)")
@click.pass_obj
def idea_kind_cmd(cfg, selector, kind):
    """Set the idea's :KIND: label (SPEC-0044). Does not change TODO state or next hints."""
    try:
        path = set_idea_kind(cfg, selector, kind)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] kind ← {selector} = {kind.lower()}  [dim]{path}[/]")


@idea_group.command("workstream")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--set", "workstream", required=True,
              help="curated workstream lane (must be in config workstreams:)",
              shell_complete=C.complete_workstream)
@click.pass_obj
def idea_workstream_cmd(cfg, selector, workstream):
    """Set the idea's :WORKSTREAM: lane (SPEC-0090)."""
    try:
        path = set_idea_workstream(cfg, selector, workstream)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] workstream ← {selector} = {workstream}  [dim]{path}[/]")


@idea_group.command("priority")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--set", "priority", required=True, type=click.Choice(["A", "B", "C"]),
              help="org priority cookie [#A|B|C] (SPEC-0093)")
@click.pass_obj
def idea_priority_cmd(cfg, selector, priority):
    """Set the idea's [#A|B|C] priority cookie (SPEC-0093)."""
    try:
        path = set_idea_priority(cfg, selector, priority)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] priority ← {selector} = {priority}  [dim]{path}[/]")


@idea_group.command("show")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--json", "as_json", is_flag=True,
              help="emit wt.idea.v1 JSON on stdout (no human text)")
@click.option("--plain", is_flag=True,
              help="force plain text (no Rich/Pygments highlighting)")
@click.pass_obj
def idea_show_cmd(cfg, selector, as_json, plain):
    """Print headline + Summary / Open questions / Log (read-only).

    On a TTY, highlights org-shaped output via Rich + Pygments unless --plain or --json
    (SPEC-0035)."""
    try:
        task = resolve_selector(cfg, selector)
        if not task.is_idea:
            raise ValueError(f"{selector!r} is not an idea")
        if as_json:
            EX.dump_idea_show_json(cfg, task)
        else:
            EX.print_idea_show(cfg, task, plain=plain)
    except ValueError as e:
        raise click.ClickException(str(e))


@idea_group.group("ext")
def idea_ext_group():
    """Project-private Ext fields under `** Ext <Project>` (SPEC-0119/0122)."""


@idea_ext_group.command("show")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.option("--project", help="Ext namespace (default: idea's :PROJECT:)",
              shell_complete=C.complete_project)
@click.pass_obj
def idea_ext_show_cmd(cfg, selector, project):
    """Print Ext key/value map for an idea."""
    try:
        task = resolve_selector(cfg, selector)
        if not task.is_idea:
            raise ValueError(f"{selector!r} is not an idea")
        project = resolve_ext_project(task, project)
        mapping = IX.read_idea_extension(cfg, task, project)
        if not mapping:
            console.print(f"[dim]no Ext keys under {project!r}[/]")
            return
        for key in sorted(mapping):
            console.print(f"[white]{key}[/]=[cyan]{mapping[key]}[/]")
    except ValueError as e:
        raise click.ClickException(str(e))


@idea_ext_group.command("list")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.pass_obj
def idea_ext_list_cmd(cfg, selector):
    """List Ext namespaces present on an idea."""
    try:
        task = resolve_selector(cfg, selector)
        if not task.is_idea:
            raise ValueError(f"{selector!r} is not an idea")
        exts = IX.read_idea_extensions(cfg, task)
        if not exts:
            console.print("[dim]no Ext namespaces[/]")
            return
        for project in sorted(exts):
            keys = ", ".join(sorted(exts[project]))
            console.print(f"[white]{project}[/]  [dim]({keys})[/]")
    except ValueError as e:
        raise click.ClickException(str(e))


@idea_ext_group.command("set")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.argument("key", shell_complete=C.complete_ext_key)
@click.argument("value")
@click.option("--project", help="Ext namespace (default: idea's :PROJECT:)",
              shell_complete=C.complete_project)
@click.pass_obj
def idea_ext_set_cmd(cfg, selector, key, value, project):
    """Set one Ext key (schema-validated when configured)."""
    try:
        task = resolve_selector(cfg, selector)
        if not task.is_idea:
            raise ValueError(f"{selector!r} is not an idea")
        proj = resolve_ext_project(task, project)
        allowed = enum_values_for(cfg, proj, key.strip())
        if allowed is not None and value not in allowed:
            raise ValueError(f"value {value!r} not in {allowed}")
        path = IX.set_idea_extension(cfg, selector, key, value, project=proj)
        console.print(f"[green]✓[/] ext ← {selector} {key}={value!r}  [dim]{path}[/]")
    except ValueError as e:
        raise click.ClickException(str(e))


@idea_ext_group.command("clear")
@click.argument("selector", shell_complete=C.complete_idea_id)
@click.argument("key", shell_complete=C.complete_ext_key)
@click.option("--project", help="Ext namespace (default: idea's :PROJECT:)",
              shell_complete=C.complete_project)
@click.pass_obj
def idea_ext_clear_cmd(cfg, selector, key, project):
    """Remove one Ext key."""
    try:
        task = resolve_selector(cfg, selector)
        if not task.is_idea:
            raise ValueError(f"{selector!r} is not an idea")
        proj = resolve_ext_project(task, project)
        path = IX.clear_idea_extension(cfg, selector, key, project=proj)
        console.print(f"[green]✓[/] ext cleared ← {selector} {key}  [dim]{path}[/]")
    except ValueError as e:
        raise click.ClickException(str(e))


@idea_ext_group.command("lint")
@click.argument("selector", required=False, shell_complete=C.complete_idea_id)
@click.option("--all", "all_ideas", is_flag=True, help="lint every idea")
@click.option("--strict", is_flag=True, help="exit 1 when any problem is found")
@click.pass_obj
def idea_ext_lint_cmd(cfg, selector, all_ideas, strict):
    """Report Ext schema / :PROJECT: problems (read-only)."""
    try:
        if all_ideas:
            problems = IX.lint_all_idea_extensions(cfg)
        elif selector:
            task = resolve_selector(cfg, selector)
            if not task.is_idea:
                raise ValueError(f"{selector!r} is not an idea")
            problems = IX.lint_idea_extensions(cfg, task)
        else:
            raise click.ClickException("pass SELECTOR or --all")
        if not problems:
            console.print("[green]✓[/] no Ext problems")
            return
        for msg in problems:
            console.print(f"[yellow]![/] {msg}")
        if strict:
            raise SystemExit(1)
    except ValueError as e:
        raise click.ClickException(str(e))


class ProjectsGroup(click.Group):
    """`wt projects [filters…]` lists; `wt projects add|list …` are subcommands (SPEC-0137).

    Prepends `list` in `parse_args` so flags like `--json` bind to the list command
    (same pattern as IdeasGroup).
    """

    def parse_args(self, ctx, args):
        args = list(args)
        if not args or args[0] not in self.commands:
            args = ["list", *args]
        return super().parse_args(ctx, args)


@cli.group("projects", cls=ProjectsGroup)
def projects_group():
    """List known projects or register an outbox target (SPEC-0095 / SPEC-0137 / SPEC-0159).

    \b
    wt projects                      Rich list of mapping buckets + topic counts
    wt projects --json               wt.projects.v1 for agents
    wt projects add NAME             dry-run plan (association stub)
    wt projects add NAME --repo P --yes   write outbox_targets[NAME]
    wt projects rename OLD NEW       dry-run bucket rename (SPEC-0159)
    wt projects rename OLD NEW --yes apply rename
    """


@projects_group.command("list")
@click.option("--json", "as_json", is_flag=True,
              help="emit wt.projects.v1 JSON (SPEC-0095; includes outbox + research)")
@click.pass_obj
def projects_list_cmd(cfg, as_json):
    """List every registered project (mapping buckets, outbox targets, Meta-Tools — SPEC-0189):
    a table, or the full routing map with --json."""
    if as_json:
        import json
        import sys
        from .rules import projects_payload
        json.dump(projects_payload(cfg), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    from rich.text import Text

    from . import render as R
    from .rules import projects_payload
    table = R.section_table("Projects")
    table.add_column("project", no_wrap=True)
    table.add_column("topics", justify="right")
    table.add_column("outbound", justify="center")
    table.add_column("research root", overflow="fold")
    for p in projects_payload(cfg)["projects"]:
        research = p.get("research") or {}
        root = research.get("root")
        where = (Text(root) if root else Text(research.get("note") or "—", style="dim"))
        if root and research.get("source"):
            where.append(f"  ({research['source']})", style="dim")
        table.add_row(R.project_text(p["name"]), str(p["topics"]),
                      Text("✓", style="green") if p["outbound"] else Text("—", style="dim"),
                      where)
    console.print(table)
    if not known_projects(cfg):
        console.print("[dim]no projects mapped to topics yet — see `wt map`[/]")


@projects_group.command("add")
@click.argument("name")
@click.option("--repo", "repo", default=None,
              help="destination repo path (sets outbox_targets[NAME].repo_path)")
@click.option("--scheme", default=None, help="optional export scheme name")
@click.option("--spec-dir", "spec_dir", default=None,
              help="optional specs subdirectory inside the repo")
@click.option("--force", is_flag=True,
              help="allow missing repo path or overwrite conflicting repo_path")
@click.option("--yes", "do_write", is_flag=True,
              help="commit the plan to config.yaml (default is dry-run)")
@click.option("--json", "as_json", is_flag=True, help="emit the plan/result as JSON")
@click.pass_obj
def projects_add_cmd(cfg, name, repo, scheme, spec_dir, force, do_write, as_json):
    """Register or update outbox_targets[NAME] (SPEC-0137). Dry-run unless --yes."""
    import json
    import sys
    from .rules import apply_project_add, plan_project_add
    try:
        plan = plan_project_add(cfg, name, repo=repo, scheme=scheme, spec_dir=spec_dir,
                                force=force)
    except ValueError as e:
        raise click.ClickException(str(e))
    if do_write:
        apply_project_add(cfg, plan)
        plan = {**plan, "written": True}
    else:
        plan = {**plan, "written": False}
    if as_json:
        json.dump({"schema": "wt.projects.add.v1", **plan}, sys.stdout, indent=2,
                  ensure_ascii=False)
        sys.stdout.write("\n")
        return
    action = plan["action"]
    console.print(f"[bold]plan[/]  {action}  outbox_targets[{name!r}]")
    console.print(f"  config: {plan['config_path']}")
    if plan["before"] is not None:
        console.print(f"  before: {plan['before']!r}")
    console.print(f"  after:  {plan['after']!r}")
    if do_write:
        console.print(f"[green]✓ wrote[/] {plan['config_path']}")
    else:
        console.print("[dim]not written — re-run with --yes to commit[/]")


@projects_group.command("rename")
@click.argument("old")
@click.argument("new")
@click.option("--yes", "do_write", is_flag=True,
              help="apply the rename (default is dry-run)")
@click.option("--json", "as_json", is_flag=True, help="emit the plan/result as JSON")
@click.pass_obj
def projects_rename_cmd(cfg, old, new, do_write, as_json):
    """Rename outbox_targets[OLD]→[NEW] + outbox dir + idea :PROJECT: (SPEC-0159).

    Dry-run unless --yes. Does not rekey outbound ids (DEMO-* stays DEMO-*).
    """
    import json
    import sys
    from .outbox_migrate import apply_project_rename, plan_project_rename
    try:
        plan = plan_project_rename(cfg, old, new)
    except ValueError as e:
        raise click.ClickException(str(e))
    if do_write:
        try:
            plan = apply_project_rename(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
    else:
        plan = {**plan, "written": False}
    if as_json:
        json.dump({"schema": "wt.projects.rename.v1", **plan}, sys.stdout, indent=2,
                  ensure_ascii=False)
        sys.stdout.write("\n")
        return
    console.print(f"[bold]plan[/]  rename  {plan['old']!r} → {plan['new']!r}")
    console.print(f"  config: {plan['config_path']}")
    console.print(f"  outbox: {plan['outbox_old']} → {plan['outbox_new']}"
                  f"  ({'exists' if plan['outbox_exists'] else 'missing — config-only'})")
    console.print(f"  ideas:  {plan['idea_count']} :PROJECT: rewrite(s); "
                  f"{plan['ext_headline_hits']} Ext headline(s); "
                  f"idea_extensions key={'yes' if plan['idea_extensions_key'] else 'no'}")
    for w in plan.get("warnings") or []:
        console.print(f"  [yellow]![/] {w}")
    if do_write:
        console.print(
            f"[green]✓ applied[/] ideas={plan.get('ideas_updated', 0)} "
            f"ext={plan.get('ext_renamed', 0)} "
            f"fm_target_project={plan.get('fm_target_project_updated', 0)} "
            f"outbox_renamed={plan.get('outbox_renamed', False)}")
        console.print("[dim]outbound ids unchanged (v1); optional: wt spec reconcile[/]")
    else:
        console.print("[dim]not written — re-run with --yes to apply[/]")


class IdeasGroup(click.Group):
    """`wt ideas [filters…]` lists; `wt ideas export|list …` are explicit subcommands (SPEC-0132).


    Prepends `list` in `parse_args` so flags like `--json` are not rejected as unknown
    *group* options before the subcommand is chosen (same problem `IdeaGroup` avoids by
    putting free text first).
    """

    def parse_args(self, ctx, args):
        args = list(args)
        if not args or args[0] not in self.commands:
            args = ["list", *args]
        return super().parse_args(ctx, args)


@cli.group("ideas", cls=IdeasGroup)
def ideas_group():
    """List ideas (default) or export a filtered org slice (SPEC-0132).

    \b
    wt ideas                open ideas
    wt ideas --all          incl. PROMOTED/DROPPED
    wt ideas export -o PATH --project Meta-Tools --all
    """


@ideas_group.command("list")
@click.option("--state", help="only this idea state",
              shell_complete=C.complete_idea_state)
@click.option("--kind", type=click.Choice(list(IDEA_KINDS), case_sensitive=False),
              help="only this capture kind (SPEC-0044)")
@click.option("--tag", help="only ideas with this tag (SPEC-0089)")
@click.option("--project", help="only ideas under this project (SPEC-0089)",
              shell_complete=C.complete_project)
@click.option("--epic", help="only ideas under this :EPIC: spec id (SPEC-0116)")
@click.option("--workstream", help="only this :WORKSTREAM: (SPEC-0090)",
              shell_complete=C.complete_workstream)
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="only this priority (A/B/C; SPEC-0093)")
@click.option("--all", "all_done", is_flag=True, help="include PROMOTED/DROPPED")
@click.option("--query", "query", default=None,
              help="search heading+Summary/questions/Log (SPEC-0086); implies full corpus")
@click.option("--limit", default=25, show_default=True, type=int,
              help="max --query hits (0 = unlimited)")
@click.option("--tree", is_flag=True,
              help="group project > epic > idea (SPEC-0114); nesting needs --all")
@click.option("--reindex", is_flag=True, help="backfill :ID: on id-less ideas, then list")
@click.option("--json", "as_json", is_flag=True,
              help="emit wt.ideas.v1 JSON on stdout (no Rich table)")
@click.option("--plain", is_flag=True, help="spell the kind out instead of using a glyph")
@click.option("--sort", type=click.Choice(list(R.IDEA_SORT_NAMES), case_sensitive=False),
              help="override order (SPEC-0094; default freshness; ignored with --query)")
@click.option("--desc", is_flag=True, help="reverse --sort order (SPEC-0094)")
@click.pass_obj
def ideas_list_cmd(cfg, state, kind, tag, project, epic, workstream, priority, all_done, query,
                   limit, reindex, as_json, plain, sort, desc, tree):
    """List ideas. Default: open (non-done) ideas. The headline column uses the full terminal
    width (set COLUMNS=N when piping); next-step hints live on `wt next` (SPEC-0075).

    \b
    wt ideas                open ideas
    wt ideas --all          incl. PROMOTED/DROPPED
    wt ideas --state INCUBATE
    wt ideas --kind bug     only bugs (capture label)
    wt ideas --tag X        only tagged X (SPEC-0089)
    wt ideas --project P    only that project (SPEC-0089)
    wt ideas --workstream W only that workstream (SPEC-0090)
    wt ideas --priority A   only [#A] (SPEC-0093)
    wt ideas --sort priority  A before B before C (SPEC-0094)
    wt ideas --query TEXT   related-idea search (open+closed; SPEC-0086)
    wt ideas --json         structured stdout for agents
    wt ideas --reindex      backfill ids onto pre-existing id-less ideas
    """
    if reindex:
        assigned = reindex_ideas(cfg)
        if assigned:
            for idea_id, heading in assigned:
                console.print(f"[green]✓[/] {idea_id}  [white]{heading}[/]")
        else:
            console.print("[dim]no id-less ideas to reindex[/]")
    if query is not None and not query.strip():
        raise click.BadParameter("query must contain at least one token", param_hint="--query")
    if project:
        from .org_write import _warn_unknown_associations
        _warn_unknown_associations(cfg, project=project)
    R.ideas(cfg, state=state, all_done=all_done, as_json=as_json, kind=kind, plain=plain,
            query=query, limit=limit, tag=tag, project=project, workstream=workstream,
            priority=priority, sort=sort, desc=desc, tree=tree, epic=epic)


@ideas_group.command("group-followups")
@click.option("--yes", "apply", is_flag=True, help="write the groups and close the singles")
@click.option("--json", "as_json", is_flag=True, help="emit wt.ideas.group_followups.v1 JSON")
@click.pass_obj
def ideas_group_followups_cmd(cfg, apply, as_json):
    """Fold the open single `wtfu:` review follow-up ideas into one grouped idea per spec
    (SPEC-0239). Dry run unless --yes; the singles are closed as DROPPED, FOLDED_INTO the group.

    \b
    wt ideas group-followups          plan only
    wt ideas group-followups --yes    write it
    """
    from .agentic import followups as FU
    try:
        report = FU.group_legacy(cfg, apply=apply)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps(report, indent=2))
        return
    if not report["groups"]:
        console.print("[dim]no open single follow-up ideas to group[/]")
        return
    for g in report["groups"]:
        ids = ", ".join(g["singles"])
        target = (g.get("group_id") or g["target"]) if apply else g["target"]
        console.print(f"{g['spec_id']}  {len(g['singles'])} singles ({escape(ids)})  "
                      f"\u2192 {target}  ({len(g['lines'])} new lines)", highlight=False)
    console.print(f"{len(report['groups'])} groups, {report['singles']} singles"
                  + ("" if apply else "  \u2014 re-run with --yes"), highlight=False)


@ideas_group.command("export")
@click.option("--state", help="only this idea state",
              shell_complete=C.complete_idea_state)
@click.option("--kind", type=click.Choice(list(IDEA_KINDS), case_sensitive=False),
              help="only this capture kind (SPEC-0044)")
@click.option("--tag", help="only ideas with this tag (SPEC-0089)")
@click.option("--project", help="only ideas under this project (SPEC-0089)",
              shell_complete=C.complete_project)
@click.option("--epic", help="only ideas under this :EPIC: spec id (SPEC-0116)")
@click.option("--workstream", help="only this :WORKSTREAM: (SPEC-0090)",
              shell_complete=C.complete_workstream)
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="only this priority (A/B/C; SPEC-0093)")
@click.option("--all", "all_done", is_flag=True, help="include PROMOTED/DROPPED")
@click.option("-o", "--output", "output", type=click.Path(),
              help="write org file here (required unless --dry-run)")
@click.option("--dry-run", is_flag=True, help="print matching idea ids; do not write")
@click.pass_obj
def ideas_export_cmd(cfg, state, kind, tag, project, epic, workstream, priority, all_done,
                     output, dry_run):
    """Export filtered idea subtrees to an org file (SPEC-0132). Read-only — never moves org.

    \b
    wt ideas export --project Meta-Tools --all -o /tmp/ideas.org
    wt ideas export --project Meta-Tools --all --dry-run
    """
    from .idea_export import export_idea_subtrees
    if not dry_run and not output:
        raise click.UsageError("pass -o/--output PATH or --dry-run")
    try:
        ids = export_idea_subtrees(
            cfg, None if dry_run else output, dry_run=dry_run, state=state, all_done=all_done,
            kind=kind, tag=tag, project=project, epic=epic, workstream=workstream,
            priority=priority,
        )
    except ValueError as e:
        raise click.ClickException(str(e))
    if dry_run:
        console.print(f"[dim]{len(ids)} idea(s)[/]")
        for iid in ids:
            console.print(iid)
        return
    console.print(f"[green]✓[/] exported {len(ids)} idea(s) → [white]{output}[/]")


@cli.command("release-archive")
@click.option("-o", "--output", "output", type=click.Path(),
              help="output .tar.gz path (default: ../work-tracking-YYYYMMDD-<sha>.tar.gz)")
@click.option("--project", default="Meta-Tools", show_default=True,
              help="idea :PROJECT: for the ideas/ slice",
              shell_complete=C.complete_project)
@click.option("--all/--no-all", "all_done", default=True, show_default=True,
              help="include done/archived ideas in the slice (SPEC-0133)")
@click.pass_obj
def release_archive_cmd(cfg, output, project, all_done):
    """Pack git HEAD + a filtered idea org slice into one release tarball (SPEC-0133).

    \b
    wt release-archive
    wt release-archive -o /tmp/wt.tgz --project Meta-Tools
    """
    from .release_archive import build_release_archive
    try:
        path = build_release_archive(cfg, output, project=project, all_done=all_done)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] release archive → [white]{path}[/]")


@cli.command("next")
@click.option("--all", "all_done", is_flag=True,
              help="include PROMOTED (DROPPED still omitted)")
@click.option("--json", "as_json", is_flag=True,
              help="emit wt.next.v1 JSON on stdout (no Rich table)")
@click.option("--plain", is_flag=True, help="spell the kind out instead of using a glyph")
@click.option("--state", help="only this idea state (SPEC-0096)",
              shell_complete=C.complete_idea_state)
@click.option("--kind", type=click.Choice(list(IDEA_KINDS), case_sensitive=False),
              help="only this capture kind (SPEC-0096)")
@click.option("--tag", help="only this tag (SPEC-0096)")
@click.option("--project", help="only this project (SPEC-0096)",
              shell_complete=C.complete_project)
@click.option("--workstream", help="only this :WORKSTREAM: (SPEC-0096)",
              shell_complete=C.complete_workstream)
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="only this priority (SPEC-0096)")
@click.pass_obj
def next_cmd(cfg, all_done, as_json, plain, state, kind, tag, project, workstream, priority):
    """Next-step hints for open ideas. Same rules as the `next` column on `wt ideas`.
    See docs/WORKFLOW.md.

    \b
    wt next                 open ideas + recommended command
    wt next --all           also PROMOTED (build reminders)
    wt next --project P     only that project (SPEC-0096)
    wt next --json          structured stdout for agents
    """
    R.next_ideas(cfg, all_done=all_done, as_json=as_json, plain=plain, state=state,
                 kind=kind, tag=tag, project=project, workstream=workstream,
                 priority=priority)


@cli.command("hub")
@click.option("--json", "as_json", is_flag=True,
              help="emit wt.hub.v1 JSON on stdout (required in v1)")
@click.option("--tasks-slice", "tasks_slice",
              type=click.Choice(["today", "open", "none"], case_sensitive=False),
              default="today", show_default=True,
              help="hub task block: today+overdue (default), capped open, or omit (SPEC-0092)")
@click.option("--state", help="filter ideas + today/open tasks by state (SPEC-0096)",
              shell_complete=C.complete_idea_state)
@click.option("--kind", type=click.Choice(list(IDEA_KINDS), case_sensitive=False),
              help="filter hub ideas by capture kind (SPEC-0096)")
@click.option("--tag", help="filter ideas + today/open tasks by tag (SPEC-0096)")
@click.option("--project", help="filter ideas + today/open tasks by project (SPEC-0096)",
              shell_complete=C.complete_project)
@click.option("--workstream", help="filter ideas + today/open tasks by workstream (SPEC-0096)",
              shell_complete=C.complete_workstream)
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="filter ideas + today/open tasks by priority (SPEC-0096)")
@click.pass_obj
def hub_cmd(cfg, as_json, tasks_slice, state, kind, tag, project, workstream, priority):
    """Read-only triage across open ideas, active internal specs, and outbound outbox
    (SPEC-0052). Never writes docs/LEDGER.md. Includes a tasks/today slice (SPEC-0092).

    \b
    wt hub --json                     wt.hub.v1 + today{date,overdue,tasks}
    wt hub --json --project P         ideas + today filtered (SPEC-0096)
    wt hub --json --tasks-slice open  capped open actionable tasks
    wt hub --json --tasks-slice none  ideas/specs only
    """
    try:
        R.hub(cfg, as_json=as_json, tasks_slice=tasks_slice, state=state, kind=kind,
              tag=tag, project=project, workstream=workstream, priority=priority)
    except ValueError as e:
        raise click.UsageError(str(e))


@cli.command("tui")
@click.option("--state", help="open pre-filtered to this idea state (SPEC-0100)",
              shell_complete=C.complete_idea_state)
@click.option("--kind", type=click.Choice(list(IDEA_KINDS), case_sensitive=False),
              help="open pre-filtered to this capture kind")
@click.option("--tag", help="open pre-filtered to this tag")
@click.option("--project", help="open pre-filtered to this project",
              shell_complete=C.complete_project)
@click.option("--workstream", help="open pre-filtered to this :WORKSTREAM:",
              shell_complete=C.complete_workstream)
@click.option("--priority", type=click.Choice(["A", "B", "C"]),
              help="open pre-filtered to this priority")
@click.option("--sort", type=click.Choice(list(R.IDEA_SORT_NAMES), case_sensitive=False),
              help="list order (default freshness, same as `wt ideas`)")
@click.option("--desc", is_flag=True, help="reverse --sort order")
@click.option("--all", "all_done", is_flag=True, help="start with non-active ideas shown")
@click.option("--query", default=None, help="open on a related-idea search (SPEC-0086)")
@click.pass_obj
def tui_cmd(cfg, state, kind, tag, project, workstream, priority, sort, desc, all_done, query):
    """Interactive ideas desk (SPEC-0098/0099/0100). Needs the optional `tui` extra.

    Read-only in v1: browse, search, expand Summary / questions / Log. Human-only surface —
    agents keep the classic commands and `--json`. Install with `uv sync --extra tui` from a
    checkout, or `uv tool install 'wt[tui]'`.

    \b
    wt tui                    active ideas, freshest first
    wt tui --all              start with non-active shown (`a` toggles in-app)
    wt tui --project Example    open pre-filtered
    wt tui --query textual    open on a search (`/` searches in-app)
    """
    from . import tui as T

    try:
        T.run(cfg, all_done=all_done, query=query,
              filters={"state": state, "kind": kind, "tag": tag, "project": project,
                       "workstream": workstream, "priority": priority,
                       "sort": sort, "desc": desc})
    except T.MissingTextual as e:
        raise click.ClickException(str(e))


@cli.group("spec")
def spec_group():
    """Spec scaffolding (SPEC-0013+). Doesn't wrap tools/spec_lint.py (still a dev tool)."""


@spec_group.command("new")
@click.option("--from-idea", "from_idea", help="idea selector to promote",
              shell_complete=C.complete_idea_id)
@click.option("--target",
              help="scaffold an OUTBOUND spec for this project "
                   "(<data_dir>/outbox/<target>/)",
              shell_complete=C.complete_outbox_target)
@click.option("--internal", is_flag=True,
             help="force an internal SPEC-NNNN even if the idea has a :PROJECT:")
@click.option("--epic", is_flag=True,
              help="scaffold from TEMPLATE-epic.md (internal or outbound)")
@click.option("--title", help="override the derived title (default: the idea's heading)")
@click.option("--force", is_flag=True,
              help="re-promote an idea that already has a :SPEC:; also skip empty-Summary warn")
@click.pass_obj
def spec_new_cmd(cfg, from_idea, target, internal, epic, title, force):
    """Promote an org idea into a scaffolded draft spec, routed by its :PROJECT: association
    (SPEC-0020): a project mapped in outbox_targets promotes OUTBOUND
    (<data_dir>/outbox/<proj>/); no project (or --internal) promotes an internal SPEC-NNNN;
    an unconfigured project errors rather than silently minting an internal id. --target
    overrides the routing target and also scaffolds a blank OUTBOUND spec when --from-idea
    is omitted.

    \b
    wt spec new --from-idea "instant review agent"
    wt spec new --from-idea idea-42 --epic --title "Review platform"
    wt spec new --from-idea idea-7 --internal
    wt spec new --target acme --from-idea "adversarial reviewer"
    wt spec new --target acme --title "Adversarial reviewer"
    """
    project = target
    try:
        if from_idea:
            idea = resolve_selector(cfg, from_idea)
            if project is None and not internal:
                project = idea.properties.get("PROJECT")
            path, spec_id, kind = promote_idea(cfg, from_idea, internal=internal, target=target,
                                               epic=epic, title=title, force=force)
        elif target:
            path, spec_id = scaffold_outbound(cfg, target, from_idea=from_idea, title=title)
            kind = "outbound"
        else:
            raise click.UsageError("--from-idea is required (or pass --target for an "
                                   "outbound spec)")
    except ValueError as e:
        raise click.ClickException(str(e))
    if kind == "internal":
        routed = "internal"
    else:
        repo = (cfg.get("outbox_targets") or {}).get(project, {}).get("repo_path", project)
        routed = f"outbound → {repo}"
    # SPEC-0167: name the idea's heading so a wrong id is visible at a glance.
    src = f"<- {idea.id or from_idea} {escape(repr(idea.heading))}" if from_idea else ""
    console.print(f"[green]✓[/] {spec_id}  [white]{path}[/]  [dim]{routed} {src}[/]")


def _spec_by():
    import os
    return os.environ.get("WT_MAILBOX_ADDR") or "human"


def _reservation_age(rec):
    try:
        secs = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(rec["at"])
                ).total_seconds()
    except (KeyError, ValueError, TypeError):
        return "?"
    secs = max(int(secs), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


_specs_dir_opt = click.option("--specs-dir", "specs_dir", type=click.Path(file_okay=False),
                              help="another repository's spec directory (default: the "
                                   "internal specs directory)")


@spec_group.command("reserve")
@click.option("--for", "for_", help="free-text note: what the number is for")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@_specs_dir_opt
@click.pass_obj
def spec_reserve_cmd(cfg, for_, as_json, specs_dir):
    """Reserve the next free SPEC number (SPEC-0208), visible across every branch and
    worktree of the repository. Release with `wt spec release`; it is pruned automatically
    once the number is committed on a branch."""
    from . import spec_numbers as SN
    try:
        rec = SN.allocate(cfg, by=_spec_by(), note=for_, specs_dir=specs_dir)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps({"schema": "wt.spec.reservation.v1", **rec}, indent=2))
    else:
        click.echo(rec["id"])


@spec_group.command("reservations")
@click.option("--all", "all_keys", is_flag=True, help="every repository / spec directory")
@click.option("--json", "as_json", is_flag=True, help="machine-readable output")
@_specs_dir_opt
@click.pass_obj
def spec_reservations_cmd(cfg, all_keys, as_json, specs_dir):
    """List live SPEC-number reservations (SPEC-0208); committed ones are pruned first."""
    from . import spec_numbers as SN
    try:
        recs = SN.list_reservations(cfg, specs_dir=specs_dir, all_keys=all_keys)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        click.echo(json.dumps({"schema": "wt.spec.reservations.v1", "reservations": recs},
                              indent=2))
        return
    if not recs:
        console.print("[dim]no reservations[/]")
        return
    from rich.table import Table
    table = Table(show_header=True, header_style="bold")
    for col in ("id", "note", "by", "campaign", "age"):
        table.add_column(col)
    for r in recs:
        table.add_row(r["id"], escape(r.get("note") or ""), escape(r.get("by") or ""),
                      escape(r.get("campaign") or ""), _reservation_age(r))
    console.print(table)


@spec_group.command("release")
@click.argument("number")
@click.option("--yes", is_flag=True, help="apply (default is a dry run)")
@_specs_dir_opt
@click.pass_obj
def spec_release_cmd(cfg, number, yes, specs_dir):
    """Release a SPEC-number reservation (SPEC-0208). Dry run without --yes."""
    from . import spec_numbers as SN
    try:
        if yes:
            rec = SN.release(cfg, number, specs_dir=specs_dir)
        else:
            n = SN.parse_number(number)
            rec = next((r for r in SN.list_reservations(cfg, specs_dir=specs_dir)
                        if r["number"] == n), None)
            if rec is None:
                raise ValueError(f"SPEC-{n:04d} is not reserved under this spec directory")
    except ValueError as e:
        raise click.ClickException(str(e))
    if yes:
        console.print(f"[green]✓[/] released {rec['id']}")
    else:
        console.print(f"would release {rec['id']} (by {escape(rec.get('by') or '')}); "
                      f"pass --yes to apply")


@spec_group.command("export")
@click.argument("outbound_id", shell_complete=C.complete_outbound_id)
@click.option("--scheme", help="export scheme (default: wt-native, or the target's "
                              "configured outbox_targets[project].scheme)",
              shell_complete=C.complete_scheme)
@click.option("--emit", "emit", multiple=True,
             help="only emit this artifact (repeatable); default: everything the scheme owns",
             shell_complete=C.complete_emit)
@click.option("--to", type=click.Path(file_okay=False),
              help="destination repo path (default: configured outbox_targets[...])")
@click.option("--force", is_flag=True,
             help="overwrite existing owned/spliced content that differs")
@click.option("--task-id", "task_id",
              help="pin allocated task id X.Y.Z when the scheme supports it (overrides frontmatter task_id)")
@click.pass_obj
def spec_export_cmd(cfg, outbound_id, scheme, emit, to, force, task_id):
    """File-drop an outbound spec into its target repo via a pluggable export scheme
    (SPEC-0015's wt-native by default; other schemes via --scheme, SPEC-0016).

    Successful export advances the linked idea to EXPORTED (SPEC-0041).

    \b
    wt spec export ACME-0001                             into outbox_targets[acme]
    wt spec export ACME-0001 --to /tmp/somerepo           explicit destination
    wt spec export ACME-0001 --scheme wt-native --to .
    wt spec export ACME-0001 --emit task-spec             only that artifact
    wt spec export ACME-0001 --force                      overwrite despite divergence
    wt spec export ACME-0001 --scheme wt-native --to .
    """
    try:
        result = export_spec(cfg, outbound_id, scheme=scheme, emit=emit or None, to=to,
                             force=force, task_id=task_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    if isinstance(result, tuple):
        dest_spec, dest_contract = result
        detail = f"[white]{dest_spec}[/]"
        if dest_contract:
            detail += f"  [dim]+ {dest_contract}[/]"
    else:
        detail = "  ".join(f"[white]{p}[/]" for p in result)
    console.print(f"[green]✓[/] {outbound_id} exported  {detail}")


@spec_group.command("pull-status")
@click.argument("outbound_id", shell_complete=C.complete_outbound_id)
@click.option("--to", type=click.Path(file_okay=False),
              help="destination repo path (default: configured outbox_targets[...])")
@click.pass_obj
def spec_pull_status_cmd(cfg, outbound_id, to):
    """Mirror portable `status` from the target repo into the outbox copy (SPEC-0041).

    The portable file in the target is the working copy for build progress; this command
    is optional convenience — you can also edit the outbox frontmatter by hand.

    \b
    wt spec pull-status ACME-0001
    wt spec pull-status ACME-0001 --to /tmp/somerepo
    """
    from .export import pull_status
    try:
        path, status = pull_status(cfg, outbound_id, to=to)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] {outbound_id} status → {status}  [dim]{path}[/]")


@spec_group.command("pull-clock")
@click.argument("outbound_id", shell_complete=C.complete_outbound_id)
@click.option("--to", type=click.Path(file_okay=False),
              help="destination repo path (default: configured outbox_targets[...])")
@click.pass_obj
def spec_pull_clock_cmd(cfg, outbound_id, to):
    """Reconcile a portable spec's markdown Clock Log into org CLOCK: entries on the linked
    idea (SPEC-0060/0062/0063). Idempotent; requires source_idea on the outbound spec.

    \b
    wt spec pull-clock ACME-0001
    """
    from .export import pull_clock
    try:
        idea_id, added = pull_clock(cfg, outbound_id, to=to)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] {outbound_id} → {idea_id}  {added} clock entr"
                 f"{'y' if added == 1 else 'ies'} added")


@spec_group.command("fit-log")
@click.argument("outbound_id", shell_complete=C.complete_outbound_id)
@click.option("--note", required=True, help="fit/miss note to append on the source idea Log")
@click.pass_obj
def spec_fit_log_cmd(cfg, outbound_id, note):
    """Append a post-ship fit/miss note to the outbound's source idea Log (SPEC-0049).

    \b
    wt spec fit-log ACME-0001 --note "used it; filters still too coarse"
    """
    from .export import fit_log
    try:
        idea_id, path = fit_log(cfg, outbound_id, note)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] fit-log ← {idea_id} ({outbound_id})  [dim]{path}[/]")


@spec_group.command("postmortem")
@click.argument("spec_id", shell_complete=C.complete_any_spec_id)
@click.option("--note", required=True, help="defect/post-mortem note to append on the source idea Log")
@click.pass_obj
def spec_postmortem_cmd(cfg, spec_id, note):
    """Append a post-mortem/defect note to a spec's linked source idea's Log (SPEC-0069).

    Works for internal (SPEC-NNNN) and outbound (PROJ-NNNN) specs alike — for a defect found
    in already-done (or any) work, distinct from `wt spec fit-log`'s narrower post-ship
    fit/miss framing.

    \b
    wt spec postmortem SPEC-0063 --note "parse_clock_log silently dropped unbracketed timestamps"
    """
    from .export import postmortem
    try:
        idea_id, path = postmortem(cfg, spec_id, note)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] postmortem ← {idea_id} ({spec_id})  [dim]{path}[/]")


@spec_group.command("status")
@click.argument("spec_id", shell_complete=C.complete_any_spec_id)
@click.option("--set", "new_status", type=click.Choice(SPEC_STATUSES), default=None,
              help="set the frontmatter status (edits only status: and updated:)")
@click.option("--json", "as_json", is_flag=True, help="emit wt.spec.status.v1")
@click.pass_obj
def spec_status_cmd(cfg, spec_id, new_status, as_json):
    """Show or set a spec's frontmatter status (SPEC-0179).

    A pencil, not a judge: no lifecycle validation (`wt spec verify` and the linter own that)
    and no ledger regeneration (run `python3 tools/spec_lint.py --write-ledger`).

    \b
    wt spec status SPEC-0179
    wt spec status SPEC-0179 --set in-progress
    wt spec status SPEC-0179 --json
    """
    import json as _json
    import sys

    from .specs import resolve_spec_path, set_spec_status, spec_status
    try:
        path = resolve_spec_path(cfg, spec_id)
        old = None
        if new_status:
            old, _ = set_spec_status(path, new_status)
        info = spec_status(path)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _json.dump({"schema": "wt.spec.status.v1", "id": spec_id, "path": str(path),
                    "status": info["status"], "updated": info["updated"]},
                   sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    elif new_status:
        console.print(f"{spec_id}  {old} → {new_status}  [dim]{path}[/]")
    else:
        console.print(f"{spec_id}  {info['status']}")


@spec_group.command("review")
@click.argument("spec_id", required=False, shell_complete=C.complete_any_spec_id)
@click.option("--path", "path", type=click.Path(exists=True, dir_okay=False),
              help="review this spec file (any repository) instead of a SPEC-ID")
@click.option("--json", "as_json", is_flag=True, help="emit the wt.spec.review.v1 payload")
@click.option("--force", is_flag=True,
              help="review a spec that already has a stored review (an extra round)")
@click.pass_obj
def spec_review_cmd(cfg, spec_id, path, as_json, force):
    """Review a spec with the read-only spec_reviewer persona (SPEC-0188).

    Stores the review in the data dir, keyed by the spec id and the file's hash
    (`<data>/agentic/reviews/<SPEC-ID>/<sha>.json`); `wt queue add` admits a spec with an
    `approve` for its current text, or one whose latest review is a `revise` and whose text
    was edited since. A spec is reviewed once: reviewing an already-reviewed spec needs
    --force and warns. Give exactly one of SPEC-ID and --path;
    --path reviews a file in any repository, with that repository as the reviewer's root.
    Run inside a checkout (main or linked worktree) of the specs repository, a SPEC-ID
    reviews that checkout's copy; elsewhere, the registered checkout's. The reviewed file
    is printed first.

    \b
    wt spec review SPEC-0188
    wt spec review SPEC-0188 --json
    wt spec review --path ~/code/other/docs/specs/0007-thing.md
    """
    import json as _json
    import sys
    from pathlib import Path

    from .agentic.spec_review import (latest_review, reviewing_line, run_spec_review,
                                      spec_root, would_pick)
    from .specmeta import split_frontmatter
    from .specs import resolve_spec_path_for_cwd
    if bool(spec_id) == bool(path):
        raise click.UsageError("give exactly one of SPEC-ID and --path")
    try:
        if path:
            fm, _ = split_frontmatter(Path(path).read_text(encoding="utf-8"))
            spec_id = str((fm or {}).get("id") or "")
            if not spec_id:
                raise click.ClickException(f"{path}: front matter has no `id`")
            spec_path, origin = Path(path), "path"
        else:
            spec_path, origin = resolve_spec_path_for_cwd(cfg, spec_id)
        prior = latest_review(cfg, spec_id, spec_path)                # SPEC-0230
        if prior is not None:
            what = f"{prior.get('verdict')}, {str(prior.get('spec_sha256') or '')[:12]}"
            if not force:
                raise click.ClickException(
                    f"{spec_id} was already reviewed ({what}); a revised spec is queued "
                    f"without a second review. Pass --force for an extra round")
            click.echo(f"warning: extra review round for {spec_id} (previous: {what})",
                       err=True)
        line = reviewing_line(cfg, spec_path, origin)
        if as_json:
            click.echo(line, err=True)
        else:
            console.print(line, highlight=False, markup=False, soft_wrap=True)
        review = run_spec_review(cfg, spec_path, spec_id, origin=origin)
        pick = would_pick(cfg, review, project_root=spec_root(cfg, spec_path))
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _json.dump({**review, "profile": pick["profile"] if pick else None}, sys.stdout,
                   indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    color = {"approve": "green", "revise": "yellow", "split": "yellow"}[review["verdict"]]
    cost = review.get("cost_usd")
    cost_txt = f"  ${cost:.2f}" if cost is not None else ""
    console.print(f"[bold]{spec_id}[/]  [{color}]{review['verdict']}[/]  "
                  f"{review['model']}{cost_txt}")
    if review.get("size"):
        console.print(f"  size {review['size']}  tone {review.get('tone')}", highlight=False)
        console.print(f"  rationale: {review.get('rationale') or '-'}", highlight=False)
        if review["size"] == "xl":
            console.print("  xl: split recommended, no profile", highlight=False)
        elif pick:
            console.print(f"  → profile [bold]{pick['profile']}[/]  [dim]{pick['reason']}[/]")
    if review["findings"]:
        from rich.table import Table
        table = Table(show_header=True, header_style="bold")
        for col in ("severity", "section", "title", "detail"):
            table.add_column(col, overflow="fold")
        for f in review["findings"]:
            table.add_row(f.get("severity", ""), f.get("section", ""), f.get("title", ""),
                          f.get("detail", ""))
        console.print(table)
    for sp in review["suggested_splits"]:
        console.print(f"  split: [bold]{sp.get('title')}[/] — {sp.get('scope', '')}",
                      highlight=False)


@spec_group.command("verify")
@click.argument("spec_id", shell_complete=C.complete_spec_id)
@click.option("--json", "as_json", is_flag=True, help="emit wt.spec.verify.v1")
@click.option("--strict", is_flag=True,
              help="exit 1 if any mechanical check failed (SPEC-0117)")
@click.pass_obj
def spec_verify_cmd(cfg, spec_id, as_json, strict):
    """Post-hoc Definition-of-Done audit for an internal SPEC-NNNN (SPEC-0117).

    Mechanical checks only (AC boxes, Test plan present, epic children/Breakdown). Does not
    mutate status. The global /wt-verify skill uses this for internal specs; outbound
    portables are audited by that skill's Procedure B (no CLI). Outbound ids refused here.

    \b
    wt spec verify SPEC-0117
    wt spec verify SPEC-0117 --json
    wt spec verify SPEC-0117 --strict
    """
    import json as _json
    import sys

    from .verify import verify_spec

    try:
        report = verify_spec(cfg, spec_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        marks = {"pass": ("green", "✓"), "fail": ("red", "✗"),
                 "warn": ("yellow", "!"), "skip": ("dim", "·")}
        console.print(f"[bold]{report['id']}[/]  {report.get('kind')}  "
                      f"status={report.get('status') or '?'}  "
                      f"{'[green]ok[/]' if report['ok'] else '[red]not ok[/]'}")
        for c in report["checks"]:
            style, glyph = marks.get(c["status"], ("dim", "·"))
            console.print(f"  [{style}]{glyph}[/] {c['name']}: {c['detail']}")
        console.print("[dim]judgment (agent must attest — not scored by CLI):[/]")
        for j in report["judgment"]:
            console.print(f"  [dim]-[/] {j}")
        console.print("[dim]playbook: /wt-verify[/]")
    if strict and not report["ok"]:
        raise SystemExit(1)


@spec_group.command("reconcile")
@click.option("--apply", "do_apply", is_flag=True,
              help="write the changes (default: dry run)")
@click.pass_obj
def spec_reconcile_cmd(cfg, do_apply):
    """Sync idea states with their linked spec's status (SPEC-0078). Dry run by default.

    Walks every idea carrying a :SPEC: — internal or outbound — and reports where its org state
    disagrees with the spec: a `done` spec whose idea is still SPECCED, a `superseded`/`rejected`
    spec whose idea is still open, an idea left behind at IDEA/INCUBATE while its spec is being
    built. An idea *ahead* of its spec is flagged, never rewound.

    --apply also writes a missing `source_idea` back onto a spec, and advancing to
    PROMOTED/DROPPED archives the idea's subtree (SPEC-0073) — hence the dry-run default.

    \b
    wt spec reconcile            # what would change
    wt spec reconcile --apply    # change it
    """
    from .specs import reconcile_ideas

    plan = reconcile_ideas(cfg, apply=do_apply)
    marks = {"advance": ("green", "✓" if do_apply else "→"),
             "flag-ahead": ("yellow", "!"),
             "flag-no-backlink": ("yellow", "!"),
             "questions-open": ("yellow", "?"),      # SPEC-0080: report-only, never written
             "missing": ("red", "✗"),
             "unchanged": ("dim", "·")}
    shown = 0
    for idea_id, spec_id, action, detail in plan:
        if action == "unchanged":
            continue
        style, glyph = marks.get(action, ("dim", "·"))
        shown += 1
        console.print(f"[{style}]{glyph}[/] {idea_id}  [dim]{spec_id}[/]  {detail}")
    if not shown:
        console.print("[dim]reconcile: every linked idea already matches its spec[/]")
        return
    n_adv = sum(1 for _i, _s, a, _d in plan if a == "advance")
    if do_apply:
        console.print(f"[dim]{n_adv} idea(s) advanced[/]")
    elif n_adv:
        console.print(f"[dim]{n_adv} idea(s) would change — re-run with --apply[/]")


@spec_group.command("sweep")
@click.pass_obj
def spec_sweep_cmd(cfg):
    """Bulk pull-status/pull-clock across every pending EXPORTED idea (SPEC-0067/0064).

    "Pending" = idea state EXPORTED whose linked outbound spec's status isn't yet `done`.
    A destination that no longer exists on disk is logged to that project's PROVENANCE.md
    (not raised) and the sweep continues with the rest.

    \b
    wt spec sweep
    """
    from .export import sweep
    results = sweep(cfg)
    if not results:
        console.print("[dim]nothing pending[/]")
        return
    marks = {"missing": ("yellow", "!"), "unchanged": ("dim", "·"), "updated": ("green", "✓")}
    for idea_id, outbound_id, result in results:
        label = result.split(":", 1)[0]
        style, mark = marks.get(label, ("green", "✓"))
        console.print(f"[{style}]{mark}[/] {idea_id} ({outbound_id})  {result}")


@spec_group.command("schemes")
@click.pass_obj
def spec_schemes_cmd(cfg):
    """List available export schemes: name, description, required fields, emittable
    artifacts (SPEC-0016). A user-config manifest scheme (SPEC-0058) shows its origin."""
    for s in available_schemes():
        origin = getattr(s, "origin", "built-in")
        tag = r"  [yellow]\[user-config][/]" if origin == "user-config" else ""
        console.print(f"[bold]{s.name}[/]{tag}  [dim]{s.description}[/]")
        console.print(f"  required: {', '.join(s.required_fields) or '(none)'}")
        console.print(f"  emit: {', '.join(s.artifacts)}")


@spec_group.command("generate")
@click.argument("spec_id", shell_complete=C.complete_spec_id)
@click.option("--file", type=click.Path(exists=False),
              help="target .org file (default: org_capture_file)")
@click.option("--key", help="JIRA key (e.g. DEMO-100) to set as each task's topic_key")
@click.pass_obj
def spec_generate_cmd(cfg, spec_id, file, key):
    """Generate org tasks from a spec's breakdown (epic) / acceptance criteria (feature).

    \b
    wt spec generate SPEC-0011                   tasks from the epic's breakdown
    wt spec generate SPEC-0020 --key DEMO-100    feature spec, tasks keyed to a JIRA id
    wt spec generate SPEC-0011 --file ~/work/org/agenda.org
    """
    try:
        path, added = generate_from_spec(cfg, spec_id, file=file, key=key)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] {spec_id} → {added} new task(s)  [dim]{path}[/]")


@spec_group.command("seed-children")
@click.argument("epic_id", shell_complete=C.complete_epic)
@click.pass_obj
def spec_seed_children_cmd(cfg, epic_id):
    """Seed INCUBATE ideas from an epic's Breakdown bullets (SPEC-0043 + SPEC-0097).

    Each idea gets :EPIC: <epic_id>. :PROJECT: from optional `(project: Name)` on the bullet,
    else outbound target_project. Unknown annotated names hard-fail. Idempotent on headings.

    \b
    wt spec seed-children SPEC-0011
    wt spec seed-children DEMO-0001
    """
    try:
        added = seed_ideas_from_epic(cfg, epic_id)
    except ValueError as e:
        raise click.ClickException(str(e))
    if not added:
        console.print(f"[dim]✓[/] {epic_id} — no new child ideas (Breakdown empty or already seeded)")
        return
    console.print(f"[green]✓[/] {epic_id} → {len(added)} child idea(s)  "
                  + " ".join(f"[cyan]{i}[/]" for i in added))


def _read_json_arg(path):
    """Read a JSON value from a file path, or stdin when path is '-'."""
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as f:
        return json.load(f)


@cli.group("sheet")
def sheet_group():
    """Sheet-sync plan/record (SPEC-0125): JSON-only, no network access ever — `wt` computes
    a sync diff and records its outcome; the driving agent performs the actual Sheets reads/
    writes via its own tools (see the /wt-sheet-sync skill)."""


@sheet_group.command("plan")
@click.option("--project", required=True, help="project to sync (see `wt projects`)")
@click.option("--sheet-rows", default=None,
              help="path to a JSON array of current sheet rows ('-' for stdin); when given, "
                   "also computes the pull list (unknown rows to adopt)")
@click.pass_obj
def sheet_plan_cmd(cfg, project, sheet_rows):
    """Print the sync plan (push + optional pull) for PROJECT as JSON."""
    rows = _read_json_arg(sheet_rows) if sheet_rows else None
    try:
        result = SHS.make_plan(cfg, project, rows)
    except ValueError as e:
        raise click.ClickException(str(e))
    print(json.dumps(result, indent=2))


@sheet_group.command("record")
@click.option("--project", required=True, help="project to sync (see `wt projects`)")
@click.option("--payload", required=True,
              help="path to a JSON {pushed: [...], adopted: [...]} payload ('-' for stdin)")
@click.pass_obj
def sheet_record_cmd(cfg, project, payload):
    """Persist a completed sync run's outcome (Ext sheet_row/sheet_hash + adopted ideas)."""
    data = _read_json_arg(payload)
    try:
        result = SHS.record(cfg, project, data)
    except ValueError as e:
        raise click.ClickException(str(e))
    print(json.dumps(result, indent=2))


@cli.group("skills")
def skills_group():
    """Install/list the repo's agent-workflow skills (SPEC-0017) into ~/.claude/skills/ (or
    any --dest), so a fresh agent session can drive wt without reading its code."""


def _skills_source():
    """The checkout's skills/ dir, or a ClickException with the SPEC-0166 fix hint — never a
    silent empty install."""
    from .rules import CHECKOUT_HINT
    try:
        source = SK.repo_skills_dir(load_config())
    except ValueError as e:                     # invalid `wt_checkout` override
        raise click.ClickException(str(e))
    if source is None:
        raise click.ClickException(f"cannot find wt's skills/ directory; {CHECKOUT_HINT}")
    return source


@skills_group.command("install")
@click.option("--link/--copy", "link", default=True,
              help="symlink into the repo (default) or make a portable copy")
@click.option("--dest", type=click.Path(file_okay=False),
              help="install destination (default: ~/.claude/skills)")
@click.option("--force", is_flag=True, help="overwrite a non-wt directory of the same name")
def skills_install_cmd(link, dest, force):
    """Install every skills/<name>/SKILL.md into --dest. Idempotent; refuses to clobber a
    directory it doesn't already manage unless --force.

    \b
    wt skills install                 symlink all skills into ~/.claude/skills
    wt skills install --copy          portable copy instead
    wt skills install --dest /tmp/x   install elsewhere (tests MUST use this)
    """
    source = _skills_source()
    try:
        results = SK.install(dest=dest, mode="link" if link else "copy", force=force,
                             source=source)
    except ValueError as e:
        raise click.ClickException(str(e))
    if not results:
        console.print("[yellow]no skills found under skills/[/]")
        return
    for name, action in results:
        console.print(f"[green]✓[/] {name}  [dim]{action}[/]")


@skills_group.command("list")
@click.option("--dest", type=click.Path(file_okay=False),
              help="destination to check (default: ~/.claude/skills)")
def skills_list_cmd(dest):
    """Show repo skills and whether/how each is installed at --dest."""
    for entry in SK.status(dest=dest, source=_skills_source()):
        console.print(f"[white]{entry['name']}[/]  [dim]{entry['state']}[/]")


@cli.group("completion")
def completion_group():
    """Generate / install shell completion scripts for bash, zsh, or fish (SPEC-0022).

    \b
    wt completion fish                         print fish script to stdout
    wt completion install --shell fish         write ~/.config/fish/completions/wt.fish
    wt completion install --shell bash --dest /tmp/wt.bash
    """


def _print_completion(shell):
    click.echo(C.source_script(shell), nl=False)


@completion_group.command("bash")
def completion_bash_cmd():
    """Print the bash completion script to stdout."""
    _print_completion("bash")


@completion_group.command("zsh")
def completion_zsh_cmd():
    """Print the zsh completion script to stdout."""
    _print_completion("zsh")


@completion_group.command("fish")
def completion_fish_cmd():
    """Print the fish completion script to stdout."""
    _print_completion("fish")


@completion_group.command("install")
@click.option("--shell", type=click.Choice(list(C.SHELLS)), required=True,
              help="shell to install completion for")
@click.option("--dest", type=click.Path(dir_okay=False),
              help="override the default install path")
@click.option("--force", is_flag=True,
              help="overwrite a divergent non-wt completion file")
def completion_install_cmd(shell, dest, force):
    """Write the completion script to the shell's conventional path (or --dest).

    \b
    wt completion install --shell fish
    wt completion install --shell zsh --dest ~/.zsh/completion/_wt
    wt completion install --shell bash --force
    """
    try:
        path = C.install(shell, dest=dest, force=force)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] {shell} completion → [white]{path}[/]")


@cli.command("state")
@click.argument("selector", shell_complete=C.complete_task_selector)
@click.argument("new_state", shell_complete=C.complete_todo_state)
@click.option("--no-closed", is_flag=True, help="don't add/remove a CLOSED: stamp")
@click.pass_obj
def state_cmd(cfg, selector, new_state, no_closed):
    """Set a task's TODO state. SELECTOR is an :ID:, file:line, JIRA key, or heading substring.

    \b
    wt state DEMO-100 DONE
    wt state est-504 INPROGRESS --no-closed
    """
    try:
        path = set_state_by_selector(cfg, selector, new_state, stamp_closed=not no_closed)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] {selector} → [bold green]{new_state}[/]  [dim]{path}[/]")


@cli.command("done")
@click.argument("selector", shell_complete=C.complete_task_selector)
@click.option("--no-closed", is_flag=True, help="don't add a CLOSED: stamp")
@click.pass_obj
def done_cmd(cfg, selector, no_closed):
    """Mark a task done (its file's first done keyword). SELECTOR as for `wt state`."""
    try:
        path = mark_done(cfg, selector, stamp_closed=not no_closed)
    except ValueError as e:
        raise click.ClickException(str(e))
    console.print(f"[green]✓[/] {selector} → [bold green]done[/]  [dim]{path}[/]")


@cli.command("snapshot")
@click.option("--through", help="archive closed days up to this YYYY-MM-DD (default: yesterday)")
@click.pass_obj
def snapshot_cmd(cfg, through):
    """Archive past days to history.jsonl so reports survive Claude transcript cleanup."""
    snapshot(cfg, through)


@cli.command("map")
@click.argument("raw", shell_complete=C.complete_mapped_topic)
@click.argument("assignments", nargs=-1, required=True)
@click.pass_obj
def map_cmd(cfg, raw, assignments):
    """Assign facets to a base topic: RAW axis=value [axis=value ...].

    \b
    wt map acme project=Acme area=perf
    wt map acme Acme          # shorthand for bucket=Acme
    """
    facets = {}
    for a in assignments:
        k, v = a.split("=", 1) if "=" in a else ("bucket", a)
        facets[k] = v
    set_facets(cfg, raw, facets)
    shown = "  ".join(f"[cyan]{k}[/]=[green]{v}[/]" for k, v in facets.items())
    console.print(f"[green]✓[/] [white]{raw}[/] → {shown}")


@cli.command("override")
@click.argument("session")
@click.argument("start")
@click.argument("end")
@click.argument("topic")
@click.option("--note", default="")
@click.pass_obj
def override_cmd(cfg, session, start, end, topic, note):
    """One-off time-scoped reclassification (SESSION START END TOPIC)."""
    add_override_rec(cfg, session, start, end, topic, note)
    console.print(f"[green]✓[/] override: {session[:8]} [{start}..{end}] → [bold green]{topic}[/]")


@cli.command("ingest-meetings")
@click.argument("rawfile", type=click.Path(exists=True, dir_okay=False))
@click.pass_obj
def ingest_cmd(cfg, rawfile):
    """Filter a raw search_events JSON dump into meetings.jsonl."""
    ingest_meetings(cfg, rawfile)


@cli.command("refresh-meetings")
@click.argument("date", required=False)
@click.option("--week", "-w", is_flag=True, help="pull the whole week (of DATE, or this week)")
@click.option("--days", type=int, default=7, help="trailing N days (default mode)")
@click.pass_obj
def refresh_cmd(cfg, date, week, days):
    """Pull Outlook calendar via headless claude + m365 MCP into meetings.jsonl."""
    tz = cfg["_tz"]
    if week:
        wd = week_days(parse_date(date or "now", tz)); start, end = wd[0], wd[-1] + dt.timedelta(days=1)
    elif date:
        d = parse_date(date, tz); start, end = d, d + dt.timedelta(days=1)
    else:
        end = dt.datetime.now(tz).date() + dt.timedelta(days=1); start = end - dt.timedelta(days=days)
    off = dt.datetime.now(tz).strftime("%z"); off = off[:3] + ":" + off[3:]
    refresh_meetings(cfg, f"{start}T00:00:00{off}", f"{end}T00:00:00{off}")


# SPEC-0169: agentic workflow groups (`wt queue`, `wt campaign`, `wt projects config|init`).
from .agentic.cli import register as _register_agentic  # noqa: E402

_register_agentic(cli, projects_group)


if __name__ == "__main__":
    cli()
