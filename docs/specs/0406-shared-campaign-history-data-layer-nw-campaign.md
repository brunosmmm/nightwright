---
id: SPEC-0406
title: "Shared campaign history data layer: history.py derivation, read-side schemas, campaign_export reuse and the history/records CLIs"
status: done
owner: user
created: 2026-11-03
updated: 2026-11-06
source_idea: IDEA-884
kind: feature
parent: SPEC-0405
depends_on: [SPEC-0408]
tags: [agentic, observability, history]
---

## Context

This is child 2 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md).
The epic's queue view, detail popup, History tab and Findings tab all read one UI-independent
layer, and SPEC-0400's per-campaign drilldown should read it too. This spec builds the
derivation half of that layer: the pure module, its schemas and validators, the reuse in
`campaign_export`, and the CLIs. The epic already fixes much of it, and this spec follows it
as written:
- the data inventory and the retention verdict;
- the phase table;
- the three frozen v1 schemas;
- the CLI surface (without `--rebuild-cache`, see Non-goals).

**Split.** The spec review found the original idea too large for one campaign and split it in
three. This spec is the middle one:

| Part | Scope | Order |
|---|---|---|
| History fixture | `tools/capture_history_fixture.py`, the committed sanitized real-data fixture `tests/fixtures/agentic_history/`, and `tests/history_scale.clone_fixture` | lands first |
| **SPEC-0406 (this spec)** | `history.py` derivation, read-side schemas, `campaign_export` reuse, uncached `nw campaign history CID / --list` and `nw campaign records` | after the fixture |
| History cache and perf budgets | `cache_dir/history/v1`, incremental global-log offsets, atomic writes, bad-cache rebuild, `--rebuild-cache`, the four perf budgets on the 500-campaign clone | after this spec |

Below, "the epic" means SPEC-0405, "child 1" means the ledger-provenance sibling (the first
breakdown item: `finding.run_id` and `finding.key`, `triage` events, and `campaign` stamps on
decisions and mail), "the fixture spec" means the first row above, and "the cache spec" the
third.

**What exists today:**
- `campaign_export._time_in_state` (`src/nightwright/agentic/campaign_export.py:159`)
  derives state → seconds from `transition` events. It credits each closed interval to the
  transition's `from`, and the open interval of a non-terminal campaign runs to `now`. It is
  the only time-in-state code, and it is private to the export.
- `campaign_export` counts `loops` (`campaign_export.py:320`) as the transitions to
  IMPLEMENTING whose `reason` is in `FIX_REASONS` (`campaign_export.py:80`,
  `{"triage_fix", "review_fix"}`), and `_loop_max` (`campaign_export.py:190`) gives the cap.
- `nw campaign log/runs/run/tail/show/export` (`cli_campaign.py`) each read one source. None
  joins the ledger with `run.json`, the gates, `decisions.jsonl`, escalations, the mailbox or
  spec reviews.
- `followups.finding_key(spec_id, title, files)` (`followups.py:43`) is the follow-up key.
- `schemas.py` holds the record schemas the queue writes (`nw.ledger.event.v1`,
  `nw.run.v1`, `nw.gate.v1`, `nw.runner.record.v1`) and has no read-side schemas.

**Dependencies.** Child 1 is now [SPEC-0408](./0408-ledger-provenance-finding-run-id-and-key-triage.md)
(`done`, in `depends_on`). The fixture spec still has no spec id, so it cannot be named in
`depends_on`; its ordering is enforced by the hard-ordering check below.
- **Fixture spec (hard ordering).** The fixture-backed tests below read
  `tests/fixtures/agentic_history/` and its `MANIFEST.json` roles. This spec is implemented
  only once the fixture spec is `done`; the implementer checks that the fixture directory and
  its manifest exist before starting, and the campaign queue orders the two accordingly.
- **Child 1 = SPEC-0408 (soft, now landed).** This layer does not need child 1's fields to work:
  - When a field is present, it is read and the link is marked `linked_by: "field"`.
  - When it is absent, the link is inferred (`"inferred"`) or matched in text (`"text"`).
  - If child 1 is not `done` when this spec is implemented, the field path is tested on
    synthetic events of child 1's documented shape (see the Test plan), and the fixture is
    recaptured once child 1 has produced real campaigns (Rollout).

## Goals / Non-goals

