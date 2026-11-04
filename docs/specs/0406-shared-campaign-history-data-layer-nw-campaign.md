---
id: SPEC-0406
title: "Shared campaign history data layer: nw.campaign.history.v1, nw campaign history and records CLIs, cache and real-data fixture"
status: proposed
owner: user
created: 2026-11-03
source_idea: IDEA-884
kind: feature
parent: SPEC-0405
tags: [agentic, observability, history]
---

## Context

This is child 2 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md).
The epic's queue view, detail popup, History tab and Findings tab all read one UI-independent
layer, and SPEC-0400's per-campaign drilldown should read it too. This spec builds that
layer. The epic already fixes a lot of it, and this spec follows it as written:
- the data inventory and the retention verdict;
- the phase table;
- the three frozen v1 schemas;
- the CLI surface;
- the cache location and invalidation;
- the perf budgets.

Below, "the epic" means SPEC-0405, and "child 1" means the ledger-provenance sibling (the
first breakdown item: `finding.run_id` and `finding.key`, `triage` events, and `campaign`
stamps on decisions and mail).

**What exists today:**
- `campaign_export._time_in_state` (`src/nightwright/agentic/campaign_export.py:159`)
  derives state → seconds from `transition` events. It credits each closed interval to the
  transition's `from`, and the open interval of a non-terminal campaign runs to `now`. It is
  the only time-in-state code, and it is private to the export.
- `nw campaign log/runs/run/tail/show/export` (`cli_campaign.py`) each read one source. None
  joins the ledger with `run.json`, the gates, `decisions.jsonl`, escalations, the mailbox or
  spec reviews.
- `followups.finding_key(spec_id, title, files)` (`followups.py:43`) is the follow-up key.
- `schemas.py` holds the record schemas the queue writes (`nw.ledger.event.v1`,
  `nw.run.v1`, `nw.gate.v1`, `nw.runner.record.v1`) and has no read-side schemas.
- `cfg["cache_dir"]` (`config.py:120`) exists and is used only by `meetings.py`.
- `tests/fixtures/` has no real campaign data. Every agentic test builds a campaign by hand.

**Dependency on child 1.** Child 1 adds the provenance fields. It has no spec id yet, so it
cannot be named in `depends_on`. This layer does not need those fields to work:
- When a field is present, it is read and the link is marked `linked_by: "field"`.
- When it is absent, the link is inferred (`"inferred"`) or matched in text (`"text"`).
- The implementer lands this spec after child 1 when child 1 is already `done`. If child 1
  is not done yet, the field path is tested on synthetic events of child 1's documented shape
  (see the Test plan), and the fixture is recaptured once child 1 has produced real campaigns
  (Rollout).

## Goals / Non-goals

**Goals**
- **One pure module, `src/nightwright/agentic/history.py`.**
  - It never writes the data dir.
  - It never imports `nightwright.tui`, Textual or a dashboard package.
  - It produces `nw.campaign.history.v1`, `nw.history.index.v1` and `nw.history.record.v1`
    exactly as the epic froze them.
- **Phases.** Per-phase visits, with wall, run, gate, gate-wait, runner-down and other time,
  plus cost and run counts. The dominant phase.
- **One merged timeline**, plus runs, gates, records, links with their provenance, and gaps.
- **A disposable cache** under `cache_dir/history/v1` that makes closed campaigns
  compute-once and makes global logs incremental.
- **CLIs**: `nw campaign history CID | --list | --rebuild-cache` and `nw campaign records`,
  each with filters and `--json`.
- **One time-in-state function.** `campaign_export`'s time-in-state reuses history's
  function, so the two cannot drift.
- **A committed fixture.** A sanitized fixture of real closed campaigns, a tool that
  reproduces it, and a scale-clone helper that child 3 and the perf tests reuse.
- **The epic's data-layer perf budgets**, asserted on a 500-campaign clone.

**Non-goals**
- Any TUI or web view. Children 3 to 7 and SPEC-0400 build those.
- Writing new ledger, decision or mail fields. That is child 1.
- Editing, deleting or migrating history, or any change to retention.
- Reading the pruned sources (`gatejobs/`, `remote-jobs/`, worker caches).
- Cross-host aggregation.
- The live-queue repaint budget. That belongs to child 4.

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
4. **The cache splits in two.**
   - *Campaign-local* derivations (from the ledger, `state.json`, `runs/` and `gates/`) are
     cached per campaign, keyed by that campaign's file signature.
   - *Global-log* records (decisions, mailbox, escalations, spec reviews, flaky tests,
     sentinel, runner intervals) are parsed incrementally into a global cache by offset.
   - The two are merged at read time, so a new decision on a closed campaign shows up
     without recomputing its ledger.
   - The cache lives only under `cache_dir`. It is always safe to delete, and a cache entry
     that is corrupt, of the wrong version or of an unknown builder is rebuilt, never trusted.
