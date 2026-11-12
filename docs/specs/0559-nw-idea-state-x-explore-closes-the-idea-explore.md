---
id: SPEC-0559
title: "nw idea state refuses unknown keywords; nw idea reopen recovers a stranded idea"
status: proposed
owner: user
created: 2026-11-12
source_idea: IDEA-1078
kind: feature
tags: [ideas, cli, lifecycle, bug]
depends_on: [SPEC-0012, SPEC-0135]
---

## Context

`nw idea state <idea> <KEYWORD>` ([SPEC-0135](./0135-shipped-terminal-fix-outbound-reconcile-force.md))
calls `set_idea_state` (`src/nightwright/explore.py:781`). It upper-cases the keyword, calls
`ensure_idea_keyword` (`src/nightwright/org_write.py:609`) and then `set_state`. `ensure_idea_keyword`
appends any keyword that is missing to the done side of the ideas file's `#+TODO` line, so
nothing checks that the keyword belongs to the idea vocabulary (`org_idea_keywords`,
`src/nightwright/config.py:42`).

On 2026-11-09, `nw idea state IDEA-1059 EXPLORE` (and the same on IDEA-1060) therefore:

1. wrote `EXPLORE` as a **done** keyword into the `#+TODO` line of the ideas file;
2. flipped the headline to `EXPLORE`, which stamped `CLOSED:`.

`Task.is_idea` is true only when the state is in `org_idea_keywords` (`load_tasks`,
`src/nightwright/org.py:261`). `EXPLORE` is not in that vocabulary, so the idea stopped being an
idea. `nw idea show`, `log` and `state` (and `ext`, `retitle`, and the other `nw idea` commands
that check `task.is_idea`) all answer `'IDEA-1059' is not an idea`. The idea is stranded:
nothing in the CLI can flip it back to `IDEA`, because the only way back is `nw idea state`,
which now refuses to see it. The human had to hand-edit the org file.

There are two defects: the write path accepts a keyword outside the vocabulary (the cause), and
there is no CLI recovery for an idea already in that state (the stranding).

## Goals / Non-goals

**Goals**
- `nw idea state` (and every other `set_idea_state` caller, including the TUI state picker)
  rejects a keyword that is not in the configured idea vocabulary, before touching any file.
- A new `nw idea reopen <selector>` puts a stranded idea back into an active idea state without
  hand-editing the org file.
- The `is not an idea` error on an idea-shaped entry names the recovery command.

**Non-goals**
- Changing `org_idea_keywords`, or adding `EXPLORE` as an idea keyword. `nw-explore` enriches an
  idea durably and leaves its state alone ([SPEC-0081](./0081-record-exploration-explicitly-on-an-idea-seeded.md)
  tracks passes through `:EXPLORED:`); a state keyword for it is not wanted.
- Cleaning stray keywords already present in existing `#+TODO` lines. Org tolerates them, and
  another headline might still use one.
- Reopening ideas that were archived to `ideas-archive.org` (SPEC-0072 owns that); `reopen`
  works on live ideas files only.

## Decision

Close the cause and add the recovery:

1. `set_idea_state` validates the new keyword against `org_idea_keywords` (both sides of the
   `|`) **before** `ensure_idea_keyword` runs, and raises a `ValueError` listing the valid
   keywords otherwise. Nothing is written on rejection.
2. Add `nw idea reopen <selector> [--state STATE]`, backed by `reopen_idea(cfg, selector,
   state="IDEA")` in `explore.py`. It resolves the selector **without** the `is_idea` check,
   accepts any entry whose `:ID:` matches `IDEA-<digits>`, and flips it to `STATE`, which
   must be on the active (left of `|`) side of `org_idea_keywords`.
3. The `is not an idea` message on an entry with an `IDEA-<n>` `:ID:` and a non-vocabulary
   state says so and names `nw idea reopen`.

## Design

**Validation (`explore.py`).** In `set_idea_state`, after the empty check, split
`cfg["org_idea_keywords"]` with the same helper `load_tasks` uses (`org._split_keywords`,
falling back to the `config.py` default when the key is empty). If `new_state` is not in
`active + done`, raise
`ValueError(f"{new_state!r} is not an idea state; valid: <active> | <done>")`. This happens
before `resolve_selector` writes anything and before `ensure_idea_keyword`, so the `#+TODO` line
is never touched for an unknown keyword. The other callers of `ensure_idea_keyword`
(`specs.py:1415`, `agentic/queue.py:4529`, `export.py:301`, `explore.py:767`) only pass
vocabulary keywords already, so they are unchanged. A custom `org_idea_keywords` that adds a
keyword still works: the vocabulary is the single source.

**Reopen (`explore.py`, `cli.py`).** `reopen_idea(cfg, selector, state="IDEA")`:

1. `state` is upper-cased and must be in the **active** side of `org_idea_keywords`, else
   `ValueError`.
2. `task = resolve_selector(cfg, selector)` (no `is_idea` check). The task's `:ID:` must match
   `^IDEA-\d+$`, else `ValueError("<selector> is not an idea entry")`, so `reopen` cannot
   retarget an ordinary task.
3. If `task.is_idea` and the state is already on the active side, raise
   `ValueError("<id> is already open (<state>)")`. An idea in a done idea state (for example
   `DROPPED`) is reopened normally; reopening an idea that was auto-archived is out of scope
   (non-goals).
4. `ensure_idea_keyword` is not needed (the target is in the vocabulary). Call
   `set_state(cfg, task, state)`. Leaving a done keyword drops the `CLOSED:` stamp
   (`org_write.py:376`). Because `set_state` bumps `:UPDATED:` only when `task.is_idea`, and a
   stranded task is not one, `reopen_idea` also calls `stamp_updated` on the headline in the
   same atomic write, by passing through a small `is_idea`-override argument to `set_state`
   (`touch_updated=True`), rather than writing twice.
