---
id: SPEC-0329
title: "Deflake the load-dependent tests: condition waits, deterministic sync, and two real races in production code"
status: done
owner: user
created: 2026-10-31
updated: 2026-10-31
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-553
tags: [tests, flaky, agentic, tui, org]
depends_on: [SPEC-0269]
---

## Context

On 2026-10-31, IDEA-553 listed about 17 tests that each failed in the pytest gate of two or
more different campaigns. Because the failures span unrelated campaigns, the cause is the
environment (host load, gate TMPDIRs), not any one change. IDEA-511, IDEA-512 and IDEA-537
are single-test reports that belong to the same set. The human's rule (2026-10-31): "if there
are load flakes then the tests are implemented wrong". SPEC-0269 already set the repo rule:
wait for a condition with `tests/waits.py`, never sleep for a fixed time before a positive
assertion.

The real failure output is in `~/.local/share/wt/agentic/campaigns/*/gates/*-pytest.log`.
Reading it showed three kinds of cause:

1. **Timing races in the test.** A fixed `threading.Timer`, a fixed `sleep`, or a
   wall-clock bound raced a real subprocess or worker thread.
2. **Assertions made before a worker thread had recorded its start.**
3. **Two real production races.** Load or filesystem behaviour made them show up as test
   flakes:
   - **Directory scan versus atomic rename.** `wt.org._iter_org_paths` expands a configured
     org *directory* with `rglob`. On btrfs, a directory scan that runs while another process
     `os.replace`s `ideas.org` misses the file in about 2% of scans (measured: 783 misses in
     35,483 scans; 0 on tmpfs). `load_tasks` then sees no ideas, and the result is
     "no task matched 'IDEA-016'". Gates put TMPDIR under `~/.local/share` (btrfs), which is
     why only gates failed. A user whose `org_files` names a directory is exposed in the same
     way whenever two wt sessions write at once.
   - **The TUI poll during teardown.** The ideas desk's 1.5 s external-change poll
     (`IdeaDesk._check_external_changes`) could fire while the app was tearing its DOM down.
     It then raised `NoMatches: '#list'` and crashed the app.

## Goals / Non-goals

**Goals**
- Every target test passes under CPU pressure, with the real cause fixed, not hidden.
- No assertion is weakened. No test is skipped or marked xfail.

**Non-goals**
- Raising timeouts.
- Fixing `threading.Timer` uses in tests that never failed (see Open questions).

## Decision

Find the real cause of each target from its gate log. Fix it in one of these ways, in this
order of preference:

1. Deterministic synchronisation: fire the stop from inside the run, hold a run with a
   release file, or count loop steps instead of seconds.
2. A `waits.until` condition wait.
3. A minimal production fix, where the flake was a real bug.

## Design

