---
name: nw-implement-spec
description: "Implement an exported nightwright portable spec in the current (target) repo: status, AC, Test plan, no silent Design drift. Use when building work described by docs/specs/<OUTBOUND-ID>-*.md dropped by nw spec export. For post-hoc DoD audit (any repo), use /nw-verify."
trigger: implementing an exported / portable nightwright outbound spec in a foreign repo
---

# nw-implement-spec

## When to use

You are in a **target repo** (not the nightwright checkout) that has a **portable spec** exported by
`nw` — typically `docs/specs/<OUTBOUND-ID>-*.md` with a paired `.contract.md`, or frontmatter
`source_outbound_id`. The human wants that slice **implemented** and closed honestly.

Do **not** use this for authxxing new nw ideas/specs (`nw-new-work`) or for internal
`SPEC-NNNN` work inside the nightwright repo (`AGENTS.md` + `/nw-verify` there). Do **not** run
`nw spec generate` on outbound / portable ids.

**Post-hoc verify only** (no new build): use **`/nw-verify`** (Procedure B — portable). That
skill is global; it is the outbound close-the-loop playbook. This skill stays the
**implement** path.

## Procedure

1. **Resolve** the portable file: `docs/specs/<ID>-*.md` (or the path the human gave). Read
   the whole spec **and** the paired `.contract.md` if present.
2. **Gate on status:** proceed if `accepted` or `in-progress`. If `done`, stop unless the
   human asked to rework. If `draft`/`proposed`, the export was premature — ask before coding.
3. Set portable frontmatter `status: in-progress` and refresh `updated` (YYYY-MM-DD). There is
   **no** nightwright ledger in this repo.
4. **Clock in** (SPEC-0060/0062): append `CLOCK-IN: [timestamp]` to the spec's `## Clock Log`
   section (add the section if the portable file predates it) when you start working this
   session. This is a plain markdown edit — no `nw` command runs here, since this repo has no
   org access; a nightwright-side `pull-clock` step reconciles it later. Timestamp format: ISO 8601
   `YYYY-MM-DD HH:MM` (brackets optional), e.g. `[2026-08-05 19:30]` — **not** org-mode's
   `YYYY-MM-DD Day HH:MM` weekday form.
5. **Implement** to Decision / Design. Prefer the smallest change that meets Acceptance
   criteria. Match the target repo’s existing style and tools.
6. **Execute the Test plan** in the spec (automated commands + any manual checks). Keep
   pre-existing tests green.
7. Check off each Acceptance criterion that is actually true. Set `status: done` **only**
   when AC and the Test plan are honestly complete; update the spec body if Design drifted.
8. **Clock out**: append the matching `CLOCK-OUT: [timestamp]` to `## Clock Log` when you
   pause or finish this session — every clock-in gets a clock-out before you stop.
9. **Close the loop:** run **`/nw-verify`** (portable branch) on this id before treating the
   slice as finished. Optionally note that the nightwright authxx may later run
   `nw spec pull-status <OUTBOUND-ID>` / `nw spec fit-log` (`nw <cmd> --help`).

## Conventions / guardrails

- **Portable file = working copy** for build progress (`status` + AC checkboxes).
- **Clock every session** (SPEC-0060/0062): a `CLOCK-IN` with no matching `CLOCK-OUT` left at
  the end of a session is a bug in your own bookkeeping — always close it before stopping.
- **Never** `nw spec generate` on outbound ids; never invent org tasks / a nightwright ledger here.
  Do not use `nw spec verify` on outbound ids — `/nw-verify` Procedure B is file-based.
- **Outcome bar (SPEC-0048):** keep at least one outcome-shaped AC (“I can …”) or `outcome:`
  on the portable file when you edit it.
- **Never** silently diverge from Design — edit the portable spec (or record the deviation
  where the human asked) when reality changes.
- If this repo has its **own** stronger spec-first workflow, fold into that workflow but still
  honor this portable file’s AC, Test plan, and `status`.
- Exact `nw` flags (if any): `nw <cmd> --help` — do not hardcode flag lists in this skill.
- **SPEC-0138:** Superpowers TDD/debug may help *inside* this slice; claiming finished still
  requires `/nw-verify` (and later `pull-status` on the nightwright side). Do not mark the portable
  `done` from Superpowers “verification-before-completion” alone.
- **No Superpowers-only ship:** If the only design artifact is under `docs/superpowers/**`
  and there is no `docs/specs/<OUTBOUND-ID>-*.md` portable for this slice, **stop** — return
  to nightwright (`/nw-explore` → `/nw-new-work` → export) before writing feature code. Do not keep
  the linked idea at `INCUBATE` while calling Superpowers notes “implemented.”

## Verify

Hand off to `/nw-verify` (portable branch): `status` is `done`, AC checked and true, Test
plan executed, Clock Log has no open CLOCK-IN, body matches what shipped.
