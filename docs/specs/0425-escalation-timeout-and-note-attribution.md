---
id: SPEC-0425
title: "Escalation timeout and note attribution"
status: done
owner: user
created: 2026-11-04
source_idea: IDEA-931
parent: SPEC-0419
milestone: "M9: agentic workflow"
tags: [agentic, escalations, conductor, ledger]
depends_on: [SPEC-0422]
---

## Context

Child 6 of [SPEC-0419](./0419-self-healing-queue.md). Escalation records
([SPEC-0232](./0232-escalation-records-wt-queue-escalate-and-notify.md),
[SPEC-0235](./0235-escalation-answers-decide-waive-attest-conductor-applies.md),
`src/nightwright/agentic/escalations.py`) stay `open` until a human or steward answers. The
conductor row `parked.human_decision` says "Do nothing". `remind_due` (`:673-700`) only
re-notifies. Of the 25 records so far, 13 carried a recommendation. Each one still waited for the
human, for hours.

**Decided by the human** (2026-11-04): after 30 min with no answer, the conductor applies its own
recommendation, records it, and leaves it visible and reversible. Escalations with no
recommendation keep waiting.

The epic's human-only list (SPEC-0419) must never be auto-applied, whether or not a recommendation
exists:

- scope and product;
- spend above the 3× ceiling or on diverging campaigns;
- manual-AC waivers or attests about the human's own machines;
- `git push origin main`;
- `gh pr merge` by agents;
- the human's own checkout and settings.

**Attribution.** `nw campaign note` writes `by: human` unless `--author` is passed
(`src/nightwright/agentic/cli_campaign.py:413-415`). Persona runs export `NW_PERSONA_RUN=1` but not
the persona name (`personas.py:1006-1013`). The ledgers hold 75 `by: human` notes since 2026-11-01,
mostly implementer prose ("implementer blocked: …", "run 004: …").

## Goals / Non-goals

**Goals**
- Every escalation gets a `topic`. A record that has a recommendation and a non-human-only topic
  becomes **due** `auto_apply_after_s` (default 1800) after it was created.
- When a record is due, the follower emits one `escalation_timeout` event. The conductor then runs
  `nw escalations answer <id> --auto --by conductor`, which applies exactly the recommended
  option.
- The auto answer is visible. It is marked in the record, mailed and notified to the human, shown
  in `nw escalations list` and in `conductor-brief`. It is also reversible: a later human answer
  replaces it, and the conductor undoes what it can.
- New notes name their real author: the persona, the conductor, a steward, an unregistered Claude
  session, or the human.

**Non-goals**
- Applying anything to human-only topics, or to records without a recommendation.
- Rewriting existing ledger notes.
- Changing `--by` defaults of `nw queue` commands (SPEC-0318 decision records depend on them).

## Decision

1. **Topics.** `TOPICS = ("recovery", "judgment", "scope", "product", "spend_ceiling",
   "spend_diverging", "manual_ac", "push_main", "pr_merge", "human_checkout",
   "human_settings")`. `HUMAN_ONLY_TOPICS` is everything except `recovery` and `judgment`.
   - `plan_escalate(..., topic=None)` validates `topic`.
   - **`--recommend` requires `--topic`** (ValueError "a recommended escalation needs --topic"), so
     classification is deliberate.
   - Queue-filed escalations set the topic in code: SPEC-0420 `recovery` (kind `red_main`) and
     SPEC-0422 `spend_ceiling` / `spend_diverging` (kind `cost`).
   - Kind `cost` is human-only whatever the topic says. Kinds in `RECOVERY_KINDS = {"red_main",
     "worker_lost"}` count as `recovery` when they have no topic.
2. **Eligibility.**
   - `eligible(rec) = rec.recommendation is not None and topic_of(rec) not in HUMAN_ONLY_TOPICS
     and rec.kind != "cost"`, where `topic_of` returns the topic, or `recovery` for a
     `RECOVERY_KINDS` kind, or None.
   - None (a legacy record) is never eligible.
   - It is computed at create and stored as `auto: {"eligible": bool, "due_at": iso | null,
     "why": str}`. `due_at = created + auto_apply_after_s`. Setting `auto_apply_after_s: 0`
     disables timeouts.
