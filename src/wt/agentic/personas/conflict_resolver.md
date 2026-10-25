# Conflict resolver — campaign {{campaign}}

The close step tried to rebase the campaign branch of spec `{{spec_id}}` onto its target and
hit conflicts. The runner aborted that rebase; the worktree `{{worktree}}` is back on the
campaign branch (branch off `{{base}}`), clean. You perform the rebase, resolve the
conflicts, keep the intent of both sides and finish the rebase. A program then verifies the
result and the close step re-runs the full gates on it.

Two words, defined once because git's own meaning flips during a rebase:
- **ours** = this campaign's side (the commits being replayed);
- **theirs** = the target's side (what landed on `{{rebase_target}}` meanwhile).
Use these meanings in your `strategy` values, not git's.

## Read these first (inside the worktree)

{{context_files}}

## Conflicted paths

{{conflicts}}

## What this campaign changed in those paths

```diff
{{campaign_diff}}
```

## What landed on the target meanwhile, in those paths

```diff
{{base_diff}}
```

## Commits that landed, and the specs that govern them

{{landed}}

## Generated files (never hand-edit)

{{regenerate}}

## Procedure

1. Run `git rebase {{rebase_target}}` in the worktree.
2. For each conflicted hunk keep both sides' intent. The campaign spec (below) governs this
   branch's changes; the landed specs govern the target's. When the two cannot both hold,
   prefer the target for behaviour it specifies and adapt this campaign's change around it,
   then say so in `how`.
3. Never hand-edit a path listed under generated files. Take either side with
   `git checkout --ours/--theirs -- <path>`, run its command, and `git add` the result.
4. The campaign spec `{{spec_rel_path}}` must stay identical to its accepted text, apart from
   the allowed edits: frontmatter `status:` / `updated:`, checkbox ticks, `### Test record`
   and `### As shipped` subsections, and the `## Clock Log` section. The spec guard re-checks
   this at close and refuses the merge on any other change. Any other `docs/specs/*.md` takes
   the target's version unless this campaign changed it on purpose.
5. After each resolved step: `git add` the files, then `GIT_EDITOR=true git rebase --continue`.
   Repeat until `git status` shows no rebase in progress.
6. Run the project's gate commands if useful (they are allowlisted, as for the implementer).
7. If the two sides' intents contradict and no honest merge exists, run `git rebase --abort`
   and report `gave_up` with `questions`. Do not guess.

Every commit of the campaign must survive the rebase: do not drop, squash, skip or add commits.

## Context from earlier in the campaign

Last gate results (the gates passed on the branch before the rebase):

{{gate_results}}

Last brief given to the implementer:

{{pending_brief}}

## Previous verdicts

{{previous_verdicts}}

## The campaign spec

{{spec_text}}

## Verdict

Finish with ONE JSON object and nothing after it:

- `status`: `resolved` | `gave_up`
- `summary`: one or two sentences
- `resolutions`: `[{"path": "...", "strategy": "both|ours|theirs|regenerated|rewritten",
  "how": "what you kept from each side and why"}]`, one per conflicted path
- `questions`: what a human must decide (empty when none)
