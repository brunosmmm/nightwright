"""Local Cursor SDK persona child (SPEC-0442).

``python -m nightwright.agentic.cursor_exec <run_dir> [--read-only] [--add-dir DIR]
[--resume AGENT_ID]``
is the process ``runproc`` supervises when a persona spawn resolves to Cursor.
It reads ``request.json`` and ``prompt.md``, runs one local agent, and writes
SDK messages to stdout plus a ``type: result`` line so ``extract_result`` and
``parse_result`` stay the collectors. ``cursor-sdk`` is imported only when a
run starts. The API key comes from ``CURSOR_API_KEY`` and is never written out.
"""
from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

from .cursor_map import sdk_selection

# SPEC-0556: `--resume <agent id>` continues that agent (`Agent.resume`) instead of creating one.
# The result's `session_id` is the agent id either way.

# SPEC-0456 / SPEC-0457. An empty list is "no built-in tools" in the SDK, which
# is how read-only reviewers lost file access. The API accepts the lowercase
# names only; the capitalized ones are rejected as unknown. Shell, Write,
# StrReplace, and Delete stay off.
READ_ONLY_TOOLS = ("read", "grep", "glob", "ls")


def agent_tools(read_only: bool) -> list[str] | None:
    """The SDK ``tools`` value. ``None`` keeps the default toolset for writers."""
    return list(READ_ONLY_TOOLS) if read_only else None


