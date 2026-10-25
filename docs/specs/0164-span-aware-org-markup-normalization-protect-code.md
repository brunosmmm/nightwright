---
id: SPEC-0164
title: "Span-aware org markup normalization (protect code, verbatim, fences)"
status: done
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M4: cli ergonomics"
kind: feature
tags: [ideas, org, explore, ergonomics, bug]
source_idea: IDEA-002
supersedes: [SPEC-0030]
---

## Context

Promoted from idea `IDEA-002`.

[SPEC-0030](./0030-org-native-markup-for-idea-enrichment-bodies.md) added
`src/wt/org_markup.py`: `to_org_body` (Markdown → org, run on every
`wt idea summary|questions|log` write via `explore.py:140/281/624`) and `from_org_body`
(org → Markdown, run on the idea text prefilled into a spec's Context on promote,
`specs.py:113`). Both are flat per-line `re.sub` passes with no notion of protected spans or
emphasis borders, so they corrupt code and paths. Probed on 2026-10-25 (10 of 11 inputs wrong):

| Direction | Input | Output today |
|---|---|---|
| md → org | `=Path(__file__)=` | `=Path(*file*)=` |
| md → org | `` `Path(__file__)` `` | `~Path(*file*)~` |
| md → org | `~__init__~` | `~*init*~` |
| md → org | `snake__case__name` | `snake*case*name` |
| md → org | `plain __init__.py file` | `plain *init*.py file` |
| md → org | `f(*args, **kwargs) and g(**kw)` | `f(*args, *kwargs) and g(*kw)` |
| md → org | fenced block containing `def __init__` | fence dropped, `def *init*(…)` |
| org → md | `a = b = c` | ``a ` b ` c`` |
| org → md | `2*3*4` | `2**3**4` |
| org → md | `path ~/foo and ~/bar~` | ``path `/foo and `/bar~`` |

The `to_org_body` damage is persisted to the ideas org file, not just the display, and it hits
agents that already follow the wt-explore guidance to write org: `=verbatim=` text is still
rewritten. Promoting IDEA-002 to this spec reproduced the org → md defects in its own
Context prefill.

## Goals / Non-goals

**Goals**
- Code and verbatim text survives both conversions byte-for-byte: org `~code~` / `=verbatim=`,
  Markdown backtick spans, and fenced blocks.
- Emphasis conversion fires only where the source dialect would actually render emphasis:
  CommonMark-style flanking for `**x**` / `__x__`, org emphasis border rules for `*x*`,
  `~x~`, `=x=`.
- Fenced blocks become org source blocks (`#+begin_src LANG` … `#+end_src`) instead of being
  unwrapped, and convert back to fences on promote, so code keeps its structure both ways.
- Everything else SPEC-0030 decided stays as it is (carried forward below).

**Non-goals**
- Full Markdown ↔ org fidelity (still only the common agent idioms).
- Repairing ideas already corrupted. A stored `*file*` cannot be told apart from intended bold,
  so automated repair is unsafe.
- A `--raw` / `--no-markup` CLI flag. Protected spans are the escape hatch: anything in
  `=…=`, `~…~` or backticks is left untouched.
- Faithfully copying org's quirks for tilde-led paths. In org, `~/foo and ~/bar~` *is* a code
  span, so converting it to one stays correct. The guidance documents quoting paths as
  `=~/foo=` instead.
- Changing the question-headline path (`_items_to_question_body` normalizes item text only;
  SPEC-0054/0111) or the Context prefill's structure.

## Decision

Replace the flat regex passes in `org_markup.py` with a **mask → rewrite → unmask**
normalizer. Block level first: fenced blocks (md) and `#+begin_src`/`#+begin_example`
blocks (org) are cut out verbatim and converted between the two forms. Then within each
remaining line, protected inline spans are masked before any emphasis rewrite, and each
emphasis rewrite checks the source dialect's border rules. This spec **supersedes SPEC-0030**
and restates its unchanged decisions: normalize on write, org → md only for the promote
Context prefill, org-style `** Summary` labels in `wt idea show`, and skills/docs that tell
agents to write org.

## Design

### Carried forward from SPEC-0030 (unchanged)

- Call sites: `set_summary`, `set_questions`/`add_question` (per item text), `append_log` →
  `to_org_body`; promote Context prefill → `from_org_body`.
- ATX heading hashes at line start are stripped, outside blocks only.
- Normalization is idempotent on org text, and unknown prose is left alone.
- `wt idea show` org-style section labels.

