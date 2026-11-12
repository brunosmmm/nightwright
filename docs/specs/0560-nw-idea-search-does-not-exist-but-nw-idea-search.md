---
id: SPEC-0560
title: "nw idea search is a real subcommand; multi-word captures that start with a search/list verb are refused"
status: accepted
kind: feature
owner: user
created: 2026-11-12
updated: 2026-11-12
source_idea: IDEA-1312
tags: [ideas, cli, safety]
---

## Context

Promoted from idea `IDEA-1312`: `nw idea search TEXT` does not exist, yet it exits 0 and
silently captures `search TEXT` as a new idea. Agent lanes ("Jerome lanes") looking for
related ideas ran it and created `IDEA-1177` and `IDEA-1306`..`IDEA-1308` by mistake.

Why it happens today:

- `IdeaGroup.resolve_command` (`src/nightwright/cli.py:355`) routes every first positional that
  is not a registered `idea` subcommand to `capture`. There is no `search` subcommand.
- [SPEC-0272](./0272-wt-idea-subcommand-like-word-silently-captures.md) added
  `_refuse_verb_capture` (`cli.py`), but it only fires for a **single** word
  (`len(word.split()) != 1` returns early). `nw idea search some text` is several words, so it
  slips through. SPEC-0272 explicitly left multi-word captures alone, which is right in general
  but wrong for text whose first word is the verb the user clearly meant as a command.
- The search the agent wanted exists as `nw ideas --query TEXT`
  ([SPEC-0086](./0086-related-idea-search-cli-skill-over-open-and.md)), but under `nw ideas`,
  not `nw idea`. Both groups are easy to confuse, and `search` is the natural verb.

## Goals / Non-goals

**Goals**
- `nw idea search TEXT…` works: it runs the same related-idea search as
  `nw ideas --query TEXT` (open + closed ideas, heading + Summary/questions/Log), read-only.
- A multi-word `nw idea <verb> …` whose first word is an unambiguous list/search/delete verb that
  has no `idea` subcommand refuses with a hint instead of capturing; `--force` captures anyway.
- Real captures of ordinary text are unchanged.

**Non-goals**
- Refusing every unknown first word: `nw idea refactor the parser` and most multi-word captures
  stay valid (the SPEC-0272 non-goal stands).
- Making the top-level `cli.commands` names or `show`/`status`/`new`/`edit`/`get` refuse when
  followed by more words (`nw idea add retry to the queue` is a plausible real idea).
- Changing the `nw ideas --query` behavior, ranking or JSON schema.
- Changing the persona-run org-write guard.

## Decision

1. Add a read-only `nw idea search TEXT… [--limit N] [--json] [--plain]` subcommand that calls
   the same code path as `nw ideas --query` (`report.ideas(..., query=...)`).
2. Extend the SPEC-0272 verb guard from "single word" to "first word": when the joined capture
   text starts with one of a fixed set of list/search/delete verbs that have no `idea`
   subcommand (`find`, `list`, `ls`, `rm`, `delete`, `del`), refuse with a hint, whether or not
   more words follow. The single-word rule of SPEC-0272 is unchanged. `--force` bypasses both.
   `search` is not in the set because it is now a subcommand and never reaches `capture`.
3. Because `search` becomes a subcommand, a genuine idea that begins with the word `search` is
   captured with `nw idea capture search the ideas file faster`. The `idea` group help says so.

Decided without human input (judgment call, recorded): the multi-word verb set is deliberately
small and excludes words that are common in real idea headlines (`add`, `new`, `status`, `get`,
`edit`, `help`); the cost of a false refusal is one `--force`, the cost of a false capture is a
junk idea in the live file.

## Design

