---
id: SPEC-0174
title: "Headless persona runner, prompt assembly and verdict schemas"
status: accepted
owner: user
created: 2026-10-25
updated: 2026-10-25
milestone: "M9: agentic workflow"
kind: feature
parent: SPEC-0169
tags: [agentic, personas, headless, claude]
source_idea: IDEA-021
depends_on: [SPEC-0170, SPEC-0171]
---

## Context

Child of epic [SPEC-0169](./0169-mini-agentic-workflow-queue-campaigns-headless.md)
(promoted from `IDEA-021`).

The judgment steps of the loop (implement; triage a gate failure; later, review) run as
fresh headless Claude sub-agents. wt has one headless call today, `meetings.refresh_meetings`
(`meetings.py:93-117`): `claude -p … --output-format stream-json`, no timeout, exit code
unchecked, cost unread. The loop needs the opposite: a deterministic prompt, a model, an
effort, a dollar budget, a wall-clock timeout, an explicit permission mode, a structured
verdict, and the cost of every run recorded. It also needs an offline stand-in so the
whole loop is testable without the network.

Verified on this machine (claude 2.1.310): `-p` supports `--output-format json`,
`--json-schema`, `--model`, `--effort`, `--max-budget-usd`, `--permission-mode`,
`--permission-prompts none`, `--restricted`, `--add-dir`, `--no-session-persistence`,
`--append-system-prompt`, `--allowedTools`; there is no `--max-turns`.

## Goals / Non-goals

**Goals**
- `RunRequest` / `RunResult` dataclasses and a `PersonaRunner` protocol.
- `ClaudeRunner`: builds the argv above, passes the prompt on stdin, enforces the timeout,
  parses the json result (structured output, cost, usage, session id, error flag), saves
  prompt and raw result under the campaign's `runs/` directory.
- `FakeRunner`: consumes a JSON script of scripted edits and canned verdicts, applies the
  edits inside the worktree, returns `RunResult`s; selected by config or env.
- Persona templates for `implementer` and `gate_triager`, a deterministic
  `build_prompt`, verdict JSON schemas, and `build_request(...)` that turns profile +
  project config + campaign state into a `RunRequest`.
- Hidden `wt campaign prompt CAMPAIGN PERSONA` to print the assembled prompt.

**Non-goals**
- Deciding when to run a persona or what to do with its verdict (SPEC-0176).
- Committing (the queue commits via SPEC-0172 after a run).
- Reviewer personas (later wave) beyond leaving `PERSONAS` extensible.

## Decision

Add `src/wt/agentic/runner.py` and `src/wt/agentic/personas.py` plus
`src/wt/agentic/personas/implementer.md` and `gate_triager.md` (shipped in the wheel; the
hatch target includes non-Python files under `src/wt`). The prompt is assembled by regex
placeholder substitution over the persona markdown (never `str.format`; spec text contains
braces) and contains no timestamps, so identical inputs give byte-identical prompts. The
runner is the only place that knows `claude`'s command line.

## Design

### Dataclasses

```python
@dataclass(frozen=True)
class RunRequest:
    persona: str; prompt: str; system_append: str; cwd: Path; model: str; effort: str | None
    max_budget_usd: float; timeout_s: int; permission_mode: str; read_only: bool
    allowed_tools: tuple[str, ...]; add_dirs: tuple[Path, ...]; json_schema: dict
    env: dict[str, str]; run_dir: Path

@dataclass(frozen=True)
class RunResult:
    exit_code: int; timed_out: bool; interrupted: bool; duration_s: float; is_error: bool
    cost_usd: float | None; usage: dict; session_id: str | None
    verdict: dict | None; verdict_ok: bool; result_text: str; raw_path: Path
```

### `ClaudeRunner.run(req)`

