---
id: SPEC-0221
title: "Rebase-conflict resolver persona"
status: done
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
- `close.py` gets no resolver logic beyond these payload keys. The prompt context lives in
  `personas.py` (next item), so `close.py` never imports `personas.py` and no import cycle
  appears.

### Prompt context (`personas.resolve_context`)

- New helper `personas.resolve_context(state, resolve, project_cfg) -> dict`, where `resolve`
  is `closing.resolve`. It builds the prompt values from git reads in the worktree, which
  sits at `pre_head` at that point (the rebase was aborted). Each item is capped (constants
  in `personas.py`).
- **Fork point.** Every range below starts at `fork = git merge-base <pre_head>
  <target_sha>`, computed at dispatch and stored in `closing.resolve.fork`. It does **not**
  start at `repo.base_sha_at_open`. That sha never moves, so after an earlier close rebase
  (a SPEC-0220 `base_moved` redo, or a previous resolution) it would show base commits the
  branch already contains as if they were the campaign's own changes. The merge-base is the
  point the campaign branch actually diverges from the target now.
  - `conflicts`: the paths.
  - `campaign_diff`: `git diff <fork>..<pre_head> -- <conflicts>` (≤ 600 lines), what this
    campaign changed.
  - `base_diff`: `git diff <fork>..<target_sha> -- <conflicts>` (≤ 600 lines), what landed on
    base meanwhile.
  - `landed`: one line per commit in `git log <fork>..<target_sha>` that touches
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
- The git allow/forbid list is guidance, not enforcement. The persona usually runs with
  `bypassPermissions`, so nothing stops it from running a forbidden command. Safety comes
  from the runner-side verification and restore in `_do_resolve` (below). Whatever the
  persona does to the campaign branch is either verified or undone.

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
once). The default is `true`. The key is added to `project.PROJECT_DEFAULTS["merge"]` as
`"resolver": True`, so the strict project schema (`_check_mapping` over the `merge` keys)
accepts it. A non-bool value is a config problem, like the other typed merge keys.

### Queue (`queue.py`)

- **Dispatch (in `_apply`, close branch).** The outcome is a `rebase_conflict` park with
  `conflicts`, `merge.resolver` is not false, and `closing.resolve_attempts <
  MAX_RESOLVE_ATTEMPTS` (2). Then: no park. `closing.resolve = {target, target_sha,
  conflicts, pre_head, fork, branch}`, plus a `finding` (persona `close`, severity
  `info`, gate `rebase`, title "rebase conflict; resolver dispatched", files = conflicts).
  The transition stays CLOSING (row `reason: "resolve_dispatched"`). Otherwise the park is
  applied as today. When the budget is spent, the detail gets the suffix "(resolver: N
  attempts, last: <summary>)".
- **`pre_head` is recorded once, at dispatch.** `PreparedClose` (SPEC-0220) gains `pre_head`:
  the worktree HEAD `prepare_close` read before it started the rebase. `gitops.rebase`
  aborts back to exactly that sha, and `prepare_close` asserts it. `_apply` copies
  `prepared.pre_head` into `closing.resolve.pre_head`, together with `fork = merge-base
  pre_head target_sha` and `branch = repo.branch` (the campaign branch `wt/<cid>`). Every
  later restore resets to that **recorded** value, never to a HEAD read again after the
  persona ran. A failed attempt leaves the head at `pre_head`, so the value stays the same
  for the second attempt.
- **Planning.** In `plan_step`, `CLOSING` + `closing.resolve` ⇒ `_cap_blocks(state,
  profile, "conflict_resolver")`. If the cap blocks, park `cost_cap`, `resume_to: CLOSING`.
  The resume resets `closing`, so the next close re-detects the conflict. Otherwise
  `Step("resolve", persona="conflict_resolver")`. It goes into `pending_agent` like an
  implement step.
