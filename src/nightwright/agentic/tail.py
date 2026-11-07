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
DIFF_LINES = 20                      # added and removed lines shown for one edit
OUTPUT_LINES = 8                     # stdout or stderr lines shown for one shell
SPEECH = "bold yellow"               # the agent's own words, against dim tool output
END_STATES = ("finished", "vanished", "orphaned")
RUNNING = ("running", "orphan_group")
RUN_LOOKBACK = 8                     # newest run dirs looked at per campaign
CLEAR = "\033[H\033[2J\033[3J"       # home, clear screen, clear scrollback


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


def running_runs(cfg, cid) -> list[str]:
    """Run ids of `cid` whose process is alive, newest first (run ids sort by sequence)."""
    dirs = RN._run_dirs(cfg, cid)
    out = []
    for name in sorted(dirs, reverse=True)[:RUN_LOOKBACK]:
        try:
            if RP.run_status(dirs[name]) in RUNNING:
                out.append(name)
        except OSError:
            pass
    return out


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
    # `\n` only. `splitlines` also breaks on U+2028/U+2029 inside JSON strings (SPEC-0442).
    text = chunk.decode("utf-8", errors="replace")
    if text.endswith("\n"):
        text = text[:-1]
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")] if text else []
    return lines, offset + len(chunk)


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


