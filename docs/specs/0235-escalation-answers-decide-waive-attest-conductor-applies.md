---
id: SPEC-0235
title: "Escalation answers: wt escalations answer (choose/waive/attest/text); the conductor applies them"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, conductor, escalation, review, triage]
source_idea: IDEA-087
depends_on: [SPEC-0232, SPEC-0203, SPEC-0206]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from `IDEA-087` (part 2 of 2).
[SPEC-0232](./0232-escalation-records-wt-queue-escalate-and-notify.md) creates escalation
records (`wt.escalation.v1`, `ESC-NNNN`), parks the campaign as `human_decision`, and mails
and notifies the human. Two things are still missing:
- the human has no command to answer;
- the conductor gets no signal that an answer arrived.

Many escalations come from the review loop: `loop_exhausted` / `triage_escalated` after the
reviewers keep asking for the same fix.
[SPEC-0203](./0203-review-triage-score-and-routing.md) scores verdicts
(`queue.triage_reviews`), and [SPEC-0206](./0206-review-triager-persona.md) lets the review
triager waive `minor`/`nit` findings only (`queue._accept_waivers`).

The human sometimes has a different answer:
- "that `major` is wrong, ship it";
- "AC 4 is met; I checked by hand".

Today the human can only express that as a resume note, which the next reviewer may ignore.
The fix loop then runs again on the same finding.

## Goals / Non-goals

**Goals**
- G1. `wt escalations answer ID` with exactly one answer form:
  - `--choose` an option;
  - an **override**: `--waive FINDING` and/or `--attest AC`, with `--reason`;
  - free `--text`.

  The answer is recorded on the escalation and on the campaign ledger.
- G2. An `escalation_answered` event wakes the conductor, which applies the answer (resume,
  abandon, reprioritize, re-review). The answer command never moves the campaign itself.
- G3. Human waivers (any severity) and attestations persist on the campaign, are given to
  every later reviewer as "do not re-raise", and are honoured by `triage_reviews` as a safety
  net (with the matching limits stated in Design), so the fix loop is unblocked.

**Non-goals**
- A TUI answer form (IDEA-085).
- Answering by replying in the mailbox. The command is the one entry point, and the steward
  relays to it.
- Letting the review triager (SPEC-0206) waive more than `minor`/`nit`. Only the human
  overrides severity.
- Auto-resume on answer. The conductor's table stays the single place where a park is
  resolved.

## Decision

Add `escalations.plan_answer` / `apply_answer` and a `wt escalations answer` command (dry-run,
then `--yes`). Apply runs, in this order:
1. Write the answer into the record (status `answered`).
2. Append a ledger `human_decision` event and a `note` on the campaign.
3. Store override items in `state["human_overrides"]`.
4. Append an `escalation_answered` queue event.

`triage_reviews` / `decide_reviews` gain `human_waived` and `attested` arguments, fed from
`state["human_overrides"]` by `queue._route_reviews`, `_accept_waivers` and the
`needs_triager` path. The conductor skill gets an `escalation_answered` row.

## Design

### Command

```
wt escalations answer ID (--choose N|LABEL | --text TEXT |
                          [--waive FINDING]... [--attest AC]... --reason TEXT)
                         [--note TEXT] [--by ADDR] [--yes] [--json]
```

- Exactly one form is allowed:
  - `choose`;
  - `text`;
  - `override` (one or more `--waive` / `--attest`, combinable, with a required non-empty
    `--reason`).

  Mixing forms is a `UsageError`. `--note` is optional free text and is allowed with any
  form.
- `--by` defaults to `human`. The steward passes `--by steward:<P>` when it relays the human's
  words (see Skills). It must be a valid mailbox address.
- `plan_answer(cfg, id, ...)` validates. Errors are `ValueError`, turned into
  `ClickException`:
  - Refuses a record that is not `open` (the error says answered or withdrawn, and when).
  - `choose`: the value must be a 1-based option number or an option label (exact match
    after strip and casefold). The record must have options.
  - `override`: the record must have a campaign. Each `--waive FINDING` must be the id of a
    finding (`blocking` or `advisory`, synthetic included, any severity) in
    `triage_reviews(stored, profile)`:
    - `stored` is every reviewer verdict in `state["last_review"]["verdicts"]`, in profile
      order, regardless of `loop`, because the human answers about the review that led to the
      park;
    - `profile` is `profiles.resolve_profile(cfg, state.get("profile"))`.

    Each `--attest AC` is either a synthetic AC finding id (`<persona>#ac<j>`, resolved to its
    `ac` text) or a text that `queue._covers` matches against an `ac_assessment` entry with
    `met: false` in those verdicts. Anything else is refused and names the valid ids (at most
    10).
  - Reach of an attestation: the plan lists every distinct unmet AC text that each
    `--attest` covers. When a text covers **more than one** distinct AC, the plan is refused
    and lists them, asking for a longer text or a `#ac` id. Choice: refuse rather than
    warn. `_covers`' substring rule would silently extend the attestation to other ACs in
    every later review.
  - A `--waive` of a real finding whose `ac` covers an unmet entry also lists that AC in the
    plan as `implicitly attested` (see Triage).