- `src/nightwright/cli.py`:
  - New `@idea_group.command("search")` with `@click.argument("text", nargs=-1, required=True)`
    and options `--limit` (default 25, same as `ideas`), `--json` (`nw.ideas.v1`), `--plain`.
    It joins `text` with spaces and raises `click.BadParameter` (param_hint `TEXT`) when the
    joined text is blank (the analogue of `ideas_list_cmd`'s `--query` blank check, which uses
    param_hint `--query`). It then calls exactly
    `R.ideas(cfg, state=None, all_done=False, as_json=as_json, kind=None, plain=plain,
    query=joined, limit=limit, tag=None, project=None, workstream=None, priority=None,
    sort=None, desc=False, tree=False, epic=None)`. Every unexposed argument is pinned to the
    value `nw ideas --query X` passes by default (the click defaults of `ideas_list_cmd`,
    `cli.py:1315-1376`: `tag`/`kind`/`project`/`epic`/`workstream`/`priority`/`sort`/`state`
    are `None`, `all_done`/`tree`/`desc` are `False`). Because `project` is always `None`,
    the `_warn_unknown_associations` project-warning path is skipped, as it is for
    `nw ideas --query X` without `--project`; `--reindex` is not exposed (it writes). The
    `--limit` help text documents `0 = unlimited` (as `ideas_list_cmd` does). The command
    performs no org write, so it works under `NW_PERSONA_RUN=1` (`PERSONA_ENV`,
    `src/nightwright/org_write.py:70`).
  - `IdeaGroup.resolve_command` needs no change: `search` is in `self.commands`, so it
    dispatches there before `capture`.
  - `_MULTIWORD_VERBS = frozenset({"find", "list", "ls", "rm", "delete", "del"})`.
    `_refuse_verb_capture` keeps its single-word branch and adds: if the first token's casefold
    is in `_MULTIWORD_VERBS` (and there are two or more tokens), raise `ClickException` with
    `refusing to capture '<text>' as an idea — it starts with the command-like word '<verb>';
    did you mean <hint>? (pass --force to capture it anyway)`, where `<hint>` is the literal
    string for the verb:
    - `list`/`ls`/`find` → ``nw idea search TEXT` (related-idea search) or `nw ideas` (list)``
      (both commands appear in the message, each backticked);
    - `rm`/`delete`/`del` → ``nw idea close IDEA-NNN --as drop --reason "…"` (closes an idea;
      `nw idea --help` lists the rest)``. `nw idea close` is the real subcommand
      (`cli.py:802`, `--as drop|researched` required); there is no delete.
  - Guard semantics: it operates on the **joined text** (`" ".join(text)`, whitespace-stripped,
    first token by `split()`), not on the argv shape. So `nw idea "list the ideas"` (one quoted
    argument, several words) refuses exactly like `nw idea list the ideas`. Click options
    (`--tag`, `--project`, …, anywhere on the line) are parsed out before `text` is joined, so
    `nw idea find x --tag foo` refuses the same as `nw idea find x`; the guard sees only
    `find x`. Casing is ignored (`LS`, `Find`). A first word that merely *starts* with a verb
    (`nw idea listing of the queue`, `nw idea delegate the lease`) is not a match: the
    comparison is on the whole first token.
  - `idea_group` docstring gains `nw idea search TEXT   # related-idea search (read-only)` and a
    line `nw idea capture search …   # capture text that starts with a subcommand name`.
  - The single-word hint for `search` itself is no longer reachable (it is a subcommand); a bare
    `nw idea search` with no text fails with click's missing-argument error, exit 2, no capture.
- `skills/nw-related/SKILL.md`: where it tells agents to search with `nw ideas --query`, add
  `nw idea search` as the equivalent spelling (docs only).
- No data or schema changes.

## Alternatives considered

- **Alias only (`search` → `ideas --query`), no guard** — fixes the exact typo but leaves
  `nw idea find X`, `nw idea list X` silently capturing. Lost on coverage.
- **Error on every unknown first word** — breaks bare-text capture, the core `nw idea TEXT`
  contract (SPEC-0024/0031); lost.
- **Guard only, no `search` subcommand** — agents would be told "refused" for the verb they most
  naturally reach for, when the capability already exists; an alias is the better answer.
- **Make `nw ideas search` instead** — `nw ideas` is the list group with `--query`; the
  mistake was made under `nw idea`, so that is where the verb belongs.

## Acceptance criteria

- [ ] `nw idea search <words>` exits 0, prints the same hits as `nw ideas --query "<words>"`
      (open and closed ideas), and the ideas file is byte-identical afterwards.
- [ ] `nw idea search a b --json` emits the same `nw.ideas.v1` document as
      `nw ideas --query "a b" --json`; `--limit N` caps the hits like `ideas --limit`.
- [ ] `nw idea search` with no text and `nw idea search "   "` exit non-zero and create no idea.
- [ ] `nw idea search <words>` with `NW_PERSONA_RUN=1` exits 0 (read-only path) and leaves the
      ideas file byte-identical.
- [ ] `nw idea find x y`, `nw idea list the ideas`, `nw idea LS all`, `nw idea rm IDEA-1 now`,
      `nw idea delete foo bar` and `nw idea del foo bar` each exit 1 with a refusal that names the
      verb and `--force`, and leave the ideas file byte-identical.
