---
id: SPEC-0272
title: "wt idea refuses verb-like one-word captures; persona runs cannot write the org"
status: in-progress
kind: feature
owner: user
created: 2026-10-27
source_idea: IDEA-381
tags: [ideas, cli, agentic, safety]
---

## Context

Promoted from idea `IDEA-381` (wt idea <subcommand-like word> silently captures junk ideas
('wt idea list' made 5 empty ideas); campaign agents write to the live ideas file).

On 2026-10-27 five empty ideas titled `list` (IDEA-367..371) appeared in the live ideas file
while campaigns 0265/0268/0269/0270/0271 ran: a persona ran `wt idea list` five times. Two
separate defects, both still present on main:

1. **Verb-like captures.** `IdeaGroup.resolve_command` (`src/wt/cli.py`) routes any first
   positional that is not a registered subcommand to `capture`. `wt idea` has no `list`
   subcommand (listing is `wt ideas`), so `wt idea list` calls `idea_capture_cmd` with
   `text=("list",)` and `add_idea` appends a headline `list`. Nothing checks for a lone word
   that is obviously a command typo.
2. **Persona runs reach the human's org.** `personas.build_request`
   (`src/wt/agentic/personas.py`) passes `WT_CONFIG_DIR` and `WT_DATA_DIR` of the live
   install into every persona's env (`runner._child_env` merges `req.env` over
   `os.environ`). Every org write funnels through `org_write.org_writer` (lock decorator) and
   `org_write._atomic_backup_write` (the single file-write primitive, also used by
   `explore.py` and `idea_ext.py`), and neither knows it is inside a campaign agent. Agents
   are meant to report out-of-scope findings via `wt campaign note` or the verdict's
   `followup_ideas` (`personas/implementer.md`), which the runner files itself
   (`agentic/followups.py`, called from the runner process, not the persona).

## Goals / Non-goals

**Goals**
- `wt idea <one wt verb>` refuses with a hint instead of capturing junk; `--force` captures.
- Any org write attempted from a persona run (`wt idea …`, `wt ideas … --apply`, `wt add`,
  clock-in/out, …) refuses with one clear message naming the right channel.
- The test suite still passes when itself run inside a persona run (implementers run pytest).

**Non-goals**
- Sandboxing persona config/data dirs (a copied config) — the env flag is enough and keeps
  read-only `wt` commands (`wt ideas`, `wt idea show`) working for agents.
- Guarding non-org writes (campaign ledgers, mailbox) — personas legitimately use
  `wt campaign note`.
- Refusing multi-word captures or arbitrary single words (`wt idea refactor` is a fine idea).

## Decision

- **Verb guard:** in `idea_capture_cmd`, when the joined text is a single token whose
  casefold is in the capture-verb set — the top-level `cli.commands` names plus
  `{list, ls, help, rm, delete, del, new, edit, status, get}` —
  raise `ClickException("refusing to capture the single word 'list' as an idea — did you
  mean `wt ideas`? (pass --force to capture it anyway)")`. A `--force` flag bypasses it. The
  hint maps `list`/`ls` → `wt ideas`, anything else → `wt idea --help`. `idea_group`
  subcommand names (`show`, `log`, `state`, …) are not in the set: `IdeaGroup.resolve_command`
  dispatches them to their subcommand before `capture` runs, so the guard never sees them.
  `--force` is independent of the existing `--force-idea` (SPEC-0091, which only silences the
  `--kind bug` routing note): neither implies the other, and the help text of each says so.
- **Persona guard:** `personas.build_request` adds `WT_PERSONA_RUN=1` to the persona env.
  `org_write` gains `PersonaOrgWriteRefused(ValueError)` and `refuse_in_persona_run()`,
  called at the top of the `org_writer` wrapper and of `_atomic_backup_write` (defense in
  depth: writers not decorated still hit the primitive). The message: `wt: refusing to
  write the org from a campaign agent (WT_PERSONA_RUN=1); report it with 'wt campaign note'
  or the verdict's followup_ideas instead`. There is no override flag — the runner process
  (which files follow-ups) never has the variable.
- **Clean CLI error:** the `cli` root group uses a `click.Group` subclass whose `invoke`
  converts `PersonaOrgWriteRefused` into `click.ClickException` (exit 1, no traceback), so
  commands that do not already catch `ValueError` also fail cleanly.
- **Suite isolation:** `tests/conftest.py` gains an autouse fixture
  `monkeypatch.delenv("WT_PERSONA_RUN", raising=False)`, so pytest run by an implementer
  (env inherited) behaves as on a human shell; tests opt in with `monkeypatch.setenv`.

## Design

- `src/wt/cli.py`:
  - `_capture_verbs()` computed lazily, since `cli.commands` is complete only after all
    subcommand modules register.
  - `idea_capture_cmd` gets `@click.option("--force", is_flag=True, …)`; the check runs before
    `_warn_bug_as_idea`/`add_idea`. `IdeaGroup.resolve_command` needs no change (`--force`
    is a valueless flag, skipped by the existing `--` branch).
  - `cli` declared with `cls=WtRootGroup`; `WtRootGroup.invoke` wraps `super().invoke` in
    `try/except OW.PersonaOrgWriteRefused as e: raise click.ClickException(str(e))`.
