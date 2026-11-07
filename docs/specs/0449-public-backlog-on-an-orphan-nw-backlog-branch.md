---
id: SPEC-0449
title: Public backlog on an orphan nw-backlog branch
status: proposed
owner: user
created: 2026-11-07
kind: feature
tags: [public-backlog]
source_idea: IDEA-1019
---

## Context

A public backlog cannot live on the project's default branch. On a protected
branch every new item and every title or summary edit is a pull request, and a
backlog cannot bear that. The item list also cannot be an index file in that
tree: every create would conflict on the index.

nightwright already reads foreign blobs with `git show <ref>:<path>` and already
builds commits through a temporary index (`src/nightwright/agentic/gitops.py`).
It has no backlog ref, no per-item file format, and no push of a branch other
than a campaign branch.

The store has to be a real branch. A ref outside `refs/heads`, such as
`refs/nw/backlog`, is the same commit and does not show up as a branch. GitHub
accepts pushes to branches and tags, so the branch is `refs/heads/nw-backlog`.
The file format does not record which ref points at the commit.

## Goals / Non-goals

**Goals**

- One backlog item is one file on an orphan `refs/heads/nw-backlog` in the
  project's git repo, named only by its id, so a title change does not rename it.
- nightwright reads those files with `git show` and writes them with a temporary
  index and `commit-tree`. The product checkout, its index, and its current
  branch do not move.
- The commit's author and committer are the git identity of the person running
  the command. A write is refused when `user.name` or `user.email` is unset.
- A push is a fast-forward of `nw-backlog`. Different files merge. The same file
  stops and a person chooses. nightwright does not force-push, and it does not
  delete the branch.
- Creator and modifier displayed for an item come from the commits that added
  and last changed that file. A merge that only brought in other files does not
  count. When the frontmatter copy disagrees, history is the authority.

**Non-goals**

- An operation log, per-item refs, or Lamport clocks. Concurrent comments that
  both survive are out of scope. git-bug is that design; this backlog's write
  is a new file or a replacement of one file's title, summary, status, or spec id.
- GitHub issues, or any forge issue mirror. That is a later opt-in, not this store.
- Calling the forge API to install branch rules. This spec states the rule the
  forge must already enforce.
- Storing an `IDEA-NNN`, a visibility flag, comments, labels, assignees, or a
  sort order.
- Deleting an item, renaming its file, or composing a third text during a
  conflict. The person takes one existing side, then may `edit` afterwards.
- A TUI or dashboard editor for the backlog.

## Decision

The public backlog of a project is the orphan branch `refs/heads/nw-backlog` in
that project's git repository. Each item is one markdown file at the branch
root, `REQ-` plus eight Crockford characters, minted on the machine that creates
the file. A fixed `README.md` is the only other file nightwright adds. There is
no index file.

The frontmatter holds `id`, `title`, `status` (`open` or `closed`), `created`,
`updated`, `created_by`, `updated_by`, and `spec` once a spec id has been set.
`created_by` and `updated_by` are copies of the git author at write time.
History is the authority when a copy disagrees. The file format has no
`IDEA-NNN` and no visibility field. Public means everyone who can fetch that
repo. On a private repository the branch is as private as the code.

nightwright fetches and pushes `nw-backlog` by name. Cloning the repo fetches
the branch with the default refspec. Pulling the default branch does not update
it. A push that is not a fast-forward is fetched and merged: different files
merge, the same file stops for a person. Any other rejection means this
repository cannot host the backlog. nightwright does not force-push and does
not delete the branch.

The forge has to allow a fast-forward of this one branch, including the push
that creates it, refuse force-push, refuse deletion, and not require a pull
request. A repository that cannot say that cannot host the backlog.

## Design

### Branch and checkout

The ref is `refs/heads/nw-backlog`. Commands take the git toplevel of `--repo`,
or of the current directory when `--repo` is omitted. The directory must be a
non-bare work tree. The remote defaults to `origin` and `--remote` overrides it.

Every mutating command (`add`, `edit`, `close`, `reopen`, `fetch` when it
fast-forwards, `push`, `resolve`) refuses, before it updates any ref, when
`git worktree list --porcelain` shows any worktree whose branch is
`refs/heads/nw-backlog`. The refusal exits 1 and names the worktree. That keeps
`update-ref` from moving a checked-out branch and leaving its work tree stale.