- [ ] The quoted single-argument form refuses the same as the unquoted form
      (`nw idea "list the ideas"` and `nw idea list the ideas` both exit 1, no capture), and an
      option on the line does not change that (`nw idea find x --tag foo` exits 1, no capture).
- [ ] The `list`/`ls`/`find` refusals name `nw idea search TEXT` and `nw ideas`; the
      `rm`/`delete`/`del` refusals name `nw idea close` (with `--as drop`), and that command
      exists (`nw idea close --help` exits 0).
- [ ] The same inputs with `--force` capture the full text as one new idea.
- [ ] `nw idea refactor the parser`, `nw idea add retry to the queue`,
      `nw idea status page for the dashboard` and `nw idea capture search the ideas file faster`
      still capture normally.
- [ ] The SPEC-0272 single-word behavior is unchanged (`nw idea list` refuses with the
      `nw ideas` hint; `nw idea list --force` captures).
- [ ] `nw idea --help` lists `search` and mentions `nw idea capture`.

## Test plan

- **Automated tests:**
  - `tests/test_idea_capture_guard.py` (extend; it already carries
    `pytestmark = pytest.mark.fast`, so no second tier): add
    `test_multiword_verb_capture_refused` (parametrized over the inputs above, asserts exit 1,
    verb and `--force` in output, `_ideas_bytes` unchanged),
    `test_multiword_verb_quoted_and_with_options_refused` (`nw idea "list the ideas"`,
    `nw idea find x --tag foo`; exit 1, no capture), `test_multiword_verb_hints` (list/ls/find
    name `nw idea search` and `nw ideas`; rm/delete/del name `nw idea close`),
    `test_multiword_verb_force_captures`, `test_ordinary_multiword_captures_unchanged`
    (`refactor …`, `add …`, `status …`, `listing of the queue`, `capture search …`), and
    `test_single_word_guard_unchanged`.
  - **Replace** the existing `test_multi_word_verb_text_captures` (line 87; it currently asserts
    `nw idea list all the things` exits 0 and captures it, which Decision 2 makes a refusal)
    with `test_multiword_verb_capture_refused`; the capture-of-multiword-verb-text assertion is
    kept only with `--force` (`test_multiword_verb_force_captures`). A scan of `tests/` for
    other multi-word `list`/`ls`/`rm`/`delete`/`del`/`find` captures through the CLI found no
    other (`tests/test_queue_conflict_resolve.py:475` calls the `capture` helper directly with
    "list fm", not the CLI guard); the implementer re-runs that grep and fixes any hit the same
    way. No test may still assert that a multi-word verb-led capture succeeds without `--force`.
  - `tests/test_idea_search.py` (extend; already `pytestmark = pytest.mark.fast`): add
    `test_idea_search_matches_ideas_query` (seed ideas incl. a PROMOTED one; compare `idea search`
    stdout and `--json` payload to `ideas --query`), `test_idea_search_is_read_only` (file bytes
    and `_snapshot` unchanged, also with `NW_PERSONA_RUN=1`), `test_idea_search_limit`, and
    `test_idea_search_requires_text` (no args, blank text: non-zero, no new idea).
  - Both edits reuse the modules' existing `_cfg`/`env` fixtures; no new test module is
    introduced, so no new speed-tier marker is needed. `python3 tools/test_tiers.py --check`
    exits 0.
- **Sabotage check:** drop the first-word branch from `_refuse_verb_capture` →
  `test_multiword_verb_capture_refused` fails; remove the `search` command →
  `test_idea_search_matches_ideas_query` fails and `nw idea search x` captures again. Restore.
- **Manual verification:** in a scratch `NW_CONFIG_DIR`/`NW_DATA_DIR`, run
  `uv run nw idea search queue lease` (hits, no new idea), `uv run nw idea find queue lease`
  (refusal, no new idea), `uv run nw ideas | wc -l` before/after to confirm the count is
  unchanged.
- **Regression guard:** `python3 tools/affected_tests.py --run uv run pytest -q`, plus
  `tests/test_idea_capture_guard.py`, `tests/test_idea_search.py`, `tests/test_capture_routing.py`,
  `tests/test_ideas.py`; `python3 tools/spec_lint.py` exits 0.

## Rollout / migration

Ships with the code. No data migration. The already-created junk ideas (`IDEA-1177`,
`IDEA-1306`..`IDEA-1308`) are closed by the human or conductor as DROPPED; this spec does not
edit the live ideas file.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass; manual steps performed and noted.
- [ ] `python3 tools/test_tiers.py --check` exits 0.
- [ ] No regressions (queue close gate affected tests pass).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
