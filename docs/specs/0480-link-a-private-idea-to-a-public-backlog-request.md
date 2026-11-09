---
id: SPEC-0480
title: Link a private idea to a public backlog request
status: proposed
owner: user
created: 2026-11-08
kind: feature
tags: [public-backlog]
source_idea: IDEA-1020
---

## Context

A private idea is an `IDEA-NNN` headline in the local org file. It keeps its TODO
state, a Summary, open questions, a log, and clock entries. Graduation is
unchanged: the idea becomes `SPECCED` when a spec is written. The org file is
not copied between machines.

IDEA-1019 is the public record: an orphan branch `refs/heads/nw-backlog` in the
project's git repo, one `REQ-` file per item at the branch root. The file has
no idea id. This spec is the link from one local idea to one of those files,
and the four operations on that link. Publish and adopt create the link. Push
and pull copy the title and the summary. Show and `nw next` report which side
moved. They do not write.

An earlier pull took the headline's tag cookie as `rest[tm.start():]` after
`_RE_HEADING_TAGS.search`. That pattern matches from the start of the
remainder, so the slice was the state word, the old title, and the tags. The
rewrite then wrote the new title in front of that whole remainder. A headline
`* INCUBATE  Link :session-triage:` pulled to `Widgets` kept a second
`INCUBATE` and the old title. The test only checked that `:session-triage:`
still occurred on the line, so it stayed green.

A heading lookup with no level must keep the headline at `task.line`. The
summary reader that did not keep it emptied Summary after a stamp created the
properties drawer, and
`tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections`
failed.

## Goals / Non-goals

**Goals**

- `canonical_pair` and `req_base` define the title and summary that count as
  the same text. `:REQ_BASE:` stores that digest.
- `nw idea publish` creates one public file from the idea's title and Summary
  and sets `:REQ:` and `:REQ_BASE:`. The idea stays `IDEA-NNN` and keeps its
  TODO state.
- `nw idea adopt` creates one local idea from a public file that this project's
  org does not already link, and sets the same two properties.
- `nw idea push` writes the private title and Summary into the file, copies
  every other front-matter key, and updates `:REQ_BASE:`.
- `nw idea pull` writes the file's title into the headline and the file's
  summary into Summary, leaves the log and the questions, and updates
  `:REQ_BASE:`. The trailing tag cookie stays a trailing tag cookie: one state
  word, and the old title is gone.
- `nw idea show` and `nw next` are the only callers of `link_status`. They
  compare the private pair, the file, and `:REQ_BASE:`, and name push, pull,
  or both texts. They do not write. `nw ideas`, `nw hub`, and the TUI leave
  the branch unread.
- A heading lookup with no level keeps the headline at `task.line` only, so
  the stamp test above still sees Summary, Open questions, and Log.

**Non-goals**

- Fetch, fast-forward push, same-file merge, and force-push policy for
  `nw-backlog`. Those belong to IDEA-1019. No command in this spec runs
  `git checkout`, `git switch`, `git add`, `git fetch`, or `git push`.
- Writing `source_request` on a spec, or writing `spec:` because a spec was
  created. That is IDEA-1021.
- `nw queue add REQ-…`. That is IDEA-1022.
- Syncing the org file between machines. The link exists only on the machine
  that holds the idea.
- Publishing ideas while loading the org file, from `nw hub`, or from the TUI.
  An existing idea stays unpublished until `nw idea publish`. Reading the
  branch from `nw ideas`, `nw hub`, or the TUI.
- Mapping file `status` onto the idea's TODO state, or closing the file when
  the idea is dropped.
- Storing the idea id in the public file.

## Decision

Publishing is a link, not a graduation. The idea keeps its TODO state.

`:REQ:` is `REQ-` plus eight Crockford characters
(`0123456789ABCDEFGHJKMNPQRSTVWXYZ`). `:REQ_BASE:` is the SHA-256 hex digest of
the canonical title and summary at the last time the two sides matched.

The private title is the idea headline. The private summary is the Summary
body. Pull sets the headline from the file title and the Summary body from the
file summary, with no markup conversion and no internal-whitespace collapse.
The log and the open questions stay. The file title has to land on the
headline: that is the title push sends, and the digest covers both fields.
Leaving the headline stale would make the link look private-moved on the next
check.