argv: `claude -p --output-format json --json-schema <json.dumps(req.json_schema)>
--model <model> [--effort <effort>] --max-budget-usd <B> --permission-mode <PM>
--permission-prompts none --no-session-persistence --add-dir <each add_dir>
--append-system-prompt <system_append> [--allowedTools <each>] [--restricted when
read_only]`. Prompt via `input=req.prompt`; `cwd=req.cwd`; `env=os.environ | req.env`;
`subprocess.run(timeout=req.timeout_s)`. `TimeoutExpired` ⇒ terminate the child, then kill,
`timed_out=True`, `exit_code=None→-1`. Raw stdout is saved to `run_dir/result.json`,
stderr to `stderr.log`, the prompt to `prompt.md` before the call. Parsing: the stdout is a
single JSON object; `verdict = obj.get("structured_output")`, falling back to
`json.loads(obj.get("result", ""))`; `verdict_ok` = the verdict is a dict and validates
against `req.json_schema` with a small local `_conforms()` (required keys + enum values;
no `jsonschema` dependency at runtime); `cost_usd = obj.get("total_cost_usd")`; `usage`;
`session_id`; `is_error = obj.get("is_error", exit_code != 0)`. **The field names are
confirmed against a real `claude -p --output-format json` run during implementation and
recorded in this spec's test record.** Unparseable stdout ⇒ `verdict=None`,
`is_error=True`, `result_text` = raw tail.

### `FakeRunner`

Selected when `cfg["agentic"]["runner"] == "fake"` or `WT_AGENTIC_RUNNER=fake`; script
path from `cfg["agentic"]["fake_script"]` or `WT_AGENTIC_FAKE_SCRIPT`. The script is a JSON
list consumed in order; each entry `{persona, edits: [{path, write|append|delete}],
verdict, cost_usd, exit_code?, timed_out?, malformed?, sleep_s?}`. Edits apply relative to
`req.cwd`; `malformed` returns a non-conforming verdict; a persona mismatch raises so a test
never silently runs the wrong step. The prompt is still built and saved, so prompt assembly
is exercised by every end-to-end test. A cursor file in `run_dir.parent` makes the script
advance across separate `wt` processes.

`select_runner(cfg) -> PersonaRunner`.

### Personas (`personas.py`)

- `PERSONAS = {"implementer": Persona(template, schema, read_only=False),
  "gate_triager": Persona(template, schema, read_only=True)}`.
- `build_prompt(persona, ctx) -> str`: placeholders `{{campaign}} {{spec_id}}
  {{spec_rel_path}} {{spec_text}} {{worktree}} {{base}} {{loop}} {{loop_max}}
  {{context_files}} {{gate_results}} {{pending_brief}} {{previous_verdicts}}`; `spec_text`
  is read from the **worktree**; `gate_results` renders name / required / result / tail
  (≤ 60 lines each); `previous_verdicts` the last two summaries; a missing value renders as
  `(none)`.
- `INVARIANT` text (also used as `system_append`): one run of wt's queue for campaign X;
  work only inside the worktree; do not commit (the runner commits); do not switch
  branches or touch the main checkout; `wt campaign note` is available for notes; finish by
  emitting the JSON verdict, which a program parses.
- `implementer.md`: read the context files first; set the spec `status: in-progress`;
  implement to Decision/Design with the smallest change that meets the ACs; run the gate
  commands yourself; tick only true ACs; set `done` only when honest; regenerate whatever
  the repo's own rules require (for wt: `python3 tools/spec_lint.py --write-ledger`); report
  `partial`/`blocked` truthfully.
- `gate_triager.md`: read the gate logs and the diff; decide `fix` (with a concrete
  `fix_brief` and `must_fix` items), `retry_gate` (only when `suspected_flaky`), `waive`
  (only for optional gates), or `escalate`; never edit files.
- Verdict schemas (JSON Schema objects): implementer `{status: done|partial|blocked,
  summary, changed_files[], tests_run[], ac_status[{ac, met}], blockers[],
  followup_ideas[]}` (required `status, summary`); gate_triager `{disposition:
  fix|retry_gate|waive|escalate, confidence: low|medium|high, suspected_flaky,
  must_fix[{gate, title, detail, files[]}], waived[{gate, reason}], fix_brief,
  followup_ideas[]}` (required `disposition, fix_brief`).
