---
id: SPEC-0408
title: "Ledger provenance: finding run_id and key, triage events, campaign stamps on decisions and mail"
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
that produced it, the decision that resolved it and its follow-up idea. Today three links are
missing at the writer, so a reader has to guess them. The epic's data inventory (2026-11-03,
217 campaigns) found:

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
- **Conductor decisions and mail often have no `campaign`.** 61 of 440
  `conductor/decisions.jsonl` records and 315 of about 1 000 `mailbox/*.jsonl` messages carry
  none, although many name exactly one campaign id in their text. `nw queue decision` takes the
  campaign only as an explicit first positional argument (`cli_conductor.py`
  `queue_decision`); `nw mailbox send` only through `--campaign` (`cli_mailbox.py`
  `mailbox_send`).

Ledgers are append-only and ledger events allow extra keys ("additive evolution",
`schemas.py`), so all of this can be added to the writers without a migration. Old events stay
untouched; the history layer keeps inferring their links and marks them `linked_by: "inferred"`
or `"text"` (epic Decision 2).

## Goals / Non-goals

**Goals**
- Every new `finding` event carries `key` (`followups.finding_key`) and `run_id` (the persona
  run that produced it; `null` for findings no persona run produced).
- A new `triage` ledger event records each applied review-triager and gate-triager verdict, with
  waived findings resolved to stable finding keys.
- `nw queue decision` and `nw mailbox send` stamp `campaign` when the caller gave none and the
  text names exactly one existing campaign, and mark the stamp as text-derived.
- `nw.ledger.event.v1` is documented with the new keys and the new event type.
- Existing ledger lines, decision records and messages are never rewritten.

**Non-goals**
- Backfilling old campaigns, decisions or mail. Child 2 keeps inferring them.
- Making `run_id`, `key` or `campaign` *required*. They stay optional so old ledgers still
  validate.
- Linking gate-produced findings to a `gate_seq`. They keep their `gate` name and `run_id: null`.
- Stamping any other writer (`nw campaign note`, escalations, `nw queue events`) or guessing a
  campaign from anything but explicit ids in the text.
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
   - Review triager: `{persona: "review_triager", run_id, kind: "review", disposition,
     waive: [{id, key, severity, title, reason}], ignored: [{id, why}]}`. `disposition` is
     `"waive"` when at least one waiver was accepted and `"none"` otherwise. `id` is the
     original `persona#index`; `key` is the finding key resolved from the reviewed round at
     write time.
   - Gate triager: `{persona: "gate_triager", run_id, kind: "gate", disposition, routed_to,
     confidence, suspected_flaky, waive: [{gate, key, reason}], fix_keys: [key]}`.
     `disposition` is the verdict's (`fix | retry_gate | waive | escalate`); `routed_to` is the
     queue's decision (`decide_triage` kind: `gate | close | fix | park`). `fix_keys` are the
     keys of the verdict's `must_fix` items. A waived gate entry's `key` is the key of the one
     open `finding` of that gate in the current gate round, and `null` when there is none or
     more than one (never guessed).
   - No `triage` event for a run that produced no valid verdict (not started, timed out, failed,
     malformed, interrupted): those keep their existing `note`s. Only applied verdicts are
     recorded.
3. **Campaign stamps are text-derived, exact-one only, and marked.**
   - A shared helper `store.sole_campaign_named(cfg, *texts) -> str | None` extracts
     campaign-id-shaped tokens (`<PREFIX>-<digits>-<YYYYMMDD>-<hex>`) from the texts, keeps those
     whose `campaigns/<id>/state.json` exists, de-duplicates them, and returns the id only when
     exactly one remains. Two distinct existing ids, none, or only unknown ids give `None`.
   - `nw queue decision` calls it over `rationale` and `--action` when no campaign was given;
     `nw mailbox send` calls it over the message text when `--campaign` was not given. An
     explicit campaign always wins and is never overridden.
   - A stamped record carries `campaign_from: "text"`; an explicit campaign leaves the key
     absent. A stamped decision then follows the existing campaign path (the ledger `note`
     with the `decision` back-reference), exactly as if the id had been passed.
