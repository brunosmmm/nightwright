---
id: SPEC-0552
title: Leaner agentic campaign loop (role tiers, review packet, findings collator, resumable sessions)
status: accepted
owner: user
created: 2026-11-11
kind: epic
tags: [agentic, cost, review]
source_idea: IDEA-1297
---

## Context

The campaign loop ([queue.py](../../src/nightwright/agentic/queue.py):
`SPEC_WRITING → SPEC_REVIEW → IMPLEMENTING → GATING → TRIAGING → REVIEWING → CLOSING`) spends
most of its money on code review, and most of the review money on re-reading. Each persona
run is a fresh `claude -p … --no-session-persistence` (or a fresh Cursor / Pi agent), so
every loop re-reads the spec, the full diff and the repository from scratch. The three code
reviewers (`conformance_reviewer`, `test_reviewer`, `adversarial_reviewer`) then raise new
findings anywhere in the change on every loop, and the implementer receives all of them.

**Measured evidence** (quoted from the IDEA-1297 Summary, from `nw campaign export`):

> Since Nov 5, 1,488 persona runs / 54 campaigns: 3 code reviewers = 77% of agent time and
> ~72% of cost; implementer 14%; median campaign 9 review runs vs 4 implementer runs;
> adversarial reviewer read 812M cached tokens. All-time: Opus-high reviewers 1,684 runs =
> $1,117 = 56% of all recorded spend; same-era Sonnet reviewer runs cost about the same per
> run ($0.81 vs $0.72) because they read ~3x more context (0.74M vs 0.22M median cached), so
> the waste is re-reading, not only the model.

A re-run of the export on 2026-11-11 (`--since 2026-11-05`) agrees: per-persona median
`cache_read_tokens` per run is 2.44M (adversarial), 1.47M (conformance), 1.01M (test) against
1.03M for the implementer. Many runs since Nov 5 record `cost_usd = 0` (Cursor / subscription
runs), so token counts are measured next to dollars in every phase below.

**Human decisions** (quoted from the IDEA-1297 Summary; authoritative):

> Restructure, not just retune; apply to every backend (Claude now; cursor/pi via the same
> logic); size/tone capped by default, higher tiers only when the user writes them into the
> spec; slight quality dip acceptable at default; test by building it.

Today the strength of each persona is fixed per profile (`profile_defs/{frugal,balanced,
thorough}.yaml` set `model: haiku|sonnet|opus`), the profile is picked by the spec
reviewer's own `size`/`tone` estimate through `PROFILE_MATRIX` in
[profiles.py](../../src/nightwright/agentic/profiles.py) (`select_profile`), so an agent's
estimate can raise every persona to Opus. The loop needs many specs because it touches the
model binding, the review data flow, finding routing and the runner backends, each of which
must be measurable on its own.

## Goals / Non-goals

**Goals**
- Cut review re-reading: reviewers read one per-loop packet and, from loop 2, only the delta
  and their own prior findings.
- Make strength a property of the persona's *role* (marshal / judge / deep), mapped per
  backend, so Claude, Cursor and Pi follow the same logic.
- Strength above the default happens only when a human writes it into the spec.
- Fix loops receive only in-scope findings; out-of-scope ones become follow-up ideas
  automatically, with no steward or human ruling.
- Each phase ships behind its own flag and is measured against the export baseline and a
  regression set of real catches.

**Non-goals**
- Changing the gate (GATING) or its triage beyond the gate triager's tier.
- Changing how specs are written or reviewed, beyond their tier.
- A new backend, or removing an existing one.
- Automatic tuning of tiers from the measurements (later work, from evidence).

## Decision

Restructure the review half of the loop around cheap *marshal* steps that prepare and route
information, so that *judge* reviewers read less and the implementer fixes less, and keep
one session per persona per campaign where the backend can resume it. Ship in four phases,
one child spec each, each behind a flag in `agentic.lean` and pinned per campaign at open so
flag-on and flag-off campaigns can run side by side (A/B).

