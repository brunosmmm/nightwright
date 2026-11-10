---
id: SPEC-0473
title: Public backlog on an orphan nw-backlog branch
status: done
owner: user
created: 2026-11-08
updated: 2026-11-10
kind: feature
tags: [public-backlog]
source_idea: IDEA-1019
---

## Context

A public backlog cannot live on the project's default branch. On a protected
branch every new item and every title or summary edit is a pull request, and a
backlog cannot bear that. The item list also cannot be an index file in that
tree: every create would conflict on the index.

nightwright already reads blobs with `git show <ref>:<path>` and already builds
commits through a temporary index (`src/nightwright/agentic/gitops.py`). It has
no backlog ref and no per-item file format.

The store has to be a real branch. A ref outside `refs/heads`, such as
`refs/nw/backlog`, is the same commit and does not show up as a branch. GitHub
accepts pushes to branches and tags, so the branch is `refs/heads/nw-backlog`.
The file format does not record which ref points at the commit.

This spec is the first slice: recognize that branch, serialize `README.md` and
one file per item, mint ids, and commit `add`, `edit`, `close`, and `reopen`
without moving the product checkout. Reads and provenance, fetch and
fast-forward publication, and concurrent merge and resolution are later specs.
Their binding rules are listed under Non-goals so this slice does not invent
a conflicting contract for them.

This file is SPEC-0473 and its status is proposed. Review of this file runs
before acceptance. On main, SPEC-0449 is the tail-shell spec. This backlog's
governing id is SPEC-0473.

## Goals / Non-goals

**Goals**

- One backlog item is one regular file at the root of an orphan
  `refs/heads/nw-backlog`, named only by its id, so a title change does not
  rename it.
- The branch is as visible as the repository. Anyone who can fetch the
  repository can fetch `refs/heads/nw-backlog`. On a private repository the
  branch is as private as the code. These commands take no visibility flag
  and write no visibility key.
- `add`, `edit`, `close`, and `reopen` read blobs with `git show` of a captured
  sha and write them with a temporary index and `commit-tree`. The product
  checkout, its index, and its current branch stay as they were.
- The commit's author and committer are the git identity of the person running
  the command. A write is refused when `user.name` or `user.email` is missing,
  whitespace-only, or contains a character this spec forbids.
- A commit this spec writes matches the file format below. `add` requires a
  summary flag, an empty summary is a real empty body, `--spec` and
  `--clear-spec` cannot be combined, and an edit whose requested values equal
  the stored values writes nothing.
- A tip that is not an orphan backlog is refused before any object write. The
  selected item is validated. An invalid selected item is not rewritten. No
  command in this spec deletes a `REQ-*.md` path.

**Non-goals**

- **Backlog reads and provenance** (later spec). That spec adds `list` and
  `show`, human output, `nw.backlog.list.v1`, and `nw.backlog.show.v1`. `list`
  tolerates a stray path and reports it, including beside valid items. `show`
  reads one named valid item; a stray path or an invalid item makes `show` exit
  1 and write nothing. Displayed creator and modifier come from history: the
  creator is the commit that added the file, the modifier is the latest commit
  that changed that file's blob, and a merge that only brought in other files
  is not the modifier. When the frontmatter copy disagrees, the displayed
  identity is the history value and the JSON marks the disagreement. A read
  does not rewrite the file. Every invalid form in this spec's validation table
  has a deterministic list entry that carries this spec's reason token. That
  spec's tests include `test_list_reports_stray_paths`,
  `test_show_reads_one_valid_item_when_the_tip_has_a_stray_path`, and
  `test_show_and_list_render_each_invalid_form`. This spec does not add those
  commands.
- **Backlog fetch and fast-forward publication** (later spec). That spec fetches
  `nw-backlog` by name, fast-forwards the local branch when the remote is ahead,
  and pushes `refs/heads/nw-backlog:refs/heads/nw-backlog` with no force and no
  delete. A fetch that would fast-forward, and a push, reject a tip that
  contains a stray path. The forge has to allow a fast-forward of this one
  branch, including the push that creates it, refuse force-push, refuse
  deletion, and not require a pull request. nightwright does not call the forge
  API to install that rule, does not force-push, and does not delete the branch.
  Commands in this spec take no `--push` and no `--remote`.
- **Concurrent backlog merge and resolution** (later spec). Different item
  files merge. Before accepting a clean merge, that spec computes the paths
  changed by each tip relative to their merge base. If any `REQ-*.md` path
  changed on both sides, it exits 3 and requires a person's choice, including
  when one side changed the title and the other changed the summary and a
  content merge would have been clean. Its test is
  `test_same_item_title_and_summary_edits_stop_for_a_person`. Resolve refuses
  any choice that would remove a `REQ-*.md` path, exits 1, and writes no
  commit. A modify/delete conflict is resolved only by keeping one side's
  existing item blob. Its test is
  `test_resolve_refuses_a_choice_that_deletes_an_item`. The person takes one
  existing side, then may `edit` afterwards. This spec does not merge.
- An operation log, per-item refs, or Lamport clocks. git-bug is that design.
  This slice's write is a new file or a replacement of one file's title,
  summary, status, or spec id.
- GitHub issues, or any forge issue mirror.
- Storing an `IDEA-NNN`, a visibility flag, comments, labels, assignees, or a
  sort order. The writer never emits those keys.
- A command that deletes an item, renames its file, or composes a third text.
- A TUI or dashboard editor.

## Decision

The backlog of a project is the orphan branch `refs/heads/nw-backlog` in that
project's git repository. Each item is one markdown file at the branch root,
`REQ-` plus eight Crockford characters, minted on the machine that creates the
file. A fixed `README.md` is the only other file these commands add. There is
no index file. The file format has no `IDEA-NNN` and no visibility field.
Public means everyone who can fetch that repository. On a private repository
the branch is as private as the code. `add`, `edit`, `close`, and `reopen`
take no `--visibility`, `--public`, or `--private`.

`nw backlog add`, `edit`, `close`, and `reopen` commit onto that branch through
a temporary index. They leave the checkout where it was. The first `add`
creates the branch as a parentless commit. Later commands in this spec add one
parent, the previous tip. Publication, history-derived display, and same-file
choice are the later specs named above.

## Design

### Repository and refs

