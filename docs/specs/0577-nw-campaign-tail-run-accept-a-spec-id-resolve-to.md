---
id: SPEC-0577
title: nw campaign commands accept a spec id and resolve it to its campaign
status: done
owner: user
created: 2026-11-12
updated: 2026-11-12
kind: feature
source_idea: IDEA-657
tags: [agentic, cli]
---

## Context

`nw campaign tail`, `run`, `runs`, `log` and `show` take a campaign id as their first
argument (`src/nightwright/agentic/cli_campaign.py`). A campaign id is
`<SPEC-ID>-<YYYYMMDD>-<4 hex>` (`store.new_campaign_id`), so the caller who knows only
"SPEC-0577" must first run `nw campaign list --json` and pipe it through python to find the
id of that spec's active campaign. `nw campaign list` already filters by `--spec`, and
`store.active_campaign_for_spec` already answers "the open campaign for this spec", but
neither is reachable from the commands people reach for next.

Spec ids are per project (SPEC-0327), so one spec id can name campaigns in several projects,
and one spec can have a closed (MERGED / ABANDONED) campaign and a later open one.

## Goals / Non-goals

**Goals**
- Wherever those five commands take a campaign, a spec id works too: it resolves to that
  spec's campaign and the command runs on it.
- The choice is deterministic and predictable: the newest open campaign, else the newest
  campaign of any state.
- A real campaign id always keeps meaning that campaign.
- A resolution that could pick the wrong campaign fails with a message naming the candidates.

**Non-goals**
- Changing `nw campaign history`, `records`, `export`, `note`, or the queue/dashboard commands.
- A new `--project` flag; the ambiguity error lists the campaign ids to use instead.
- Shell completion of spec ids for the campaign argument.
- Resolving idea ids, or any other prefix.

## Decision

Add one resolver, `store.resolve_campaign_ref(cfg, ref)`, and call it first in
`campaign tail`, `run`, `runs`, `log` and `show`. An argument that is an existing campaign id
is used as is. Otherwise it is treated as a spec id: the newest non-terminal campaign for that
spec wins; with none, the newest terminal one. When the winning tier holds campaigns from more
than one project, the command fails and lists them. The CLI prints the resolved campaign id on
stderr, so stdout (JSON, transcripts) is unchanged.

## Design

`src/nightwright/agentic/store.py`, next to `active_campaign_for_spec`:

```python
def resolve_campaign_ref(cfg, ref: str) -> tuple[str, bool]:
    """(campaign id, resolved_from_spec). ValueError when nothing matches or it is ambiguous."""
```

Algorithm:
1. If `campaign_state(cfg, ref)` is not None (which also rejects path-like input,
   SPEC-0431), return `(ref, False)`. A real campaign id never goes through spec lookup.
2. `rows = list_campaigns(cfg, include_terminal=True, spec_id=ref)`, already newest first by
   `created_at`. No rows: `ValueError(f"no campaign or spec {ref!r} ...")`, the same wording
   family as `read_state`'s "no campaign ...", so the existing `click.ClickException` wrapping
   keeps working.
3. `open_rows` are the rows whose state is not in `TERMINAL_STATES`. The pool is `open_rows`
   when non-empty, else `rows`.
4. If the pool spans more than one distinct project (`project or INTERNAL_PROJECT`), raise
   `ValueError` listing `id (project, state)` for every campaign in the pool.
5. Return `(pool[0]["id"], True)`.

`src/nightwright/agentic/cli_campaign.py`: a small helper `_campaign_arg(cfg, ref)` calls the
resolver, turns `ValueError` into `click.ClickException`, and, when `resolved_from_spec`, writes
`campaign <id> (newest open campaign for <ref>)` or `(newest campaign for <ref>; none open)` to
stderr with `click.echo(err=True)`. The five commands replace their `campaign` parameter with
the helper's result before any other use. `campaign tail --follow-campaign` gets the same
resolution; its stdout (the herdr run pane viewer, SPEC-0366) is unchanged because the note
goes to stderr.

Help text for the five `campaign` arguments says "campaign id, or a spec id (its newest open
campaign)". `docs/product/guides/agent-surface.md` gets one line in the campaign command
section. No schema or JSON shape changes: `--json` output carries the resolved campaign id in
its existing `campaign` / `id` field.

## Alternatives considered

- **A `--spec` option on each command** — a second way to name the same thing, and it
  collides with `campaign list --spec` meaning "filter". A positional that accepts either is
  what callers already type.
- **Add `nw campaign resolve <SPEC>` and keep the commands strict** — the caller still needs
  command substitution; the idea is to remove that step.
- **Always pick the newest campaign, open or not** — a newer ABANDONED campaign would hide an
  older campaign that is still open. Preferring open is what a live `tail` wants.
- **Fail when a spec has more than one campaign** — most specs that were retried have a
  closed one, so this would fail the common case.

## Acceptance criteria

- [x] `store.resolve_campaign_ref(cfg, "<existing campaign id>")` returns that id with
      `resolved_from_spec` false, even when the id also looks like it starts with a spec id.
