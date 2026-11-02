---
id: SPEC-0382
title: "Conductor never blocks on the human: launch guard and hook backstop"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
kind: feature
source_idea: IDEA-775
tags: [agentic, conductor]
depends_on: [SPEC-0232, SPEC-0235, SPEC-0319]
---

## Context

One conductor serves every project in the queue. On 2026-11-02 it called `AskUserQuestion` twice
(conductor session `cb3a50a4-409e-4aa8-8fc6-2fe025abf8c3`) and each call stalled every project until
the human answered:

| asked (UTC) | blocked | what |
|---|---|---|
| 15:10:37 | 90 s | "Direct merge": checking whether a steward-relayed HUMAN ORDER was real |
| 16:44:10 | 63 s | "IDEA-714 DoD": defer / run / split a real-box check, after a `parked triage_escalated` event |

In both cases a steward for Meta-Tools was armed, so the existing steward route was available and
skipped. Causes found in the investigation (plan: `/home/bruno/work/nw-plans/conductor-never-blocks.md`):

- Nothing in code forbids `AskUserQuestion` for the conductor. `launch.py` (`build_plan`, conductor
  launch) passes no `--disallowedTools AskUserQuestion` and no `--permission-mode`, so the session
  inherits the user's auto mode, whose classifier can also fall back to an interactive prompt (a
  third way to block; the session had 15 rejected tool calls).
- The `gate_triager` and `conflict_resolver` personas say "what the human must decide"
  (`personas/gate_triager.md:37`, `personas/conflict_resolver.md:95`; the same phrase is the help
  text of `nw queue escalate --question`, `cli_queue.py:2010`). The conductor read the park detail
  "The human must either (a) run ... or (b) waive" as "ask the human now".
- `escalations._announce` mails only `human`, never the owning project's steward (slice B, out of
  scope here).