5. **`time_in_state` moves into `history.py`.** It is moved there unchanged in behaviour.
   `campaign_export` imports it, and the phase visits are built from the same transition
   walk.
6. **The fixture is real data, sanitized by a committed tool.**
   - `tools/capture_history_fixture.py` copies the selected real closed campaigns plus the
     matching slices of the global logs into `tests/fixtures/agentic_history/`.
   - The tool sanitizes the copy and writes a `MANIFEST.json`.
   - The real data dir has no HANDED_OFF campaign (none at survey: 191 merged, 14 abandoned,
     12 live). The tool therefore derives the handed-off fixture campaign from a real merged
     one by rewriting only its close, and it marks that campaign `derived` in the manifest.

## Design

### Module layout

`src/nightwright/agentic/history.py` (pure functions; no write outside `cache_dir`):

| Function | Returns |
|---|---|
| `PHASES` | the ordered phase → states table from the epic; `PHASE_OF[state]` is its inverse; an unknown state maps to `other` and adds a gap |
| `time_in_state(state, events, start, end) -> dict[str, int]` | moved verbatim from `campaign_export._time_in_state` |
| `phase_visits(state, events, start, end) -> list[Visit]` | `Visit = (phase, state, start, end, loop)`, from the same walk |
| `campaign_history(cfg, cid, *, now=None, cache=True) -> dict` | `nw.campaign.history.v1` |
| `history_index(cfg, *, now=None, cache=True, progress=None) -> list[dict]` | `nw.history.index.v1` rows, one per campaign dir |
| `history_records(cfg, *, campaign=None, agent=None, kind=None, project=None, severity=None, status=None, since=None, until=None, query=None, limit=None, cache=True) -> list[dict]` | `nw.history.record.v1`, newest first |
| `filter_index(rows, *, project, state, spec, since, until, min_cost) -> list[dict]` | the `--list` filters, pure over rows |
| `rebuild_cache(cfg, *, progress=None) -> dict` | drops `cache_dir/history/v1` and rebuilds it; returns counts and elapsed time |
| `campaign_signature(cfg, cid) -> str` | the cache key below, hex |

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
| `loops` | the number of transitions into IMPLEMENTING from a non-spec state, plus one if IMPLEMENTING was ever entered |
| `loop_max` | `campaign_export._loop_max`, moved into `history` and imported back |
| `runs` | the count of `run.end` events, plus `run.start` events with no matching end (counted as interrupted) |
| `cost_usd` | `state.cost.total_usd`, falling back to the sum of `run.end` `charged_usd` |
| `conductor_usd` | `conductor_cost` attribution, exactly as `campaign_export` computes it (shared helper, null when unavailable) |
| `followup_idea` | the id on the last `followup` event |
| `tone` | the same tone function that `nw queue watch` / `journey.py` use for the campaign's state |

### Phases and dominant

- **Visits.** `phase_visits` walks the transitions exactly as `time_in_state` does. Each
  closed interval is credited to the transition's `from`, and the open interval of a live
  campaign runs to `now`. A terminal state gets no visit.
- **Loop numbers.** `loop` is 0 before the first IMPLEMENTING, and goes up by 1 on every
  entry into IMPLEMENTING.
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

Each record's `id` is `<source>:<locator>`, and the locator is stable across rebuilds:

| Source | Locator | Example |
|---|---|---|
| `ledger` | `<cid>:<seq>` | |
| `run` | `<cid>:<run_id>` | |
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

Inferred links are never gaps. They show in `linked_by`, so gaps stay a list of real
absences.

### Index (`nw.history.index.v1`)

There is one row per campaign dir (live ones included). Each row has:
- the header fields and `dominant`;
- `phase_wall_s` and `phase_cost_usd`, as `{phase: value}`;
- `findings_by_severity`, as `{severity: n}`;
- `records`, the record count;
- `signature`.

`--list` sorts rows by `closed_at or created_at`, newest first.

### Cache (`<cache_dir>/history/v1/`)

