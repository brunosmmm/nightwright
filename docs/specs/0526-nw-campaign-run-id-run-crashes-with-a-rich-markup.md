---
id: SPEC-0526
title: "nw campaign run renders verdict text that contains brackets"
status: accepted
owner: user
created: 2026-11-09
updated: 2026-11-09
kind: feature
source_idea: IDEA-1128
tags: [agentic, campaign, cli]
---

## Context

`nw campaign run <campaign> <run>` prints one persona run through `_print_run` in
`src/nightwright/agentic/cli_campaign.py` (the human view added by
[SPEC-0199](./0199-wt-campaign-runs-and-run.md)). The header is one Rich markup string:

```python
console.print(f"[bold]{rec['run_id']}[/]  {rec['campaign']}  "
              f"[{_VERDICT_STYLES.get(vs, '')}]{vs}[/]  ({rec['source']}, {rec['status']})",
              highlight=False)
```

`vs` is `verdict_status`: the string form of the verdict's `status`, `disposition`, or
`verdict` field (`runs.verdict_status`). Persona verdicts are free text. Rich parses `[…]`
in that string as markup. A closing tag that matches nothing, such as `blocked [/bold]`,
raises `rich.errors.MarkupError` and the command dies with a traceback. A tag Rich accepts,
such as `[x]`, is consumed and the bracketed text never appears.

The same format always emits `[{style}]{vs}[/]` even when `vs` is absent from
`_VERDICT_STYLES` (`done` / `pass` / `approve` green, `blocked` / `fail` / `escalate` red,
`invalid` and `—` dim). The style is then the empty string, so the markup is `[]{vs}[/]`.
Rich raises `MarkupError: closing tag '[/]' has nothing to close`. That is every run whose
status is outside the map, including the `fix` and `?` readings SPEC-0199 already defines.

`kv()` in the same function prints `f"  [dim]{key:<14}[/] {value}"` with markup on. Verdict
`summary`, `must_fix` items, `blockers`, `result_tail`, the permission-denial JSON, and path
values all go through it, so a summary of `see [/dim]` crashes the same way. stderr lines
already use `rich.text.Text` and do not.

This is the crash seen on `nw campaign run` for IDEA-1020 run 025, IDEA-1070 runs 006 and
007, and IDEA-1040 run 025. `--json` does not call `_print_run`. The `nw campaign runs`
table already puts the verdict in `Text(vs, style=…)`.

[SPEC-0265](./0265-operator-surfaces-survive-bad-data-and-never.md) fixed the same class of
bug in `wt spec review --show` by wrapping stored text in `rich.markup.escape` and leaving
the structural tags in place.

## Goals / Non-goals

**Goals**

- `nw campaign run` exits 0 when the verdict, its summary, must_fix, blockers, or the error
  tail contains brackets, and the human output shows that text with the brackets intact.
- A verdict status with no entry in `_VERDICT_STYLES`, including `fix` and `?`, prints and
  does not raise `MarkupError`.
- Statuses that do have a style stay colored. The bold run id and the dim field labels stay.
- `--json` still returns the record unchanged.

**Non-goals**

- Changing `verdict_status` or adding colors for `fix` and `?`.
- `nw campaign runs`. Its verdict cell is already `Text`. Other columns of that table are
  left as they are.
- `nw campaign show`, `log`, `note`, and `tail`.
- The `--json` schema (`nw.campaign.run.v1`).

## Decision

`_print_run` keeps its own markup tags and passes every record string through
`rich.markup.escape` before it is placed inside a markup string. The verdict color tags are
emitted only when `_VERDICT_STYLES` has an entry for that status. `markup=False` on the
whole line is not used: it would drop the bold id, the dim labels, and the verdict color
SPEC-0199 specifies.

## Design

All of this is in `_print_run` (`src/nightwright/agentic/cli_campaign.py`). Import
`escape` from `rich.markup`, the same helper `src/nightwright/cli.py` already uses.

**Header.** Read `style = _VERDICT_STYLES.get(vs, "")`. The status fragment is
`f"[{style}]{escape(vs)}[/]"` when `style` is non-empty, and `escape(vs)` when it is empty.
Escape `run_id`, `campaign`, `source`, and `status` the same way, each wrapped in `str`
before `escape`. The surrounding tags stay literal: `[bold]…[/]` around the run id, and
`({source}, {status})` around the two escaped tokens. `highlight=False` stays.

`source` and `status` are the fixed tokens from `runs._record` today (`ledger+run.json`,
`ledger`, `run.json`, `files`; `running`, `orphan`, `done`). They do not contain brackets.
They are escaped because they sit in the same markup string as the verdict.

**`kv()`.** Escape the value, not the key:

```python
console.print(f"  [dim]{key:<14}[/] {escape(str(value))}", highlight=False)
```

Keys stay the function's own labels (`persona`, `started`, `exit`, `cost`, `summary`,
`must_fix`, `blockers`, `error tail`, `denials`, and the path keys `dir`, `prompt`, `raw`,
`stderr`, `run_json`, `stream`). Callers keep building the value as they do now, including
`json.dumps` for a non-string must_fix item and for permission denials. `escape` runs on
that finished string, so a `[` inside the JSON is printed as a bracket.

