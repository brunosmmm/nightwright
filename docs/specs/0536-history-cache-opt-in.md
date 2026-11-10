---
id: SPEC-0536
title: "History cache opt-in (replaces SPEC-0472 Decision 1)"
status: done
owner: user
created: 2026-11-10
updated: 2026-11-10
source_idea: IDEA-1077
kind: feature
depends_on: [SPEC-0472]
tags: [agentic, observability, history]
---

## Context

[SPEC-0472](./0472-history-cache-for-the-campaign-history-layer.md) is `done` and shipped its
Decision 1 as: `cache=None` (the default) uses the history cache whenever `cfg["cache_dir"]`
is set. Its review still had open cache-invalidation findings, deferred to IDEA-1077:
same-inode same-size `runner.jsonl` rewrites, mailbox delete/reply, `decisions.jsonl`
rewrite/replace/shrink offsets, and a live campaign-id set that does not invalidate stored
terminal documents. The human approved shipping SPEC-0472 on the condition that the steward
immediately makes the cache opt-in, so a stale cache cannot silently affect default users.

Per AGENTS.md "Changing a past decision" this is a new spec. `supersedes` is whole-spec only
(it sets the old spec `superseded`), and only Decision 1 changes here, so SPEC-0472 stays
`done` and this spec is linked through `depends_on`. **This spec replaces SPEC-0472's
Decision 1**; the rest of SPEC-0472 (layout, cursors, rebuild, budgets) is unchanged.

## Goals / Non-goals

**Goals**
- Default `cache=None` is the pure reader even when `cache_dir` is set.
- An explicit config key turns the cache on for the CLI and other `cache=None` callers.

**Non-goals**
- Fixing the open invalidation findings (IDEA-1077); flipping the default back on happens there.

## Decision

`cache=None` is the pure reader (no cache read, no cache write, no `cache: not writable` gap)
unless config `agentic.history.cache` is true, and then it also needs a non-empty
`cache_dir`. `cache=True` still forces the cache; `cache=False` stays pure. The config
default is `agentic.history.cache: false`. `--rebuild-cache` always builds under `cache_dir`.

## Design

- `history_cache.cache_wanted` / `cache_enabled` resolve `cache=None` from
  `cfg["agentic"]["history"]["cache"]`; `history._history_cache_active` delegates to them.
- `config.py` default `agentic.history.cache: False`; `cli_campaign.py` help and the
  unwritable-cache warning respect the opt-in; `docs/product/capabilities/agentic.md` shows the key.

## Alternatives considered

- **Revert SPEC-0472 / supersede it whole** — loses a done, otherwise working cache.
- **Env var** — config is where every other agentic knob lives.

## Acceptance criteria

- [x] With `cache_dir` set and no `agentic.history.cache`, `campaign_history`, `history_index`
  and `history_records` return results equal to `cache=False`, write nothing under `cache_dir`
  and add no gap.
- [x] With `agentic.history.cache: true` and `cache_dir`, `cache=None` uses the cache.
- [x] `cache=True` forces the cache and `cache=False` stays pure regardless of config.
- [x] The config default is `false`; CLI help and docs name the key.

## Test plan

- **Automated tests:** `tests/test_history_cache.py::test_default_cfg_is_pure_with_cache_dir_and_writes_nothing`,
  `::test_config_opt_in_uses_cache`; existing cache tests set the key through their `cfg()` helper
  (and `tests/test_history_perf.py`, `tests/test_cli_campaign_history.py` set it in their cfgs).
- **Regression guard:** `tests/test_history_cache.py`, `tests/test_history.py`,
  `tests/test_cli_campaign_history.py`, `tests/test_history_scale.py`, config tests.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; affected tests pass (box gate at landing is the conductor's).
- [x] Spec body matches what shipped; ledger regenerated.

## As shipped

As designed. Verified after rebasing onto main: `python3 tools/spec_lint.py` exit 0 and `python3 tools/affected_tests.py --run uv run pytest -q` (1958 passed, 3 skipped). No box gate (human waived heavy gating to land).
