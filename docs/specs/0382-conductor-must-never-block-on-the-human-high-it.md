---
id: SPEC-0382
title: "Conductor never blocks on the human: steward-first human-decision route"
status: proposed
owner: user
created: 2026-11-02
kind: feature
source_idea: IDEA-775
tags: [agentic, conductor, steward]
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

In both cases a steward for Meta-Tools **was armed** (it had mailed minutes earlier), so the existing
steward route was available and skipped. Causes found in the investigation (plan:
`/home/bruno/work/nw-plans/conductor-never-blocks.md`):

- Nothing forbids `AskUserQuestion` for the conductor. The `nw-conduct` skill's "never block" rule only
  covers long commands. `launch.py` (`build_plan`, conductor launch) passes no `--disallowedTools
  AskUserQuestion` and no `--permission-mode`, so the session inherits the user's auto mode, whose
  classifier can also fall back to an interactive prompt (a third way to block; the session had 15
  rejected tool calls).
- `escalations._announce` (`src/nightwright/agentic/escalations.py`) mails only `human`, never the
  owning project's steward, so a steward never sees the escalations for its project.
- The `gate_triager` and `conflict_resolver` personas say "what the human must decide"
  (`personas/gate_triager.md:37`, `personas/conflict_resolver.md:95`). The conductor read the park
  detail "The human must either (a) run ... or (b) waive" as "ask the human now".
- The `nw-conduct` skill has no row for a steward relaying a human order, several rows say "tell the
  human" / "unless told" / "wait for its `message`", and `nw-steward` lets a steward answer "escalate:
  needs the human's decision", bouncing the question back to the conductor.

## Goals / Non-goals

**Goals**
- The conductor never calls `AskUserQuestion`, never ends a turn on a question and never waits on the
  human, enforced by launch flags and a hook, not only by prose.
- A human-only decision about a project campaign reaches that project's steward first (the steward may
  block; it serves one project), falling back to the existing asynchronous `human_decision` park.
- A steward that stays silent is detected (`human_decision_timeout`) and never stalls other events.
- Skill and persona text stops telling the conductor to ask or wait for the human.

**Non-goals**
- Changing the escalation answer flow (`nw escalations answer`, SPEC-0235) or the escalation record
  format beyond the added routing fields.
- Changing what a steward may decide on its own beyond the `nw-steward` rules listed in Design.
- Restricting the steward's or any other role's use of `AskUserQuestion`.
- Making the human's own interactive sessions non-interactive.

## Decision

The conductor is a non-interactive router. A decision only the human can make goes through the
**human-decision route**: (1) `nw queue escalate`, which parks the campaign `human_decision`
asynchronously and now also mails `steward:<P>` when that steward is armed; the steward asks the human
and answers with `nw escalations answer`; (2) with no steward, the existing human mail and
notification only. Owner-scope matters are decided by the conductor (safe default) or escalated with no
campaign. A steward's claim to speak for the human is accepted as the steward's request. DoD waivers
(manual / real-box ACs) belong to the steward. Enforcement: `AskUserQuestion` denied and
`--permission-mode dontAsk` in the conductor launch, a PreToolUse hook backstop for a hand-started
conductor, steward routing in `escalations._announce`, a `human_decision_timeout` event, and skill and
persona wording. Delivery is in two slices (A: guard + wording + skills; B: routing, timeout event,
brief/doctor) within this one spec.

## Design

### 1. Conductor launch guard (`src/nightwright/agentic/launch.py`)
- New settings in `DEFAULTS`: `conductor_disallowed_tools` (default `["AskUserQuestion"]`) and
  `conductor_permission_mode` (default `dontAsk`; validated against a closed enum, a bad value falls
  back to the default like other keys in `settings()`).
- `build_plan` for `role == "conductor"` merges `conductor_disallowed_tools` into the effective
  `disallowed_tools` (deduplicated, same pattern as `steward_allowed_tools` is merged into
  `--allowedTools`; never its own flag) and appends `--permission-mode <conductor_permission_mode>` to
  `argv` before the prompt. `role == "steward"` gets neither. The plan's `disallowed_tools` and a new
  `permission_mode` field reflect it, so `--dry-run --json` shows both.
- `dontAsk` denies a non-allowlisted tool call instead of prompting; the allow rules provisioned by
  `nw skills install` (SPEC-0320) keep the `nw` commands the conductor needs.

