"""`nw queue gates` (SPEC-0173), `nw queue add|list|watch|tick|run|park|resume|abandon`
(SPEC-0176), `nw queue backlog|prioritize` (SPEC-0180) and `nw queue gc` (SPEC-0275). `attach`
is called by `nightwright.agentic.cli.register` with the `queue` group.

Conventions (as elsewhere in nightwright): destructive commands are dry-run by default and write only
with `--yes`; `--json` emits `{"schema": "nightwright.queue.<x>.v1", …plan, "written": bool}`; library
`ValueError`s become `click.ClickException`s."""
from __future__ import annotations

import json
import os
import socket
import sys
import time

import click
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import completion as C
from .. import render as R
from ..config import load_config
from ..console import console
from ..report import _dump_json
from . import flaky as FL
from . import gate_progress as GPG
from . import gates as G
from . import journey as JY
from . import lease as L
from . import listview as LV
from . import mailbox as M
from . import project as P
from . import decisions as DEC
from . import queue as Q
from . import restart as RS
from . import store
from .gitops import GitError

_NOT_WRITTEN = "[yellow]not written — re-run with --yes to commit[/]"


def _actor(cfg, by) -> str:
    """SPEC-0273: who a human-command acts as — `--by`, else $NW_MAILBOX_ADDR, else human.
    ValueError on a bad address (callers run it inside their ValueError → ClickException)."""
    return M.validate_address(cfg, by or os.environ.get("NW_MAILBOX_ADDR") or "human")


def _decision_request(by, decision, rule, kind, *, default_kind, required):
    """SPEC-0318: the decision record a park / resume / abandon carries, as the validated request
    (`decisions.validate_request`) or None. A human needs none and may not pass the options
    (records are `by: conductor|queue|steward:<P>`). `required`: a non-human actor must pass
    `--decision` and `--rule`; otherwise they come together or not at all. ValueError."""
    given = decision is not None or rule is not None or kind is not None
    if not DEC.needs_decision(by):
        if given:
            raise ValueError("a human's park, resume or abandon needs no decision record: drop "
                             "--decision / --rule / --kind (records are by conductor, queue or "
                             "steward:<P>)")
        return None
    if not given and not required:
        return None
    if decision is None or rule is None:
        raise ValueError(f"as {by}, pass both --decision TEXT and --rule ID (the /nw-conduct "
                         f"table row; --kind K optional)")
    return DEC.validate_request(kind or default_kind, rule, decision, by)


def _age_txt(age) -> str:
    return f"{age}s" if age is not None and age < 120 else (f"{age // 60}m" if age else "")


def _size_tone(it: dict) -> str:
    """`m/high-end`, or `-` for a campaign without sizing (SPEC-0219)."""
    size, tone = it.get("size"), it.get("tone")
    return f"{size or '?'}/{tone or '?'}" if size or tone else "-"


def _profile_text(it: dict) -> Text:
    """The profile name, then a dim `(flag)`/`(default)` when it was not picked by the review."""
    t = Text(str(it.get("profile") or "-"))
    src = it.get("profile_source")
    if src and src != "review":
        t.append(f" ({src})", style="dim")
    return t


def _source_marker(it: dict) -> Text:
    src = it.get("profile_source")
    return Text(f" ({src})", style="dim") if src and src != "review" else Text()


