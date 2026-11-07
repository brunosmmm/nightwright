---
id: SPEC-0458
title: Periodic deterministic resolution of queue dependency conflicts
status: proposed
owner: user
created: 2026-11-07
milestone: "M9: agentic workflow"
kind: feature
tags: [agentic, queue]
source_idea: IDEA-1024
---

## Context

[SPEC-0423](./0423-idea-campaign-ordering-with-after-and-epic.md) is the wait. `queue.after_met` (`src/nightwright/agentic/queue.py`) turns a campaign's `after` list into `blocked_by`, and `quiet.dependency_wait` (`src/nightwright/agentic/quiet.py`) holds a backlog line only when every entry starts with `waiting on ` and every id in it is a live campaign's spec id, campaign id, or idea id. Any other text wakes the conductor with the raw string. A plain `backlog` event is held more loosely (`backlog_resolved` only checks the `waiting on ` prefix); `backlog_blocked_long` and the conductor ping use `dependency_wait`.

That wake is wrong for four conflicts seen on 2026-11-07. Each has one outcome.

- An idea ref is treated as met when the main checkout's copy of the prerequisite campaign's `spec_id` is `done`. A reused number trips this. IDEA-967's live harness campaign `IDEA-967-20261107-2a79` is REVIEWING with `spec_id` SPEC-0446, while main's `docs/specs/0446-sdk-model-effort-catalog.md` is a different spec already marked `done`. `after_met` releases the wait while that campaign is still running.
- The prerequisite spec is not on main, but a live campaign carries the id. IDEA-888 waits on IDEA-887, whose campaign carries SPEC-0407, a file that exists only in that campaign's worktree. `after_met` reports `SPEC-0407 not found`.
- The prerequisite idea has no campaign and no `:SPEC:`. IDEA-889 waits on IDEA-1015. `after_met` returns `waiting on IDEA-1015 (after: no campaign queued for IDEA-1015)`. IDEA-1015 is not a live id, so the ping and `backlog_blocked_long` wake the conductor with that string. There is nothing to wait on, and nothing to queue.
- A QUEUED campaign already has its branch checked out at `repo.worktree`. Admission calls `git.worktree_add` (`gitops.worktree_add`), which refuses an existing path or branch. IDEA-887, IDEA-888, and IDEA-889 were re-queued onto worktrees that already exist. IDEA-887's spec file is still `proposed`. IDEA-888 and IDEA-889 have `accepted` spec files (the accept commit is the last commit on each branch). A fresh open would also send IDEA-887 to IMPLEMENTING, because `spec.id` is set and `has_no_spec` is false.

## Goals / Non-goals

**Goals**
- Keep a wait unmet, with text `waiting on <campaign-id>`, while the prerequisite idea's own campaign is still live, including when main's file for that campaign's `spec_id` is already `done`.
- When the spec file is missing and a live campaign carries that id, wait on that campaign. Do not report `not found`.
- When the prerequisite idea has no campaign and no spec, mail the conductor once, naming the waiter, the idea, and that it has no campaign. Do not queue the idea. Do not keep waking on the raw string.
- When a QUEUED campaign's branch is already checked out at its planned worktree, resume into that worktree at the state the worktree's spec supports. Do not call `worktree_add`.

**Non-goals**
- Replacing SPEC-0423. `after` storage, `nw queue after`, epic `:AFTER:` seeding, and the spec writer's must-include `depends_on` stay as they are.
- Holding a spec ref (`--after SPEC-…`) unmet because some other campaign reused a number whose file on main is `done`. A done file still satisfies a spec ref.
- Changing the wait text for a live idea campaign that has no `spec_id` yet (`waiting on <idea> (after: campaign <id> is <state>)`).
- Queueing the missing idea, removing the `after` entry, deleting a worktree or branch, or resetting a branch onto main.
- Merge open (`_do_open_merge`), which already adopts a branch's checkout. Split and red-main opens, which keep today's `worktree_add` behavior.
- A new timer or config knob. The queue tick is the period.

## Decision