- The plan prints the record's question and options, the resolved answer and, for
  overrides, each finding (`id severity title`) and each AC. `--json` emits
  `wt.escalation.answer.v1` with `written`.

### Apply (`apply_answer`)

Locking follows SPEC-0232's order: `store.item_lock(cfg, cid)` when there is a campaign,
then, nested, the escalations lock (`events._locked(<escalations>/.lock)`), always. The
escalations lock is held across the record's read-check-write. Inside it `apply_answer`
re-reads the record and **re-checks `status == "open"`**. A record withdrawn or answered
since the plan raises `ValueError("<id> is <status> since <ts>")`, and nothing is written.
`remind_due` and `withdraw` write the record under the same lock, so no lost update is
possible. The state and ledger writes (steps 2 and 3) happen under `item_lock` after the
record write. The event (step 4) is appended after both locks are released.
1. Record:
   - `status: answered`;
   - `answer: {kind: choose|text|override, choice: n|null, label: str|null, text:
     str|null, waive: [ids], attest: [ac texts], reason: str|null, note: str|null}`;
   - `answered_by`, `answered_at`.
2. Campaign ledger (when there is a campaign):
   - `store.append_event(cfg, cid, "human_decision", {"escalation": id, **answer}, by=<by>)`;
   - a `note` with the text `<by>: <id> <summary>`. The summary format is pinned:
     - choose: `chose <n> (<label>)`;
     - override: `waived <id>, <id>; attested "<ac>", "<ac>"; reason: <reason>`. Items are
       joined by `, ` and sections by `; `, and an empty section is omitted. Each AC text is
       cut to 60 chars, with `…` added when cut;
     - text: `text: <text folded to one line, cut to 200 chars>`;
     - when `--note` is given: ` — note: <note cut to 200 chars>` appended.

     Example: `human: ESC-0003 waived conformance_reviewer#1; attested "AC 4 output is
     sorted"; reason: checked by hand`.
   - The note marks the park decided under SPEC-0222's rule. That is harmless, because a
     `human_decision` park is human-owned and never overdue.
   - `human_decision` is not projected by `events._project_ledger_event`, so it raises no
     second queue event.
3. Overrides: append to `state["human_overrides"] = {"waive": [{id, title, files, severity,
   synthetic, ac, reason, escalation, at}], "attest": [{ac, reason, escalation, at}]}` and write
   the state. The state stays `PARKED` with `park` unchanged.
4. `events.append(cfg, "escalation_answered", campaign=…, spec_id=…, project=…, by=<by>,
   data={"escalation_id": id, "kind": kind, "summary": summary, "answer": answer})`.
   `EVENT_TYPES["escalation_answered"] = ("escalation_id", "kind", "summary")`. This is the
   conductor's wake-up. No mail is sent, because mail to the conductor would add a second
   `message` event for the same answer.

### Triage honours overrides (`queue.py`)

- `triage_reviews(verdicts, profile, waived=(), human_waived=(), attested=())`:
  - `attested`: an `ac_assessment` entry with `met: false` that `_covers(a, entry.ac)` for
    any attested `a` is treated as met. It does not set `unmet` and gets no synthetic `#ac`
    finding. The effective attested set is `attested` plus the `ac` of every **real**
    human-waived finding that has one. Waiving a real `major` that covers an unmet AC
    therefore also clears that AC. Otherwise `unmet` (set before the coverage check in the `ac_assessment` loop, L290-L296)
    would keep the outcome at `fix`, and no `#ac` id exists for the human to attest.
    Choice: option (a). The human waived the very finding that stands for that AC, and
    `plan_answer` shows the AC as `implicitly attested`, so the reach is visible.
  - `human_waived` (the stored waive dicts): a merged finding is removed, **whatever its
    severity**, synthetic included, when either:
    - its `id` equals a waiver's `id` and its normalized title equals the waiver's; or
    - it is not synthetic, its `norm_finding_title(title)` equals the waiver's, and its
      `sorted(files)` equals the waiver's (files included).
    The second rule matches a re-review's findings: ids are positional, and a fresh review
    renumbers them. A merged finding (two reviewers with the same files and title) is waived
    through its surviving merged id.
  - Limits, stated plainly:
    - A **synthetic** waiver (`<p>#fix`, titled from `fix_brief[:80]`) only applies to the
      review it came from, because a fresh review rewrites the brief. It is kept in state
      for the record, but in practice it does not survive the REVIEWING resume.
    - A real finding that a fresh reviewer **rewords** or re-files does not match.

    The defence against both is to tell the reviewers (next bullet). Exact-key matching is
    the safety net, not the main mechanism.