def _exit_leftover() -> None:
    """Worker threads outlived `Q.run` (SPEC-0229): flush and leave with status 70; executor
    threads are not daemonic, so a normal interpreter exit would join them."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(70)


# ---- list tables (SPEC-0356: one column fitter, ids and states never cut) -----------------------

def _col(key, header, **kw) -> LV.Column:
    return LV.Column(key, header, rank=kw.pop("rank", LV.RANK.get(key, 0)), **kw)


def _list_title(it: dict) -> Text:
    return Text(str(it.get("title") or "—"))


def _blocked_cell(it: dict) -> Text:
    """`✓ ready`, or `⧗` + what the item waits on: the dependency ids parsed from `blocked_by`
    (SPEC-0356 `waiting_on`), other blockers (status, branch, idea) verbatim."""
    blocked = it.get("blocked_by") or []
    if not blocked:
        return Text("✓ ready", style="green") if (it.get("backlog") or {}).get("ready") else Text()
    parts = []
    for b in blocked:
        ids = Q.waiting_on_ids([b])
        parts.extend(i for i in (ids or [b]) if i not in parts)
    return Text("⧗ " + ", ".join(parts), style="red")


def _cost_cell(it: dict) -> str:
    est = (it.get("cost") or {}).get("estimated_usd") or 0.0
    return (f"${it['cost_usd']:.2f}" + (f" (~${est:.2f} est.)" if est > 0 else "")
            + (f"/{it['cap_usd']:.0f}" if it.get("cap_usd") else ""))


def _width(width) -> int:
    return width or console.width


def _queue_table(payload: dict, width: int | None = None) -> Group:
    """The human listing: an Active table (the slots), Needs you, Waiting and a Backlog table
    (admission order), each fitted to `width` by `listview` (SPEC-0356): ids and states never
    cut, a title on every row, low-value columns dropped first; the park reason has its own
    column (folded into the title when there is no room)."""
    width = _width(width)
    lease = payload["lease"]
    slots = payload["slots"]
    if lease.get("held"):
        hb = lease.get("heartbeat_age_s")
        stale = " [red]STALE[/]" if lease.get("stale") else ""
        lease_txt = f"lease pid {lease.get('pid')} (heartbeat {hb}s ago){stale}"
    else:
        lease_txt = "no loop running"
    active = [it for it in payload["items"] if it["state"] not in ("QUEUED", "WAITING")]
    waiting = [it for it in payload["items"] if it["state"] == "WAITING"]
    backlog = sorted((it for it in payload["items"] if it["state"] == "QUEUED"),
                     key=lambda it: it["backlog"]["position"])
    rows = []
    for it in active:
        park = it.get("park") or {}
        state = it["state"]
        if state in ("SPEC_WRITING", "SPEC_REVIEW") and it.get("spec_round"):
            state = f"{state} r{it['spec_round']}"      # SPEC-0211
        if it.get("run"):
            state = f"{state} ({it['run']})"           # SPEC-0229: run: detached (pid N)
        if not park.get("reason") and it.get("park_requested"):
            state = f"{state}:park?"
        color = {"PARKED": "yellow", "MERGED": "green", "HANDED_OFF": "green",
                 "ABANDONED": "dim"}.get(it["state"])
        conductors = it["state"] == "PARKED" and park.get("owner") != "human"
        if conductors:
            color = "dim"           # SPEC-0222: the conductor's, not the human's
        cell = Text().append(state, style=color or "")
        if conductors and park.get("overdue"):
            cell.append(" ").append("!", style="red")
        rows.append({"campaign": _campaign_cell(it), "state": cell, "title": _list_title(it),
                     "blocked_by": _blocked_cell(it),
                     "park": Text(str(it.get("park_reason") or ""), style="yellow"),
                     "pri": str(it.get("priority")), "cost": _cost_cell(it),
                     "project": R.project_text(it.get("project")), "loops": str(it["loops"]),
                     "size": it.get("size") or "-", "tone": it.get("tone") or "-",
                     "profile": _profile_text(it), "age": _age_txt(it.get("updated_age_s"))})
    cols = [_col("campaign", "campaign", fixed=True), _col("state", "state", fixed=True),
            _col("title", "title", flex=True),
            _col("blocked_by", "blocked by", fold="{} ", hide_empty=True),
            _col("park", "park", fold="[{}] ", hide_empty=True),
            _col("pri", "pri", justify="right"), _col("cost", "cost"),
            _col("project", "project"), _col("loops", "loops"),
            _col("size", "size", rank=7), _col("tone", "tone", rank=7),
            _col("profile", "profile", rank=7), _col("age", "age")]
    parts = [LV.render(f"Active ({slots['busy']}/{slots['total']}) · {lease_txt}", cols, rows,
                       width)]
    needs_you = [it for it in active if it["state"] == "PARKED"
                 and (it.get("park") or {}).get("owner") == "human"]
    if needs_you:
        parts.append(_list_needs_you(needs_you, width))
    if waiting:
        parts.append(_waiting_table(waiting, width))
    return Group(*parts, _backlog_table(backlog, width))


def _list_needs_you(items: list, width: int | None = None) -> Table:
    """SPEC-0222: parks only the human can resolve."""
    rows = []
    for it in items:
        park = it.get("park") or {}
        rows.append({"campaign": _campaign_cell(it), "title": _list_title(it),
                     "reason": str(park.get("reason") or "?"),
                     "detail": Text(str(park.get("detail") or ""), style="dim"),
                     "resume": Text(str(park.get("resume_to") or ""),
                                    style=R.state_style(park.get("resume_to")))})
    cols = [_col("campaign", "campaign", fixed=True), _col("title", "title", flex=True),
            _col("reason", "reason", fixed=True, style="yellow"),
            _col("detail", "detail", flex=True), _col("resume", "resume to", rank=3)]
    return LV.render(f"Needs you ({len(items)})", cols, rows, _width(width),
                     title_style="yellow")


def _waiting_table(items: list, width: int | None = None) -> Table:
    """Started campaigns that need a slot (SPEC-0200): next in line, ahead of the backlog."""
    rows = []
    for it in items:
        w = it.get("waiting") or {}
        rows.append({"campaign": _campaign_cell(it),
                     "state": Text(str(w.get("resume_to") or "?"),
                                   style=R.state_style(w.get("resume_to"))),
                     "title": _list_title(it), "pri": str(it.get("priority")),
                     "project": R.project_text(it.get("project")), "loops": str(it["loops"]),
                     "size": _size_tone(it), "age": _age_txt(it.get("updated_age_s"))})
    cols = [_col("campaign", "campaign", fixed=True), _col("state", "continues at", fixed=True),
            _col("title", "title", flex=True), _col("pri", "pri", justify="right"),
            _col("project", "project"), _col("loops", "loops"), _col("size", "size"),
            _col("age", "waiting", justify="right")]
    return LV.render(f"Waiting for a slot ({len(items)})", cols, rows, _width(width))


def _backlog_table(items: list, width: int | None = None) -> Table:
    """The backlog section: `items` are `nw.queue.v1` queued items (each has `backlog`)."""
    rows = []
    for it in items:
        b = it["backlog"]
        rows.append({"#": str(b["position"]), "campaign": _campaign_cell(it),
                     "state": "QUEUED", "title": _list_title(it),
                     "blocked_by": _blocked_cell({**it, "blocked_by": b["blocked_by"]}),
                     "pri": str(b["priority"]), "project": R.project_text(it.get("project")),
                     "size": _size_tone(it), "age": _age_txt(it.get("age_s"))})
    cols = [_col("#", "#", fixed=True, justify="right"), _col("campaign", "campaign", fixed=True),
            _col("state", "state", fixed=True), _col("title", "title", flex=True),
            _col("blocked_by", "waiting on", fold="{} ", hide_empty=True),
            _col("pri", "pri", justify="right"), _col("project", "project"),
            _col("size", "size"), _col("age", "age")]
    return LV.render(f"Backlog ({len(items)})", cols, rows, _width(width))


def _graph_line(n: dict, nodes: dict, room: int = 10_000) -> Text:
    """`SPEC-0338  IMPLEMENTING  <title>  $3.20/15  pri 0  ⧗ waiting on SPEC-0337`; the title is
    ellipsized so the line fits in `room` cells (min 12 for the title)."""
    title = n.get("title") or ""
    if title:
        rest = len(_graph_line({**n, "title": None}, nodes).plain) + 2
        cap = max(12, room - rest)
        if len(title) > cap:
            title = title[:cap - 1] + "…"
    n = {**n, "title": title}
    dim = n["stub"] and n.get("spec_status") in ("done", "superseded") or \
        n.get("state") in ("MERGED", "HANDED_OFF", "ABANDONED")
    t = Text(n["id"], style="dim" if dim else "bold")
    status = n.get("state") or n.get("spec_status") or "?"
    t.append(f"  {status}", style="dim" if dim else R.state_style(n.get("state")) or
             ("red" if n["stub"] and n.get("spec_status") not in ("done", "superseded") else ""))
    if n.get("title"):
        t.append(f"  {n['title']}", style="dim")
    if n.get("cost_usd") is not None and not n["stub"]:
        t.append(f"  ${n['cost_usd']:.2f}" + (f"/{n['cap_usd']:.0f}" if n.get("cap_usd") else ""))
    if n.get("priority") is not None and not n["stub"]:
        t.append(f"  pri {n['priority']}")
    if n.get("waiting_on"):
        red = any(nodes.get(w, {}).get("stub") and not nodes.get(w, {}).get("campaign")
                  for w in n["waiting_on"])
        t.append("  ⧗ waiting on " + ", ".join(n["waiting_on"]), style="red" if red else "yellow")
    if n.get("park_reason"):
        t.append(f"  [parked: {n['park_reason']}]", style="yellow")
    if n.get("external"):
        t.append("  (external)", style="dim")
    return t


def _graph_render(g: dict, width: int) -> Group:
    """SPEC-0356: roots first; a node with several parents in full under the first, as
    `↑ ID (above)` elsewhere, so the output is linear in edges. Cycle members print once, apart."""
    from rich.tree import Tree
    nodes = {n["id"]: n for n in g["nodes"]}
    order = {n["id"]: i for i, n in enumerate(g["nodes"])}
    kids: dict = {}
    for e in g["edges"]:
        kids.setdefault(e["from"], []).append(e["to"])
    in_cycle = {m for c in g["cycles"] for m in c}
    printed: set = set()
    tree = Tree(Text(f"Queue graph ({sum(1 for n in g['nodes'] if not n['stub'])} campaigns, "
                     f"{sum(1 for n in g['nodes'] if n['stub'])} stubs)", style="bold"),
                guide_style="dim")
    stack = [(tree, r) for r in reversed(g["roots"])]
    depth = {id(tree): 0}
    while stack:
        parent, nid = stack.pop()
        if nid in printed:
            parent.add(Text(f"↑ {nid} (above)", style="dim"))
            continue
        printed.add(nid)
        branch = parent.add(_graph_line(nodes[nid], nodes, width - 4 * depth[id(parent)] - 4))
        depth[id(branch)] = depth[id(parent)] + 1
        for child in sorted((k for k in kids.get(nid, []) if k not in in_cycle),
                            key=lambda k: order[k], reverse=True):
            stack.append((branch, child))
    parts = [tree]
    if g["cycles"]:
        cyc = Tree(Text("Cycles", style="bold red"), guide_style="dim")
        for c in g["cycles"]:
            sub = cyc.add(Text(" ⇄ ".join(c), style="red"))
            for nid in c:
                sub.add(_graph_line(nodes[nid], nodes, width - 8))
        parts.append(cyc)
    return Group(*parts)


def _unqueued_table(items: list, width: int) -> Table:
    """SPEC-0356: id / status / title never dropped; `ready` rows green."""
    rows = []
    for it in items:
        codes = [r["code"] for r in it["reasons"]]
        style = "green" if codes == ["ready"] else ""
        rows.append({"id": Text(it["id"], style=style),
                     "status": str(it.get("status") or it.get("state") or ""),
                     "title": Text(str(it.get("title") or "—")),
                     "reason": Text("; ".join(r["text"] for r in it["reasons"]), style=style),
                     "project": R.project_text(it.get("project")),
                     "next": Text(it["next"], style="dim")})
    cols = [_col("id", "id", fixed=True), _col("status", "status", fixed=True),
            _col("title", "title", flex=True), _col("reason", "reason", rank=1, fold="{} — "),
            _col("project", "project", rank=5), _col("next", "next", rank=8)]
    return LV.render(f"Not queued ({len(items)})", cols, rows, width)


# ---- dashboard (nw queue watch, SPEC-0185) ------------------------------------------------------

def _dur(s) -> str:
    """Compact duration: 45s, 4m12s, 2h05m, 3d4h."""
    if s is None:
        return "—"
    s = int(float(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 86400}d{s % 86400 // 3600}h"


def _bar(frac: float, width: int = 12, style: str = "green") -> Text:
    frac = max(0.0, min(1.0, frac))
    full = round(frac * width)
    if frac >= 0.9:
        style = "red"
    elif frac >= 0.6:
        style = "yellow"
    return Text.assemble(("━" * full, style), ("━" * (width - full), "grey30"))


def _spec_cell(spec_id, title) -> Text:
    t = Text(str(spec_id or "?"), style="bold")
    if title:
        t.append("  ")
        t.append(title, style="dim")
    return t


_SPEC_CELL_MAX = 60


def _idea_spec_cell(it: dict) -> Text:
    """SPEC-0211: an idea campaign's spec row — `SPEC-NNNN <title>`, or `writing… <heading>`
    before a spec exists; the whole cell is cut to 60 characters."""
    if it.get("spec_id"):
        text, head = f"{it['spec_id']} {it.get('spec_title') or ''}".rstrip(), len(str(it["spec_id"]))
    else:
        text, head = f"writing… {it.get('spec_title') or ''}".rstrip(), 0
    if len(text) > _SPEC_CELL_MAX:
        text = text[:_SPEC_CELL_MAX - 1] + "…"
    t = Text(text, style="dim")
    if head:
        t.stylize("bold", 0, head)
    return t


def _spec_review_text(it: dict):
    """SPEC-0211: the `spec review` row of an idea campaign from its state, `spec_round` and
    `last_spec_review`, or None when there is nothing to show."""
    rnd = it.get("spec_round")
    last = it.get("last_spec_review")
    verdict = (last or {}).get("verdict")
    if it["state"] == "SPEC_WRITING":
        text = "writing" if rnd is None else f"r{rnd} · revising · last: {verdict}"
    elif it["state"] == "SPEC_REVIEW":
        text = f"r{rnd} · reviewing" + (f" · last: {verdict}" if last else "")
    elif rnd is not None:
        text = f"r{rnd} · last: {verdict}"
    else:
        return None
    return Text(text, style=R.state_style("SPEC_REVIEW"))


def _campaign_cell(it: dict) -> Text:
    """The campaign id; a merge request (SPEC-0233) also shows its `merge:<branch>` label."""
    t = Text(it["campaign"])
    if it.get("kind") == "merge" and it.get("label"):
        t.append("\n" + it["label"], style="dim")
    if it.get("kind") == "split":                                      # SPEC-0290
        t.append("\nsplit", style="dim")
    return t


def _title_cell(title) -> Text:
    return Text(title or "—", style="" if title else "dim")


def _ago(s) -> str:
    """Coarse age for tables: 45s, 11m, 3h, 2d."""
    if s is None:
        return "—"
    s = int(s)
    return (f"{s}s" if s < 60 else f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400
            else f"{s // 86400}d")


def _owner_chip(label: str, n, style: str) -> Text:
    """SPEC-0222: label first (`needs you 2`), so it reads as a sentence."""
    if not n:
        return Text(f"{label} 0", style="dim")
    return Text.assemble((label, style), " ", (str(n), f"bold {style}"))


def _conductor_chip(tot: dict) -> Text:
    n, overdue = tot.get("conductor_handling", 0), tot.get("conductor_overdue", 0)
    chip = Text(f"conductor {n}", style="dim")
    if overdue:
        chip.append(f" ({overdue} overdue)", style="red")
    return chip


def _chip(label: str, n, style: str) -> Text:
    return Text.assemble((f" {n} ", f"bold reverse {style}" if n else "dim reverse"),
                         (f" {label}", style if n else "dim"))


IDLE_REFRESH_S = 15.0       # SPEC-0372: re-render an unchanged dashboard this often (ages)


def _watch_fingerprint(cfg) -> tuple:
    """SPEC-0372: (name, mtime_ns, size) of the files the dashboard derives from: campaign
    state and ledger, the events journal and runner files, the gate-slot dirs, the worker
    registry. A few hundred stats, no parsing."""
    root = store.agentic_dir(cfg)
    out = []

    def add(path):
        try:
            st = os.stat(path)
            out.append((str(path), st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    try:
        top = list(os.scandir(root))
    except OSError:
        top = []
    for e in top:
        if e.is_file():
            add(e.path)
    for sub in ("gate-slots", "mailbox", "escalations"):
        for dirpath, _dirs, files in os.walk(root / sub):
            add(dirpath)
            for f in files:
                add(os.path.join(dirpath, f))
    try:
        camps = list(os.scandir(root / "campaigns"))
    except OSError:
        camps = []
    for c in camps:
        add(os.path.join(c.path, "state.json"))
        add(os.path.join(c.path, "ledger.jsonl"))
    try:
        from . import workers as WK
        add(WK.path(cfg))
    except Exception:
        pass
    return tuple(sorted(out))


def pool_marker(where) -> str:
    """SPEC-0372: 🏠 local gate slot, 🖥️ + worker name remote, ⏳ waiting for a slot."""
    return "🏠" if where == "local" else "⏳" if where == "waiting" else f"🖥️ {where}"


def _workers_line(p: dict) -> Text | None:
    """SPEC-0372: one slim line (only when a worker is registered): each worker's state and
    slots held, the local pool, and where each gating campaign is."""
    gp = p.get("gate_pools")
    if not gp:
        return None
    t = Text(" workers ", style="dim")
    for w in gp["workers"]:
        if not w["enabled"]:
            dot, state, style = "○", "off", "grey50"
        elif w["cooldown"]:
            dot, state, style = "●", "cooldown", "yellow"
        else:
            dot, state, style = "●", (("updating" if w.get("updating") else "draining") if w["draining"]
                               else "on"), "green"
        t.append(f"🖥️ {w['name']} ")
        t.append(dot, style=style)
        t.append(f" {state} {w['held']}/{w['slots']}  ")
    loc = gp["local"]
    t.append(f"🏠 local {loc['held']}/{loc['slots']}")
    if gp["campaigns"]:
        t.append("  │ ", style="dim")
        t.append("  ".join(f"{pool_marker(c['where'])} {c['campaign']}"
                           + (f" {GPG.label(c['progress'])}" if c.get("progress") else "")
                           for c in gp["campaigns"]))
    t.no_wrap, t.overflow = True, "ellipsis"
    return t


def _gate_jobs_line(p: dict) -> Text | None:
    """SPEC-0384: standalone `nw gate run` jobs (SPEC-0381) on their own line under the workers
    line, so campaign markers can never truncate them away. None when no job is running."""
    jobs = (p.get("gate_pools") or {}).get("gate_jobs")
    if not jobs:
        return None
    t = Text(" gate jobs ", style="dim")
    t.append("  ".join(
        f"{pool_marker(j['where'])} ⚙ {j['job']} by {j.get('by') or '?'}"
        + (f" ({j['project']}@{j['branch']})" if j.get("project") and j.get("branch")
           else f" ({j.get('project') or j.get('branch')})" if j.get("project") or j.get("branch")
           else "")
        for j in jobs))
    t.no_wrap, t.overflow = True, "ellipsis"
    return t


def _runner_code_line(rc) -> Text | None:
    """SPEC-0375: one line when the runner runs older nightwright code than the checkout."""
    if not rc or not rc.get("stale"):
        return None
    n = rc.get("changed")
    what = f"{n} code file{'s' if n != 1 else ''}" if n is not None else "diff unknown"
    line = Text.assemble(("⚠ ", "yellow"), ("runner code stale: ", "bold yellow"),
                         f"{str(rc.get('runner_sha'))[:7]} → {str(rc.get('head_sha'))[:7]}",
                         (f" ({what})", "dim"))
    if rc.get("pending_reexec"):
        line.append("  re-exec pending (draining)", style="dim")
    else:
        line.append("  · nw queue restart", style="dim")
    line.no_wrap, line.overflow = True, "ellipsis"
    return line


def _dash_header(p: dict, width=None) -> Panel:
    lease, slots, tot = p["lease"], p["slots"], p["totals"]
    if lease.get("held"):
        runner = Text.assemble(("● ", "red" if lease.get("stale") else "green"),
                               f"runner pid {lease.get('pid')}",
                               (f"  heartbeat {_dur(lease.get('heartbeat_age_s'))} ago", "dim"))
        if lease.get("stale"):
            runner.append("  STALE", style="bold red")
    else:
        runner = Text.assemble(("○ ", "red"), ("no loop running", "bold red"),
                               ("  (nw queue run)", "dim"))
    busy, total = slots.get("busy", 0), slots.get("total", 0) or 1
    slot_txt = Text.assemble(("campaigns ", "dim"), _bar(busy / total, width=max(total * 3, 6),
                                                        style="cyan"), f" {busy}/{slots.get('total')}")
    over = slots.get("over", max(0, busy - total))
    if over:                        # SPEC-0326: the limit was lowered under running campaigns
        slot_txt.append(f" over by {over}, no admissions until ≤{slots.get('total')}",
                        style="yellow")
    gs = p.get("gate_slots")
    if gs:
        slot_txt.append("  gates ", style="dim")
        slot_txt.append(f"{len(gs.get('held') or [])}/{gs.get('total')}")
        if gs.get("waiting"):
            slot_txt.append(f" +{len(gs['waiting'])} waiting", style="yellow")
    chips = Text(" ").join([
        _chip("active", tot["active"], "cyan"), _chip("waiting", tot.get("waiting", 0), "bright_blue"),
        _owner_chip("needs you", tot.get("needs_you", 0), "yellow"), _conductor_chip(tot),
        _chip("ready", tot["ready"], "green"), _chip("blocked", tot["blocked"], "magenta"),
        _chip("delivered", tot["delivered"], "green")])
    if tot.get("reviews_running"):                         # SPEC-0277
        chips.append(f" · reviews {tot['reviews_running']}", style="cyan")
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)
    grid.add_column(justify="right", no_wrap=True)
    slot_txt.append_text(Text.assemble(("  today ", "dim"),
                                       (f"${tot['spend_today_usd']:.2f}", "bold")))
    chips.no_wrap, chips.overflow = True, "ellipsis"
    if width is not None and len(runner) + len(slot_txt) + 2 > width - 4:
        slot_txt.no_wrap, slot_txt.overflow = True, "ellipsis"
        rows = [runner, slot_txt, chips]          # narrow: one line each, never folded
    else:
        grid.add_row(runner, slot_txt)
        rows = [grid, chips]
    rc = _runner_code_line(p.get("runner_code"))
    if rc is not None:
        rows.append(rc)
    wl = _workers_line(p)
    if wl is not None:
        rows.append(wl)
    gj = _gate_jobs_line(p)
    if gj is not None:
        rows.append(gj)
    by_project = tot.get("by_project") or {}
    if len(by_project) > 1 or p.get("project"):
        line = Text(" ")
        for i, (name, c) in enumerate(sorted(by_project.items())):
            if i:
                line.append("   ")
            line.append_text(R.project_text(name))
            bits = [f"{c[k]} {k.replace('_', ' ')}" for k in ("active", "waiting", "needs_you", "conductor", "backlog",
                                         "delivered")
                    if c.get(k)]
            line.append(" ▸ " + (" · ".join(bits) or "idle"), style="dim")
        line.no_wrap, line.overflow = True, "ellipsis"
        rows.append(line)
    body = Group(*rows)
    when = (p.get("generated_at") or "")[11:19]
    scope = f" · {p['project']}" if p.get("project") else ""
    return Panel(body, title=Text(f"nw queue{scope}", style="bold"),
                 subtitle=Text(when, style="dim"), title_align="left",
                 subtitle_align="right", border_style="grey50", box=box.ROUNDED)


def _stepper(state: str, phase_index, phases=None) -> Text:
    t = Text()
    for i, name in enumerate(phases or Q.PHASES):
        if i:
            t.append(" › ", style="grey50")
        label = name.lower()
        if phase_index is None:
            t.append(label, style="dim")
        elif i < phase_index:
            t.append("✓ " + label, style="green")
        elif i == phase_index:
            t.append("● " + label, style=f"bold {R.state_style(state) or 'cyan'}")
        else:
            t.append(label, style="grey50")
    return t


def _activity_text(a: dict, state: str) -> Text:
    kind = (a or {}).get("kind")
    if kind == "run":
        t = Text.assemble(("▶ ", "bold cyan"), (str(a.get("persona")), "bold"))
        model = "/".join(x for x in (a.get("model"), a.get("effort")) if x)
        if model:
            t.append(f" · {model}", style="cyan")
        t.append(f" · run {a.get('run_id')}", style="dim")
        el, to = a.get("elapsed_s"), a.get("timeout_s")
        t.append(f" · {_dur(el)}")
        if to:
            t.append(f" / {_dur(to)}", style="dim")
            t.append(" ")
            t.append_text(_bar((el or 0) / float(to), width=8, style="cyan"))
        if a.get("max_budget_usd") is not None:
            t.append(f" · budget ${float(a['max_budget_usd']):.2f}", style="dim")
        return t
    if kind == "gates":
        return Text.assemble(("⚙ ", "bold blue"), ("running gates" if state == "GATING"
                                                    else "closing (re-gate, rebase, merge)"))
    return Text("· idle (waiting for the next tick)", style="dim")


def _gate_text(g) -> Text:
    if not g:
        return Text("no gate run yet", style="dim")
    t = Text(f"#{g.get('gate_seq')} ", style="dim")
    for i, r in enumerate(g.get("results") or []):
        if i:
            t.append("  ")
        ok = r.get("ok")
        t.append("✓ " if ok else "✗ ", style="green" if ok else "red")
        t.append(str(r.get("name")), style="" if ok else "bold red")
        t.append(f" {_dur(r.get('duration_s'))}", style="dim")
        n = (r.get("summary") or {}).get("failed")
        if not ok and n:                    # SPEC-0284
            t.append(f" ({n} failed)", style="red")
        retried = sum(1 for a in r.get("attempts") or [] if a.get("attempt"))
        if retried:                         # SPEC-0289
            t.append(f" ↻{retried}", style="dim")
            if ok and r.get("flaky"):
                t.append(" (flaky)", style="yellow")
    return t


def _verdict_text(v) -> Text:
    if not v:
        return Text("no verdict yet", style="dim")
    status = str(v.get("status") or "?")
    style = {"done": "green", "blocked": "red", "needs_info": "yellow"}.get(status, "magenta")
    t = Text.assemble((status, f"bold {style}"), (f" ({v.get('persona')})", "dim"))
    if v.get("summary"):
        t.append("  ")
        t.append(v["summary"])
    return t


def _reviews_text(chips) -> Text:
    """SPEC-0277: one chip per reviewer persona — short name, then `▶ 3m`, `✓ 92`,
    `✗ 2mf/1mj` (must_fix/major counts), `! failed` or `·`."""
    t = Text()
    for i, c in enumerate(chips or []):
        if i:
            t.append("  ")
        t.append(Q.review_short_name(c["persona"]) + " ", style="dim")
        status, counts = c["status"], c.get("counts") or {}
        if status == "running":
            t.append(f"▶ {_dur(c.get('elapsed_s'))}", style="cyan")
        elif status == "pass":
            t.append("✓" + (f" {c['score']}" if c.get("score") is not None else ""), style="green")
        elif status == "fix":
            t.append(f"✗ {counts.get('must_fix', 0)}mf/{counts.get('major', 0)}mj", style="red")
        elif status == "failed":
            t.append("! failed", style="red")
        else:
            t.append("·", style="dim")
    return t


_TRACK_GLYPH = {True: ("✓", "green"), False: ("✗", "red"), None: ("", "")}


def _track_text(it: dict, width=None) -> Text:
    """SPEC-0326: `spec ✓ → impl ✓ → gates ✗ 2 failed → triage retry → gates ●`. With `width`,
    the oldest visits are dropped (counted in the `+N …` prefix) until the current one fits."""
    visits = list(it.get("track") or [])
    elided = int(it.get("track_elided") or 0)
    while True:
        t = _track_line(visits, elided, it.get("state"))
        if width is None or len(t) <= width or len(visits) <= 1:
            return t
        visits, elided = visits[1:], elided + 1


def _track_line(visits: list, elided: int, state) -> Text:
    t = Text()
    if elided:
        t.append(f"+{elided} … ", style="dim")
    for i, v in enumerate(visits):
        if i:
            t.append(" → ", style="grey50")
        label = JY.short(v.get("state"))
        last = i == len(visits) - 1
        if last and v.get("state") == state:
            t.append(f"{label} ●", style=f"bold {R.state_style(state) or 'cyan'}")
            continue
        glyph, style = _TRACK_GLYPH.get(v.get("ok"), ("", ""))
        out = v.get("outcome")
        t.append(label, style="" if v.get("ok") is not None else "dim")
        if glyph:
            t.append(f" {glyph}", style=style)
        if out and out != "ok":
            t.append(f" {out}", style=style or "dim")
    if not visits:
        t.append(JY.short(state) + " ●", style=f"bold {R.state_style(state) or 'cyan'}")
    return t


def _waiting_now(w: dict) -> Text:
    t = Text.assemble(("⏳ ", "bold yellow"), (JY.waiting_text(w), "yellow"))
    waited = w.get("waited_s")
    if waited is not None:
        t.append(f" · {_dur(waited)}", style="dim")
    return t


def _now_text(it: dict) -> Text:
    """The `now` line: the running persona, a wait (gate slot, close train), the gates, idle."""
    a, state = it.get("activity") or {}, it["state"]
    if it.get("waiting_for"):
        return _waiting_now(it["waiting_for"])
    if a.get("kind") == "gates" and it.get("gate_hold"):
        h = it["gate_hold"]
        what = "running gates" if state == "GATING" else "closing: re-gate, rebase, merge"
        where = f" · {pool_marker(h['pool'])}" if h.get("pool") else ""   # SPEC-0372
        return Text.assemble(("⚙ ", "bold blue"), what,
                             (f"{where} · slot {h.get('slot')} · {_dur(h.get('held_s'))}", "dim"),
                             (f" {GPG.label(h.get('progress'))}" if h.get("progress") else "",
                              "cyan"))
    return _activity_text(a, state)


def _card_title(it: dict, width=None) -> Text:
    """`campaign · spec title · STATE 12m · loop N/M · project` (SPEC-0326). With `width`, the
    spec title is cut so the state, loop and project still fit."""
    state = it["state"]
    head = Text(it["campaign"], style="bold")
    if it.get("kind") == "split":                                      # SPEC-0290
        head.append(" · kind split", style="cyan")
    tail = Text(" · ", style="grey50")
    tail.append(state, style=f"bold {R.state_style(state)}")
    if it.get("state_age_s") is not None:
        tail.append(f" {_ago(it['state_age_s'])}", style="dim")
    if it.get("park_requested"):
        tail.append(" · park requested", style="yellow")
    tail.append(f" · loop {it.get('loops')}", style="bold")
    tail.append(" · ", style="grey50")
    tail.append_text(R.project_text(it.get("project")))
    spec = (_idea_spec_cell(it) if it.get("idea") else
            _spec_cell(it.get("label") or it.get("spec_id") or it.get("idea_id"),
                       it.get("spec_title")))
    room = 40 if width is None else max(9, width - len(head) - len(tail) - 3)
    if len(spec) > room:
        spec.truncate(room - 1)
        spec.append("…", style="dim")
    return Text.assemble(head, (" · ", "grey50"), spec, tail)


def _cost_text(it: dict) -> Text:
    """`$2.85/$15 ━━━━━━ ~$4.00 est. · m/high-end → balanced (flag) · 8 runs · 2h`."""
    cap = it.get("cap_usd")
    cost = Text(f"${it['cost_usd']:.2f}", style="bold")
    if cap:
        cost.append(f"/{JY._money(cap)} ", style="dim")
        cost.append_text(_bar(it["cost_usd"] / float(cap), width=6))
    est = (it.get("cost") or {}).get("estimated_usd") or 0.0
    if est > 0:
        cost.append(f" ~${est:.2f} est.", style="dim")
    cost.append(f" · {_size_tone(it)} → {it.get('profile')}", style="dim")
    cost.append_text(_source_marker(it))
    runs = it.get("runs")
    cost.append(f" · {runs} run{'' if runs == 1 else 's'} · {_ago(it.get('age_s'))}", style="dim")
    return cost


def _active_panel(it: dict, *, last_n: int = 2, width=None) -> Panel:
    """SPEC-0326 full card: track, why, now, cost, review chips (when present), the last events
    as sentences; the title carries campaign · spec · STATE time · loop N/M · project.
    `width` is the card's width (for cutting the title and the track to fit)."""
    review = _spec_review_text(it) if it.get("idea") and it["state"] in Q._SPEC_PHASES else None
    labels = ["track", "why", "now", "cost", "last"] + (["spec review"] if review else []) + (
        ["reviews"] if it.get("reviews") else [])
    label_w = max(len(x) for x in labels)
    inner = None if width is None else width - 4 - label_w - 1
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="dim", no_wrap=True, justify="right", min_width=label_w)
    grid.add_column(overflow="ellipsis", no_wrap=True)
    grid.add_row("track", _track_text(it, inner))
    grid.add_row("why", Text(it.get("why") or "", style="italic"))
    if review:
        grid.add_row("spec review", review)
    grid.add_row("now", _now_text(it))
    grid.add_row("cost", _cost_text(it))
    if it.get("reviews"):
        grid.add_row("reviews", _reviews_text(it["reviews"]))
    lines = [ln["text"] for ln in (it.get("recent_lines") or [])][-last_n:]
    if lines:
        grid.add_row("last", Group(*[Text(x, style="dim", no_wrap=True, overflow="ellipsis")
                                     for x in lines]))
    return Panel(grid, title=_card_title(it, None if width is None else width - 6),
                 title_align="left", border_style=R.state_style(it["state"]) or "cyan",
                 box=box.ROUNDED)