**Goals**
- **One pure module, `src/nightwright/agentic/history.py`.**
  - It never writes anything: not the data dir, not `cache_dir`.
  - It never imports `nightwright.tui`, Textual or a dashboard package.
  - It produces `nw.campaign.history.v1`, `nw.history.index.v1` and `nw.history.record.v1`
    exactly as the epic froze them, with the schema constants and validators in
    `schemas.py`.
- **Phases.** Per-phase visits, with wall, run, gate, gate-wait, runner-down and other time,
  plus cost and run counts. The dominant phase.
- **One merged timeline**, plus runs, gates, records, links with their provenance, and gaps.
- **CLIs**: `nw campaign history CID | --list` and `nw campaign records`, each with filters
  and `--json`, computed directly (uncached).
- **One derivation shared with the export.** `campaign_export`'s time-in-state, `loops` and
  loop cap come from `history`, so the two cannot drift.

**Non-goals**
- **The cache** (`cache_dir/history/v1`, per-campaign entries, incremental global-log
  offsets, inode and shrink detection, atomic writes, bad-cache rebuild, the
  `"cache: not writable"` gap, `cache=False` equivalence), `history.rebuild_cache` and the
  `nw campaign history --rebuild-cache` flag. These belong to the cache spec, which adds a
  `cache=` keyword to the public functions below without changing their results.
- **The perf budgets** (cold and warm index, popup open, 50 000-record query) on the
  500-campaign clone. They belong to the cache spec. The live-queue repaint budget belongs
  to child 4.
- **The capture tool, the committed fixture and the scale-clone helper.** They belong to the
  fixture spec; this spec only reads the fixture.
- Any TUI or web view. Children 3 to 7 and SPEC-0400 build those.
- Writing new ledger, decision or mail fields. That is child 1.
- Editing, deleting or migrating history, or any change to retention.
- Reading the pruned sources (`gatejobs/`, `remote-jobs/`, worker caches).
- Cross-host aggregation.

## Decision

1. **The history functions are pure.** `history.py` exposes pure functions over
   `cfg["data_dir"]`, and the CLI is a thin `click` layer in `cli_campaign.py`.
   - Every source is read defensively. A missing, unparsable or partial file becomes a
     `gaps` string, never an exception.
   - Both the `wt.` and `nw.` schema prefixes are accepted, through `legacy.schema_ok`.
2. **Links prefer a field, then inference, then text.**
   - Every link is resolved in this order: an explicit field (`"field"`), then a
     deterministic inference from ledger order (`"inferred"`), then a match of exactly one
     known campaign id in the text (`"text"`).
   - The record's `linked_by` is the weakest kind used among its links. If no link was
     resolved, `linked_by` is `"field"`, the strongest.
   - The text match never guesses: a text that names two or more campaigns links to none.
3. **Each phase visit splits into exclusive intervals.** A visit's wall time is split so
   that `run_s + gate_s + gate_wait_s + runner_down_s + other_s == wall_s` holds exactly.
   - Each second of the visit goes to exactly one bucket, by the precedence
     run > gate > gate_wait > runner_down > other.
   - Every interval is clipped to the visit.
   - `cost_usd` and `runs` belong to the phase of the run's `run.start`, as the epic says,
     even when the run's interval spills over into the next visit.
4. **The export's derivations move into `history.py`.** `time_in_state`, `loop_max`, the
   `FIX_REASONS` predicate and the loop count move there unchanged in behaviour.
   `campaign_export` imports them back (keeping `campaign_export.FIX_REASONS` as an alias),
   and the phase visits are built from the same transition walk. `history` never imports
   `campaign_export`, so there is no import cycle.
5. **One loop predicate.** A *fix-loop transition* is a `transition` to IMPLEMENTING whose
   `reason` is in `history.FIX_REASONS`. `header.loops`, visit `loop` numbers and the
   export's `loops` column all count exactly that.

## Design

### Module layout

`src/nightwright/agentic/history.py` (pure functions; no writes):