4. **No migration.** Only new writes change; every added key is optional.

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
  `finding` has both keys.
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
  `finish(...)` calls (all four routes: park, gate retry, close/waived, fix). `waive[].key`
  resolves against `store.read_ledger(cfg, cid, types=["finding"])` entries whose `gate`
  matches and whose `seq` is after the most recent `gate` event.
- Readers of ledger types that switch on `type` must tolerate the new type: the
  `nw campaign log` renderer and `journey.py` fall through to a generic line for unknown types;
  the spec's tests pin that.

### Campaign stamps

- `store.sole_campaign_named(cfg, *texts)`: regex `\b[A-Za-z][A-Za-z0-9_]*-\d+-\d{8}-[0-9a-f]{4,8}\b`;
  existence is `(campaign_dir(cfg, id) / "state.json").is_file()`. Pure read, no new lock.
  `texts` may contain `None`.
- `cli_conductor.queue_decision`: when `campaign is None`, after the existing "a campaign id
  alone" guard, `campaign = S.sole_campaign_named(cfg, rationale, action)`; if found, pass
  `campaign_from="text"`. `decisions.record` gains a keyword `campaign_from=None`, written into
  the `nw.conductor.decision.v1` record only when set. The console line says
  `on <campaign> (from text)` for a stamp.
- `cli_mailbox.mailbox_send`: when `campaign is None`, compute the stamp over `" ".join(text)`
  and pass it to `mailbox.send`, which gains `campaign_from=None` and writes it into the
  `nw.message.v1` record only when set. The conductor event mirror in `mailbox.send` then
  carries the stamped campaign unchanged.
- `mailbox.send` and `decisions.record` called from Python with `campaign=None` do not infer;
  inference lives in the two CLI commands (the human/agent entry points where ids are free
  text).

### Docs

- `schemas.py` module docstring and `EVENT_KEYS` comments: add `finding.key`, `finding.run_id`
  and the `triage` event.
- `docs/product/loop/principles.md` mentions `nw.ledger.event.v1`; add one short paragraph
  there listing the provenance keys, and note `campaign_from` on decision and message records.

## Alternatives considered

- **Stamp `key` at every finding producer.** About 25 call sites across three modules; any new
  producer would silently miss it. Central stamping in `append_event` is one place.
- **Make `run_id` and `key` required in `EVENT_KEYS`.** Would make every existing ledger
  invalid (`ledger_problems`), and old ledgers are read by `nw` constantly. Optional keys keep
  old events valid.
- **Infer the campaign inside `decisions.record` / `mailbox.send`.** Python callers (the queue's
  own writers) always pass a campaign; inferring there would change behavior for internal
  callers and could attach a campaign to a deliberately queue-level record. The CLI entry points
  are where free text arrives.
- **Pick the first (or most frequent) campaign when the text names several.** Rejected by the
  idea: a wrong stamp is worse than none, because it looks authoritative.
