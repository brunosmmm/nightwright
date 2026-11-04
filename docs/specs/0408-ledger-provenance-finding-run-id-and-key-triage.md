---
id: SPEC-0408
title: "Ledger provenance: finding run_id and key, triage events"
status: proposed
owner: user
created: 2026-11-03
kind: feature
parent: SPEC-0405
source_idea: IDEA-883
tags: [agentic, ledger, observability]
---

## Context

Child 1 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md). The
epic's shared history layer (`agentic/history.py`, child 2) must link every finding to the run
that produced it, the decision that resolved it and its follow-up idea. Today two of those
links are missing at the writer, so a reader has to guess them. The epic's data inventory
(2026-11-03, 217 campaigns) found:

- **`finding` ledger events have no `run_id` and no `key`.** `queue._record_run`
  (`src/nightwright/agentic/queue.py`) turns a persona verdict (`must_fix`, `blockers`,
  reviewer `findings`) into `finding` events but writes only
  `{persona, severity, gate, title, detail, files, ac?, category?}`. The producing run is
  inferred as "the last `run.end` of that persona before the finding". The follow-up key
  `followups.finding_key(spec_id, title, files)` is recomputed by every reader. The `followup`
  event does carry `key`, so the two can only be joined by recomputing.
- **No event records a triager's decision.**
  - The review triager (`_apply_triager`) accepts waivers named by `persona#index` ids into the
    reviewed round (`_accept_waivers`). It leaves only free-text `note` events
    (`waived <id>: <reason>`, `triager ignored <id>: <why>`) and `last_review.waived` in
    `state.json`, which the next round overwrites.
  - The gate triager (the `triage` branch of `_apply`) decides `fix`, `retry_gate`, `waive` or
    `escalate`. The decision lives only in that run's `run.json` verdict and in a transition
    reason.
  - The `persona#index` ids are meaningful only inside one review round, so they cannot be
    joined to anything later.

Ledgers are append-only and ledger events allow extra keys ("additive evolution",
`schemas.py`), so this can be added to the writers without a migration. Old events stay
untouched; the history layer keeps inferring their links and marks them `linked_by: "inferred"`
(epic Decision 2).

**Scope split.** The idea also asked for text-derived `campaign` stamps on `nw queue decision`
and `nw mailbox send`. That is an independent subsystem (CLI free-text entry points, the
decision and mailbox stores) that shares no code or data with the ledger writers, and its
semantics need their own decisions (opt-out, effect on `open_dispatches` / `match_parks`). It
is split into a separate follow-up spec (see Non-goals). This spec keeps only ledger
provenance.

## Goals / Non-goals

**Goals**
- Every new `finding` event carries `key` (`followups.finding_key`) and `run_id` (the persona
  run that produced it; `null` for findings no persona run produced).
- A new `triage` ledger event records each applied review-triager and gate-triager verdict,
  with waived findings resolved to stable finding keys.
- `nw.ledger.event.v1` is documented with the new keys and the new event type.
- Existing ledger lines are never rewritten, and readers keep working on them.

**Non-goals**
- **Text-derived campaign stamps on conductor decisions and mail** (`nw queue decision`,
  `nw mailbox send`, `store.sole_campaign_named`, `campaign_from`, a `--no-campaign` opt-out,
  the effect on `decisions.open_dispatches` / `match_parks`, and the docs for them). Moved to a
  separate spec under SPEC-0405, filed as its own idea when this spec is accepted. The epic
  child 1 line describes both; this spec delivers the ledger half.
- Backfilling old campaigns. Child 2 keeps inferring them.
- Making `run_id` or `key` *required*. They stay optional so old ledgers still validate.
- Linking gate-produced findings to a `gate_seq`. They keep their `gate` name and
  `run_id: null`.
- Any change to the TUI, `history.py` or the retention policy.

## Decision

1. **`finding` gains `key` and `run_id`, stamped where the event is written.**
   - `key` is added centrally in `store.append_event`: for `type == "finding"` with no `key`, it
     is `followups.finding_key(state.spec_id, title, files)`. That covers all ~25 producers
     (`queue.py`, `close.py`, `remote_gates.py`) without touching each one and cannot drift from
     the follow-up key. If the campaign state cannot be read, the event is written without `key`
     rather than failing the append.
   - `run_id` is added in `queue._record_run`, which knows the run: every finding it emits
     (verdict `must_fix`, `blockers`, reviewer `findings`, and the run-level info findings for
     unknown cost and permission denials) carries the run's id. Findings from deterministic
     producers (gates, close, remote-gate eligibility, queue-internal checks) carry
     `run_id: null` explicitly, so a reader can tell "no run" from "old event".
