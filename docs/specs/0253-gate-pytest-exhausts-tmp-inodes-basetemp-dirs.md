---
id: SPEC-0253
title: "Per-campaign TMPDIR for gates and personas, plus a temp-filesystem admission guard"
status: done
owner: user
created: 2026-10-26
updated: 2026-10-26
kind: feature
source_idea: IDEA-341
tags: [agentic, runner, reliability]
---

## Context

IDEA-341. On 2026-10-26 `/tmp` (tmpfs, 1,048,576 inodes) hit 100% inode use: about 933k files
under `/tmp/pytest-of-bruno` left by the gate and implementer `pytest` runs of six parallel
campaigns. Every worktree, Bash tool and runner child then failed with ENOSPC; three campaigns
and a merge parked and the runner had to be stopped. pytest keeps only the last 3 basetemps per
run name, but concurrent runs and large suites still pile up, and nothing bounds them.

Today gate children get `gates.gate_env` (src/wt/agentic/gates.py) and persona children get the
`env` dict in `personas.py` (the function that builds the persona `RunRequest`), both merged over `os.environ` (gates.py:
`run_gate`; runner.py: `_child_env`). Neither sets a temp dir, so everything lands in the
runner's `/tmp`.

## Goals / Non-goals

**Goals:** (1) campaign child processes write temp files to disk under the campaign dir and the
files are removed when the gate/run ends; (2) the runner stops admitting new work, with a named
watchdog event, before the temp filesystem is exhausted.
**Non-goals:** changing the repo's pytest config; cleaning other users' `/tmp` content; a
global GC daemon.

## Decision

1. Add `store.campaign_tmp(cfg, cid) -> Path` = `campaign_dir(cfg, cid) / "tmp"` (the parent;
   created on demand) and `store.child_tmp(cfg, cid, owner) -> Path` = `campaign_tmp / owner`
   where `owner` is `gate-<seq:03d>` for a gate run and `run-<run_id>` for a persona run. A
   campaign is NOT single-process: several reviewers run concurrently (`_apply_reviews`), persona
   runs can be detached and adopted by a later runner (SPEC-0225), and a human can fire
   `wt gate run` during a live run. A shared directory would be wiped under a live sibling, so
   every child gets its own unique subdirectory. `gates.gate_env` and the persona RunRequest env
   add `TMPDIR`, `TEMP`, `TMP` pointing at that child directory (created before the child starts). pytest's default basetemp derives from `tempfile.gettempdir()`, so no `--basetemp`
   flag (and no gate-command edit) is needed.
2. Only the owner removes its directory: `gates.run_all` removes its `gate-<seq>` dir in a
   `finally`; a persona run's `run-<run_id>` dir is removed when that run's result is recorded
   (`_record_run` apply: success or failure). An interrupt that leaves a detached process alive
   does NOT remove the dir; it is removed when the adopted run is finally recorded. When the
   campaign reaches MERGED or ABANDONED (`_transition`), the whole `tmp/` tree is removed.
   Sweep of residue from a killed runner: at admission/tick time a child dir is swept only if its
   owner is provably finished: `gate-<seq>` with a recorded `gate` event of that seq in the ledger
   and `run-<run_id>` with a recorded `run.end` (or no live/detached run record) for that run id.
   Anything else is left alone (bounded leak, reaped at the terminal-state removal). All removals
   go through `store.clear_child_tmp(cfg, cid, owner)` / `store.clear_campaign_tmp(cfg, cid)`,
   which resolve the target and assert it is strictly under `campaign_dir(cfg, cid) / "tmp"`
   (an empty/odd owner or the `tmp` dir itself for a child clear raises instead of deleting; the
   campaign dir holds `state.json` and `ledger.jsonl`).