3. **Timeout detection.**
   - `escalations.timeouts_due(cfg, now)` is called from `events.sync(ping=True)` next to
     `remind_due`, and never raises.
   - For each open, live, eligible record past `due_at` with no `auto.announced_at`, it claims the
     record under the escalations lock (writes `announced_at`), then appends `escalation_timeout`:
     data `escalation_id`, `campaign`, `recommendation`, `label`, `topic`.
   - That event **wakes** the conductor (not held).
4. **Applying.** `nw escalations answer <id> --auto --by conductor`:
   - `plan_answer(..., auto=True)` requires an open record, eligible, past due, and `by ==
     "conductor"`. It refuses otherwise, naming why.
   - Answer form: `choose` the recommendation, with `answer.auto = true`, `answer.note =
     "auto-applied after <n> min without an answer (SPEC-0425)"`, `answered_by: conductor`.
   - `apply_answer` then works as today: ledger note, `escalation_answered` event with `data.auto:
     true`.
   - After the answer, `_announce_auto(rec)` mails `human` (kind `info`): "ESC-N: no answer in 30
     min; applied option k (<label>). To reverse: nw escalations answer ESC-N --choose M". It also
     runs `notify()` with title "nw: ESC-N auto-applied".
5. **Reversal.**
   - `plan_answer` allows one more answer on an `answered` record whose `answer.auto` is true,
     when `by` is `human` or a `steward:<P>`. It skips `_check_parked`, because the campaign has
     moved on.
   - `apply_answer` appends the previous answer to `rec.answer_history`, stores the new one, and
     emits `escalation_answered` with `data.reverses_auto: true` and `data.previous`.
   - The conductor row says: undo what the auto answer did where it can (re-park with `nw queue
     park`, abandon, lower a cap back, or revert through `nw queue merge --fixes-red` / a revert
     campaign), then apply the new answer and record `--kind apply_answer`.
6. **Attribution.** `campaign_note` resolves `(by, author)` in this order (`notes.note_actor(cfg,
   env, author)`, new, in `cli_campaign.py` or `store.py`):
   1. `--author X` → `agent:X`;
   2. `NW_PERSONA` (new env var exported by `personas.py`, `remote_personas.py` and the worker's
      persona env, set to the persona name) → `agent:<persona>`;
   3. `NW_PERSONA_RUN=1` → `agent:persona`;
   4. `NW_MAILBOX_ADDR` (validated) → that address;
   5. `CLAUDE_CODE_SESSION_ID` registered in `conductor_cost.sessions` for `conductor` or
      `steward:<P>` → that reader;
   6. `CLAUDECODE=1` → `agent:session`;
   7. otherwise → `human`.

   The `author` field mirrors `by` without the `agent:` prefix.

## Design

Files and functions:

- `src/nightwright/agentic/escalations.py`:
  - `TOPICS`, `HUMAN_ONLY_TOPICS`, `RECOVERY_KINDS`, `topic_of`, `eligible`;
  - `plan_escalate`/`apply_escalate` (`topic`, `auto`);
  - `settings` (`auto_apply_after_s`, default 1800);
  - `timeouts_due`;
  - `plan_answer`/`apply_answer` (`auto`, reversal, `answer_history`);
  - `_announce_auto`.
- `src/nightwright/agentic/cli_escalations.py`: `answer --auto`; `list` shows a `topic` column
  and an `auto` marker (`auto-applied`, `due in 12m`, `human-only`); `show` prints `auto` and
  `answer_history`.
- `src/nightwright/agentic/cli_queue.py`: `nw queue escalate --topic` (choice of `TOPICS`).
- `src/nightwright/agentic/events.py`: `sync()` ping branch calls `ESC.timeouts_due`.
- `src/nightwright/agentic/quiet.py`: `escalation_timeout` wakes (not held).
- `src/nightwright/agentic/brief.py`: `escalations_open` rows gain `topic` and `due_at`; a new
  `auto_applied_24h` list.
