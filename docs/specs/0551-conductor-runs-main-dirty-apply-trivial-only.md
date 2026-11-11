---
id: SPEC-0551
title: Conductor runs main-dirty-apply --trivial-only for the trivial class
status: done
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, conductor, launch]
depends_on: [SPEC-0550]
supersedes: [SPEC-0544]
source_idea: IDEA-1159
---

## Context

IDEA-1159 asks the conductor to commit a trivial stray edit (frontmatter, docs prose, comments)
that blocks closes, and to escalate only what needs the human. The human said yes to the
conductor clearing `main_dirty` through one audited verb, `nw queue main-dirty-apply` (campaign
`IDEA-1159-20261110-02ed`, resume note seq 437), and at ESC-0067 chose to split
[SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md). The steward's part C,
[SPEC-0544](./0544-conductor-runs-main-dirty-apply-for-trivial.md), was split per its spec
review into the guard ([SPEC-0550](./0550-conductor-git-guard-hook.md)) and this grant.

The chain, in queue order: [SPEC-0546](./0546-main-dirty-plan-classifier.md) (classifier),
[SPEC-0547](./0547-main-dirty-conductor-owned-plan-and-escalate.md) (owner and escalation),
[SPEC-0548](./0548-main-dirty-apply-core-commit.md) (verb core, conductor denied),
[SPEC-0549](./0549-main-dirty-apply-wip-save-and-hold.md) (verb WIP save),
[SPEC-0550](./0550-conductor-git-guard-hook.md) (git-guard hook), **SPEC-0551 (this spec,
last)**. The conductor gains its only `main_dirty` write here, and only after SPEC-0550's hook
is merged (`depends_on`), so every other main-checkout write stays denied to it.

**Carries SPEC-0544 AC1, AC2, AC8, AC9, AC10, AC11** (as AC1–AC6). It folds in the SPEC-0544
review's nits: the allow rule is documentary, the new skill step is numbered `2b` so SPEC-0547's
steps keep their numbers.

## Goals / Non-goals

**Goals**

- The conductor clears the trivial class (a live `commit` plan) with one audited, flagged verb,
  `nw queue main-dirty-apply --trivial-only`, and mails the owner.
- Every other class (`save_branch`, `branch_and_escalate`, `hold`, `escalate`, `resume`)
  escalates as SPEC-0547 says.
- No other main-checkout write opens to the conductor.

**Non-goals**

- **Deliberate, partial delivery of IDEA-1159.** The idea says "else save to a branch; escalate
  only for substantive code edits", and never to leave `main_dirty` as a human-only park. This
  spec does not do that: the conductor saves no WIP branch and commits via no sub-agent. The
  `save_branch` class escalates to a `human_checkout` record and the WIP save stays with the
  human or the steward (SPEC-0548, SPEC-0549). The human approved this at ESC-0067.
- The git-guard hook and its install (SPEC-0550).
- Any steward rule change: no change to the steward's allow or deny lists, to
  `.claude/settings.json` allow rules (SPEC-0496), or to what the SPEC-0496 dispatch paragraph
  tells sub-agents to run.
- A `git switch <base>` allow for the conductor; the trivial path never switches.

## Decision

1. Add `--trivial-only` to `main-dirty-apply`. After SPEC-0548 guards 1–4 (through the plan
   check), the verb refuses with no write unless the live action is `commit`.
2. Replace SPEC-0548's two blanket conductor denies of the verb with static denies of the bare
   form and of every form whose first argument is an option other than `--trivial-only`
   (anchored on `uv run nw` / `nw`, with each root-group option as an optional lead).
   SPEC-0550's hook step 7 repeats the check on the parsed command, so the hook backs up any
   other spelling.
3. The conductor's `parked.main_dirty` row gains step `2b`: on a live `commit` plan it runs the
   flagged verb, mails the owner, and resumes. Every other class escalates as SPEC-0547 says.
4. `CONDUCTOR_CURSOR_POLICY` states the rule for the cursor runner, which has no hooks.
5. The skill rows this spec edits besides `parked.main_dirty`: `parked.main_not_on_base`
   (escalate `human_checkout`, the human resumes the campaign), `wt_broken` (mail the steward
   or escalate `human_checkout`), and the SPEC-0496 permission-denial paragraph (`git -C
   <worktree>` forms, short commit message).