Commands take the git toplevel of `--repo`, or of the current directory when
`--repo` is omitted. The directory must be a non-bare work tree
(`git rev-parse --is-inside-work-tree` is `true` and
`--is-bare-repository` is `false`). Anything else exits 1 with
`not a git work tree`.

The backlog ref is `refs/heads/nw-backlog`. A missing ref is an empty backlog.
`add` creates it. `edit`, `close`, and `reopen` of an id that is not on the
tip exit 1 with `<id> is not on nw-backlog`.

Every command in this spec refuses, before `hash-object`, when
`git worktree list --porcelain` shows a worktree whose `branch` line is
`refs/heads/nw-backlog`. Stderr is one line per such worktree,
`nw-backlog is checked out at <path>`, using the absolute `worktree` path from
that porcelain record. Exit 1. The ref is unchanged.

The product checkout is the work tree the command resolved. After every
command, all of these are unchanged: `HEAD` (sha, and whether it is unborn,
detached, or which branch it names), the bytes of that work tree's index file
when it exists and its absence when it does not, `git status --porcelain`, and
the files in the work tree. The command does not create `MERGE_HEAD`,
`MERGE_MSG`, or `CHERRY_PICK_HEAD`. It does not check the backlog files out
into the work tree.

The only git subcommands these commands run are `rev-parse`, `rev-list`,
`ls-tree`, `show`, `config`, `worktree`, `hash-object`, `read-tree`,
`update-index`, `write-tree`, `commit-tree`, and `update-ref`. `worktree` is
only `worktree list --porcelain`. `config` is only `config --get user.name`
and `config --get user.email`. `update-ref` is only
`update-ref refs/heads/nw-backlog <new> <expected>` and never includes `-d`.
`<expected>` is the full sha read at the start of that attempt, or forty `0`
digits when the ref was missing. `GIT_INDEX_FILE` is set only on those child
processes, pointing at a file in a temporary directory that the command
deletes before it returns.

Reads in an attempt use the sha captured at the start of that attempt:
`git ls-tree -r <sha>`, `git show <sha>:<path>`, and
`git rev-list --max-parents=0 <sha>`. They do not read `HEAD` or the work tree.
A file of the same name in the checkout is not the backlog.

### Stray paths

A tip is an orphan backlog when every root commit reachable from it, and the
tip tree itself, contain only root-level regular files (`mode` `100644`) whose
path is `README.md` or `REQ-[0-9A-HJKMNP-TV-Z]{8}.md`. An empty tree satisfies
that. Any other path is stray. Any other mode is a mode problem. A missing ref
is an empty backlog, not a stray tip.

`add`, `edit`, `close`, and `reopen` are the mutating commands of this spec.
Each rejects a tip with any problem: exit 1, no `hash-object`, ref unchanged.
Stderr is the header `refs/heads/nw-backlog is not an orphan backlog`, then
one line per finding:

- tip: `stray: <path>` or `mode: <path>`
- a root commit: `root <fullsha> stray: <path>` or `root <fullsha> mode: <path>`

Tip lines come first, then roots in ascending sha order. Within one tree,
lines sort by path and then by kind (`mode` before `stray`). A path that is
both stray and the wrong mode produces both lines.

`list` reports stray paths and still succeeds. `show` may read one named valid
item and exits 1 for a stray path. Fetch that fast-forwards, push, merge, and
resolve reject a stray tip. Those five commands belong to the later specs in
Non-goals. This spec's tests cover the four mutating refusals. The reporting
tests are named on that later reads spec.

### File bytes

The id is `REQ-` plus eight characters from
`0123456789ABCDEFGHJKMNPQRSTVWXYZ` (no `I`, `L`, `O`, or `U`). The filename is
`<id>.md` at the branch root. The id inside the file equals the filename stem.

`add` writes UTF-8, LF, no BOM. The file ends with one newline. `title`,
`created_by`, and `updated_by` are double-quoted YAML scalars. Inside the
quotes, `\` is written `\\` and `"` is written `\"`. No other escapes are
used. Keys stay in this order. `spec` is omitted until set.

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

`created` and `updated` are UTC instants `YYYY-MM-DDTHH:MM:SSZ`. They equal
the commit's author instant, which equals its committer instant. `status` is
`open` or `closed`. `spec`, when present, is the last key and matches
`^SPEC-[0-9]{4}$`. nightwright does not check that a spec file exists.

The summary is the markdown body. A non-empty summary is written after a
blank line that follows the closing `---`, with LF newlines and exactly one
trailing newline. An empty summary omits that blank line and the body; the
file ends at the newline of the closing `---` line.

On `edit`, `close`, and `reopen`, `created` and `created_by` are the logical
strings parsed from the blob being replaced. `updated` and `updated_by` are
set from this write. Title, summary, and `spec` change when the command says
so. The path does not change. A commit that changes the item drops every key
other than the keys above, including `idea`, an `IDEA-NNN`, and a visibility
key. A no-op edit writes nothing, so those extra keys stay until a later
commit actually changes a field.

The first backlog commit, and any later write whose tip lacks `README.md`,
adds this exact file. The lines are `# nw-backlog`, an empty line,
`This branch is the public backlog for this repository. It is not a product checkout.`,
`Do not build or run anything from it.`, an empty line, and
``Each `REQ-*.md` file at the root is one backlog item. There is no index file.``
Those lines are joined with `\n` and the blob ends with `\n`. If `README.md`
is already on the tip, a later write leaves those bytes alone.

Commit messages are a single line and a newline, with no body:

- add: `backlog: add REQ-K7H3MQ2P`
- edit: `backlog: edit REQ-K7H3MQ2P`
- close: `backlog: close REQ-K7H3MQ2P`
- reopen: `backlog: reopen REQ-K7H3MQ2P`

An add commit names the new item and, when the tip lacked it, `README.md`.
An edit, close, or reopen commit names that item and, when the tip lacked it,
`README.md`. It names no other path. None of these commits delete a path.

### Validation

`parse_item(filename, blob)` splits a blob that passed the byte checks with
the DOTALL regex `\A---\n(.*?)\n---\n(.*)\Z`. Group 1 plus a newline is the
YAML document. Group 2 is the raw body. The first `\n---\n` closes the
frontmatter. The loader is PyYAML's safe loader (YAML 1.1, so `yes`, `no`,
`on`, and `off` are booleans). Its mapping constructor handles one key at a
time. If the constructed key is not a `str`, it raises an error that
`parse_item` already maps to `invalid yaml` — a `yaml.YAMLError`, or a
dedicated exception handled in that same `except` — before the
`key in mapping` duplicate test. A repeated string key still raises the
duplicate-key error, and that token stays `duplicate key <key>`. Every
other loader failure is `invalid yaml`.

