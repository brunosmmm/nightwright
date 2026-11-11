---
id: SPEC-0537
title: Conductor clears main_dirty itself
status: superseded
owner: user
created: 2026-11-10
updated: 2026-11-11
kind: feature
tags: [agentic, conductor, queue]
source_idea: IDEA-1159
superseded_by: SPEC-0542
---

> **Superseded** (2026-11-11, ESC-0067 "rework the spec (split / narrow)"): split into [SPEC-0542](./0542-main-dirty-plan-and-escalate.md), [SPEC-0543](./0543-main-dirty-apply-audited-verb.md) and [SPEC-0544](./0544-conductor-runs-main-dirty-apply-for-trivial.md); the campaign was abandoned and its branch `nw/IDEA-1159-20261110-02ed` never merged, so nothing below shipped to main.

## Context

`finish_close` parks `main_dirty` when the main checkout is on the base branch and
`git status --porcelain --untracked-files=no` is non-empty
(`src/nightwright/agentic/close.py`, the merge preconditions). That park is correct: an
ff-only merge must not land on a dirty checkout. What happens next is not.

[SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md) sets
`PARK_OWNERS["main_dirty"] = "human"`. A human-owned park is never overdue, so
`park_undecided` does not fire, and `quiet.ping_fingerprint` skips it. The dashboard
inbox tells the human to move the files and resume. Herdr stays silent for the park.
The `parked` event itself still wakes a running conductor, and
`skills/nw-conduct/SKILL.md` already tells that conductor to move the work off main.
Ownership wins in practice: a stray tracked edit sits in **Needs you** until a person
sees it. On the night of IDEA-1159 that one edit blocked every close. Nothing delivered.

The conductor has to clear the checkout. A trivial tracked diff (frontmatter, docs
prose, or comment lines) is committed on the base branch by a sub-agent when the
checkout is already on that base. Anything else is saved intact on a new branch and
the main checkout is restored. A substantive code edit, a merge in progress, a hold
that is still off the base after that save, or a clear that cannot leave main
tracked-clean, is escalated. `main_dirty` is conductor-owned, so a park that is still
sitting wakes the conductor.

## Goals / Non-goals

**Goals**

- `main_dirty` is conductor-owned. An undecided park shows under Conductor handling,
  becomes overdue, fires `park_undecided` once, and is included in the conductor ping
  fingerprint. Herdr notifies it the same way it notifies any other conductor park.
- A read-only plan classifies the tracked diff as trivial, branch, substantive, or
  merge, and names one action: commit, save to a branch, save to a branch and
  escalate, hold, escalate, or resume.
- The conductor skill sends one sub-agent per checkout to perform that action. Every
  git write of the clear goes through one audited verb, `nw queue main-dirty-apply`;
  the conductor holds no raw git write rule. After a successful clear it resumes every `main_dirty` campaign of that checkout to
  `CLOSING`.
- Escalation uses `nw queue escalate --topic human_checkout --yes` with no campaign,
  so it does not park the closing campaign. It fires for a substantive code edit, a
  merge in progress, a hold that is still `hold` after the save, and a clear that
  leaves main tracked-dirty.

**Non-goals**

- `main_not_on_base`, `human_decision`, and `restart` stay human-owned.
- Untracked files do not park `main_dirty` and are left in the main checkout.
- The queue runner does not commit, branch, or restore during `finish_close`. The
  conductor's sub-agent does, after the park, by running `main-dirty-apply`.
- No gate run and no push before or during the clear. The sub-agent does not push.
  A later close with `push_after_merge` pushes the base as it already does.
- `wt_broken` does not commit onto the base. Its skill row keeps a save-to-branch
  procedure of its own.
- Rewriting [SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md). That
  spec stays the owner of Needs you, Conductor handling, and `park_undecided`. This
  spec changes one row of the owner table.
- Superseding [SPEC-0382](./0382-conductor-must-never-block-on-the-human-high-it.md).
  This spec amends the line that left dropping the moved WIP from main to the human.
  SPEC-0382 stays in force for everything else.

## Decision

Remove `main_dirty` from `PARK_OWNERS`, so `park_owner("main_dirty")` is `conductor`.
Add a read-only classifier, `nw queue main-dirty-plan`, whose `base` is that
checkout's project base, and point the `parked.main_dirty` skill row at it. One clear
runs per repo root at a time. The sub-agent is spawned without `isolation: worktree`,
in the main checkout, and writes git only through `nw queue main-dirty-apply`, one
audited verb that replaces raw git allow-rules. The verb re-plans from the live
checkout, refuses a merge in progress, commits a trivial diff onto the base when the
checkout is already on that base, and saves every other diff onto a WIP branch under
`.nw-worktrees` before restoring those tracked paths on main. A substantive diff is
saved and flagged `escalate`. All the conductor denies stay (`git checkout -- .`, `cp`,
`git rm -r`, `git clean`, `git reset --hard`, `git switch -f` / `--discard-changes`,
push). A merge in progress is `class: merge`, `action: escalate`, and nobody writes git. After a successful clear it resumes every
`main_dirty` campaign of that repo to `CLOSING`. It escalates with
`nw queue escalate --topic human_checkout --yes` and no campaign when the class is
substantive, when a merge is in progress, when a later plan is still `hold`, or when
a second attempt still leaves main tracked-dirty.

## Design

### Ownership and wake

`queue.PARK_OWNERS` keeps `main_not_on_base`, `human_decision`, and `restart` mapped to
`human`, and drops `main_dirty`. `park_owner` is unchanged: a missing key is
`conductor`. Owner stays derived, so a `main_dirty` park already on disk becomes
conductor-owned on the next read. If it is older than `park_decide_s` and has no note
after the park, `park_view` marks it overdue and the next `events.sync` emits one
`park_undecided`. That is the recovery for a park that sat overnight.

These existing paths pick the new owner up with no extra branch:

- `quiet.ping_fingerprint` adds `("parked", campaign)` for an undecided conductor park,
  so the next `conductor_ping` whose fingerprint changed wakes the conductor. The
  `conductor_ping` skill row already acts on those items.
- `quiet.classify` returns `wake` for a `parked` event unless `auto`, `red_main`, or
  `by == "conductor"`. A runner-emitted `main_dirty` park stays a wake. A test pins that.
- `herdr_watch.notification_for` notifies `nw: parked` when `park_owner` is `conductor`.
- `dashboard_payload` files the row under `conductor_handling`, and the inbox
  `park_entries` walk only `needs_you`, so the host-fix line drops out.
- `mvp_report.e5` counts a touchpoint only when `park_owner` is `human`. A `main_dirty`
  park stops failing E5. `main_not_on_base` still fails it.

Delete the `main_dirty` sentence from `dashboard/inbox.py` `HOST_FIX`. Leave the
`main_not_on_base` sentence. `docs/product/loop/operate.md` describes the reason as the
conductor classifying the tracked diff, committing a trivial one on the base, saving
any other on a WIP branch, resuming, and escalating when the plan says the edit is
substantive code, a merge is in progress, or the clear cannot leave main tracked-clean.
`docs/product/capabilities/agentic.md` keeps the sentence that close parks `main_dirty`;
add that the conductor clears it per this spec.

### The plan

