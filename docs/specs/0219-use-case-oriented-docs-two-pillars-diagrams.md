---
id: SPEC-0219
title: "Use-case oriented docs: two pillars, bootstrap path and rich diagrams"
status: in-progress
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M8: product documentation"
kind: feature
parent: SPEC-0148
tags: [docs, agentic, ideas]
supersedes: [SPEC-0218]
---

## Context

User, 2026-10-25: the product docs are "very poor". They read as reference command
listings, not as a human path through the product. They need to be much more visual (clear
block diagrams with transitions) and use-case oriented, with clear steps to bootstrap from the
ground up. Two areas matter most:

1. **Ideas to specs:** capture ideas, explore them until they are fully fledged, and turn them
   into specs good enough to feed the agentic loop.
2. **The agentic loop itself:** how it works, its principles, and how it stays simple while
   using several mechanisms to converge, escalate and recover.

SPEC-0218 (a refresh queued for an implementer persona) is superseded. Its campaign was
abandoned before it started, and the human wants to iterate on this work directly.

## Goals / Non-goals

**Goals**
- A navigation organised around use, in this order: Start here → Ideas to specs → The
  agentic loop → Everyday use → Concepts → Reference (capabilities and commands) →
  Contribute.
- A ground-up bootstrap guide: install, configure, register a project, capture and explore
  a first idea, write and review a spec, queue it, and watch it run.
- Ideas-to-specs pages covering the idea lifecycle, capture, exploration, and writing a spec
  that the loop can build well. Each page has diagrams and concrete steps.
- Agentic-loop pages covering:
  - how it works (a block diagram, and the journey of one campaign);
  - its principles;
  - the full state machine;
  - convergence and escalation mechanisms (including rework and planned divergence
    detection);
  - day-to-day operation (park reasons and what to do, costs, merges and re-exec, working
    outside the queue).
- Mermaid diagrams throughout: flowcharts, state diagrams and sequence diagrams.

**Non-goals**
- Rewriting every capability page (they move under Reference unchanged, apart from links).
- New CLI behaviour.
- Documenting unshipped features as shipped. The idea-to-spec queue phase (SPEC-0208..0217)
  and divergence detection (IDEA-089) appear as "in progress" or "planned".

## Decision

Restructure `mkdocs.yml` nav and add two new sections, `ideas/` and `loop/`, plus a bootstrap
guide. Keep existing page paths (tests pin them), reposition them in the nav, and rewrite the
home page around the two pillars. Use Mermaid, which is already enabled through
`pymdownx.superfences`. A new test pins the structure and keeps the state and park-reason
docs in sync with the code.

## Design

- **Pages (new):**
  - `guides/bootstrap.md`;
  - `ideas/index.md`, `ideas/capture.md`, `ideas/explore.md`, `ideas/spec.md`;
  - `loop/index.md`, `loop/principles.md`, `loop/states.md`, `loop/convergence.md`,
    `loop/operate.md`.
- **Pages (rewritten):** `index.md`, which keeps the phrases pinned by
  `test_docs_product_ia.py`.
- **Nav:**
  - Home;
  - Start here (Bootstrap, Getting started);
  - Ideas to specs (four pages);
  - The agentic loop (five pages);
  - Everyday use (daily/weekly ops, track time, multi-project, outbound, org storage, agent
    surface, idea → ship);
  - Concepts;
  - Reference (capabilities index and pages, command reference);
  - Contribute.
- **Diagrams:** each `ideas/*` and `loop/*` page has at least one ```mermaid block.
  `loop/states.md` has a `stateDiagram-v2` naming every state in `wt.agentic.queue.STATES`,
  and `loop/operate.md` has a table naming every reason in `queue.PARK_REASONS`.
- **Test:** `tests/test_docs_two_pillars.py` checks:
  - the new pages exist and appear in `mkdocs.yml`'s nav;
  - each has a mermaid block;
  - every state and park reason in the code appears on its page;
  - the home page links both pillars.
- **README:** links the bootstrap guide.

## Alternatives considered

- **A refresh through the queue (SPEC-0218)** — rejected by the user: the content needs
  human iteration.
- **Static images for diagrams** — they're hard to keep current, whereas Mermaid lives in the
  markdown.

## Acceptance criteria

- [ ] Every new page exists, is in the nav, and is not a stub (more than 1500 characters).
- [ ] Each `ideas/*` and `loop/*` page has at least one mermaid diagram.
- [ ] `loop/states.md` names every state in `queue.STATES`, and `loop/operate.md` every
      reason in `queue.PARK_REASONS` (enforced by the test).
- [ ] The home page presents the two pillars and links `ideas/index.md`, `loop/index.md` and
      `guides/bootstrap.md`.
- [ ] `mkdocs build --strict` passes, and the existing doc tests pass.
- [ ] `uv run pytest` passes; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_docs_two_pillars.py` (above); the existing `test_docs_*`;
  `test_docs_mkdocs_build.py` (strict build).
- **Manual verification:** the human reviews the rendered site locally (`mkdocs serve`) and
  signs off on the structure and the diagrams.
- **Regression guard:** full `uv run pytest`.

## Rollout / migration

Built directly on branch `docs-rework`, previewed with the human, and merged through the
conductor as a docs-only change.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

(none)
