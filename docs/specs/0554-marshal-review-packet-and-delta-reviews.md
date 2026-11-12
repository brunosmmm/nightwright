---
id: SPEC-0554
title: Marshal review packet (packet-loop-N.json) and delta reviews from loop 2
status: in-progress
owner: user
created: 2026-11-11
parent: SPEC-0552
depends_on: [SPEC-0553]
tags: [agentic, cost, review]
source_idea: IDEA-1297
---

## Context

Phase 2 of [SPEC-0552](./0552-leaner-agentic-campaign-loop.md). Each code reviewer run is
fresh and re-reads the whole change: `personas.build_context` renders `{{spec_text}}`,
`{{diff}}` (against base), `{{changed_files}}`, `{{gate_results}}`,
`{{previous_verdicts}}` and `{{pending_brief}}` into
`personas/{conformance,test,adversarial}_reviewer.md`, and the reviewer then reads the
repository again on its own. The evidence: the adversarial reviewer read 812M cached tokens
since Nov 5 (median 2.44M per run), and median campaigns spend 9 review runs to 4
implementer runs. From loop 2 the reviewers re-review everything, so new findings keep
appearing on untouched code and the loop does not converge.

## Goals / Non-goals

**Goals**
- One per-loop packet in the campaign dir that every reviewer reads first.
- Reviewers from loop 2 verify their own prior findings and review only the delta.
- Every prior finding carries a status (open / fixed / waived) with evidence.

**Non-goals**
- Routing or scoping findings (SPEC-0555); session reuse (SPEC-0556).
- Changing the gate or what reviewers judge in loop 1.

## Decision

After green gates, a **marshal** step builds `<campaign_dir>/packet-loop-N.json`. The
deterministic parts are computed by code; a marshal-tier persona (`review_marshal`) adds
only the AC → hunks/tests map. The marshal makes no decisions (no severities, no verdict).
Reviewers read the packet first and may open raw files from its pointers. From loop 2 each
reviewer returns a status for each of its own prior findings and may raise a new finding
only as a regression in the delta or as tied to an AC it missed in loop 1; code enforces
the rule.

## Design

### Packet (`nw.review_packet.v1`)

New module `src/nightwright/agentic/packet.py`, built in `queue._prepare_reviews` before the
reviewer fan-out (inside REVIEWING, not a new queue state — see Alternatives):

```json
{"schema": "nw.review_packet.v1", "campaign": "…", "spec_id": "SPEC-…", "loop": 2,
 "head": "<sha>", "base": "<sha>", "prev_head": "<sha of loop 1 review>",
 "acs": [{"id": "AC1", "text": "<verbatim checkbox text>"}],
 "diff": {"stat": [...], "path": "packet-loop-2.diff"},
 "delta": {"stat": [...], "path": "packet-loop-2.delta.diff"},
 "ac_map": [{"ac": "AC1", "hunks": ["src/x.py:120-148"], "tests": ["tests/t.py::test_a"],
             "coverage": "covered|partial|none"}],
 "gates": {"results": [...], "failing": [...], "flaky": [...]},
 "findings": [{"id": "<stable key>", "persona": "test_reviewer", "loop": 1,
               "severity": "major", "ac": "AC3", "title": "…", "status": "open|fixed|waived",
               "evidence": "…"}],
 "pointers": {"spec": "<rel path>", "diff": "…", "runs": "runs/", "gate_logs": [...]}}
```

- `acs`: parsed verbatim from the spec's `## Acceptance criteria` checkboxes at the
  campaign head (ids `AC1..ACn` in order, matching the ids reviewers already use in
  `ac_assessment`).
- `diff` / `delta`: `git diff base..head` and `git diff prev_head..head`; the full text is
  written next to the packet, the JSON holds the stat. `prev_head` is the head reviewed in the
  previous loop, recorded per loop in `state.review_heads`.
- `gates`: from the last gate payload (`personas.render_gate_results` inputs) and
  `flaky.py`.
- `findings`: every finding from earlier loops keyed by the stable finding key (spec id +
  normalized title + cited files, as `followups.py` computes it), status from the previous
  loop's reviewer `prior_status` entries, `waived` from human waivers
  (`queue.human_overrides_of`) and triager waivers.
- `ac_map`: from the `review_marshal` run. `personas.tests_touched` and `_split_diff`
  pre-compute candidate tests and hunks so the marshal only assigns them to ACs, and lists
  `coverage: none` explicitly ("no coverage found").