**Files:**
- `campaigns/<cid>.json`: `{builder, signature, local}`. `local` is everything derived from
  campaign-local files: header (without `conductor_usd`), visits, run and gate rows, ledger
  and run records, ledger gaps.
- `globals.json`, plus `globals/<source>.jsonl`: parsed global-log records, already linked
  to a campaign, with a lowercased `_q` search field.
- `sources.json`: `{path: {size, mtime_ns, inode, offset}}` for each global log, plus the
  runner intervals parsed so far.
- `index.jsonl`: one index row per campaign, each with its combined signature.
- `records.jsonl`: every record, with `_q`, sorted newest first.

**Keys and versions:**
- `builder` is `history.BUILDER_VERSION`, an int the implementer bumps whenever derivation
  logic changes. A file with a different builder is ignored and rewritten.
- A campaign's local signature is `sha1(ledger size, ledger mtime_ns, state.json mtime_ns,
  runs/ mtime_ns, gates/ mtime_ns)`. It adds `gates/` to the epic's tuple, because gate
  sidecars feed `gates[].results`.
- The combined history `signature` is `sha1(local signature, the number of global records
  for that cid, runner.jsonl offset)`. `runner.jsonl` matters only to campaigns whose window
  is still open: for a terminal campaign whose `closed_at` falls before the last parsed
  runner record, runner intervals cannot change, and its offset is fixed in its signature at
  that value.

**Incremental reads.** On each read, every global log is `stat`ed.
- If `size >= offset` and the inode is unchanged, only the bytes from `offset` are parsed,
  and a torn final line (no newline) is left for the next read.
- If the file shrank or the inode changed, it is re-read in full.
- `escalations/` and `reviews/` are directories of immutable or rarely-changed JSON files.
  They are keyed per file by `(mtime_ns, size)`.

**Writes.** Every write goes to `<file>.tmp.<pid>` and is then renamed with `os.replace`,
so concurrent readers see the old file or the new one, never a torn one.

**Bad cache files.** A JSON error, a wrong schema or a wrong builder means "rebuild this
file". A cache error never fails the call. If `cache_dir` is not writable, the result is
computed uncached and gets the gap `"cache: not writable"`.

**`cache=False`** bypasses the cache entirely. Tests use it to compare cached results with
uncached ones.

### CLI (`cli_campaign.py`)

```
nw campaign history CID [--json]
nw campaign history --list [--project P] [--state merged|handed_off|abandoned|live|all]
    [--spec S] [--since D] [--until D] [--min-cost X] [--json]
nw campaign history --rebuild-cache
nw campaign records [--campaign C] [--agent A] [--kind K] [--project P] [--severity S]
    [--status S] [--since D] [--until D] [--query TEXT] [--limit N] [--json]
```

**Argument rules:**
- `CID`, `--list` and `--rebuild-cache` are mutually exclusive, and exactly one is required.
  Breaking that rule is a usage error with exit 2.
- An unknown `CID` prints `campaign <cid> not found` to stderr and exits 1.
- `--state` defaults to `all` for `--list`.
- `--since` and `--until` take `YYYY-MM-DD` or ISO-8601. `--until` is inclusive of that day.
- For `--list`, the dates apply to `closed_at or created_at`. For `records`, they apply to
  `ts`.
- `--min-cost` compares against `cost_usd`.
- `--query` is a case-insensitive substring match over `title + text`, using `_q`.
- `--limit` defaults to 200 for text output, with no limit for `--json`.
- `--kind`, `--agent`, `--severity` and `--status` may each be repeated, and repeated values
  are ORed.

**Text output** is Rich renderables built from `Text` objects. Untrusted strings are never
parsed as markup (SPEC-0265).
- `history CID` prints the header, a phase table (wall, share, run, gate, gate-wait,
  runner-down, other, cost, runs) with a unicode bar column, the dominant line, the last 30
  timeline rows, a runs table and the gaps.
- `--list` prints one table row per index row.
- `records` prints one line per record: ts, source, kind, agent, campaign, severity, status
  and title.
- `--rebuild-cache` prints a progress line per 50 campaigns to stderr, then the totals.

**JSON output** has these shapes:
- `history CID --json`: the `nw.campaign.history.v1` document.
- `--list --json`: `{"schema": "nw.history.index.v1", "rows": [...]}`.
- `records --json`: `{"schema": "nw.history.record.v1", "records": [...]}`.

### campaign_export reuse

- `campaign_export._time_in_state` becomes an import of `history.time_in_state`, and so
  does `_loop_max`.
- `export_tables` output is unchanged byte for byte. The existing `test_campaign_export*`
  tests pin it.
- No other export column changes in this spec.

### Fixture capture (`tools/capture_history_fixture.py`)

```
python3 tools/capture_history_fixture.py [--data-dir D] [--out tests/fixtures/agentic_history]
    [--campaign CID ...] [--auto] [--check]