New module `src/nightwright/agentic/main_dirty.py`. `plan_clear(repo) -> dict` reads the
checkout and writes nothing. `nw queue main-dirty-plan --root PATH` (`--json` prints the
dict) is registered on the queue CLI in `cli_queue.py` and named from
`docs/product/reference/index.md` under `nw queue`. Exit 0 for every class, including a
clean tree. Exit 2 when `PATH` is not a git worktree. Text mode prints one line
`<action> <class> <n> paths` and then one path per line. The command takes `--root`
and `--json`. It does not take a campaign id and does not look up a campaign.

Tracked dirt is `git status --porcelain=v1 -z --untracked-files=no`. The diff against
HEAD is `git diff HEAD -z` (staged and unstaged). Per path, `old` is `git show HEAD:path`
when the blob exists, and `new` is the worktree bytes when the file is there, else absent
for a deletion. A NUL byte or a "Binary files differ" diff sets `binary`.

Repo-relative paths use forward slashes. A **code path** starts with `src/`, `tests/`,
or `tools/`, or its suffix is `.py`, `.pyi`, `.sh`, `.bash`, `.js`, `.mjs`, `.ts`,
`.tsx`, `.go`, `.rs`, `.c`, `.h`, `.cc`, or `.cpp`. `docs/LEDGER.md` is never trivial.

A line is a **comment line** for a path when `strip()` is empty, or:

- suffix `.py`, `.pyi`, `.sh`, `.bash`, `.yml`, `.yaml`, `.toml`, `.rb`, or `.pl`, and
  the stripped line starts with `#` and does not start with `#!`;
- suffix `.js`, `.mjs`, `.ts`, `.tsx`, `.go`, `.rs`, `.c`, `.h`, `.cc`, `.cpp`, `.css`,
  or `.java`, and the stripped line starts with `//`, or is `/*`, `*/`, or `*`, or
  starts with `*` followed by whitespace (`* text`). A stripped line that starts with
  `*` and then a non-space (`*out`, `*=`) is not a comment line;
- suffix `.md`, `.html`, or `.xml`, and the stripped line starts with `<!--` or is `-->`.

`#` headings in Markdown are not comment lines. A file is **comment-only** when it is
not binary, status is not a delete or rename, and every added line and every removed
line is a comment line. A mode-only change has no such lines and is not comment-only.

A file is **frontmatter-only** when the path ends with `.md`, is not `docs/LEDGER.md`,
`old` and `new` are both text, each begins with a `---` line and has a later line whose
stripped text is `---`, and the bytes after that closing fence are identical.

A file is **docs-prose** when it is not `docs/LEDGER.md` and the path is `README.md`,
`CHANGELOG.md`, or under `docs/` and ends with `.md`. Fence state toggles on a line
whose stripped text starts with a backtick fence (three or more backticks). An added
line whose number in `new` sits inside a fence, or a removed line whose number in `old`
sits inside a fence, is a fence edit. Docs-prose requires text on both sides and no
fence edit. Conflict markers are not special in this rule; a merge is caught before
the file rollup, below.

**File class**, first match:

| class | when | rollup |
|---|---|---|
| `code` | code path, and the change is not comment-only (a delete, rename, or binary on a code path counts) | substantive |
| `comment` | comment-only | trivial |
| `frontmatter` | frontmatter-only | trivial |
| `docs` | docs-prose | trivial |
| `other` | everything else, including `docs/LEDGER.md`, `skills/`, config, binaries outside code paths, deletions of non-code paths, and fence edits in docs | branch |

Worst file wins for the checkout: any `code` → `class: substantive`, `action:
branch_and_escalate`; else any `other` → `class: branch`, `action: save_branch`; else
one or more trivial files → `class: trivial`, `action: commit`; no tracked dirt →
`class: clean`, `action: resume`.

If `<root>/.git/MERGE_HEAD` exists, or any porcelain status has `U` in either column,
that result is replaced: `class` is `merge` and `action` is `escalate`. File rows stay
in `files` for the fact list. The on-base rule below does not change this action.

`base` is the checkout's project base, `.nw/project.yaml` key `base`, default `main`,
the same value `finish_close` compares to `git.current_branch` (`_close_cfg`:
`merge.base` when set, otherwise key `base`, otherwise `main`). The command takes
only `--root` and does not look up a campaign. Detached HEAD (`symbolic-ref` fails)
sets `on_base` false. When HEAD is not that base, `action` is `hold` and `class` stays
as computed, unless `class` is already `merge`. `on_base` is true only when
`git symbolic-ref --short HEAD` equals `base`.

The JSON schema name is `nw.main_dirty_plan.v1`. Keys: `schema`, `root`, `base`,
`on_base`, `class`, `action`, `paths` (every tracked dirty path), `substantive_paths`
(the `code` ones), `files` (each `{path, status, file_class}`). `class` is `clean`,
`trivial`, `branch`, `substantive`, or `merge`. `action` is `resume`, `commit`,
`save_branch`, `branch_and_escalate`, `hold`, or `escalate`.

### Permissions

The conductor's main-checkout writes go through **one audited verb**,
`nw queue main-dirty-apply`, not through raw git allow-rules. The conductor and its
sub-agent hold no raw git write verb for this clear. The sub-agent is spawned without
`isolation: worktree`, cwd the main checkout (the general skill rule that file-editing
sub-agents use `isolation: worktree` does not apply to this clear), and runs
`nw queue main-dirty-plan` and `nw queue main-dirty-apply` through the existing
`uv run nw *` allow rule. It writes no file itself.

**Allow-list** (`launch.py`): the read-only conductor git verbs (`status`, `log`, `diff`,
`show`, `rev-parse`, `rev-list`, `merge-base`, `ls-files`, `branch` listings,
`worktree list`), `git switch <base>` for each project's base branch name only, and
`uv run nw queue main-dirty-apply`. There is no allow rule for `git checkout`,
`git add`, `git commit`, `git rm`, `git worktree add`, `git hash-object`, or
`git update-index`.

**Deny-list** (`launch.py`, conductor-only: `_conductor_extra_deny` /
`_conductor_guard`, never the shared list the steward inherits). Every one of these
stays denied for the conductor and its sub-agents:

- `git checkout -- .` and every `git checkout -- <pathspec>`, and the raw
  `git checkout HEAD -- <path>`;
- `cp`;
- `git rm -r`, `git rm -rf`, and the raw `git rm -f -- <path>` (also under
  `git -C <wip>`);
- `git clean`;
- `git reset --hard`, and `git branch -D`;
- `git switch -f`, `git switch --force`, `git switch --discard-changes`,
  `git switch -C`, and `git switch --create`;
- every `git push` form the shared push deny list already covers (main/master, force);
- raw `git add --`, `git commit`, `git worktree add`, `git hash-object -w --`, and
  `git update-index` (the verb runs them itself, inside its own process).

`CONDUCTOR_CURSOR_POLICY` says the same in prose: a main_dirty clear runs
`uv run nw queue main-dirty-apply`; trivial edits may be committed on the base and
tracked paths restored to HEAD only through that verb; no raw checkout/add/commit, no
`reset --hard`, `clean`, `branch -D`, `switch -f`, or push.

### The audited verb: `nw queue main-dirty-apply`

