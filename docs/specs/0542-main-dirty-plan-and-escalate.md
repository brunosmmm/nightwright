---
id: SPEC-0542
title: main_dirty is conductor-owned; plan the clear and escalate it
status: accepted
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, conductor, queue]
supersedes: [SPEC-0537]
source_idea: IDEA-1159
---

## Context

`finish_close` parks `main_dirty` when the main checkout is on the base branch and
`git status --porcelain --untracked-files=no` is non-empty (`src/nightwright/agentic/close.py`).
The park is correct. [SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md) makes it
human-owned (`PARK_OWNERS["main_dirty"] = "human"`), so it is never overdue, `park_undecided`
never fires, the conductor ping fingerprint skips it, and herdr stays silent. On the night of
IDEA-1159 one stray tracked edit sat in **Needs you** and blocked every close: zero deliveries.

[SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md) tried to fix this in one
spec: owner change, a classifier, an audited write verb, conductor permissions, and skill rows.
The human said yes to the conductor clearing `main_dirty`, and approved replacing raw git
allow-rules with one audited verb, `nw queue main-dirty-apply` (campaign resume note, seq 437,
2026-11-10). The campaign (`IDEA-1159-20261110-02ed`, Meta-Tools) then ran 85 runs and spent
$26.96; the review score fell 92 → 79 and it stopped converging. At ESC-0067 (2026-11-11) the
human chose **"rework the spec (split / narrow)"**. The branch `nw/IDEA-1159-20261110-02ed` is
kept, never merged.

The steward split SPEC-0537 into three specs, each small enough to converge in one campaign:

- **SPEC-0542 (this spec)** — ownership, the read-only classifier, and a conductor row that
  plans and escalates. No conductor git write, no allow-list change.
- [SPEC-0543](./0543-main-dirty-apply-audited-verb.md) — the audited verb
  `nw queue main-dirty-apply`, run by the human or a steward.
- [SPEC-0544](./0544-conductor-runs-main-dirty-apply-for-trivial.md) — the conductor runs that
  verb for the trivial class only.

Reasoning for the split: almost every review major on the branch was about writes (the verb's
trust of its plan file, the WIP save ordering, pathspecs, symlinks, the allow/deny globs, and
skill text that contradicted the deny list). Ownership and classification were close to done
by loop 3 but could not land while the write half churned. Shipping the read-only half first
removes the overnight block (the conductor is woken and the human gets one precise escalation
with the exact command), and each write step after it is reviewed on its own.

## Goals / Non-goals

**Goals**

- `main_dirty` is conductor-owned: overdue, `park_undecided`, ping fingerprint, herdr toast,
  Conductor handling, and not an E5 human touchpoint.
- `nw queue main-dirty-plan` classifies the tracked dirt and prints the exact command that
  clears it.
- The `parked.main_dirty` skill row runs the plan, resumes when the checkout is clean, and
  otherwise escalates once per checkout with `--topic human_checkout`, naming the class and the
  command. It resumes the campaigns after the checkout is clean.

**Non-goals**

- No git write by the conductor or any sub-agent for `main_dirty`. No sub-agent is dispatched
  for it. No change to `launch.py` allow or deny lists or `CONDUCTOR_CURSOR_POLICY`.
- The write verb (SPEC-0543) and conductor writes (SPEC-0544).
- `main_not_on_base`, `human_decision`, `restart` stay human-owned. The
  `parked.main_not_on_base` row keeps its current procedure.
- Untracked files never park `main_dirty` and are never listed.
- `finish_close` is unchanged.

## Decision

Drop `main_dirty` from `queue.PARK_OWNERS`. Add `src/nightwright/agentic/main_dirty.py`
`plan_clear(root) -> dict` (read-only) and `nw queue main-dirty-plan --root PATH [--json]`.
The plan carries `class`, `action`, the paths, and `recommended`: the shell commands a human
runs to clear the checkout. The conductor's `parked.main_dirty` row runs the plan and either
resumes (`clean` on the base) or escalates `human_checkout` with no campaign, once per root,
then resumes on the wake after the checkout is clean.

## Design

### Ownership and wake