- [x] Given a spec id with one open campaign, it returns that campaign with
      `resolved_from_spec` true.
- [x] Given a spec id with a MERGED campaign and a newer or older open one, the open one
      wins; given only terminal campaigns, the newest by `created_at` wins.
- [x] Given a spec id whose winning tier spans two projects, it raises `ValueError` and the
      message lists every candidate campaign id with its project and state.
- [x] Given an unknown id, a path-like string (`../x`) or an unknown spec id, it raises
      `ValueError` and no directory outside the campaigns root is read.
- [x] `nw campaign tail SPEC-N`, `run SPEC-N <run>`, `runs SPEC-N`, `log SPEC-N` and
      `show SPEC-N` behave exactly as with the resolved campaign id, exit non-zero with the
      resolver's message when it raises, and their `campaign` argument help mentions spec ids.
- [x] A spec id prints the resolution note on stderr only (stdout and `--json` unchanged,
      same schema); a real campaign id prints no note.
- [x] `nw campaign tail --follow-campaign SPEC-N` follows the resolved campaign.
- [x] `nw campaign history`, `records`, `export` and `note` are unchanged.

## Test plan

- **Automated tests:** extend `tests/test_agentic_campaign_cli.py`, which already carries its
  one module marker `pytestmark = pytest.mark.fast`; no new test module and no second tier.
  - Resolver tests (fixtures built with `_seed`-style `S.create_campaign`): a real id passes
    through; one open spec campaign; open beats a newer MERGED one and an older MERGED one;
    only terminal campaigns gives the newest; two projects with the same spec id both open
    raises with all ids in the message; the same spec id open in one project and terminal in
    another picks the open one; unknown id, unknown spec id and `../x` raise.
  - CLI tests through `_run`: `campaign runs SPEC-0170 --json` equals `campaign runs
    <full id> --json` and parses; `campaign log SPEC-0170 --tail 1`; `campaign show SPEC-0170
    --json`; `campaign run SPEC-0170 <run prefix>` and `campaign tail SPEC-0170` over a seeded
    run directory; `campaign tail --follow-campaign` using the existing `follow_campaign`
    `stop` seam or a monkeypatched `TL.follow_campaign` asserting the campaign id it receives;
    ambiguity and unknown-id exit codes and messages; the stderr note present for a spec id
    and absent for a campaign id; `history`/`note` with a spec id still fail as before.
  - `python3 tools/test_tiers.py --check` exits 0.
- **Manual verification:** with a real data dir, `nw campaign tail SPEC-<n>` for a spec with
  a running campaign shows its transcript and prints the resolved id on stderr;
  `nw campaign runs SPEC-<n> --json | jq .campaign` prints the same id `nw campaign list
  --spec SPEC-<n> --json` shows for the open campaign.
- **Regression guard:** run the affected tests (`python3 tools/affected_tests.py --run uv run
  pytest -q`) plus `tests/test_agentic_campaign_cli.py`; `python3 tools/spec_lint.py` exits 0.

### Test record (2026-11-12)

- `uv run pytest -q tests/test_agentic_campaign_cli.py tests/test_rich_output_guard.py`: 33 passed
  (loop 1 added: several-open ordering, `run` ambiguity, per-command `--help` mentions spec ids,
  `history`/`note`/`records`/`export` do not resolve, corrupt real-campaign state reports the read
  error; earlier, 11 SPEC-0577 tests:
  resolver cases incl. two-project ambiguity and `../x` never opened; CLI `runs`/`log`/`show`/
  `run`/`tail` with a spec id equal to the full-id output, both stderr note variants, real id
  prints no note, ambiguity/unknown exit codes, `--follow-campaign` receives the resolved id,
  `history` does not resolve). Stdout/stderr asserted via `r.stdout` / `r.stderr` (Click 8.4
  keeps them separate).
- `python3 tools/test_tiers.py --check` exit 0; `python3 tools/spec_lint.py` OK.
- Affected tests (`python3 tools/affected_tests.py --exclude-marks=integration,tui,remote,perf --run uv run pytest -q`): 1809 passed, 3 skipped; full suite left
  to gating.
- Manual, real data dir: `nw campaign runs SPEC-0577 --json` printed `campaign
  IDEA-657-20261112-6e90 (newest open campaign for SPEC-0577)` on stderr and `.campaign` equal to
  the id `nw campaign list --spec SPEC-0577 --json` shows.

### As shipped

- A real campaign directory with an unreadable `state.json` raises `campaign '<id>' has an
  unreadable state.json: <error>` instead of falling through to the spec lookup.
- The unknown-ref message reads `no campaign '<ref>' (not a campaign id, and no campaign has
  that spec id)`, keeping the `no campaign '<ref>'` prefix existing tests and callers match.
- For `--follow-campaign` the stderr note is written before following starts and may be wiped
  by the viewer's screen clear.
- `docs/product/guides/agent-surface.md` carries the one-line note under the schema table.

## Rollout / migration

None. The change is additive: every argument that worked before still resolves to the same
campaign. Add the guide line in `docs/product/guides/agent-surface.md` in the same change.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected automated tests pass; manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`).

## Open questions

None.
