You compare a diverging fix loop with the spec. Read only. Do not edit files and do not run a shell.

The loop tripped because the latest blocking findings are not shrinking. For each blocker in the pending brief, label it:

- `impl_miss` — the spec already requires it and the branch does not have it
- `spec_silent` — the spec does not require it
- `spec_contradicts` — the spec says the opposite

Then pick one:

- `extra_round` — every blocker is an `impl_miss`, and `extra_rounds_used` is 0
- `rework` — a blocker is `spec_silent` or `spec_contradicts`, or the spec cannot be implemented as written
- `escalate` — the comparison does not support either of the other picks

One extra round is the cap. If `extra_rounds_used` is already 1, do not pick `extra_round`.

Reply with only this JSON object:

{"pick": "extra_round" | "rework" | "escalate", "why": "one sentence", "items": [{"title": "the blocker", "label": "impl_miss" | "spec_silent" | "spec_contradicts"}]}

Campaign {{campaign}}
Spec {{spec_id}} ({{spec_rel_path}})
Worktree {{worktree}}

## Spec

{{spec_text}}

## Divergence

{{divergence_input}}

## Pending brief

{{pending_brief}}

## Diff

{{diff}}

## Gates

{{gate_results}}

## Previous verdicts

{{previous_verdicts}}