- `build_request(cfg, profile, project_cfg, state, persona, *, run_dir) -> RunRequest`:
  model/effort/budget/permission from `profiles.persona_settings`; `timeout_s =
  profile["run_timeout_s"]`; `add_dirs = (worktree,)`; `allowed_tools` = project
  `agents.allowed_tools` plus `Bash(<each gate cmd>*)` for the implementer; `env` =
  `WT_CAMPAIGN`, `WT_DATA_DIR`, `WT_CONFIG_DIR`, `WT_SPEC_ID`, `WT_WORKTREE`, `WT_BASE`.

### CLI

`wt campaign prompt CAMPAIGN PERSONA` (hidden): prints `build_prompt` for the campaign's
current state. `PERSONA` is a `click.Choice`.

## Alternatives considered

- **`stream-json` and harvest tool results** (the meetings approach) — rejected. The loop
  needs one verdict and one cost figure, which the single-result `json` format gives
  directly, and `--json-schema` validates the verdict shape.
- **Resume a session for the fix loop** — rejected. Fresh context per run is the plan's
  rule (no context rot, deterministic prompts) and the brief carries what the next run
  needs.
- **Agents log verdicts via `wt campaign …` themselves** — rejected as the primary path. The
  runner logs the structured output deterministically; `wt campaign note` stays available
  for extra notes.
- **`str.format` templates** — rejected. Spec bodies contain braces.

## Acceptance criteria

- [ ] `ClaudeRunner` passes exactly the documented flags (asserted on a monkeypatched
      `subprocess.run`), the prompt on stdin, `cwd` = worktree, env with the six `WT_*`
      variables, `--restricted` only for read-only personas.
- [ ] A canned result with `structured_output` yields `verdict_ok`, `cost_usd`, `usage`,
      `session_id`; a result whose `structured_output` is absent falls back to parsing
      `result`; invalid JSON ⇒ `verdict=None`, `is_error=True`; a conforming-but-wrong
      enum value ⇒ `verdict_ok=False`; `TimeoutExpired` ⇒ `timed_out=True`; a missing
      `total_cost_usd` ⇒ `cost_usd=None`.
- [ ] `prompt.md` and `result.json` exist under `run_dir` after a run.
- [ ] `build_prompt` is byte-identical across two calls with the same inputs and contains
      the spec text, worktree, gate tails and pending brief; missing values render as
      `(none)`.
- [ ] `build_request` for the implementer carries `bypassPermissions` by default, the
      project's `allowed_tools` plus gate commands, and the profile budget; for the triager
      `read_only=True` and the triager budget.
- [ ] `FakeRunner` applies edits in `cwd`, returns verdicts in script order, raises on a
      persona mismatch, advances across processes, and honors `malformed`, `exit_code`,
      `timed_out`.
- [ ] `select_runner` picks the fake via config or env, otherwise `ClaudeRunner`.
- [ ] A real `claude -p --output-format json --json-schema …` run on this machine confirms
      the parsed field names (recorded in the test record with its cost).
- [ ] Full `uv run pytest` green; `python3 tools/spec_lint.py` exits 0.

## Test plan

- **Automated tests:** `tests/test_agentic_runner.py` (argv assertion, parsing matrix,
  timeout, file outputs, `FakeRunner` script semantics, `select_runner`);
  `tests/test_agentic_personas.py` (determinism, content, `build_request` settings per
  persona, schemas list the documented required keys and enums, the two markdown
  templates contain their placeholders).
- **Manual verification:** one real run of the gate-triager persona with a tiny budget
  (`--max-budget-usd 0.2`) against a scratch campaign: confirm the json field names, that
  `--restricted` prevents edits, and record cost and session id in this spec.
- **Regression guard:** full `uv run pytest`; `tests/test_mcp_deferred.py` still passes
  (no "mcp" in pyproject).

## Clock Log

Internal work: clock on IDEA-021 with `wt idea clock-in` / `clock-out`.

## Rollout / migration

Library plus a hidden command. Nothing spawns `claude` unattended until SPEC-0176.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; automated tests pass (`uv run pytest`); manual steps performed.
- [ ] No regressions.
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` updated (status + date).

## Open questions

_None — decisions locked above._
