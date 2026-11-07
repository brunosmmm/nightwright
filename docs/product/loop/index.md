# How the agentic loop works

The agentic loop takes an **accepted spec** and turns it into **merged, tested code**
without anyone at the keyboard. You queue the work, and the loop opens a branch, implements,
tests, reviews, fixes and merges. It only comes back to you when it needs a decision.

It is built from a few plain parts. Each part does one job, and every step is written down.

## The big picture

```mermaid
flowchart TB
    H(["👤 You"])

    subgraph SES["Sessions — Claude Code or Cursor"]
        direction LR
        C["🎛️ Conductor · /nw-conduct<br/>wakes on events, decides queue actions"]
        ST["🧭 Steward · /nw-steward<br/>knows one project (optional)"]
        C <-- "mailbox" --> ST
    end

    subgraph CORE["nightwright — deterministic core"]
        direction LR
        Q[("📋 Queue<br/>backlog · waiting · slots")] --> R["⚙️ Runner · nw queue run<br/>one tick at a time"]
        R --> LG[("📒 Ledger<br/>every step, every dollar")] --> EV[("🔔 Events<br/>parks · merges · failures")]
    end

    subgraph CAMP["Per campaign — its own branch + worktree"]
        direction LR
        P["🤖 Personas · claude -p / Cursor SDK<br/>implementer · triager · reviewers"]
        G["🧪 Gates<br/>your tests and linters"]
    end

    MAIN[("🌿 main")]

    H -- "nw queue add" --> Q
    R -- "runs" --> P
    R -- "runs" --> G
    R -- "rebase · re-gate · merge" --> MAIN
    EV -- "wakes" --> C
    C -- "resume · park · abandon" --> Q
    C -- "escalates when the table says so" --> H
```

| Part | What it is | What it does |
|---|---|---|
| **Queue** | Campaign state files under `<data_dir>/agentic/` | Holds the backlog (not started), what's waiting for a slot, what's active, what's parked |
| **Runner** | One `nw queue run` process holding a lease | Each *tick*: admit work into free slots, run the next step of every active campaign, record everything |
| **Campaign** | One spec's trip through the loop | Has its own branch and worktree, a state, counters, a cost, and an append-only ledger |
| **Personas** | Fresh headless `claude -p` or Cursor SDK runs | Do the judgment work: implement, triage a failing gate, review. Each run starts with fresh context and a budget. Default is Claude; Cursor is opt-in |
| **Gates** | Shell commands from `.nw/project.yaml` | Your tests and linters, run deterministically in the worktree. Gates are where code meets reality |
| **Event stream** | A projection of every ledger | Turns "campaign parked", "merged", "runner dead" into alerts |
| **Conductor** | A long-lived Claude Code or Cursor session | Wakes on events and decides from a fixed decision table. It never implements anything |
| **Steward** | A project session (optional) | Answers project questions the conductor asks over the mailbox |
| **You** | — | You're asked only when the conductor's table says so |

## One campaign, end to end

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant Q as Queue / Runner
    participant NIGHTWRIGHT as Worktree
    participant I as Implementer
    participant G as Gates
    participant Rv as Reviewer
    participant M as main

    You->>Q: nw queue add SPEC-0042 --yes
    Q->>Q: backlog → admitted when a slot is free
    Q->>NIGHTWRIGHT: create branch + worktree, run setup
    loop until gates pass and review passes (bounded)
        Q->>I: implement (spec + last findings)
        I->>NIGHTWRIGHT: edit, commit
        Q->>G: run gates in the worktree
        alt a gate fails
            Q->>Q: triager decides: fix / retry / waive / escalate
        else gates pass
            Q->>Rv: conformance review vs the spec
            Rv-->>Q: pass or fix (with findings)
        end
    end
    Q->>M: rebase on main, re-run gates, fast-forward merge
    Q-->>You: merged (or parked with a reason, if it got stuck)
```

Everything in that diagram is written to the campaign's ledger as it happens: every state
transition, persona run, gate result, finding and dollar. `nw campaign log <id>` replays it,
and `nw queue watch` shows it live.

![nw queue watch](../assets/captures/cli-queue-watch.svg)

## Where to go next

<div class="grid cards" markdown>

- **[Principles](principles.md)** — why it's built this way, and what it will never do.
- **[The state machine](states.md)** — every state a campaign can be in, and every move
  between them.
- **[Convergence and escalation](convergence.md)** — how loops are kept short, and what
  happens when they aren't.
- **[Operating the loop](operate.md)** — starting it, watching it, and what to do when
  something parks.

</div>

New here? Start with **[Bootstrap from zero](../guides/bootstrap.md)**. To get good specs
into the loop, read **[Ideas to specs](../ideas/index.md)**.
