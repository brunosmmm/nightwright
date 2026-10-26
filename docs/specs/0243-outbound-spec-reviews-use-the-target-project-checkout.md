---
id: SPEC-0243
title: "Outbound spec reviews use the target project's checkout; admission recorded on the campaign"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, specs, review, outbound]
source_idea: IDEA-073
depends_on: [SPEC-0210, SPEC-0230]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md), promoted
from bug `IDEA-073` ("`wt spec review` resolves the repo via a hardcoded Meta-Tools project;
must use the spec's own project").

[SPEC-0210](./0210-spec-review-store-in-the-data-dir-any-repo-reviews.md) fixed half of the bug:
an internal spec, or any file given with `--path`, is now reviewed with its own git toplevel
as root. It deliberately kept the old rule for **outbound** specs (`<PROJ>-NNNN` files in the
data-dir outbox): `spec_review.spec_root` still returns the Meta-Tools checkout
(`P.project_config(cfg, "Meta-Tools")["_root"]`). The reviewer of a comfort-assistant spec
therefore runs with `cwd` and `add_dirs` in the wt repository. It cannot read the code the
spec changes, so it cannot judge whether the spec fits that codebase or how big it is
(SPEC-0219 sizing), and the recorded `repo_root` is wrong. `queue.plan_add` already resolves
an outbound spec's project correctly (`project or fm["target_project"] or
path.parent.name`); review should use the same rule. This spec refines that one SPEC-0210
rule; the rest of SPEC-0210 stands.

The same review left a nit: the admission that `plan_add` computes (`review`: `approved`,
`after_revision` or `forced`) is in the plan but not in the campaign state or the `open`
event. SPEC-0219 already records `sizing.review_sha256`, so only the admission kind is
missing.

## Goals / Non-goals

**Goals**
- An outbound spec is reviewed with its target project's checkout as root (`cwd`,
  `add_dirs`, `repo_root`, the `reviewing` line).
- Reviews of outbound specs recorded under the old root keep counting for admission.
- The campaign state and its `open` event record how the spec was admitted.

**Non-goals**
- A `--project` override on `wt spec review` (the front matter and the outbox directory name
  are authoritative; `wt queue add --project` keeps its own override).
- Reviewing the exported portable copy (`target_spec_path`) instead of the outbox file: the
  outbox file stays the reviewed and admitted text, as today.
- Changes for internal specs or `--path` reviews.

## Decision

`spec_root` resolves an outbox spec's project with a new helper shared with `plan_add`, and
uses that project's root. The SCHEMA.md given to the reviewer is the target's when it has
one, else wt's own. `list_reviews` skips its same-repository filter for outbox specs, whose
ids are unique across projects. `apply_add` stores `state.admission`, and
`store.create_campaign` copies it to the `open` event.

## Design

- **`specs.outbound_project(path, fm) -> str`** (new, `src/wt/specs.py`): returns
  `fm.get("target_project") or Path(path).parent.name`. `queue.plan_add` uses it for its
  default (`project = project or SP.outbound_project(path, fm)`), with no behaviour change.
- **`spec_review.spec_root(cfg, spec_path)`**, outbox branch: read the front matter
  (`split_frontmatter`), `project = SP.outbound_project(path, fm)`, and return
  `Path(P.project_config(cfg, project)["_root"])`. When the project cannot be loaded
  (ValueError or KeyError from `project_config`), fall back to the Meta-Tools root as today,
  then to `_fallback_root`. The fallback is the old behaviour, so an unregistered target
  never breaks a review.
- **`spec_review.review_context`** gains `schema_root=None`. The schema is read from
  `<schema_root or root>/docs/specs/SCHEMA.md`. `run_spec_review` passes `schema_root` only
  for an outbox spec: the target root when that file exists there, else the Meta-Tools root
  (wt's SCHEMA.md, which defines the portable format). `spec_rel_path` for an outbox file,
  which is outside the root, stays the absolute path, as `review_context` already does.
- **`run_spec_review`** is otherwise unchanged: `cwd=root`, `add_dirs=(root,)`, and
  `review_payload(..., root=root)` records the target root as `repo_root`.
- **`list_reviews(cfg, spec_id, spec_path)`**: when `spec_path` is inside
  `SP.outbox_dir(cfg)`, the same-repository filter is skipped and every stored review of the
  id counts. Without this, a `revise` recorded with the Meta-Tools `repo_root` would be
  filtered out, and `check_admissible` would report "no spec review" for a spec that was
  reviewed and then edited.
- **Admission on the campaign** (`src/wt/agentic/queue.py`, `store.py`): `apply_add` sets
  `state["admission"] = plan["review"]` (`approved`, `after_revision` or `forced`).
  `store.create_campaign` adds `"admission": state.get("admission")` to the `open` event
  payload, next to the SPEC-0219 sizing keys. It is additive and optional:
  `schemas.EVENT_KEYS["open"]` is unchanged, and older states without the key read as
  None. Idea campaigns (`apply_add_idea`) do not set it.

## Alternatives considered

- **Keep the Meta-Tools root and add the target as an extra `add_dirs` entry** — the
  reviewer would still start in the wrong repository and record the wrong `repo_root`.
- **Review the exported portable copy in the target repo** — it may not exist yet (export is
  after acceptance), and admission checks the outbox file's sha.
- **Re-key outbound reviews by project** — ids are already unique in the outbox; a migration
  buys nothing.

## Acceptance criteria

- [ ] `wt spec review PROJ-0001` for an outbox spec whose `target_project` is a registered
      project (scratch git repo) runs the FakeRunner reviewer with `cwd` and `add_dirs`
      equal to that project's root, records it as `repo_root`, and prints it in the
      `reviewing` line.
- [ ] With no `target_project` key, the outbox directory name picks the project. With an
      unregistered project, the review falls back to the Meta-Tools root, as before.
- [ ] The prompt carries the target's `docs/specs/SCHEMA.md` when it exists, else wt's.
- [ ] An outbound spec with a stored `revise` whose `repo_root` is the Meta-Tools root, edited
      afterwards, is admitted by `wt queue add` as `after_revision`; a stored `approve` of the
      current text with that legacy root is admitted as `approved`.
- [ ] `plan_add` picks the same default project as before for outbound specs (existing
      outbound queue tests pass unchanged).
- [ ] After `wt queue add`, `state.json` has `admission` (`approved`, `after_revision` or
      `forced` for `--force`), and the campaign's `open` event carries the same value.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_spec_review_outbound_root.py`, one test per AC. Fixtures:
  a scratch target repo registered as a project (with and without its own SCHEMA.md), an
  outbox spec with and without `target_project`, store entries written with the Meta-Tools
  `repo_root`, and a FakeRunner `spec_reviewer` entry whose recorded request is inspected for
  `cwd`, `add_dirs` and the prompt text. Admission tests use `Q.plan_add` / `Q.apply_add`
  and read the ledger's `open` event.
- **Regression guard:** full `uv run pytest`, in particular `tests/test_spec_review_store.py`
  (the SPEC-0210 outbound test is updated to the new root and documented as such) and the
  outbound cases in `tests/test_agentic_queue.py`.

## Rollout / migration

None. Old outbound reviews keep admitting through the filter exception; new ones record the
target root.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
