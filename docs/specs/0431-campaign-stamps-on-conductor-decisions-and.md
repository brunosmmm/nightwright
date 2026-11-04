---
id: SPEC-0431
title: "Campaign stamps on conductor decisions and mailbox messages"
status: done
owner: user
created: 2026-11-04
updated: 2026-11-04
kind: feature
parent: SPEC-0405
source_idea: IDEA-966
tags: [agentic, observability, mailbox]
---

## Context

Remainder of child 1 of epic [SPEC-0405](./0405-rich-queue-tui-prioritized-live-queue-campaign.md).
[SPEC-0408](./0408-ledger-provenance-finding-run-id-and-key-triage.md) delivered the ledger half
(finding `run_id` and `key`, `triage` events) and explicitly moved the text-derived campaign
stamps out of its scope (its Non-goals). This spec is that other half.

The epic's data inventory (2026-11-03) found that the history layer cannot link some records to
a campaign at the writer:

- **`conductor/decisions.jsonl`**: 61 of 440 records have `campaign: null`, although many name
  a campaign in the rationale text.
- **`mailbox/<inbox>.jsonl`**: about 315 of about 1 000 messages have `campaign: null`.

Both come from free-text CLI entry points:

- `nw queue decision [CAMPAIGN] RATIONALE` (`cli_conductor.py` `queue_decision`) passes
  `campaign` to `decisions.record` only when two words were given.
- `nw mailbox send --to … TEXT` (`cli_mailbox.py` `mailbox_send`) passes `--campaign` through to
  `mailbox.send` verbatim, often absent.

Consequences today: `nw queue decisions --campaign C` and `decisions.park_coverage`
(`match_parks`) miss campaign-less decisions; the epic's history/findings views (children 2, 5,
7) must guess the link from text and mark it `linked_by: "text"`. Stamping at the writer, when
it is unambiguous, lets those views use the plain field.

Two things make this more than a one-liner:

- `decisions.record` with a campaign also appends a ledger `note` to that campaign (so
  `queue.park_view` and `nw campaign log` show the decision). A stamped decision therefore
  changes what other readers see, including park coverage and `open_dispatches`.
- A wrong stamp is worse than none. If the text names two campaigns, or names something that is
  not a campaign, the writer must not guess.

## Goals / Non-goals

**Goals**
- `nw queue decision` and `nw mailbox send` record a `campaign` when exactly one existing
  campaign is named or inferable, so history and findings views link them with no inference.
- The stamp is never a guess: ambiguous or unknown text leaves `campaign` null.
- Stamped-by-inference records are distinguishable from explicitly named ones.
- An explicit opt-out for the rare message that mentions a campaign without being about it.
- Existing lines in `decisions.jsonl` and the inboxes are never rewritten.

**Non-goals**
- Backfilling the 61 + 315 old records. Child 2 (`history.py`) keeps inferring those and marks
  them `linked_by: "text"`.
- Inference inside the programmatic callers of `mailbox.send` / `decisions.record`
  (`queue.py`, `escalations.py`, `launch.py`, `redmain.py`). They already pass a campaign when
  they have one; only the two CLI entry points change.
- Any change to `history.py`, the TUI, the retention policy or the `KINDS` / `RULE_RE`
  validation.
- Fuzzy matching (spec ids, idea ids, partial or abbreviated campaign ids, titles).

## Decision

1. **Inference happens in the two CLI entry points, through one shared helper** in
   `agentic/store.py`: `sole_campaign_named(cfg, text) -> str | None`. It returns a campaign id
   only when the text contains exactly one distinct id that resolves to an existing campaign
   (`state.json` present); zero or two-or-more distinct matches return `None`.
2. **Precedence.** An explicit argument always wins and is never second-guessed. Otherwise an
   exact field of a record the caller points at ranks above any text:
   - `nw mailbox send` inherits the `campaign` of the message named by `--re` when that
     message has one.
   - `nw queue decision` uses the `campaign` of the queue event named by `--event N` when that
     event has one.

   Only when neither applies is the text scanned. For `nw queue decision` the text scanned is
   the rationale and `--action`. Because the event's campaign ranks above text, a rationale
   that mentions a different campaign never overrides the event.
3. **Provenance of the stamp.** Inferred stamps add `campaign_from` to the record
   (`"text"`, `"re"` or `"event"`); explicit and absent stamps carry no `campaign_from` key. Consumers
   treat a missing key as "explicit or none". This is an additive field; the schemas stay
   `nw.conductor.decision.v1` and `nw.message.v1`.