- `src/nightwright/agentic/cli_campaign.py`: `campaign_note` uses `note_actor`.
- `src/nightwright/agentic/personas.py` (env `NW_PERSONA`), `remote_personas.py`, `worker.py`
  (same).
- `skills/nw-conduct/SKILL.md`:
  - `parked.human_decision`: wait. An `escalation_timeout` arrives when the record is due.
  - New row `escalation_timeout`: run `nw escalations answer <id> --auto --by conductor` (dry run,
    then `--yes`), then handle the `escalation_answered` that follows as usual.
  - `escalation_answered` gains the `reverses_auto` handling.
  - The human-decision route lists the human-only topics verbatim and says every `nw queue
    escalate --recommend` needs `--topic`. Scope, split and product questions use
    `scope`/`product`, and so never time out.
- `skills/nw-steward/SKILL.md`: a steward answering for the human may reverse an auto answer with
  `nw escalations answer`.

## Alternatives considered

- **The queue applies recommendations itself.** Lost: options are free text, and the conductor is
  the component that turns an option into commands.
- **Time out every escalation that has a recommendation.** Lost: the epic's human-only list
  includes recommended scope and spend questions. An explicit topic is the only way to tell them
  apart.
- **Retro-attribute old notes.** Lost: the ledger is append-only. The fix applies forward.

## Acceptance criteria

- [x] AC1: `nw queue escalate <cid> --question q --option a --option b --recommend 1` without
      `--topic` is refused. With `--topic recovery` the record has `auto.eligible: true` and
      `due_at = created + 1800 s`. With `--topic scope`, or any human-only topic, or `kind:
      cost`, or no recommendation, `eligible` is false and there is no `due_at`.
- [x] AC2: A follower sync after `due_at` (driven by `now`) appends exactly one
      `escalation_timeout` for the record. A second sync and a second follower append none. A
      human-only or answered record gets none.
- [x] AC3: `nw escalations answer ESC-N --auto --by conductor --yes`:
      - before `due_at`, by a non-conductor, or on an ineligible record → refused with the reason;
      - otherwise → answered with the recommended option, `answer.auto: true`, `answered_by:
        conductor`, an `escalation_answered` event with `auto: true`, one human mail and a
        notification.
- [x] AC4: A human `nw escalations answer ESC-N --choose 2 --yes` after an auto answer is
      accepted. `answer_history` holds the auto answer, and `escalation_answered` carries
      `reverses_auto: true` and `previous`. A second reversal of a non-auto answer is refused as
      today.
- [x] AC5: A note written by `nw campaign note` gets these attributions:
      - inside a persona run → `by: agent:<persona>`;
      - with `NW_MAILBOX_ADDR=conductor` → `by: conductor`;
      - from a registered steward session id → `by: steward:<P>`;
      - from an unregistered Claude session → `by: agent:session`;
      - from a plain shell → `by: human`.
- [x] AC6: The conductor skill has the `escalation_timeout` row, the `reverses_auto` handling and
      the human-only topic list. `quiet.RULES` does not hold `escalation_timeout`.

## Test plan

- **Automated tests**
  - `tests/test_agentic_escalations.py` (extend): topic validation; `eligible` matrix (topics ×
    recommendation × kind) (AC1); `timeouts_due` with `now`, the claim, and two concurrent callers
    (AC2); `--auto` refusals and the success path (AC3); reversal and `answer_history` (AC4).
  - `tests/test_agentic_escalations_cli.py` (extend): `--topic`, `--auto`, the list markers.
  - `tests/test_agentic_campaign*.py` / the note tests (extend): the `note_actor` matrix through a
    monkeypatched env and a tmp sessions registry (AC5). `tests/test_agentic_personas*.py`:
    `NW_PERSONA` in the run env.
  - `tests/test_skills.py` and `tests/test_agentic_quiet.py` (AC6).
