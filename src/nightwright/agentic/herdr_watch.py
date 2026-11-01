"""`nw herdr watch` (SPEC-0361, display design SPEC-0363, epic SPEC-0360): push queue state to
herdr (a display agent on nw's panes, tokens on nw's workspaces) and raise herdr
notifications for the events that need the human.

The only caller of `agentic/herdr.py`. It reads the same state as `nw queue conduct-status` /
`nw queue list` and the event stream with its own cursor (`herdr`); it targets explicit pane /
workspace ids (the placements the conductor / steward followers record) with `--source nw`, a
TTL of 3 push intervals and an increasing `--seq`, so a dead watcher's values expire on their
own. Nothing here is imported by the queue loop, the runner or `runproc`."""
from __future__ import annotations

import datetime as dt
import os
import sys
import time

from . import conductor_cost as CC
from . import events as E
from . import herdr as H
from . import store

READER = "herdr"
INTERVAL_S = 10.0
POLL_S = 2.0
COST_REFRESH_S = 60.0
WS_TOKENS = ("nw_queue", "nw_attn", "nw_cost")
AGENT = "claude"                 # the label herdr shows for a Claude pane; kept as the prefix
SEP = " · "
DISPLAY_MAX = 64
PART_MAX = 24


def _usd(x) -> str:
    return f"${float(x or 0.0):.2f}"


def _money(x) -> str:
    """`$40.95`; whole dollars drop the cents (`$115`, `$0`)."""
    v = round(float(x or 0.0), 2)
    return f"${v:.0f}" if v == int(v) else f"${v:.2f}"


def _cut(text, n) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def _display(parts) -> str:
    return _cut(SEP.join([AGENT, *[p for p in parts if p]]), DISPLAY_MAX)


def conductor_display(snap, cost="") -> str:
    """`claude · conductor · $5.24 · 2 need you · 1 undecided` (empty parts omitted)."""
    return _display(["conductor", cost,
                     f"{snap['escalations']} need you" if snap.get("escalations") else "",
                     f"{snap['undecided']} undecided" if snap.get("undecided") else ""])


def steward_display(snap, project) -> str:
    """`claude · steward:P · SPEC-0353 loop 16 · $40.95/$115`, `… · 2 active · $x/$y` or
    `… · idle`, from P's items in an active state."""
    mine = [i for i in snap["items"] if i.get("project") == project
            and i.get("state") in snap["active_states"]]
    role = f"steward:{_cut(project, PART_MAX)}"
    if not mine:
        return _display([role, "idle"])
    caps = [i.get("cap_usd") for i in mine]
    spend = _money(sum(float(i.get("cost_usd") or 0) for i in mine))
    cost = (f"{spend}/{_money(sum(float(c) for c in caps))}"
            if all(c is not None for c in caps) else spend)
    if len(mine) == 1:
        it = mine[0]
        label = _cut(it.get("label") or it.get("spec_id") or it.get("campaign") or "?", PART_MAX)
        loop = str(it.get("loops") or "").split("/", 1)[0]
        what = f"{label} loop {loop}" if loop else label
    else:
        what = f"{len(mine)} active"
    return _display([role, what, cost])


def _pane_workspace(pane_id) -> str | None:
    """herdr 0.9.5 pane ids are workspace-scoped (`w7:p2` lives in `w7`)."""
    ws, sep, rest = str(pane_id or "").partition(":")
    return ws if sep and ws and rest else None


def notification_for(ev) -> tuple[str, str, str] | None:
    """`(title, body, sound)` for an event that should notify (epic invariant 8), else None."""
    from .queue import park_owner
    t, d = ev.get("type"), ev.get("data") or {}
    who = ev.get("spec_id") or ev.get("campaign") or ""
    if t == "escalation":
        return "nw: needs you", f"{who} {d.get('question') or ''}".strip(), "request"
    if t == "run_failed" and d.get("transient") is False:
        return ("nw: run failed",
                f"{who} {d.get('persona') or ''}: {str(d.get('error') or '')[:160]}".strip(),
                "request")
    if t == "parked" and park_owner(d.get("reason")) == "conductor":
        return "nw: parked", f"{who} {d.get('reason') or ''}".strip(), "request"
    if t == "merged":
        return "nw: merged", f"{who} {str(d.get('merged_sha') or '')[:10]}".strip(), "done"
    return None