New in `src/nightwright/agentic/main_dirty.py` (`apply_clear(root, plan=None)`) and
registered in `cli_queue.py`. Options: `--root PATH` (required), `--plan FILE` (an
`nw.main_dirty_plan.v1` JSON), `--json`. It prints `nw.main_dirty_apply.v1` JSON with
keys `schema`, `root`, `action`, `ok`, and when set `sha`, `wip_branch`, `wip_path`,
`wip_sha`, `escalate`, `replanned`, `error`. Exit 0 whenever the verb ran, including a
refusal (`ok: false` and `error`); exit 2 when `--root` is not a git worktree or the
`--plan` file is not a plan. It never runs `nw queue escalate`, `resume`, or `mailbox`;
those stay with the conductor.

**Always re-plan, never trust the file.** The verb calls `plan_clear(root)` on the live
checkout before any write. The `--plan` file is advisory: if its `action` or `paths`
differ from the live plan, the verb refuses with `error: plan is stale` and writes
nothing. Without `--plan` it uses the live plan.

**Refuse a merge in progress.** When the live plan is `class: merge` (`.git/MERGE_HEAD`
or a `U` porcelain status), the verb performs no git write of any kind (no add, commit,
checkout, rm, worktree add, switch) and returns `ok: false`, `escalate: true`,
`error: merge in progress`, whatever `action` the `--plan` file claims.

**Escalate substantive diffs.** `branch_and_escalate` (`class: substantive`) is saved to
a WIP branch (below) and never committed on the base. The verb returns `ok: true`,
`escalate: true` plus the WIP fields, so the conductor sends the `human_checkout`
escalation. The verb itself sends nothing.

**Save non-trivial diffs to a WIP branch before restoring.** For `save_branch`,
`branch_and_escalate`, and `hold` with tracked dirt, from the current HEAD it adds a
worktree `git worktree add -b wip/main-dirty-<YYYYMMDDThhmmssZ> <path> HEAD`, with
`<path>` = `<repo>/../.nw-worktrees/<project>/main-dirty-<YYYYMMDDThhmmssZ>` (the
`project.py` `worktree_root` parent). If the branch or directory exists it appends `-2`,
`-3`, `-4`. It copies each text path's bytes into the WIP tree, records a deletion with
`git rm -f -- <path>` in the WIP tree, and loads a binary path by `git hash-object -w`
plus `update-index --add --cacheinfo <mode>,<oid>,<path>` in the WIP tree. It commits
there with subject `WIP: stray main-checkout edits (<class>)` and one path per `-m`
body line. Only then does it verify that each WIP blob equals the main worktree blob
(absent for a deletion). Main is touched **only after** that check passes: per path,
`git checkout HEAD -- <path>` when the path exists in HEAD, otherwise `git rm -f --
<path>` for a newly added tracked path. All git calls use `--literal-pathspecs`; a path
that is absolute, contains `..`, a glob character, or names a directory is refused. A
failed copy, commit, or blob match leaves main untouched, removes the WIP worktree, and
returns `ok: false` with `error`. It never runs `reset --hard`, `clean`, `checkout --
.`, `rm -r`, or a push. Untracked files stay. It succeeds only when
`git status --porcelain --untracked-files=no` is empty and the WIP commit exists.

**Commit.** `commit` re-plans (the live action must still be `commit`, else
`ok: false`), stages only the plan `paths` one at a time with literal pathspecs, commits
with subject `chore: land stray trivial main-checkout edits` and one path per `-m` body
line, and succeeds only when main is tracked-clean afterwards. It returns the new `sha`.
No `commit -a`, no `add -A`, no push.

**Hold.** Saves any tracked dirt as above, switches to `base` with `git switch <base>`
only when the checkout is tracked-clean (never `-f`), re-plans, and returns the
`replanned` `on_base` and `action`.

**Resume and escalate actions.** For a live plan `resume` the verb returns
`ok: true` only when main is tracked-clean; for `escalate` it writes nothing and returns
`ok: false`, `escalate: true`.

### What the sub-agent does

`finish_close` is unchanged: it still parks, detail still lists up to ten porcelain
lines, `resume_to` is still `CLOSING`. The conductor does not run git in its own
context. On `parked.main_dirty`, and on a `conductor_ping` or `park_undecided` whose
park is an undecided `main_dirty`, it runs `nw queue main-dirty-plan --root <repo.root>
--json` and dispatches one background sub-agent with that JSON. One clear per
`repo.root` at a time. A second `parked.main_dirty`, `park_undecided`, or
`conductor_ping` for a root that already has a clear in flight does not dispatch
another sub-agent and writes no decision note. The in-flight sub-agent still resumes
every non-terminal `main_dirty` campaign of that root when the clear succeeds. The
sub-agent groups campaigns by `repo.root` and clears each root once. Every git write
of the clear is `nw queue main-dirty-apply --root <repo.root> --plan <plan.json>
--json`; the sub-agent never runs raw `git checkout`, `add`, `commit`, `rm`, or
`worktree add`.

**`escalate` (`class: merge`).** Do not run `main-dirty-apply`, and do not `git add`,
`git commit`, `git checkout`, `git rm`, or `git worktree add`. Escalate with no
campaign: `nw queue escalate --topic human_checkout --yes`, one option
`I will finish the merge in the main checkout`, `--recommend 1`, and a fact that a
merge is in progress. Do not resume.

**`hold`.** Do not commit onto the current branch and do not resume. Run
`main-dirty-apply` (it saves, restores, and switches to `base` per the verb), and if
`class` is `substantive` also escalate as in `branch_and_escalate`. Read the verb's
`replanned`. Resume only when it has `on_base` true and `action` `resume`. If it is
still `hold`, escalate `nw queue escalate --topic human_checkout --yes` with no
campaign, one option `I will put the main checkout on <base>`, `--recommend 1`, and a
fact that the checkout is still not on the base, and do not resume. Do not use the
`parked.main_not_on_base` row for a `main_dirty` park. This path does not also send
the failed-clear escalation.

**`commit`.** Run `main-dirty-apply`. A result with `ok: false` is a failed clear.

**`save_branch` and `branch_and_escalate`.** Run `main-dirty-apply`. A result with
`ok: false` is a failed clear and main was not touched by the save.

**Notify and escalate.** On `save_branch` and `branch_and_escalate`, mail with
`nw mailbox send --from conductor --to steward:<Project> --kind info` when
`nw mailbox list --to steward:<Project>` shows that reader, otherwise `--to human`,
with `wip_branch` and `wip_path` in the text. `<Project>` is the checkout's
`.nw/project.yaml` `project` value. On `branch_and_escalate` only, also
`nw queue escalate --topic human_checkout --yes` with no campaign, `--option "leave
the work on the WIP branch"`, `--option "I will move it myself"`, `--recommend 1`, and
one `--fact` per substantive path. The question names the branch, the worktree, and
those paths. No campaign argument means the command does not park anyone
`human_decision`. A `commit` sends the same mail, `--from conductor` and `--to` as
above, with the new `sha` and the paths in the text, and does not escalate.

**Resume.** After a successful clear, for every non-terminal campaign of that root
whose park reason is `main_dirty`: `nw queue resume <id> --to CLOSING --by conductor
--decision "cleared main (<action>, <sha-or-branch>)" --rule parked.main_dirty --yes`.
The decision note is written only together with that resume. A failed attempt writes
no decision note, so the park stays undecided. Resume is skipped when the plan action
is `escalate`, and when `hold` re-plans to anything other than `on_base` true and
`action` `resume`.