- **Store only `persona#index` ids in `triage`.** They are meaningless after the round; the
  point of the event is the stable `key`.

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
- [ ] The gate triager's applied verdict writes one `triage` event with `persona:
  "gate_triager"`, `kind: "gate"`, `disposition`, `routed_to`, `fix_keys` (keys of the verdict's
  `must_fix` items) and `waive[]` entries; a waived gate entry gets a `key` only when exactly
  one open finding of that gate exists in the current round, otherwise `null`. It is written
  for each of the four routes (fix, retry, waive-to-close, escalate/park) and not for a
  malformed verdict.
- [ ] `triage` is a known type in `schemas.EVENT_KEYS` with the required keys above, and
  `append_event` rejects a `triage` missing one.
- [ ] `nw queue decision` with no campaign and a rationale naming exactly one existing
  campaign id records that campaign, `campaign_from: "text"`, and the matching ledger `note`; an
  explicit campaign is kept as given and has no `campaign_from`.
- [ ] `nw queue decision` and `nw mailbox send` stamp nothing when the text names two different
  existing campaign ids, none, or only ids that do not exist (the record has `campaign: null`).
- [ ] `nw mailbox send` without `--campaign` and with text naming exactly one existing campaign
  writes `campaign` and `campaign_from: "text"` on the message and on its conductor event
  mirror; `--campaign` wins when given.
- [ ] Ledger lines, `decisions.jsonl` lines and mailbox lines written before this change are
  not modified, and reading them (`store.read_ledger`, `decisions.read`, `mailbox.read_inbox`,
  `nw campaign log`) works unchanged.
- [ ] `nw campaign log` and `journey.py` render a campaign whose ledger has `triage` events
  without error.
- [ ] `schemas.py` docs and `docs/product/loop/principles.md` describe `finding.key`,
  `finding.run_id`, the `triage` event and `campaign_from`.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_ledger_provenance.py` (new), over the real writers and a tmp data dir:
    - `store.append_event` stamping: finding gets `key` equal to `followups.finding_key`;
      explicit `key` kept; `run_id` defaulted to `None`; unreadable state still appends;
      `triage` validated by `ledger_problems`.
    - `queue._record_run` with a fake `RunResult` carrying `must_fix`, `blockers` and
      reviewer `findings`: every emitted `finding` carries the run's `run_id` and the right
      `key` after being appended.
    - `_apply_triager` through the existing review-triage harness
      (`tests/test_agentic_review_triage.py` helpers): accepted waiver gives a `triage`
      event whose `waive[].key` equals the `followup` key for the same finding; ignored ids
      (major finding, empty reason, duplicate) land in `ignored`; timed-out and malformed runs
      write no `triage`.
    - Gate triager through `tests/test_agentic_queue.py` helpers: one test per route
      (fix, `retry_gate`, waive, escalate) asserting disposition, `routed_to`, `fix_keys`;
      the waive-key resolution cases (exactly one open gate finding, none, two).
    - `store.sole_campaign_named`: one id, repeated same id, two ids, unknown id, `None` and
      empty texts, id embedded in punctuation.
  - `tests/test_agentic_decisions.py` (extended): `queue decision` CLI stamps with
    `campaign_from: "text"` and writes the ledger note; no stamp for two ids; explicit
    campaign wins; the guard for a lone campaign id still fires.
  - `tests/test_agentic_mailbox.py` (extended): `mailbox send` CLI stamp, no-stamp cases,
    `--campaign` wins, conductor event mirror carries the campaign.
  - `tests/test_agentic_ledger_provenance.py` also pins backward compatibility: a ledger
    fixture of old events (no `key`/`run_id`, no `triage`) plus new ones is read by
    `store.read_ledger`, `nw campaign log` and `journey` without error.
- **Manual verification:** in a scratch data dir with a real campaign id `C`:
  `nw queue decision --kind wait --rule parked.cost_cap "waiting on <C>"` then
  `nw queue decisions --json` shows `campaign: C` and `campaign_from: "text"`; the same with
  two real ids shows `campaign: null`. `nw mailbox send --to conductor --from human "<C> done"`
  then `nw mailbox list --to conductor --json` shows the stamp. After a real run that
  produces a reviewer finding, `grep '"type": "finding"'` on the campaign's `ledger.jsonl`
  shows `key` and `run_id` and the `run_id` matches a `runs/` dir.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_agentic_ledger_provenance.py`, `test_agentic_decisions.py`,
  `test_agentic_mailbox.py`, `test_agentic_review_triage.py`, `test_agentic_queue.py`; existing
  ledger/decision/mail tests pass unmodified apart from additions; `python3 tools/spec_lint.py`
  exits 0.

## Rollout / migration

1. Land the writer changes, schema entries and docs together; no data migration, no flag.
2. New campaigns write the new keys from the next tick of an updated runner; running campaigns
   mix old and new events, which readers already tolerate.
3. Child 2 of SPEC-0405 reads the keys when present (`linked_by: "field"`) and infers
   otherwise.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (affected tests via
  `python3 tools/affected_tests.py --run uv run pytest -q`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing: gate-produced findings carry `run_id: null` and no `gate_seq`
(their `gate` name is enough for child 2); `campaign_from` marks text-derived stamps so child 2
can label them `linked_by: "text"`; inference lives in the CLI entry points only.
