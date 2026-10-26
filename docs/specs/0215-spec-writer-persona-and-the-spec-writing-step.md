---
id: SPEC-0215
title: "spec_writer persona and the SPEC_WRITING step (write and revise)"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, queue, specs, personas]
source_idea: IDEA-032
depends_on: [SPEC-0191, SPEC-0208, SPEC-0209]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md),
promoted from `IDEA-032` (a headless spec-writer phase). It is part of the IDEA-063 chain
(SPEC-0209 → **0215** → 0214). After SPEC-0209, an idea campaign opens into SPEC_WRITING and
parks `spec_writer_unavailable`.

This spec adds a headless `spec_writer` persona that writes the campaign's spec in its
worktree. Given review findings, it revises the same file in the same state. wt validates
the result deterministically, regenerates the project's generated files, then commits or
rolls back. SPEC-0214 adds the review that follows.

Reworked on 2026-10-25 after the earlier, larger design kept diverging in review. One state
covers writing and revising, and there is no human-accept path.

## Goals / Non-goals

**Goals**
- The SPEC_WRITING step runs `spec_writer`:
  - with no spec yet, it writes `docs/specs/NNNN-<slug>.md` under a SPEC-0208 number;
  - with a spec and review findings (SPEC-0214), it revises that file.
- Bad output is rolled back and retried once, then parked `spec_write_failed`.
- The commit includes the project's generated files (for wt, `docs/LEDGER.md`), so the
  branch lints clean.

**Non-goals**
- `wt spec reconcile` for ideas whose spec exists only on a campaign branch (deferred to
  IDEA-056; reconcile writes nothing for them today).
- Reviewing and accepting (SPEC-0214); display (SPEC-0211, SPEC-0217).
- Epic-sized ideas, meaning ideas that need several independent specs. The writer answers
  `blocked`, and wt never writes epics. An idea with `:EPIC:` is a *child* of an existing
  epic and is written normally (see Prompt).
- Running a repo's own spec linter during validation. Validation checks a fixed subset (below).
  A spec that passes it but fails a repo-specific lint rule is caught by the project's
  gates at GATING, not by a writer retry.

## Decision

`plan_step` maps SPEC_WRITING to `Step("write_spec", persona="spec_writer")`, replacing
SPEC-0209's park. The persona runs in the worker. The worker validates the output, runs the
project's `merge.regenerate` commands, and either commits (the spec file plus those
generated paths) or rolls back. `_apply` records the result in its own `write_spec` branch.

Until SPEC-0214 lands, a successful write parks `spec_review_unavailable`, so nothing
unreviewed is ever implemented.

## Design

- **Reasons and constants:**
  - `PARK_REASONS` gains `spec_write_failed` and `spec_review_unavailable`.
    `spec_writer_unavailable` (SPEC-0209) stays in `PARK_REASONS`, and resuming a campaign
    parked with it now runs the writer in write mode;
  - `SPEC_WRITE_RETRY_MAX = 1`, so the second consecutive failure parks;
  - `schemas.EVENT_KEYS["spec_written"] = ("spec_id", "rel_path", "title", "mode")`;
  - `_apply.finish`'s copied-key whitelist (`queue.py` ≈625) gains `spec_write` and
    `spec_id`. `idea` is *not* copied, so a stale in-memory snapshot can't overwrite
    a concurrent resume's refresh.
- **Mode:** `write` when `state.spec.id` is None, else `revise`.
- **`plan_step` for SPEC_WRITING**, in this order:
  1. In revise mode with an empty `pending_brief` (nothing to revise), it parks
     `spec_review_unavailable` with resume_to SPEC_WRITING and does no run. This happens, for
     example, on a resume of that park without a `--note`.
  2. The cost cap: `_cap_blocks(state, profile, "spec_writer")`, using the profile's
     `spec_writer.max_budget_usd`, parks `cost_cap` with resume_to SPEC_WRITING (resume with
     `--raise-cap`).
  3. Otherwise `Step("write_spec", persona="spec_writer")`.