2. **A new `triage` ledger event per applied triager verdict**, written in the same `events`
   list as the run's `run.end`, before the transition the verdict causes.
   - **Review triager:** `{persona: "review_triager", run_id, kind: "review", disposition,
     waive: [{id, key, severity, title, reason}], ignored: [{id, why}]}`. `waive[]` is built
     from the *accepted* entries that `_accept_waivers` returns for the verdict's `waive`
     list; `ignored[]` from its rejected `(id, why)` pairs. `disposition` is `"waive"` when at
     least one waiver was accepted and `"none"` otherwise. `id` is the original `persona#index`;
     `key` is the finding key resolved from the reviewed round at write time.
   - **Gate triager:** `{persona: "gate_triager", run_id, kind: "gate", disposition, routed_to,
     confidence, suspected_flaky, waive: [{gate, key, reason}], fix_keys: [key]}`.
     - `waive[]` is built from the verdict's **`waived`** list (`GATE_TRIAGER_SCHEMA` in
       `personas.py`; the key is `waived`, not `waive`): one `{gate, key, reason}` per entry,
       with `key` resolved as below.
     - `disposition` is the verdict's (`fix | retry_gate | waive | escalate`). `routed_to` is
       the kind of the `Step` that `decide_triage` returned (`gate | close | implement | park`),
       so it records what the queue actually did, e.g. a `retry_gate` over the retry cap routes
       to `implement`.
     - `fix_keys` are the `finding_key`s of the verdict's `must_fix` items.
     - A waived entry's `key` is the key of the one open `finding` of that gate in the current
       gate round, and `null` when there is none or more than one (never guessed).
   - No `triage` event for a run that produced no valid verdict (not started, timed out, failed,
     malformed, interrupted): those keep their existing `note`s. Only applied verdicts are
     recorded.
3. **No migration.** Only new writes change; every added key is optional.

## Design

### Finding events

- `store.append_event` (`store.py`, the `append_event` function): after building `rec`, if
  `type == "finding"` and `"key"` not in the payload, read the campaign state (the cached
  `read_state` already used under the item lock) and set
  `rec["key"] = followups.finding_key(state.get("spec_id"), rec.get("title"), rec.get("files"))`.
  `followups` is imported lazily inside the branch (it imports `queue`, which imports `store`).
  Any `ValueError`/`OSError` from reading the state skips the key. A payload that already has
  `key` is kept.
- `queue._record_run`: build the finding payloads through one local helper that adds
  `"run_id": run_id`. The `run_id` argument already exists. Key stays centralized.
- Deterministic producers are not edited; for them `append_event` adds `key`, and `run_id` is
  set to `None` only when the event has no `run_id` key, in the same branch, so *every* new
  `finding` has both keys (`key` absent only when the state is unreadable).
- `schemas.EVENT_KEYS["finding"]` stays as is (required keys unchanged).

### Triage event

- `schemas.EVENT_KEYS["triage"] = ("persona", "run_id", "kind", "disposition", "waive")`,
  with a comment citing this spec. Optional keys: `ignored`, `routed_to`, `confidence`,
  `suspected_flaky`, `fix_keys`.
- `queue._apply_triager`: in the branch where `verdict["waive"]` is a list, after
  `_accept_waivers`, append one `("triage", {...})` before the `waived …` notes. `accepted`
  already holds the finding records (`title`, `files`, `severity`) keyed by `persona#index`, so
  `key = FU.finding_key(state["spec_id"], rec["title"], rec["files"])`; `ignored` is the
  `(id, why)` list.
- Gate triager (the `# triage` branch of `_apply`, after `decide_triage`): when
  `decision.kind != "none"` and `rr.verdict_ok`, append the `triage` event before the existing
  `finish(...)` calls (all four routes: park, gate retry, close/waived, implement). `waive[].key`
  resolves against `store.read_ledger(cfg, cid, types=["finding"])` entries whose `gate`
  matches the waived entry's `gate` and whose `seq` is after the most recent `gate` event.
- Readers of ledger types that switch on `type` must tolerate the new type: the
  `nw campaign log` renderer and `journey.py` fall through to a generic line for unknown types;
  tests pin that.

### Docs

- `schemas.py` module docstring and `EVENT_KEYS` comments: add `finding.key`, `finding.run_id`
  and the `triage` event.
- `docs/product/loop/principles.md` mentions `nw.ledger.event.v1`; add one short paragraph
  there listing the provenance keys and the `triage` event.

## Alternatives considered

- **Stamp `key` at every finding producer.** About 25 call sites across three modules; any new
  producer would silently miss it. Central stamping in `append_event` is one place.
- **Make `run_id` and `key` required in `EVENT_KEYS`.** Would make every existing ledger
  invalid (`ledger_problems`), and old ledgers are read by `nw` constantly. Optional keys keep
  old events valid.
- **Store only `persona#index` ids in `triage`.** They are meaningless after the round; the
  point of the event is the stable `key`.
- **Guess a gate waive's key when several findings of that gate are open.** A wrong key looks
  authoritative and would mark an unrelated finding `waived`; `null` is honest and child 2 can
  still show the waive by gate name.
- **Keep the campaign-stamp work in this spec.** Rejected on review: it is an independent
  subsystem that roughly doubled the acceptance criteria; it ships separately.