1. **SPEC-0423 stays the wait.** `after_met` still decides met or unmet. This spec corrects two of its answers, and adds the two actions `after_met` cannot take.
2. **A live idea campaign wins over the file on main.** For an idea ref, if the idea's newest campaign is non-terminal and `_spec_after_met` would report that campaign's `spec_id` met from the main checkout's file, the ref stays unmet. The `blocked_by` entry is `waiting on <campaign-id> (after: campaign <campaign-id> is <state>)`. Quiet already holds that shape. A spec ref whose file is `done` stays met.
3. **A missing file waits on the campaign that carries the id.** When the spec file is not on main and the newest campaign with that `spec_id` is non-terminal, both idea refs and spec refs use the same `waiting on <campaign-id> (after: campaign <campaign-id> is <state>)` text. They do not say `not found`. With no such campaign, today's `not found` and `abandoned` texts stay, and they still wake the conductor.
4. **Nothing to wait on is one mail.** Once per tick, before admission, `resolve_dependency_conflicts` mails the conductor one request for each QUEUED waiter whose unmet idea ref is `waiting on <idea> (after: no campaign queued for <idea>)`. The queue does not queue that idea. After the send, readiness rewrites that entry so the ping and `backlog_blocked_long` hold it. A second pass does not send again.
5. **An existing checkout is resumed, not re-opened.** At open, if `worktree_for_branch` is already the planned path, skip `worktree_add` and skip setup when `base_sha_at_open` is already set. Move to SPEC_WRITING when the worktree spec is missing, `draft`, or `proposed`, and to IMPLEMENTING when it is `accepted` or `in-progress`. That is why IDEA-887 resumes to SPEC_WRITING and IDEA-888 and IDEA-889 resume to IMPLEMENTING. A later campaign uses the same rule on its own worktree.

## Design

### Wait text (`queue.after_met`)

`_spec_after_met` is unchanged for its callers' met/unmet result, except that `after_met` does not take two of those results at face value.

Idea ref, newest campaign non-terminal (not `MERGED`, `HANDED_OFF`, or `ABANDONED`), and that campaign has a `spec_id`:

- `_spec_after_met` reports met from the file on main: return unmet, `waiting on <that campaign id> (after: campaign <id> is <state>)`. Do not return met.
- `_spec_after_met` reports `<spec_id> not found`, and this campaign is the newest campaign with that `spec_id` (or it is itself non-terminal and carries the id): same campaign-wait text. Do not return `waiting on <spec_id> (… not found)`.
- Any other unmet why (the file exists and is `accepted`, `in-progress`, `draft`, `proposed`, …) stays today's `waiting on <spec_id> (after: <idea> -> <why>)`.
- No `spec_id` on that campaign: today's `waiting on <idea> (after: campaign <id> is <state>)` stays.

Spec ref: when the why ends in `not found` and `list_campaigns(..., spec_id=)`'s newest hit is non-terminal, return the campaign-wait text for that hit. When the file is met, or the newest hit is `MERGED`, return met as today, even if an older campaign reused the id. When the newest hit is missing or terminal and the file is missing, keep `after <spec_id> not found`.

`list_campaigns` is newest first. "Newest" means that first hit, not an older non-terminal row behind a newer terminal one.

### One mail (`queue.resolve_dependency_conflicts`)

Called from `_tick` after the parked-campaign recovery steps and before the admission loop that calls `backlog_readiness` and `_record_backlog`. It scans QUEUED campaigns only.

For each unmet `after` entry whose `after_met` text is exactly `waiting on <idea> (after: no campaign queued for <idea>)`:

- That text is already the "idea exists, `:SPEC:` empty, no campaign" case. Do not queue the idea (`plan_add_idea` is not called). Do not remove the `after` entry.
- If `state["conflict_mail"]` (a list of strings, absent on older campaigns) does not contain the idea id, send one mailbox message and then append the idea id and write the state under the campaign lock. A failed send appends nothing, so the next tick retries.
- Message: `to=conductor`, `from=conductor`, `kind=request`, `campaign` the waiter id, text `{waiter} waits on {idea}. {idea} has no campaign and no spec. Do not queue {idea}.`

`_after_blocked` rewrites a no-campaign line to `mailed conductor: {idea} has no campaign ({waiter})` when the idea id is in `conflict_mail`. Any other `after_met` result is kept as-is, so a campaign that appears later replaces the mailed line with an ordinary wait on its own.

`quiet._BENIGN_BLOCKED` gains the prefix `mailed conductor: `. `dependency_wait` holds an entry with that prefix without requiring a live id. Other entries in the same list are judged as today. `backlog_resolved` holds it because the prefix is benign.

### Resume into an existing worktree (`_do_open_unwrapped`)

Before `git.worktree_add`, for a non-merge campaign: if `git.worktree_for_branch(root, repo.branch)` resolves to `Path(repo.worktree)`, adopt that checkout.

