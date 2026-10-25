---
id: SPEC-0221
title: "Rebase-conflict resolver persona"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, close, personas, rebase, queue]
source_idea: IDEA-034
depends_on: [SPEC-0220, SPEC-0191, SPEC-0186]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from `IDEA-034` ("Rebase-conflict resolver persona").

The close step ([SPEC-0175](./0175-close-step-rebase-re-gate-ff-only-merge-or-pr.md)) rebases
the campaign branch onto the base before the re-gate. `gitops.rebase` aborts on a conflict,
returns the unmerged paths, and leaves the worktree at its pre-rebase HEAD.
[SPEC-0191](./0191-deterministic-rebase-resolver-for-generated-files.md) added a deterministic
resolver for declared generated files (`merge.regenerate`). Any other conflict parks the
campaign `rebase_conflict` (`resume_to: CLOSING`). The conductor's decision table then tells
it to hand-spawn a resolver sub-agent in the worktree, wait for its report, and resume to
`GATING`. The human (2026-10-25) said resolving rebase conflicts is the system's job, not
theirs. With several campaigns in flight and specs landing on `main` every hour, these parks
are routine. Each one costs a conductor wake-up, a hand-written brief and a manual resume.

[SPEC-0220](./0220-close-regate-off-main-thread.md) splits the close into an in-flight
worktree phase (`prepare_close`: guard, DoD, snapshot, rebase, re-gate) and a short
main-thread finish phase (merge, push, PR). It also adds the per-CLOSING-entry `closing`
state key. The resolver described here has to fit that split.

## Goals / Non-goals

**Goals**
- G1. A close rebase that conflicts on non-generated paths dispatches a `conflict_resolver`
  persona run inside the campaign, instead of parking. The run performs the rebase in the
  worktree, resolves the conflicts, keeps the intent of both sides, regenerates generated
  files and finishes the rebase.
- G2. A resolved rebase goes back through the normal close path: spec guard, DoD, rebase
  (now a no-op unless base moved again), full re-gate. A red re-gate goes to the existing
  triage/fix loop.
- G3. Bounded: at most `MAX_RESOLVE_ATTEMPTS = 2` resolver runs per entry into CLOSING,
  inside the campaign cost cap. A `gave_up` verdict, an unverifiable result or a spent
  budget parks `rebase_conflict` exactly as today. The branch is left as it was before the
  resolver ran.
- G4. Everything the resolver did is on the ledger: `run.start`/`run.end`, and a new
  `rebase_resolved` event listing every path and how it was resolved.
- G5. Runs off the runner's main thread, as an in-flight persona step (SPEC-0213/0220). It
  never touches the main checkout.

**Non-goals**
- Resolving conflicts outside the close rebase (e.g. at open time).
- A conformance review of the resolution (see Alternatives). The full re-gate is the check.
- Changing SPEC-0191's regenerate path. Generated-only conflicts never reach the resolver.
- Changing the `rebase_conflict` park reason or its `resume_to`.
- Splitting `queue watch` attention sections (that is IDEA-096 / SPEC-0222).

## Decision

No new campaign state. The resolver is a **step within CLOSING**. When the worktree phase's
rebase conflicts on non-generated paths, `_apply` checks the resolve budget. If budget is
left, the campaign stays CLOSING with `closing.resolve = {target, target_sha, conflicts,
pre_head}`, and a `finding` (info) says a resolver was dispatched. `plan_step` returns
`Step("resolve", persona="conflict_resolver")` for a CLOSING campaign whose `closing.resolve`
is set, and `Step("close")` otherwise. The resolve step is a normal persona step:
`_prepare_run` on the main thread, then `_do_resolve` on the pool, which runs the persona and
then verifies the result deterministically in the worker. At harvest, a verified `resolved`
clears `closing.resolve` and records `rebase_resolved`. The next plan returns `Step("close")`
and re-runs the whole worktree phase. Anything else restores the worktree to `pre_head`. If
budget is left the resolver runs again, otherwise the campaign parks `rebase_conflict`.

## Design

### Why a step within CLOSING, not a RESOLVING state

- A new state would have to be added to `STATES`, `ACTIVE_STATES`, `RESUMABLE_STATES`,
  `PHASES` (the dashboard stepper), the watchdog's `GATING`/`CLOSING` budget rules,
  `_resume_target` and the docs. A resolving campaign is closing in every sense that matters:
  it holds its slot, it may not be reviewed or implemented, and a park resumes to CLOSING.