On pull, `tag_part` is the trailing tag cookie only (`rest[tm.end(1):]`). It
is not the whole remainder. A headline `* INCUBATE  Link :session-triage:`
pulled to `Widgets` becomes `* INCUBATE  Widgets :session-triage:`, one state
word and one tag block. A headline
`* INCUBATE [#A] Link :session-triage:` pulled to `Widgets` becomes
`* INCUBATE [#A] Widgets :session-triage:`.

`resolve_linked_repo` exits 1 with `project is required` on a blank project
and does not call `project_root`. Meta-Tools uses `nw_checkout` and ignores
`outbox_targets` and the current directory. Every other project uses
`project_root`, which reads `outbox_targets[project].repo_path`. No command
takes `--repo`. An idea with no `:PROJECT:` cannot be published, because the
branch lives in that project's repo.

`link_status` runs only from `nw idea show` and `nw next`. Nothing syncs in
the background. When both sides differ from `:REQ_BASE:`, both texts are shown
and neither command runs until a person runs push or pull.

A summary lookup that has no level matches only the headline at `task.line`.
Creating `:REQ:` or `:UPDATED:` does not drop Summary, Open questions, or Log.

## Design

### Canonical pair

`idea_link.canonical_pair(title, summary)` in `src/nightwright/idea_link.py`
strips the title, converts CR LF and remaining CR in the summary to LF, then
strips the summary. An empty summary is `""`. Internal spaces and marker
characters such as `**` stay.

`idea_link.req_base(title, summary)` is the lowercase SHA-256 hex digest of the
UTF-8 bytes of `canonical_title + "\n" + canonical_summary`. There is no
byte-order mark and no extra newline after the summary.

| Inputs | Payload | Digest |
|---|---|---|
| `("  Link  ", " \n Hello \n ")`, `("Link", "Hello")`, `("Link", "Hello\n")` | `Link\nHello` | `9700eaa1f712c11423e0542ae8882ba96653f4575be89e3f481d05df79eff208` |
| `("Link", "Hello\r\nworld\r")` | `Link\nHello\nworld` | `a0d1682323b9536aa06d0ce3c5a21ab22ac8f0be7cf24906aa88df17bc50c45d` |
| `("Link", " \r\n ")` | `Link\n` | `f8e7c032715c4691e8f0e4bfa4fae7b80f449f9fcae4c077b00d1bb1dd9fa654` |

### Request file

One file, `REQ-<eight>.md`, at the root of `refs/heads/nw-backlog`. The file
is UTF-8. It starts with a line `---`, then YAML keys in this order: `id`,
`title`, `status`, `created`, `updated`, `created_by`, `updated_by`, then
`spec` only when the previous file had that key. `title` is a double-quoted
YAML scalar. `status` is `open` or `closed`. `created` and `updated` are UTC
instants `YYYY-MM-DDTHH:MM:SSZ`. `created_by` and `updated_by` are
`Name <email>` from `user.name` and `user.email`. A line `---` closes the
header. The summary body follows, with no extra heading. An empty summary is
an empty body. Pull and `link_status` parse it with `split_frontmatter`. A
file that does not parse exits 1 with `invalid request file` and writes
nothing. Publish and push refuse before any write when `user.name` or
`user.email` is unset.

Publish creates the file with `status: open` and no `spec` key. The blob
contains no `IDEA-NNN` unless that text was already in the title or the
summary. Push changes `title`, the body, `updated`, and `updated_by`, and
copies every other front-matter key through unchanged.

Publish and push build one commit with `git hash-object`, `git mktree`, and
`git commit-tree`, then move `refs/heads/nw-backlog` with `git update-ref`.
They do not run `git checkout`, `git switch`, `git add`, `git fetch`, or
`git push`. The parent is the current tip, or there is no parent when the ref
is absent. The tree is the parent tree, or an empty tree when there is no
parent, with only `REQ-<id>.md` added or replaced. Every other path stays
byte-for-byte. The message is `publish REQ-<id>` or `push REQ-<id>`. Author
and committer are that repo's `user.name` and `user.email`. If `HEAD` is
`refs/heads/nw-backlog`, both commands exit 1 before any object is written,
stderr `refusing to update nw-backlog while it is checked out`. Otherwise the
symbolic `HEAD`, the index, and the worktree stay as they were. Read is
`git show refs/heads/nw-backlog:REQ-<id>.md`.