## Acceptance criteria

- [ ] A `finding` produced from a persona verdict (`must_fix`, implementer blocker, reviewer
  finding, and the run-level info findings) is written with `run_id` equal to the producing
  run's id and `key` equal to `followups.finding_key(spec_id, title, files)`.
- [ ] A `finding` written by a deterministic producer (a gate, close, remote-gate eligibility,
  a queue-internal check) has `key` and `run_id: null`; an explicit payload `key` is not
  overwritten.
- [ ] If the campaign state is unreadable, a `finding` is still appended (without `key`) and no
  exception escapes `append_event`.
- [ ] The review triager's applied verdict writes exactly one `triage` event with
  `persona: "review_triager"`, `kind: "review"`, the run's `run_id`, `waive[]` entries whose
  `id` is the `persona#index` and whose `key` equals the key the same finding gets as a
  `followup`, the accepted `reason`, and `ignored[]` for rejected ids. A run with no valid
  verdict writes none.
- [ ] The gate triager's applied verdict writes one `triage` event with
  `persona: "gate_triager"`, `kind: "gate"`, `disposition`, `routed_to` (the `decide_triage`
  step kind), `fix_keys` (keys of the verdict's `must_fix` items) and `waive[]` built from the
  verdict's `waived` list; a waived entry gets a `key` only when exactly one open finding of
  that gate exists in the current round, otherwise `null`. It is written for each of the four
  routes (implement, gate retry, close on waive, park) and not for a malformed verdict.
- [ ] `triage` is a known type in `schemas.EVENT_KEYS` with the required keys above, and
  `append_event` rejects a `triage` missing one.
- [ ] Ledger lines written before this change are not modified, and a ledger mixing old events
  (no `key`/`run_id`, no `triage`) with new ones is read by `store.read_ledger` and
  `ledger_problems` without error.
- [ ] `nw campaign log` and `journey.py` render a campaign whose ledger has `triage` events
  without error.
- [ ] `schemas.py` docs and `docs/product/loop/principles.md` describe `finding.key`,
  `finding.run_id` and the `triage` event.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_ledger_provenance.py` (new), over the real writers and a tmp data dir:
    - `store.append_event` stamping: finding gets `key` equal to `followups.finding_key`;
      explicit `key` kept; `run_id` defaulted to `None`; unreadable state still appends;
      `triage` validated by `ledger_problems`, and one missing a required key rejected.
    - `queue._record_run` with a fake `RunResult` carrying `must_fix`, `blockers` and
      reviewer `findings`: every emitted `finding` carries the run's `run_id` and the right
      `key` after being appended.
    - `_apply_triager` through the existing review-triage harness
      (`tests/test_agentic_review_triage.py` helpers): accepted waiver gives a `triage`
      event whose `waive[].key` equals the `followup` key for the same finding; ignored ids
      (major finding, empty reason, duplicate) land in `ignored`; timed-out and malformed runs
      write no `triage`.
    - Gate triager through `tests/test_agentic_queue.py` helpers: one test per route
      (`fix` to implement, `retry_gate`, `waive` to close, `escalate` to park) asserting
      `disposition`, `routed_to`, `fix_keys`; a `retry_gate` past the retry cap asserting
      `routed_to: "implement"`; the waive-key resolution cases, with the verdict's **`waived`**
      list set (exactly one open gate finding, none, two); a verdict that sets only `waive`
      (wrong key) yields an empty `waive[]`.
    - Backward compatibility: a ledger fixture of old events (no `key`/`run_id`, no `triage`)
      plus new ones is read by `store.read_ledger`, `nw campaign log` and `journey` without
      error, and the old lines are byte-identical after new appends.
- **Manual verification:** after a real run that produces a reviewer finding, in a scratch data
  dir, `grep '"type": "finding"'` on the campaign's `ledger.jsonl` shows `key` and `run_id`,
  and the `run_id` matches a `runs/` dir. After a review-triager round with a waiver,
  `nw campaign log <cid>` shows a `triage` line and its `waive[].key` equals the `followup`
  event's `key`.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_agentic_ledger_provenance.py`, `test_agentic_review_triage.py` and
  `test_agentic_queue.py`; existing ledger tests pass unmodified apart from additions;
  `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Land the writer changes, schema entries and docs together; no data migration, no flag.
2. New campaigns write the new keys from the next tick of an updated runner; running campaigns
   mix old and new events, which readers already tolerate.
3. Child 2 of SPEC-0405 reads the keys when present (`linked_by: "field"`) and infers
   otherwise.
4. The text-derived campaign stamps (Non-goals) land independently and are not a prerequisite
   for child 2.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (affected tests via
  `python3 tools/affected_tests.py --run uv run pytest -q`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing: gate-produced findings carry `run_id: null` and no `gate_seq`
(their `gate` name is enough for child 2); the campaign-stamp half of the idea ships as a
separate spec.
