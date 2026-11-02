---
id: SPEC-0380
title: "test_reexec_off_by_default asserts the SPEC-0375 started code_sha"
status: done
owner: user
created: 2026-11-02
updated: 2026-11-02
depends_on: [SPEC-0375]
---

## Context

SPEC-0375 made the runner record the `code_sha` it started on in the `start` journal record
and lease even with re-exec off (for `nw queue watch`'s stale-code line). The older
`tests/test_agentic_reexec.py::test_reexec_off_by_default` still asserted `code_sha is None`,
so the full-suite sentinel on main went red. Not env-dependent: it fails deterministically.

## Goals / Non-goals

**Goals**
- Bring the test in line with the shipped SPEC-0375 behavior.

**Non-goals**
- Any runtime code change.

## Decision

The runtime is correct; the test expectation was stale. With re-exec off the `start` record
carries the sha the runner started on, and later commits neither trigger `_EXEC` nor change it.

## Design

`test_reexec_off_by_default` captures `HEAD` before `Q.run` and asserts
`calls == []` and `start.code_sha == that sha`.

## Acceptance criteria

- [x] `test_reexec_off_by_default` passes and still proves no re-exec happens.
- [x] No source change.

## Test plan

- **Automated tests:** `uv run pytest -q -n 0 tests/test_agentic_reexec.py`.
- **Manual verification:** N/A.
- **Regression guard:** the rest of the re-exec tests in the same file.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated.

## As shipped

Test-only change as described in Design; 9 passed in tests/test_agentic_reexec.py.