- Do not call `worktree_add` or `worktree_add_existing`. Do not reset the branch, commit, or remove the worktree.
- Keep a `base_sha_at_open` that is already set. If it is null, set it the way a fresh open does, and run `_run_worktree_setup`. If it is set, skip setup.
- Refresh `repo.head` from the worktree. Set `spec.sha256_at_open` only when it is null and the spec file exists.
- `worktree_resume_state(state, worktree)` picks the phase. The spec file is `spec.rel_path` inside the worktree when that path is set and the file exists.
  - File present, status `draft` or `proposed`: `SPEC_WRITING`.
  - File present, status `accepted` or `in-progress`: `IMPLEMENTING`.
  - File absent: the fresh-open target, `SPEC_WRITING` when `has_no_spec(state)` or `admission == "revise_pending"`, otherwise `IMPLEMENTING`.
  - File present with any other status (`done`, `rejected`, `superseded`, empty, unreadable): do not adopt into a phase. Set the open error to a string that includes that status, so today's `open_failed` park applies (`resume_to` QUEUED). Leave the worktree and branch. Mail once, deduped by `conflict_mail` containing `worktree:<status>`, text `{campaign} already has worktree {path} on {branch}, and its spec status is {status}. Not opened.` Same sender, recipient, and `kind=request` as the other mail.
