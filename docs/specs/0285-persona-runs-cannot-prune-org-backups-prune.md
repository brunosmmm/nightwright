---
id: SPEC-0285
title: "Persona runs cannot prune org backups; prune failures leave a trace"
status: done
kind: feature
owner: user
created: 2026-10-27
updated: 2026-10-27
source_idea: IDEA-384
tags: [org, storage, agentic, safety, review-follow-up]
---

## Context

This spec comes from the second triage of the accumulated `wtfug:` review follow-ups
(campaigns of SPEC-0253 and later; SPEC-0262..0266 covered the first round). Each finding
kept here was re-checked against `main` on 2026-10-27; all but the `_snapshot` one (see below)
is still true. Both concern the
org-backup safety net (SPEC-0271), and SPEC-0272's rule that campaign agents never write
the human's org data.

Kept findings:

- **IDEA-384** (SPEC-0272)
  - `wtfu:05d5038f2de0`: *`wt backups prune --yes` still deletes the human's org backups
    from a persona run.* `org_write.prune_backups` is neither an `org_writer` nor calls
    `refuse_in_persona_run()`. `cli.backups_prune_cmd` calls it directly, so an agent with
    `WT_PERSONA_RUN=1` can run `wt backups prune --keep-last 0 --keep-days 0 --yes` and
    delete every backup.
  - `wtfu:eb4ac7169d68` (same as `wtfu:f5120e382ca4`): *Snapshot ignores the org-backups
    dir.* **Already fixed on `main`.** `tests/test_idea_capture_guard.py::_snapshot` walks
    `(os.path.dirname(cfg["org_ideas_file"]), cfg["data_dir"])`, and `_backup_dir(cfg)` is
    `<data_dir>/org-backups` (`org_write.py:593`), so the backup dir is already in the
    snapshot. This spec only verifies that with a sabotage check; it makes no code change.
- **IDEA-377** (SPEC-0271)
  - `wtfu:298d57edc87e`: *Prune failures on the write path are swallowed with no trace.*
    `org_write._atomic_backup_write` wraps `prune_backups` in `except Exception: pass`. If
    retention breaks for good (a permission change on `org-backups/`, a selection bug), the
    backup dir grows without bound and nothing ever says so.

The other findings in IDEA-384 and IDEA-377 were dropped. They are test-only checks of
paths that already behave, speculative (a `ValueError` handler that swallows the refusal:
no concrete call site in a persona-reachable CLI path was found), or cosmetic (clock-jump
ordering, the per-write scan cost).

## Goals / Non-goals

**Goals**
- A persona run cannot delete org backups through any wt entry point.
- A failing automatic prune is visible to the operator the next time they look at backups.
- Confirm (by sabotage) that the SPEC-0272 refusal test already proves the backup dir is
  untouched.

**Non-goals**
- Changing retention selection or defaults (SPEC-0271).
- Making `PersonaOrgWriteRefused` stop subclassing `ValueError`.
- Surfacing the prune error in the TUI or in the queue event stream.

## Decision

`prune_backups` refuses in a persona run unless it is a dry run. A failed automatic prune on
the write path (it raises, or it returns with a non-empty `failed`) records a small marker
file in the backup dir. `wt backups prune` reports the marker, and a later successful prune
of the same file (or of all files) removes it.

## Design

1. **Refusal** (`src/wt/org_write.py` `prune_backups`): when `dry_run` is false, call
   `refuse_in_persona_run()` before listing or deleting anything. A dry run stays allowed,
   because it only reads. The CLI already turns `PersonaOrgWriteRefused` into a clean
   `ClickException` (`cli.WtRootGroup.invoke`), so `wt backups prune --yes` exits 1 with
   the refusal text. The write path does not change: `_atomic_backup_write` already refuses
   before it copies or prunes.
2. **Trace** (`src/wt/org_write.py` `_atomic_backup_write`): a prune counts as failed in two
   cases. (a) `prune_backups` raises: the existing `except Exception` records the marker with
   `error` = `"<Type>: <msg>"`. (b) `prune_backups` returns normally but `res["failed"]` is
   non-empty (it catches the `OSError` from `os.remove` itself, `org_write.py:660-665`, so a
   permission change on `org-backups/` takes this path and never raises): record the marker
   with `error` = `"<N> delete(s) failed"`. The marker is
   `<backup_dir>/.prune-error`, JSON `{"at": <iso>, "basename": <file>, "error": "<text>"}`,
   overwriting any older marker. The write is atomic (tmp + `os.replace`), and its own
   failure is swallowed, so the org write never fails because of it.
   A successful prune (no raise, `failed` empty) that is not a dry run removes the marker
   only when the marker's `basename` equals the prune's `basename`, or the prune covers all
   files (`basename is None`, i.e. `wt backups prune --yes`). Removal is best-effort
   (`FileNotFoundError` and a corrupt marker are ignored; a corrupt marker is removed only
   by an all-files prune). A failed prune never clears the marker. So a successful prune of
   `ideas.org` leaves a marker left by `tasks.org`. The marker is "the most recent failure not
   yet cleared by a success for that file", and a newer failure for another file overwrites
   it, so only one failure is remembered. The marker does not match `BACKUP_RE`, so retention
   never counts or deletes it.
