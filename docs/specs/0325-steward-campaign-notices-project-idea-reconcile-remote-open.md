---
id: SPEC-0325
title: "Stewards get campaign notices; a second-project merge reconciles its idea; pr campaigns branch from the remote"
status: done
owner: user
created: 2026-10-31
updated: 2026-10-31
source_idea: IDEA-534
parent: SPEC-0298
milestone: "M10: agentic MVP"
tags: [agentic, steward, mailbox, reconcile, open, mvp]
---

## Context

Three gaps showed up on the comfort-assistant canary (campaign `IDEA-008-20261031-7de5`, the
first second-project PR campaign, SPEC-0298 E5):

1. **IDEA-534.** The human merged comfort-assistant PR #1 and `wt queue sweep-prs` moved the
   campaign to `MERGED`, but `steward:comfort-assistant` (armed, lag 0) heard nothing. The
   queue only mails a steward when the conductor asks it a question.
2. **IDEA-535.** `_reconcile_closed` (`src/wt/agentic/queue.py`) skips the idea reconcile
   with the finding "idea reconcile skipped: spec store is not this repo" whenever the
   campaign's repo is not wt's own spec store. IDEA-008's spec (comfort-assistant
   `docs/specs/0001-…`, `done` on `origin/main`) shipped, but the idea was never closed.
