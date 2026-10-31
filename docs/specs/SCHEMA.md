# Spec schema

Every file in `docs/specs/NNNN-slug.md` (except `TEMPLATE.md` and this file) begins with a
**YAML frontmatter** block validated by [`spec.schema.json`](./spec.schema.json), followed
by a **Markdown body** with a conventional set of sections.

## Frontmatter fields

| Field | Required | Type | Notes |
|---|---|---|---|
| `id` | ✔ | `SPEC-NNNN` | Zero-padded to 4 digits; must match the filename number. |
| `title` | ✔ | string | Short human title. |
| `status` | ✔ | enum | See lifecycle below. |
| `owner` | ✔ | string | Accountable person. |
| `created` | ✔ | date | `YYYY-MM-DD`. |
| `updated` | – | date | Last substantive edit. |
| `milestone` | – | string | Grouping/phase label the ledger sorts by (e.g. `M1: packaging`). |
| `kind` | – | enum | `feature` (default if absent), `epic`, `policy`, or `spike`. Drives kind-specific lint rules. |
| `parent` | – | `SPEC-NNNN` | The epic this spec is a child of (see below). |
| `tags` | – | string[] | Free-form (e.g. `ingest`, `reporting`, `infra`). |
| `depends_on` | – | `SPEC-NNNN[]` | Must land first. |
| `supersedes` | – | `SPEC-NNNN[]` | Decisions this replaces. |
| `superseded_by` | – | `SPEC-NNNN` | Required when `status: superseded`. |
| `source_idea` | – | string | The org idea's `:ID:`/`file:line`, set when the spec was scaffolded via `nw spec new --from-idea` (SPEC-0013). |

`additionalProperties` is `false` — unknown keys fail validation, so typos are caught.

## Lifecycle (status state machine)

```
        ┌──────────────────────── rejected
        │
draft → proposed → accepted → in-progress → done
                                              │
                                              └──→ superseded  (via a newer spec)
```

- **draft** — being written; not yet up for review.
- **proposed** — complete and awaiting a decision.
- **accepted** — decision made; approved to build, not started.
- **in-progress** — implementation underway.
- **done** — shipped; the body reflects what actually landed.
- **superseded** — a later spec replaced it. Set `superseded_by`. Keep the file — it is
  the historical record.
- **rejected** — decided against. Keep the file with a short rationale.

A spec is never deleted or silently rewritten once past `draft`; decisions are amended by
**superseding**, so the ledger stays a faithful timeline.

## Body sections

The body is prose (not schema-enforced) but should use these headings so specs read
consistently and the ledger can link to stable anchors:

- **Context** — the problem/motivation; what exists today.
- **Goals / Non-goals** — what success is, and explicitly what's out of scope.
- **Decision** — the chosen approach, stated plainly.
- **Design** — the how: modules, data shapes, interfaces, migrations.
- **Alternatives considered** — options weighed and why they lost.
- **Acceptance criteria** — objectively checkable statements that must be true to mark `done`.
- **Test plan** — *how* the acceptance criteria are proven: automated tests (what/where),
  manual steps, and the regression guard. Required for feature/behavior specs; a pure
  policy/doc spec may write "N/A — <reason>".
- **Rollout / migration** — steps, ordering, data moves.
- **Definition of done** — the loop-closure checklist (see `AGENTS.md`). A spec is not
  `done` until its test plan has been executed and the spec reflects what shipped.
- **Open questions** — unresolved items (should be empty before `accepted`).

Use `[[SPEC-NNNN]]`-style references in prose by linking to the file:
`[SPEC-0001](./0001-package-refactor-and-xdg-layout.md)`.

## Kinds

`kind` selects which rules apply (introduced in [SPEC-0003](./0003-epic-and-subspec-model.md)):

- **feature** (default) — a behavior/code change. Requires a real Test plan.
- **epic** — an umbrella over many child specs (see below). Requires an integration-level
  Test plan; a `done` epic requires all its children `done`/`superseded`.
- **policy** — a process/doc decision (like [SPEC-0000](./0000-how-we-write-specs.md)). May
  use `N/A — <reason>` for the Test plan.
- **spike** — throwaway research/exploration. May use `N/A`.

## Epics & sub-specs

A large feature is modeled as an **epic** plus **child** specs:

- The epic sets `kind: epic` and is authxxed from `TEMPLATE-epic.md` (adds *Architecture /
  cross-cutting design*, *Breakdown / sub-specs*, and an integration *Test plan*).
- Each child is a normal spec that sets `parent: SPEC-NNNN` pointing at the epic. `parent`
  is the **single source of truth** for hierarchy — the tree is derived from it, never a
  hand-maintained `children` list.
- Ids stay flat and sequential (no `SPEC-0003.1`, no per-epic subdirectories).
- The linter enforces: `parent` resolves to a spec whose `kind` is `epic`, and an epic
  can't be `done` while any child is unfinished.

## The ledger is generated

`docs/LEDGER.md`'s spec index (Epics rollup + Active/Done/Superseded tables) is **generated
from frontmatter** between its `<!-- specs:begin -->` / `<!-- specs:end -->` markers:

```bash
python3 tools/spec_lint.py --write-ledger   # regenerate the block
python3 tools/spec_lint.py                  # verify it's current (fails if stale)
```

Don't hand-edit the generated block; edit spec frontmatter and regenerate. The hand-written
header and Milestones section around the markers are preserved.