YAML 1.1 collapses `yes` and `true` (and `on`) to boolean `True`. A
document whose keys are `yes` and `true` therefore fails as a non-string
key. `parse_item` returns `invalid yaml`, not `duplicate key True`. A
repeated string key `id` still returns `duplicate key id`. A single
non-string key such as `yes: 1` is also `invalid yaml`. `edit`, `close`,
and `reopen` of the boolean-key blob exit 1 with
`invalid backlog item <id>: invalid yaml`, do not call `hash-object`, and
leave the tip sha unchanged.

The logical summary of group 2: if it is empty or only `\n` characters, the
summary is `""`. Otherwise drop one leading `\n` when present, and if what
remains is empty or only `\n` characters the summary is `""`. Otherwise the
summary is that remainder with trailing `\n` characters removed and exactly
one `\n` appended.

Checks run in this order. The first match is the reason token. A fixture for
a row passes every earlier check.

| # | Form | Reason token |
|---|---|---|
| 1 | Prefix bytes `EF BB BF` | `bom` |
| 2 | Not valid UTF-8 | `not utf-8` |
| 3 | Contains a CR byte | `cr` |
| 4 | Does not match the frontmatter regex | `no frontmatter` |
| 5 | YAML error, or a non-string key, including a repeated non-string key | `invalid yaml` |
| 6 | Duplicate string key | `duplicate key <key>` |
| 7 | Document is not a mapping | `not a mapping` |
| 8 | First missing key among `id`, `title`, `status`, `created`, `updated`, `created_by`, `updated_by` | `missing <key>` |
| 9 | `id` is not a string matching `^REQ-[0-9A-HJKMNP-TV-Z]{8}$` | `bad id` |
| 10 | `id` does not equal the filename stem | `id mismatch` |
| 11 | `title` is not a string, is empty after `strip`, or contains a newline | `title` |
| 12 | `status` is not the string `open` or `closed` | `status` |
| 13 | `created` is not a string matching `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$` that `strptime` accepts with `%Y-%m-%dT%H:%M:%SZ` | `created` |
| 14 | `updated` fails the same instant check | `updated` |
| 15 | `created_by` is not a string whose `strip` is non-empty | `created_by` |
| 16 | `updated_by` fails the same string check | `updated_by` |
| 17 | `spec` is present and is not a string matching `^SPEC-[0-9]{4}$` | `spec` |

Unknown keys, any key order, an unquoted title, a body that uses or omits the
separator blank line, and extra trailing newlines in the body are valid.
`updated` may be an earlier instant than `created`. A `summary` key is an
unknown key; the body is the summary.

`edit`, `close`, and `reopen` run `parse_item` on the selected blob. Any
reason exits 1 with `invalid backlog item <id>: <token>`, does not call
`hash-object`, and leaves that blob's sha unchanged. They validate the
selected blob only. A malformed sibling with a legal filename does not block
`add` or a mutation of a different valid item, and its blob sha stays.
`add` writes a blob that passes every row.

### Identity

A write reads `git config --get user.name` and `git config --get user.email`,
which walk local, then global, then system. For each key, the first match
below is the error:

1. `git config --get` fails: `<key> is unset`.
2. The raw value contains CR or LF: `<key> contains a line break`.
3. `strip` of the value is empty: `<key> is unset`.
4. The raw value is not strict UTF-8: `<key> is not valid utf-8`.
5. The raw value contains a character in PyYAML's `Reader.NON_PRINTABLE`,
   NEL U+0085, U+2028, or U+2029. The message is
   `<key> contains control character` followed by the `_safe_path` text of
   that character.
6. For the name, `<` is checked on the raw value before `>`, and both are
   checked before the crud strip below. The message is
   `user.name contains '<'` or `user.name contains '>'`.
   `Ada <Lovelace` is `user.name contains '<'`.
7. For the name, strip leading and trailing characters in git's ident crud
   set. A character is in that set when its code point is `<= 32` or it is
   one of `.`, `,`, `:`, `;`, `<`, `>`, `"`, `'`, and `\`. The check uses
   that set, not a smaller set of space, tab, CR, LF, and `<>()\"`. `(` and
   `)` are not crud.
   - If the remainder is empty, the error is
     `user.name consists only of disallowed characters`. This covers `.` and
     `'`. It also covers `"` and `\"`.
   - If the remainder differs from the raw name and any removed character is
     not `"`, the error is
     `user.name contains a leading or trailing disallowed character`.
     `Ada Jr.` is this case: the command exits 1 before `hash-object`, so
     `%an` and `%cn` stay equal to the raw `user.name` and the name is not
     stored as `Ada Jr`.
   - If the remainder differs and every removed character is `"`, the name
     is accepted. `"Ada"` still commits, and `created_by` keeps the quotes.
8. For the email, whitespace is checked first and the message is
   `user.email contains whitespace`. Otherwise the first of `<`, `>`, a
   double quote, and a backslash produces `user.email contains '<char>'`.

`()` is a legal `user.name`. A commit that uses it has `%an` and `%cn`
equal to `()`.

Both keys are checked. Name errors are printed before email errors. Exit 1.
The check runs after the work tree is known to be non-bare and before the
checked-out refusal, and it does not call `hash-object`. The ref stays
unchanged.

The stored identity string is the raw config name, a space, `<`, the raw
config email, and `>`. The backlog commit sets both author and committer to
that pair. The `commit-tree` child receives `GIT_AUTHOR_NAME`,
`GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL`,
`GIT_AUTHOR_DATE`, and `GIT_COMMITTER_DATE` from those values and from the
write instant, overriding any ambient `GIT_AUTHOR_*` or `GIT_COMMITTER_*`.
Dates are `<unix-seconds> +0000`.

`created_by` on add, and `updated_by` on every commit this spec writes, are
that `Name <email>` string. `created_by` on edit, close, and reopen is the
parsed string from the previous blob.

### Minting

