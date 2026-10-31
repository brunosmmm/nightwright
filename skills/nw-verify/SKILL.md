---
name: nw-verify
description: "Post-hoc Definition-of-Done audit after implementation — any repo. Internal SPEC-NNNN (nightwright checkout): nw spec verify + ledger. Outbound portable PROJ-NNNN (target repo): audit the portable file in place. Use whenever closing a loop or checking a claimed-done spec."
trigger: verifying a spec was implemented correctly; close the loop; before status done; EXAMPLE-/SPEC- done-check
---

# nw-verify

## When to use

You need to **verify** that a governing spec was implemented honestly (Acceptance criteria,
Test plan, status) — in **any** checkout. This skill is installed globally (`nw skills
install`); invoke it wherever the work lives.

**Not** for authxxing (`/nw-new-work`), generating org tasks (`/nw-generate`), or exporting
(`/nw-export`). For *building* an outbound portable from scratch, prefer `/nw-implement-spec`
first; come back here for the post-hoc audit (or run Verify as the last step of that flow).

## Branch (pick one)

| Context | How to recognize | Path |
|---------|------------------|------|
| **Internal** | Id is `SPEC-NNNN`; you are in the **nightwright** repo (or a checkout with `docs/specs/SPEC-*` + `AGENTS.md` ledger) | **A — Internal** |
| **Outbound portable** | Id is `EXAMPLE-NNNN` / `PFW-…` / other `PROJ-NNNN`; portable markdown under `docs/specs/` (often + `.contract.md` / `source_outbound_id`) in a **target** repo | **B — Portable** |

Do **not** treat “skill is global” as “always run `nw spec verify`.” That CLI is
**internal-only**. Outbound verification is file-based in the target repo (no nightwright ledger).

## Procedure A — Internal (`SPEC-NNNN`)

1. Resolve `SPEC-NNNN` in the nightwright checkout.
2. Mechanical audit: `nw spec verify <SPEC-ID>`. Flags: `nw spec verify --help`
   (`--json` → `nw.spec.verify.v1`, `--strict` → non-zero on mechanical fail). Read-only.
3. Run remaining Test plan work (`uv run pytest` + named manual steps).
4. Attest judgment (from the report / `AGENTS.md` DoD): AC actually true; body matches
   shipped; ledger current (`python3 tools/spec_lint.py`, `--write-ledger` if status changed).
5. Only then set `status: done`, check true AC boxes, re-run `nw spec verify <ID> --strict`.
6. Clock out on the linked idea if needed: `nw idea clock-out --help`.

## Procedure B — Outbound portable (`EXAMPLE-0024`, …)

Run **in the target repo** that holds the portable file. Do **not** call `nw spec verify`
(it refuses outbound ids by design).

1. Resolve `docs/specs/<OUTBOUND-ID>-*.md` (+ `.contract.md` if present).
2. Mechanical read of the portable:
   - `status` is `done` (or explain why not).
   - Every Acceptance criteria bullet is `- [x]` (list stragglers).
   - Test plan non-empty / not a template placeholder.
   - Open questions cleared or explicitly deferred.
3. Re-run automated Test plan commands here; confirm the suite is green; note manual steps.
4. Judgment: each checked AC is *actually* true; Design matches shipped (edit the portable
   if drifted); outcome-shaped AC or `outcome:` present (SPEC-0048); no dangling
   `CLOCK-IN` without `CLOCK-OUT` in `## Clock Log`.
5. Report pass/fail. No nightwright ledger in this repo. Optionally remind the nightwright authxx:
   `nw spec pull-status` / `nw spec fit-log` (`nw <cmd> --help` from the nightwright side).

## Conventions / guardrails

- **One skill, two backends.** Global playbook; CLI only on branch A.
- Read-only audit — never auto-mark `done` for the human.
- `ok: true` from the CLI does not skip judgment.
- Thinness (SPEC-0017): defer flag lists to `--help`.
- **SPEC-0138:** Superpowers “verification-before-completion” (pytest green, etc.) is
  **not** a substitute for this skill. Close the loop here before treating work as done.

## Verify

**A:** `nw spec verify <ID> --strict` exits 0, judgment attested, `spec_lint` 0, status `done`
if closing. **B:** portable `status: done`, AC/Test plan/clock honest, suite green.