New persona `review_marshal` (`persona_defs/review_marshal.yaml`, role system, read-only,
tier marshal, template `personas/review_marshal.md`, output schema `review_marshal` in
`personas.py`). If the marshal run fails or exceeds its budget the packet is written with
`ac_map: null` and `ac_map_error`; reviewers still run. If the packet itself cannot be built,
reviewers get today's prompt and the run records `packet: false`.

### Reviewers

- Templates gain `{{packet_path}}` and `{{review_mode}}` (`full` in loop 1, `delta` from
  loop 2). In `delta` mode the prompt replaces `{{diff}}` with the delta and lists only the
  reviewer's own prior findings; the instructions: verify each, review the delta, raise a new
  finding only if it is a regression in the delta or tied to an AC missed in loop 1.
- `personas.reviewer_schema` (and the three reviewer schemas) gain
  `prior_status: [{id, status: fixed|open, evidence}]` and, per finding,
  `basis: initial|delta_regression|missed_ac`.
- `queue.triage_reviews` (enforcement, loop 2+): a new finding with `basis:
  delta_regression` must cite a file in the delta; `missed_ac` must name an AC id present in
  the packet; any other new finding is demoted to advisory (still recorded and filed by the
  existing follow-up path, never blocking) and counted as `demoted` in the review event.
  A prior finding a reviewer marks `fixed` stops counting; one it omits stays `open`.
- `ac_assessment` is unchanged; in delta mode an AC assessed `met` in an earlier loop stays
  met unless the delta touches its mapped hunks.

### Export

`campaign_export.RUN_COLUMNS` gains `loop` and `packet` (bool); `CAMPAIGN_COLUMNS` gains
`demoted_findings`.

Flag: `state.lean.packet` (pinned at open, SPEC-0553). Off → today's prompts and schemas.

### Design as built (where it differs from the above)

- **The marshal is its own REVIEWING sub-step.** `plan_step` returns
  `Step("review", persona="review_marshal")` while `packet.marshal_due(state)` (flag pinned,
  this loop's packet not attempted or its marshal pending), before the reviewer fan-out, like
  the review triager. `_prepare_reviews` (→ `_prepare_marshal`) builds the packet there (code,
  main thread) and prepares the marshal run; its harvest (`_apply_marshal`) writes the map and
  stays in REVIEWING (reason `review_packet`), and the next tick fans the reviewers out. So the
  tick thread never waits on an LLM, and adoption, cost, budgets and the export treat the
  marshal like any persona run (`adopt: review/[review_marshal]`). A packet that cannot be
  built (`state.packet.ok: false`), a marshal the cost cap blocks or one that cannot be
  prepared skips the marshal (`ac_map: null`, `ac_map_error`) and never parks. New ledger
  event `review_packet` (`loop`, `ok`, `mode`, `bytes`, `ac_map`, `none`, `ac_map_error`).
- **Marshal settings.** The builtin profiles do not list `review_marshal` (adding it would
  change every profile's sha for flag-off campaigns); `profiles.persona_settings` falls back to
  `packet.MARSHAL_SETTINGS` (haiku/medium, $0.25, read-only, `dontAsk`) and a profile may set
  it. With `lean.tiers` the model is the marshal tier's (`claude-haiku-5-5` on claude).
- **Marshal input / output.** The marshal gets the packet's ACs and code-computed candidates
  (`candidates.hunks` as `path:start-end` new-side ranges of `git diff --unified=0`,
  `candidates.tests` as `path::name` from `personas.tests_touched`). Its map is cleaned by code
  (`packet.clean_map`): only packet AC ids and candidate strings are kept, one entry per AC in
  order; an AC it left out (or with no hunk/test left) is `coverage: none`.
- **Diff range.** `diff` is `merge-base(base, HEAD)..head` (the three-dot diff the reviewers
  already get); `delta` is `prev_head..head`, `prev_head` from `state.review_heads[N-1]`
  (written by each packet build). The packet also carries `mode` (`full` / `delta`) and
  `candidates`.
- **Prior findings across loops.** Verdicts are cleared when REVIEWING is entered
  (`_begin_review`), so with the flag pinned `_begin_review` first copies the outgoing verdicts
  and triager waivers to `state.packet_prev`. Loop N's `findings` = loop N-1's packet findings,
  updated by `packet_prev`'s `prior_status` entries (a reviewer moves only its own findings),
  plus its new findings (`open`; a demoted one carries `advisory: true` and never counts
  again); human waivers (`human_overrides`) and the triager's waivers then set `waived`. A
  `fixed` finding raised again is `open` again.
- **Prompts.** The templates are unchanged (flag off must be byte-identical); with a packet,
  `build_request` appends a packet note (`packet.FULL_NOTE` / `DELTA_NOTE`) rendered with
  `{{packet_path}}`, `{{review_mode}}`, `{{packet_loop}}` and `{{prior_findings}}` (registered
  placeholders). Delta mode sets `diff` to the delta (≤ 1500 lines) and `base` to the previous
  loop's head (so the template's "Diff against" header is true) and lists only the reviewer's
  own open, non-advisory prior findings. `packet.extend_schema` adds `prior_status` and
  per-finding `basis` as optional keys, so a verdict without them still conforms. Applies to
  builtin and declared reviewers, not to the review triager.
- **Enforcement where verdicts are stored.** `_store_verdicts` (given `packet.enforcement`,
  delta mode only) stores the reviewer's still-open prior findings (`prior: <id>`, unless
  marked `fixed`) followed by its new findings; a new finding whose key equals a prior one is
  that prior one; an invalid basis marks it `demoted: true`. `triage_reviews` puts a demoted
  finding straight into `advisory` (never scored, never backs a `fix` verdict) and lists its id
  under `basis_demoted` (key present only when non-empty). `review_outcome` gains `demoted`
  (only with the flag pinned).
