#!/usr/bin/env python3
"""Stand-in for `claude -p` (SPEC-0225 tests). Put on PATH as `claude`.

Env: FAKE_CLAUDE_SLEEP_S sleep that long, or until a `release` file exists in the cwd;
FAKE_CLAUDE_VERDICT JSON of the structured output; FAKE_CLAUDE_COST total_cost_usd;
FAKE_CLAUDE_STDOUT raw stdout instead of a result JSON; FAKE_CLAUDE_RC exit code;
FAKE_CLAUDE_STUBBORN ignore SIGTERM; FAKE_CLAUDE_TERM_EXIT catch SIGTERM and exit with that code; FAKE_CLAUDE_RELEASE path of the release file (default ./release); FAKE_CLAUDE_RECORD path for {argv, stdin, cwd, env};
FAKE_CLAUDE_EDITS JSON [{path, write}] and FAKE_CLAUDE_EDIT a file to write in the cwd (an implementer's work);
FAKE_CLAUDE_BY_ARGV JSON [[substring, {ENV: value}]] env overrides for runs whose argv contains
the substring (different reviewers behave differently). Writes `child.pid`."""
import json
import os
import signal
import sys
import time

stdin = sys.stdin.read()
for _needle, _env in json.loads(os.environ.get("FAKE_CLAUDE_BY_ARGV") or "[]"):
    if _needle in " ".join(sys.argv):
        os.environ.update(_env)
with open("child.pid", "w") as f:
    f.write(str(os.getpid()))
if os.environ.get("FAKE_CLAUDE_RECORD"):
    with open(os.environ["FAKE_CLAUDE_RECORD"], "w") as f:
        json.dump({"argv": sys.argv, "stdin": stdin, "cwd": os.getcwd(),
                   "env": {k: v for k, v in os.environ.items() if k.startswith("WT_")}}, f)
if os.environ.get("FAKE_CLAUDE_STUBBORN"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if os.environ.get("FAKE_CLAUDE_TERM_EXIT"):
    signal.signal(signal.SIGTERM, lambda *_: os._exit(int(os.environ["FAKE_CLAUDE_TERM_EXIT"])))
if os.environ.get("FAKE_CLAUDE_EDIT"):
    with open(os.environ["FAKE_CLAUDE_EDIT"], "w") as f:
        f.write("edited\n")
if os.environ.get("FAKE_CLAUDE_STDOUT") is not None:      # partial output, before the wait
    sys.stdout.write(os.environ["FAKE_CLAUDE_STDOUT"])
    sys.stdout.flush()
for edit in json.loads(os.environ.get("FAKE_CLAUDE_EDITS") or "[]"):    # [{path, write}]
    os.makedirs(os.path.dirname(edit["path"]) or ".", exist_ok=True)
    with open(edit["path"], "w") as f:
        f.write(edit["write"])
sleep_s = float(os.environ.get("FAKE_CLAUDE_SLEEP_S") or 0)
end = time.monotonic() + sleep_s
while time.monotonic() < end and not os.path.exists(os.environ.get("FAKE_CLAUDE_RELEASE") or "release"):
    time.sleep(0.05)
if os.environ.get("FAKE_CLAUDE_STDOUT") is None:
    verdict = json.loads(os.environ.get("FAKE_CLAUDE_VERDICT") or
                         '{"status": "done", "summary": "implemented"}')
    cost = os.environ.get("FAKE_CLAUDE_COST", "0.05")
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "result": "done", "session_id": "sess-1", "usage": {},
                      "total_cost_usd": float(cost) if cost != "none" else None,
                      "structured_output": verdict}))
sys.exit(int(os.environ.get("FAKE_CLAUDE_RC") or 0))
