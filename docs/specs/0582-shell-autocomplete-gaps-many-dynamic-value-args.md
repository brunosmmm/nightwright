---
id: SPEC-0582
title: Shell completion for the remaining dynamic-value args, a live SPEC-0039 guard, and group tab-completion
status: proposed
owner: user
created: 2026-11-12
kind: feature
source_idea: IDEA-605
tags: [cli, completion]
depends_on: [SPEC-0039]
---

## Context

IDEA-605 (audit run 2026-11-01) lists dynamic-value CLI args with no `shell_complete`:
`queue steward-launch PROJECT`, `queue merge BRANCH/--base/--profile`, `mailbox thread MSG_ID`
and `mailbox send --re`, `--campaign` on `queue events/decisions`, the `--reader/--from/--by`
addresses, `--project` on several commands, `idea close --folded-into/--superseded-by`,
`spec export --task-id`, `spec release NUMBER`, `override` topic, `projects rename OLD`, and
`--tag`/`--epic` on list commands. It also reports that `nw projects` / `nw projects show`
complete to nothing although `cli_projects.py:31,55` carry completers.

Investigation for this spec found three root causes, not just missing wiring:

1. **The SPEC-0039 guard is vacuous.** `missing_required_shell_completes`
   (`src/nightwright/completion.py:367`) skips a param when
   `getattr(param, "shell_complete", None) is not None`. In Click 8 `Parameter.shell_complete`
   is a *method* on every param, so the check is always true and the function always returns
   `[]`; `tests/test_completion.py::test_required_shell_complete_names_wired` can never fail.
   The Click attribute that records a user-supplied completer is `_custom_shell_complete`.
   With that attribute, params whose names are *already* in `REQUIRED_SHELL_COMPLETE_NAMES`
   turn out to lack completers: `project` (`queue steward-launch`, `queue mvp-report`,
   `sheet plan|record`, `idea project`, `spec lint`, `gate run|stats`, `gate cadence run|status`,
   `campaign history|records`), `by` (every `queue … --by`, `gate run --by`,
   `escalations answer --by`), `campaign` (`queue events|decisions`, `campaign records`),
   `epic` (`ideas list|export`), `scheme` (`projects add`), `spec_id`
   (`backlog add|edit`, `campaign history`) and `key` (`add`, `idea capture`, `spec generate`).
2. **The guard matches by param name only** (`REQUIRED_SHELL_COMPLETE_NAMES`,
   `completion.py:33`), so a dynamic param with a new name (`branch`, `reader`, `frm`,
   `re_`, `msg_id`, `folded_into`, `superseded_by`, `profile`, `topic`, `old`, `number`,
   `task_id`, `tag`) is invisible to it.
3. **`nw projects <TAB>` and `nw ideas <TAB>` return nothing** because `ProjectsGroup` /
   `IdeasGroup` (`cli.py:1228`, `cli.py:1389`) prepend `list` in `parse_args` whenever the
   first arg is not a subcommand. Click resolves the completion context by calling
   `parse_args` on a resilient context, so with no args the context becomes the `list`
   subcommand and the group's own subcommand names (`config`, `init`, `add`, `rename`, …) are
   never offered. `projects show` is not a command (there is no such subcommand, `show` is
   rejected as an unknown argument to `list`), so it completing to nothing is correct; the
   working completers are on `projects config` and `projects init`, which already
   complete project names (verified with `_NW_COMPLETE=bash_complete`).

The installed fish stub is Click's live wrapper and is current: no reinstall is needed for any
of this.

## Goals / Non-goals

**Goals**
- Every dynamic-value argument/option named in IDEA-605 completes real values.
- The SPEC-0039 guard actually works (fix the vacuous check) and is strengthened from a
  name list to "any string-valued param without a completer must be justified".
- `nw projects <TAB>` and `nw ideas <TAB>` offer subcommands (and `--json` etc. still
  complete for the default `list`).