- `_apply`'s `open` branch, for a successful adopt, finishes at `worktree_resume_state` with reason `resumed_worktree` and a ledger `note` `resumed into existing worktree → {state}` (author `queue`). The fresh-open target is not used when the spec file is present. A campaign with `spec.id` set and a `proposed` file goes to SPEC_WRITING, not IMPLEMENTING.
- Any other layout (path exists but is not that branch's checkout, or the branch is checked out elsewhere) is unchanged: `worktree_add` runs and parks `open_failed` as today. No mail.

Merge, split, and red-main opens do not use this adopt path.

## Alternatives considered

- **Keep waking the conductor with the raw `blocked_by` string for all four.** Lost: three of the four have one correct outcome, and the raw string is the wake this pass replaces.
- **Treat main's `done` file as met whenever the `spec_id` matches, including for an idea ref.** Lost: that is the reused-id release. SPEC-0446 on main is not the harness campaign's spec.
- **Apply that same hold to spec refs.** Lost: a waiter of the shipped SPEC-0446 would then wait on the harness campaign that reused the number.
- **Queue the missing idea automatically.** Lost: the outcome for IDEA-1015 is that there is nothing to wait on, and the queue must not queue it.
- **Admit by calling `worktree_add` at a new path and leaving the old worktree.** Lost: the branch already exists, and the spec commits already on it would be left behind. A fresh open would also put a `proposed` spec into IMPLEMENTING.

## Acceptance criteria

- [ ] AC1: An idea ref whose newest campaign is non-terminal and has a `spec_id` stays unmet when the main checkout's file for that id is `done`. `blocked_by` is exactly `waiting on <campaign-id> (after: campaign <campaign-id> is <state>)`. `dependency_wait` holds it. After that campaign is `MERGED`, the `done` file satisfies the ref.
- [ ] AC2: An idea ref and a spec ref whose file is absent stay unmet when the newest campaign with that `spec_id` is non-terminal. `blocked_by` is the same campaign-wait text and does not contain `not found`. `dependency_wait` holds it. With no non-terminal campaign carrying the id, a spec ref still blocks as `after <spec_id> not found`, which is not a `dependency_wait`.
- [ ] AC3: A spec ref whose file on main is `done` is met even when some other non-terminal campaign has that `spec_id`. An idea ref whose live campaign has no `spec_id` still blocks as `waiting on <idea> (after: campaign <id> is <state>)`.
- [ ] AC4: For a QUEUED waiter of an idea that has no campaign and no `:SPEC:`, the first `resolve_dependency_conflicts` sends one conductor request whose text is `{waiter} waits on {idea}. {idea} has no campaign and no spec. Do not queue {idea}.` A second call sends nothing. No campaign is created for the idea. The `after` entry remains. Readiness then reports `mailed conductor: {idea} has no campaign ({waiter})`, and both `dependency_wait` and a `backlog_blocked_long` classification hold that entry. The pre-mail text `waiting on {idea} (after: no campaign queued for {idea})` still does not.
- [ ] AC5: `queue.tick` runs the pass before it records backlog readiness, so the same tick that sends the mail records the mailed `blocked_by` text.
- [ ] AC6: Opening a non-merge campaign whose branch is already checked out at `repo.worktree` does not call `worktree_add`, does not change the branch HEAD, and does not run setup when `base_sha_at_open` is set. A worktree spec of status `proposed` or `draft` finishes in SPEC_WRITING, including when `spec.id` is set. Status `accepted` or `in-progress` finishes in IMPLEMENTING. The transition reason is `resumed_worktree`. A campaign with no such checkout still calls `worktree_add` and uses today's fresh-open target.
- [ ] AC7: A worktree spec whose status is `done` does not move the campaign to SPEC_WRITING or IMPLEMENTING. The open parks `open_failed` with that status in the detail, the worktree and branch remain, and one conductor mail names the campaign, the path, and the status. A second open attempt does not send a second mail.

## Test plan

- **Automated tests**
  - Extend `tests/test_idea_ordering.py`. AC1: idea campaign with `spec_id` pointing at a fixture spec flipped to `done`, state REVIEWING; a second idea `--after` the first; assert the exact campaign-wait `blocked_by`, `QT.dependency_wait` true, then `_transition` to MERGED and assert readiness `(True, [])`. AC2: set `spec_id` to an id with no file in the fixture `docs/specs` (SPEC-0407), state QUEUED; idea ref and spec ref both get the campaign-wait text and not `not found`; abandon that campaign and assert the spec ref is `after SPEC-0407 not found` and not a `dependency_wait`. AC3: a spec ref to the `done` fixture spec is ready while another live campaign carries that `spec_id`; a live idea campaign with `spec_id` left null still produces `waiting on <idea> (after: campaign <id> is QUEUED)`, the assertion `test_after_idea_waits_then_is_ready_when_prerequisite_merges` already pins.
  - New `tests/test_queue_conflict_resolve.py`, using the `env` fixture from `tests/test_agentic_idea_campaign.py`. AC4: waiter `--after` an idea that was captured and then has no campaign (do not call `add_idea_campaign` for it) and no `:SPEC:`; call `resolve_dependency_conflicts` twice; assert one `mailbox.read_inbox(cfg, "conductor")` request with the exact text, `needs_reply` true, `campaign` the waiter; `list_campaigns` grows by none for that idea; `conflict_mail` contains the idea id; `backlog_readiness` returns the `mailed conductor:` line; `QT.dependency_wait` holds it and `QT.classify` holds a `backlog_blocked_long` event of it; the same classify on the pre-mail text wakes. AC5: run `queue.tick` once on that fixture and assert the mail and the recorded `state["backlog"]["blocked_by"]` in the same tick. AC6: `git.worktree_add` the campaign's own planned path and branch, write `docs/specs/` files with status `proposed` and `accepted` (two campaigns), set `spec.rel_path` and `spec.id`, set `base_sha_at_open`, transition to OPENING, `_do_open` then `_apply`; monkeypatch `git.worktree_add` and `_run_worktree_setup` to fail the test if called; assert SPEC_WRITING vs IMPLEMENTING, reason `resumed_worktree`, and unchanged HEAD. A third campaign with no checkout asserts `worktree_add` is called and a spec-less idea finishes SPEC_WRITING. AC7: status `done` in the worktree; assert `open_failed`, detail contains `done`, worktree still present, one mail, second attempt no new mail.
- **Manual verification:** none. The tests build the four situations in a temp repo and a temp data dir. Do not run them against the live `NW_DATA_DIR` or the existing IDEA-887, IDEA-888, IDEA-889, or IDEA-967 worktrees.
- **Regression guard:** `tests/test_idea_ordering.py` and `tests/test_agentic_quiet.py` pass without weakening their current assertions. A campaign with no `after` is unchanged. `python3 tools/affected_tests.py --run uv run pytest -q` on the touched files, plus `python3 tools/spec_lint.py`.

## Clock Log

No implementation session is clocked here. Implementation clocks IDEA-1024 with `nw idea clock-in` and `nw idea clock-out`.

## Rollout / migration

1. Land the `after_met` corrections, the quiet prefix, the tick pass, and the open adoption together. They are one change.
2. No migration and no rewrite of existing campaign files. The next tick recomputes `blocked_by`. A waiter of an idea with no campaign gets one mail and a `conflict_mail` marker. A QUEUED campaign is adopted only when it is admitted, so one that is still blocked keeps its worktree untouched until then.
3. Confirm a campaign without `after`, and a fresh open whose path does not exist, follow the current open target.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None._
