"""`nw campaign tail` (SPEC-0362): a readable transcript of one persona run, live or after the
fact, from the run's stream-json capture `runs/<run>/stream.jsonl`.

`Renderer` turns stream-json events (the format of `claude -p --output-format stream-json
--verbose`, see `tests/fixtures/stream_json/`) into lines: tool calls with a one-line summary
of their input, a short summary of each tool result, assistant text, a running cost estimate
against the run's budget, and the final result. `tail` drives it over a file, once or
following it until the run ends. A run launched without stream capture (before SPEC-0362, or
with `agentic.stream_runs: false`) has no stream: `tail` says so and shows its result."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from rich.console import Console
from rich.text import Text

from . import conductor_cost as CC
from . import runproc as RP
from . import runs as RN
from . import store

TOOL_W = 120
RESULT_W = 100
STDERR_LINES = 10
RESULT_TEXT_LINES = 15
END_STATES = ("finished", "vanished", "orphaned")


# ---- locating a run ------------------------------------------------------------------------

def resolve(cfg, cid, run=None) -> Path:
    """The run dir of `run` (an id or unique prefix) in campaign `cid`; default the newest run
    (run ids start with a zero-padded sequence number). ValueError when there is none."""
    store.read_state(cfg, cid)                      # unknown campaign → ValueError
    dirs = RN._run_dirs(cfg, cid)
    if not dirs:
        raise ValueError(f"no runs yet in {cid}")
    if run is None:
        return dirs[sorted(dirs)[-1]]
    return dirs[RN._resolve(dirs, cid, run)]


def read_lines(path, offset: int = 0) -> tuple[list[str], int]:
    """Complete lines of `path` from byte `offset`, and the offset after the last newline. A
    trailing fragment without a newline is left for the next read."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    chunk = data[:end + 1]
    return chunk.decode("utf-8", errors="replace").splitlines(), offset + len(chunk)