### Repository

`idea_link.resolve_linked_repo(cfg, project)` exits 1 with
`project is required` on a blank project and does not call `project_root`.
For Meta-Tools it uses `nw_checkout` (config `nw_checkout`, otherwise the
discovered nightwright checkout) and ignores `outbox_targets` and the current
directory, even when `outbox_targets` also has a Meta-Tools `repo_path`. For
every other project it uses `project_root`
(`src/nightwright/agentic/project.py`), which reads
`outbox_targets[project].repo_path`. A missing root, including an empty
`repo_path`, exits 1 with a stderr line that starts
`cannot resolve a repo for project ` and names the project.

### Commands

Registered on the existing `idea` group in `src/nightwright/cli.py`.

| Command | Effect |
|---|---|
| `nw idea publish SELECTOR` | Mint one file from the canonical title and Summary, then set `:REQ:` and `:REQ_BASE:` in the same org write. TODO state, headline, Summary, log, questions, tags, clocks, and every other property stay. `:UPDATED:` may move. |
| `nw idea adopt REQ-ID --project NAME` | Append one idea in state `IDEA` with the canonical title left intact, the Summary body as plain lines, that `:PROJECT:`, empty questions, an empty log, and both properties. The file is unchanged. |
| `nw idea push SELECTOR` | Write the private canonical title and Summary into the linked file. Copy every other front-matter key. Set `:REQ_BASE:` to the new digest. |
| `nw idea pull SELECTOR` | Keep the stars, the gap after the stars, the TODO keyword, the whitespace between that keyword and the heading text, the priority cookie, and the trailing tag cookie. Replace only the heading text with the file title. Set Summary from the file summary as plain lines. Leave the log, the questions, and the clocks. Set `:REQ_BASE:` to the digest of that file pair. |

Publish, adopt, push, and pull copy the canonical title and canonical summary
with no markup conversion. They do not call `to_org_body`, `from_org_body`,
`set_summary`, `retitle_idea`, or `add_idea`'s whitespace collapse. Adopt
appends to `org_ideas_file` with the canonical title left intact, then writes
the Summary body as plain lines. After each of the four commands, `req_base`
of the private headline and Summary equals `req_base` of the file title and
summary and equals `:REQ_BASE:`. `link_status` compares those two `req_base`
values to `:REQ_BASE:`.

Publish refuses, writes no commit, and sets neither property when the selector
is not an idea (`{selector!r} is not an idea`), the canonical title is empty
(`title is empty`) or contains a line feed (`title must be one line`), or
`:REQ:` is already set (`<id> is already linked to <REQ-…>`). Stdout on
success is `<id>\n`.

Adopt stores `:REQ:` without a `.md` suffix. An id that is not `REQ-` plus
eight Crockford characters, with an optional `.md`, exits 1 with
`invalid id`. An unknown file exits 1 with `<id> is not on nw-backlog`. A
second adopt of the same `:PROJECT:` and `:REQ:` exits 1, including when the
existing idea is `DROPPED` and including an idea in
`org_ideas_archive_file`. The same id on a different project does not block.
Omitting `--project` exits 1 with `project is required`. The file's `status`
and `spec` are not copied onto the idea: a `closed` file still creates state
`IDEA`, and a file with `spec` creates an idea with no `:SPEC:`.

Push and pull refuse when `:REQ:` is unset or the file is missing, and they
do not mint a replacement file. A file that does not parse exits 1 with
`invalid request file` and writes nothing.

### Pull headline

`_pull_rewrite` matches the headline with `_HEADLINE_RE`
(`src/nightwright/org_write.py`) and the tag cookie with
`_RE_HEADING_TAGS` (`src/nightwright/org.py`),
`(.*?)\s*:([\w@:-]+):\s*$`.

```python
tm = _RE_HEADING_TAGS.search(rest)
tag_part = rest[tm.end(1):] if tm else ""
body = tm.group(1) if tm else rest
```