`mint_id(existing, draw=secrets.token_bytes)` draws five bytes and packs them
high bits first. `n = int.from_bytes(raw, "big")`. Character `i` for `i` in
`0..7` is `alphabet[(n >> ((7 - i) * 5)) & 31]` with
`alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"`. Pinned vectors:
`99e23a5c56` encodes `K7H3MQ2P`, and `abcd123456` encodes `NF6H4D2P`.

The candidate is compared case-insensitively with the filenames already on
the tip. A collision draws again, up to eight draws. If all eight collide,
`mint_id` raises and the command exits 1 with `could not mint an id`, without
a ninth draw and without `hash-object`. The id is minted once per `add`, after
the tip check, and reused on a compare-and-swap retry. A retry whose tip
already contains that filename exits 1 with `<id> is already on nw-backlog`
and does not draw again and does not call `update-ref` again.

### Commit

The write instant comes from `current_time()`, which returns
`datetime.now(timezone.utc)`. Library functions take a `now` callable that
defaults to `current_time`. A naive value is given `timezone.utc`. An aware
value is converted with `astimezone(timezone.utc)`. The unix second is then
`int(instant.timestamp())`, which floors a positive instant. Frontmatter and
both git dates use that same second.

Before the first attempt the command has checked arguments, refused a
directory that is not a non-bare work tree, checked identity, and refused a
checked-out `nw-backlog`. Those four checks are not repeated on a retry.

Each attempt:

1. Read the tip sha, or note that the ref is missing.
2. Reject a tip that is not an orphan backlog.
3. On the first attempt of `add`, mint the id from the filenames on that tip.
   Later attempts reuse it. If a later tip already contains that filename,
   exit 1 with `<id> is already on nw-backlog` and do not call `update-ref`.
4. For edit, close, and reopen, load `<sha>:<id>.md` and validate it. A missing
   path exits 1 with `<id> is not on nw-backlog`. Close of `closed` exits 1
   with `<id> is already closed`. Reopen of `open` exits 1 with
   `<id> is already open`. An edit whose requested logical values equal the
   stored ones exits 0 and stops, with no `hash-object`.
5. Put `GIT_INDEX_FILE` on a new file in a temporary directory.
6. `git read-tree <tip tree>`, or `git read-tree --empty` when the ref is
   missing.
7. `git hash-object -w` the new item blob and
   `git update-index --add --cacheinfo 100644,<blob>,<path>`. Add `README.md`
   the same way when the index lacks it. Leave an existing `README.md` blob
   as `read-tree` loaded it.
8. `git write-tree`, then `git commit-tree`. A commit created when the ref was
   missing has no `-p`. Every commit onto an existing tip has
   `-p <tip sha of this attempt>`.
9. `git update-ref` compare-and-swaps. A miss is a non-zero `update-ref` after
   which the ref sha differs from `<expected>`. That starts another attempt
   against the new tip, up to three attempts. The minted id, the requested
   field values, and the write instant stay fixed across attempts. A
   precondition failure on a later attempt (stray tip, missing item, invalid
   item, status already at the target, minted id now present, or a no-op)
   returns immediately and does not use the remaining attempts. A non-zero
   `update-ref` that leaves the ref at `<expected>` exits 1 with
   `update-ref failed` and does not retry. After the third miss the command
   exits 1 with `nw-backlog changed` and the ref is the sha the last attempt
   read as the winner. Dangling blobs from the failed attempts may remain.
   The ref does not point at those attempts' commits.

No attempt uses `update-ref -d`. No command creates `MERGE_HEAD` in the
product git directory.

### Binding notes (2026-11-07)

These notes are part of this proposed spec's contract. They record behavior
the tests lock.

- `commit-tree` is given the one-line message with no trailing newline.
  `git log -1 --format=%B` is tformat and appends one newline, so that log
  text is exactly `backlog: add <id>\n` and there is no blank body line.
- Item YAML uses PyYAML's safe loader with the timestamp implicit resolver
  removed on that loader subclass only. An unquoted instant stays a string,
  `strptime` checks it, and `2026-02-31T00:00:00Z` is the `created` token.
  `yes`, `no`, `on`, and `off` stay booleans. The process-wide safe loader
  is unchanged. The mapping constructor raises the `invalid yaml` failure
  when the constructed key is not a `str`, and it does that before the
  `key in mapping` duplicate test. A repeated string key is still
  `duplicate key <key>`. Keys `yes` and `true` together are `invalid yaml`.
- Git drops a leading or trailing `"` from a commit author name. `created_by`
  and `updated_by` keep the raw config string and escape it like a title. A
  quote that is not at either end is also stored on the commit ident.
  `"Ada"` is the quote exception and still commits. Any other leading or
  trailing ident-crud character is refused by the Identity section before
  `hash-object`.
