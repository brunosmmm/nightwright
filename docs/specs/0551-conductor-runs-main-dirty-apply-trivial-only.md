---
id: SPEC-0551
title: Conductor runs main-dirty-apply --trivial-only for the trivial class
status: accepted
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

## Decision

1. Add `--trivial-only` to `main-dirty-apply`. After all SPEC-0548 guards, the verb refuses
   with no write unless the live action is `commit`.
2. Replace SPEC-0548's two blanket conductor denies of the verb with static denies of every
   form that does not put `--trivial-only` first. SPEC-0550's hook step 7 repeats the check on
   the parsed command, so a form the static tails miss is still denied.
3. The conductor's `parked.main_dirty` row gains step `2b`: on a live `commit` plan it runs the
   flagged verb, mails the owner, and resumes. Every other class escalates as SPEC-0547 says.
4. `CONDUCTOR_CURSOR_POLICY` states the rule for the cursor runner, which has no hooks.

## Design

### The flag

`apply_clear(root, plan=None, *, trivial_only=False)` and CLI `--trivial-only`. With the flag,
the verb first runs every SPEC-0548 guard (lock, re-plan, in-progress, plan check, paths, index).
Then a live action other than `commit` returns `ok: false`, `error: not trivial: <action>`, and
SPEC-0548's `escalate` for the live class, and writes nothing. Without the flag the verb is
unchanged.

### Launch (`src/nightwright/agentic/launch.py`), static rules

- **Allow:** add `Bash(uv run nw queue main-dirty-apply --trivial-only *)` to
  `conductor_allowed_tools`. It is documentary: `Bash(uv run nw *)` already allows the form; what
  grants it is removing SPEC-0548's blanket denies. `_CONDUCTOR_WIP_VERBS` and
  `_CONDUCTOR_GIT_VERBS` are unchanged.
- **Verb deny (conductor-only):** SPEC-0548's `Bash(uv run nw * main-dirty-apply*)` and
  `Bash(nw * main-dirty-apply*)` are removed (they would also deny the allowed form: deny wins).
  For each prefix `uv run nw` and `nw`, and each of `queue main-dirty-apply` and
  `* queue main-dirty-apply`, deny the bare form and one tail per option of the click command
  other than `--trivial-only`: today ` --root*`, ` --plan*`, ` --base*`, ` --json*`, ` --help*`,
  ` -h*`. None of these matches `uv run nw queue main-dirty-apply --trivial-only …`. A launch
  test introspects the click command and fails when an option has no deny tail.
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
    the conductor's own context: one command, no sub-agent, one per root at a time. This runs
    even when an open `human_checkout` record exists for the root; on `ok: true` withdraw that
    record (`nw escalations withdraw <id> --reason "cleared by main-dirty-apply" --yes`).
  - On `ok: true`, mail `nw mailbox send --from conductor --to steward:<Project> --kind info`
    with the `sha` and the paths, or `--to human` when the `stewards` list of
    `nw queue conduct-status --json` has no `address` equal to `steward:<Project>` (a steward is
    the owner only while its mailbox follower is held). Then resume each `main_dirty` campaign
    of the root with `--decision "cleared main (commit, <sha>)" --rule parked.main_dirty`.
  - On `error: not trivial: resume`, the checkout became clean meanwhile: go to step 2.
  - On any other `ok: false`, do not retry. Go on to step 3 / 4 with an extra
    `--fact 'main-dirty-apply: <error>'`.

  Every other action goes straight to step 3. Steps 1, 3, 4 and 5 are unchanged.
- **`parked.main_not_on_base` row:** the conductor does not move dirt or switch branches in the
  main checkout. It runs `main-dirty-plan` and escalates `human_checkout` (no campaign, `--yes`)
  with the class and `recommended`, as in SPEC-0547 step 4.
- **`wt_broken` row:** mail the steward (or escalate `human_checkout`) to save the tracked dirt
  to a WIP branch and restore main (the steward runs the audited verb). Use
  `uv run nw` from a worktree meanwhile. No raw git write in the main checkout.
- **SPEC-0496 paragraphs, amended.** The dispatch paragraph keeps telling sub-agents to run
  plain single `git add <paths>` / `git commit -m …` in their worktree. The permission-denial
  paragraph today says "run the blocked plain command yourself in that worktree"; the
  conductor's cwd is not that worktree, so it becomes: "run it yourself as
  `git -C <worktree> add -- <paths>` / `git -C <worktree> commit -m …` (the forms the conductor
  allow-list permits), never in the main checkout: the conductor-git-guard hook denies raw git
  writes there".

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

- [ ] AC1 (SPEC-0544 AC1) `main-dirty-apply --trivial-only` on each non-`commit` live action
      (`save_branch`, `branch_and_escalate`, `hold`, `escalate`, `resume`) writes nothing: HEAD,
      index bytes, worktree bytes, and the branch list are unchanged. It returns `ok: false`,
      `error: not trivial: <action>` and SPEC-0548's `escalate` for the class. On a trivial diff
      it commits exactly as SPEC-0548 AC2.
- [ ] AC2 (SPEC-0544 AC2) Static rules: `decide` permits
      `uv run nw queue main-dirty-apply --trivial-only --root /r --json` and denies
      `uv run nw queue main-dirty-apply --root /r --json`, the bare form, `--plan f`, `--base x`,
      `--json`, `-h`, the same forms through `nw`, and
      `uv run nw --x queue main-dirty-apply --root /r`. A test introspects the click command and
      fails when an option other than `--trivial-only` has no deny tail.
