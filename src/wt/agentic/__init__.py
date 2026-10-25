"""Agentic workflow (SPEC-0169): a deterministic queue moves accepted specs through
implementation, gates, triage and merge; judgment steps run as fresh headless Claude personas;
everything is logged to per-campaign ledgers under `<data_dir>/agentic/`.

Package layout (one module per wave-1 child spec):
  project.py   .wt/project.yaml loader                     (SPEC-0170)
  profiles.py  workflow profiles (models, budgets, loops)  (SPEC-0171)
  store.py     campaign state + append-only ledger          (SPEC-0171)
  lease.py     singleton runner lease                       (SPEC-0171)
  gitops.py    worktree / branch / rebase / merge / gh      (SPEC-0172)
  gates.py     deterministic gate executor                  (SPEC-0173)
  runner.py    headless `claude -p` + FakeRunner            (SPEC-0174)
  personas.py  prompt assembly + verdict schemas            (SPEC-0174)
  close.py     rebase → re-gate → ff-only merge / PR        (SPEC-0175)
  queue.py     state machine, tick / run                    (SPEC-0176)
  cli*.py      click surface: `wt queue`, `wt campaign`, `wt projects config|init`

Invariant: Python decides every transition; agents only produce commits and one structured
verdict. Nothing in this package imports textual or talks to the network except the runner's
`claude` subprocess."""
