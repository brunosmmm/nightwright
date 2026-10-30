"""Terminal reports (rich), markdown/CSV/JSON export, the review timeline, and the topics
listing. All read via aggregate.assemble/build and the rules layer."""
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict

from rich import box
from rich.cells import cell_len
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from .aggregate import assemble, build
from .console import console
from .dates import parse_date, week_days
from .history import read_history
from .ingest import load_all_streams
from .org import filter_tasks, join_time, load_tasks
from .rules import load_mappings, load_overrides, resolve_facets, set_facets
from .topics import JIRA_RE, RepoResolver


def _dump_json(payload):
    """Stdout JSON for agent --json paths (SPEC-0026)."""
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def idea_row(cfg, task) -> dict:
    """Stable agent-facing row for an idea (SPEC-0026 + SPEC-0044 kind).
    Omits empty project/spec/created/updated; always includes resolved `kind`.
    `created`/`updated` are the SPEC-0076 stamps, absent on ideas never backfilled or touched."""
    from .org_write import idea_kind
    from .workflow import next_step_for_idea

    row = {
        "id": task.properties.get("ID") or task.id,
        "state": task.state or "",
        "heading": task.heading,
        "tags": sorted(task.tags),
        "kind": idea_kind(task),
        "next": next_step_for_idea(cfg, task) or "",
    }
    proj = task.properties.get("PROJECT") or ""
    if proj:
        row["project"] = proj
    spec = task.properties.get("SPEC") or ""
    if spec:
        row["spec"] = spec
    epic = (task.properties.get("EPIC") or "").strip()      # SPEC-0116
    if epic:
        row["epic"] = epic
    for key, prop in (("created", "CREATED"), ("updated", "UPDATED")):
        val = (task.properties.get(prop) or "").strip()
        if val:
            row[key] = val
    ws = (task.properties.get("WORKSTREAM") or "").strip()
    if ws:
        row["workstream"] = ws
    handed = (task.properties.get("HANDED_OFF") or "").strip()     # SPEC-0280
    if handed:
        row["handed_off"] = handed
    campaign = (task.properties.get("CAMPAIGN") or "").strip()      # SPEC-0292
    if campaign:
        row["campaign"] = campaign
    if task.priority:
        row["priority"] = task.priority
    from .explore import clock_fields, explored_count, open_question_count
    if (n := explored_count(task)):                        # SPEC-0081
        row["explored"] = n
    row.update(clock_fields(task))                         # SPEC-0103, from Task.clock (parsed)
    # SPEC-0103: one enrichment read per row. Measured over the live 43-idea corpus,
    # `wt ideas --json` was 0.583s before and 0.579s after — no measurable cost, and it saves
    # agents an `idea show` round-trip per row just to learn "does this still have open Qs?".
    row["open_questions"] = open_question_count(cfg, task)
    return row


def task_row(cfg, task) -> dict:
    """Stable agent-facing row for an org task (SPEC-0092). Omits empty optional fields."""
    row = {
        "id": task.properties.get("ID") or task.id,
        "state": task.state or "",
        "heading": task.heading,
        "tags": sorted(task.tags),
    }
    if task.priority:
        row["priority"] = task.priority
    if task.project:
        row["project"] = task.project
    if task.epic:
        row["epic"] = task.epic
    if task.topic_key:
        row["topic_key"] = task.topic_key
    if task.scheduled:
        row["scheduled"] = task.scheduled.isoformat()
    if task.deadline:
        row["deadline"] = task.deadline.isoformat()
    ws = (task.properties.get("WORKSTREAM") or "").strip()
    if ws:
        row["workstream"] = ws
    return row


def _hub_noise_states(cfg) -> set[str]:
    return {str(s).strip().upper() for s in (cfg.get("hub_noise_states") or []) if str(s).strip()}


def _drop_noise(cfg, tasks):
    noise = _hub_noise_states(cfg)
    if not noise:
        return list(tasks)
    return [t for t in tasks if (t.state or "").upper() not in noise]


def collect_idea_tasks(cfg, *, state=None, all_done=False, kind=None, tag=None, project=None,
                       workstream=None, priority=None, sort=None, desc=False, epic=None):
    """Filtered/sorted idea Tasks shared by Rich `ideas` and JSON builders.

    SPEC-0077: ordered newest-updated first, unstamped last (`_idea_freshness_key`), so the
    table, `wt ideas --json` and `wt hub` agree. `wt next` keeps its own order.
    SPEC-0089: optional `tag` / `project` forwarded to `filter_tasks` (parity with `wt tasks`).
    SPEC-0090: optional `workstream` matches `:WORKSTREAM:`.
    SPEC-0093: optional `priority` matches `[#A|B|C]`.
    SPEC-0094: optional `sort` / `desc` override (default freshness)."""
    from .org_write import idea_kind, normalize_idea_kind

    all_tasks = load_tasks(cfg)
    if all_done:
        rows = filter_tasks(all_tasks, state=state, is_idea=True, tag=tag, project=project,
                            workstream=workstream, priority=priority, epic=epic)
    else:
        rows = filter_tasks(all_tasks, state=state, is_idea=True, done=False, tag=tag,
                            project=project, workstream=workstream, priority=priority, epic=epic)
    if kind is not None:
        want = normalize_idea_kind(kind)
        rows = [t for t in rows if idea_kind(t) == want]
    return _apply_sort(rows, sort=sort, desc=desc, keys=idea_sort_keys(cfg))


NO_PROJECT = "(no project)"


def _epic_parent_map(cfg) -> dict:
    """`{child_idea_id: parent_idea_id}` derived from spec `parent:` (SPEC-0114).

    There is no idea→idea link in org. The relation is `idea -> :SPEC: -> parent spec ->
    source_idea`, so it is *derived* rather than stored — which is why no schema change is needed.
    Single-valued `parent:` is also what makes the result a forest rather than a DAG.
    """
    from .specmeta import split_frontmatter
    from .specs import _specs_dir

    specs = {}
    try:
        for path in sorted(_specs_dir(cfg).glob("[0-9]*.md")):
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            if (fm or {}).get("id"):
                specs[fm["id"]] = fm
    except OSError:
        return {}

    idea_of_spec, spec_of_idea = {}, {}
    for task in filter_tasks(load_tasks(cfg), is_idea=True):
        ident = task.properties.get("ID") or task.id
        spec = (task.properties.get("SPEC") or "").strip()
        if ident and spec:
            spec_of_idea[ident] = spec
            idea_of_spec.setdefault(spec, ident)

    out = {}
    for ident, spec in spec_of_idea.items():
        parent_spec = (specs.get(spec) or {}).get("parent")
        parent_idea = idea_of_spec.get(_hub_str(parent_spec)) if parent_spec else None
        if parent_idea and parent_idea != ident:
            out[ident] = parent_idea
    return out


def build_idea_tree(cfg, tasks, *, sort=None, desc=False, matched=None) -> list[dict]:
    """Group `tasks` as **project > epic > idea** (SPEC-0114).

    A pure function over tasks the caller already collected, so filters and search apply *before*
    grouping and the tree never re-queries. `matched` is the set of ids that satisfied the filter;
    ancestors outside it are still emitted with `matched=False` so the renderer can dim them —
    without that, a matched child appears with no indication of which epic it belongs to.

    Leaves are ordered by the SPEC-0113 key **within** their group, and groups by their own best
    member under the same key, so the key means the same thing at every level. `(no project)`
    always sorts last.
    """
    keys = idea_sort_keys(cfg)
    leaf_key = keys.get(str(sort).strip().lower()) if sort else keys["default"]
    if leaf_key is None:
        raise ValueError(f"invalid sort {sort!r}; expected one of: {', '.join(sort_key_names(keys))}")

    parent_of = _epic_parent_map(cfg)
    by_id = {}
    for t in tasks:
        ident = t.properties.get("ID") or t.id
        if ident:
            by_id[ident] = t
    matched = set(matched) if matched is not None else set(by_id)

    # Ancestors that survived only as context: pull them in so a child is never orphaned.
    for ident in list(by_id):
        parent = parent_of.get(ident)
        while parent and parent not in by_id:
            task = _idea_by_id(cfg, parent)
            if task is None:
                break
            by_id[parent] = task
            parent = parent_of.get(parent)

    def project_of(task):
        return (task.properties.get("PROJECT") or "").strip() or NO_PROJECT

    def epic_key(task):
        """The epic an idea belongs to (SPEC-0116).

        The stored `:EPIC:` first — a spec id, internal or outbound, present on 64 of 166 ideas
        and visible in the default open view. SPEC-0114 derived epic-ness from spec `parent:`
        instead, having never measured `:EPIC:`; that derivation is kept as a fallback so the
        idea→idea nesting it produces does not regress.
        """
        stored = (task.properties.get("EPIC") or "").strip()
        if stored:
            return stored
        return parent_of.get(task.properties.get("ID") or task.id)

    def order(items):
        return sorted(items, key=leaf_key, reverse=bool(desc))

    def node(task, children=()):
        ident = task.properties.get("ID") or task.id
        kids = [node(c) for c in order(children)]
        return {"id": ident, "task": task, "row": idea_row(cfg, task),
                "matched": ident in matched, "children": kids,
                "count": 1 + sum(c["count"] for c in kids)}

    groups = []
    for project in {project_of(t) for t in by_id.values()}:
        members = [t for t in by_id.values() if project_of(t) == project]
        member_ids = {t.properties.get("ID") or t.id for t in members}

        # Two node kinds share the epic level (SPEC-0116): a *parent idea* present in this group
        # (SPEC-0114's derived nesting), and a *spec id* from `:EPIC:` which has no idea of its
        # own and so becomes a synthetic node.
        kids_of, synthetic = {}, {}
        loose = []
        for t in members:
            key = epic_key(t)
            if key in member_ids and key != (t.properties.get("ID") or t.id):
                kids_of.setdefault(key, []).append(t)
            elif key:
                synthetic.setdefault(key, []).append(t)
            else:
                loose.append(t)

        nodes = [node(t, kids_of.get(t.properties.get("ID") or t.id, []))
                 for t in members
                 if (t.properties.get("ID") or t.id) in kids_of or t in loose]
        for epic_id, kids in synthetic.items():
            children = [node(t) for t in order(kids)]
            nodes.append({"id": epic_id, "task": children[0]["task"], "row": {"heading": epic_id},
                          "matched": any(c["matched"] for c in children), "children": children,
                          "count": sum(c["count"] for c in children), "synthetic": True})
        nodes.sort(key=lambda n: leaf_key(n["task"]), reverse=bool(desc))
        groups.append({"project": project, "nodes": nodes,
                       "count": sum(n["count"] for n in nodes),
                       "matched": any(n["matched"] for n in nodes)})

    def group_key(group):
        # Groups ordered by their own best member under the same key; `(no project)` last.
        best = min((leaf_key(n["task"]) for n in group["nodes"]), default=())
        return (group["project"] == NO_PROJECT, best)

    groups.sort(key=group_key)
    return groups


