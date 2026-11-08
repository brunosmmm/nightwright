# Write a spec the loop can build

A spec is the contract between you and the loop. The implementer builds against it, the
reviewer checks against it, and the close step refuses to merge if it changed along the way.
A good spec is short, decided and checkable.

## From idea to spec

```mermaid
flowchart LR
    I["Explored idea<br/>IDEA-042"] -- "nw spec new --from-idea IDEA-042" --> D["Draft spec<br/>internal: docs/specs/NNNN-….md<br/>outbound: data-dir outbox/PROJ-NNNN<br/>prefilled from the idea"]
    D -- "/nw-new-work<br/>fill decision · design · ACs · test plan" --> A["Accepted spec<br/>lint-clean"]
    A -- "nw spec review" --> R{"Reviewer"}
    R -- "revise / split" --> A
    R -- approve --> Q["nw queue add SPEC-NNNN --yes"]
```

`nw spec new --from-idea` scaffolds the spec from the template, prefills Context from the
idea, links the two (the spec's `source_idea`, the idea's `:SPEC:`), and moves the idea to
SPECCED. An idea with no project, the built-in project Meta-Tools, or `--internal` writes
`docs/specs/NNNN-….md`. An idea whose `:PROJECT:` is an `outbox_targets` key writes
`<data_dir>/outbox/<project>/PROJ-NNNN-….md` instead, and `nw queue add` of that id waits
for `nw spec export`. `/nw-new-work` guides an agent through finishing it and getting it to
`accepted`.

## Anatomy of a good spec

```mermaid
flowchart TB
    C["<b>Context</b> — the problem and today's behaviour"] --> G["<b>Goals / Non-goals</b> — what's in, what's explicitly out"]
    G --> DE["<b>Decision</b> — the chosen approach, in a few sentences"]
    DE --> DS["<b>Design</b> — modules, data shapes, interfaces, failure paths"]
    DS --> AC["<b>Acceptance criteria</b> — objectively checkable statements"]
    AC --> TP["<b>Test plan</b> — how each AC is proven"]
    AC -. "the reviewer and the<br/>implementer read these".-> L(["the loop"])
    TP -. "the gates prove these".-> L
```

| Section | The loop uses it for | Make it good by |
|---|---|---|
| Goals / **Non-goals** | Knowing what *not* to build | Naming tempting extras explicitly |
| Decision | Picking one approach instead of exploring | Stating it as a decision, not a discussion |
| Design | Concrete targets: files, functions, shapes | Naming real code paths; specifying failure cases, not just the happy path |
| **Acceptance criteria** | The implementer's checklist and the reviewer's yardstick | Each one checkable by a test or a command; no "should be nice" |
| **Test plan** | What the gates must prove | One test, or more, per AC; named test files |

### Acceptance criteria that work (examples)

| Weak | Strong |
|---|---|
| "The dashboard shows reviews nicely." | "`nw queue list` shows `SPEC_REVIEW r2` for a campaign in its second review round." |
| "Handle errors." | "An empty title exits non-zero with the message `empty title`, and writes nothing." |
| "Fast enough." | "`nw ideas` on 2,000 ideas returns in under 1 s (test with the fixture in `tests/…`)." |

## One campaign, or an epic?

A spec should be one campaign's worth of work: roughly up to ten acceptance criteria, in one
subsystem. If it's bigger, the reviewer will answer `split`, and it's better to split
deliberately:

```mermaid
flowchart TB
    E["Epic spec<br/><code>nw spec new --from-idea … --epic</code><br/>architecture + breakdown"] --> C1["Child spec A<br/>parent: SPEC-E"]
    E --> C2["Child spec B<br/>depends_on: A"]
    E --> C3["Child spec C<br/>depends_on: B"]
    C1 & C2 & C3 --> Q[("queue — admitted in<br/>dependency order")]
```

`depends_on` matters: the queue admits a spec only once everything it depends on is done on
main. `nw spec seed-children` turns an epic's breakdown into ideas, and `/nw-rework` extends
an incomplete epic or supersedes a design that turned out wrong.

## Review, then queue

```bash
nw spec review SPEC-0042        # one review: approve | revise | split
nw spec review SPEC-0042 --show # stored verdict; a bare second run errors
nw queue add SPEC-0042 --yes    # admits approve, or a revise/split whose text changed
```

An approving review of the current text admits the spec. A `revise` or `split` whose text
you then edit is also admitted, with no second review. An unchanged `revise` is admitted
only for an internal spec: the writer amends it. An unchanged outbound `revise` is refused
until you edit. `--force` on `nw spec review` starts another review. `--force` on
`nw queue add` skips the check. The verdict is stored under the data dir at
`agentic/reviews/<SPEC-ID>/<sha256>.json`, not next to the spec file.

The reviewer checks four things:
- consistency: every AC follows from the design, and every design promise has an AC;
- testability;
- fit to the source idea;
- size.

It returns findings by severity: must_fix, major, minor and nit. Fix the must_fix and major
ones. Editing the text after a `revise` is enough to queue it. Another review is
`--force`, not the default. If the same spec keeps coming back with new findings, stop and
narrow or rework it (see
[Convergence](../loop/convergence.md#persistent-divergence-means-rework)).

## Changing your mind

Never silently rewrite an accepted spec. Write a new one that `supersedes` it, and mark the
old one `superseded`. `/nw-rework` does this. The ledger of specs then stays a faithful
history of what was decided and why.

**Next:** [How the agentic loop works](../loop/index.md).