`rest[tm.start():]` is the whole remainder, because the match starts at index
0. `rest[tm.end(1):]` starts at the whitespace before `:tags:`, which is the
trailing cookie `set_idea_tags` keeps with `_TAG_BLOCK_RE`. The state word and
any `[#A]`, `[#B]`, or `[#C]` cookie are parsed from `body`, not from the full
match. The whitespace that already separates the state word from the heading
text stays, and so does the whitespace around a priority cookie. Only the
heading text is replaced. `.rstrip()` on the rebuilt line must not eat the
cookie.

- `* INCUBATE  Link :session-triage:` pulled to `Widgets` is exactly
  `* INCUBATE  Widgets :session-triage:`.
- `* INCUBATE [#A] Link :session-triage:` pulled to `Widgets` is exactly
  `* INCUBATE [#A] Widgets :session-triage:`.
- A no-op pull, file title still `Link`, leaves each of those headlines
  unchanged.

Each result contains the state word once and the tag block once, and it does
not contain the old title. The public blob does not contain `:session-triage:`
unless that text was already in the title or the summary. After the pull,
`req_base` of the private headline and Summary equals `req_base` of the file
pair and equals `:REQ_BASE:`, and `link_status` is `quiet`.

### Drift

`idea_link.link_status(cfg, task)` reads the local ref only. It compares
`req_base` of the private headline and Summary and `req_base` of the file
title and summary to `:REQ_BASE:`.

- Private `req_base` differs and the file `req_base` equals `:REQ_BASE:`:
  `req_link` is `push`.
- File `req_base` differs and the private `req_base` equals `:REQ_BASE:`:
  `req_link` is `pull`.
- Both differ from `:REQ_BASE:`: `req_link` is `diverged`.
- Both equal `:REQ_BASE:`: `req_link` is `quiet`.
- A missing ref or a missing file: `req_link` is `missing`.

`link_status` runs only from `nw idea show` and from `nw next`.
`idea_show_payload` adds `req` and `req_link` only when the caller passes
`link=True`. `nw idea show`, including `--json`, passes `link=True`.
`format_idea_show` prints `req: REQ-…` when it is set. quiet adds no push,
pull, or diverged lines. diverged prints the private headline, the private
Summary, the file title, and the file summary, then the line
`choose: nw idea push <id>  or  nw idea pull <id>`. missing prints
`REQ-<id> is not on nw-backlog`. `next_step_for_idea` does not call
`link_status`. `nw next`, after `idea_row`, replaces that idea's hint when
`req_link` is push (`nw idea push <id>`), pull (`nw idea pull <id>`),
diverged (the same choose line, and the plain and JSON output also include
both texts), or missing (`REQ-<id> is not on nw-backlog`). quiet leaves the
usual hint. `nw ideas`, `nw hub`, and the TUI call `idea_show_payload` and
`next_step_for_idea` with the branch unread, including when `:REQ:` is set.
Show and `nw next` do not create the ref and do not change the org file.

### Org-write failure

Publish records the old `nw-backlog` tip (the sha, or the fact that the ref
is absent) before adding the file. If that add succeeds and the org write
then fails, the org bytes stay as they were and the command compare-and-swaps
the ref back: `git update-ref -d refs/heads/nw-backlog <new>` when the ref was
absent, otherwise `git update-ref refs/heads/nw-backlog <old> <new>`. Stderr
is `REQ-<id> was not linked; restored refs/heads/nw-backlog` and the exit code
is 1. If the ref no longer points at `<new>`, the command leaves it, exits 1,
and stderr is `REQ-<id> was not linked; refs/heads/nw-backlog no longer points at <new>; left unchanged`.

Push records the old tip the same way. On a failed org write it restores that
tip, or deletes the ref when it was absent. Stderr is
`REQ-<id> was not updated; restored refs/heads/nw-backlog`. When the ref no
longer points at `<new>`, stderr is
`REQ-<id> was not updated; refs/heads/nw-backlog no longer points at <new>; left unchanged`
and the ref stays. Pull writes only the org file, so a failed pull leaves the
branch tip unchanged.

### Heading lookup

