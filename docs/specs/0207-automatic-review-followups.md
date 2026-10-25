---
id: SPEC-0207
title: "Automatic follow-up ideas for non-blocking review findings"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, review, followups, close]
source_idea: IDEA-031
depends_on: [SPEC-0203]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-031`).

[SPEC-0203](./0203-review-triage-score-and-routing.md) lets a change merge with non-blocking
(`advisory`) findings; [SPEC-0206](./0206-review-triager-persona.md) may additionally waive some.
Today those observations stay in the campaign's ledger and are never seen again. This spec makes
sure what is forwarded is not forgotten: at the close step the non-blocking findings are filed as
follow-up **ideas** (the repo's durable backlog), idempotently and without ever blocking a merge.

Split out of SPEC-0201/0203 on 2026-10-25 after spec review. It needs only SPEC-0203's
`last_review.advisory`; `last_review.waived` is `[]` unless SPEC-0206 has shipped.

## Goals / Non-goals

**Goals**
- G1. File `major`/`minor` (and optionally `nit`) advisory and waived findings of a merged
  campaign as ideas, idempotent and crash-safe, carrying advisory findings of earlier `fix`
  loops over.
- G2. Record them as `followup` ledger events and list them (and the ones that could not be
  filed) where the merge is reported.
- G3. Configurable (`review.followups`, `review.followups_nits`) and never blocking.

**Non-goals**
- Auto-implementing follow-ups; they are ordinary ideas for the human or a later campaign.
- Filing findings of a campaign that never reaches `CLOSING` (parked, abandoned): its advisory
  findings stay in the ledger only. Accepted limitation.
- Filing blocking findings, or deduplicating against ideas not created by this filer.

## Decision

A deterministic filer run at the start of the close step. Two identities exist and are kept
apart: a finding's **`id`** (`<persona>#<n>`, SPEC-0203) is per review and only used within one
review; its **`key`** (below) is stable across loops and is what carry-over, ledger events and
the idea marker use. Choices that were open: the key deliberately ignores persona and severity so
the same issue keeps its key when its severity or reporter set changes; a filing is retried only
when the close step is re-run (no background retry); a spurious follow-up is cheaper than a lost
one.

## Design

### Key, carry-over and the considered set