The product checkout is the work tree the command was started in. Before and
after every command, all of these are unchanged: `HEAD` (sha and whether it is
detached or which branch it names), the path and bytes of that work tree's
index, `git status --porcelain`, and the files in the work tree. The command
never runs `checkout`, `switch`, `merge`, `rebase`, `reset`, or `clean`. It
never pushes `HEAD` or the current branch name. The push refspec is always
`refs/heads/nw-backlog:refs/heads/nw-backlog`.

A missing `refs/heads/nw-backlog` is an empty backlog. `list` and `show` succeed
with no items. `add` creates the branch.

A tip that already exists is a backlog only when every root commit
(`git rev-list --max-parents=0`) and the tip tree itself contain only root-level
paths matching `README.md` or `REQ-[0-9A-HJKMNP-TV-Z]{8}.md`. Otherwise the
command exits 1 and says `refs/heads/nw-backlog is not an orphan backlog`. It
does not commit on top of product history.

### File format

The id is `REQ-` plus eight characters from the Crockford alphabet
`0123456789ABCDEFGHJKMNPQRSTVWXYZ` (no `I`, `L`, `O`, or `U`). The minter draws
`secrets.token_bytes(5)` (40 bits) and encodes five bits per character, high
bits first. It does not add a checksum. The filename is `<id>.md` at the branch
root. The id inside the file equals the filename stem.

If that filename, compared case-insensitively, is already on the tip, the
minter draws again, up to eight draws. The ninth collision exits 1 and writes
nothing.

`add` writes this shape, UTF-8, LF, no BOM. The file ends with one newline.
`title`, `created_by`, and `updated_by` are double-quoted YAML scalars with
`\` and `"` escaped. Keys stay in this order. `spec` is omitted until set.

```markdown
---
id: REQ-K7H3MQ2P
title: "Public backlog"
status: open
created: 2026-11-07T15:41:00Z
updated: 2026-11-07T15:41:00Z
created_by: "Ada Lovelace <ada@example.com>"
updated_by: "Ada Lovelace <ada@example.com>"
---

The summary.
```

`created` and `updated` are UTC instants `YYYY-MM-DDTHH:MM:SSZ`, equal to the
commit's author instant. `status` is `open` or `closed`. The summary is the
markdown body after the blank line that follows the closing `---`. An empty
summary omits that blank line and the body; the file ends at the closing `---`
line's newline. A non-empty summary is stored with its newlines normalized to
LF and exactly one trailing newline.

On `edit`, `created` and `created_by` are copied from the file being replaced.
`updated` and `updated_by` are set from this write. Title, summary, and `spec`
change only when the command says so. The path does not change.

The writer emits only the keys above. A later edit of a file that contains
`idea`, an `IDEA-NNN`, a visibility key, or any other key drops those keys.
`spec`, when set, matches `^SPEC-[0-9]{4}$`. nightwright does not check that a
spec file exists in this repo.

The first backlog commit, and any later write whose tip lacks `README.md`,
adds this exact file (trailing newline included):

```markdown
# nw-backlog

This branch is the public backlog for this repository. It is not a product checkout.
Do not build or run anything from it.

Each `REQ-*.md` file at the root is one backlog item. There is no index file.
```

If `README.md` is already on the tip, a later write leaves those bytes alone,
whether or not they match the text above.

Commit subjects, with no body:

- add: `backlog: add REQ-K7H3MQ2P`
- edit: `backlog: edit REQ-K7H3MQ2P`
- close: `backlog: close REQ-K7H3MQ2P`
- reopen: `backlog: reopen REQ-K7H3MQ2P`
- merge: `backlog: merge nw-backlog`

An add commit touches the new item and, when the tip lacked it, `README.md`.
An edit, close, or reopen commit touches that one item file. nightwright never
deletes a `REQ-*.md` path itself.

### Identity