**Already in the repo** (not part of this spec's edits): the `nw-conduct` rule "Never ask the human,
never wait on the human" (`skills/nw-conduct/SKILL.md:33-39`), its `message.request.human_order`
row (line 142), the "Never `AskUserQuestion`" line in the escalate convention (line 227), and the
conductor-never-asks sentence in `nw-orient` (line 40). A grep of `nw-conduct` and `nw-orient` finds
no "tell the human", "unless told" or "wait for the human" outside that rule. Still missing, and
owned by slice B: a `human_decision_timeout` row in `nw-conduct` and step 4b in `nw-steward`.

## Goals / Non-goals

**Goals**
- The conductor never calls `AskUserQuestion`, enforced by launch flags and a hook, not only by prose.
- The conductor launch no longer falls back to an interactive permission prompt (`dontAsk`), without
  starving the conductor's sub-agents of the tools their normal work needs.
- A conductor started by hand is covered by the same deny.
- Persona and CLI help wording stops telling the triagers and the conductor that "the human must"
  decide.
- The already-landed skill wording stays true (guard test).

**Non-goals** (each is slice B, to be a follow-up spec/idea that depends on this one; priority 2)
- Steward-first escalation routing: `escalations._announce` mailing `steward:<P>` when its reader
  lock is held, `routed_to` / `steward_mail_id` record fields, `nw queue escalate
  --to-steward/--no-steward`.
- The `human_decision_timeout` ticker event, its `quiet.RULES` wake entry, and the `nw-conduct` row
  for it.
- `nw-steward` step 4b and the "escalate: needs the human's decision" rewording.
- `routed_to` in `conductor-brief`.
- Changing the escalation answer flow (`nw escalations answer`, SPEC-0235) or restricting
  `AskUserQuestion` for the steward or any other role.
- Making the human's own interactive sessions non-interactive.

## Decision

The conductor is a non-interactive router. Its launch denies `AskUserQuestion` and runs under
`--permission-mode dontAsk`, with an explicit allow-list so sub-agents keep working. A PreToolUse hook
(`nw queue conductor-guard`), installed by `nw skills install`, denies `AskUserQuestion` for the
registered conductor session, covering a hand-started conductor. `nw doctor` warns when the
registered conductor was launched without the deny. Persona and CLI wording is changed from "the
human must decide" to "needs a human-only decision (routed via the project steward)". This is slice A
(priority 1); slice B is listed under Non-goals. Stopgap until this merges: add `"AskUserQuestion"`
to the conductor's `agentic.conductor.launch.disallowed_tools`, then `nw queue conductor-launch
--resume`.

## Design

### 1. Conductor launch guard (`src/nightwright/agentic/launch.py`)
- New settings in `DEFAULTS`:
  - `conductor_disallowed_tools` (default `["AskUserQuestion"]`);
  - `conductor_permission_mode` (default `dontAsk`; validated against a closed enum, a bad value falls
    back to the default like other keys in `settings()`);
  - `conductor_allowed_tools` (see below).
- `build_plan` for `role == "conductor"` merges `conductor_disallowed_tools` into the effective
  `disallowed_tools` and `conductor_allowed_tools` into `--allowedTools` (deduplicated, the same
  pattern as `steward_allowed_tools`: merged, never its own flag) and appends `--permission-mode
  <conductor_permission_mode>` to `argv` before the prompt. `role == "steward"` gets none of the three.
  The plan's `disallowed_tools` and a new `permission_mode` field reflect it, so `--dry-run --json`
  shows both.
- `dontAsk` denies a non-allowlisted tool call instead of prompting. The rules provisioned by `nw
  skills install` (`agent_permissions.ALLOW_RULES`, SPEC-0320) only allow `Bash(nw <group> *)`, and the
  existing conductor `allowed_tools` default adds only the push rules and `gh pr create/edit/comment`.
  The conductor's sub-agents (rebase conflicts in campaign worktrees, looking into stuck runs) run
  under the parent's permission mode, so `dontAsk` applies to them too. The conductor launch therefore
  also adds `conductor_allowed_tools`, default: `Read`, `Grep`, `Glob`, `Edit` and `Write` limited to
  the worktrees directory (`.nw-worktrees`, as path-scoped rules), `Bash(git *)`, `Bash(uv run pytest
  *)`, `Bash(ps *)`, `Bash(tail *)`. The existing push/merge denies in `disallowed_tools` still win
  over `Bash(git *)`. If `conductor_permission_mode` is set to `default`, only the `AskUserQuestion`
  deny and the hook apply (the allow additions are harmless).

### 2. Hook backstop for a hand-started conductor
- New command `nw queue conductor-guard`: reads the PreToolUse hook JSON on stdin and takes
  `session_id` from it (falling back to `$CLAUDE_CODE_SESSION_ID`). It exits 2 with the message
  "conductor never asks the human: use steward:<P> / nw queue escalate" when that id is a registered
  conductor session (`CC.sessions(cfg, "conductor")`); it exits 0 for any other session, including
  when no conductor is registered or the registry cannot be read (fail open, so it can never block a
  human's session).
- Registration of a hand-started conductor: when `/nw-conduct` arms the Monitor on `nw queue events
  --follow --for conductor`, the follower calls `conductor_cost.record_session(cfg, "conductor")`
  (`events.py:1097`), which writes the session id from `$CLAUDE_CODE_SESSION_ID` to the registry
  (`source: follower`). A session that needs registering before it arms, or after the fact, uses `nw
  queue conductor-session add <session_id>` (`source: manual`, `cli_conductor.py`). Launched
  conductors are registered by `conductor-launch` itself (`source: launch`, `launch.py:267`).
- `nw skills install` (via `agent_permissions.install/sync`, `~/.claude/settings.json`) writes a
  PreToolUse hook entry with matcher `AskUserQuestion` running that command, idempotently (no duplicate
  on re-run; other hooks untouched).

### 3. Persona and CLI wording
`gate_triager.md:37`, `conflict_resolver.md:95` and the `nw queue escalate --question` help text
(`cli_queue.py:2010`): "what the human must decide" becomes "what needs a human-only decision (routed
via the project steward)". `review_triager.md` is checked and changed only if it carries the phrase
(today it does not). Text only; triager behavior and output schema unchanged.

### 4. `nw doctor`
`nw doctor` warns when the registered conductor session's launched argv (the `source: launch` record,
`launch.py:210`) lacks the `AskUserQuestion` deny. A hand-started conductor (`source` other than
`launch`) is not warned about: the hook is its guard.

### 5. Skills (guard only)
`skills/nw-conduct/SKILL.md` and `skills/nw-orient/SKILL.md` already carry the required rule text
(see Context). This spec adds no skill edit; it adds a test that keeps the text true. The
`human_decision_timeout` row and `nw-steward` step 4b belong to slice B.

## Alternatives considered

- **Skill text only** — the 2026-11-02 failures were already against explicit "never block" prose;
  prose alone is not enforcement. Launch flags plus a hook make the tool call impossible.
- **Hook only** — does not cover the auto-mode classifier prompt fallback; `dontAsk` does.
- **`dontAsk` without an allow-list** — sub-agents would be refused `git rebase`, `Edit` and `ps`
  instead of prompted, silently breaking the conductor's conflict and stuck-run handling.
- **One spec for both slices** — ten acceptance criteria over about ten modules with different
  priorities; slice A fixes the incident, slice B is a routing feature that depends on it.

## Acceptance criteria

- [x] AC1: `nw queue conductor-launch --dry-run --json` argv contains `--disallowedTools` with
      `AskUserQuestion` (the existing push/merge denies kept), `--permission-mode dontAsk`, and the
      `conductor_allowed_tools` defaults in `--allowedTools`; `nw queue steward-launch <P> --dry-run
      --json` contains none of these three additions. All three settings are overridable via
      `agentic.conductor.launch`, and a bad `conductor_permission_mode` falls back to `dontAsk`.
- [x] AC2: `nw queue conductor-guard` reads `session_id` from the PreToolUse JSON on stdin and exits 2
      with the guidance message for a registered conductor session id (whether registered by launch,
      by the follower, or by `conductor-session add`), and 0 for any other session id, no registered
      conductor, or an unreadable registry.
- [x] AC3: `nw skills install` writes the `AskUserQuestion` PreToolUse hook entry, a second run adds no
      duplicate, and pre-existing unrelated hooks are preserved.
- [x] AC4: the `gate_triager` and `conflict_resolver` persona prompts and the `nw queue escalate
      --question` help contain neither "the human must" nor "a human must decide"; `review_triager`
      likewise; the existing persona tests pass unchanged.
- [x] AC5: `skills/nw-conduct/SKILL.md` contains "Never call `AskUserQuestion`" and the
      `message.request.human_order` row, and none of "tell the human" or "unless told", nor an
      instruction to wait for the human anywhere outside the *Never ask the human* rule (the assertion
      excludes that paragraph, which itself says `never "wait for the human"`); `skills/nw-orient/
      SKILL.md` contains the conductor-never-asks sentence. This test passes against the current
      files. The quiet-stream paragraph in the skill still matches `quiet.RULES` (existing comparison
      test).
- [x] AC6: `nw doctor` warns when the registered conductor session was launched (`source: launch`)
      with an argv lacking the `AskUserQuestion` deny, and does not warn when the deny is present or
      the conductor was not launched by `conductor-launch`.
- [x] AC7: against the real `claude` binary, (a) a conductor launched with `nw queue conductor-launch`
      in a scratch data dir and prompted to call `AskUserQuestion` has the call denied, (b) a
      sub-agent dispatched by that conductor can edit a file in a scratch worktree and run `git
      status` without a denial, and (c) a plain `claude` session registered as conductor (by `nw
      queue conductor-session add <session_id>`, or by arming `nw queue events --follow --for
      conductor` inside it) with the hook installed is denied `AskUserQuestion` by the hook. Results
      are recorded in the spec's As shipped section.

## Test plan

- **Automated tests**
  - `tests/test_agentic_launch.py`: argv/plan per role (AC1), settings override and bad-value
    fallback, steward plan lacks all three additions, push/merge denies still present.
  - New `tests/test_agentic_conductor_guard.py`: exit codes for conductor (registered via each of the
    three sources) / other / unregistered / unreadable registry; session id read from stdin JSON
    (AC2).
  - `tests/test_skills.py`: hook install and idempotency with pre-existing hooks (AC3); skill text
    assertions (AC5, paragraph excluded), including the existing quiet-stream vs `quiet.RULES`
    comparison.
  - Persona and CLI help tests: prompt text and `nw queue escalate --help` assertions (AC4).
  - `tests/test_doctor.py` (or the existing doctor tests): warn / no warn cases (AC6).
- **Manual verification (real binary, AC7)**, in a scratch data dir:
  1. `nw queue conductor-launch`; prompt the session "call AskUserQuestion"; confirm the call is
     denied and no question reaches the terminal.
  2. In that conductor, ask it to dispatch a sub-agent that edits a file under a scratch worktree in
     `.nw-worktrees` and runs `git status`; confirm no permission denial.
  3. Install the hook (`nw skills install`), start a plain `claude` session, register it with `nw queue
     conductor-session add <session_id>` (the id is in its transcript path / `$CLAUDE_CODE_SESSION_ID`),
     prompt it to call `AskUserQuestion`, and confirm the hook denies with the guard message.
  Record each outcome in As shipped.
- **Regression guard**: affected tests (`python3 tools/affected_tests.py --run uv run pytest -q`),
  `python3 tools/spec_lint.py`; steward launch and the other launch settings tests must pass
  unchanged. Post-ship check on the live queue: after `nw queue conductor-launch --resume`, the
  conductor transcript contains no `AskUserQuestion` call.

## Rollout / migration

1. Land the launch guard (Design 1), `conductor-guard` + hook install (2), wording (3), doctor (4) and
   the skill guard test (5). Existing conductor sessions pick up the launch flags only after `nw queue
   conductor-launch --resume`; the hook reaches the human's `~/.claude/settings.json` on their next
   `nw skills install`.
2. Slice B is captured as a follow-up idea/spec that depends on SPEC-0382; until then the conductor
   routes human-only decisions with the existing `nw queue escalate` (asynchronous `human_decision`
   park) and mail to `steward:<P>` by hand, as the landed `nw-conduct` rule says.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass; manual real-binary steps performed and noted.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None. Decided here: fail-open guard; `dontAsk` with a conductor allow-list; hand-started conductors
register through the follower (or `conductor-session add`); the spec is narrowed to slice A and
slice B is a Non-goal follow-up.

### Test record (2026-11-02)

- Automated: `tests/test_agentic_launch.py`, new `tests/test_agentic_conductor_guard.py`,
  `tests/test_agent_permissions.py`, `tests/test_skills.py`, `tests/test_doctor.py`,
  `tests/test_agentic_decisions.py`, `tests/test_rename_nightwright.py`, the persona tests and
  `python3 tools/spec_lint.py` pass. The full suite was left to gating.
- Real binary (`claude` 2.1.316, scratch dir and scratch XDG data dir, model haiku, `claude -p` with
  the exact permission flags from `nw queue conductor-launch --dry-run --json`; the interactive
  `conductor-launch` TUI itself was not started):
  - (a) with `--disallowedTools AskUserQuestion --permission-mode dontAsk`, `AskUserQuestion` is not
    in the session's tool list at all; the model reports it unavailable, no question is possible.
  - (b) a session under those flags edited `a.txt` in `<scratch>/.nw-worktrees/wt1` and ran `git
    status` without a denial; `git push origin feature/x` was denied. The first rule form
    `Edit(**/.nw-worktrees/**)` did NOT match (paths match relative to cwd); `Edit(//**/.nw-worktrees/**)`
    does, and is the shipped default. A sub-agent dispatched through the Agent tool in such a session
    also edited the file and ran `git status` with no denial.
  - (c) the installed hook entry (`nw skills install --settings`) and `nw queue conductor-session
    add <id>`: in `claude -p` `AskUserQuestion` is not offered at all, so the hook could not be
    triggered by it. The same command was therefore run with the matcher swapped to `Bash`: the
    registered session's `echo hello` was refused ("PreToolUse:Bash hook error: [nw queue
    conductor-guard]: conductor never asks the human ..."), an unregistered session got `hello`.
    This proves the harness passes `session_id` on stdin and honours exit 2. **Open:** the real
    `AskUserQuestion` matcher in an interactive session (AC7 (c)) is not exercised, so AC7 stays
    unticked and the spec stays `in-progress`; `--tools AskUserQuestion` in `-p` mode does not offer
    the tool either.

### Test record (2026-11-02, loop 2: interactive real binary)

- Automated: `tests/test_agentic_launch.py`, `tests/test_agentic_conductor_guard.py`,
  `tests/test_agent_permissions.py`, `tests/test_doctor.py` pass (new: a resumed old-style launch
  record stops warning and a resume without the deny warns again; the unreadable-registry test now
  makes `CC.sessions` raise, plus a monkeypatched `OSError` test; a launch behind a newer
  hand-started record does not warn; `"hooks": null` is accepted). The full suite was left to gating.
- Real binary, **interactive** `claude` 2.1.316 under tmux (own socket), scratch `NW_DATA_DIR` /
  `NW_CONFIG_DIR` (model haiku-5-5), `nw` wrapper running this worktree:
  - (a) `nw queue conductor-launch` started the interactive conductor (status line `don't ask on`);
    it ran nw-orient and nw-conduct and armed its Monitor. Prompted "call AskUserQuestion": the model
    reported the tool is not in its tool list and ToolSearch finds nothing; no question appeared.
  - (b) in that conductor, a dispatched sub-agent edited `.nw-worktrees/wt1/a.txt` (Edit) and ran `git
    -C <wt1> status` with no denial; `git -C <wt1> push origin main` was denied. A second sub-agent ran
    `git -C <wt1> add -A` and `commit -m wip` (allowed), `cp` inside `.nw-worktrees`, `uv run nw
    --help` and `python3 tools/spec_lint.py --help` (allowed). Refused: `git -C <wt1> worktree add
    /tmp/ac7/wt2` (the steward guard deny `git * worktree *` wins over the allow) and `cp` into a
    directory outside the working directories ("Bash has been denied because Claude Code is running in
    don't ask mode").
  - (c) `nw skills install --dest <scratch> --settings <scratch>/settings.json` wrote the
    `AskUserQuestion` hook; `nw queue conductor-session add <uuid>`; a plain interactive `claude
    --session-id <uuid> --settings ...` prompted "call AskUserQuestion" got `PreToolUse:AskUserQuestion
    hook error: [nw queue conductor-guard]: conductor never asks the human: use steward:<P> / nw queue
    escalate (SPEC-0382)`, and no question reached the terminal.

### As shipped (2026-11-02)

- `conductor_allowed_tools` default: `Bash(git *)` was NOT shipped. It would allow pushes to any
  branch outside the main/master deny set (the existing `test_default_rules_deny_dangerous_pushes`
  fails for the conductor) and SPEC-0353's `test_conductor_plan_excludes_rebase_set` keeps the rebase
  set off the conductor. The default instead holds read-only git verbs (`status`, `log`, `diff`,
  `show`, `rev-parse`, `rev-list`, `merge-base`, `ls-files`, `branch` listing; each also as `git -C *`).
  Consequence: under `dontAsk` a conductor sub-agent can read git state and edit files in
  `.nw-worktrees`, but cannot `git rebase`; resolving rebase conflicts in a campaign worktree stays
  with the queue's `conflict_resolver` and the steward. A user who wants it adds the rules to
  `conductor_allowed_tools`.
- Review fix, the `main_dirty` / `main_not_on_base` / `wt_broken` rows (move in-place WIP onto a
  worktree branch): `conductor_allowed_tools` also holds `git worktree add *`, `worktree list`,
  `switch *`, `checkout -- *`, `add -A`, `add -A *`, `add -- *`, `commit -m *`, `commit -am *` (each
  also as `git -C *`), `uv run nw *`, `python3 tools/spec_lint.py*`, `cp *` and `mkdir *`. They are
  deliberately not the steward's `add *` / `commit *` strings, so the SPEC-0353 rebase set stays off
  the conductor, and the steward guard denies (`reset --hard`, `clean`, pushes) still win. The real
  binary showed the limits (Test record): `git -C <dir> worktree add` is denied by the steward guard
  (`git * worktree *`), so the sub-agent must run plain `git worktree add` from the conductor's cwd;
  `cp` is allowed only into the working directories (`.nw-worktrees` is inside the conductor's
  project root), so the WIP worktree belongs under `.nw-worktrees`, not `../nw-<topic>`; `rm` and
  `git clean` stay refused, so the "remove the moved untracked files" step is left to the steward or
  the human.
- Edit/Write rules are `//**/.nw-worktrees/**` (see the Test record).
- `conductor-launch` writes `disallowed_tools` into the `source: launch` session record
  (`record_session(..., extra=)`); `nw doctor` reads it. Launch records written before this change
  have no such field and so warn until the conductor is relaunched with `--resume`: a resumed launch
  reuses its session id, so `register` passes `refresh=True` and `record_session` merges the current
  `disallowed_tools` into the existing row in place (one row per session id, so the session counts
  and `build_plan`'s resume lookup are unchanged). A resume after the deny was removed from config
  warns again. Doctor judges only the newest registered conductor record, and only when it is a
  `source: launch` one: a hand-started conductor registered after an old launch is guarded by the
  hook and does not warn.
- `agent_permissions.sync()` returns `{"added", "removed", "hook"}`; `skills list` is unchanged.
- `decisions.RULE_RE` now accepts up to two qualifier segments: the already-landed `nw-conduct` row id
  `message.request.human_order` did not match the old pattern (`test_conduct_skill_rule_ids` failed,
  and `nw queue decision --rule message.request.human_order` would have been refused).
- Skills: no edit, as designed. The review brief's request to change the `nw-conduct` timeout line
  belongs to slice B with the `human_decision_timeout` row.