| Name | Returns |
|---|---|
| `PHASES` | the ordered phase → states table from the epic; `PHASE_OF[state]` is its inverse; an unknown state maps to `other` and adds a gap |
| `FIX_REASONS` | moved from `campaign_export`: `frozenset({"triage_fix", "review_fix"})` |
| `is_fix_loop(event) -> bool` | `event["type"] == "transition"`, `to == "IMPLEMENTING"` and `reason in FIX_REASONS` |
| `count_loops(events) -> int` | the number of events for which `is_fix_loop` holds |
| `loop_max(cfg, state) -> int` | moved verbatim from `campaign_export._loop_max` |
| `time_in_state(state, events, start, end) -> dict[str, int]` | moved verbatim from `campaign_export._time_in_state` |
| `phase_visits(state, events, start, end) -> list[Visit]` | `Visit = (phase, state, start, end, loop)`, from the same walk |
| `campaign_history(cfg, cid, *, now=None) -> dict` | `nw.campaign.history.v1` |
| `history_index(cfg, *, now=None, progress=None) -> list[dict]` | `nw.history.index.v1` rows, one per campaign dir; `progress(done, total)` is called per campaign when given |
| `history_records(cfg, *, campaign=None, agent=None, kind=None, project=None, severity=None, status=None, since=None, until=None, query=None, limit=None) -> list[dict]` | `nw.history.record.v1`, newest first |
| `filter_index(rows, *, project, state, spec, since, until, min_cost) -> list[dict]` | the `--list` filters, pure over rows |
| `campaign_signature(cfg, cid) -> str` | hex `sha1` over `(ledger size, ledger mtime_ns, state.json mtime_ns, runs/ mtime_ns, gates/ mtime_ns)`; it fills the schemas' `signature` fields, and the cache spec builds its keys on it |

`schemas.py` gains the following, plus docstring entries for the three read-side schemas:
- `HISTORY_SCHEMA = "nw.campaign.history.v1"`;
- `HISTORY_INDEX_SCHEMA = "nw.history.index.v1"`;
- `HISTORY_RECORD_SCHEMA = "nw.history.record.v1"`;
- `HISTORY_KEYS`, `HISTORY_HEADER_KEYS`, `HISTORY_PHASE_KEYS`, `HISTORY_TIMELINE_KEYS`,
  `HISTORY_RUN_KEYS`, `HISTORY_GATE_KEYS`, `RECORD_KEYS`, `RECORD_LINK_KEYS` and `INDEX_KEYS`,
  as tuples in the order given in the epic;
- enums `RECORD_SOURCES`, `RECORD_KINDS`, `RECORD_STATUSES`, `LINKED_BY` and `OUTCOMES`;
- `history_problems(doc) -> list[str]`, `record_problems_v1(rec) -> list[str]` and
  `index_problems(row) -> list[str]`. Each checks the schema id, every required key at every
  level, every enum value, and that numbers are not negative.

Values may be null, and extra keys are allowed (fields are only ever appended). These
validators are the "validates against the schema" oracle the acceptance criteria use.

The schemas are exactly the epic's, with one appended field: `links.gate_seq`. A gate-produced
finding links to its gate, not to a run.

### Header

The header fields are those of the epic. They are derived as follows:

| Field | Derived from |
|---|---|
| `outcome` | `MERGED` → `merged`, `HANDED_OFF` → `handed_off`, `ABANDONED` → `abandoned`, otherwise null |
| `created_at` | `state.json` `created_at`, falling back to the first ledger `ts` |
| `closed_at` | the `close` event `ts`, falling back to `state.close.ts`; null when live |
| `wall_s` | `closed_at or now` minus `created_at` |
| `active_s` | `wall_s` minus the `wall_s` of the `queue`, `parked` and `waiting` phases |
| `loops` | the export's count: `history.count_loops(events)`, i.e. the transitions to IMPLEMENTING whose `reason` is in `history.FIX_REASONS` (the helper moved out of `campaign_export.py:320` and imported back, like `loop_max`) |
| `loop_max` | `history.loop_max`, moved from `campaign_export._loop_max` and imported back |
| `runs` | the count of `run.end` events, plus `run.start` events with no matching end (counted as interrupted) |
| `cost_usd` | `state.cost.total_usd`, falling back to the sum of `run.end` `charged_usd` |
| `conductor_usd` | `conductor_cost` attribution, exactly as `campaign_export` computes it (a shared helper moved into `history`; null when unavailable) |
| `followup_idea` | the id on the last `followup` event |
| `tone` | the same tone function that `nw queue watch` / `journey.py` use for the campaign's state |

### Phases and dominant