- SPEC-0220's `closing` key already carries per-CLOSING-entry state and is reset on every
  entry into CLOSING and dropped on exit. Resolve state lives there too. So a resume, a
  WAITING → CLOSING or a TRIAGING round trip resets the resolve budget, with no new rules.
- The watchdog needs no change. An open resolver run counts as a running persona
  (`run_overdue` uses its `timeout_s`). `runner_busy` already treats CLOSING as busy.

### `close.py`

- `prepare_close` is unchanged in its decisions. For a non-generated conflict it still
  returns the `rebase_conflict` park. The park payload gains (additive) `conflicts` (the list
  of paths), `target` and `target_sha`, so `_apply` can dispatch without re-parsing the
  detail string. `close_campaign` (serial wrapper) behaves as today.
- New pure helper `resolve_context(state, target_sha, conflicts, project_cfg) -> dict`. It
  builds the prompt values from git reads in the worktree. Each item is capped (constants in
  `personas.py`):
  - `conflicts`: the paths.
  - `campaign_diff`: `git diff <base_sha_at_open>..HEAD -- <conflicts>` (≤ 600 lines), what
    this campaign changed.
  - `base_diff`: `git diff <base_sha_at_open>..<target_sha> -- <conflicts>` (≤ 600 lines),
    what landed on base meanwhile.
  - `landed`: one line per commit in `git log <base_sha_at_open>..<target_sha>` that touches
    a conflicted path (sha, subject), plus the governing specs of those commits: every
    `SPEC-NNNN` id in the subjects, and every `docs/specs/NNNN-*.md` file the range adds or
    modifies. The full text of each spec is included, with the frontmatter and the
    acceptance criteria and Design sections, ≤ 3 specs and ≤ 400 lines each. Choice: give
    the resolver the *other* side's intent explicitly. A conflict diff alone does not tell
    it why base changed.
  - `regenerate`: the `merge.regenerate` map rendered as `path: cmd` lines.
  - `rebase_target`: `target_sha`.

### Persona (`personas.py`, `personas/conflict_resolver.md`)

- `PERSONAS["conflict_resolver"]` uses `read_only=False` and its own invariant
  (`RESOLVER_INVARIANT`), because the shared `INVARIANT` says "do not commit" and a rebase
  must commit. The invariant:
  - Work only in `{{worktree}}`, on its current branch.
  - Allowed git: `rebase <target>`, `rebase --continue`, `rebase --abort`, `add`,
    `checkout --ours/--theirs -- <path>`, `status`, `diff`, `log`, `show`.
  - Forbidden: `push`, `merge`, `reset` (any form), `commit --amend`, `switch`/`checkout
    <branch>`, `branch -f`, `worktree`, `stash`, and anything in the main checkout.
  - Finish with the rebase complete (`git status` shows no rebase in progress) or aborted.
- The template states the job:
  1. `git rebase {{rebase_target}}`.
  2. For each conflicted hunk, keep both sides' intent. The campaign spec governs this
     branch's changes and the landed specs govern base's. When the two cannot both hold,
     prefer base for behaviour it specifies and adapt this campaign's change around it,
     then say so in `how`.
  3. Never hand-edit a path listed under `regenerate`. Take either side and run its command.
  4. The campaign spec `{{spec_rel_path}}` must stay identical to its accepted text. The
     SPEC-0193 rules are quoted, and the spec guard re-checks it at close. Any other
     `docs/specs/*.md` takes base's version unless this campaign changed it on purpose.
  5. Run the project gates if useful (gate commands are allowlisted, as for the implementer).
  6. Report `gave_up` rather than guess when the intents contradict.
- `CONFLICT_RESOLVER_SCHEMA`:
  `{status: enum[resolved, gave_up] (required), summary: string (required),
  resolutions: [{path (required), strategy: enum[both, ours, theirs, regenerated, rewritten]
  (required), how: string (required)}] (required), questions: [string]}`.
  `ours` means the campaign's side, `theirs` means base's side. The template defines both
  words, because git's own meaning of ours and theirs flips during a rebase.
- New placeholders: `rebase_target`, `conflicts`, `campaign_diff`, `base_diff`, `landed`,
  `regenerate`. `build_request` calls `resolve_context` when `persona == "conflict_resolver"`
  and adds `WT_REBASE_TARGET=<target_sha>` to the run env. The permission mode and allowlist
  follow the implementer's rules (the project's `agents.permission_mode` / `allowed_tools` +
  gate patterns).

