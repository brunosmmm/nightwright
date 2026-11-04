---
id: SPEC-0424
title: "Launch freshness and steward dontAsk"
status: accepted
owner: user
created: 2026-11-04
source_idea: IDEA-930
parent: SPEC-0419
milestone: "M9: agentic workflow"
tags: [agentic, launch, permissions, conductor, steward, doctor]
---

## Context

Child 5 of [SPEC-0419](./0419-self-healing-queue.md).
[SPEC-0319](./0319-wt-queue-conductor-launch-steward-launch-start.md) added `nw queue
conductor-launch` and `steward-launch`. [SPEC-0382](./0382-conductor-must-never-block-on-the-human-high-it.md)
made the conductor launch with `--permission-mode dontAsk` and an allow-list
(`launch.DEFAULTS["conductor_*"]`).

`launch.build_plan()` (`src/nightwright/agentic/launch.py:278`) sets a permission mode **only for
the conductor**. `nw queue steward-launch Meta-Tools --dry-run` (run 2026-11-04) shows no
`--permission-mode`, so stewards run in Claude Code's auto mode. Their allow-list
(`steward_allowed_tools`) has only git rebase and commit verbs plus the shared push and PR rules.

In the audit, the live conductor was still a pre-SPEC-0382 launch. The auto-mode classifier blocked
`nw queue merge` twice and `nw queue stop` once ("Merge Without Review", "Interfere With
Workloads"), despite the user-scope `Bash(nw queue *)` allow rules.

Nothing detects a stale launch. `doctor._unguarded_launch` (`src/nightwright/doctor.py:389-398`)
checks only whether the newest conductor record carries the `AskUserQuestion` deny.
`launch.register()` records `disallowed_tools` and nothing that would show the rest of the launch
changed.

## Goals / Non-goals

**Goals**
- Stewards launch with `--permission-mode dontAsk` and an allow-list that covers their job:
  - read anything;
  - mail and every `nw` command;
  - run tests and spec lint;
  - make fix branches in worktrees under `.nw-worktrees/`;
  - request gated merges.

  Everything else is denied without a prompt. That includes pushes to the base, `gh pr merge`, and
  edits outside worktrees.
- Every launch records a **launch hash**. A conductor or steward session whose recorded hash
  differs from what `build_plan` produces now is reported stale by `nw queue conduct-status`,
  `nw queue conductor-brief` and `nw doctor`, with the exact relaunch command.
- The event follower files one `launch_stale` event, and one mail to the human, per stale reader
  and launch hash.

**Non-goals**
- Relaunching automatically. A session cannot replace itself, and the terminal is the human's.
  The request names the `--resume` command, which keeps the session's context.
- Editing `~/.claude/settings.json`. User-scope rules change only through `nw skills install`
  (SPEC-0320 `agent_permissions.sync`), which the human runs. No code here writes settings.
- Changing persona-run permissions.

## Decision

1. **Steward permission mode.** `launch.DEFAULTS` gains `steward_permission_mode: "dontAsk"`,
   validated like `conductor_permission_mode`. It is settable at
   `agentic.conductor.launch.steward.steward_permission_mode`. `build_plan` passes
   `--permission-mode` for both roles.
2. **Steward allow-list.** `steward_allowed_tools` is extended to the following. The existing
   rebase/commit verbs, push rules and every deny rule stay; deny rules win.
   - `Read`, `Grep`, `Glob`;
   - `Edit(//**/.nw-worktrees/**)`, `Write(//**/.nw-worktrees/**)`;
   - `Bash(nw *)`, `Bash(uv run nw *)`, `Bash(uv run pytest *)`, `Bash(uv sync*)`;
   - `Bash(python3 tools/spec_lint.py*)`, `Bash(python3 tools/affected_tests.py*)`;
   - the conductor's read-only git verbs (`_CONDUCTOR_GIT_VERBS`, both prefixes);
   - `Bash(git worktree add ../.nw-worktrees/*)`, `Bash(git worktree list)`;
   - `Bash(gh pr view *)`, `Bash(gh pr checks *)`, `Bash(gh pr list *)`;
   - `Bash(ls *)`, `Bash(mkdir *)`, `Bash(ps *)`, `Bash(tail *)`;
   - `AskUserQuestion`. The steward is the human's channel in its own attended session; it is
     never in the steward's deny list.

   `gh pr merge *` stays denied, and so do pushes to the base. A steward merges only by asking
   the conductor for `nw queue merge` (SPEC-0233).
3. **Launch hash.**
   - `launch.launch_hash(plan) -> "sha256:<hex16>"` is a hash of canonical JSON of `{role, name,
     cwd, argv, env, env_remove}`. In `argv`, the session-id value and the `--session-id` /
     `--resume` flag are replaced by a fixed token, so a `--resume` launch hashes equal to a fresh
     one.
   - `register()` stores `launch_hash` and `nw_build` (`buildid`) in the session record. The
     refresh on `--resume` that SPEC-0382 added covers both.
4. **Freshness.** `launch.freshness(cfg) -> list[dict]` returns, for `conductor` and every
   `steward:<P>` reader with a registered session, a row for the newest record: `{reader,
   session_id, source, recorded_hash, current_hash, stale, why, fix}`.
   - `current_hash` comes from `build_plan(cfg, role, project, session_id="<fixed>")`.
   - `stale` is true for a `source: launch` record whose hash is missing or differs.
   - A hand-started session (`source` other than `launch`) is `managed: false`, never stale.
   - `fix` is `nw queue conductor-launch --resume` or `nw queue steward-launch <P> --resume`.
   - A project that no longer resolves gives `why: "project unknown"`, not an exception.
5. **Surfacing.**
   - `nw queue conduct-status --json` (`cli_conduct.conduct_status`) gains `launch:
     [freshness rows]`.
   - `conductor-brief` prints a `launch:` line per stale reader.
   - `nw doctor` gains check `agentic.launch_fresh` (`doctor.check_launch_fresh`). It warns per
     stale reader with `fix`, and is ok when none is stale. `check_conductor`'s
     `_unguarded_launch` stays.
6. **Relaunch request.**
   - In `events.sync(ping=True)` (follower only), after the reminders, `launch.request_relaunch(cfg,
     now)` appends a `launch_stale` event (data: reader, session_id, recorded_hash, current_hash,
     fix) and mails `human` (kind `info`, text with `fix`).
   - It does this once per `(reader, current_hash)`, remembered in
     `<agentic>/conductor/launch_requests.json` under its lock.
   - `launch_stale` **wakes** the conductor (it is not in `quiet.RULES`' held set).
   - Conductor skill row `launch_stale`:
     - reader `steward:<P>`: mail the steward `kind: info` "your launch is stale: finish the event
       in hand, then tell the human in your session to run `<fix>`";
     - reader `conductor`: give the human one chat line with `fix` and carry on;
     - in both cases, record `nw queue decision --kind other --rule launch_stale`.

     The steward skill says that on this notice the steward tells the human the command once.

## Design

Files and functions:

- `src/nightwright/agentic/launch.py`:
  - `DEFAULTS` (`steward_permission_mode`, extended `steward_allowed_tools`);
  - `LIST_KEYS`, `_good`;
  - `build_plan` (a mode for both roles; `plan["permission_mode"]`);
  - `launch_hash`, `register` (stores hash and build);
  - `freshness`, `request_relaunch`.
- `src/nightwright/agentic/conductor_cost.py`: `record_session(extra=…, refresh=…)` is unchanged;
  `launch_hash` / `nw_build` travel in `extra`.
- `src/nightwright/agentic/cli_conduct.py`: `conduct_status` adds `launch`.
- `src/nightwright/agentic/brief.py`: a `launch` section (stale rows only).
- `src/nightwright/doctor.py`: `check_launch_fresh`, registered in the check list.
- `src/nightwright/agentic/events.py`: `sync()` calls `launch.request_relaunch` in the ping
  branch, wrapped like `remind_due` (never breaks the follower).
- `src/nightwright/agentic/quiet.py`: `launch_stale` is not held; the skill paragraph is unchanged.
- `skills/nw-conduct/SKILL.md`: the `launch_stale` row.
- `skills/nw-steward/SKILL.md`: a "stale launch notice" line, plus "your launch runs dontAsk:
  work in `../.nw-worktrees/<P>/<name>`; a denied command means it is outside your job — ask the
  conductor".

## Alternatives considered

- **Put the steward rules in `~/.claude/settings.json` through `nw skills install`.** Lost: user
  scope applies to every Claude session the human runs. Launch flags scope the rules to the
  steward session.
- **Keep stewards in auto mode.** Lost: the classifier denied legitimate `nw` commands, and
  stewards then waited on the human.
- **Compare nightwright versions instead of a hash.** Lost: most releases do not change the
  launch. A hash of the effective argv flags exactly the launches that differ.

## Acceptance criteria

- [ ] AC1: `nw queue steward-launch Meta-Tools --dry-run` shows `--permission-mode dontAsk` and the
      extended allow-list. `conductor-launch --dry-run` is unchanged apart from the recorded hash.
      `agentic.conductor.launch.steward.steward_permission_mode: auto` gives `--permission-mode
      auto`, and a bad value falls back to `dontAsk`.
- [ ] AC2: `launch_hash` is equal for a fresh and a `--resume` plan of the same config, and
      differs when any flag, the model, the env or the allow-list differs.
- [ ] AC3: A registered `source: launch` record with a missing or different hash is reported
      stale, with the right `fix`, by `conduct-status --json`, `conductor-brief` and `nw doctor`
      (warn). After `--resume` re-registers, the record is not stale. A hand-started session is
      never stale.
- [ ] AC4: The follower appends exactly one `launch_stale` event and one human mail per (reader,
      current hash), even across restarts. A new config change yields one more.
- [ ] AC5 (real binary): a steward session started with the plan's flags (`claude` 2.1.x, `-p`,
      model haiku, scratch data and config dirs, scratch repo):
      - allowed: `nw queue list`, `uv run nw --help`, `python3 tools/spec_lint.py --help`,
        `git worktree add ../.nw-worktrees/T/x -b steward/x`, an Edit and a `git -C <wt>
        commit -m x` inside it;
      - denied without a prompt: `git push origin main`, `gh pr merge 1`, an Edit outside
        `.nw-worktrees`, `rm -rf x`;
      - in an interactive steward launch, `AskUserQuestion` reaches the terminal.

      Results are recorded in As shipped.
- [ ] AC6: The conductor skill has the `launch_stale` row. The steward skill has the notice and
      dontAsk lines. No code path writes `~/.claude/settings.json`.

## Test plan

- **Automated tests**
  - `tests/test_agentic_launch.py` (extend):
    - steward argv has the mode; settings override and fallback;
    - the allow-list contains the new rules and the deny list still has every push, `gh pr merge`
      and guard rule (AC1);
    - `launch_hash` equality and inequality cases (AC2);
    - `freshness` over a tmp `sessions` registry: stale, fresh, unmanaged, unknown project (AC3).
  - `tests/test_doctor.py` (extend): `check_launch_fresh` warn and ok (AC3).
  - `tests/test_agentic_conductor_cost.py` / the conduct-status tests: `launch` key (AC3).
  - `tests/test_agentic_events*.py` (extend): `request_relaunch` de-dup across two `sync(ping)`
    calls and a process restart (the state file), plus one more after a hash change (AC4).
  - `tests/test_skills.py`: rows present; a grep test that no module under `src/` opens
    `settings.json` for writing except `agent_permissions.py` (AC6).
- **Real verification (beyond fakes)**: AC5 against the real `claude` binary, following the
  SPEC-0382 procedure:
  - scratch `NW_DATA_DIR` / `NW_CONFIG_DIR`, a scratch git repo registered as project `T`, and a
    `nw` wrapper on PATH that runs this worktree;
  - run `claude -p --settings <scratch>/settings.json` with the argv of `nw queue steward-launch T
    --dry-run --json` (prompt replaced by one command per run, model haiku);
  - record each allow and deny outcome and the CLI version;
  - for `AskUserQuestion`, run one interactive steward launch under tmux, prompt "ask me a
    question", and record that it appears.

  Also run `nw doctor` against a scratch registry holding an old-hash record (warn), then after a
  real `steward-launch T --resume` (ok).
- **Live check after landing** (manual): `nw doctor` on the human's machine lists the currently
  running conductor and stewards as stale until relaunched. Record it, then confirm ok after the
  human's `--resume`s.
- **Regression guard**: affected tests and `python3 tools/spec_lint.py`. The existing SPEC-0382
  launch and guard tests pass unchanged.

## Clock Log

CLOCK-IN: [timestamp]
CLOCK-OUT: [timestamp]

## Rollout / migration

1. Land. Every existing launch record has no hash, so it is stale, and the follower requests one
   relaunch per reader. This is intended: it moves the stewards to dontAsk.
2. The human runs the `--resume` commands once.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## As shipped

_To be filled at close: the AC5 real-binary record and the live doctor check._

## Open questions

_None._