- Reviewer brief: `personas.PLACEHOLDERS` gains `human_overrides`.
  `personas.render_human_overrides(state)` renders `state["human_overrides"]` as two lists:
  - "The human waived these findings; do not raise them again, even reworded:
    `<severity> <title> (<files>) — <reason>`";
  - "The human attested these ACs as met; do not assess them as unmet: `<ac> — <reason>`".

  An empty state renders `""`. `build_request` fills it (next to `pending_brief`, L457), and
  `personas/conformance_reviewer.md` and `personas/test_reviewer.md` include
  `{{human_overrides}}` after their previous-verdicts section. This mirrors how SPEC-0207's
  `review_carry` feeds earlier review results forward.
  - Removed findings join the same `gone` set as triager waivers, so `fixing`, `score`,
    `must` and `blocking` all ignore them. The result gains `human_waived: [ids]` and
    `attested: [ac texts]` (the applied ones).
- `decide_reviews(..., human_waived=(), attested=())` passes them through.
  `queue._route_reviews` reads `state.get("human_overrides")` and passes both. It appends a
  `note` `human override: waived N, attested M` when the triage applied any.
  `_accept_waivers` and `needs_triager` see the same overridden triage, so the triager never
  re-waives a human-waived finding.
- `state["human_overrides"]` is top-level and outside `last_review`, so `_begin_review` keeps
  it. It applies for the rest of the campaign. `queue._apply` does not copy it from the
  step's snapshot (it is not in the copied-keys list), so the on-disk value wins.

### Skills

- `skills/wt-conduct/SKILL.md`, new row `escalation_answered`. First read `wt escalations show
  <id>` and `wt campaign log <id> --tail 10`, then act on the kind:
  - `choose`: do what the chosen option says (resume with `--to` / `--note`, abandon,
    `wt queue prioritize`, raise the cap…).
  - `override`: `wt queue resume <cid> --to REVIEWING --note "human override <id>" --yes`.
    The fresh review then triages with the overrides.
  - `text`: act on it per the table, or escalate again if it is not actionable. SPEC-0232's
    re-park rule allows a new `wt queue escalate` on a `human_decision` park once its
    escalation is answered. It keeps `escalated_from` / `resume_to`, and the new record is a
    fresh `ESC-NNNN`.

  Record `wt campaign note <cid> "conductor: applied <id>: …"`, and give the human one line.
- `skills/wt-steward/SKILL.md`: when the human answers an open escalation for this project
  in the steward's session, run `wt escalations answer <id> … --by steward:<P> --yes` with the
  human's words, and never decide on their behalf. Point the human at `wt escalations list` for
  open ones.

## Alternatives considered

- **The answer command resumes the campaign itself.** Rejected. Choosing an option can mean
  abandon, reprioritize, raise a cap or resume to a specific state. The conductor's table
  already knows how to do each, and one actor resolving parks keeps the audit trail simple.
