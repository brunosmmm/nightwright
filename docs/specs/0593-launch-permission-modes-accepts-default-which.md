---
id: SPEC-0593
title: Validate launch permission modes against the current claude CLI choices (add manual, retire default)
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
kind: feature
tags: [agentic, launch, claude-cli]
depends_on: [SPEC-0424]
source_idea: IDEA-945
---

## Context

`claude --permission-mode` lists its accepted choices in `--help`. On claude 2.1.322 they are
`acceptEdits, auto, bypassPermissions, manual, dontAsk, plan`; `default` is no longer listed and
`manual` is its new name. An unlisted value is rejected by the CLI's choice check (`claude
--permission-mode bogus` exits with "argument 'bogus' is invalid. Allowed choices are …"); on 2.1.322
`default` still happens to be accepted, and `manual` is accepted too, but an unlisted value is not a
contract and a later release may drop it.

nightwright keeps three hand-written copies of the old list:

- `src/nightwright/agentic/launch.py:224` `PERMISSION_MODES = ("default", "acceptEdits", "plan",
  "auto", "dontAsk", "bypassPermissions")`. `_good` (`launch.py:267`) validates
  `conductor_permission_mode` / `steward_permission_mode` against it ([SPEC-0382], [SPEC-0424]),
  and `build_plan` (`launch.py:388`) passes the cleaned value to `--permission-mode`. A configured
  `default` is therefore accepted by nightwright and handed to the CLI as-is. `manual`, the value
  the CLI now advertises, is rejected by nightwright and silently replaced by the `dontAsk` fallback.
- `src/nightwright/agentic/profiles.py:59` `PERMISSION_MODES = ("default", "acceptEdits",
  "bypassPermissions", "dontAsk", "plan")` validates persona `permission_mode`; the value reaches
  `--permission-mode` at `src/nightwright/agentic/runner.py:204`. Same drift: `default` passes,
  `manual` and `auto` fail validation.
- `src/nightwright/agentic/project.py:25` lists only `bypassPermissions, dontAsk, acceptEdits`, all
  still valid; it is not affected.

The drift was found while verifying [SPEC-0424](./0424-launch-freshness-and-steward-dontask.md)
(its "side findings" note).

## Goals / Non-goals

**Goals**
- One list of the permission modes the current claude CLI accepts, used by both `launch.py` and
  `profiles.py`, containing `manual` and not `default`.
- A configured `default` keeps working: it is accepted as a legacy alias of `manual` and the value
  that reaches `claude --permission-mode` is always one of the CLI's listed choices.
- Every `--permission-mode` argv built by nightwright carries a listed choice, whatever the config
  held.

**Non-goals**
- Probing the installed `claude` at runtime for its choices (a `--help` parse per launch, or a cached
  probe). The list is static and updated when the CLI changes; see Alternatives.
- Changing any default mode (`dontAsk` stays the default for conductor, steward and the cheap
  personas) or any other launch setting.
- Changing `project.py`'s narrower `agents.permission_mode` set.

## Decision

Replace the hard-coded lists with one shared constant, `CLAUDE_PERMISSION_MODES = ("manual",
"acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")`, the choices of claude 2.1.322, and
a legacy-alias map `{"default": "manual"}`. Validation accepts the constant plus the alias keys and
returns the canonical value, so `default` is rewritten to `manual` when settings are read and no
unlisted value can reach the CLI. An invalid value (`yolo`, `3`, `""`) keeps today's behaviour in
launch settings: fall back to the setting's default (`dontAsk`); in persona profiles it stays a
validation error.

## Design

1. **Shared definition.** Add `CLAUDE_PERMISSION_MODES`, `LEGACY_PERMISSION_MODES = {"default":
   "manual"}` and `canonical_permission_mode(v)` (returns the canonical mode for a listed mode or a
   legacy alias, `None` for anything else, including non-strings and `bool`) to a module both
   `launch.py` and `profiles.py` can import without a cycle. `runner.py` (the builder of the
   argv at `runner.py:204`) is the home unless it would create an import cycle, in which case the
   implementer picks the leaf module and records it in "As shipped". A comment names the claude
   version the list was taken from.
2. **launch.py.** Delete the local `PERMISSION_MODES` (or alias it to the shared constant, so
   `launch.PERMISSION_MODES` keeps existing for callers and tests) and make `_good` for the two
   `*_permission_mode` keys return `canonical_permission_mode(v)`. `DEFAULTS` stay `dontAsk`.
   `build_plan` therefore always puts a listed choice after `--permission-mode`, and the launch
   record's `permission_mode` field shows the canonical value (`manual` for a configured
   `default`).
3. **profiles.py.** `PERMISSION_MODES` becomes the shared constant plus the alias keys, so the
   validation message lists `manual` and `auto`, and `default` still validates. Persona settings
   are not rewritten in the resolved profile (the file stays as the user wrote it); the rewrite
   happens where the argv is built.
4. **runner.py:204.** Build `--permission-mode` from `canonical_permission_mode(req.permission_mode)`,
   falling back to the raw value only when it is empty (unchanged behaviour for requests that carry
   no mode), so a profile that says `default` launches the run with `manual`.
5. **Docs.** Wherever the permission-mode list is documented for users (the launch settings
   comment/help in `launch.py` and the profile docs, found by grepping `bypassPermissions`), update
   the list to the new choices and note that `default` is a deprecated alias of `manual`.
6. **Keeping it honest.** A test compares the constant to the installed `claude --help` choices when
   a `claude` binary is on `PATH` (skipped otherwise), so the next CLI change shows up as a test
   failure rather than a rejected launch.

## Alternatives considered

- **Parse the choices from `claude --help` at launch time** — always right for the installed
  version, but adds a subprocess per launch, a new failure mode when `--help` format changes, and
  makes config validation depend on the machine; rejected in favour of a static list plus a test
  that detects drift.
- **Only add `manual` and keep `default` valid unchanged** — leaves an unlisted value flowing to the
  CLI, the exact risk in the idea; rejected.
- **Drop `default` outright** — silently changes a configured `conductor_permission_mode: default`
  to the `dontAsk` fallback, which is a different (non-interactive) behaviour from what the user
  asked for; mapping it to `manual` preserves the meaning.

## Acceptance criteria

- [x] `CLAUDE_PERMISSION_MODES` equals `("manual", "acceptEdits", "plan", "auto", "dontAsk",
      "bypassPermissions")` (as a set, the six choices of claude 2.1.322) and does not contain
      `default`.
- [x] `launch.settings(cfg)` returns `manual` for `conductor_permission_mode: manual` and for
      `steward_permission_mode: manual`.
- [x] A configured `conductor_permission_mode: default` or `steward_permission_mode: default` yields
      `build_plan(...)["argv"]` with `--permission-mode manual` and `plan["permission_mode"] ==
      "manual"`; no launch argv contains `--permission-mode default`.
- [x] Invalid launch values (`"nope"`, `3`, `True`, `""`) still fall back to `dontAsk`; the other
      listed modes (`acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`) pass through
      unchanged; the steward still ignores the conductor's setting ([SPEC-0424]).
- [x] A persona `permission_mode` of `manual` and of `auto` passes profile validation; `default`
      still passes; `yolo` still fails with a message that lists `manual`.
- [x] A runner request carrying `permission_mode="default"` produces argv with `--permission-mode
      manual`; one carrying `dontAsk` is unchanged; an empty mode is unchanged.
- [x] With a `claude` binary on `PATH`, the choices parsed from `claude --help` equal
      `CLAUDE_PERMISSION_MODES` (test skipped when the binary is absent).
- [x] The user-facing docs/comments listing permission modes name `manual` and mark `default` a
      deprecated alias.
- [x] `python3 tools/test_tiers.py --check` exits 0 and `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests** (extending existing modules; no new test module, so no new speed-tier marker:
  `tests/test_agentic_launch.py` has `pytestmark = pytest.mark.fast`, `tests/test_agentic_profiles.py`
  carries its marker at line 450, and `tests/test_agentic_runner*.py` already has its own):
  - `tests/test_agentic_launch.py`: extend `test_conductor_guard_settings_override_and_bad_value`
    with `manual` / `default` / `auto` / `bypassPermissions` parametrized cases for both role
    settings; add `test_default_permission_mode_is_launched_as_manual` (conductor and steward argv
    contain `--permission-mode manual`, never `default`); add the constant-equals-the-six-choices
    assertion; add `test_permission_modes_match_installed_claude` which runs `claude --help`
    (skip when `shutil.which("claude")` is `None`) and compares the parsed choices.
  - `tests/test_agentic_profiles.py`: persona `permission_mode` `manual`, `auto`, `default` validate;
    `yolo` fails and the message names `manual`.
  - the runner's argv test module (`grep -l "permission-mode" tests/`): `default` becomes `manual`,
    other values and the empty mode are untouched.
  - `python3 tools/test_tiers.py --check` exits 0.