**Failed clear.** This applies when the verb returned `ok: false` for `commit`,
`save_branch`, or `branch_and_escalate`. Run the plan once more and try the new action
once. If that new action is `escalate`, follow the merge paragraph and do not run the
verb. If it is `hold`, follow the hold paragraph. If main is still tracked-dirty,
escalate with no campaign: `nw queue escalate --topic human_checkout --yes`, one option
`I will clean the main checkout`, `--recommend 1`, and the two `error` strings as
facts. Do not resume. A dirty main still blocks every close.

### Skill rows

In `skills/nw-conduct/SKILL.md`, split today's combined row:

- `parked.main_dirty` — the procedure in this Design. The conductor uses
  `main-dirty-plan` and does not re-classify by eye, and every git write of the clear is
  `uv run nw queue main-dirty-apply` (never raw `git checkout`, `add`, `commit`, `rm`,
  or `worktree add`). The row says a
  second wake for the same root does not start a second clear while one is in flight.
  For `class: merge` it forbids git writes and resume. It does not send the reader to
  the `parked.main_not_on_base` row.
- `parked.main_not_on_base` — keep the current behavior: switch back to the base when
  the checkout is tracked-clean; when it is dirty, move the tracked work onto a WIP
  worktree branch first, then switch back. Resume to `CLOSING` when main is on the
  base and tracked-clean. This row is for a `main_not_on_base` park only.

The `wt_broken` row stops saying "same as `main_dirty`". It says: save the tracked
dirt onto a WIP worktree branch, restore those paths on main, mail the steward where
it lives, and use `uv run nw` from a worktree until `nw` imports. It does not call
`main-dirty-plan` and it does not commit onto the base.

The `parked.main_dirty` rule id stays the one recorded on `nw queue decision` and on
resume.

## Alternatives considered

- **Leave `main_dirty` human-owned and only rewrite the skill.** The ping fingerprint
  and `park_undecided` would still ignore a park that the conductor missed, which is
  how the checkout sat all night.
- **Have `finish_close` commit or branch itself.** A wrong class would land from the
  runner with no conductor decision, and the idea names the conductor's sub-agent.
- **Save every diff to a branch, including frontmatter and comments.** The idea asks
  for those trivial edits to be committed so they are not left as a side branch.
- **Escalate every non-trivial diff.** A ledger line or a skill typo would page the
  human. Escalation is for substantive code, a merge in progress, a hold that stays
  off the base, and a clear that failed twice.
- **Keep the clear on the old allow-list and `isolation: worktree`.** That sub-agent
  cannot copy, cannot restore main, and cannot record a deletion, so every non-commit
  clear fails closed or a denied command is skipped and main is touched anyway.
- **Widen the conductor's raw git allow-rules** (`checkout HEAD -- *`, `rm -f -- *`,
  `add --`, `commit -m`, `update-index`, `hash-object`) so the sub-agent runs the
  steps itself. A glob over those verbs can be bent (a literal `*` pathspec, `-C` into
  another tree, a `..` escape) and nothing checks the WIP blob before main is touched.
  One verb that re-plans, validates paths, and orders save-then-restore is smaller to
  audit, and the deny list can keep every raw write denied.

## Acceptance criteria

- [x] `park_owner("main_dirty")` is `conductor`. `main_not_on_base`, `human_decision`,
      and `restart` stay `human`.
- [x] An undecided `main_dirty` park older than `park_decide_s` is overdue, appears in
      Conductor handling, is absent from Needs you, and produces one `park_undecided`.
      A note after the park clears the overdue flag.
- [x] `quiet.ping_fingerprint` includes `["parked", <campaign>]` for that undecided
      park, and `quiet.classify` returns `wake` for a `parked` / `main_dirty` event
      that is not `auto`, not `red_main`, and not `by: conductor`.
- [x] Herdr emits `nw: parked` for a `main_dirty` park. The inbox has no `main_dirty`
      host-fix row. E5 does not treat `main_dirty` as a human touchpoint.
- [x] `plan_clear` returns `commit` / `trivial` for a frontmatter-only spec edit, a
      docs prose edit outside fences (`docs/**/*.md` except `docs/LEDGER.md`,
      `README.md`, `CHANGELOG.md`), and a comment-only edit under `src/`. It returns
      `save_branch` / `branch` for `docs/LEDGER.md`, a docs fence edit, and
      `skills/**`. It returns `branch_and_escalate` / `substantive` when any file is
      a non-comment change on a code path, including when trivial files are dirty
      beside it. A delete or binary on a code path is substantive. A delete of a doc
      is `branch`. No tracked dirt returns `resume` / `clean`. Untracked files do not
      appear in `paths`. A one-line `*ptr = 1;` edit under `src/` (fixture
      `star_deref`, file `src/star_deref.c`) is `branch_and_escalate` / `substantive`.
      A one-line ` * kept comment` edit under `src/` (fixture `star_comment`, file
      `src/star_comment.c`) is `commit` / `trivial`. A checkout with `.git/MERGE_HEAD`,
      or any porcelain status with `U` in either column, returns `escalate` / `merge`
      (fixture `merge_conflict`).
- [x] `plan_clear`'s `base` is the checkout's project base from `.nw/project.yaml` key
      `base`, default `main`, the same value `finish_close` compares to
      `git.current_branch`. The command takes `--root` and does not look up a campaign.
      On a repo whose project base is not `main`, a trivial diff on that branch stays
      `class: trivial` with `action: commit`; on another branch the same diff is
      `action: hold` and `class` stays `trivial`. Detached HEAD (`symbolic-ref` fails)
      sets `on_base` false and, unless `class` is `merge`, `action` `hold`.
- [x] `nw queue main-dirty-plan --root PATH --json` prints `nw.main_dirty_plan.v1` and
      exits 0. A non-git `--root` exits 2. The JSON `class` set includes `merge` and
      the JSON `action` set includes `escalate`.
- [x] The conductor's main-checkout writes go through `nw queue main-dirty-apply`.
      `tests/test_agentic_launch.py` shows `uv run nw queue main-dirty-apply --root
      <r> --json` permitted, `git switch <base>` permitted for the project base name
      only, and every one of these denied for the conductor: `git checkout -- .`,
      `git checkout HEAD -- <path>`, `cp`, `git rm -r`, `git rm -f -- <path>` (also
      under `git -C <wip>`), `git clean`, `git reset --hard`, `git switch -f`,
      `git switch --discard-changes`, raw `git add --`, `git commit -m`,
      `git worktree add`, `git hash-object -w --`, `git update-index`, and the push
      forms. The steward's allow rules for `git -C <dir> add|commit` and
      `git worktree add ../.nw-worktrees/*` are unaffected.
      `CONDUCTOR_CURSOR_POLICY` names `main-dirty-apply`, allows trivial edits on the
      base and restoring tracked paths to HEAD only through it, and still forbids push,
      `reset --hard`, and `clean`.
- [x] `nw queue main-dirty-apply --root PATH [--plan FILE] --json` prints
      `nw.main_dirty_apply.v1` and exits 0 for a refusal or a failed step (`ok: false`
      and `error`), and exits 2 for a non-git root or a `--plan` that is not a plan.
- [x] The verb refuses a merge in progress: with `.git/MERGE_HEAD`, or a `U` porcelain
      status, it makes no git write (HEAD, index, worktree files, and branch list
      unchanged) and returns `ok: false`, `escalate: true`, `error: merge in progress`,
      even when the `--plan` file claims `commit` or `save_branch`.