def _card_compact(it: dict, width=None) -> Group:
    """SPEC-0326 compact card (3 lines): title, track, why (a gate-slot wait shows as ⏳)."""
    why = Text("  ")
    w = it.get("waiting_for")
    if w and w.get("kind") == "gate_slot":
        why.append("⏳ " + JY.waiting_text(w) + " · ", style="yellow")
    why.append(it.get("why") or "", style="italic")
    lines = [Text.assemble(("▌", R.state_style(it["state"]) or "cyan"),
                           _card_title(it, None if width is None else width - 1)),
             Text("  ") + _track_text(it, None if width is None else width - 2), why]
    for ln in lines:
        ln.no_wrap, ln.overflow = True, "ellipsis"
    return Group(*lines)


def _run_glyph(it: dict) -> Text:
    if it.get("waiting_for"):
        return Text("⏳", style="yellow")
    kind = (it.get("activity") or {}).get("kind")
    if kind == "run" or (kind == "gates" and it.get("gate_hold")):
        return Text("▶", style="cyan")
    return Text("·", style="dim")


def _cards_rows(items: list, limit=None) -> Table:
    """SPEC-0326 row style: `campaign  STATE ⏳/▶  loop N/M  $cost  why`, one line each."""
    t = Table(box=None, pad_edge=False, expand=True, show_header=False, padding=(0, 1))
    t.add_column(no_wrap=True, style="bold")
    t.add_column(no_wrap=True)
    t.add_column(no_wrap=True)
    t.add_column(no_wrap=True, style="dim")
    t.add_column(no_wrap=True, justify="right")
    t.add_column(no_wrap=True, overflow="ellipsis", ratio=1)
    shown = items if limit is None or len(items) <= limit else items[:max(0, limit - 1)]
    for it in shown:
        state = Text(it["state"], style=R.state_style(it["state"]))
        if it.get("state_age_s") is not None:
            state.append(f" {_ago(it['state_age_s'])}", style="dim")
        why = Text()
        w = it.get("waiting_for")
        if w and w.get("kind") == "gate_slot":
            why.append("gate slot · ", style="yellow")
        why.append(it.get("why") or "")
        t.add_row(it["campaign"], state, _run_glyph(it), f"loop {it.get('loops')}",
                  f"${it['cost_usd']:.2f}", why)
    if len(shown) < len(items):
        t.add_row(Text(f"+{len(items) - len(shown)} more", style="dim"), "", "", "", "", "")
    return t