5. Returns the file path.

`nw idea reopen <selector> [--state STATE]` in `cli.py` calls it and prints
`✓ reopened IDEA-1059 → IDEA  <path>`, with `ClickException` on `ValueError` like its siblings.
`--state` completes from `complete_idea_state`.

**Hint.** One helper, `not_an_idea_message(task, selector)`, returns
`f"{selector!r} is not an idea"`, plus
`f" (state {task.state!r} is not an idea keyword; recover with `nw idea reopen {task.id}`)"`
when `task.id` matches `^IDEA-\d+$`. The `cli.py` sites that raise `is not an idea` for
`show`/`log`/`state`/`ext`/`retitle`/`kind`/`project` and `set_idea_state` use it.

## Alternatives considered

- **Treat any entry with an `IDEA-n` `:ID:` as an idea in `load_tasks`** — it would make the
  stranded idea visible again, but it changes the model for every consumer (digest, hub, TUI
  filters), and a `done` stray keyword would still hide it from open-idea views. Larger blast
  radius than the bug.
- **Add `EXPLORE` to `org_idea_keywords`** — a state nobody asked for, and it does not stop the
  next typo (`nw idea state X EXPLORED`) from stranding an idea.
- **Validation only, no reopen** — leaves IDEA-1059/1060-style ideas (already stranded, and any
  stranded by a hand-edited keyword) unrecoverable from the CLI.
- **Make `nw idea state` itself accept stranded ideas** — it conflates a force-flip with
  recovery and would let `state` silently skip its guard; a separate, narrow command is clearer.

## Acceptance criteria

- [ ] `nw idea state <idea> EXPLORE` (any keyword outside `org_idea_keywords`) exits non-zero
  with a message listing the valid keywords, and leaves the ideas file byte-identical
  (headline, `CLOSED:` stamp and `#+TODO` line unchanged).
- [ ] `nw idea state` still accepts every configured idea keyword, active and done, including
  one added through a custom `org_idea_keywords`.
- [ ] The TUI state picker path (`tui/model.py:339`) gets the same rejection through
  `set_idea_state`, with no file change.
- [ ] `nw idea reopen <id>` on an idea whose headline state is `EXPLORE` flips it to `IDEA`,
  removes its `CLOSED:` line, bumps `:UPDATED:`, and afterwards `nw idea show`, `log` and `state`
  resolve it as an idea.
- [ ] `nw idea reopen <id> --state INCUBATE` flips it to `INCUBATE`; `--state SHIPPED` (a done
  keyword) and `--state NOPE` are rejected with no file change.
- [ ] `nw idea reopen` on an idea already in an active state exits non-zero ("already open")
  with no file change; on an idea in a done idea state (`DROPPED`) it reopens normally.
- [ ] `nw idea reopen` on an ordinary task (no `IDEA-<n>` `:ID:`) is rejected with no file change.
- [ ] The `is not an idea` error for a stranded idea names `nw idea reopen <id>`; for an
  ordinary task id it is unchanged.
- [ ] `python3 tools/spec_lint.py` exits 0 and `python3 tools/test_tiers.py --check` exits 0.

## Test plan

- **Automated tests:** new `tests/test_idea_reopen.py` with `pytestmark = pytest.mark.fast`
  (name matches no `tui`/`remote`/`integration` rule in the SPEC-0490 taxonomy). It builds a
  temp ideas file and cfg (the pattern in `tests/test_close.py`) and asserts:
  - `set_idea_state(cfg, id, "EXPLORE")` raises `ValueError` and the file bytes are identical;
  - each configured keyword (active and done) is accepted; a custom `org_idea_keywords` adds
    one that is then accepted;
  - a file hand-set to `EXPLORE` (reproducing IDEA-1059): `resolve_selector(...).is_idea` is
    false, `reopen_idea` flips it to `IDEA`, `CLOSED:` is gone, `:UPDATED:` is set, and the
    task then has `is_idea` true;
  - `--state INCUBATE` works; `SHIPPED` and `NOPE` are rejected with no write;
  - already-open and non-`IDEA-<n>` entries are rejected with no write; a `DROPPED` idea reopens;
  - `CliRunner` runs of `nw idea state ... EXPLORE`, `nw idea reopen ...` and `nw idea show` on
    a stranded idea show the exit codes and the `nw idea reopen` hint in the message.
- **Manual verification:** on a scratch copy of the ideas file, `nw idea state IDEA-N EXPLORE`
  must fail; hand-edit a headline to `EXPLORE`, run `nw idea show IDEA-N` (error with hint),
  `nw idea reopen IDEA-N`, then `nw idea show IDEA-N` (resolves). Run this against the real
  `nw` binary, not only the `CliRunner`.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q` plus
  `tests/test_close.py`, `tests/test_reconcile.py`, `tests/test_tui_mutations.py` and
  `tests/test_promote_guard.py` (existing `set_idea_state`/`ensure_idea_keyword` callers) must
  stay green. `python3 tools/test_tiers.py --check` exits 0 with the new module marked.
  `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

1. Land the validation, `reopen_idea`, the CLI command and the hint helper together.
2. Recover the two known stranded ideas with `nw idea reopen IDEA-1059` and
   `nw idea reopen IDEA-1060` (run by the human or the conductor on their own checkout, not by
   the campaign agent).
3. The stray `EXPLORE` token in the ideas file's `#+TODO` line is left in place; it is
   harmless.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and noted here.
- [ ] No regressions (affected tests pass at the close gate).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).
- [ ] `python3 tools/spec_lint.py` exits 0.

## Open questions

- None.