Same as SPEC-0537's "Ownership and wake" section, which the branch implemented and reviewers
accepted: `PARK_OWNERS` keeps `main_not_on_base`, `human_decision`, `restart`; `park_owner`
defaults to `conductor`. Existing paths pick the owner up with no new branch:
`quiet.ping_fingerprint`, `quiet.classify` (a runner-emitted `main_dirty` park is `wake`),
`herdr_watch.notification_for`, `dashboard_payload` (Conductor handling), `mvp_report.e5`.
Delete the `main_dirty` entry of `dashboard/inbox.py` `HOST_FIX`; keep `main_not_on_base`.

### The plan

`plan_clear(root)` writes nothing (no index refresh either: it runs git with
`GIT_OPTIONAL_LOCKS=0`). CLI: `--root` must be the top of a git worktree; otherwise exit 2 with
a stderr message. A git failure on a real worktree exits 3 with a stderr message. Every class
exits 0. Text mode: one line `<action> <class> <n> paths`, one path per line, then one line per
`recommended` command.

Inputs and the classification rules are SPEC-0537 "The plan" verbatim (code paths, comment
lines per suffix, comment-only, frontmatter-only, docs-prose with backtick fences, the file
class table, worst file wins, base from `.nw/project.yaml` `merge.base` → `base` → `main`,
detached HEAD → `on_base: false`, off base → `action: hold`), plus these rules that the branch
added after review majors:

- Lines split on `\n` only, never `splitlines()` (a form feed or `\r` does not hide code).
- Diffs are decoded with `errors="surrogateescape"`; a non-UTF-8 diff never raises. A NUL byte
  on either side, or git's binary marker, sets `binary`; a binary `.md` is never trivial.
- Only backtick fences toggle fence state; a `~~~` line inside a backtick fence does not close
  it. An unclosed fence runs to end of file.
- Frontmatter-only needs the closing `---` (compared stripped) before any fence.
- A change with no added or removed text lines (mode-only, typechange `T`, a symlink on either
  side) is file class `other`, never `comment` / `docs` / `frontmatter`.
- Under `skills/` or for `docs/LEDGER.md` the file is `other`, except that a code path
  (code suffix) under `skills/` still follows the `code` rule.
- Staged and unstaged changes are both classified (`git diff HEAD`). A rename classifies both
  the old and the new path, and both appear in `paths`.
- **Operation in progress → `class: merge`, `action: escalate`**, whatever the files and the
  branch: `MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge/` or `rebase-apply/`
  under the worktree's git dir (resolved with `git rev-parse --git-path`, so a linked worktree
  is covered), or any unmerged porcelain status (`UU AA DD AU UA DU UD`). Key `in_progress` is
  `merge`, `cherry-pick`, `revert`, `rebase`, `conflict`, or `null`.

JSON schema `nw.main_dirty_plan.v1`, keys: `schema`, `root`, `base`, `on_base`, `class`
(`clean|trivial|branch|substantive|merge`), `action`
(`resume|commit|save_branch|branch_and_escalate|hold|escalate`), `in_progress`, `paths`,
`substantive_paths`, `files` (`{path, status, file_class}`), `recommended` (list of strings).

### `recommended`

Commands for a human in a shell, each argument quoted with `shlex.quote`, every path command
with `git -C <root> --literal-pathspecs`, paths after `--`. `<wip>` is
`wip/main-dirty-<YYYYMMDDThhmmssZ>` (UTC, plan time), with `-2`, `-3`, … appended while
`git branch --list` shows it.

| action | `recommended` |
|---|---|
| `resume` | `[]` |
| `commit` | `git -C R --literal-pathspecs add -- P…`, `git -C R commit -m 'chore: land stray trivial main-checkout edits'` |
| `save_branch`, `branch_and_escalate` | `git -C R switch -c <wip>`, `git -C R --literal-pathspecs add -- P…`, `git -C R commit -m 'WIP: stray main-checkout edits (<class>)'`, `git -C R switch <base>` |
| `hold` with tracked dirt | the `save_branch` list (it ends on `git -C R switch <base>`) |
| `hold`, no tracked dirt | `git -C R switch <base>` |
| `escalate` (`merge`) | `[]`; the escalation option names the operation to finish |

