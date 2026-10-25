---
id: SPEC-0030
title: Org-native markup for idea enrichment bodies
status: superseded
superseded_by: SPEC-0164
owner: user
created: 2026-07-22
updated: 2026-07-22
milestone: "M4: cli ergonomics"
kind: feature
tags: [ideas, org, explore, ergonomics]
source_idea: IDEA-018
depends_on: [SPEC-0024]
---

## Context

Promoted from idea `IDEA-018`.

[SPEC-0024](./0024-formal-explore-stage-wt-explore-skill.md) made Summary / Open questions /
Log org *subtrees* the system of record, with correct headline structure (`** Summary`,
`*** YYYY-MM-DD` under Log). Body prose, however, is written by agents via
`wt idea summary|questions|explore` as a pass-through (`explore.py` does not normalize).
In practice that means Markdown lands in `ideas.org`: backtick code spans, `**bold**`,
ATX habits. IDEA-017 is a concrete example.

`wt idea show` compounds the dialect mismatch by labeling sections with Markdown `##`
headings even though the file is org (display-only; not written to disk).

Org is already the SoR for tasks/ideas (SPEC-0004/0005/0012). Enrichment bodies must
match that dialect.

## Goals / Non-goals

**Goals**
- Enrichment bodies written/stored as org-native markup (`~code~` / `=verbatim=`, `*bold*`,
  `/italic/`, plain `-` lists; no Markdown fences or backtick spans).
- Light **normalize-on-write** in `set_summary` / `set_questions` / `append_log` (and
  `add_question`): convert common Markdown idioms → org before persist.
- Update `wt-explore` / `wt-seed` skills (+ short WORKFLOW note) so agents authxx org, not MD.
- `wt idea show` prints org-style section labels (`** Summary`, etc.), not `##`.
- On promote, Context prefill gets a light **org→Markdown** pass so `docs/specs/` Context
  stays Markdown-native (specs remain MD by design).

**Non-goals**
- Full Markdown↔org pandoc fidelity; only the common agent idioms above.
- Hard-reject / fail the CLI when Markdown is detected (normalize instead).
- Bulk rewrite of all historical idea bodies (optional best-effort only if a section is
  rewritten; no mandatory migration command in v1).
- Changing section headline structure from SPEC-0024.
- Making outbound/internal **spec bodies** org (specs stay Markdown).

## Decision

Ship **normalize-on-write** + **skill/docs convention** + **org-flavored show**, and
**org→md on promote Context**. Do not require a one-shot retro-fix of every idea.

## Design

### Normalizer (`src/wt/explore.py` or small `org_markup.py`)

`to_org_body(text) -> str` applied before every enrichment write:

| Input (Markdown-ish) | Output (org) |
|---|---|
| `` `code` `` | `~code~` |
| `**bold**` / `__bold__` | `*bold*` |
| Line-leading ATX `#`…`######` | strip hashes + space (keep rest as prose) |
| Fenced ` ``` ` blocks | unwrap to indented org example / plain lines (best-effort) |

Leave unknown prose alone. Idempotent on already-org text as far as practical (do not
double-wrap `~code~`).

`from_org_body(text) -> str` for promote Context only (inverse of the common cases:
`~code~`→backticks, `*bold*`→`**bold**` when safe).

### Call sites

- `set_summary`, `set_questions`, `add_question`, `append_log` → `to_org_body`.
- `scaffold_from_idea` / `scaffold_outbound` Context builder → `from_org_body` on the
  enrichment/subtree text included in Context.
- `format_idea_show`: section banners become `** Summary` / `** Open questions` / `** Log`
  (plain text; still Rich-safe).

### Skills / docs

- `skills/wt-explore/SKILL.md`, `skills/wt-seed/SKILL.md`: one short rule — write org
  markup in `--set` / `--note` bodies; examples of `~code~` / `*bold*`.
- `docs/WORKFLOW.md`: one line under explore.

### Tests

Unit-test the normalizer round-trips for the table above; CLI/integration: writing a
summary containing backticks persists `~…~` in the org file; `idea show` lacks `## Summary`.

## Alternatives considered

- **Docs/skills only** — rejected; agents will keep pasting Markdown without a write-path
  guard.
- **Hard reject Markdown** — too brittle for mixed prose; normalize is enough.
- **Bulk migrate all ideas** — deferred; low value vs cost; rewriting a section re-normalizes.
- **Keep `##` in show “for agents”** — rejected; teaches the wrong dialect; `--json` already
  serves agents (SPEC-0026).

## Acceptance criteria

- [x] `to_org_body` converts backticks → `~…~`, `**…**` → `*…*`, strips leading ATX hashes.
- [x] `wt idea summary|questions|explore` persist normalized org in the ideas file.
- [x] `wt idea show` section labels are org-style (`** Summary`), not Markdown `##`.
- [x] Promote Context prefill converts common org emphasis to Markdown.
- [x] Skills + WORKFLOW document org-native enrichment bodies.
- [x] Tests cover normalizer + write/show; full pytest green; generic explore structure
      unchanged.

## Test plan

- **Automated:** `tests/test_org_markup.py` + `tests/test_explore.py` — conversion table;
  MD summary → org on disk; show uses `** Summary`.
- **Manual:** `wt idea show IDEA-018` shows `** Summary`; `to_org_body('use \`export-scheme\` — **not** inventing')` → `use ~export-scheme~ — *not* inventing`.
- **Regression:** full pytest (273 passed after ledger refresh).

## Rollout / migration

Opt-in behavior on next write. No required data migration. Historical Markdown bodies remain
until edited. Skills are symlinked from the repo (`wt skills install`); text already updated.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; pytest green; manual check noted.
- [x] No regressions.
- [x] Spec body matches what shipped.
- [x] `docs/LEDGER.md` regenerated.

## Open questions

_None — decisions locked above._