# SPEC-0326: the bottom sections' fixed row budgets; each truncates with a `+N more` row
SECTION_ROWS = {"needs_you": 4, "conductor": 4, "waiting": 3, "spec_reviews": 3, "backlog": 4,
                "delivered": 5}


def allocate_rows(counts: dict, minimums: dict, spare: int, lanes=None) -> dict:
    """SPEC-0351: per-section row limits. Each section starts at `min(minimum, count)`; each of
    `spare` extra lines goes round-robin to a lane (sections sharing screen lines, e.g. side by
    side) that still has hidden items, growing every such section in it by one. Pure."""
    limits = {k: min(minimums.get(k, 0), n) for k, n in counts.items()}
    lanes = [[k for k in lane if k in counts] for lane in (lanes or [[k] for k in counts])]
    lanes = [ln for ln in lanes if ln]
    while spare > 0:
        grew = False
        for lane in lanes:
            if spare <= 0:
                break
            open_ = [k for k in lane if limits[k] < counts[k]]
            if open_:
                for k in open_:
                    limits[k] += 1
                spare -= 1
                grew = True
        if not grew:
            break
    return limits


def _more_row(t: Table, more: int) -> None:
    """Append `+N more` under the campaign (or spec) column of a truncated section."""
    if more <= 0:
        return
    cells = [""] * len(t.columns)
    names = [str(c.header) for c in t.columns]
    at = next((i for i, n in enumerate(names) if n in ("campaign", "spec")), 0)
    cells[at] = Text(f"+{more} more", style="dim")
    t.add_row(*cells)


def _cap(items: list, limit):
    if limit is None or len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _needs_you_table(items: list, limit=None) -> Table:
    t = R.section_table(f"Needs you ({len(items)})", border_style="yellow", show_edge=False)
    t.add_column("campaign", no_wrap=True, style="bold")
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
    t.add_column("reason", style="yellow", no_wrap=True)
    t.add_column("detail", no_wrap=True, overflow="ellipsis", ratio=3)
    t.add_column("resume to", no_wrap=True)
    t.add_column("for", justify="right", no_wrap=True)
    items, more = _cap(items, limit)
    for it in items:
        park = it.get("park") or {}
        reason = park.get("reason") or "?"
        t.add_row(_campaign_cell(it), R.project_text(it.get("project")),
                  _title_cell(it.get("spec_title")), reason,
                  Text(str(park.get("detail") or ""), style="dim"),
                  Text(str(park.get("resume_to") or ""), style=R.state_style(park.get("resume_to"))),
                  _ago(it.get("park_age_s")))
    _more_row(t, more)
    return t


def _decision_cell(it: dict) -> Text:
    """SPEC-0222: the conductor's latest note and its age, `overdue`, or `pending`."""
    park = it.get("park") or {}
    decision = park.get("decision")
    if decision:
        text = " ".join(str(decision.get("text") or "").split())
        if len(text) > 120:
            text = text[:119] + "…"
        t = Text(text, style="dim")
        ago = _ago(_note_age(decision.get("ts")))
        t.append(f" · {ago} ago", style="dim")
        return t
    age = _dur(it.get("park_age_s"))
    if park.get("overdue"):
        return Text(f"overdue {age}", style="bold red")
    return Text(f"pending {age}", style="dim")


def _note_age(ts):
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    return max(0, int((dt.datetime.now().astimezone() - t).total_seconds()))


def _conductor_table(items: list, limit=None) -> Table:
    t = R.section_table(f"Conductor handling ({len(items)})", border_style="grey50", show_edge=False)
    t.add_column("campaign", no_wrap=True, style="dim")
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8, style="dim")
    t.add_column("reason", no_wrap=True, style="dim")
    t.add_column("decision", no_wrap=True, overflow="ellipsis", ratio=3)
    t.add_column("for", justify="right", no_wrap=True, style="dim")
    items, more = _cap(items, limit)
    for it in items:
        park = it.get("park") or {}
        reason = park.get("reason") or "park requested"
        t.add_row(_campaign_cell(it), R.project_text(it.get("project")),
                  _title_cell(it.get("spec_title")), reason, _decision_cell(it),
                  _ago(it.get("park_age_s")))
    _more_row(t, more)
    return t


def _dash_waiting(items: list, limit=None) -> Table:
    t = R.section_table(f"Waiting for a slot ({len(items)})", border_style="bright_blue", show_edge=False)
    t.add_column("campaign", no_wrap=True)
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
    t.add_column("size", no_wrap=True)
    t.add_column("continues at", no_wrap=True)
    t.add_column("pri", justify="right")
    t.add_column("cost", justify="right", no_wrap=True)
    t.add_column("for", justify="right", no_wrap=True)
    items, more = _cap(items, limit)
    for it in items:
        t.add_row(_campaign_cell(it), R.project_text(it.get("project")), _title_cell(it.get("spec_title")),
                  _size_tone(it), Text(str(it.get("resume_to") or "?"), style=R.state_style(it.get("resume_to"))),
                  str(it.get("priority")), f"${it['cost_usd']:.2f}", _ago(it.get("waiting_s")))
    _more_row(t, more)
    return t


def _dash_spec_reviews(items: list, generated_at=None, limit=None) -> Table:
    """SPEC-0217: standalone `nw spec review` runs (SPEC-0210 journal)."""
    import datetime as dt
    try:
        now = dt.datetime.fromisoformat(generated_at) if generated_at else None
    except ValueError:
        now = None
    t = R.section_table(f"Spec reviews ({len(items)})", border_style="cyan", show_edge=False)
    t.add_column("spec", no_wrap=True)
    t.add_column("status", no_wrap=True)
    t.add_column("findings", no_wrap=True, overflow="ellipsis")
    t.add_column("cost", justify="right", no_wrap=True)
    t.add_column("when", justify="right", no_wrap=True)
    style = {"approve": "green", "revise": "yellow", "split": "yellow", "failed": "red",
             "running": "cyan"}
    items, more = _cap(items, limit)
    for r in items:
        status = r["status"]
        counts = r.get("counts")
        findings = ("—" if not isinstance(counts, dict) else
                    " · ".join(f"{n} {k}" for k, n in counts.items() if n) or "none")
        cost = r.get("cost_usd")
        cost = cost if isinstance(cost, (int, float)) else None
        if status == "running":
            when = f"▶ {_dur(r.get('duration_s'))}"
        else:
            when = _ago(Q._age(r.get("ended_at"), now))
        t.add_row(str(r.get("spec_id") or "—"), Text(status, style=style.get(status, "")), findings,
                  "—" if cost is None else f"${float(cost):.2f}", when)
    _more_row(t, more)
    return t


def _dash_backlog(items: list, limit=None):
    if not items:
        return Group(Text("Backlog (0)", style="bold"),
                     Text("  empty — nw queue add SPEC-NNNN --yes", style="dim"))
    t = R.section_table(f"Backlog ({len(items)})", show_edge=False)
    t.add_column("#", justify="right", style="dim")
    t.add_column("campaign", no_wrap=True)
    t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
    t.add_column("size", no_wrap=True)
    t.add_column("pri", justify="right")
    t.add_column("status", no_wrap=True, overflow="ellipsis", ratio=2, min_width=14)
    t.add_column("waiting", justify="right", no_wrap=True)
    items, more = _cap(items, limit)
    for b in items:
        status = (Text("● ready", style="green") if b.get("ready")
                  else Text.assemble(("◌ waiting on ", "yellow"),
                                     ("; ".join(b.get("blocked_by") or []), "bold yellow")))
        pri = b.get("priority")
        t.add_row(str(b.get("position")), _campaign_cell(b), R.project_text(b.get("project")),
                  _title_cell(b.get("spec_title")), _size_tone(b),
                  Text(str(pri), style="bold" if pri is not None and pri <= 2 else "dim"),
                  status, _ago(b.get("age_s")))
    _more_row(t, more)
    return t


def _dash_delivered(items: list, hours: int, width: int = 120, limit=None):
    wide = width >= 110
    narrow = width < 90                   # SPEC-0326: beside the backlog, no project column
    t = R.section_table(f"Recently delivered ({len(items)}, last {hours}h)", show_edge=False)
    t.add_column("ago", justify="right", no_wrap=True, style="dim", min_width=3)
    t.add_column("campaign", no_wrap=True)
    if not narrow:
        t.add_column("project", no_wrap=True, overflow="ellipsis", max_width=18)
    t.add_column("title", no_wrap=True, overflow="ellipsis", ratio=1, min_width=8)
    t.add_column("outcome", no_wrap=True)
    if wide:
        t.add_column("ref", no_wrap=True, overflow="ellipsis")
    t.add_column("cost", justify="right", no_wrap=True, min_width=5)
    if wide:
        t.add_column("took", justify="right", no_wrap=True)
    glyph = {"MERGED": "✓ merged", "HANDED_OFF": "↗ handed off", "ABANDONED": "✗ abandoned"}
    total = len(items)
    items, more = _cap(items, limit)
    for d in items:
        ref = d.get("pr_url") or ((d.get("merged_sha") or "")[:7])
        outcome = Text(glyph.get(d["state"], d["state"]), style=R.state_style(d["state"]))
        cells = [_ago(d.get("finished_age_s")), d["campaign"]] + (
            [] if narrow else [R.project_text(d.get("project"))]) + [
                 _title_cell(d.get("spec_title")),
                 outcome] + ([ref] if wide else []) + [f"${d['cost_usd']:.2f}"]
        t.add_row(*cells + ([_dur(d.get("duration_s"))] if wide else []))
    _more_row(t, more)
    if not total:
        return Group(t, Text("  nothing in this window", style="dim"))
    return t


SIDE_BY_SIDE = 150           # SPEC-0326: Backlog | Recently delivered from this width
DEFAULT_HEIGHT = 50          # SPEC-0326: the `--once` frame's height without a terminal
DENSITIES = ("full", "compact", "row")


def _lines(renderable, width: int) -> int:
    """How many terminal lines `renderable` takes at `width`."""
    import io
    from rich.console import Console
    c = Console(width=width, file=io.StringIO(), force_terminal=False, color_system=None)
    return len(c.render_lines(renderable, c.options.update(width=width), pad=False))


def card_columns(width: int, n: int) -> int:
    """1 column; 2 when width >= 140; 3 when width >= 210 (never more than the cards)."""
    return max(1, min(n, 3 if width >= 210 else 2 if width >= 140 else 1))


def _card_grid(items: list, width: int, density: str):
    cols = card_columns(width, len(items))
    cw = (width - (cols - 1)) // cols
    cards = [_active_panel(it, width=cw) if density == "full" else _card_compact(it, cw)
             for it in items]
    if cols == 1:
        return Group(*cards)
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in range(cols):
        grid.add_column(ratio=1)
    for i in range(0, len(cards), cols):
        row = cards[i:i + cols]
        grid.add_row(*(row + [Text("")] * (cols - len(row))))
    return grid