3. **Remote base (human decision 2026-10-31).** `_do_open_unwrapped` branched every campaign
   from the *local* `base` (`git.rev_parse(root, base)`), while the close step of a `pr` /
   `draft-pr` project fetches and rebases onto `<remote>/<base>`
   (`close.prepare_close`). In a second project the human's local `main` can be far behind
   (comfort-assistant's was 6 commits behind), so the implementer worked on a stale tree.

## Goals / Non-goals

**Goals**
- A steward with an armed mailbox follower gets one `info` message (no reply wanted) for
  each `HANDED_OFF`, `MERGED`, `PARKED` and `ABANDONED` transition of its project's campaigns.
- A merged second-project campaign advances its idea from the spec as merged in the project's
  own repo, without writing any spec file.
- `pr` / `draft-pr` campaigns start from the freshly fetched `<remote>/<base>`.

**Non-goals**
- Changing the conductor's wakeups (the steward is mailed, not the conductor).
- Telling the human that a local checkout is behind its remote (dropped by the human,
  2026-10-31: no local-vs-origin drift is computed or mentioned).
- Fixing IDEA-533 (bare `SPEC-NNNN` ids resolved against wt's spec store elsewhere, e.g. the
  global `reconcile_ideas` walk). This spec only avoids that collision on the close path.
- Changing the `merge` policy's open (wt itself keeps branching from its local base).

## Decision

1. `_transition` calls `_notify_steward` after every state change into
   `STEWARD_NOTIFY_STATES = {HANDED_OFF, MERGED, PARKED, ABANDONED}`. It mails
   `steward:<campaign project>` only when `events.reader_holder(cfg, "mailbox:steward:<P>")`
   reports a live follower. Idempotent per transition: the key `<to>#<transition ledger seq>`
   is stored in `state["steward_notified"]` (last 20 kept). A failed send is a ledger `note`;
   the transition stands.
2. `_reconcile_closed`, for a campaign whose repo root is known, whose repo is not wt's spec
   store, and which has a `spec.rel_path`, calls `_reconcile_project_spec`. That function reads
   the spec from git and advances the idea. A `done` spec gives `SHIPPED`.
3. `_do_open_unwrapped` starts the branch from `_open_start(root, pcfg, base)`. For the `merge`
   policy that is the local `base`. Otherwise it runs `git fetch <merge.remote|origin> <base>`
   and returns `<remote>/<base>`. A failed fetch raises, and the open parks `open_failed`.

## Design

**Steward notices** (`src/wt/agentic/queue.py`: `STEWARD_NOTIFY_STATES`,
`_steward_notice_text`, `_notify_steward`). The call runs under the campaign lock inside
`_transition`, after `write_state` and the terminal bookkeeping, and only when `to != frm`.
A `PARKED → PARKED` re-park therefore mails nothing. The follower check comes first and is
cheap (one `flock` probe). Only then is the address validated with
`mailbox.validate_address`, and an unknown project silently has no steward. The sender is
`conductor`, which the queue already uses for its own mail (`escalations.withdraw`).
`validate_address` accepts no `queue` address, and adding one would make it a recipient too.
The text starts with `queue:` so the origin is clear. Steward-bound mail is not mirrored into
`events.jsonl` (only conductor-bound mail is), so conductor wakeups are unchanged. The text
reads `queue: campaign <id> (<spec>) is <STATE> (<reason>); PR <url>; merged <sha10>;
detail: <park detail ≤200 chars>; cost $X.XX`, and each optional part appears only when
present. `skills/wt-steward/SKILL.md` step 4 gains one line: report a campaign `info` notice to
the human in one line, and send no reply.

*Idempotency choice:* the key is per transition (ledger `seq`), not per (campaign, state). A
campaign that parks, resumes and parks again is mailed for each park, because each is a new
fact for the steward. A re-run of the same transition (or a second sweep) never mails twice:
the sweep only moves a `HANDED_OFF` campaign once, and `_notify_steward` checks the marker.

**Project-spec reconcile** (`_project_spec_fm`, `_reconcile_project_spec`).
- The frontmatter comes from `git -C <root> show <ref>:<spec.rel_path>`, never from the
  working tree and never after a fetch. The refs are tried in order: `close.merged_sha`, then
  `origin/<base>`, then `repo.head` (the campaign branch head, which is what the PR merged; a
  local clone usually has not fetched a GitHub merge commit yet).
- The idea is the spec's `source_idea`, else `state.idea_link.id`, else `state.idea.id`. It
  is picked with the SPEC-0267 identity rule (`specs._pick_idea`). An idea whose `:SPEC:`
  names another spec is a `mismatch` finding and gets no write.
- Status `done` advances the idea to `SHIPPED` from a pre-terminal state or from `PROMOTED`.
  PROMOTED is what the IDEA-533 collision leaves behind: wt's own `SPEC-0001` is done.
  `SHIPPED` was chosen over `PROMOTED` because the work shipped into another repo, the same
  meaning SPEC-0135 gives outbound `done`.
- Status `accepted` / `in-progress` gives `SPECCED` (from IDEA/INCUBATE).
  `superseded` / `rejected` drops a pre-terminal idea. A status other than `done` is
  also a minor finding.
- The write sets `:SPEC:` when empty, clears `:HANDED_OFF:` before an archiving state, adds
  the keyword with `ensure_idea_keyword`, then calls `set_state`. Each step re-resolves the
  idea, pinned by `(file, heading)` when its id is duplicated. An `idea_reconciled` event
  records the move.
- `HANDED_OFF` gets an info finding ("waits for the merge"), and the sweep reconciles at
  `MERGED`. Errors propagate to `_reconcile_closed`'s existing guard, where they become a
  minor `bookkeeping` finding.
- The old "spec store is not this repo" info finding remains for an unknown repo root or an
  unlocatable wt spec store.

**Remote open** (`_open_start`). The fetch error is re-raised as a `GitError` whose tail
reads `cannot fetch <remote>/<base> for a <policy> campaign (the branch must start from the
remote, not the local <base>): …`. The existing `open` error path parks it `open_failed`
(resume to QUEUED) before any worktree exists. `git worktree add -b <branch> <path> <sha>`
uses the resolved sha, not the remote ref, so the campaign branch gets no upstream. The
human's local `base` is never read for a pr project and never modified. `base_sha_at_open`
is the remote tip.

## Alternatives considered

- **Generalise `specs.reconcile_ideas` with a frontmatter override.** It walks every spec
  claim in wt's store and resolves bare ids there, which is the IDEA-533 collision. A scoped
  function over one campaign is smaller and cannot touch wt's store.
- **Add a `queue` mailbox address.** It would also become a valid recipient and a TUI
  option. The `conductor` sender has precedent.
- **Fall back to the local base when the fetch fails.** Rejected by the human: a silent
  stale base is the bug being fixed.

## Acceptance criteria

- [x] A steward with an armed follower receives exactly one `info`, `needs_reply: false`
      message on `HANDED_OFF` and one on `MERGED` (by the sweep), and nothing on a second
      sweep or a re-run of the same transition.
- [x] `PARKED` (with the park detail) and `ABANDONED` are mailed. A `PARKED → PARKED`
      re-park and non-listed states are not mailed.
- [x] No follower: no message and no marker.
- [x] A send failure leaves the transition in place and writes a ledger `note`.
- [x] Steward notices add no `message` event to the conductor's stream, and the text never
      mentions pulling or drift.
- [x] A second-project PR campaign whose merged spec is `done` advances its idea (SPECCED
      or PROMOTED) to `SHIPPED` after the sweep and writes `idea_reconciled`. No file in wt's
      spec dir or in the project's working tree changes, even when wt has its own
      `SPEC-0001`.
- [x] A merged sha missing locally falls back to the branch head. `HANDED_OFF` waits with an
      info finding.
- [x] A `pr` / `draft-pr` campaign's worktree and `base_sha_at_open` start at the remote tip
      when the remote is ahead of local `main`. Local `main` is unchanged and the branch has
      no upstream.
- [x] A `merge`-policy campaign still starts at local `main`.
- [x] A failing fetch for a `pr` campaign parks `open_failed` with the "cannot fetch" detail
      and creates no worktree.

## Test plan

- **Automated tests:** `tests/test_agentic_steward_notify.py`, offline and in tmp dirs only.
  An armed follower is a `events.reader_lock` held in the test, and git remotes are temp bare
  repos.
  - `test_armed_steward_gets_one_info_per_transition`, `test_no_follower_no_message`,
    `test_park_and_abandon_notify_reparks_do_not`, `test_other_states_send_nothing`,
    `test_send_failure_keeps_the_transition`.
  - `test_second_project_merge_ships_its_idea[SPECCED|PROMOTED]`,
    `test_second_project_merged_sha_not_local_falls_back_to_head`,
    `test_second_project_hand_off_waits`.
  - `test_pr_policy_opens_from_the_remote_tip[pr|draft-pr]`,
    `test_merge_policy_still_opens_from_local_base`,
    `test_pr_policy_fetch_failure_parks_open_failed`.
  - `tests/test_agentic_queue.py::test_merge_reconcile_skipped_for_another_repo` now expects
    the project path. Its non-git "other repo" yields a minor "spec not readable" finding, and
    the idea is unchanged.
- **Manual verification:** none needed beyond the suite; the behaviour is exercised end to
  end through `sweep_handed_off` and `wt queue tick`.
- **Regression guard:** the full `uv run pytest -q -n 4` suite (minus the two known load
  flakes IDEA-511/512).

## Rollout / migration

None. Campaigns already `MERGED` (IDEA-008) are not re-reconciled automatically. Their ideas
can be moved with `wt idea state`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`).
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped

Shipped directly on `main` at the human's request (2026-10-31), with no queue campaign. The
"local checkout is behind origin, pull" line from IDEA-534's original design was dropped by
the human's amendment, and the remote-base open (fix 3) was added to this spec at the same
time. IDEA-533 is not fixed. The close path no longer resolves a second project's bare spec
id against wt's store, but the global `reconcile_ideas` walk still can.

## Open questions

(none)
