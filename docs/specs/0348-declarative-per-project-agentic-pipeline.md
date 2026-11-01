---
id: SPEC-0348
title: "Declarative per-project agentic pipeline: steps, personas and routing as data, with a lean fast-track lane"
status: accepted
owner: user
created: 2026-11-01
updated: 2026-11-01
milestone: "M9: agentic workflow"
kind: epic
tags: [agentic, queue, pipeline, personas, profiles, project-config]
source_idea: IDEA-616
depends_on: [SPEC-0201, SPEC-0219, SPEC-0237, SPEC-0328]
---

## Context

Promoted from `IDEA-616`. Related: `IDEA-617` (lean fast profile, delivered as this epic's
last child), `IDEA-090` (pluggable reviewer personas, absorbed by Stage A below).

The agentic queue ([SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)) runs
every campaign through one fixed shape. What a campaign *does* is code; only how expensively it
does it is data:

- **The step graph is code.** `agentic/queue.py` (about 7,200 lines) holds `STATES` as a
  hardcoded tuple (`QUEUED, WAITING, OPENING, SPEC_WRITING, SPEC_REVIEW, IMPLEMENTING, GATING,
  TRIAGING, REVIEWING, CLOSING, PARKED, MERGED, HANDED_OFF, ABANDONED`). `plan_step` dispatches
  with an if-chain on the state, and the transitions (`finish("IMPLEMENTING", …)` after a review
  fix, waive → `CLOSING`, `loop_max` exhaustion → park) are scattered through the module.
  Special campaign kinds (merge SPEC-0233, split SPEC-0290, `--docs-only` SPEC-0234) are
  further special cases in the same chain.
- **Personas are code.** `agentic/personas.py` binds name → template `.md`, output schema,
  `read_only` and invariant in the `PERSONAS` dict; `REVIEWER_PERSONAS` and `ADOPT_REGISTRY`
  are hardcoded tables and `PLACEHOLDERS` is a fixed tuple. A project that wants a security or
  accessibility reviewer must patch nightwright.
- **Profiles are data** ([SPEC-0237](./0237-workflow-profiles-as-yaml-files-builtin-user-project.md)):
  `agentic/profile_defs/*.yaml`, layered builtin/user/project, set model/effort/budget/permission
  per persona, the `reviewers` list ([SPEC-0201](./0201-reviewer-fan-out.md)), `loop_max`,
  review weights, the triager flag and slots. `PROFILE_MATRIX` (size x tone,
  [SPEC-0219](./0219-spec-sizing-and-tone-pick-the-campaign-profile.md)) picks one, `--profile`
  overrides.
- **Gates are data**, per project in `.nw/project.yaml`, tiered `when: loop | close | always`
  ([SPEC-0328](./0328-tiered-gates-affected-test-selection.md)).

So a project can tune cost but cannot change shape: a one-line docs fix pays for the same
spec → implement → gate → three reviewers → triage → close as a new subsystem, and adding a
reviewer means editing `personas.py`. Making the shape declarative is too large for one spec:
it spans the persona registry, a behavior-preserving refactor of the queue's hottest module,
selection/routing, and every consumer of state names (events, conductor, TUI, steward). Hence
an epic.

