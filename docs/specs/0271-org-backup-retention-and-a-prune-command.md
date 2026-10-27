---
id: SPEC-0271
title: "Org backup retention and a prune command"
status: done
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-326
tags: [org, storage, cli]
---

## Context

Every wt write to an org file goes through
`src/wt/org_write.py:_atomic_backup_write(cfg, path, content)`. Before the atomic replace, it
copies the whole original to `<data_dir>/org-backups/<basename>.<YYYYmmddTHHMMSS>.bak`
(`shutil.copy2`). Nothing ever deletes a backup. `set_property`, `set_state`, `append_log`
and the archive moves each make one backup per call, so a migration that touches 198 ideas
writes hundreds of full copies of the ideas file.

On 2026-10-27, `~/.local/share/wt/org-backups` held **1918 files (176 MB)**: 807
`ideas-archive.org`, 785 `ideas.org`, 260 `ideas-20261025-2.org` and 66 `ideas-20261025.org`.
The growth comes mostly from `wt ideas group-followups --yes` (SPEC-0239) and from the
follow-up filer, which runs on every close. Two backups made in the same second share a name,
and the later one overwrites the earlier one. That is harmless.

IDEA-326 also proposes a batch or transaction API for `org_write` (one lock, one load, one
backup). That is a separate, larger change to every writer, so it is a non-goal here.
Retention caps the disk use whatever the write pattern.

## Goals / Non-goals

**Goals**
- Bounded backups. For each backed-up file (keyed by basename), keep the newest
  `keep_last` backups plus the newest backup of each calendar day within the last
  `keep_days` days. Delete the rest.
- Retention runs on every backup write, for that file's backups only.
- `wt backups prune` applies the same retention to the whole directory at once. It is a dry
  run by default and deletes only with `--yes`. `--keep-last` / `--keep-days` override the
  config for that run.
- The retention counts can be set in the user config.

**Non-goals**
- A batch or transaction write API (the other half of IDEA-326; it stays on the idea).
- Compressing backups, or a restore command.
- Backups of `config.yaml` (`rules.py`, `outbox_migrate.py`), which are another mechanism and
  rare.

## Decision

- New config key, `org_backups: {keep_last: 50, keep_days: 14}`, in
  `src/wt/config.py:DEFAULT_CONFIG`. A non-integer or negative value falls back to its default.
  `keep_last: 0` together with `keep_days: 0` is allowed and means "keep nothing": that is the
  user's call.
- A new pure function, `org_write.select_backup_prunes(names, *, keep_last, keep_days, today)
  -> list[str]`. It groups by basename, which is everything before the final
  `.<ts>.bak`, and parses `ts` with `%Y%m%dT%H%M%S`. Within each group it keeps the newest
  `keep_last`, plus the newest of each date in `[today - keep_days + 1, today]`. It returns
  the rest. A file whose name does not match the pattern is **never** selected.
- `_atomic_backup_write` calls `prune_backups(cfg, basename=os.path.basename(path))` after a
  successful `copy2`. The prune is best-effort: an `OSError` on a delete is ignored, and a
  failed prune never fails the write.
- `prune_backups(cfg, *, basename=None, keep_last=None, keep_days=None, dry_run=False) ->
  dict` lists the directory, calls `select_backup_prunes`, and deletes unless `dry_run`. It
  returns `{"dir", "scanned", "delete": [names], "kept", "bytes"}`.
- CLI: a new top-level group, `wt backups`, with one command, `prune [--keep-last N]
  [--keep-days D] [--yes] [--json]`. Without `--yes` it prints `would delete X of Y backups
  (Z MB); kept K` and a per-basename table, and exits 0 without deleting anything. With
  `--yes` it deletes and prints `deleted X …`.

## Design

- **`src/wt/org_write.py`**:
  - `BACKUP_RE = re.compile(r"^(?P<base>.+)\.(?P<ts>\d{8}T\d{6})\.bak$")`.
  - `_backup_dir(cfg)` returns `os.path.join(cfg["data_dir"], "org-backups")`. The existing
    inline join in `_atomic_backup_write` changes to use it.
  - `_retention(cfg)` reads `cfg.get("org_backups")` and applies the defaults and the
    validation.
  - `select_backup_prunes` and `prune_backups` as in Decision. Byte counts come from
    `os.stat` of the selected files, and only in `prune_backups`.
  - `_atomic_backup_write`: after `shutil.copy2(...)`, `try: prune_backups(cfg,
    basename=os.path.basename(path)) except OSError: pass`.
- **`src/wt/config.py:DEFAULT_CONFIG`**: `"org_backups": {"keep_last": 50, "keep_days": 14}`,
  with a comment that cites SPEC-0271.
- **`src/wt/cli.py`**: `@cli.group("backups")` and `@backups.command("prune")`, which call
  `org_write.prune_backups(cfg, keep_last=…, keep_days=…, dry_run=not yes)`. Output follows
  the existing dry-run convention: `wt queue add` without `--yes` previews.
- **`docs/product/reference/index.md`**: add `wt backups` to the top-level command list. This
  is required by `tests/test_docs_product_reference.py`.

## Alternatives considered

- **Age-only retention (delete anything older than N days).** Rejected. A single burst, like
  the 785 `ideas.org` copies in one evening, would still keep hundreds of copies for N days.
- **Global keep-last across all files.** Rejected. A burst on `ideas-archive.org` would push
  out every backup of `ideas.org`.
- **Prune only through the command.** Rejected. The directory grows back between runs, and
  nobody remembers to run it.