**stderr.** The literal `  [dim]stderr tail[/]` line and the per-line `Text("    " + line)`
stay. `Text` does not parse markup; those lines need no `escape`.

**Unchanged.** `--json` returns before `_print_run`. `_runs_table` is not edited.

## Alternatives considered

- **`markup=False` on the header and on `kv()`.** This cannot crash, and the bracketed text
  prints. It also drops the bold run id, the dim labels, and the verdict color. Escape keeps
  those tags and neutralizes only the record text.
- **Build the header as `rich.text.Text` spans.** Same visible result as escape, and it is
  what the stderr lines already do. Escape matches the `_print_review` fix in SPEC-0265 and
  leaves the existing f-string structure in place.
- **Strip `[` and `]` from the verdict.** The text the persona wrote would change. `must_fix`
  items and summaries use brackets on purpose.

## Acceptance criteria

- [ ] `nw campaign run C R` on a run whose `verdict.status` is `blocked [/bold] keep [x]`,
      whose `summary` is `see [x] and [/dim]`, with `must_fix` `["fix [link]"]`, `blockers`
      `["wait [/on] review"]`, and `result_tail` `err [/red]`, exits 0. The human output
      contains the status `blocked [/bold] keep [x]`, the summary `see [x] and [/dim]`,
      the must_fix item `fix [link]`, the blocker `wait [/on] review`, and the error tail
      `err [/red]`, each with its brackets intact, and it contains no `MarkupError` and no
      traceback.
- [ ] The same command on a run with `disposition: fix` (`verdict_status` `fix`) and on a run
      with an empty verdict dict (`verdict_status` `?`) exits 0, and the output contains
      `fix` and `?`.
- [ ] A mapped status still prints: the existing human views that show `done`, `pass`, and
      `blocked` still contain those words.
- [ ] `--json` for that run returns `run.verdict.status` and `run.verdict.summary` equal
      to those two strings, `run.must_fix` equal to `["fix [link]"]`, `run.blockers` equal
      to `["wait [/on] review"]`, and `run.result_tail` equal to `err [/red]`, with no
      backslash escapes added.
- [ ] A stderr line `boom [/dim]` printed by the human view appears in full.

## Test plan

- **Automated tests:** extend `tests/test_agentic_campaign_runs.py`, using the existing
  `merged` fixture, `add_run`, and `run_cli` from `test_agentic_e2e`.
  - `test_run_human_view_prints_bracketed_verdict`: `add_run` a `060-implementer` `run.end`
    with `verdict_ok: True`, `result_tail: "err [/red]"`, and `verdict: {"status":
    "blocked [/bold] keep [x]", "summary": "see [x] and [/dim]", "must_fix": ["fix [link]"],
    "blockers": ["wait [/on] review"]}`. Write `stderr.log` with a final line `boom [/dim]`.
    `run_cli(…, ["campaign", "run", cid, "060"])` exits 0 and `r.exception` is `None`.
    Assert the five printed strings (`blocked [/bold] keep [x]`, `see [x] and [/dim]`,
    `fix [link]`, `wait [/on] review`, and `err [/red]` — the item text `_print_run` emits,
    not the JSON array literals) and `boom [/dim]` are substrings of `r.output`.
  - `test_run_human_view_prints_unstyled_verdict_status`: two further runs, one
    `{"disposition": "fix"}` and one `{}`, both with `verdict_ok: True`. Each human
    `campaign run` exits 0 and the output contains `fix` or `?`.
  - `test_run_json_keeps_bracketed_verdict`: `campaign run … --json` on `060` parses.
    `run.verdict.status` and `run.verdict.summary` equal those two strings, `run.must_fix`
    equals `["fix [link]"]`, `run.blockers` equals `["wait [/on] review"]`, and
    `run.result_tail` equals `err [/red]`, with no added escapes.
- **Manual verification:** where those campaigns are still on disk, `nw campaign run
  IDEA-1020 025`, `nw campaign run IDEA-1070 006`, `nw campaign run IDEA-1070 007`, and
  `nw campaign run IDEA-1040 025` each exit 0 and print the verdict. The automated test is
  the proof when those runs are gone.
- **Regression guard:** the existing cases in `tests/test_agentic_campaign_runs.py`, in
  particular `test_human_views_render` and `test_run_detail_must_fix_and_blockers` (`done`,
  `pass`, `blocked`, summary `stuck`). Run that file with `uv run pytest -q
  tests/test_agentic_campaign_runs.py`. `python3 tools/spec_lint.py` exits 0 once the ledger
  is regenerated.

## Rollout / migration

No data move and no config change. The next `nw campaign run` on an existing campaign uses
the new printer. `--json` consumers see the same document as before.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; `uv run pytest -q tests/test_agentic_campaign_runs.py` passes, and
      the manual commands above were run when those campaigns were still present.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

- None.
