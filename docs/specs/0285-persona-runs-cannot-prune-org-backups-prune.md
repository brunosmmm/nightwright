---
id: SPEC-0285
title: "Persona runs cannot prune org backups; prune failures leave a trace"
status: accepted
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

- [ ] With `WT_PERSONA_RUN=1`, `wt backups prune --keep-last 0 --keep-days 0 --yes` exits 1
      with the persona-refusal message, and the `org-backups/` listing is unchanged. The
      same command without `--yes` exits 0.
- [ ] When `prune_backups` raises inside `_atomic_backup_write`, the org write still
      succeeds, and `<backup_dir>/.prune-error` holds `at`, `basename` and `error`.
- [ ] When `prune_backups` returns a non-empty `failed` (a delete raised `OSError`), the org
      write still succeeds, `.prune-error` is present with `error` `"<N> delete(s) failed"`,
      and an existing marker is not removed.
- [ ] `wt backups prune` prints the `last automatic prune failed` warning, and `--json` carries
      `last_auto_prune_error`, in every run while the marker exists, including the `--yes`
      run that removes it. A successful `--yes` prune removes it, and the next run shows
      nothing.
- [ ] A successful non-dry-run prune clears the marker only when its `basename` matches the
      marker's or it covers all files. A successful prune for another basename leaves it.
- [ ] The marker is never selected for deletion and never counted as a backup.
- [ ] `test_persona_run_refuses_org_writes` fails when the refusal in `_atomic_backup_write`
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

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- (none)

## Clock Log