## Design

### The flag

`apply_clear(root, plan=None, *, trivial_only=False)` and CLI `--trivial-only`. Guard order with
the flag: SPEC-0548 guards 1–4 run first (lock, re-plan, guard 3 in-progress, guard 4 plan
check). **After guard 4 and before guards 5–7** (`bad path`, `partially staged`, and SPEC-0549's
guard 7 for the WIP actions: project config, project name, base ref), a live action other than
`commit` returns `ok: false`, `error: not trivial: <action>`, and the class's `escalate`
(SPEC-0548), and writes nothing. Guard 3 still wins first: a live merge or sequencer returns
`<in_progress> in progress` with `escalate: true`, with or without the flag. So the `not
trivial` refusal is reachable for `save_branch`, `branch_and_escalate`, `hold` and `resume`, and
the conductor-run flag never runs the WIP location guards. A live `commit` goes on through
guards 5–7 and commits as SPEC-0548. Without the flag the verb is unchanged.

### Launch (`src/nightwright/agentic/launch.py`), static rules

- **Allow:** add `Bash(uv run nw queue main-dirty-apply --trivial-only *)` to
  `conductor_allowed_tools`. It is documentary: `Bash(uv run nw *)` already allows the form; what
  grants it is removing SPEC-0548's blanket denies. `_CONDUCTOR_WIP_VERBS` and
  `_CONDUCTOR_GIT_VERBS` are unchanged.