- Loop 2: each ``_git`` child clears inherited ``GIT_DIR``, ``GIT_WORK_TREE``,
  ``GIT_OBJECT_DIRECTORY``, ``GIT_NAMESPACE``, ``GIT_COMMON_DIR``, and
  ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` in addition to ``GIT_INDEX_FILE``.
  Stray and worktree paths on stderr use ``backslashreplace`` encoding.
- Loop 3: ``worktree list`` uses ``--porcelain -z``. Identity, title, and
  summary arguments refuse non-strict UTF-8 before ``hash-object``. Title and
  identity also refuse every character in PyYAML's ``Reader.NON_PRINTABLE`` and
  NEL U+0085 (double-quoted scalars fold NEL). Summary bodies are not YAML-loaded;
  C1 controls and NEL remain legal there.
- Loop 0 (queue): title and identity checks also refuse U+2028 and U+2029.
  ``edit`` / ``close`` / ``reopen`` run the same render-safety check on parsed
  ``title`` and ``created_by`` before ``render_item``. ``rev-parse
  --show-toplevel`` is decoded with ``surrogateescape`` and only a trailing
  newline is stripped.
- Review loop 2: ``_safe_path`` escapes newline, CR, and ESC in stderr paths.
  ``parse_item`` maps ``RecursionError`` to ``invalid yaml``. A ``user.name``
  whose leading and trailing git ident crud strips to nothing is refused
  before ``hash-object`` with
  ``user.name consists only of disallowed characters``. The crud set, the
  ``<`` then ``>`` order, the leading-or-trailing refusal, the ``"Ada"``
  quote exception, and acceptance of ``()`` are the Identity section.

### Commands

The Click group `backlog` lives in `src/nightwright/backlog.py` and is
registered on the root `cli` from `src/nightwright/cli.py` by importing the
group and calling `add_command`. `backlog.py` does not import `cli`. Library
functions do the work and raise `BacklogError`. The CLI prints `str(error)`
on stderr and exits with `error.exit_code`. Every error defined here uses
exit code 1. Success and a no-op edit use exit code 0. Required flags are
checked in the callback so a missing flag is exit 1 with the message below,
rather than Click's missing-option exit.

Argument checks run before the repo is opened. The first failure is the only
message. `add` checks: `--title` omitted (`title is required`), `--summary`
omitted (`summary is required`), title empty after `strip` (`title is empty`),
title containing CR or LF (`title must be one line`), then a present `--spec`
that fails the pattern (`invalid spec id`). `edit` checks: `invalid id`, then
`spec and clear-spec are mutually exclusive`, then `nothing to change`, then
the same title checks when `--title` is present, then the spec pattern when
`--spec` is present. `close` and `reopen` check `invalid id` only.

| Command | Effect |
|---|---|
| `nw backlog add --title TEXT --summary TEXT [--spec SPEC-NNNN] [--repo PATH]` | Mint an id, commit one open item, print `<id>\n` on stdout and nothing else. Stderr is empty. |
| `nw backlog edit ID [--title TEXT] [--summary TEXT] [--spec SPEC-NNNN] [--clear-spec] [--repo PATH]` | Replace the named fields. At least one of those four flags is required. Stdout and stderr are empty on success. |
| `nw backlog close ID [--repo PATH]` | Set `status: closed`. |
| `nw backlog reopen ID [--repo PATH]` | Set `status: open`. |

`ID` matches `^(REQ-[0-9A-HJKMNP-TV-Z]{8})(?:\.md)?$`. Any other spelling exits
1 with `invalid id`. The path on the branch is always the stem plus `.md`.

`add` requires `--title` and `--summary`. Omitting `--title` exits 1 with
`title is required`. Omitting `--summary` exits 1 with `summary is required`.
`--summary ""` is present and stores an empty body. A summary that normalizes
to empty does the same. A summary that still has non-newline characters is
stored, including a whitespace-only summary.

Summary arguments are normalized before compare and before store: `\r\n`
becomes `\n`, then remaining `\r` become `\n`; if the result is empty or only
`\n` characters the summary is `""`; otherwise trailing `\n` characters are
removed and one `\n` is appended. This normalization does not drop a leading
newline. The one-leading-newline drop belongs to parsing a file's separator
blank line, not to the argument.

A title flag that is present and whose `strip` is empty exits 1 with
`title is empty`. A title that contains CR or LF exits 1 with
`title must be one line`. A title that still has characters is stored
unchanged, including leading or trailing spaces. `--spec` that does not match
`^SPEC-[0-9]{4}$` exits 1 with `invalid spec id`.

On `edit`, `--spec` and `--clear-spec` are mutually exclusive. Passing both
exits 1 with `spec and clear-spec are mutually exclusive` and does not commit,
including when the repo path does not exist and including when the item is
invalid. Omitting both leaves `spec` as parsed. `--clear-spec` when `spec` is
absent is a matching value, not an error. `--summary ""` clears the body.
Omitting `--summary` leaves the summary as parsed.

An edit whose every provided flag matches the stored logical value exits 0,
writes no commit, and does not call `hash-object`. Matching means: provided
title equals the parsed title, provided summary equals the parsed logical
summary, provided spec equals the parsed spec, and `--clear-spec` agrees with
a parsed spec of `None`. Flags that were omitted do not have to match anything.
When at least one provided flag differs, the commit stores the new values and
keeps the omitted fields, then refreshes `updated` and `updated_by`.

`edit` with none of `--title`, `--summary`, `--spec`, and `--clear-spec`
exits 1 with `nothing to change`.

`close` and `reopen` do not take field flags. A second `close` or a second
`reopen` exits 1 and does not commit. The first one canonicalizes the file,
drops unknown keys, and refreshes `updated` and `updated_by`.

## Alternatives considered

- **Files under `docs/backlog` on the default branch.** On a protected branch
  every new item and every summary edit is a pull request. A backlog cannot
  bear that.
- **`refs/nw/backlog`.** The commit is the same, and the file format does not
  record the ref, but a ref outside `refs/heads` does not show up as a branch.
  The ref is `refs/heads/nw-backlog`.
- **An index file listing every id.** Every create would conflict on it.
- **One spec that also reads, publishes, and merges.** A defect in those paths
  can drop a write or move a ref. They are later specs, with the stray, same-file,
  and delete rules fixed in Non-goals rather than left to a content merge.
- **Repairing an invalid item inside `edit` or `close`.** A selected blob that
  fails validation is left unchanged so a command cannot rewrite a file it
  cannot parse.

## Acceptance criteria

- [x] `nw backlog add --title "Public backlog" --summary "The summary."` with
      `current_time` fixed at `2026-11-07T15:41:00.900000Z` and identity
      `Ada Lovelace` / `ada@example.com` creates a parentless
      `refs/heads/nw-backlog`. The tree is the fixed README bytes plus one
      `REQ-????????.md` whose stem matches `id` and whose eight characters are
      from `0123456789ABCDEFGHJKMNPQRSTVWXYZ`. Frontmatter keys are `id`,
      `title`, `status`, `created`, `updated`, `created_by`, `updated_by` in
      that order, `status` is `open`, both instants are
      `2026-11-07T15:41:00Z`, and both identities are
      `Ada Lovelace <ada@example.com>`. `spec` is absent. Stdout is `<id>\n`
      and stderr is empty. The work tree does not gain those files.
      `git log -1 --format=%B` is `backlog: add <id>\n`. Author and committer
      names, emails, and unix times equal that identity and that second.
      A second `add` leaves the first filename in place and adds no path other
      than the new item. If `README.md` already differs from the fixed text,
      its blob sha is unchanged. If a plumbing commit removes `README.md`,
      the next `edit` commit names `README.md` and that one item, the README
      bytes are the fixed text, and every other `REQ-*.md` blob is unchanged.
      `git log --diff-filter=R --name-only` for the branch is empty. No `add`,
      `edit`, `close`, or `reopen` commit deletes a path.
- [x] `--title` is required on add. A whitespace-only title and a title that
      contains a newline each exit 1 with `title is empty` or
      `title must be one line` and do not call `hash-object`. `--summary` is
      required on add; omitting it exits 1 with `summary is required` and
      writes nothing. `--summary ""` and a summary that normalizes to empty
      store no body: the blob ends at the closing `---` line's newline.
      `--summary "  "` stores a body of two spaces and one newline.
      `--summary "Hello\r\n"` stores `Hello` plus one LF. `--title` whose text is
      `a`, a double quote, `b`, a backslash, and `c` is stored as a line
      `title: "a\"b\\c"`. An edit `--summary ""`
      clears an existing body. `--spec SPEC-0473` adds that key last, and
      `--spec SPEC-449` exits 1 with `invalid spec id` and no commit.
      `--spec` together with `--clear-spec` exits 1 with
      `spec and clear-spec are mutually exclusive` and no commit, including
      when `--repo` is not a git directory. `edit` with no field flag exits 1
      with `nothing to change`. An id that is not `REQ-` plus eight alphabet
      characters, with an optional `.md`, exits 1 with `invalid id`. An edit
      whose provided title, summary, spec, and clear-spec all equal the stored
      logical values exits 0, does not call `hash-object`, and leaves an
      `idea` key in the blob. An edit that changes the title of a file that
      also contains `idea` and `IDEA-1019` writes a blob with no key outside
      the format, the new title, and the previous `created` and `created_by`.
- [x] `parse_item` returns the reason token of the first matching validation
      row for a fixture of each row, including YAML 1.1 `status: yes`, a
      duplicate key, a filename/id mismatch, `SPEC-12`, an offset timestamp,
      and `2026-02-31T00:00:00Z`. `edit`, `close`, and `reopen` of that blob
      exit 1 with `invalid backlog item <id>: <token>` and leave the tip sha
      unchanged. A sibling blob that fails validation does not block `add` or
      an edit of a different valid item, and the sibling's blob sha is
      unchanged. A valid file whose `updated` is earlier than `created`, or
      whose keys are permuted, can be closed; the new blob is canonical and
      keeps those instants' `created` value and the parsed `created_by`.
      `parse_item` on a blob whose frontmatter keys are `yes` and `true`
      returns `invalid yaml`, not `duplicate key True`. A repeated string
      key `id` returns `duplicate key id`. `edit`, `close`, and `reopen` of the boolean-key
      blob exit 1 with `invalid backlog item <id>: invalid yaml`, do not call
      `hash-object`, and leave the tip sha unchanged.
- [x] `close` sets `status: closed`, refreshes `updated` and `updated_by`,
      drops unknown keys, and leaves title, summary, spec, `created`, and
      `created_by` as parsed. A second `close` exits 1 with
      `<id> is already closed` and does not call `hash-object`. `reopen`
      returns `status` to `open`, and a second `reopen` exits 1 with
      `<id> is already open` and no commit. Unknown id exits 1 with
      `<id> is not on nw-backlog`.
- [x] With `user.name` or `user.email` missing or whitespace-only, `add` exits
      1, prints `<key> is unset` for each such key with the name line first,
      and does not call `hash-object`. A value that contains a line break
      prints `<key> contains a line break`. A name containing `<` or `>`, and
      an email containing whitespace or `"`, exit 1 the same way and do not
      move the ref. When the only identity is in the global config, the commit
      uses it. When local config and `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`,
      `GIT_AUTHOR_DATE`, and `GIT_COMMITTER_NAME` are all set to different
      values, the commit's author and committer are the local pair and the
      injected instant. A name that contains a double quote is stored in
      `created_by` with the same backslash and quote escaping as a title.
      `user.name` uses git's ident crud set: a code point `<= 32`, or one of
      `.`, `,`, `:`, `;`, `<`, `>`, `"`, `'`, and `\`. `(` and `)` are outside
      that set. `Ada <Lovelace` exits 1 with `user.name contains '<'`
      and does not call `hash-object`. `.` and `'` exit 1 with
      `user.name consists only of disallowed characters` and do not call
      `hash-object`. `Ada Jr.` exits 1 with
      `user.name contains a leading or trailing disallowed character`, does
      not call `hash-object`, and leaves the ref unchanged, so the stored
      name stays the raw `user.name` and is never rewritten to `Ada Jr`.
      `"Ada"` commits and `created_by` keeps
      the quotes. `()` commits, and `%an` and `%cn` equal `()`.
