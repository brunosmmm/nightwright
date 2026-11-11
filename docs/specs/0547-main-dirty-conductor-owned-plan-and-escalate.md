---
id: SPEC-0547
title: main_dirty is conductor-owned; the skill row plans and escalates
status: accepted
owner: user
created: 2026-11-11
updated: 2026-11-11
kind: feature
tags: [agentic, conductor, queue]
depends_on: [SPEC-0546]
supersedes: [SPEC-0542]
source_idea: IDEA-1159
---

## Context

[SPEC-0222](./0222-queue-watch-needs-you-vs-conductor-handling.md) makes `main_dirty`
human-owned (`PARK_OWNERS["main_dirty"] = "human"`), so it is never overdue, `park_undecided`
never fires, the conductor ping fingerprint skips it, and herdr stays silent. On the night of
IDEA-1159 one stray tracked edit sat in **Needs you** and blocked every close: zero deliveries.

This is the second of six specs that replace [SPEC-0537](./0537-conductor-must-clear-main-dirty-itself-a-stray.md)
(human chose "split / narrow" at ESC-0067) and split
[SPEC-0542](./0542-main-dirty-plan-and-escalate.md) per its spec review. The chain, in queue
order: [SPEC-0546](./0546-main-dirty-plan-classifier.md) (classifier), **SPEC-0547 (this
spec)**, [SPEC-0548](./0548-main-dirty-apply-core-commit.md) (verb core),
[SPEC-0549](./0549-main-dirty-apply-wip-save-and-hold.md) (verb WIP save),
[SPEC-0550](./0550-conductor-git-guard-hook.md) (git-guard hook),
[SPEC-0551](./0551-conductor-runs-main-dirty-apply-trivial-only.md) (conductor trivial-only).

With SPEC-0546's `nw queue main-dirty-plan` in place, this spec flips the owner so the
conductor is woken, and gives it a row that plans and escalates once with the exact commands.
No git write by anyone is added here.

**Carries SPEC-0542 AC1, AC2, AC3, AC4, AC13, AC14, AC15.** It also folds in the SPEC-0542
review's row findings: the plan is run with the campaign's base, a plan that exits non-zero
still leads to a recorded decision, the open-escalation check matches a `root:` fact (not a
substring of the question), facts are single-quoted, and AC5 pins literal phrases.

## Decision

Drop `main_dirty` from `queue.PARK_OWNERS`. The conductor's new `parked.main_dirty` row runs
`nw queue main-dirty-plan` and either resumes (`clean` on the base) or escalates
`human_checkout` with no campaign, once per root, then resumes on the wake after the checkout
is clean. A plan failure is escalated the same way.

## Design

### Ownership and wake

`PARK_OWNERS` keeps `main_not_on_base`, `human_decision`, `restart`; `park_owner` defaults to
`conductor`. Existing paths pick the owner up with no new branch: `quiet.ping_fingerprint`,
`quiet.classify` (a runner-emitted `main_dirty` park is `wake`), `herdr_watch.notification_for`,
`dashboard_payload` (Conductor handling), `mvp_report.e5`. Delete the `main_dirty` entry of
`dashboard/inbox.py` `HOST_FIX`; keep `main_not_on_base`.

### Skill (`skills/nw-conduct/SKILL.md`)

The combined `parked.main_dirty, parked.main_not_on_base` row is split. The
`parked.main_not_on_base` row keeps today's text for that reason only. The `wt_broken` row
stops saying "same as `main_dirty`" and spells out its current move procedure. The "Clear
blockers yourself" bullet names `parked.main_dirty` as the exception for a dirty main checkout
(plan and escalate, never clear).

The `escalation_answered` row gains one sentence at its start: "A record with
`topic: human_checkout` and no campaign (read it with `nw escalations show <id>`) is a
`main_dirty` answer: run `parked.main_dirty` step 5, nothing else." Its campaign-based steps do
not apply to such a record, because the event `data` carries no topic and no campaign.

The new `parked.main_dirty` row, triggered by `parked` `main_dirty`, and by a
`conductor_ping` / `park_undecided` / `escalation_answered` that concerns an undecided
`main_dirty` park or its escalation:

1. For each `main_dirty` campaign read `repo.root` and `repo.base` from
   `nw campaign show <id> --json`; group by (`root`, `base`). Per group run
   `nw queue main-dirty-plan --root <root> --base <base> --json` in the conductor's own context
   (read-only, no sub-agent). **If it exits non-zero**, escalate once per root (step 3's check
   first) with `--question "Cannot plan the main checkout <root>: <first stderr line>"`,
   options `I fixed it` / `I cleared it another way`, `--fact 'root: <root>'`, then step 4's
   decisions; the answer re-runs step 1.
