---
name: wt-explore
description: "Durably enrich an idea (Summary / open questions / log) without promoting it to a spec. Use when an idea needs learning before any accepted design."
trigger: exploring or incubating an idea, enriching IDEA-NNN, not ready to promote
---

# wt-explore

## When to use

An idea needs **durable learning** before any governing spec: research, poke at code, sharpen
the problem. Free chat alone loses knowledge; this skill writes onto the idea itself and
**never** promotes.

If the idea is already explored (non-empty Summary) and you want an accepted spec, use
`wt-new-work` instead. If no idea exists yet and the chat already shaped the thought, use
`/wt-seed` first (conversation → idea), then continue here if more research is needed.

## Procedure

1. Triage: `wt ideas` / `wt next` — pick an `IDEA`/`INCUBATE` without a ripe Summary.
2. Read what you have: `wt idea show <IDEA-ID>` (and `--json` when scripting). **SPEC-0042:**
   the show payload includes `research` (`root`, `source`, optional `note` / `spec_dir` /
   `scheme`). If `research.root` is set, **analyze that tree first** (outbound:
   `outbox_targets[project].repo_path`; Meta-Tools: this wt checkout). If `root` is null, do
   **not** invent a path — use conversation + the `note`, or register via
   `wt projects add NAME --repo PATH` (dry-run, then `--yes` after human OK; SPEC-0137).
3. **Multi-project gate (SPEC-0095 / SPEC-0137):** if the chat (or Summary draft) names ≥2
   tracked projects or repo paths, run `wt projects --json`, **preview a project map**, and
   soft-warn before continuing a single-root explore. Prefer fan-out: umbrella epic +
   per-project children (each with its own `:PROJECT:` / research.root). Paths not in the map
   → warn, propose `wt projects add …`, confirm, `--yes`. **Do not write Summary until the
   project map is confirmed** when this gate fires.
4. Research / think (code under `research.root` when set, docs, conversation). Keep disposition
   questions **scoped per project** (do not mix “which repos?” with “which data model?”).
5. Write back:
   - `wt idea summary <ID> --set "…"` — current understanding (mutable)
   - `wt idea questions <ID> --set "…"` or `--add "…"` — open questions
   - `wt idea log <ID> --note "…"` — append a dated Log finding
   - optional `wt idea retitle <ID> "sharper title"`
6. **Record the pass (SPEC-0081):** `wt idea mark-explored <ID>`. This is the only thing that marks
   an idea as explored — nothing else writes `:EXPLORED:`, so an `INCUBATE` idea without it is
   one that was *seeded* with context but never researched. Repeat explorations increment it.
7. Leave state as `INCUBATE` (log/explore auto-moves `IDEA` → `INCUBATE`). Do **not** promote.

Exact flags: `wt idea --help` and `wt idea <subcommand> --help`.

## Conventions / guardrails

- **Never** `wt spec new`, never set a spec to `accepted`, never `wt spec generate`.
- Capture stays `wt idea "short thought"` or `/wt-seed` from conversation; enrichment is the
  subcommands above.
- Write Summary / questions / Log **bodies as org-mode markup**, not Markdown: use `~code~`
  or `=verbatim=`, `*bold*`, `/italic/`, plain `-` lists. Avoid backtick spans and
  `**double-star**` bold. `wt` normalizes common Markdown idioms on write (SPEC-0030), but
  prefer authxxing org directly. Quote identifiers, paths and code in `~…~` / `=…=` (left
  untouched, so `__init__` stays intact); write multi-line code as `#+begin_src` (SPEC-0164).
- Promotion belongs in `wt-new-work` only after Summary is non-empty.
- See `docs/WORKFLOW.md` (explore stage).
- **SPEC-0138:** durable learning belongs on the idea (Summary/Log), not a governing
  `docs/superpowers/specs` file. Superpowers brainstorming is fine as dialogue; park
  outcomes here.

## Verify

`wt idea show <ID>` shows Summary / questions / Log, and an `explored:` line with the pass count.
`wt next` for that id should suggest `wt spec new --from-idea …` once Summary is non-empty (not
`wt idea log`).