- [x] A tip whose tree or whose root commit contains `notes.txt`,
      `REQ-IIIIIIII.md`, a slash path, or mode `100755` makes `add`, `edit`,
      `close`, and `reopen` exit 1 with the header
      `refs/heads/nw-backlog is not an orphan backlog` and the matching
      `stray:` or `mode:` line, without `hash-object`. A tip that is only
      legal backlog files, reached by a root commit that contains `src/main.c`,
      is refused with a `root <sha> stray: src/main.c` line and the same
      unchanged ref.
- [x] After `add`, `edit`, `close`, `reopen`, a no-op edit, a stray refusal,
      and an invalid-item refusal, the starting work tree's `HEAD`, index
      bytes or index absence, and `git status --porcelain` are unchanged,
      including a dirty index, a detached `HEAD`, and an unborn `HEAD`. No
      command creates `MERGE_HEAD`. The git spy records only the allowlisted
      subcommands. Every `update-ref` is
      `refs/heads/nw-backlog <new> <expected>` with no `-d`, and `<expected>`
      is forty zeros on the creating add. If any worktree has `nw-backlog`
      checked out, the command exits 1, stderr names that worktree path, and
      `hash-object` is not called. `edit` reads `git show <sha>:<path>`: a
      dirty work-tree file of the same relative path is unchanged and is not
      the source of the new blob.
- [x] `mint_id` maps `99e23a5c56` to `REQ-K7H3MQ2P` and `abcd123456` to
      `REQ-NF6H4D2P`. Eight colliding draws exit 1 with `could not mint an id`
      and do not draw a ninth time. When `update-ref` compare-and-swap fails
      three times and each failure leaves a different valid backlog tip that
      does not contain the minted id, `add` exits 1 with `nw-backlog changed`,
      calls `update-ref` three times and not a fourth, and the ref remains the
      last winner. The expected old sha of each call is the sha that attempt
      read. If `update-ref` fails and the ref sha is still `<expected>`, `add`
      exits 1 with `update-ref failed` after that one call and does not retry.
      If the second tip already contains the minted id, `add` exits 1 with
      `<id> is already on nw-backlog`, does not call `update-ref` again, and
      does not mint a different id.