### Today vs proposed

```
TODAY (balanced profile)
 spec_writer(sonnet) -> spec_reviewer(sonnet, sets size/tone -> profile; may pick thorough=opus)
 loop N:
   implementer(sonnet, fresh run) -> gates -> gate_triager(sonnet)
   -> conformance + test + adversarial (opus-high, 3 fresh runs, full diff + repo each loop)
   -> triage_reviews (code) [-> review_triager(sonnet)]
   -> all findings -> implementer (next fresh run)        ... up to loop_max
 CLOSING

PROPOSED (default = capped)
 spec_writer(judge) -> spec_reviewer(judge; size/tone may only LOWER the profile)
 loop N:
   implementer(judge, ONE session per campaign, resumed)
   -> gates (unchanged) -> gate_triager(marshal)
   -> review_marshal(marshal): packet-loop-N.json  (ACs, diff + delta, AC->hunks/tests,
                                                    gates, prior findings+status, pointers)
   -> conformance + test + adversarial (judge, one session each per campaign)
        loop 1: full review from the packet
        loop 2+: verify OWN prior findings + the delta; new finding only if
                 regression-in-delta or tied to an AC missed in loop 1
   -> findings_collator(marshal): de-dup, tie to AC, in/out of scope
        out-of-scope -> follow-up idea (linked to campaign)
        in-scope     -> implementer (resumed session)
 CLOSING  [+ fresh adversarial_reviewer(deep) ONLY if size/tone raised by hand in the spec]
```

## Architecture / cross-cutting design

### Role tiers

Every persona declares a tier in its builtin definition (`persona_defs/*.yaml`, new `tier:`
key). A backend maps each tier to a model; the profile no longer names models for builtin
personas when `lean.tiers` is on (an explicit `model:` in a profile or model set stays as an
override, so existing configs keep working).

| Persona | Tier | Note |
|---|---|---|
| spec_writer, spec_reviewer | judge | unchanged role |
| implementer, conflict_resolver | judge | one session per campaign (phase 4) |
| conformance / test / adversarial_reviewer | judge | packet + delta (phase 2) |
| gate_triager, review_triager | marshal | classification only |
| divergence_triager | judge | rare; picks extra round / rework / escalate |
| review_marshal (new) | marshal | builds the AC map of the packet |
| findings_collator (new) | marshal | de-dup, AC tie, scope |
| close adversarial pass | deep | only when raised by hand |

| Backend | marshal | judge | deep |
|---|---|---|---|
| claude | `claude-haiku-5-5` | `claude-sonnet-5-5` | `claude-opus-5-5` |
| cursor (to be confirmed) | `grok-4.7` effort low | `grok-4.7` effort high | `gpt-5.6-sol` effort high |
| pi (to be confirmed) | `openai/gpt-5.6-luna` | `openai/gpt-5.6-sol` | `openai/gpt-6.1-terra` |

Cursor and Pi candidates are today's values in `cursor_map.yaml` (haiku/sonnet/opus rows)
and `model_set_defs/gpt.yaml`; the Cursor implementer keeps `composer-2.5` from
`model_set_defs/grok.yaml` as a per-persona override. Each is confirmed with one real call
before its row is marked confirmed (fakes are not verification).

### Cap rule