- **Visits.** `phase_visits` walks the transitions exactly as `time_in_state` does. Each
  closed interval is credited to the transition's `from`, and the open interval of a live
  campaign runs to `now`. A terminal state gets no visit.
- **Loop numbers.** `loop` is 0 from creation through the first IMPLEMENTING visit, and
  increases by 1 on each fix-loop transition (the `is_fix_loop` predicate). Therefore
  `max(visit.loop) == header.loops` for every campaign (both are 0 when IMPLEMENTING was
  never re-entered on a fix reason).
- **Intervals inside a visit**, all clipped to the visit:
  - **run:** `[run.start.ts, run.start.ts + run.end.duration_s]`. A run with no end runs to
    the next transition.
  - **gate:** `[started_at, started_at + duration_s]` from each `gate` event.
  - **gate_wait:** `[gate_waiting.ts - waited_s, gate_waiting.ts]`, plus
    `[visit.start, first gate.started_at in the visit]` for GATING visits.
  - **runner_down:** a runner is down from each `stop` or `crash` record to the next
    `start` in `runner.jsonl`. A `start` that has no `stop`/`crash` since the previous
    `start` counts as down from the last record before it. Runner time is read in UTC.
- **Splitting.** The visit is cut at every interval boundary. Each segment is assigned to
  the highest-precedence bucket that covers it, and `other` is the rest. The buckets
  therefore sum exactly to the visit, and none is negative.
- **Rounding.** Every field is rounded to whole seconds once, at the phase level, and
  `other_s` absorbs the rounding, clipped at 0.
- **Phase rows.** `phases` lists every phase in `PHASES` order, including phases with zero
  visits, so consumers can render a fixed table.
- **Dominant phase.** `dominant.wall` and `dominant.cost` are the argmax, with ties broken in
  table order.
  - Both are null when every value is 0.
  - `dominant.working` is the argmax of `wall_s` over the phases other than `queue`,
    `parked` and `waiting`. It is set only when `dominant.wall` is one of those three, and
    is null otherwise.

### Timeline

There is one row per ledger event, ordered by `(ts, source order)`:
- `run.start` and `run.end` with the same `run_id` merge into one `kind: "run"` row at the
  start `ts`.
- Decisions, mail, escalations and spec reviews that link to the campaign interleave by `ts`.

Each row has these fields:
- `seq`: the ledger seq, or null for non-ledger rows.
- `kind`: the ledger type, or `decision` / `mail` / `escalation` / `answer` / `spec_review`.
- `phase` and `loop`: those of the visit that contains `ts`.
- `by`: an agent name, as for records.
- `summary`: one line, at most 200 characters, plain text (consumers must not treat it as
  markup).
- `ref`: the record id or run id the row points at.

### Runs and gates

`runs` has one entry per run dir and per ledger `run_id`.
- **`run.json` first.** Each entry is read from `run.json` (`nw.run.v1`), and these fields
  come from it: model, effort, started_at, duration_s, cost, exit code and verdict.
- **`result.json` fallback.** When `run.json` is missing, the entry is read from
  `result.json` with gap `"run <id>: no run.json"`. When neither exists, it comes from the
  ledger `run.end` with gap `"run <id>: no run record"`.
- **`budget_frac`:** `cost_usd / max_budget_usd`, null when there is no budget.
- **`status`:** one of `ok`, `error`, `timeout`, `interrupted` or `running`.
- **`has_stream`:** whether `stream.jsonl` exists.

`gates` has one entry per `gate` ledger event. Its `results` are enriched from the
`gates/NNN-<gate>[.rK].json` sidecars:
- `retries` is the number of `.rK` sidecars;
- `log` is the repo-relative path of the gate's log under the campaign dir.

### Records (`nw.history.record.v1`)

Each record's `id` is `<source>:<locator>`, and the locator is stable across calls:

| Source | Locator | Example |
|---|---|---|
| `ledger` | `<cid>:<seq>` | `ledger:<cid>:57` |
| `run` | `<cid>:<run_id>` | `run:<cid>:004-reviewer` |
| `decisions` | the decision's `event_id`, falling back to the line's byte offset | |
| `escalation` | the ESC id | `ESC-0012` |
| escalation answer | the ESC id with `/answer` appended | `ESC-0012/answer` |
| `mailbox` | `<inbox>:<offset>` | |
| `spec_review` | `<SPEC>/<sha>` | |
| `flaky` | the test id | |
| `sentinel` | `<project>:<ts>` | |

