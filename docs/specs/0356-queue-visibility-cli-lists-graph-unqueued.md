---
id: SPEC-0356
title: "Queue visibility (CLI): untruncated lists with title/priority/blocked-by, nw queue graph, nw queue unqueued, remote gate line"
status: done
owner: user
created: 2026-11-01
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue, ux, visibility]
depends_on: [SPEC-0351]
source_idea: IDEA-615
updated: 2026-11-01
---

## Context

IDEA-615 (human, 2026-11-01): "near zero visibility on what is happening and how, what is in
the queue and what depends on what". What exists today:

- `nw campaign list` (`agentic/cli_campaign.py:150`) is a plain rich `Table` with
  `id, spec, project, state, loops, cost, updated`; no column is `no_wrap`, so on an
  80-column terminal rich shrinks every column and ids/states come out as `SPEC-035…`,
  `IMPLEMEN…`. No title, priority, dependencies or park reason. `nw.campaigns.v1`
  (`_summary`, `cli_campaign.py:25`) has no priority, depends_on, blocked_by or title.
- `nw queue list` (`_queue_table`, `agentic/cli_queue.py:101`) keeps the campaign id
  `no_wrap` but ellipsizes project at 18, glues the park reason into the state cell
  (`PARKED:budget_cap`) and has no title or depends_on. The `nw.queue.v1` items
  (`queue_payload`, `agentic/queue.py:5512`) carry `priority` and, for QUEUED items only,
  `backlog.blocked_by` as free-text strings (`"waiting on SPEC-0337 (accepted)"`).
- `nw queue backlog` shows admission order and "waiting on" text, but never the spec title
  nor the dependency ids as data.
- The dependency chain is only visible by reading `blocked_by` item by item. There is no
  `nw queue graph`.
- Nothing lists work that is accepted but **not queued**. SPEC-0349 sat `accepted` with no
  campaign and nobody noticed; `nw hub --json` has the raw parts (internal specs, outbound
  specs, ideas, `agentic.campaigns`) but never joins them.
- Remote gate runs (SPEC-0338, IDEA-641) are invisible while they run: which worker, which
  job, for how long. On the SPEC-0338 branch the finished `gate` ledger event carries
  `worker`, and `worker_failed` carries `job_id`, but no event marks a remote job as started.

This spec is the **CLI slice** of IDEA-615. The TUI queue panes the idea's log asks for
(active, backlog with blocked_by, graph, parked with reasons, gate slots/workers, mailbox)
are a separate later spec; it must reuse the JSON defined here (see *TUI contract*).

## Goals / Non-goals

**Goals**
- List views (`nw campaign list`, `nw queue list`, `nw queue backlog`) never truncate the
  campaign id or the state, always show a title, and show priority, blocked-by (waiting-on
  ids) and park reason; they adapt to terminal width by dropping low-value columns in a
  documented order. `--json` carries the same fields.
- `nw queue graph`: the queue as a dependency tree/DAG with state, blocker and cost per node.
- `nw queue unqueued`: accepted/draft specs and SPECCED/INCUBATE ideas that have no campaign,
  with the reason they are not queued where it can be derived.
- `nw campaign show` names the remote gate run (worker, job id, elapsed) when there is one.

**Non-goals**
- Any change to queue semantics: admission order, readiness, parking, slots, priorities.
  Every addition is read-only presentation plus additive JSON keys.
