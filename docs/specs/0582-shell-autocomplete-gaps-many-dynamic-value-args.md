---
id: SPEC-0582
title: Make the SPEC-0039 shell-completion guard live with default-deny and an exemption ratchet
status: proposed
owner: user
created: 2026-11-12
kind: feature
source_idea: IDEA-605
tags: [cli, completion]
depends_on: [SPEC-0039]
---

## Context

IDEA-605 (audit run 2026-11-01) lists many dynamic-value CLI args with no `shell_complete`
(`queue steward-launch PROJECT`, `queue merge BRANCH/--base/--profile`, `mailbox thread MSG_ID`,
`--campaign` on `queue events|decisions`, the `--reader/--from/--by` addresses, `--project`,
`idea close --folded-into`, `spec export --task-id`, `spec release`, `override` topic,
`projects rename OLD`, `--tag`/`--epic`, …). It asks to strengthen the SPEC-0039 guard so the
next uncompleted dynamic param is caught automatically. It also reports that `nw projects` /
`nw projects show` complete to nothing.

Investigation found three independent root causes. This spec fixes only the first two
guard-related ones (the guard itself); the wiring of new completers and the group
tab-completion fix are separate follow-up specs (see Non-goals).

1. **The SPEC-0039 guard is vacuous.** `missing_required_shell_completes`
   (`src/nightwright/completion.py:367`) skips a param when
   `getattr(param, "shell_complete", None) is not None`. In Click 8 `Parameter.shell_complete`
   is a *method* on every param, so the check is always true and the function always returns
   `[]`; `tests/test_completion.py::test_required_shell_complete_names_wired` can never fail.
   The Click attribute that records a user-supplied completer is `_custom_shell_complete`.
   With it, params whose names are *already* in `REQUIRED_SHELL_COMPLETE_NAMES` turn out to
   lack completers: `project` (`queue steward-launch`, `queue mvp-report`, `sheet plan|record`,
   `idea project`, `spec lint`, `gate run|stats`, `gate cadence run|status`,
   `campaign history|records`), `by` (every `queue … --by`, `gate run --by`,
   `escalations answer --by`), `campaign` (`queue events|decisions`, `campaign records`),
   `epic` (`ideas list|export`), `scheme` (`projects add`), `spec_id`
   (`backlog add|edit`, `campaign history`) and `key` (`add`, `idea capture`, `spec generate`).
2. **The guard matches by param name only** (`REQUIRED_SHELL_COMPLETE_NAMES`,
   `completion.py:33`), so a dynamic param with a new name (`branch`, `reader`, `frm`, `re_`,
   `msg_id`, `folded_into`, `superseded_by`, `profile`, `topic`, `old`, `number`, `task_id`,
   `tag`) is invisible to it.
3. *(Out of scope here.)* `nw projects <TAB>` / `nw ideas <TAB>` return nothing because
   `ProjectsGroup`/`IdeasGroup` (`cli.py:1228`, `cli.py:1389`) prepend `list` in `parse_args`
   even during resilient completion parsing. `projects show` is not a command, so it completing
   to nothing is correct; the working completers are on `projects config` and `projects init`.

The installed fish stub is Click's live wrapper and is current: no reinstall is needed.

## Goals / Non-goals

**Goals**
- The guard really detects a missing completer (fix the vacuous predicate).
- The guard becomes default-deny: any string-valued, non-flag param without a completer must be
  named in `FREE_TEXT_NAMES` or carry an explicit, reasoned entry in `UNCOMPLETED_EXEMPT`.
- `UNCOMPLETED_EXEMPT` is a ratchet: seeded with every current violation, only allowed to
  shrink, enforced by a stale-entry check.

**Non-goals** (each is a separate follow-up spec; this spec lands none of their behavior)
- **Completer wiring spec** (depends on this one): add `complete_address`, `complete_msg_id`,
  `complete_branch`, `complete_base_branch`, `complete_bucket`, `complete_tag`,
  `complete_idea_or_spec_id`, `complete_reserved_number`, `complete_task_id`, attach them and
  the existing completers to the commands listed in IDEA-605, and delete the matching
  `UNCOMPLETED_EXEMPT` entries (enforced by `stale_exemptions()`).