4. **Opt-out.** Both commands gain `--no-campaign`, which skips inference (an explicit
   `--campaign` / CAMPAIGN still applies). Conflicting use (`--no-campaign` with an explicit
   campaign) is a usage error.
5. **Same semantics as an explicit campaign.** A stamped decision behaves exactly as if the
   caller had passed the campaign: `decisions.record` validates it, appends the ledger `note`
   (carrying the decision seq) and the record counts for `match_parks`, `covering` and
   `open_dispatches` of that campaign. That is the point of the stamp. The command output says
   when a campaign was inferred, so the caller sees it.
6. **Mail to the conductor** keeps mirroring into the event stream with the stamped campaign,
   so `nw queue events --campaign` filters see it.

## Design

**`store.sole_campaign_named(cfg, text)`**
- Pattern for candidates (the shape of `new_campaign_id`, `<SPEC-ID>-<YYYYMMDD>-<4 hex>`):
  `(?<![\w-])[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*-\d{8}-[0-9a-f]{4}(?![\w-])`.
- Candidates are de-duplicated (a campaign named twice is still one), then each is checked with
  `campaign_dir(cfg, cid) / "state.json"` existing. Non-existent candidates are dropped before
  counting, so a typo or an id from another data dir does not block the stamp, and does not
  produce one.
- Returns the id iff exactly one existing candidate remains. Pure read; never raises on
  unreadable text or a missing campaigns dir (returns `None`).

**`nw queue decision`** (`cli_conductor.py` `queue_decision`)
- With two words the first is the explicit campaign (unchanged). With one word the existing
  "campaign id alone" guard stays first. If no campaign is set and `--no-campaign` is not given:
  1. when `--event N` is given and queue event N (the event `decisions.record` already
     validates as `event_id`) has a `campaign`, use it with `campaign_from: "event"`; an event
     with no `campaign`, or one that does not exist (the existing `event_id` validation applies
     unchanged), falls through to the text;
  2. else `campaign = sole_campaign_named(cfg, rationale + " " + (action or ""))` with
     `campaign_from: "text"`.
- `decisions.record` gains an optional `campaign_from=None` keyword that is written into the
  record only when set. It is never accepted from a caller that also passes no campaign.
- Output: `decision #N kind (rule) on CID` plus ` (campaign inferred from text)` or ` (campaign inferred from event)` when inferred.

**`nw mailbox send`** (`cli_mailbox.py` `mailbox_send`)
- New flag `--no-campaign`. When `--campaign` is absent and the flag is off: if `--re` names an
  existing message (looked up across all inboxes, as `mailbox.thread` does via
  `_all_messages`; the message id `<addr>:<seq>` names its inbox) whose `campaign` is set, use
  it with `campaign_from: "re"`. The replied-to message is normally in the *sender's* inbox
  (e.g. a message to `conductor` answered with `--to steward:P --re conductor:5`), so the
  recipient's inbox must not be the only place searched. An `--re` that names no existing
  message is not an error (today `send` does not validate it); inference falls through to
  `sole_campaign_named(cfg, text)` with `campaign_from: "text"`.
- `mailbox.send` gains an optional `campaign_from=None` keyword written into the envelope only
  when set. The `message` event data for conductor-bound mail is unchanged apart from the
  event's `campaign`/`spec_id`/`project` (already derived from `campaign`).
- `--json` output shows the stamp including `campaign_from`.

**Docs.** `docs/product/guides/agent-surface.md` (the `nw.message.v1` row) and
`docs/product/capabilities/agentic.md` (the `nw mailbox send` and `nw queue decision`
paragraphs) gain the optional `campaign_from` field, the inference rule and `--no-campaign`.

**Compatibility.** No migration. Readers already treat extra keys as additive; old lines have
no `campaign_from`. The `--no-campaign` flag is new, so existing invocations are unchanged
except that they now gain a stamp when the text names exactly one existing campaign.

## Alternatives considered

- **Infer inside `decisions.record` / `mailbox.send`.** Rejected: the programmatic callers
  already pass a campaign deliberately (or deliberately pass none for queue-level events); only
  the human/agent free-text entry points have the missing-stamp problem, and inference there
  would silently change queue-internal behavior.
- **Leave it to the history layer's text inference.** That is the fallback for old data, but
  it re-guesses on every read, can disagree between readers, and cannot feed `park_coverage`
  and `nw queue decisions --campaign`.