- **`_prepare_run`** (main thread):
  - for an `:EPIC:` idea, resolves the epic first (see Prompt);
  - records `untracked_before` only on a step's **first** attempt
    (`spec_write_retries == 0`) and reuses it on retries, so a leftover from a failed attempt
    can never become part of the baseline;
  - in write mode, gets the number from `spec_numbers.allocate(cfg, by="queue",
    campaign=<cid>, specs_dir=<worktree>/docs/specs)` (idempotent per campaign). The target
    path is `docs/specs/<NNNN>-<specs.slugify(idea heading)>.md`, fixed by wt;
  - records `state.spec_write = {mode, number, path, head_before: <worktree HEAD>,
    untracked_before: [<untracked files>]}`.
- **Persona:** `personas.PERSONAS["spec_writer"]`, with template `personas/spec_writer.md`,
  the verdict schema below, `read_only=False` and `invariant=SPEC_WRITER_INVARIANT` ("You are
  the spec writer of campaign {{campaign}} in its worktree. Edit only the spec file you are
  told to. Finish with the JSON verdict."). `build_request` uses `p.invariant or INVARIANT`.
  Builtin profile entries, all `bypassPermissions`:
  - `balanced`: sonnet, effort high, $1.50;
  - `lean`: sonnet, medium, $0.80;
  - `premium`: opus, high, $3.00.
- **Prompt:** the idea snapshot, `docs/specs/TEMPLATE.md` and `docs/specs/SCHEMA.md`, the
  spec id and path, and `pending_brief` (problems, review findings or human notes).
  - **Epic child:** when `state.idea.epic` (the idea's `:EPIC:`) is set, `_prepare_run` looks
    the epic up in the worktree's `docs/specs/`: the file whose `id` is the epic, plus the
    titles of the specs whose `parent` is that id. The prompt carries the epic's text and
    those titles, and requires `parent: <epic>`. This lookup is the first thing
    `_prepare_run` does, before `spec_numbers.allocate`. If no spec with that id and `kind:
    epic` exists, `_prepare_run` raises ValueError, and the tick (which already passes
    `_prepare_run` errors to `_apply`) parks the campaign through the generic
    error path instead of submitting a run. The detail is "`:EPIC: <id>` is not an
    epic spec", and no number is reserved.
  - **Write mode:** "create exactly `<path>`". The front matter has `status: proposed`,
    `source_idea`, `owner: user`, `parent: <epic>` when the idea has `:EPIC:`, and
    `kind: feature`.
  - **Revise mode:** "edit only `<rel_path>`; keep its id, `source_idea` and `status:
    proposed`; address every must_fix and major finding". When the findings include
    `suggested_splits`, it adds "narrow the spec to the first suggested split (or the one a
    human note names) and list the rest under Non-goals".
  - **Always:** no template placeholders, a real Test plan, and no other file edited
    (including generated files such as `docs/LEDGER.md`; wt regenerates them). "If the idea
    needs several independent specs, answer `blocked`." This rule is in the prompt only;
    there is no pre-check.
  - **Verdict:** `{status: written|blocked, spec_path, title, summary, blockers[]}`.
- **Validation** (`queue.validate_spec_change(worktree, spec_write, idea) -> problems`) runs
  on the writer's output, before any regeneration:
  1. The changed paths, `git diff --no-renames --name-status <head_before>` plus `git status
     --porcelain --untracked-files=all` minus `untracked_before`, are exactly the spec file:
     the added `path` in write mode, the modified `rel_path` in revise mode. A deletion is a
     problem, and so is any other changed path, generated files included. In revise mode,
     an unchanged file is a problem ("no change made"). The worktree is assumed clean at
     `head_before`. Edits to pre-existing untracked or git-ignored files are not detected;
     this is accepted.
  2. Front matter: it validates against `docs/specs/spec.schema.json` with `jsonschema`,
     normalized as the linter's `_normalize` does (dates become strings). That covers
     unknown keys, missing required keys such as `title` and `created`, and value types.
     Then `id ==
     SPEC-<NNNN>`, `status == proposed`, `source_idea ==` the idea, `owner == user`, `kind`
     absent or `feature`. `parent` must equal `state.idea.epic` when that is set (a missing
     parent is a problem), and must be absent otherwise.
  3. Body: the headings Context, Goals / Non-goals, Decision, Design, Acceptance criteria
     (with at least one `- [ ]`) and Test plan are present. There is no leftover template
     placeholder, using the same rule as `tools/spec_lint.py`'s `first_placeholder`. A copy
     goes into `wt.specmeta`, and the linter keeps its own so it stays standalone (the
     existing `specmeta` convention). A test asserts that the two give the same result on
     the same inputs.
  4. A run error, timeout, unparseable verdict, or a verdict `spec_path` (repo-relative,
     normalized) that isn't the spec file is also a problem.
- **Generated files** (defined here; SPEC-0214's accept reuses it): after a valid write, the
  worker runs every project `merge.regenerate` entry's `cmd` (SPEC-0191), in order, in the
  worktree with `sh -c` and SPEC-0191's timeout. The entries are expected to be cheap and
  idempotent. For wt that is `python3 tools/spec_lint.py --write-ledger` for `docs/LEDGER.md`.
  Of the declared `path`s, those that changed are staged with the spec. A command that exits
  non-zero, or any changed path that isn't declared, is a problem, and the change is rolled
  back. A project with no `regenerate` entries commits the spec alone.
- **Worker outcomes:**
  - **Interrupted** (stop requested): roll back; no retry is counted.
  - **Problems or `blocked`:** roll back (`git reset --hard <head_before>` plus `git clean
    -fd -- <untracked files the run created>`, so worktree setup output survives). The set of
    created files is recomputed at rollback time (current untracked minus
    `untracked_before`), so it includes files a failing regenerate command created.
  - **Valid:** `git add <spec file> <changed generated paths>`, then commit `wt(<cid>): spec
    <mode> — SPEC-<NNNN> <title>`. If the writer already committed the file, and
    regeneration changed nothing, there is no new commit.
- **`_apply`** has its own `kind == "write_spec"` branch. It records the run (cost, interrupt)
  as the persona branch does, then:
  - **Problems:**
    - ledger `finding` (major, gate `spec-write`);
    - `pending_brief` keeps everything it held (in revise mode, the review findings and
      `suggested_splits`) and gains `write_problems: <problems>`, which the prompt shows
      first ("your last attempt was rejected: …"). The review findings are never replaced
      by validation problems. `counters.spec_write_retries += 1`;
    - retry while `spec_write_retries <= SPEC_WRITE_RETRY_MAX`, else park
      `spec_write_failed`, resume_to SPEC_WRITING.
  - **`blocked`:** park `spec_write_failed`, detail "blocked: <blockers>", resume_to
    SPEC_WRITING.
  - **Committed:** before `finish`, under `store.item_lock`, `_apply` records the commit on
    the campaign's on-disk state, whatever state a human moved it to. That way the commit
    and the bookkeeping never disagree:
    - `state.spec = {id, rel_path, internal: True}`, `state.spec_id = id`;
    - in write mode, the idea gets `:SPEC:` and SPECCED (both idempotent), unless the
      on-disk campaign is already terminal (abandon restores the idea itself).
    It sets the **same** `spec` and `spec_id` values on the in-memory `state` as well, because
    `finish` copies whitelisted keys from the in-memory state onto disk, and a stale
    `{id: None}` would otherwise overwrite the record. Then, in the in-memory state, it sets
    `spec_write_retries = 0` and `pending_brief = None`, and appends ledger `spec_written {spec_id, rel_path, title, mode}`, and calls
    `finish`, which parks `spec_review_unavailable` (resume_to SPEC_WRITING) until SPEC-0214.
    As for every step, `finish` honors a human park requested during the run instead.
- **Generic errors:** `_apply`'s error table (`queue.py` ≈651) maps `write_spec` to reason
  `spec_write_failed` and resume_to SPEC_WRITING, with detail "write_spec step failed:
  <error>". This covers worker exceptions such as a GitError from rollback or commit,
  instead of the `open_failed`/IMPLEMENTING default. A rollback that itself fails (e.g. a
  GitError in `reset --hard`) is not retried: it parks `spec_write_failed` at once, with
  detail "rollback failed; clean the worktree, then resume". On that resume the worker first
  checks for no tracked changes and parks again if the worktree is still dirty.
- **Worker exceptions reach this table.** Today `tick` turns any exception escaping a pool
  worker into `StepResult(st["id"], Step("implement"), …)` (`queue.py` ≈941). That handler
  changes to keep the step it submitted (`StepResult(st["id"], step, …)`), so `write_spec`
  (and SPEC-0214's `spec_review`) exceptions are mapped by their own row, not as
  implementer failures.
- **Resuming `spec_write_failed`:** resets `spec_write_retries`, refreshes `state.idea` from
  the ideas file (keeping `state_before`), and adds `--note` to `pending_brief`.
- **Resuming `spec_review_unavailable`:** with a `--note`, the writer revises per the note,
  then parks again. Without one, plan_step rule 1 re-parks with no run. SPEC-0214 changes
  the no-note case to resume into SPEC_REVIEW.

## Alternatives considered

- **Separate write and revise states** (the earlier design) — two steps with the same
  persona, validation and rollback; one state with a mode is simpler.
- **Commit with `add -A`** — it would commit untracked setup output.
- **Let the writer run `--write-ledger`** — it ties the persona to one repo's tooling. The
  existing `merge.regenerate` config already names the generated files and their commands.

## Acceptance criteria

- [x] **Write mode** with a FakeRunner writer producing a valid spec:
  - the commit contains the spec file at the wt-chosen path and the regenerated
    `docs/LEDGER.md`, and nothing else;
  - `python3 tools/spec_lint.py` exits 0 in the worktree afterwards;
  - `state.spec` and `state.spec_id` are set, and the idea has `:SPEC:` and SPECCED;
  - `spec_written` (mode `write`) validates against `schemas.EVENT_KEYS`;
  - the campaign parks `spec_review_unavailable`;
  - `state.spec_write` and `state.spec_id` survive `finish`, while a
    concurrent change to the on-disk `idea` is not overwritten.
  When the writer committed the file itself and regeneration changes nothing, no new commit
  is made. With a project that has no `merge.regenerate`, only the spec file is committed.
- [x] **Revise mode** (fixture: a committed spec and findings in `pending_brief`):
  - the prompt carries the findings, and the file is modified and committed;
  - `pending_brief` is None afterwards;
  - an unchanged file is a problem;
  - with `suggested_splits` in the findings, the prompt contains the narrowing rule.
- [x] Invalid output is rolled back to `head_before`, retried once with the same number,
      then parks `spec_write_failed` (resume_to SPEC_WRITING). Parametrized cases:
  - a wrong number or path, a second file, an edited `docs/LEDGER.md`, or a deletion;
  - `status: accepted`, a missing `source_idea`, a missing `created`, an unknown
    front-matter key, a wrong `owner`, a wrong `parent`, or a missing `parent` for an
    `:EPIC:` idea;
  - a non-string `title` or a malformed `depends_on`;
  - a missing `## Test plan` or a template placeholder;
  - a change outside `docs/specs/`;
  - a runner error, a `spec_path` mismatch, or a failing regenerate command.
  An untracked setup file survives the rollback and is never committed. An untracked file
  created by a failing regenerate command is removed.
- [x] A `blocked` verdict parks `spec_write_failed` with the blockers. An interrupted run is
      rolled back without counting a retry.
- [x] **Generic error:** a GitError injected into the worker's commit parks
      `spec_write_failed` with resume_to SPEC_WRITING. A resume then writes and commits.
- [x] **Cost cap:** with `cap_usd` below `total_usd + spec_writer budget`, the campaign parks
      `cost_cap` (resume_to SPEC_WRITING) without a run. `wt queue resume --raise-cap`
      runs the writer. In revise mode with an empty brief, the `spec_review_unavailable`
      rule wins over the cap.
- [x] Resuming `spec_write_failed` with `--note` puts the note in the next prompt, refreshes
      the idea snapshot and resets the retry count.
- [x] **Epic child:** an `:EPIC:` that names no `kind: epic` spec parks `spec_write_failed`
      with no run.
- [x] Resuming `spec_review_unavailable` without a note re-parks it with no run. With a note,
      it runs the writer in revise mode.
- [x] When a human changed the campaign's state during a successful write (so `finish`
      skips the move), `state.spec`, `state.spec_id` and the idea's `:SPEC:` still match the
      commit. When the campaign is already terminal, the idea is not written.
- [x] The saved prompt in write mode contains the summary, the TEMPLATE text, the SCHEMA
      text, the id and path, and the blocked rule. For an `:EPIC:` idea, it also contains the
      epic's title, a sibling's id and the `parent` rule. No `{{…}}` remains. The request
      uses the writer invariant.
- [x] Two idea campaigns writing in the same tick (`slots=2`) get different numbers. Every
      builtin profile has a `spec_writer` entry. `wt.specmeta.first_placeholder` and the
      linter's copy agree on the lint test inputs, and the existing lint tests pass.
- [x] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_spec_writer.py`, using the `test_agentic_e2e`
  harness, SPEC-0209's idea helpers, and a FakeRunner helper `spec_written(number, mode=…,
  extra_fm=…, extra_files=…)`. At least one test per AC; the invalid cases are
  parametrized. The setup-survival case uses a project whose `worktrees.setup` creates an
  untracked file. The ledger case uses a fixture repo with `tools/spec_lint.py`, a
  `docs/LEDGER.md` and a `merge.regenerate` entry. The generic-error case monkeypatches
  the commit call.
- **Manual verification:** none beyond the automated e2e; the FakeRunner covers the persona
  contract.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest tests/test_agentic_spec_writer.py`: 65 passed (write, revise, 20 parametrized
  invalid-output cases (incl. timed-out run, unparseable verdict), rejected revise keeping the
  review findings, wrong epic parent, non-Git worker exception, leftover spec file, off-branch HEAD, epic child, blocked/interrupted, generic error and failed rollback,
  cost cap, resumes, human state change, prompt/request, two campaigns in one tick, profiles,
  placeholder/`_normalize` parity with the linter).
- Existing tests touched: `test_agentic_idea_campaign.py` (the SPEC-0209 park is replaced by the
  writer), `test_agentic_personas.py` (the writer template has no gates/previous runs).
- Full `uv run pytest` and `python3 tools/spec_lint.py` pass after `--write-ledger`.

### As shipped

- The logic lives in the new `src/wt/agentic/spec_write.py` (validation, regeneration,
  rollback, commit, prompt pieces); `queue.validate_spec_change` is `spec_write.validate_spec_change`
  reached through `SW`, and `queue.py` holds `_prepare_spec_write`, `_do_write_spec`,
  `_apply_spec_write`. Persona template: `personas/spec_writer.md`.
- The builtin profiles here are `balanced`, `frugal`, `thorough` (not `lean`/`premium`); the
  entries went to `balanced` (sonnet, high, $1.50), `frugal` (sonnet, medium, $0.80) and
  `thorough` (opus, high, $3.00).
- `jsonschema` is a dev-only dependency; when it (or the repo's `spec.schema.json`) is missing,
  the schema check is skipped and only the explicit front-matter checks apply.
- The dirty-worktree check on resume runs in `_prepare_spec_write` (before `run.start`), on every
  writer start: tracked changes park `spec_write_failed` with no run.
- A worker exception after the run still records the run's cost and `run.end`.
- `render_pending_brief` also renders `section` as a tag and `suggested_splits`; a brief's
  `write_problems` are shown by the prompt's top block, not by the brief.
- `docs/product/loop/operate.md` lists the new park reasons.
- Hardening after review: `_prepare_spec_write` removes an untracked file at the wt-chosen path
  and keeps it out of `untracked_before`; `_do_write_spec` rolls back on any exception (a
  non-Git one is also a `report.errors` entry); if HEAD is no longer on the campaign branch
  after the run, nothing is reset and the campaign parks `spec_write_failed` (rollback failed).
- Review loop 2: the HEAD-on-campaign-branch guard now sits inside `roll_back()` (queue.py), so
  the blocked, interrupted, invalid-output and generic-exception rollbacks all refuse to
  `reset --hard` when HEAD is detached or on another branch (`rollback_failed`, park
  `spec_write_failed`). Validation also requires `git merge-base --is-ancestor <head_before>
  HEAD` and folds `git diff --name-status <head_before> HEAD` into `changed_paths`, so a file
  the writer committed and then deleted, or an amended base commit, is a problem.

## Rollout / migration

Ships through the agentic queue after SPEC-0209. Written specs then park
`spec_review_unavailable` until SPEC-0214 lands.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