3. New helper `events.temp_pressure(cfg) -> dict | None` does `os.statvfs` on each distinct
   filesystem among `tempfile.gettempdir()` and `store.agentic_dir(cfg)`, and returns the first
   with free bytes < `tmp_min_free_mb` (default 512) or free inodes < `tmp_min_free_inodes_pct`
   percent of total (default 10; skipped when `f_files == 0`, e.g. btrfs). Both settings live in
   `events.DEFAULTS` / `agentic.events` and use the existing `settings()` sanitiser, which rejects
   bool, negative and non-finite values; `tmp_min_free_inodes_pct` above 100 also falls back to
   the default (clamping would pause admission forever). Distinct filesystems are de-duplicated
   by `os.stat(path).st_dev`; the "path" in the result and event key is the checked directory
   (`tempfile.gettempdir()` or `store.agentic_dir(cfg)`), since `statvfs` gives no mount point.
4. `queue._tick` step 1 skips admission (the `pick_admissions` loop; running campaigns continue,
   QUEUED items stay QUEUED) while `temp_pressure` is non-None, and records a note in the tick
   report (`admission_paused: temp_pressure`).
5. `events.watchdog_checks` emits `("temp_pressure:<checked dir path>", "temp_pressure", None,
   {path, free_mb, free_inodes, free_inodes_pct, thresholds})` while the condition holds; it
   clears itself like the other keyed conditions.
