---
id: SPEC-0236
title: "`wt campaign export`: a flat campaign dataset for mis-sizing analysis"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, campaign, export, analysis, sizing]
source_idea: IDEA-092
depends_on: [SPEC-0219, SPEC-0199, SPEC-0192]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from `IDEA-092` ("Export campaign data for after-the-fact analysis: find patterns showing a
problem was sized wrong").

[SPEC-0219](./0219-spec-sizing-and-tone-pick-the-campaign-profile.md) makes the spec reviewer
guess each spec's `size` and `tone` and picks the profile from that guess. It records the guess
in `state.sizing` and in the `open` event, and names `IDEA-092` as the consumer that measures
whether the guess was right. Everything needed is already on disk, but spread out:
- `campaigns/<id>/state.json`: counters, cost, sizing, last review, close;
- `ledger.jsonl` (`wt.ledger.event.v1`, [SPEC-0192](./0192-schema-d-agentic-logs-validated-ledger-run-json.md)):
  transitions, runs, gates, parks, findings, notes;
- `runs/<NNN-persona>/run.json`, joined per run by `runs.campaign_runs`
  ([SPEC-0199](./0199-wt-campaign-runs-and-run.md));
- `spec_at_open.md`: the accepted spec text, saved at open time ([SPEC-0186](./0186-spec-change-guard-at-close-no-merge-when-the.md)).

Today there is no way to look at all of it across campaigns without one-off scripts.

## Goals / Non-goals

**Goals**
- G1. `wt campaign export` writes three flat tables: campaigns (one row per campaign), runs (one
  row per persona run) and findings (one row per `finding` event). The schema is fixed,
  `wt.campaign.export.v1`, with typed columns. Formats: JSONL (default) and CSV, and Parquet
  when `pyarrow` is importable.
- G2. The campaign row carries the sizing guess (size, tone, profile, source) next to what
  happened: loops against `loop_max`, cost against the cap, runs and cost per persona, time
  per state, gate failures, parks and their reasons, review score, must-fix and waived counts,
  and the outcome.
- G3. Each campaign row has a `signals` column listing named mis-sizing signals, computed by
  documented rules, so a query can filter on them without re-deriving them.

**Non-goals**
- Analysis, charts or a notebook. The dataset is the product.
- The runner journal (`runner.jsonl`). Its records are per tick and cross-campaign, and
  everything campaign-scoped is already in the ledgers.
- A "split after the fact" signal. Nothing links a campaign to a later spec that split it.
  A follow-up can add a column when such a link exists.
- Exporting spec-review records for specs that were never queued.
- Incremental or streaming export. Every run is a full snapshot.

## Decision

A new module `src/wt/agentic/campaign_export.py` builds the three tables from `store`
(`list_campaigns`, `read_ledger`, `campaign_dir`) and `runs.campaign_runs`. Column names and
types are module constants, and every row has exactly those keys in that order (null when
unknown). `wt campaign export` (in `cli_campaign.py`) writes one file per table plus a
`manifest.json` into an export directory. Nested values (lists, maps) stay native in JSONL and
become JSON strings in CSV and Parquet. Parquet is written only when `import pyarrow`
succeeds, otherwise the command fails before writing anything.

## Design

### Schema (`campaign_export.py`)

- `EXPORT_SCHEMA = "wt.campaign.export.v1"`.
- `CAMPAIGN_COLUMNS`, `RUN_COLUMNS`, `FINDING_COLUMNS` are tuples of `(name, type)` with type in
  `str | int | float | bool | json`. Under v1 a new column may only be appended at the end of a
  tuple. Renaming, removing or retyping a column needs `v2` and a superseding spec.

**campaigns**:
- identity and outcome: `campaign` str, `kind` str, `spec_id` str, `project` str, `state` str,
  `outcome` str, `created_at` str, `ended_at` str, `wall_s` int, `active_s` int;
- the sizing guess: `size` str, `tone` str, `profile` str, `profile_source` str,
  `sizing_rationale` str;
- limits and spec shape: `loop_max` int, `cap_usd` float, `cap_raises` int, `models` json,
  `ac_count` int, `spec_lines` int;
- runs, gates and parks: `loops` int, `loop_resets` int, `runs` int, `runs_by_persona` json,
  `gate_runs` int, `gate_failures` int, `gate_retries` int, `triage_retries` int, `parks` int,
  `park_reasons` json;
- cost, time and review: `cost_usd` float, `cost_by_persona` json, `cost_cap_frac` float,
  `time_in_state_s` json, `review_score` int, `must_fix_count` int, `finding_count` int,
  `waived_count` int;
- close and signals: `close_mode` str, `merged_sha` str, `signals` json.

**runs**:
- join keys: `campaign` str, `spec_id` str, `profile` str, `size` str, `tone` str;
- the run: `run_id` str, `persona` str, `model` str, `effort` str, `max_budget_usd` float,
  `timeout_s` int, `started_at` str, `duration_s` float, `cost_usd` float, `charged_usd` float,
  `budget_frac` float;
- the result: `exit_code` int, `is_error` bool, `timed_out` bool, `interrupted` bool,
  `transient` bool, `verdict_status` str, `status` str;
- tokens: `input_tokens` int, `output_tokens` int, `cache_read_tokens` int.

**findings**:
- join keys: `campaign` str, `spec_id` str;
- the event: `seq` int, `ts` str, `state_at` str;
- the finding: `persona` str, `severity` str, `gate` str, `ac` str, `title` str, `detail` str,
  `files` json.

### Derivations (pure functions over `state`, its ledger and its campaign directory)

- `outcome`: `state` when it is in `store.TERMINAL_STATES`, else null. `ended_at`: the `ts` of
  the last `transition` event whose `to` is terminal. `wall_s`: from `created_at` to `ended_at`,
  or to the export time while open.
- `time_in_state_s`, a map state → seconds:
  - the campaign starts in `QUEUED` at the `open` event's `ts`;
  - each `transition` event closes the current interval and credits it to its `from`;
  - the last interval runs to `ended_at` (or now) and is credited to the current state, unless
    that state is terminal.

  `active_s` is the sum over `queue.ACTIVE_STATES`.
- `size`, `tone`, `profile_source`, `sizing_rationale` come from `state.sizing.size / .tone /
  .source / .rationale` (the SPEC-0219 contract). They are null when `sizing` is absent
  (campaigns created before SPEC-0219). `profile` is `state.profile`.
- `loop_max`: `resolve_profile(cfg, state.profile)["loop_max"]` at export time, null on
  ValueError. It reflects the **current** profile config, because the campaign does not store
  its loop limit. The manifest says so in `notes`.
- `cap_usd` is `state.cost.cap_usd`: the **final** cap, because `apply_resume --raise-cap`
  overwrites it. `cap_raises` counts the `resume` events with a non-null `cap_usd`. The manifest
  `notes` say that `cap_usd`, `cost_cap_frac` and `cost_near_cap` use the final cap. The
  opening cap is not recovered: no event records it before the first raise when no run has
  happened yet, and a partial answer would be worse than a stated one.
- `models`: `{persona: model}` from the last `run.start` per persona. `runs_by_persona`: count of
  `run.start` events per persona.
- `ac_count`: the number of lines matching `^\s*[-*] \[[ xX]\]` in the `Acceptance criteria`
  section (`specmeta.sections`) of `campaign_dir/spec_at_open.md`. `spec_lines`: that file's
  line count. Both are null when the file is missing (merge campaigns, campaigns that never
  opened).
- `gate_failures`: `gate` events with `ok: false`. `park_reasons`: the `reason` of every `park`
  event, in order.
- Totals come from the **ledger**, not `state.counters`. `counters.gate_retries` and
  `counters.triage_retries` are per-loop streaks that `_apply` resets after each fix, and
  `apply_resume --reset-loops` zeroes `loops`, `gate_retries` and `triage_retries`. So:
  - `loops`: `transition` events to `IMPLEMENTING` with reason `triage_fix` or `review_fix`
    (the only two fix-loop transitions in `_apply`);
  - `gate_retries`: `transition` events with reason `retry_gate`;
  - `triage_retries`: `run.end` events of persona `gate_triager` with `verdict_ok: false` and
    `interrupted: false`. A triage retry stays in TRIAGING, and `_transition` writes no
    `transition` event when the state does not change, so there is no transition to count.
    `decide_triage` retries exactly on a malformed verdict, so these run ends are the
    retries, plus a final one that escalates;
  - `loop_resets`: `resume` events with `loops_reset: true`.

  `runs`, `gate_runs` and `parks` are monotonic counters that are never reset, so they come
  from `state.counters.runs / .gate_runs / .parks`.
- `cost_usd`: `state.cost.total_usd`. `cost_by_persona`: `state.cost.by_persona`.
  `cost_cap_frac`: `cost_usd / cap_usd` rounded to 3 places, null when `cap_usd` is null or
  0.
- `review_score`: `state.last_review.score`. `must_fix_count`: `finding` events with severity
  `must_fix`. `finding_count`: `finding` events with severity other than `info`.
  `waived_count`: `note` events with `author == "queue"` whose text starts with `waived `
  (written by `_apply_triager`, SPEC-0206).
- `close_mode`, `merged_sha`: `state.close.mode / .merged_sha`.
- Run rows: one per `runs.campaign_runs(cfg, cid)` record, projected onto `RUN_COLUMNS`.
  - `budget_frac` is `charged_usd / max_budget_usd` rounded to 3 places. It is null when
    either operand is null (runs with status `running` or `orphan`) or the budget is 0.
  - The token columns read `usage.input_tokens`, `usage.output_tokens` and
    `usage.cache_read_input_tokens`, null when missing.
- Finding rows: one per `finding` event. `state_at` is the campaign state at the event's `seq`,
  replayed from the transitions. `detail` is truncated to 500 characters.

### Signals (`signals(row) -> list[str]`, pure, in this order)

| signal | rule |
|---|---|
| `loops_exhausted` | `"loop_exhausted"` in `park_reasons` |
| `cap_parked` | `"cost_cap"` in `park_reasons` |
| `cost_near_cap` | `cost_cap_frac >= NEAR_CAP_FRAC` (0.8) |
| `many_fix_loops` | `loops >= MANY_LOOPS` (2) |
| `escalated` | any of `triage_escalated`, `review_escalated`, `implementer_blocked` in `park_reasons` |
| `gate_churn` | `gate_failures >= GATE_CHURN` (3) |
| `long_for_size` | `size` known and `active_s > LONG_FOR_SIZE_S[size]`, with `{xs: 3600, s: 7200, m: 14400, l: 28800}` |
| `abandoned` | `outcome == "ABANDONED"` |

Why these thresholds: they come from the SPEC-0219 rubric and the loop limits of the
built-in profiles. `frugal` allows 2 loops, so a second fix loop already means the work was
harder than "small". `many_fix_loops` is size-agnostic on purpose: a second fix loop is
worth a look at any size, and size-relative variants can be computed from the exported
`loops`, `loop_max` and `size` columns. An `xs` (at most 3 ACs, one module) that keeps a slot for more than an
hour is the idea's "xs that took several hours", with margin for gate time. The constants
are module-level so the data can be used to tune them later. The rules are part of the v1
contract.

### Writing and CLI

- `export_tables(cfg, *, project=None, since=None, terminal_only=False, now=None) -> dict`
  returns `{"campaigns": [...], "runs": [...], "findings": [...]}`. Campaigns come from
  `store.list_campaigns(cfg, include_terminal=True, project=project)`, filtered by
  `since` and by `terminal_only`. `since` is a `datetime.date`: a campaign is kept when
  `datetime.fromisoformat(created_at)`, converted to `cfg["_tz"]`, has a local date `>=
  since`. The boundary day is included, in the user's configured timezone. and sorted by `created_at` ascending
  so the output is deterministic.
- `write_export(tables, out_dir, fmt, *, filters) -> dict` writes `campaigns.<ext>`,
  `runs.<ext>`, `findings.<ext>` and `manifest.json`, and returns the manifest. The manifest
  contains:
  - `schema`, `generated_at`, `wt_version` (`importlib.metadata.version("wt")`), `format`,
    `filters`;
  - `tables: {name: {file, rows, columns: [[name, type], ...]}}`;
  - `notes` (the `loop_max` caveat and the final-cap caveat).

  Files are written with `export._atomic_write`.
  - JSONL: one `json.dumps(row, ensure_ascii=False)` per line.
  - CSV: `csv.DictWriter` with the header in column order. `json` cells are `json.dumps`,
    booleans `true`/`false`, null is the empty string.
  - Parquet: `pyarrow.Table.from_pylist(rows, schema=...)`, built from the column types
    (`str`→`string`, `int`→`int64`, `float`→`float64`, `bool`→`bool_`, `json`→`string` holding
    JSON) and written with `pyarrow.parquet.write_table`.
- `wt campaign export [--format jsonl|csv|parquet] [--out DIR] [--project P] [--since
  YYYY-MM-DD] [--terminal-only] [--table campaigns|runs|findings --stdout] [--json]`:
  - `--out` defaults to `<data_dir>/agentic/exports/<YYYYMMDD-HHMMSS>/`. When that directory
    exists (two exports in the same second), `-2`, `-3`, … is appended. An explicit `--out`
    must not exist or must be empty, otherwise ClickException `--out <dir> is not empty`. The
    command never overwrites an earlier export. It prints the directory and the row counts,
    or the manifest with `--json`.
  - `--stdout` writes the single `--table` to stdout in `jsonl` or `csv` and writes no files.
    Invalid combinations raise ClickException:
    - `--stdout` without `--table`: `--stdout needs --table campaigns|runs|findings`;
    - `--table` without `--stdout`: `--table is only used with --stdout`;
    - `--stdout --format parquet`: `--stdout supports jsonl or csv`;
    - `--stdout` with `--out`: `--out and --stdout are exclusive`.
  - `--format parquet` without an importable `pyarrow` raises ClickException `--format parquet
    needs pyarrow (pip install pyarrow); use jsonl or csv` before creating the directory.
    `pyarrow` is **not** added to the dependencies.

## Alternatives considered

- **One wide table with runs and findings nested in the campaign row**: rejected. pandas and
  DuckDB want flat tables joined on `campaign`, and per-run analysis (cost by model) would
  first have to unnest.
- **SQLite output**: rejected. JSONL and CSV load directly into DuckDB, pandas and
  spreadsheets, and Parquet covers the typed, columnar case. SQLite adds a schema migration
  story for no gain.
- **Recording `loop_max` on the campaign now, so the export is historical**: deferred. It is
  a state-schema change on the queue path, out of scope for an export. The caveat is explicit
  in the manifest.
- **pyarrow as a hard dependency**: rejected. It is about 100 MB for an optional format.

## Acceptance criteria

- [x] `CAMPAIGN_COLUMNS`, `RUN_COLUMNS`, `FINDING_COLUMNS` match the lists above. Every exported
      row has exactly those keys in that order, with null for unknown values.
- [x] For a fixture campaign (state + ledger with transitions, runs, gates, parks, findings and
      waiver notes), the campaign row's `outcome`, `ended_at`, `wall_s`, `time_in_state_s`,
      `active_s`, `runs_by_persona`, `models`, `gate_failures`, `park_reasons`,
      `cost_cap_frac`, `must_fix_count`, `finding_count`, `waived_count`, `close_mode` and
      `merged_sha` equal the values derived by hand. The fixture includes a `--reset-loops`
      resume, a mid-campaign `retry_gate` and a `triage_retry`, a `--raise-cap` resume, and
      both kinds of fix loop: `loops`, `gate_retries`, `triage_retries`, `loop_resets` and
      `cap_raises` equal the ledger totals, not the reset counters.
- [x] `size`, `tone`, `profile_source` and `sizing_rationale` come from `state.sizing` and are
      null for a campaign without it. `loop_max` comes from the current profile, and the
      manifest `notes` say so.
- [x] `ac_count` and `spec_lines` come from `spec_at_open.md` (checked and unchecked boxes both
      count) and are null when the file is missing.
- [x] Run rows match `runs.campaign_runs` for every shared column, with `budget_frac` and the
      token columns derived as documented. Finding rows carry `state_at` replayed from the
      transitions.
- [x] `signals` implements the table: each rule is triggered and not triggered by a fixture, and
      the list order is the table order.
- [x] `wt campaign export` writes the three JSONL files and `manifest.json` (schema, row counts,
      column types) into the default or `--out` directory. `--format csv` encodes `json`
      columns as JSON strings. `--project`, `--since` and `--terminal-only` filter.
      `--table X --stdout` prints one table and writes no files. Each invalid flag combination,
      an existing non-empty `--out` and a same-second default directory behave as documented.
      `--since` keeps the boundary day in `cfg["_tz"]`.
- [x] `--format parquet` writes typed Parquet files when `pyarrow` is importable (test skipped
      otherwise). With the import blocked, it fails with the documented message, exits non-zero
      and creates no directory.
- [x] The export writes nothing under `campaigns/`. `state.json` and `ledger.jsonl` mtimes
      and contents are unchanged after a run.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests** (`tests/test_agentic_campaign_export.py`, new; fixture campaigns built with
  `store.create_campaign` / `append_event` and `schemas.with_defaults`, timestamps set
  explicitly):
  - column constants and row key order for all three tables;
  - a hand-computed campaign row, with a pre-SPEC-0219 campaign (no `sizing`) and a campaign
    with no `spec_at_open.md`;
  - `time_in_state_s` across QUEUED → OPENING → IMPLEMENTING → GATING → TRIAGING → … →
    MERGED, including a PARKED interval;
  - run rows against `runs.campaign_runs` (one run with `usage`, one without, and one
    `running` run with a null `budget_frac`);
  - `cost_cap_frac` null for `cap_usd` null and for `cap_usd` 0;
  - finding rows with `state_at`;
  - every signal on and off;
  - CLI via `CliRunner`: default directory under a temp `WT_DATA_DIR` (and a second export
    in the same second gets `-2`), `--out` (empty, missing, non-empty), the filters with a
    `--since` boundary-day campaign, `--stdout`, each invalid flag combination, the `--json`
    manifest and its `notes`, CSV encoding;
  - read-only guard: mtimes and bytes of every `state.json` / `ledger.jsonl` before and after;
  - Parquet round trip with `pytest.importorskip("pyarrow")`;
  - the missing-pyarrow path with `monkeypatch.setitem(sys.modules, "pyarrow", None)`.
- **Manual verification:** `uv run wt campaign export --json` against the real data dir. Load
  `campaigns.jsonl` in DuckDB (`select size, tone, profile, loops, cost_usd, signals from
  read_json_auto('campaigns.jsonl')`) and check that the numbers for two known campaigns match
  `wt campaign show`.
- **Regression guard:** full `uv run pytest`. The export only reads: no state or ledger file
  changes (the test compares file mtimes before and after).

### Test record (2026-10-25)

- `uv run pytest tests/test_agentic_campaign_export.py`: 42 passed, 1 skipped (loop 2 added the
  pinned column types, the terminal-state/edge/escalation cases, `--json` notes, plain output,
  UTF-8 writes under an ASCII default encoding and failed-write cleanup); earlier: 31 passed, 1 skipped (Parquet round trip
  needs `pyarrow`, which is not installed in the project env). Covers column constants and key
  order, the hand-computed campaign row (reset-loops and raise-cap resumes, `retry_gate`, a
  `triage_retry`, both fix-loop kinds, a PARKED interval), pre-SPEC-0219 and no-`spec_at_open.md`
  campaigns, run rows, finding `state_at`, `cost_cap_frac` null cases, every signal on/off and
  order, the CLI (default dir and same-second `-2`, `--out` rules, filters with a `--since`
  boundary day in `cfg["_tz"]`, `--stdout`, invalid flag combinations, `--json` manifest and
  notes, CSV encoding), the read-only guard and the missing-`pyarrow` path.
- `uv run --with pyarrow pytest tests/test_agentic_campaign_export.py`: 43 passed (loop 2); earlier `-k parquet`: 2 passed
  (typed Parquet round trip and missing-pyarrow path).
- Loop 3 (review fixes): `uv run pytest tests/test_agentic_campaign_export.py`: 47 passed, 1
  skipped; with `--with pyarrow`: 48 passed. New cases: counters that differ from the ledger pin
  `runs`/`gate_runs`/`parks` to `state.counters`; an `open` event later than `created_at`
  separates the `time_in_state_s` and `wall_s` starts; `loop_max` follows an overridden profile;
  fix-reason transitions to other states and IMPLEMENTING transitions with other reasons are not
  counted as loops; off-type values (float review score, non-string `ac`/`title`) are coerced to
  the declared column types, including through Parquet; a foreign subdirectory in `--out` cannot
  make the failure cleanup raise.
- Manual: `wt campaign export --json` against the real data dir (46 campaigns, 196 runs, 393
  findings); sizing, profile and cost for the latest campaigns match `wt campaign show`.
- Full `uv run pytest`: 2211 passed, 2 skipped. `python3 tools/spec_lint.py` (after
  `--write-ledger`): exit 0. The raw-output guard (`tests/test_rich_output_guard.py`) got an
  allowlist entry for the `--stdout` path of `campaign_export`.

### As shipped (2026-10-25)

- Every row is coerced to the declared column types when it is projected (`str` of non-strings,
  `int` rounded from floats, null when a value cannot convert), so one off-type value from an LLM
  verdict or config cannot fail a Parquet export or ship the wrong type in JSONL/CSV. The failure
  cleanup removes only the export's own files (and the new directory when it is empty), ignoring
  OS errors, so it cannot mask the `export failed` error.
- As designed. Small additions: `wt campaign export --json` prints the manifest plus an `out` key
  with the export directory (the `manifest.json` file does not carry it); `--json` is ignored
  with `--stdout`. A campaign with a null/unparseable `created_at` is dropped when `--since` is
  given. `wall_s` is null for a terminal campaign with no terminal `transition` event.
- Files are written as UTF-8 (`export._atomic_write` got an optional `encoding`, default
  unchanged). Parquet tables are all built before any file is written, and a failed write removes
  what it wrote (and the directory, if it was new) and exits with `export failed, nothing
  written: <reason>`, so the same `--out` can be retried.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None required. The export works on campaigns from before SPEC-0219: their sizing columns are null and
`long_for_size` is off.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
