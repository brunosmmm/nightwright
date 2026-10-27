---
id: SPEC-0253
title: "Per-campaign TMPDIR for gates and personas, plus a temp-filesystem admission guard"
status: accepted
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
`env` dict in `personas.py` (RunRequest build, ~line 747), both merged over `os.environ` (gates.py:
`run_gate`; runner.py: `_child_env`). Neither sets a temp dir, so everything lands in the
runner's `/tmp`.

## Goals / Non-goals

**Goals:** (1) campaign child processes write temp files to disk under the campaign dir and the
files are removed when the gate/run ends; (2) the runner stops admitting new work, with a named
watchdog event, before the temp filesystem is exhausted.
**Non-goals:** changing the repo's pytest config; cleaning other users' `/tmp` content; a
global GC daemon.

## Decision

1. Add `store.campaign_tmp(cfg, cid) -> Path` = `campaign_dir(cfg, cid) / "tmp"` (created on
   demand). `gates.gate_env` and the persona RunRequest env add `TMPDIR`, `TEMP`, `TMP` pointing
   at it. pytest's default basetemp derives from `tempfile.gettempdir()`, so no `--basetemp`
   flag (and no gate-command edit) is needed.
2. After every `gates.run_all` and after every persona run ends (success, failure, interrupt),
   the contents of that directory are removed (`shutil.rmtree(..., ignore_errors=True)` then
   recreate empty); a final removal happens when the campaign reaches a terminal state or is
   abandoned. A residual from a killed runner is bounded to one campaign and is swept at the next
   gate/run start of that campaign.
3. New helper `events.temp_pressure(cfg) -> dict | None` does `os.statvfs` on each distinct
   filesystem among `tempfile.gettempdir()` and `store.agentic_dir(cfg)`, and returns the first
   with free bytes < `tmp_min_free_mb` (default 512) or free inodes < `tmp_min_free_inodes_pct`
   percent of total (default 10; skipped when `f_files == 0`, e.g. btrfs). Both settings live in
   `events.DEFAULTS` / `agentic.events` and use the existing `settings()` sanitiser.
4. `queue._tick` step 1 skips admission (the `pick_admissions` loop; running campaigns continue,
   QUEUED items stay QUEUED) while `temp_pressure` is non-None, and records a note in the tick
   report (`admission_paused: temp_pressure`).
5. `events.watchdog_checks` emits `("temp_pressure:<mount path>", "temp_pressure", None,
   {path, free_mb, free_inodes, free_inodes_pct, thresholds})` while the condition holds; it
   clears itself like the other keyed conditions.
6. `skills/wt-conduct/SKILL.md` gets a `temp_pressure` table row: admission is paused, not
   failed; find the consumer (`df -i`, `du`/`find ... | wc -l` on the path), remove stale
   `pytest-of-*` and finished-campaign `tmp/` dirs (never active campaigns' dirs), confirm the
   event clears, no human escalation unless the space cannot be freed.

## Design

Files: `src/wt/agentic/store.py` (`campaign_tmp`, `clear_campaign_tmp`), `gates.py` (`gate_env`,
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

- [ ] `gate_env` and persona RunRequest env set `TMPDIR`/`TEMP`/`TMP` to `<campaign_dir>/tmp`.
- [ ] That directory is emptied after each gate run and each persona run, including failures.
- [ ] While `/tmp` or the agentic dir's filesystem is under either threshold, `_tick` admits no
      QUEUED item; in-flight campaigns are unaffected; admission resumes when pressure clears.
- [ ] `watchdog_checks` emits `temp_pressure` while pressure holds and not otherwise.
- [ ] `wt-conduct` SKILL.md has a `temp_pressure` row.
- [ ] Thresholds are configurable and bad values fall back to defaults.

## Test plan

Automated (`tests/test_agentic_tmp_isolation.py`, fake runner as in `test_agentic_queue.py`):
- `gate_env` returns TMPDIR under the campaign dir; a gate `cmd` of `touch "$TMPDIR/x"; echo
  $TMPDIR` run through `run_all` leaves the dir empty afterwards and the log shows the path.
- A fake persona run writes a file into `$TMPDIR`; after the apply the dir is empty, also when the
  run result is an error.
- Monkeypatch `os.statvfs` to report low free blocks, then low free inodes: `temp_pressure`
  returns the path; with `f_files == 0` it returns None; `_tick` with a QUEUED ready item admits
  nothing and the item stays QUEUED; restoring normal values admits it on the next tick.
- `watchdog_checks` includes a `temp_pressure` key under pressure and omits it otherwise;
  `settings()` rejects a negative/bool threshold.
- A test asserting the SKILL.md table contains `temp_pressure`.
- **Sabotage check:** drop the TMPDIR export from `gate_env` (and separately the admission guard)
  and confirm the matching tests fail; restore.
Regression: full `uv run pytest`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`).
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated.