2. `action: resume` (clean and on the base): for each non-terminal `main_dirty` campaign of that
   group, `nw queue resume <id> --to CLOSING --by conductor --decision "main checkout clean"
   --rule parked.main_dirty --yes`. Done.
3. Otherwise, if `nw escalations list --json` already has an open `human_checkout` record whose
   facts contain exactly `root: <root>`, do not escalate again; record
   `nw queue decision <id> --kind wait --rule parked.main_dirty "waiting on ESC-NNNN"` once per
   campaign that has no decision after its park.
4. Else escalate once, no campaign: `nw queue escalate --topic human_checkout --question
   "<question>" --option "I ran the recommended commands" --option "I cleared it another way"
   --recommend 1 --fact 'root: <root>' --fact 'class: <class>, action: <action>' --fact
   '<command>'` (one `--fact` per `recommended` line, one per path up to 10) `--yes`. Every
   `--fact` value is a single-quoted shell string with each `'` written as `'\''`; a path that
   contains `$`, a backtick, `"` or a newline is not listed, and one fact
   `<n> paths (see nw queue main-dirty-plan)` stands for all such paths. `<question>` is "Main
   checkout <root> has <class> tracked edits blocking closes: run the commands below in order,
   stop at the first error, then answer"; for `class: clean` with `action: hold` it is "Main
   checkout <root> is on <branch>, not <base>: run the command below, then answer". For
   `merge` the first option is `I will finish the <in_progress> in the main checkout`. Then
   `nw queue decision <id> --kind escalate --rule parked.main_dirty "escalated ESC-NNNN"` for
   each `main_dirty` campaign of that root, so the park counts as decided and `park_undecided`
   does not repeat.
5. On `escalation_answered` for that record, run step 1 again: `resume` → step 2; still dirty →
   step 4 with an extra `--fact 'still dirty after ESC-NNNN'`.

The row runs no git command other than through `main-dirty-plan`, dispatches no sub-agent, and
never names `main-dirty-apply` (SPEC-0551 later adds the `--trivial-only` step as `2b`).

**What wakes a decided park.** Once the campaigns carry the `escalate` decision they leave the
ping fingerprint and `park_undecided` does not fire again; `main_dirty` has no auto-resume. The
only wake is the answer to the escalation (step 5). A checkout the human cleans without
answering leaves the campaigns parked until the human answers ESC-NNNN or runs
`nw queue resume`; the question says "then answer" for that reason. This is accepted: the
human-owned record is in Needs you and never times out, so the block is visible, unlike the
silent park this spec removes.

### Files the implementer may cherry-pick from `nw/IDEA-1159-20261110-02ed`

An optional source (tip `61876af9`), never the contract:

- `src/nightwright/agentic/queue.py` (`PARK_OWNERS`), `src/nightwright/agentic/dashboard/inbox.py`
  (`HOST_FIX`).
- `docs/product/loop/operate.md`, `docs/product/capabilities/agentic.md`: reword to "the
  conductor classifies and escalates"; name `main-dirty-plan` only.
- Test updates in `tests/test_agentic_queue.py`, `test_agentic_events.py`,
  `test_agentic_dashboard.py`, `test_agentic_queue_cli.py`, `test_tui_queue_model.py`,
  `test_dashboard_cli.py`, `test_dashboard_inbox.py`, `test_dashboard_page.py`,
  `test_dashboard_snapshot.py`, `test_herdr.py`, `test_agentic_mvp_report.py`,
  `test_skills.py`, `test_rich_output_guard.py`.
- Not `launch.py`, not `test_agentic_launch.py`, not the branch's `SKILL.md` text.

## Alternatives considered

- **Leave `main_dirty` human-owned and only rewrite the skill row.** The fingerprint and
  `park_undecided` would still ignore a missed park, which is how the checkout sat overnight.
- **Mail the steward instead of escalating.** The edit is in the human's own checkout, which
  the skill's human-decision route reserves for the human (`human_checkout` never times out).
- **Match the open escalation by its question text.** Root `/home/u/proj` would match a
  question about `/home/u/proj2`; a dedicated `root:` fact matches exactly.

## Acceptance criteria

- [ ] AC1 (SPEC-0542 AC1) `park_owner("main_dirty") == "conductor"`; `main_not_on_base`,
      `human_decision`, `restart` stay `human`.
- [ ] AC2 (SPEC-0542 AC2) An undecided `main_dirty` park older than `park_decide_s` is overdue,
      is in Conductor handling and not in Needs you, and `events.sync` emits exactly one
      `park_undecided` for it; a decision note after the park clears the overdue flag and no
      second `park_undecided` fires.
- [ ] AC3 (SPEC-0542 AC3) `quiet.ping_fingerprint` contains `["parked", <cid>]` for a real
      `main_dirty` park written by `finish_close` (not a hand-built dict); `quiet.classify`
      returns `wake` for it.
