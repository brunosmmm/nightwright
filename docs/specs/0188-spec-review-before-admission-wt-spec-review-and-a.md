---
id: SPEC-0188
title: "Spec review before admission: wt spec review and a queue gate on an approving review"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, specs, queue]
source_idea: IDEA-033
depends_on: [SPEC-0176]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-033`, pulled forward on 2026-10-25).

A spec written by an agent is checked only by `tools/spec_lint.py`: frontmatter schema,
required sections, references, ledger. Nothing checks that its acceptance criteria are
testable and consistent, that it matches the explored idea, that it is sized for one campaign,
or that the template placeholders are gone — lint even accepted a bare scaffold marked
`accepted` on 2026-10-25. `accepted` is set by whoever wrote the spec, and `wt queue add`
admits any accepted spec. The plan's spec reviewer exists to close exactly this gap.

## Goals / Non-goals

**Goals**
- `wt spec review SPEC_ID [--json]`: runs a read-only `spec_reviewer` persona through the
  existing runner (headless, budget about $0.8) on the spec text, its source idea
  (`wt idea show --json` payload) and `docs/specs/SCHEMA.md`.
- Verdict schema: `{verdict: approve|revise|split, findings: [{severity:
  must_fix|major|minor|nit, section, title, detail}], suggested_splits: [{title, scope}]}`.
- The result is stored as a sidecar `docs/specs/reviews/<SPEC-ID>.json`: `{schema:
  "wt.spec.review.v1", spec_id, verdict, spec_sha256, reviewed_at, model, cost_usd, findings,
  suggested_splits}` (committed with the spec, so the review travels with it).
- `wt queue add` refuses unless the latest review is `approve` **and** its `spec_sha256`
  equals the current spec file's hash; `--force` overrides and the plan records
  `review: "forced"`.
- `tools/spec_lint.py` fails `accepted` / `in-progress` / `done` specs that still contain
  template placeholders: a line starting `<The chosen approach`, a bullet that is only `- …`,
  `<option>`, or a `# milestone: "M?` comment line.
- With `WT_AGENTIC_RUNNER=fake`, `wt spec review` uses the fake runner (tests).

**Non-goals**
- Sizing → profile selection, automatic splitting, a spec-writer phase (later waves).
- Re-reviewing automatically on every spec edit (the hash check forces a fresh review instead).

## Decision

Add a `spec_reviewer` persona (template + schema + profile defaults), a `review_spec(cfg,
spec_id)` function in a new `src/wt/agentic/spec_review.py` that builds the request, runs it
in a scratch run directory under `<data_dir>/agentic/spec-reviews/`, writes the sidecar, and a
`spec review` CLI subcommand; extend `queue.plan_add` with the review check; extend the linter
with placeholder detection.

## Design

- **Persona** `src/wt/agentic/personas/spec_reviewer.md`: check internal consistency (ACs vs
  Decision/Design), testability (every AC objectively checkable, Test plan covers each), fit to
  the source idea's Summary, size (more than ~10 ACs or several independent subsystems ⇒
  `split` with `suggested_splits`), leftover placeholders, missing Non-goals; `approve` only
  with no `must_fix` finding. Read-only (`--restricted`), cwd = the repo root.
- **Profiles:** `spec_reviewer` persona in each builtin profile (`balanced`: sonnet, effort
  high, $0.8; `thorough`: opus, $1.5; `frugal`: sonnet, effort medium, $0.4).
- **`review_spec`:** resolve spec path (`specs.resolve_spec_path`), read text and hash, load
  the source idea payload when `source_idea` is set, build a `RunRequest` without a campaign
  (env `WT_SPEC_ID`; no `WT_CAMPAIGN`), run, parse, write the sidecar atomically, return it.
  A run error or non-conforming verdict raises `ValueError` and writes nothing.
- **CLI:** `wt spec review SPEC_ID [--json]` (in `cli.py`'s `spec` group, `spec_id` completer)
  prints verdict, findings table and cost; `--json` emits the sidecar payload.
- **Queue gate:** `plan_add` loads `docs/specs/reviews/<id>.json` from the repo root; missing,
  not `approve`, or hash mismatch ⇒ `ValueError("SPEC-X has no approving review for its
  current text; run wt spec review SPEC-X (or --force)")`; `--force` sets `review: forced`.
- **Lint:** new rule in `tools/spec_lint.py`'s validate loop, reporting the first offending
  line number.

### As shipped (2026-10-25)

- The sidecar lives next to the resolved spec file (`<spec dir>/reviews/`), which is
  `docs/specs/reviews/` for internal specs.
- `plan_add` also reports `review: approved|forced`.
- An `approve` verdict that lists a `must_fix` finding is rejected as non-conforming.
- The persona carries its own invariant block (no campaign/worktree) via `Persona.invariant`.
- The lint rule ignores fenced code blocks and inline code spans, so a spec can name the
  placeholders it forbids.

## Alternatives considered

- **Review status in spec frontmatter** — rejected; the schema is strict and a review that
  edits the spec changes its hash. A sidecar keyed by hash is self-invalidating.
- **Human approval only** — rejected; the point is that agents write specs and the system
  checks them without stopping the human, who can still read every sidecar.

## Acceptance criteria

- [x] `wt spec review` with the fake runner writes a valid `wt.spec.review.v1` sidecar
      (verdict, hash of the current spec, cost) and prints/returns it; a malformed verdict
      writes nothing and errors.
- [x] `wt queue add` refuses a spec with no review, a non-approving review, or a review whose
      hash no longer matches; accepts with a matching approve; `--force` admits and records
      `review: forced`.
- [x] `tools/spec_lint.py` fails an accepted spec containing each placeholder form and passes
      the current corpus.
- [x] The persona template, schema and profile defaults exist; the request is read-only.
- [x] Completion, reference and agent-surface guards pass.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_spec_review.py` (fake-runner review, sidecar shape,
  malformed verdict, queue gate cases incl. hash mismatch and `--force`), lint placeholder cases
  in `tests/test_spec_lint*.py` (or a new file), persona/profile tests.
- **Manual verification:** one real `wt spec review` on an existing accepted spec; record the
  verdict and cost.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

- `uv run pytest -q`: full suite green (new `tests/test_agentic_spec_review.py`: fake-runner
  sidecar, malformed / non-approving-with-must_fix / errored / timed-out verdicts writing nothing,
  read-only request, queue gate cases incl. stale hash and `--force`, lint placeholder cases
  per status and form, corpus clean). Existing queue/e2e/dashboard tests now write an approving
  sidecar via `write_review`; `test_promote` strips placeholders from its accepted scaffold.
- `python3 tools/spec_lint.py` exits 0 (current corpus passes the new placeholder rule).
- Manual: real `uv run wt spec review SPEC-0187 --json` (sonnet, balanced profile) returned
  `revise` with 7 findings, cost $0.18, ~36 s; sidecar shape as specified (sidecar discarded,
  SPEC-0187 is out of scope here).

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Rollout / migration

Existing accepted specs need a review before they can be queued (or `--force`). Specs already
in a campaign are unaffected.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