**Non-goals**
- Completing free-text params (`text`, `title`, `note`, `reason`, `--for` on `spec reserve`
  which is documented as a free-text note, dates, `--since/--until`, `KEY=VALUE` properties).
- Adding a `projects show` subcommand.
- Completers for `workers` names, gate/worker job ids, and other args outside IDEA-605; they
  are recorded in the guard's exemption list as follow-ups and the list may only shrink.
- Reinstalling any shell stub.

## Decision

1. Fix the guard predicate to use `param._custom_shell_complete` and wire every violation the
   corrected guard reveals.
2. Add the missing completers to `completion.py` and attach them to the args in IDEA-605.
3. Replace "name in required set" with a **default-deny** rule: every non-flag param whose
   Click type is plain string (`click.STRING`, not Path/Choice/numeric/DateTime) must either
   carry a custom completer, have a name in a generic `FREE_TEXT_NAMES` set, or appear in an
   explicit `UNCOMPLETED_EXEMPT` set of `"<command path>:<param>"` entries. `REQUIRED_SHELL_COMPLETE_NAMES`
   stays as an extra check: a required-name param needs a completer even if a path is exempted
   by name. A test asserts no exemption entry is stale.
4. Give the two default-`list` groups a shared base class whose `parse_args` does not prepend
   `list` during resilient (completion) parsing when there are no args, and whose
   `shell_complete` also offers the `list` command's options when the incomplete word starts
   with `-`.

## Design

### Completers (`src/nightwright/completion.py`, all wrapped in `@_soft`)

| Completer | Values | Attached to |
|---|---|---|
| `complete_address` (moved from `agentic/cli_mailbox.py:18`, re-exported there) | `mailbox.known_addresses(cfg)`: `conductor`, `human`, `steward:<project>` | every `--by`, `--from`/`frm`, `--reader`, `--requested-by` option: `queue add|abandon|after|backend|decision|park|prioritize|restart|resume|stop|sentinel resume-fix --by`, `queue merge --by`, `gate run --by`, `escalations answer --by`, `escalations withdraw --from`, `queue escalate --from`, `queue events|wakeups --reader`, `queue conductor-session add|list --reader` |
| `complete_msg_id` | `id` of each record from `mailbox._all_messages`, newest first, capped at 50, help = `summary_line` clipped via `_clip_help` | `mailbox thread MSG_ID`, `mailbox send --re` |
| `complete_branch` | `nw/<campaign id>` for non-terminal campaigns from the store, then `git for-each-ref refs/heads` of the cwd repo (2 s timeout; any failure yields only the campaign branches) | `queue merge BRANCH`, `gate run --branch` |
| `complete_base_branch` | local branches of the cwd repo only (same git call) | `--base` on `queue merge`, `queue add`, `gate run`, `queue main-dirty-plan|apply` |
| `complete_bucket` | distinct `facets[cfg project_axis]` values in `load_mappings(cfg)` plus `registered_projects` | `projects rename OLD` |
| `complete_tag` | distinct org tags over `load_tasks(cfg)` (ideas and tasks) | `--tag` on `ideas list|export`, `tasks`, `agenda`, `hub`, `next`, `tui` |
| `complete_idea_or_spec_id` | idea ids (open and closed) plus internal spec ids | `idea close --folded-into`, `--superseded-by` |
| `complete_reserved_number` | numbers from `spec_numbers.list_reservations(cfg, specs_dir=…)` as `SPEC-NNNN` | `spec release NUMBER` |
| `complete_task_id` | ids of open org tasks (`complete_task_selector` without topic keys) | `spec export --task-id` |

