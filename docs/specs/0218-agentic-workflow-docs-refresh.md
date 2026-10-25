---
id: SPEC-0218
title: "Agentic workflow docs refresh: a guide, a coherent reference, concepts and front door"
status: superseded
superseded_by: SPEC-0219
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M8: product documentation"
kind: feature
parent: SPEC-0148
tags: [docs, agentic]
---

## Context

The agentic layer (epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md))
has shipped most of its children, but the product docs (`docs/product/`, MkDocs Material, nav in
`mkdocs.yml`) only describe it in one place:

- `docs/product/capabilities/agentic.md` (274 lines) was appended to section by section by each
  child spec (Project configuration, Campaigns and the ledger, Profiles, Gates, Personas, Reviews,
  Closing, Queue, Backlog and admission, Events and watchdog, Mailbox, Conductor, Stewards). Its
  intro still says "the sections below are filled in by its child specs as they land"; its
  state-machine text diagram (under `## Queue`) omits `WAITING` (SPEC-0200) and the terminal
  `ABANDONED`; its park-reason list omits `config_invalid` (SPEC-0194). Nothing covers the
  spec-change guard (SPEC-0186), the `LEDGER.md` rebase resolver (SPEC-0191), the schema'd logs
  and `wt queue runner-log` (SPEC-0192), runner re-exec (SPEC-0194), the lease heartbeat
  (SPEC-0204), `wt queue watch` (SPEC-0185/0200), `wt queue followers` (SPEC-0198) or
  `wt campaign runs|run` (SPEC-0199).
- There is **no guide** — no page answers "how do I run this day to day": who the roles are, how
  to start the loop and the conductor/steward sessions, what a park means and what to do about it,
  how merged code is picked up, how to work outside the queue.
- `concepts/architecture.md` (storage stores diagram) and `concepts/workflows.md` (idea→ship,
  outbound, desk diagrams) say nothing about the queue, campaigns, the data-dir `agentic/` tree or
  the conductor/steward sessions. `concepts/lifecycle.md` has no mention either.
- `docs/WORKFLOW.md` (the in-repo cheatsheet) has four agentic rows (`wt spec review`,
  `wt queue add`, `wt queue run / list`, `wt campaign log`). `README.md` links only
  `capabilities/agentic.md` (line ~61).

What is shipped vs in flight, from `docs/LEDGER.md` on 2026-10-25:

- **Done:** SPEC-0170–0177, 0179–0181, 0183–0188, 0191–0194, 0198–0201, 0204, 0212.
- **In progress (shipped but spec not closed):** SPEC-0182 (event-driven `/wt-conduct`; the
  skill and `wt queue conduct-status` exist and are already documented).
- **Accepted, not built:** reviewer personas/triage SPEC-0202, 0203, 0205, 0206, 0207; the
  idea-to-spec phase SPEC-0208, 0209, 0210, 0211 plus 0214, 0215, 0216, 0217 (being authored on
  another branch at the time of writing; titles may change).

Facts the docs must reflect (from code, `src/wt/agentic/`):

- `queue.STATES` = `QUEUED, WAITING, OPENING, IMPLEMENTING, GATING, TRIAGING, REVIEWING,
  CLOSING, PARKED, MERGED, HANDED_OFF, ABANDONED`; `ACTIVE_STATES` (hold a slot) = `OPENING …
  CLOSING`; `RESUMABLE_STATES` (`wt queue resume --to`) = `IMPLEMENTING, GATING, REVIEWING,
  CLOSING, QUEUED`.
- `queue.PARK_REASONS` = `cost_cap, loop_exhausted, implementer_blocked, triage_escalated,
  open_failed, rebase_conflict, main_dirty, main_not_on_base, base_moving, gh_failed, human,
  review_escalated, config_invalid`.
- Builtin profiles (`profiles.py`): `balanced` 2 slots / 3 loops / $10 cap, `frugal` 1 / 2 / $3,
  `thorough` 3 / 4 / $30.
