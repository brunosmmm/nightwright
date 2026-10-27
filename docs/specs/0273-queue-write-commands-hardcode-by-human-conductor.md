---
id: SPEC-0273
title: "Queue write commands record the real actor (--by), not always human"
status: accepted
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-091
tags: [agentic, queue, ledger, cli]
---

## Context

Promoted from idea `IDEA-091` (queue write commands hardcode by=human: conductor/steward
actions are attributed to the human in campaign ledgers).

Still present on main. In `src/wt/agentic/queue.py` the human-command appliers pass a
literal `by="human"` to the ledger:

- `apply_prioritize` → `_transition(..., by="human")`
- `apply_park` → deferred: `park_requested` (no `by`) + `store.append_event(... "note",
  {"author": "human"}, by="human")`; immediate: `_park(cfg, st, "human", ..., by="human")`.
  The deferred request is later honoured in the tick (the `requested and park is None`
  branch of the step-apply path) with `by="human"` too, while the escalation branch next to
  it already uses `req.get("by") or "human"`.
- `apply_resume` → `_enter_slot_or_wait(..., by="human")`, and
  `ESC.withdraw_for_campaign(cfg, cid, "campaign resumed by human", "human")`, which writes a
  ledger `note` (`author=by, by=by`), a queue-stream `escalation_withdrawn` event (`by=by`) and mail
  (`escalations.withdraw`)
- `apply_abandon` → `_transition(..., "ABANDONED", ..., by="human",
  withdraw_reason="campaign abandoned by human")` (`_transition` forwards its `by` to
  `withdraw_for_campaign`)

The conductor (`WT_MAILBOX_ADDR=conductor`, `skills/wt-conduct/SKILL.md`) and stewards run
these commands constantly, so campaign ledgers, `wt queue events` and the history views
attribute their actions to the human. `wt queue escalate` already does it right: `--from`
defaulting to `$WT_MAILBOX_ADDR`, then `conductor`, validated by
`mailbox.validate_address`. `cli._spec_by()` uses the same env default.

## Goals / Non-goals

**Goals**
- `wt queue park|resume|abandon|prioritize` take `--by ADDR` (default `$WT_MAILBOX_ADDR`,
  else `human`), validated as a mailbox address, and every ledger event they write carries
  that `by`.
- A deferred park keeps its requester through to the tick that applies it.

**Non-goals**
- `wt queue add` / `merge` / `add-idea` attribution (a separate concern).
- Changing park *reasons* (`reason: "human"` stays the category meaning "operator park").
- Authentication — `--by` is a label, as with `escalate --from`.

## Decision

Add `--by` to the four commands; thread it through the plan dict (`plan["by"]`) into the
appliers; replace each literal `by="human"` there with the plan's actor. Default resolution
lives in one helper `cli_queue._actor(cfg, by)` = `M.validate_address(cfg, by or
os.environ.get("WT_MAILBOX_ADDR") or "human")` (`cli_queue` gains `from . import mailbox as
M`). `_actor` is called first inside each command's existing `try: … except ValueError →
ClickException` block, before any `plan_*`/`apply_*`, so an invalid actor — from `--by` or
from `$WT_MAILBOX_ADDR` — exits 1 and writes nothing (same as `escalate`).
Default is `human` (not `conductor` as for escalate) because these are human commands by
default — only escalation is conductor-originated by nature. The deferred park stores
`park_requested["by"]` and the tick applies it with `by=req.get("by") or "human"`; the
deferred note's `author` is the same actor.

## Design

- `src/wt/agentic/cli_queue.py`: `queue_park`, `queue_resume`, `queue_abandon`,
  `queue_prioritize` gain `@click.option("--by", default=None, help="who acts (default:
  $WT_MAILBOX_ADDR, then human)")`; each calls `_actor(cfg, by)` and passes `by=` to its
  `Q.plan_*`. The `--json` payload includes `by` (it is in the plan).
- `src/wt/agentic/queue.py`: `plan_prioritize/plan_park/plan_resume/plan_abandon` accept
  `by="human"` and put it in the plan; `apply_*` use `plan.get("by") or "human"` (old plans
  without the key still work). `apply_park` deferred: `park_requested` gains `"by"`; the note
  event uses `author=by, by=by`. Storing `park_requested["by"]` feeds every existing
  consumer, each of which keeps its current default rather than adding a new one:
  `plan_step` already copies `by=req.get("by")` into the park `Step` (applied by `_park_row`
  as `step.by or ("human" if …)`), the history view reads `req.get("by") or "human"`, and the
  tick's park-request handler — the only literal — changes
  `_park(cfg, current, "human", detail, ..., by="human")` → `by=req.get("by") or "human"`.