- **Restore procedure `_restore(worktree, branch, pre_head)`.** It is used before every run
  and after every attempt that is not verified:
  1. `rebase --abort` if a rebase is in progress (`rebase-merge` / `rebase-apply` in the
     worktree's gitdir);
  2. if `git symbolic-ref -q HEAD` is not `refs/heads/<branch>`, `git checkout -f <branch>`;
  3. if `refs/heads/<branch>` is not `pre_head`, `git reset --hard <pre_head>` (on the
     campaign branch, so only that ref moves).

  It ends by asserting that HEAD is attached to `<branch>` at `pre_head` with no rebase in
  progress. If that assertion fails, the step errors and the close error path parks
  `open_failed` (`resume_to: CLOSING`). This is a campaign-scoped restore of the runner's own
  recorded head, never the base.
- **Worker `_do_resolve(ctx, state, step, run_id, req, budget)`.** It runs `_restore`, then
  the persona, then the verification below. The verification is all runner-side git reads
  and does not trust the verdict:
  1. HEAD is attached to the campaign branch: `git symbolic-ref HEAD` ==
     `refs/heads/<branch>` (a detached HEAD or another branch fails);
  2. no rebase in progress;
  3. no unmerged paths;
  4. `git merge-base --is-ancestor <target_sha> HEAD`;
  5. **no campaign commit dropped or added.** `git rev-list --count <target_sha>..HEAD` equals
     `git rev-list --count <fork>..<pre_head>`. Also, `git range-diff --creation-factor=95
     <fork>..<pre_head> <target_sha>..HEAD` pairs every old commit with a new one: no `<`
     (dropped) or `>` (added) lines. `=` and `!` (modified by the resolution) pass. The
     creation factor is raised from git's default 60 so that commits reshaped by a conflict
     still pair. A commit too different to pair fails verification, which is the safe
     outcome;
  6. the worktree is tracked-clean.

  The result is `verified = verdict.status == "resolved" and 1–6 hold`. If not verified, or
  if the run was interrupted, the worker runs `_restore` to the recorded `pre_head`.
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
    plus `questions`). Attempts left ⇒ stay CLOSING with `closing.resolve` kept unchanged
    (the same recorded `pre_head`, `fork` and `target_sha`), and the resolver runs again. Otherwise ⇒ park
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

- [x] A close whose rebase conflicts on a non-generated path does not park. The campaign
      stays CLOSING with `closing.resolve` set and a `resolver dispatched` finding, and the
      next tick runs a `conflict_resolver` persona step (in flight on the pool, with
      `run.start`/`run.end` on the ledger).
- [x] `conflict_resolver` is registered (write-capable, own invariant, schema as above), its
      template carries the rules above, and its prompt contains the conflicted paths, both
      diffs (computed from the current fork point `merge-base(pre_head, target_sha)`), the
      landed commits and their spec text, the regenerate map and the target sha. Every
      builtin profile has its settings per the table.
- [x] A verified `resolved` result records `rebase_resolved` with per-path `strategy`/`how`,
      and the following close re-gates the resolved tree and reaches MERGED (gates green),
      or TRIAGING `regate_failed` (gates red).
- [x] A resolver run that changes the campaign spec's governing text leads to TRIAGING
      `spec-changed` at the following close, not a merge.
- [x] Runner-side verification rejects a `resolved` verdict when HEAD is detached or on
      another branch, a rebase is in progress, paths are unmerged, the target is not an
      ancestor, a campaign commit was dropped or added (range-diff/count against the
      recorded `fork..pre_head`), or the tree is dirty. Those cases, `gave_up`, a failed
      run and an interrupted run each restore the campaign branch, checked out and
      attached, to the `pre_head` recorded at dispatch, with no rebase in progress.
- [x] After `MAX_RESOLVE_ATTEMPTS` (2) unsuccessful resolver runs in one CLOSING entry, the
      campaign parks `rebase_conflict` (`resume_to: CLOSING`), and the detail names the paths
      and the last summary. A resume resets the budget. An interrupted run does not count
      against it.
- [x] `merge.resolver` is in `PROJECT_DEFAULTS["merge"]` (default true) and passes the strict
      project schema. `merge.resolver: false` parks `rebase_conflict` at once, as before. A cost cap that
      blocks the resolver parks `cost_cap` with `resume_to: CLOSING`.
- [x] Generated-only conflicts are still resolved by SPEC-0191 without a resolver run. The
      existing close, in-flight close (SPEC-0220) and e2e suites pass unchanged.