- `wt queue` subcommands: `abandon add backlog conduct-status events followers gates list park
  prioritize resume run runner-log tick watch`; `wt campaign`: `list log note run runs show`
  (plus hidden `prompt`); `wt mailbox`: `follow list send thread`; `wt spec review`.
- `wt queue run` re-execs itself between ticks when merged commits touch `src/wt/` or
  `pyproject.toml` (`--no-reexec` opts out); `--once`, `--until-idle`, `--max-ticks`,
  `--json-lines`.
- Personas in `src/wt/agentic/personas/`: `implementer`, `gate_triager`, `conformance_reviewer`,
  `spec_reviewer`.
- Skills: `skills/wt-conduct/SKILL.md` (Monitor on `wt queue events --follow --for conductor`,
  decision table, restart command `setsid nohup wt queue run >> …/runner.log 2>&1 &`),
  `skills/wt-steward/SKILL.md` (Monitor on `wt mailbox follow --to steward:<Project>`; queue
  writes go to the conductor as mailbox `request`s).

## Goals / Non-goals

**Goals**
- One task-oriented guide that lets a new user run the agentic loop end to end.
- `capabilities/agentic.md` becomes a coherent reference of everything shipped, organized by
  topic rather than by landing order, consistent with the guide.
- The concept pages and the front door (README, `docs/WORKFLOW.md`) include the agentic layer.
- A test pins the guide and the reference against the code's state and park-reason sets, so a
  new state or park reason cannot ship undocumented.

**Non-goals**
- No code or behavior change in `src/`. No change to skills.
- Not documenting accepted-but-unbuilt features (SPEC-0202/0203/0205/0206/0207) beyond, at most,
  a one-line "planned" mention; the idea-to-spec phase gets an explicit "in progress" section
  only.
- No new SVG captures (capture toolchain is SPEC-0149; agentic captures can be a later spec).
- No changes to `guides/agent-surface.md` beyond fixing a link if the restructure breaks one.

## Decision

Add `docs/product/guides/agentic-workflow.md` (nav: Guides → "Agentic workflow"), rewrite
`docs/product/capabilities/agentic.md` as a topic-ordered reference that links the guide, add an
agentic section with a Mermaid diagram to `concepts/architecture.md` and
`concepts/workflows.md` (and a short note in `concepts/lifecycle.md`), extend `docs/WORKFLOW.md`
and `README.md`, and add `tests/test_docs_agentic_workflow.py`, which imports
`wt.agentic.queue.STATES` and `PARK_REASONS` so the docs are checked against the code.

## Design

### Conventions for every edited page

- Spec references from `docs/product/` are **plain text ids** (`SPEC-0194`), never relative links
  to `../../specs/…`: those files are outside `docs_dir` and `mkdocs build --strict` fails on
  them (this is the convention the existing agentic page already follows).