- **Real verification (beyond fakes)**:
  - A real follower: in a scratch `NW_DATA_DIR`, with `agentic.escalations.auto_apply_after_s: 5`
    in a scratch config:
    1. `nw queue escalate` a real parked campaign (a real fixture campaign) with `--recommend 1
       --topic recovery --yes`;
    2. run `nw queue events --follow --for conductor --all` for 90 s (the ping interval is set low
       in the scratch config);
    3. assert one `escalation_timeout` line;
    4. run `nw escalations answer ESC-0001 --auto --by conductor --yes`;
    5. assert the record, `nw mailbox list --to human`, and `nw campaign log` (the note by
       `conductor`);
    6. reverse it with `nw escalations answer ESC-0001 --choose 2 --yes`.

    Record the transcript in As shipped. Repeat with `--topic scope` and assert no timeout line.
  - A real persona run: one real implementer persona run on a fixture campaign (haiku, scratch
    data dir) calls `nw campaign note "x"`. Assert `by: agent:implementer` in its ledger.
  - **Live check after landing** (manual): over 3 days, no escalation with a recommendation and a
    non-human-only topic stays open longer than 35 min, and the ledgers show no new `by: human`
    note that the human did not write. Record the counts.
- **Regression guard**: affected tests and `python3 tools/spec_lint.py`. Existing answer, withdraw
  and remind tests pass unchanged.


## Rollout / migration

1. Land after SPEC-0422 (`depends_on`). Open records from before the change have no topic and are
   not eligible; they keep waiting.