- [x] `--repo` selects the repository that receives the branch. The current
      directory is the default. A `--repo` that is not a non-bare work tree
      exits 1 with `not a git work tree` after argument validation.
      `nw backlog --help` lists `add`, `edit`, `close`, and `reopen` and does
      not list `list`, `show`, `fetch`, `push`, or `resolve`. `add --help`
      lists `--title`, `--summary`, `--spec`, and `--repo`, and does not list
      `--push` or `--clear-spec`. An aware `current_time` of
      `2026-11-07T10:41:00-05:00` is stored as `2026-11-07T15:41:00Z`.
      `nw backlog add --help`, `edit --help`, `close --help`, and
      `reopen --help` omit `--visibility`, `--public`, and `--private`.
      The item blob after `add`, and after `edit`, `close`, and `reopen`, has
      no `visibility` key. `git rev-parse --symbolic-full-name nw-backlog` is
      `refs/heads/nw-backlog`.

## Test plan

- **Automated tests:** `tests/test_backlog.py`, using temporary repos.
  `tests/conftest.py` sets `user.name` and `user.email` on its git fixture;
  tests that isolate identity point `GIT_CONFIG_GLOBAL` and
  `GIT_CONFIG_SYSTEM` at their own files. A git spy records subcommands.
  `current_time` is monkeypatched. CliRunner covers exit codes and stdout.
  The tests and the criteria they lock:
  - `test_add_writes_orphan_readme_and_req_file` — parentless root, README
    bytes, alphabet, frontmatter order, stdout, author instant floored from
    a microsecond clock, work tree untouched.
  - `test_second_add_does_not_add_an_index`,
    `test_existing_readme_bytes_are_left_alone`,
    `test_edit_restores_missing_readme_only`, and
    `test_commands_do_not_delete_or_rename_items`.
  - `test_add_summary_contract` — missing summary, `""`, whitespace-only,
    CR/LF normalization, and title escaping.
  - `test_rejected_arguments_write_nothing` — missing title, blank title,
    multiline title, bad id, bad spec id, no field flag, mutual exclusion
    before a missing repo.
  - `test_edit_equal_values_do_not_write` and
    `test_edit_drops_unknown_keys_when_a_field_changes`.
  - `test_parse_item_reason_tokens` and
    `test_mutation_refuses_each_invalid_form` — one fixture per validation
    row, first-match token, tip unchanged.
  - `test_yes_and_true_are_invalid_yaml_before_duplicate_key` — frontmatter
    keys `yes` and `true` return `invalid yaml`, not `duplicate key True`.
    A repeated string key `id` returns `duplicate key id`. `edit`, `close`,
    and `reopen` of the boolean-key blob exit 1 with
    `invalid backlog item <id>: invalid yaml`, do not call `hash-object`,
    and leave the tip sha unchanged.
  - `test_malformed_sibling_does_not_block_other_writes`.
  - `test_close_and_reopen_are_single_file_commits` — field preservation,
    copied `created_by`, repeated status, permuted keys, `updated` earlier
    than `created`.
  - `test_refuses_unset_or_illegal_identity` and
    `test_author_follows_local_config_not_environment_or_global` — local
    identity wins, and a double quote in the name is escaped in `created_by`.
  - `test_name_crud_matches_git_ident` — `Ada <Lovelace` is
    `user.name contains '<'`; `.` and `'` are
    `user.name consists only of disallowed characters`; `Ada Jr.` is
    `user.name contains a leading or trailing disallowed character`. None of
    those call `hash-object`, and `Ada Jr.` leaves the ref unchanged.
    `"Ada"` commits and `created_by` keeps the quotes
    (`test_created_by_escapes_leading_trailing_quotes_in_name`). `()` commits
    and `%an` and `%cn` equal `()`.
  - `test_refuses_non_utf8_git_identity`,
    `test_refuses_pyyaml_unsafe_git_identity`, and
    `test_refuses_line_separator_in_title_and_git_name` — strict UTF-8 and
    control characters in `user.name` and `user.email` exit 1 before
    `hash-object`.
  - `test_mutations_reject_stray_and_bad_mode` and
    `test_refuses_a_root_that_contains_product_history`.
  - `test_dirty_detached_and_unborn_checkout_stay_put` — index bytes, porcelain
    status, no `MERGE_HEAD`, allowlisted git subcommands, creating
    `update-ref` expected sha of forty zeros, work-tree file is not the blob
    `edit` reads.
  - `test_refuses_when_nw_backlog_is_checked_out`.
  - `test_mint_vectors_and_eight_draw_cap`.
  - `test_compare_and_swap_gives_up_after_three_attempts`,
    `test_update_ref_failure_without_a_moved_ref_does_not_retry`, and
    `test_retry_stops_when_the_minted_id_appears`.
  - `test_repo_flag_and_command_surface` — `--repo`, default directory,
    non-work-tree, help text has the local commands and omits the later
    slices' commands and `--push`.
  - `test_no_visibility_flag_and_ref_is_a_branch` — `add`, `edit`, `close`,
    and `reopen` help omit `--visibility`, `--public`, and `--private`; the
    blob after each of those writes has no `visibility` key;
    `git rev-parse --symbolic-full-name nw-backlog` is `refs/heads/nw-backlog`.
  - `test_aware_clock_is_stored_as_utc`.
  Implementers run `uv run pytest -q tests/test_backlog.py` and
  `python3 tools/affected_tests.py --run uv run pytest -q`. They do not run
  the full suite. Reporting, show-beside-stray, same-file merge, and
  resolve-deletes tests are named under Non-goals and are not part of
  `tests/test_backlog.py`.
- **Manual verification:** from a product checkout whose current branch is not
  `nw-backlog`, record `git rev-parse HEAD` and `git status --porcelain`.
  Run `nw backlog add --title "Try" --summary "One item"`. `HEAD` and the
  porcelain status are unchanged, and the work tree has no new backlog path.
  `git rev-parse nw-backlog^` fails. `git log -1 --format='%an <%ae>' nw-backlog`
  is the repo's `user.name` and `user.email` as `Name <email>`.
  `git ls-tree -r --name-only nw-backlog` prints `README.md` and one
  `REQ-????????.md`. `nw backlog edit <that id> --title "Next"` keeps that
  filename. A second `nw backlog close <that id>` exits 1. No forge is
  required for this slice.