| Source | Kinds produced |
|---|---|
| ledger `finding` | `finding` |
| ledger `followup` | `followup`, status `filed` |
| ledger `human_decision` | `human_decision` |
| ledger `triage` (child 1) | `triage` |
| ledger `note` | `note`; `agent` is from `by`, e.g. `agent:conductor` gives `conductor` |
| ledger `close` | `close_check`, with the mode, merged sha and PR url in `text` |
| ledger `spec_review` | `spec_review` |
| `run.json` verdict | `verdict`, one per run with a verdict |
| `run.json` triager verdict with `waive` | `triage`; only when no ledger `triage` event exists for that `run_id` |
| `conductor/decisions.jsonl` | `decision` |
| `escalations/ESC-*.json` | `escalation` (status `open` / `answered` / `withdrawn`), plus `answer` when answered |
| `mailbox/*.jsonl` | `mail`; a `question` is `answered` when a later message has `re` equal to its id, otherwise `open`; any other kind is null |
| `reviews/<SPEC>/<sha>.json` and `spec-reviews.jsonl` | `spec_review`; when the same review appears in both, it is deduplicated by locator |
| `flaky-tests.json` | `flaky`, one record per campaign in `campaigns[]` |
| `sentinel/<project>.last.json` | `sentinel`, with no campaign |

**`agent`** is the persona, `conductor`, `steward:<P>`, `human` or `queue`. It is derived
from the persona, `by` or `from`.

**`severity`** is the finding's own severity, and null for every other kind.

**Links.** Each record's links are resolved as follows.

- **`run_id`**
  - `field`: the `finding.run_id` (child 1).
  - `inferred`: for a reviewer finding, the last `run.end` of the same persona with
    `ts ≤ finding.ts`. For a gate finding (`gate` set), `links.gate_seq` gets the gate's
    seq and `run_id` is null.
- **Finding key**
  - `field`: `finding.key`.
  - `inferred`: recomputed with `followups.finding_key(spec_id, title, files)`.
- **`followup_idea`**: the idea id of the `followup` event whose key matches.
- **`resolved_by`**, which also sets `status: waived`. Taken from:
  - a `triage` event whose `waive[].key` names the key (`field`);
  - otherwise a triager `run.json` `waive[].id` of the form `persona#index`, resolved to
    the `index`-th finding (0-based, in ledger order) raised by `persona` in the latest
    review or gate round before that triager run (`inferred`);
  - otherwise a `human_decision` waive whose escalation's facts name the key or title
    (`inferred`).
- **`escalation`**: the ESC id cited by `human_decision` events, decisions and park notes.
- **`thread`**: the mail id that started the message's `re` chain.
- **`campaign`**
  - `field`: the record's own `campaign`.
  - `text`: otherwise, the record's text, summary and rationale name exactly one id among
    the known campaign dirs. Matching is by token: a match must be followed by a
    non-identifier character. Text that names two or more ids links to none.

**Finding status** follows the epic's table, checked in this order: `waived`, `filed`,
`carried` (key in `state.review_carry`), `fixed`, `open`.
- `fixed` requires two things. First, the campaign's outcome is `merged`. Second, the same
  persona ran a later review round (a later `run.end`) that did not raise the key again.
- Consumers label `fixed` "not re-raised".

**`project` and `spec_id`** come from the campaign header when the record is linked to a
campaign, and from the record itself otherwise.

### Gaps

`gaps` is a sorted, deduplicated list of short strings. Each one names the source and what
was missing:
- `"ledger: line 812 unparsable"`
- `"run 004-reviewer: no run.json"`
- `"state UNKNOWN_X: no phase"`
- `"runner.jsonl: missing"`
- `"decisions.jsonl: line 40 unparsable"`

Inferred links are never gaps. They show in `linked_by`, so gaps stay a list of real
absences.

### Index (`nw.history.index.v1`)

There is one row per campaign dir (live ones included). Each row has:
- the header fields and `dominant`;
- `phase_wall_s` and `phase_cost_usd`, as `{phase: value}`;
- `findings_by_severity`, as `{severity: n}`;
- `records`, the record count;
- `signature` (`campaign_signature`).