None of these discards work: the dirt is committed, on the base or on `<wip>`. No `checkout --`,
`reset`, `clean`, `stash drop`, `rm`, or push ever appears. SPEC-0543 replaces the non-empty
lists with the verb once it ships.

### The `parked.main_dirty` skill row

`skills/nw-conduct/SKILL.md` splits today's combined `parked.main_dirty, parked.main_not_on_base`
row. The `parked.main_not_on_base` row keeps today's text for that reason only. The
`wt_broken` row stops saying "same as `main_dirty`" and spells out its current move procedure.
The "Clear blockers yourself" bullet names `parked.main_dirty` as the exception for a dirty main
checkout (plan and escalate, never clear).

The new `parked.main_dirty` row, triggered by `parked` `main_dirty`, and by a
`conductor_ping` / `park_undecided` / `escalation_answered` that concerns an undecided
`main_dirty` park or its escalation:

1. Group the `main_dirty` campaigns by `repo.root`. Per root, run
   `nw queue main-dirty-plan --root <root> --json` in the conductor's own context (read-only,
   no sub-agent).
2. `action: resume` (clean and on the base): for each non-terminal `main_dirty` campaign of that
   root, `nw queue resume <id> --to CLOSING --by conductor --decision "main checkout clean"
   --rule parked.main_dirty --yes`. Done.
3. Otherwise, if `nw escalations list --json` already has an open `human_checkout` record whose
   question names `<root>`, do not escalate again; record
   `nw queue decision <id> --kind wait --rule parked.main_dirty "waiting on ESC-NNNN"` once per
   campaign that has no decision after its park.
4. Else escalate once, no campaign: `nw queue escalate --topic human_checkout --question
   "Main checkout <root> has <class> tracked edits blocking closes: run the commands below,
   then answer" --option "I ran the recommended commands" --option "I cleared it another way"
   --recommend 1 --fact "class: <class>, action: <action>" --fact "<command>"` (one `--fact` per
   `recommended` line, one per path up to 10) `--yes`. For `merge` the first option is
   `I will finish the <in_progress> in the main checkout`. Then
   `nw queue decision <id> --kind escalate --rule parked.main_dirty "escalated ESC-NNNN"` for
   each `main_dirty` campaign of that root, so the park counts as decided and `park_undecided`
   does not repeat.
5. On `escalation_answered` for that record, run step 1 again: `resume` → step 2; still dirty →
   step 4 with an extra `--fact "still dirty after ESC-NNNN"`.

The row runs no git command other than through `main-dirty-plan`, dispatches no sub-agent, and
never names `main-dirty-apply`.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

Read-only with `git show nw/IDEA-1159-20261110-02ed:<path>`; take only the plan half:

- `src/nightwright/agentic/main_dirty.py` — everything up to and including `plan_clear`
  (lines 1–502 at the branch tip: `_is_git_repo` … `_merge_state`, `plan_clear`, `NotAWorktree`).
  Not `apply_clear` or any `_wip_*` / `_commit_*` / `_restore_*` helper. Add `in_progress`,
  the sequencer markers via `--git-path`, and `recommended`.
- `src/nightwright/agentic/cli_queue.py` — the `main-dirty-plan` command only.
- `src/nightwright/agentic/queue.py` (`PARK_OWNERS`), `src/nightwright/agentic/dashboard/inbox.py`
  (`HOST_FIX`).
- `docs/product/loop/operate.md`, `docs/product/capabilities/agentic.md`,
  `docs/product/reference/index.md` — reword to "the conductor classifies and escalates";
  name `main-dirty-plan` only.
- Test updates in `tests/test_agentic_queue.py`, `test_agentic_events.py`,
  `test_agentic_dashboard.py`, `test_agentic_queue_cli.py`, `test_tui_queue_model.py`,
  `test_dashboard_cli.py`, `test_dashboard_inbox.py`, `test_dashboard_page.py`,
  `test_dashboard_snapshot.py`, `test_herdr.py`, `test_agentic_mvp_report.py`,
  `test_skills.py`, `test_rich_output_guard.py`.
