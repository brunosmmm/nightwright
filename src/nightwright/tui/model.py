"""View models for the ideas desk (SPEC-0100).

No Textual import here on purpose: the desk's whole data layer is testable on a base install,
and the widget code stays a thin renderer over these dataclasses. Every value comes from the
same library calls the classic CLI uses — `collect_idea_tasks` / `search_idea_tasks` /
`idea_show_payload` — so the desk can never disagree with `nw ideas` or `nw idea show`.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .. import report as R

# States on the done side of `org_idea_keywords`' `|` are "non-active". The desk never
# hardcodes that vocabulary: `all_done` on the collectors is the same switch `nw ideas --all`
# flips, so a custom `#+TODO` line stays honest.


@dataclass
class DeskRow:
    """One list-pane row. `task` is the live org Task the detail pane re-reads."""
    id: str
    state: str
    priority: str
    kind: str
    project: str
    epic: str
    age: str
    heading: str
    is_done: bool
    state_style: str
    open_questions: int = 0        # SPEC-0103
    clock_open: bool = False       # SPEC-0103
    score: int | None = None
    snippet: str = ""

    @property
    def kind_glyph(self) -> str:
        return R._kind_cell(self.kind, plain=False)


@dataclass
class DeskDetail:
    """Detail-pane view model, straight off the `nw.idea.v1` payload."""
    id: str
    state: str
    heading: str
    meta: list[str] = field(default_factory=list)
    summary: str = ""
    open_questions: list[str] = field(default_factory=list)
    resolved_questions: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    next_hint: str = ""
    state_style: str = ""
    clock_open: bool = False       # SPEC-0103
    clocked_hours: float = 0.0     # SPEC-0103

    @property
    def open_count(self) -> int:
        return len(self.open_questions)

    @property
    def resolved_count(self) -> int:
        return len(self.resolved_questions)


def split_log(body: str) -> list[str]:
    """Split a `** Log` body into newest-first entries.

    `read_idea_enrichment` returns Log as one **string**, not a list: entries are
    `*** [2026-07-30 Thu 16:25]` headlines with prose underneath (SPEC-0031/0032). Anything
    before the first headline (a legacy plain body) becomes one leading entry, so nothing is
    silently dropped.
    """
    entries: list[str] = []
    current: list[str] = []
    for line in (body or "").splitlines():
        if line.startswith("*** "):
            if current:
                entries.append("\n".join(current).strip())
            current = [line[4:].strip()]
        else:
            current.append(line.rstrip())
    if current:
        entries.append("\n".join(current).strip())
    return [e for e in entries if e]


def _row_for(cfg, task, *, now, score=None, snippet="") -> DeskRow:
    from .. import explore as EX
    from ..org_write import idea_kind

    return DeskRow(
        id=task.properties.get("ID") or task.id or "",
        state=task.state or "",
        priority=task.priority or "",
        kind=idea_kind(task),
        project=task.properties.get("PROJECT") or "",
        epic=(task.properties.get("EPIC") or "").strip(),      # SPEC-0116
        age=R._age(task.properties.get("UPDATED"), now=now),
        heading=task.heading or "",
        is_done=task.is_done,
        state_style=R._idea_state_style(task),
        open_questions=EX.open_question_count(cfg, task),
        clock_open=EX.clock_fields(task)["clock_open"],
        score=score,
        snippet=snippet,
    )


def load_rows(cfg, *, query=None, all_done=False, state=None, kind=None, tag=None,
              project=None, workstream=None, priority=None, sort=None, desc=False,
              limit=0, now=None, epic=None) -> list[tuple[DeskRow, object]]:
    """`[(DeskRow, Task)]` for the list pane.

    With `query`, ranked `search_idea_tasks` hits (which always scan open+closed, so the
    non-active switch does not apply — the same rule as `nw ideas --query`). Without one,
    `collect_idea_tasks` in freshness order, narrowed to active states unless `all_done`.

    Duplicate `:ID:` values across org files (e.g. a copied rotation sibling under the same
    `org_files` dir) are collapsed so the desk DataTable never raises Textual's
    `DuplicateKey` when row keys are idea ids.
    """
    now = now or dt.datetime.now(tz=cfg.get("_tz"))
    filters = dict(state=state, kind=kind, tag=tag, project=project,
                   workstream=workstream, priority=priority, epic=epic)
    if query and query.strip():
        hits = R.search_idea_tasks(cfg, query, limit=limit, **filters)
        tasks_scored = [(t, s, snip) for t, s, snip in hits]
        tasks_scored = _dedupe_scored_idea_tasks(cfg, tasks_scored)
        return [(_row_for(cfg, t, now=now, score=s, snippet=snip), t)
                for t, s, snip in tasks_scored]
    tasks = R.collect_idea_tasks(cfg, all_done=all_done, sort=sort, desc=desc, **filters)
    tasks = _dedupe_idea_tasks(cfg, tasks)
    return [(_row_for(cfg, t, now=now), t) for t in tasks]


def _idea_id(task) -> str:
    return (task.properties or {}).get("ID") or task.id or ""


def _prefer_live_ideas_file(cfg, current, challenger) -> bool:
    """True if `challenger` should replace `current` when both share an idea id."""
    import os
    ideas = os.path.expanduser(cfg.get("org_ideas_file") or "")
    if not ideas:
        return False
    try:
        live = os.path.normpath(os.path.realpath(ideas))
        return os.path.normpath(os.path.realpath(challenger.file)) == live and (
            os.path.normpath(os.path.realpath(current.file)) != live)
    except OSError:
        return False


def _dedupe_idea_tasks(cfg, tasks) -> list:
    """Keep one Task per idea id; prefer `org_ideas_file` over historical siblings."""
    best: dict[str, object] = {}
    order: list[str] = []
    for t in tasks:
        iid = _idea_id(t)
        if not iid:
            # no id — keep as-is under a synthetic slot so we don't drop it
            order.append(id(t))
            best[id(t)] = t
            continue
        if iid not in best:
            order.append(iid)
            best[iid] = t
        elif _prefer_live_ideas_file(cfg, best[iid], t):
            best[iid] = t
    return [best[k] for k in order if k in best]


def _dedupe_scored_idea_tasks(cfg, scored) -> list:
    best: dict[str, tuple] = {}
    order: list[str] = []
    for t, s, snip in scored:
        iid = _idea_id(t)
        if not iid:
            key = str(id(t))
            order.append(key)
            best[key] = (t, s, snip)
            continue
        if iid not in best:
            order.append(iid)
            best[iid] = (t, s, snip)
        elif _prefer_live_ideas_file(cfg, best[iid][0], t):
            best[iid] = (t, s, snip)
    return [best[k] for k in order]



def flatten_tree(cfg, tasks, *, sort=None, desc=False, now=None) -> list[tuple]:
    """`[(DeskRow, Task, depth, project)]` for the desk's tree mode (SPEC-0114).

    **Leaves only — no synthetic group-header rows.** The desk already has a `project` column
    that labels each group, so a header row would duplicate it *and* force every mutation path
    (`selected_id`, row keys, focus, refresh) to special-case a row with no idea behind it. Rows
    are emitted grouped by project and indented by epic depth, which conveys the same structure
    with one row type. The CLI renders explicit headers instead, because its tree view has no
    project column.

    `depth` drives indentation, charged against the heading budget by the SPEC-0110 planner.
    Context ancestors (kept so a matched child is never orphaned) come back marked `is_done`, so
    the existing recessive styling applies without a new flag.
    """
    now = now or dt.datetime.now(tz=cfg.get("_tz"))
    matched = {(t.properties.get("ID") or t.id) for t in tasks}
    out: list[tuple] = []
    for group in R.build_idea_tree(cfg, tasks, sort=sort, desc=desc, matched=matched):
        stack = [(n, 0) for n in reversed(group["nodes"])]
        while stack:
            node, depth = stack.pop()
            row = _row_for(cfg, node["task"], now=now)
            row.is_done = row.is_done or not node["matched"]
            out.append((row, node["task"], depth, group["project"]))
            stack.extend((c, depth + 1) for c in reversed(node["children"]))
    return out


def load_detail(cfg, task) -> DeskDetail:
    """Detail view model for one idea, from `idea_show_payload` (`nw.idea.v1`)."""
    from ..explore import idea_show_payload
    from ..workflow import next_step_for_idea

    p = idea_show_payload(cfg, task)
    meta = [f"kind {p.get('kind') or 'idea'}"]
    for label, key in (("", "project"), ("", "spec"), ("workstream ", "workstream"),
                       ("priority ", "priority")):
        if p.get(key):
            meta.append(f"{label}{p[key]}")
    if p.get("explored"):
        n = p["explored"]
        at = p.get("explored_at")
        meta.append(f"explored {n}×" + (f" (latest {at})" if at else ""))
    for label, key in (("→ folded into ", "folded_into"),
                       ("→ superseded by ", "superseded_by"),
                       ("close reason: ", "close_reason")):
        if p.get(key):
            meta.append(f"{label}{p[key]}")

    # `_classify_question_lines` emits lowercase "open"/"resolved" — not the org keywords.
    open_q, done_q = [], []
    for item in p.get("question_items") or []:
        text = item.get("text") or ""
        if item.get("priority"):
            text = f"[#{item['priority']}] {text}"
        # SPEC-0112 renders rationale in the questions *modal*, not here — the detail pane's
        # question list is a summary, and the modal is where questions are actually worked.
        (done_q if (item.get("state") or "").lower() == "resolved" else open_q).append(text)

    research = p.get("research") or {}
    research_line = ""
    if research.get("root"):
        research_line = f"research {research['root']} ({research.get('source') or 'none'})"
    elif research.get("note"):
        research_line = f"research (none) — {research['note']}"
    if research_line:
        meta.append(research_line)

    return DeskDetail(
        id=p["id"], state=p.get("state") or "", heading=p.get("heading") or "",
        meta=meta, summary=p.get("summary") or "",
        open_questions=open_q, resolved_questions=done_q,
        log=split_log(p.get("log") or ""),
        next_hint=next_step_for_idea(cfg, task) or "",
        state_style=R._idea_state_style(task),
        clock_open=bool(p.get("clock_open")),
        clocked_hours=float(p.get("clocked_hours") or 0.0),
    )


# --- mutations (SPEC-0102) ---------------------------------------------------------------
#
# Thin named wrappers over the *existing* library mutators. The desk calls these; it never
# shells out to `nw` and never invents a write rule of its own (SPEC-0098 invariant). Keeping
# them here rather than in the widget layer means every mutation is testable without Textual.


def capture_idea(cfg, text: str, *, project: str | None = None) -> str:
    """Capture a new idea; returns its `IDEA-NNN` id (`add_idea` → `(path, headline, id)`).

    `project` is passed to `add_idea` rather than patched on afterwards (SPEC-0109), so the
    idea is *written* with its project — one write, and never a moment where it exists
    unassociated.
    """
    from ..org_write import add_idea

    if not (text or "").strip():
        raise ValueError("idea text must not be empty")
    project = (project or "").strip() or None
    _path, _headline, idea_id = add_idea(cfg, text.strip(), project=project)
    return idea_id or ""


def known_project_choices(cfg) -> list[str]:
    """Project names for the desk's chooser (SPEC-0139).

    Same names as `nw projects --json` / `projects_payload` — mappings ∪ outbox_targets ∪
    Meta-Tools — not mappings-only `known_projects` (SPEC-0018), which left the desk empty
    when operators only had outbox targets.
    """
    from ..rules import projects_payload

    try:
        return [p["name"] for p in projects_payload(cfg)["projects"]]
    except (OSError, ValueError, KeyError, TypeError):
        # KeyError: `load_mappings` wants `config_dir`. A config without one is unusual, but
        # "no projects resolvable" must degrade to an empty chooser, never break capture.
        return []


def apply_mutation(cfg, action: str, selector: str, **kw):
    """Dispatch one desk action to its library mutator.

    A single table rather than a method per binding: the desk's job is to name an action and
    hand over: every write rule stays in `explore` / `org_write`, and an unknown action is a
    programming error caught here rather than a silent no-op in a key handler.
    """
    from .. import explore as EX

    if action == "log":
        return EX.append_log(cfg, selector, kw["note"])
    if action == "summary":
        return EX.set_summary(cfg, selector, kw["text"])
    if action == "retitle":
        return EX.retitle_idea(cfg, selector, kw["title"])
    if action == "explored":
        return EX.mark_explored(cfg, selector)
    if action == "question_add":
        return EX.add_question(cfg, selector, kw["text"])
    if action == "question_resolve":
        return EX.resolve_question(cfg, selector, kw["index"])
    if action == "question_unresolve":
        return EX.unresolve_question(cfg, selector, kw["index"])
    if action == "question_edit":
        return EX.edit_question(cfg, selector, kw["index"], kw["text"])
    if action == "question_body":                                    # SPEC-0112
        return EX.set_question_body(cfg, selector, kw["index"], kw["text"])
    if action == "question_delete":
        return EX.delete_question(cfg, selector, kw["index"])

    from .. import org_write as W                                    # SPEC-0103

    if action == "kind":
        return W.set_idea_kind(cfg, selector, kw["value"])
    if action == "priority":
        return W.set_idea_priority(cfg, selector, kw["value"])
    if action == "workstream":
        return W.set_idea_workstream(cfg, selector, kw["value"])
    if action == "project":                                          # SPEC-0109
        return W.set_idea_project(cfg, selector, kw["value"])
    if action == "ext_set":                                          # SPEC-0122
        from ..idea_ext import set_idea_extension
        return set_idea_extension(cfg, selector, kw["key"], kw["value"])
    if action == "ext_clear":                                        # SPEC-0122
        from ..idea_ext import clear_idea_extension
        return clear_idea_extension(cfg, selector, kw["key"])
    if action == "tags":
        return W.set_idea_tags(cfg, selector, kw["tags"])
    if action == "close":
        return EX.close_idea(cfg, selector, outcome=kw["outcome"], reason=kw.get("reason"))
    if action == "state":                                            # SPEC-0135
        return EX.set_idea_state(cfg, selector, kw["value"])
    if action == "clock_in":
        return W.clock_in(cfg, selector)
    if action == "clock_out":
        return W.clock_out(cfg, selector)
    raise ValueError(f"unknown desk action {action!r}")


# --- hub (SPEC-0104) ----------------------------------------------------------------------


@dataclass
class HubLine:
    """One row in the hub tab. `idea_id` is set only when the row can be drilled into."""
    section: str
    left: str
    text: str
    meta: str = ""
    idea_id: str | None = None
    hint: str = ""
    style: str = ""


def load_hub(cfg, *, tasks_slice="today", **filters) -> tuple[list[HubLine], str]:
    """Flatten `hub_payload` (`nw.hub.v1`) into display lines + a summary.

    Read-only by construction: this reads the same payload `nw hub --json` emits and never
    calls a writer. Only *idea* rows carry an `idea_id`, so drilling into detail can only reach
    things the ideas desk already shows; spec rows offer a copyable CLI hint instead of a
    wizard (SPEC-0052's "hub is not a write surface" stands).
    """
    payload = R.hub_payload(cfg, tasks_slice=tasks_slice, **filters)
    lines: list[HubLine] = []

    for row in payload.get("ideas") or []:
        oq = row.get("open_questions") or 0                      # SPEC-0103
        meta = " · ".join(x for x in (
            row.get("project") or "",
            f"{oq} open q" if oq else "",
            "◕ clocked in" if row.get("clock_open") else "",
        ) if x)
        lines.append(HubLine("ideas", row.get("id") or "", row.get("heading") or "",
                             meta=meta, idea_id=row.get("id"),
                             hint=row.get("next") or ""))

    for row in payload.get("internal") or []:
        meta = " · ".join(x for x in (row.get("status") or "", row.get("milestone") or "",
                                      row.get("parent") or "") if x)
        lines.append(HubLine("internal specs", row.get("id") or "", row.get("title") or "",
                             meta=meta, hint=f"nw spec generate {row.get('id') or ''}".strip()))

    for row in payload.get("outbound") or []:
        meta = " · ".join(x for x in (row.get("status") or "", row.get("project") or "",
                                      row.get("target_repo") or "") if x)
        lines.append(HubLine("outbound", row.get("id") or "", row.get("title") or "",
                             meta=meta, hint=f"nw spec export {row.get('id') or ''}".strip()))

    today = payload.get("today") or {}
    for row in today.get("tasks") or []:
        meta = " · ".join(x for x in (row.get("state") or "", row.get("project") or "",
                                      row.get("deadline") or row.get("scheduled") or "") if x)
        lines.append(HubLine(f"tasks ({today.get('slice') or 'today'})",
                             row.get("id") or "", row.get("heading") or "", meta=meta))

    summary = (f"{len(payload.get('ideas') or [])} ideas · "
               f"{len(payload.get('internal') or [])} internal · "
               f"{len(payload.get('outbound') or [])} outbound · "
               f"{payload.get('stale_ideas', 0)} stale · "
               f"{payload.get('ideas_with_open_questions', 0)} with open Qs")
    return lines, summary


# SPEC-0110: the desk's in-app filter vocabulary is exactly `collect_idea_tasks`' arguments —
# no filter the CLI cannot also express, and no matching logic in the UI.
FILTER_KEYS = ("state", "kind", "tag", "project", "epic", "workstream", "priority")
# SPEC-0113: the desk cycles exactly the CLI's vocabulary — one registry, one set of names.
SORT_FIELDS = R.IDEA_SORT_NAMES


def parse_filter_expr(text: str) -> tuple[dict, list[str]]:
    """`"project=Example kind=bug"` -> ({project: Example, kind: bug}, []).

    Returns `(filters, problems)`; an unknown or malformed token is reported rather than
    silently dropped, because a filter that quietly does nothing looks identical to a filter
    that matched nothing.
    """
    filters, problems = {}, []
    for token in (text or "").split():
        if "=" not in token:
            problems.append(f"{token!r} is not key=value")
            continue
        key, _, value = token.partition("=")
        key, value = key.strip().lower(), value.strip()
        if key not in FILTER_KEYS:
            problems.append(f"unknown filter {key!r} (try {', '.join(FILTER_KEYS)})")
        elif not value:
            problems.append(f"{key!r} has no value")
        else:
            filters[key] = value
    return filters, problems


def format_filter_expr(filters: dict) -> str:
    """The inverse, for pre-filling the bar and for the status line."""
    return " ".join(f"{k}={v}" for k in FILTER_KEYS if (v := (filters or {}).get(k)))


def search_tokens(query: str | None) -> list[str]:
    """Tokens to highlight for `query` (SPEC-0106) — the **searcher's own** tokeniser.

    Routed through `tokenize_idea_query` on purpose: highlighting that used a different split
    would mark text the search never matched on, which is a lie about the result set. The desk
    defines no tokenising or matching of its own.
    """
    return R.tokenize_idea_query(query or "")


def status_line(rows, *, all_done: bool, query: str | None, filters: dict | None = None,
                sort: str | None = None, desc: bool = False, mode: str = "split") -> str:
    """One-line footer summary (SPEC-0110).

    Shows the *active filter set, sort and mode*, not just a count. Invisible filter state is
    how a user ends up staring at a list wondering where a row went. When a query is active it
    also says the visibility toggle is being overridden, because that is the one genuinely
    surprising interaction — search always scans open+closed (SPEC-0086).
    """
    n = len(rows)
    parts = []
    if query and query.strip():
        parts.append(f"{n} hit{'' if n == 1 else 's'}")
        parts.append(f"query {query.strip()!r}")
        parts.append("all states (search)")
    else:
        parts.append(f"{n} shown")
        parts.append("all states" if all_done else "active only")
    if (expr := format_filter_expr(filters)):
        parts.append(expr)
    if not (query and query.strip()):
        parts.append(f"sort {sort or 'freshness'}{' desc' if desc else ''}")
    if mode != "split":
        parts.append(mode)
    return " · ".join(parts)
