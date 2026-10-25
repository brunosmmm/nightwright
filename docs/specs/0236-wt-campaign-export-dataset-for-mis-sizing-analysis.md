---
id: SPEC-0236
title: "`wt campaign export`: a flat campaign dataset for mis-sizing analysis"
status: accepted
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
- limits and spec shape: `loop_max` int, `cap_usd` float, `models` json, `ac_count` int,
  `spec_lines` int;
- runs, gates and parks: `loops` int, `runs` int, `runs_by_persona` json, `gate_runs` int,
  `gate_failures` int, `gate_retries` int, `triage_retries` int, `parks` int,
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
  its loop limit. The manifest says so in `notes`. `cap_usd` is `state.cost.cap_usd`, which is
  the historical value.
- `models`: `{persona: model}` from the last `run.start` per persona. `runs_by_persona`: count of
  `run.start` events per persona.
- `ac_count`: the number of lines matching `^\s*[-*] \[[ xX]\]` in the `Acceptance criteria`
  section (`specmeta.sections`) of `campaign_dir/spec_at_open.md`. `spec_lines`: that file's
  line count. Both are null when the file is missing (merge campaigns, campaigns that never
  opened).
- `gate_failures`: `gate` events with `ok: false`. `park_reasons`: the `reason` of every `park`
  event, in order. `loops`, `runs`, `gate_runs`, `gate_retries`, `triage_retries`, `parks`:
  `state.counters`.
- `cost_usd`, `cost_by_persona`: `state.cost`. `cost_cap_frac`: `cost_usd / cap_usd` rounded to
  3 places, null without a cap.
- `review_score`: `state.last_review.score`. `must_fix_count`: `finding` events with severity
  `must_fix`. `finding_count`: `finding` events with severity other than `info`.
  `waived_count`: `note` events with `author == "queue"` whose text starts with `waived `
  (written by `_apply_triager`, SPEC-0206).
- `close_mode`, `merged_sha`: `state.close.mode / .merged_sha`.
- Run rows: one per `runs.campaign_runs(cfg, cid)` record, projected onto `RUN_COLUMNS`.
  - `budget_frac` is `charged_usd / max_budget_usd`.
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
harder than "small". An `xs` (at most 3 ACs, one module) that keeps a slot for more than an
hour is the idea's "xs that took several hours", with margin for gate time. The constants
are module-level so the data can be used to tune them later. The rules are part of the v1
contract.

### Writing and CLI

- `export_tables(cfg, *, project=None, since=None, terminal_only=False, now=None) -> dict`
  returns `{"campaigns": [...], "runs": [...], "findings": [...]}`. Campaigns come from
  `store.list_campaigns(cfg, include_terminal=True, project=project)`, filtered by
  `created_at >= since` (a date) and by `terminal_only`, and sorted by `created_at` ascending
  so the output is deterministic.
- `write_export(tables, out_dir, fmt, *, filters) -> dict` writes `campaigns.<ext>`,
  `runs.<ext>`, `findings.<ext>` and `manifest.json`, and returns the manifest. The manifest
  contains:
  - `schema`, `generated_at`, `wt_version` (`importlib.metadata.version("wt")`), `format`,
    `filters`;
  - `tables: {name: {file, rows, columns: [[name, type], ...]}}`;
  - `notes` (the `loop_max` caveat).

  Files are written with `export._atomic_write`.
  - JSONL: one `json.dumps(row, ensure_ascii=False)` per line.
  - CSV: `csv.DictWriter` with the header in column order. `json` cells are `json.dumps`,
    booleans `true`/`false`, null is the empty string.
  - Parquet: `pyarrow.Table.from_pylist(rows, schema=...)`, built from the column types
    (`str`→`string`, `int`→`int64`, `float`→`float64`, `bool`→`bool_`, `json`→`string` holding
    JSON) and written with `pyarrow.parquet.write_table`.
