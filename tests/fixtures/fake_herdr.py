"""A fake `herdr` 0.9.5 for tests (SPEC-0361/0363/0364/0366). Logs argv as one JSON line to
$FAKE_HERDR_LOG; $FAKE_HERDR_MODE = ok | fail | hang.

It models the real parser of every command nightwright uses: the positional id right after the
subcommand (herdr rejects options before it with `unknown option`, exit 2, although its
`--help` lists the id last), the options each command takes, and the JSON replies nightwright
reads, copied from herdr 0.9.5 output (`tab create` -> result.tab + result.root_pane, `tab get`,
`pane split` -> result.pane, `pane layout`, `pane get`, `pane process-info`). Tabs and panes
live in $FAKE_HERDR_STATE (JSON); closing a tab's last pane closes the tab, like herdr. Server
errors are JSON on stderr with exit 1 (`pane_not_found`). Panes listed in the state's
`fresh_shell` have only their shell in the foreground."""
import json
import os
import sys
import time

argv = sys.argv[1:]
mode = os.environ.get("FAKE_HERDR_MODE", "ok")
with open(os.environ["FAKE_HERDR_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
if mode == "hang":
    time.sleep(30)
if mode == "fail":
    sys.exit(1)

STATE = os.environ.get("FAKE_HERDR_STATE")


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, TypeError, ValueError):
        return {"n": 0, "tabs": {}, "panes": {}, "closed": [], "fresh_shell": []}


def save(st):
    if STATE:
        with open(STATE, "w") as f:
            json.dump(st, f)


def usage(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)


def server_error(code, msg, cid):
    sys.stderr.write(json.dumps({"error": {"code": code, "message": msg}, "id": cid}) + "\n")
    sys.exit(1)


def reply(cid, result):
    print(json.dumps({"id": cid, "result": result}))
    sys.exit(0)


def parse(rest, flags, *, positional=0):
    """`positional` ids first (none may start with '-'), then only `flags` (name -> arity)."""
    if len(rest) < positional or any(a.startswith("-") for a in rest[:positional]):
        bad = next((a for a in rest if a.startswith("-")), "")
        usage(f"unknown option: {rest[positional - 1] if len(rest) >= positional and positional else bad}")
    pos, opts, i = rest[:positional], {}, positional
    while i < len(rest):
        name = rest[i]
        if name not in flags:
            usage(f"unknown option: {name}")
        if flags[name]:
            if i + 1 >= len(rest):
                usage(f"missing value for {name}")
            opts.setdefault(name, []).append(rest[i + 1])
        else:
            opts.setdefault(name, []).append(True)
        i += 1 + flags[name]
    return pos, opts


def pane_info(st, pid):
    p = st["panes"][pid]
    out = {"agent_status": p.get("agent_status", "unknown"), "cwd": p["cwd"], "focused": False,
           "foreground_cwd": p["cwd"], "pane_id": pid, "revision": 0,
           "scroll": {"max_offset_from_bottom": 0, "offset_from_bottom": 0, "viewport_rows": 38},
           "tab_id": p["tab"], "terminal_id": "term_" + pid.replace(":", "_"),
           "workspace_id": pid.split(":")[0]}
    for k in ("label", "agent", "display_agent"):
        if p.get(k) is not None:
            out[k] = p[k]
    return out


def need_pane(st, pid, cid):
    if pid not in st["panes"]:
        server_error("pane_not_found", f"pane {pid} not found", cid)


def tab_info(st, tid):
    t = st["tabs"][tid]
    return {"agent_status": "unknown", "focused": False, "label": t["label"],
            "number": int(tid.split(":t")[1]), "pane_count": len(t["panes"]), "tab_id": tid,
            "workspace_id": tid.split(":")[0]}


group, cmd, rest = (argv + ["", ""])[0], (argv + ["", ""])[1], argv[2:]
st = load()

if cmd == "report-metadata" and group in ("pane", "workspace"):
    flags = {"--source": 1, "--seq": 1, "--ttl-ms": 1, "--token": 1, "--clear-token": 1}
    if group == "pane":
        flags.update({"--display-agent": 1, "--clear-display-agent": 0, "--title": 1,
                      "--clear-title": 0, "--state-label": 1, "--clear-state-labels": 0,
                      "--agent": 1, "--applies-to-source": 1})
    if not rest or rest[0].startswith("-"):
        usage("unknown option: nw")
    (target,), opts = parse(rest, flags, positional=1)
    if group == "pane" and target in st["closed"]:
        server_error("pane_not_found", f"pane {target} not found", "cli:pane:report_metadata")
    if group == "pane" and target in st["panes"]:
        if "--display-agent" in opts:
            st["panes"][target]["display_agent"] = opts["--display-agent"][-1]
        if "--clear-display-agent" in opts:
            st["panes"][target].pop("display_agent", None)
        save(st)
    sys.exit(0)

if group == "notification" and cmd == "show":
    parse(rest, {"--body": 1, "--sound": 1}, positional=1)
    sys.exit(0)

if group == "tab" and cmd == "create":
    _, opts = parse(rest, {"--workspace": 1, "--cwd": 1, "--label": 1, "--env": 1, "--focus": 0,
                           "--no-focus": 0})
    ws = (opts.get("--workspace") or ["w1"])[-1]
    st["n"] += 1
    tid, pid = f"{ws}:t{st['n']}", f"{ws}:p{st['n']}"
    st["tabs"][tid] = {"label": (opts.get("--label") or [str(st["n"])])[-1], "panes": [pid],
                       "focus": "--focus" in opts}
    st["panes"][pid] = {"tab": tid, "cwd": (opts.get("--cwd") or ["/"])[-1],
                        "rect": [0, 0, 120, 40]}
    save(st)
    reply("cli:tab:create", {"root_pane": pane_info(st, pid), "tab": tab_info(st, tid),
                             "type": "tab_created"})

if group == "tab" and cmd == "get":
    (tid,), _ = parse(rest, {}, positional=1)
    if tid not in st["tabs"]:
        server_error("tab_not_found", f"tab {tid} not found", "cli:tab:get")
    reply("cli:tab:get", {"tab": tab_info(st, tid), "type": "tab_info"})

if group == "tab" and cmd == "rename":
    (tid, label), _ = parse(rest, {}, positional=2)
    if tid not in st["tabs"]:
        server_error("tab_not_found", f"tab {tid} not found", "cli:tab:rename")
    st["tabs"][tid]["label"] = label
    save(st)
    reply("cli:tab:rename", {"tab": tab_info(st, tid), "type": "tab_info"})

if group == "pane" and cmd == "split":
    (target,), opts = parse(rest, {"--direction": 1, "--ratio": 1, "--cwd": 1, "--env": 1,
                                   "--right-click": 1, "--focus": 0, "--no-focus": 0},
                            positional=1)
    direction = (opts.get("--direction") or [""])[-1]
    if direction not in ("right", "down"):
        usage("--direction must be right or down")
    need_pane(st, target, "cli:pane:split")
    t = st["panes"][target]
    x, y, w, h = t["rect"]
    if direction == "right":
        t["rect"], new = [x, y, w // 2, h], [x + w // 2, y, w - w // 2, h]
    else:
        t["rect"], new = [x, y, w, h // 2], [x, y + h // 2, w, h - h // 2]
    st["n"] += 1
    pid = f"{target.split(':')[0]}:p{st['n']}"
    st["panes"][pid] = {"tab": t["tab"], "cwd": (opts.get("--cwd") or ["/"])[-1], "rect": new,
                        "focus": "--focus" in opts}
    st["tabs"][t["tab"]]["panes"].append(pid)
    save(st)
    reply("cli:pane:split", {"pane": pane_info(st, pid), "type": "pane_info"})

if group == "pane" and cmd in ("layout", "process-info"):
    _, opts = parse(rest, {"--pane": 1, "--current": 0})
    pid = (opts.get("--pane") or [""])[-1]
    need_pane(st, pid, f"cli:pane:{cmd}")
    if cmd == "process-info":
        fg = 100 if pid in st["fresh_shell"] else 200
        reply("cli:pane:process_info", {"process_info": {
            "foreground_process_group_id": fg, "foreground_processes": [], "pane_id": pid,
            "shell_pid": 100}, "type": "pane_process_info"})
    tid = st["panes"][pid]["tab"]
    panes = [{"focused": False, "pane_id": q,
              "rect": dict(zip(("x", "y", "width", "height"), st["panes"][q]["rect"]))}
             for q in st["tabs"][tid]["panes"]]
    reply("cli:pane:layout", {"layout": {"area": {"height": 40, "width": 120, "x": 0, "y": 0},
                                         "panes": panes, "splits": [], "tab_id": tid,
                                         "workspace_id": tid.split(":")[0], "zoomed": False},
                              "type": "pane_layout"})

if group == "pane" and cmd in ("get", "close"):
    (pid,), _ = parse(rest, {}, positional=1)
    need_pane(st, pid, f"cli:pane:{cmd}")
    if cmd == "get":
        reply("cli:pane:get", {"pane": pane_info(st, pid), "type": "pane_info"})
    tid = st["panes"].pop(pid)["tab"]
    st["closed"].append(pid)
    st["tabs"][tid]["panes"].remove(pid)
    if not st["tabs"][tid]["panes"]:
        del st["tabs"][tid]
    save(st)
    reply("cli:pane:close", {"type": "ok"})

if group == "pane" and cmd == "rename":
    (pid, label), _ = parse(rest, {}, positional=2)
    need_pane(st, pid, "cli:pane:rename")
    st["panes"][pid]["label"] = label
    save(st)
    reply("cli:pane:rename", {"pane": pane_info(st, pid), "type": "pane_info"})

if group == "pane" and cmd == "run":
    (pid, command), _ = parse(rest, {}, positional=2)
    need_pane(st, pid, "cli:pane:run")
    st["panes"][pid].setdefault("typed", []).append(command)
    if pid in st["fresh_shell"]:
        st["fresh_shell"].remove(pid)
    save(st)
    reply("cli:pane:run", {"type": "ok"})

if group == "pane" and cmd == "report-agent":
    (pid,), opts = parse(rest, {"--source": 1, "--agent": 1, "--state": 1, "--message": 1,
                                "--seq": 1, "--agent-session-id": 1, "--agent-session-path": 1},
                         positional=1)
    if not opts.get("--source") or not opts.get("--agent") or not opts.get("--state"):
        usage("--source, --agent and --state are required")
    if opts["--state"][-1] not in ("idle", "working", "blocked", "unknown"):
        usage("invalid --state")
    need_pane(st, pid, "cli:pane:report_agent")
    st["panes"][pid]["agent"] = opts["--agent"][-1]
    st["panes"][pid]["agent_status"] = opts["--state"][-1]
    save(st)
    sys.exit(0)

if group == "pane" or group == "tab":
    usage(f"unknown command: {group} {cmd}")
sys.exit(0)
