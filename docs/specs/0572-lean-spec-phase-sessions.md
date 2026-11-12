---
id: SPEC-0572
title: Lean spec phase — resumable spec writer and delta spec re-review (`spec_sessions`)
status: accepted
owner: user
created: 2026-11-12
parent: SPEC-0552
depends_on: [SPEC-0556, SPEC-0558]
tags: [agentic, cost, spec-review]
source_idea: IDEA-1297
---

## Context

Child of [SPEC-0552](./0552-leaner-agentic-campaign-loop.md). The lean A/B of 2026-11-12
(five lean / control pairs of idea campaigns, `nw queue add --lean`, SPEC-0558) found the lean
cohort 15% cheaper overall, but its spec phase (`spec_writer` + `spec_reviewer`, with revise
rounds) cost **$4.82** against **$3.94** in control: no lean piece touches the spec phase, so it
only adds noise. `nw campaign export --since 2026-11-12 --table runs` shows the shape of that
phase in every campaign: a first writer run (cache read 238k–905k tokens, $0.22–$0.59), one
spec review (one review per campaign, [SPEC-0230](./0230-single-spec-review-round.md)), and in
two thirds of the campaigns a revise writer run that is a fresh session again (cache read
137k–642k, $0.19–$0.53): it re-reads the idea, the template, the schema and the repository
to apply a handful of findings.