- **Key:** `key = sha1(spec_id | norm_finding_title(title) | ",".join(sorted(files)))`
  (`norm_finding_title` from SPEC-0203, lower-case with whitespace collapsed); `key12` = first 12
  hex chars. **This spec owns `key` and `origin`**: SPEC-0203's `last_review.advisory` entries (`{id,
  personas, severity, title, detail, files, ac, category}`) and SPEC-0206's `last_review.waived`
  entries (same fields plus the triager's `reason`) carry neither; the carry-over merge and the
  filer compute them. The working entry shape (in `review_carry` and inside the filer) is `{key,
  personas, severity (highest seen), title, detail, files, ac, category, origin:
  advisory|waived, waive_reason}` where `origin` is `waived` for a `last_review.waived` entry
  (then `waive_reason` = that entry's `reason`) and `advisory` otherwise (no `waive_reason`); on
  key collision the entry keeps the highest severity, the union of `personas`, and `waived`
  wins over `advisory`. The canonical reporter is `personas[0]`.
- **Carry-over** (function `_merge_carry(state, review)` in `queue.py`, called by
  `_apply_reviews` on both `fix` branches — the transition to `IMPLEMENTING` and the
  `loop_exhausted` park — after the routing decision and before `finish`; it is idempotent per
  review because entries are de-duplicated by key, so a SPEC-0206 re-evaluation of the same
  review merges nothing new; if `finish` finds that a human moved the campaign meanwhile, the
  carry is not persisted, accepted since the review re-runs on resume): on every `fix` outcome, the review's `advisory` and `waived` findings are merged
  into `state.review_carry` (a list de-duplicated by key; `origin` = `waived` wins over
  `advisory`), so advisory findings of intermediate loops are not lost although the implementer
  was never asked to fix them. `review_carry` is added to the key list that `_apply`'s `finish`
  persists (`queue.py`, next to `pending_brief`, `close`). A key that is **blocking** in that
  review (any copy of it in the `blocking` list, whatever its severity or reporter) is removed
  from the carry (the implementer is told to fix it) and is not added. A carried key that later
  neither reappears nor is blocking is still filed. A carried finding the implementer fixed
  incidentally is still filed (accepted: the filer cannot tell).
- **Considered set at the close step** = `state.review_carry` ∪ `last_review.advisory` ∪
  `last_review.waived`, de-duplicated by key. **File** those of severity `major` or `minor`;
  `nit` only when `profile.review.followups_nits`. `review.followups: false` files nothing.
  Blocking findings of the final (passing) review do not exist by construction (a pass has none).
- **Profile keys (`profiles.py`)** added to the allowed set under `review:`: `followups: bool`
  (default true in all three builtin profiles), `followups_nits: bool` (default false); validated
  (`ValueError` naming the key); a hand-built profile without them behaves as the defaults.

### Filing

- **When and where:** `_do_close` (a serial worker-side step, `queue.py`) calls
  `file_followups(cfg, state, profile)` first, before `close_campaign` (project = `state["project"]`;
  epic = the `parent` frontmatter of the governing spec, read from `state.spec.rel_path` in the
  main checkout, or `None` when it has none; a test round-trips both through the temp data dir), i.e. only for a campaign
  whose review passed. `file_followups` wraps its whole body in `except Exception`: any failure
  (not just `OSError`) becomes a `note` event and the affected findings are `unfiled`, so the
  filer can never prevent `close_campaign` from running (tested with an org layer that raises a
  non-`OSError`). It does not write the ledger itself: it returns the events (`followup`,
  `note`) in `StepResult.events_before`, which `_apply` appends on the main thread whether or not
  `close_campaign` then succeeds or raises, and a summary (`{filed: [{idea_id, severity, title,
  origin}], unfiled: [{severity, title, error}], waived: [{severity, title, waive_reason}]}`;
  `waived` lists every waived finding including nits that are not filed, and is empty unless
  SPEC-0206 has shipped) on a new `StepResult.followups`. `_apply` stores it
  in `state["close"]["followups"]` **after** it assigns `state["close"] = oc.close` (which
  replaces the dict, or the list would be lost); `close` is persisted by `finish`. `_do_close` builds the filer result first, keeps its
  existing handler for `(GitError, ValueError, OSError)` and adds `except Exception` around
  `close_campaign` that sets `res.error = repr(e)` (the existing close-error path, parking
  `open_failed`), so the `StepResult` carrying `events_before` and `followups` always reaches
  `_apply`; the test injects a `RuntimeError` from `close_campaign`.
- **Retries:** a close step is re-run after a park/resume (`open_failed` with `resume_to:
  CLOSING`) or when the close returns the campaign to TRIAGING→…→CLOSING (`regate_failed`); each
  re-run repeats the filer, so a filing that failed before a close that then failed is retried.
  A capture error on a close that then succeeds is not retried: the finding is reported
  `unfiled`.
- **Capture:** through the in-process API, `org_write.add_idea(cfg, text, project=<campaign
  project>, epic=<parent epic when the spec has one>, kind="improvement")` (returns `(path,
  headline, idea_id)`; the CLI `wt idea capture` calls the same function), in the main repo's
  data dir, never inside the worktree. Text: `wtfu:<key12> [<spec id>] <persona>/<slot>: <title> —
  <first 200 chars of detail> (files: …)`, where `<persona>` is the canonical reporter, `<slot>`
  is the finding's `ac` if present, else its `category`, else omitted together with its `/`.
  Before building the text, `title`, `detail` and file names are sanitized: newlines and control
  characters become spaces, `[`/`]` are dropped, `—` is written `-`, `*` and `:` become spaces
  (so no org tag-like `:word:` suffix or heading marker forms), and the title is cut at 120
  characters. The headline check below (with its `note` fallback) covers any other
  transformation `add_task` applies. The marker `wtfu:<key12>` leads the text (so no cap or wrapping can truncate it) and
  is alphanumeric (so sanitizing cannot alter it).
- **Idempotency (crash-safe)**, for each finding to file:
  1. if a `followup` event with that key is in the ledger ⇒ skip filing, but include it in the
     summary's `filed` list with the `idea_id` read from that event (so a re-run, a resume or
     `regate_failed` still reports the complete list, not just new filings); an adopted idea
     (step 2) is reported the same way;
  2. else look the marker up in the ideas file and its archive (`org_write.ideas_archive_path`),
     by plain substring search of the headline text through the org reader; found ⇒ **adopt**
     it: write the `followup` event with that idea's `:ID:`, do not capture again (this covers a
     crash between `add_idea` and the event);
  3. else capture; on success verify that the returned headline contains the marker; write the
     `followup` event with the new `idea_id` (a headline without the marker adds a `note` event
     but the event is still written, so a created idea is never captured twice);
  4. a capture error ⇒ `note` event, **no** `followup` event; the merge is never blocked and,
     if the close then succeeds, the finding is reported `unfiled`.
- **Ledger:** new event type `followup` registered in `schemas.py` `EVENT_KEYS`: `("persona",
  "severity", "title", "key", "idea_id", "origin")`, where `origin` is `advisory` or `waived`
  and `idea_id` is always set (events are only written for existing ideas). Existing ledgers without
  `followup` events stay valid.
- **Reporting:** the surfaces are `wt campaign show` (renders `state.close.followups`) and the
  close step's report row in `wt.queue.tick.v1` (gains an additive `followups` key with the
  filed/unfiled counts and idea ids, set from `StepResult.followups` in `_apply`). They list the follow-ups
  (`IDEA-NNN severity title`), the `unfiled` findings and the waivers with their `waive_reason`.
- **Docs:** `docs/product/capabilities/agentic.md` documents the filer, the keys and the marker.

### Known limitations

- The key hashes the normalized title and the cited files: if a reviewer rewords the title of
  the same issue, or cites an extra or different file, between loops, the keys differ and two
  ideas are filed (adoption by marker only helps for an identical
  key). Accepted: a spurious follow-up is cheaper than a lost one.
- The ideas file is written before `close_campaign`. It lives in the user's data dir, outside the
  project repo that the close step merges into, so it cannot dirty the merge target (a test with a
  temp data dir outside the repo asserts the repo stays clean). A setup that keeps the ideas file
  inside the project repo is unsupported for the filer's purposes.

## Alternatives considered

- **Do not file follow-ups; only list them** — lists in a merge log are not seen again; ideas
  are the repo's durable backlog.
- **File only the final review's findings** — rejected: advisory findings of earlier loops would
  be silently dropped (see carry-over).
- **File every finding including nits** — noise; nits are opt-in.
- **Key on persona and severity too** — the same issue would re-file when its severity changes
  between loops.

## Acceptance criteria

- [x] AC1 (G2). `followup` is registered in `schemas.py` and valid under `ledger_problems`
      (a missing key is rejected).
- [x] AC2 (G3). `resolve_profile` provides and validates `review.followups` /
      `review.followups_nits` (defaults above; bad values raise `ValueError` naming the key; the
      keys are accepted under `review:`).
- [x] AC3 (G1). The filer files `major`/`minor` advisory and waived findings (`nit` only with
      `followups_nits`; nothing with `followups: false`) through `add_idea` with
      `kind="improvement"`, the campaign's project and its parent epic when it has one; the text
      starts with the `wtfu:<key12>` marker, is sanitized and bounded (a test files a finding
      with a 300-character title and 20 files and asserts the headline still starts with the
      marker); the key is the same for a changed severity or reporter set.
- [x] AC4 (G1). Carry-over: advisory findings of earlier `fix` loops are filed once; a key that is
      blocking in a later review (any copy, any severity) is removed from the carry and not
      filed; `review_carry` survives `_apply`'s `finish`.
- [x] AC5 (G1, G3). Idempotency: re-running the filer files each finding once, including after
      a simulated crash between `add_idea` and the event (the marker lookup in ideas and archive
      adopts the idea) and after a capture error on the first attempt followed by a re-run (no
      duplicate); a capture failure does not block the merge and the finding is reported
      `unfiled`; the filer's events are appended on the main thread even when `close_campaign`
      raises, and `state.close.followups` survives the assignment of `state["close"]`; a re-run reports skipped
      and adopted findings in `filed` with their `idea_id`; a non-`OSError` raised by the org layer
      (filer) or a `RuntimeError` from `close_campaign` never prevents the other from running;
      a headline returned without the marker adds a `note` but still writes the `followup` event;
      a test through the real `add_task` path asserts the marker round-trips in the lookup;
      `review.followups: false` end to end files nothing.
- [x] AC6 (G2). `wt campaign show` and the close report row list follow-ups (`IDEA-NNN severity
      title`), unfiled findings and waivers with their `waive_reason` (including waived nits that are not filed).
- [x] AC7. End to end with FakeRunner and a temp org data dir: (a) a clean pass, no findings ⇒
      no ideas; (b) minor/major advisory findings with a passing score ⇒ `MERGED` and ideas filed
      with the right project/epic/kind and marker, read back from the temp data; (c) `must_fix`
      ⇒ `IMPLEMENTING` once, then pass: the fixed finding is not filed while an advisory finding
      of the first review is; (d) the close step run twice (resume) files each idea once.
- [x] AC8. `docs/product/capabilities/agentic.md` documents the filer (grep test); full
      `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_review_triage.py` (key stability, text sanitizing and
  marker preservation, carry-over merge/removal incl. severity-changing duplicates, filer
  idempotency with a temp org data dir including the crash and error cases);
  `tests/test_agentic_profiles.py` (`review.followups*` defaults/validation, docs grep);
  `tests/test_agentic_logs.py` (`followup` accepted / missing key rejected);
  `tests/test_agentic_close.py` (merge summary lists follow-ups, unfiled findings and waivers;
  filer events recorded when `close_campaign` raises); `tests/test_agentic_queue.py` (carry
  persisted by `finish`, `state.close.followups` survives, `campaign show`);
  `tests/test_agentic_e2e.py` (AC7 paths a-d; ideas read back from the temp org data).
- **Manual verification:** on the next real campaign with advisory findings confirm the filed
  ideas (`wt idea list`) and record them here.
- **Regression guard:** full `uv run pytest`.

### Test record (2026-10-25)

Ran `uv run pytest -q` (full suite: all green except the generated-ledger staleness check until
`python3 tools/spec_lint.py --write-ledger` ran; re-run green afterwards) and
`python3 tools/spec_lint.py` (exit 0). New tests, by file:
`tests/test_agentic_review_triage.py` (key stability, sanitizing/marker/bounds through the real
`add_idea`, project/epic/kind read back, nits and `followups: false`, re-run reporting, crash
adoption in ideas and archive, capture error then re-run, non-`OSError` org layer, marker-less
headline, `_merge_carry` merge/removal/idempotency, considered set);
`tests/test_agentic_profiles.py` (defaults, validation, docs grep); `tests/test_agentic_logs.py`
(`followup` accepted, missing key rejected); `tests/test_agentic_queue.py` (carry persisted by
`finish` on both `fix` branches, `state.close.followups` survives, `close_campaign` raising
`RuntimeError`, filer failure, `campaign show`); `tests/test_agentic_e2e.py` (AC7 a-d plus a
profile opt-out).

Review loop 1 additions: the outer filer guard reports the not-yet-handled findings as `unfiled`
(asserted); `find_marker` refuses to adopt an idea without `:ID:` (that finding is `unfiled`, no
duplicate capture); `regate_failed` close re-run test; the `wt.queue.tick.v1` payload test;
the archive-adoption precondition tightened.

Manual verification (2026-10-25): filed a `major` advisory finding through `file_followups`
against a scratch org data dir and ran `wt ideas` through the real CLI: the idea showed as
`IDEA-001`, kind improvement, project `Meta-Tools`, epic `SPEC-0169`, headline starting
`wtfu:<key12>`. A first run on a real campaign with advisory findings remains worth a glance.

### As shipped (2026-10-25)

- New module `src/wt/agentic/followups.py` holds the pure helpers (key, entries, sanitizing,
  marker lookup) and `file_followups(cfg, state, profile)`, which returns `(events, summary)`;
  `queue._merge_carry` and `queue._do_close` call it. `finish` persists `review_carry`.
- `followup_params(profile)` in `profiles.py` reads the two flags with the documented defaults.
- Minor choices the spec left open: the `<slot>` (`ac`/`category`) is cut at 80 characters and at
  most 5 file names are listed (`+N more`), so the text stays bounded; `waived` in the summary is
  filled even with `review.followups: false`; the campaign show lines read `follow-up IDEA-NNN
  severity title`, `[unfiled] ...` and `[waived] ...`.
- The close tests that need a real campaign (filer events kept when `close_campaign` raises, the
  summary in `state.close`) live in `tests/test_agentic_queue.py` rather than
  `tests/test_agentic_close.py`; `campaign show` is covered there too.
- The close report row's `followups` key carries more than the Design's counts and ids: it also
  has `items` (filed: idea_id, severity, title, origin), `unfiled_items` and `waived` (with
  `waive_reason`), so the row satisfies AC6 without a lookup in `campaign show`.
- If the filer fails before the per-finding loop, the findings it had selected are reported
  `unfiled` with the failure; an adopted idea without `:ID:` is `unfiled` rather than guessed.

## Rollout / migration

Takes effect at the next close step; no data migration. Existing ledgers without `followup`
events are valid; a state without `review_carry` means an empty carry. Users can set
`review.followups: false` to opt out. Restart the running loop to load the code.

## Clock Log

Built by the agentic loop or the human lane; idea bookkeeping via `wt spec reconcile --apply`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