- **Regression guard:** `test_dirty_detached_and_unborn_checkout_stay_put`
  and the git allowlist assertion. `src/nightwright/agentic/gitops.py` stays
  unchanged, so campaign commits keep their existing temporary-index behavior.
  The new module is the test file this work adds, plus the `cli.py`
  registration. `tests/test_gate_docs.py` still sees `gate` in the root help.

### Test record (2026-11-07)

- `uv run pytest -q tests/test_backlog.py` — 49 passed.
- `python3 tools/affected_tests.py --run uv run pytest -q` — left to gating
  after the backlog-only run above.
- Full suite left to gating.

### Test record (loop 3, 2026-11-07)

- `uv run pytest -q tests/test_backlog.py` — 56 passed (PyYAML unsafe title and
  identity, UTF-8 title and summary, reopen canonical bytes, inherited git env,
  safe-path stderr).
- Manual checkout checks from the test plan: performed on a non-``nw-backlog``
  product checkout; ``HEAD``, porcelain status, and work tree unchanged after
  ``add``; ``nw-backlog`` is parentless; author matches local identity.
- Full suite left to gating.

### Test record (loop 0, 2026-11-07)

- `uv run pytest -q tests/test_backlog.py` — 65 passed (reviewer gaps: README
  literal bytes, C1 summaries, edit refusals, parse variants, U+2028/2029, index
  bytes, email punctuation, quoted ``created_by``).
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (loop 1, 2026-11-07)

- `uv run pytest -q tests/test_backlog.py` — 69 passed (render-safety on parsed
  ``created_by`` and YAML-surviving ``title`` escapes, edit ``%B`` message,
  title-only no-op, ``Hello\\rworld`` summary normalization, non-strict-UTF-8
  toplevel ``--repo``).
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (loop 2, 2026-11-07)

- `uv run pytest -q tests/test_backlog.py` — 75 passed (CAS retry lands commit,
  README stable on edit/close/reopen, ``show`` uses captured tip sha, detached
  and unborn full command set, unknown id with missing ref, empty-tree parent,
  safe-path stderr controls, deep YAML ``invalid yaml``, git-only-disallowed
  name).
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (campaign loop 0, 2026-11-10)

- `uv run pytest -q tests/test_backlog.py` — 76 passed (added
  ``test_no_visibility_flag_and_ref_is_a_branch``; ticked remaining acceptance
  criteria).
- Manual checkout checks from the test plan: ``HEAD``, porcelain status, and work
  tree unchanged after ``add`` on this worktree; parentless ``nw-backlog``;
  author matches local identity; symbolic ref is ``refs/heads/nw-backlog``.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (campaign loop 1, 2026-11-10)

- `uv run pytest -q tests/test_backlog.py` — 78 passed (``_construct_mapping`` rejects
  non-string keys before duplicate check; git ident crud in ``_identity_error``;
  ``test_yes_and_true_are_invalid_yaml_before_duplicate_key``,
  ``test_name_crud_matches_git_ident``, edit summary CR/LF no-op).
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (campaign loop 2, 2026-11-10)

- `uv run pytest -q tests/test_backlog.py` — 85 passed (review fixes:
  ``GIT_NO_REPLACE_OBJECTS`` on ``_git``, ``_safe_path`` U+0085/LS/PS, duplicate-key
  token via ``_safe_path``, ident crud space/tab/comma/colon/semicolon, CAS retry
  precondition stop, unquoted close title assertion, replace-ref sibling retention).
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (campaign loop 3, 2026-11-10)

- `uv run pytest -q tests/test_backlog.py` — 91 passed (``_identity_error`` uses
  ``_safe_path(bad)``; CAS add/edit retry instant and rival re-read; missing-item
  retry stop; process-wide ``yaml.safe_load`` timestamps; system-config identity;
  identity-before-worktree; two-root ascending sha; email whitespace before ``<``).
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (campaign loop 0 review, 2026-11-10)

- `uv run pytest -q tests/test_backlog.py` — 94 passed (review gaps: ``status: done``
  membership, YAML-escaped newline in stored ``title``, CR in ``user.name``/``user.email``,
  ``reopen`` invalid-id rejections, ``test_reopen_canonicalizes_a_noncanonical_closed_blob``).
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (campaign loop 1 review, 2026-11-10)

- `uv run pytest -q tests/test_backlog.py` — 105 passed (adversarial review: missing-key
  coverage, ``title: 1``/``title: yes``, summary ``---`` round-trip and mutations,
  empty-body ``parse_item``, leading-newline logical summary, ``%ai``/``%ci`` ``+0000``,
  ``test_reopen_refuses_unrenderable_closed_item``).
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- Full suite left to gating.

### Test record (campaign loop 2, 2026-11-10)

- Gate triager fixes: deleted stray ``0449-public-backlog-on-an-orphan-nw-backlog-branch.md``
  (duplicate SPEC-0449); ``pytestmark = pytest.mark.fast`` on ``tests/test_backlog.py``.
- `uv run pytest -q tests/test_backlog.py tests/test_specs.py::test_all_specs_pass_lint tests/test_test_tiers.py::test_every_real_test_module_has_exactly_one_tier` — 107 passed.
- `python3 tools/spec_lint.py --write-ledger` and `python3 tools/spec_lint.py` — exit 0.
- `python3 tools/test_tiers.py --check` — exit 0.
- Full suite left to gating.

## Clock Log

No session is recorded in this file. Implementation clocks IDEA-1019 with
`nw idea clock-in` and `nw idea clock-out`.

- 2026-11-07: implementation session. `nw idea clock-in IDEA-1019` was refused
  (`NW_PERSONA_RUN=1` blocks org writes from a campaign agent). Wall time stays
  on the queue transcript.

## Rollout / migration

There is no `docs/backlog` tree to import. The first `nw backlog add` creates
the orphan branch in the local repository. This spec does not push it and does
not set a forge rule. Visibility is the repository's fetch permission: a
private repository keeps `nw-backlog` as private as the code, and this slice
does not add a visibility flag. Existing clones are unchanged until the
publication spec fast-forwards `nw-backlog`. The product checkout does not
move.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual steps performed and
      noted in the spec.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).
- [x] `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
