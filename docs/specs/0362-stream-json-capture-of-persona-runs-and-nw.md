---
id: SPEC-0362
title: "Stream-json capture of persona runs and nw campaign tail viewer"
status: done
owner: user
created: 2026-11-01
updated: 2026-11-01
kind: feature
source_idea: IDEA-654
parent: SPEC-0360
tags: [agentic, observability, runner]
---

## Context

Promoted from idea `IDEA-654`. Second child of epic
[SPEC-0360](./0360-look-inside-running-agents-herdr-status-reporting.md), which governs the
design: its Decision 2 and cross-cutting invariant 9 (whether a run streams is fixed at launch
by `"stream": true` in `launch.json`; adoption and `collect` follow the run's own record, never
the current code's default). Independent of the herdr reporter
([SPEC-0361](./0361-herdr-reporter-nw-herdr-watch-pushes-queue-tokens.md)); does not touch herdr.

What exists today (checked 2026-11-01):

- `runner.build_argv` (`src/nightwright/agentic/runner.py:153`) runs `claude -p
  --output-format json … --no-session-persistence`. `ClaudeRunner.run` writes `request.json`
  and `launch.json` (`{argv, cwd, env}`) and starts the detached wrapper
  `python -m nightwright.agentic.runproc <run_dir>` (SPEC-0225).
- The wrapper (`runproc.main`, ~line 265) opens `result.json` as the child's stdout. The file
  holds one JSON object, written by `claude` when it exits. While the run is going, nothing is
  on disk but an empty `result.json` and `stderr.log`.
- `runner.collect` (finished or vanished run) and `ClaudeRunner._orphan_duties` (wrapper died,
  `claude` kept going; synthesizes `exit.json`) read `result.json` and call `parse_result`,
  which takes one JSON document (fenced or not) or, defensively, a JSON *array* of events. Not
  JSONL.
- `run.json` (`nw.run.v1`, SPEC-0192) `files` is `{prompt, raw, stderr}`.
  `runs.campaign_run` (`nw campaign run`) lists paths, the stderr tail and
  `permission_denials` read from `result.json`.
- `conductor_cost` has the configured price table (`agentic.conductor.prices`, per million
  tokens; no built-in table ships) with `price_for(cfg, model)` / `turn_usd(turn, price)`.
- `claude` 2.1.x: `--output-format stream-json` in `-p` mode requires `--verbose`. Each stdout
  line is one event: `system` (subtype `init`: model, tools, session id), `assistant` (one
  line per content block: `text`, `thinking`, `tool_use`; `message.id`, `message.model` and
  `message.usage` repeated on each), `user` (`tool_result` blocks), and a last `result` event
  whose fields are exactly the `--output-format json` object (`type: result`, `subtype`,
  `is_error`, `result`, `session_id`, `total_cost_usd`, `usage`, `structured_output`,
  `permission_denials`, …). Verified on two real samples captured 2026-11-01 with claude
  2.1.316 (saved as `tests/fixtures/stream_json/*.jsonl`, paths sanitized): the stream also
  carries `system` `hook_started` / `hook_response` events (the human's SessionStart hooks),
  `rate_limit_event` lines, `user` events with a `tool_use_result` copy, and with
  `--json-schema` the verdict arrives as a `StructuredOutput` `tool_use` before the `result`
  event (whose `structured_output` holds it). The result event carries `result_index`, so
  more than one result event is possible; the last one wins, as in json mode.

## Goals / Non-goals

**Goals**
- Every new persona run writes its full event stream to `runs/<run>/stream.jsonl` while it
  runs.
- Verdict, cost, usage, session id, error flags and budget handling get exactly the same
  input as today: `result.json` still holds the final result object.
- Runs launched before this ships (no `stream` in `launch.json`) are adopted, collected and
  displayed exactly as before.
- `nw campaign tail CAMPAIGN [--run RUN] [--follow] [--raw]`: a readable transcript of one run
  (tool calls with a short result summary, assistant text, running cost vs budget), live or
  after the fact, in any terminal.

**Non-goals**
- herdr panes (child 3), OSC 7501 (child 4).
- Persisting Claude Code sessions (`--no-session-persistence` stays).
- Per-run difficulty features from the stream (IDEA-423).
- Following a whole campaign across runs (the viewer follows one run and stops when it ends;
  child 3 opens one viewer per run).
- Retention/GC of `stream.jsonl` beyond what removes the run directory today.

## Decision

Persona runs use `--output-format stream-json --verbose`, recorded as `"stream": true` in
`launch.json`. The wrapper points the child's stdout at `stream.jsonl` (a plain file, as
today with `result.json`) and, after the child exits and before it writes `exit.json`,
copies the last `result` event line to `result.json`. Readers of a run go through one helper
that honours the run's own `stream` flag, so a pre-upgrade run is read exactly as before. A
kill switch `agentic.stream_runs: false` restores the json mode for new runs. A new module
`agentic/tail.py` renders a stream; `nw campaign tail` is its CLI.

## Design

### Launch — `runner.py`

- `build_argv(req, stream=True)`: `stream` → `["claude", "-p", "--output-format",
  "stream-json", "--verbose", …]`; `stream=False` → today's argv unchanged. Everything else
  (including `--no-session-persistence`) is identical.
- `ClaudeRunner(spawn_timeout_s=30.0, stream=True)`; `select_runner(cfg)` passes
  `stream=bool(agentic.get("stream_runs", True))`. `launch.json` gains `"stream": true` for a
  stream launch; absent means json mode, which is what every older run has.

### Wrapper — `runproc.py`

- `is_stream(run_dir)`: `launch.json` has `"stream": true`.
- `main`: stdout goes to `stream.jsonl` when the launch is a stream launch, else to
  `result.json` as today. After the child exits, before `finish(...)` writes `exit.json`,
  `extract_result(run_dir)` runs (stream launches only). Also attempted best-effort in the
  wrapper's failure path.
- `extract_result(run_dir) -> bool`: scan `stream.jsonl` for the last complete line that
  parses as an object with `type == "result"`; write that line (verbatim, plus newline)
  atomically to `result.json`. No result event → no `result.json` (as a killed json-mode run
  has an empty one). Idempotent; never raises on a bad file.
- `run_output(run_dir) -> str`: the text to parse for a run. Not a stream launch →
  `result.json` (unchanged behaviour). Stream launch → `extract_result` if `result.json` is
  missing/empty (wrapper died before writing it), then `result.json` if non-empty, else the
  whole `stream.jsonl` (so a run killed mid-stream yields an error whose `result_text` is the
  stream's tail, like a non-JSON stdout today).
- Stdout stays a file, never a pipe the wrapper pumps: if the wrapper dies, `claude` keeps
  writing its stream (no SIGPIPE), which is the existing orphan-group contract.

### Parsing / collection — `runner.py`

- `parse_result` additionally accepts JSONL: when the text is not one JSON document, the last
  line that parses as an object with `type: "result"` is the result object. One JSON object
  or a JSON array parse exactly as before.
- `collect` and `_orphan_duties` read `RP.run_output(run_dir)` instead of `result.json`.
  For a json-mode run that is the same text.
- `write_run_record` adds `"stream": "stream.jsonl"` to `files` when the file exists;
  `runs._PATH_FILES` lists `stream`, so `nw campaign run` / `--json` show the path.

### Viewer — `agentic/tail.py`

- `resolve(cfg, cid, run=None) -> Path`: the run dir; `run` is an id or unique prefix
  (`runs._resolve`), default the newest run dir (run ids sort by their `NNN-` prefix).
- `Renderer(cwd, budget_usd, price_lookup)`: `feed(event) -> list[Text]` turns one event into
  printable lines and keeps state (`tool_use` id → name, per-`message.id` usage):
  - `system/init` → `● session · model M · N tools`.
  - `assistant` `text` → the text, indented; `thinking` → `· thinking (N chars)` (dim);
    `tool_use` → `→ Name summary`, where the summary is per tool: `Read` path (+ offset/limit),
    `Edit`/`Write`/`MultiEdit`/`NotebookEdit` path, `Bash` first command line (≤120 chars),
    `Grep` pattern + path, `Glob` pattern, `Task`/`Agent` description, `WebFetch` url,
    `TodoWrite` item count, otherwise compact JSON of the input (≤120 chars). Paths under the
    run's `cwd` are shown relative.
  - `user` `tool_result` → `  ← N lines: first non-empty line` (≤100 chars), red `error:` when
    `is_error`.
  - A new `message.id` with usage → one dim line `  $ ~0.42 / $4.00 budget (est., turn 7)`;
    the estimate prices each turn with `conductor_cost.price_for(cfg, message.model)`
    (`input`, `output`, `cache_read`, `cache_write_5m/1h`); a model without a price shows
    tokens instead (`  tokens in 12k · out 3k · cache 80k (no price for M)`).
  - `result` → `■ result: subtype · cost $0.47 / $4.00 · N turns · 3m10s · verdict V`
    (the real `total_cost_usd`, `num_turns`, `duration_ms`, `runs.verdict_status` of
    `structured_output`), red when `is_error`.
  - Unknown event types are skipped; an unparseable complete line prints
    `[unparseable line]` (dim) and is otherwise ignored.
- `stream_stats(path, cfg) -> dict` (`turns`, `usage`, `est_usd`, `cost_usd`, `done`): the
  same tally without rendering, for child 3's run tokens.
- `read_lines(path, offset) -> (lines, new_offset)`: complete lines from `offset`; a trailing
  fragment without newline is left for the next read (so a half-written line is never parsed).
- `tail(cfg, run_dir, *, follow, raw, out, poll_s=0.2)`:
  - header: `run R · persona P · model M · budget $B · <state>` (`runproc.run_status`).
  - stream run: render (or with `--raw` echo) every complete line. Post hoc, a trailing
    fragment is reported as `[incomplete last line]`.
    `--follow` polls from the last offset until `run_status` is `finished` / `vanished` /
    `orphaned`, drains once more and returns. A stream launch with no `stream.jsonl` yet
    prints `no output yet` (post hoc) or waits (follow).
  - run without a stream (no `stream.jsonl` and `launch.json` without `stream`): prints
    `no stream.jsonl: this run was launched without stream capture (before SPEC-0362 or with
    agentic.stream_runs off); showing its result` and then the result summary from
    `result.json` (the same `■ result` line), the result text tail, and the last 10
    `stderr.log` lines. `--follow` on such a running run waits for it to end first. `--raw`
    echoes `result.json`.
  - Ctrl-C exits quietly (130).

### CLI — `cli_campaign.py`

`nw campaign tail CAMPAIGN [--run RUN] [--follow/-f] [--raw]`. Unknown campaign/run →
`ClickException`. Documented in `docs/product/loop/operate.md` (Watching).

## Alternatives considered

- **The wrapper pipes stdout and tees to both files** — more code in the wrapper, and a dead
  wrapper would SIGPIPE `claude`. A file plus a post-exit extraction keeps the orphan contract.
- **Leave `result.json` absent and parse the stream everywhere** — every reader of
  `result.json` (`campaign run` denials, adoption, `_orphan_duties`) would change; the epic
  fixes `result.json` as the raw final object.
- **Decide streaming from the code's default at collect time** — breaks runs already in flight
  when the code lands; invariant 9 rules it out.
- **Persist sessions and read transcripts** — pollutes transcript-based time tracking (epic).

## Acceptance criteria

- [x] A new persona run's argv has `--output-format stream-json --verbose` and still
      `--no-session-persistence`; its `launch.json` has `"stream": true`.
- [x] `stream.jsonl` grows while the run is still going (before `exit.json`).
- [x] After the run, `result.json` is the final `result` event and `run.json`'s verdict,
      `verdict_ok`, `cost_usd`, `usage`, `session_id`, `is_error`, `result_text` equal those of
      a json-mode run of the same output; `files.stream` is set.
- [x] A run dir without `stream` in `launch.json` is collected exactly as before.
- [x] A stream run whose wrapper died gets `result.json` from the stream at collection; a
      stream run killed before its result event is an error run with the stream tail as text,
      not a crash.
- [x] `parse_result` accepts JSONL; a truncated last line or garbage lines do not break it.
- [x] `agentic.stream_runs: false` gives the json-mode argv and no `stream` flag.
- [x] `nw campaign tail C [--run R]` renders tool calls (Read/Edit/Bash with summaries), short
      result summaries, assistant text, a running cost-vs-budget line (estimated when priced,
      tokens otherwise) and the final result line.
- [x] `--follow` prints lines appended after it starts and returns once the run ends; a
      half-written line is not printed until complete.
- [x] On a run without `stream.jsonl` it says so and shows the result summary and stderr tail.
- [x] `--raw` echoes the stream lines verbatim.

## Test plan

- **Automated tests** — a stream-aware fake `claude` (`tests/fixtures/fake_claude.py`: with
  `stream-json` in argv and no `FAKE_CLAUDE_STDOUT`, it prints `system`, `assistant`
  `tool_use`, `user` `tool_result` and `assistant` text lines before its wait and the `result`
  line after). New `tests/test_agentic_stream.py`:
  - argv: stream vs json (`--verbose`, `--no-session-persistence`), `launch.json` flag,
    `select_runner` honouring `agentic.stream_runs`.
  - `parse_result`: JSONL == single JSON for the same result object; garbage + truncated last
    line; no result event → error.
  - live growth: fake waits on its release file; `stream.jsonl` holds the `tool_use` line while
    `exit.json` is absent (`tests/waits.py`).
  - equivalence: the same fake output through `ClaudeRunner(stream=True)` and
    `ClaudeRunner(stream=False)` gives equal `run.json` fields; `result.json` equals the
    result line; `files.stream` set.
  - old run dir: hand-built pre-change run dir (no `stream` in `launch.json`, `result.json`,
    `exit.json`) → `collect` result equals parsing `result.json` as before; no `stream` file.
  - wrapper died: stream run dir with `stream.jsonl` (result event) but no `result.json` →
    `collect` writes `result.json`, verdict ok; killed mid-stream (no result event) →
    `is_error`, text = stream tail.
  - tail: golden stream rendered post hoc (Read/Edit/Bash summaries, text, error result,
    priced cost line vs budget, unpriced tokens line, final result line), `[incomplete last
    line]`, `[unparseable line]`, `--raw`; old run message + summary; `--follow` in a thread
    over a file appended by the test, half line written in two parts, return after
    `exit.json` appears; CLI via Click's runner for unknown run and the default (newest) run.
- **Regression guard:** `tests/test_agentic_runner.py` / `test_agentic_runproc.py` (whole run
  contract over the fake, now in stream mode by default), `test_agentic_campaign_runs.py`,
  `test_agentic_campaign_cli.py`, docs reference tests; `python3 tools/affected_tests.py`.
- **Manual verification:** after landing, `nw campaign tail` (read-only) against an existing
  pre-change run on the live queue: it must report "no stream.jsonl" and show the result
  summary. The first streamed run appears once the queue runner re-execs on the new code; then
  `nw campaign tail C --follow` on it.

## Rollout / migration

Safe to merge while campaigns run: in-flight runs have no `stream` in `launch.json` and their
wrappers are already running old code; new runs are launched by the runner after its
codewatch re-exec. A wrapper started by new code for a launch written by an older runner
(no `stream` flag) behaves as before. Rollback for new runs without a revert:
`agentic.stream_runs: false`.

## Definition of done

- [x] Acceptance criteria all met.
- [x] Test plan executed; automated tests pass; manual check performed.
- [x] No regressions.
- [x] Spec body updated to match what shipped.
- [x] `docs/LEDGER.md` updated (status + date).

## As shipped (2026-11-01)

- **Files:** `agentic/runproc.py` (`is_stream`, `_result_line`, `extract_result`,
  `run_output`; the wrapper writes stdout to `stream.jsonl` for a stream launch and extracts
  `result.json` before `exit.json`, also in its failure path), `agentic/runner.py`
  (`build_argv(req, stream=True)`, `ClaudeRunner(stream=)`, `launch.json` `stream: true`,
  `parse_result` JSONL branch, `collect` / `_orphan_duties` via `RP.run_output`,
  `files.stream` in `run.json`, `select_runner` reads `agentic.stream_runs`),
  `agentic/runs.py` (`paths.stream`), `agentic/tail.py` (new: `resolve`, `read_lines`,
  `Renderer`, `stream_stats`, `tail`), `agentic/cli_campaign.py` (`nw campaign tail`),
  `agentic/conductor_cost.py` (`usage_kinds` factored out of `read_turns` so the viewer
  prices turns the same way), `docs/product/loop/operate.md` (Watching),
  `tests/fixtures/fake_claude.py` (stream mode), `tests/fixtures/stream_json/*.jsonl`.
- **Validated against real `claude` output**, not only a fake: two real samples were captured
  with claude 2.1.316 (`claude -p "reply with the word ok" --output-format stream-json
  --verbose --no-session-persistence --max-turns 1`, and a run with the runner's flag set —
  `--json-schema`, `--model haiku`, `--max-budget-usd`, `--permission-prompts none`,
  `--no-session-persistence`, `--allowedTools Read` — that called `Read` and returned its
  verdict through `StructuredOutput`). Both are test fixtures; the fake `claude` replays the
  second one in stream mode, so the wrapper, parser and viewer tests run on the real event
  shapes. Every flag `build_argv` passes was checked in `claude --help`. A sample with
  `--permission-mode bypassPermissions` was not captured (the agent's sandbox refused to start
  an unrestricted agent); the permission mode does not change the output format.
- **Judgment calls / deviations from the Design above:**
  - `parse_result` also takes the stream path when the text parses as one JSON object whose
    `type` is not `result` (a stream cut after its first line), so such a run is an error,
    never a result-less "success".
  - The viewer treats a run as ended when `runproc.run_status` is finished / vanished /
    orphaned **or** `run.json` exists (FakeRunner runs have no `proc.json`).
  - Post hoc, a trailing fragment of an ended run that parses is rendered; otherwise
    `[incomplete last line]`. A stream that ended without a `result` event prints
    `■ run ended without a result event` plus the last cost line.
  - Subagent events (`parent_tool_use_id` set) are prefixed `┆` and do not emit cost lines.
  - The old-run summary prints the result text tail only when there is no structured verdict.
- **Tests executed:** `tests/test_agentic_stream.py`, 24 tests, all passing (real-sample
  shape; argv both modes; kill switch; json-mode launch has no flag/stream; JSONL == single
  JSON; garbage + truncated line; no result event → error; live growth before `exit.json`
  then `result.json` == last line; stream vs json run `run.json` equality; pre-change run dir
  collected as before; old unparseable result; wrapper-died extraction; killed mid-stream;
  timeout in stream mode; rendering of the real sample with priced and unpriced cost lines;
  tool summaries and error results; unparseable/incomplete lines; `--raw`; old-run message;
  follow with a line written in two halves, ending on `exit.json`; follow on an old running
  run; `stream_stats`; CLI default/`--run`/unknown run/unknown campaign).
  `tests/test_agentic_runner.py::test_claude_runner_argv_and_stdin` now expects the stream
  argv. Regression set: `tests/test_agentic_*.py`, `tests/test_docs_*.py`,
  `tests/test_herdr.py`, `tests/test_completion*.py` — 2832 passed, 2 skipped; plus
  `test_tui_campaign_model.py` and `test_cli*.py` (15 passed). `tools/affected_tests.py` fell
  back to the full suite (new test fixtures), so the set was chosen by hand; the queue close
  gate runs the full suite.
- **Manual verification:** `nw campaign tail` on a real pre-change run of the live queue
  (SPEC-0357's `002-implementer`, read-only): prints the header, `no stream.jsonl: this run
  was launched without stream capture …` and `■ result: success · cost $0.84 / $4.00 · 21
  turns · 9m42s · verdict done`. A live streamed run will be checked when the queue runner
  re-execs on this code.

## Open questions

None. Decided here: stdout goes to a file and the result is extracted after exit (not a pipe);
`result.json` is absent when no result event exists; kill switch `agentic.stream_runs`; cost
estimate uses the conductor price table; the viewer follows one run.