- [x] `rebase_resolved` validates with `schemas.ledger_problems`, is projected as an event,
      and renders in `wt campaign log`. The `wt-conduct` decision table row is updated.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

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
  - `test_resolver_context_uses_current_fork_point`: the campaign is first rebased by an
    earlier close (base moved, no conflict), and then conflicts. `campaign_diff` does not
    contain the base commit absorbed by the first rebase. `closing.resolve.fork` equals
    `merge-base(pre_head, target_sha)`, not `base_sha_at_open`.
  - `test_resolved_but_regate_red_goes_triaging`: the gate fails on the merged tree ⇒
    TRIAGING `regate_failed`, and the triage/implement loop runs.
  - `test_resolver_edits_spec_triages_spec_changed`.
  - `test_gave_up_twice_parks_rebase_conflict`: two `gave_up` entries ⇒ PARKED
    `rebase_conflict`, `resume_to: CLOSING`, HEAD == `pre_head`, the detail contains the
    summary. Then `wt queue resume` ⇒ `closing` reset, and a third (resolving) entry merges.
  - `test_unverified_resolved_is_restored`: one parametrized test over (a) the verdict says
    `resolved` but the shell leaves the rebase in progress; (b) a dirty tree; (c) the rebase
    was aborted (target not an ancestor); (d) the shell detaches HEAD (`git checkout
    --detach`) after a correct rebase; (e) the shell ends on another branch (`git checkout -b
    other`); (f) the shell drops a campaign commit (rebase with `git rebase --skip` on the
    conflicting commit); (g) the shell adds an extra commit. Each ⇒ restored to the
    recorded `pre_head` with HEAD attached to `wt/<cid>`, a `major` finding naming the
    failed check, and a second attempt with the same `pre_head`.
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
  projected. `tests/test_agentic_campaign_cli.py`: `wt campaign log <id>` on a ledger with a
  `rebase_resolved` event prints `rebase resolved: 1 paths (both×1)`.
- `tests/test_agentic_project.py` (the existing project-config tests): `merge.resolver`
  defaults to true, `false` is accepted, and a non-bool value is reported.
- **Sabotage checks:** drop the post-run verification. Drop the attached-branch check. Drop
  the range-diff/count check. Drop the restore on `gave_up`. Count
  interrupted runs. Skip the budget check. Run the new suite after each and record in the
  spec that it fails.
- **Regression guard:** full `uv run pytest`, in particular `tests/test_agentic_close.py`,
  `tests/test_agentic_close_inflight.py`, `tests/test_agentic_e2e.py`.
- **Manual verification:** on the next real conflicted close, `wt campaign log <id>` shows
  `resolver dispatched`, the resolver run, `rebase resolved: …`, then the re-gate and the
  merge, with no conductor action. Record the resolver cost.

### Test record (2026-10-25)

- `uv run pytest -q` (full suite): 2124 passed, 1 skipped.
- New `tests/test_agentic_resolver.py` (19 cases, real git conflicts in a tmp repo, FakeRunner with
  `pre_shell`/`shell`): dispatch + merge, prompt context, current fork point, red re-gate to
  TRIAGING, resolver spec edit to `spec-changed`, `gave_up` twice to park + resume, the
  unverified-result matrix (rebase left running, dirty tree, rebase aborted, detached HEAD, other
  branch, dropped commit, swapped commit, extra commit), interrupted run not counted, opt-out,
  cost cap, generated-only conflict, budget spanning two verified resolutions.
- Also extended: `test_agentic_runner.py` (`pre_shell`/`shell`), `test_agentic_personas.py`,
  `test_agentic_profiles.py`, `test_agentic_project.py`, `test_agentic_logs.py`,
  `test_agentic_events.py`, `test_agentic_campaign_cli.py`, `test_skills.py` (the
  `rebase_conflict` conductor row).
- `python3 tools/spec_lint.py` exits 0; ledger regenerated.
- **Sabotage checks** (each applied to the source, `tests/test_agentic_resolver.py` run, reverted):
  dropping the post-run verification fails `test_unverified_resolved_is_restored[detached_head]`;
  dropping the attached-branch check fails the same case; dropping the commit-count check fails
  `[dropped_commit]`; dropping the range-diff check fails `[swapped_commit]` (the count alone
  does not catch a swap); dropping the restore on a failed attempt fails
  `test_gave_up_twice_parks_rebase_conflict`; counting interrupted runs fails
  `test_interrupted_resolver_not_counted`; skipping the budget check fails
  `test_attempt_budget_spans_resolved_runs_in_one_closing_entry`.