**IDEA-090 / SPEC-0296 discrepancy.** `IDEA-090` carries `:SPEC: SPEC-0296`, but no
`docs/specs/0296-*.md` exists on `main`. SPEC-0296 ("Pluggable reviewer personas, defined in the
project repo: declarations and registry", slice 1 of 4, marked `done`) exists only on the
unmerged campaign branch `wt/IDEA-090-20261030-236a` (6 commits, pre-rename `src/wt/` paths). This
epic does not depend on or supersede it (a `supersedes` link cannot resolve to a file absent
from `main`); Stage A re-specifies that scope against current `main`, and may reuse that branch's
design notes (strict validation, main-checkout-only reads, `<name>_reviewer` naming) as prior
art. Reconciling the idea's dangling `:SPEC:` and the stranded branch is a separate cleanup.

## Goals / Non-goals

**Goals**
- G1. Personas are data: builtin personas load from YAML + template + schema, and a user or
  project can add personas in a layered builtin/user/project registry, limited to roles that
  reuse an existing output schema.
- G2. The campaign step graph is data: a pipeline (`pipelines/*.yaml`) names its steps, their
  kinds and their `on_pass`/`on_fail` edges. The builtin `default` pipeline reproduces today's
  behavior exactly.
- G3. A campaign selects a pipeline (spec field, classifier route, `--pipeline` override);
  profiles become parameters of a pipeline instead of the only knob.
- G4. Events, conductor, TUI and steward show and reason about the pipeline and step a
  campaign is on.
- G5. A builtin `fast` pipeline plus a `lean` profile give small changes a cheap lane.
- G6. Every pipeline, builtin or project-defined, keeps the non-negotiable invariants below,
  enforced by validation, not by convention.

**Non-goals**
- **A pipeline with no spec at all.** Every campaign stays governed by a spec (AGENTS.md:
  "no behavior/feature change without a governing spec"). A spec-less lane needs an explicit
  contract amendment to AGENTS.md first; it is out of scope here.
- Custom personas with new output schemas or new roles (a spec writer, a conflict resolver, a
  new triage kind). Roles are limited to implementer-like, reviewer-like and triager-like.
- Custom step kinds or user code in pipelines. Step kinds are a closed set implemented in
  nightwright; pipelines only compose them.
- Changing close semantics, parking, the cost cap, WAITING/slots, adoption, or the
  `nw.queue.v1` state vocabulary (see Architecture: state names).
- Converting merge (SPEC-0233), split (SPEC-0290) and `--docs-only` (SPEC-0234) campaigns into
  pipelines. They stay as today's pre-graph special cases; folding them in is a follow-up idea
  once the graph exists.
- Changing the Definition of Done in AGENTS.md. No pipeline can relax it.

## Decision

Introduce two data layers, delivered in two independent lanes that meet at selection:

- **Stage A — personas as data.** A persona registry loaded from layered YAML definitions
  (builtin `agentic/persona_defs/`, user config dir, project `.nw/personas/` read from the
  project's main checkout only). Builtins move to YAML first with no behavior change; then
  projects declare role-limited custom personas; then the queue consumes them in reviews.
- **Stage B — pipelines as data.** A pipeline schema and validator plus a golden transition
  fixture captured from today's code; then a behavior-preserving extraction of `plan_step` and
  the transition targets onto the builtin `default` pipeline; then selection; then consumer
  alignment.
- **Fast lane last.** A builtin `fast` pipeline and a `lean` profile, routed for small changes.

Step kinds, close, parking and the invariants stay code. Pipelines compose; they never script.

## Architecture / cross-cutting design

### Persona definitions (Stage A)

```yaml
# persona_defs/security_reviewer.yaml   (or .nw/personas/security_reviewer.yaml)
schema: nw.persona.v1
name: security_reviewer
role: reviewer            # implementer | reviewer | triager
template: security_reviewer.md     # resolved next to the YAML file
description: "crypto misuse, secrets, injection"
category: [crypto, secrets, injection]   # optional: narrows the findings `category` vocabulary
```

- `role` fixes the output schema and `read_only`: `implementer` → `IMPLEMENTER_SCHEMA`,
  writable; `reviewer` → the shared findings schema (`verdict`, `score`, `findings`,
  `fix_brief`) that `triage_reviews` (SPEC-0203) already reads, read-only; `triager` →
  `GATE_TRIAGER_SCHEMA`, read-only. A definition cannot set its own schema or flip `read_only`.
- Builtin-only personas (`spec_writer`, `spec_reviewer`, `review_triager`,
  `conflict_resolver`) keep their invariants and cannot be overridden or shadowed by a
  user/project file; a project file with a builtin name is a validation error.
- Templates may use only the role's subset of `PLACEHOLDERS`; an unknown `{{name}}` fails
  validation. All problems are reported at once (SPEC-0170 style).
- `REVIEWER_PERSONAS` and `ADOPT_REGISTRY` become functions derived from the registry
  (`role: reviewer` → `("review", None)`), so membership tests have one source of truth.
- Project definitions are read from the project's main checkout, never a campaign worktree, so
  a campaign cannot rewrite the persona that judges it.
- Profiles keep setting model/effort/budget per persona name; a custom persona with no profile
  entry gets its role's defaults from the selected profile.

### Pipeline definitions (Stage B)

```yaml
# pipelines/default.yaml  (builtin; user and project layers like profiles)
schema: nw.pipeline.v1
name: default
description: "spec -> review -> implement -> gate -> review -> close (today's behavior)"
entry: spec_write                # or: implement, with requires: accepted_spec
profile: auto                    # auto = PROFILE_MATRIX selection; or a profile name
steps:
  spec_write:  {kind: spec_write,  on_pass: spec_review}
  spec_review: {kind: spec_review, on_pass: implement, on_fail: spec_write}
  implement:   {kind: implement,   on_pass: gate}
  gate:        {kind: gate, tier: loop, on_pass: review, on_fail: triage}
  triage:      {kind: triage,      on_pass: implement}
  review:      {kind: review, reviewers: profile, on_pass: close, on_fail: implement}
  close:       {kind: close}
```

- **Step kinds (closed set):** `spec_write`, `spec_review`, `implement`, `gate`, `triage`,
  `review`, `close`. Each kind's runner, park reasons and cost-cap checks are today's code; the
  graph only says which step comes next on pass/fail.
- **Parameters per kind:** `gate.tier` (`loop` runs `loop`+`always` gates, today's GATING);
  `review.reviewers` (`profile` = the profile's list, or an explicit list of reviewer-role
  personas); `implement`/`triage` may name a persona of the matching role (default the builtin).
- **Profiles become parameters.** A pipeline names its default `profile` (`auto` keeps the
  SPEC-0219 matrix); `--profile` still overrides; `loop_max`, weights and per-persona settings
  still come from the profile.
- **Framework states are not steps.** `QUEUED`, `WAITING`, `OPENING`, `PARKED`, `MERGED`,
  `HANDED_OFF`, `ABANDONED`, human park requests, the cost cap and adoption stay in code
  around the graph.

### State names

The persisted `state` keeps today's phase vocabulary, derived from the current step's kind
(`implement` → `IMPLEMENTING`, `gate` → `GATING`, …). A new `pipeline` (name) and `step` (step
id) pair is recorded on the campaign and in events. Reason: `nw.queue.v1`, the conductor
decision table, `RESUMABLE_STATES`, the TUI and every stored campaign key on the phase names;
renaming them would turn this epic into a schema migration, while two steps of the same kind
are told apart by `step` alone. Consumers move to `(pipeline, step)` for display and to the
phase for decisions.

### Invariants every pipeline must keep (validator-enforced)

1. **Close is fixed.** Exactly one `close` step; it is reachable from the entry and has no
   parameters. Closing always rebases onto the base and runs the close-tier (`close`+`always`)
   gates; a pipeline cannot skip, reorder or configure either.
2. **The Test plan always runs.** Every path from an `implement` step to `close` passes
   through a `gate` step with `tier: loop` (in nightwright that is the affected-tests gate,
   SPEC-0328). A lean pipeline may drop reviewers, triage and spec review; it may not drop that
   gate. Together with (1) the AGENTS.md Definition of Done is unchanged.
3. **A spec governs.** The entry is `spec_write` (followed, directly or via `spec_review`, by
   the first `implement`), or the pipeline declares `requires: accepted_spec`; a campaign
   launched on such a pipeline without an accepted governing spec parks `config_invalid`. A
   pipeline with neither fails validation (spec-less lanes are a non-goal).
4. **Loops are bounded.** Any edge back to `implement` counts against the profile's
   `loop_max`; exhaustion parks `loop_exhausted` as today. Not configurable per edge.
5. **Well-formed graph.** Known kinds only; every edge target exists; every step is reachable
   from `entry`; every non-close step has `on_pass`; `on_fail` is required where the kind can
   fail (`gate`, `spec_review`, `review`); persona references resolve to the right role.
6. **Same trust boundary as personas.** Project pipelines are read from the main checkout.

### Merge hotspot and blast radius

- **`queue.py` is the hotspot.** B2 rewrites `plan_step` and every `finish(<STATE>, …)` target;
  A3 touches `_pending_reviewers`/`_prepare_reviews`/adoption in the same module. These two must
  not be in flight together: A3 `depends_on` B2. Keep B2 a pure refactor (no behavior change, no
  renames beyond the dispatch) so concurrent campaigns rebase cleanly.
- **Conductor/TUI blast radius (B4).** `events.py` (`_PERSONA_STATES`, `ACTIVE_STATES`
  consumers), `tui/agentic_model.py`, `journey.py`, `render.py`, `cli_queue.py`,
  `completion.py`, the conductor decision table and the `/nw-conduct` and `/nw-steward` skills.
  B4 is additive (`pipeline`/`step` fields), so old rows without them read as `default`.
- **Spec schema.** B3 adds an optional `pipeline:` frontmatter field to `spec.schema.json`
  (`additionalProperties: false`, so this is a schema change every spec lint run sees).

## Breakdown / sub-specs

Children set `parent: SPEC-0348`. Labels (A1…C1) are for sequencing; seeded ideas get real ids.

- [ ] Pipeline A1: persona registry as data — builtin personas moved to `persona_defs/*.yaml` + existing templates; layered loader (builtin/user/project-main-checkout) and strict validator; `PERSONAS`, `REVIEWER_PERSONAS`, `ADOPT_REGISTRY` derived from it; `nw personas list|show`; equality test against the old tables. No behavior change. (depends_on: none)
- [ ] Pipeline A2: role-limited custom personas — user/project persona definitions (`role: implementer|reviewer|triager`, placeholder subset, `category` narrowing, builtin-name shadowing refused); `nw personas validate`; profile `reviewers:` and `personas:` accept declared names. Not yet run by the queue. Absorbs IDEA-090 slices 1–2. (depends_on: A1)
- [ ] Pipeline A3: queue runs custom personas — review fan-out, triage, divergence and adoption use registry membership; custom reviewers appear in watch chips; `config_invalid` park for a broken declaration. Absorbs IDEA-090 slice 3. (depends_on: A2, B2 — queue.py hotspot)
- [ ] Pipeline B1: pipeline schema, loader, validator and golden fixture — `nw.pipeline.v1`, layered `pipelines/*.yaml`, the six invariants, builtin `default.yaml`, `nw pipelines list|show|validate`; a golden-transition fixture (state x outcome → next state/park) captured from today's `plan_step`/`finish` targets. Inert. (depends_on: none; parallel with A1/A2)
- [ ] Pipeline B2: behavior-preserving extraction — `plan_step` and transition targets driven by the `default` graph; `pipeline`/`step` recorded on campaigns; golden fixture and the existing suite pass unchanged. (depends_on: B1)
- [ ] Pipeline B3: pipeline selection — optional spec frontmatter `pipeline:`, classifier route (SPEC-0219 review extended to pick a pipeline), `nw queue add --pipeline`; precedence flag > spec field > route > `default`; pipeline `profile:` default, `--profile` still overrides; `requires: accepted_spec` enforced at launch. (depends_on: B2)
- [ ] Pipeline B4: consumer alignment — `pipeline`/`step` in `nw.queue.v1` rows and events, TUI and `nw queue` show them, conductor decision table and `/nw-conduct`, `/nw-steward` skills keyed on step kind; rows without the fields read as `default`. (depends_on: B3)
- [ ] Pipeline C1: fast-track lane — builtin `fast` pipeline (`requires: accepted_spec`, implement → gate tier loop → close; no reviewers, no triage) plus builtin `lean` profile (`loop_max: 1`, small budgets), and a classifier route for small, low-risk specs. Delivers IDEA-617. (depends_on: B3, B4)

Sequencing / dependencies: two lanes start in parallel, A1 → A2 and B1 → B2. A3 waits for B2
(both edit `queue.py`; serializing them is cheaper than resolving the conflict). B3 → B4 follow
B2 serially; A3 can run alongside B3/B4. C1 is last because it needs selection (B3) and must be
visible in the TUI/conductor (B4) before anyone is routed onto a lane with no reviewers.

```
A1 -> A2 -> A3        (A3 also waits for B2)
B1 -> B2 -> B3 -> B4 -> C1   (C1 also needs B3)
```

## Acceptance criteria

- [ ] A project adds a reviewer-role persona under `.nw/personas/`, lists it in a profile's
      `reviewers:`, and a campaign runs it and triages its findings, with no nightwright
      code change.
- [ ] `PERSONAS`, `REVIEWER_PERSONAS`, `ADOPT_REGISTRY` and the campaign step graph are no
      longer hand-written tables in `personas.py`/`queue.py`; they are derived from YAML.
- [ ] With no `pipeline:` and no route, every campaign follows the builtin `default` pipeline
      and the golden-transition fixture captured before B2 passes unchanged.
- [ ] A pipeline that violates any of the six invariants (skips close, no loop-tier gate
      between implement and close, no spec step and no `requires: accepted_spec`, unbounded
      loop, malformed graph, wrong persona role) is refused by `nw pipelines validate` and by
      launch.
- [ ] A campaign's pipeline and step are visible in `nw queue`, events, the TUI and the
      conductor's view.
- [ ] A campaign launched with `--pipeline fast` on an accepted spec runs implement →
      loop-tier gates → close (rebase + close-tier gates) and runs no reviewer.
- [ ] AGENTS.md's Definition of Done text is unchanged.

## Test plan

Integration-level; unit tests live in each child.

- **Golden transitions (B1, re-run in B2 onward):** `tests/test_agentic_pipeline_golden.py`
  drives `plan_step` and the transition handlers over every (state, outcome) pair with the fake
  runner and compares against the committed fixture captured from pre-B2 code.
- **Existing suite as the regression guard:** the agentic queue tests (`tests/test_agentic_*`)
  pass unchanged after B2; any test edit in B2 must be justified in that child's spec.
- **Invariant negatives:** one fixture pipeline per invariant in
  `tests/test_agentic_pipelines.py`, each refused with the expected message.
- **End-to-end (after C1):** a fake-runner campaign per builtin pipeline (`default`, `fast`)
  through to MERGED in a temp repo, asserting the step sequence, that close rebased and ran the
  close-tier gates, and (for `fast`) that no reviewer ran; one campaign with a project-defined
  reviewer persona asserting its verdict reached triage.
- **Manual verification:** on this repo, `nw pipelines list`, `nw personas list`, and one real
  `--pipeline fast` campaign on a small accepted spec, observed in the TUI; noted in C1's spec.

## Rollout / sequencing

Each child is inert or behavior-preserving until selection (B3) lets a campaign leave `default`;
partial delivery leaves today's queue unchanged. A1/A2 and B1 can land in any order. B2 lands
when the queue is quiet enough that open campaigns can rebase over a `queue.py` refactor. C1
ships `fast` routed only by explicit `--pipeline`/spec field at first; the classifier route is
enabled in the same child after one observed manual campaign.

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; `uv run pytest` green at the queue's close gate.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

_None._ Decisions taken while drafting, recorded for later tuning: state vocabulary kept and
`(pipeline, step)` added (see State names); A3 serialized after B2 for the `queue.py` hotspot;
gates are selected by tier, not by gate name, because gate names are per project; spec-less
pipelines excluded pending an AGENTS.md amendment; merge/split/docs-only campaigns left outside
the graph.