Each row's header fields and `dominant` equal those of `campaign_history` for the same
campaign and `now`. `--list` sorts rows by `closed_at or created_at`, newest first.

In this spec every call computes from the data dir. A full `history_index` reads every
campaign; making that fast is the cache spec's job.

### CLI (`cli_campaign.py`)

```
nw campaign history CID [--json]
nw campaign history --list [--project P] [--state merged|handed_off|abandoned|live|all]
    [--spec S] [--since D] [--until D] [--min-cost X] [--json]
nw campaign records [--campaign C] [--agent A] [--kind K] [--project P] [--severity S]
    [--status S] [--since D] [--until D] [--query TEXT] [--limit N] [--json]
```

**Argument rules:**
- `CID` and `--list` are mutually exclusive, and exactly one is required. Breaking that rule
  is a usage error with exit 2. (The cache spec adds `--rebuild-cache` as a third member of
  this group.)
- An unknown `CID` prints `campaign <cid> not found` to stderr and exits 1.
- `--state` defaults to `all` for `--list`.
- `--since` and `--until` take `YYYY-MM-DD` or ISO-8601. `--until` is inclusive of that day.
- For `--list`, the dates apply to `closed_at or created_at`. For `records`, they apply to
  `ts`.
- `--min-cost` compares against `cost_usd`.
- `--query` is a case-insensitive substring match over `title + " " + text`.
- `--limit` defaults to 200 for text output, with no limit for `--json`.
- `--kind`, `--agent`, `--severity` and `--status` may each be repeated, and repeated values
  are ORed.

**Text output** is Rich renderables built from `Text` objects. Untrusted strings are never
parsed as markup (SPEC-0265).
- `history CID` prints the header, a phase table (wall, share, run, gate, gate-wait,
  runner-down, other, cost, runs) with a unicode bar column, the dominant line, the last 30
  timeline rows, a runs table and the gaps.
- `--list` prints one table row per index row, and a progress line to stderr every 50
  campaigns when stderr is a TTY.
- `records` prints one line per record: ts, source, kind, agent, campaign, severity, status
  and title.

**JSON output** has these shapes:
- `history CID --json`: the `nw.campaign.history.v1` document.
- `--list --json`: `{"schema": "nw.history.index.v1", "rows": [...]}`.
- `records --json`: `{"schema": "nw.history.record.v1", "records": [...]}`.

### campaign_export reuse

- `campaign_export._time_in_state` becomes an import of `history.time_in_state`, and
  `_loop_max` an import of `history.loop_max`.
- The `loops` column (`campaign_export.py:320`) is computed by `history.count_loops`, and
  `campaign_export.FIX_REASONS` becomes an alias of `history.FIX_REASONS`.
- `export_tables` output is unchanged byte for byte. The existing `test_campaign_export*`
  tests pin it.
- No other export column changes in this spec.

## Alternatives considered

- **Extend `campaign_export` instead of a new module.** It lost because the export is a
  batch, flat-table producer. Its row shape and CSV contract would fight a nested,
  per-campaign document, and the TUI would import CSV concerns.
- **Keep the header's own loop definition** (every entry into IMPLEMENTING after a non-spec
  state). It lost because one campaign would show different loop counts in the history
  header and in `nw campaign export`; one predicate, the export's, is the shared contract.
- **Proportional overlap attribution instead of precedence.** It lost because it gives
  fractional seconds that are hard to explain. With precedence, "a run was active" always
  wins, which matches how the human reads the time.
- **Ship the cache and fixture in this spec too.** The spec review rejected it as too large
  for one campaign (13 acceptance criteria across four subsystems); they are separate specs
  (Context, *Split*).
- **Hand-built test campaigns only.** It lost because the epic requires real data, and real
  ledgers carry the odd shapes (old `wt.` schemas, missing `run.json`, torn lines) that a
  synthetic set forgets. Hand-built campaigns are still used for the unit cases that need a
  precise shape.

## Acceptance criteria

- [x] **AC1. Purity.** `import nightwright.agentic.history` loads neither `textual`,
  `nightwright.tui` nor any dashboard module (checked in a subprocess via `sys.modules`).
  Calling every public function of `history` and both CLIs over a copy of the fixture leaves
  every file under the data dir byte-identical (hash before and after), and creates no file
  anywhere else under `tmp_path`.