- Diagrams use the existing ```` ```mermaid ```` fence (`pymdownx.superfences` custom fence in
  `mkdocs.yml`, already used by `concepts/workflows.md`). No `mkdocs.yml` extension changes.
- Command examples must be real: every `wt …` invocation shown must parse (subcommand and flags
  exist in `--help` at implementation time). Placeholders use the existing style (`CAMPAIGN`,
  `SPEC-0170`, `steward:<Project>`).
- Shipped vs in progress is decided by `docs/LEDGER.md` **at implementation time**: a feature is
  documented as shipped only if its spec is `done` (SPEC-0182 counts as shipped since its skill and
  command exist and are already documented). If any idea-to-spec spec has reached `done` by then,
  its states/commands are documented as shipped in the guide and reference (the test below forces
  this for any new state or park reason) and it is dropped from the in-progress list.

### 1. New guide `docs/product/guides/agentic-workflow.md`

Added to `mkdocs.yml` nav under Guides as `Agentic workflow: guides/agentic-workflow.md` and to
the table in `guides/index.md`. Sections (H2 headings, in this order):

1. **Roles** — the queue/runner (`wt queue run`, one process under the lease, Python decides every
   transition; personas only commit and return one verdict), the conductor session
   (`/wt-conduct`), steward sessions (`/wt-steward`, one per project), and the human (writes and
   accepts specs, answers escalations, never edits campaign worktrees or the main checkout while
   the loop runs). A Mermaid `flowchart` showing human → spec review → queue → runner → personas,
   events → conductor, mailbox ↔ stewards.
2. **The campaign state machine** — a Mermaid `stateDiagram-v2` with every state in
   `queue.STATES` and the transitions: `QUEUED → OPENING` (admitted: ready + free slot, priority
   then age), `OPENING → IMPLEMENTING → GATING`, `GATING → REVIEWING` (required gates green) /
   `→ TRIAGING` (required gate red), `TRIAGING → IMPLEMENTING` (fix, loops+1) / `→ GATING`
   (retry_gate) / `→ REVIEWING` (waive optional) / `→ PARKED` (escalate),
   `REVIEWING → CLOSING` (all reviewers pass) / `→ IMPLEMENTING` (fix, loops+1) / `→ PARKED`
   (`review_escalated`), `CLOSING → MERGED | HANDED_OFF` / `→ TRIAGING` (spec-dod, spec-changed,
   re-gate failure) / `→ PARKED`, any active state `→ PARKED`, `PARKED → resume target | WAITING |
   ABANDONED`, `WAITING → resume target` when a slot frees (ahead of the backlog),
   `QUEUED → ABANDONED`. A table follows with one row per state: meaning, holds a slot (yes for
   `ACTIVE_STATES`), who moves it on.
3. **Before you start** — `.wt/project.yaml` (`wt projects init`, `wt projects config`), gates,
   merge policy, `merge.regenerate`; profile choice; `wt spec review SPEC-NNNN` until `approve`
   (sidecar keyed by the spec's sha256; editing the spec invalidates it; `--force` bypass).
4. **Starting the loop** — `wt queue add SPEC-NNNN` (dry-run) then `--yes`, `--priority`,
   `depends_on` chains queued in one go; `wt queue run` (and the detached form the conductor
   uses), `--once` / `--until-idle`; then `/wt-conduct` in one Claude Code session and
   `/wt-steward` in each project session (both after `/wt-orient`); `wt queue followers` to confirm
   who is armed.
5. **Day to day** — real examples, each with a one-line "what you get": `wt queue list`,
   `wt queue watch`, `wt queue backlog`, `wt queue prioritize`, `wt queue events --tail 20`,
   `wt queue conduct-status`, `wt queue runner-log --tail 30`, `wt campaign show`,
   `wt campaign log --tail 20`, `wt campaign runs`, `wt campaign run`, `wt campaign note`,
   `wt queue gates`, `wt queue park`, `wt queue resume`, `wt queue abandon`, `wt mailbox send`,
   `wt mailbox list --to conductor --unanswered`, `wt mailbox thread`.
6. **Park reasons and what to do** — a table with one row per value of `queue.PARK_REASONS`:
   what triggered it and the usual remedy (e.g. `cost_cap` → `wt queue resume ID --raise-cap N
   --yes`; `loop_exhausted` → read the last brief/findings, fix the spec or resume with
   `--reset-loops --note`; `rebase_conflict` → resolve in the campaign worktree, resume `--to
   GATING`; `main_dirty` / `main_not_on_base` → move in-place work off the main checkout, resume
   `--to CLOSING`; `config_invalid` → fix `.wt/project.yaml`, auto-resumes once it loads;
   `human` → whoever parked it decides). The remedies must agree with the
   `skills/wt-conduct/SKILL.md` decision table.
7. **Costs, profiles and caps** — builtin `balanced` / `frugal` / `thorough` with their slots,
   loops and caps; per-persona `max_budget_usd`; the pessimistic pre-spawn cap check (including the
   reviewer fan-out sum); where costs show (`wt queue list`, `wt campaign runs`, ledger `cost`
   events); `cost_warning` events; a config delta example.
8. **How merged code is picked up** — the runner re-execs between ticks when a merge touches
   `src/wt/` or `pyproject.toml` (`stop {reason: code_changed}` in `runner-log`, lease handed
   over; `--no-reexec`); the lease heartbeat keeps a long tick from looking stale (SPEC-0204);
   `config_invalid` recovery; sessions (conductor/steward) keep their already-loaded skill text.
9. **Working outside the queue** — never edit the main checkout (it is the merge target; the close
   step parks `main_dirty` / `main_not_on_base`); work in a `git worktree add -b <branch>` of your
   own; when done, send the conductor a mailbox `request` naming branch, head sha and worktree
   (example `wt mailbox send --to conductor --kind request "…"`); from a project session route
   queue requests through the steward/conductor rather than running queue writes.
10. **In progress: from idea to spec in the queue** — an admonition (`!!! note "In progress"`)
    describing the planned flow `wt queue add IDEA-NNN` → `SPEC_WRITING` → `SPEC_REVIEW` /
    `SPEC_REVISION` → accept → implement, explicitly marked as not shipped, listing each of
    SPEC-0208, 0209, 0210, 0211, 0214, 0215, 0216, 0217 that is not `done` at implementation time,
    and pointing at `docs/LEDGER.md` for current status. The planned state names appear only in
    this section.
11. **See also** — links to `../capabilities/agentic.md`, `../concepts/workflows.md`,
    `../concepts/architecture.md`, `agent-surface.md`.

### 2. Restructure `docs/product/capabilities/agentic.md`

Rewritten as a reference (not a narrative): intro paragraph that links
`../guides/agentic-workflow.md` for the how-to and removes the "filled in by its child specs"
wording. Topic order: Overview & roles → Project configuration (incl. `merge.regenerate`) →
Profiles → Campaigns, ledger and logs (state.json, ledger.jsonl validated as
`wt.ledger.event.v1`, `runs/NNN-<persona>/run.json` as `wt.run.v1`, gate `.log` + `.json`
sidecars as `wt.gate.v1`, `runner.jsonl` as `wt.runner.record.v1`) → States (table of every
`queue.STATES` value, active/terminal sets, `WAITING` semantics) → Admission and backlog →
Personas → Gates and triage → Reviews (spec review before admission; conformance review and
fan-out in REVIEWING) → Closing (DoD audit, spec-change guard with `spec_at_open.md` and the
`spec-changed` synthetic gate, rebase with the deterministic regenerate resolver, re-gate,
merge or PR, cleanup) → Park reasons (every `queue.PARK_REASONS` value, one line each, linking
the guide's remedies table) → The runner (lease, heartbeat thread, re-exec, signals,
`runner-log`) → Observability (`wt queue list|watch|backlog|events|followers|runner-log|
conduct-status`, `wt campaign show|log|runs|run`) → Events and watchdog (event types, watchdog
conditions, `conductor_ping`) → Mailbox → Conductor → Stewards → Dogfooding (wt's own
`.wt/project.yaml`). Existing correct content is kept and moved, not dropped: every H2 topic in
the current page survives as a heading or subsection.

### 3. Concept pages

- `concepts/architecture.md`: a new `## Agentic data` section with a Mermaid flowchart of
  `<data_dir>/agentic/` (`campaigns/<id>/{state.json, ledger.jsonl, runs/, gates/,
  spec_at_open.md}`, `events.jsonl`, `queue.lock` / `queue.lease.json`, `runner.jsonl`, mailbox
  inboxes and cursors) plus the repo-side `.wt/project.yaml` and spec review sidecars, and the
  campaign worktrees under `worktrees.root`; a row added to the stores table. Paths must match
  the code at implementation time (`store.py`, `events.py`, `mailbox.py`, `spec_review.py`).