- [x] The verb re-plans from the live checkout: a `--plan` whose `action` or `paths`
      differ from the live plan returns `ok: false`, `error: plan is stale`, with no
      write. A `commit` whose live action is not `commit` also writes nothing.
- [x] The verb escalates substantive diffs: a `branch_and_escalate` plan saves the
      paths to a `wip/main-dirty-<ts>` branch under `.nw-worktrees/<project>/`, never
      commits on the base, and returns `ok: true`, `escalate: true`, `wip_branch`,
      `wip_path`, `wip_sha`. It does not call `nw queue escalate`.
- [x] The verb saves every non-trivial diff to the WIP branch before restoring main:
      the WIP commit has subject `WIP: stray main-checkout edits (<class>)`, each WIP
      blob equals the main blob (a deletion is absent) before any `git checkout HEAD --
      <path>` or `git rm -f -- <path>` runs on main, and a forced mismatch leaves main
      byte-identical, removes the WIP worktree, and returns `ok: false`. A name clash
      appends `-2`, then `-3`. Untracked files stay. After success
      `git status --porcelain --untracked-files=no` is empty.
- [x] A literal `*` or `..` or absolute or directory path in the plan is refused by the
      verb with no write; the verb never runs `reset --hard`, `clean`, `checkout --
      .`, `rm -r`, or a push (asserted on the git argv the tests record).
- [x] The `parked.main_dirty` skill row names `main-dirty-plan`, `main-dirty-apply`, the commit subject
      `chore: land stray trivial main-checkout edits`, the WIP branch prefix
      `wip/main-dirty-`, resume to `CLOSING` with rule `parked.main_dirty`, and
      `human_checkout` for substantive code, for a merge in progress, for a hold that
      is still `hold`, and for a clear that stays dirty. Those escalations include
      `--yes`. The mail includes `--from conductor` and `--to`. The row says a second
      wake for the same root does not start a second clear while one is in flight.
      For `class: merge` it forbids `git add`, `git commit`, `git checkout`, `git rm`,
      and `git worktree add`, and it does not resume. `hold` does not follow the
      `parked.main_not_on_base` row, and resume happens only when a later plan has
      `on_base` true and `action` `resume`. The `parked.main_not_on_base` row is
      separate. The `wt_broken` row does not tell the conductor to commit onto the base.
      The row tells the sub-agent to use `main-dirty-apply` and no raw git write.
- [x] `finish_close` still parks `main_dirty` with `resume_to: CLOSING` when the base
      checkout has tracked dirt, and still merges once that checkout is tracked-clean.

## Test plan

- **Automated tests:** new `tests/test_main_dirty_clear.py` with exactly one module
  marker, `pytestmark = pytest.mark.fast`. It builds file records and temp git repos
  and asserts each class, action, and path list in the acceptance criteria, including
  worst-class-wins, `docs/LEDGER.md`, fence edits, comment-only Python, a logic change
  beside a docs edit, deletions, a binary under `src/`, untracked files ignored, and
  `hold` off the base branch. Fixtures, named in the assertions:
  `star_deref` (`src/star_deref.c`, one-line `*ptr = 1;`) is
  `branch_and_escalate` / `substantive`; `star_comment` (`src/star_comment.c`, one-line
  ` * kept comment`) is `commit` / `trivial`. A temp-repo `merge_conflict` fixture
  with `MERGE_HEAD`, and one whose porcelain has `U` in either column, returns
  `escalate` / `merge`. A repo whose `.nw/project.yaml` `base` is not `main`: on that
  branch a trivial diff stays `class: trivial` with `action: commit`; on another
  branch the same diff is `action: hold`. Detached HEAD sets `on_base` false. It
  asserts `quiet.classify` wakes a non-auto `main_dirty` park, and it reads
  `skills/nw-conduct/SKILL.md` for the row phrases in the criteria, including `--yes`
  on the `human_checkout` escalations, `--from conductor` and `--to` on the mail, the
  second-wake sentence, the merge git-write ban, and that `hold` does not name the
  `parked.main_not_on_base` procedure. CLI: `main-dirty-plan --json` exit 0 and a
  non-git root exit 2. The JSON includes `class: merge` and `action: escalate` on the
  merge fixture. The same module (one marker, no second tier) holds the
  `main-dirty-apply` refusal tests, each on a temp git repo with a real `git status`
  / `git rev-parse` / `git branch --list` before-and-after comparison:
  `test_apply_refuses_merge_in_progress` (`MERGE_HEAD` and a `U` status fixture, with a
  `--plan` file claiming `commit`; HEAD, index, worktree files, and branch list
  unchanged; `ok: false`, `escalate: true`, `error: merge in progress`),
  `test_apply_refuses_stale_plan` (plan `paths` or `action` differ from live),
  `test_apply_commit_aborts_when_replan_not_commit`,
  `test_apply_substantive_saves_wip_and_flags_escalate` (WIP branch under
  `.nw-worktrees/<project>/`, no commit on the base, `escalate: true`),
  `test_apply_saves_wip_before_restoring_main` (records git argv order: WIP commit and
  blob check precede every `checkout HEAD --` / `rm -f --` on main; text, binary,
  deletion, and newly added paths),
  `test_apply_wip_mismatch_leaves_main_untouched` (monkeypatched copy mismatch; main
  byte-identical, WIP worktree removed, `ok: false`), `test_apply_wip_name_clash_suffix`
  (`-2`, `-3`), `test_apply_untracked_files_stay`, `test_apply_hold_switches_base_only_
  when_clean`, `test_apply_rejects_bad_pathspec` (literal `*`, `..`, absolute,
  directory), `test_apply_never_runs_forbidden_git` (recorded argv has no `reset
  --hard`, `clean`, `checkout -- .`, `rm -r`, `push`), and the existing
  `test_apply_commit_trivial` / `test_apply_save_branch_literal_star_path`. CLI:
  `main-dirty-apply --json` exit 0 on a refusal, exit 2 on a non-git root and on a
  malformed `--plan` file.