def _plain(value):
    """JSON-safe copy of an SDK object. Last resort is ``str``; callers scrub secrets."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        try:
            return _plain(to_json())
        except Exception:
            pass
    fields = getattr(value, "__dataclass_fields__", None)
    if fields:
        return {k: _plain(getattr(value, k)) for k in fields if getattr(value, k) is not None}
    return str(value)


def _pick(msg, *names) -> dict:
    out = {}
    for name in names:
        value = getattr(msg, name, None)
        if value is not None:
            out[name] = _plain(value)
    return out


def usage_dict(usage) -> dict:
    """SDK token counts plus the Claude cache aliases ``usage_kinds`` already reads."""
    if usage is None:
        return {}
    if isinstance(usage, dict):
        src = usage
        def get(*names):
            for name in names:
                if src.get(name) is not None:
                    return src[name]
            return None
    else:
        def get(*names):
            for name in names:
                value = getattr(usage, name, None)
                if value is not None:
                    return value
            return None

    def num(*names) -> int:
        value = get(*names)
        try:
            return int(value) if value is not None else 0
        except (TypeError, ValueError):
            return 0

    out = {
        "input_tokens": num("input_tokens", "inputTokens"),
        "output_tokens": num("output_tokens", "outputTokens"),
        "cache_read_tokens": num("cache_read_tokens", "cacheReadTokens"),
        "cache_write_tokens": num("cache_write_tokens", "cacheWriteTokens"),
        "total_tokens": num("total_tokens", "totalTokens"),
    }
    reasoning = get("reasoning_tokens", "reasoningTokens")
    if reasoning is not None:
        try:
            out["reasoning_tokens"] = int(reasoning)
        except (TypeError, ValueError):
            pass
    out["cache_read_input_tokens"] = out["cache_read_tokens"]
    out["cache_creation_input_tokens"] = out["cache_write_tokens"]
    return out


def _block(block) -> dict:
    if isinstance(block, dict):
        return block
    kind = getattr(block, "type", None)
    if kind == "text":
        return {"type": "text", "text": getattr(block, "text", "") or ""}
    if kind == "tool_use":
        return {"type": "tool_use", "id": getattr(block, "id", "") or "",
                "name": getattr(block, "name", "") or "",
                "input": _plain(getattr(block, "input", None))}
    return _plain(block) if not isinstance(_plain(block), str) else {"type": str(kind or "unknown")}


def event_dict(msg) -> dict:
    """One SDK message as a JSON object. Assistant text matches the Claude content-block shape."""
    if isinstance(msg, dict):
        return msg
    typ = getattr(msg, "type", None)
    if typ == "system":
        model = getattr(msg, "model", None)
        model_id = model if isinstance(model, str) or model is None else getattr(model, "id", None)
        tools = getattr(msg, "tools", ())
        ev = {"type": "system", "subtype": getattr(msg, "subtype", "") or "init",
              "model": model_id, "tools": list(tools or ())}
        for name in ("agent_id", "run_id"):
            value = getattr(msg, name, None)
            if value:
                ev[name] = value
        if model is not None and not isinstance(model, str):
            params = getattr(model, "params", None)
            if params:
                ev["model_params"] = _plain(list(params))
        return ev
    if typ == "assistant":
        message = getattr(msg, "message", None)
        content = getattr(message, "content", ()) if message is not None else ()
        ev = {"type": "assistant",
              "message": {"role": "assistant", "content": [_block(b) for b in content or ()]}}
        for name in ("agent_id", "run_id"):
            value = getattr(msg, name, None)
            if value:
                ev[name] = value
        return ev
    if typ == "user":
        message = getattr(msg, "message", None)
        content = getattr(message, "content", ()) if message is not None else ()
        ev = {"type": "user",
              "message": {"role": "user", "content": [_block(b) for b in content or ()]}}
        for name in ("agent_id", "run_id"):
            value = getattr(msg, name, None)
            if value:
                ev[name] = value
        return ev
    if typ == "thinking":
        return _pick(msg, "type", "agent_id", "run_id", "text", "thinking_duration_ms")
    if typ == "tool_call":
        return _pick(msg, "type", "agent_id", "run_id", "call_id", "name", "status",
                     "args", "result", "truncated")
    if typ == "usage":
        ev = {"type": "usage", "usage": usage_dict(getattr(msg, "usage", None))}
        for name in ("agent_id", "run_id"):
            value = getattr(msg, name, None)
            if value:
                ev[name] = value
        return ev
    if typ in ("status", "task", "request", "summary"):
        return _pick(msg, "type", "agent_id", "run_id", "status", "message", "text",
                     "request_id", "fallback")
    return {"type": str(typ or "unknown")}


def _frames(run):
    """SDK events when the run exposes them, otherwise the message iterator.

    ``events()`` carries ``shell-output-delta`` frames. ``messages()`` drops
    every frame that is not an ``sdk_message``.
    """
    events = getattr(run, "events", None)
    if callable(events):
        yield from events()
        return
    yield from run.messages()


def frame_dicts(frame) -> list[dict]:
    """JSON objects to write for one run frame.

    Shell output deltas are kept. Text, thinking, and step deltas are not:
    the message stream already carries that text.
    """
    update = getattr(frame, "interaction_update", None)
    if update is not None:
        if getattr(update, "type", None) != "shell-output-delta":
            return []
        event = _plain(getattr(update, "event", None) or {})
        if not isinstance(event, dict):
            event = {}
        return [{"type": "shell_output", "event": event}]
    msg = getattr(frame, "sdk_message", None)
    if msg is not None:
        return [event_dict(msg)]
    if getattr(frame, "type", None):
        return [event_dict(frame)]
    return []


def _scrub(text, key: str) -> str:
    text = str(text or "")
    if key:
        text = text.replace(key, "[redacted]")
    return text[:2000]


def _write(out, obj) -> None:
    out.write(json.dumps(obj, ensure_ascii=False) + "\n")
    flush = getattr(out, "flush", None)
    if flush:
        flush()


def _fail(out, err, message: str, *, code: str) -> int:
    if err is not None:
        err.write(message + "\n")
        flush = getattr(err, "flush", None)
        if flush:
            flush()
    _write(out, {"type": "result", "subtype": "error", "is_error": True, "result": message,
                 "session_id": None, "usage": {}, "total_cost_usd": None, "error_code": code})
    return 1


def _money(cost):
    """``(usd, raw_cents, charged_cents)``. USD prefers raw cents, else charged.
    A present cost object with zeros is a reported 0, not a missing lookup."""
    if cost is None:
        return None, None, None
    if isinstance(cost, dict):
        raw, charged = cost.get("raw_cost_cents"), cost.get("charged_cents")
    else:
        raw, charged = getattr(cost, "raw_cost_cents", None), getattr(cost, "charged_cents", None)
    raw_f = float(raw or 0)
    charged_f = float(charged or 0)
    cents = raw_f if raw_f else charged_f
    return cents / 100.0, raw_f, charged_f


def _fetch_cost(agent):
    try:
        got = agent.get_usage()
    except Exception as e:
        if "feature_unavailable" in str(e):
            return None, "feature_unavailable"
        return None, type(e).__name__
    if isinstance(got, dict):
        return got.get("cost"), None
    return getattr(got, "cost", None), None


def _result_event(agent, result, cost, cost_error) -> dict:
    status = getattr(result, "status", None)
    ok = status == "finished"
    usd, raw, charged = _money(cost)
    usage = getattr(result, "usage", None)
    ev = {
        "type": "result",
        "subtype": "success" if ok else "error",
        "is_error": not ok,
        "result": getattr(result, "result", "") or "",
        "session_id": getattr(agent, "agent_id", None),
        "sdk_run_id": getattr(result, "id", None),
        "duration_ms": getattr(result, "duration_ms", None) or 0,
        "usage": usage_dict(usage),
        "total_cost_usd": usd,
    }
    if cost is not None:
        ev["cost"] = {"raw_cost_cents": raw, "charged_cents": charged}
    if cost_error:
        ev["cost_error"] = cost_error
    for name, attr in (("error_code", "error_code"), ("error_message", "error_message")):
        value = getattr(result, attr, None)
        if value:
            ev[name] = value
    return ev


def _flags(argv) -> tuple[bool, list[str], str | None]:
    read_only = False
    dirs: list[str] = []
    resume = None
    rest = list(argv or [])
    while rest:
        token = rest.pop(0)
        if token == "--read-only":
            read_only = True
        elif token == "--add-dir":
            if not rest:
                raise ValueError("--add-dir needs a path")
            dirs.append(rest.pop(0))
        elif token == "--resume":                # SPEC-0556
            if not rest or not rest[0].strip():
                raise ValueError("--resume needs an agent id")
            resume = rest.pop(0)
        else:
            raise ValueError(f"unknown argument {token}")
    return read_only, dirs, resume


def _agent_options(options):
    from cursor_sdk import AgentOptions, LocalAgentOptions
    local = LocalAgentOptions(
        cwd=options["cwd"],
        dirs=options["dirs"] or None,
        setting_sources=list(options["setting_sources"]),
    )
    return AgentOptions(
        model=options["model"],
        api_key=options["api_key"],
        name=options.get("name") or None,
        local=local,
        tools=agent_tools(options["read_only"]),
    )


def _default_create(options):
    from cursor_sdk import Agent
    return Agent.create(_agent_options(options))


def _default_resume(agent_id, options):
    """SPEC-0556: the SDK's `Agent.resume(agent_id, options)`."""
    from cursor_sdk import Agent
    return Agent.resume(agent_id, _agent_options(options))


