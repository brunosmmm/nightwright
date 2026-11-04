---
id: SPEC-0424
title: "Launch freshness and steward dontAsk"
status: done
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
   - ~~`AskUserQuestion`~~ — **changed while building:** claude 2.1.318 denies `AskUserQuestion`
     under `dontAsk` even when it is allow-listed (in `--allowedTools` or in `--settings`
     `permissions.allow`), so the rule is not shipped. The steward is still the human's channel in
     its attended session: it asks in plain text in the chat. `AskUserQuestion` is in no steward
     deny list.

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
   - The row is for the newest `source: launch` record (the one `--resume` resumes), else the
     newest record; a later hand-registered record (`conductor-session add`) does not hide it.
     *(Changed while building: the live registry's newest conductor rows were `manual`, which
     would have hidden the stale launched conductor.)*
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
       in hand, then give the human in your session the command `<fix>`" (worded so, as shipped:
       the SPEC-0382 skill test bans the phrase "tell the human" in the conductor skill);
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
  conductor", plus "the question picker is not available under dontAsk: ask the human in plain
  text".

## Alternatives considered

- **Put the steward rules in `~/.claude/settings.json` through `nw skills install`.** Lost: user
  scope applies to every Claude session the human runs. Launch flags scope the rules to the
  steward session.
- **Keep stewards in auto mode.** Lost: the classifier denied legitimate `nw` commands, and
  stewards then waited on the human.
- **Compare nightwright versions instead of a hash.** Lost: most releases do not change the
  launch. A hash of the effective argv flags exactly the launches that differ.

## Acceptance criteria

- [x] AC1: `nw queue steward-launch Meta-Tools --dry-run` shows `--permission-mode dontAsk` and the
      extended allow-list. `conductor-launch --dry-run` is unchanged apart from the recorded hash.
      `agentic.conductor.launch.steward.steward_permission_mode: auto` gives `--permission-mode
      auto`, and a bad value falls back to `dontAsk`.
- [x] AC2: `launch_hash` is equal for a fresh and a `--resume` plan of the same config, and
      differs when any flag, the model, the env or the allow-list differs.
- [x] AC3: A registered `source: launch` record with a missing or different hash is reported
      stale, with the right `fix`, by `conduct-status --json`, `conductor-brief` and `nw doctor`
      (warn). After `--resume` re-registers, the record is not stale. A hand-started session is
      never stale.
- [x] AC4: The follower appends exactly one `launch_stale` event and one human mail per (reader,
      current hash), even across restarts. A new config change yields one more.
- [x] AC5 (real binary): a steward session started with the plan's flags (`claude` 2.1.x, `-p`,
      model haiku, scratch data and config dirs, scratch repo):
      - allowed: `nw queue list`, `uv run nw --help`, `python3 tools/spec_lint.py --help`,
        `git worktree add ../.nw-worktrees/T/x -b steward/x`, an Edit and a `git -C <wt>
        commit -m x` inside it;
      - denied without a prompt: `git push origin main`, `gh pr merge 1`, an Edit outside
        `.nw-worktrees`, `rm -rf x`;
      - ~~in an interactive steward launch, `AskUserQuestion` reaches the terminal.~~ Amended:
        the real binary always denies it under dontAsk (recorded below); the steward asks in
        plain text instead.

      Results are recorded in As shipped.
- [x] AC6: The conductor skill has the `launch_stale` row. The steward skill has the notice and
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

CLOCK-IN: 2026-11-04 (IDEA-930)
CLOCK-OUT: 2026-11-04 (IDEA-930)

## Rollout / migration

1. Land. Every existing launch record has no hash, so it is stale, and the follower requests one
   relaunch per reader. This is intended: it moves the stewards to dontAsk.
2. The human runs the `--resume` commands once.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

Shipped as designed, with two changes made while building (both also recorded in Decision):
`AskUserQuestion` is not in the steward allow-list, and freshness judges the newest
`source: launch` record.

- **Code:** `launch.py` (`steward_permission_mode`, the extended `steward_allowed_tools`,
  `build_plan` gives a mode to both roles and a `launch_hash` to the plan, `launch_hash`, `register`
  stores `launch_hash` + `nw_build`, `freshness`, `fix_for`, `request_relaunch` with
  `<agentic>/conductor/launch_requests.json` under `launch_requests.json.lock`, checked at most
  every 60 s); `events.py` (`launch_stale` type; `sync(ping=True)` calls `request_relaunch` in a
  never-raising wrapper); `quiet.py` (`launch_stale: wake`); `cli_conduct.py` (`launch` key);
  `brief.py` (`launch:` section, after `conductor`, one `- <reader> stale (<why>); run `<fix>``
  line per stale reader); `doctor.py` (`check_launch_fresh`, id `agentic.launch_fresh`). The
  relaunch request never stops, kills or signals a session: it is an event plus a mail.