### Profiles (`profiles.py`)

`conflict_resolver` persona settings are added to each builtin profile. Resolving a conflict
is judgment work on someone else's design, so it gets the profile's implementer-class model
with a smaller budget than the implementer:

| profile  | model  | effort | max_budget_usd |
|----------|--------|--------|----------------|
| frugal   | sonnet | medium | 1.0            |
| balanced | sonnet | high   | 2.0            |
| thorough | opus   | high   | 4.0            |

Project opt-out: `.wt/project.yaml` `merge.resolver: false` keeps today's behaviour (park at
once). The default is `true`.

### Queue (`queue.py`)

- **Dispatch (in `_apply`, close branch).** The outcome is a `rebase_conflict` park with
  `conflicts`, `merge.resolver` is not false, and `closing.resolve_attempts <
  MAX_RESOLVE_ATTEMPTS` (2). Then: no park. `closing.resolve = {target, target_sha,
  conflicts, pre_head: <worktree HEAD>}`, plus a `finding` (persona `close`, severity
  `info`, gate `rebase`, title "rebase conflict; resolver dispatched", files = conflicts).
  The transition stays CLOSING (row `reason: "resolve_dispatched"`). Otherwise the park is
  applied as today. When the budget is spent, the detail gets the suffix "(resolver: N
  attempts, last: <summary>)".
- **Planning.** In `plan_step`, `CLOSING` + `closing.resolve` ⇒ `_cap_blocks(state,
  profile, "conflict_resolver")`. If the cap blocks, park `cost_cap`, `resume_to: CLOSING`.
  The resume resets `closing`, so the next close re-detects the conflict. Otherwise
  `Step("resolve", persona="conflict_resolver")`. It goes into `pending_agent` like an
  implement step.
- **Worker `_do_resolve(ctx, state, step, run_id, req, budget)`.** Before the run, if a
  rebase is in progress, abort it. Then, if HEAD is not `pre_head`, restore it with
  `git reset --hard <pre_head>` in the worktree only. This is a campaign-scoped restore of
  the runner's own recorded head, never the base. Run the persona. Then verify, without
  trusting the verdict:
  1. no rebase in progress (`.git/rebase-merge` / `rebase-apply` absent for the worktree's
     gitdir);
  2. no unmerged paths;
  3. `git merge-base --is-ancestor <target_sha> HEAD`;
  4. the worktree is tracked-clean.

  The result is `verified = verdict.status == "resolved" and 1–4 hold`. If not verified, or
  if the run was interrupted, the worker restores (abort rebase, `reset --hard pre_head`).
  So a failed or discarded attempt leaves the branch byte-identical to before. The
  resolver's work exists only as commits on the campaign branch. Nothing outside the
  worktree and its branch is written. This keeps SPEC-0220's rule that a discardable
  result never has effects outside the campaign.
- **Harvest (`_apply`, `kind == "resolve"`).** It goes through the standard `finish()` path,
  with its fresh-state gate and `park_requested` handling. `closing.resolve_attempts += 1`
  unless the run was interrupted (an interrupted run keeps `closing.resolve` and is
  re-dispatched next tick).
  - **verified:** `closing.resolve = None`. Append `rebase_resolved` `{run_id, target_sha,
    conflicts, resolutions, summary, head}`, `repo.head = head`, stay CLOSING (row `reason:
    "rebase_resolved"`). The next plan is `Step("close")`. Its worktree phase runs the spec
    guard (a spec edit by the resolver ⇒ TRIAGING `spec-changed`, SPEC-0186), the DoD, a
    rebase onto the current base (a no-op unless base moved; a new conflict re-enters
    dispatch while budget remains) and the re-gate (red ⇒ TRIAGING `regate_failed`, so the
    triager briefs the implementer).
  - **gave_up / unverified / failed run:** a `finding` (severity `major`, gate `rebase`,
    title "conflict resolver did not resolve", detail = verdict summary or the failed check,
    plus `questions`). Attempts left ⇒ stay CLOSING with `closing.resolve` kept (a fresh
    `pre_head` equal to the restored head), and the resolver runs again. Otherwise ⇒ park
    `rebase_conflict`, `resume_to: CLOSING`, with the detail naming the paths and the last
    summary. Choice: a `gave_up` still uses the second attempt. A fresh run with the first
    run's `questions` in `previous_verdicts` often settles what the first could not, and the
    cost is bounded by the persona budget.