- **"Stays met" rule.** An AC assessed met earlier staying met unless the delta touches its
  hunks is a delta-mode prompt instruction, not code-enforced (`ac_assessment` handling is
  unchanged).
- **Export.** `runs.loop` / `runs.packet` come from `run.start` (`packet.run_marks`, written
  only with the flag pinned; `packet` true for a marshal or reviewer run with a built packet);
  `campaigns.demoted_findings` sums the last `review_outcome.demoted` of each loop.

## Alternatives considered

- **A new `PACKETING` queue state** — clearer on the timeline, but touches `STATES`,
  `ACTIVE_STATES`, `RESUMABLE_STATES`, adoption, the dashboard and slot logic; a pre-step of
  the review group fails soft to today's prompt, which a new state would have to park on.
- **Let the marshal LLM build the whole packet** — diff, gates and prior findings are
  deterministic; code is cheaper and cannot hallucinate them. The LLM only does the
  judgement-free mapping code cannot.
- **Diff-only reviews from loop 2 without prior-finding status** — loses the "fixed"
  evidence the collator and the implementer need.

## Acceptance criteria

- [x] With `lean.packet` on, every review loop writes `packet-loop-N.json` (valid against
      `nw.review_packet.v1`) with verbatim ACs, diff and delta stats and files, gates, prior
      findings with status and evidence, and pointers, before any reviewer starts.
- [x] `review_marshal` runs at tier marshal, outputs only the AC map (incl. `coverage:
      none` entries), and its failure leaves a packet with `ac_map: null` and the reviewers
      still running.
- [x] Reviewer prompts reference the packet; from loop 2 they are in `delta` mode with the
      delta diff and only that reviewer's own prior findings.
- [x] From loop 2, a new finding without a valid `delta_regression` or `missed_ac` basis is
      demoted to advisory and never blocks; a prior finding marked `fixed` stops counting.
- [x] Runs export `loop` and `packet`; campaigns export `demoted_findings`.
- [x] With the flag off, reviewer prompts and triage are byte-identical to today.
- [ ] The regression-set replay with the flag on catches the SPEC-0529 reflection oracle and
      the SPEC-0476 trailing-newline hang. *(Pending: before default-on, with the
      measurement; see As shipped.)*

## Test plan

- **Automated tests:**
  - `tests/test_agentic_packet.py` (new): packet from a fixture repo — AC parse, diff/delta,
    gates, prior findings and statuses across two loops; marshal failure → `ac_map: null`;
    builder failure → no packet, today's prompt.
  - `tests/test_agentic_review_triage.py` (extend): basis enforcement — demotion of an unsupported
    new finding, `missed_ac` with an unknown AC demoted, `fixed` prior finding not counted.
  - `tests/test_agentic_personas.py` (extend): template rendering in `full` / `delta` mode;
    schema accepts `prior_status` / `basis`; flag-off prompt snapshot unchanged.
  - FakeRunner queue test: two loops; packet files present per loop; loop-2 reviewers see
    only the delta.
- **Manual verification / regression set:** replay the review step of
  `SPEC-0529-20261110-f7b4` at the commit of the loop where seq 552/593 were raised, and of
  `SPEC-0476-20261109-ca46` at loop 1, with the flag on; both catches must reappear.
- **Measurement:** ≥10 Meta-Tools campaigns with `tiers + packet` on vs `tiers` only:
  median `cache_read_tokens` per review run (runs export, reviewer personas) and median
  `loops` per merged campaign.

## Rollout / migration