- **Verb deny (conductor-only):** SPEC-0548's `Bash(uv run nw * main-dirty-apply*)` and
  `Bash(nw * main-dirty-apply*)` are removed (they would also deny the allowed form: deny wins).
  The new denies are **anchored**; no `*` sits between `nw` and `queue`. A `* queue
  main-dirty-apply` form would also span a quoted argument (`decide` lets `*` match spaces, deny
  wins), so it would deny the conductor's own SPEC-0547 step-4 command `uv run nw queue
  escalate … --fact 'nw queue main-dirty-apply --root <R>' … --yes` (and `--fact
  'main-dirty-apply: <error>'` from step 2b) and the conductor could not file the escalation
  under dontAsk. The anchored forms do not match it, because `escalate` follows `uv run nw
  queue `. For each prefix `uv run nw` and `nw`, and each root-group lead of `` (nothing),
  `--split `, `-h ` and `--help ` (today's options of the `cli` group in `cli.py`), deny
  `<prefix> <lead>queue main-dirty-apply` (bare) and one `<prefix> <lead>queue main-dirty-apply
  <tail>` per option of the verb other than `--trivial-only`: today ` --root*`, ` --plan*`,
  ` --base*`, ` --json*`, ` --help*`, ` -h*`. None of these matches `uv run nw queue
  main-dirty-apply --trivial-only …`. SPEC-0550's hook step 7 is the only layer for any other
  spelling (extra whitespace, `--root=M` as a lead, an option the group gains later). A launch
  test introspects the click command and fails when an option of `queue main-dirty-apply` has no
  deny tail, and introspects the root group and fails when one of its options has no lead.
- **Unchanged:** `DEFAULTS["disallowed_tools"]` (push deny, `_steward_guard`,
  `_conductor_guard`, `gh pr merge`) and the steward's rules stay byte-identical. No rule
  contains a project path or a base name.
- **`CONDUCTOR_CURSOR_POLICY`** replaces SPEC-0548's sentence with: "A main_dirty clear of the
  trivial class runs `uv run nw queue main-dirty-apply --trivial-only`; never run it otherwise.
  Never write the main checkout with raw git (add, commit, rm, checkout, switch, restore, merge,
  rebase, reset, mv, cherry-pick, revert, stash, apply, am, hash-object, update-index, worktree
  add outside `.nw-worktrees/`) or cp; in a worktree under `.nw-worktrees/` add and commit as
  usual. No reset --hard, clean, branch -D, or push to the base."

### Skill (`skills/nw-conduct/SKILL.md`)

- **`parked.main_dirty` row, new step `2b`** between SPEC-0547's steps 2 and 3. If the live
  plan's `action` is `commit`:
  - Run `uv run nw queue main-dirty-apply --trivial-only --root <root> --base <base> --json` in
    the conductor's own context, once per (root, base) group (step 1's grouping): one command,
    no sub-agent, one per root at a time. This runs even when an open `human_checkout` record
    exists for the root; on `ok: true` find that record with `nw escalations list --json` (the
    open `human_checkout` record whose facts contain exactly `root: <root>`, as in step 3) and
    withdraw it (`nw escalations withdraw <id> --reason "cleared main (trivial commit)" --yes`).
  - On `ok: true`, mail `nw mailbox send --from conductor --to steward:<Project> --kind info`
    (`<Project>` is the campaign's project) with the `sha` and the paths, or `--to human` when
    the `stewards` list of `nw queue conduct-status --json` has no `address` equal to
    `steward:<Project>` (a steward is the owner only while its mailbox follower is held). Then
    run step 2's resume command (it carries `--to CLOSING --by conductor --yes`) for each
    non-terminal `main_dirty` campaign of the (root, base) group, with
    `--decision "cleared main (commit, <sha>)" --rule parked.main_dirty`.
  - On `error: not trivial: resume`, the checkout became clean meanwhile: go to step 2.
  - On any other `ok: false`, do not retry. Go on to step 3 / 4 with an extra
    `--fact 'main-dirty-apply: <error>'`.

  Every other action goes straight to step 3. Steps 1, 3, 4 and 5 are unchanged.
- **`parked.main_not_on_base` row:** the conductor does not move dirt or switch branches in the
  main checkout. It runs `main-dirty-plan` and escalates `human_checkout` (no campaign, `--yes`)
  with the class and `recommended`, as in SPEC-0547 step 4. After the answer, the human resumes
  the campaign (`nw queue resume`); the conductor does not (the park stays human-owned,
  SPEC-0547 AC1, and step 5 resumes only `main_dirty` campaigns).
- **`wt_broken` row:** mail the steward (or escalate `human_checkout`) to save the tracked dirt
  to a WIP branch and restore main (the steward runs the audited verb). Use
  `uv run nw` from a worktree meanwhile. No raw git write in the main checkout.
- **SPEC-0496 paragraphs, amended.** The dispatch paragraph keeps telling sub-agents to run
  plain single `git add <paths>` / `git commit -m …` in their worktree. The permission-denial
  paragraph today says "run the blocked plain command yourself in that worktree"; the
  conductor's cwd is not that worktree, so it becomes: "run it yourself as
  `git -C <worktree> add -- <paths>` / `git -C <worktree> commit -m …` (the forms the conductor
  allow-list permits), never in the main checkout: the conductor-git-guard hook denies raw git
  writes there. Use a short message without the words `add`, `commit`, `fetch`, `rebase`,
  `restore`, `checkout`, `rm`, `hook`, `config`: the unchanged `_steward_guard` rules deny a
  `-C` command that contains them (`*` matches spaces, deny wins), so `git -C <worktree> commit
  -m "feat: add x"` is denied".

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

- `src/nightwright/agentic/launch.py`: only the `CONDUCTOR_CURSOR_POLICY` wording, as a source.
  **Not** `_CONDUCTOR_WRITE_DENY`, `_conductor_write_guard`, the removal of
  `_CONDUCTOR_WIP_VERBS`, `_branch_ref_ok`, `_conductor_project_bases`, or the `switch <base>`
  allow.
- `tests/test_agentic_launch.py`: `test_push_guard_rule_count` and the steward swallow-family
  tests, as the steward-unchanged regression guard.
- `tests/test_main_dirty_clear.py`: `test_conductor_cursor_policy_allows_clear` and
  `test_skill_row_phrases`, rewritten to this row.

## Alternatives considered

- **Let the conductor run the verb for every class.** The human approved escalating substantive
  edits in their checkout; non-trivial saves move their work to a branch, and that stays with
  the human or the steward.
- **Enforce trivial-only by an env var the verb reads.** The cursor runner launches with no env,
  and the check would be invisible in the permission list. A flag plus deny globs is visible and
  testable with the real binary.
- **A second verb (`main-dirty-commit`).** The human approved a single audited verb.
- **Commit via a sub-agent, else save to a branch** (IDEA-1159's wording). Replaced on purpose:
  the human approved the single audited verb (seq 437) and, at ESC-0067, the split that leaves
  WIP saves with the human or the steward.

## Acceptance criteria

"Permitted" means SPEC-0550's helper `tests/_permission_rules.py::decide` returns `allow` on the
effective rules of `build_plan(cfg, "conductor")`.

- [x] AC1 (SPEC-0544 AC1) Guard order and refusals of `main-dirty-apply --trivial-only`, in the
      stated order (guards 1–4, then the `not trivial` check, then guards 5–7):
      - For each live action `save_branch`, `branch_and_escalate`, `hold`, `resume`: `ok: false`,
        `error: not trivial: <action>` and SPEC-0548's `escalate` for the class; nothing is
        written (HEAD, index bytes, worktree bytes and the branch list are unchanged). The
        fixtures for the save actions are otherwise valid (symlink-free, fully staged, project
        config present). One more case per save action (`save_branch`, `branch_and_escalate`,
        `hold`) has a deliberately bad path (guard 5), a partially staged index (guard 6), or a
        missing project config (guard 7), and still returns `not trivial: <action>`, proving the
        flag skips guards 5–7 for non-`commit` actions.
      - For a live merge or sequencer (live action `escalate`): the result is SPEC-0548 guard 3's
        `<in_progress> in progress`, `escalate: true`, no write, with and without
        `--trivial-only`. `escalate` is not in the `not trivial` list.
      - On a live `commit` (trivial diff) the flagged verb commits exactly as SPEC-0548 AC2.
- [x] AC2 (SPEC-0544 AC2) Static rules and hook, with the anchored deny set of Design / Launch:
      - `decide` permits `uv run nw queue main-dirty-apply --trivial-only --root /r --json`, and
        denies `uv run nw queue main-dirty-apply --root /r --json`, the bare form, `--plan f`,
        `--base x`, `--json`, `-h`, the same forms through `nw`, and
        `uv run nw --split queue main-dirty-apply --root /r` (also with `-h ` and `--help ` as
        the lead). A test introspects the click command and fails when an option of
        `queue main-dirty-apply` other than `--trivial-only` has no deny tail, and
        introspects the root group and fails when one of its options has no lead.
      - `decide` PERMITS the SPEC-0547 step-4 command
        `uv run nw queue escalate … --fact 'nw queue main-dirty-apply --root /r' … --yes` and the
        same command with `--fact 'main-dirty-apply: <error>'` (the test builds the conductor
        rules from `build_plan(cfg, "conductor")` and feeds the command through `decide`).
      - `decide` denies `git -C W commit -m "feat: add x"` (a guarded word) and permits
        `git -C W commit -m "x"` (SPEC-0550 AC1).
      - The SPEC-0550 hook, fed through its `main` with a registered conductor session, is
        **silent** for `uv run nw queue main-dirty-apply --trivial-only --root M` and for the
        escalate command above (its `--fact` text contains `main-dirty-apply --root`), and
        **denies** `uv run nw queue main-dirty-apply --root M` (the same verb without
        `--trivial-only`), `uv run nw --split queue main-dirty-apply --root M`, and
        `uv run nw queue main-dirty-apply --root=M`.
- [x] AC3 (SPEC-0544 AC8) No conductor static rule contains a project root path or a base
      branch name: a launch with two projects whose roots and bases differ yields identical rule
      lists. `DEFAULTS["disallowed_tools"]`, `_CONDUCTOR_WIP_VERBS`, and the steward's effective
      rules equal a snapshot taken before this spec. The steward still denies
      `git -C /w rm -f --ignore-unmatch -- f "x log y"`.
- [x] AC4 (SPEC-0544 AC9) `CONDUCTOR_CURSOR_POLICY` names `main-dirty-apply --trivial-only` and
      the main checkout write ban, says worktree add/commit is allowed, and no longer contains
      "Do not run `nw queue main-dirty-apply`."
- [x] AC5 (SPEC-0544 AC10) The `parked.main_dirty` row, extracted by rule id, contains a step
      `2b` between steps 2 and 3 with `main-dirty-apply --trivial-only`, `--from conductor`,
      `steward:<Project>`, `conduct-status`, `--to human`, `--to CLOSING`,
      `nw escalations list --json`, `cleared main (commit`, `not trivial: resume`,
      `escalations withdraw`, `--rule parked.main_dirty`, and the `main-dirty-apply: <error>`
      fact. Every `main-dirty-apply` command in the skill is followed by `--trivial-only` (regex
      `main-dirty-apply(?!:)(?! --trivial-only)` finds nothing; the fact label
      `main-dirty-apply: <error>` is exempt). The row contains none of `git add`, `git commit`,
      `git checkout`, `git rm`, `git worktree add`. The `parked.main_not_on_base` row contains
      `human_checkout`, `nw queue resume` and says the conductor does not resume; the
      `wt_broken` row contains `steward` (and `human_checkout`); neither has a raw git write.
      The permission-denial paragraph contains `git -C <worktree> add -- <paths>`,
      `never in the main checkout` and the guarded-word list (`add`, `commit`, `fetch`,
      `rebase`, `restore`, `checkout`, `rm`, `hook`, `config`); the dispatch paragraph keeps its
      plain `git add <paths>` / `git commit -m` wording.
- [x] AC6 (SPEC-0544 AC11) Inherited assertions this spec edits, and only these: SPEC-0547 AC5's
      forbidden list loses `main-dirty-apply` (the row may name the `--trivial-only` form);
      SPEC-0548 AC7's two blanket deny rules are replaced by AC2's rule set (with the four named
      `tests/test_agentic_launch.py` sites updated to it) and its "`nw-conduct` never names
      `main-dirty-apply`" is replaced by AC5's regex; SPEC-0548 AC7's cursor-policy sentence is
      replaced by AC4. SPEC-0547's steps 1 and 3–5, its question, options and `--fact` text are
      byte-identical.

## Test plan

- **Automated tests:**
  - `tests/test_main_dirty_apply.py` (SPEC-0548) covers AC1, including the bad-path /
    partially-staged / missing-project-config cases and the live merge case. It already has its
    one speed-tier marker; no second tier is added.
  - `tests/test_agentic_launch.py` covers AC2, AC3 and AC4: the static `decide` cases, the
    SPEC-0547 step-4 escalate command (allow), the guarded-word `git -C` case, the snapshot, the
    two-project launch, the two introspection tests, and the SPEC-0550 hook cases (silent /
    deny) fed through the hook's `main` with a registered conductor session. It replaces
    SPEC-0548's AC7 deny test. The module keeps its existing marker; no second tier is added.
  - `tests/test_main_dirty_plan.py`: its skill-row test (SPEC-0547 AC5) drops `main-dirty-apply`
    from the forbidden list and gains AC5; SPEC-0548's conduct-skill assertion is replaced by
    AC5's regex (AC6).
- **Regression guard:** `tests/test_claude_settings_allowlist.py` (SPEC-0496) and
  `tests/test_conductor_git_guard.py` (SPEC-0550) pass unchanged. Also
  `python3 tools/affected_tests.py --run uv run pytest -q`, `python3 tools/test_tiers.py --check`
  (exits 0 when `tools/test_tiers.py` exists), `python3 tools/spec_lint.py`.
- **Manual verification (real binary):** with the SPEC-0550 hook installed, run the real
  `claude -p` once as a registered conductor session with the plan's `--allowedTools` /
  `--disallowedTools` and `--permission-mode dontAsk`, on a scratch repo under the scratchpad
  with a frontmatter-only edit on main. Have it run
  `uv run nw queue main-dirty-apply --root <main> --json` (expect denied), then
  `uv run nw queue main-dirty-apply --trivial-only --root <main> --json` (expect `ok: true`, one
  commit with the SPEC-0548 subject). Then a logic edit in `src/x.py` with `--trivial-only` →
  `ok: false`, `not trivial: branch_and_escalate`, nothing changed. Also have it run
  `uv run nw queue escalate … --fact 'nw queue main-dirty-apply --root <main>' … --yes` on the
  scratch queue (expect allowed). Record each result in As shipped.

### Test record (2026-11-11)

- `uv run pytest -q tests/test_agentic_launch.py tests/test_main_dirty_apply.py
  tests/test_main_dirty_plan.py tests/test_skills.py tests/test_claude_settings_allowlist.py
  tests/test_conductor_git_guard.py`: all pass. New cases: AC1 in `test_main_dirty_apply.py`
  (`test_trivial_only_*`, `test_cli_trivial_only_flag`); AC2-AC4 in `test_agentic_launch.py`
  (`test_conductor_verb_rules_permit_only_the_flagged_form`,
  `test_conductor_guarded_word_in_a_git_c_message_is_denied`,
  `test_verb_denies_cover_every_option_of_the_verb_and_the_root_group`,
  `test_hook_is_silent_for_the_flagged_verb_and_denies_the_rest`,
  `test_verb_rules_snapshot_and_two_projects_launch_the_same_rules`,
  `test_conductor_cursor_policy_names_the_flagged_verb`); AC5/AC6 in `test_main_dirty_plan.py`
  (`test_skill_step_2b_runs_the_flagged_verb`, the two row tests) and the regex in
  `test_main_dirty_apply.py::test_skills_name_the_verb_for_the_steward_only`.
- `python3 tools/test_tiers.py --check` exits 0; `python3 tools/spec_lint.py` exits 0.
- Real binary (scratch repo under the campaign tmp dir, `claude -p` on the plan's
  `--allowedTools` / `--disallowedTools`, `dontAsk`, `uv` shimmed to this worktree):
  1. frontmatter edit, `uv run nw queue main-dirty-apply --root <m> --json`: DENIED by the
     permission check; `--trivial-only --root <m> --json`: ran, `ok: true`, one commit
     `chore: land stray trivial main-checkout edits`, tree clean.
  2. logic edit in `src/x.py`, `--trivial-only`: ran, `ok: false`,
     `error: not trivial: branch_and_escalate`, `escalate: true`, nothing changed.
  3. `uv run nw queue escalate --topic human_checkout ... --fact 'nw queue main-dirty-apply
     --root <m>' --yes` on the scratch queue: PERMITTED, `ESC-0001` opened.
  4. With the SPEC-0550 hook installed through `--settings` and a registered conductor
     session: the flagged verb on a fresh frontmatter edit was PERMITTED (`ok: true`, second
     commit), and `git -C <m> add -A` was DENIED by the hook (`no raw git writes in the main
     checkout`).
- `python3 tools/affected_tests.py --exclude-marks=integration,tui,remote,perf --run uv run
  pytest -q -x -n 3` (the queue gate's form): 3265 passed, 1 skipped. The unrestricted affected
  run includes the TUI modules and exceeded the 10-minute tool limit, so it was left to gating,
  as was the full suite.

### As shipped (2026-11-11)

- As designed: `apply_clear(..., trivial_only=)` and `--trivial-only` (the check sits after
  guard 4, before guards 5-7); anchored verb denies (`_verb_deny()` in `launch.py`: 2 prefixes
  x 4 leads x bare + 6 tails = 56 rules) and the documentary allow; the cursor policy; step
  `2b` and the three other skill edits; docs (`operate.md`, `agentic.md`, `reference/index.md`).
- Deviation: SPEC-0547 AC5's test `test_skill_main_not_on_base_row_is_the_old_row_with_two_cells_edited`
  pinned the old `main_not_on_base` text; Decision item 5 rewrites that row, so the test was
  replaced by `test_skill_main_not_on_base_row_escalates_and_the_human_resumes` (AC5). AC6's
  list of edited inherited assertions did not name it.
- The `Do not edit the main checkout; write only under .nw-worktrees/` bullet of the cursor
  policy is kept; the old `Git: ... add -A, commit -m` bullet is folded into the new
  sentence.
- The snapshot of AC3 is a hash of `DEFAULTS` allow/steward-allow/disallow lists,
  `_CONDUCTOR_WIP_VERBS` and `STEWARD_CURSOR_POLICY` taken before the edit (the steward's
  effective rules derive only from those); the test also denies
  `git -C /w rm -f --ignore-unmatch -- f "x log y"` for the steward.

## Rollout / migration

Ships after SPEC-0550. A conductor launched after the deploy gets the new verb rules; a running
one keeps its old flags (SPEC-0548's blanket deny) until it is relaunched (`launch_hash`
changes). Worktree sub-agent commits are unaffected. IDEA-1159 is delivered when this spec is
done. Queued under project Meta-Tools.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed, including the real-binary check; affected tests pass.
- [x] No regressions in launch, settings-allowlist, skills, git-guard and main-dirty tests.
- [x] `docs/product/loop/operate.md` and `docs/product/capabilities/agentic.md` say the
      conductor clears the trivial class with `--trivial-only` and escalates the rest;
      `docs/product/reference/index.md` documents the `--trivial-only` flag on the verb.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
