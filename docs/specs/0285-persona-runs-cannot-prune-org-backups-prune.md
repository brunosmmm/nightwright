---
id: SPEC-0285
title: "Persona runs cannot prune org backups; prune failures leave a trace"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-384
tags: [org, storage, agentic, safety, review-follow-up]
---

## Context

This spec comes from the second triage of the accumulated `wtfug:` review follow-ups
(campaigns of SPEC-0253 and later; SPEC-0262..0266 covered the first round). Each finding
kept here was re-checked against `main` on 2026-10-27 and is still true. Both concern the
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
    dir.* `tests/test_idea_capture_guard.py::_snapshot` walks only the org dir. Backups
    live under `<data_dir>/org-backups`, so a refusal that came after the backup copy would
    still pass the test.
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
- The SPEC-0272 refusal test proves that the backup dir is untouched.

**Non-goals**
- Changing retention selection or defaults (SPEC-0271).
- Making `PersonaOrgWriteRefused` stop subclassing `ValueError`.
- Surfacing the prune error in the TUI or in the queue event stream.

## Decision

`prune_backups` refuses in a persona run unless it is a dry run. A failed automatic prune on
the write path records a small marker file in the backup dir. `wt backups prune` reports the
marker, and the next successful prune removes it.

## Design

1. **Refusal** (`src/wt/org_write.py` `prune_backups`): when `dry_run` is false, call
   `refuse_in_persona_run()` before listing or deleting anything. A dry run stays allowed,
   because it only reads. The CLI already turns `PersonaOrgWriteRefused` into a clean
   `ClickException` (`cli.WtRootGroup.invoke`), so `wt backups prune --yes` exits 1 with
   the refusal text. The write path does not change: `_atomic_backup_write` already refuses
   before it copies or prunes.
2. **Trace** (`src/wt/org_write.py` `_atomic_backup_write`): in the `except Exception` around
   `prune_backups`, write `<backup_dir>/.prune-error` as JSON
   `{"at": <iso>, "basename": <file>, "error": "<Type>: <msg>"}`, overwriting any older
   marker. The write is atomic (tmp + `os.replace`), and its own failure is swallowed, so
   the org write never fails because of it. A successful `prune_backups` that is not a dry
   run removes the marker (best-effort, `FileNotFoundError` ignored). The marker does not
   match `BACKUP_RE`, so retention never counts or deletes it.
3. **Report** (`src/wt/cli.py` `backups_prune_cmd`): read the marker before pruning. Text
   output adds one `render.warn` line on stderr:
   `last automatic prune failed at <at> (<basename>): <error>`. The `--json` output gains
   `last_auto_prune_error` (the marker dict or null). A `--yes` run that succeeds removes
   the marker (point 2), so the warning shows once more and then stops.
4. **Test fix** (`tests/test_idea_capture_guard.py` `_snapshot`): also walk
   `OW._backup_dir(cfg)`, so the persona-refusal test fails when a backup copy lands before
   the refusal.

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
- [ ] `wt backups prune` prints the `last automatic prune failed` warning, and `--json` carries
      `last_auto_prune_error`, while the marker exists. A successful `--yes` prune removes it.
- [ ] The marker is never selected for deletion and never counted as a backup.
- [ ] `test_persona_run_refuses_org_writes` snapshots the `org-backups/` dir too, and it
      fails when the refusal in `_atomic_backup_write` is moved after the `shutil.copy2`.

## Test plan

- **Automated tests:**
  - `tests/test_org_backups.py::test_persona_run_cannot_prune_backups`: seed three backups.
    Set `WT_PERSONA_RUN=1` and invoke `wt backups prune --keep-last 0 --keep-days 0 --yes`
    with `CliRunner`. Assert exit 1, the refusal text in the output, and the same dir
    listing. Then run without `--yes` and assert exit 0 and the listing is unchanged.
  - `tests/test_org_backups.py::test_failed_auto_prune_leaves_marker`: monkeypatch
    `OW.prune_backups` to raise `PermissionError("denied")`, then do an org write
    (`set_property`). Assert the write landed and the marker JSON has `basename` equal to
    the org file's basename and `error` containing `PermissionError`.
  - `tests/test_org_backups.py::test_prune_cmd_reports_and_clears_marker`: write a marker.
    `wt backups prune --json` shows `last_auto_prune_error`. Text mode shows the warning on
    stderr. `wt backups prune --yes` removes the marker, and the next run shows nothing.
  - `tests/test_org_backups.py::test_marker_not_a_backup`: with a marker present and
    `--keep-last 0 --keep-days 0`, the dry run's `delete` and `scanned` exclude
    `.prune-error`.
  - `tests/test_idea_capture_guard.py::test_persona_run_refuses_org_writes`: extend
    `_snapshot` to the backup dir.
- **Sabotage checks.** Each one must fail its test; record the result in the spec:
  - remove the `refuse_in_persona_run()` call from `prune_backups`
  - restore the bare `except Exception: pass`
  - skip the marker removal on success
  - move `refuse_in_persona_run()` in `_atomic_backup_write` below `shutil.copy2`
    (`test_persona_run_refuses_org_writes` must fail)
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