## Acceptance criteria

- [x] `select_backup_prunes` keeps the newest `keep_last` per basename plus the newest per day
      within `keep_days`. It never selects a name that does not match `BACKUP_RE`. Different
      basenames are independent.
- [x] After any `org_write` write, the backup directory holds at most `keep_last + keep_days`
      backups of that file. With 60 existing same-day backups and the defaults, a write leaves
      50.
- [x] A prune failure (a delete that raises `OSError`) does not fail the write, and the write
      still lands atomically.
- [x] `wt backups prune` without `--yes` deletes nothing and reports the would-delete count and
      bytes. With `--yes` it deletes exactly that set. `--keep-last` / `--keep-days` override
      the config.
- [x] The `org_backups` config is honoured, and invalid values fall back to the defaults.
      `wt backups` is listed in `docs/product/reference/index.md`.

## Test plan

- **Automated tests** (`tests/test_org_backups.py`, new):
  - `test_select_keeps_last_n_and_one_per_day`. Synthetic names over 20 days, several per day,
    and two basenames. Assert the exact kept set for `keep_last=3, keep_days=5`, with a fixed
    `today`.
  - `test_select_ignores_foreign_files`: `notes.txt` and `ideas.org.bak` are never selected.
  - `test_write_prunes_that_files_backups`. Seed 60 `ideas.org.<ts>.bak` files in a tmp
    `data_dir`, then run `org_write.set_property` on a tmp ideas file. Assert 50 remain, plus
    whichever per-day extras the rule keeps (for same-day seeds, exactly 50). A seeded
    `other.org.<ts>.bak` is untouched.
  - `test_prune_failure_does_not_fail_write`: monkeypatch `os.remove` to raise `OSError`. The
    write succeeds and the file content is new.
  - `test_cli_prune_dry_run_then_yes`: `CliRunner` runs `wt backups prune`; files are
    unchanged and the output has `would delete`. Then `--yes --keep-last 1 --keep-days 0`
    leaves one per basename.
  - `test_retention_config_fallback`: `org_backups: {keep_last: "x", keep_days: -1}` → (50,
    14).
- **Sabotage check:** make `select_backup_prunes` ignore the `base` grouping (one global
    group). `test_select_keeps_last_n_and_one_per_day` and the `other.org` assertion must
    fail. Remove the `prune_backups` call from `_atomic_backup_write`.
    `test_write_prunes_that_files_backups` must fail.
- **Manual verification:** `uv run wt backups prune` on the real data dir reports about 1900
    would-delete files and deletes nothing (`ls | wc -l` is unchanged). Record the numbers in
    As shipped. The `--yes` run is left to the human, because it is their data.
- **Regression guard:** the full `uv run pytest`, in particular the `org_write` tests and
    `tests/test_docs_product_reference.py`.

### Test record (2026-10-27)

- `uv run pytest -q tests/test_org_backups.py`: 8 passed. These are the six planned tests,
  plus `test_prune_listdir_failure_does_not_fail_write` (an `OSError` from `os.listdir` does
  not fail the write either) and `test_cli_prune_honours_config` (`org_backups.keep_last: 5`
  in `config.yaml` drives the dry-run count).
- Sabotage 1 (one global group in `select_backup_prunes`) fails
  `test_select_keeps_last_n_and_one_per_day`, `test_write_prunes_that_files_backups` (the
  `ideas.org`/`other.org` counts) and `test_cli_prune_dry_run_then_yes`. Sabotage 2 (the
  `prune_backups` call removed from `_atomic_backup_write`) fails
  `test_write_prunes_that_files_backups`. Both were reverted, and the tests pass again.
- Manual: `uv run wt backups prune` on the real data dir printed `would delete 1735 of 1937
  backups (158.3 MB); kept 202`. Per basename: 749 `ideas.org`, 760 `ideas-archive.org`, 210
  `ideas-20261025-2.org`, 16 `ideas-20261025.org`. `ls | wc -l` was 1937 before and after, so
  nothing was deleted. The `--yes` run is left to the human.
- Gates: `python3 tools/spec_lint.py --write-ledger && python3 tools/spec_lint.py` exits 0.
  `uv run pytest -q -x` passes in full: 3318 passed, 3 skipped (13m29s).

### As shipped

- `prune_backups(cfg, basename=…)` runs `select_backup_prunes` over every backup in the
  directory and then keeps only `basename`'s prunes. Filtering first would select the same
  names, because selection is per basename. Selecting first means the per-basename grouping is
  what protects other files, which is what sabotage 1 checks. The returned dict also carries
  `failed`, the deletes that raised `OSError`, which are skipped (best-effort). `wt backups
  prune --yes` reports them. A failing `os.listdir` still raises; `_atomic_backup_write`
  catches it.
- `wt backups prune` prints the per-basename table as the would-delete (or deleted) count per
  file. `--json` prints the `prune_backups` dict plus `dry_run`. Because `--json` uses a raw
  `print`, it is allowlisted in `tests/test_rich_output_guard.py` like the other JSON
  emitters.
- The CLI tests patch `wt.cli.load_config` with a real `load_config()` of tmp dirs.
  `tests/test_agentic_queue_cli.py::test_queue_help_lists_every_command` leaks an unrestored
  `MonkeyPatch` of `wt.cli.load_config` (`data_dir: /nonexistent`), and that leak would
  otherwise redirect env-only CLI tests that run after it.

## Rollout / migration

The first write after the upgrade prunes only the file it writes. The rest waits for `wt
backups prune --yes`, which the human runs once.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