Publish reads the Summary through `idea_summary_text` and
`read_idea_enrichment` on the Task from `resolve_selector`, which has a level.
Do not add `_idea_summary_text_from_file`. `_is_same_headline` keeps today's
comparison when `task.level` is an int. When level is absent or None, it
returns true only for `idx == task.line - 1`, and only when that line is a
headline and, if `task.heading` is set, the heading text still matches. It
returns false for every other index, so `_locate_headline` does not raise
`N matches`. Creating the `:PROPERTIES:` drawer for `:REQ:` and `:REQ_BASE:`
stays after the body edit, `stamp_updated` last, and does not replace
`** Summary`, `** Open questions`, or `** Log`. The stamp test remains the
guard.

## Alternatives considered

- **Graduate the idea into the file and drop the org headline.** The public
  request is the shareable statement. The log, the clocks, the questions, and
  the TODO state stay private, and the idea can keep moving after publication.
- **Store `IDEA-NNN` in the public file.** The file format has no idea id. The
  link is the `:REQ:` property on the machine that holds the idea.
- **Write the file title into Summary and leave the headline on pull.** Push
  sends the headline as the title, and `:REQ_BASE:` hashes both. A pull that
  left the headline unchanged would disagree with the base on the next show.
- **Take the tag cookie as `rest[tm.start():]`.** The match starts at index 0,
  so that slice is the remainder. Pull then duplicates the state word and
  keeps the old title, and the next show reports push.
- **Sync on a timer, or while rendering `nw hub`.** The check runs when the
  idea is shown or `nw next` is asked. The person chooses before any write.
- **Treat a missing level as "not this headline".** That is the failure that
  emptied Summary for a hand-written idea with no properties drawer. A missing
  level that matched every headline would raise `N matches` and empty Summary
  again.

## Acceptance criteria

- [ ] `test_req_base_known_vectors` asserts the three digests in the Design
      table. The expected strings are written in the test, not produced by
      calling `req_base` to obtain the expected value.
- [ ] `nw idea publish` on an `INCUBATE` idea whose `:PROJECT:` resolves
      creates one `open` `REQ-*.md` on that repo's `refs/heads/nw-backlog`
      with the canonical title and summary, no `spec` key, and no idea id in
      the blob when the summary does not already contain that id. The idea's
      TODO state stays `INCUBATE`. `:REQ:` is that id. `:REQ_BASE:` equals
      `9700eaa1f712c11423e0542ae8882ba96653f4575be89e3f481d05df79eff208` for
      the pinned pair `Link` / `Hello`. Headline, Summary, log, questions,
      tags, and `CLOCK:` lines stay. When `nw-backlog` is not checked out, the
      symbolic `HEAD`, the index, and the worktree stay as they were. The
      commit message is `publish REQ-<id>`. Author and committer are that
      repo's `user.name` and `user.email`. The file is in the project's repo,
      not the current directory. Meta-Tools uses `nw_checkout` even when
      `outbox_targets` also has a Meta-Tools `repo_path`. A blank project, a
      missing `:PROJECT:`, an unknown project, and an empty `repo_path` each
      exit 1, add no commit, and set neither property. Publish of an idea that
      already has `:REQ:`, an empty title, or a title that contains a line
      feed exits 1 and adds no commit. Unset `user.name` or `user.email`
      refuses before any write.
- [ ] A second publish of another idea leaves the first file's blob sha
      unchanged, and publish while `nw-backlog` is checked out exits 1 with
      stderr `refusing to update nw-backlog while it is checked out` and adds
      no commit. Push while that ref is checked out exits 1 with the same
      stderr and adds no commit. The new commit's parent is the previous tip,
      or there is no parent when the ref was absent, and every other path
      stays byte-for-byte.
- [ ] The request file is UTF-8. It starts with a line `---`, then YAML keys
      in the order `id`, `title`, `status`, `created`, `updated`,
      `created_by`, `updated_by`, then `spec` only when the previous file had
      that key. `title` is a double-quoted YAML scalar, so a title
      `Link: widgets` round-trips through pull. `created_by` and `updated_by`
      are `Name <email>`. A line `---` closes the header. The summary body
      follows, with no extra heading. An empty summary is an empty body. A
      file that does not parse exits 1 with `invalid request file` and writes
      nothing. Push changes `title`, the body, `updated`, and `updated_by`,
      and copies every other front-matter key through unchanged.