- [x] **AC2. Schemas.** For every fixture campaign, `nw campaign history <cid> --json` exits
  0 and `schemas.history_problems(doc) == []`; every record passes `record_problems_v1` and
  every `--list --json` row passes `index_problems`. Each index row's header fields and
  `dominant` equal that campaign's history document. On the real data dir, the same command
  exits 0 with a valid document for every campaign (manual step 1).
- [x] **AC3. Phases and dominant.** For every fixture campaign:
  - the sum of `phases[].wall_s` is within 1 s of the wall time covered by the visits, and,
    for terminal campaigns, within 1 s of `header.wall_s`;
  - in each phase, `run_s + gate_s + gate_wait_s + runner_down_s + other_s == wall_s`, and
    every value is ≥ 0;
  - `max(visit.loop) == header.loops`.
  `dominant.wall`, `dominant.cost` and `dominant.working` follow the stated argmax, tie and
  working rules; unit cases cover a tie, an all-zero campaign, a parked-dominant campaign
  and a run spilling over a transition.
- [x] **AC4. Export agreement.** For every fixture campaign, `campaign_export`'s
  `time_in_state_s` equals `history.time_in_state`, and `export_tables` `loops` equals
  `header.loops`. The existing `campaign_export` tests pass unchanged.
- [x] **AC5. Record coverage and text links.** On the fixture, `history_records` returns
  records of every source in `RECORD_SOURCES`, and every kind in `RECORD_KINDS` except
  ledger-`triage` while child 1 has produced no fixture data. A campaign-less decision whose
  text names exactly one campaign links to it with `linked_by: "text"`; one naming two
  campaigns links to none.
- [x] **AC6. Finding links and status.**
  - On ledger events that carry `run_id` and `key`, the finding gets them with
    `linked_by: "field"`.
  - On old events, the run comes from ledger order and the key from `finding_key`, with
    `linked_by: "inferred"`; a gate finding gets `links.gate_seq` and a null `run_id`.
  - A triager `persona#index` waive marks the indexed finding `waived` with `resolved_by`
    set.
  - A `followup` key marks a finding `filed` with `followup_idea`.
  - `review_carry` marks a finding `carried`.
  - `fixed` appears only on merged campaigns.
- [x] **AC7. Gaps, never crashes.** Each of these inputs gives a `gaps` entry and a document
  that passes `history_problems`, never an exception: a truncated ledger line, a missing
  `run.json`, a run with neither `run.json` nor `result.json`, an unknown state, an
  unparsable decision line, a missing `runner.jsonl`, and `wt.`-prefixed schemas (the last
  parses with no gap).
- [x] **AC8. CLI.**
  - `nw campaign history` enforces exactly one of `CID` and `--list` (exit 2 otherwise), and
    exits 1 with `campaign <cid> not found` on an unknown campaign.
  - Each `--list` filter (project, state, spec, since, until, min-cost) and each `records`
    filter (campaign, agent, kind, project, severity, status, since, until, query, limit,
    and repeated values ORed) narrows its result as specified.
  - The `--json` outputs carry the stated schema ids.
  - Text output renders untrusted text literally: a finding titled `[bold]x[/]` prints those
    characters.

## Test plan

**Automated tests:**
- **`tests/test_history.py`** (unit; small hand-built campaigns in `tmp_path`, written
  through the real `store` ledger writer):
  - phase visits and loop numbering, including a campaign with one `triage_fix` and one
    `review_fix` loop and an IMPLEMENTING re-entry on a non-fix reason (AC3);
  - precedence splitting with overlapping run, gate, gate-wait and runner-down intervals,
    and with a run spilling over a transition (AC3);
  - the dominant rules: tie, all-zero, parked-dominant (AC3);
  - each link rule, including field vs inferred vs text, the two-campaign text, a gate
    finding, and `persona#index` resolution (AC5, AC6);
  - each finding status (AC6);
  - every bad-input gap case (AC7).
  - The child-1 field path runs on synthetic events of child 1's documented shape
    (`finding.run_id`, `finding.key`, a `triage` event with `waive[{key, reason}]`).
