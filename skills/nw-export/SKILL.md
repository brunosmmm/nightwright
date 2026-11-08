---
name: nw-export
description: "File-drop an accepted outbound spec into its target repo. Use when a spec authxxed in the nightwright data-dir outbox (<data_dir>/outbox/<project>/) is ready to land in the actual project it describes work for."
trigger: exporting an outbound spec to its target repo
---

# nw-export

## When to use

You have an **outbound** spec (scaffolded via `nw spec new --target <project>`, living under
`<data_dir>/outbox/<project>/`, default `~/.local/share/nightwright/outbox/<project>/`) that's accepted
and ready to become a real artifact in the target project's own repo.

## Procedure

1. Confirm the outbound spec is accepted and complete (Acceptance criteria + Test plan filled
   in — same bar as an internal spec, see `AGENTS.md`).
2. Check what export schemes exist and what each one requires/emits: `nw spec schemes`.
3. Pick a scheme (or accept the target's configured default) and export:
   `nw spec export <OUTBOUND-ID>`. Run `nw spec export --help` for the exact flag syntax
   (`--scheme`, `--emit`, `--to`, `--force`).
4. Successful export advances the linked idea to **EXPORTED** (not PROMOTED — that is the
   internal generate path). Do **not** run `nw spec generate` on outbound ids.
5. If the destination already has content this export owns, don't blindly pass `--force` —
   understand why it diverged first (see SPEC-0016's non-clobbering guardrail below).

## Conventions / guardrails

- **Non-clobbering / division of labor (SPEC-0016):** export owns specific artifacts (e.g. a
  task-spec file) and only splices into shared ones (e.g. a project plan) — it must not
  silently overwrite content it doesn't own. If `nw spec export` reports a divergence, read the
  message before reaching for `--force`.
- Prefer the target's configured `outbox_targets[project]` scheme/destination over ad hoc
  `--scheme`/`--to` overrides, so repeated exports stay consistent.
- **Portable working copy (SPEC-0041):** the file dropped into the target repo is where the
  foreign agent tracks build (`status` + AC). Prefer `/nw-implement-spec` there for the
  close-the-loop playbook. Optional later: `nw spec pull-status <ID>` to mirror that status
  back into the outbox.
- **Outcome bar (SPEC-0048):** outbound specs need product-fit signal — `outcome:` frontmatter
  or an AC starting with “I can” / “User can” / “Operator can”. The default scheme
  `nw-native` warns when that signal is missing. `--force` strips a `"product-fit outcome"`
  validation problem. It does not apply to that warning. There is no `REMOVED` scheme.
  Built-ins are `nw-native` and `plain-md`. Prefer `/nw-implement-spec` in the target after export.
- **Epic children:** export each accepted child with the project's scheme. SPEC-0047's
  compose adapter is withdrawn. Per-child export is the path. Include outcome AC / `outcome:`
  (SPEC-0048).
- **Fit log (SPEC-0049):** after real use (or before supersede), `nw spec fit-log <ID> --note
  "…"`.

## Verify

Check the destination repo for the dropped artifact(s) and any provenance marker the scheme
writes (e.g. a source-spec reference). Confirm the source idea is **EXPORTED**. Re-running the
same export should be idempotent (and preserve dest `status` when the body is unchanged) or
clearly report the divergence — confirm before trusting the drop.