- The **default** strength is the `agentic.default_profile` (`balanced`) with tiers as in
  the table. Automatic sizing (the spec reviewer's `size`/`tone`) may select a profile at or
  below the default (e.g. `frugal`) and may **never** raise above it.
- A higher size or tone takes effect only when a human writes `size:` / `tone:` into the
  spec frontmatter by hand. "By hand" is enforced: the value counts only when it is in the
  spec as committed on the base branch when the campaign opens, and a value added or changed
  inside a campaign (spec writer, implementer) is ignored and reported.
- The `--profile` flag of `nw queue add` is subject to the same cap (the conductor is an
  agent and uses it).
- Only a hand-raised campaign gets the deep tier and the close-time deep adversarial pass.

### Campaign-dir artefacts and state (shared contract)

- `<campaign_dir>/packet-loop-N.json` (schema `nw.review_packet.v1`) — phase 2.
- `<campaign_dir>/findings-loop-N.json` (schema `nw.collated_findings.v1`) — phase 3.
- `state.lean` — the flags pinned at open (`{tiers, packet, collator, sessions}`) — phase 1.
- `state.sessions[persona] = {backend, session_id, host, model, loops}` — phase 4.
- Every finding keeps one stable id across loops (`followups.finding_key`-style key: spec
  id + normalized title + cited files) so status (open / fixed / waived) is traceable.
- Export (`campaign_export.py`) gains `tier`, `loop` and `resumed` on runs and `lean` on
  campaigns, so every measurement below is one query.

### Measurement plan

Baseline: campaigns created 2026-11-05 to the day phase 1 merges, all flags off. Each phase
is compared on campaigns run with only that phase's flag added (A/B by flag, pinned per
campaign).

```bash
nw campaign export --since 2026-11-05 --table runs --stdout > runs.jsonl
nw campaign export --since 2026-11-05 --table campaigns --stdout > campaigns.jsonl
python3 - <<'EOF'
import json, statistics as st
from collections import defaultdict
runs = [json.loads(l) for l in open("runs.jsonl")]
camps = [json.loads(l) for l in open("campaigns.jsonl") if json.loads(l)["state"] in ("MERGED", "HANDED_OFF")]
by = defaultdict(list)
for r in runs: by[r["persona"]].append(r)
for p, rs in sorted(by.items()):
    print(p, len(rs), round(sum(r["cost_usd"] or 0 for r in rs), 2),
          st.median([r["cache_read_tokens"] or 0 for r in rs]))
print("cost/campaign", st.median([c["cost_usd"] or 0 for c in camps]),
      "loops", st.median([c["loops"] or 0 for c in camps]),
      "reviewer_runs", st.median([c["reviewer_runs"] or 0 for c in camps]))
EOF
```

| Phase | Primary measure | Expected direction |
|---|---|---|
| 1 tiers + cap | cost (and tokens) per campaign | down; reviewers no longer Opus by default |
| 2 packet + delta | cache-read tokens per review run; loops to converge | down; not up |
| 3 collator | fix loops per campaign; steward/human scope rulings | down |
| 4 sessions | tokens (input + cache read + output) per loop | down from loop 2 |

**Quality regression set** — real catches that must still be caught with every flag on.
Each is replayed by re-running the review step against the recorded commit of the campaign
(`nw campaign export --table findings` gives seq, persona, AC):

1. **SPEC-0529 reflection oracle** (campaign `SPEC-0529-20261110-f7b4`, findings seq 552 /
   593 adversarial, 580 test): "Signed 401 is a MAC oracle: a bearer-token holder can forge a
   valid request MAC without the secret". Found in a late loop, with no AC on the
   adversarial finding — the hardest case for delta reviews and for scoping, so it is the
   gating case for phases 2 and 3.
2. **SPEC-0476 missing-trailing-newline hang** (campaign `SPEC-0476-20261109-ca46`, seq 29
   conformance AC4 must_fix, 217, 238 adversarial): "Ended stream initial paint hangs on a
   no-newline fragment".

A phase whose flag-on replay misses either catch does not go default-on; a slight quality
dip elsewhere at default is accepted (human decision).

## Breakdown / sub-specs

- [x] SPEC-0553 (project: Meta-Tools) — Role tiers, per-backend tier mapping, size/tone cap
  and the hand-raised deep close pass; `agentic.lean` flag scaffolding. Measure: cost per
  campaign.
- [ ] SPEC-0554 (project: Meta-Tools) — Marshal review packet `packet-loop-N.json` and delta
  reviews from loop 2 (depends_on SPEC-0553). Measure: cache-read tokens per review run,
  loops to converge.