- [ ] `nw idea adopt REQ-ID --project NAME` creates one idea in state `IDEA`
      whose headline and Summary are the file's canonical title and summary,
      with that `:PROJECT:`, that `:REQ:`, and `:REQ_BASE:` of that pair. A
      file title `Link  two` and summary `say **bold**` are stored unchanged.
      The questions and the log are empty. There is no `:SPEC:`. A `closed`
      file still creates state `IDEA`. The file's blob sha is unchanged. A
      second adopt of the same `:PROJECT:` and `:REQ:` exits 1 and adds no
      idea, including a `DROPPED` idea in the archive file. The same `:REQ:`
      on a different project does not block. An unknown id and a missing
      `--project` each exit 1 and add no idea.
- [ ] `nw idea push` replaces the file title and body with the private
      headline and Summary, copies every other front-matter key unchanged,
      including `status` and `spec`, and sets `:REQ_BASE:` to the digest of
      the new pair. The commit message is `push REQ-<id>`. `nw idea pull`
      sets the headline from the file title and Summary from the file summary,
      leaves the log, the questions, and every `CLOCK:` line, and sets
      `:REQ_BASE:` to the digest of the file pair. A title `Link  two` and a
      summary `say **bold**` are unchanged on the idea and in the file, and
      the next show is quiet. The idea's TODO state is unchanged by either
      command. After publish, adopt, push, and pull, `req_base` of the
      private headline and Summary equals `req_base` of the file title and
      summary and equals `:REQ_BASE:`.
- [ ] `_pull_rewrite` sets `tag_part` to `rest[tm.end(1):]` when
      `_RE_HEADING_TAGS` matches, and to `""` otherwise. The state word and
      the priority cookie are parsed from `tm.group(1)`. A headline
      `* INCUBATE  Link :session-triage:` pulled to `Widgets` is exactly
      `* INCUBATE  Widgets :session-triage:`. A headline
      `* INCUBATE [#A] Link :session-triage:` pulled to `Widgets` is exactly
      `* INCUBATE [#A] Widgets :session-triage:`. A no-op pull leaves each
      headline unchanged. Each result has one state word and one tag block,
      and it does not contain the old title. The public blob does not contain
      `:session-triage:` unless that text was already in the title or the
      summary. Log, questions, and `CLOCK:` lines stay. `req_base` of the
      private headline and Summary equals `req_base` of the file pair and
      equals `:REQ_BASE:`, and `link_status` is `quiet`.
- [ ] On `nw idea show`, including `--json`, and on `nw next`: only the
      private side moved names `nw idea push <id>`; only the public side
      moved names `nw idea pull <id>`; both sides differ from `:REQ_BASE:`
      prints the private headline, the private Summary, the file title, and
      the file summary, then the line
      `choose: nw idea push <id>  or  nw idea pull <id>` (plain and JSON
      `nw next` include both texts); neither side moved adds no push, pull, or
      diverged lines and leaves the usual next hint. A missing ref or file
      prints `REQ-<id> is not on nw-backlog`. `format_idea_show` prints
      `req: REQ-…` when `:REQ:` is set. `next_step_for_idea` does not call
      `link_status`. Show and `nw next` do not write the org file, do not
      create the ref, and do not run `git fetch` or `git push`.
- [ ] `nw ideas`, `nw hub`, and the TUI call `idea_show_payload` and
      `next_step_for_idea` with the branch unread, including when `:REQ:` is
      set. A linked idea whose file summary changed still has today's next
      hint from `nw ideas --json` and the hub payload, and a git spy sees no
      read of `refs/heads/nw-backlog`. `nw next` names `nw idea pull <id>`.
- [ ] Changing the file between `open` and `closed` leaves the idea's TODO
      state unchanged. `nw idea close --as drop` leaves the file's `status`
      unchanged and leaves `:REQ:` set. An idea with no `:REQ:` gains none
      from show, next, or load.