def execute(run_dir, *, argv=None, environ=None, out=None, err=None,
            agent_create=None, holder=None, agent_resume=None) -> int:
    """Run one local agent for ``run_dir``. Returns 0 when the run finished."""
    run_dir = Path(run_dir)
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    env = os.environ if environ is None else environ
    key = (env.get("CURSOR_API_KEY") or "").strip()
    try:
        flag_read_only, dirs, resume = _flags(argv)
    except ValueError as e:
        return _fail(out, err, str(e), code="startup")
    try:
        req = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _fail(out, err, "request.json is missing or not JSON", code="startup")
    if not isinstance(req, dict):
        return _fail(out, err, "request.json is missing or not JSON", code="startup")
    model = req.get("model") or ""
    if not model:
        return _fail(out, err, "request.json has no model", code="startup")
    try:
        prompt = (run_dir / "prompt.md").read_text(encoding="utf-8")
    except OSError:
        return _fail(out, err, "prompt.md is missing", code="startup")
    if not key:
        return _fail(out, err, "CURSOR_API_KEY is not set", code="startup")
    if agent_create is None or (resume and agent_resume is None):
        try:
            import cursor_sdk  # noqa: F401
        except ImportError as e:
            return _fail(out, err, f"cursor-sdk is not installed ({e})", code="startup")
        agent_create = agent_create or _default_create
        agent_resume = agent_resume or _default_resume
    persona = req.get("persona") or ""
    options = {
        "model": sdk_selection(model),
        "api_key": key,
        "cwd": req.get("cwd") or ".",
        "dirs": dirs,
        "read_only": bool(req.get("read_only")) or flag_read_only,
        "setting_sources": ["project"],
        "name": f"nw-{persona}" if persona else None,
    }
    agent = None
    wrote = False
    try:
        agent = agent_resume(resume, options) if resume else agent_create(options)
        run = agent.send(prompt)
        if holder is not None:
            holder["run"] = run
        for frame in _frames(run):
            for obj in frame_dicts(frame):
                _write(out, obj)
        result = run.wait()
        cost, cost_error = _fetch_cost(agent)
        _write(out, _result_event(agent, result, cost, cost_error))
        wrote = True
        return 0 if getattr(result, "status", None) == "finished" else 1
    except Exception as e:
        if not wrote:
            _fail(out, err, _scrub(e, key), code=type(e).__name__)
        return 1
    finally:
        close = getattr(agent, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].startswith("-"):
        # Same shape as runproc's diagnostics: stderr of a supervised child, not a TTY render.
        os.write(2, b"usage: python -m nightwright.agentic.cursor_exec RUN_DIR "
                 b"[--read-only] [--add-dir DIR] [--resume AGENT_ID]\n")
        return 2
    holder: dict = {}

    def _term(signum, frame):
        run = holder.get("run")
        cancel = getattr(run, "cancel", None)
        if callable(cancel):
            try:
                cancel()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, _term)
    try:
        return execute(args[0], argv=args[1:], holder=holder)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