- **Human waivers in `last_review.waived` (the triager's list).** Rejected. `_begin_review`
  clears it on every review, ids are positional, and a fresh review would bring the finding
  straight back.
- **Re-route the stored verdicts without re-reviewing (resume straight to CLOSING when the
  overrides clear every blocker).** Rejected for now. A fresh review costs one pass but
  re-checks the branch as it is. Pre-computing "would pass" would duplicate `decide_reviews`
  in the answer path.
- **Answer by mailbox reply (`wt mailbox send --to conductor --kind answer`).** Rejected. It
  is free text with no validation of finding ids or ACs, and the record would never close.

## Acceptance criteria

- [x] `wt escalations answer` accepts exactly one form (`--choose`, `--text`, or `--waive` /
      `--attest` with `--reason`). Mixing forms, a missing reason, a non-open record and an
      unknown option are refused. Without `--yes` nothing is written.
- [x] `--choose` resolves a 1-based number or a case-insensitive label. The record becomes
      `answered` with `answer`, `answered_by` (default `human`) and `answered_at` set.
- [x] `--waive` accepts any finding id of the stored review (any severity, synthetic and merged
      included) and refuses other ids. `--attest` accepts a `#ac` finding id or an AC text
      covering exactly one distinct unmet AC, and refuses others (listing the matches when
      more than one). Overrides without a campaign are refused.
- [x] Apply takes `item_lock` then the escalations lock, re-checks `open` (a record withdrawn
      after the plan is refused, with nothing written), appends a `human_decision` ledger event
      and a `note` in the pinned summary format, stores override items in
      `state["human_overrides"]`, and leaves the campaign `PARKED` with its `park` unchanged.
- [x] Apply appends exactly one `escalation_answered` event (registered in `EVENT_TYPES`,
      with `escalation_id`, `kind`, `summary`). The ledger `human_decision` event projects no
      queue event.
- [x] `triage_reviews` drops human-waived findings of any severity, both by id and, for a
      fresh review, by (normalized title, files). Attested ACs, and the ACs of waived real
      findings, are neither unmet nor synthetic findings. A review whose only blockers are
      overridden routes to `pass`, including a waived real `major` that covers an unmet AC.
- [x] `state["human_overrides"]` survives `_begin_review`. `_route_reviews` notes applied
      overrides. `_accept_waivers` does not list a human-waived finding as waivable. The
      reviewer prompts receive `{{human_overrides}}` (empty when there are none).
- [x] After an answered `text`, `wt queue escalate <cid>` on the still-`human_decision` park
      succeeds with a new id (SPEC-0232 re-park rule).
- [x] The `wt-conduct` skill has the `escalation_answered` row (choose / override ⇒ resume to
      REVIEWING / text), and `wt-steward` has the relay rule.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_agentic_escalations.py` (from SPEC-0232), new cases:
    - `plan_answer` / `apply_answer` for `choose` by number and by label, and for `text`;
    - refusals: an answered record, a withdrawn record, mixed forms, an override without a
      reason, with a blank (whitespace) reason, or without a campaign, an unknown finding,
      an unmatched AC, an AC text covering two distinct ACs (both listed), `--choose 3` on a
      2-option record, an unmatched `--choose` label, `--choose` on an options-less record;
    - the apply race: plan an answer, withdraw the record, then apply ⇒ `ValueError`, the
      record is still `withdrawn`, and there is no ledger event;
    - the pinned note text for choose, for a mixed override with `--note`, and for a long
      text;
    - escalate again: after an answered `text`, `plan_escalate` / `apply_escalate` on the
      `human_decision` park creates `ESC-0002` and keeps `escalated_from`;
    - a campaign parked `human_decision` whose `last_review` holds a fixture verdict with a
      `major` finding, a `minor` finding and an unmet AC: `--waive conformance_reviewer#0 --attest
      conformance_reviewer#ac0 --reason r` stores both overrides, the ledger has
      `human_decision` + `note`, the state is still PARKED, and `events.read_since` has one
      `escalation_answered` (and no extra projected event after `events.sync`).
  - `tests/test_agentic_review_triage.py`:
    - `triage_reviews` with `human_waived` by id, and by title+files after renumbering (a
      fresh verdict with the finding at another index);
    - a waived `must_fix` no longer forces `fix`;
    - `attested` removes the unmet flag and the synthetic `#ac`;
    - only-overridden blockers ⇒ `outcome == "pass"`;
    - a real `major` with `ac` covering an unmet entry, human-waived ⇒ no `unmet`,
      `outcome == "pass"`;
    - a merged finding (two reviewers, same files and title) waived by its merged id;
    - a reworded title on re-review is **not** matched, which pins the documented limit.
  - `tests/test_agentic_queue.py`:
    - `_begin_review` keeps `human_overrides`;
    - a tick in REVIEWING with stored verdicts and overrides routes to CLOSING, with the
      `human override:` note;
    - `_accept_waivers` ignores a human-waived id.
  - `tests/test_agentic_escalations_cli.py`: `answer` dry-run vs `--yes`, `--json` schema,
    mixed-form usage error.
  - `tests/test_agentic_personas.py`: `render_human_overrides` for empty and populated
    state; `build_request` for `conformance_reviewer` and `test_reviewer` includes the
    waived title and the attested AC.
  - `tests/test_skills.py`: `wt-conduct` mentions `escalation_answered`, and `wt-steward`
    mentions `wt escalations answer`.
- **Manual verification:** in a throwaway `WT_DATA_DIR`, create a campaign-less escalation
  with two options (SPEC-0232), run `uv run wt escalations answer ESC-0001 --choose 2 --yes`,
  then check `uv run wt queue events --tail 2` shows `escalation_answered` and
  `uv run wt escalations show ESC-0001` shows the answer. Record the output in the spec.
- **Regression guard:** full `uv run pytest`. With no overrides, the existing SPEC-0203/0206
  triage tests are unchanged, because the new arguments default to empty.

### Test record (2026-10-25)

- `uv run pytest -q tests/test_agentic_review_triage.py tests/test_agentic_escalations.py
  tests/test_agentic_escalations_cli.py tests/test_agentic_queue.py
  tests/test_agentic_personas.py tests/test_skills.py`: all new SPEC-0235 cases pass.
- Manual, throwaway `WT_DATA_DIR`: `wt queue escalate --question "ship it?" --option yes
  --option no --yes`, then `wt escalations answer ESC-0001 --choose 2 --yes` printed
  `✓ ESC-0001 answered`; `wt queue events --tail 2` showed `escalation` then
  `escalation_answered` (`kind: choose`, `summary: chose 2 (no)`); `wt escalations show
  ESC-0001` printed `answered by human at …: chose 2 (no)`.
- Full gates: `uv run pytest -q -x` and `python3 tools/spec_lint.py` (see the run record).
- Review round 3 additions (all pass): CLI usage errors (exit 2 + message), override plan /
  `--json` / `show` output, nested lock order (enter/exit events), whole-`park` equality after
  each answer form, repeated `--waive` stored once, unreadable campaign state refused, waived `ac`
  reach refusal, and the review triager's input without a human-waived finding.

### As shipped

- `human_decision` had to be registered as a ledger event type
  (`schemas.EVENT_KEYS["human_decision"] = ("escalation", "kind")`); it was not known before.
- Re-park after an answered `text` needed no code change: SPEC-0232's `plan_escalate` only
  refuses while another record is *open*; a test pins it.
- An attestation whose text is exactly an unmet AC is accepted even when `_covers` also matches
  a longer/shorter variant (the plan lists the whole reach); only an inexact text covering
  several distinct ACs is refused. `triage_reviews` still uses `_covers`, so such a variant is
  cleared too, as listed in `reach`.
- A human waiver of a synthetic `#ac` finding also clears its AC (its `ac` joins the attested
  set), otherwise `unmet` would keep the outcome at `fix`.
- `wt escalations show` prints the answer; the plan output of `answer` lists `covers:` for each
  attestation. `_triage_input` (the review triager's input) also uses the overridden triage.
- Review round 2: `plan_answer` and `apply_answer` (re-checked under `item_lock`, before the
  record is written) refuse a campaign-bound record whose campaign is not PARKED
  `human_decision`. A deferred escalation (SPEC-0232 `park_requested`) is refused with "ESC-NNNN
  is pending: the campaign has not parked yet; answer once it parks", so the later park can never
  land on an already-answered record. Tests cover synthetic (`#ac`, `#fix`) and merged waive ids,
  the deferred refusal, and accumulating overrides.
- Review round 3: wrong answer-form combinations (mixed, none, `--reason` without an override,
  an override without a reason) raise `escalations.AnswerFormError` (a `ValueError`), which the CLI
  shows as a `UsageError` (exit 2, usage text); every other refusal stays a `ClickException`. A
  campaign-bound record whose campaign state is missing or unreadable is refused in `plan_answer`
  and in `apply_answer` (before any lock) instead of being answered without a ledger entry.
- Review round 3: waiving a real finding whose `ac` covers more than one distinct unmet AC (and
  is not exactly one of them) is refused, like `--attest`, through the shared `_unmet_reach`
  check. A later review that words an unmet AC differently can still be cleared by the substring
  rule; that residual reach of `_covers` is the documented limit, not closed here.
- If the final `escalation_answered` append fails, the answer stays recorded (the error is
  suppressed, as in `withdraw`).

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

None. `human_overrides` is absent on existing campaigns and is read as empty. After merge,
the conductor re-reads `/wt-conduct` (automatic with SPEC-0231). Escalations opened under
SPEC-0232 before this merge can be answered at once.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