def _idea_by_id(cfg, ident):
    for t in filter_tasks(load_tasks(cfg), is_idea=True):
        if (t.properties.get("ID") or t.id) == ident:
            return t
    return None


def collect_idea_rows(cfg, *, state=None, all_done=False, kind=None, tag=None,
                      project=None, workstream=None, priority=None, sort=None,
                      desc=False) -> list[dict]:
    """Filtered/sorted idea rows for `wt ideas --json`."""
    return [idea_row(cfg, t) for t in collect_idea_tasks(
        cfg, state=state, all_done=all_done, kind=kind, tag=tag, project=project,
        workstream=workstream, priority=priority, sort=sort, desc=desc)]


# ---- idea search (SPEC-0086) ----------------------------------------------------------

_QUERY_SPLIT_RE = re.compile(r"\s+")


def tokenize_idea_query(text: str) -> list[str]:
    """Whitespace-split, lowercased tokens for `--query` (empty → [])."""
    return [t for t in _QUERY_SPLIT_RE.split((text or "").strip().lower()) if t]


def score_idea_match(tokens, heading, summary, questions, log):
    """AND-match score, or None on miss. Weights: heading 3, Summary 2, questions|Log 1."""
    if not tokens:
        return None
    h = (heading or "").lower()
    s = (summary or "").lower()
    rest = f"{questions or ''}\n{log or ''}".lower()
    blob = f"{h}\n{s}\n{rest}"
    if not all(tok in blob for tok in tokens):
        return None
    score = 0
    for tok in tokens:
        if tok in h:
            score += 3
        if tok in s:
            score += 2
        if tok in rest:
            score += 1
    return score


def idea_match_snippet(tokens, heading, summary, questions, log, *, width=80) -> str:
    """Short excerpt around the earliest token hit (heading → Summary → questions → Log)."""
    for text in (heading, summary, questions, log):
        raw = text or ""
        low = raw.lower()
        for tok in tokens:
            idx = low.find(tok)
            if idx < 0:
                continue
            start = max(0, idx - 20)
            end = min(len(raw), idx + len(tok) + 40)
            snip = " ".join(raw[start:end].split())
            if start > 0:
                snip = "…" + snip
            if end < len(raw):
                snip = snip + "…"
            return snip[:width]
    return " ".join((heading or "").split())[:width]


def search_idea_tasks(cfg, query, *, state=None, kind=None, tag=None, project=None,
                      workstream=None, priority=None, limit=25, epic=None):
    """Ranked idea hits for `--query` (SPEC-0086). Always scans open+closed ideas.

    Returns `list[(Task, score, snippet)]` sorted by descending score, then freshness.
    `limit` 0 means unlimited; negative treated as 0 (unlimited).
    SPEC-0089: `tag` / `project` narrow the corpus before scoring (AND with query).
    SPEC-0090: `workstream` likewise.
    SPEC-0093: `priority` likewise.
    """
    from .explore import read_idea_enrichment

    tokens = tokenize_idea_query(query)
    if not tokens:
        return []
    tasks = collect_idea_tasks(cfg, state=state, all_done=True, kind=kind, tag=tag,
                               project=project, workstream=workstream, priority=priority,
                               epic=epic)
    hits = []
    for task in tasks:
        en = read_idea_enrichment(cfg, task)
        score = score_idea_match(
            tokens, task.heading, en["summary"], en["questions"], en["log"])
        if score is None:
            continue
        snip = idea_match_snippet(
            tokens, task.heading, en["summary"], en["questions"], en["log"])
        hits.append((task, score, snip))
    hits.sort(key=lambda h: (-h[1], _idea_freshness_key(h[0])))
    if limit and limit > 0:
        hits = hits[:limit]
    return hits


def collect_next_tasks(cfg, *, all_done=False, state=None, kind=None, tag=None,
                       project=None, workstream=None, priority=None):
    """Filtered/sorted idea Tasks for `wt next` (terminal closes DROPPED/RESEARCHED omitted).
    SPEC-0096: optional filters parity with `wt ideas`."""
    from .org_write import idea_kind, normalize_idea_kind

    all_tasks = load_tasks(cfg)
    if all_done:
        rows = filter_tasks(all_tasks, is_idea=True, state=state, tag=tag, project=project,
                            workstream=workstream, priority=priority)
    else:
        rows = filter_tasks(all_tasks, is_idea=True, done=False, state=state, tag=tag,
                            project=project, workstream=workstream, priority=priority)
    if kind is not None:
        want = normalize_idea_kind(kind)
        rows = [t for t in rows if idea_kind(t) == want]
    rows = sorted(rows, key=_task_sort_key)
    return [t for t in rows if (t.state or "").upper() not in ("DROPPED", "RESEARCHED")]


def collect_next_rows(cfg, *, all_done=False, state=None, kind=None, tag=None,
                      project=None, workstream=None, priority=None) -> list[dict]:
    """Filtered/sorted idea rows for `wt next --json`."""
    return [idea_row(cfg, t) for t in collect_next_tasks(
        cfg, all_done=all_done, state=state, kind=kind, tag=tag, project=project,
        workstream=workstream, priority=priority)]


def _stale_idea_count(cfg) -> int:
    """Ideas whose state disagrees with their spec (SPEC-0078). Read-only; shared by `wt next`
    and `wt hub` so there is one definition of "out of sync"."""
    from .specs import stale_idea_count

    return stale_idea_count(cfg)


def _open_question_idea_count(cfg) -> int:
    """Ideas whose spec landed but whose questions were never closed (SPEC-0080)."""
    from .specs import open_question_idea_count

    return open_question_idea_count(cfg)


_HUB_INTERNAL_ACTIVE = frozenset({"draft", "proposed", "accepted", "in-progress"})


def _hub_str(val) -> str:
    """Coerce YAML scalars (incl. dates) to JSON-safe strings."""
    if val is None:
        return ""
    return str(val).strip() if not isinstance(val, str) else val.strip()


def hub_today_payload(cfg, *, day=None, state=None, tag=None, project=None,
                      workstream=None, priority=None) -> dict:
    """Agenda-shaped today+overdue for hub (SPEC-0092). Non-ideas only; noise states dropped.
    SPEC-0096: optional task filters."""
    days, _title = _scope_days(cfg, day, None, None)
    date = days[0]
    tasks = _drop_noise(cfg, filter_tasks(
        load_tasks(cfg), is_idea=False, state=state, tag=tag, project=project,
        workstream=workstream, priority=priority))
    by_day, overdue = _agenda_buckets(tasks, days)
    # Deduplicate tasks that appear as both SCHEDULED and DEADLINE on the same day.
    seen = set()
    day_tasks = []
    for _kind, t in by_day.get(date, []):
        key = (t.file, t.line)
        if key in seen:
            continue
        seen.add(key)
        day_tasks.append(t)
    day_tasks = sorted(day_tasks, key=_task_sort_key)
    overdue = sorted(overdue, key=lambda t: (t.deadline or date, t.heading or ""))
    return {
        "date": date,
        "overdue": [task_row(cfg, t) for t in overdue],
        "tasks": [task_row(cfg, t) for t in day_tasks],
    }


def hub_open_tasks_payload(cfg, *, state=None, tag=None, project=None, workstream=None,
                           priority=None) -> dict:
    """Capped open actionable non-idea tasks for hub --tasks-slice open (SPEC-0092).
    SPEC-0096: optional task filters."""
    rows = [t for t in filter_tasks(
        load_tasks(cfg), is_idea=False, done=False, state=state, tag=tag,
        project=project, workstream=workstream, priority=priority)
            if t.state is not None]
    rows = _drop_noise(cfg, rows)
    rows = sorted(rows, key=_task_sort_key)
    cap = int(cfg.get("hub_tasks_cap") or 40)
    if cap > 0:
        rows = rows[:cap]
    return {"slice": "open", "tasks": [task_row(cfg, t) for t in rows]}