- **Updates to existing modules** (each already has its one tier marker; this spec adds
  no second marker):
  - `tests/test_agentic_queue.py::test_park_owner_table` and
    `test_queue_payload_park_keys_decision_rules` — `main_dirty` is conductor-owned;
    past `park_decide_s` it is overdue.
  - `tests/test_agentic_events.py::test_park_undecided_fires_once_per_park_and_stops_on_a_note`
    keeps a single firing conductor park by using `main_not_on_base` as the
    human-owned control (that campaign does not fire). The stale `main_dirty` fire is
    covered in `test_ping_digest_owner_overdue_and_counts`: owner `conductor`,
    overdue, not `needs_you`, and `undecided_parks` counts it.
  - `tests/test_agentic_quiet.py` — fingerprint contains the parked campaign.
  - `tests/test_herdr.py::test_notifications_once_per_event_are_silent` — the
    `main_dirty` park adds a second `nw: parked` toast.
  - `tests/test_agentic_mvp_report.py::test_e5_pr_campaign` — the human touchpoint in
    that test is `main_not_on_base` (or the escalation alone). A `main_dirty` park is
    not in `touchpoints`.
  - `tests/test_agentic_launch.py` (marker already on the module) —
    `test_conductor_default_rules_git_work_vs_dangerous`: `uv run nw queue
    main-dirty-apply --root /w --json` and `git switch <base>` (project base name
    only; `git switch trunk` denied) are permitted; denied: `git checkout -- .`,
    `git checkout HEAD -- <path>`, `cp`, `git rm -r`, `git rm -f -- <path>` and
    `git -C <wip> rm -f -- <path>`, `git clean`, `git reset --hard`, `git switch -f`,
    `git switch --discard-changes`, `git add --`, `git commit -m`, `git worktree add`,
    `git hash-object -w --`, `git update-index --add --cacheinfo`, and the push forms.
    The steward-side tests (`git -C <dir> add|commit`, `git worktree add
    ../.nw-worktrees/*`) keep passing, pinning that the conductor guard is not in the
    shared deny list. `test_conductor_cursor_policy_text`: the policy names
    `main-dirty-apply`, allows committing trivial edits on the base and restoring
    tracked paths to HEAD through it, and still forbids push, `reset --hard`, and
    `clean`.
  - Dashboard fixture and tests that put `main_dirty` in Needs you
    (`tests/fixtures/dashboard_data.py`, `tests/test_dashboard_cli.py`,
    `tests/test_dashboard_snapshot.py`, `tests/test_dashboard_page.py`,
    `tests/test_agentic_dashboard.py`, `tests/test_agentic_queue_cli.py`,
    `tests/test_tui_queue_model.py::test_load_queue_groups_order_and_cells`,
    `tests/test_tui_queue_model.py::test_load_queue_needs_you_and_overdue`) — that
    row is Conductor handling. `main_not_on_base` stays Needs you with its host fix.
    In `test_load_queue_groups_order_and_cells` the `main_dirty` row is in the
    conductor group, not Needs you (`SPEC-0001-dirty` was the only Needs you row, so
    that header is absent). In `test_load_queue_needs_you_and_overdue` the row is
    `("conductor", ids["dirty"])`, not `("needs you", ids["dirty"])`, and the
    conductor order gains that campaign by parked-at (it is the oldest in
    `_park_fixture`), so the order is
    `[ids["dirty"], ids["rebase"], ids["moving"], ids["req"]]`.
    `tests/test_agentic_dashboard.py::test_park_split_lists_and_totals` drops
    `ids["dirty"]` from `needs_you`; `conductor_handling` includes it (overdue first,
    then oldest).
  - `tests/test_skills.py` row parsing still sees one `parked.main_dirty` rule id and
    one `parked.main_not_on_base` rule id.
- **Regression guard:** `tests/test_agentic_close.py` and
  `tests/test_agentic_mvp_integration.py` still show a tracked edit parks `main_dirty`
  and a clean checkout resumes to a merge. `python3 tools/affected_tests.py --run uv
  run pytest -q` on the touched files, then `python3 tools/spec_lint.py`. When
  `tools/test_tiers.py` exists, `python3 tools/test_tiers.py --check` exits 0.
- **Manual verification:** none. The temp-repo tests cover `plan_clear` and
  `apply_clear` against a real git checkout; the skill-row assertions are the
  automated check of the sub-agent's procedure.

## Rollout / migration

No stored owner field and no data move. Deploying the owner-table change reclassifies
every existing `main_dirty` park on the next queue read. One that is already older
than `park_decide_s` and has no decision note emits `park_undecided` once, which wakes
the conductor to clear it. Campaigns parked `main_not_on_base` stay in Needs you.
The allow/deny change is in the conductor launch defaults, so a conductor started
after the deploy can run the clear. SPEC-0382 is not superseded. This revision amends
the permission design from raw git allow-rules to the single audited verb; the code and
tests that predate the amendment (raw `checkout HEAD --` / `rm -f --` allow tails in
`launch.py`, tests that permit them) are aligned to the amended criteria and nothing
else changes. Before the campaign merges (reaches `CLOSING`), the conductor mails
`steward:Meta-Tools` the final conductor allow and deny list from `launch.py`
(`_conductor_extra_allow`, `_conductor_extra_deny`, `DEFAULTS["conductor_*"]`) and the
names and results of the `main-dirty-apply` refusal tests.

## Definition of done

- [x] Acceptance criteria all met.
- [x] The final launch.py allow/deny list and the `main-dirty-apply` refusal test
      results were sent to `steward:Meta-Tools` before the CLOSING merge.
- [x] Test plan executed; the new module and the updated tests pass. `python3
      tools/spec_lint.py` exits 0. `python3 tools/test_tiers.py --check` exits 0.
- [x] No regressions in the close, dashboard, herdr, launch, and E5 tests named above.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

### History (before the amendment)

The records below describe the earlier raw-allow-rule permission design (sub-agent
running `git checkout HEAD --`, `git rm -f --`, `update-index` itself). The Permissions
and verb sections above supersede that design; these records stay as the timeline.

### Test record (2026-11-10)

Ran `uv run pytest -q tests/test_main_dirty_clear.py` (23 tests), targeted updates in
`test_agentic_launch`, `test_agentic_queue`, `test_agentic_events`, `test_agentic_dashboard`,
`test_tui_queue_model`, `test_herdr`, `test_agentic_mvp_report`, `test_dashboard_*`, and
`python3 tools/test_tiers.py --check`. Full suite left to gating.

### Test record (2026-11-10 gate loop 1)

Fixed stale park-split expectations after `main_dirty` conductor ownership: five tests in
`test_agentic_dashboard` and `test_agentic_queue_cli` (lookup by campaign id, empty Needs you
section, overdue `main_dirty` marker). All five passed; `python3 tools/spec_lint.py` exit 0.
Full suite left to gating.

### Test record (2026-11-10 gate loop 2)

`test_existing_payload_keys_and_values_are_unchanged`: added human `main_not_on_base` park
after `_park_fixture` so `needs_you` is non-empty for AC9 key contract. Targeted
`uv run pytest -q tests/test_agentic_dashboard.py::test_existing_payload_keys_and_values_are_unchanged`
passed; `python3 tools/spec_lint.py` exit 0. Full suite left to gating.

### Test record (2026-11-10 gate loop 3)

Loop-3 review fixes: `plan_clear` diff/porcelain/fence/MERGE_HEAD classification,
conductor allow-list (project `switch <base>`, deny full-tree checkout/rm, WIP-only
`update-index`, no `add -A`/`commit -am`), expanded `parked.main_dirty` skill row and
fixtures in `tests/test_main_dirty_clear.py` and `tests/test_agentic_launch.py`.
`uv run pytest -q tests/test_main_dirty_clear.py` (35 tests) and conductor allow-list
parametrize passed; `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py`
exit 0. Full suite left to gating.

### Test record (2026-11-11 campaign loop 1)

Review fixes: `plan_clear` LEDGER/NUL/binary classification, backtick-only fences,
binary UTF-8-safe diffs; conductor deny broadened pathspecs and `update-index` `..`
escape; `parked.main_dirty` skill WIP commit, escalation options, failed-clear branching.
`uv run pytest -q tests/test_main_dirty_clear.py` (49 tests),
`test_agentic_launch.py::test_conductor_default_rules_git_work_vs_dangerous`,
`test_e5_main_dirty_park_not_a_touchpoint`, `test_conduct_skill_rule_ids`; `python3
tools/test_tiers.py --check` and `python3 tools/spec_lint.py` exit 0. Full suite left to
gating.

### Test record (2026-11-10 campaign loop 2)