def _fragment(path, offset: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


# ---- rendering -----------------------------------------------------------------------------

def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _first_line(text: str) -> str:
    for line in str(text).splitlines():
        if line.strip():
            return line.strip()
    return ""


def _usd(x) -> str:
    return f"${float(x):.2f}"


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _duration(ms) -> str:
    try:
        s = float(ms) / 1000
    except (TypeError, ValueError):
        return "?"
    return f"{int(s // 60)}m{int(s % 60):02d}s" if s >= 60 else f"{s:.1f}s"


def _result_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type')}]")
        return "\n".join(parts)
    return "" if content is None else json.dumps(content)


class Renderer:
    """Stateful stream-json → lines. `price_lookup(model)` returns a `conductor_cost` price
    entry or None; `budget_usd` is the run's `--max-budget-usd`."""

    def __init__(self, cwd=None, budget_usd=None, price_lookup=None):
        self.cwd = str(cwd).rstrip("/") + "/" if cwd else None
        self.budget = budget_usd
        self.price_lookup = price_lookup or (lambda _model: None)
        self.tools: dict[str, str] = {}             # tool_use id → name
        self.turns: dict[str, dict] = {}            # message id → {model, kinds}
        self.cost_usd = None                        # the result event's real cost
        self.done = False
        self.result = None

    # -- tallies --
    def _note_usage(self, msg) -> bool:
        """Record a message's usage; True when the message id is new (a new API turn)."""
        usage = msg.get("usage")
        if not isinstance(usage, dict) or msg.get("model") == "<synthetic>":
            return False
        mid = msg.get("id") or f"anon-{len(self.turns)}"
        new = mid not in self.turns
        self.turns[mid] = {"model": msg.get("model"), **CC.usage_kinds(usage)}
        return new

    def usage(self) -> dict:
        tot = {k: 0 for k in CC.KINDS}
        for t in self.turns.values():
            for k in CC.KINDS:
                tot[k] += t[k]
        return tot

    def est_usd(self):
        """Estimated USD of the turns so far; None when some model has no configured price."""
        total = 0.0
        for t in self.turns.values():
            usd = CC.turn_usd(t, self.price_lookup(t["model"]))
            if usd is None:
                return None
            total += usd
        return total

    def _budget(self) -> str:
        return f" / {_usd(self.budget)} budget" if self.budget else ""

    def cost_line(self) -> Text:
        est = self.est_usd()
        n = len(self.turns)
        if est is not None:
            return Text(f"  $ ~{est:.2f}{self._budget()} (est., turn {n})", style="dim")
        u = self.usage()
        models = sorted({str(t["model"]) for t in self.turns.values()
                         if self.price_lookup(t["model"]) is None})
        return Text(f"  tokens in {_k(u['input'])} · out {_k(u['output'])} · cache "
                    f"{_k(u['cache_read'] + u['cache_write_5m'] + u['cache_write_1h'])} "
                    f"(turn {n}; no price for {', '.join(models)})", style="dim")

    # -- events --
    def _rel(self, path) -> str:
        path = str(path)
        if self.cwd and path.startswith(self.cwd):
            return path[len(self.cwd):] or "."
        return path

    def tool_summary(self, name: str, inp) -> str:
        inp = inp if isinstance(inp, dict) else {}
        if name == "Read":
            extra = "".join(f" {k}={inp[k]}" for k in ("offset", "limit") if inp.get(k) is not None)
            return self._rel(inp.get("file_path", "")) + extra
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            return self._rel(inp.get("file_path") or inp.get("notebook_path") or "")
        if name == "Bash":
            return _clip(_first_line(inp.get("command", "")), TOOL_W)
        if name == "Grep":
            where = f" in {self._rel(inp['path'])}" if inp.get("path") else ""
            return _clip(f"{inp.get('pattern', '')!r}{where}", TOOL_W)
        if name == "Glob":
            return _clip(str(inp.get("pattern", "")), TOOL_W)
        if name in ("Task", "Agent"):
            return _clip(str(inp.get("description") or inp.get("prompt") or ""), TOOL_W)
        if name == "WebFetch":
            return _clip(str(inp.get("url", "")), TOOL_W)
        if name == "TodoWrite":
            return f"{len(inp.get('todos') or [])} items"
        return _clip(json.dumps(inp, separators=(",", ":"), ensure_ascii=False), TOOL_W)

    def feed_line(self, line: str) -> list[Text]:
        line = line.strip()
        if not line:
            return []
        try:
            ev = json.loads(line)
        except ValueError:
            return [Text("[unparseable line]", style="dim")]
        return self.feed(ev) if isinstance(ev, dict) else [Text("[unparseable line]", style="dim")]

    def feed(self, ev: dict) -> list[Text]:
        typ = ev.get("type")
        nest = "  ┆ " if ev.get("parent_tool_use_id") else ""
        out: list[Text] = []
        if typ == "system" and ev.get("subtype") == "init":
            out.append(Text(f"● session · model {ev.get('model')} · "
                            f"{len(ev.get('tools') or [])} tools", style="bold"))
        elif typ == "assistant":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            new_turn = self._note_usage(msg)
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text" and str(block.get("text") or "").strip():
                    for ln in str(block["text"]).strip().splitlines():
                        out.append(Text(f"{nest}  {ln}"))
                elif kind == "thinking":
                    n = len(str(block.get("thinking") or ""))
                    out.append(Text(f"{nest}· thinking ({n} chars)", style="dim"))
                elif kind == "tool_use":
                    name = str(block.get("name") or "?")
                    self.tools[str(block.get("id"))] = name
                    t = Text(f"{nest}→ ")
                    t.append(name, style="cyan")
                    summary = self.tool_summary(name, block.get("input"))
                    if summary:
                        t.append(" " + summary)
                    out.append(t)
            if new_turn and not nest:
                out.append(self.cost_line())
        elif typ == "user":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = _result_content_text(block.get("content"))
                n = len(text.splitlines())
                first = _clip(_first_line(text), RESULT_W)
                if block.get("is_error"):
                    out.append(Text(f"{nest}  ← error: {first}", style="red"))
                else:
                    out.append(Text(f"{nest}  ← {n} line{'s' if n != 1 else ''}"
                                    + (f": {first}" if first else ""), style="dim"))
        elif typ == "result":
            out.append(self.result_line(ev))
        return out

    def result_line(self, ev: dict) -> Text:
        self.done, self.result = True, ev
        cost = ev.get("total_cost_usd")
        try:
            self.cost_usd = float(cost) if cost is not None else None
        except (TypeError, ValueError):
            self.cost_usd = None
        verdict = ev.get("structured_output")
        if verdict is None:
            try:
                verdict = json.loads(ev.get("result") or "")
            except (TypeError, ValueError):
                verdict = None
        parts = [f"■ result: {ev.get('subtype') or '?'}",
                 f"cost {_usd(self.cost_usd) if self.cost_usd is not None else '?'}{self._budget().replace(' budget', '')}"]
        if ev.get("num_turns") is not None:
            parts.append(f"{ev['num_turns']} turns")
        if ev.get("duration_ms") is not None:
            parts.append(_duration(ev["duration_ms"]))
        parts.append(f"verdict {RN.verdict_status(verdict, isinstance(verdict, dict))}")
        return Text(" · ".join(parts), style="bold red" if ev.get("is_error") else "bold green")

    def stats(self) -> dict:
        return {"turns": len(self.turns), "usage": self.usage(), "est_usd": self.est_usd(),
                "cost_usd": self.cost_usd, "done": self.done}


def _price_lookup(cfg):
    return lambda model: CC.price_for(cfg or {}, model)


def stream_stats(path, cfg=None, budget_usd=None) -> dict:
    """Turns, token usage, estimated USD so far, the real cost once the result event is in, and
    whether it is (SPEC-0360 child 3: a run pane's cost token)."""
    r = Renderer(budget_usd=budget_usd, price_lookup=_price_lookup(cfg))
    lines, _ = read_lines(path, 0)
    for line in lines:
        r.feed_line(line)
    return r.stats()


# ---- the viewer ----------------------------------------------------------------------------

def _ended(run_dir: Path) -> bool:
    return RP.run_status(run_dir) in END_STATES or (run_dir / "run.json").is_file()


def _print(out: Console, lines) -> None:
    for t in lines:
        out.print(t, highlight=False)


def _raw(out: Console, line: str) -> None:
    out.file.write(line + "\n")
    out.file.flush()


def _summary_without_stream(out: Console, run_dir: Path, r: Renderer, raw: bool) -> None:
    text = RP._text(run_dir / "result.json")
    if raw:
        if text.strip():
            _raw(out, text.rstrip("\n"))
        return
    obj = None
    try:
        obj = json.loads(text) if text.strip() else None
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        _print(out, [r.result_line(obj)])
        body = obj.get("result")
        if isinstance(body, str) and body.strip() and not isinstance(obj.get("structured_output"), dict):
            for ln in body.strip().splitlines()[-RESULT_TEXT_LINES:]:
                out.print(Text("  " + ln), highlight=False)
    elif text.strip():
        out.print(Text("result.json is not a result object; last lines:", style="yellow"))
        for ln in text.strip().splitlines()[-RESULT_TEXT_LINES:]:
            out.print(Text("  " + ln), highlight=False)
    else:
        out.print(Text("no result.json", style="dim"))
    err = RP._text(run_dir / "stderr.log").strip().splitlines()[-STDERR_LINES:]
    if err:
        out.print(Text(f"stderr (last {len(err)} lines):", style="dim"))
        for ln in err:
            out.print(Text("  " + ln, style="dim"), highlight=False)


def tail(cfg, run_dir, *, follow=False, raw=False, out: Console | None = None,
         poll_s: float = 0.2, sleep=time.sleep) -> int:
    """Render (or with `raw` echo) one run's stream; with `follow`, keep reading appended lines
    until the run ends. Returns the exit code (0)."""
    run_dir = Path(run_dir)
    out = out or Console()
    req = RP.read_json(run_dir / "request.json") or {}
    rj = RP.read_json(run_dir / "run.json") or {}
    budget = req.get("max_budget_usd") or rj.get("max_budget_usd")
    r = Renderer(cwd=req.get("cwd"), budget_usd=budget, price_lookup=_price_lookup(cfg))
    stream = run_dir / "stream.jsonl"
    streaming = RP.is_stream(run_dir) or stream.is_file()
    if not raw:
        state = "done" if (run_dir / "run.json").is_file() else RP.run_status(run_dir)
        out.print(Text(f"run {run_dir.name} · persona {req.get('persona') or rj.get('persona') or '?'}"
                       f" · model {req.get('model') or rj.get('model') or '?'}"
                       + (f" · budget {_usd(budget)}" if budget else "") + f" · {state}",
                       style="bold"), highlight=False)
    if not streaming:
        if not raw:
            out.print(Text("no stream.jsonl: this run was launched without stream capture "
                           "(before SPEC-0362 or with agentic.stream_runs off); showing its result",
                           style="yellow"), highlight=False)
        if follow and not _ended(run_dir):
            if not raw:
                out.print(Text("waiting for the run to end…", style="dim"))
            while not _ended(run_dir):
                sleep(poll_s)
        _summary_without_stream(out, run_dir, r, raw)
        return 0

    def emit(lines):
        for line in lines:
            if raw:
                _raw(out, line)
            else:
                _print(out, r.feed_line(line))

    offset = 0
    waiting_said = False
    while True:
        ended = _ended(run_dir)
        lines, offset = read_lines(stream, offset)
        emit(lines)
        if not stream.is_file() and not ended and not raw and not waiting_said:
            out.print(Text("no output yet" + (" — waiting" if follow else ""), style="dim"))
            waiting_said = True
        if ended or not follow:
            break
        sleep(poll_s)
    frag = _fragment(stream, offset)
    if frag:
        parsed = None
        if ended:
            try:
                parsed = json.loads(frag)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            emit([frag])
        elif not raw:
            out.print(Text("[incomplete last line]", style="dim"))
    if not raw and ended and not r.done:
        out.print(Text("■ run ended without a result event", style="bold red"))
        _print(out, [r.cost_line()] if r.turns else [])
    return 0