- [ ] AC4 (SPEC-0542 AC4) Herdr emits `nw: parked` for a `main_dirty` park; `HOST_FIX` has no
      `main_dirty` key; `mvp_report.e5` on a campaign that really parked `main_dirty` has no
      touchpoint for it, while a `main_not_on_base` park still is one.
- [ ] AC5 (SPEC-0542 AC13) Skill: the `parked.main_dirty` row (extracted by its rule id)
      contains the literal substrings `main-dirty-plan`, `--base <base>`,
      `nw campaign show <id> --json`, `Cannot plan the main checkout`, `human_checkout`,
      `--yes`, `--recommend 1`, `recommended`, `escalation_answered`, `--to CLOSING`,
      `--rule parked.main_dirty`, `stop at the first error`, `is on <branch>, not <base>`,
      `nw escalations list --json`, `root: <root>`, and `'\''`; it contains none of `git add`,
      `git commit`, `git checkout`, `git rm`, `git worktree add`, `main-dirty-apply`,
      `isolation`. The `escalation_answered` row names `human_checkout` and
      `parked.main_dirty`. The "Clear blockers yourself" bullet names `parked.main_dirty`. The
      `wt_broken` row does not contain `same as`; `parked.main_not_on_base` is its own row.
- [ ] AC6 (SPEC-0542 AC14) The test extracts the step-4 command from the row, substitutes sample
      values for the placeholders (a root, `class: branch`, two sample `recommended` lines, and a
      sample path containing a space and a `'`), drops `--yes`, runs it through `CliRunner` as
      `nw queue escalate … --json` against a temp cfg (via `shlex.split`), and asserts exit 0,
      `written: false`, `topic: human_checkout`, `campaign: null`, `recommendation: 1`, two
      options, a `root: <root>` fact, and one fact per sample line equal to the input byte for
      byte. The same test with the step-1 failure command asserts two options and the
      `Cannot plan` question.
- [ ] AC7 (SPEC-0542 AC15) `launch.py` and `tests/test_agentic_launch.py` are unchanged by this
      spec; `finish_close` still parks `main_dirty` with `resume_to: CLOSING` and still merges
      once the checkout is tracked-clean.

## Test plan

- **Automated tests:** the existing-module updates listed in Design (each module already has its
  tier marker); E5 uses a `main_dirty` park planted through `finish_close` (AC3, AC4). The
  skill-row tests (AC5, AC6) go in `tests/test_main_dirty_plan.py` (SPEC-0546's module), the
  branch's `test_skill_row_phrases` rewritten to AC5 (rows extracted by rule id; negative
  phrases).
- **Regression guard:** `tests/test_agentic_close.py`, `tests/test_agentic_mvp_integration.py`,
  `tests/test_agentic_launch.py` pass unchanged. `python3 tools/affected_tests.py --run uv run
  pytest -q`, `python3 tools/test_tiers.py --check`, `python3 tools/spec_lint.py`.
- **Manual verification:** with a scratch queue cfg, plant a `main_dirty` park on a scratch repo
  with a frontmatter edit; run `uv run nw queue list --json` and the dashboard payload → the
  park is under Conductor handling; run the row's step-4 command with `--json` and without
  `--yes` → a dry-run `human_checkout` record whose facts list the `recommended` lines. Record
  the output in As shipped.

## Out of scope

- The classifier and `nw queue main-dirty-plan` (SPEC-0546).
- Any git write by the conductor or a sub-agent for `main_dirty`; no sub-agent is dispatched
  for it. The write verb is SPEC-0548/0549; conductor writes are SPEC-0551.
- `launch.py` allow or deny lists and `CONDUCTOR_CURSOR_POLICY`.
- `main_not_on_base`, `human_decision`, `restart` stay human-owned; the
  `parked.main_not_on_base` row keeps its current procedure (SPEC-0551 changes it).
- `finish_close` is unchanged.

## Rollout / migration

No data move. The owner change reclassifies existing `main_dirty` parks on the next read; an
old one with no decision emits one `park_undecided`, which wakes the conductor to plan and
escalate. Once decided, the only wake for a `main_dirty` park is the answer to its escalation
(Design, "What wakes a decided park"). This spec removes the silent park, not the human step:
every class, trivial included, still needs the human until SPEC-0551 lands, and IDEA-1159 stays
open until SPEC-0551 is done. Queued under project Meta-Tools after SPEC-0546.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed, including the manual check; affected tests pass.
- [ ] No regressions in close, dashboard, herdr, launch, E5 tests.
- [ ] `docs/product/loop/operate.md` and `docs/product/capabilities/agentic.md` say the
      conductor classifies and escalates, and name only `main-dirty-plan`.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated; `python3 tools/spec_lint.py` exits 0.

## Open questions

None.