- **Default-list group completion spec** (independent): a shared `DefaultListGroup` base for
  `ProjectsGroup`/`IdeasGroup` so `nw projects <TAB>` / `nw ideas <TAB>` offer subcommands.
- Adding a `projects show` subcommand; completing free-text params; reinstalling any shell stub;
  changing any command's runtime behavior.

## Decision

1. Fix `missing_required_shell_completes` to use `param._custom_shell_complete`, via one shared
   `has_completer(param)` helper.
2. Add a **default-deny** `missing_dynamic_completes()`: every non-flag param whose Click type is
   plain string (`click.STRING`; not `Path`, `Choice`, numeric, bool or `DateTime`) must carry a
   custom completer, have its name in `FREE_TEXT_NAMES`, or appear in `UNCOMPLETED_EXEMPT`.
3. `UNCOMPLETED_EXEMPT` is a dict `"<command path>:<param>" -> reason`. **One ordering process:**
   this spec seeds it with *every* violation the corrected guards currently report on the real
   CLI tree, each with a non-empty reason (grouped by cause, e.g. "pending completer wiring
   (IDEA-605 follow-up)", "workers names", "gate/worker job ids", "KEY=VALUE properties").
   The later wiring spec removes the entries it wires; `stale_exemptions()` fails the suite if
   an entry is left behind. The list is never regenerated after the fact.
4. The required-name check keeps its contract (a `REQUIRED_SHELL_COMPLETE_NAMES` param needs a
   completer) with the corrected predicate; it honors `UNCOMPLETED_EXEMPT` too, so the real-tree
   guards pass on seeded exemptions while a new violation fails.

## Design

All in `src/nightwright/completion.py` (no change to any command or completer behavior).

- `has_completer(param) -> bool`: true when `getattr(param, "_custom_shell_complete", None)` is
  not None, or the param is a flag option, or its type is `click.Path`, `click.Choice`,
  numeric (`INT`/`FLOAT`), `BOOL` or `DateTime`.
- `FREE_TEXT_NAMES`: frozenset of generic free-text param names (`text`, `title`, `note`,
  `reason`, `summary`, `tags`, `query`, `since`, `until`, `date`, `value`, `message`, …; the
  implementer fixes the final list from the audit walk, each name genuinely free text).
- `UNCOMPLETED_EXEMPT: dict[str, str]`: see Decision 3. Path keys use the same
  `"<space-joined command path>:<param name>"` format `missing_required_shell_completes` emits.
- `missing_dynamic_completes(cmd=None) -> list[str]`: walks `iter_cli_params(cmd)`; for each
  string-typed param where `has_completer` is false, reports `"<path>:<param>"` unless the name
  is in `FREE_TEXT_NAMES` or the key is in `UNCOMPLETED_EXEMPT`.
- `missing_required_shell_completes(cmd=None)`: same signature; uses `has_completer`, skips
  `UNCOMPLETED_EXEMPT` keys.
- `stale_exemptions(cmd=None) -> list[str]`: `UNCOMPLETED_EXEMPT` keys that no longer name an
  existing, completer-less, string-typed param (the command/param vanished or was wired).
- Update the `REQUIRED_SHELL_COMPLETE_NAMES` comment and the README / `docs/WORKFLOW.md`
  completion checklist: a new string param needs a completer, a `FREE_TEXT_NAMES` name, or an
  `UNCOMPLETED_EXEMPT` entry with a reason; the exemption list may only shrink.

## Alternatives considered

- **Extend only `REQUIRED_SHELL_COMPLETE_NAMES` with the new names** — still name-only; the next
  new param name slips through again, which is the weakness IDEA-605 reports.
- **Allowlist without a staleness test** — the list would rot and stop shrinking; the stale
  check makes it a ratchet.
- **Do guard, wiring and group fix in one spec** — three independent subsystems exceed one
  campaign; the guard lands first so the wiring spec is checked by it.
- **Generate the exemption list after wiring** — hides real gaps behind a hand-maintained list
  and conflicts with the ratchet; seeding first and removing as wired is the single process.

## Acceptance criteria

- [ ] `has_completer` and `missing_required_shell_completes` use `_custom_shell_complete`: a
      Click fixture with one uncompleted `project` argument and one completed one reports only
      the first.
- [ ] `missing_dynamic_completes()` flags a fixture string option named `widget` with no
      completer; adding `shell_complete`, a `FREE_TEXT_NAMES` name or an `UNCOMPLETED_EXEMPT`
      entry removes it.
- [ ] Flags, `Path`, `Choice`, numeric, bool and `DateTime` params are never reported.
- [ ] On the real CLI tree `missing_required_shell_completes()` and `missing_dynamic_completes()`
      both return `[]`, and `stale_exemptions()` returns `[]`.
- [ ] `UNCOMPLETED_EXEMPT` contains an entry for every violation the corrected guards find
      without the exemptions (including each `project`, `by`, `campaign`, `epic`, `scheme`,
      `spec_id`, `key` case listed in Context), and every entry has a non-empty reason.
- [ ] `stale_exemptions()` reports an entry whose param was given a completer and an entry whose
      command does not exist (fixture-tested).
- [ ] The `REQUIRED_SHELL_COMPLETE_NAMES` comment and the README / `docs/WORKFLOW.md`
      completion checklist describe the default-deny rule and the exemption ratchet.
- [ ] No command runtime behavior or existing completer output changes.

## Test plan

- **Automated tests** (extend `tests/test_completion.py`, which already carries its single module
  marker `pytestmark = pytest.mark.fast`; no new module and no second marker):
  - `test_guard_detects_missing_custom_completer`: small Click group, one uncompleted and one
    completed `project` argument; only the first is reported (regression for the vacuous
    predicate).
  - `test_default_deny_flags_unknown_string_param` and
    `test_default_deny_accepts_free_text_and_exempt` (monkeypatch `FREE_TEXT_NAMES` /
    `UNCOMPLETED_EXEMPT` for the fixture).
  - `test_default_deny_ignores_non_string_params`: flags, `Path`, `Choice`, `INT`, `BOOL`,
    `DateTime`.
  - `test_required_and_dynamic_guards_clean_on_real_cli` and
    `test_uncompleted_exempt_has_no_stale_entries` (real tree).
  - `test_uncompleted_exempt_has_reasons`: every value is a non-empty string.
  - `test_stale_exemptions_detects_wired_and_missing`: fixture tree.
  - `test_exempt_covers_context_cases`: the real tree with `UNCOMPLETED_EXEMPT` monkeypatched
    empty reports at least the `project`, `by`, `campaign`, `epic`, `scheme`, `spec_id`, `key`
    paths named in Context (proves the seed is complete and the guard is live).
  - `python3 tools/test_tiers.py --check` exits 0 (no new test module is added).
- **Manual verification:** temporarily delete one `UNCOMPLETED_EXEMPT` entry and run
  `uv run pytest tests/test_completion.py -q`; expect the real-tree guard to fail naming it.
  Restore it.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` and
  `python3 tools/spec_lint.py` exit 0.

## Rollout / migration

1. Fix the predicate and add `has_completer`, `FREE_TEXT_NAMES`, `missing_dynamic_completes`,
   `stale_exemptions`.
2. Run the corrected guards against the real tree and seed `UNCOMPLETED_EXEMPT` with every
   violation, each with a reason; add the tests.
3. Update the comment, README and `docs/WORKFLOW.md`.
4. The follow-up wiring spec (depends on this one) removes entries as it wires completers; the
   group-completion spec is independent. No shell reinstall: the fish stub is Click's live wrapper.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run pytest -q`);
      manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