def _hub_agentic(cfg, *, project=None) -> dict:
    """SPEC-0292: the active (non-terminal) campaigns, a per-idea index and state counts.
    Only `project` filters it; idea filters do not describe campaigns. Never raises: a failure
    is `{"error": text}` so `wt hub` keeps working."""
    try:
        from .agentic import store
        rows, by_idea, counts = [], {}, {}
        for st in store.list_campaigns(cfg, project=project):
            cost = st.get("cost") or {}
            idea_id = (st.get("idea") or {}).get("id") or (st.get("idea_link") or {}).get("id")
            rows.append({
                "id": st.get("id"), "kind": st.get("kind"), "state": st.get("state"),
                "spec_id": st.get("spec_id"), "idea_id": idea_id,
                "project": st.get("project"), "priority": st.get("priority"),
                "cost_usd": cost.get("total_usd"), "cap_usd": cost.get("cap_usd"),
                "park_reason": (st.get("park") or {}).get("reason"),
                "updated_at": st.get("updated_at")})
            if idea_id:
                by_idea.setdefault(idea_id, []).append(st.get("id"))
            counts[st.get("state")] = counts.get(st.get("state"), 0) + 1
        return {"campaigns": rows, "by_idea": by_idea, "counts": counts}
    except Exception as e:                  # noqa: BLE001 — the hub must not fail on this slice
        return {"error": str(e) or type(e).__name__}


def hub_payload(cfg, *, tasks_slice="today", state=None, kind=None, tag=None,
                project=None, workstream=None, priority=None) -> dict:
    """Read-only triage union for `wt hub --json` (SPEC-0052 + SPEC-0092 tasks slice).
    Never writes the ledger. SPEC-0096: idea filters on ideas; task filters on today/open."""
    from .specs import _outbound_specs, _specs_dir
    from .specmeta import split_frontmatter

    ideas = collect_idea_rows(cfg, all_done=False, state=state, kind=kind, tag=tag,
                              project=project, workstream=workstream, priority=priority)

    internal = []
    specs_dir = _specs_dir(cfg)
    if specs_dir.exists():
        for path in sorted(specs_dir.glob("[0-9]*.md")):
            fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
            fm = fm or {}
            status = _hub_str(fm.get("status"))
            if status not in _HUB_INTERNAL_ACTIVE:
                continue
            row = {
                "id": _hub_str(fm.get("id")),
                "title": _hub_str(fm.get("title")),
                "kind": _hub_str(fm.get("kind")) or "feature",
                "status": status,
                "milestone": _hub_str(fm.get("milestone")),
                "updated": _hub_str(fm.get("updated") or fm.get("created")),
            }
            parent = fm.get("parent")
            if parent:
                row["parent"] = _hub_str(parent)
            internal.append(row)

    outbound = []
    for s in _outbound_specs(cfg):
        fm = s["fm"] or {}
        outbound.append({
            "id": _hub_str(fm.get("id")),
            "title": _hub_str(fm.get("title")),
            "kind": _hub_str(fm.get("kind")) or "feature",
            "status": _hub_str(fm.get("status")),
            "project": s["project"],
            "target_repo": _hub_str(fm.get("target_repo")),
            "updated": _hub_str(fm.get("updated") or fm.get("created")),
            "path": str(s["path"]),
        })

    task_kw = dict(state=state, tag=tag, project=project, workstream=workstream,
                   priority=priority)
    out = {
        "schema": "wt.hub.v1",
        "ideas": ideas,
        "internal": internal,
        "outbound": outbound,
        "stale_ideas": _stale_idea_count(cfg),      # SPEC-0078
        "ideas_with_open_questions": _open_question_idea_count(cfg),   # SPEC-0080
        "agentic": _hub_agentic(cfg, project=project),                 # SPEC-0292
    }
    slice_name = (tasks_slice or "today").strip().lower()
    if slice_name == "today":
        out["today"] = hub_today_payload(cfg, **task_kw)
    elif slice_name == "open":
        out["today"] = hub_open_tasks_payload(cfg, **task_kw)
    elif slice_name == "none":
        pass
    else:
        raise ValueError(f"invalid tasks_slice {tasks_slice!r}; expected today|open|none")
    return out


def hub(cfg, *, as_json=False, tasks_slice="today", state=None, kind=None, tag=None,
        project=None, workstream=None, priority=None):
    """Emit hub triage. v1 requires --json (SPEC-0052)."""
    if not as_json:
        raise ValueError("wt hub requires --json in v1 (Rich table deferred)")
    _dump_json(hub_payload(cfg, tasks_slice=tasks_slice, state=state, kind=kind, tag=tag,
                           project=project, workstream=workstream, priority=priority))


def _scope_days(cfg, day, week, last):
    tz = cfg["_tz"]
    if week is not None:
        ds = [x.isoformat() for x in week_days(parse_date(week, tz))]
        return ds, f"Week of {ds[0]} … {ds[-1]}"
    if last:
        today = dt.datetime.now(tz).date()
        ds = [(today - dt.timedelta(days=i)).isoformat() for i in range(last - 1, -1, -1)]
        return ds, f"Last {last} days"
    d = parse_date(day, tz).isoformat()
    return [d], f"Day {d}"


def _topic_style(tp):
    if tp == "MEETINGS":      return "yellow"
    if tp == "UNCLASSIFIED":  return "bold red"
    if JIRA_RE.match(tp):     return "bold green"
    if ":" in tp:             return "cyan"
    return "white"


def _bar(frac, width=22):
    return "█" * max(0, min(width, int(round(frac * width))))


def _stats(billed, wall):
    p = f"{billed/wall:.2f}×" if wall > 0 else "—"
    return (f"billed [bold cyan]{billed/3600:.2f}h[/]   "
            f"wall [dim]{wall/3600:.2f}h[/]   parallelism [magenta]{p}[/]")


def _day_table(label, topics, wall, src=None):
    billed = sum(topics.values())
    title = f"[bold]{label}[/]" + (" [dim](archived)[/]" if src == "archive" else "")
    t = Table(title=title, caption=_stats(billed, wall), box=box.SIMPLE_HEAVY,
              title_justify="left", caption_justify="left", header_style="dim", pad_edge=False)
    t.add_column("hours", justify="right")
    t.add_column("share")
    t.add_column("topic")
    mx = max(topics.values()) if topics else 1
    for tp, secs in sorted(topics.items(), key=lambda kv: -kv[1]):
        st = _topic_style(tp)
        t.add_row(f"[bold]{secs/3600:.2f}[/]", f"[{st}]{_bar(secs/mx)}[/]", f"[{st}]{tp}[/]")
    return t


def _split_line(actor):
    c = actor.get("claude", 0.0); y = actor.get("you", 0.0); tot = c + y
    if not tot:
        return None
    return (f"   [dim]split (transcript only):[/] Claude-gen [cyan]{c/3600:.2f}h[/] "
            f"[dim]({100*c/tot:.0f}%)[/]  ·  your-turn [green]{y/3600:.2f}h[/] "
            f"[dim]({100*y/tot:.0f}%)[/]")


def _regroup(topics, axis, mappings):
    """Regroup base-topic hours by a facet axis; topics with no value keep their own name.
    Uses resolve_facets (SPEC-0027) so prefix:suffix children inherit the parent's axis."""
    out = defaultdict(float)
    for base, s in topics.items():
        out[resolve_facets(None, base, mappings=mappings).get(axis) or base] += s
    return dict(out)


def report(cfg, day=None, week=None, last=None, md=False, split=False, by=None):
    data, _ = assemble(cfg)
    by = by or (cfg.get("default_axis") if cfg.get("default_axis") not in (None, "topic") else None)
    if by:                                # regroup every day's base topics by the facet axis
        facets = load_mappings(cfg)
        for d in data:
            data[d]["topics"] = _regroup(data[d]["topics"], by, facets)
    days, title = _scope_days(cfg, day, week, last)
    if by:
        title += f"  · by {by}"
    console.print(Panel(f"[bold]{title}[/]  [dim]({cfg['timezone']})[/]",
                        expand=False, border_style="blue"))
    wk_topic = defaultdict(float); wk_wall = 0.0; wk_actor = defaultdict(float); any_data = False
    for d in days:
        dd = data.get(d)
        if not dd or not dd["topics"]:
            continue
        any_data = True
        console.print(_day_table(d, dd["topics"], dd["wall"], dd.get("src")))
        if split:
            line = _split_line(dd.get("actor", {}))
            if line:
                console.print(line)
        for tp, s in dd["topics"].items():
            wk_topic[tp] += s
        for a, s in dd.get("actor", {}).items():
            wk_actor[a] += s
        wk_wall += dd["wall"]            # days don't overlap, so summing == union
    if not any_data:
        console.print("[dim]no activity in range[/]")
    elif len(days) > 1:
        console.print(_day_table("TOTAL", wk_topic, wk_wall))
        if split:
            line = _split_line(wk_actor)
            if line:
                console.print(line)
    if md and any_data:
        _write_md(cfg, days, title, data)