6. `skills/wt-conduct/SKILL.md` gets a `temp_pressure` table row: admission is paused, not
   failed; find the consumer (`df -i`, `du`/`find ... | wc -l` on the path), remove stale
   `pytest-of-*` and finished-campaign `tmp/` dirs (never active campaigns' dirs), confirm the
   event clears, no human escalation unless the space cannot be freed.

## Design

Files: `src/wt/agentic/store.py` (`campaign_tmp`, `child_tmp`, `clear_child_tmp`, `clear_campaign_tmp`), `gates.py` (`gate_env`,
`run_all` clears in a `finally`), `personas.py` (env), `queue.py` (persona-run completion hook
where `_record_run` is applied; `_tick` admission guard; terminal-state cleanup in `_transition`
for MERGED/ABANDONED), `events.py` (`DEFAULTS`, `temp_pressure`, `watchdog_checks`),
`skills/wt-conduct/SKILL.md`. `statvfs` failures (OSError) count as no pressure. The guard
reads the real filesystem; tests monkeypatch `os.statvfs`.

## Alternatives considered

- **Edit the gate command with `--basetemp`** - only covers gates, not implementer test runs, and
  bakes a path into every project's config. TMPDIR covers both and every other tool.
- **Rely on pytest retention / `/tmp` cleanup** - already failed under concurrency.
- **Only the admission check** - treats the symptom; the leak would keep paging the conductor.

## Acceptance criteria

- [x] `gate_env` and persona RunRequest env set `TMPDIR`/`TEMP`/`TMP` to `<campaign_dir>/tmp`.
- [x] Each gate run and persona run gets its own unique `TMPDIR` subdirectory
      (`gate-<seq>` / `run-<run_id>`) under `<campaign_dir>/tmp`, removed only by its owner when
      it ends, including failures; a concurrent sibling's directory and files are untouched.
- [x] On transition to MERGED or ABANDONED the campaign's `tmp/` directory no longer exists.
- [x] The residue sweep removes only child dirs whose owner is recorded finished, never a live one.
- [x] The clear helpers refuse any target not strictly under `<campaign_dir>/tmp`.
- [x] A running campaign still plans and executes its next step in a tick with admission paused,
      and the tick report carries `admission_paused: temp_pressure`.
- [x] While `/tmp` or the agentic dir's filesystem is under either threshold, `_tick` admits no
      QUEUED item; in-flight campaigns are unaffected; admission resumes when pressure clears.
- [x] `watchdog_checks` emits `temp_pressure` while pressure holds and not otherwise.
- [x] `wt-conduct` SKILL.md has a `temp_pressure` row.
- [x] Thresholds are configurable and bad values fall back to defaults.

## Test plan

Automated (`tests/test_agentic_tmp_isolation.py`, fake runner as in `test_agentic_queue.py`):
- `gate_env` returns TMPDIR under the campaign dir; a gate `cmd` of `touch "$TMPDIR/x"; echo
  $TMPDIR` run through `run_all` leaves no `gate-<seq>` dir afterwards and the log shows the path.
- A fake persona run writes a file into `$TMPDIR`; after the apply its `run-<id>` dir is gone, also
  when the run result is an error.
- Two concurrent fake reviewer runs (same campaign) each write a file; each still sees its own file
  until it finishes, and finishing one does not remove the other's dir.
- `_transition` to MERGED and to ABANDONED removes `tmp/`; the sweep removes a `gate-<seq>` dir with
  a recorded gate event and a finished `run-<id>`, but leaves an unfinished/unknown one.
- `clear_child_tmp` with an empty owner, `..`, or an absolute path raises and leaves `state.json`
  and `ledger.jsonl` intact.
- Under monkeypatched pressure, an already-running campaign still has its next step planned and
  executed in the same `_tick`, and the report contains `admission_paused: temp_pressure`.
- `tmp_min_free_inodes_pct = 150` falls back to the default; two paths with the same `st_dev` are
  checked once; the event key uses the checked dir path and passes `schemas.ledger_problems` if
  persisted.
- Monkeypatch `os.statvfs` to report low free blocks, then low free inodes: `temp_pressure`
  returns the path; with `f_files == 0` it returns None; `_tick` with a QUEUED ready item admits
  nothing and the item stays QUEUED; restoring normal values admits it on the next tick.
- `watchdog_checks` includes a `temp_pressure` key under pressure and omits it otherwise;
  `settings()` rejects a negative/bool threshold.
- A test asserting the SKILL.md table contains `temp_pressure`.
- **Sabotage check:** drop the TMPDIR export from `gate_env` (and separately the admission guard)
  and confirm the matching tests fail; restore.
Regression: full `uv run pytest`.

### Test record (2026-10-26)

- `uv run pytest -q tests/test_agentic_tmp_isolation.py`: 32 passed (new file, covers every Test plan item).
- Sabotage: removing the `TMPDIR` export from `gate_env` failed 3 tests; disabling the `_tick`
  admission guard failed `test_pressure_pauses_admission_but_not_in_flight_campaigns`; both restored.
- Review loop 1: added `clear_campaign_tmp` refusal tests (empty id, symlinked `tmp`) and a
  `temp_pressure` test with `gettempdir()` broken; `tests/test_agentic_tmp_isolation.py`: 35 passed.
- Full `uv run pytest -q`: 3021 passed, 3 skipped. `python3 tools/spec_lint.py` exits 0 after
  `--write-ledger`.

### As shipped (2026-10-26)

- The persona-run removal hook is in `store.append_event`: appending a `run.end` event removes
  `tmp/run-<run_id>` (every `_record_run` caller reaches it, adopted runs included), instead of
  a separate hook in `queue.py`.
- `gate_env(cfg, state, seq=None)` sets `TMPDIR`/`TEMP`/`TMP` only when `seq` is given; `run_all`
  passes it. `gates.tmp_env(path)` builds the three variables for both gates and personas.
- `store.child_tmp(..., create=False)` is a pure path unless `create=True`; owners must be a single
  path component (empty, `.`, `..`, separators raise `ValueError`).
- The residue sweep (`store.sweep_campaign_tmp`) runs at the start of every `_tick` over all
  non-terminal campaigns and removes only `gate-<seq>` with a recorded `gate` event of that
  `gate_seq` and `run-<id>` with a recorded `run.end`; a `run-<id>` with no `run.start` yet is
  left alone because `build_request` creates the dir before `run.start` is appended.
- `TickReport.admission_paused` is `"temp_pressure"` when admission was skipped and appears in the
  tick payload only then. WAITING campaigns still resume (they are started work, not admissions).
- `events.temp_pressure` checks `os.environ["TMPDIR"]` (default `/tmp`) instead of
  `tempfile.gettempdir()`: that call writes a probe file and silently falls back to `/var/tmp`
  (or raises) when `/tmp` is out of inodes/space, which is the condition being detected.
- Free inodes use `f_ffree`, free space `f_bavail * f_frsize`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated.