- **Whitelist.** `closing` is already in `finish()`'s copied keys (SPEC-0220). This spec
  adds `resolve` and `resolve_attempts` inside it, and nothing else.
- **Single shot (`wt queue tick`).** The resolve step runs in the agent phase of a tick and
  the close runs on the next tick, as for every other persona step.

### Ledger and events

- `schemas.EVENT_KEYS["rebase_resolved"] = ("run_id", "target_sha", "conflicts",
  "resolutions", "summary", "head")`. This is a new event type, so it is additive.
- `events.EVENT_TYPES["rebase_resolved"] = ("conflicts",)`, projected from the ledger. It is
  informational for the conductor, and it records that the system handled a conflict that
  used to park.
- `R.campaign_event` renders it as `rebase resolved: 3 paths (both×2, theirs×1)`.

### Conductor skill

`skills/wt-conduct/SKILL.md`, row `parked rebase_conflict`: such a park now means the
resolver persona already tried `MAX_RESOLVE_ATTEMPTS` times, or the project opted out. Read
the detail and the last `conflict resolver did not resolve` finding. If the questions are
answerable from project knowledge (ask the steward), note the answer and resume to `CLOSING`.
The resume resets the budget, and the resolver sees the note through `previous_verdicts` /
the ledger. Otherwise escalate. The hand-spawned resolver sub-agent is removed from the
table.

## Alternatives considered

- **New RESOLVING state.** Rejected for the reasons above. It touches every state table
  for no behaviour that CLOSING + `closing.resolve` does not give.
- **Run the resolver inside `prepare_close` (the worktree phase itself).** That would nest a
  persona run inside a close flight, hide its cost and run events from the normal persona
  path (`_prepare_run`, cap check, `run.start`), and turn a close into a 30-minute flight.
  Rejected: a separate persona step reuses all the existing run machinery.
- **Resume to GATING → REVIEWING after resolving (the current hand-run practice).**
  Rejected as the default. It costs a full review round per conflict, and the campaign's own
  change is unchanged in intent. The re-gate runs the full suite on the exact tree that will
  be merged, and `rebase_resolved` keeps every resolution reviewable afterwards. If evidence
  shows resolver-introduced defects slipping past the gates, a follow-up can route to
  REVIEWING.
- **Let the runner perform the rebase and hand the persona a conflicted tree.** Rejected.
  A rebase with several conflicting commits stops once per commit, so the persona must drive
  `--continue` anyway. Having it run the whole rebase keeps one actor in charge of the
  rebase.
- **Unlimited attempts until the cost cap.** Rejected. Two failures on the same conflict
  mean it needs a decision, not more tokens.

## Acceptance criteria

- [ ] A close whose rebase conflicts on a non-generated path does not park. The campaign
      stays CLOSING with `closing.resolve` set and a `resolver dispatched` finding, and the
      next tick runs a `conflict_resolver` persona step (in flight on the pool, with
      `run.start`/`run.end` on the ledger).
- [ ] `conflict_resolver` is registered (write-capable, own invariant, schema as above), its
      template carries the rules above, and its prompt contains the conflicted paths, both
      diffs, the landed commits and their spec text, the regenerate map and the target sha.
      Every builtin profile has its settings per the table.
- [ ] A verified `resolved` result records `rebase_resolved` with per-path `strategy`/`how`,
      and the following close re-gates the resolved tree and reaches MERGED (gates green),
      or TRIAGING `regate_failed` (gates red).
- [ ] A resolver run that changes the campaign spec's governing text leads to TRIAGING
      `spec-changed` at the following close, not a merge.
- [ ] `gave_up`, a `resolved` verdict that fails verification (rebase still in progress,
      unmerged paths, target not an ancestor, dirty tree), a failed run and an interrupted
      run each leave the worktree HEAD equal to `pre_head`, with no rebase in progress.
- [ ] After `MAX_RESOLVE_ATTEMPTS` (2) unsuccessful resolver runs in one CLOSING entry, the
      campaign parks `rebase_conflict` (`resume_to: CLOSING`), and the detail names the paths
      and the last summary. A resume resets the budget. An interrupted run does not count
      against it.
- [ ] `merge.resolver: false` parks `rebase_conflict` at once, as before. A cost cap that
      blocks the resolver parks `cost_cap` with `resume_to: CLOSING`.
- [ ] Generated-only conflicts are still resolved by SPEC-0191 without a resolver run. The
      existing close, in-flight close (SPEC-0220) and e2e suites pass unchanged.