- `src/wt/org_write.py`: `PERSONA_ENV = "WT_PERSONA_RUN"`; `refuse_in_persona_run()` raises
  when `os.environ.get(PERSONA_ENV) == "1"`; called first in `org_writer.wrapper` (before
  taking the lock) and in `_atomic_backup_write`.
- `src/wt/agentic/personas.py:build_request`: `env["WT_PERSONA_RUN"] = "1"`. The spec-review
  `RunRequest` built in `src/wt/agentic/spec_review.py` sets the same. Gate subprocesses
  (`agentic/gates.py`) do **not** get the flag: the runner runs them, not an agent, and gate
  tests rely on the autouse fixture anyway.
- Read paths stay write-free: `wt ideas` (list) and `wt idea show` do no org writes — ideas-file
  rotation (`org_write.maybe_rotate_ideas_file`) runs only from the capture path, and
  `reconcile_ideas` writes only under `wt spec reconcile --apply`. If an
  implementer finds a best-effort write on a read path, it must skip quietly when
  `WT_PERSONA_RUN=1` rather than fail the read.
- `tests/conftest.py`: autouse `_no_persona_run` fixture.

## Alternatives considered

- **Sandbox `WT_CONFIG_DIR`/`WT_DATA_DIR` per run** — copying config is heavy, breaks
  read-only lookups agents need (`wt idea show`, `wt campaign show`), and the data dir holds
  the campaign ledger the persona must write notes to.
- **Guard only `wt idea` CLI commands** — misses `wt add`, clock-in, `wt ideas
  group-followups --apply` and direct library calls; the write primitive is the one choke
  point.
- **Refuse every single-word capture** — rejects real one-word ideas.

## Acceptance criteria

- [ ] `wt idea list` (and `ls`, `help`, `delete`, `status`, any `wt` top-level command name such
      as `campaign`) exits 1 with the refusal + hint, and the ideas file is byte-identical
      afterwards.
- [ ] `wt idea list --force` and `wt idea refactor` capture normally.
- [ ] With `WT_PERSONA_RUN=1`, `wt idea some text`, `wt idea log IDEA-x --note n`,
      `wt idea clock-in IDEA-x` and `wt add t` each exit 1 with the "campaign agent" message
      (no traceback) and leave the org files and backup dir unchanged.
- [ ] With `WT_PERSONA_RUN=1`, read-only `wt ideas` and `wt idea show IDEA-x` still succeed.
- [ ] `personas.build_request` and the spec-review request put `WT_PERSONA_RUN=1` in
      `req.env`; the runner process's own follow-up filing is unaffected.
- [ ] `uv run pytest` passes both normally and with `WT_PERSONA_RUN=1` exported.

## Test plan

- **Automated tests** (`tests/test_idea_capture_guard.py`, new):
  - `test_verb_capture_refused` — parametrized over `list`, `LS`, `help`, `delete`, `status`, `campaign`:
    CliRunner `["idea", w]` exits 1, output contains `--force`, ideas file unchanged.
  - `test_list_hint_points_to_wt_ideas` — output for `list` contains `wt ideas`.
  - `test_force_and_real_word_capture` — `["idea", "list", "--force"]` and
    `["idea", "refactor"]` both create a headline.
  - `test_persona_run_refuses_org_writes` — `monkeypatch.setenv("WT_PERSONA_RUN", "1")`;
    capture, `idea log`, `idea clock-in`, `add` exit 1, output contains `wt campaign note`,
    no `Traceback`, org file bytes and `org-backups/` listing unchanged.
  - `test_persona_run_allows_reads` — same env, `["ideas"]` and `["idea", "show", id]` exit 0.
  - `test_atomic_write_primitive_refuses` — direct `OW._atomic_backup_write` under the env
    raises `PersonaOrgWriteRefused` and the target file is not created.
  - `test_build_request_sets_persona_flag` (in `tests/test_agentic_personas.py`) —
    `build_request(...).env["WT_PERSONA_RUN"] == "1"`.
- **Sabotage check:** delete the `refuse_in_persona_run()` calls from `_atomic_backup_write`
  and from the `org_writer` wrapper → `test_persona_run_refuses_org_writes` and
  `test_atomic_write_primitive_refuses` fail; remove the verb check → the verb tests fail.
  Restore.
- **Manual verification:** `WT_PERSONA_RUN=1 uv run pytest -q` passes (autouse fixture), and
  in a scratch config `uv run wt idea list` prints the refusal.
- **Regression guard:** full `uv run pytest`; `tests/test_org_write_race.py` (every writer
  decorated) still passes.

## Rollout / migration

Ships with the code; no data migration. Persona prompts need no change (they already say
to use `followup_ideas`).

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

None.
