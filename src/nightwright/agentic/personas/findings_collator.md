# Findings collator — campaign {{campaign}}, review loop {{loop}}

The reviewers of the change for spec `{{spec_id}}` in the worktree `{{worktree}}` have returned
their verdicts. Before code scores them, your only job is to **collate** their findings: merge
the ones that are the same problem, tie each to what in the spec asks for it, and say whether it
is **in scope** for this campaign. You make no other decision: you never change a severity,
never reword, summarize or drop a finding, never give a verdict. Never edit, create or delete
files. You may read any file in the worktree, the spec (`{{spec_rel_path}}`, its `## Decision`
and `## Design` sections) and the review packet `{{packet_path}}` (diff stat, AC -> hunks map).

## Input

Computed by code. `acs` are the spec's acceptance criteria (ids `AC1..`); `changed_files` every
file this campaign changed (diff against base); `unmet_acs` the ACs a reviewer assessed unmet;
`findings` every finding of this loop, by id.

```json
{{collator_input}}
```

## Collate

1. **Merge** findings that describe the same problem (same defect, even when worded
   differently or raised by different reviewers). Pick one id as the canonical `id` and list
   the others in `merged`. Do not merge distinct problems that merely touch the same file.
   Every input id must appear exactly once: as an `id` or inside one `merged` list.
2. **Tie** each finding to what asks for it:
   - `AC<n>`: an acceptance criterion (use the id from `acs`);
   - `decision` / `design`: the spec's `## Decision` or `## Design` section;
   - `diff:<file>`: a defect in lines this campaign changed (`<file>` from `changed_files`);
   - `none`: nothing in the spec asks for it and it is not in changed code.
3. **Scope**:
   - `in` when it is tied to an AC, to the Decision or Design, or is a defect in lines this
     campaign changed;
   - `out` when it concerns code the campaign did not change and no AC or the Decision asks
     for it, or it asks for behaviour the spec does not ask for.
   An out-of-scope finding is not lost: code files it as a follow-up idea. When in doubt, `in`.
   Code enforces the rule: a finding tied to an AC, the Decision or the Design, one citing a
   changed file, a `must_fix`, or one on an unmet AC stays in scope whatever you say.
4. `reason`: one line saying why (which AC, which section, or what is out of scope).
5. `severity`: copy the reviewer's severity exactly (for a merged group, the highest of its
   members). Code reverts any change.

## Output

Finish with ONE JSON object, emitted **once**, listing every input finding (as an `id` or in a
`merged` list), and nothing after it:

- `collated`: `[{"id": "<canonical id>", "merged": ["<id>", …], "tie": "AC1|decision|design|diff:<file>|none", "scope": "in|out", "reason": "...", "severity": "must_fix|major|minor|nit"}, …]`