Existing completers are reused where they already fit: `complete_project` for every `--project`
(`queue mvp-report`, `sheet plan|record`, `idea project`, `spec lint`, `gate run|stats`,
`gate cadence run|status`, `campaign history|records`) and `queue steward-launch PROJECT`;
`complete_profile` for `queue add|merge --profile`; `complete_campaign_id` for `--campaign` on
`queue events|decisions` and `campaign records`; `complete_spec_id` for `backlog add|edit
--spec-id` and `campaign history --spec-id`; `complete_epic` for `--epic` on `ideas list|export`;
`complete_scheme` for `projects add --scheme`; `complete_mapped_topic` for `override` topic.
`override`'s `session`/`start`/`end` arguments and `map assignments` stay free text/exempt.

### Group completion (`src/nightwright/cli.py`)

A `DefaultListGroup(click.Group)` replaces the duplicated `ProjectsGroup`/`IdeasGroup`
`parse_args`: it prepends `list` exactly as today except when `ctx.resilient_parsing` is true
and `args` is empty. `shell_complete(ctx, incomplete)` returns `super()` results, and when
`incomplete.startswith("-")` also the `list` command's option completions (so `nw projects
--j<TAB>` still offers `--json`). Non-completion behavior is unchanged.

### The strengthened guard (`completion.py`, `tests/test_completion.py`)

- `has_completer(param)`: `getattr(param, "_custom_shell_complete", None) is not None`, or the
  type is `click.Path`/`click.Choice`, or the param is a flag, or its type is numeric/bool/
  `DateTime`.
- `missing_dynamic_completes(cmd=None)` walks `iter_cli_params`; for each non-flag param with
  string type and no completer it reports `"<path>:<param>"` unless its name is in
  `FREE_TEXT_NAMES` (`text`, `title`, `note`, `reason`, `summary`, `tags`, `query`, `since`,
  `until`, `date`, `value`, `message`, …) or the path is in `UNCOMPLETED_EXEMPT`.
  `missing_required_shell_completes` keeps its contract (required-name params) with the
  corrected predicate.
- `stale_exemptions()` returns `UNCOMPLETED_EXEMPT` entries that no longer name an existing,
  completer-less string param. The initial `UNCOMPLETED_EXEMPT` is generated from the audit
  walk after wiring this spec's completers; each group of entries carries a one-line comment
  (workers names, gate/worker job ids, `--key` KEY=VALUE properties, free-form `--cmds`, …).
- The docstring of `REQUIRED_SHELL_COMPLETE_NAMES` and the README/`docs/WORKFLOW.md` completion
  checklist are updated: a new string param needs a completer or an exemption entry with a reason.

## Alternatives considered

- **Extend only `REQUIRED_SHELL_COMPLETE_NAMES` with the new names** — still name-only: the next
  new param name slips through again, which is the weakness IDEA-605 reports.
- **Allowlist every currently uncompleted param without a staleness test** — the list would rot
  and stop shrinking; the stale check makes it a ratchet.
- **Add a `projects show` subcommand to match the audit** — the audit mis-read the surface;
  `projects config` already serves that purpose.
- **Fix group completion by overriding `resolve_command`** — Click's completion resolution goes
  through `parse_args`, so changing it there is the smaller, symmetric fix.

## Acceptance criteria

- [ ] `missing_required_shell_completes()` uses `_custom_shell_complete`; temporarily removing a
      completer from `queue steward-launch PROJECT` makes it return that entry.
- [ ] After this change `missing_required_shell_completes()` returns `[]` and
      `missing_dynamic_completes()` returns `[]` on the real CLI tree; `stale_exemptions()` returns `[]`.
- [ ] A fixture command with an un-annotated string option named `widget`, added to a Click tree,
      is reported by `missing_dynamic_completes`; adding `shell_complete`, a `FREE_TEXT_NAMES`
      name or an exemption removes it.
- [ ] `nw queue steward-launch <TAB>` yields the registered project names.
- [ ] `nw queue merge <TAB>` yields `nw/<campaign id>` for non-terminal campaigns; `--base <TAB>`
      yields local git branches; with git unavailable it still returns the campaign branches / `[]`
      without raising.
- [ ] `nw mailbox thread <TAB>` and `nw mailbox send --re <TAB>` yield message ids with a clipped
      one-line help.
- [ ] `--campaign` on `queue events` and `queue decisions` yields campaign ids.
- [ ] `--reader`, `--from`, `--by`, `--requested-by` on the commands in the Design table yield
      `conductor`, `human` and `steward:<project>`.
- [ ] `--project` on `queue mvp-report`, `sheet plan`, `sheet record`, `idea project`, `spec lint`,
      `gate run`, `gate stats` yields registered projects; `--profile` on `queue add|merge`
      yields profile names.
- [ ] `idea close --folded-into <TAB>` / `--superseded-by <TAB>` yield idea and spec ids.
- [ ] `spec release <TAB>` yields reserved `SPEC-NNNN` numbers; `spec export --task-id <TAB>`
      yields open task ids; `override <TAB>` topic yields mapped topics;
      `projects rename <TAB>` yields buckets; `--tag` and `--epic` on `ideas list|export` yield tags and epics.
- [ ] `nw projects <TAB>` yields `add`, `config`, `init`, `list`, `rename`; `nw projects --<TAB>`
      includes `--json`; `nw ideas <TAB>` yields `export` and `list`; `nw projects --json` and
      `nw ideas --all` still run the default `list`.
- [ ] Every completer fails soft: with an unreadable config each returns `[]`.
- [ ] README and `docs/WORKFLOW.md` describe the default-deny rule and the exemption ratchet.

## Test plan

- **Automated tests** (extend `tests/test_completion.py`, which already carries its single
  module marker `pytestmark = pytest.mark.fast`; no new module and no second marker):
  - `test_guard_detects_missing_custom_completer`: build a small Click group with one
    uncompleted `project` argument and one completed one; assert only the first is reported
    (regression for the vacuous predicate).
  - `test_default_deny_flags_unknown_string_param` and `test_default_deny_accepts_free_text_and_exempt`.
  - `test_required_and_dynamic_guards_clean_on_real_cli` and `test_uncompleted_exempt_has_no_stale_entries`.
  - One parametrized test calling each new completer (`complete_msg_id`, `complete_branch`,
    `complete_base_branch`, `complete_bucket`, `complete_tag`, `complete_idea_or_spec_id`,
    `complete_reserved_number`, `complete_task_id`, `complete_address`) against `_cfg(tmp_path)`
    fixtures (mailbox jsonl, mappings.yaml, a tmp git repo for branches) asserting prefix
    filtering and values; plus a soft-fail test with a broken config.
  - `test_wiring_by_command`: resolve each command in the Design table via `cli` and assert the
    param's `_custom_shell_complete` is the expected completer.
  - Group tests with `CliRunner` + `_NW_COMPLETE=bash_complete` env (or Click's
    `shell_complete` API): `projects` and `ideas` with no args list their subcommands;
    `projects --` offers `--json`; `projects --json` and `ideas --all` still execute `list`.
  - `python3 tools/test_tiers.py --check` exits 0.
- **Manual verification** (against a real config): run
  `_NW_COMPLETE=bash_complete COMP_WORDS="nw projects " COMP_CWORD=2 uv run nw` and the same for
  `nw queue steward-launch `, `nw queue merge `, `nw mailbox thread `, `nw queue events --reader `;
  expect non-empty value lists. One interactive fish `nw projects <TAB>` check on the installed stub.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`, the existing
  idea/projects CLI tests (default `list` routing), and `python3 tools/spec_lint.py` exit 0.

## Rollout / migration

1. Fix the predicate and add the default-deny guard with a temporary exemption list covering every
   current violation; run the guard tests.
2. Add completers and wire them; delete the matching exemption entries as each lands.
3. Land the group base class; update README/`docs/WORKFLOW.md`.
4. No shell reinstall: the fish stub is Click's live wrapper.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run pytest -q`);
      manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