def active_region(items: list, width: int, budget=None) -> tuple:
    """`(density, renderable)`: the densest card style that fits every active campaign in
    `budget` lines — full, then compact, then one row each (truncated with `+N more` when even
    the rows do not fit). `budget=None` (no height limit) is always full."""
    if not items:
        return "full", Text("  No active campaigns", style="dim")
    for density in DENSITIES[:2]:
        r = _card_grid(items, width, density)
        if budget is None or _lines(r, width) <= budget:
            return density, r
    return "row", _cards_rows(items, None if budget is None else max(1, budget))


def _bottom_sections(p: dict, width: int, recent_hours: int, extra: int = 0) -> list:
    """SPEC-0326: the bottom sections, each with a minimum row budget (SECTION_ROWS) plus a
    share of `extra` spare lines (SPEC-0351); Backlog and Recently delivered side by side when
    width >= 150."""
    side = width >= SIDE_BY_SIDE
    keys = {"needs_you": "needs_you", "conductor": "conductor_handling",
            "spec_reviews": "spec_reviews", "waiting": "waiting",
            "backlog": "backlog", "delivered": "delivered"}
    counts = {k: len(p.get(f) or []) for k, f in keys.items() if p.get(f) or k in ("backlog", "delivered")}
    lanes = [[k] for k in counts if not (side and k in ("backlog", "delivered"))]
    if side:
        lanes.append(["backlog", "delivered"])
    lim = allocate_rows(counts, SECTION_ROWS, extra, lanes)
    parts = []
    if p.get("needs_you"):
        parts.append(_needs_you_table(p["needs_you"], lim["needs_you"]))
    if p.get("conductor_handling"):
        parts.append(_conductor_table(p["conductor_handling"], lim["conductor"]))
    if p.get("spec_reviews"):
        parts.append(_dash_spec_reviews(p["spec_reviews"], p.get("generated_at"),
                                        lim["spec_reviews"]))
    if p.get("waiting"):
        parts.append(_dash_waiting(p["waiting"], lim["waiting"]))
    if side:
        half = (width - 2) * 2 // 5             # the backlog has more columns: 3:2
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(ratio=3)
        grid.add_column(ratio=2)
        grid.add_row(_dash_backlog(p["backlog"], lim["backlog"]),
                     _dash_delivered(p["delivered"], recent_hours, half, lim["delivered"]))
        parts.append(grid)
    else:
        parts.append(_dash_backlog(p["backlog"], lim["backlog"]))
        parts.append(_dash_delivered(p["delivered"], recent_hours, width, lim["delivered"]))
    return parts


def _dashboard(p: dict, width: int = 120, recent_hours: int = 24, height=None,
               fill: bool = True) -> Group:
    """The `nw queue watch` frame for a `nw.queue.dashboard.v1` payload. With `height`, the
    header and the bottom sections keep their fixed budget and the active cards get the rest
    (SPEC-0326), so nothing is pushed off the screen."""
    head = _dash_header(p, width)
    bottom = _bottom_sections(p, width, recent_hours)
    budget = None
    if height is not None:
        budget = int(height) - _lines(head, width) - _lines(Group(*bottom), width) - 1
    items = p["active"]
    density, region = active_region(items, width, budget)
    if budget is not None and fill:
        # SPEC-0351: lines the active region leaves unused go to the truncated sections
        spare = budget - _lines(region, width)
        while spare > 0:
            alt = _bottom_sections(p, width, recent_hours, spare)
            over = _lines(Group(*alt), width) - _lines(Group(*bottom), width) - spare
            if over <= 0:
                bottom = alt
                break
            spare -= max(1, over)
    title = Text.assemble((f"Active ({len(items)})", "bold"))
    if items and density != "full":
        title.append(f"  {density} view", style="dim")
    return Group(head, title, region, *bottom)


def _journal_lines(rec: dict) -> Group:
    """One runner-journal record: a dim timestamp + pid, then what happened."""
    head = Text.assemble((str(rec.get("ts", ""))[:19], "dim"), " ",
                         (f"pid {rec.get('pid')}", "dim"), " ")
    kind = rec.get("kind")
    if kind == "start":
        head.append("▶ start", style="bold green")
        head.append(f"  every {rec.get('interval_s')}s", style="dim")
        return Group(head)
    if kind == "stop":
        head.append("■ stop", style="bold")
        head.append(f"  {rec.get('reason')} after {rec.get('ticks')} tick(s)", style="dim")
        return Group(head)
    if kind == "crash":
        head.append("✗ crash", style="bold red")
        head.append(f"  {rec.get('error')}", style="red")
        return Group(head)
    if kind == "config_reloaded":                          # SPEC-0226
        changed = list(rec.get("changed") or [])
        keys = [k for k in changed if k != "…"]
        more = len(keys) > 6 or len(keys) < len(changed)
        head.append(f"⟳ config reloaded ({rec.get('scope')}): ", style="bold cyan")
        head.append(", ".join(keys[:6]) + (", …" if more else ""))
        if rec.get("restart_required"):
            head.append(f"  restart required: {', '.join(rec['restart_required'])}",
                        style="yellow")
        return Group(head)
    if kind == "config_load_failed":
        head.append(f"✗ config load failed ({rec.get('scope')}): ", style="bold red")
        head.append(f"{rec.get('error')}", style="red")
        return Group(head)
    if kind == "reexec_deferred":                          # SPEC-0226
        head.append("⏸ re-exec deferred", style="bold yellow")
        head.append(f"  {rec.get('reason')} (code at {str(rec.get('to_sha'))[:8]})")
        return Group(head)
    if kind == "restart":                                  # SPEC-0375
        head.append(f"⟲ restart {rec.get('id')}", style="bold cyan")
        head.append(f"  runner {rec.get('old_pid') or '-'} → {rec.get('new_pid') or '-'}; "
                    f"parked {len(rec.get('parked') or [])}, "
                    f"resumed {len(rec.get('resumed') or [])}")
        return Group(head)
    if kind == "admission_blocked":                        # SPEC-0268
        held = ", ".join(f"{h.get('campaign')} {h.get('state')}"
                         + (" deferred" if h.get("deferred") else "")
                         for h in rec.get("holders") or [] if isinstance(h, dict))
        head.append(f"⏳ admission blocked: {len(rec.get('ready') or [])} ready, slots "
                    f"{rec.get('occupied')}/{rec.get('slots')} (held: {held})",
                    style="yellow")
        return Group(head)
    if rec.get("drain") and rec.get("tick") is not None:
        label = f"tick {rec['tick']} (drain)"
    else:
        label = "drain" if rec.get("drain") else f"tick {rec.get('tick')}"
    head.append(label, style="cyan")
    if rec.get("admitted"):
        head.append(f"  admitted {rec['admitted']}", style="green")
    rows = [head] + [Text("  ") + R.tick_row(row) for row in rec.get("steps") or []]
    rows += [Text("  ") + R.started_row(row) for row in rec.get("started") or []]
    rows += [Text(f"  ! {e}", style="red") for e in rec.get("errors") or []]
    return Group(*rows)


def _gc_table(rows: list) -> Table:
    """SPEC-0275: one row per worktree / orphan dir / branch / tmp residue (SPEC-0287) and
    what gc does with it."""
    table = Table(title=f"gc ({len(rows)})", box=box.SIMPLE_HEAD, pad_edge=False)
    table.add_column("kind", no_wrap=True)
    table.add_column("target", overflow="fold")
    table.add_column("campaign", no_wrap=True)
    table.add_column("action", no_wrap=True)
    table.add_column("why", overflow="fold")
    styles = {"remove": "green", "delete": "green", "keep": "dim"}
    for r in rows:
        why = r["why"] + (f" — FAILED: {r['error']}" if r.get("error") else "")
        table.add_row(r["kind"], r.get("path") or r.get("branch") or "",
                      f"{r['campaign']} ({r['campaign_state']})" if r["campaign"] else "",
                      Text(r["action"], style=styles.get(r["action"], "")),
                      Text(why, style="red" if r.get("error") else ""))
    return table


def _print_restart_plan(plan: dict) -> None:
    """SPEC-0375: the `nw queue restart` dry run."""
    r, cmd = plan.get("runner") or {}, plan.get("command") or {}
    if plan.get("id"):
        console.print(f"[bold]continue restart {plan['id']}[/] from phase {plan.get('phase')} "
                      f"(done: {', '.join(plan.get('done_phases') or []) or '-'})",
                      highlight=False)
    if r.get("held"):
        console.print(f"runner   pid {r.get('pid')} on {r.get('host')}"
                      + (" (detached)" if r.get("detached") else "")
                      + (f"; {r['guard']}: the stop waits for it" if r.get("guard") else ""),
                      highlight=False)
    else:
        console.print("runner   none holds the lease: start only", highlight=False)
    console.print(f"command  {' '.join(cmd.get('argv') or [])}", highlight=False)
    console.print(f"         cwd {cmd.get('cwd')} · command from {cmd.get('source')} · env from "
                  f"{cmd.get('env_source')}", style="dim", highlight=False)
    console.print(f"log      {plan.get('log')}", highlight=False)
    gating = [t["campaign"] for t in plan.get("targets") or [] if t["state"] == "GATING"]
    if plan.get("drain_min"):
        console.print(f"drain    up to {plan['drain_min']:g} min for "
                      f"{', '.join(gating) or 'no running gate'}", highlight=False)
    for t in plan.get("targets") or []:
        what = (f"park restart → resume {t['resume_to']}" if t["action"] == "park"
                else "not parked: the new runner adopts its persona run")
        console.print(f"  {t['campaign']}  {t['state']}  p{t['priority']}  {what}",
                      highlight=False)
    if not plan.get("targets"):
        console.print("  no campaign in flight", style="dim")
    if plan.get("untouched"):
        console.print(f"  not in --only (a running gate is still ended): "
                      f"{', '.join(plan['untouched'])}", style="dim", highlight=False)


def _print_plan(title: str, plan: dict) -> None:
    rev = plan.get("revision")
    if isinstance(rev, dict) and rev.get("pending"):                   # SPEC-0279
        plan = {**plan, "revision": (f"pending ({len(rev.get('findings') or [])} findings), "
                                     "spec_writer amends first")}
    elif isinstance(rev, dict):                                        # SPEC-0230
        plan = {**plan, "revision": (f"review {str(rev.get('review_sha'))[:12]} -> spec "
                                     f"{str(rev.get('current_sha'))[:12]}, "
                                     f"{len(rev.get('findings') or [])} findings")}
    console.print(R.plan(title, plan), highlight=False)


def _queue_add_one(cfg, spec_id, project, profile, base, force, priority, do_write, as_json,
                    by="human"):
    """The single-id `nw queue add` (nw.queue.add.v1)."""
    try:
        if Q._IDEA_ID_RE.match((spec_id or "").strip()):
            plan = Q.plan_add_idea(cfg, spec_id, project=project, profile=profile,
                                   base=base, priority=priority)
            if do_write:
                Q.apply_add_idea(cfg, plan, by=by)
        else:
            plan = Q.plan_add(cfg, spec_id, project=project, profile=profile, base=base,
                              force=force, priority=priority)
            if do_write:
                Q.apply_add(cfg, plan, by=by)
    except ValueError as e:
        raise click.ClickException(str(e))
    if as_json:
        _dump_json({"schema": "nw.queue.add.v1", **plan, "written": bool(do_write)})
        return
    _print_plan(f"queue {spec_id}", plan)
    if do_write:
        console.print(f"[green]✓[/] {plan['campaign']}  QUEUED  [dim]{plan['worktree']}[/]")
    else:
        console.print(_NOT_WRITTEN)


def _print_batch(batch: dict, do_write: bool, refused) -> None:
    """SPEC-0276: the batch table, the skipped epic items, the totals line and the hint."""
    table = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    for col in ("#", "id", "kind", "profile", "cap", ""):
        table.add_column(col)
    for it in batch["items"]:
        plan = it.get("plan") or {}
        cap = plan.get("cap_usd")
        if "error" in it:
            note = Text(it["error"], style="red")
        else:
            note = Text("; ".join(f"would block: {w}" for w in it.get("would_block") or ()),
                        style="yellow")
            if it.get("written"):
                note = Text(f"✓ {plan['campaign']}  ", style="green") + note
        table.add_row(str(it["position"]), Text(it["id"]), it["kind"],
                      str(plan.get("profile") or "-"), f"${cap:g}" if cap is not None else "-",
                      note)
    if batch["items"]:
        console.print(table)
    for sk in batch.get("skipped") or ():
        console.print(Text(f"skipped {sk['id']}: {sk['reason']}", style="dim"), highlight=False)
    totals = f"{batch['ok']} to queue · {batch['errors']} refused · ${batch['cap_usd_total']:g} cap total"
    if batch["uncapped"]:
        totals += f" + {batch['uncapped']} uncapped"
    console.print(totals, highlight=False)
    if refused:
        console.print(f"[red]✗ {refused}[/]", highlight=False)
    elif not do_write:
        console.print(_NOT_WRITTEN)