- `apply_resume`: `ESC.withdraw_for_campaign(cfg, cid, f"campaign resumed by {by}", by)`.
  `apply_abandon`: `withdraw_reason=f"campaign abandoned by {by}"`.
- No schema change: ledger `by` is already a free string; the `_HUMAN_EVENTS` supersession
  check tests `by != "tick"`, so `conductor`/`steward:X` behave like `human` there.

## Alternatives considered

- **Default `conductor`** like escalate — would mislabel a human typing in a plain shell.
- **Name it `--from`** — `by` matches the ledger field and `wt spec` reservations
  (`_spec_by`); escalate keeps `--from` for mailbox symmetry.

## Acceptance criteria

- [ ] `wt queue park|resume|abandon|prioritize ... --by conductor --yes` writes ledger events
      whose `by` is `conductor` (transition, park/resume/abandon events alike).
- [ ] With no `--by` and `WT_MAILBOX_ADDR=steward:<Project>`, events carry that address; with
      neither set, `by: human` (unchanged behaviour).
- [ ] An invalid `--by nobody` exits 1 with the `validate_address` message and writes nothing.
- [ ] A deferred park requested `--by conductor` is applied by the tick with `by: conductor`
      and its note has `author: conductor`; resuming a campaign with an open escalation
      `--by conductor` writes the withdrawal note with `by: conductor`.
- [ ] `--json` output of the four commands includes `by`.

## Test plan

- **Automated tests** (`tests/test_agentic_queue_by.py`, new; reuse the repo/cfg/script
  helpers used by `tests/test_agentic_queue_cli.py`):
  - `test_park_resume_abandon_record_by` — park (immediate, QUEUED campaign) `--by conductor`,
    resume `--by conductor`, abandon `--by steward:<project>`; assert the `by` of the new
    ledger records via `store.read_ledger`.
  - `test_prioritize_records_by_from_env` — `monkeypatch.setenv("WT_MAILBOX_ADDR",
    "conductor")`, prioritize without `--by` → `by == "conductor"`.
  - `test_default_by_is_human` — env unset → `by == "human"`.
  - `test_invalid_by_refused` — `--by nobody --yes` exits 1, ledger length unchanged; same with
    no `--by` and `WT_MAILBOX_ADDR=steward:NoSuchProject`.
  - `test_resume_withdrawal_records_by` — escalate a campaign (`wt queue escalate C … --yes`,
    parking it `human_decision`), resume `--by conductor`; the withdrawal ledger `note` and the queue-stream
    `escalation_withdrawn` events have `by == "conductor"`.
  - `test_deferred_park_keeps_requester` — park while a run is in flight (deferred mode)
    `--by conductor`, let the tick apply it, assert the park event `by == "conductor"` and the
    note `author == "conductor"`; assert on whichever path the tick takes (park `Step` via
    `plan_step`/`_park_row`, or the park-request handler).
- **Sabotage check:** restore the literal `by="human"` in `apply_park` → the park assertion in
  `test_park_resume_abandon_record_by` fails; drop `park_requested["by"]` →
  `test_deferred_park_keeps_requester` fails; restore the literal in `apply_abandon` → the
  abandon assertion fails; pass `"human"` again to `withdraw_for_campaign` in `apply_resume`
  → `test_resume_withdrawal_records_by` fails. Restore.
- **Manual verification:** in a scratch data dir, `WT_MAILBOX_ADDR=conductor wt queue park C
  --reason x --yes`, then `wt queue events` shows `by conductor`.
- **Regression guard:** full `uv run pytest`, notably `tests/test_agentic_queue_cli.py` and the
  supersession tests that rely on `by != "tick"`.

## Rollout / migration

None; old ledger records keep `by: human`. Behaviour change: a stale or misspelled
`$WT_MAILBOX_ADDR` (e.g. a steward of a removed project) now makes these four commands fail
with the `validate_address` error, as `escalate` already does — deliberate, since silently
falling back would mislabel the ledger; fix the env or pass `--by`. The conductor/steward skills already export
`WT_MAILBOX_ADDR`, so attribution becomes correct with no skill change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