2. The conductor reloads the skill on `code_changed` (`skills_changed` includes `nw-conduct`).

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (affected tests; the full suite runs at the queue's close gate); manual steps performed (the 3-day live check is post-landing, below).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

Built as decided, with these details settled in the build:

- **`depends_on: SPEC-0422`** was not waited on: a record with no topic never times out, and
  kind `cost` plus the `spend_ceiling` / `spend_diverging` topics are human-only (the
  `HUMAN_ONLY_KINDS` set). The 3× ceiling therefore holds whether or not SPEC-0422 has landed,
  so no gate on its config key was needed.
- **Code callers.** The `--topic` requirement is enforced in `plan_escalate`. A
  `RECOVERY_KINDS` kind counts as `recovery`, so `workers.py`'s `worker_lost` escalation
  (recommend 1, "off") needs no change and is eligible. `sentinel._escalate` has no
  recommendation and stays ineligible until SPEC-0420 passes `kind=red_main, topic=recovery`.
- **Conservative timeouts.**
  - A record without an `auto` block (created before this change, e.g. ESC-0024) never times
    out, and `--auto` refuses it ("predates").
  - Eligibility is checked again when the timeout is announced, in `plan_answer` and inside
    the `apply_answer` lock.
  - A campaign-bound record is announced only once its campaign is PARKED `human_decision`.
  - Setting `auto_apply_after_s: 0` disables timeouts for existing records too.
  - A failed `escalation_timeout` append releases the claim.
- **Reversal** is allowed for `human` and `steward:<P>` only (not the conductor), once: the
  new answer carries no `auto`, so a second reversal is refused as before. The record gains
  `answer_history`. `auto` also records `applied_at`, `mail_id` and `notified`. `nw escalations
  list` marks `auto-applied` / `reversed` / `due in Nm` / `due now` / `human-only`. `show`
  prints the reverse command. `conductor-brief` lists `auto_applied_24h` (REVERSED when
  replaced) and shows `[topic due …]` on open rows.
- **Attribution.**
  - `note_actor` lives in `cli_campaign.py`. A session row whose reader is not a valid address
    (e.g. a bare `steward`) is ignored. The note's `author` field mirrors `by`.
  - Remote runs: `NW_PERSONA` is an `ENV_RESET` key (`runner.py`), not a verbatim one, so it
    never travels in the job's env (a worker on older code would refuse it). The worker side
    sets it from the job's `persona` (`remote_personas`); `worker.py` needs no change.
  - `tests/conftest.py` unsets `NW_PERSONA`, `NW_MAILBOX_ADDR`, `CLAUDECODE` and
    `CLAUDE_CODE_SESSION_ID`, so note tests run as a plain shell.
- **Tests.** The SPEC-0425 cases are in the new `tests/test_agentic_escalation_timeout.py`
  (topics, the eligibility matrix, `timeouts_due` including 4 concurrent callers, `--auto`
  refusals and success, reversal, brief, quiet, the CLI, and the skill rows: AC1-AC4, AC6).
  The note_actor matrix and CLI attribution tests are in `tests/test_agentic_campaign_cli.py`
  (AC5); `NW_PERSONA` is checked in `tests/test_agentic_personas.py`. Existing callers that
  pass a recommendation now pass `topic="scope"`, which is human-only so their behaviour is
  unchanged.

**Real verification** (2026-11-04, scratch `NW_DATA_DIR` / `NW_CONFIG_DIR`, real CLI, two real
follower processes `nw queue events --follow --for conductor` (quiet) and `--for audit --all`,
`ping_interval_s: 1`, notifier = a shell command that logs the id). To avoid real waiting, the
records were backdated by 31 min instead of the spec's `auto_apply_after_s: 5` config.

- `nw queue escalate … --recommend 1` without `--topic` → `Error: a recommended escalation
  needs --topic (…)`, exit 1.
- ESC-0001 (`--topic recovery`): `auto: {eligible: true, due_at: created + 1800 s}`. ESC-0002
  (`--topic scope`): `eligible: false, "topic scope is human-only"`. ESC-0003 (no
  recommendation): `eligible: false, "no recommendation"`. `list` showed `due in 29m` and
  `human-only`.
- With both followers running for 4 pings: 0 `escalation_timeout`. `answer ESC-0001 --auto --by
  conductor --yes` → `not due yet: due at … (in 29m 50s)`.
- After backdating all three by 31 min, the quiet conductor reader printed
  `{"type": "escalation_timeout", "campaign": "SPEC-0990-…", "data": {"escalation_id": "ESC-0001",
  "recommendation": 1, "label": "resume to GATING", "topic": "recovery"}}`. After 4 more pings
  with 2 followers, events.jsonl held exactly 1 `escalation_timeout`, and none for ESC-0002 or
  ESC-0003.
- `--auto` on ESC-0002 → "topic scope is human-only"; on ESC-0003 → "no recommendation";
  `--by human` → "--auto is the conductor's". ESC-0002 and ESC-0003 stayed `open`, with no
  answer and no `announced_at`.
- `--auto --by conductor --yes` → `answered`, `answered_by: conductor`, choice 1, `auto: true`,
  note "auto-applied after 31 min without an answer (SPEC-0425)". The conductor reader got
  `escalation_answered` with `data.auto: true`. Human mail `human:4` (info): "ESC-0001: no
  answer in 30 min; applied option 1 (resume to GATING). To reverse: nw escalations answer
  ESC-0001 --choose 2 --yes". The notifier ran a second time for ESC-0001. The campaign ledger
  got `human_decision … auto=True` and a note by `conductor`. `list --all` showed
  `auto-applied`.
- `nw escalations answer ESC-0001 --choose 2 --yes` (human) → accepted.
  `escalation_answered` carried `reverses_auto: true` and `previous.choice: 1, auto: true`.
  `show` printed "earlier answer by conductor … (auto, reversed)". A second `--choose 1` was
  refused ("answered since …"). `list` showed `reversed`. `conductor-brief` showed
  "auto-applied escalations (24h): ESC-0001 … option 1 (resume to GATING) … REVERSED" and
  `[scope]` on ESC-0002.
- Notes through the real CLI: plain shell → `human`; `NW_MAILBOX_ADDR=conductor` → `conductor`;
  `CLAUDECODE=1` + a session id registered for `conductor` → `conductor`; `CLAUDECODE=1` + an
  unregistered session id → `agent:session`. The steward-session case is covered by the
  automated matrix.
- A real persona run: `personas.build_request` for `implementer` (env `NW_PERSONA=implementer`,
  `NW_PERSONA_RUN=1`) run through the real `ClaudeRunner` (haiku, $0.006) on a fixture campaign.
  The agent ran `nw campaign note "x from the implementer run"`, and the ledger records `by:
  agent:implementer`, `author: implementer`.

**Live check (post-landing, manual):** over 3 days, count escalations with a recommendation and
an eligible topic left open more than 35 min, and new `by: human` notes the human did not write.
This needs the conductor to reload the skill first (`code_changed`). Records filed before
landing have no `auto` block and keep waiting.

## Open questions

_None._