- [ ] If the org write fails after publish's add, the org bytes are unchanged,
      the command exits 1, and compare-and-swap restores the recorded tip, or
      deletes the ref when it was absent. Stderr is
      `REQ-<id> was not linked; restored refs/heads/nw-backlog`. When the ref
      no longer points at that new commit, stderr is
      `REQ-<id> was not linked; refs/heads/nw-backlog no longer points at <new>; left unchanged`
      and the ref is left as it is.
- [ ] If the org write fails after push's file edit, the command exits 1 and
      compare-and-swap restores the recorded tip, or deletes the ref when it
      was absent. Stderr is
      `REQ-<id> was not updated; restored refs/heads/nw-backlog`. When the ref
      no longer points at that new commit, stderr is
      `REQ-<id> was not updated; refs/heads/nw-backlog no longer points at <new>; left unchanged`
      and the ref stays. A failed pull leaves the branch tip unchanged.
- [ ] A heading lookup with no level matches only `task.line`. When
      `task.level` is absent or None, `_is_same_headline` returns true only
      for `idx == task.line - 1`, and only when that line is a headline and,
      if `task.heading` is set, the heading text still matches, and it returns
      false for every other index, so `_locate_headline` does not raise
      `N matches`. There is no `_idea_summary_text_from_file`.
      `tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections`
      passes: after a log append creates the properties drawer, Summary still
      contains `keep me`, Open questions still contain `keep this question`,
      and the log still contains `keep this finding` and the new finding.

## Test plan

- **Automated tests:** `tests/test_idea_link.py`. The fixture is a temporary
  git repo with `user.name` and `user.email` set and a config whose
  `outbox_targets[<project>].repo_path` is that repo, plus a temporary org
  ideas file. A second temporary repo is the current directory when a test
  checks that cwd is ignored. Meta-Tools cases set `nw_checkout` to a
  temporary checkout-shaped directory and also set
  `outbox_targets` for Meta-Tools to a different repo. Request bytes are read
  with `git show refs/heads/nw-backlog:<path>`. CliRunner covers exit codes.
  `test_req_base_known_vectors` hard-codes the digests in the Design table.
  A git spy records which refs a command reads.
  - `test_req_base_known_vectors` — the three payloads, including whitespace,
    a trailing newline, CR LF, and an empty summary.
  - `test_publish_uses_the_project_repo_not_cwd` — Meta-Tools uses
    `nw_checkout` even when `outbox_targets` also has a Meta-Tools
    `repo_path`, another project uses `project_root`
    (`outbox_targets[project].repo_path`), cwd is a different repo, symbolic
    `HEAD` is unchanged, and the command runs no `git checkout`, `git switch`,
    `git add`, `git fetch`, or `git push`.
  - `test_publish_refuses_without_a_repo` — blank project, missing
    `:PROJECT:`, unknown project, empty `repo_path`, unset `user.name`, unset
    `user.email`, and the checked-out refusal: publish and push while
    `refs/heads/nw-backlog` is checked out exit 1 with stderr
    `refusing to update nw-backlog while it is checked out` and add no commit.
  - `test_publish_links_without_changing_state_or_sections` — the INCUBATE
    publish criterion, including the pinned digest, the absence of the idea
    id, the `---` fences, a double-quoted title, `Name <email>`, the message
    `publish REQ-<id>`, and author and committer from that repo's `user.name`
    and `user.email`.
  - `test_second_publish_preserves_existing_requests` — a second publish of
    another idea leaves the first file's blob sha unchanged; the new commit's
    parent is the previous tip; every other path stays byte-for-byte.
  - `test_publish_refuses_empty_title_multiline_title_and_second_link`.
  - `test_request_file_layout_and_invalid_file` — key order, double-quoted
    `Link: widgets` round-trips through pull, empty body, and a file that
    does not parse exits 1 with `invalid request file` and writes nothing.
  - `test_adopt_creates_a_local_idea` — including a `closed` file, a file
    that already has `spec`, and a title `Link  two` with summary
    `say **bold**` stored unchanged.
  - `test_adopt_refuses_duplicate_unknown_and_missing_project`.
  - `test_push_writes_title_and_summary_and_leaves_status_and_spec` — copies
    every other front-matter key, including a key that is not `status` or
    `spec`, and the commit message is `push REQ-<id>`.
  - `test_pull_writes_headline_and_summary_and_leaves_log_and_questions` — a
    title `Link  two` and a summary `say **bold**` are unchanged on the idea
    and in the file, and the next show is quiet. A failed pull leaves the
    branch tip unchanged.
  - `test_session_triage_tag_round_trips_on_pull` — for both
    `* INCUBATE  Link :session-triage:` and
    `* INCUBATE [#A] Link :session-triage:`, a no-op pull asserts that exact
    headline, and a pull whose file title is `Widgets` asserts
    `* INCUBATE  Widgets :session-triage:` or
    `* INCUBATE [#A] Widgets :session-triage:`. A second `INCUBATE`, or the
    old title `Link` after the Widgets pull, fails the test. An assertion
    that only checks that `:session-triage:` occurs on the line is not this
    test. The public blob's front matter does not contain `:session-triage:`.
    Log, questions, and `CLOCK:` lines are unchanged. `req_base` of the
    private headline and Summary equals `req_base` of the file pair and
    equals `:REQ_BASE:`, and `link_status` is `quiet`.
  - `test_show_and_next_classify_drift_and_do_not_write` — private-only,
    public-only, both moved (both texts and
    `choose: nw idea push <id>  or  nw idea pull <id>` on plain and JSON
    `nw next`), quiet, and a missing file (`REQ-<id> is not on nw-backlog`).
  - `test_ideas_hub_and_tui_do_not_read_the_branch` — a linked idea whose file
    summary changed still has today's next hint from `nw ideas --json` and
    the hub payload, and a git spy sees no read of `refs/heads/nw-backlog`;
    `nw next` names `nw idea pull <id>`.
  - `test_file_status_and_drop_do_not_cross` — `open`/`closed` versus TODO
    state, and drop versus file `status`.
  - `test_publish_org_write_failure_restores_the_old_tip` — both the restored
    case and the lost compare-and-swap case.
  - `test_push_org_write_failure_restores_the_old_tip` — stderr
    `REQ-<id> was not updated; restored refs/heads/nw-backlog`, and stderr
    `REQ-<id> was not updated; refs/heads/nw-backlog no longer points at <new>; left unchanged`
    with the ref left as it is.
  - `test_heading_without_level_matches_only_task_line` — when `task.level`
    is absent or None, `_is_same_headline` is true only for
    `idx == task.line - 1` when that line is the matching headline, and false
    for every other index, so `_locate_headline` does not raise `N matches`.