3. **Report** (`src/wt/cli.py` `backups_prune_cmd`): read the marker before pruning. Text
   output adds one `render.warn` line on stderr:
   `last automatic prune failed at <at> (<basename>): <error>`. The `--json` output gains
   `last_auto_prune_error` (the marker dict or null). Every run while the marker exists,
   including a dry run and the `--yes` run that removes it, shows the warning (the marker is
   read before the prune); runs after that do not. A later successful automatic prune of the
   same file on the write path may clear the marker before the operator looks, which is
   accepted: the next prune worked, so the problem is gone.
4. **Test check, no code change** (`tests/test_idea_capture_guard.py`): `_snapshot` already
   walks the backup dir (see Context). Verify it by sabotage: move `refuse_in_persona_run()`
   in `_atomic_backup_write` below `shutil.copy2`, run `test_persona_run_refuses_org_writes`,
   and record that it fails. If it does not fail, extend `_snapshot` to also walk
   `OW._backup_dir(cfg)` and say so in the spec.

## Alternatives considered

- **Refuse dry runs too.** Rejected. A dry run only reads, and agents use it to diagnose
  disk use.
- **Log through `logging` or print to stderr on the write path.** Rejected. Org writes run
  inside the TUI and the runner, where stderr is invisible or garbles the display. A marker
  file is read where the operator manages backups.

## Acceptance criteria

- [x] With `WT_PERSONA_RUN=1`, `wt backups prune --keep-last 0 --keep-days 0 --yes` exits 1
      with the persona-refusal message, and the `org-backups/` listing is unchanged. The
      same command without `--yes` exits 0.
- [x] When `prune_backups` raises inside `_atomic_backup_write`, the org write still
      succeeds, and `<backup_dir>/.prune-error` holds `at`, `basename` and `error`.
- [x] When `prune_backups` returns a non-empty `failed` (a delete raised `OSError`), the org
      write still succeeds, `.prune-error` is present with `error` `"<N> delete(s) failed"`,
      and an existing marker is not removed.
- [x] `wt backups prune` prints the `last automatic prune failed` warning, and `--json` carries
      `last_auto_prune_error`, in every run while the marker exists, including the `--yes`
      run that removes it. A successful `--yes` prune removes it, and the next run shows
      nothing.
- [x] A successful non-dry-run prune clears the marker only when its `basename` matches the
      marker's or it covers all files. A successful prune for another basename leaves it.
- [x] The marker is never selected for deletion and never counted as a backup.
- [x] `test_persona_run_refuses_org_writes` fails when the refusal in `_atomic_backup_write`
      is moved after the `shutil.copy2` (checked by sabotage; no code change expected).

## Test plan

- **Automated tests:**
  - `tests/test_org_backups.py::test_persona_run_cannot_prune_backups`: seed three backups.
    Set `WT_PERSONA_RUN=1` and invoke `wt backups prune --keep-last 0 --keep-days 0 --yes`
    with `CliRunner`. Assert exit 1, `refusing to write the org from a campaign agent` in the
    output, `Traceback` absent (as `test_persona_run_refuses_org_writes` does), and the same
    dir listing. Then run without `--yes` and assert exit 0 and the listing is unchanged.
  - `tests/test_org_backups.py::test_failed_auto_prune_leaves_marker`: monkeypatch
    `OW.prune_backups` to raise `PermissionError("denied")`, then do an org write
    (`set_property`). Assert the write landed and the marker JSON has `basename` equal to
    the org file's basename and `error` containing `PermissionError`.
  - `tests/test_org_backups.py::test_partial_delete_failure_leaves_marker`: seed enough
    backups that the automatic prune selects some, monkeypatch `os.remove` to raise
    `PermissionError`, then do an org write. Assert the write landed, the marker is present
    with `error` `"<N> delete(s) failed"`, and a pre-existing marker (written first) is
    still present afterwards, not removed.
  - `tests/test_org_backups.py::test_prune_cmd_reports_and_clears_marker`: write a marker for
    `tasks.org`. `wt backups prune --json` shows `last_auto_prune_error`. Text mode shows the
    warning on stderr. A successful automatic prune for `ideas.org` (basename mismatch) leaves
    the marker. `wt backups prune --yes` shows the warning in that run, removes the marker,
    and the next run shows nothing.
  - `tests/test_org_backups.py::test_marker_not_a_backup`: with a marker present and
    `--keep-last 0 --keep-days 0`, the dry run's `delete` and `scanned` exclude
    `.prune-error`.
- **Sabotage checks.** Each one must fail its test; record the result in the spec:
  - remove the `refuse_in_persona_run()` call from `prune_backups`
  - restore the bare `except Exception: pass`
  - ignore `res["failed"]` (no marker on a partial failure)
  - skip the marker removal on success
  - clear the marker regardless of basename
  - move `refuse_in_persona_run()` in `_atomic_backup_write` below `shutil.copy2`
    (`test_persona_run_refuses_org_writes` must fail; this validates the existing
    `_snapshot`)