- Not `launch.py`, not `test_agentic_launch.py`, not the branch's `SKILL.md` text.

## Alternatives considered

- **Keep SPEC-0537 whole and push on.** 85 runs, score falling; the human chose to split.
- **Leave `main_dirty` human-owned and only rewrite the skill row.** The fingerprint and
  `park_undecided` would still ignore a missed park, which is how the checkout sat overnight.
- **Recommend `git stash push`** for non-trivial dirt. A stash is easy to lose and does not
  name a branch; a `wip/main-dirty-*` commit is visible in `git branch` and matches what the
  verb (SPEC-0543) produces.
- **Mail the steward instead of escalating.** The edit is in the human's own checkout, which
  the skill's human-decision route reserves for the human (`human_checkout` never times out).

## Acceptance criteria

- [ ] AC1 `park_owner("main_dirty") == "conductor"`; `main_not_on_base`, `human_decision`,
      `restart` stay `human`.
- [ ] AC2 An undecided `main_dirty` park older than `park_decide_s` is overdue, is in Conductor
      handling and not in Needs you, and `events.sync` emits exactly one `park_undecided` for
      it; a decision note after the park clears the overdue flag and no second
      `park_undecided` fires.
- [ ] AC3 `quiet.ping_fingerprint` contains `["parked", <cid>]` for a real `main_dirty` park
      written by `finish_close` (not a hand-built dict); `quiet.classify` returns `wake` for it.
- [ ] AC4 Herdr emits `nw: parked` for a `main_dirty` park; `HOST_FIX` has no `main_dirty` key;
      `mvp_report.e5` on a campaign that really parked `main_dirty` has no touchpoint for it,
      while a `main_not_on_base` park still is one.
- [ ] AC5 Classes on temp git repos, one test per row, each test first asserting its fixture
      produced the named diff: frontmatter-only spec, docs prose outside fences, `README.md`,
      `CHANGELOG.md`, comment-only `.py` → `trivial`/`commit`; `docs/LEDGER.md` (even a blank
      line), a fence edit, an unclosed fence, `skills/x.md`, a doc delete, a binary outside code
      paths, a NUL in a `.md` → `branch`/`save_branch`; a logic edit beside a docs edit, a `src/`
      delete, a binary under `src/`, a root `.py`, `tests/`, `skills/x/run.py`, a rename between
      `src/` and `docs/` in either direction → `substantive`/`branch_and_escalate`;
      `star_deref` (`src/star_deref.c`, `*ptr = 1;`) → `substantive`; `star_comment`
      (`src/star_comment.c`, ` * kept comment`) → `trivial`; a shebang edit in `.py` → not
      trivial; no tracked dirt → `clean`/`resume`; an untracked file never appears in `paths`.
- [ ] AC6 `_is_comment_line` is pinned by a parametrized table covering every listed suffix,
      `#!`, `*x`, `* x`, `/*`, `*/`, `<!--`, `-->`, and a Markdown `#` heading (not a comment).
- [ ] AC7 Mode-only (`chmod +x`), typechange, and a symlink retarget are `other`, never trivial;
      a staged-only comment edit is `trivial`; a staged fence edit is `branch`; a form-feed code
      line and a non-UTF-8 diff are classified without raising.
- [ ] AC8 Operation in progress: `MERGE_HEAD`, each of the seven unmerged codes,
      `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge/`, and a merge in a linked worktree each
      give `merge`/`escalate` with the matching `in_progress`, also when off the base.
- [ ] AC9 Base: `merge.base` wins over `base`, `base` over the default, a missing or scalar
      `merge` falls back, and no `.nw/project.yaml` means `main`. On a non-`main` base a trivial
      diff is `commit` on it and `hold` (class still `trivial`) elsewhere; detached HEAD gives
      `on_base: false` and `hold`.
- [ ] AC10 `recommended` matches the table exactly (asserted as whole lists) for `commit`,
      `save_branch`, `branch_and_escalate`, `hold` dirty, `hold` clean, `resume`, `escalate`;
      a path with a space or a literal `*` is quoted; the `<wip>` name takes `-2` when the
      branch exists. Running the `commit` list and the `save_branch` list in the temp repo
      leaves `git status --porcelain --untracked-files=no` empty, the edit on the base commit
      or on `<wip>`, and the untracked file in place.
