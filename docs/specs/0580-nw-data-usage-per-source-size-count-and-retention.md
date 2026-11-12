---
id: SPEC-0580
title: nw data usage — per-source size, count and retention report of the agentic data dir, with --json
status: proposed
owner: user
kind: feature
created: 2026-11-12
source_idea: IDEA-890
tags: [agentic, cli, storage]
---

## Context

The agentic data dir (`<data_dir>/agentic`, `store.agentic_dir`, `src/nightwright/agentic/store.py:35`)
holds campaign records, per-run replay logs, gate records, the sentinel's logs, standalone gate jobs,
remote-job records, spec reviews and several append-only logs. On the maintainer's box it grew to
about 1.1 GiB of campaigns (110 MiB of it `stream.jsonl`), 157 MiB of sentinel and 43 MiB of gate
jobs. While writing SPEC-0405's data inventory (2026-11-03) the only way to learn how big each
source is, how many campaigns / runs / streams exist and which of them is ever pruned was
`du`, `ls` and ad-hoc Python.

Retention is spread over four unrelated mechanisms, and nothing lists them next to the data they
govern:

- **SPEC-0524 disk sweep** (`src/nightwright/agentic/disk_clean.py`): terminal campaigns' `tmp/`
  at once, terminal campaigns' `runs/*/stream.jsonl` older than `agentic.disk_clean.run_days`
  (default 14), sentinel `<name>.logs/*` files older than `agentic.disk_clean.log_days`
  (default 14, newest `KEEP_NEWEST_LOGS` = 40 per dir always kept), and final gate jobs through
  `gate_jobs.gc`. Run hourly by the runner and by `nw queue disk-clean`.
- **`agentic.gate_jobs.retention_days`** (default 7, `gate_jobs.py:45`): final or lost
  `gatejobs/<id>` records.
- **`remote_jobs.prune`** (`remote_jobs.py:206`): a finished campaign's final `remote-jobs/*.json`
  records and, for pull jobs, their `job-frames` / `job-uploads` files.
- **`nw worker gc --keep-days`**: the *worker box's* job dirs and caches (not this data dir).

Everything else (campaign records, ledgers, `gates/`, events, runner logs, decisions, mailbox,
spec reviews) is kept forever. SPEC-0405 and SPEC-0406 read campaign *history*; no spec reports
*storage*. `nw queue disk-clean` reports only what it just freed.

## Goals / Non-goals

**Goals**
- `nw data usage [--json]`: one read-only report with a row per agentic data source giving
  size, file count, source-specific count (campaigns by state, runs, streams, jobs ...),
  oldest and newest timestamps, and the retention rule that applies, with the *effective*
  configured value.
- The rows partition the agentic dir: they add up to the dir's total size, so nothing is
  hidden and a new unknown file is visible under `other`.
- A stable machine-readable form (`nw.data.usage.v1`) usable by the TUI, dashboards and
  agents.

**Non-goals**
- No deletion, no "would free" dry-run, no new retention setting. Cleanup stays with
  `nw queue disk-clean`, `nw gate` jobs retention and `nw worker gc`.
- No coverage of the rest of `<data_dir>` (`history.jsonl`, `org-backups/`, `outbox/`,
  spec reservations) or of a remote worker box's disk; those have their own commands
  (`nw backups`, `nw worker jobs`).