class Watcher:
    def __init__(self, cfg, client, *, interval_s=INTERVAL_S, conductor_pane=None,
                 workspace=None, env=None, now=None, run_panes=False, run_pane_linger=None,
                 clock=None):
        self.cfg = cfg
        self.client = client
        self.interval_s = float(interval_s)
        self.conductor_pane = conductor_pane
        self.workspace = workspace
        self.env = os.environ if env is None else env
        self.now = now or (lambda: dt.datetime.now().astimezone())
        self.pushed: dict = {}                    # (kind, target) -> (token names, display set?)
        self._cost_cache: dict = {}                        # session id -> (monotonic, usd|None)
        self.run_panes = None                     # SPEC-0364: opt-in; None = SPEC-0363 exactly
        if run_panes:
            from . import herdr_runpanes as RPN
            self.run_panes = RPN.RunPanes(
                cfg, client, ttl_ms=self.ttl_ms, env=self.env,
                linger_s=RPN.LINGER_S if run_pane_linger is None else run_pane_linger,
                **({"clock": clock} if clock else {}))

    @property
    def ttl_ms(self) -> int:
        return int(round(3 * self.interval_s * 1000))

    # ---- state ---------------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Everything the tokens need, from the queue's own read models."""
        from .queue import ACTIVE_STATES, queue_payload
        now = self.now()
        items = queue_payload(self.cfg, now=now)["items"]
        counts = (E.ping_digest(self.cfg, now) or {}).get("counts") or {}
        spend = 0.0
        by_project: dict = {}

        def proj(name):
            return by_project.setdefault(name, {"active": 0, "queued": 0, "parked": 0,
                                                "spend_today": 0.0})
        for st in store.list_campaigns(self.cfg, include_terminal=True):
            updated = st.get("updated_at") or st.get("created_at")
            try:
                if updated and dt.datetime.fromisoformat(updated).astimezone().date() == now.date():
                    usd = float((st.get("cost") or {}).get("total_usd") or 0.0)
                    spend += usd
                    proj(st.get("project"))["spend_today"] += usd
            except (TypeError, ValueError):
                pass
        for i in items:
            key = ("active" if i.get("state") in ACTIVE_STATES else
                   {"QUEUED": "queued", "PARKED": "parked"}.get(i.get("state")))
            if key:
                proj(i.get("project"))[key] += 1
        return {"items": items, "active_states": ACTIVE_STATES, "spend_today": spend,
                "by_project": by_project,
                "active": sum(1 for i in items if i.get("state") in ACTIVE_STATES),
                "queued": sum(1 for i in items if i.get("state") == "QUEUED"),
                "parked": sum(1 for i in items if i.get("state") == "PARKED"),
                "escalations": int(counts.get("escalations_open") or 0),
                "undecided": int(counts.get("undecided_parks") or 0)}

    @staticmethod
    def attn(snap) -> str:
        parts = []
        if snap["escalations"]:
            parts.append(f"{snap['escalations']} esc")
        if snap["undecided"]:
            parts.append(f"{snap['undecided']} undecided")
        return " · ".join(parts)

    def workspace_tokens(self, snap) -> dict:
        """The conductor's workspace: queue totals."""
        return {"nw_queue": f"{snap['active']} active · {snap['queued']} queued · "
                            f"{snap['parked']} parked",
                "nw_attn": self.attn(snap), "nw_cost": _usd(snap["spend_today"])}

    @staticmethod
    def project_workspace_tokens(snap, projects) -> dict:
        """A steward's workspace: only its project(s)' numbers; `nw_attn` is queue-wide, so
        it is cleared here."""
        rows = [snap.get("by_project", {}).get(p) or {} for p in projects]
        tot = {k: sum(r.get(k) or 0 for r in rows) for k in ("active", "queued", "parked")}
        return {"nw_queue": f"{', '.join(projects)}: {tot['active']} active · "
                            f"{tot['queued']} queued · {tot['parked']} parked",
                "nw_attn": "",
                "nw_cost": _usd(sum(r.get("spend_today") or 0.0 for r in rows))}

    def conductor_cost(self, session_id) -> str:
        if not session_id:
            return ""
        hit = self._cost_cache.get(session_id)
        if hit and time.monotonic() - hit[0] < COST_REFRESH_S:
            return hit[1]
        try:
            today = self.now().date()
            rep = CC.cost_report(self.cfg, since=today, until=today, session_ids=[session_id])
            usd = (rep.get("totals") or {}).get("usd")
            val = _money(usd) if usd and not rep.get("unpriced_models") else ""
        except Exception:
            val = ""
        self._cost_cache[session_id] = (time.monotonic(), val)
        return val

    # ---- targets -------------------------------------------------------------------------

    def placements(self) -> dict:
        """Live placements of the readers the watcher reports to (`conductor`, `steward:P`)."""
        return {r: p for r, p in CC.herdr_panes(self.cfg).items()
                if (r == "conductor" or r.startswith("steward:")) and p.get("pane_id")
                and CC.placement_live(p)}

    def conductor_workspace(self, live=None) -> str | None:
        """`--workspace`, else the conductor pane's placement workspace, else its id prefix."""
        if self.workspace:
            return self.workspace
        live = self.placements() if live is None else live
        cond_pane = self.conductor_pane or (live.get("conductor") or {}).get("pane_id")
        if not cond_pane:
            return None
        by_pane = [p for p in live.values() if p.get("pane_id") == cond_pane]
        return by_pane[0].get("workspace_id") if by_pane else _pane_workspace(cond_pane)

    def targets(self, snap) -> list[tuple[str, str, dict, str | None]]:
        """`[(kind, explicit id, tokens, display_agent)]` for this push (SPEC-0363): a display
        agent on the conductor's and stewards' panes; queue totals on the conductor's
        workspace; a project summary on each steward workspace that is not the conductor's.
        Nothing else, the watcher's own pane/workspace included."""
        live = self.placements()
        out = []
        cond = live.get("conductor")
        cond_pane = self.conductor_pane or (cond or {}).get("pane_id")
        cond_ws = self.conductor_workspace(live)
        if cond_ws:
            out.append(("workspace", cond_ws, self.workspace_tokens(snap), None))
        stewards = sorted((r.split(":", 1)[1], p) for r, p in live.items()
                          if r.startswith("steward:"))
        groups: dict[str, list[str]] = {}
        for project, p in stewards:
            ws = p.get("workspace_id")
            if ws and ws != cond_ws:
                groups.setdefault(ws, []).append(project)
        for ws, projects in groups.items():
            out.append(("workspace", ws, self.project_workspace_tokens(snap, projects), None))
        if cond_pane:
            out.append(("pane", cond_pane, {},
                        conductor_display(snap, self.conductor_cost((cond or {}).get("session_id")))))
        for project, p in stewards:
            if p["pane_id"] != cond_pane:
                out.append(("pane", p["pane_id"], {}, steward_display(snap, project)))
        return out

    # ---- actions -------------------------------------------------------------------------

    def push(self) -> int:
        """One metadata push to every target; returns the number of accepted calls."""
        ok = 0
        snap = self.snapshot()
        for kind, target, tokens, display in self.targets(snap):
            args = H.metadata_args(kind, target, tokens, seq=self.client.next_seq(),
                                   ttl_ms=self.ttl_ms, display_agent=display)
            names, had_display = self.pushed.get((kind, target), (set(), False))
            self.pushed[(kind, target)] = (names | set(tokens), had_display or display is not None)
            ok += bool(self.client.call(args))
        if self.run_panes is not None:
            self.run_panes.reconcile(snap["items"], self.run_pane_workspace())
        return ok

    def run_pane_workspace(self) -> str | None:
        """SPEC-0364: the run tab goes to the conductor's workspace, else the watcher's own."""
        return self.conductor_workspace() or self.env.get("HERDR_WORKSPACE_ID") or None

    def poll_events(self) -> int:
        """Notify for every event after the `herdr` cursor (then advance it); returns how
        many notifications were attempted. Call under the reader lock."""
        try:
            E.sync(self.cfg)
        except Exception:
            pass
        sent = 0
        for ev in E.read_since(self.cfg, E.cursor_get(self.cfg, READER)):
            note = notification_for(ev)
            if note is not None:
                self.client.call(H.notify_args(*note))
                sent += 1
            E.cursor_set(self.cfg, READER, ev["id"])
        return sent

    def start_cursor(self) -> None:
        """A brand-new reader starts at the current end: history is not an alert."""
        if not (E.cursors_dir(self.cfg) / f"{E._safe(READER)}.json").is_file():
            E.cursor_set(self.cfg, READER, E._last_id(E.events_path(self.cfg)))

    def clear(self) -> None:
        for (kind, target), (names, display) in self.pushed.items():
            self.client.call(H.clear_args(kind, target, sorted(names), seq=self.client.next_seq(),
                                          clear_display_agent=display))
        self.pushed.clear()

    def _guard(self, fn):
        """A broken read model (an unreadable campaign, ...) costs one line, not the watcher."""
        try:
            return fn()
        except Exception as e:                      # noqa: BLE001 - best effort by design
            try:
                sys.stderr.write(f"nw herdr: {fn.__name__} failed: {e}\n")
            except Exception:
                pass
            return None

    def run(self, *, once=False, stop=None, sleep=time.sleep, on_locked=None) -> None:
        """Hold the `herdr` reader lock; poll events every `min(POLL_S, interval)` and push
        tokens every interval until `stop()` (or a signal). A clean exit clears what it pushed;
        `once` does one poll + one push and leaves the tokens to their TTL. `on_locked()` runs
        once the lock is held (SPEC-0365: the CLI records `herdr_watch.json` there)."""
        with E.reader_lock(self.cfg, READER):
            if on_locked is not None:
                self._guard(on_locked)
            self.start_cursor()
            if self.run_panes is not None:
                self._guard(self.run_panes.load)
            if once:
                self.poll_events()
                self.push()
                return
            poll = min(POLL_S, self.interval_s)
            next_push = 0.0
            try:
                while True:
                    self._guard(self.poll_events)
                    if time.monotonic() >= next_push:
                        self._guard(self.push)
                        next_push = time.monotonic() + self.interval_s
                    if stop is not None and stop():
                        return
                    sleep(poll)
            finally:
                self.clear()
                if self.run_panes is not None:
                    self._guard(self.run_panes.close_all)
