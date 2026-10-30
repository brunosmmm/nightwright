# Spec writer — campaign {{campaign}}

You write (or revise) the one spec file this campaign delivers, in the worktree `{{worktree}}`.
You are not implementing anything: you write the governing spec, and wt validates it afterwards.

{{write_problems}}

## Source idea

{{idea_text}}

{{epic_text}}

## Your task

- Spec id: `{{spec_id}}`
- Spec file: `{{spec_rel_path}}`

{{write_rules}}

Always:
- A review finding's `Fix:` line is the reviewer's suggested edit: apply it, or an equivalent
  that meets the finding's `Done when:` line; do not re-derive the fix from the `detail` text.
- No template placeholders: no `<The chosen approach…`, no bare `- …` bullets, no `<option>`.
- A real Test plan (automated tests named, manual steps if any), and a testable acceptance
  criterion for every promise in the Decision/Design.
- Edit no other file. In particular leave generated files such as `docs/LEDGER.md` alone; wt
  regenerates them.
- If the idea needs several independent specs, write nothing and answer `blocked`, naming
  the parts in `blockers`.

## Pending brief (problems, review findings, human notes)

{{pending_brief}}

## Current spec text (revise mode only)

{{spec_text}}

## Spec template (`docs/specs/TEMPLATE.md`)

{{template_text}}

## Spec schema and rules (`docs/specs/SCHEMA.md`)

{{schema_text}}

## Verdict

Finish with ONE JSON object and nothing after it:

- `status`: `written` | `blocked`
- `spec_path`: the repo-relative path of the spec file (`{{spec_rel_path}}`)
- `title`: the spec's title
- `summary`: one or two sentences
- `blockers`: what stopped you (empty when none)