- No history cache or timeline analytics (SPEC-0405 / SPEC-0472 territory).
- No git worktree sizes (they live outside the agentic dir and are SPEC-0275's `nw queue gc`).

## Decision

Add a read-only `nw data usage` command (new root group `nw data`, room for later siblings)
backed by a pure module `src/nightwright/agentic/data_usage.py` with `usage(cfg, *, now=None) -> dict`.
It walks the agentic dir once, assigns every regular file to exactly one source by a fixed,
ordered classification table, and returns the `nw.data.usage.v1` document. A static table
in the same module names each source's retention rule; the rule text interpolates the current
effective settings by calling the existing `disk_clean.settings(cfg)` and `gate_jobs.settings(cfg)`,
so the report cannot drift from the sweep's own defaults. The CLI renders a Rich table, or the
document with `--json`.

## Design

### Module and CLI

- `src/nightwright/agentic/data_usage.py`: `SOURCES` (ordered classification table), `usage(cfg, now=None)`,
  `render_rows` helpers kept out of the pure function.
- `src/nightwright/agentic/cli_data.py`: `data_group = click.group("data")` with the `usage`
  command; registered from `register()` in `src/nightwright/agentic/cli.py` next to the other
  `cli.add_command(...)` lines. `--json` emits the document with `json.dumps`; the text form is a
  Rich table sorted by bytes descending, with a final `total` line, sizes in IEC units
  (`KiB`, `MiB`, `GiB`).
- Exit code is 0 whenever the report is produced, including when the agentic dir does not exist
  (every source reports zeros and `total.bytes` is 0). It never creates, touches or locks anything
  (no stamp file, no lease), so it is safe while the runner is live.

### Walk and size rules

- One `os.scandir` walk of `store.agentic_dir(cfg)`. Only regular files count; symlinks (to files
  or dirs) are neither followed nor counted, as in `disk_clean._du`. Size is `st_size` (apparent
  bytes, matching `du --apparent-size`). A file that vanishes or is unreadable mid-walk is skipped
  and increments `skipped`.
- `oldest` / `newest` are the minimum and maximum file `mtime` of the source, as UTC ISO-8601 with
  a `Z` suffix, or `null` for an empty source.
- Classification is by the file's path relative to the agentic dir, split into parts, and every
  file is assigned to exactly one source. The first matching rule wins, tested in this order:
  1. `campaigns/<id>/gates/**` is `gates`; `campaigns/<id>/tmp/**` is `campaign-tmp`. These two
     are tested before the `campaigns` row, which is defined as "everything else in an id dir".
  2. `streams` is exactly `campaigns/<id>/runs/<run>/stream.jsonl` (4 parts below `campaigns`).
     A `stream.jsonl` at any other depth (for example `runs/<run>/sub/stream.jsonl`) is not a
     stream: like every other file under `campaigns/<id>/runs/`, it is `runs`.
  3. Any file directly under `campaigns/` (not inside an id dir), for example `campaigns/x.txt`,
     is `other`. A loose file directly under `campaigns/<id>/runs/` is `runs` (rule 2).
  4. The remaining table rows by their first path part (or exact file name); anything unmatched
     is `other`.

  The table below is the stable listing order of `sources` (not the matching order). Its `paths`
  column holds the glob patterns, relative to `root`, that are emitted verbatim as each row's
  `path` list; the exclusions in the `campaigns` row are enforced by the rules above, not by the
  glob.

| source id | paths (emitted as `path`) | `count` unit |
|---|---|---|
| `campaigns` | `campaigns/*/*` (files in an id dir, and below it except under `runs/`, `gates/`, `tmp/`) | campaign dirs with a `state.json`, plus `by_state` |
| `runs` | `campaigns/*/runs/**` except the streams | run dirs (`campaigns/*/runs/<run>/`) |
| `streams` | `campaigns/*/runs/*/stream.jsonl` | stream files |
| `gates` | `campaigns/*/gates/**` | gate record files |
| `campaign-tmp` | `campaigns/*/tmp/**` | campaigns that have a `tmp/` |
| `sentinel` | `sentinel/**` | files under `sentinel/*.logs/` (the sweepable ones) |
| `gatejobs` | `gatejobs/**` | job dirs |
| `remote-jobs` | `remote-jobs/**`, `job-queue/**`, `job-uploads/**`, `job-frames/**` | `remote-jobs/*.json` records |
| `runner-jobs` | `runner-jobs/**` | job records |
| `spec-reviews` | `reviews/**`, `spec-reviews/**`, `spec-reviews.jsonl` | review files |
| `events` | `events.jsonl`, `events.lock`, `events.projector.json`, `cursors/**` | files |
| `runner-logs` | `runner.jsonl`, `runner.log` | files |
| `decisions` | `conductor/**` | files |
| `mailbox` | `mailbox/**` | inbox files |
| `other` | everything else (locks, `workers.json`, `cost_stats.json`, `escalations/`, a loose `campaigns/x.txt`, ...) | files |

  The `sentinel` row has two counters on purpose: `files` counts every file under `sentinel/**`
  (and `bytes` sizes them all), while `count` counts only the files under `sentinel/*.logs/`.
  `count` is always an integer and each row also carries `files` (the number of counted regular
  files) so the unit is never ambiguous. Line counts are deliberately not computed: they would
  mean reading the 100 MiB logs.
- `campaigns` additionally carries `by_state: {<STATE>: {count, bytes}}` where `bytes` are the
  record files assigned to the `campaigns` source (not the whole campaign dir) and the state is
  read from each `state.json` (an unreadable one is bucketed as `UNKNOWN`). This answers "how many
  campaigns are live vs terminal" without a second command.

### Retention column

Each row has `retention: {rule, text, setting}`. `rule` is one of `forever`, `age`,
`on-terminal`, `on-campaign-end`; `setting` names the governing config key or command (or `null`),
and `text` is human readable with the effective value:

| source | rule | setting / text |
|---|---|---|
| `campaigns`, `runs`, `gates`, `events`, `runner-logs`, `decisions`, `mailbox`, `spec-reviews`, `runner-jobs`, `other` | `forever` | `null`; "kept forever (no sweep)" |
| `streams` | `age` | `agentic.disk_clean.run_days`; "terminal campaigns only, older than 14 d (live campaigns never)" |
| `campaign-tmp` | `on-terminal` | `agentic.disk_clean`; "removed when the campaign is terminal" |
| `sentinel` | `age` | `agentic.disk_clean.log_days`; "logs older than 14 d, newest 40 per dir kept; other files forever" |
| `gatejobs` | `age` | `agentic.gate_jobs.retention_days`; "final/lost jobs older than 7 d" |
| `remote-jobs` | `on-campaign-end` | `remote_jobs.prune`; "final records dropped when the campaign ends" |

The numbers in `text` come from `disk_clean.settings(cfg)` (`run_days`, `log_days`,
`KEEP_NEWEST_LOGS`) and `gate_jobs.settings(cfg)` (`retention_days`); when
`agentic.disk_clean.enabled` is false the `streams`, `campaign-tmp` and `sentinel` texts say
"sweep disabled (`nw queue disk-clean` still works)". The `forever` rows are verified during
implementation by grepping for any unlink/rmtree of those paths; a source found to have a sweep
gets its true rule instead.

### JSON document (`nw.data.usage.v1`)

```json
{
  "schema": "nw.data.usage.v1",
  "root": "/home/u/.local/share/nightwright/agentic",
  "generated_at": "2026-11-12T10:00:00Z",
  "skipped": 0,
  "total": {"bytes": 1234, "files": 56},
  "sources": [
    {"id": "streams", "path": ["campaigns/*/runs/*/stream.jsonl"],
     "bytes": 110000000, "files": 412, "count": 412, "count_unit": "stream files",
     "oldest": "2026-10-30T08:00:00Z", "newest": "2026-11-12T09:58:00Z",
     "retention": {"rule": "age", "setting": "agentic.disk_clean.run_days",
                   "text": "terminal campaigns only, older than 14 d (live campaigns never)"}}
  ]
}
```

`path` is a list of strings: the glob patterns from the classification table's `paths` column,
relative to `root`, e.g. `["remote-jobs/**","job-queue/**","job-uploads/**","job-frames/**"]`
for `remote-jobs`; it is never empty and a single-pattern row is a one-element list.

`sources` is always the full table in the order above (stable for consumers); the text form is the
one that sorts. `campaigns` has the extra `by_state` key. `total.bytes` equals the sum of
`sources[*].bytes`. All keys are present in every row.

## Alternatives considered

- **Extend `nw queue disk-clean` with `--report`** — that command is a mutation; a read-only
  report under a mutating verb invites accidental sweeps, and its scope is the swept sources only.
- **Shell out to `du`** — not portable, cannot attribute `stream.jsonl` inside a campaign dir,
  and gives no counts, states or retention.
- **Group by top-level directory only** — hides the largest item (`stream.jsonl` is inside
  `campaigns/`); the classification table is what makes the report useful.
- **A "would free" estimate** — requires re-implementing the sweep's eligibility rules in a second
  place; left to a later spec if wanted.

## Acceptance criteria

- [ ] `nw data usage` exists and is read-only: it creates, modifies or touches no file under the
  data dir (including the `.disk-clean` stamp) and exits 0, including when the agentic dir is
  missing (all rows zero, `total.bytes` 0).
- [ ] `--json` emits one JSON document with `schema: "nw.data.usage.v1"`, `root`, `generated_at`,
  `skipped`, `total` and a `sources` list holding every source id of the Design table in that
  order, each row with `id`, `path` (a non-empty list of strings, the table's glob patterns),
  `bytes`, `files`, `count`, `count_unit`, `oldest`, `newest` and `retention{rule,setting,text}`.
- [ ] Every regular file under the agentic dir is counted in exactly one source, and
  `total.bytes` equals both the sum of source bytes and the bytes of an independent walk of the
  fixture. Symlinks (to files or dirs) are neither followed nor counted; a file that vanishes
  during the walk does not fail the command and increments `skipped`. The scope rules hold:
  `streams` is exactly `campaigns/<id>/runs/<run>/stream.jsonl`; a nested
  `runs/<run>/sub/stream.jsonl` and a loose file under `campaigns/<id>/runs/` are `runs`;
  `gates/` and `tmp/` files are `gates` / `campaign-tmp`, never `campaigns`; a loose
  `campaigns/x.txt` is `other`.
- [ ] `campaigns.count` equals the number of campaign dirs with a `state.json`, and `by_state` counts
  sum to it, with an unreadable `state.json` bucketed as `UNKNOWN`.
- [ ] `oldest`/`newest` are the min/max file mtime of the source in UTC `Z` ISO-8601, `null` when
  the source is empty.
- [ ] Retention texts use the effective config: overriding `agentic.disk_clean.run_days`,
  `agentic.disk_clean.log_days` and `agentic.gate_jobs.retention_days` changes the numbers in the
  `streams`, `sentinel` and `gatejobs` rows; `enabled: false` changes the sweep-governed rows to
  say the sweep is disabled.
- [ ] `forever` is reported for the campaign records, `gates`, events, runner logs, decisions,
  mailbox and spec reviews, and each `forever` claim is backed by the implementer's grep recorded
  in the spec's "As shipped".
- [ ] The text form is a table sorted by bytes descending with a total line and IEC units;
  `nw data --help` and `nw data usage --help` describe the command, and `nw data` appears in
  the root command list.

## Test plan

- **Automated tests:** new `tests/test_data_usage.py`, module-level `pytestmark = pytest.mark.fast`
  (a name that is not `test_tui_*`, `test_remote_*`, `*_e2e`, `*_integration` or `*_race`, and
  imports no `tui_harness`/`remote_loopback`). It builds a fixture agentic dir under `tmp_path`
  with `cfg = {"data_dir": str(tmp_path)}` (as `tests/test_disk_clean.py` does) containing live
  and terminal campaigns with runs, streams, gates and `tmp/`, a sentinel dir with `.logs`, gatejobs,
  remote-jobs, reviews, events, runner logs, conductor decisions, mailbox inboxes and a stray
  file. The fixture also holds the ambiguous shapes: a loose `campaigns/x.txt`, a loose
  `campaigns/<id>/runs/note.txt`, and a nested `campaigns/<id>/runs/<run>/sub/stream.jsonl`.
  Tests:
  - `test_partition_total`: `total.bytes` and `total.files` equal an independent `os.walk` sum
    and the sum of rows; the stray file lands in `other`.
  - `test_each_source_scope`: bytes/files/count per source for the fixture, including
    `streams` vs `runs` and `gates`/`campaign-tmp` separation, and the named expectations
    `campaigns/x.txt` -> `other`, `runs/note.txt` -> `runs`, nested `sub/stream.jsonl` -> `runs`
    (not `streams`), `gates/*` -> `gates` and `tmp/*` -> `campaign-tmp` (not `campaigns`), and the
    `sentinel` row's `files` (all of `sentinel/**`) versus `count` (only `sentinel/*.logs/*`).
  - `test_by_state`: counts per state sum to `campaigns.count`; a corrupt `state.json` is `UNKNOWN`.
  - `test_oldest_newest`: `os.utime` set files, rows match; empty source is `null`.
  - `test_symlinks_and_vanishing`: a symlinked file/dir is not counted; a monkeypatched `stat`
    raising `FileNotFoundError` bumps `skipped` and does not raise.
  - `test_missing_dir`: no agentic dir gives zero rows and exit 0.
  - `test_retention_follows_config`: cfg overrides change the texts; `enabled: false` changes them.
  - `test_read_only`: a snapshot of every path's `(mtime_ns, size)` before and after is equal
    and no `.disk-clean` stamp appears.
  - `test_cli_json_and_table` (Click `CliRunner`): `nw data usage --json` parses with the full key
    set and order, and every row's `path` is a non-empty list of strings (`remote-jobs` carries its
    four patterns, `streams` carries `["campaigns/*/runs/*/stream.jsonl"]`); the plain form exits 0, shows every non-empty source, sorted descending, with
    a total line; `nw data --help` lists `usage`; the root `nw --help` lists `data`.
  - `test_scale` (non-gating, not an acceptance criterion): 200 campaigns x 5 runs; records the
    elapsed time (target well under 5 s) and fails only on a loose 30 s bound, so a busy CI box
    does not flake.
- **Marker check:** `python3 tools/test_tiers.py --check` exits 0 (the new module has exactly one
  tier marker).
- **Manual verification:** on the maintainer's box run `nw data usage` and `nw data usage --json |
  jq '.sources[] | {id,bytes,count}'`; compare `campaigns`+`runs`+`streams`+`gates`+`campaign-tmp`
  bytes with `du -sb ~/.local/share/nightwright/agentic/campaigns` (equal within files created by
  the live runner between the two calls) and the `total.bytes` with
  `du -sb --apparent-size ~/.local/share/nightwright/agentic` (directories' own sizes excluded, so
  `du` is slightly higher). Run `nw queue disk-clean`, then `nw data usage` again: `streams`,
  `campaign-tmp`, `sentinel` and `gatejobs` shrink and the rest is unchanged.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_disk_clean.py` and `tests/test_console_piped.py` (root command registration);
  `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

No data migration. Ship `data_usage.py`, `cli_data.py` and the `register()` line together; add the
command to the CLI help text of `nw queue disk-clean` ("see `nw data usage`") and to the
nw-orient skill's command list if it enumerates storage commands. Document the JSON schema in the
spec's "As shipped" section on completion.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed: `tests/test_data_usage.py` passes, affected tests pass, `python3
  tools/test_tiers.py --check` exits 0, the manual comparison against `du` was done and noted.
- [ ] No regressions (affected tests at the queue's close gate).
- [ ] Spec body updated to match what shipped (including the grep that confirmed each `forever` row).
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
  `python3 tools/spec_lint.py` exits 0.

## Open questions

- None. A source later found to have a sweep gets its true rule in the table (recorded in "As
  shipped"); extra retention settings or a "would free" column are follow-up specs.