- **Manual verification:** with the real `claude` on `PATH`, set
  `agentic.conductor.launch.steward.steward_permission_mode: default` in a scratch config, run
  `nw queue steward-launch <Project> --dry-run` (or the plan-printing equivalent) and confirm the
  printed argv has `--permission-mode manual`; then launch once for real, confirm `claude` starts,
  and exit. Repeat with `manual`.
- **Regression guard:** run the affected tests (`python3 tools/affected_tests.py --run uv run pytest
  -q`) plus the three modules above; the existing `dontAsk` default assertions
  (`test_steward_plan_has_none_of_the_conductor_guard` and siblings) must pass unmodified; run
  `python3 tools/spec_lint.py`.

## Rollout / migration

No data migration. Existing configs with `default` keep working and now launch with `manual`;
launch records written before the change keep their old `permission_mode` string. When claude
changes its choices again, update the constant (the drift test fails first).

### Test record (2026-11-12)

- `uv run pytest -q tests/test_agentic_launch.py tests/test_agentic_profiles.py tests/test_agentic_runner.py`: 521 passed (new: listed modes pass through for both roles, `default` launches as `manual`, constant equals the six choices, drift test against the installed `claude --help` (2.1.322, ran, not skipped), persona `manual`/`auto`/`default` validate and `yolo` message names `manual`, runner argv `default`->`manual`, `dontAsk`/`manual`/empty unchanged).
- `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py` exit 0. The full suite was left to gating.
- Manual: with the real claude 2.1.322, `claude --permission-mode manual --version` and `--permission-mode default --version` are accepted and `bogus` is rejected with "Allowed choices are acceptEdits, auto, bypassPermissions, manual, dontAsk, plan". The printed argv for a configured `default` was verified by the `build_plan` tests (the cfg fixture) rather than a scratch-config `nw queue steward-launch --dry-run`; no interactive launch was performed.

### As shipped

- Shared definition lives in `src/nightwright/agentic/runner.py` (`CLAUDE_PERMISSION_MODES`, `LEGACY_PERMISSION_MODES`, `canonical_permission_mode`); no import cycle. `launch.PERMISSION_MODES` aliases the constant; `profiles.PERMISSION_MODES` is the constant plus the alias keys.
- Docs: the profile docs (`docs/product/capabilities/agentic.md`) list the new choices and mark `default` a deprecated alias of `manual`; the code comments at the definitions say the same.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run pytest
      -q`); the manual real-`claude` check performed and noted in the spec.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