- `concepts/workflows.md`: a new `## Agentic loop` section with a Mermaid `sequenceDiagram`
  (human → `wt spec review` → `wt queue add` → runner → implementer/gates/reviewers → close →
  merged → event → conductor → `wt spec reconcile --apply`) linking the guide; the "Dual systems"
  intro mentions the agentic path.
- `concepts/lifecycle.md`: a short section stating that the loop closes a spec only when
  `wt spec verify` passes and the spec is `done` (the close step's DoD audit), so the DoD is the
  same with or without the queue.

### 4. Front door

- `docs/WORKFLOW.md`: the agentic rows of the pipeline block are extended to cover at least
  `wt queue watch`, `wt queue backlog`, `wt queue resume`, `wt queue events`,
  `wt campaign runs`, `/wt-conduct`, `/wt-steward`, plus a pointer line to
  `docs/product/guides/agentic-workflow.md`.
- `README.md`: the agentic line links `docs/product/guides/agentic-workflow.md` (and keeps the
  capability link); README stays ≤ 120 lines (`tests/test_docs_readme_front_door.py`).

### 5. Test `tests/test_docs_agentic_workflow.py`

Pure file reads plus imports of `wt.agentic.queue`:

- the guide file exists, is > 4000 characters, and `guides/agentic-workflow.md` appears in
  `mkdocs.yml` and `guides/index.md`;
- every value of `STATES` appears backticked (`` `VALUE` ``) in `capabilities/agentic.md` and
  as a token inside the guide's first ```` ```mermaid ```` block that contains `stateDiagram-v2`;
  every value of `PARK_REASONS` appears backticked in `capabilities/agentic.md` and as the first
  cell of a guide table row (a line starting ``| `VALUE` |``);
- the guide contains `/wt-conduct`, `/wt-steward`, `wt queue run`, `wt queue add`,
  `wt queue resume`, `wt queue watch`, `wt queue followers`, `wt queue runner-log`,
  `wt campaign runs`, `wt mailbox send`, `wt spec review`, `stateDiagram`, `--raise-cap`,
  `--no-reexec`, and an `!!! note "In progress"` admonition; a module constant
  `IN_FLIGHT = ("SPEC-0208", "SPEC-0209", "SPEC-0210", "SPEC-0211", "SPEC-0214", "SPEC-0215",
  "SPEC-0216", "SPEC-0217")` is filtered to the ids whose row in the **generated table** of
  `docs/LEDGER.md` (a line starting `| [SPEC-NNNN](` whose fourth cell is the status) is not
  `done` (an id absent from those tables counts as not done), and each remaining id must appear in the
  guide's text after the admonition line; while SPEC-0209 is not `done`, the admonition section
  must also contain `SPEC_WRITING`, and no other page under `docs/product/` (and no text of the
  guide before the admonition) contains `SPEC_WRITING`, `SPEC_REVIEW` or `SPEC_REVISION`;
- `capabilities/agentic.md` does not contain `filled in` and links `agentic-workflow.md`;
- `concepts/architecture.md` and `concepts/workflows.md` each contain `campaign` and link
  `agentic-workflow.md`;
- `docs/WORKFLOW.md` contains `wt queue watch`, `wt queue resume`, `wt campaign runs` and
  `agentic-workflow.md`; `README.md` contains `agentic-workflow.md`;
- no page under `docs/product/` links to `specs/` via a relative path (strict-build guard).

## Alternatives considered

- **Only extend `capabilities/agentic.md`** — keeps one page, but mixes how-to with reference;
  the page is already the hardest to read in the site, and guides are where the IA puts workflows.
- **Hard-code state and park-reason lists in the test** — simpler, but lets the code grow a state
  the docs never mention; importing from `wt.agentic.queue` makes the test the consistency check.
- **Document the idea-to-spec phase as if shipped** — it is not; describing unshipped commands
  as available would mislead. An explicit in-progress section is honest and cheap to update.
- **Split into guide spec + reference/concepts spec** — the pages must agree with each other and
  one test pins both; one campaign keeps them consistent. The scope is docs plus one test file.

## Acceptance criteria

- [ ] `docs/product/guides/agentic-workflow.md` exists with the eleven sections of Design §1, is
      in `mkdocs.yml` nav under Guides and in `guides/index.md`.
- [ ] The guide's state diagram is a Mermaid `stateDiagram-v2` containing every value of
      `wt.agentic.queue.STATES`, and its park-reason table has one row per value of
      `wt.agentic.queue.PARK_REASONS`, each with a remedy consistent with
      `skills/wt-conduct/SKILL.md`.
- [ ] The guide's in-progress section is an admonition that marks the idea-to-spec flow as not
      shipped and lists every not-`done` spec among SPEC-0208, 0209, 0210, 0211, 0214, 0215, 0216,
      0217 (per `docs/LEDGER.md` at implementation time); the planned states appear nowhere else
      in `docs/product/`.
- [ ] `docs/product/capabilities/agentic.md` follows the Design §2 topic order, links the guide,
      contains every `STATES` and `PARK_REASONS` value, covers the spec-change guard, the
      regenerate resolver, schema'd logs, re-exec and the lease heartbeat, and no longer contains
      "filled in by".
- [ ] `concepts/architecture.md` has an `## Agentic data` section and `concepts/workflows.md` an
      `## Agentic loop` section, each with a Mermaid diagram and a link to the guide;
      `concepts/lifecycle.md` states the loop's DoD audit.
- [ ] `docs/WORKFLOW.md` and `README.md` are updated per Design §4; README stays ≤ 120 lines.
- [ ] Every `wt …` command shown in the edited pages exists (subcommand and flags) in the
      current `--help`, and every spec reference in `docs/product/` is a plain id, not a relative
      link to `docs/specs/`.
- [ ] `tests/test_docs_agentic_workflow.py` exists with the assertions of Design §5 and passes.
- [ ] `uv run mkdocs build --strict` passes and the full `uv run pytest` suite (incl. the
      existing `tests/test_docs_*.py`) passes; no file under `src/` or `skills/` is changed.

## Test plan

- **Automated tests:** new `tests/test_docs_agentic_workflow.py` (Design §5) — imports
  `STATES` / `PARK_REASONS` from `wt.agentic.queue`, reads `mkdocs.yml`, the guide, the
  capability page, the concept pages, `docs/WORKFLOW.md`, `README.md` and `docs/LEDGER.md`; no
  fixtures needed. Existing `tests/test_docs_mkdocs_build.py` (strict build),
  `tests/test_docs_product_ia.py`, `tests/test_docs_product_guides.py`,
  `tests/test_docs_product_capabilities.py`, `tests/test_docs_readme_front_door.py`,
  `tests/test_docs_product_reference.py`, `tests/test_docs_captures.py` must stay green.
- **Manual verification:** (1) `uv run mkdocs build --strict` exits 0; (2) extract every line
  starting with `wt ` from the code blocks of the edited pages and run `uv run wt <subcommand>
  --help` for each distinct subcommand, confirming each shown flag is listed; (3) read the park
  table against `skills/wt-conduct/SKILL.md`'s decision table and record that the remedies
  agree as a `wt campaign note` on the implementing campaign (or, outside the queue, in the
  closing commit message); (4) `git diff --stat main` shows changes only under `docs/`, `README.md`,
  `mkdocs.yml` and `tests/`.
- **Regression guard:** full `uv run pytest`; `python3 tools/spec_lint.py`.

## Clock Log

CLOCK-IN: [timestamp]
CLOCK-OUT: [timestamp]

## Rollout / migration

1. Write the guide, then restructure the capability page, then concepts, then front door.
2. Add the test; run the strict build and the full suite.
3. After this lands, any spec that adds a campaign state or park reason (e.g. SPEC-0209's
   `SPEC_WRITING`) must add it to the guide and the capability page, or this test fails its
   gates — intended: it keeps the docs honest. Such a spec should then also remove itself from
   the guide's in-progress list.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

- None.