- **Regression guard:**
  `tests/test_idea_stamps.py::test_stamp_creates_missing_drawer_without_corrupting_sections`
  remains the guard, and the other tests in `tests/test_idea_stamps.py`. An
  idea with no `:REQ:` still gets today's `workflow.next_step_for_idea` hint
  (`tests/test_workflow.py`).
  `uv run pytest tests/test_idea_link.py::test_session_triage_tag_round_trips_on_pull tests/test_idea_link.py::test_pull_writes_headline_and_summary_and_leaves_log_and_questions -q`
  is the pull-headline check.
- **Manual verification:** in a temporary repo, `nw idea publish IDEA-NNN`,
  then `nw idea show IDEA-NNN` prints a quiet link and `req: REQ-…`. Edit the
  Summary and run `nw next`; the hint is `nw idea push IDEA-NNN` and the file
  is unchanged until that command is run. Change only the public summary;
  `nw ideas --json` keeps the usual hint, and `nw next` names
  `nw idea pull IDEA-NNN`. Publish a second idea; the first file's blob sha
  is unchanged. On an idea whose headline is
  `* INCUBATE  Link :session-triage:`, change the public title to `Widgets`
  and run `nw idea pull`; the headline is exactly
  `* INCUBATE  Widgets :session-triage:` and the next show is quiet.

## Rollout / migration

No backfill. Ideas that already exist gain `:REQ:` only when someone runs
`nw idea publish` or `nw idea adopt`. The first publish creates
`refs/heads/nw-backlog` when the ref is absent. The product checkout is left
on its current branch. Publish and push refuse when that branch is the
checked-out `HEAD`.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; the tests named above pass. The full suite stays on
      the queue's close gate.
- [ ] No regressions in `tests/test_idea_stamps.py` and `tests/test_workflow.py`.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated and `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