- [ ] AC3 (SPEC-0544 AC8) No conductor static rule contains a project root path or a base
      branch name: a launch with two projects whose roots and bases differ yields identical rule
      lists. `DEFAULTS["disallowed_tools"]`, `_CONDUCTOR_WIP_VERBS`, and the steward's effective
      rules equal a snapshot taken before this spec. The steward still denies
      `git -C /w rm -f --ignore-unmatch -- f "x log y"`.
- [ ] AC4 (SPEC-0544 AC9) `CONDUCTOR_CURSOR_POLICY` names `main-dirty-apply --trivial-only` and
      the main checkout write ban, says worktree add/commit is allowed, and no longer contains
      "Do not run `nw queue main-dirty-apply`."
- [ ] AC5 (SPEC-0544 AC10) The `parked.main_dirty` row, extracted by rule id, contains a step
      `2b` between steps 2 and 3 with `main-dirty-apply --trivial-only`, `--from conductor`,
      `steward:<Project>`, `conduct-status`, `--to human`, `cleared main (commit`,
      `not trivial: resume`, `escalations withdraw`, `--rule parked.main_dirty`, and the
      `main-dirty-apply: <error>` fact. Every `main-dirty-apply` command in the skill is followed
      by `--trivial-only` (regex `main-dirty-apply(?!:)(?! --trivial-only)` finds nothing; the
      fact label `main-dirty-apply: <error>` is exempt). The row contains none of `git add`, `git commit`, `git checkout`,
      `git rm`, `git worktree add`. The `parked.main_not_on_base` and `wt_broken` rows name
      `human_checkout` or the steward and no raw git write. The permission-denial paragraph
      contains `git -C <worktree> add -- <paths>` and `never in the main checkout`; the dispatch
      paragraph keeps its plain `git add <paths>` / `git commit -m` wording.
- [ ] AC6 (SPEC-0544 AC11) Inherited assertions this spec edits, and only these: SPEC-0547 AC5's
      forbidden list loses `main-dirty-apply` (the row may name the `--trivial-only` form);
      SPEC-0548 AC7's two blanket deny rules are replaced by AC2's rule set (with the four named
      `tests/test_agentic_launch.py` sites updated to it) and its "`nw-conduct` never names
      `main-dirty-apply`" is replaced by AC5's regex; SPEC-0548 AC7's cursor-policy sentence is
      replaced by AC4. SPEC-0547's steps 1 and 3–5, its question, options and `--fact` text are
      byte-identical.

## Test plan

- **Automated tests:**
  - `tests/test_main_dirty_apply.py` (SPEC-0548) covers AC1.
  - `tests/test_agentic_launch.py` covers AC2, AC3 and AC4, including the snapshot, the
    two-project launch, and the option-introspection test; it replaces SPEC-0548's AC7 deny test.
  - `tests/test_main_dirty_plan.py`: its skill-row test (SPEC-0547 AC5) drops `main-dirty-apply`
    from the forbidden list and gains AC5; SPEC-0548's conduct-skill assertion is replaced by
    AC5's regex (AC6).
- **Regression guard:** `tests/test_claude_settings_allowlist.py` (SPEC-0496) and
  `tests/test_conductor_git_guard.py` (SPEC-0550) pass unchanged. Also
  `python3 tools/affected_tests.py --run uv run pytest -q`, `python3 tools/test_tiers.py --check`,
  `python3 tools/spec_lint.py`.
- **Manual verification (real binary):** with the SPEC-0550 hook installed, run the real
  `claude -p` once as a registered conductor session with the plan's `--allowedTools` /
  `--disallowedTools` and `--permission-mode dontAsk`, on a scratch repo under the scratchpad
  with a frontmatter-only edit on main. Have it run
  `uv run nw queue main-dirty-apply --root <main> --json` (expect denied), then
  `uv run nw queue main-dirty-apply --trivial-only --root <main> --json` (expect `ok: true`, one
  commit with the SPEC-0548 subject). Then a logic edit in `src/x.py` with `--trivial-only` →
  `ok: false`, `not trivial: branch_and_escalate`, nothing changed. Record each result in As
  shipped.

## Out of scope

- The conductor never runs the verb for `save_branch`, `branch_and_escalate`, `hold`,
  `escalate`, or `resume`; the human or the steward does (SPEC-0548, SPEC-0549).
- The hook itself and its install (SPEC-0550).
- No change to the steward's allow or deny lists, to `.claude/settings.json` allow rules
  (SPEC-0496), or to what the SPEC-0496 dispatch paragraph tells sub-agents to run.
- No `git switch <base>` allow for the conductor; the trivial path never switches.

## Rollout / migration

Ships after SPEC-0550. A conductor launched after the deploy gets the new verb rules; a running
one keeps its old flags (SPEC-0548's blanket deny) until it is relaunched (`launch_hash`
changes). Worktree sub-agent commits are unaffected. IDEA-1159 is delivered when this spec is
done. Queued under project Meta-Tools.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the real-binary check; affected tests pass.
- [ ] No regressions in launch, settings-allowlist, skills, git-guard and main-dirty tests.
- [ ] `docs/product/loop/operate.md` and `docs/product/capabilities/agentic.md` say the
      conductor clears the trivial class with `--trivial-only` and escalates the rest.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