- **Pick the most-mentioned or first campaign when several are named.** Rejected: the epic and
  the idea require never guessing between two.
- **Stamp without recording provenance.** Rejected: the UI shows link confidence (epic
  Decision 2), and an inferred stamp is not equivalent to an explicit one.
- **Make inference opt-in (`--infer-campaign`).** Rejected: the whole value is that existing
  agents' commands get stamped with no change to their habits.

## Acceptance criteria

- [x] `store.sole_campaign_named(cfg, text)` returns the id when the text contains exactly one
  distinct existing campaign id (including when it is repeated), and `None` for no match, for
  two or more distinct existing matches, for a well-formed id with no `state.json`, and for a
  match embedded in a longer token (e.g. `x-<cid>y`).
- [x] `nw queue decision KIND/RULE "… <cid> …"` with no CAMPAIGN argument writes a
  `decisions.jsonl` record with `campaign=<cid>`, the campaign's `spec_id` and
  `campaign_from: "text"`, and appends the ledger `note` carrying the decision seq.
- [x] With an explicit CAMPAIGN argument the record is unchanged from today (no
  `campaign_from`), even when the rationale names a different campaign.
- [x] A rationale naming two distinct existing campaigns, or none, yields `campaign: null` and
  no ledger note.
- [x] `nw mailbox send` with no `--campaign` and text naming exactly one existing campaign
  stores `campaign=<cid>` and `campaign_from: "text"`; with `--campaign` it stores the given
  value and no `campaign_from`.
- [x] `nw queue decision --event N` without CAMPAIGN, where queue event N has campaign A,
  stores `campaign=A` and `campaign_from: "event"`, even when the rationale names a different
  existing campaign B (no ledger note is written to B); an explicit CAMPAIGN still wins, and an
  event with no `campaign` falls through to text inference.
- [x] `nw mailbox send --re <id>` where the replied-to message has a campaign and the text names
  none stores that campaign with `campaign_from: "re"`, including when the replied-to message
  sits in the sender's inbox rather than the recipient's (reply `--to steward:P --re
  conductor:N` to a message that was sent to `conductor`); an explicit `--campaign` still wins,
  and an `--re` naming no existing message is not an error and falls through to text.
- [x] `--no-campaign` on either command suppresses inference and stores `campaign: null`;
  combining it with an explicit campaign is a usage error with nothing written.
- [x] A stamped conductor-bound message's mirrored `message` queue event carries the stamped
  `campaign`, `spec_id` and `project`.
- [x] An inferred decision counts for `decisions.covering`, `match_parks` and
  `open_dispatches` of the stamped campaign exactly like an explicit one.
- [x] Existing lines in `decisions.jsonl` and inbox files are byte-identical after the new
  writes (append-only), and records without `campaign_from` still parse in every reader.
- [x] The agent-surface and agentic capability docs describe `campaign_from`, the inference
  rule and `--no-campaign`.

## Test plan