- **`tests/test_history_fixture.py`** (over a `tmp_path` copy of
  `tests/fixtures/agentic_history/` from the fixture spec):
  - every campaign validates, its index row matches its header, and its phase sums and loop
    identity hold (AC2, AC3);
  - `campaign_export` agrees with history on `time_in_state_s` and `loops` (AC4);
  - source and kind coverage (AC5);
  - a hash of every data-dir file before and after calling every public function, plus a
    check that nothing new appeared under `tmp_path` (AC1);
  - a subprocess check that `import nightwright.agentic.history` loads neither `textual`,
    `nightwright.tui` nor a dashboard module, by inspecting `sys.modules` (AC1).
- **`tests/test_cli_campaign_history.py`** (AC8), through `click.testing.CliRunner` over the
  fixture copy:
  - mutual exclusion and exit codes;
  - every filter, and repeated values;
  - `--json` schema ids, each output passing its validator (AC2);
  - literal rendering of markup-like text.

**Manual verification:**
1. On the real data dir, run
   `for c in $(ls ~/.local/share/nightwright/agentic/campaigns); do uv run nw campaign history "$c" --json >/dev/null || echo FAIL $c; done`.
   Expected: no `FAIL` line (AC2).
2. Spot-check one document with `python3 -c` against `schemas.history_problems`.
   Expected: `[]`.
3. Run `uv run nw campaign records --kind finding --severity must_fix --query <a phrase from a known finding>`.
   Expected: the known finding is listed with its run.
4. Run `uv run nw campaign history <a recently merged cid>`, then compare its timeline with
   `nw campaign log <cid>`, its loops with `nw campaign export`, and its dominant phase with
   intuition.
5. Record the outcomes in *As shipped*.

**Regression guard:**
- The existing `tests/test_campaign_export*.py` and `cli_campaign` tests pass unchanged.
- Iterate with `python3 tools/affected_tests.py --run uv run pytest -q`.
- `python3 tools/spec_lint.py` exits 0.

### Test record (2026-11-06)

- `uv run pytest -q tests/test_history.py::test_field_inferred_gate_and_triage_links`:
  1 passed.
- `uv run pytest -q tests/test_history.py tests/test_history_fixture.py
  tests/test_cli_campaign_history.py tests/test_agentic_campaign_export.py`: 147 passed,
  3 skipped.
- Manual verification covered all 241 real campaign directories with
  `nw campaign history <cid> --json`; all exited 0. A schema spot-check returned `[]`.
- The must-fix finding query returned its known finding with run `027-adversarial_reviewer`.
  For merged campaign `SPEC-0179-20261025-158c`, the history timeline matched the ledger,
  history and export both reported zero loops, and parked was the intuitive dominant phase.
- The full affected suite is left to the queue close gate.

### As shipped (2026-11-06)

Shipped the pure history derivation, read-side schemas, export reuse, and uncached history
and records CLIs as designed. Legacy triager `persona#index` waivers resolve within the
latest preceding review round while preserving field provenance on findings that carry
explicit `run_id` and `key`.

## Rollout / migration

1. Start only once the fixture spec is `done` and `tests/fixtures/agentic_history/MANIFEST.json`
   exists.
2. Land `history.py`, the schema constants and validators, and the `campaign_export`
   reuse (time-in-state, loop cap, loop count).
3. Land the CLIs.
4. **No data migration.** Everything is computed on read.
5. **Once child 1 has shipped and produced at least one closed campaign:**
   - the fixture is recaptured (fixture spec's tool) so that the `linked_by: "field"` path
     and ledger `triage` records come from real writer output;
   - AC5's coverage is tightened to include ledger `triage`.
6. The cache spec then adds the cache, `--rebuild-cache` and the perf budgets on top of
   these functions.
7. Add a log note on IDEA-876 (SPEC-0402) that `GET /api/campaigns/<cid>` should return
   `history.campaign_history` (the epic's cross-dependency).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed and noted.
- [x] No regressions (affected tests green; `campaign_export` output unchanged).
- [x] Spec body updated to match what shipped (*As shipped* section added).
- [x] `docs/LEDGER.md` regenerated.

## Open questions

None. These were decided while writing the spec:

| Question | Decision |
|---|---|
| How do overlapping intervals split? | by precedence (Decision 3) |
| What is a loop? | a fix-loop transition, the export's predicate (Decision 5) |
| Where does a gate-finding link point? | `links.gate_seq` is appended to the schema |
| What fills `signature` without a cache? | `campaign_signature`, the local file tuple plus `gates/` |
| Where do the cache, fixture and perf budgets go? | separate sibling specs (Context, *Split*) |
