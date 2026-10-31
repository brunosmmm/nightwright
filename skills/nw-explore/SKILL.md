---
name: nw-explore
description: "Durably enrich an idea (Summary / open questions / log) without promoting it to a spec. Use when an idea needs learning before any accepted design."
trigger: exploring or incubating an idea, enriching IDEA-NNN, not ready to promote
---

# nw-explore

## When to use

An idea needs **durable learning** before any governing spec: research, poke at code, sharpen
the problem. Free chat alone loses knowledge; this skill writes onto the idea itself and
**never** promotes.

If the idea is already explored (non-empty Summary) and you want an accepted spec, use
`nw-new-work` instead. If no idea exists yet and the chat already shaped the thought, use
`/nw-seed` first (conversation → idea), then continue here if more research is needed.

## Procedure

1. Triage: `nw ideas` / `nw next` — pick an `IDEA`/`INCUBATE` without a ripe Summary.
2. Read what you have: `nw idea show <IDEA-ID>` (and `--json` when scripting). **SPEC-0042:**
   the show payload includes `research` (`root`, `source`, optional `note` / `spec_dir` /
   `scheme`). If `research.root` is set, **analyze that tree first** (outbound:
   `outbox_targets[project].repo_path`; Meta-Tools: this nightwright checkout). If `root` is null, do
   **not** invent a path — use conversation + the `note`, or register via
   `nw projects add NAME --repo PATH` (dry-run, then `--yes` after human OK; SPEC-0137).
3. **Multi-project gate (SPEC-0095 / SPEC-0137):** if the chat (or Summary draft) names ≥2
   tracked projects or repo paths, run `nw projects --json`, **preview a project map**, and
   soft-warn before continuing a single-root explore. Prefer fan-out: umbrella epic +
   per-project children (each with its own `:PROJECT:` / research.root). Paths not in the map
   → warn, propose `nw projects add …`, confirm, `--yes`. **Do not write Summary until the
   project map is confirmed** when this gate fires.
4. Research / think (code under `research.root` when set, docs, conversation). Keep disposition
   questions **scoped per project** (do not mix “which repos?” with “which data model?”).
5. Write back:
   - `nw idea summary <ID> --set "…"` — current understanding (mutable)
   - `nw idea questions <ID> --set "…"` or `--add "…"` — open questions
   - `nw idea log <ID> --note "…"` — append a dated Log finding
   - optional `nw idea retitle <ID> "sharper title"`
6. **Record the pass (SPEC-0081):** `nw idea mark-explored <ID>`. This is the only thing that marks
   an idea as explored — nothing else writes `:EXPLORED:`, so an `INCUBATE` idea without it is
   one that was *seeded* with context but never researched. Repeat explorations increment it.
7. Leave state as `INCUBATE` (log/explore auto-moves `IDEA` → `INCUBATE`). Do **not** promote.

Exact flags: `nw idea --help` and `nw idea <subcommand> --help`.

## Conventions / guardrails

- **Never** `nw spec new`, never set a spec to `accepted`, never `nw spec generate`.
- Capture stays `nw idea "short thought"` or `/nw-seed` from conversation; enrichment is the
  subcommands above.
- Write Summary / questions / Log **bodies as org-mode markup**, not Markdown: use `~code~`
  or `=verbatim=`, `*bold*`, `/italic/`, plain `-` lists. Avoid backtick spans and
  `**double-star**` bold. `nw` normalizes common Markdown idioms on write (SPEC-0030), but
  prefer authxxing org directly. Quote identifiers, paths and code in `~…~` / `=…=` (left
  untouched, so `__init__` stays intact); write multi-line code as `#+begin_src` (SPEC-0164).
- Promotion belongs in `nw-new-work` only after Summary is non-empty.
- See `docs/WORKFLOW.md` (explore stage).
- **SPEC-0138:** durable learning belongs on the idea (Summary/Log), not a governing
  `docs/superpowers/specs` file. Superpowers brainstorming is fine as dialogue; park
  outcomes here.

## Verify

`nw idea show <ID>` shows Summary / questions / Log, and an `explored:` line with the pass count.
`nw next` for that id should suggest `nw spec new --from-idea …` once Summary is non-empty (not
`nw idea log`).