- TUI panes (next spec), `nw queue watch` layout (SPEC-0326/0351 own it; unchanged here).
- Emitting new ledger events from the remote gate dispatch path (see *Remote gate line*:
  the event is specified here, emitted by SPEC-0338's code under IDEA-641).
- An idea "hold" concept: none exists in code today (see Design, `unqueued` reasons).

## Decision

Introduce one row model and one column fitter, shared by every list view and by the future
TUI:

1. `queue.row_view(cfg, state, backlog_entry=None) -> dict` computes, for one campaign, the
   display fields (`title`, `priority`, `depends_on`, `blocked_by`, `waiting_on`,
   `park_reason`, `park_detail`, `cost_usd`, `cap_usd`, ...). `queue_payload`,
   `campaign_list --json` and `backlog_view` all merge it in, so the three JSON shapes agree.
2. A new module `agentic/listview.py` with a pure `fit_columns(columns, rows, width)` that
   drops columns by rank until the table fits; id and state are fixed-width (never cut),
   title is never dropped (it flexes, ellipsized, min 12 cells), park reason and blocked-by
   fold into the title cell instead of vanishing.
3. `nw queue graph` and `nw queue unqueued` are new read-only commands with their own
   versioned JSON (`nw.queue.graph.v1`, `nw.queue.unqueued.v1`).
4. `nw campaign show` prints one `remote gate` line from ledger events; in-flight needs a
   `gate_dispatched` event that SPEC-0338's dispatch must emit (contract below); until then
   only finished/failed remote runs are shown.

## Design

### Row model (`agentic/queue.py`)

`row_view(cfg, st, backlog_entry=None) -> dict` (pure over `st` plus cached file reads):

| key | source |
|---|---|
| `title` | `_spec_title(st)` (existing, mtime-cached; idea heading / merge note fallback); `None` → rendered as `—` |
| `priority` | `_priority(st)` (existing) |
| `depends_on` | the spec frontmatter `depends_on` (normalized to a list of str), read through the same mtime cache as the title (the cache becomes `_spec_meta(st) -> {title, depends_on}`); `[]` for idea-without-spec and merge campaigns |
| `blocked_by` | QUEUED: `backlog_entry["blocked_by"]` (the existing strings, unchanged). WAITING: `["waiting for a slot"]`. Otherwise `[]` |
| `waiting_on` | ids extracted from `blocked_by` by the pure `waiting_on_ids(blocked_by)`: matches `waiting on <ID> (` and `<ID> not found`; order kept, de-duplicated |
| `park_reason` / `park_detail` | `st.park.reason` / `.detail` when PARKED, else `None` |
| `cost_usd` / `cap_usd` | existing cost fields |
| `remote_gate` | only for GATING campaigns and `campaign show` (one ledger tail read): see *Remote gate line*; `None` otherwise |

`waiting_on_ids` parses strings rather than changing `backlog_readiness`'s return type so
the readiness function, its injected test doubles and the `backlog` ledger event stay
byte-identical (no queue-semantics risk).

JSON (all additive; schema names unchanged since no key is removed or retyped):
- `nw.queue.v1` items: add `title, depends_on, blocked_by, waiting_on, park_reason,
  park_detail`, plus `remote_gate` (GATING only). `priority, cost_usd, cap_usd, park` exist.
- `nw.campaigns.v1` entries (`_summary`): add `title, priority, depends_on, blocked_by,
  waiting_on, park_reason`. `cap_usd`, `cost_usd`, `park` exist. Readiness for QUEUED rows
  comes from one `backlog_view(cfg)` call per invocation (not per row).
- `nw.queue.backlog.v1` items: add `title, depends_on, waiting_on, kind, label`.

### Column fitting (`agentic/listview.py`)

```
Column(key, header, rank, fixed=False, flex=False, min_width=0, fold=None)
fit_columns(columns, rows, width) -> list[Column]
```

- Natural width of a column = max(len(header), max cell length). `fixed` columns keep their
  natural width (`no_wrap`, `min_width` = natural, overflow never ellipsizes): **id** and
  **state**. `flex` = the **title**: takes what remains, min 12, ellipsized.
- While `sum(widths) + separators + title_min > width`, drop (or fold, if `fold` is set)
  the remaining non-fixed, non-flex column with the **highest** rank number (8 =
  updated/age goes first, 1 = blocked-by goes last). A column with `fold` set is not lost: its
  text is prefixed into the title cell (`[budget_cap] `, `⧗ SPEC-0337 `), dim.
- If even id + state + title-min do not fit (very narrow), nothing more is dropped and the
  title wraps under the id (rich `overflow="fold"` on the title only); id/state are still
  whole. `width` is `console.width` (honours `COLUMNS`); render functions take `width` so
  tests are deterministic.

Documented drop order (highest rank number dropped first), same for every list table:

| rank (1 = kept longest, 8 = dropped first) | column | when dropped |
|---|---|---|
| — | campaign id | never |
| — | state | never (pure state; park reason moved to its own column) |
| — | title | never (flexes) |
| 1 | blocked-by (waiting-on ids) | folds into title |
| 2 | park reason | folds into title |
| 3 | pri | dropped |
| 4 | cost (`$1.20/15`) | dropped |
| 5 | project | dropped (`--project` filter still available) |
| 6 | loops | dropped |
| 7 | size / tone / profile | dropped (today's `_WIDE_COLS` rule is subsumed) |
| 8 | updated / age | dropped first |

The state cell keeps its existing suffixes (`rN`, `(run)`, `park?`, e.g. `SPEC_WRITING r2`,
`(detached pid N)`, `:park?` from `cli_queue.py:136-143`) and its natural width includes
them; only `:reason` moves out (to the park-reason column). `campaign list`'s `spec` column
is dropped (the title and the campaign id carry it). The Needs-you table keeps `reason` as
`fixed`, `detail` as `flex` (alongside the title, both ellipsized, each min 12) and
`resume to` at rank 3 (same rank as pri). The Backlog table's `#` is `fixed` (below).

Reasoning: the id is what a human copies into the next command and the state is the answer
to "what is it doing", so they are never cut; the title is what makes an id meaningful;
"what is it waiting on" and "why is it parked" are the questions the human could not answer
today, so they survive longest (folded, never lost); cost and project are visible elsewhere
(`campaign show`, filters); age is the least actionable.

Applied to: `nw campaign list`, `nw queue list` (Active, Needs you, Waiting, Backlog tables)
and `nw queue backlog`. The Backlog table keeps its `#` (position) column as `fixed`
(it is admission order, tiny). `nw queue watch` is unchanged.

### `nw queue graph`

`nw queue graph [--project P] [--all] [--json]` — read-only.

- **Nodes:** every non-terminal campaign (plus terminal with `--all`), keyed by its spec id
  (idea id for idea-without-spec, campaign id for merge). When several campaigns share a key,
  the newest non-terminal wins; if none is non-terminal (only possible with `--all`), the
  newest campaign wins, preferring MERGED. Every id in a node's `depends_on` that has no node becomes a
  **stub node** with its spec status read from the specs dir (`accepted, no campaign`,
  `done`, `missing`) — this is how an unqueued dependency becomes visible. Under
  `--project`, campaigns are filtered but dependency stubs from elsewhere are kept and marked
  `external: true`, so chains are never cut.
- **Edges:** `dep → dependent` from `depends_on` only (not `parent`, not `supersedes`).
- **Render (rich `Tree`):** roots = nodes with no in-graph dependency, sorted by admission
  key (priority, then age); children = dependents. A node reachable from more than one
  parent is printed in full under the first and as `↑ SPEC-X (above)` elsewhere, so output is
  linear in edges. Node line: `SPEC-0338  IMPLEMENTING  <title>  $3.20/15  pri 0` plus, when
  set, `⧗ waiting on SPEC-0337` (red if a dependency is a stub without campaign),
  `[parked: budget_cap]`. Done/merged dependencies render dim. Title ellipsized to width.
- **Cycles:** found with an iterative DFS; members are printed once in a final
  `Cycles` section and excluded from the tree; never infinite.
- **JSON `nw.queue.graph.v1`:** `{schema, nodes:[{id, campaign|null, kind, state|null,
  spec_status|null, title, project, priority, cost_usd, cap_usd, park_reason, blocked_by,
  waiting_on, depends_on, stub, external}], edges:[{from, to}], roots:[id], cycles:[[id]]}`.
  The graph builder `queue.graph_view(cfg, project=None, include_terminal=False)` is pure
  over `list_campaigns` + spec files and is what the TUI graph pane will call.

### `nw queue unqueued`

Decision: a **new command**, not a `backlog` flag — `backlog` lists campaigns in admission
order (`nw.queue.backlog.v1`); unqueued items have no campaign, no position and a different
schema, and mixing them would make the backlog JSON ambiguous for the conductor.

`nw queue unqueued [--project P] [--status S ...] [--json]`; schema `nw.queue.unqueued.v1`,
builder `queue.unqueued_view(cfg, project=None, statuses=None)`.

Candidates:
- Internal specs (`docs/specs/[0-9]*.md`) with status in `{accepted, proposed, draft}`
  (default; `--status` narrows), excluding `kind: epic` (queued via `queue add --epic`, its
  children are listed instead) and `kind: policy` (nothing to implement).
- Outbound specs (`_outbound_specs`, same statuses, same kind rule), project from the outbox.
- Ideas in `SPECCED` or `INCUBATE`. A SPECCED idea whose spec exists (any status) is never
  listed on its own: if the spec is a candidate it collapses into that row (`idea_id` set),
  otherwise the idea is omitted (the spec's lifecycle owns it). Only a SPECCED idea whose
  spec cannot be found is listed, with reason `spec-missing`
  (`idea is SPECCED but its spec was not found`). Consequently no idea row is ever `ready`.

A candidate is **unqueued** when no campaign references it (`spec_id` match; for ideas the
campaign's idea id or the spec's `source_idea`) in a non-terminal state or `MERGED`.
`MERGED` means delivered, so it is not listed; `ABANDONED`/`HANDED_OFF` campaigns do not
count as queued (listed, with the reason).

Reasons (a list of `{code, text}`; all that apply, derived in this order):

| code | when | text example |
|---|---|---|
| `not-accepted` | spec status draft/proposed | `spec is draft` |
| `deps-unmet` | `dependency_met` false for a `depends_on` id (existing function) | `waiting on SPEC-0337 (accepted)` |
| `previous-campaign` | newest terminal campaign is ABANDONED / HANDED_OFF | `campaign SPEC-0349-…-ab12 ABANDONED 2026-10-30` |
| `spec-missing` | SPECCED idea whose spec file is not found (internal or outbound) | `idea is SPECCED but its spec was not found` |
| `no-spec` | INCUBATE idea | `idea has no spec yet` |
| `ready` | none of the above | `accepted, dependencies met, never queued` |

`ready` rows are the forgotten ones (the SPEC-0349 case): they sort first and render
green, then `deps-unmet`, `previous-campaign`, `not-accepted`, `spec-missing`, `no-spec`; within a group by
id. Each row carries `next`: the command that would act on it
(`nw queue add SPEC-0349`, `nw queue add IDEA-615` for an idea campaign) — printed, never run.

"HELD" and "parked" from the idea's wording: a parked item is a campaign in PARKED, which
is non-terminal and therefore already in `nw queue list` (with its reason after this spec),
so it is not "unqueued". No hold flag exists on specs or ideas in code today, so a held
reason is not derivable; if one is introduced later it becomes a new reason code here.

Row JSON: `{kind: spec|outbound|idea, id, title, status|state, project, idea_id,
depends_on, reasons:[{code,text}], next, previous_campaign|null}`. Table columns (fitted as
above; id/status/title never dropped): `id, status, title, reason, project, next`.

### Remote gate line (`nw campaign show`)

One line after the cost line, only when the campaign's latest gate run was remote or is in
flight remotely:

```
  remote gate #7 on worker build-01  job SPEC-0338-…-gate-007-a0  running 3m12s
  remote gate #6 on worker build-01  job …-gate-006-a0  ok in 4m02s, 18m ago
  remote gate #5 worker build-01 failed (stage bundle) → ran locally
```

Sources, newest relevant event wins, from the campaign ledger tail:
- **in flight:** a `gate_dispatched` event with no later `gate` event of the same
  `gate_seq` → `running <now - started_at>`.
- **finished:** a `gate` event with a `worker` key (SPEC-0338) → `ok|failed in duration_s`;
  job id from the event's `job_id` if present, else derived as
  `<campaign>-gate-<gate_seq:03d>-a0` and marked derived in JSON.
- **fell back:** a `worker_failed` event (SPEC-0338: `{worker, stage, error, job_id}`)
  for the latest seq, followed by a local `gate` event.

No such events → no line (local-only campaigns print exactly what they print today).
JSON: `nw.campaign.v1` gains `remote_gate: {gate_seq, worker, job_id, job_id_derived,
status: running|ok|failed|fell_back, started_at, elapsed_s, duration_s} | null`; the same
object rides on `nw.queue.v1` GATING items.

**Required upstream event (IDEA-641, SPEC-0338 dispatch):** the ledger has no "remote job
started" record today, so the in-flight case is not derivable. SPEC-0338's
`remote_gates.run` must append, when the worker's `started` frame arrives:

```
gate_dispatched {gate_seq, worker, job_id, started_at}   (by="gates", informational,
                                                          added to EVENT_TYPES, conductor ignores)
```

and the finished `gate` event should also carry `job_id` when `worker` is set (so retries
under SPEC-0342 with `-a1`… are named correctly). This spec renders both when present and
degrades to the finished/fell-back forms without them; it does not emit them.

### TUI contract

The later TUI spec (IDEA-615 log) builds or rewires its panes (including the existing `tui/queue_tab.py`; `tui/mailbox_tab.py` stays as is) from these builders only, no re-parsing:
active / waiting / parked / backlog → `queue_payload` items (`row_view` fields, including
`park_reason`, `blocked_by`, `waiting_on`, `remote_gate`); dependency graph →
`graph_view`; not-yet-queued → `unqueued_view`; column fitting → `listview.fit_columns`
(cell widths in a Textual DataTable obey the same drop order). Each pane keeps its CLI
`--json` twin per SPEC-0098.

## Alternatives considered

- **Let rich shrink columns (status quo) and only add `no_wrap`.** With seven-plus columns
  `no_wrap` everywhere just overflows the terminal; an explicit ranked drop order is the
  only way to keep id/state whole at 60 columns.
- **Return structured blockers from `backlog_readiness`.** Cleaner data, but it changes the
  readiness contract, its test doubles and the `backlog` ledger event; parsing the stable
  strings keeps queue semantics untouched. Revisit if a third consumer appears.
- **`nw queue backlog --unqueued`.** Rejected: different row type and schema (see above).
- **Graph as an indented list keyed by campaign id.** Rejected: the dependency is between
  specs, and stub nodes for unqueued dependencies are the most useful thing the graph shows.
- **Emit `gate_dispatched` in this spec.** It would edit `remote_gates.py`, which lives on the
  SPEC-0338 branch not yet merged to main; owning the emission there (IDEA-641) avoids a
  cross-branch conflict. This spec defines the event so both sides agree.

## Acceptance criteria

- [x] At widths 60, 100 and 160, `nw campaign list`, `nw queue list` and `nw queue backlog`
      print every campaign id and state whole (no `…` in either), a title cell for every row,
      and no line longer than the width.
- [x] Columns disappear exactly in the documented order as width shrinks; at 160 all
      columns are present for the snapshot fixture (whose natural widths are chosen to fit
      in 160); blocked-by and park reason, when dropped as columns, appear in the
      title cell.
- [x] `nw queue list` no longer appends the park reason to the state cell; it has its own
      column (or folds into the title).
- [x] `nw campaign list --json`, `nw queue list --json` and `nw queue backlog --json` carry
      `title, priority, depends_on, blocked_by, waiting_on, park_reason, cost_usd, cap_usd`
      with identical values for the same campaign; every pre-existing key keeps its value.
- [x] `waiting_on_ids` returns `["SPEC-0337"]` for `"waiting on SPEC-0337 (accepted)"` and
      the id for `"PROJ-0012 not found"`, `[]` for status/branch blockers.
- [x] `nw queue graph` renders the fixture DAG as a tree with dependents under their
      dependencies, a shared node printed once in full and once as a reference, cycle members
      in a `Cycles` section, stub nodes for dependencies without a campaign, and terminates.
- [x] `nw queue graph --json` emits `nw.queue.graph.v1` with nodes, edges, roots, cycles;
      `--project` keeps external dependency stubs marked `external: true`.
- [x] `nw queue unqueued` lists an accepted spec with no campaign as `ready` first, lists
      draft, deps-unmet, previously-abandoned specs and INCUBATE ideas with their reasons,
      collapses a SPECCED idea into its spec row, lists a SPECCED idea whose spec is missing
      as `spec-missing`, omits a SPECCED idea whose spec exists but is not a candidate (e.g.
      `done`), and omits epics, policies, specs with a non-terminal or MERGED campaign; no
      idea row is ever `ready`.
- [x] `nw campaign show` prints the remote gate line for fixture ledgers with
      (a) `gate_dispatched` and no `gate` → running, (b) `gate` with `worker` → finished,
      (c) `worker_failed` then local `gate` → fell back; and prints no such line for a
      local-only campaign. `--json` carries `remote_gate` (null when local).
- [x] No queue behavior changes: admission, readiness, the `backlog` ledger event and
      `nw queue watch` output are unchanged (existing tests pass untouched).

## Test plan

- **Automated tests** — new `tests/test_agentic_queue_visibility.py`, fixtures built with
  `store` helpers in a tmp data dir plus a tmp specs dir (pattern of
  `tests/test_agentic_queue_cli.py`):
  - *Width snapshots:* a fixture with long ids (`SPEC-0338-20261031-23a7`), each state
    incl. PARKED (budget_cap), WAITING and QUEUED blocked on two deps. Render the three list
    commands through `CliRunner` with `COLUMNS=60|100|160` (and the render functions with
    `width=`), compare against golden files `tests/data/queue_visibility/<cmd>_<w>.txt`
    (plain text, no ANSI; regenerate with `NW_UPDATE_SNAPSHOTS=1`), and assert directly:
    no `…` inside id/state cells, every line `<= width`, the set of headers at each width
    matches the documented drop order. The fixture's natural widths are chosen so all
    columns fit in 160.
  - *fit_columns unit tests:* drop order (rank 8 dropped before rank 1), fold behavior, fixed widths, too-narrow fallback.
  - *JSON parity:* the same campaign's fields equal across the three JSON outputs; a
    pre-change key snapshot of `nw.queue.v1` items is a subset of the new output.
  - *waiting_on_ids* table test.
  - *Graph:* fixture DAG `A ← B ← C`, `A ← D`, `C` also depends on `D` (shared node),
    `E ⇄ F` (cycle), `B` depends on `X` (accepted spec, no campaign → stub), a dependency in
    another project (external under `--project`). Assert tree text, reference line,
    `Cycles` section, JSON nodes/edges/roots/cycles.
  - *Unqueued:* specs dir with accepted-no-campaign (ready), draft, accepted with unmet
    dep, accepted with an ABANDONED campaign, accepted with a MERGED campaign (omitted),
    accepted with a QUEUED campaign (omitted), an epic and a policy (omitted), an outbound
    accepted spec; ideas fixture with an INCUBATE idea, a SPECCED idea whose spec is
    listed (collapsed), a SPECCED idea whose spec is `done` (omitted) and a SPECCED idea
    whose spec does not exist (`spec-missing`). Assert order, reason codes, `next` commands,
    collapse, that the done-spec idea is absent, and that no idea row has reason `ready`.
  - *Remote gate line:* the three ledger fixtures (a)/(b)/(c) plus local-only; assert the
    line text with a frozen `now` and the `remote_gate` JSON. Fixtures (a) and (c) write raw
    JSONL lines to the campaign ledger (not `store.append_event`), because
    `gate_dispatched`/`worker_failed` are registered by SPEC-0338 and `events.validate`
    (`agentic/events.py:225`) rejects unknown types on main. The renderer reads them by
    `type` string and must not depend on `EVENT_TYPES`.
- **Manual verification:** in a real data dir run `nw queue list`, `nw campaign list`,
  `nw queue backlog` in a terminal resized to ~60, ~100, ~160 columns; `nw queue graph`;
  `nw queue unqueued` (expect any accepted-but-unqueued spec, e.g. the SPEC-0349 case, at
  the top as `ready`); `nw campaign show` on a campaign gated on a worker. Note results in
  *As shipped*.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` covering
  `tests/test_agentic_queue_cli.py`, `tests/test_agentic_queue.py`,
  `tests/test_agentic_dashboard*.py`, `tests/test_agentic_watch_height.py`; the full suite
  runs at the queue's close gate.

### Test record (2026-11-01)

- `uv run pytest -q -n 4 tests/test_agentic_queue_visibility.py`: 17 passed. Covers the width
  snapshots (golden files `tests/data/queue_visibility/{queue_list,queue_backlog,campaign_list}_{60,100,160}.txt`,
  plus CliRunner runs of the three list commands at each width: ids/states whole, every line
  `<= width`, a title cell per row), the drop order checked per table at every width 60..160
  (step 5), the folds, `fit_columns` units, `waiting_on_ids`, JSON parity across the three
  JSON outputs (with a pre-change `nw.queue.v1` key snapshot as a subset), the graph fixture
  (tree, shared-node reference, `Cycles`, stubs, `--project` external, self-loop, `--all`
  tie-break), the unqueued fixture (order, reason codes, `next`, collapse, done-spec idea
  omitted, `spec-missing`, no idea row `ready`, `--status`) and the remote gate fixtures
  (a)/(b)/(c)/local with a frozen `now`, written as raw JSONL.
- Related suites: `uv run pytest -q -n 4 tests/test_agentic_queue_cli.py
  tests/test_agentic_campaign_cli.py tests/test_agentic_dashboard.py
  tests/test_agentic_dashboard_spec_phase.py tests/test_agentic_waiting.py
  tests/test_agentic_cost_estimate.py tests/test_agentic_queue_merge.py
  tests/test_agentic_idea_campaign.py tests/test_agentic_watch_height.py
  tests/test_agentic_queue.py`: the first gate run failed
  `test_queue_watch_shows_estimate_only_when_above_zero` (cost column dropped at the default
  80 columns). A rerun of the related set plus `tests/test_tui_queue_*.py` gave 427 passed,
  2 failed (`test_queue_list_json_and_table_show_sizing`,
  `test_list_marks_overdue_and_colours_parks_by_owner`). After the fixes listed under
  *As shipped (2026-11-01, run 004)*, both pass.
- `python3 tools/affected_tests.py --include tests/test_agentic_dogfood_config.py --include
  tests/test_agentic_project.py --run uv run pytest -q -n 4` (83 files): 2510 passed,
  4 skipped, exit 0. The full suite is left to the queue's close gate.
- `python3 tools/spec_lint.py`: OK.
- Manual (real data dir, Meta-Tools queue): `nw queue list`, `nw campaign list` and
  `nw queue backlog` at `COLUMNS=60` and `160` (no line over the width, ids and states whole;
  at 60 the park reason and waiting-on ids fold into the title, e.g. `⧗ SPEC-0358 Close-…`,
  `[human] Gate timeo…`); `nw queue list` at 160 shows all columns; `nw queue graph` at
  100/120 (dependents under `done` dependency stubs, `↑ SPEC-0296 (above)` references);
  `nw queue unqueued` (40 rows: no accepted spec is unqueued today, SPEC-0349 now has a
  campaign; IDEA-616 shows `spec-missing` because SPEC-0348 is only on the queued merge
  branch; IDEA-606 shows its ABANDONED campaign). `nw campaign show` on SPEC-0338 and
  SPEC-0358 (the campaigns that ran on worker `box`): no remote line, correctly, since
  SPEC-0338's latest gate (#15) ran locally with no preceding `worker_failed`, and
  SPEC-0358's `worker_failed` is not yet followed by its local `gate` (the fallback is still
  running). No in-flight run could be shown: `gate_dispatched` is not emitted yet (IDEA-641).

### As shipped (2026-11-01)

- `listview.render(title, columns, rows, width)` builds the rich table on top of
  `fit_columns`. Columns that share a rank (size/tone/profile = 7) drop together. A
  `hide_empty` column (blocked-by, park reason) is left out when no row has a value, so the
  Active table shows no blocked-by column (active rows never wait) and a table with no parked
  row has no park column. Folded prefixes are written in the declared column order
  (blocked-by, then park reason).
- The too-narrow fallback puts the title as extra lines inside the id cell, cut at the id's
  width. Id and state stay whole as long as id + state fit in the width; below that, rich
  has to cut them.
- Backlog tables (in `nw queue list` and `nw queue backlog`) show `QUEUED` in the state
  column. The blocked-by cell (`waiting on` header) shows `✓ ready`, or `⧗` followed by the
  waiting-on ids. Blockers with no id (`spec status is draft`, `branch x missing`) are shown
  verbatim. The Waiting table's fixed state column is `continues at`. `nw campaign list` keeps
  the merge `merge:<branch>` label under the id, as `nw queue list` does.
- Internal specs in `nw queue unqueued` carry `project: Meta-Tools` (`INTERNAL_PROJECT`).
  `next` is always `nw queue add <ID>` (outbound ids resolve their project).
- Graph stubs whose dependency has a campaign outside `--project` keep that campaign's id and
  state, and are marked `external: true`.
- `remote_gate` on `nw.queue.v1` is present only on GATING items (`null` when local). A
  fell-back object also carries `stage`.
- Existing tests whose assertions this spec changes on purpose were updated:
  `test_backlog_json_order_and_list_sections` (the backlog key set is now a superset;
  `waiting_on` is asserted), `test_list_needs_you_table_and_park_keys` and
  `test_list_marks_overdue_and_colours_parks_by_owner` (the park reason has its own column,
  no `PARKED:reason`), `test_queue_list_json_and_table_show_sizing` (at 80 columns
  size/tone/profile are dropped, not shown as a dim line under the id; this replaces
  `_WIDE_COLS`) and `test_multiple_projects_in_flight_are_labelled` (pins a 160-column
  console, because project, rank 5, drops at 80). Admission, readiness, the `backlog` ledger
  event and `nw queue watch` code are untouched.
- Rollout step 4: from a campaign agent the org write to IDEA-641 is refused
  (`NW_PERSONA_RUN`). The `gate_dispatched` contract is recorded as campaign note #16 and
  in the verdict's follow-ups instead.

### As shipped (2026-11-01, run 004)

- The conductor-owned PARKED state cell is styled as on main again: plain `dim`, not dim
  yellow. The overdue marker is an unstyled space followed by a red `!` that is not dim.
- Tests now pin the fitter width where they assert rank 3-8 cells:
  `test_queue_watch_shows_estimate_only_when_above_zero` uses `width=240` (cost is rank 4)
  and `test_queue_list_json_and_table_show_sizing` uses 200 for its wide render.

### Test record (2026-11-01, run 006)

- `uv run pytest -q -n 4 tests/test_agentic_queue_visibility.py`: 20 passed. New:
  `test_narrow_backlog_title_goes_under_the_campaign_id` (the backlog table at width 45: no
  line over 45, the title wraps under the campaign id in chunks wider than one cell; it fails
  with the old `kept[0]` host), `test_remote_gate_sequences_after_dispatch` (`gate_dispatched`
  #7 then its own remote `gate` #7 gives `failed`, not `running`; `gate_dispatched` then
  `worker_failed` gives no line; then a local `gate` gives `fell_back` #7; a `worker_failed`
  with no local gate after it gives no line, the SPEC-0358 case) and
  `test_unqueued_without_internal_specs_dir`.
- Related suites (`test_agentic_queue_cli`, `test_agentic_campaign_cli`, `test_agentic_queue`,
  `test_agentic_dashboard`, `test_agentic_waiting`, `test_agentic_watch_height`,
  `test_agentic_queue_merge`, `test_tui_queue_model`, `test_tui_queue_tab`, plus the file
  above): 436 passed. The full suite is left to the queue's close gate.
- `python3 tools/spec_lint.py`: OK.

### As shipped (2026-11-01, run 006)

- The too-narrow fallback writes the title under the `campaign` (or `id`) column, else under
  the widest kept fixed column (`listview.render(title_host=...)`), never under a small leading
  column such as the backlog's `#`. The title is cut at that column's width.
- `remote_gate_view`: a `worker_failed` after the newest `gate_dispatched` ends the in-flight
  remote run (not `running`); the line then follows the finished / fell-back rules.
- `unqueued_view`: a missing internal specs dir gives no internal rows; outbound entries whose
  frontmatter is not a mapping are skipped, and a failing outbox read gives no outbound rows.

## Rollout / migration

1. `row_view`, `waiting_on_ids`, `_spec_meta` cache; JSON additions.
2. `listview.fit_columns`; switch the three list commands to it.
3. `graph_view` + `nw queue graph`; `unqueued_view` + `nw queue unqueued`.
4. Remote gate line + `remote_gate` JSON; log the `gate_dispatched` contract on IDEA-641
   for the SPEC-0338 dispatch side.
5. Document the commands and the drop order in `docs/WORKFLOW.md` (queue section).

No data migration: everything is read-only over existing state, ledgers and spec files.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; manual steps performed and noted.
- [x] No regressions (full suite at the close gate).
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. (TUI panes: separate spec under IDEA-615. `gate_dispatched` emission: IDEA-641 /
  SPEC-0338 dispatch path.)
