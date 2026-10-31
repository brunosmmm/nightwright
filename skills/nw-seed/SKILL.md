---
name: nw-seed
description: "Distill pre-existing conversation context into a durable nw idea (title + optional Summary/Log). Use when a chat already shaped a thought and you need it parked without retyping a one-liner."
trigger: seeding an idea from conversation context; inverse of nw-capture
---

# nw-seed

## When to use

You've **already discussed** something in this session and want it parked as an idea — not a
stray one-liner you already know how to phrase. That is the inverse of `/nw-capture` (thought
→ idea). If you only have a short unformed thought, use `nw-capture` instead. If an idea id
already exists and needs more research, use `/nw-explore`.

## Procedure

1. **Confirm intent** — seed from this conversation, or the topic named in `$ARGUMENTS`.
2. **Optional related check:** `/nw-related` (`nw ideas --query "…" --json`) if the topic may
   already be parked; see `nw ideas --help`.
3. **Multi-project gate (SPEC-0095 / SPEC-0137):** scan the chat for ≥2 project names or repo
   paths. Run `nw projects --json` and match them. If ≥2 hits (or one hit + an unmatched path),
   **preview a project map** before any Summary: which umbrella epic, which child per
   `:PROJECT:`, which paths are unknown. For unknown paths: propose
   `nw projects add NAME --repo PATH` (show dry-run), get agreement, then `--yes` — do **not**
   hand-edit `config.yaml` or invent entries silently. Fan-out shape = epic + `seed-children`
   after accept. Soft-warn if the human insists on a single project anyway. Breakdown bullets
   use `(project: Name)` when children span projects (SPEC-0097).
4. **Draft** — one-sentence title; optional project/tags/priority only from **explicit** cues
   or the confirmed project map (do not invent associations). Exact flags: `nw idea --help`.
5. **Preview** — show the proposed title, flags, project map (if multi), and any Summary /
   Open questions / Log notes you would write. Wait for confirm or edit. **No silent write.**
   **No Summary write until the project map is confirmed** when the gate fired.
6. **Capture** — `nw idea "<title>"` with the chosen flags. Prefer `--state INCUBATE` when
   step 7 will also write enrichment. For multi-project: capture the **umbrella** first
   (Meta-Tools or the owning product), note child projects in Summary/questions; promote as
   epic later and `nw spec seed-children`.
7. **Optionally enrich** (recommended when the chat already did explore-quality thinking):
   - `nw idea summary <ID> --set "…"` — current understanding from the conversation
   - `nw idea questions <ID> --set "…"` or `--add "…"` — open questions already raised
   - `nw idea log <ID> --note "…"` — one or more Log findings already decided (date+time stamp)
   Exact flags: `nw idea summary --help`, `nw idea questions --help`, `nw idea log --help`.
8. **Stop** — `nw idea show <ID>`. Point at `nw next`, `/nw-explore`, or `/nw-new-work` as
   appropriate. **Do not** promote or generate.

## Conventions / guardrails

- Do not invent requirements or scope not present in the conversation.
- Prefer short titles; put substance in Summary / Log.
- Write enrichment bodies as **org-mode markup** (not Markdown): ~code~, =verbatim=,
  *bold*, /italic/, plain `-` lists. `nw idea summary|explore` also normalizes common
  Markdown idioms on write (SPEC-0030). Quote identifiers, paths and code in ~…~ / =…=
  (left untouched, so `__init__` stays intact); write multi-line code as `#+begin_src`
  (SPEC-0164).
- Prefer `nw idea log` (not the hidden `explore` alias) when appending Log notes (SPEC-0031).
- Never `nw spec new`, never set a spec to `accepted`, never `nw spec generate`.
- Title-only seed is valid when the chat was thin; Summary is recommended, not required.
- Sibling skills: `/nw-capture` (one-liner), `/nw-explore` (enrich existing id).
- **SPEC-0138:** if Superpowers brainstorming ran, park decisions here (or `/nw-new-work`) —
  do **not** write a governing design under `docs/superpowers/specs` or `plans`.

## Verify

`nw idea show <ID>` reflects the previewed content. `nw ideas` lists it. Do not run promote.