```

**Selection.** `--auto` picks the most recent real campaign that matches each required
role. `--campaign` pins explicit ids. The required roles are:
- merged with ≥ 2 loops and ≥ 2 review rounds;
- abandoned;
- answered human escalation;
- pre-SPEC-0362 (no `stream.jsonl` anywhere);
- a run with only `result.json`;
- flaky tests and a triager waive;
- live (non-terminal) at capture time;
- handed-off, derived as described below.

**What it copies:**
- Each selected `campaigns/<cid>/` dir, without `tmp/`, `prompt.md` and `request.json`.
- `stream.jsonl` cut to its last 200 lines, and gate logs cut to their last 200 lines.
- The global-log lines and files that link to a selected campaign, by field or by text:
  - `conductor/decisions.jsonl`;
  - `mailbox/*.jsonl`;
  - `escalations/`;
  - `reviews/` and `spec-reviews*` for their specs;
  - the `flaky-tests.json` entries;
  - `events.jsonl` lines.
- The `runner.jsonl` window that covers their lifetimes.
- One `sentinel/<project>.last.json`.

**Sanitizing:**
- `launch.json` `env` is reduced to an allowlist of `NW_*`/`WT_*` keys. Any value that
  matches a secret pattern (`sk-`, `ghp_`, `gho_`, `xox`, `AKIA`, `-----BEGIN`, `token=`,
  `Authorization`) becomes `"<redacted>"` in every file.
- The home dir and user name become `/home/user` and `user`.
- Email addresses become `user@example.com`.

**Handed-off campaign.** One merged campaign is copied a second time under a new id (suffix
`-ho00`). Only its `close` event mode, `state.json` state, `close` and `merged_sha` change.
`MANIFEST.json` lists it as `derived: "handed_off from <cid>"`.

**`MANIFEST.json`** records the capture date, the tool's git sha, each campaign with its
role, the sanitizing rules and the total size.

**Size.** The committed fixture must stay ≤ 8 MiB. The tool fails, rather than writing, if
the output would exceed that.

**`--check`** re-scans an existing fixture for secret patterns and absolute home paths, and
exits 1 if it finds any. A test runs it.

### Scale clone (`tests/history_scale.py`)

`clone_fixture(src, dst, n=500) -> Path` copies the fixture campaigns round-robin into `n`
campaign dirs with rewritten ids:
- ids are suffixed `-cNNN`, and `campaign` fields in the ledger and global logs are rewritten
  to match;
- ledger `ts` values are shifted by a day per copy, so dates spread out;
- global-log lines are replicated per copy until there are about 50 000 records in total.

It uses hard links for files that need no rewriting (runs and gates), so the clone is cheap.
Child 3 reuses this helper.

### Perf budgets

These are the epic's data-layer budgets. Tests assert them with ×3 slack:

| Operation | Budget |
|---|---|
| cold `history_index` on 500 campaigns | ≤ 10 s |
| warm `history_index` | ≤ 300 ms |
| warm `campaign_history` of one campaign | ≤ 500 ms |
| `history_records` filter or `query` over about 50 000 cached records | ≤ 200 ms |

The live-queue repaint budget belongs to child 4. The perf tests run in the default suite,
with the 500-campaign clone built once per module.

## Alternatives considered

- **Extend `campaign_export` instead of a new module.** It lost because the export is a
  batch, flat-table producer. Its row shape and CSV contract would fight a nested,
  per-campaign, cached document, and the TUI would import CSV concerns.
- **Keep the cache in the data dir.** It lost because it would break SPEC-0400's
  byte-identical data-dir invariant and AC10 of the epic.
- **One cache file per campaign that includes its global-log records.** It lost because every
  new decision or mail line would invalidate a closed campaign's whole cached history. The
  split local/global cache recomputes nothing for appends.
- **Proportional overlap attribution instead of precedence.** It lost because it gives
  fractional seconds that are hard to explain. With precedence, "a run was active" always
  wins, which matches how the human reads the time.
- **A synthetic fixture.** It lost because the epic requires real data, and real ledgers
  carry the odd shapes (old `wt.` schemas, missing `run.json`, torn lines) that a synthetic
  fixture forgets. Only the handed-off role is derived, because no real one exists.

## Acceptance criteria

- [ ] **AC1.** `history.py` imports neither `nightwright.tui`, `textual` nor any dashboard
  module, and calling any of its public functions leaves the data dir byte-identical. A test
  hashes every file before and after.
- [ ] **AC2.** For every fixture campaign, `nw campaign history <cid> --json` exits 0 and
  `schemas.history_problems(doc) == []`. Every record passes `record_problems_v1` and every
  `--list --json` row passes `index_problems`.
- [ ] **AC3.** For every fixture campaign:
  - the sum of `phases[].wall_s` is within 1 s of the wall time covered by the visits;
  - for terminal campaigns, it is within 1 s of `header.wall_s`;
  - in each phase, `run_s + gate_s + gate_wait_s + runner_down_s + other_s == wall_s`, and
    every value is ≥ 0.
- [ ] **AC4.** `campaign_export`'s `time_in_state_s` equals `history.time_in_state` for every
  fixture campaign, and the existing `campaign_export` tests pass unchanged.
- [ ] **AC5.** `dominant.wall`, `dominant.cost` and `dominant.working` follow the stated
  argmax, tie and working rules. Unit cases cover a tie, an all-zero campaign and a
  parked-dominant campaign.
- [ ] **AC6.** On the fixture, the history contains records of every source in
  `RECORD_SOURCES`, and every kind except `triage`-from-ledger when child 1 has not yet
  produced fixture data. A campaign-less decision whose text names exactly one campaign
  links to it with `linked_by: "text"`, and one naming two campaigns links to none.
- [ ] **AC7.** Finding links and status:
  - On ledger events that carry `run_id` and `key`, the finding gets them with
    `linked_by: "field"`.
  - On old events, the run comes from ledger order and the key from `finding_key`, with
    `linked_by: "inferred"`.
  - A triager `persona#index` waive marks the indexed finding `waived` with `resolved_by`
    set.
  - A `followup` key marks a finding `filed` with `followup_idea`.
  - `review_carry` marks a finding `carried`.
  - `fixed` appears only on merged campaigns.
- [ ] **AC8.** Bad or partial inputs give a `gaps` entry and a valid document, never an
  exception. The inputs covered are: a truncated ledger line, a missing `run.json`, a run
  with neither `run.json` nor `result.json`, an unknown state, an unparsable decision line,
  a missing `runner.jsonl` and `wt.`-prefixed schemas.
- [ ] **AC9.** Cache behaviour:
  - A second `campaign_history` of a closed campaign reads no file under its campaign dir
    except the `stat` calls for its signature.
  - Appending a decision line for that campaign shows the new record on the next call
    without recomputing its local entry.
  - A truncated global log is re-read in full.
  - A corrupt or wrong-builder cache file is rebuilt.
  - The output with `cache=True` equals the output with `cache=False`, except for
    `generated_at`.
- [ ] **AC10.** CLI behaviour:
  - `nw campaign history` enforces exactly one of `CID`, `--list` and `--rebuild-cache`
    (exit 2 otherwise), and exits 1 on an unknown campaign.
  - Each `--list` filter (project, state, spec, since, until, min-cost) and each `records`
    filter (campaign, agent, kind, project, severity, status, since, until, query, limit)
    narrows its result as specified.
  - Text output renders untrusted text literally: a finding titled `[bold]x[/]` prints those
    characters.
- [ ] **AC11.** Fixture and capture tool:
  - `tests/fixtures/agentic_history/` holds real captured campaigns covering every role
    listed under *Fixture capture*, plus a `MANIFEST.json`.
  - It is ≤ 8 MiB.
  - `tools/capture_history_fixture.py --check` exits 0 on it and exits 1 when a planted
    `ghp_` token or a `/home/<name>` path is added.
- [ ] **AC12.** On the 500-campaign clone, the four budgets in *Perf budgets* hold with ×3
  slack.
- [ ] **AC13.** `nw campaign history <cid> --json` exits 0 with a valid document for every
  campaign in the real data dir (manual step).

## Test plan

**Automated tests:**
- **`tests/test_history.py`** (unit; small hand-built campaigns in `tmp_path`, written
  through the real `store` ledger writer):
  - phase visits and loop numbering;
  - precedence splitting with overlapping run, gate, gate-wait and runner-down intervals,
    and with a run spilling over a transition (AC3);
  - the dominant rules (AC5);
  - each link rule, including field vs inferred vs text, the two-campaign text, and
    `persona#index` resolution (AC6, AC7);
  - each finding status (AC7);
  - every bad-input gap case (AC8).
  - The child-1 field path runs on events of child 1's documented shape.
- **`tests/test_history_fixture.py`** (over a copy of `tests/fixtures/agentic_history/`):
  - every campaign validates and its phase sums hold (AC2, AC3);
  - `campaign_export` agrees with history (AC4);
  - source and kind coverage (AC6);
  - the index rows equal the per-campaign headers;
  - a byte-identical data-dir hash check (AC1);
  - a subprocess check that `import nightwright.agentic.history` loads neither `textual` nor
    `nightwright.tui`, by inspecting `sys.modules` (AC1).
- **`tests/test_history_cache.py`** (AC9):
  - `open`/`os.stat` are patched to count calls on a warm read;
  - an appended decision line;
  - a truncated `mailbox/*.jsonl` and an inode change;
  - a corrupt `campaigns/<cid>.json`;
  - a bumped `BUILDER_VERSION`;
  - an unwritable `cache_dir`;
  - cached and uncached results compared.
- **`tests/test_cli_campaign_history.py`** (AC10), through `click.testing.CliRunner` over the
  fixture copy:
  - mutual exclusion and exit codes;
  - every filter;
  - `--json` schema ids;
  - literal rendering of markup-like text;
  - the `--rebuild-cache` totals.
- **`tests/test_capture_history_fixture.py`** (AC11):
  - the tool's `--auto` selection on a small synthetic data dir;
  - redaction of each secret pattern, the home path and emails;
  - the 8 MiB refusal;
  - `--check` on the committed fixture (exit 0) and on a planted secret (exit 1).
- **`tests/test_history_perf.py`** (AC12): it uses `tests/history_scale.clone_fixture` to
  build 500 campaigns and about 50 000 records once per module, then times a cold index, a
  warm index, a warm `campaign_history` and a `records` query against the budgets × 3.

**Manual verification:**
1. On the real data dir, run
   `for c in $(ls ~/.local/share/nightwright/agentic/campaigns); do uv run nw campaign history "$c" --json >/dev/null || echo FAIL $c; done`.
   Expected: no `FAIL` line (AC13).
2. Spot-check one validated document with `python3 -c` against `schemas.history_problems`.
3. Run `uv run nw campaign history --rebuild-cache`, then `time uv run nw campaign history --list --state merged`.
   Expected: the warm list returns well under a second.
4. Run `uv run nw campaign records --kind finding --severity must_fix --query <a phrase from a known finding>`.
   Expected: the known finding is listed with its run.
5. Run `uv run nw campaign history <a recently merged cid>`, then compare its timeline with
   `nw campaign log <cid>`, and its dominant phase with intuition.
6. Record the outcomes in *As shipped*.

**Regression guard:**
- The existing `tests/test_campaign_export*.py` and `cli_campaign` tests pass unchanged.
- Iterate with `python3 tools/affected_tests.py --run uv run pytest -q`.
- `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Land `history.py`, the schema constants and validators, and the `campaign_export`
   reuse.
2. Land the cache, then the CLIs.
3. Capture the fixture with `--auto` from the real data dir. Review `MANIFEST.json` and
   `--check`, then commit it.
4. **No data migration.** The cache builds lazily on first use, and the first
   `nw campaign history --list` shows progress.
5. **Once child 1 has shipped and produced at least one closed campaign:**
   - recapture or extend the fixture so that the `linked_by: "field"` path and ledger
     `triage` records come from real writer output;
   - tighten AC6's coverage to include ledger `triage`.
6. Add a log note on IDEA-876 (SPEC-0402) that `GET /api/campaigns/<cid>` should return
   `history.campaign_history` (the epic's cross-dependency).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass; manual steps performed and noted.
- [ ] No regressions (affected tests green; `campaign_export` output unchanged).
- [ ] Spec body updated to match what shipped (*As shipped* section added).
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

None. These were decided while writing the spec:

| Question | Decision |
|---|---|
| How do overlapping intervals split? | by precedence (Decision 3) |
| Which tuple keys a campaign's cache? | the epic's tuple plus `gates/` |
| How is the handed-off fixture campaign obtained? | derived from a merged one (Decision 6) |
| Where does a gate-finding link point? | `links.gate_seq` is appended to the schema |
| How large may the fixture be? | 8 MiB at most |