def attach(queue_group):
    """Register the queue subcommands on `queue_group` (SPEC-0173 / SPEC-0176)."""

    @queue_group.command("gates")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--json", "as_json", is_flag=True, help="emit nw.campaign.gate.v1 JSON")
    @click.option("--worker", "worker", default=None,
                  help="run on this registered worker only (its cooldown ignored; a "
                       "transport failure still falls back to a local run)")
    @click.option("--local", "local", is_flag=True, help="run locally, never on a worker")
    @click.pass_obj
    def queue_gates(cfg, campaign, as_json, worker, local):
        """Run the project's gates on a campaign's worktree now (debugging a parked item).

        Logs one `gate` event; never changes the campaign's state or `last_gate`, so a debug
        run cannot steer the queue. With workers registered (SPEC-0338) the run may go to one.
        """
        if worker and local:
            raise click.ClickException("--worker and --local are mutually exclusive")
        try:
            state = store.read_state(cfg, campaign)
            project_cfg = P.project_config(cfg, state.get("project") or "")
        except ValueError as e:
            raise click.ClickException(str(e))
        worktree = (state.get("repo") or {}).get("worktree")
        if not worktree or not os.path.isdir(worktree):
            raise click.ClickException(
                f"campaign {campaign} has no worktree on disk ({worktree or 'none recorded'})")
        project_cfg, _ = P.campaign_gate_config(cfg, project_cfg, worktree)     # SPEC-0316
        try:
            run = G.run_and_log(cfg, state, project_cfg, by="human", worker=worker,
                                local=local)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.campaign.gate.v1", "campaign": campaign,
                        **G.as_payload(run)})
            return
        where = f" · on {run.worker}" if run.worker else ""            # SPEC-0338
        table = Table(title=f"gates #{run.seq} · {campaign}{where} · "
                            f"{'ok' if run.ok else 'FAILED'}")
        for col in ("gate", "required", "result", "duration", "log"):
            table.add_column(col)
        for r in run.results:
            if r.timed_out:
                res = "[red]timeout[/]"
            elif r.ok:
                res = "[green]ok[/]"
            else:
                res = f"[red]exit {r.exit_code}[/]"
            table.add_row(r.name, "yes" if r.required else "no", res, f"{r.duration_s:.1f}s",
                          r.log)
        console.print(table)
        for r in G.blocking_failures(run):
            console.print(f"[red]✗[/] {r.name} — last lines:", highlight=False)
            R.print_code("\n".join(r.tail.splitlines()[-10:]) + "\n", "text", console=console)

    @queue_group.command("flaky")
    @click.option("--project", shell_complete=C.complete_project, help="only this project")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.flaky.v1 JSON")
    @click.pass_obj
    def queue_flaky(cfg, project, as_json):
        """List the cross-campaign flaky-test record (SPEC-0293), most-flaked first.

        Read-only: never writes and never prunes the record.
        """
        entries = FL.list_entries(cfg, project)
        if as_json:
            _dump_json({"schema": "nw.queue.flaky.v1", "path": str(FL.path(cfg)),
                        "entries": entries})
            return
        if not entries:
            console.print("no flaky tests on record")
            return
        table = Table(title="flaky tests")
        for col in ("project", "test", "count", "campaigns", "last seen", "sources"):
            table.add_column(col)
        for e in entries:
            src = ", ".join(f"{k} {v}" for k, v in sorted((e.get("sources") or {}).items()))
            table.add_row(Text(str(e["project"])), Text(str(e["id"])), Text(str(e.get("count"))),
                          Text(str(len(e.get("campaigns") or []))),
                          Text(str(e.get("last_seen") or "")[:10]), Text(src))
        console.print(table)

    # ---- SPEC-0176 -------------------------------------------------------------------------

    @queue_group.command("add")
    @click.argument("ids", nargs=-1, shell_complete=C.complete_any_spec_id)
    @click.option("--from-file", "from_file", type=click.Path(exists=True, dir_okay=False),
                  help="also queue the ids in this file (one per line; blanks and # comments "
                       "ignored)")
    @click.option("--epic", "epic", default=None, shell_complete=C.complete_any_spec_id,
                  help="also queue the epic's accepted child specs and open, spec-less ideas")
    @click.option("--skip-invalid", is_flag=True,
                  help="batch: with --yes, write the valid items instead of nothing")
    @click.option("--project", shell_complete=C.complete_project,
                  help="project (default: Meta-Tools for SPEC-NNNN, the outbox project otherwise; "
                       "the idea's :PROJECT: for IDEA-NNN)")
    @click.option("--profile", default=None, help="workflow profile (default: agentic.default_profile)")
    @click.option("--base", default=None, help="base branch (default: the project's merge.base)")
    @click.option("--force", is_flag=True, help="also admit an in-progress spec")
    @click.option("--priority", type=int, default=Q.PRIORITY_DEFAULT, show_default=True,
                  help="backlog priority 1..5 (1 = admitted first)")
    @click.option("--yes", "do_write", is_flag=True, help="create the campaign (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True,
                  help="emit nw.queue.add.v1 JSON (nw.queue.add.batch.v1 for a batch)")
    @click.option("--by", default=None,
                  help="who acts (default: $NW_MAILBOX_ADDR, then human)")
    @click.pass_obj
    def queue_add(cfg, ids, from_file, epic, skip_invalid, project, profile, base, force,
                  priority, do_write, as_json, by):
        """Queue accepted specs and ideas as new campaigns (dry-run unless --yes).

        \b
        nw queue add SPEC-0170            show the plan
        nw queue add SPEC-0170 --yes      create the campaign in QUEUED
        nw queue add IDEA-063 --yes       queue an idea: its campaign opens into SPEC_WRITING
        nw queue add SPEC-0171 SPEC-0170 --yes   a batch, created in depends_on order
        nw queue add --epic SPEC-0169 --yes      the epic's accepted children and open ideas
        nw queue add --from-file ids.txt --skip-invalid --yes
        """
        if not ids and not from_file and not epic:
            raise click.UsageError("give at least one SPEC-NNNN / IDEA-NNN id, --from-file or "
                                   "--epic")
        try:
            actor = _actor(cfg, by)
        except ValueError as e:
            raise click.ClickException(str(e))
        if len(ids) == 1 and not from_file and not epic:
            return _queue_add_one(cfg, ids[0], project, profile, base, force, priority,
                                  do_write, as_json, by=actor)
        try:
            batch_ids = list(ids)
            if from_file:
                with open(from_file, encoding="utf-8") as fh:
                    batch_ids += [ln.split("#", 1)[0].strip() for ln in fh]
            skipped = []
            explicit = {Q._canon_id(i) for i in batch_ids}      # never reported as skipped
            if epic:
                epic_ids, skipped = Q.expand_epic(cfg, epic)
                batch_ids += epic_ids
            batch = Q.plan_add_batch(cfg, batch_ids, project=project, profile=profile,
                                     base=base, force=force, priority=priority)
            batch["skipped"] = [s for s in skipped if s["id"] not in explicit]
            refused = None
            if do_write:
                try:
                    Q.apply_add_batch(cfg, batch, skip_invalid=skip_invalid, by=actor)
                except ValueError as e:
                    refused = str(e)
        except (ValueError, OSError) as e:
            raise click.ClickException(str(e))
        written = sum(1 for it in batch["items"] if it.get("written"))
        if as_json:
            _dump_json({**batch, "written": bool(written), "refused": refused})
        else:
            _print_batch(batch, do_write, refused)
        if do_write and (refused or batch["errors"]):
            raise click.exceptions.Exit(1)

    @queue_group.command("merge")
    @click.argument("branch")
    @click.option("--project", shell_complete=C.complete_project,
                  help="project (default: the one whose repo the cwd is in, else Meta-Tools)")
    @click.option("--worktree", default=None, type=click.Path(),
                  help="the branch's worktree (default: its existing worktree, else a new one)")
    @click.option("--profile", default=None, help="workflow profile (sizes the cap and slots)")
    @click.option("--priority", type=int, default=Q.MERGE_PRIORITY_DEFAULT, show_default=True,
                  help="backlog priority 1..5 (1 = admitted first)")
    @click.option("--note", default=None, help="why this merge (shown in the queue)")
    @click.option("--by", "requested_by", default="human", show_default=True,
                  help="who asks: human, conductor, steward:<Project>")
    @click.option("--docs-only", "docs_only", is_flag=True,
                  help="skip the gates; only if every changed path is in merge.docs_only_paths")
    @click.option("--yes", "do_write", is_flag=True, help="create the campaign (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.merge.v1 JSON")
    @click.pass_obj
    def queue_merge(cfg, branch, project, worktree, profile, priority, note, requested_by,
                    docs_only, do_write, as_json):
        """Queue a local branch for a rebase + re-gate + ff-only merge (dry-run unless --yes).

        \b
        nw queue merge fix/typo           show the plan
        nw queue merge fix/typo --yes     create the merge campaign in QUEUED
        nw queue merge fix/doc --docs-only    gate-free, for allowlisted docs paths only
        """
        try:
            plan = Q.plan_merge(cfg, branch, project=project, worktree=worktree, profile=profile,
                                priority=priority, note=note, requested_by=requested_by,
                                docs_only=docs_only)
            if do_write:
                Q.apply_merge(cfg, plan, by=requested_by)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.queue.merge.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"queue merge {branch}", plan)
        if do_write:
            console.print(f"[green]✓[/] {plan['campaign']}  QUEUED  [dim]{plan['worktree']}[/]")
        else:
            console.print(_NOT_WRITTEN)

    @queue_group.command("list")
    @click.option("--all", "include_terminal", is_flag=True, help="include terminal campaigns")
    @click.option("--project", shell_complete=C.complete_project, help="filter by project")
    @click.option("--state", shell_complete=C.complete_campaign_state, help="filter by state")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.v1 JSON")
    @click.pass_obj
    def queue_list(cfg, include_terminal, project, state, as_json):
        """What is in flight, in which state, at what cost — plus the lease and the slots."""
        payload = Q.queue_payload(cfg, include_terminal=include_terminal, project=project,
                                  state=state)
        if as_json:
            _dump_json(payload)
            return
        console.print(_queue_table(payload))

    @queue_group.command("backlog")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.backlog.v1 JSON")
    @click.pass_obj
    def queue_backlog(cfg, as_json):
        """The inactive backlog in admission order: priority, then age; what each item waits on."""
        items = Q.backlog_view(cfg)
        if as_json:
            _dump_json({"schema": "nw.queue.backlog.v1", "items": items})
            return
        table = _backlog_table([{"campaign": b["campaign"], "spec_id": b["spec_id"] or b.get("idea_id"),
                                 "kind": b.get("kind"), "label": b.get("label"),
                                 "project": b.get("project"), "title": b.get("title"),
                                 "age_s": b["age_s"], "backlog": b} for b in items])
        console.print(table)

    @queue_group.command("graph")
    @click.option("--project", shell_complete=C.complete_project, help="filter by project "
                  "(dependency stubs from elsewhere are kept, marked external)")
    @click.option("--all", "include_terminal", is_flag=True, help="include terminal campaigns")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.graph.v1 JSON")
    @click.pass_obj
    def queue_graph(cfg, project, include_terminal, as_json):
        """The queue as a dependency tree (SPEC-0356): dependents under their dependencies, a
        stub for every dependency without a campaign, cycles listed apart. Read-only."""
        g = Q.graph_view(cfg, project=project, include_terminal=include_terminal)
        if as_json:
            _dump_json(g)
            return
        console.print(_graph_render(g, console.width))

    @queue_group.command("unqueued")
    @click.option("--project", shell_complete=C.complete_project, help="filter by project")
    @click.option("--status", "statuses", multiple=True,
                  type=click.Choice(list(Q.UNQUEUED_STATUSES)),
                  help="spec statuses to list (repeatable; default accepted, proposed, draft)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.unqueued.v1 JSON")
    @click.pass_obj
    def queue_unqueued(cfg, project, statuses, as_json):
        """Specs and ideas with no campaign (SPEC-0356), with why each is not queued; `ready`
        rows (accepted, dependencies met, never queued) first. Prints the queue command, never
        runs it."""
        u = Q.unqueued_view(cfg, project=project, statuses=statuses or None)
        if as_json:
            _dump_json(u)
            return
        console.print(_unqueued_table(u["items"], console.width))

    @queue_group.command("prioritize")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.argument("priority", type=int)
    @click.option("--by", default=None,
                  help="who acts (default: $NW_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.prioritize.v1 JSON")
    @click.pass_obj
    def queue_prioritize(cfg, campaign, priority, by, do_write, as_json):
        """Set a QUEUED or PARKED campaign's backlog priority (1..5, 1 = admitted first)."""
        try:
            plan = Q.plan_prioritize(cfg, campaign, priority, by=_actor(cfg, by))
            if do_write:
                Q.apply_prioritize(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.queue.prioritize.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"prioritize {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} priority {plan['from']} → {plan['to']}"
                      if do_write else _NOT_WRITTEN)

    @queue_group.command("watch")
    @click.option("--interval", type=float, default=2.0, show_default=True,
                  help="refresh interval in seconds")
    @click.option("--project", shell_complete=C.complete_project, help="only this project")
    @click.option("--recent", type=int, default=5, show_default=True,
                  help="how many recently delivered campaigns to show")
    @click.option("--hours", "recent_hours", type=int, default=24, show_default=True,
                  help="the 'recently delivered' window")
    @click.option("--once", is_flag=True, help="print one frame and exit (no live refresh)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.dashboard.v1 JSON once")
    @click.option("--iterations", type=int, default=0, hidden=True,
                  help="stop after N refreshes (0 = until Ctrl-C)")
    @click.option("--height", type=int, default=None, hidden=True,
                  help="frame height in lines (default: the terminal's; --once off a "
                       "terminal: 50)")
    @click.pass_obj
    def queue_watch(cfg, interval, project, recent, recent_hours, once, as_json, iterations,
                    height):
        """Live dashboard: the runner, active campaigns (their journey track, why they are
        where they are, what each is doing or waiting for now, cost, review chips, the last
        events), what needs attention, the backlog and recently delivered work (Ctrl-C to
        stop). The bottom sections keep a fixed size; the cards shrink (full, compact, one row)
        to fit the terminal height (SPEC-0326).

        Review chips: `▶ 3m` running, `✓ 92` pass with score, `✗ 2mf/1mj` fix with
        must_fix/major counts, `! failed` no valid verdict, `·` not run."""
        def payload():
            return Q.dashboard_payload(cfg, project=project, recent=recent,
                                       recent_hours=recent_hours)

        if as_json:
            _dump_json(payload())
            return
        def frame():
            h = height or (console.height if console.is_terminal else DEFAULT_HEIGHT)
            return _dashboard(payload(), console.width, recent_hours, h,
                              fill=bool(height) or console.is_terminal)

        if once:
            console.print(frame())
            return
        from rich.live import Live
        n = 0
        # the alternate screen redraws in place (no scroll-back ghosts) and restores the
        # terminal on exit; `--once` is the frame to keep. SPEC-0372: no auto-refresh thread
        # re-rendering a static frame 4x/s; the frame is rebuilt when the data changed (a
        # cheap stat fingerprint) or every IDLE_REFRESH_S so ages stay fresh.
        with Live(frame(), console=console, auto_refresh=False,
                  screen=console.is_terminal) as live:
            fp, last = _watch_fingerprint(cfg), time.monotonic()
            while True:
                n += 1
                if iterations and n >= iterations:
                    break
                time.sleep(interval)
                now_fp = _watch_fingerprint(cfg)
                if now_fp == fp and time.monotonic() - last < IDLE_REFRESH_S:
                    continue
                fp, last = now_fp, time.monotonic()
                live.update(frame(), refresh=True)

    @queue_group.command("tick")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.tick.v1 JSON")
    @click.pass_obj
    def queue_tick(cfg, as_json):
        """One iteration of the loop under the lease (admit, open, run steps, close)."""
        try:
            lease = L.acquire(cfg, ["nw", "queue", "tick"])
        except ValueError as e:
            raise click.ClickException(str(e))
        try:
            report = Q.tick(cfg)
        finally:
            lease.release()
        if as_json:
            _dump_json(report.payload())
            return
        for row in report.steps:
            console.print(R.tick_row(row), highlight=False)
        if not report.steps:
            console.print("[dim]nothing to do[/]" if report.idle else "[dim]no step this tick[/]")

    @queue_group.command("run")
    @click.option("--once", is_flag=True, help="one tick, then exit")
    @click.option("--max-ticks", type=int, default=None, help="exit after N ticks")
    @click.option("--poll-interval", type=float, default=None,
                  help="seconds between ticks (default: agentic.poll_interval_s)")
    @click.option("--until-idle", is_flag=True, help="exit when nothing is in flight")
    @click.option("--json-lines", "json_lines", is_flag=True,
                  help="one nw.queue.tick.v1 object per tick on stdout")
    @click.option("--no-reexec", "no_reexec", is_flag=True,
                  help="keep running old code when a merge changes nightwright (default: re-exec)")
    @click.option("--detach", is_flag=True,
                  help="start the loop as a detached session leader; wait for its lease")
    @click.option("--log", "log_path", type=click.Path(dir_okay=False), default=None,
                  help="--detach output (appended; default: <agentic_dir>/runner.log)")
    @click.pass_obj
    def queue_run(cfg, once, max_ticks, poll_interval, until_idle, json_lines, no_reexec,
                  detach, log_path):
        """The loop: tick, heartbeat, sleep. One process at a time (lease-guarded).

        Agent steps carry across ticks (SPEC-0213): a tick starts steps and applies the ones
        that finished, so a slow step never holds up admission or other campaigns.
        SIGINT/SIGTERM stop admissions, end running gates, and return. Persona runs keep
        running and the next `nw queue run` adopts them (SPEC-0225/0229). If worker threads
        are still stuck after the 30 s bound, the traceback (if any) is printed to stderr and
        the process exits with status 70. --once / --max-ticks: persona runs still running at
        exit are left running; the next `nw queue run` collects them. When a commit changes
        nightwright's own code, the loop keeps admitting while it drains (no new gate or close), then
        re-executes itself, keeping the lease (SPEC-0194/0229).

        --detach (SPEC-0291) starts the same command in its own session, output appended to
        --log, and returns once the child holds the lease (exit 1 with the log tail if it does
        not within 15 s). Stop it with `nw queue stop`.
        """
        if detach:
            if once or max_ticks is not None:
                raise click.UsageError("--detach runs the long-lived loop; it cannot be "
                                       "combined with --once or --max-ticks")
            log = log_path or str(store.agentic_dir(cfg) / "runner.log")
            try:
                d = Q.detach_runner(cfg, Q.entry_argv(), log, timeout_s=15)
            except ValueError as e:
                raise click.ClickException(str(e))
            console.print(f"runner started: pid {d['pid']}, log {d['log']}", highlight=False)
            return

        def on_tick(report):
            if json_lines:
                sys.stdout.write(json.dumps(report.payload(), ensure_ascii=False) + "\n")
                sys.stdout.flush()
                return
            for row in report.steps:
                console.print(R.tick_row(row), highlight=False)
            for row in report.started:            # SPEC-0213: its result lands in a later tick
                console.print(R.started_row(row), highlight=False)

        try:
            ticks = Q.run(cfg, once=once, max_ticks=max_ticks, poll_interval_s=poll_interval,
                          until_idle=until_idle, on_tick=on_tick,
                          argv=["nw", "queue", "run"], reexec=not (once or no_reexec),
                          config_loader=load_config)
        except ValueError as e:
            if Q.leftover_threads():       # SPEC-0229: a stuck thread would hang the exit
                import traceback
                traceback.print_exc()
                _exit_leftover()
            raise click.ClickException(str(e))
        except BaseException:
            if Q.leftover_threads():       # SPEC-0229: no stale thread outlives the lease
                import traceback
                traceback.print_exc()
                _exit_leftover()
            raise
        if Q.leftover_threads():
            _exit_leftover()
        if not json_lines:
            console.print(f"[dim]{ticks} tick(s); lease released[/]")

    @queue_group.command("stop")
    @click.option("--timeout", "timeout_s", type=float, default=60, show_default=True,
                  help="seconds to wait for the lease to be released after SIGTERM")
    @click.option("--wait-guard", is_flag=True,
                  help="inside the close guard, wait for it to clear instead of refusing")
    @click.option("--by", default=None,
                  help="who acts (default: $NW_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.stop.v1 JSON")
    @click.pass_obj
    def queue_stop(cfg, timeout_s, wait_guard, by, do_write, as_json):
        """SIGTERM the runner holding the lease on this host and wait until it releases it
        (SPEC-0291). Refuses inside the close guard (an `open` or `close` step within its
        budget) unless --wait-guard; never SIGKILLs. Exit 0 with "no runner" when none runs."""
        try:
            actor = _actor(cfg, by)
        except ValueError as e:
            raise click.ClickException(str(e))
        st = L.lease_status(cfg)
        guard = Q.close_guard(st) if st["held"] else None
        refusal = (Q.stop_refusal(st, (L.read_lease(cfg) or {}).get("argv"),
                                  wait_guard=wait_guard) if st["held"] else None)
        plan = {"held": st["held"], "pid": st["pid"], "host": st["host"], "guard": guard,
                "refusal": refusal, "wait_guard": wait_guard, "timeout_s": timeout_s,
                "by": actor}
        result = error = None
        if do_write:
            try:
                result = Q.stop_runner(cfg, timeout_s=timeout_s, wait_guard=wait_guard)
            except ValueError as e:
                if not as_json:
                    raise click.ClickException(str(e))
                error = str(e)
        if result and result["stopped"] and result["pid"] != plan["pid"]:
            # a runner that took the lease after the plan read: report the one stopped
            plan.update(held=True, pid=result["pid"], host=socket.gethostname(), guard=None,
                        refusal=None)
        if as_json:
            _dump_json({"schema": "nw.queue.stop.v1", **plan, "written": bool(do_write),
                        "result": result, "error": error})
            if error is not None:
                raise click.exceptions.Exit(1)
            return
        if result is None and not st["held"]:
            console.print("[dim]no runner[/]")
        elif result is None:
            shown = {**plan, "guard": guard and Q._guard_text(guard),
                     "action": (f"refuse: {refusal}" if refusal
                                else "wait for the guard, then SIGTERM" if guard
                                else "SIGTERM")}
            _print_plan("stop runner", shown)
            console.print(_NOT_WRITTEN)
        elif result["stopped"]:
            console.print(f"[green]✓[/] runner pid {result['pid']} stopped "
                          f"({result['waited_s']}s)", highlight=False)
        else:
            console.print(f"[dim]{result['reason']}[/]")

    @queue_group.command("restart")
    @click.option("--drain", "drain_min", type=float, default=0, show_default=True,
                  help="minutes to let running gate steps finish before the stop")
    @click.option("--only", "only", multiple=True, shell_complete=C.complete_campaign_id,
                  help="park/resume only this in-flight campaign (repeatable)")
    @click.option("--timeout", "timeout_s", type=float, default=60, show_default=True,
                  help="seconds to wait for the lease to be released after SIGTERM")
    @click.option("--continue", "cont", is_flag=True,
                  help="finish an interrupted restart from its plan file")
    @click.option("--by", default=None,
                  help="who acts (default: $NW_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.restart.v1 JSON")
    @click.pass_obj
    def queue_restart(cfg, drain_min, only, timeout_s, cont, by, do_write, as_json):
        """Restart the runner gracefully (SPEC-0375): optionally let running gates finish
        (--drain), SIGTERM the runner as `nw queue stop --wait-guard` does, park the campaigns
        in OPENING/GATING/CLOSING with reason `restart`, resume them in priority order, then
        start a detached runner with the old one's command line, cwd and environment. Persona
        runs are not parked: the new runner adopts them. Remote gate jobs are not cancelled.
        Crash-safe: the plan is kept in <agentic_dir>/restart.json and `--continue --yes`
        finishes it. Dry run without --yes."""
        try:
            actor = _actor(cfg, by)
            if cont:
                if drain_min or only:
                    raise ValueError("--continue finishes the recorded plan: drop --drain / --only")
                plan = RS.continue_plan(cfg)
            else:
                plan = RS.plan_restart(cfg, drain_min=drain_min, only=only)
        except ValueError as e:
            raise click.ClickException(str(e))
        result = error = None
        if do_write:
            try:
                result = RS.apply_restart(cfg, plan, by=actor, timeout_s=timeout_s)
            except ValueError as e:
                if not as_json:
                    raise click.ClickException(str(e))
                error = str(e)
        if as_json:
            shown = result if result is not None else (
                (RS.read_plan(cfg) or plan) if do_write else plan)
            _dump_json({**RS.public(shown), "written": bool(do_write), "error": error})
            if error is not None:
                raise click.exceptions.Exit(1)
            return
        if result is None:
            _print_restart_plan(RS.public(plan))
            console.print(_NOT_WRITTEN)
            return
        stop, start = result.get("stop") or {}, result.get("start") or {}
        old = f"runner pid {stop['pid']} stopped" if stop.get("stopped") else "no runner to stop"
        new = (f"runner pid {start.get('pid')} started, log {start.get('log')}"
               if start.get("started")
               else f"runner pid {start.get('pid')} already holds the lease")
        console.print(f"[green]✓[/] restart {result['id']}: {old}; parked "
                      f"{len(result.get('parked') or [])}, resumed "
                      f"{len(result.get('resumed') or [])}; {new}", highlight=False)

    @queue_group.command("runner-log")
    @click.option("--tail", type=int, default=30, show_default=True,
                  help="last N records (0 = all)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.runner.log.v1 JSON")
    @click.pass_obj
    def queue_runner_log(cfg, tail, as_json):
        """The runner's own journal (`runner.jsonl`, SPEC-0192): starts, stops, crashes and
        every tick that did something — what `nw queue run` did, without reading free text."""
        recs = Q.read_journal(cfg, tail=tail or None)
        if as_json:
            _dump_json({"schema": "nw.runner.log.v1", "path": str(Q.runner_journal_path(cfg)),
                        "records": recs})
            return
        if not recs:
            console.print(f"[dim]no runner records yet ({Q.runner_journal_path(cfg)})[/]")
            return
        for rec in recs:
            console.print(_journal_lines(rec), highlight=False)

    @queue_group.command("park")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--reason", required=True, help="why (recorded on the campaign)")
    @click.option("--decision", default=None,
                  help="the decision's rationale (required for conductor / steward)")
    @click.option("--rule", default=None, help="the /nw-conduct decision-table row id")
    @click.option("--kind", default=None, help="decision kind (default: wait)")
    @click.option("--by", default=None,
                  help="who acts (default: $NW_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.park.v1 JSON")
    @click.pass_obj
    def queue_park(cfg, campaign, reason, decision, rule, kind, by, do_write, as_json):
        """Park a campaign. With a loop running, the current step finishes first. As conductor
        or steward it needs --decision and --rule: the decision record is written right after
        the park (SPEC-0318)."""
        try:
            actor = _actor(cfg, by)
            request = _decision_request(actor, decision, rule, kind, default_kind="wait",
                                        required=True)
            plan = Q.plan_park(cfg, campaign, reason, by=actor, decision=request)
            if do_write:
                Q.apply_park(cfg, plan)
                if plan["mode"] == "immediate":
                    Q.settle_decision_requests(cfg, plan["campaign"])
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.queue.park.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"park {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} park {plan['mode']}" if do_write else _NOT_WRITTEN)

    @queue_group.command("escalate")
    @click.argument("campaign", required=False, shell_complete=C.complete_campaign_id)
    @click.option("--question", required=True, help="the human-only decision needed")
    @click.option("--option", "options", multiple=True, help="a numbered choice (repeatable)")
    @click.option("--recommend", type=int, default=None, help="the recommended option, 1-based")
    @click.option("--fact", "facts", multiple=True, help="a fact the decision rests on")
    @click.option("--from", "frm", default=None,
                  help="who escalates (default: $NW_MAILBOX_ADDR, then conductor)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.escalate.v1 JSON")
    @click.pass_obj
    def queue_escalate(cfg, campaign, question, options, recommend, facts, frm, do_write,
                       as_json):
        """Escalate a decision to the human (SPEC-0232): an ESC record, the campaign parked as
        `human_decision`, mail to `human` and a desktop notification. Without CAMPAIGN the
        escalation is about the runner or the queue as a whole and parks nothing."""
        from . import escalations as ESC
        try:
            plan = ESC.plan_escalate(cfg, campaign, question=question, options=options,
                                     recommend=recommend, facts=facts, frm=frm)
            rec = ESC.apply_escalate(cfg, plan) if do_write else None
        except (ValueError, OSError) as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.queue.escalate.v1", **plan, "written": bool(do_write),
                        "escalation": rec})
            return
        shown = {**plan, "options": "; ".join(f"{o['n']}. {o['label']}" for o in plan["options"]),
                 "park": plan["park"] and f"{plan['park']['mode']} → human_decision"}
        _print_plan("escalate" + (f" {campaign}" if campaign else ""), shown)
        console.print(f"[green]✓[/] {rec['id']} opened" if do_write else _NOT_WRITTEN)

    @queue_group.command("resume")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--to", type=click.Choice(Q.RESUMABLE_STATES), default=None,
                  help="state to resume to (default: where it parked)")
    @click.option("--note", default=None, help="a note for the next implementer run")
    @click.option("--raise-cap", type=float, default=None, help="new campaign cost cap in USD")
    @click.option("--reset-loops", is_flag=True, help="reset the loop counters")
    @click.option("--decision", default=None,
                  help="the decision's rationale (required for conductor / steward)")
    @click.option("--rule", default=None, help="the /nw-conduct decision-table row id")
    @click.option("--kind", default=None, help="decision kind (default: resume)")
    @click.option("--by", default=None,
                  help="who acts (default: $NW_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.resume.v1 JSON")
    @click.pass_obj
    def queue_resume(cfg, campaign, to, note, raise_cap, reset_loops, decision, rule, kind, by,
                     do_write, as_json):
        """Resume a PARKED campaign (optionally with a note, a higher cap, fresh loops). As
        conductor or steward a decision record at or after the park must exist, or be given
        with --decision and --rule (SPEC-0318)."""
        try:
            actor = _actor(cfg, by)
            request = _decision_request(actor, decision, rule, kind, default_kind="resume",
                                        required=False)
            plan = Q.plan_resume(cfg, campaign, to=to, note=note, raise_cap=raise_cap,
                                 reset_loops=reset_loops, by=actor, decision=request)
            if do_write:
                Q.apply_resume(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.queue.resume.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"resume {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} → {plan['to']}" if do_write else _NOT_WRITTEN)

    @queue_group.command("sweep-prs")
    @click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1),
                  help="check at most N HANDED_OFF campaigns")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit the rows as a JSON list")
    @click.pass_obj
    def queue_sweep_prs(cfg, limit, do_write, as_json):
        """Check each HANDED_OFF campaign's PR with `gh pr view`: a merged PR moves its campaign
        to MERGED, a PR closed unmerged records one finding (SPEC-0283)."""
        rows = Q.sweep_handed_off(cfg, apply=do_write, limit=limit)
        if as_json:
            _dump_json(rows)
            return
        table = Table(box=box.SIMPLE_HEAD)
        for col in ("campaign", "PR URL", "PR state", "action", "detail"):
            table.add_column(col, overflow="fold")
        for r in rows:
            table.add_row(r["campaign"], r["pr_url"], r["pr_state"] or "-", r["action"],
                          r["detail"])
        console.print(table if rows else "no HANDED_OFF campaigns with a PR to check")
        if rows and not do_write:
            console.print(_NOT_WRITTEN)

    @queue_group.command("abandon")
    @click.argument("campaign", shell_complete=C.complete_campaign_id)
    @click.option("--keep-worktree", is_flag=True, help="leave the worktree on disk")
    @click.option("--decision", default=None,
                  help="the decision's rationale (required for conductor / steward)")
    @click.option("--rule", default=None, help="the /nw-conduct decision-table row id")
    @click.option("--kind", default=None, help="decision kind (default: abandon)")
    @click.option("--by", default=None,
                  help="who acts (default: $NW_MAILBOX_ADDR, then human)")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.abandon.v1 JSON")
    @click.pass_obj
    def queue_abandon(cfg, campaign, keep_worktree, decision, rule, kind, by, do_write, as_json):
        """Abandon a PARKED or QUEUED campaign (removes its worktree; keeps the branch). As
        conductor or steward a PARKED campaign needs a decision record at or after its park, or
        --decision and --rule (SPEC-0318)."""
        try:
            actor = _actor(cfg, by)
            request = _decision_request(actor, decision, rule, kind, default_kind="abandon",
                                        required=False)
            plan = Q.plan_abandon(cfg, campaign, keep_worktree=keep_worktree, by=actor,
                                  decision=request)
            if do_write:
                Q.apply_abandon(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        if as_json:
            _dump_json({"schema": "nw.queue.abandon.v1", **plan, "written": bool(do_write)})
            return
        _print_plan(f"abandon {campaign}", plan)
        console.print(f"[green]✓[/] {campaign} ABANDONED" if do_write else _NOT_WRITTEN)

    @queue_group.command("gc")
    @click.option("--project", shell_complete=C.complete_project, help="filter by project")
    @click.option("--yes", "do_write", is_flag=True, help="apply (default is dry-run)")
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.gc.v1 JSON")
    @click.pass_context
    def queue_gc(ctx, project, do_write, as_json):
        """Garbage-collect worktrees and merged branches of finished campaigns (SPEC-0275).
        Never touches dirty worktrees, unmerged branches, live campaigns, the main checkout
        or the cwd's worktree. Exits 1 when a --yes row or repo failed."""
        cfg = ctx.obj
        try:
            plan = Q.plan_gc(cfg, project=project)
            if do_write:
                Q.apply_gc(cfg, plan)
        except ValueError as e:
            raise click.ClickException(str(e))
        failed = sum(r.get("done") is False for r in plan["rows"]) if do_write else 0
        if as_json:
            _dump_json({"schema": "nw.queue.gc.v1", **plan, "written": bool(do_write)})
            if failed or (do_write and plan["errors"]):
                ctx.exit(1)
            return
        console.print(_gc_table(plan["rows"]), highlight=False)
        for err in plan["errors"]:
            console.print(f"[red]![/] {err}", highlight=False)
        tmp = plan.get("tmp_removable") or 0
        summary = (f"{plan['worktrees_removable']} worktrees, {plan['branches_removable']} "
                   f"branches" + (f", {tmp} tmp dirs" if tmp else "") + " removable")
        if not do_write:
            console.print(f"{summary} (dry-run; --yes to apply)")
            return
        console.print(f"[green]✓[/] {summary}; applied" + (f", [red]{failed} failed[/]" if failed else ""))
        if failed or plan["errors"]:
            ctx.exit(1)

    @queue_group.group("sentinel")
    def queue_sentinel():
        """Full-suite sentinel on a project's base after close-tier reuses (SPEC-0359)."""

    def _sentinel_project(cfg, project):
        try:
            pcfg = P.project_config(cfg, project)
        except ValueError as e:
            raise click.ClickException(str(e))
        return pcfg

    @queue_sentinel.command("run")
    @click.option("--project", required=True, shell_complete=C.complete_project)
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.sentinel.v1 JSON")
    @click.pass_context
    def queue_sentinel_run(ctx, project, as_json):
        """Run the project's close gates on its base HEAD now (waits for a gate slot like any
        gate run) and settle the reuse state. Exits 1 when the sentinel is red."""
        from . import sentinel as SN
        cfg = ctx.obj
        pcfg = _sentinel_project(cfg, project)
        try:
            out = SN.run(cfg, project, project_cfg=pcfg)
            state = SN.read(cfg, project, root=pcfg.get("_root"), project_cfg=pcfg)
        except (GitError, ValueError, OSError) as e:
            raise click.ClickException(getattr(e, "tail", None) or str(e))
        if as_json:
            _dump_json({"schema": "nw.queue.sentinel.v1", **out, "state": state})
        else:
            verdict = ("[yellow]interrupted[/]" if out["interrupted"] else
                       "[green]green[/]" if out["ok"] else "[red]red[/]")
            console.print(f"sentinel {project} at {out['sha'][:10]}: {verdict}", highlight=False)
            for r in out["results"]:
                console.print(f"  {'✓' if r['ok'] else '✗'} {r['name']}  {r['log']}",
                              highlight=False)
            if out["suspects"]:
                console.print("  settled suspects: " + ", ".join(
                    f"{x['campaign']} ({x['merge_sha'][:10]})" for x in out["suspects"]),
                    highlight=False)
            console.print(f"  reuses since green: {state['reuses_since_green']}, "
                          f"force_full_close: {state['force_full_close']}", highlight=False)
        if not out["ok"]:
            ctx.exit(1)

    @queue_sentinel.command("clear")
    @click.option("--project", required=True, shell_complete=C.complete_project)
    @click.option("--json", "as_json", is_flag=True, help="emit nw.queue.sentinel.v1 JSON")
    @click.pass_obj
    def queue_sentinel_clear(cfg, project, as_json):
        """Clear `force_full_close` and `due_since` (the suspects stay)."""
        from . import sentinel as SN
        pcfg = _sentinel_project(cfg, project)
        try:
            state = SN.clear(cfg, project, root=pcfg.get("_root"), project_cfg=pcfg)
        except (GitError, ValueError, OSError) as e:
            raise click.ClickException(getattr(e, "tail", None) or str(e))
        if as_json:
            _dump_json({"schema": "nw.queue.sentinel.v1", "project": project, "state": state})
            return
        console.print(f"[green]✓[/] {project}: force_full_close cleared "
                      f"({len(state['suspects'])} suspects kept)", highlight=False)

    return queue_group