- `wt campaign export [--format jsonl|csv|parquet] [--out DIR] [--project P] [--since
  YYYY-MM-DD] [--terminal-only] [--table campaigns|runs|findings --stdout] [--json]`:
  - `--out` defaults to `<data_dir>/agentic/exports/<YYYYMMDD-HHMMSS>/`. The command prints the
    directory and the row counts, or the manifest with `--json`.
  - `--stdout` writes the single `--table` (required with it) to stdout in `jsonl` or `csv`
    and writes no files.
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

- [ ] `CAMPAIGN_COLUMNS`, `RUN_COLUMNS`, `FINDING_COLUMNS` match the lists above. Every exported
      row has exactly those keys in that order, with null for unknown values.
- [ ] For a fixture campaign (state + ledger with transitions, runs, gates, parks, findings and
      waiver notes), the campaign row's `outcome`, `ended_at`, `wall_s`, `time_in_state_s`,
      `active_s`, counters, `runs_by_persona`, `models`, `gate_failures`, `park_reasons`,
      `cost_cap_frac`, `must_fix_count`, `finding_count`, `waived_count`, `close_mode` and
      `merged_sha` equal the values derived by hand.
- [ ] `size`, `tone`, `profile_source` and `sizing_rationale` come from `state.sizing` and are
      null for a campaign without it. `loop_max` comes from the current profile, and the
      manifest `notes` say so.
- [ ] `ac_count` and `spec_lines` come from `spec_at_open.md` (checked and unchecked boxes both
      count) and are null when the file is missing.
- [ ] Run rows match `runs.campaign_runs` for every shared column, with `budget_frac` and the
      token columns derived as documented. Finding rows carry `state_at` replayed from the
      transitions.
- [ ] `signals` implements the table: each rule is triggered and not triggered by a fixture, and
      the list order is the table order.
- [ ] `wt campaign export` writes the three JSONL files and `manifest.json` (schema, row counts,
      column types) into the default or `--out` directory. `--format csv` encodes `json`
      columns as JSON strings. `--project`, `--since` and `--terminal-only` filter.
      `--table X --stdout` prints one table and writes no files.
- [ ] `--format parquet` writes typed Parquet files when `pyarrow` is importable (test skipped
      otherwise). With the import blocked, it fails with the documented message, exits non-zero
      and creates no directory.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests** (`tests/test_agentic_campaign_export.py`, new; fixture campaigns built with
  `store.create_campaign` / `append_event` and `schemas.with_defaults`, timestamps set
  explicitly):
  - column constants and row key order for all three tables;
  - a hand-computed campaign row, with a pre-SPEC-0219 campaign (no `sizing`) and a campaign
    with no `spec_at_open.md`;
  - `time_in_state_s` across QUEUED → OPENING → IMPLEMENTING → GATING → TRIAGING → … →
    MERGED, including a PARKED interval;
  - run rows against `runs.campaign_runs` (one run with `usage`, one without);
  - finding rows with `state_at`;
  - every signal on and off;
  - CLI via `CliRunner`: default directory under a temp `WT_DATA_DIR`, `--out`, the filters,
    `--stdout`, `--json` manifest, CSV encoding;
  - Parquet round trip with `pytest.importorskip("pyarrow")`;
  - the missing-pyarrow path with `monkeypatch.setitem(sys.modules, "pyarrow", None)`.
- **Manual verification:** `uv run wt campaign export --json` against the real data dir. Load
  `campaigns.jsonl` in DuckDB (`select size, tone, profile, loops, cost_usd, signals from
  read_json_auto('campaigns.jsonl')`) and check that the numbers for two known campaigns match
  `wt campaign show`.
- **Regression guard:** full `uv run pytest`. The export only reads: no state or ledger file
  changes (the test compares file mtimes before and after).

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None required. The export works on campaigns from before SPEC-0219: their sizing columns are null and
`long_for_size` is off.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
