---
id: SPEC-0179
title: "wt spec status: one-line frontmatter status setter"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [specs, cli, agentic]
source_idea: IDEA-025
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-025`). This is the **canary**: the first spec the agentic loop builds
by itself, chosen because it is one small library function, one CLI subcommand and one
test file, and both repo gates are relevant to it.

Nothing in wt sets a spec's `status:` today. Humans and agents edit the frontmatter line by
hand (or `sed`), and the loop's implementer persona is told to flip `in-progress` → `done`
the same way. Three re-serialization styles already exist for frontmatter (`specs.py`
regex on templates, `_write_source_idea` single-line insert, `export._rewrite_frontmatter`
full `yaml.safe_dump`); a status setter must not add a fourth that rewrites the whole block
and churns diffs.

## Goals / Non-goals

**Goals**
- `set_spec_status(path, status, *, today=None) -> (old, new)` in `src/wt/specs.py`: edits
  only the `status:` line and the `updated:` line (inserting `updated:` after `created:`
  when absent). Everything else in the file is byte-identical.
- `wt spec status SPEC_ID [--set STATUS] [--json]`: prints the current status, or sets it
  and prints `old → new`; `--json` emits `wt.spec.status.v1`
  `{"schema", "id", "path", "status", "updated"}`.
- Works for internal `SPEC-NNNN` and outbound ids through `resolve_spec_path`.

**Non-goals**
- Lifecycle validation (e.g. refusing `done` with unchecked ACs). `wt spec verify` and lint
  own that; this command is the pencil, not the judge.
- Regenerating the ledger. The caller (or the repo's gate) runs
  `python3 tools/spec_lint.py --write-ledger`.

## Decision

Add the library function and the subcommand, with the status value restricted to the
schema's enum (`draft, proposed, accepted, in-progress, done, superseded, rejected`) as a
`click.Choice` on the CLI and a `ValueError` in the library.

## Design

- `src/wt/specs.py`:
  - `SPEC_STATUSES = ("draft", "proposed", "accepted", "in-progress", "done", "superseded",
    "rejected")`.
  - `set_spec_status(path, status, *, today=None)`: read text; `split_frontmatter` must
    succeed (else `ValueError("no frontmatter")`); find the single line matching
    `^status:\s*(\S+)\s*$` inside the frontmatter block (else `ValueError`); replace its
    value; for `updated:` replace the value of an existing `^updated:` line, else insert
    `updated: <today>` right after the `created:` line; write the file back with the same
    newline convention; return `(old, status)`. `today` defaults to the local date
    (`YYYY-MM-DD`). Setting the same status still refreshes `updated:`.
  - `spec_status(path) -> dict` returning `{"status", "updated"}` from the frontmatter.
- `src/wt/cli.py` (`spec` group): `@spec_group.command("status")`, argument `spec_id` with
  `shell_complete=C.complete_any_spec_id`, option `--set "new_status"` as
  `click.Choice(SPEC_STATUSES)`, option `--json`. Resolve with `resolve_spec_path(cfg,
  spec_id)`; `ValueError` → `click.ClickException`. Human output: `SPEC-0179  accepted`
  or `SPEC-0179  accepted → in-progress  <path>`.
- `docs/product/reference/index.md`: add `status` to the `wt spec …` nested list.

## Alternatives considered

- **`yaml.safe_dump` the whole frontmatter** — rejected; it reorders/reflows and churns
  diffs on every spec (the reason `_write_source_idea` inserts one line).
- **Validate lifecycle transitions here** — rejected; keep one judge (`verify` + lint).

## Acceptance criteria

- [ ] `set_spec_status` on a fixture spec changes exactly the `status:` and `updated:`
      lines (asserted by a line diff) and returns `(old, new)`; inserts `updated:` after
      `created:` when absent; refreshes `updated:` when setting the same status.
- [ ] Invalid status, missing frontmatter and missing `status:` line each raise
      `ValueError` naming the problem.
- [ ] `wt spec status SPEC_ID` prints the current status; `--set` writes and prints
      `old → new`; `--json` emits `wt.spec.status.v1` with `id, path, status, updated`;
      an unknown id is a `ClickException`; `--set` rejects values outside the enum.
- [ ] Completion guard passes (`spec_id` wired; `--set` is a Choice).
- [ ] `docs/product/reference/index.md` lists `status` under `wt spec …`.
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_spec_status.py`: library cases above on a tmp spec
  copied from `TEMPLATE.md` (with a `created:` line), including the byte-identical rest
  of the file; CLI cases through `CliRunner` with a patched `load_config` and a tmp
  `specs_dir`.
- **Manual verification:** none beyond the suite (the loop's gates run it).
- **Regression guard:** full `uv run pytest`.

## Clock Log

Built by the agentic loop (campaign recorded in SPEC-0177). Human bookkeeping on IDEA-025
happens via `wt spec reconcile --apply` after the merge.

## Rollout / migration

Additive command. No migration.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