def _cut(text: str, n: int) -> str:
    """Clip one display line without collapsing the indentation a diff relies on."""
    text = str(text).replace("\t", "    ")
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
        self._prose: dict[str, str] = {}            # parent id → held assistant text
        self._pending_cost: Text | None = None      # cost line waiting behind held text
        self._shell_live = False                    # stdout already shown for the open shell

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

    def _sdk_arg_summary(self, args) -> str:
        if isinstance(args, dict):
            for key in ("command", "path", "file_path", "pattern", "glob_pattern",
                        "globPattern", "description", "url", "query"):
                if args.get(key):
                    val = args[key]
                    if key in ("path", "file_path"):
                        val = self._rel(val)
                    return _clip(_first_line(val), TOOL_W)
            todos = args.get("todos")
            if isinstance(todos, list) and todos:
                return f"{len(todos)} items"
            return _clip(json.dumps(args, separators=(",", ":"), ensure_ascii=False), TOOL_W)
        if args:
            return _clip(_first_line(args), TOOL_W)
        return ""

    @staticmethod
    def _sdk_result_text(result) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            inner = result.get("result") if isinstance(result.get("result"), dict) else result
            if isinstance(inner, dict):
                if "exitCode" in inner or "exit_code" in inner:
                    code = inner.get("exitCode", inner.get("exit_code"))
                    first = _first_line(inner.get("stderr") or inner.get("error")
                                        or inner.get("stdout") or "")
                    return f"exit {code}" + (f": {first}" if first else "")
                failure, success = inner.get("failure"), inner.get("success")
                if isinstance(failure, dict):
                    return Renderer._sdk_result_text(failure)
                if isinstance(success, dict):
                    if "content" in success or "file" in success or "fileContent" in success:
                        return "ok"
                    return Renderer._sdk_result_text(success)
                for key in ("error", "message", "stdout", "output", "text"):
                    if inner.get(key):
                        return str(inner[key])
            return json.dumps(result, ensure_ascii=False)
        return str(result)

    def _diff_result(self, value: dict, nest: str) -> list[Text]:
        added = value.get("linesAdded")
        removed = value.get("linesRemoved")
        head = Text(f"{nest}  ← ")
        head.append(f"+{added if added is not None else '?'}", style="green")
        head.append(" ")
        head.append(f"−{removed if removed is not None else '?'}", style="red")
        body = [ln for ln in str(value.get("diffString") or "").splitlines()
                if not ln.startswith("---") and not ln.startswith("+++")]
        out = [head]
        for ln in body[:DIFF_LINES]:
            style = "green" if ln.startswith("+") else "red" if ln.startswith("-") else "dim"
            out.append(Text(f"{nest}    {_cut(ln, RESULT_W)}", style=style))
        extra = len(body) - DIFF_LINES
        if extra > 0:
            out.append(Text(f"{nest}    … {extra} more lines", style="dim"))
        return out

    def _shell_result(self, value: dict, nest: str) -> list[Text]:
        code = value.get("exitCode", value.get("exit_code"))
        head = Text(f"{nest}  ← exit {code}", style="red" if code else "dim")
        # The live deltas already printed this output. The exit line is enough.
        if self._shell_live:
            self._shell_live = False
            return [head]
        err = str(value.get("stderr") or "").strip()
        stdout = str(value.get("stdout") or "").strip()
        text, style = (err, "red") if err else (stdout, "dim")
        rows = text.splitlines()
        out = [head]
        for ln in rows[:OUTPUT_LINES]:
            out.append(Text(f"{nest}    {_cut(ln, RESULT_W)}", style=style))
        extra = len(rows) - OUTPUT_LINES
        if extra > 0:
            out.append(Text(f"{nest}    … {extra} more lines", style="dim"))
        return out

    def _shell_delta(self, ev: dict, nest: str) -> list[Text]:
        """One ``shell-output-delta`` chunk: stdout, stderr, a trim notice, or the real exit."""
        event = ev.get("event") if isinstance(ev.get("event"), dict) else {}
        kind = str(event.get("type") or "")
        if kind in ("stdout", "stderr"):
            self._shell_live = True
            style = "red" if kind == "stderr" else "dim"
            rows = [ln for ln in str(event.get("data") or "").splitlines() if ln.strip()]
            out = [Text(f"{nest}  │ {_cut(ln, RESULT_W)}", style=style) for ln in rows[:OUTPUT_LINES]]
            extra = len(rows) - OUTPUT_LINES
            if extra > 0:
                out.append(Text(f"{nest}  │ … {extra} more lines", style="dim"))
            return out
        if kind in ("stdout_trimmed", "stderr_trimmed"):
            return [Text(f"{nest}  │ … output trimmed", style="dim")]
        if kind == "exit":
            code = event.get("code", event.get("exitCode"))
            return [Text(f"{nest}  · shell exit {code}", style="red" if code else "dim")]
        return []

    def _file_list(self, files, total, nest: str) -> list[Text]:
        shown = [str(f).removeprefix("./") for f in files[:OUTPUT_LINES]]
        label = f"{total} file" if total == 1 else f"{total} files"
        head = Text(f"{nest}  ← {label}", style="dim")
        if shown:
            head.append(": " + ", ".join(shown))
        out = [head]
        extra = len(files) - len(shown)
        if extra > 0:
            out.append(Text(f"{nest}    … {extra} more", style="dim"))
        return out

    def _grep_files(self, value: dict) -> list[str]:
        files = []
        workspaces = value.get("workspaceResults")
        if not isinstance(workspaces, dict):
            return files
        for body in workspaces.values():
            if not isinstance(body, dict):
                continue
            output = body.get("output") if isinstance(body.get("output"), dict) else body
            matches = output.get("matches") if isinstance(output, dict) else None
            for match in matches or []:
                if isinstance(match, dict) and match.get("file"):
                    files.append(str(match["file"]))
                elif isinstance(match, str):
                    files.append(match)
        return files

    def _sdk_rich_result(self, result, nest: str) -> list[Text] | None:
        """Cursor SDK tool results are ``{status, value}``. Render the value, not the JSON."""
        if not isinstance(result, dict) or "status" not in result or "value" not in result:
            return None
        value = result.get("value")
        if result.get("status") == "error":
            if isinstance(value, str):
                msg = value
            elif isinstance(value, dict):
                msg = str(value.get("error") or value.get("message") or value.get("stderr") or "")
            else:
                msg = ""
            return [Text(f"{nest}  ← error: {_clip(msg, RESULT_W) or 'failed'}", style="red")]
        if not isinstance(value, dict):
            return None
        if "diffString" in value or ("linesAdded" in value and "linesRemoved" in value):
            return self._diff_result(value, nest)
        if "exitCode" in value or "exit_code" in value:
            return self._shell_result(value, nest)
        if "totalLines" in value:
            size = value.get("fileSize")
            extra = f" · {size} bytes" if size is not None else ""
            return [Text(f"{nest}  ← {value.get('totalLines')} lines{extra}", style="dim")]
        if "files" in value and "totalFiles" in value:
            files = value.get("files") if isinstance(value.get("files"), list) else []
            return self._file_list(files, value.get("totalFiles") or len(files), nest)
        if "workspaceResults" in value:
            files = self._grep_files(value)
            return self._file_list(files, len(files), nest)
        todos = value.get("todos")
        if isinstance(todos, list):
            out = [Text(f"{nest}  ← {len(todos)} todos", style="dim")]
            for todo in todos[:OUTPUT_LINES]:
                if not isinstance(todo, dict):
                    continue
                state = str(todo.get("status") or "")
                out.append(Text(f"{nest}    {state}  {_clip(todo.get('content') or '', RESULT_W)}",
                                style="dim"))
            extra = len(todos) - OUTPUT_LINES
            if extra > 0:
                out.append(Text(f"{nest}    … {extra} more", style="dim"))
            return out
        return None

    def _sdk_tool_call(self, ev: dict, nest: str) -> list[Text]:
        name = str(ev.get("name") or "?")
        status = str(ev.get("status") or "")
        if status == "error":
            first = _clip(_first_line(self._sdk_result_text(ev.get("result"))), RESULT_W)
            return [Text(f"{nest}  ← error: {first or name}", style="red")]
        if status == "completed":
            rich = self._sdk_rich_result(ev.get("result"), nest)
            if rich is not None:
                return rich
            text = self._sdk_result_text(ev.get("result"))
            n = len(str(text).splitlines()) if text else 0
            first = _clip(_first_line(text), RESULT_W)
            label = f"{n} line{'s' if n != 1 else ''}" if text else "done"
            return [Text(f"{nest}  ← {label}" + (f": {first}" if first else ""), style="dim")]
        t = Text(f"{nest}→ ")
        t.append(name, style="cyan")
        summary = self._sdk_arg_summary(ev.get("args"))
        if summary:
            t.append(" " + summary)
        return [t]

    def _sdk_usage_line(self, ev: dict) -> Text | None:
        usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}

        def num(*names) -> int:
            for name in names:
                if usage.get(name) is not None:
                    try:
                        return int(usage[name])
                    except (TypeError, ValueError):
                        return 0
            return 0

        inp = num("input_tokens", "inputTokens")
        out_n = num("output_tokens", "outputTokens")
        if not inp and not out_n:
            return None
        return Text(f"  tokens in {_k(inp)} · out {_k(out_n)}", style="dim")

    def _nest(self, key: str) -> str:
        return "  ┆ " if key else ""

    def _speech_line(self, nest: str, ln: str) -> Text:
        """One spoken line: a yellow marker, then the words."""
        t = Text(f"{nest}  ")
        t.append("▸ ", style=SPEECH)
        t.append(ln, style=SPEECH)
        return t

    def _append_prose(self, key: str, text: str) -> list[Text]:
        """Hold assistant text. A newline flushes the finished lines; a token fragment stays."""
        if not text:
            return []
        buf = (self._prose.get(key) or "") + text
        if "\n" not in buf:
            self._prose[key] = buf
            return []
        parts = buf.split("\n")
        self._prose[key] = parts[-1]
        nest = self._nest(key)
        return [self._speech_line(nest, ln) for ln in parts[:-1] if ln.strip()]

    def _flush_key(self, key: str) -> list[Text]:
        """Emit held text for one stream as a single line, then a cost line that was waiting."""
        buf = self._prose.pop(key, "") or ""
        nest = self._nest(key)
        out = [self._speech_line(nest, ln) for ln in buf.split("\n") if ln.strip()]
        if out and not key and self._pending_cost is not None:
            out.append(self._pending_cost)
            self._pending_cost = None
        return out

    def flush_prose(self) -> list[Text]:
        """Emit every held fragment. The viewer calls this at the end of a poll."""
        keys = [k for k in self._prose if not k] + [k for k in self._prose if k]
        out: list[Text] = []
        for key in keys:
            out.extend(self._flush_key(key))
        if self._pending_cost is not None:
            out.append(self._pending_cost)
            self._pending_cost = None
        return out

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
        key = ev.get("parent_tool_use_id") or ""
        nest = self._nest(key)
        # Thinking stays between tokens. Anything else closes the sentence so far.
        out: list[Text] = [] if typ in ("assistant", "thinking", "shell_output") else self._flush_key(key)
        if typ == "system" and ev.get("subtype") == "init":
            model = ev.get("model")
            if isinstance(model, dict):
                model = model.get("id") or model
            tools = ev.get("tools")
            extra = f" · {len(tools)} tools" if isinstance(tools, list) else ""
            out.append(Text(f"● session · model {model}{extra}", style="bold"))
        elif typ == "thinking":
            return out
        elif typ == "shell_output":
            out.extend(self._shell_delta(ev, nest))
        elif typ == "tool_call":
            out.extend(self._sdk_tool_call(ev, nest))
        elif typ == "usage":
            line = self._sdk_usage_line(ev)
            if line is not None:
                out.append(line)
        elif typ == "assistant":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            new_turn = self._note_usage(msg)
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "text":
                    out.extend(self._append_prose(key, str(block.get("text") or "")))
                elif kind == "thinking":
                    n = len(str(block.get("thinking") or ""))
                    out.append(Text(f"{nest}· thinking ({n} chars)", style="dim"))
                elif kind == "tool_use":
                    out.extend(self._flush_key(key))
                    name = str(block.get("name") or "?")
                    self.tools[str(block.get("id"))] = name
                    t = Text(f"{nest}→ ")
                    t.append(name, style="cyan")
                    summary = self.tool_summary(name, block.get("input"))
                    if summary:
                        t.append(" " + summary)
                    out.append(t)
            if new_turn and not key:
                if (self._prose.get(key) or "").strip():
                    self._pending_cost = self.cost_line()
                else:
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
        if lines and not raw:
            _print(out, r.flush_prose())

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