- **Steward argv** (`steward-launch Meta-Tools --dry-run`, real config): `claude -n
  steward:Meta-Tools --session-id <uuid> --allowedTools <93 rules> --disallowedTools <188 rules>
  --permission-mode dontAsk --model claude-sonnet-5-5 --effort medium --autocompact 150000
  <prompt>`. The conductor's argv is unchanged; its plan gains only `launch_hash`.
- **Tests:** `tests/test_agentic_launch.py` (mode/override/fallback, allow and deny lists, a
  steward allow-vs-deny command matrix, hash equality/inequality, register, freshness stale /
  fresh / unmanaged / unknown project / later manual record, relaunch de-dup across calls, a
  restart and a hash change, conduct-status and brief); `tests/test_doctor.py` (warn then ok);
  `tests/test_skills.py` (rows); `tests/test_no_settings_writes.py` (only `agent_permissions.py`
  builds-and-writes a `settings.json` path; a `tree_guard` module per SPEC-0421, since it scans
  `src/`). Two SPEC-0353 assertions were adjusted for the longer, de-duplicated list, and two
  SPEC-0382 ones for the steward now having a mode and sharing read-only rules with the conductor
  (the 18 rebase rules and the conductor's WIP verbs stay off the other role). Affected
  tests (`tools/affected_tests.py`, `-n 2`): 3415 passed, 4 failed; three were the stale
  assertions above (fixed, re-run green: 641 passed across the launch, guard, skills, doctor,
  quiet, brief and conduct-cli files), one was `test_remote_gates.py::
  test_transport_failures_fall_back_with_cooldown[drop-ssh]`, a load-dependent flake unrelated to
  this change (passes alone and in a full `-n 2` run of its file, here and on main). After rebasing onto SPEC-0421/0422, a second affected run: 5010 passed, 2
  failed: the SPEC-0421 guard registry flagged the settings scan in `test_skills.py` (moved to its
  own `tree_guard` module, green), and `test_dashboard_serve_cli.py::
  test_jobs_alone_serves_on_its_default_bind`, a load flake (its file passes 24/24 twice alone).
- **AC5, real binary** (`claude` 2.1.318; flags checked against `claude --help`; `-p`, model
  haiku, `--setting-sources project` so user rules do not count, `--output-format json`, scratch
  `NW_DATA_DIR` / `NW_CONFIG_DIR`, a scratch repo registered as project `T`, `nw` wrapper running
  the worktree, argv from `steward-launch T --dry-run --json`):
  - ran with no denial: `nw queue list`, `uv run nw --help`, `python3 tools/spec_lint.py --help`,
    `git worktree add ../.nw-worktrees/T/x -b steward/x`, a Write in that worktree, `git -C
    ../.nw-worktrees/T/x add a.txt` and `commit -m x` (the commit landed on `steward/x`);
  - denied without a prompt (each in `permission_denials`): `git push origin main`, `gh pr merge
    1`, a Write to `repo/OUTSIDE.txt`, `rm -rf x`;
  - interactive launch under tmux with the plan's flags: `AskUserQuestion` is denied ("denied
    because Claude Code is running in don't ask mode"), with the rule in `--allowedTools` and
    again in `--settings` `permissions.allow`. Hence the change above.
  - side findings filed as ideas: claude warns that 8 SPEC-0353 deny rules (`Bash(git push *
    nw/*:*)` and siblings) are read with a literal `*`; `PERMISSION_MODES` accepts `default`, which
    2.1.318 no longer lists (`manual` is the new name).
- **Freshness on real registry data** (scratch data dir): a real steward launch by the
  pre-SPEC-0424 code (`git archive main`, real `claude`, exited) left a record without a hash;
  `nw doctor` warned `agentic.launch_fresh` "1 stale launch(es): steward:T" with fix `nw queue
  steward-launch T --resume`; `conduct-status --json` `launch[]` and `conductor-brief` showed
  it; two `sync(ping=True)` calls filed exactly one `launch_stale` event and one human mail. A
  real `nw queue steward-launch T --resume` (new code, `--permission-mode dontAsk`) refreshed the
  row with `launch_hash` and `nw_build`, and `nw doctor` went ok.
- **Live check** (read-only, the human's data, before landing): `agentic.launch_fresh` reports the
  conductor (`cb3a50a4`) and the stewards comfort-assistant, Meta-Tools and pwrctl stale, "no
  launch hash recorded", as the rollout expects. Confirming ok after the human's `--resume`s is
  left to the human.

## Open questions

_None._
