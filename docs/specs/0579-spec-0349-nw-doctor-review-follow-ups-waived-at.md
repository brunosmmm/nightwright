---
id: SPEC-0579
title: "`nw doctor` follow-ups: close SPEC-0349's review gaps (tests, profiles-dir crash, ssh dir safety, data.dir and retention hints)"
status: proposed
owner: user
created: 2026-11-12
kind: feature
source_idea: IDEA-666
depends_on: [SPEC-0349]
tags: [install, cli, diagnostics, doctor]
---

## Context

[SPEC-0349](./0349-nw-doctor-install-health-check.md) shipped `nw doctor` (`src/nightwright/doctor.py`,
`tests/test_doctor.py`). Its review was waived at 88/90 with no major finding, and left eight
follow-ups, filed as IDEA-666. Four are untested branches or loose assertions; four are small
behavior gaps in checks that already exist:

1. **Untested `tools.gh` warn branch.** `check_gh` (`doctor.py`) warns when `gh --version` fails,
   exits non-zero or times out (`err` branch, fix "reinstall the GitHub CLI"). `test_gh_missing_is_warn`
   only covers "missing" and "ok".
2. **Untested unset `org_ideas_file`.** `check_org_ideas_file` warns when the key is absent or
   empty (`set org_ideas_file: in config.yaml`). `test_org_ideas_file_present_missing_unreadable`
   passes only a set value or `cfg=None`.
3. **Default entry-point detection not exercised.** `check_nw_on_path(entry_points=None)` calls
   `_entry_points()`, which derives the candidates from `sys.argv[0]` (when its name is `nw`) and
   the `nw` beside `sys.executable`. Every test either injects `entry_points=[...]` or monkeypatches
   `_entry_points` away, so that function has no test.
4. **Loose group-heading assertion.** `test_human_output_groups_fix_hints_and_counts` asserts
   `f"\n{group}" in "\n" + out`, which any line that merely *starts* with the group name satisfies
   (a check row `config.timezone` already satisfies `config`), so it cannot tell a missing group
   table from a present one.
5. **An unreadable project profiles dir crashes the check.** `profiles._read_dir_uncached` raises
   `ValueError` for an unreadable `<root>/.nw/profiles` (documented in its docstring).
   `profiles.check_all` calls `load_layers` outside its per-profile `try`, so the `ValueError`
   escapes `check_project_profiles`, and `doctor._safe` reports "check crashed … report this as a
   nightwright bug" instead of the real problem (a permissions error on one directory).
6. **`agentic.workers` lacks `ensure_ssh_dir`'s safety check.** `workers.ensure_ssh_dir` refuses
   (`OSError`) an ssh ControlPath dir that is a symlink or owned by someone else (a shared `/tmp`
   path could be pre-created by another user). `check_workers` only tests that the nearest existing
   parent is a writable directory, so doctor says `ok` for a dir every worker command will then refuse.
7. **`data.dir` hint is wrong for a file or a broken symlink.** `check_data_dir` answers
   `chmod u+w <path>` when the path exists but is not a directory (a regular file), and for a
   dangling symlink (`exists()` is False) answers "does not exist yet … nw creates it", a lie:
   `mkdir` will fail with `FileExistsError`.
8. **`retention.cleanup_period` misreads two inputs.** A non-integer `cleanupPeriodDays` (`"90"`,
   `90.5`, `null`) is silently treated as "unset (Claude default 30)", so the summary names the
   wrong cause. And the settings file is always `~/.claude/settings.json`, though Claude Code reads
   `$CLAUDE_CONFIG_DIR/settings.json` when that variable is set, so doctor can judge retention from
   a file Claude Code is not using.

## Goals / Non-goals

**Goals**
- Items 1–4: add the missing tests and tighten the group-heading assertion in
  `tests/test_doctor.py`; no production change for these.
- Item 5: an unreadable profiles directory (project `.nw/profiles`, or the user-level
  `<config_dir>/profiles` or the builtin dir, which fail the same way) is reported as `fail` on
  `project.<Name>.profiles` with the directory and its OS error taken from the `ValueError` message; `nw profiles check` reports the same error as
  a failed result instead of a traceback.
- Item 6: `agentic.workers` reports a symlinked or foreign-owned ControlPath dir, through one read-only
  predicate shared with `ensure_ssh_dir` (no copied rule).