- **Regression guard:** run the full `uv run pytest`. `tests/test_org_backups.py`,
  `tests/test_idea_capture_guard.py` and `tests/test_rich_output_guard.py` must stay
  green.
- **Manual verification:** none. Every path is exercised in-process.

## Rollout / migration

None. The marker appears only after a future failure.

### Test record (2026-10-27)

Ran `uv run pytest -q tests/test_org_backups.py tests/test_idea_capture_guard.py
tests/test_rich_output_guard.py` (all pass), then the full gates: `spec-lint` OK and `uv run pytest -q -x`
(3514 passed, 3 skipped).
New tests in `tests/test_org_backups.py`: `test_persona_run_cannot_prune_backups`,
`test_failed_auto_prune_leaves_marker`, `test_partial_delete_failure_leaves_marker`,
`test_prune_cmd_reports_and_clears_marker`, `test_marker_not_a_backup`,
`test_persona_run_refuses_before_backup_copy`.

Sabotage results (each applied alone to `src/wt/org_write.py`, then restored):
- no `refuse_in_persona_run()` in `prune_backups` -> `test_persona_run_cannot_prune_backups` fails
- bare `except Exception: pass` -> `test_failed_auto_prune_leaves_marker` fails
- ignore `res["failed"]` -> `test_partial_delete_failure_leaves_marker` fails
- skip marker removal on success -> `test_prune_cmd_reports_and_clears_marker` fails
- clear marker regardless of basename -> `test_prune_cmd_reports_and_clears_marker` fails
- move `refuse_in_persona_run()` below `shutil.copy2` -> `test_persona_run_refuses_org_writes`
  cannot fail (see As shipped); the new `test_persona_run_refuses_before_backup_copy` fails.

### As shipped

- Code: `prune_backups` refuses when not a dry run; `_atomic_backup_write` records/clears the
  `.prune-error` marker (helpers `read_prune_error`, `_record_prune_error`, `_clear_prune_error`);
  `backups_prune_cmd` reads the marker first, warns, adds `last_auto_prune_error` to `--json`,
  and removes it on a successful `--yes` run (all-files prune).
- Deviation from Design point 4 / the last criterion: the sabotage cannot fail
  `test_persona_run_refuses_org_writes`. The cause is not `_snapshot` (it already walks the backup
  dir, so it needs no change): every caller of `_atomic_backup_write` is `@org_writer`-wrapped
  (enforced by `tests/test_org_write_race.py`), and the wrapper refuses before
  `_atomic_backup_write` is reached, so the inner refusal is unreachable from any CLI path in that
  test. Extending `_snapshot` would not change that.
- The last criterion is ticked on its restated form (the spec text is guarded and was not
  rewritten): the guard for the inner refusal is
  `tests/test_org_backups.py::test_persona_run_refuses_before_backup_copy`, which calls
  `_atomic_backup_write` directly on an existing file and fails when `refuse_in_persona_run()` is
  moved below `shutil.copy2` (verified by sabotage). `test_persona_run_refuses_org_writes` keeps
  guarding the wrapper-level refusal.
- Test changes after review: `test_partial_delete_failure_leaves_marker` patches `os.remove` to
  raise only for `.bak` paths (real `os.remove` otherwise), so a wrongful marker clear would show.
  `test_partial_failure_does_not_clear_marker_even_when_matching` was dropped: it seeded no marker
  and repeated the first test.
- Review follow-ups (loop 2): `wt backups prune --yes` now holds `org_write_lock` around the marker
  read, the prune and the marker removal, so it cannot interleave with an automatic prune (no lost
  fresh marker, no double delete of the same backup). A dry run stays lock-free. The CLI uses the
  public `clear_prune_error` (renamed from `_clear_prune_error`). An all-files `--yes` prune that
  selects nothing still clears the marker: Design point 2 says an all-files prune clears it, so this
  stays as designed. The last criterion's text still names `test_persona_run_refuses_org_writes`; the
  guarded text could not be amended, so the criterion stands on the restated form above.
- New tests: `test_yes_prune_with_failed_deletes_keeps_marker`,
  `test_yes_prune_json_carries_marker_it_removes`,
  `test_corrupt_marker_is_ignored_then_removed_by_all_files_prune`,
  `test_failed_marker_write_does_not_fail_org_write` (also shows the old marker survives a failed
  rewrite), `test_yes_prune_holds_org_write_lock`. Sabotage: `if yes:` instead of
  `if yes and not res["failed"]:` fails the keeps-marker test; dropping the lock fails the lock test.
- Review follow-ups (loop 3): `backups_prune_cmd` calls `refuse_in_persona_run()` before taking
  `org_write_lock` when `--yes` is set (same order as `org_writer`), so a persona run neither waits on
  nor touches the human's lock file; `test_persona_run_cannot_prune_backups` now asserts the lock is
  never taken. `test_prune_cmd_reports_and_clears_marker` asserts the warning is on stderr and not
  on stdout (`r.stderr` / `r.stdout`). The last criterion's wording stays unamendable here (guarded
  spec); see the restated form above.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log