[SPEC-0556](./0556-resumable-implementer-and-reviewer-sessions.md) built the session
machinery (`agentic/sessions.py`: registry, validity, continuation prompts, fallback, cost
netting of a resumed run's cumulative `total_cost_usd`) for the implementer and the code
reviewers only, and lists the spec personas as never resumed.

**Human approval (2026-11-12).** The human approved extending the lean loop to the spec phase:
a resumable spec writer, a delta spec re-review, behind a new lean flag that `--lean` also
turns on. This spec records that decision and is accepted on it.

## Goals / Non-goals

**Goals**
- A writer run after the first one in a campaign (the revise round, a retry after rejected
  problems, a resume after a park) resumes the writer's session and gets only what is new: the
  write rules of this attempt, the problems and the reviewer's findings.
- A second spec review of a changed spec in the same campaign resumes the reviewer's session
  with the spec diff since the reviewed text and its prior findings with a status, instead of
  re-reading from scratch.
- One new flag, `agentic.lean.spec_sessions`, pinned per campaign at open; `nw queue add
  --lean` turns it on.
- Flag off (and every campaign pinned before this merge) behaves exactly as today.

**Non-goals**
- Allowing more spec reviews per campaign: SPEC-0230's one review stays, so a delta
  re-review only happens where a second review already runs today.
- The standalone `nw spec review` (no campaign, no pinned flags).
- A `review_marshal` for specs (decided below), split campaigns, or the spec writer of an
  `amend` admission's first run (it has no session to resume).
- Editing the user's `~/.config/nightwright/config.yaml`.

## Decision

Reuse SPEC-0556's registry and fallback for two more managed personas, `spec_writer` and
`spec_reviewer`, under their own flag `spec_sessions`, which has no dependency: unlike
`sessions`, its fresh fallback is today's prompt, not the packet and findings history. A writer
run with a valid registry entry resumes with `SPEC_WRITER_CONTINUATION`; a spec review with a
valid entry and an earlier `spec_review` event in the campaign resumes with
`SPEC_REVIEWER_CONTINUATION` (diff + prior findings with a section status). Any resume failure
is run once more, in the same step, as today's fresh run (`session_fallback`), and never parks.

**No marshal for specs (decided).** A review marshal earns its cost on a code diff of many
files tied to ACs and tests; a spec diff is one file, usually under a hundred lines, and the
status a marshal would compute (is the finding's section touched?) is a deterministic check on
the two texts. A Haiku marshal run (~$0.01–0.02, plus latency and a failure mode) would cost
about what the delta saves; the status is computed in code instead.

## Design

- **Flag** (`tiers.py`). `LEAN_FLAGS` gains `spec_sessions` (so `--lean` and `--lean-flags
  spec_sessions` select it); no `LEAN_REQUIRES` entry. `PIN_WHEN_ON = ("spec_sessions",)`:
  `effective_lean` drops the key when it is off, so a campaign that does not ask for it pins the
  same `state.lean` dict (and `open` event, and export `lean` column) as before this merge.
  `config.DEFAULT_CONFIG` gets `agentic.lean.spec_sessions: false`.
- **Managed** (`sessions.py`). `SPEC_PERSONAS = ("spec_writer", "spec_reviewer")`;
  `spec_on(state)` = `state.lean.spec_sessions is True`; `managed` is true for them when
  `spec_on` and the campaign is not a split; `record` keeps their registry entries under the
  same rule (`_keeps`). The registry entry, its validity (`check`: backend, model, tier,
  worktree, campaign, resumable backend, context under `agentic.lean_session_max_tokens`) and
  the cost netting are SPEC-0556's, unchanged.
- **Shaping** (`sessions._shape_spec`, from `shape`, from `personas.build_request` after tiers
  and the effort ceiling picked model and effort). On a resumable backend every managed spec
  run persists its session; the fresh prompt is today's prompt, byte for byte.
  - Writer: with a valid entry, the prompt is `SPEC_WRITER_CONTINUATION` (spec id and path,
    the worktree, `write_problems`, `write_rules` of this attempt's mode, the rendered
    `pending_brief`; a note that a rejected attempt was rolled back) and `fresh_prompt` is
    today's prompt. The idea, the epic text, the template, the schema and the spec text are not
    sent again.
  - Reviewer: with a valid entry, `spec_review_delta` must also find the text of the campaign's
    last `spec_review` event: the newest of the last 50 commits of the spec file whose blob has
    the event's `spec_sha256`, and the stored review of that sha. The prompt is then
    `SPEC_REVIEWER_CONTINUATION`: the round, the prior verdict, `git diff <base> -- <spec>`, and
    each prior finding as `[severity] (section) title — <status>. Fix asked: …`, where status is
    `section changed` / `section unchanged` / `section not found` (`finding_status`, comparing
    the `##`/`###` sections of the two texts by heading). It must restate the findings that
    still apply, drop the fixed ones, and raise new ones only on changed text or a missed
    must_fix. No base found, no stored review, an empty diff, or a diff over 400 lines: the
    review runs fresh (and persists a new session).
- **Fallback** (`queue.py`). `_spec_resume_failed` is SPEC-0556's test (`resume_failed`, not an
  environment fault, not a usage stop, not stopping). `_do_write_spec` rolls the worktree back
  to the attempt's snapshot, then `_spec_session_fallback` runs `_rerun_locally(...,
  fallback=True)`: the failed run's `run.end` and cost are recorded (its entry dropped), a
  `session_fallback` event and the new run's `run.start` (`retry_of`, `session_fallback: true`)
  are appended, and today's fresh prompt runs under the new run id in the same step.
  `_do_spec_review` does the same when the failed resume left the worktree untouched (else the
  existing reset-and-retry path handles it). The fallback is never a resume, so it cannot loop;
  no counter (`spec_write_retries`, `spec_review_retries`) is spent.
- **Effort ceiling** (SPEC-0557). The session fields are added after `effort_ceiling.apply`, so
  a resumed spec run carries the same clamped effort (at most `high`) and `effort_clamped_from`
  as a fresh one.
- **Meta-Tools default-on.** The human's `agentic.project_lean.Meta-Tools` has `tiers`,
  `packet`, `collator` and `sessions` true. For spec-phase sessions to be on by default there,
  it needs one more line, `spec_sessions: true`; the human adds it after the merge (this change
  does not edit the config). Until then only `--lean` / `--lean-flags spec_sessions` campaigns
  use it.

## Alternatives considered

- **Require `sessions` (or `packet`)** — rejected: the spec phase uses neither the packet nor
  the collated findings, and its fallback needs nothing beyond today's prompt; a dependency
  would only stop an A/B of the spec phase alone.
- **Pin `spec_sessions: false` like the other flags** — rejected: every lean campaign's pinned
  dict, `open` event and export row would change shape for a flag it did not ask for.
- **A second spec review after every revise under the flag** — rejected: SPEC-0230 removed it on
  purpose (cost), and it would make the lean spec phase dearer, not cheaper.
- **A review marshal for spec deltas** — rejected (see Decision).
- **Fork the session per round** (`--fork-session`) — as in SPEC-0556: re-pays the history.

## Acceptance criteria

- [x] `spec_sessions` is a lean flag with no dependency; `--lean` and `--lean-flags
      spec_sessions` turn it on; when off it is absent from the pinned `state.lean`.
- [x] With the flag pinned on, a campaign's revise writer run resumes the first writer run's
      session (`--resume <id>` on claude) with a continuation that carries the reviewer's
      findings, the write rules and the problems, and not the idea, template or schema; the
      fresh prompt is kept as its fallback; the run is charged only its own cost.
- [x] With the flag pinned on, a second spec review of a changed spec in the same campaign
      resumes the reviewer's session with the spec diff since the reviewed text and each prior
      finding with its section status.
- [x] A resume that fails runs once more in the same step as today's fresh run, records
      `session_fallback`, spends no retry counter and never parks.
- [x] A resumed spec run keeps the effort ceiling (at most `high`, `effort_clamped_from`).
- [x] With the flag off, or on a campaign pinned before this merge (whatever the live config
      says after the open), the spec runs' request docs and prompts equal a flag-off campaign's,
      with no session key, no `prompt.fresh.md`, no `state.sessions` and no `resumed` mark.
- [x] On a non-resumable backend the flag changes nothing.
- [x] One real Claude call compares a resumed spec-review round 2 with a fresh one (tokens and
      cost recorded below).

## Test plan

- **Automated tests:** new `tests/test_agentic_spec_sessions.py` (`pytestmark =
  pytest.mark.integration`; `python3 tools/test_tiers.py --check` exits 0): the flag and its
  pin shape; managed only with the flag and not in a split; `finding_status`; FakeRunner idea
  campaigns (`test_agentic_spec_review_loop` helpers): the resumed revise writer (request doc,
  continuation vs `prompt.fresh.md`, `run.end.resumed`, registry, netted cost, accepted after
  revision with one review), the writer fallback (`session_fallback`, the retry runs today's
  prompt, no park, no retry counter), the round-2 reviewer after a human edit (diff, statuses,
  no schema text) and its fallback, the effort ceiling (`max` clamped to `high` on the resumed
  run), a non-resumable backend, and the flag-off identity test (flag off with `shape` patched
  to raise, and two pre-merge pins, with the live config flipped on after the open, compared to
  a flag-off campaign's spec runs). Updated: `tests/test_agentic_lean_optin.py` (`--lean` now
  pins five flags; an off `spec_sessions` is not pinned).
- **Manual verification:** one real `claude` call through `runner.ClaudeRunner`: a spec
  reviewer round 1 persisted, an edit of the spec, then round 2 resumed with the continuation
  vs round 2 fresh.
- **Regression guard:** `tests/test_agentic_sessions.py`, `test_agentic_tiers.py`,
  `test_agentic_lean_optin.py`, `test_agentic_spec_review_loop.py`,
  `test_agentic_spec_writer.py`, `test_agentic_effort_ceiling.py`, docs tests; one close gate
  on the box.

## Rollout / migration

Additive and off by default. Campaigns opened before the merge have no `spec_sessions` in
`state.lean` and keep today's spec phase. To turn it on for Meta-Tools, the human adds
`spec_sessions: true` under `agentic.project_lean.Meta-Tools` in
`~/.config/nightwright/config.yaml`; `--lean` campaigns get it from the merge on.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass; real call recorded.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.
- [ ] Measurement: the spec-phase cost of ≥ 5 lean campaigns with `spec_sessions` against
      the 2026-11-12 control ($3.94 / 5), recorded here (needs live campaigns after the merge).

## As shipped

Built 2026-11-12 behind `agentic.lean.spec_sessions` (default **off**). Status stays
**accepted** as the human set it; the post-merge measurement above is open.

**Modules.** `agentic/tiers.py` (`LEAN_FLAGS` + `spec_sessions`, `PIN_WHEN_ON`);
`agentic/sessions.py` (`SPEC_PERSONAS`, `spec_on`, `_keeps`, `_shape_spec`,
`SPEC_WRITER_CONTINUATION`, `SPEC_REVIEWER_CONTINUATION`, `spec_review_delta`,
`finding_status`); `agentic/queue.py` (`_spec_resume_failed`, `_spec_session_fallback`, the
fallback in `_do_write_spec` and `_do_spec_review`); `config.py`; `cli_queue.py` (`--lean`
help); docs (`docs/product/capabilities/agentic.md`, README).

**Tests.** `tests/test_agentic_spec_sessions.py` (13) pass; touched and regression files pass
locally. A mutation check (`spec_on` forced true) fails the flag-off identity test, the
managed test and the SPEC-0556 never-resumed test, so the identity test has teeth.

**Real call** (`claude` 2.1.322, `claude-haiku-5-5`, effort `medium`, through
`runner.ClaudeRunner` with the real `build_argv`, the in-campaign reviewer invariant and the
builtin `spec_reviewer` prompt and schema, on a scratch repo holding SPEC-0557 and
`SCHEMA.md`): round 1 persisted, then one acceptance criterion added and committed, then
round 2 two ways. The continuation was 4,550 characters against 19,687 for the fresh prompt.

| Run | Cost | Input | Cache create | Cache read | Output | Time |
|---|---|---|---|---|---|---|
| round 1 (persisted) | $0.0105 | 8 | 33,005 | 85,854 | 6,015 | 29.9 s |
| round 2 resumed (`--resume`, diff + 8 prior findings with status) | **$0.0052** (session total $0.0157 − $0.0105) | 4 | 11,785 | 71,662 | 4,288 | 18.3 s |
| round 2 fresh (today's full prompt) | $0.0073 | 6 | 19,184 | 66,905 | 5,674 | 27.8 s |

The resumed round cost 29% less than the fresh one (cache writes 11.8k vs 19.2k, output 4.3k
vs 5.7k, 1.5× faster), kept the session id and returned a conforming verdict. The fresh run
here already profits from the cache round 1 warmed minutes earlier; in a campaign the revise
writer runs minutes later as well, so the comparison is fair for the writer's case. Total
real-call spend about $0.023.

**Where the saving lands.** Per SPEC-0230 a campaign runs one spec review, so the delta
re-review only fires after a human edit of an approved spec whose accept failed; the expected
saving is the revise writer run (two thirds of campaigns, $0.19–$0.53 each in the A/B).

**Meta-Tools.** Default-on there needs `spec_sessions: true` added to
`agentic.project_lean.Meta-Tools` (one line, by the human, after the merge).

## Open questions

None.
