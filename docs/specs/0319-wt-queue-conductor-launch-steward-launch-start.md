---
id: SPEC-0319
title: "`wt queue conductor-launch` / `steward-launch`: start the conductor and stewards with forced context limits, a pinned model and a pre-registered session id"
status: proposed
owner: user
created: 2026-10-31
milestone: "M10: agentic MVP"
kind: feature
parent: SPEC-0298
source_idea: IDEA-509
tags: [agentic, conductor, cost, context]
depends_on: [SPEC-0299, SPEC-0301]
---

## Context

[SPEC-0298](./0298-agentic-mvp-cheap-durable-quiet-conductor.md) sets exit criterion E2: the
median conductor context per turn is at most 150k tokens. The `mvp-report` of 2026-10-31
08:17 measured a median of 176k. The `/wt-conduct` skill already says "compact at about 150k
and re-orient from `wt queue conductor-brief`" ([SPEC-0301](./0301-conductor-state-handoff-and-decision-log.md)),
but that is an instruction the model may or may not follow. Today the human starts the
conductor with a bare `claude` and types `/wt-orient`, then `/wt-conduct`, by hand, so:

- nothing bounds the context: it grows until Claude Code's own default auto-compaction;
- tool output is uncapped at the defaults (`BASH_MAX_OUTPUT_LENGTH` 30000 characters,
  `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` 25000), so one large `wt queue events` or file
  read adds tens of thousands of tokens that are then re-read on every turn;
- the model and effort are whatever the shell or the last session had; 66 conductor turns on
  `claude-fable-5-1` were unpriced in `wt queue conductor-cost`;
- the session is registered in `sessions.jsonl`
  ([SPEC-0299](./0299-conductor-cost-measurement.md)) only when the follower first runs
  inside it, so cost and wakeups attribute only after the conductor armed its Monitor, and a
  restart that never reaches that step is invisible.