- [ ] `rebase_resolved` validates with `schemas.ledger_problems`, is projected as an event,
      and renders in `wt campaign log`. The `wt-conduct` decision table row is updated.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** new `tests/test_agentic_resolver.py`, offline, using FakeRunner and a
  tmp git repo with a **real** conflict. The fixture builds `main` with `src/app.py`, opens a
  campaign whose implementer edits line 3. While the campaign is in REVIEWING, it commits a
  different edit to line 3 on `main`, plus a new `docs/specs/9001-other.md` and a subject
  `SPEC-9001: change app line 3`. Helpers come from `tests/test_agentic_e2e.py` /
  `tests/test_agentic_close_inflight.py`.
  - FakeRunner gains two optional entry keys: `pre_shell` (commands run in `req.cwd` with
    `req.env` before `edits`) and `shell` (commands run after `edits`). A resolver entry
    therefore reads `pre_shell: ["git rebase $WT_REBASE_TARGET || true"]`, `edits: [{path:
    src/app.py, write: <merged>}]`, `shell: ["git add -A && GIT_EDITOR=true git rebase
    --continue"]`. Tested in `tests/test_agentic_runner.py`.
  - `test_conflict_dispatches_resolver_then_merges`: the conflict ⇒ CLOSING +
    `closing.resolve` + finding. The resolver entry resolves it ⇒ `rebase_resolved`
    (strategy `both`), the next close re-gates and merges, and `main` contains the merged
    line.
  - `test_resolver_prompt_context`: the rendered prompt (via `wt campaign prompt` /
    `build_request`) contains `src/app.py`, both diff hunks, the `SPEC-9001` subject and the
    spec text, and `WT_REBASE_TARGET` is in the env.
  - `test_resolved_but_regate_red_goes_triaging`: the gate fails on the merged tree ⇒
    TRIAGING `regate_failed`, and the triage/implement loop runs.
  - `test_resolver_edits_spec_triages_spec_changed`.
  - `test_gave_up_twice_parks_rebase_conflict`: two `gave_up` entries ⇒ PARKED
    `rebase_conflict`, `resume_to: CLOSING`, HEAD == `pre_head`, the detail contains the
    summary. Then `wt queue resume` ⇒ `closing` reset, and a third (resolving) entry merges.
  - `test_unverified_resolved_is_restored`: one parametrized test over (a) the verdict says
    `resolved` but the shell leaves the rebase in progress; (b) a dirty tree; (c) the rebase
    was aborted (target not an ancestor). Each ⇒ restored to `pre_head`, `major` finding, a
    second attempt.
  - `test_interrupted_resolver_not_counted`: `stop` set during the run (FakeRunner
    `sleep_s`) ⇒ restored, `resolve_attempts` unchanged, re-dispatched.
  - `test_resolver_opt_out_parks` (`merge.resolver: false`) and
    `test_resolver_cost_cap_parks`.
  - `test_generated_only_conflict_no_resolver`: a `docs/LEDGER.md`-style regenerate conflict
    ⇒ no `conflict_resolver` run.
- `tests/test_agentic_personas.py`: registry entry, schema, template placeholders, invariant
  allow/forbid list, `build_request` permission mode and allowlist.
- `tests/test_agentic_profiles.py`: per-profile resolver settings.
- `tests/test_agentic_logs.py`: `rebase_resolved` validates. `tests/test_agentic_events.py`:
  projected.
- **Sabotage checks:** drop the post-run verification. Drop the restore on `gave_up`. Count
  interrupted runs. Skip the budget check. Run the new suite after each and record in the
  spec that it fails.
- **Regression guard:** full `uv run pytest`, in particular `tests/test_agentic_close.py`,
  `tests/test_agentic_close_inflight.py`, `tests/test_agentic_e2e.py`.
- **Manual verification:** on the next real conflicted close, `wt campaign log <id>` shows
  `resolver dispatched`, the resolver run, `rebase resolved: …`, then the re-gate and the
  merge, with no conductor action. Record the resolver cost.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Implement after SPEC-0220 has merged (it provides `prepare_close` and `closing`). No data
migration. Existing `rebase_conflict` parks stay parked until resumed, and a resume to
CLOSING then goes through the resolver. Merging changes runner code, so the live runner
drains and re-execs (SPEC-0194). Update `skills/wt-conduct/SKILL.md` in the same change, and
add `docs/product/capabilities/agentic.md`'s `rebase_conflict` line.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