Gate fix: removed `add -A` / `add --all` globs from `_CONDUCTOR_CLEAR_DENY` so the shared
guard no longer denies steward `git -C <dir> add -A`; conductor bare `git add -A` / `git add
--all` moved to `conductor_disallowed_tools`. `uv run pytest -q
tests/test_agentic_launch.py::test_steward_c_swallow_family_keeps_legit_commands_and_pins_false_positive
tests/test_agentic_launch.py::test_conductor_default_rules_git_work_vs_dangerous` (56 passed);
`python3 tools/spec_lint.py` and `python3 tools/test_tiers.py --check` exit 0. Full suite left
to gating.

### As shipped (2026-11-10 campaign loop 2)

Conductor `git add -A` denial is `Bash(git add -A)` and `Bash(git add --all)` on
`conductor_disallowed_tools` only, not `_CONDUCTOR_CLEAR_DENY` glob forms that matched
`git -C <dir> add -A` under the steward allow `add *`.

### Test record (2026-11-10 campaign loop 0)

Review fixes: `plan_clear` merge only on `MERGE_HEAD` or porcelain `U`, staged diffs,
rename both paths, LF-only line splits; conductor pathspec/commit/switch-base guards; skill
merge git-write phrases. `uv run pytest -q tests/test_main_dirty_clear.py` (56 tests),
`test_agentic_launch.py::test_conductor_default_rules_git_work_vs_dangerous` and launch base
tests; `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py --write-ledger`.
Full suite left to gating.

### Test record (2026-11-10 campaign loop 1)

Review fixes: conductor pathspec class denies, per-project directory denies at launch,
narrow main-checkout allow tails (`*/*`, `*.*`, `.*`), WIP git only under
`.nw-worktrees`, extended commit/pathspec denies; `plan_clear` skills/LEDGER rename
classification, fence-aware frontmatter, porcelain `AA`/`DD` merge; skill row
`--literal-pathspecs`; tests in `test_main_dirty_clear.py`, `test_agentic_launch.py`,
`test_agentic_events.py`. `uv run pytest -q tests/test_main_dirty_clear.py
tests/test_agentic_launch.py` (393 passed); `python3 tools/test_tiers.py --check` and
`python3 tools/spec_lint.py` exit 0. Full suite left to gating.

### As shipped (2026-11-10 campaign loop 1)

Main-checkout restore/stage/hash-object allow rules use `*/*`, `*.*`, and `.*` tails
instead of a bare `checkout HEAD -- *` glob so a literal `*` pathspec is not matched.
WIP `commit -m` is allowed on main and under `git -C */.nw-worktrees/*` only.

### Test record (2026-11-10 campaign loop 3)

Gate fix: aligned `tests/test_agentic_launch.py` with full
`DEFAULTS["conductor_disallowed_tools"]` (AskUserQuestion plus bare `git add -A` /
`git add --all` denies). Seven targeted launch tests (61 passed);
`python3 tools/spec_lint.py` exit 0. Full suite left to gating.

### Test record (2026-11-10 campaign loop 3 gate fix)

Moved the conductor guard rules (`_conductor_guard()`) out of the shared
`DEFAULTS["disallowed_tools"]` into `_conductor_extra_deny`, so the steward no longer inherits
them (its `git -C <dir> commit` and `git worktree add ../.nw-worktrees/...` work again). Aligned
three stale launch tests with the dedup'd conductor deny list and the `main-dirty-apply` allow-list.
`uv run pytest -q tests/test_agentic_launch.py tests/test_main_dirty_clear.py` (409 passed);
`python3 tools/spec_lint.py` and `python3 tools/test_tiers.py --check` exit 0. Full suite left to
gating.

### Test record (2026-11-11 amended design)

Aligned code to the amended permission design. `apply_clear` now re-plans the live checkout before
any write: a merge (`MERGE_HEAD` / `U`) returns `ok: false`, `escalate: true`, `error: merge in
progress` with no git write, whatever the `--plan` file claims; a `--plan` whose `action` or `paths`
differ returns `plan is stale`; `base` comes from the project, never the plan; `branch_and_escalate`
(and a substantive `hold`) return `escalate: true`. The WIP blob check now reads the blob from the
WIP commit (the old check read the WIP worktree files and failed for binary paths). The shared steward
guard keeps the `rm` swallow deny (`git * rm *`); `rm -r*`/`rm -rf*` moved to the conductor-only deny.
Dropped the quote check in `_PATH_BAD`. A literal `*` path is accepted only when it really exists
(all git calls use `--literal-pathspecs`); a nonexistent glob, `..`, absolute, or directory path is refused.
Ran `uv run pytest -q tests/test_main_dirty_clear.py` (80 passed, incl. the new `test_apply_*` refusal
tests with real before/after `git status`/`rev-parse`/`branch --list` snapshots and recorded git argv),
`uv run pytest -q tests/test_agentic_launch.py` (343 passed, incl. the steward `git -C /w rm -f
--ignore-unmatch -- f "x log y"` denial), `python3 tools/test_tiers.py --check` and
`python3 tools/spec_lint.py --write-ledger` (exit 0). `steward:Meta-Tools` was mailed the final
conductor allow/deny list and refusal-test names/results (message 891). Full suite left to gating.

### As shipped (2026-11-11 amended design)

`apply_clear` does not abort on a rebase/cherry-pick/revert in progress unless porcelain shows `U`
(not specified; noted as a follow-up). A failed WIP save removes the WIP worktree but leaves the
`wip/main-dirty-*` branch ref (no `branch -D`, which the conductor deny list keeps off). The WIP
worktree copy of a binary path is also written to disk so it matches the index.

### Test record (2026-11-10 campaign loop 1 review fixes)

Review fixes to `apply_clear` and the skill rows. New tests in `tests/test_main_dirty_clear.py`
cover the commit subject/body/sha, the hold paths (substantive escalate, save failure with no
`switch`, `switch` failure with `replanned` still `hold`), live-path validation through
`apply_clear`, the `_commit_trivial` guard, the WIP save failures and `resume`/idempotency, symlink,
staged-only and exec-bit dirt, sequencer and detached-HEAD refusals, an option-shaped base, malformed
`--plan` paths, backslash names, the top-of-checkout `--root` rule, and the plan/apply text output;
plus `test_agentic_launch.py` (cursor policy `only through`, base-name shapes),
`test_dashboard_inbox.py` (no `main_dirty` host fix) and `test_agentic_events.py` (a real
`main_dirty` park reaches the ping fingerprint). `uv run pytest -q tests/test_main_dirty_clear.py
tests/test_agentic_launch.py tests/test_agentic_events.py tests/test_dashboard_inbox.py
tests/test_skills.py` passed; `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py`
exit 0. Full suite left to gating.

### As shipped (2026-11-10 campaign loop 1 review fixes)

- `apply_clear` also refuses (`ok: false`, `escalate: true`, `error: sequencer in progress`) while a
  rebase, cherry-pick or revert is in progress, and `hold` refuses a detached HEAD that no branch
  contains (`detached HEAD not on any branch`) before any switch. `hold` refuses a `base` that is
  option-shaped or fails `git check-ref-format --branch`.
- The WIP save refuses, before any write, a symlink path (or a path under one) and index content that
  differs from both HEAD and the worktree; the exec bit is saved and verified. Any `GitError` /
  `OSError` / `ValueError` during the clear is `ok: false` JSON; once the WIP commit exists the
  error carries `wip_branch` / `wip_sha`. A failed save that made no commit also deletes its empty
  branch (`git branch -d`, inside the verb), superseding the earlier "leaves the ref" note.