def _campaign_state(cfg, cid) -> str:
    try:
        st = store.read_state(cfg, cid)
    except (ValueError, OSError):
        return "?"
    text = str(st.get("state") or "?").lower().replace("_", " ")
    park = st.get("park") or {}
    if st.get("state") == "PARKED" and park.get("reason"):
        text += f": {park['reason']}"
    return text


def follow_campaign(cfg, cid, *, raw=False, out: Console | None = None, poll_s: float = 1.0,
                    sleep=time.sleep, stop=None, clear=True) -> int:
    """SPEC-0366: the run pane's viewer. Follows campaign `cid` across its runs for as long as
    it lives (herdr binds the pane's agent state to this process): tails the newest running run,
    prints a footer when it ends, a line when the campaign's state changes, and tails the next
    run by itself. Returns only when `stop()` says so (tests); a signal ends the process."""
    out = out or Console()
    stop = stop or (lambda: False)
    if clear and not raw:
        out.file.write(CLEAR)                  # wipes the command line the shell echoed
        out.file.flush()
    store.read_state(cfg, cid)                 # unknown campaign → ValueError
    shown: set[str] = set()
    said = None
    if not running_runs(cfg, cid):
        dirs = RN._run_dirs(cfg, cid)
        if dirs:
            newest = sorted(dirs)[-1]
            tail(cfg, dirs[newest], raw=raw, out=out)
            shown.add(newest)
        if not raw:
            said = _campaign_state(cfg, cid)
            out.print(Text(f"■ waiting for the next run · campaign {said}", style="dim"),
                      highlight=False)
    while not stop():
        pick = next((r for r in running_runs(cfg, cid) if r not in shown), None)
        if pick is not None:
            if shown and not raw:
                out.print(Text("─" * 40, style="dim"), highlight=False)
            shown.add(pick)
            tail(cfg, RN._run_dirs(cfg, cid)[pick], follow=True, raw=raw, out=out,
                 sleep=sleep)
            if not raw:
                said = _campaign_state(cfg, cid)
                out.print(Text(f"■ run {pick} ended · waiting for the next run · campaign "
                               f"{said}", style="bold"), highlight=False)
            continue
        if not raw:
            now = _campaign_state(cfg, cid)
            if now != said:
                said = now
                out.print(Text(f"· campaign {now}", style="dim"), highlight=False)
        sleep(poll_s)
    return 0