Flag off by default; on for Meta-Tools via `agentic.project_lean`, measure, default-on
after the regression set passes. Campaigns opened before the flag keep today's flow.

## Risks

- **Delta rule hides a real late bug** (the SPEC-0529 oracle was found late, by the
  adversarial reviewer, with no AC on the finding). Mitigated by `missed_ac` (the oracle
  ties to the request-auth AC) and by demotion rather than drop: the finding is still
  recorded and filed. The replay is the gate.
- **A wrong AC map steers reviewers.** Reviewers may open raw files; the map is a hint, and
  `coverage: none` entries draw attention rather than hide it.
- **Packet size.** Full diffs go to side files; the JSON stays small.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; measurement and replay recorded.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated.

## As shipped

Built 2026-11-11 behind `agentic.lean.packet` (default **off**, pinned per campaign at
creation by SPEC-0553's `state.lean`). Flag off: no marshal step, no packet files, no new
`state` keys (`packet`, `packet_prev`, `review_heads`), `run.start` and `review_outcome`
unchanged, every review persona's prompt, system block and schema object identical (asserted
for the conformance / test / adversarial reviewers and the review triager with `lean` absent,
all false and packet-false, with the packet code patched to raise), and `triage_reviews`
returns the same keys.

**Modules.** `agentic/packet.py` (flag helpers, `parse_acs`, `build`, `prior_findings`,
`validate` + `PACKET_SCHEMA`, marshal values / `clean_map` / `apply_marshal`, reviewer
`review_values` / notes / `extend_schema`, `enforcement` / `enforce`, `run_marks`),
`persona_defs/review_marshal.yaml` + `personas/review_marshal.md` (`REVIEW_MARSHAL_SCHEMA`),
`personas.build_request` (marshal values, packet note + schema), `profiles` (marshal default
settings, not required in a profile), queue (`plan_step` pre-step, `_prepare_marshal`,
`_apply_marshal`, `_begin_review` → `remember_verdicts`, `_store_verdicts` enforcement,
`triage_reviews` demotions, `review_outcome.demoted`, the new state keys kept across
`finish`), `schemas` (`review_packet` event), export columns, docs (capabilities, README).

**Tests.** `tests/test_agentic_packet.py` (new: AC parse, two-loop build from a fixture repo
with diff / delta / gates / candidates, prior-finding statuses incl. waivers and re-raise,
marshal map cleaning and failure, builder failure, validation, basis enforcement, helpers);
`tests/test_agentic_packet_queue.py` (new, FakeRunner: two loops with a packet per loop and the
marshal before the reviewers, loop-2 delta prompts with only own findings, a demoted finding
counted and advisory, a `fixed` prior not counted, an omitted prior still blocking, marshal
failure → `ac_map: null` and reviewers run, build failure → today's prompt and `packet:
false`, flag off → nothing); `tests/test_agentic_review_triage.py` (demotion, human waiver of
a demoted finding, `fixed` vs open priors, unknown `missed_ac`, result keys);
`tests/test_agentic_personas.py` (flag-off identity over all review personas, full / delta
rendering, pinned-but-unbuilt fallback, the marshal request); registry / CLI / tiers / export
fixtures updated for the new builtin persona and columns.

**Real run (marshal, `claude -p --model claude-haiku-5-5`).** Read-only replay on the merged
campaign `SPEC-0547-20261111-a2c5`: its `state.json` copied into a scratch data dir, the
worktree a scratch clone at the campaign head `45e38aa0` with base `0ab6ff54` (live campaign
dir untouched), `lean: {tiers, packet}`. `packet.build` took 0.9 s; the packet JSON was 14,936
bytes before the map and **18,798 bytes** after (9 ACs, 20 files in the diff stat), the full
diff side file 77,357 bytes. The marshal prompt was 13,289 chars; the run took 27.6 s and cost
**$0.0108**; tokens: input 8, cache creation 33,079, cache read 50,332, output 7,365 (3,533
thinking). It returned a valid map for all 9 ACs (2 covered, 7 partial, 0 none) using only
candidate hunks and tests; the packet validated.

**Pending (before default-on).** The regression-set replay (SPEC-0529 oracle at the loop of
seq 552/593 in delta mode, SPEC-0476 hang at loop 1) with real reviewers, and the measurement
(≥ 10 Meta-Tools campaigns, `tiers + packet` vs `tiers`: median `cache_read_tokens` per review
run, median `loops` per merged campaign) need live campaigns with the flag on: post-merge.
This spec stays `in-progress` until the replay is recorded.

## Open questions

- Should `review_heads` also let the gate triager see the delta? (Not needed for this
  phase.)