- `--root` must be the top of the checkout; `--plan` `paths` must be a list of strings (else exit 2);
  porcelain paths are no longer backslash-rewritten. `_branch_ref_ok` (conductor `switch <base>`
  allow rule) rejects shell/rule metacharacters and a leading `-`.
- Deviation from the Skill rows text: with no raw git write rule the conductor cannot perform the
  dirty `parked.main_not_on_base` save or the `wt_broken` save itself. The `main_not_on_base` row now
  runs `main-dirty-apply` (its `hold` action saves then switches); the `wt_broken` row mails the
  steward / escalates `human_checkout` to have the save and restore performed, since `nw` (hence the
  verb) does not import there.

### Test record (2026-11-10 campaign loop 2 review fixes)

Review fixes: the shared `-C * * <read-verb>` and `switch -f` guards stay in the steward's deny list
(only the new raw-write denies are conductor-only; new steward denial of `git -C /w tag -d v1 "x log
y"`); `_commit_trivial` refuses an index blob that differs from HEAD and the worktree and unstages on
a failed commit; the restore re-verifies each path against the WIP blob right before its checkout; a
non-blocking flock in the git dir enforces one clear per checkout; `hold` reports `wip_*` and
`escalate` even when the switch fails; CLI plan/apply print a stderr message, exit 2 only for a
non-worktree or a bad `--plan`, exit 3 for a failed git read, and apply turns a git failure into
`ok: false` JSON. Tests: literal-`*` siblings on the commit and restore paths with a literal-pathspecs
assertion on every recorded main call (mutation-checked), WIP mismatch on binary/deleted/added paths,
commit-path untracked files, staged-only refusal, failed-commit unstage, hold switch failure,
lock, CLI codes, `parked.main_not_on_base` resume condition; fixed the precedence in
`test_apply_live_path_validation_refuses`. `uv run pytest -q tests/test_main_dirty_clear.py
tests/test_agentic_launch.py tests/test_skills.py` passed; `python3 tools/test_tiers.py --check` and
`python3 tools/spec_lint.py` exit 0. Full suite left to gating.

### As shipped (2026-11-10 campaign loop 2 review fixes)

- The `parked.main_not_on_base` row resumes to `CLOSING` only when `main-dirty-apply` returns
  `ok: true` and `replanned` has `on_base` true and `action: resume`; otherwise it escalates
  `human_checkout` (with `--yes`, no campaign) and does not resume.
- The Design's "Skill rows" paragraph (guarded text) still describes `wt_broken` as saving and
  restoring itself and `main_not_on_base` as a plain switch-and-resume. What shipped is the
  verb-based `main_not_on_base` row and a `wt_broken` row that asks the steward to do the save (the
  verb needs `nw` to import); the 2026-11-10 loop 1 "As shipped" note and this one are the record.
- `main-dirty-plan` exits 3 (stderr message) when git itself fails on a real worktree.
- Only the steward-shared guard split was needed: `_conductor_guard()` (switch and read-verb
  swallow denies) is back in `DEFAULTS["disallowed_tools"]`; `_conductor_write_guard()` is
  conductor-only.

### Test record (2026-11-10 scoped loop: skill text and trivial-commit hardening)

`skills/nw-conduct/SKILL.md`: the dispatch paragraph and the sub-agent permission-denial paragraph
no longer tell anyone to run raw `git add` / `git commit` (the conductor deny list is inherited by
its sub-agents); the main-checkout clear goes through `uv run nw queue main-dirty-apply`. The
`parked.main_dirty` row again says one clear per `repo.root` at a time and a clear already in
flight; `test_skill_row_phrases` asserts both. `_commit_trivial` refuses a symlink path like
`_precheck_save`; a typechange (`T`) or mode-only change is file class `other`, never
docs/comment/frontmatter. New tests `test_commit_trivial_refuses_symlink` and
`test_typechange_and_mode_only_are_other_not_trivial`. `uv run pytest -q
tests/test_main_dirty_clear.py tests/test_skills.py tests/test_agentic_launch.py`;
`python3 tools/test_tiers.py --check`; `python3 tools/spec_lint.py`. Full suite left to gating.

### Test record (2026-11-10 campaign loop 1 review round 3)

Review fixes. `apply_clear` turns a malformed `.nw/project.yaml` (`worktrees:` not a mapping,
`worktrees.root` not a string, a `project` with a path separator) into `ok: false` JSON before any
write (`_wip_parent`), and the verb and CLI also catch `AttributeError`/`TypeError`. Commit failures
(`commit failed`, `WIP commit failed`) now carry git's stderr (hooks, gpg, identity). `hold` re-reads
`base` after the save/restore so a stray `project.yaml` edit cannot pick the switch target.
`_is_docs_prose` needs a visible diff, and a symlink (worktree or HEAD) is file class `other`, so the
plan no longer says `commit` for dirt the verb refuses. Tests: comment-line suffix table, the seven
unmerged porcelain codes, `_forbidden` switch clause, a live-plan bad-pathspec refusal, malformed
`worktrees`, hook stderr, hold base re-read, empty-diff docs, retargeted symlink; fixed
`test_push_guard_rule_count`. `uv run pytest -q tests/test_main_dirty_clear.py
tests/test_agentic_launch.py tests/test_skills.py` passed; `python3 tools/test_tiers.py --check` and
`python3 tools/spec_lint.py` exit 0. Full suite left to gating.

### As shipped (2026-11-11 re-acceptance)

The human approved the amended spec (raw git allow-rules replaced by the audited
`nw queue main-dirty-apply` verb; gate waived once, ESC-0066). A substantive diff in the human's
main checkout is saved to a `wip/main-dirty-<ts>` branch under `.nw-worktrees/<project>/` and the
human is escalated (`nw queue escalate --topic human_checkout --yes`, no campaign), so their edit
leaves main but is not lost. The verb itself sends nothing; the conductor does the mail and the
escalation. No code changed in this loop: the implementation already matched the amended Design.

### Test record (2026-11-11 re-acceptance)

`uv run pytest -q tests/test_main_dirty_clear.py tests/test_skills.py tests/test_agentic_launch.py`;
`python3 tools/test_tiers.py --check`; `python3 tools/spec_lint.py`. Full suite left to gating.

### Test record (2026-11-11 review round 4)

Review fixes: `Bash(cp *)` is now a conductor-only deny rule (not in the steward's); the
`skills/` / `docs/LEDGER.md` shortcut no longer covers code-suffix files, so a logic edit to
`skills/x/run.py` is `substantive` while `skills/x.md` stays `branch`; the `hold` skill
paragraph names the verb's `replanned`. New tests: `test_conductor_denies_cp_by_rule_steward_does_not`,
`git branch -D` conductor cases, `test_skills_script_with_code_suffix_is_substantive`, the
blob-check argv-order assertion in `test_apply_saves_wip_before_restoring_main`,
`test_apply_runs_only_git_never_nw`, and a `replanned` assertion in `test_skill_row_phrases`.
`uv run pytest -q tests/test_main_dirty_clear.py tests/test_agentic_launch.py tests/test_skills.py`
(619 passed); `python3 tools/test_tiers.py --check` and `python3 tools/spec_lint.py` exit 0.
Full suite left to gating.

## Open questions

None.