A write reads `git config --get user.name` and `git config --get user.email`
(local, then global, then system, which is what `--get` already walks). A
missing, empty, or whitespace-only value is unset. The check runs before
`hash-object`, `commit-tree`, or `update-ref`. When either is unset the command
exits 1, names each missing key, and leaves the object database and
`refs/heads/nw-backlog` unchanged. A name or email containing CR or LF is
refused the same way.

The backlog commit sets both author and committer to that pair. The
`commit-tree` child receives `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
`GIT_COMMITTER_NAME`, and `GIT_COMMITTER_EMAIL` from those values, overriding
any ambient `GIT_AUTHOR_*` or `GIT_COMMITTER_*`. `created_by` on add, and
`updated_by` on every write, are `Name <email>` from that same pair. Author and
committer dates are that same instant.

Displayed identity comes from history, not from the frontmatter copy.

- The creator commit is the `--diff-filter=A` commit for the path, searched with
  `git log --full-history --root`. If two adds exist, the earlier committer
  timestamp wins, and an equal timestamp yields to the lexicographically smaller
  sha. `created_by` is that commit's `%an <%ae>`.
- The modifier candidates are `git log --full-history --root` for the path.
  Drop a commit whose blob for the path equals its first parent's blob. Drop a
  merge (two or more parents) whose blob equals the blob at any parent, which
  is a merge that took one side or only brought the file in. The newest
  remaining commit by committer timestamp, with the same sha tie-break, is the
  modifier. `updated_by` is that commit's `%an <%ae>`.
- A merge whose diff does not touch the path never appears in that log, so a
  merge that only brought in other files is not the modifier.

`list` and `show` report the history values. `show --json` also reports
`frontmatter_created_by`, `frontmatter_updated_by`, and `identity_disagrees`.
A read does not rewrite the file.

### Read and write

`show` and `list` read blobs with
`git show refs/heads/nw-backlog:<path>`. They do not read the work tree. A file
of the same name in the checkout is not the backlog.

`list` orders items by id. A path that is not `README.md` and not a `REQ-*.md`
name is not an item: human output prints `stray: <path>` on stderr, and
`--json` lists it under `stray`. An unreadable `REQ-*.md` (not valid UTF-8, or
no YAML frontmatter) is an item with status `?` and title `unreadable`.

`--json` for `list` is `nw.backlog.list.v1`:

```json
{
  "schema": "nw.backlog.list.v1",
  "ref": "refs/heads/nw-backlog",
  "items": [
    {
      "id": "REQ-K7H3MQ2P",
      "path": "REQ-K7H3MQ2P.md",
      "title": "Public backlog",
      "status": "open",
      "created": "2026-11-07T15:41:00Z",
      "updated": "2026-11-07T15:41:00Z",
      "created_by": "Ada Lovelace <ada@example.com>",
      "updated_by": "Ada Lovelace <ada@example.com>"
    }
  ],
  "stray": []
}
```

`spec` is present on an item only when the file has a valid spec id. `show
--json` is one item plus `summary`, `frontmatter_created_by`,
`frontmatter_updated_by`, and `identity_disagrees`.

The write builds a commit without the product index:

1. Check the git identity and the worktree-not-checked-out rule.
2. Read the current tip sha, or note that the ref is missing.
3. Reject a tip that is not a backlog, as defined above.
4. Put `GIT_INDEX_FILE` on a new file in a temporary directory. That variable
   is set only on the child git processes.
5. `git read-tree` of the tip tree, or an empty index when the branch is new.
6. `git hash-object -w` the new blob and `git update-index --add --cacheinfo`
   it. Add `README.md` the same way when the index lacks it.
7. `git write-tree`, then `git commit-tree` of that tree. The first commit has
   no `-p`. Every later item commit has `-p <old tip>`.
8. `git update-ref refs/heads/nw-backlog <new> <old>` where `<old>` is forty
   zeros when the ref was missing. A failed compare-and-swap retries the whole
   write against the new tip, up to three attempts, then exits 1. No attempt
   uses `update-ref -d`.

### Fetch, push, and the person's choice

`nw backlog fetch` runs
`git fetch <remote> refs/heads/nw-backlog:refs/remotes/<remote>/nw-backlog`
with no leading `+`. A remote that lacks the branch exits 1, says
`nw-backlog is not on <remote>`, and does not create a local ref. When the
remote tip fast-forwards the local branch (or the local ref is missing),
nightwright points `refs/heads/nw-backlog` at it with `update-ref`. When the
two tips have diverged, fetch leaves the local branch where it is, updates only
the remote-tracking ref, and exits 0. It does not merge.

`git pull` of the default branch is not how the backlog updates. nightwright
never runs it for this ref.

`nw backlog push` pushes
`refs/heads/nw-backlog:refs/heads/nw-backlog` with no `--force`, no
`--force-with-lease`, no `--delete`, and no `+` refspec. A remote that does
not yet have the branch receives the commit that creates it. That is the
ordinary first push, not a force-push.

When the rejection text says the update was not a fast-forward (`non-fast-forward`
or `failed to push some refs` together with the tip being behind), nightwright
fetches by name as above and merges:

1. Both tips must pass the backlog-tree check. A failure exits 1 and does not
   update the local ref.
2. `git merge-tree --write-tree` of the local tip and the fetched tip. Pass
   `--allow-unrelated-histories` only when `git merge-base` finds no base, and
   only after both tips passed the backlog-tree check. Two orphan backlog roots
   are allowed to merge. A product commit is not.
3. Exit status 0 is a clean merge. `commit-tree` records the printed tree with
   `-p <local> -p <fetched>`, subject `backlog: merge nw-backlog`, and the
   identity of the person running the command. `update-ref` compare-and-swaps
   the local tip to that merge, then push runs again.
4. Exit status 1 is a same-file conflict (content, add/add, or modify/delete).
   nightwright does not commit the tree `merge-tree` wrote: that tree can
   contain conflict markers. It does not move the local ref. Stderr lists
   `conflict: <path>` for each conflicted path. The exit code is 3.
5. Any other `merge-tree` status exits 1 and does not move the local ref.

After five rejected pushes, nightwright exits 4, says the remote kept moving,
and does not merge again and does not push a sixth time. The local tip is
whatever the last successful local commit or clean merge left.

Any push rejection that is not a non-fast-forward — a hook that requires a pull
request, a permission error, a refused branch creation — exits 2, prints the
remote's message, and does not fetch, merge, or force-push. That repository
cannot host the backlog until a fast-forward of `nw-backlog` is allowed, a
force-push is refused, deletion is refused, and no pull request is required.

`nw backlog resolve` is the person's choice after exit 3. It merges the current
local tip with `refs/remotes/<remote>/nw-backlog`. `--take local` or
`--take remote` is the default side. `--file REQ-K7H3MQ2P=local` (repeatable)
overrides one path. Every conflicted path needs a side, from `--take` or from
`--file`. A `--file` for a path that is not conflicted, or a missing side,
exits 1 and writes no commit. The new tree takes the clean merge blob for
every unconflicted path and the chosen side's blob (or that side's absence,
for a deletion) for every conflicted path. It is a merge commit with the same
two parents and the same push rules. Resolve does not invent a third text.

No command creates `MERGE_HEAD` or `MERGE_MSG` in the product git directory.

### Commands

The Click group lives in `src/nightwright/backlog.py` and is registered on the
root `cli` from `src/nightwright/cli.py`. Library functions in that module do
the work; the commands parse arguments and set the exit code.

| Command | Effect |
|---|---|
| `nw backlog add --title TEXT --summary TEXT [--spec SPEC-NNNN] [--push]` | Mint an id, commit one open item. `--push` then runs push. |
| `nw backlog edit ID [--title TEXT] [--summary TEXT] [--spec SPEC-NNNN] [--clear-spec] [--push]` | Replace the named fields. At least one field flag is required. The path is unchanged. |
| `nw backlog close ID [--push]` | Set `status: closed`. Already closed exits 1 with no commit. |
| `nw backlog reopen ID [--push]` | Set `status: open`. Already open exits 1 with no commit. |
| `nw backlog list [--json]` | Read the local ref. A missing ref prints no items and exits 0. |
| `nw backlog show ID [--json]` | Read one blob. Unknown id exits 1. |
| `nw backlog fetch` | Fetch `nw-backlog` by name. |
| `nw backlog push` | Fast-forward push, or merge, as above. |
| `nw backlog resolve [--take local\|remote] [--file ID=local\|remote ...] [--push]` | Record the choice. `--push` defaults on for resolve, because the choice exists to publish it. `--no-push` leaves the merge commit local. |

`ID` is `REQ-` plus eight alphabet characters, with an optional `.md` suffix.
Any other spelling exits 1. An empty or whitespace-only title or a summary
flag that was not passed when required exits 1 with no commit. `--summary` may
contain newlines. `--repo` and `--remote` exist on every command; `--remote`
matters for fetch, push, and resolve.

Exit codes: `0` success, `1` usage, validation, identity, checkout, or a tip
that is not a backlog, `2` the remote rejected the push for a reason other than
non-fast-forward, `3` same-file conflict, `4` the remote was still not a
fast-forward after five merges.

## Alternatives considered

- **Files under `docs/backlog` on the default branch.** On a protected branch
  every new item and every summary edit is a pull request. A backlog cannot
  bear that.
- **`refs/nw/backlog`.** The commit is the same, and the file format does not
  care which ref points at it, but GitHub accepts pushes to branches and tags.
  A ref outside `refs/heads` does not show up as a branch. The first ref is
  `refs/heads/nw-backlog`.
- **A git-bug operation DAG, one ref per item, ordered by Lamport clocks.**
  Concurrent comments both survive. This backlog's writes are a new file or a
  replacement of one file. New files merge. A same-file edit is shown to a
  person. The operation log, identities-as-a-protocol, and clocks are out of
  scope.
- **GitHub issues as the store.** A later opt-in mirror, not this store. The
  files stay readable with git alone.
- **An index file listing every id.** Every create would conflict on it.
- **Force-push or rebase so the second writer wins.** That drops a person's
  write. nightwright stops and the person chooses a side.
- **`git merge` in the product checkout.** That moves the current branch and
  touches the index. `merge-tree` plus `commit-tree` keeps the checkout still.

## Acceptance criteria

- [ ] `nw backlog add` creates `refs/heads/nw-backlog` as a parentless commit
      whose tree is `README.md` plus one `REQ-????????.md`. The eight
      characters are from `0123456789ABCDEFGHJKMNPQRSTVWXYZ`. The id inside the
      file equals the filename stem. The README bytes are the fixed text in
      Design. No other path is in the tree.
- [ ] A second `add` leaves the filename of the first item unchanged and adds
      no index file. `git ls-tree -r --name-only` of the tip is `README.md` and
      the `REQ-*.md` files only. If `README.md` on the tip already differs from
      the fixed text, that second `add` leaves those bytes unchanged.
- [ ] An add commit's subject is `backlog: add <id>`. An edit, close, reopen,
      and merge use the subjects in Design. An edit, close, or reopen commit
      names only that item path. An add commit names the new item and, only
      when the tip lacked it, `README.md`.
- [ ] `edit` with no field flag, a whitespace-only `--title`, an id that is not
      `REQ-` plus eight alphabet characters, and `fetch` when the remote has no
      `nw-backlog`, each exit 1 and add no commit. `list` on a missing ref
      exits 0 with no items. `list --json` is `nw.backlog.list.v1` sorted by
      id, omits `README.md`, and reports any other path under `stray`.
- [ ] `edit --title` changes the title in that blob and the path stays the same.
      `git log --diff-filter=R --name-only` for the branch is empty.
- [ ] Frontmatter of a new item has `id`, `title`, `status: open`, `created`,
      `updated`, `created_by`, and `updated_by` in that order. `created` and
      `updated` equal the commit's author instant in `YYYY-MM-DDTHH:MM:SSZ`.
      `spec` is absent until `--spec SPEC-NNNN`, and a value that is not
      `SPEC-` plus four digits is refused with no commit.
- [ ] `close` sets `status: closed` and refreshes `updated` and `updated_by`,
      leaving `created`, `created_by`, title, summary, and `spec` as they were.
      A second `close` exits 1 and adds no commit. `reopen` returns `status` to
      `open` and a second `reopen` exits 1 with no commit.
- [ ] With `user.name` or `user.email` unset, or set to whitespace, `add` exits
      1, names each missing key, and does not change the object count or the
      `nw-backlog` ref. When both are set, the new commit's author and
      committer are that name and email, including when `GIT_AUTHOR_NAME` in
      the environment is a different string.
- [ ] After a merge that brings in a second file and does not change the first
      file's blob, `show` of the first file reports `updated_by` as the author
      of the commit that last changed that file, not the author of the merge.
      When the frontmatter `created_by` or `updated_by` differs from those
      history values, `show --json` sets `identity_disagrees` and the reported
      `created_by` and `updated_by` are the history values.
- [ ] `show` prints the blob from `git show refs/heads/nw-backlog:<path>` when
      the work tree has a different file at the same relative path.
- [ ] After `add`, `edit`, `push`, a rejected push, `fetch`, and `resolve`, the
      starting work tree's `HEAD` sha, branch name or detached state, index
      bytes, and `git status --porcelain` are unchanged, including when the
      work tree was dirty. No command creates `MERGE_HEAD`. If any worktree has
      `nw-backlog` checked out, the command exits 1 and the ref is unchanged.
- [ ] A tip whose root or tree contains a path other than `README.md` or a
      `REQ-*.md` file is refused. `add` exits 1 and the tip sha is unchanged.
- [ ] An `edit` of a file that contains an `idea` key or an `IDEA-NNN` writes a
      blob whose frontmatter has none of those keys and no key outside the
      format above.
- [ ] `push` from a clone whose current branch is `main` updates only
      `refs/heads/nw-backlog` on a bare remote. The remote branch's root has no
      parent, and the clone's `main` commit is not an ancestor of it. The push
      arguments contain `refs/heads/nw-backlog:refs/heads/nw-backlog` and do not
      contain `--force`, `--force-with-lease`, `--delete`, or a refspec that
      starts with `+`.
- [ ] Two clones that add different ids: the second `push` fast-forwards the
      remote to a merge commit with both files. Each file's displayed
      `created_by` stays the author who added it.
- [ ] Two clones that change the same id: the second `push` exits 3, prints
      `conflict:` and that path, leaves the remote at the first clone's tip,
      and leaves the second clone's local tip at its own edit. `resolve --take
      remote` then publishes a merge whose blob for that path equals the
      remote side. `resolve --take local` publishes the local side. A resolve
      that leaves a conflicted path without a side exits 1 and adds no commit.
- [ ] A pre-receive hook that rejects with a pull-request message makes `push`
      exit 2. The remote sha is unchanged and the local tip gains no merge
      commit.
- [ ] After the remote branch has moved forward, `git pull` of the default
      branch in a clone does not move `refs/remotes/origin/nw-backlog`.
      `nw backlog fetch` does, and it does so by fetching
      `refs/heads/nw-backlog` by name. When the histories have diverged, `fetch`
      exits 0 and does not move the local branch.
- [ ] When `update-ref` compare-and-swap fails on all three attempts, `add`
      exits 1 and `refs/heads/nw-backlog` remains the tip that won the race.
- [ ] Five consecutive non-fast-forward rejections after clean merges exit 4
      without a sixth push.

## Test plan

- **Automated tests:** `tests/test_backlog.py`, using temporary repos and a bare
  remote the way `tests/conftest.py` sets `user.name` and `user.email`. CliRunner
  covers exit codes; the library functions cover trees and history. The tests
  and the criteria they lock:
  - `test_add_writes_orphan_readme_and_req_file` — parentless root, README
    bytes, filename alphabet, frontmatter order, no index path.
  - `test_second_add_does_not_add_an_index` and
    `test_existing_readme_bytes_are_left_alone`.
  - `test_commit_subjects_and_paths` — add, edit, close, reopen, and merge
    subjects, and the paths each commit names.
  - `test_title_edit_keeps_the_path` — `--diff-filter=R` is empty.
  - `test_rejected_invocation_writes_nothing` — no field flag, blank title,
    bad id, fetch of a missing remote branch.
  - `test_list_json_orders_items_and_reports_stray`.
  - `test_spec_field_and_rejected_spec_id`.
  - `test_close_and_reopen_are_single_file_commits`.
  - `test_refuses_unset_identity_before_any_object` — object count unchanged;
    `test_author_and_committer_follow_config_not_environment`.
  - `test_merge_of_another_file_is_not_the_modifier` and
    `test_show_prefers_history_when_frontmatter_disagrees` — a hand-built
    `commit-tree` replaces the blob's `created_by` while the add commit keeps
    the real author.
  - `test_show_reads_git_show_not_the_worktree`.
  - `test_dirty_checkout_and_index_stay_put` — staged change, unstaged change,
    and a detached `HEAD`, across add, push, exit 3, fetch, and resolve.
  - `test_refuses_when_nw_backlog_is_checked_out`.
  - `test_refuses_a_branch_that_contains_product_history`.
  - `test_edit_drops_idea_and_unknown_keys`.
  - `test_push_argv_is_a_fast_forward_of_nw_backlog` — argv assertions and the
    bare remote containing only `nw-backlog`, with `main` not an ancestor.
  - `test_push_merges_different_files` — two clones, two ids, both creators
    preserved.
  - `test_push_stops_on_the_same_file` and
    `test_resolve_takes_one_side` — exit 3, remote sha unchanged, then
    `--take remote` and `--take local` in separate repos; missing side exits 1.
  - `test_pull_request_hook_exits_2_without_merging` — bare-repo pre-receive
    hook.
  - `test_pull_of_default_does_not_update_nw_backlog` and
    `test_fetch_by_name_fast_forwards_or_leaves_a_diverged_local`.
  - `test_stops_after_five_non_fast_forwards` — a stub of the push function that
    returns non-fast-forward five times. The assertion is exit 4 and five push
    calls, with no sixth.
  - `test_compare_and_swap_gives_up_after_three_attempts` — `update-ref` fails
    three times, exit 1, and the ref stays at the tip that won.
  - `test_unrelated_orphan_roots_merge` — two independent backlog roots with
    different ids merge; a root that contains `src/main.c` does not.
  Implementers run `uv run pytest -q tests/test_backlog.py` and
  `python3 tools/affected_tests.py --run uv run pytest -q`. They do not run the
  full suite.
- **Manual verification:** on a forge repository whose `nw-backlog` rule allows
  a fast-forward (including the push that creates the branch), refuses
  force-push, refuses deletion, and does not require a pull request:
  `nw backlog add --title "Try" --summary "One item" --push` from a product
  checkout. `git rev-parse HEAD` is unchanged, `git status --porcelain` shows
  no backlog path, and `git log -1 --format='%an <%ae>' nw-backlog` is
  `user.name` and `user.email`. A second clone's `git branch -r` lists
  `origin/nw-backlog` after clone. `git pull` on the default branch, after the
  first clone adds another item, leaves `origin/nw-backlog` where it was until
  `nw backlog fetch`. `git push --force origin refs/heads/nw-backlog` is
  rejected by the forge. A repository whose rule requires a pull request makes
  `nw backlog push` exit 2.
- **Regression guard:** `test_dirty_checkout_and_index_stay_put` and
  `test_push_argv_is_a_fast_forward_of_nw_backlog`. The new module is the only
  test file this work adds. `src/nightwright/agentic/gitops.py` is unchanged, so
  campaign commits keep their existing temporary-index behavior. Affected tests
  cover the `cli.py` registration line.

## Clock Log

No session is recorded in this file. Implementation clocks IDEA-1019 with
`nw idea clock-in` and `nw idea clock-out`.

## Rollout / migration

There is no `docs/backlog` tree to import. The first `nw backlog add` creates
the orphan branch locally. The first `nw backlog push` creates it on the remote
when the forge allows that fast-forward.

Before that push, the repository owner sets the forge rule for
`refs/heads/nw-backlog`: allow a fast-forward, including branch creation; refuse
force-push; refuse deletion; do not require a pull request. nightwright does
not set this rule. A push that the forge rejects for any other reason exits 2
and leaves the local branch as the only copy.

A clone made after the branch exists receives `nw-backlog` through the default
fetch refspec. Existing clones see new items by `nw backlog fetch`, not by
pulling the default branch.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass; manual steps performed where a
      forge repository is available, and the spec notes it when one is not.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).
- [ ] `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
