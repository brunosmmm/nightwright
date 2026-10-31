---
name: nw-new-work
description: "Post-explore: turn an enriched idea into a lint-clean accepted spec, then optionally generate tasks. Use when Summary is non-empty and you're ready to commit to a governing design."
trigger: promoting an explored idea to an accepted spec
---

# nw-new-work

## When to use

An idea is **explored enough** (non-empty Summary via `/nw-explore`) and you want a governing
**accepted** spec. This is **not** for blank captures or early research — use `nw-capture` then
`nw-explore` first.

This skill only removes *procedural* friction — scaffolding, lint, state transitions. **You
still authxx the spec's substance** (Context/Decision/Design/Acceptance criteria/Test plan).

## Procedure

0. **Gate:** `nw idea show <selector>`. If Summary is empty/(empty), **stop** and run
   `nw-explore` instead. Do not promote.
1. **Related check:** `/nw-related` (`nw ideas --query "…" --json`) so you do not promote a
   twin of an existing open or closed idea. Exact flags: `nw ideas --help`.
2. **Triage:** `nw ideas` / `nw next` — pick an idea whose next hint is promote.
3. **Promote:** `nw spec new --from-idea <selector>`. Run `nw spec new --help` for flags
   (`--epic`, `--target`, `--internal`, `--force`). Empty Summary warns unless `--force`.
4. **Authxx it for real:** before writing Decision/Design, resolve research context the same
   way explore does — `nw idea show <selector> --json` → `research.root` (SPEC-0042); search
   that tree when grounding the design. **Multi-project (SPEC-0095):** if the idea Summary
   names multiple projects, promote as **`kind: epic`** (`--epic`), authxx Breakdown as
   per-project children with `(project: Name)` on each bullet (SPEC-0097), then
   `nw spec seed-children` after accept — do not stuff N repos into one feature Decision.
   Then fill Context (prefilled from idea notes), Decision, Design, Acceptance criteria,
   Test plan per `docs/specs/SCHEMA.md` and `AGENTS.md`.
5. **Lint:** `python3 tools/spec_lint.py` and fix anything it flags.
6. **Accept:** set frontmatter `status: accepted`; for **internal** specs run
   `python3 tools/spec_lint.py --write-ledger`; for outbound, `validate_outbound` /
   outbox INDEX as usual.
7. **Close the source idea's questions (SPEC-0080):** the spec you just accepted decided some
   of the idea's Open questions — resolve those, and say in a `nw idea log` note which spec
   section decided each. Leave genuinely-unsettled ones OPEN.
   `nw idea questions <ID> --resolve 1 --resolve 3` (repeatable) or `--resolve-all`.
   Skipping this is why 18 landed specs left their ideas carrying stale OPEN questions;
   `nw spec reconcile` now lists them.
8. **Epic children (SPEC-0043):** if the accepted spec is `kind: epic`, run
   `nw spec seed-children <SPEC-or-OUTBOUND-ID>` so each Breakdown line becomes an
   INCUBATE idea (`:EPIC:` + `:PROJECT:`). Do this before optional generate/export.
9. **Optionally generate tasks:** `nw spec generate <SPEC-ID>` (`nw-generate`) when ready to
   track delivery (**internal** only — outbound uses export).

## Conventions / guardrails

- **Explore before promote.** Empty Summary → `nw-explore`, not this skill.
- **Scaffold ≠ authxx.** No template placeholders at `accepted`.
- Never silently rewrite a past-`draft` decision after `accepted` — supersede (see `AGENTS.md`).
- Epics need child specs with `parent:` — see `docs/specs/SCHEMA.md`. After accepting an epic,
  `nw spec seed-children <id>` turns Breakdown bullets into child ideas (SPEC-0043);
  optional `(project: Name)` per bullet (SPEC-0097).
- Never hand-edit the generated block in `docs/LEDGER.md`.
- **SPEC-0138:** the governing design is this nw spec (or outbound portable) — never
  `docs/superpowers/specs` / `plans`. If Superpowers brainstorming already ran, authxx into
  the nightwright scaffold; do not treat a Superpowers design doc as accepted authxxity.

## Verify

`python3 tools/spec_lint.py` exits 0, spec `status` is `accepted`, ledger current, AC/Test plan
are real prose (no `<...>` placeholders). `nw idea show <id>` shows no OPEN question the spec
decided, and `nw spec reconcile` does not list the idea under `questions-open`.