def _write_md(cfg, days, title, data):
    lines = [f"# {title} ({cfg['timezone']})", ""]
    wk = defaultdict(float); wk_wall = 0.0
    for d in days:
        dd = data.get(d)
        if not dd or not dd["topics"]:
            continue
        b = sum(dd["topics"].values())
        tag = " _(archived)_" if dd.get("src") == "archive" else ""
        lines.append(f"## {d} — billed {b/3600:.2f}h | wall {dd['wall']/3600:.2f}h{tag}")
        for tp, s in sorted(dd["topics"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- {s/3600:.2f}h  {tp}"); wk[tp] += s
        wk_wall += dd["wall"]; lines.append("")
    if len(days) > 1 and wk:
        lines.append(f"## TOTAL — billed {sum(wk.values())/3600:.2f}h | wall {wk_wall/3600:.2f}h")
        for tp, s in sorted(wk.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {s/3600:.2f}h  {tp}")
    reports_dir = os.path.join(cfg["data_dir"], "reports")
    os.makedirs(reports_dir, exist_ok=True)
    name = days[0] if len(days) == 1 else f"{days[0]}_{days[-1]}"
    fn = os.path.join(reports_dir, name + ".md")
    with open(fn, "w") as f:
        f.write("\n".join(lines) + "\n")
    console.print(f"[dim]markdown → {fn}[/]")


def export(cfg, days, name, fmt="csv", out=None):
    """Write a tidy dataset: one row per (date, base topic) with hours + every facet axis
    as a column, so you can pivot by any axis in a spreadsheet. days=None -> all data."""
    data, _ = assemble(cfg)
    facets = load_mappings(cfg)
    axes = sorted({a for f in facets.values() for a in f})
    if days is None:
        days = sorted(data)
    rows = []
    for d in days:
        dd = data.get(d)
        if not dd:
            continue
        for tp, secs in sorted(dd["topics"].items(), key=lambda kv: -kv[1]):
            row = {"date": d, "topic": tp, "hours": round(secs / 3600, 3),
                   "src": dd.get("src", "live")}
            for a in axes:
                row[a] = resolve_facets(cfg, tp, facets).get(a, "")
            rows.append(row)
    exports_dir = os.path.join(cfg["data_dir"], "exports")
    os.makedirs(exports_dir, exist_ok=True)
    path = out or os.path.join(exports_dir, f"{name}.{fmt}")
    if fmt == "json":
        with open(path, "w") as f:
            json.dump(rows, f, indent=2)
    else:
        fields = ["date", "topic", "hours"] + axes + ["src"]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader(); w.writerows(rows)
    console.print(f"[green]✓[/] exported [bold]{len(rows)}[/] rows "
                  f"({len(days)} day(s), axes: {', '.join(axes) or 'none'}) → {path}")


def review(cfg, day=None, week=None, interactive=False):
    overrides = load_overrides(cfg); rr = RepoResolver(cfg)
    _, _, segments, _ = build(cfg, overrides, rr)
    _, sessions_seen = load_all_streams(cfg)
    tz = cfg["_tz"]
    if week is not None:
        scope = set(x.isoformat() for x in week_days(parse_date(week, tz)))
    else:
        scope = {parse_date(day, tz).isoformat()}
    segs = sorted((s for s in segments if s["start"].astimezone(tz).date().isoformat() in scope),
                  key=lambda s: s["start"])
    lbl = sorted(scope)
    console.print(Panel(f"[bold]Review {lbl[0]}{' … ' + lbl[-1] if len(lbl) > 1 else ''}[/]",
                        expand=False, border_style="blue"))
    t = Table(box=box.SIMPLE, header_style="dim", pad_edge=False)
    t.add_column("when"); t.add_column("dur", justify="right")
    t.add_column("topic"); t.add_column("session"); t.add_column("time")
    unclassified = defaultdict(float)
    for s in segs:
        st = _topic_style(s["topic"]); flag = " [bold red]●[/]" if s["unc"] else ""
        t.add_row(s["start"].astimezone(tz).strftime("%m-%d %H:%M"),
                  f"{s['secs']/3600:.2f}h", f"[{st}]{s['topic']}[/]{flag}",
                  (s["session"] or "")[:8],
                  "[dim]%s–%s[/]" % (s["start"].astimezone(tz).strftime("%H:%M"),
                                     s["end"].astimezone(tz).strftime("%H:%M")))
        if s["unc"]:
            unclassified[s["topic"]] += s["secs"]
    console.print(t)
    if unclassified:
        body = "\n".join(f"[bold red]{secs/3600:6.2f}h[/]  {tp}"
                         for tp, secs in sorted(unclassified.items(), key=lambda kv: -kv[1]))
        console.print(Panel(body, title="[red]Unclassified inbox[/]",
                            subtitle="[dim]wt map <topic> <bucket>  ·  wt map <topic> project=X area=Y  ·  wt review --fix[/]",
                            border_style="red", expand=False))
    orphans = [ov for ov in overrides if ov["session"] not in sessions_seen]
    if orphans:
        body = "\n".join(f"{ov['session']}  →  {ov['topic']}  [dim]{ov['note']}[/]" for ov in orphans)
        console.print(Panel(body, title="[yellow]Orphaned overrides[/]",
                            border_style="yellow", expand=False))
    if interactive and unclassified:
        console.print("\n[bold]Map unclassified topics[/] [dim](blank = skip)[/]:")
        for tp, _ in sorted(unclassified.items(), key=lambda kv: -kv[1]):
            ans = Prompt.ask(f"  [bold red]{tp}[/] bucket →", default="")
            if ans.strip():
                set_facets(cfg, tp, {"bucket": ans.strip()})
                console.print(f"    [green]✓[/] {tp} → bucket=[cyan]{ans.strip()}[/]")


def _state_style(t):
    if t.state is None:       return "dim"
    if t.is_done:             return "dim"
    if t.state == "INPROGRESS": return "bold green"
    if t.state in ("BLOCKED", "TRIAGE"): return "yellow"
    return "white"


# Hue for an idea's state cell (SPEC-0082). Dim carries "closed"; hue carries which path it closed
# down, so PROMOTED / EXPORTED / DROPPED / RESEARCHED stop looking identical. `IDEA` is
# deliberately unstyled: `dim` is wt's established "done" marker (86 uses), so dimming the newest,
# most actionable state would invert that. `yellow` is deliberately unused — `_state_style` gives it
# to BLOCKED/TRIAGE in `wt tasks`, and one colour must not mean two things across views.
_IDEA_STATE_STYLES = {
    "IDEA": "",                  # captured, untouched
    "INCUBATE": "cyan",          # being researched
    "SPECCED": "green",          # has an accepted design
    "PROMOTED": "dim green",     # tasks generated internally
    "EXPORTED": "dim cyan",      # handed off to another repo
    "SHIPPED": "bold green",     # finished (SPEC-0135)
    "DROPPED": "dim red",        # abandoned
    "RESEARCHED": "dim",         # explored, no spec
}


def _idea_state_style(t):
    """Style for an idea's *state cell* (SPEC-0082) — not its headline, which keeps the
    open/done distinction from `_state_style`. Any state outside the idea vocabulary (a custom
    `#+TODO` keyword) falls back to `_state_style`, so it still renders."""
    state = (t.state or "").upper()
    if state in _IDEA_STATE_STYLES:
        return _IDEA_STATE_STYLES[state]
    return _state_style(t)


def _task_sort_key(t):
    return (t.is_done, t.priority or "Z", t.project or "", t.heading or "")


def tasks(cfg, *, state=None, tag=None, project=None, priority=None, all_done=False,
          key=False, workstream=None, as_json=False, sort=None, desc=False):
    """List org tasks with optional filters. Default view = actionable items (a TODO state
    that isn't done); --all also includes done and stateless structural headings.
    SPEC-0090: `workstream` filters on `:WORKSTREAM:`.
    SPEC-0092: `as_json` emits wt.tasks.v1.
    SPEC-0094: optional `sort` / `desc`."""
    all_tasks = load_tasks(cfg)
    if all_done:
        rows = filter_tasks(all_tasks, state=state, tag=tag, project=project,
                            priority=priority, has_key=key or None, is_idea=False,
                            workstream=workstream)
    else:  # actionable: has a state and is not done
        rows = [t for t in filter_tasks(all_tasks, state=state, tag=tag, project=project,
                                        priority=priority, done=False,
                                        has_key=key or None, is_idea=False,
                                        workstream=workstream)
                if t.state is not None]
    rows = _apply_sort(rows, sort=sort, desc=desc, keys=task_sort_keys(cfg))
    if as_json:
        _dump_json({"schema": "wt.tasks.v1", "tasks": [task_row(cfg, t) for t in rows]})
        return
    flt = "  ".join(f"[cyan]{k}[/]=[green]{v}[/]" for k, v in (
        ("state", state), ("tag", tag), ("project", project), ("priority", priority),
        ("workstream", workstream), ("sort", sort))
        if v) or ("[dim]all[/]" if all_done else "[dim]open[/]")
    if desc and sort:
        flt += "  [cyan]dir[/]=[green]desc[/]"
    console.print(Panel(f"[bold]Tasks[/]  [dim]({flt}; {len(rows)} shown)[/]",
                        expand=False, border_style="blue"))
    if not rows:
        console.print("[dim]no matching tasks[/]")
        return
    t = Table(box=box.SIMPLE_HEAVY, header_style="dim", pad_edge=False)
    t.add_column("state"); t.add_column("pri", justify="center")
    t.add_column("task"); t.add_column("project · tags")
    for tk in rows:
        st = _state_style(tk)
        head = tk.heading
        if tk.topic_key and head.startswith(tk.topic_key):     # key inline in the heading
            rest = head[len(tk.topic_key):]
            cell = f"[{_topic_style(tk.topic_key)}]{tk.topic_key}[/][{st}]{rest}[/]"
        elif tk.topic_key:                                     # key from a property override
            cell = f"[{_topic_style(tk.topic_key)}]{tk.topic_key}[/] [{st}]{head}[/]"
        else:
            cell = f"[{st}]{head}[/]"
        tags = " ".join(f"[dim]:{x}:[/]" for x in sorted(tk.tags))
        meta = f"[dim]{tk.project}[/]" + (f"  {tags}" if tags else "")
        t.add_row(f"[{st}]{tk.state or '·'}[/]", tk.priority or "", cell, meta)
    console.print(t)


def _clip(text: str, width: int) -> str:
    """One-line triage clip — essay headlines must not fold the ideas table."""
    text = text or ""
    if width < 1 or len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


_NO_AGE = "—"


def _age(stamp_text, *, now) -> str:
    """A stamp rendered as a compact relative age (SPEC-0077): `45m`, `6h`, `3d`, `6w`, `2y`,
    or `—` when there is no usable stamp. Kept to 2-4 chars: the headline is the column that
    deserves the width (SPEC-0075). A future stamp (clock skew) floors at `0m`."""
    from .org_write import parse_inactive_stamp

    when = parse_inactive_stamp(stamp_text)
    if when is None:
        return _NO_AGE
    # Org stamps are naive local wall-clock; compare like with like whichever `now` we're given.
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    secs = max(0, (now - when).total_seconds())
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86_400:
        return f"{int(secs // 3600)}h"
    days = int(secs // 86_400)
    if days < 7:
        return f"{days}d"
    if days < 365:
        return f"{days // 7}w"
    return f"{days // 365}y"


def _idea_freshness_key(task):
    """Sort ideas newest-updated first, unstamped last (SPEC-0077).

    `(is_done, no_stamp, -epoch, heading)`: done sinks so `--all` still reads open-first; the
    `no_stamp` boolean puts never-touched ideas at the bottom rather than treating a missing
    stamp as 1970; `heading` is a stable tiebreaker, since stamps have minute granularity and
    same-minute writes are common."""
    from .org_write import parse_inactive_stamp

    when = parse_inactive_stamp(task.properties.get("UPDATED"))
    return (task.is_done, when is None,
            -when.timestamp() if when else 0.0, task.heading or "")


def _idea_priority_key(task):
    """Priority then freshness (SPEC-0094). Missing cookie sorts after C (`Z`)."""
    fres = _idea_freshness_key(task)
    return (fres[0], task.priority or "Z", *fres[1:])


def _task_freshness_key(task):
    """Tasks: newest SCHEDULED/DEADLINE first; undated last (SPEC-0094)."""
    d = task.scheduled or task.deadline
    return (task.is_done, d is None, -(d.toordinal() if d else 0), task.heading or "")


def _task_priority_key(task):
    """Priority then project/heading (SPEC-0094); aligns with _task_sort_key's pri axis."""
    return (task.is_done, task.priority or "Z", task.project or "", task.heading or "")


def _apply_sort(rows, *, sort=None, desc=False, keys: dict):
    """Apply optional `--sort`, validated **from `keys`** (SPEC-0113).

    The registry is the single source of truth. It used to be passed in but ignored — validation
    hardcoded `("freshness", "priority")` — so adding a sort key meant four coordinated edits
    (registry, that tuple, the CLI `Choice`, the desk's cycle list) and adding one to the
    registry alone silently did nothing.
    """
    if not sort:
        key = keys["default"]
    else:
        mode = str(sort).strip().lower()
        if mode not in keys or mode == "default":
            offered = ", ".join(sort_key_names(keys))
            raise ValueError(f"invalid sort {sort!r}; expected one of: {offered}")
        key = keys[mode]
    return sorted(rows, key=key, reverse=bool(desc))


# SPEC-0113: the public sort vocabulary. A tuple rather than a derived list because Click builds
# its `Choice` at import time, before any cfg exists — a test asserts these match the registries
# exactly, so the two cannot drift.
IDEA_SORT_NAMES = ("freshness", "priority", "state", "project", "kind", "questions",
                   "created", "id")
TASK_SORT_NAMES = ("freshness", "priority", "state", "project", "id")


def sort_key_names(keys: dict) -> list[str]:
    """Public sort names in a registry — everything except the internal `default` alias."""
    return [k for k in keys if k != "default"]


def _state_order(cfg, *, idea: bool):
    """`{STATE: rank}` from the configured `#+TODO` vocabulary (SPEC-0113).

    Workflow order, not alphabetical: `IDEA < INCUBATE < SPECCED < PROMOTED` is meaningful,
    whereas alphabetically `PROMOTED` lands between `INCUBATE` and `SPECCED` and interleaves
    live work with finished work. Unknown states sort last so a custom keyword still renders.
    """
    key = "org_idea_keywords" if idea else "org_todo_keywords"
    words = [str(w).strip() for w in (cfg.get(key) or []) if str(w).strip()]
    return {w.upper(): i for i, w in enumerate(w for w in words if w != "|")}


def idea_sort_keys(cfg) -> dict:
    """Sort registry for ideas — one entry per column the tables show (SPEC-0113).

    Each key is a *tuple* ending in a stable tiebreaker, so equal primaries keep a deterministic
    order rather than depending on file order.
    """
    order = _state_order(cfg, idea=True)

    def _ident(task):
        return task.properties.get("ID") or task.id or ""

    def _by_state(task):
        return (order.get((task.state or "").upper(), len(order)), _idea_freshness_key(task))

    def _by_project(task):
        # Blank project last: a missing value is not "alphabetically first", it is unknown.
        proj = (task.properties.get("PROJECT") or "").strip()
        return (not proj, proj.lower(), _idea_freshness_key(task))

    def _by_kind(task):
        from .org_write import idea_kind
        return (idea_kind(task), _idea_freshness_key(task))

    def _by_questions(task):
        from .explore import open_question_count
        return (-open_question_count(cfg, task), _idea_freshness_key(task))

    def _by_created(task):
        from .org_write import parse_inactive_stamp
        when = parse_inactive_stamp(task.properties.get("CREATED"))
        return (when is None, -when.timestamp() if when else 0.0, _ident(task))

    return {
        "default": _idea_freshness_key,
        "freshness": _idea_freshness_key,
        "priority": _idea_priority_key,
        "state": _by_state,
        "project": _by_project,
        "kind": _by_kind,
        "questions": _by_questions,
        "created": _by_created,
        "id": _ident,
    }


def task_sort_keys(cfg) -> dict:
    """Sort registry for `wt tasks` (SPEC-0113)."""
    order = _state_order(cfg, idea=False)

    def _ident(task):
        return task.properties.get("ID") or task.id or ""

    def _by_state(task):
        return (order.get((task.state or "").upper(), len(order)), _task_sort_key(task))

    def _by_project(task):
        proj = (task.project or "").strip()
        return (not proj, proj.lower(), _task_sort_key(task))

    return {
        "default": _task_sort_key,
        "freshness": _task_freshness_key,
        "priority": _task_priority_key,
        "state": _by_state,
        "project": _by_project,
        "id": _ident,
    }


# Glyphs for the `kind` column (SPEC-0085). Bare codepoints with *no* U+FE0F variation selector:
# `⚠` is 1 cell but `⚠️` is 2, so a stray selector would silently change the column width. A test
# pins len()==1 and cell_len()==2 for each. Chosen over colour because a glyph is content — it
# survives a pipe, a file and a paste, where colour is stripped for a non-tty (cf. the SPEC-0083
# revert).
_KIND_GLYPHS = {"idea": "\U0001F4A1", "bug": "\U0001F41B",
                "improvement": "\u2728", "chore": "\U0001F9F9"}


def _kind_cell(kind: str, plain: bool) -> str:
    """The `kind` column's content (SPEC-0085): the word under `--plain`, else its glyph. An
    unrecognised value falls back to its own text rather than rendering blank."""
    if plain:
        return kind
    return _KIND_GLYPHS.get(kind, kind)


def _kind_legend() -> str:
    return " · ".join(f"{g} {k}" for k, g in _KIND_GLYPHS.items())


# Metadata columns of the idea tables (SPEC-0075): key -> (header, min_width, cap).
# `cap` bounds how wide the column may get; a column only ever reserves what its data needs,
# so a table whose every kind is "idea" reserves 4 columns, not 12.
_IDEA_META_COLS = {
    "score":   ("score",   3,  6),     # SPEC-0086 search mode only
    "id":      ("id",      10, None),  # SPEC-0140: room for `◕ `/two-space pad + IDEA-NNN
    "state":   ("state",   8,  None),
    "pri":     ("pri",     3,  3),     # SPEC-0093
    "q":       ("q",       1,  3),     # SPEC-0110: open-question count (SPEC-0103 data)
    "kind":    ("kind",    4,  12),
    "project": ("project", 6,  16),
    "epic":    ("epic",    4,  16),    # SPEC-0116: stored :EPIC:, an internal or outbound spec id
    "updated": ("updated", 7,  8),
    "next":    ("next",    10, 22),
}


def _idea_id_label(prow) -> str:
    """Id with open-clock mark (SPEC-0103 desk parity / SPEC-0140 CLI)."""
    prefix = "◕ " if prow.get("clock_open") else "  "
    return prefix + (prow.get("id") or "")


def _meta_widths(prows, keys):
    """Rendered width each metadata column needs for `prows`: the widest cell (bounded by the
    column's cap), never below the column's min_width or its own header."""
    out = {}
    for k in keys:
        header, min_w, cap = _IDEA_META_COLS[k]
        # `updated` renders as a short relative age, not the raw stamp, so measure what is shown.
        # SPEC-0140: `id` measures `_id` (glyph + id), same pattern as `_kind` / `_age`.
        if k == "id":
            widest = max(
                (cell_len(str(r.get("_id") or _idea_id_label(r))) for r in prows),
                default=0)
        else:
            cell = {"updated": "_age", "kind": "_kind"}.get(k, k)
            # Display width, not character count (SPEC-0085): a 2-cell glyph is one character, so
            # `len` would under-reserve and Rich would quietly take the difference off the headline.
            widest = max((cell_len(str(r.get(cell) or "")) for r in prows), default=0)
        if cap is not None:
            widest = min(widest, cap)
        out[k] = max(min_w, cell_len(header), widest)
    return out


def _idea_table_widths(prows, keys, *, floor=12, width=None, overhead=None):
    """Column plan for an idea table (SPEC-0075): `(widths, budget)`, where `widths` maps each
    metadata column in `keys` to its width and `budget` is what is left over for the `idea`
    headline. The headline is the column that carries the information, so when the terminal is
    too narrow to give it `floor` characters, width is reclaimed from the widest *capped*
    metadata column (kind/project/next) rather than letting Rich crop the row mid-cell.

    `width` defaults to `console.width` read at call time, which is what lets `$COLUMNS` and
    terminal resizes take effect. SPEC-0110 lets a caller pass its own budget instead, so the
    TUI can plan against its *pane* width and both surfaces share one planner rather than the
    desk carrying a hardcoded guess."""
    widths = _meta_widths(prows, keys)
    ncols = len(keys) + 1                     # metadata columns + the headline column
    # Table chrome for box.SIMPLE_HEAVY + pad_edge=False, measured rather than derived: two
    # padding spaces per column plus one separator between adjacent columns => 3n - 1. The
    # headline column also carries overflow="ellipsis", so if a future Rich changes this
    # arithmetic the symptom is a visible "…" one column early, not a silent crop.
    # SPEC-0110: a caller with different table chrome (Textual's DataTable pads 1 each side and
    # draws no separators) passes its own, rather than inheriting Rich's `box.SIMPLE_HEAVY` sums.
    overhead = (3 * ncols - 1) if overhead is None else overhead
    avail = (console.width if width is None else width) - sum(widths.values()) - overhead

    # Squeeze the capped columns (never id/state, which are fixed-shape keys) toward their
    # min_width until the headline gets its floor or there is nothing left to give.
    shrinkable = [k for k in keys if _IDEA_META_COLS[k][2] is not None]
    while avail < floor:
        k = max((k for k in shrinkable if widths[k] > _IDEA_META_COLS[k][1]),
                key=lambda k: widths[k], default=None)
        if k is None:
            break
        widths[k] -= 1
        avail += 1
    return widths, max(1, avail)


def _print_idea_tree(cfg, tree, *, plain=False):
    """Render `build_idea_tree` output (SPEC-0114).

    Indentation is charged against the heading budget the SPEC-0110 planner computes, so a nested
    row clips with an ellipsis rather than being silently cropped. Context ancestors — kept so a
    matched child is never orphaned — render dim.
    """
    for group in tree:
        console.print(f"[bold blue]▾ {group['project']}[/]  [dim]{group['count']}[/]")
        stack = [(n, 1) for n in reversed(group["nodes"])]
        while stack:
            node, depth = stack.pop()
            row = node["row"]
            indent = "  " * depth
            budget = max(12, console.width - len(indent) - 34)
            marker = "▾ " if node["children"] else "  "
            style = "white" if node["matched"] else "dim"
            count = f"  [dim]{node['count']}[/]" if node["children"] else ""
            if node.get("synthetic"):
                # An `:EPIC:` node is a spec id with no idea behind it — render it as a label,
                # not as a row with a kind glyph and a duplicated heading (SPEC-0116).
                console.print(f"{indent}[dim]{marker}[/][magenta]{node['id']}[/]{count}")
                stack.extend((c, depth + 1) for c in reversed(node["children"]))
                continue
            kind = _kind_cell(row.get("kind") or "idea", plain)
            id_label = _idea_id_label(row)
            id_style = "cyan" if row.get("clock_open") else "dim"
            console.print(
                f"{indent}[dim]{marker}[/][{id_style}]{id_label}[/] {kind} "
                f"[{style}]{_clip(row.get('heading') or '', budget)}[/]{count}")
            stack.extend((c, depth + 1) for c in reversed(node["children"]))


def ideas(cfg, *, state=None, all_done=False, as_json=False, kind=None, plain=False,
          query=None, limit=25, tag=None, project=None, workstream=None, priority=None,
          sort=None, desc=False, tree=False, epic=None):
    """List ideas (SPEC-0012): org headlines whose state is in the idea vocabulary. Default
    view = open (non-done) ideas; --all also includes PROMOTED/DROPPED (or other done states
    declared by the ideas file's own #+TODO). Shows a `next` column (SPEC-0023) with the
    recommended command from `workflow.next_step_for_idea`. `--json` (as_json) emits
    wt.ideas.v1 on stdout (SPEC-0026). `--kind` filters by capture kind (SPEC-0044).

    SPEC-0075: no `next` column — `wt next` is the view for that hint, and dropping it here
    buys the headline ~22 columns. `next` stays in the JSON row, so agents lose nothing. The
    headline is clipped to `_headline_budget`, i.e. whatever the terminal leaves over, instead
    of a hard-coded 40.

    SPEC-0086: `--query TEXT` switches to ranked related-idea search over heading + enrichment
    bodies (open+closed corpus). JSON then uses `wt.ideas.search.v1` with `score`/`snippet`.
    SPEC-0089: `--tag` / `--project` narrow via `filter_tasks` (compose with query).
    SPEC-0090: `--workstream` likewise.
    SPEC-0093: `--priority` filter + `pri` column; default order stays freshness.
    SPEC-0094: optional `--sort` / `--desc` (ignored under `--query`; relevance wins)."""
    searching = bool((query or "").strip())
    if searching:
        # SPEC-0094: --query stays relevance-first; sort/desc ignored.
        hits = search_idea_tasks(cfg, query, state=state, kind=kind, tag=tag, project=project,
                                 workstream=workstream, priority=priority, limit=limit,
                                 epic=epic)
        rows = [t for t, _score, _snip in hits]
        scores = {id(t): score for t, score, _snip in hits}
        snippets = {id(t): snip for t, _score, snip in hits}
    else:
        rows = collect_idea_tasks(cfg, state=state, all_done=all_done, kind=kind, tag=tag,
                                  project=project, workstream=workstream, priority=priority,
                                  sort=sort, desc=desc, epic=epic)
        scores, snippets = {}, {}

    if as_json:
        if searching:
            out = []
            for tk in rows:
                row = idea_row(cfg, tk)
                row["score"] = scores[id(tk)]
                row["snippet"] = snippets[id(tk)]
                out.append(row)
            _dump_json({"schema": "wt.ideas.search.v1", "query": query.strip(),
                        "ideas": out})
        else:
            _dump_json({"schema": "wt.ideas.v1", "ideas": [idea_row(cfg, t) for t in rows]})
        return

    filters = [("state", state), ("kind", kind), ("tag", tag), ("project", project),
               ("epic", epic), ("priority", priority), ("workstream", workstream)]
    if not searching and sort:
        filters.append(("sort", sort))
    if searching:
        filters = [("query", query.strip())] + filters
        flt = "  ".join(f"[cyan]{k}[/]=[green]{v}[/]" for k, v in filters if v) \
            + "  [dim]corpus=all[/]"
    else:
        flt = "  ".join(f"[cyan]{k}[/]=[green]{v}[/]" for k, v in filters if v) \
            or ("[dim]all[/]" if all_done else "[dim]open[/]")
        if desc and sort:
            flt += "  [cyan]dir[/]=[green]desc[/]"
    title = "Idea search" if searching else "Ideas"
    if tree:
        title += " (tree)"
    console.print(Panel(f"[bold]{title}[/]  [dim]({flt}; {len(rows)} shown)[/]",
                        expand=False, border_style="blue"))
    if not rows:
        console.print("[dim]no matching ideas[/]")
        return
    if tree:                                                   # SPEC-0114
        matched = {(t.properties.get("ID") or t.id) for t in rows}
        _print_idea_tree(cfg, build_idea_tree(cfg, rows, sort=None if searching else sort,
                                              desc=desc, matched=matched), plain=plain)
        console.print("[dim]flat view: drop --tree · full text: wt idea show <id>[/]",
                      soft_wrap=False)
        return
    prows = [idea_row(cfg, tk) for tk in rows]
    for tk, prow in zip(rows, prows):                       # SPEC-0077: relative age per row
        prow["_age"] = _age(prow.get("updated"), now=dt.datetime.now(cfg.get("_tz")))
        prow["_kind"] = _kind_cell(prow["kind"], plain)          # SPEC-0085
        prow["_id"] = _idea_id_label(prow)                       # SPEC-0140
        prow["pri"] = tk.priority or ""                          # SPEC-0093
        n_open = prow.get("open_questions") or 0                 # SPEC-0110
        prow["q"] = str(n_open) if n_open else ""
        prow["epic"] = prow.get("epic") or ""                # SPEC-0116
        if prow.get("handed_off"):                           # SPEC-0280: pending its PR
            prow["state"] = f"{prow['state']} ⇢ PR"
        if searching:
            prow["score"] = str(scores[id(tk)])
    # Omit empty `pri` column when no row has a cookie (keeps narrow terminals viable).
    show_pri = any(p["pri"] for p in prows)
    # SPEC-0110: same rule for `q` — a column of blanks would cost width for nothing, exactly
    # the reason `pri` is conditional and `score` is search-only.
    show_q = any(p["q"] for p in prows)
    show_epic = any(p["epic"] for p in prows)                # SPEC-0116, same rule
    optional = (("pri",) if show_pri else ()) + (("q",) if show_q else ())
    tail = ("kind", "project") + (("epic",) if show_epic else ()) + ("updated",)
    if searching:
        keys = ("score", "id", "state") + optional + tail
    else:
        keys = ("id", "state") + optional + tail
    widths, budget = _idea_table_widths(prows, keys)

    t = Table(box=box.SIMPLE_HEAVY, header_style="dim", pad_edge=False)
    for k in keys:
        t.add_column(k, no_wrap=True, min_width=widths[k])
    t.add_column("idea", no_wrap=True, overflow="ellipsis")
    for tk, prow in zip(rows, prows):
        st = _state_style(tk)                    # headline: open vs done, unchanged
        sst = _idea_state_style(tk)              # state cell: hue (SPEC-0082)
        proj = prow.get("project", "")
        cells = []
        for k in keys:
            if k == "score":
                cells.append(f"[cyan]{_clip(prow['score'], widths['score'])}[/]")
            elif k == "id":
                label = _clip(prow.get("_id") or _idea_id_label(prow), widths["id"])
                if prow.get("clock_open"):
                    cells.append(f"[cyan]{label}[/]")
                else:
                    cells.append(f"[dim]{label}[/]")
            elif k == "state":
                cells.append(
                    f"[{sst}]{_clip(prow['state'] or '·', widths['state'])}[/]" if sst
                    else _clip(prow["state"] or "·", widths["state"]))
            elif k == "pri":
                cells.append(_clip(prow["pri"], widths["pri"]))
            elif k == "q":
                cells.append(f"[yellow]{_clip(prow['q'], widths['q'])}[/]" if prow["q"] else "")
            elif k == "kind":
                cells.append(f"[magenta]{_clip(prow['_kind'], widths['kind'])}[/]")
            elif k == "project":
                cells.append(f"[green]{_clip(proj, widths['project'])}[/]" if proj else "")
            elif k == "epic":
                cells.append(f"[magenta]{_clip(prow['epic'], widths['epic'])}[/]"
                             if prow["epic"] else "")
            elif k == "updated":
                cells.append(f"[dim]{_clip(prow['_age'], widths['updated'])}[/]")
        cells.append(f"[{st}]{_clip(prow['heading'], budget)}[/]")
        t.add_row(*cells)
    console.print(t)
    console.print("[dim]full text: wt idea show <id> · structured: wt ideas --json · "
                  "related: wt ideas --query … · next steps: wt next[/]", soft_wrap=False)
    if not plain:                                                # SPEC-0085 legend
        console.print(f"[dim]{_kind_legend()}[/]", soft_wrap=False)


def next_ideas(cfg, *, all_done=False, as_json=False, plain=False, state=None, kind=None,
               tag=None, project=None, workstream=None, priority=None):
    """`wt next` (SPEC-0023): open ideas with id / state / next / idea (+ project). This is the
    home of the next-step hint — `wt ideas` dropped its own column in SPEC-0075 (the hint stays
    in that command's JSON). `--json` emits wt.next.v1 (SPEC-0026). The headline gets whatever
    width the terminal leaves over (`_headline_budget`), not a hard-coded 40.
    SPEC-0096: optional filters parity with `wt ideas`."""
    rows = collect_next_tasks(cfg, all_done=all_done, state=state, kind=kind, tag=tag,
                              project=project, workstream=workstream, priority=priority)
    if as_json:
        _dump_json({"schema": "wt.next.v1", "ideas": [idea_row(cfg, t) for t in rows]})
        return

    filters = [("state", state), ("kind", kind), ("tag", tag), ("project", project),
               ("workstream", workstream), ("priority", priority)]
    flt = "  ".join(f"[cyan]{k}[/]=[green]{v}[/]" for k, v in filters if v) \
        or ("[dim]all (excl. DROPPED)[/]" if all_done else "[dim]open[/]")
    console.print(Panel(f"[bold]Next[/]  [dim]({flt}; {len(rows)} shown)[/]  "
                        f"[dim]see docs/WORKFLOW.md[/]",
                        expand=False, border_style="cyan"))
    stale = _stale_idea_count(cfg)
    if stale:
        console.print(f"[yellow]![/] {stale} idea(s) out of sync with their specs "
                      f"[dim]→ wt spec reconcile[/]")
    if not rows:
        console.print("[dim]no ideas need a next step[/]")
        return
    prows = [idea_row(cfg, tk) for tk in rows]
    for prow in prows:                                           # SPEC-0085
        prow["_kind"] = _kind_cell(prow["kind"], plain)
        prow["_id"] = _idea_id_label(prow)                       # SPEC-0140
    keys = ("id", "state", "kind", "project", "next")
    widths, budget = _idea_table_widths(prows, keys)

    t = Table(box=box.SIMPLE_HEAVY, header_style="dim", pad_edge=False)
    for k in keys:
        t.add_column(k, no_wrap=True, min_width=widths[k],
                     style="cyan" if k == "next" else None)
    t.add_column("idea", no_wrap=True, overflow="ellipsis")
    for tk, prow in zip(rows, prows):
        st = _state_style(tk)                    # headline: open vs done, unchanged
        sst = _idea_state_style(tk)              # state cell: hue (SPEC-0082)
        proj = prow.get("project", "")
        id_label = _clip(prow.get("_id") or _idea_id_label(prow), widths["id"])
        id_cell = (f"[cyan]{id_label}[/]" if prow.get("clock_open")
                   else f"[dim]{id_label}[/]")
        t.add_row(id_cell,
                  f"[{sst}]{_clip(prow['state'] or '·', widths['state'])}[/]" if sst
                  else _clip(prow["state"] or "·", widths["state"]),
                  f"[magenta]{_clip(prow['_kind'], widths['kind'])}[/]",
                  f"[green]{_clip(proj, widths['project'])}[/]" if proj else "",
                  _clip(prow["next"], widths["next"]),
                  f"[{st}]{_clip(prow['heading'], budget)}[/]")
    console.print(t)


def _agenda_buckets(tasks, days):
    """Pure selection for `wt agenda`. Returns (by_day, overdue):
      by_day: {day_iso: [(kind, task), ...]} for SCHEDULED/DEADLINE dates within `days`;
      overdue: not-done tasks whose DEADLINE is before days[0]."""
    scope = set(days)
    start = days[0]
    by_day = defaultdict(list)
    overdue = []
    for t in tasks:
        if t.scheduled and t.scheduled.isoformat() in scope:
            by_day[t.scheduled.isoformat()].append(("SCHEDULED", t))
        if t.deadline:
            di = t.deadline.isoformat()
            if di in scope:
                by_day[di].append(("DEADLINE", t))
            elif di < start and not t.is_done:
                overdue.append(t)
    return by_day, overdue


def _agenda_head(t):
    if t.topic_key and t.heading.startswith(t.topic_key):
        rest = t.heading[len(t.topic_key):]
        return f"[{_topic_style(t.topic_key)}]{t.topic_key}[/]{rest}"
    return t.heading


def agenda(cfg, day=None, week=None, last=None, *, include_ideas=False, state=None,
           kind=None, tag=None, project=None, workstream=None, priority=None):
    """Date view of tasks by SCHEDULED/DEADLINE over the standard scope, plus overdue.

    SPEC-0088: default excludes ideas (`is_idea=False`), matching `wt tasks`. Pass
    `include_ideas=True` (`wt agenda --ideas`) for tasks ∪ dated ideas. Filtering happens
    here; `_agenda_buckets` stays date-pure.
    SPEC-0096: optional filters; `--kind` only applies to idea rows when ideas are included."""
    from .org_write import idea_kind, normalize_idea_kind

    days, title = _scope_days(cfg, day, week, last)
    tasks = load_tasks(cfg)
    if not include_ideas:
        tasks = filter_tasks(tasks, is_idea=False, state=state, tag=tag, project=project,
                             workstream=workstream, priority=priority)
    else:
        tasks = filter_tasks(tasks, state=state, tag=tag, project=project,
                             workstream=workstream, priority=priority)
        if kind is not None:
            want = normalize_idea_kind(kind)
            tasks = [t for t in tasks if (not t.is_idea) or idea_kind(t) == want]
    by_day, overdue = _agenda_buckets(tasks, days)
    filters = [("state", state), ("kind", kind), ("tag", tag), ("project", project),
               ("workstream", workstream), ("priority", priority)]
    flt = "  ".join(f"[cyan]{k}[/]=[green]{v}[/]" for k, v in filters if v)
    extra = "  [dim](+ideas)[/]" if include_ideas else ""
    if flt:
        extra += f"  {flt}"
    console.print(Panel(f"[bold]Agenda — {title}[/]  [dim]({cfg['timezone']})[/]{extra}",
                        expand=False, border_style="blue"))
    for d in days:
        entries = by_day.get(d)
        if not entries:
            continue
        t = Table(title=f"[bold]{d}[/]", box=box.SIMPLE, header_style="dim",
                  title_justify="left", pad_edge=False)
        t.add_column("when"); t.add_column("state"); t.add_column("task")
        for kind_label, tk in sorted(entries, key=lambda kt: (kt[0] != "DEADLINE", kt[1].heading)):
            kstyle = "red" if kind_label == "DEADLINE" else "cyan"
            st = _state_style(tk)
            t.add_row(f"[{kstyle}]{kind_label}[/]", f"[{st}]{tk.state or '·'}[/]",
                      _agenda_head(tk))
        console.print(t)
    if overdue:
        body = "\n".join(f"[red]{tk.deadline.isoformat()}[/]  [{_state_style(tk)}]{tk.state or '·'}[/]  "
                         f"{_agenda_head(tk)}" for tk in
                         sorted(overdue, key=lambda x: x.deadline))
        console.print(Panel(body, title="[red]Overdue[/]", border_style="red", expand=False))
    elif not any(by_day.get(d) for d in days):
        console.print("[dim]nothing scheduled in range[/]")


def _scope_topic_secs(cfg, days):
    """Sum aggregate.assemble base-topic seconds over the scope days (excluding MEETINGS)."""
    data, _ = assemble(cfg)
    topic_secs = defaultdict(float)
    for d in days:
        dd = data.get(d)
        if not dd:
            continue
        for tp, s in dd["topics"].items():
            if tp != "MEETINGS":
                topic_secs[tp] += s
    return dict(topic_secs)


def digest(cfg, day=None, week=None, last=None, by=None):
    """Join tracked time to org tasks by topic_key over a scope; show hours per task (or
    project), tracked topics with no task, and JIRA-linked tasks with no tracked time."""
    days, title = _scope_days(cfg, day, week, last)
    topic_secs = _scope_topic_secs(cfg, days)
    tasks = load_tasks(cfg)
    hours_by_key, key_to_tasks, untracked = join_time(tasks, topic_secs)
    if by:
        title += f"  · by {by}"
    console.print(Panel(f"[bold]Digest — {title}[/]  [dim]({cfg['timezone']})[/]",
                        expand=False, border_style="blue"))

    if by == "project":
        proj_secs = defaultdict(float)
        for key, secs in hours_by_key.items():
            for proj in {t.project for t in key_to_tasks[key]}:
                proj_secs[proj] += secs
        rows = sorted(proj_secs.items(), key=lambda kv: -kv[1])
        t = Table(box=box.SIMPLE_HEAVY, header_style="dim", pad_edge=False,
                  caption=_stats(sum(proj_secs.values()), sum(proj_secs.values())),
                  caption_justify="left")
        t.add_column("hours", justify="right"); t.add_column("share"); t.add_column("project")
        mx = max(proj_secs.values(), default=1)
        for proj, secs in rows:
            t.add_row(f"[bold]{secs/3600:.2f}[/]", _bar(secs / mx), proj)
        console.print(t) if rows else console.print("[dim]no tracked time joined to tasks[/]")
    else:
        pairs = []  # (secs, task)
        for key, secs in hours_by_key.items():
            for tk in key_to_tasks[key]:
                pairs.append((secs, tk))
        pairs.sort(key=lambda p: -p[0])
        t = Table(box=box.SIMPLE_HEAVY, header_style="dim", pad_edge=False)
        t.add_column("hours", justify="right"); t.add_column("share")
        t.add_column("task"); t.add_column("state")
        mx = max((s for s, _ in pairs), default=1)
        for secs, tk in pairs:
            st = _topic_style(tk.topic_key)
            t.add_row(f"[bold]{secs/3600:.2f}[/]", f"[{st}]{_bar(secs / mx)}[/]",
                      f"[{st}]{tk.topic_key}[/] {tk.heading}",
                      f"[{_state_style(tk)}]{tk.state or '·'}[/]")
        console.print(t) if pairs else console.print("[dim]no tracked time joined to tasks[/]")

    if untracked:
        body = "\n".join(f"[bold]{s/3600:6.2f}h[/]  [{_topic_style(tp)}]{tp}[/]"
                         for tp, s in sorted(untracked.items(), key=lambda kv: -kv[1]))
        console.print(Panel(body, title="[yellow]Untracked in org[/]",
                            subtitle="[dim]tracked topics with no matching task[/]",
                            border_style="yellow", expand=False))
    # JIRA-linked, actionable tasks that got no tracked time in scope
    no_time = sorted({t.topic_key for t in tasks
                      if t.topic_key and t.state and not t.is_done
                      and t.topic_key not in hours_by_key})
    if no_time:
        console.print(Panel("  ".join(no_time), title="[dim]No tracked time[/]",
                            subtitle="[dim]open JIRA-linked tasks with 0h in scope[/]",
                            border_style="dim", expand=False))

    _print_clock_supplement(tasks)


def _clocked_hours(task) -> float:
    """Sum of closed clock durations on a Task (SPEC-0061/0063), in hours. Open (end=None)
    entries don't contribute — they aren't done yet."""
    return sum((end - start).total_seconds() for start, end in task.clock if end is not None
              ) / 3600.0


def _print_clock_supplement(tasks) -> None:
    """SPEC-0063: a labeled, visibly-separate supplemental panel of clocked hours per idea —
    never merged into the passive hours-by-key totals above. Not scoped by day/week (clock
    entries aren't date-filtered here); an idea's ENTIRE closed clock history is shown."""
    rows = sorted(
        ((t, h) for t in tasks if t.is_idea and (h := _clocked_hours(t)) > 0),
        key=lambda th: -th[1])
    if not rows:
        return
    body = "\n".join(f"[bold]{h:6.2f}h[/]  {t.properties.get('ID') or t.id}  {t.heading}"
                     for t, h in rows)
    console.print(Panel(
        body, title="[cyan]Clocked (supplemental)[/]",
        subtitle="[dim]agent clock-in/out time — NOT included in hours above[/]",
        border_style="cyan", expand=False))


def topics(cfg, last=None, unmapped=False, by=None):
    """List discovered BASE topics (pre-mapping) with hours + current mapping target.

    `--by AXIS` (SPEC-0028): regroup all-time / `--last` hours by a facet axis (via
    resolve_facets), one row per effective value — same screen, condensed. Incompatible
    with `--unmapped`.
    """
    if by and unmapped:
        raise ValueError("--by and --unmapped cannot be combined")

    rr = RepoResolver(cfg)
    day_topic, _, _, _ = build(cfg, [], rr)  # no overrides -> raw base topics
    tz = cfg["_tz"]
    sel = None
    if last:
        today = dt.datetime.now(tz).date()
        sel = {(today - dt.timedelta(days=i)).isoformat() for i in range(last)}
    base_agg = defaultdict(float)
    live = set(day_topic)
    for d, tps in day_topic.items():
        if sel is None or d in sel:
            for tp, s in tps.items():
                if tp != "MEETINGS":
                    base_agg[tp] += s
    for rec in read_history(cfg):        # include archived days whose transcripts are gone
        if rec["day"] in live or (sel is not None and rec["day"] not in sel):
            continue
        for tp, s in rec.get("topics", {}).items():
            if tp != "MEETINGS":
                base_agg[tp] += s
    mappings = load_mappings(cfg)
    scope = f"last {last}d" if last else "all time"
    if by:
        rows = sorted(_regroup(dict(base_agg), by, mappings).items(), key=lambda kv: -kv[1])
        title = f"[bold]Topics[/]  [dim]({scope}; {len(rows)} shown · by {by})[/]"
    else:
        rows = sorted(base_agg.items(), key=lambda kv: -kv[1])
        if unmapped:
            rows = [(tp, s) for tp, s in rows if not resolve_facets(cfg, tp, mappings)]
        title = f"[bold]Topics[/]  [dim]({scope}; {len(rows)} shown)[/]"

    console.print(Panel(title, expand=False, border_style="blue"))
    t = Table(box=box.SIMPLE_HEAVY, header_style="dim", pad_edge=False)
    t.add_column("hours", justify="right")
    t.add_column("share")
    t.add_column(by if by else "base topic")
    if not by:
        t.add_column("facets")
    mx = max((s for _, s in rows), default=1)
    for tp, s in rows:
        st = _topic_style(tp)
        if by:
            t.add_row(f"[bold]{s/3600:.2f}[/]", f"[{st}]{_bar(s/mx)}[/]", f"[{st}]{tp}[/]")
        else:
            f = resolve_facets(cfg, tp, mappings)
            fac = ("  ".join(f"[cyan]{k}[/]=[green]{v}[/]" for k, v in f.items())
                   if f else "[dim]—[/]")
            t.add_row(f"[bold]{s/3600:.2f}[/]", f"[{st}]{_bar(s/mx)}[/]", f"[{st}]{tp}[/]", fac)
    console.print(t)
    if mappings:
        body = "\n".join(
            f"[white]{k}[/] → " + ", ".join(f"{a}=[cyan]{val}[/]" for a, val in v.items())
            + ("" if k in base_agg else "  [yellow](no recent activity)[/]")
            for k, v in sorted(mappings.items())
        )
        console.print(Panel(body, title="[cyan]mappings.yaml[/]", expand=False, border_style="cyan"))
    else:
        console.print("[dim]no mappings yet — add with: wt map <base topic> <axis>=<value> …[/]")
    ov = load_overrides(cfg)
    if ov:
        console.print(f"[dim]overrides.yaml: {len(ov)} entr{'y' if len(ov)==1 else 'ies'} "
                      f"(run `wt review` to spot orphans)[/]")