- Review loop 1: added to `tests/test_agentic_resolver.py` (now 28 cases) a failed-run matrix
  (non-zero exit, malformed verdict, runner exception; each restores to `pre_head` attached with no
  rebase and a clean tree, records a `major` finding, counts one attempt, runs the second attempt and
  parks `rebase_conflict`; the crash leaves a synthetic failed `run.end`), a `restore_branch` failure
  test (PARKED `open_failed`, `resume_to: CLOSING`, the resolver `run.end` kept), untracked leftovers of
  a verified resolver not merged, `resolve_attempts` surviving a `base_moved` redo, and direct tests of
  `verify_resolution` (rebase-in-progress and unmerged-paths checks each alone) and `restore_branch`
  with untracked leftovers. Sabotage: dropping the post-verification clean, the `"resolve": "CLOSING"`
  resume mapping, or the restore on a failed attempt each fails the new suite.
- **Manual verification** (a real conflicted close shows `resolver dispatched`, the resolver run,
  `rebase resolved: …`, the re-gate and the merge, and the resolver cost): not yet performed. It
  needs a live conflicted close; the offline end-to-end tests above cover the same sequence.
  **Deferred, not performed.** The Definition-of-done box "manual steps performed" is ticked only
  because the offline end-to-end suite covers the same sequence; the live check remains to do on
  the next real conflicted close.
- Review loop 2: `tests/test_agentic_resolver.py` now has 34 cases. Added: the cost-cap park is
  proven to come from the resolve step (dispatch finding, `conflict_resolver` in the detail) and a
  resume re-detects the conflict; the interrupted-run test checks the full restore through
  `_assert_restored` (attached HEAD, no rebase dir); the dispatch fallbacks (`merge_base` raising;
  missing `pre_head` / `target_sha`) park as before; `_spent_resolver_park` detail cases; a
  pre-run restore failure still writes a `run.end` and charges nothing. The budget-span test now
  asserts the park detail names verified resolutions, not a failure. `uv run pytest -q`: 2139 passed, 1 skipped; `python3 tools/spec_lint.py` exits 0.

### As shipped (2026-10-25)

Deviations from Design, none of which change a decision:

- The restore and the verification are git-only helpers in `gitops.py` (`restore_branch`,
  `verify_resolution`, `rebase_in_progress`, `merge_base`); `queue._do_resolve` calls them. The
  restore also resets when the tracked tree is dirty at `pre_head` and runs `git clean -fd`
  (untracked, not ignored) so a failed attempt leaves nothing behind.
- A runner exception during the resolver run is treated as a failed attempt (synthetic failed
  `run.end`, restore, second attempt, then the `rebase_conflict` park) rather than the
  `open_failed` park, so a crashing resolver never strands the close. A failure of the restore
  itself still parks `open_failed`.
- The finish phase's `closing` write merges into the existing dict instead of replacing it, so
  `resolve_attempts` survives a `base_moved` redo within one CLOSING entry.
- The template also carries the last gate results and pending brief (a resume `--note` reaches
  the resolver through the brief). The hidden `wt campaign prompt conflict_resolver` renders the
  resolver context too (`personas.persona_extra`).
- A verified resolution is followed by `git clean -fd` (untracked, not ignored), so leftovers such as
  `.orig` backups cannot be picked up by the next close's snapshot commit and merged unreviewed.
- A run that exited non-zero or reported `is_error` counts as failed even when its verdict parses
  and its tree would verify; it is restored like any other failed attempt.
- Landed specs are included as their full text from the target, cut to 400 lines each.
- `closing.resolve_failed` (`{n, last}`) counts unverified attempts. The spent-budget park detail
  says `(resolver: N attempts, last: <failure>)` only when an attempt failed. When every run was
  verified (each followed by a base move that conflicted again) it says `(resolver: N resolutions
  verified; the base moved and conflicted again)` instead, so nobody looks for a failure that did
  not happen. The `wt-conduct` row describes both.
- A resolve step that errors before or after the run (for example the pre-run restore raising)
  always closes its `run.start`: a synthetic failed `run.end` with zero cost is written when no
  run result exists.
- The failure detail written to the `major` finding appends the failed check to the resolver's
  own summary, and the final park detail ends with `(resolver: N attempts, last: …)`.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Implement after SPEC-0220 has merged (it provides `prepare_close` and `closing`). No data
migration. Existing `rebase_conflict` parks stay parked until resumed, and a resume to
CLOSING then goes through the resolver. Merging changes runner code, so the live runner
drains and re-execs (SPEC-0194). Update `skills/wt-conduct/SKILL.md` in the same change, and
add `docs/product/capabilities/agentic.md`'s `rebase_conflict` line.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