### `to_org_body(text)` (Markdown → org)

1. **Blocks.** Scan lines for a fenced block (a line ```` ```LANG ```` through the next line
   that is exactly ```` ``` ````, optionally indented ≤3). Emit `#+begin_src LANG` (or
   `#+begin_example` when LANG is empty), the content lines **verbatim**, then `#+end_src` /
   `#+end_example`. Existing org `#+begin_*`…`#+end_*` blocks pass through verbatim. An
   unterminated fence is treated as prose, as today.
   *Added during implementation:* content lines are **org comma-escaped** like Org's
   `org-escape-code-in-string`. A line whose first non-blank text is `*` or `#+` (after any
   commas) gets one more leading `,`. Without this, a pasted `* foo` line would become a real
   headline and split the idea tree, and a pasted `#+TODO:` line would be read as a file
   keyword (`explore.py:161` scans for those).
2. **Inline masking**, per prose line, left to right:
   - Markdown backtick span `` `x` `` becomes org `~x~`. If `x` contains `~`, use `=x=`. If it
     contains both `~` and `=`, leave the backticks as-is. The produced span is masked.
   - Existing org `~x~` / `=x=` spans that satisfy org borders (below) are masked as-is.
   Masked spans are swapped for opaque placeholders. The placeholder alphabet must not
   contain `*`, `_`, `~`, `=` or backticks, so no later rewrite can touch them.
3. **Emphasis** on the unmasked remainder:
   - `**x**` becomes `*x*` only when the opener is left-flanking (followed by
     non-whitespace) and the closer is right-flanking (preceded by non-whitespace and not
     followed by an alphanumeric). That fixes `f(*args, **kwargs) and g(**kw)`.
   - `__x__` becomes `*x*` only with CommonMark underscore rules: the opener is not preceded
     by an alphanumeric, the closer is not followed by an alphanumeric, and `x` has no
     leading or trailing whitespace. That fixes `snake__case__name`. Dunders standing alone
     as words (`__init__.py`, `__file__`) are still bold in CommonMark, so the rule alone does
     not save them. Agents must quote identifiers, and wt-explore says so. Unquoted, a bare
     dunder still converts the way CommonMark would render it, which is documented.
4. Unmask.

### `from_org_body(text)` (org → Markdown)

1. **Blocks.** `#+begin_src LANG` … `#+end_src` (case-insensitive) becomes a ```` ```LANG ````
   fence. `#+begin_example` … `#+end_example` becomes a bare fence. Content is verbatim apart
   from removing one level of comma escape (the inverse of the escape above).
2. Org headlines pass through untouched (as today).
3. **Org emphasis border rules** (Org manual, "Emphasis and Monospace") for `~x~`, `=x=`,
   `*x*`:
   - The opening marker sits at line start or after whitespace or one of `-('"{`.
   - The opening marker is followed by non-whitespace, and the closing marker is preceded by
     non-whitespace.
   - The closing marker is followed by line end, whitespace, or one of `-.,;:!?')"}[\`.
   - `~x~` / `=x=` become `` `x` `` and are then masked. `*x*` becomes `**x**`. Code spans are
     converted first, so `*` inside code is never treated as emphasis.
   That fixes `a = b = c` (the opener is followed by whitespace) and `2*3*4` (the opener
   follows a digit).

### Shared implementation

One small tokenizer helper in `org_markup.py` handles masking and placeholders for both
directions. No new dependency. Module docstring updated to cite SPEC-0164.

### Skills / docs

- `skills/wt-explore/SKILL.md`, `skills/wt-seed/SKILL.md`: the existing org-markup line gains
  "quote identifiers, paths and code in `~…~` / `=…=` (left untouched); write multi-line code as
  `#+begin_src`".
- `docs/WORKFLOW.md` explore note: same one-liner.

## Alternatives considered

- **Patch individual regexes** (lookarounds on `__`, skip `=…=`) — rejected. Each fix opens
  a new edge case. Without masking, any rewrite can still reach into code.
- **Keep unwrapping fences, just protect their content** — rejected. It loses the block
  boundary, and the org → md path cannot rebuild a fence for the spec Context.
  `#+begin_src` is native org and round-trips.
- **`--raw` / `--no-markup` flag** — rejected for now. Protected spans already give an
  in-band escape, and a flag adds CLI surface on four commands.
- **Pandoc / orgparse round-trip** — rejected. Pandoc is a heavy external dependency, and
  orgparse does not model inline markup.