- [ ] AC11 `plan_clear` writes nothing: HEAD, `git branch --list`, the index file's bytes, and
      the worktree files are identical before and after, for every class.
- [ ] AC12 CLI (through `CliRunner`): `--json` prints `nw.main_dirty_plan.v1` and exits 0 on
      each class; a non-git `--root` and a subdirectory of a checkout exit 2 with a message; a
      git failure exits 3; text mode prints the summary line, the paths, and the commands.
- [ ] AC13 Skill: the `parked.main_dirty` row (extracted by its rule id) names
      `main-dirty-plan`, `human_checkout`, `--yes`, `--recommend 1`, `recommended`,
      `escalation_answered`, `--to CLOSING`, `--rule parked.main_dirty`, and the
      open-escalation check; it contains none of `git add`, `git commit`, `git checkout`,
      `git rm`, `git worktree add`, `main-dirty-apply`, `isolation`. The `wt_broken` row does
      not contain `same as`; `parked.main_not_on_base` is its own row.
- [ ] AC14 The escalate command of the row, with `--yes` removed, runs as a dry run against the
      real `nw queue escalate` CLI and exits 0 (every flag exists).
- [ ] AC15 `launch.py` and `tests/test_agentic_launch.py` are unchanged by this spec;
      `finish_close` still parks `main_dirty` with `resume_to: CLOSING` and still merges once the
      checkout is tracked-clean.

## Test plan

- **Automated tests:** new `tests/test_main_dirty_plan.py`, one module marker
  `pytestmark = pytest.mark.fast`. From the branch's `tests/test_main_dirty_clear.py` take the
  plan tests (everything before `test_apply_commit_trivial` at line 753, plus
  `test_is_comment_line_suffix_table`, `test_typechange_and_mode_only_are_other_not_trivial`,
  `test_docs_prose_requires_a_visible_diff`, `test_retargeted_symlink_is_not_trivial`,
  `test_split_fm_keeps_eof_newline_and_star_tab_comment`, the plan half of
  `test_cli_text_mode_plan_and_apply` and `test_cli_git_failures_have_messages_and_distinct_codes`,
  and the plan part of `test_root_must_be_checkout_top`). Rewrite `test_skill_row_phrases` to
  AC13 (row extracted by rule id; negative phrases). Drop `test_cherry_pick_no_commit_not_merge`
  only if it contradicts AC8 (a `cherry-pick -n` that leaves no `CHERRY_PICK_HEAD` is not an
  operation in progress and stays as is). New: AC8 sequencer and linked-worktree cases, AC10
  whole-list `recommended` cases plus running them, AC11 snapshot test, AC14 dry-run test.
- **Existing modules** (already have their tier marker): the test updates listed in Design;
  E5 uses a `main_dirty` park planted through `finish_close` (AC4).
- **Regression guard:** `tests/test_agentic_close.py`, `tests/test_agentic_mvp_integration.py`,
  `tests/test_agentic_launch.py` pass unchanged. `python3 tools/affected_tests.py --run uv run
  pytest -q`, `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification (real check):** in a scratch repo under the scratchpad, `git init`,
  commit `docs/a.md` with frontmatter and `src/x.py`; edit only the frontmatter; run
  `uv run nw queue main-dirty-plan --root <scratch> --json` → `trivial`/`commit`; paste the
  `recommended` commands into a shell → tracked-clean, one new commit with the subject. Then
  edit `src/x.py` logic and add an untracked file → `substantive`; run its commands → back on
  the base, tracked-clean, the edit on `wip/main-dirty-*`, the untracked file still there.
  Record the output in the spec's As shipped.

## Rollout / migration

No data move. The owner change reclassifies existing `main_dirty` parks on the next read; an
old one with no decision emits one `park_undecided`, which wakes the conductor to plan and
escalate. SPEC-0537 is marked `superseded` by this spec, SPEC-0543 and SPEC-0544.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the scratch-repo check; affected tests pass.
- [ ] No regressions in close, dashboard, herdr, launch, E5 tests.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