- [ ] SPEC-0555 (project: Meta-Tools) — Findings collator and automatic follow-up ideas for
  out-of-scope findings (depends_on SPEC-0554). Measure: fix loops per campaign, scope
  rulings.
- [ ] SPEC-0556 (project: Meta-Tools) — Resumable implementer and reviewer sessions per
  backend, with the fresh-session fallback (depends_on SPEC-0555). Measure: tokens per loop.
- [ ] SPEC-0557 (project: Meta-Tools) — Effort ceiling: no xhigh/max persona run on any
  backend (human decision 2026-11-11). Independent of 0553..0556; default on. Measure: cost
  per review run.
- [x] SPEC-0558 (project: Meta-Tools) — Per-campaign lean opt-in (`nw queue add --lean` /
  `--lean-flags a,b`) and a `--cohort` tag exported per campaign, so lean campaigns run
  alongside normal ones as an A/B (human decision 2026-11-12). Measure: cost and loops per
  campaign by cohort.
- [ ] SPEC-0572 (project: Meta-Tools) — Lean spec phase: resumable spec writer and delta spec
  re-review behind `agentic.lean.spec_sessions`, which `--lean` turns on (human decision
  2026-11-12; depends_on SPEC-0556, SPEC-0558). Measure: spec-phase cost per campaign.

Sequencing / dependencies: linear. Tiers first because every later persona (marshal,
collator) needs a tier; the packet before the collator because the collator ties findings
to the packet's ACs and statuses; sessions last because a resumed session is fed the packet
delta and the collated findings, and the fresh-session fallback is "packet + findings
history", which must exist first.

## Acceptance criteria

- [ ] All four children are `done`, each with its flag measured against the baseline and
      its measurement recorded in its As shipped section.
- [ ] With all four flags on, a default (not hand-raised) Claude campaign runs no
      `claude-opus-*` model in any persona run.
- [ ] With all four flags on, the regression-set replay catches both the SPEC-0529
      reflection oracle and the SPEC-0476 trailing-newline hang.
- [ ] With all four flags on, median cost and median review-run cache-read tokens per
      campaign are below the baseline, on at least 10 flag-on campaigns.
- [ ] A campaign on a backend without session resume completes the full proposed flow
      using fresh sessions fed the packet and the findings history.

## Test plan

- **Integration/e2e tests:** a FakeRunner campaign (`NW_AGENTIC_RUNNER=fake`, as in
  `tests/test_agentic_*`) with all four flags on walks open → implement → gate → packet →
  three reviewers → collator → fix loop (resumed implementer) → second review on the delta →
  close; asserts the packet and collated-findings files per loop, the follow-up idea for an
  out-of-scope finding, the tiers recorded on every run, and no deep-tier run. The same test
  with a hand-raised `tone: critical` asserts exactly one deep adversarial run at close.
- **Manual verification:** run at least 10 real Meta-Tools campaigns with all flags on (and
  one on Cursor or Pi), run the measurement queries above against the baseline, replay the
  regression set, and record the numbers here.

## Rollout / sequencing

Each child lands with its flag default **off**; it is turned on for a share of campaigns
(per-project overlay) until its measurement and the regression set pass, then default-on.
Partial delivery is safe: each flag is independent at runtime except that `collator`
requires `packet` and `sessions` requires both (a campaign pinning an unmet dependency runs
with that flag off and says so in its open event).

## Definition of done

- [ ] All child specs `done` or `superseded` (the linter gates this).
- [ ] Integration test plan executed; affected tests green.
- [ ] Acceptance criteria met; epic body reflects what shipped.
- [ ] `docs/LEDGER.md` regenerated.

## Open questions

- Confirm the Cursor and Pi tier rows with one real call each (and whether Pi's deep tier
  should be `openai/gpt-6.1-terra` or an Anthropic `claude-opus-5-5` via Pi's anthropic
  provider).
- Should `divergence_triager` be marshal instead of judge? (It is rare; judge chosen for
  safety.)