- **Drop normalization entirely** — rejected. SPEC-0030's reason still holds: agents paste
  Markdown.

## Acceptance criteria

- [x] Every row of the Context table converts correctly: md → org rows leave code/verbatim
      content byte-identical and do not bold intraword `__` or a lone `**kwargs`. Org → md rows
      produce no spurious code spans or bold for `a = b = c` and `2*3*4`.
- [x] A Markdown fenced block becomes `#+begin_src LANG`/`#+end_src` (or `#+begin_example`)
      with content byte-identical, and `from_org_body` turns it back into the same fence.
- [x] `from_org_body(to_org_body(md))` returns `md` unchanged for backtick code spans and
      fenced blocks.
- [x] `to_org_body` is idempotent on its own output (corpus of all test inputs).
- [x] SPEC-0030 happy paths still hold (`tests/test_org_markup.py` existing cases, updated
      only where they pin fence unwrapping).
- [x] `wt idea summary <ID> --set` with `=Path(__file__)=` persists that text byte-for-byte
      in the ideas org file.
- [x] Skills + WORKFLOW carry the quote-your-code line.
- [x] SPEC-0030 set `status: superseded`, `superseded_by: SPEC-0164`.
- [x] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:**
  - `tests/test_org_markup.py`: one parametrized table per direction, covering every Context
    row plus the existing happy paths (backticks → `~`, `**bold**`, `__bold__` as a
    standalone word, ATX strip, verbatim → backtick). Add fence ↔ src-block cases with and
    without a LANG, an unterminated fence, `~`-containing backtick content (falls back to
    `=`), a round-trip property over the corpus, and idempotence over the corpus.
  - `tests/test_explore.py`: integration through the CLI writer against a tmp ideas file
    (config fixture with tmp `org_ideas_file`). Summary with `=Path(__file__)=` and a
    fenced block persists `=Path(__file__)=` and `#+begin_src python`; a log note with
    `` `snake__case__name` `` persists `~snake__case__name~`.
  - `tests/test_questions_state.py`: existing migration regression (no normalization when
    `apply_markup=False`) unchanged and still passing.
- **Manual verification:** re-run the IDEA-002 probe script (all 11 inputs) and note the
  outputs in this spec. On a scratch config, run `wt idea summary` with a dunder and
  `wt spec new --from-idea` into a tmp `specs_dir`, then check the Context prefill has
  correct backticks and fences.
- **Regression guard:** full `uv run pytest`. `tests/test_org_markup.py` happy-path cases kept.

### Test record (2026-10-25)

- **Automated:** `tests/test_org_markup.py` has 53 tests: both tables (every Context row plus
  the SPEC-0030 happy paths), fence ↔ src-block conversion with comma escape, unterminated
  fence, org-block passthrough, `#+BEGIN_SRC` unescape, round trip, idempotence. Two new
  `tests/test_explore.py` integration tests cover the CLI write path (a dunder in
  `=…=`, a fenced block with a `* ` line that does not become a headline, a backtick
  snake-case log note, and a second write after that) and the promote Context
  (`a = b = c`, `2*3*4`, src block back to a fence). `tests/test_questions_state.py`
  passes unchanged.
- **Manual:** the IDEA-002 probe re-run gave the expected output for all 11 inputs: 9
  fixed, and 2 documented remainders (bare `__init__.py` still bolds; `~/foo and ~/bar~` is
  one org code span). A scratch-config CLI run (`wt idea summary` then
  `wt spec new --from-idea --internal` into a tmp `specs_dir`) stored
  `=Path(__file__)=`, `~snake__case__name~` and an escaped `#+begin_src python` block.
  The Context prefill came back as `` `Path(__file__)` ``, `a = b = c; 2*3*4.` and a
  ```` ```python ```` fence with `* star` unescaped.
- **Full suite:** at first, 15 failures that were already on clean HEAD `740e9eb` (none
  new). They were repaired by [SPEC-0165](./0165-repair-test-drift-from-demo-name-scrub-and-spec.md).
  After that, `uv run pytest` gives **1141 passed, 82 skipped, 0 failed**.

## Clock Log

Internal work: clock on IDEA-002 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

1. Implement the normalizer and tests; update skills/WORKFLOW.
2. Mark SPEC-0030 superseded; regenerate ledger.
No data migration. Already-corrupted idea text stays as-is (see Non-goals) and is fixed by
hand if someone notices it.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