- Item 7: `data.dir` says what is wrong (not a directory / dangling symlink) and how to fix it.
- Item 8: `retention.cleanup_period` reads settings from `$CLAUDE_CONFIG_DIR` when set, and names a
  non-integer `cleanupPeriodDays` as such.
- Doctor stays read-only and offline; `nw.doctor.v1` is unchanged (ids, statuses, fields).

**Non-goals**
- Changing which checks exist, their ids, the JSON shape, or the exit-code rule.
- Making `skills.install` / `skills.status` honor `CLAUDE_CONFIG_DIR` (a different command family;
  doctor's retention check is the only consumer fixed here).
- Checking ControlPath dirs for `identity` records (`extra=TAG_LEN`), or the dir's mode bits.
- Coercing a string or float `cleanupPeriodDays` into a number: doctor reports it, it does not guess.

## Decision

Fix all eight in one spec, since they share one module and one test file and each is small. Tests
for items 1–4 are added to the existing `tests/test_doctor.py`. For items 5–8, change only the check
functions (and one shared helper in `workers.py` and a `try` in `profiles.check_all`), with these
judgment calls:

- **Item 5:** `profiles.check_all` catches the `ValueError` from `load_layers` and returns a single
  failed result `{"name": "<profiles>", "ok": False, "error": <message>, "layers": True}` instead of
  raising.
  `nw profiles check` and doctor both go through it, so both report the problem. (`profiles.py` already
  documents that `ValueError` as the unreadable-directory signal.)
- **Item 6:** the symlink/ownership rule moves out of `ensure_ssh_dir` into
  `workers.ssh_dir_problem(d) -> str | None` (no writes: `lstat` only); `ensure_ssh_dir` calls it after
  its `mkdir`, doctor calls it before. An unsafe dir is `fail`, not `warn`: every worker ssh call goes
  through `ensure_ssh_dir` and raises, so workers do not run, which is worse than the existing
  "no connection sharing" warn.
- **Item 7:** both are `fail`: neither can be created or written, and the fix differs from `chmod`.
- **Item 8:** `CLAUDE_CONFIG_DIR` set to a non-empty value selects `<it>/settings.json`; unset or
  empty keeps `~/.claude/settings.json`. A non-integer `cleanupPeriodDays` keeps the safe assumption
  (30 days, so the verdict is unchanged) but the summary says "cleanupPeriodDays <repr> is not an
  integer; assuming 30" (only when the file parsed; the existing read/parse-failure note is kept).

## Design

### Tests only (items 1–4), in `tests/test_doctor.py`

- **gh version failure.** `check_gh` with `run_cmd` returning exit 2 → `warn`, summary contains
  "exited 2", `fix == "reinstall the GitHub CLI"`, `detail` is the path; with `run_cmd` raising
  `subprocess.TimeoutExpired` → `warn`, summary contains "over 5 s"; with `run_cmd` raising `OSError`
  → `warn`, summary contains "could not run". All use the existing `Tools` fake or a lambda; no real
  `gh`.
- **Unset `org_ideas_file`.** `check_org_ideas_file({})`, `({"org_ideas_file": ""})` and
  `({"org_ideas_file": None})` → `warn`, summary "org_ideas_file is unset", fix mentions
  `org_ideas_file:`. Also `run()` with a config that lacks the key shows that warn in the check list.
- **Default entry points.** Call `D._entry_points(argv0=...)` and `check_nw_on_path(which=...)` with
  `entry_points` omitted, with `monkeypatch.setattr(D.sys, "executable", …)` pointing at a tmp
  `venv/bin/python` and a tmp `venv/bin/nw`:
  - `argv0` named `nw` → the set contains both `argv0`'s resolved path and the interpreter-adjacent `nw`;
  - `argv0` not named `nw` (`pytest`, `python -m …`, `""`) → only the interpreter-adjacent `nw`;
  - `which("nw")` returning the interpreter-adjacent `nw` (also through a symlink to it) → `ok`;
    returning another file → `warn` whose detail names both paths.
- **Group headings.** Replace `f"\n{group}" in "\n"+out` with: split `out` into lines, and for every
  group in the report assert that a line whose `.strip()` equals the group exists, that the group's
  check ids appear on lines after its heading line and before the next group's heading line, and that
  the group heading order is the order of first appearance in `D.run`. The group set is derived from
  `D.run` output in the test, not hard-coded, so a new group cannot silently lose its table.

### Production changes (items 5–8)

- **`profiles.check_all`** (`src/nightwright/agentic/profiles.py`): wrap `load_layers(...)` in
  `try/except ValueError as e` and return
  `[{"name": "<profiles>", "ok": False, "error": str(e), "layers": True}]`. `load_layers` raises
  `ValueError` only from `_read_dir` on one of three directories (builtin, `<config_dir>/profiles`,
  `<root>/.nw/profiles`), via `_read_dir_uncached`, whose message is `<origin>: <dir>: <strerror>`
  and already names the directory. Callers keep their current handling
  (`bad = [r for r in results if not r["ok"]]`). The row's name `<profiles>` fails `NAME_RE`, so it
  cannot be a real profile name; `check_project_profiles` recognises the row by its `layers` key
  (`len(results) == 1 and results[0].get("layers")`), never by name, so a real profile called
  `profiles` that fails is still reported as an ordinary failed profile.
  - In that case `check_project_profiles` returns `fail`, summary `"profiles dir unreadable"`
    (no path in the summary: the path comes from the error string, which goes in `detail`, e.g.
    `/p/.nw/profiles: <dir>: Permission denied`), fix
    `"fix the permissions of the directory named in the detail, then run nw profiles check --project <Name>"`.
    Other failures keep "N of M profiles do not resolve" and `nw profiles check --project <Name>`.
  - A user-level `<config_dir>/profiles` failure takes the same route and is reported the same way
    on every project's check (the error string says which directory); this is intended, since every
    project resolution would fail with it. It is covered by a test, not declared out of scope.
  - The `nw profiles check` renderer prints the row like any failed profile (`<profiles>` plus the
    error) and exits 1 as before; its output for healthy projects is unchanged.
- **`workers.ssh_dir_problem(d)`** (`src/nightwright/agentic/workers.py`): `try: st = d.lstat()`
  (never `Path.exists()`, which is False for a dangling symlink); on `FileNotFoundError` return `None`;
  otherwise return `None` when `stat.S_ISDIR(st.st_mode)` and `st.st_uid == os.getuid()`, else the
  string `f"{d} is not a directory owned by this user"`. `lstat` does not follow links, so a symlink,
  including a dangling one, is reported. `ensure_ssh_dir` becomes `mkdir`, then
  `if (why := ssh_dir_problem(d)): raise OSError(why)`, then `chmod`. Message text is unchanged.
- **`check_workers`** (`doctor.py`): after resolving `d = workers.ssh_dir(cfg)` and before the
  writable-parent test, `why = workers.ssh_dir_problem(d)`; when set, return `fail`
  "ssh ControlPath dir is unsafe", `detail=why`, `fix=f"remove {d} (a symlink or another user's
  directory) and let nw recreate it, or set XDG_RUNTIME_DIR to a private directory"`. Read-only: no
  `mkdir`, no `chmod`.
- **`check_data_dir`** (`doctor.py`), before the existing `exists()` branch:
  - `p.is_symlink() and not p.exists()` → `fail`, summary `"{p} is a dangling symlink"`,
    `detail` the `os.readlink` target, fix `"remove the symlink or recreate its target, or set
    NW_DATA_DIR to a writable directory"`;
  - `p.exists() and not p.is_dir()` → `fail`, summary `"{p} is not a directory"`, fix `"move {p}
    aside, or set NW_DATA_DIR to a writable directory"`.
  The existing not-writable (`chmod u+w`), absent-and-creatable (`warn`) and cannot-be-created
  branches are unchanged.
- **`check_retention`** (`doctor.py`): new helper `claude_settings_path()` returns
  `Path(os.environ["CLAUDE_CONFIG_DIR"]) / "settings.json"` (after `expanduser`) when that variable
  is non-empty, else `~/.claude/settings.json`; used when `settings_path` is not passed. Parsing
  stays in the existing `try`: the file is parsed into `doc` (`json.loads`), and `raw = doc.get(...)`
  is replaced by key-presence logic, because `.get` returns `None` for both an absent key and an
  explicit `null`:
  - `"cleanupPeriodDays" not in doc` → note stays "cleanupPeriodDays unset (Claude default 30)";
  - key present with an `int` that is not a `bool` → `days = raw`, note `cleanupPeriodDays {raw}` (as today);
  - key present with anything else (`"90"`, `90.5`, `true`, `null`) →
    `note = f"cleanupPeriodDays {raw!r} is not an integer; assuming 30"`, `days` stays 30.
  - The not-an-integer note applies only when the file parsed. The existing `except` branch
    (unreadable / unparseable / not an object: note "… does not read / parse; assuming 30") is kept
    and is never overwritten by the not-an-integer note.
  Every other branch is unchanged; the fix text names the actual `settings_path`.

## Alternatives considered

- **One spec per finding** — eight specs for ~60 lines of code and one test file; the idea's own
  heading treats them as one review follow-up set.
- **Catch the profiles-dir error in `check_project_profiles` only** — `nw profiles check` would still
  print a traceback for the same condition; fixing `check_all` fixes both.
- **A `warn` for the unsafe ssh dir** — matches the existing ControlPath warn, but the real effect
  (`OSError` from `ensure_record_ssh_dir` on every worker run) is a failure.
- **Coerce `"90"` / `90.0` to an int** — Claude Code's own parsing is not ours to mirror; reporting
  the bad type is honest and keeps the verdict conservative.
- **Make skills honor `CLAUDE_CONFIG_DIR` too** — separate behavior change with its own install
  semantics; out of scope here.

## Acceptance criteria

- [ ] `check_gh` returns `warn` with fix "reinstall the GitHub CLI" when `gh --version` exits non-zero,
      times out, or raises `OSError`, and a test covers each.
- [ ] `check_org_ideas_file` returns `warn` "org_ideas_file is unset" for a missing, empty and `None`
      value, and a test covers each.
- [ ] `_entry_points` and `check_nw_on_path` (default `entry_points`) are tested for: argv0 named
      `nw`, argv0 not named `nw`, `which` equal to the interpreter-adjacent `nw` (direct and via
      symlink) → `ok`, and a different `nw` → `warn`.
- [ ] The human-output test asserts each group heading as its own line, in order, with that group's
      check ids between it and the next heading, with the group list derived from `D.run`.
- [ ] With an unreadable `<root>/.nw/profiles`, `check_project_profiles` returns `fail` with summary
      "profiles dir unreadable" and a `detail` that is the `ValueError` message (names the directory and
      the OS error); it is not "check crashed" and `run()` still returns every other check. The same
      holds for an unreadable `<config_dir>/profiles`. `nw profiles check --project <Name>` exits 1
      without a traceback.
- [ ] The synthetic row is `{"name": "<profiles>", "ok": False, "error": ..., "layers": True}`; a real
      profile named `profiles` that fails to resolve is reported as "1 of N profiles do not resolve",
      not as an unreadable directory.
- [ ] `workers.ssh_dir_problem` returns `None` for an absent path (via `lstat` raising
      `FileNotFoundError`) or an own real directory, and a message for a symlink (including a dangling
      one) or a directory owned by another uid; `ensure_ssh_dir` raises `OSError` with the same message
      as before, built from it.
- [ ] `check_workers` returns `fail` "ssh ControlPath dir is unsafe" for a symlinked (including
      dangling) or foreign-owned ControlPath dir, creates nothing, and still returns `ok` for a normal one.
- [ ] `check_data_dir` returns `fail` with a not-a-directory summary and a non-`chmod` fix for a regular
      file, and `fail` with a dangling-symlink summary (target in `detail`) for a broken symlink; the
      existing writable / absent / read-only results are unchanged.
- [ ] With `CLAUDE_CONFIG_DIR` set, `check_retention` reads `$CLAUDE_CONFIG_DIR/settings.json` and
      ignores `~/.claude/settings.json`; unset or empty keeps `~/.claude/settings.json`.
- [ ] A non-integer `cleanupPeriodDays` (`"90"`, `90.5`, `true`, `null`) yields a summary containing
      "is not an integer; assuming 30" and the same verdict as 30 days; an absent key still says "unset"
      (an explicit `null` does not); an unparseable settings file keeps its "does not read / parse;
      assuming 30" note.
- [ ] `nw doctor --json` output still validates against `DOCTOR_SCHEMA`, no check id changed, and
      `test_read_only_and_offline` still passes.

## Test plan

- **Automated tests** (all in the existing `tests/test_doctor.py`, which already carries its one
  module-level marker `pytestmark = pytest.mark.fast`; no new module and no second tier):
  - `test_gh_version_failure_is_warn` (non-zero, timeout, `OSError`) — item 1.
  - `test_org_ideas_file_unset_is_warn` (parametrized `{}`, `""`, `None`, plus through `run()`) — item 2.
  - `test_entry_points_default_detection` and `test_nw_on_path_default_entry_points` (tmp venv layout,
    `monkeypatch.setattr(D.sys, "executable", …)` and `D.sys.argv`) — item 3.
  - Tighten `test_human_output_groups_fix_hints_and_counts` to line-exact, ordered headings derived
    from `D.run` — item 4.
  - `test_project_unreadable_profiles_dir_fails_not_crashes` (chmod 0 on `.nw/profiles`, skipped as
    root like the other permission tests): asserts summary "profiles dir unreadable", detail equal to
    the `ValueError` message, that no other check in `run()` is affected, and that `PR.check_all`
    returns the one `<profiles>` row (with `layers: True`) instead of raising; a variant with chmod 0
    on `<config_dir>/profiles` gives the same result — item 5. `test_profiles_named_profiles_not_misreported`:
    a project profile literally named `profiles` that fails to resolve yields "1 of N profiles do not
    resolve", not the unreadable-dir summary — item 5. Plus
    a CLI case: `CliRunner().invoke(cli, ["profiles", "check", "--project", "Foo"])` exits 1 with no
    traceback (add to `tests/test_doctor.py`, since it exercises the same shared function).
  - `test_workers_ssh_dir_problem` and `test_workers_unsafe_ssh_dir_fails`: symlinked dir, normal dir
    (`ok`), foreign owner via `monkeypatch.setattr(os, "getuid", …)`, and a dangling symlink at the dir
    (`ssh_dir_problem` non-None, `check_workers` `fail`; `Path.exists()` would be False there); assert nothing is created or
    chmodded and that `ensure_ssh_dir` still raises the same `OSError` — item 6.
  - `test_data_dir_file_and_dangling_symlink` — item 7.
  - `test_retention_claude_config_dir_and_non_int` (parametrized `"90"`, `90.5`, `True`, `None` as an
    explicit `null`, an absent key (note stays "unset"), and `CLAUDE_CONFIG_DIR` set / empty / unset with
    distinct values in the two settings files) — item 8. `test_retention_unparseable_settings_keeps_note`:
    a non-JSON `settings.json` under `CLAUDE_CONFIG_DIR` keeps "does not read / parse; assuming 30" —
    item 8.
- **Speed tier:** `tests/test_doctor.py` stays `fast` (SPEC-0490 taxonomy: no TUI/remote/e2e imports).
  `python3 tools/test_tiers.py --check` exits 0.
- **Manual verification:** in a scratch HOME, `chmod 000 <project>/.nw/profiles` and run
  `nw doctor --project <Name>`: expect `✗ project.<Name>.profiles` naming the directory, not "check
  crashed". `ln -s /nonexistent $NW_DATA_DIR` then `nw doctor`: expect `data.dir` fail "dangling
  symlink". Set `CLAUDE_CONFIG_DIR=/tmp/cc` with `{"cleanupPeriodDays": "90"}` in `/tmp/cc/settings.json`:
  expect the retention summary to say it is not an integer.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_doctor.py`, `tests/test_profiles*.py` and the workers tests; `python3 tools/spec_lint.py`
  exits 0. The existing `test_json_validates_against_schema`, `test_clean_environment_has_no_findings`
  and `test_read_only_and_offline` pin the unchanged JSON shape, the clean path, and the
  read-only/offline rule.

## Rollout / migration

No data or config migration. Order: tests for items 1–4 first (they pass against current code, so they
pin existing behavior), then items 5–8 with their tests. A user who has an unsafe ControlPath dir will
now see a doctor `fail` where they used to see `ok`; that is the intent (workers already failed there).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; manual steps performed.
- [ ] `python3 tools/test_tiers.py --check` exits 0.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