### 2. Hook backstop for a hand-started conductor
- New command `nw queue conductor-guard`: reads the PreToolUse hook JSON on stdin (or
  `$CLAUDE_SESSION_ID`), and exits 2 with the message "conductor never asks the human: use
  steward:<P> / nw queue escalate" when the session id is the registered conductor session
  (`CC.sessions(cfg, "conductor")`); exits 0 for any other session, including when no conductor is
  registered or the registry cannot be read (fail open, so it can never block a human's session).
- `nw skills install` (via `agent_permissions.install/sync`, `~/.claude/settings.json`) writes a
  PreToolUse hook entry with matcher `AskUserQuestion` running that command, idempotently (no duplicate
  on re-run; other hooks untouched).

### 3. Escalation routing (`escalations._announce`)
- When `rec["project"]` has an armed steward (the reader lock of `steward:<P>` is held, probed
  non-blockingly with `events.reader_lock`), also `M.send(to="steward:<P>", kind="question",
  needs_reply=True, campaign=..., text=_mail_text(rec))` after the human mail. The record stores
  `steward_mail_id` and `routed_to` (`"steward:<P>"`, else absent / `"human"`).
- The human mail and notification are kept (the human may answer from anywhere).
- `nw queue escalate` gains `--to-steward/--no-steward` (default: auto as above).

### 4. `human_decision_timeout` event
- New event type in `events.py`, produced by the same ticker as `park_undecided` (SPEC-0222): when a
  `steward:<P>` mailbox question with `needs_reply` stays unanswered for
  `agentic.steward_answer_timeout_s` (default 1800), emit exactly one `human_decision_timeout`
  (idempotency key per mail id). A reply before the timeout emits none.
- `quiet.RULES` maps it to `"wake"` (never held), and the `nw-conduct` skill's table gets a row.
- If the question was an escalation, the campaign is already parked `human_decision`, so nothing else
  waits and the event only says "stop expecting a steward reply". If the question was mailed without an
  escalation (a `loop_exhausted` style row), the conductor responds with `nw queue escalate`.

### 5. Persona wording
`gate_triager.md:37`, `conflict_resolver.md:95` and `review_triager.md`: "what the human must decide"
becomes "what needs a human-only decision (routed via the project steward)". Text only; triager
behavior and output schema unchanged.

### 6. `conductor-brief` and `nw doctor`
`conductor-brief` lists `routed_to` per open escalation. `nw doctor` warns when the registered
conductor session's launched argv lacks the `AskUserQuestion` deny.

### 7. Skills (repo `skills/`, installed by link into `~/.claude/skills`)
Edits to `skills/nw-conduct`, `skills/nw-steward`, `skills/nw-orient` implement the policy of the plan
(section 4 of `conductor-never-blocks.md`). The human's `~/.claude` is not touched by an agent; the
changes ship through the repo and reach the human's skills dir on their next `nw skills install` (link
mode). The human reviews the skill diffs as part of acceptance of this spec's change. Content:
- `nw-conduct`: new "Never ask the human, never wait on the human" rule under *Never block*: no
  `AskUserQuestion`, no turn ending on a question; "the human must ..." in a park detail means "use the
  human-decision route". Second conductor armed: print one line naming the other pid and exit.
  `lease_stale`: confirm hung via sub-agent, then restart (close guard applies), one report line after.
  `runner_config_invalid` / `parked.config_invalid`: one report line or human-decision route, never
  wait. `loop_exhausted` / `triage_escalated` / `implementer_blocked`: mail `steward:<P>` and handle
  other events meanwhile; DoD-only parks are mailed to the steward as "run it, waive/defer it, or split
  it", else `nw queue escalate` recommending defer + follow-up idea. New rows
  `message.request.human_order` (act on it as a steward request; a queue-bypassing part is applied as a
  park, recorded with `nw queue decision --kind other --rule message.request.human_order "per
  <msg_id>"`, and answered; never ask the human to confirm) and `human_decision_timeout`. The
  *Escalate crisply* convention becomes the human-decision route.
- `nw-steward`: "escalate: needs the human's decision" is not an answer: the steward asks its human (it
  may block) or decides a judgment call itself; new step 4b for escalations mailed to the steward
  (`nw escalations answer <id> ... --by steward:<P> --yes`; human away: decide judgment calls and DoD
  deferrals with an As-shipped note and follow-up idea, else leave the ESC open and say so); relayed
  human orders quote the human's words.