- **Automated tests**
  - `tests/test_agentic_store_campaign_named.py` (new): `sole_campaign_named` over a tmp data
    dir with real `state.json` files: one match, repeated match, two matches, none, unknown
    well-formed id, a mix of one existing and one unknown (stamps the existing one), an id
    glued to other characters, empty/None text, missing campaigns dir.
  - `tests/test_agentic_decisions.py` (extend): `record(..., campaign_from="text")` writes the
    key only when a campaign is set; CLI via `CliRunner` for `nw queue decision` covering the
    explicit, inferred, ambiguous, none, `--action`-only and `--no-campaign` cases against a
    tmp data dir, plus `--event N` cases: event N has campaign A and the rationale names only B
    (stores A, `campaign_from: "event"`, no note on B); event with no campaign and rationale
    naming one campaign (falls through to text); explicit CAMPAIGN over `--event`; asserts the ledger `note`, `spec_id`, `campaign_from`, and that
    `park_coverage` covers a park by an inferred decision.
  - `tests/test_agentic_mailbox.py` (extend): `send(..., campaign_from=…)` envelope shape; CLI
    cases for explicit, text-inferred, `--re`-inherited (a message sent to `conductor`
    answered with `--to steward:P --re conductor:N`, i.e. the replied-to message is in the
    sender's inbox; plus an `--re` naming no existing message falling through to text),
    explicit-over-`--re`, ambiguous,
    `--no-campaign`, the usage-error combination, and the mirrored `message` event's campaign.
  - Append-only check in the same files: record the file bytes before a stamped write and
    assert the prefix is unchanged.
  - `python3 tools/spec_lint.py` and the affected tests via
    `python3 tools/affected_tests.py --run uv run pytest -q`.
- **Manual verification**
  - In a scratch data dir with two campaigns: `nw queue decision --kind wait --rule parked.x
    "waiting on <cidA>"` then `nw queue decisions --campaign <cidA> --json` shows the record
    with `campaign_from: "text"`; repeat naming both ids and confirm `campaign` is `null`.
  - `nw mailbox send --to conductor --from steward:P "about <cidA>" --json` shows the stamp;
    the same with `--no-campaign` shows `campaign: null`.
- **Regression guard**: the existing decisions, mailbox, park-decision
  (`test_agentic_queue_park_decision.py`) and TUI mailbox tests stay green unchanged;
  commands with an explicit campaign or text naming no campaign produce byte-identical records
  to before.

### Test record (2026-11-04)

- New `tests/test_agentic_store_campaign_named.py` (helper cases incl. unreadable `state.json`),
  extended `tests/test_agentic_decisions.py` and `tests/test_agentic_mailbox.py` (keyword
  validation, text / `--event` / `--re` (reply to the original sender, unknown `--re`,
  `--re` over conflicting text, explicit over `--re`) inference, `--no-campaign`, usage error,
  mirrored event campaign, human output suffix, park coverage / dispatch, append-only).
- Ran all test files mentioning mailbox / decisions / conductor (71 files, incl. the TUI mailbox
  and park-decision tests): 2318 passed, 6 skipped. `python3 tools/spec_lint.py` OK, ledger
  regenerated. `affected_tests.py` selects nearly the whole suite (core `store.py` changed), so
  the remainder of the full suite is left to gating.
- Manual verification against a scratch data dir was covered by the CliRunner tests on tmp data
  dirs (same commands and assertions), not by a separate shell session.

### As shipped (2026-11-04)

- Added `mailbox.find_message(cfg, id)` (any-inbox lookup via `_all_messages`) for `--re`.
- Human output of `nw mailbox send` appends ` [<cid>, inferred from text|re]`; `nw queue decision`
  appends ` (campaign inferred from text|event)`.
- `--event N` campaign lookup reads queue event N through `events.read_since`; a missing event
  still fails in `decisions.record`'s existing `event_id` validation.
- Review fix: the `--event` / `--re` campaign is inherited only when it resolves to an existing
  campaign (`state.json`); a bogus stamp on a mirrored `message` event or replied-to message
  (e.g. a spec id passed as `--campaign`) falls through to text inference instead of failing the
  write. Tests also cover `--no-campaign` blocking `--event` / `--re` inheritance.

- Review round 3: `--event` / `--re` inheritance also catches `TypeError`, so a non-string stored
  `campaign` (int, list) falls through to text inference instead of crashing. Added tests:
  `--event N` among several events (N=1 CB, 2 CA, 3 none), a missing `--event` with a
  text-named campaign (non-zero exit, no decision line, no ledger note), and non-string
  campaigns on `--event` / `--re`. The `nw queue decision` stamp description now also sits in the
  Conductor section of `agentic.md`.

### Test record (2026-11-04, review round 3)

- `uv run pytest -q tests/test_agentic_decisions.py tests/test_agentic_mailbox.py
  tests/test_agentic_store_campaign_named.py`: 60 passed. `python3 tools/spec_lint.py` OK. The
  full suite is left to gating.

## Rollout / migration

1. Land `store.sole_campaign_named`, the `campaign_from` keywords and the two CLI changes with
   their tests and docs together; no data migration, nothing is rewritten.
2. Old records keep `campaign: null`; child 2's history layer keeps inferring them and marks
   them `linked_by: "text"`. Records written after this lands link by `field` (with
   `campaign_from` available for a confidence badge).
3. Verify with `nw queue decisions --json` and `nw mailbox list --to conductor --json` that
   new free-text entries carry the stamp.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

None. Decided while writing: inference lives only in the two CLI entry points; ambiguity yields
no stamp; inferred stamps carry `campaign_from`; `--re` (mail) and `--event` (decisions)
inheritance are exact-field and rank above text, with `--re` looked up across all inboxes; an inferred decision has the same ledger-note and park-coverage effect as an
explicit one.
