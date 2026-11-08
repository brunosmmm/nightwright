---
name: nw-rework
description: "Rework a bad governing design (supersede) or extend an incomplete epic (scaffold children). Use when an idea already has :SPEC: and next hints /nw-rework, or the design/breakdown is wrong or unfinished."
trigger: reworking a linked spec, extending an incomplete epic, SPECCED/PROMOTED idea stuck after promote
---

# nw-rework

## When to use

An idea already has a linked `:SPEC:` and either:

- the **design was wrong** (rework / supersede), or
- the linked work is an **incomplete epic** (extend — open Breakdown, missing/incomplete children).

Do **not** use this to silently rewrite a past-`draft` spec. Do **not** use for first-time authxxing (`nw-new-work`) or blank captures (`nw-capture` / `nw-seed`).

## Procedure

0. **Gate:** `nw idea show <ID>` — must show a `:SPEC:` / linked spec id. If none, stop (use
   `nw-explore` / `nw-new-work` instead).
1. **Fork** — ask the user if unclear; do not guess:
   - **Rework** — design is wrong.
   - **Extend** — epic Breakdown / children still open.

### Rework

1. `nw idea log <ID> --note "…"` — why the current design fails (durable). For outbound
   product-fit misses prefer `nw spec fit-log <OUTBOUND-ID> --note "…"` (SPEC-0049) so the
   note is tagged and linked from the outbound id.
2. Authxx a **new** spec that `supersedes` the bad one (same namespace: internal
   `docs/specs/` or outbound outbox). Set the old spec `status: superseded` +
   `superseded_by: <new-id>`. Never silent rewrite past `draft`.
3. Rare restart: `nw spec new --force` only with logged rationale (see `--help`).
4. Bring the new spec to `accepted` (lint / ledger for internal). Then generate/export as usual.

### Extend (`kind: epic`)

1. Read the epic: open Breakdown `- [ ]` items and existing children (`parent: <epic-id>`).
2. Scaffold missing child specs with `parent: <epic-id>` (internal or outbound per routing).
3. Update epic Breakdown checkboxes as children land / are accepted.
4. When a child is `accepted`: `nw spec generate <child-id>` (idempotent; new tasks only) for
   **internal** children. Outbound: do not generate.
5. Outbound: when children are accepted, export each child with the project's scheme
   (default `nw-native`). There is no `REMOVED` epic-compose scheme (SPEC-0047 is withdrawn).
   Include outcome AC / `outcome:` (SPEC-0048).
6. Do **not** mark the epic `done` until children are `done`/`superseded` (existing lint).

Exact flags: `nw spec --help`, `nw idea --help`, `nw idea log --help`.

## Conventions / guardrails

- **Explicit fork** — rework vs extend; do not invent a third path that rewrites history.
- **Supersede, don't mutate** past-draft decisions (`AGENTS.md`).
- Outbound verify includes export after new accepted children.
- Prefer org markup on idea Log notes (SPEC-0030).

## Verify

`nw next` / `nw ideas` for the idea no longer shows bare `nw tasks` while the epic is
incomplete. After a complete epic (no open Breakdown, children done/superseded), next may
show `nw tasks` (or export when still pending). Internal: `python3 tools/spec_lint.py` exits 0.
