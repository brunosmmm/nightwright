---
name: nw-capture
description: "Capture a quick idea into nightwright without breaking flow. Use when a thought is a seed for future work (not yet an actionable task) and you want it durably recorded."
trigger: capturing a stray idea/thought while working
---

# nw-capture

## When to use

A thought crosses your mind that's a **seed, not a task** — no clear next action yet, just
worth not losing. If it's already **actionable** (you could do it today as a TODO), use
`nw add` instead (see `AGENTS.md` / `nw add --help`); ideas and tasks are different
lifecycles (SPEC-0091).

If the idea was **already shaped in this conversation** and you need to distill that chat into
a durable idea (title + optional Summary/Log), use `/nw-seed` instead.

## Procedure

1. **Optional related check:** if the thought might already exist, run `/nw-related`
   (`nw ideas --query "…" --json`; see `nw ideas --help`) before capturing.
2. **Multi-project check (SPEC-0095):** if the one-liner clearly spans ≥2 products/repos, prefer
   `/nw-seed` (conversation → project-map preview) over a single bare `nw idea`. Or capture an
   umbrella title and immediately explore with a project map — do not pretend one `:PROJECT:`
   owns the whole stack.
3. Capture it: `nw idea "<the thought>"`. Run `nw idea --help` for the exact flag syntax
   (state override, tags, priority, `--kind`). Prefer `--state INCUBATE` if you know you'll
   explore soon. Use `--kind improvement|chore` when the capture is clearly that shape
   (default `idea`). **`--kind bug`:** prefer `nw add` for actionable defects; if the bug
   needs explore→spec (design/systemic), pass `--force-idea` (or accept the soft warn and
   keep the idea). Kind remains a triage label — same pipeline as ideas (SPEC-0044/0091).
4. Tag conventions: use `--tag` to group related ideas so `nw ideas` triage later is easy.
5. Leave it — **do not** promote yet.

## Conventions / guardrails

- A sentence or two is enough at capture time.
- **Next:** durable enrichment is `/nw-explore` (`nw idea summary|log|…`), not promotion.
- Conversation already rich → `/nw-seed` (preview + optional enrichment in one pass).
- Promotion (`/nw-new-work`) is only after Summary is non-empty and you're ready for an
  accepted governing spec.

## Verify

`nw ideas` shows the new idea. `nw next` will usually say `nw idea log …` until Summary
exists.