| Test | Cause (from gate logs) | Fix | Before → after (8 concurrent, 2 rounds) |
|---|---|---|---|
| `test_agentic_resolver::test_interrupted_resolver_not_counted` (IDEA-512) | A `Timer(0.15, stop.set)` raced the dispatch. Under load the stop was set before the tick admitted the resolver, so no run started and there was no `run.end` | The stop is set from inside the resolver's `runner.run`, so it always lands mid-run. The `sleep_s` in the script is gone | 2/16 → 16/16 |
| `test_agentic_adopt::test_adopt_orphan_group_waited_and_killed` (IDEA-511) | `timeout_s=3` counts from the run's `started_at`. Under load, `start_impl` plus the orphan wait used up the 3 s, so the group was killed during the "nothing happens" checks | The waiting thread re-reads `request.json` `started_at` on each poll. The test sets it an hour ahead for the idle checks, then 10 s back to make the timeout due | 4/16 → 16/16 |
| `test_agentic_adopt::test_stop_after_detach_leaves_runs` | The code change was flagged once `child.json` existed, but the fake `claude` had not yet written `child.pid`. The detach happened first, and reading `child.pid` raised `FileNotFoundError` | The `on_tick` condition also requires `child.pid`, as in the sibling `test_reexec_does_not_wait_for_persona_run` | 10/16 → 16/16 |
| `test_agentic_queue::test_tick_defers_gate_steps_around_exclusive_regate` | The fake gate appends to `started` on its worker thread. The test asserted right after `Q.tick` submitted it | `waits.until` on each positive `started`/`gate_starts` assertion. The negative assertions stay immediate, since nothing was submitted | 13/16 → 16/16 |
| `test_agentic_nonblocking::test_sigterm_mid_flight_after_several_ticks` | `sleep(1.5)` was meant to allow more than 3 ticks. Under load it allowed 3. A `sleep_s=4` run had to stay in flight across all of it | The run is held by a `pre_shell` wait on a release file. The test waits until the lease's `ticks` exceeds 3, sends SIGTERM, waits for the lease's `draining`, then releases. **Production seam:** the runner publishes `ticks` in the lease info (`lease.LOOP_KEYS`), next to `tick_at` and `last_tick_s` | 15/16 → 16/16 |
| `test_agentic_exit_paths::test_draining_ticks_admit_exec_promptly_and_new_process_adopts` | Wall-clock bound `exec - harvest < 0.5 s` | The assertion counts loop steps instead: no tick and no drain `wait_any` between the gate's harvest and the exec. This is stricter than the old 0.5 s bound: it means the exec came at the very check that harvested | 16/16 → 16/16 (gate failures on 2026-10-31) |
| `test_tui_real_terminal` (both tests; IDEA-537) | Fixed `settle=6 s` and `per_key=1.6 s` pumps. Under load the tree had not populated ("▶ ideas" only), so the indent check measured `Alpha` in the detail pane | `_render` takes `(key, ready)` steps. It pumps the pty until the desk has loaded and then until each key's effect is on the left pane (bounded by `WT_TEST_WAIT_S`). `column_of` now looks only at the left pane | tree 9/16, colour 15/16 → 16/16, 16/16 |
| `test_org_write_race::test_concurrent_processes_lose_nothing_and_never_reuse_an_id` | The btrfs directory scan missed `ideas.org` during a concurrent atomic rename (see Context) | **Production fix:** `_iter_org_paths` adds wt's own files (ideas, capture and ideas-archive) under a configured directory by name, deduped by realpath and kept in the same order. New regression test `test_a_directory_scan_that_misses_the_ideas_file_still_loads_it` (fails without the fix) | on btrfs TMPDIR: 17/160 fail → 0/160 (test body, 8 concurrent processes × 20 runs) |
| `test_tui_hub::test_hub_reflects_changes_made_since_launch` | `NoMatches: '#list'` from `_check_external_changes` → `selected_id`, called by the 1.5 s poll during teardown | **Production fix:** the poll returns on `NoMatches`. This is the same guard SPEC-0269 added to `EventsView._side_async`. The test's positive row-count checks use `pilot_until`. New regression test `test_external_change_poll_during_teardown_is_a_no_op`: it calls the poll after the app has exited and fails without the fix, with the exact gate error | 16/16 → 16/16 (gate failures through 2026-10-30) |
| `test_export::test_internal_spec_lint_unaffected_by_outbox` | The ledger was stale at pytest time, because the gate ran pytest before the spec-lint gate regenerated it | Already fixed by dbedf2f (2026-10-26: spec-lint gate runs first). No change | 16/16 |
| `test_agentic_adopt::test_adopt_after_park_resume_while_no_runner`, `test_adopt_unknown_persona_collected_with_note`, `test_adopt_review_group_applied_once` | Counted every `run.end`, including those of a fresh step that a tick starts at once | Already fixed by eecc0b3 (2026-10-26: count the adopted run's own `run.end`). No change | 16/16 each |
| `test_agentic_adopt::test_fanout_detach_precondition` | Fixed polling | Already fixed by 123146f (2026-10-30). No change | 16/16 |
| `test_agentic_runproc::test_vanished_when_group_gone` | The wrapper sometimes saw `orphan_group` first | Already fixed in SPEC-0269 (SIGSTOP the wrapper). No change | 16/16 |
| `test_agentic_profiles::test_short_origin_forms` | tmp_path was under `$HOME` (gate TMPDIR), so the path was shortened to `~` | Already fixed by 6d10427 (fake sibling HOME). No change | 16/16 |
| `test_export_schemes::test_wt_native_is_default_and_matches_spec0015_shape` | Timestamp in a byte-for-byte compare | Already fixed by dcb2bdc (2026-10-30). No change | 16/16 |
| `test_tui_events_tab::test_a_malformed_record_does_not_break_the_stream` | `NoMatches` in the `events-side` worker during teardown | Already fixed by 14842bf (SPEC-0269). No change | 16/16 |

The "before" counts come from 8 concurrent `uv run pytest` processes × 2 rounds over all
targets, on a host already at load average ~10–15. A target that passed 16/16 before was
judged from its last gate failure and the commit that fixed it. The "after" counts come from
the same harness after the fixes.

## Alternatives considered

- **Raise the timer delays and timeouts** — this only moves the race. The human ruled it out.
- **A FakeRunner "wait for file" option** — a `pre_shell` loop already holds a run, so no
  production change was needed for that.
- **Re-scan the directory twice and union the results** — it lowers the miss rate but does
  not remove it. Adding wt's own files by name is deterministic.
- **A `threading.Timer(<literal>` guard in `tests/test_test_waits.py`** — not added. Five
  `threading.Timer` uses that never failed remain in `test_agentic_exit_paths.py` and
  `test_agentic_adopt.py`, so the guard would not be free of false positives.

## Acceptance criteria

- [x] Each target in IDEA-553 that still failed under pressure now passes 16/16 under 8-way
      concurrency. The rest are shown as already fixed, with the commit.
- [x] The two production races are fixed, each with a regression test that fails without
      the fix.
- [x] No assertion is weakened and no test is skipped or marked xfail.
- [x] The full suite passes.

## Test plan

- **Automated tests:** the target tests listed above, plus the new
  `tests/test_org_write_race.py::test_a_directory_scan_that_misses_the_ideas_file_still_loads_it`
  and `tests/test_tui_hub.py::test_external_change_poll_during_teardown_is_a_no_op`. Each new
  test was checked to fail with its production change reverted.
- **Manual verification:** the stress harness runs 8 concurrent `uv run pytest -q <ids>`
  processes × 2 rounds, before and after the fixes. The org race body ran 8 × 20 times with
  TMPDIR on btrfs (`~/.cache`).
- **Regression guard:** the full suite, under the shared lock:
  `flock /tmp/claude-1000/wt-fullsuite.lock uv run pytest -q -n 2`.

## Rollout / migration

None. The changes are tests plus two small production fixes, and a new lease info key
(`ticks`) that older readers ignore.

## As shipped

Landed directly on main at the human's request (2026-10-31), not through a queue campaign.
Full suite before landing: `flock /tmp/claude-1000/wt-fullsuite.lock uv run pytest -q -n 2`: 4327 passed, 3 skipped (11m53s). While this was in flight, main landed its own fixes for the two adopt tests (6ebb8bb, a3204b8; IDEA-509). On rebase main's versions were kept for those two tests (the same fixes: wait for `child.pid`, move `started_at` to make the timeout due).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- Five other `threading.Timer(<literal>)` uses remain in `tests/test_agentic_exit_paths.py`
  and `tests/test_agentic_adopt.py`. None has failed in a gate. Making them deterministic
  would let `tests/test_test_waits.py` ban the pattern.