Checked on Claude Code 2.1.316 (`claude --help`, `code.claude.com/docs/en/env-vars`): the
flag `--autocompact <auto|tokens>` (same as `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, 100k to 1M);
`CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS` (idle compaction, default 200k, minimum 100k);
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (can only compact earlier); `BASH_MAX_OUTPUT_LENGTH`;
`CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS`; `--model`, `--effort` (overridden by
`CLAUDE_CODE_EFFORT_LEVEL` when that is set); `--session-id <uuid>`; `-n/--name`;
`--resume <id>`. Everything needed to force the limits is a launch-time flag or env var, so
the right place is a launcher, not more prose in a skill.

## Goals / Non-goals

**Goals**
- `wt queue conductor-launch` and `wt queue steward-launch PROJECT` start a Claude Code
  session with the context limits, model, effort and name set from code-held defaults, with
  no flags to remember.
- The conductor's context cannot exceed the autocompact window (150k by default), which is
  the mechanism behind E2, and routine compaction plus `wt queue conductor-brief` makes a
  clean re-orientation (E6) happen inside the window.
- The session id is chosen by wt and registered in the SPEC-0299 registry before `claude`
  starts, so cost and wakeups attribute from the first turn without a manual
  `conductor-session add`.
- A model with no `agentic.conductor.prices` entry is refused, so no turn is silently
  unpriced.
- `--dry-run` prints the exact argv and env, and `--resume` restarts the same session.

**Non-goals**
- Changing what `/wt-conduct` or `/wt-steward` do once running.
- Supervising or auto-restarting the session. A human (or a later spec) runs the launcher
  again; dependence on the conductor session stays by design.
- Shipping a price table (SPEC-0299 Decision 3 stands).
- Capping sub-agents the conductor dispatches. The limits apply to the launched session;
  sub-agents inherit the environment but are not measured by this spec.
- Stream-json capture and a viewer (IDEA-427). `--in herdr` is only a placement hook.

## Decision

1. Add `wt queue conductor-launch [--dry-run] [--resume] [--in herdr]` and `wt queue
   steward-launch PROJECT [--dry-run] [--resume] [--in herdr]`, attached from a new module
   beside the other `wt queue conductor-*` commands.
2. Each builds one `claude` invocation: `claude -n <name> --session-id <uuid> --model <M>
   --effort <E> --autocompact <W> <prompt>` with an environment that adds
   `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`,
   `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` and `CLAUDE_CODE_EFFORT_LEVEL`. The values come
   from `agentic.conductor.launch.*`, with code-held defaults: model `claude-sonnet-5-5`,
   effort `medium`, autocompact `150k`, idle compaction `120000`, bash output `15000`,
   file read `10000`.
3. The session id is a fresh UUID4 (or, with `--resume`, the one already registered), and is
   written to `sessions.jsonl` with `source: "launch"` before the process starts.
4. The launcher refuses to start a model that has no price entry, and tells the user which
   config key to set.
5. By default the launcher replaces itself with `claude` (`os.execvpe`), so the session is
   the user's interactive terminal session. `--in herdr` instead opens it in a herdr tab.

## Design

New module `src/wt/agentic/launch.py` (pure logic, no process side effects except in
`run`), and `src/wt/agentic/cli_launch.py` whose `attach(queue_group)` is registered in
`src/wt/agentic/cli.py` next to `cli_conductor.attach`.

**Settings.** `launch.settings(cfg, role) -> dict` overlays `DEFAULTS` with
`agentic.conductor.launch` (all roles) and, for stewards, `agentic.conductor.launch.steward`
(per-key overrides), the same fall-back-on-bad-value pattern as `events.settings`:

| key | default | valid | bad value |
|---|---|---|---|
| `model` | `claude-sonnet-5-5` | non-empty string | default |
| `effort` | `medium` | one of `low`, `medium`, `high`, `xhigh`, `max` | default |
| `autocompact` | `150k` | `auto`, or an integer or `Nk`/`Nm` string from 100k to 1M | default |
| `idle_compact_min_tokens` | `120000` | integer from 100000 | default |
| `bash_max_output_length` | `15000` | integer ≥ 1000 | default |
| `file_read_max_output_tokens` | `10000` | integer ≥ 1000 | default |

A bool, non-numeric, negative or non-finite number counts as a bad value. `autocompact` is
normalised to a token count (`150k` → `150000`) before it is put into argv, since the
stable form is the number; `auto` is passed through.

**Plan.** `build_plan(cfg, role, project=None, *, resume=False, session_id=None) -> dict`
returns `{role, name, cwd, session_id, resumed, argv, env, prompt, model, price}` and
touches nothing:

- `name`: `conductor` or `steward:<P>`.
- `cwd`: the conductor runs in the repo root of the internal project (`Meta-Tools`); a
  steward in the repo root of `PROJECT`; both read from `rules.projects_payload(cfg)`
  (`research.root`). An unknown project, or one with no repo root, is a `ClickException`
  that lists `wt projects --json`.
- `argv`: `["claude", "-n", name, "--session-id" | "--resume", sid, "--model", M, "--effort",
  E, "--autocompact", W, prompt]`. Under `--resume` the id is passed with `--resume` and not
  `--session-id`.
- `env`: only the keys wt sets, never the whole ambient environment:
  `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS`, `BASH_MAX_OUTPUT_LENGTH`,
  `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS`, and `CLAUDE_CODE_EFFORT_LEVEL` (set to the same
  effort, because an ambient `CLAUDE_CODE_EFFORT_LEVEL` would otherwise override
  `--effort` and silently defeat the pin). The flag form of autocompact is used, so
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` is not set.
- `prompt`: for the conductor, `/wt-orient` then, once oriented, `/wt-conduct`; for a
  steward, `/wt-orient` then `/wt-steward <PROJECT>`. Fixed constants in `launch.py`,
  identical for a fresh and a resumed session (a resumed session must re-arm its Monitor
  anyway, and `/wt-conduct` already starts from `wt queue conductor-brief`).
- `price`: `conductor_cost.price_for(cfg, model)`.

**Session registration.** `register(cfg, plan)` calls
`conductor_cost.record_session(cfg, reader, {}, source="launch", session_id=sid)` with
`reader = "conductor"` for the conductor and `"steward:<P>"` for a steward. `record_session`
is already idempotent per `(reader, session_id)` under its flock, so `--resume`, and the
follower's later `source: "follower"` attempt inside the same session, append nothing
twice. A registration failure (for example a read-only data dir) is a `ClickException`
before launch: starting an unattributed conductor is what this spec removes. The record's
`claude_pid` is null and `cwd` is the launcher's cwd.

**`--resume`.** Reuses the newest `source: "launch"` record of the same reader in
`sessions.jsonl` (`conductor_cost.sessions(cfg, reader)`); with none, the command fails with
"no launched session to resume; run without --resume". It does not look at `source:
"follower"` or `"manual"` records, since those may belong to a session started elsewhere
with other limits. If the transcript no longer exists (`find_transcript` returns None),
it fails with the same message, since `claude --resume` would only start an empty session.
The launch limits are re-applied from the current settings.

**Price check.** If `plan["price"]` is None, `run` raises a `ClickException`:
`no price for model X; set agentic.conductor.prices.<model-prefix> (SPEC-0299)`. The
check happens before registration and before exec. `--dry-run` runs the same check, prints
the plan anyway so the user can see what would start, and then exits 1 with the same
message.

**`--dry-run`.** Prints plain text: a `cwd:` line, an
`env:` block of `KEY=value` lines in sorted order, and the argv shell-quoted (`shlex.join`)
on one line, followed by `session: <uuid> (new|resumed)`. It never writes `sessions.jsonl`
and never starts a process. The session id shown for a new launch is not reserved: a real
run generates its own. `--dry-run --json` emits the plan dict. (`--json` is accepted only
together with `--dry-run`.)

**Start.** Without `--in`, `run` registers, then `os.chdir(plan["cwd"])` and
`os.execvpe("claude", argv, {**os.environ, **plan["env"]})`. A missing `claude` executable
is a `ClickException` raised before registration (`shutil.which("claude")`).

**`--in herdr`.** After the same checks, registers, then runs `herdr tab create --cwd
<cwd> --label <name> --env K=V …` (one `--env` per planned key) and runs the shell-quoted
argv in the new tab's pane with `herdr pane run <pane-id> <argv…>`; the pane id is taken
from the JSON that `herdr tab create` prints. A missing `herdr` executable, a non-zero exit
from either command, or output with no pane id is a `ClickException`; the registration
already written is harmless (an unused registered id attributes no turns). Only the value
`herdr` is accepted for `--in`.

**Skills.** `skills/wt-conduct/SKILL.md` and `skills/wt-steward/SKILL.md` get one line
each that names the launcher as the way to start the session with the context limits
(`wt queue conductor-launch` / `steward-launch`), without changing their steps.

**Cost trade-off.** One summary turn per compaction. At a 150k window and the measured
rate this is a small fraction of the turns removed by the lower context; it is measured by
`wt queue conductor-cost` like any other turn.

## Alternatives considered

- **Put the limits in the user's shell profile or `~/.claude/settings.json` `env`.** They
  would then apply to every Claude Code session, including the human's own coding sessions
  and persona runs, which want the large defaults.
- **Only `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`.** It can only compact earlier and is a
  percentage of an unknown window, so the absolute cap is not guaranteed. `--autocompact`
  names the token count directly.
- **Register the session from the follower only (SPEC-0299 as is).** It misses every turn
  before the Monitor arms and any session that never reaches that step. Pre-registration
  closes the gap and the follower's idempotent record still covers sessions not started by
  the launcher.
- **Ship a built-in default price entry so the check never fails.** Rejected for the same
  reason as in SPEC-0299: a stale hard-coded price is worse than an explicit gap.
- **Let the conductor re-exec itself on a schedule.** Moves the restart decision into the
  expensive session. A launcher the human or a supervisor runs is cheaper and testable.

## Acceptance criteria

- [ ] `wt queue conductor-launch --dry-run` prints a `cwd:` line, an `env:` block and a
      single argv line containing, in order, `claude -n conductor --session-id <uuid>
      --model claude-sonnet-5-5 --effort medium --autocompact 150000` and the prompt, with
      env `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=120000`, `BASH_MAX_OUTPUT_LENGTH=15000`,
      `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS=10000` and `CLAUDE_CODE_EFFORT_LEVEL=medium`,
      when a price for `claude-sonnet-5-5` is configured. It writes nothing and starts no
      process.
- [ ] `steward-launch Foo --dry-run` names the session `steward:Foo`, uses the repo root
      of `Foo` as `cwd`, and the prompt runs `/wt-orient` then `/wt-steward Foo`. An unknown
      project fails and lists the known ones.
- [ ] Every setting under `agentic.conductor.launch.*` (and the `steward` per-key override)
      changes the planned argv or env; each bad value in the settings table falls back to its
      default; `autocompact` accepts `auto`, `150000` and `150k` and rejects values outside
      100k to 1M.
- [ ] An ambient `CLAUDE_CODE_EFFORT_LEVEL=high` does not change the plan's
      `CLAUDE_CODE_EFFORT_LEVEL`, which equals the configured effort.
- [ ] A real launch appends exactly one `sessions.jsonl` record, `source: "launch"`, with the
      planned session id and reader `conductor` (`steward:<P>` for a steward), before the
      process is exec'd; launching twice with `--resume` appends no second record; the
      follower's later registration of the same id appends nothing.
- [ ] A model with no matching `agentic.conductor.prices` entry makes both commands exit
      non-zero naming the config key, with no `sessions.jsonl` write and no exec; with
      `--dry-run` the plan is printed first and the exit status is still 1.
- [ ] `--resume` passes the newest `source: "launch"` id with `--resume <id>` and without
      `--session-id`, and fails with a clear message when there is no launch record or its
      transcript is missing; `follower` and `manual` records are never selected.
- [ ] A real launch (without `--in`) changes directory to the plan's `cwd` and calls
      `os.execvpe` with the planned argv and an environment that is `os.environ` overlaid
      with the plan's env; a missing `claude` binary fails before any registration.
- [ ] `--in herdr` creates a tab with the planned cwd, label and one `--env` per planned key,
      runs the argv in the returned pane, and fails with a clear message when `herdr` is
      missing, fails, or returns no pane id; any other `--in` value is rejected.
- [ ] `--json` without `--dry-run` is rejected, and `--dry-run --json` emits the plan dict.
- [ ] `skills/wt-conduct/SKILL.md` and `skills/wt-steward/SKILL.md` mention the launcher;
      `python3 tools/spec_lint.py` and the skill sync tests pass.

## Test plan

- **Automated tests** in `tests/test_agentic_launch.py`, using a temp config with a temp
  data dir, a fake projects registry (`rules.projects_payload` monkeypatched, or a temp
  mapping) and `agentic.conductor.prices` set for `claude-sonnet-5-5`. `os.execvpe` and
  `subprocess.run` are monkeypatched; no real `claude` or `herdr` runs. `shutil.which` is
  patched where a binary must exist.
  - `test_default_plan_argv_and_env`: asserts the exact argv, env and prompt of the
    conductor plan from the defaults (acceptance 1).
  - `test_steward_plan_cwd_name_prompt` and `test_steward_unknown_project_lists_known`
    (acceptance 2).
  - `test_settings_overrides_and_bad_values`: parametrised over every key in the settings
    table, including bool, string, negative, NaN, and `autocompact` of `99k`, `2m`, `auto`,
    `150k`, `150000`; checks the `steward` per-key override (acceptance 3).
  - `test_ambient_effort_env_does_not_win`: sets `CLAUDE_CODE_EFFORT_LEVEL=high` in the
    process env and asserts the plan env (acceptance 4).
  - `test_launch_registers_once_before_exec`: the fake `execvpe` asserts that
    `sessions.jsonl` already holds the record with `source: "launch"` and the argv session
    id; a second `--resume` launch and a `record_session(..., source="follower")` of the
    same id add no line (acceptance 5).
  - `test_unpriced_model_refused` and `test_unpriced_model_dry_run_prints_then_fails`
    (acceptance 6).
  - `test_resume_selects_latest_launch_record`, `test_resume_ignores_follower_and_manual`,
    `test_resume_missing_transcript_fails`: transcripts are tiny files under a temp
    `cfg["_root"]/-slug/<sid>.jsonl` (acceptance 7).
  - `test_exec_chdir_and_env_overlay` and `test_missing_claude_binary_fails_before_register`
    (acceptance 8).
  - `test_in_herdr_calls_tab_create_then_pane_run` with a fake `subprocess.run` that returns
    a canned `tab create` JSON, plus the failure cases: missing binary, non-zero exit, no
    pane id, and `--in tmux` rejected (acceptance 9).
  - `test_json_requires_dry_run` (acceptance 10).
  - `test_skills_mention_launcher`: greps the two SKILL.md files (acceptance 11).
- **Manual verification:**
  1. Set the price: in the user config, `agentic.conductor.prices.claude-sonnet-5-5`.
  2. `wt queue conductor-launch --dry-run`; check the argv and env against the table.
  3. `wt queue conductor-launch`; in the new session confirm `/context` stays under the
     150k window after a long run and that `wt queue conductor-session list` shows the id
     with `source: launch` before the Monitor is armed.
  4. Quit it, run `wt queue conductor-launch --resume`, and confirm the same id continues.
  5. Run `wt queue conductor-cost` after a day and check median context against E2.
- **Regression guard:** full `uv run pytest`; `tests/test_agentic_conductor_cost.py` still
  passes (the registry format is unchanged: `source: "launch"` is a new value of an existing
  free-text field); `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Merge. No data migration: `sessions.jsonl` gains records with `source: "launch"`.
2. Set `agentic.conductor.prices` for the launch model (SPEC-0298 rollout step 2).
3. Restart the conductor with `wt queue conductor-launch` (SPEC-0298 rollout step 3); the E1
   to E6 window starts at the first campaign admitted after that.
4. Tune `agentic.conductor.launch.*` from `wt queue conductor-cost` and `wt queue
   mvp-report` if E1 or E2 is missed; defaults are code-held and no config is required
   beyond the price.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None. The default model, effort and thresholds are the conductor's choices from the
  2026-10-31 evidence; they may be tuned by a note in *As shipped* from the next
  `mvp-report` window and are not a reason to escalate.