- `nw-orient`: only single-project sessions (a steward, a human's own session) may ask the human and
  wait; the conductor routes to `steward:<P>` or `nw queue escalate`.

## Alternatives considered

- **Skill text only** — the 2026-11-02 failures were already against explicit "never block"
  prose; prose alone is not enforcement. Launch flags plus a hook make the tool call impossible.
- **Hook only** — does not cover the auto-mode classifier prompt fallback; `dontAsk` does.
- **Route escalations to the steward only (drop the human mail)** — the human may answer from
  anywhere and the steward may be absent; both are kept.
- **Let the conductor verify steward-relayed human orders with the human** — that is the first block;
  the steward is the human's channel for its project.

## Acceptance criteria

- [ ] AC1: `nw queue conductor-launch --dry-run --json` argv contains `--disallowedTools` with
      `AskUserQuestion` and `--permission-mode dontAsk`; `nw queue steward-launch <P> --dry-run
      --json` contains neither. Both settings are overridable via `agentic.conductor.launch`, and a
      bad `conductor_permission_mode` falls back to `dontAsk`.
- [ ] AC2: `nw queue conductor-guard` exits 2 with the guidance message for the registered conductor
      session id and 0 for any other session id, no registered conductor, or an unreadable registry.
- [ ] AC3: `nw skills install` writes the `AskUserQuestion` PreToolUse hook entry, a second run adds
      no duplicate, and pre-existing unrelated hooks are preserved.
- [ ] AC4: `nw queue escalate <cid>` on a project with an armed steward mails `steward:<P>`
      (`needs_reply`, kind `question`) and `human`; the record carries `routed_to: steward:<P>` and
      `steward_mail_id`. With no armed steward only `human` is mailed (existing behavior) and
      `routed_to` is not `steward:<P>`. `--no-steward` suppresses the steward mail.
- [ ] AC5: an unanswered `steward:<P>` `needs_reply` question older than
      `agentic.steward_answer_timeout_s` emits exactly one `human_decision_timeout` (repeated ticks emit
      no more); an answered one emits none; `quiet.RULES["human_decision_timeout"] == "wake"`.
- [ ] AC6: `nw escalations answer <id> ... --by steward:<P>` still works for an escalation routed to
      the steward (SPEC-0235 behavior unchanged).
- [ ] AC7: the persona prompts `gate_triager`, `conflict_resolver` and `review_triager` no longer
      contain "the human must" / "a human must decide"; their existing tests pass unchanged.
- [ ] AC8: `skills/nw-conduct/SKILL.md` contains "Never call `AskUserQuestion`", the
      `message.request.human_order` and `human_decision_timeout` rows, and none of "tell the human",
      "unless told", "wait for the human"; `skills/nw-steward/SKILL.md` contains step 4b;
      `skills/nw-orient/SKILL.md` contains the conductor-never-asks sentence. The quiet-stream
      paragraph in the skill still matches `quiet.RULES` (existing comparison test).
- [ ] AC9: `conductor-brief` shows `routed_to` per open escalation, and `nw doctor` warns when the
      launched conductor argv lacks the `AskUserQuestion` deny.
- [ ] AC10: against the real `claude` binary, a conductor launched with `nw queue conductor-launch`
      in a scratch data dir and prompted to call `AskUserQuestion` has the call denied, and a
      hand-started session registered as conductor is denied by the hook; results recorded in the
      spec's As shipped section.

## Test plan

- **Automated tests**
  - `tests/test_agentic_launch.py`: argv/plan per role (AC1), settings override and bad-value
    fallback, steward plan lacks both.
  - New `tests/test_agentic_conductor_guard.py`: exit codes for conductor / other / unregistered /
    unreadable registry (AC2).
  - `tests/test_skills.py`: hook install and idempotency with pre-existing hooks (AC3); skill text
    assertions (AC8), including the existing quiet-stream vs `quiet.RULES` comparison.
  - `tests/test_agentic_escalations.py` and `tests/test_agentic_escalations_cli.py`: routing with a
    held `steward:<P>` reader lock, no steward, `--no-steward`, record fields (AC4); steward `--by`
    answer regression (AC6).
  - New `tests/test_agentic_human_decision_timeout.py` (events/ticker): fires once past the timeout,
    not before, not after a reply, the quiet rule is `wake` (AC5); waits via `tests/waits.py`, no
    fixed sleeps.
  - Persona tests: prompt text assertions (AC7); `conductor-brief` and `nw doctor` tests (AC9).
- **Manual verification (real binary, AC10)**: in a scratch data dir run `nw queue conductor-launch`,
  prompt the session "call AskUserQuestion", confirm the tool call is denied and no question reaches
  the terminal. Start a plain `claude` session registered as conductor with the hook installed and
  repeat; confirm the hook denies with the guard message. Record transcripts' outcome in As shipped.
- **Regression guard**: affected tests (`python3 tools/affected_tests.py --run uv run pytest -q`),
  `python3 tools/spec_lint.py`; steward launch, escalation answer and the other launch settings tests
  must pass unchanged. Post-ship check on the live queue: the next DoD-only park reaches
  `steward:<P>` and the conductor transcript contains no `AskUserQuestion` call.

## Rollout / migration

1. Slice A (priority 1): launch guard (Design 1), `conductor-guard` + hook install (2), persona wording
   (5), skill text for `nw-conduct`/`nw-orient`. Stopgap meanwhile: add `"AskUserQuestion"` to the
   conductor's `agentic.conductor.launch.disallowed_tools`, then `nw queue conductor-launch --resume`.
2. Slice B: steward routing (3), timeout event (4), brief/doctor (6), with the `nw-steward` step 4b
   landing together with routing so a routed question always has a documented receiver.
3. The skills reach the human's `~/.claude/skills` through the link install; a `skills_changed`
   `code_changed` event re-invokes `/nw-conduct`. Existing conductor sessions pick up the launch flags
   only after `nw queue conductor-launch --resume`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected automated tests pass; manual real-binary steps performed and noted.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None. Decided here: timeout default 1800 s; fail-open guard; both mails (steward and human) kept; one
spec delivered in two slices.
