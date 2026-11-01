# `nw` documentation & decision system

This directory is the source of truth for **how `nw` is designed and why**. It drives
new feature development *and* records past decisions so they don't have to be
re-derived from the code.

## Layout

```
docs/
  README.md            ← you are here
  WORKFLOW.md          ← human cheatsheet: idea → spec → tasks → (export)
  LEDGER.md            ← centralized index (generated block + hand-written milestones)
  specs/
    spec.schema.json   ← JSON Schema for spec frontmatter (machine-checkable)
    SCHEMA.md          ← human description of the schema, lifecycle, kinds & epics
    TEMPLATE.md        ← copy this to start a normal spec
    TEMPLATE-epic.md   ← copy this to start an epic (large feature → many sub-specs)
    NNNN-slug.md       ← the specs themselves
  design/
    README.md          ← free-form design notes, research, diagrams
  product/             ← the published user documentation (mkdocs `docs_dir`: concepts,
                         capabilities, guides, the agentic loop, reference)
  examples/            ← example export schemes (`schemes/`)
```

Day-to-day idea→shipped work: start at [`WORKFLOW.md`](./WORKFLOW.md). The rest of this
page is how we **govern** that work with specs.
The linter lives at `tools/spec_lint.py` (repo root) and is enforced by `uv run pytest`.

## What is a spec vs. a design note?

- A **spec** (`specs/NNNN-slug.md`) is a numbered, schema-governed document that proposes
  and records a concrete decision or feature. It has structured frontmatter, moves through
  a defined lifecycle (`draft → proposed → accepted → in-progress → done`), and is indexed
  in [`LEDGER.md`](./LEDGER.md). Specs are the unit of planning **and** the historical
  record — a `done` spec is the archaeology of why the code looks the way it does.
- A **design note** (`design/…`) is anything that supports a spec but isn't itself a
  decision: exploratory research, benchmark evidence, diagrams, throwaway analysis. Notes
  are not schema-governed and not tracked in the ledger.

## Workflow for a new feature

1. `cp specs/TEMPLATE.md specs/NNNN-my-feature.md` (next free number). For a large feature
   that fans out into many specs, `cp specs/TEMPLATE-epic.md …` and give each child a
   `parent: SPEC-NNNN`.
2. Fill in the frontmatter and body; set `status: draft`.
3. `python3 tools/spec_lint.py --write-ledger` to add it to [`LEDGER.md`](./LEDGER.md).
4. Discuss / iterate → `proposed` → `accepted`.
5. Implement → `in-progress` → `done`. Keep the spec's *Design* and *Acceptance criteria*
   truthful to what actually shipped; update it, don't abandon it. See
   [`AGENTS.md`](../AGENTS.md) for the loop-closure Definition of Done.

Superseding a decision? Write a new spec, set its `supersedes: [SPEC-NNNN]`, and set the
old one's `status: superseded` + `superseded_by: SPEC-MMMM`. Never silently rewrite
history — supersede it.

## Validating specs

The linter validates frontmatter (against `specs/spec.schema.json`) plus the content and
hierarchy rules, and keeps the ledger in sync:

```bash
python3 tools/spec_lint.py                 # validate all specs + ledger freshness
python3 tools/spec_lint.py --write-ledger  # regenerate LEDGER.md's generated block
uv run pytest                              # the same lint is enforced in the test suite
```

See [`specs/SCHEMA.md`](./specs/SCHEMA.md) for the full rule set (kinds, epics, lifecycle).
