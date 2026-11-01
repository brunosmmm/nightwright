"""herdr run panes (SPEC-0364, epic SPEC-0360 child 3): with `nw herdr watch --run-panes`, one
herdr pane per active campaign that has a persona run, in one `nw runs` tab, running
`nw campaign tail --follow` of that run, with herdr's agent state and a display agent.

Used only by `herdr_watch`; every herdr call goes through the `agentic/herdr.py` client it is
given. nw touches only the tab and panes it created: their ids are recorded (per herdr socket)
in `<data_dir>/agentic/conductor/herdr_run_panes.json`, each pane is renamed
`nw-run:<campaign>`, and a recorded pane is adopted only after `pane get` shows that label in
the recorded tab. Nothing else is ever split, renamed or closed."""
from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

from . import conductor_cost as CC
from . import herdr as H
from . import runproc as RP
from . import runs as RN
from . import store
from . import tail as TL

TAB_LABEL = "nw runs"
LINGER_S = 300.0
MAX_PANES = 6
RUN_LOOKBACK = 8                     # newest run dirs looked at per campaign
RUNNING = ("running", "orphan_group")
SEP = " · "
DISPLAY_MAX = 64
PART_MAX = 24
SCHEMA = "nw.herdr.run_panes.v1"


def _cut(text, n) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def _money(x) -> str:
    v = round(float(x or 0.0), 2)
    return f"${v:.0f}" if v == int(v) else f"${v:.2f}"


def pane_label(cid) -> str:
    """The ownership mark nw puts on each run pane (`pane rename`) and checks on adoption."""
    return f"nw-run:{cid}"


def state_path(cfg) -> Path:
    return CC.conductor_dir(cfg) / "herdr_run_panes.json"


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


def tail_command(cfg, cid, run) -> str:
    """What is typed into the pane. Its shell is the herdr server's, with the server's env, so
    the config/data dirs are passed explicitly and this interpreter runs the viewer. Quoted
    with single quotes, which fish and POSIX shells read alike."""
    from .. import paths
    words = ["env", f"NW_CONFIG_DIR={cfg.get('config_dir') or paths.config_dir()}",
             f"NW_DATA_DIR={cfg.get('data_dir') or paths.data_dir()}",
             sys.executable, "-m", "nightwright", "campaign", "tail", str(cid), "--run", str(run),
             "--follow"]
    return " ".join(shlex.quote(w) for w in words)


def agent_state(running: bool, campaign_state) -> str:
    """working while a persona run runs; blocked when parked; idle otherwise (SPEC-0364 D7)."""
    if running:
        return "working"
    return "blocked" if campaign_state == "PARKED" else "idle"


def status_text(campaign_state, park=None) -> str:
    st = str(campaign_state or "?").lower().replace("_", " ")
    if campaign_state == "PARKED" and (park or {}).get("reason"):
        return f"parked: {park['reason']}"
    return st


def run_display(label, *, persona=None, loop=None, cost=None, budget=None, status=None) -> str:
    """`nw run · SPEC-0354 · implementer · loop 3 · $1.20/$10` while a run runs;
    `nw run · SPEC-0354 · gating` / `… · parked: cost_cap` otherwise. Cut to 64 chars."""
    parts = ["nw run", _cut(label or "?", PART_MAX)]
    if status:
        parts.append(_cut(status, PART_MAX))
    else:
        if persona:
            parts.append(_cut(persona, PART_MAX))
        if loop:
            parts.append(f"loop {loop}")
        if cost is not None:
            parts.append(_money(cost) + (f"/{_money(budget)}" if budget else ""))
        elif budget:
            parts.append(f"budget {_money(budget)}")
    return _cut(SEP.join(parts), DISPLAY_MAX)


class RunPanes:
    def __init__(self, cfg, client, *, ttl_ms, linger_s=LINGER_S, max_panes=MAX_PANES,
                 env=None, clock=time.time, err=None):
        self.cfg = cfg
        self.client = client
        self.ttl_ms = int(ttl_ms)
        self.linger_s = float(linger_s)
        self.max_panes = int(max_panes)
        self.env = os.environ if env is None else env
        self.clock = clock
        self.err = err
        self.socket = self.env.get("HERDR_SOCKET_PATH") or ""
        self.tab: str | None = None
        self.tab_label = TAB_LABEL
        self.workspace: str | None = None
        self.panes: dict[str, dict] = {}            # cid -> {pane_id, run, state, ended_at, ...}
        self.unverified: dict[str, dict] = {}       # recorded, not yet confirmed as ours
        self.dismissed: set[str] = set()            # the human closed its pane
        self.overflow = 0
        self._stats: dict[str, tuple] = {}          # run dir -> (Renderer, byte offset)
        self._said_no_ws = False

    # ---- persistence -------------------------------------------------------------------------

    def _read_all(self) -> dict:
        try:
            data = json.loads(state_path(self.cfg).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return {}
        return data.get("sessions") if isinstance(data.get("sessions"), dict) else {}

    def save(self) -> None:
        """Atomic replace; only this socket's entry changes (one watcher holds the lock)."""
        sessions = self._read_all()
        mine = {"tab": self.tab, "tab_label": self.tab_label, "workspace": self.workspace,
                "panes": {**self.unverified, **self.panes}, "dismissed": sorted(self.dismissed)}
        if self.tab or mine["panes"]:
            sessions[self.socket] = mine
        else:
            sessions.pop(self.socket, None)
        path = state_path(self.cfg)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"schema": SCHEMA, "sessions": sessions}, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            self._say(f"cannot save {path}: {e}")

    def load(self) -> None:
        """Take over what an earlier watcher on this herdr socket recorded; verify before use."""
        mine = self._read_all().get(self.socket) or {}
        self.tab = mine.get("tab")
        self.tab_label = mine.get("tab_label") or TAB_LABEL
        self.workspace = mine.get("workspace")
        self.dismissed = set(mine.get("dismissed") or [])
        self.unverified = {cid: dict(p) for cid, p in (mine.get("panes") or {}).items()
                           if isinstance(p, dict) and p.get("pane_id")}
        self.panes = {}
        self._verify()

    def _verify(self) -> None:
        """Adopt a recorded pane only when `pane get` shows our label in our tab; forget it when
        herdr says it is gone or it is not ours; keep it unverified on any other failure."""
        for cid, p in list(self.unverified.items()):
            info = self.client.call_json(H.pane_get_args(p["pane_id"]))
            pane = (info or {}).get("pane") if info else None
            if not isinstance(pane, dict):
                if "not_found" in (self.client.last_error or ""):
                    del self.unverified[cid]
                continue
            del self.unverified[cid]
            if pane.get("label") != pane_label(cid) or (self.tab and pane.get("tab_id") != self.tab):
                continue                                   # not ours (any more): never touched
            self.tab = self.tab or pane.get("tab_id")
            proc = self.client.call_json(H.pane_process_info_args(p["pane_id"]))
            pi = (proc or {}).get("process_info") or {}
            if pi.get("shell_pid") and pi.get("foreground_process_group_id") == pi.get("shell_pid"):
                p["retype"] = True                        # a fresh shell (herdr restored it)
            self.panes[cid] = p
        if not self.panes and not self.unverified:
            self.tab = None

    # ---- helpers -----------------------------------------------------------------------------

    def _say(self, line) -> None:
        try:
            (self.err or sys.stderr).write(f"nw herdr: run panes: {line}\n")
        except Exception:
            pass

    def _gone(self) -> bool:
        return "not_found" in (self.client.last_error or "")

    def _forget(self, cid, *, dismiss=False) -> None:
        p = self.panes.pop(cid, None)
        if p:
            self._stats.pop(str(store.runs_dir(self.cfg, cid) / str(p.get("run"))), None)
        if dismiss:
            self.dismissed.add(cid)
        if not self.panes and not self.unverified:
            self.tab = None                    # herdr closed the tab with its last pane
            self.tab_label = TAB_LABEL

    def _cwd(self, item) -> str:
        wt = (item or {}).get("worktree")
        return wt if wt and os.path.isdir(wt) else os.path.expanduser("~")

    def _type_tail(self, cid, run) -> bool:
        p = self.panes[cid]
        ok = self.client.call(H.pane_run_args(p["pane_id"], tail_command(self.cfg, cid, run)))
        if ok:
            if p.get("run") != run:
                self._stats.pop(str(store.runs_dir(self.cfg, cid) / str(p.get("run"))), None)
            p["run"] = run
            p.pop("retype", None)
        elif self._gone():
            self._forget(cid, dismiss=True)
        return ok

    def _split_target(self) -> tuple[str, str]:
        """nw's own largest pane in the tab, and `right` when it is at least twice as wide as
        tall in cells (cells are about twice as tall as wide), else `down`."""
        ours = {p["pane_id"] for p in self.panes.values()}
        anyone = next(iter(self.panes.values()))["pane_id"]
        layout = (self.client.call_json(H.pane_layout_args(anyone)) or {}).get("layout") or {}
        best = None
        for entry in layout.get("panes") or []:
            r = entry.get("rect") or {}
            if entry.get("pane_id") in ours and r.get("width") and r.get("height"):
                area = int(r["width"]) * int(r["height"])
                if best is None or area > best[0]:
                    best = (area, entry["pane_id"], int(r["width"]), int(r["height"]))
        if best is None:
            return anyone, "right"
        return best[1], "right" if best[2] >= 2 * best[3] else "down"

    def _create(self, cid, run, item, workspace) -> bool:
        cwd = self._cwd(item)
        if not self.panes:
            res = self.client.call_json(H.tab_create_args(workspace, TAB_LABEL, cwd)) or {}
            pane = ((res.get("root_pane") or {}).get("pane_id"))
            tab = (res.get("tab") or {}).get("tab_id")
            if not pane or not tab:
                return False
            self.tab, self.tab_label, self.workspace = tab, TAB_LABEL, workspace
        else:
            target, direction = self._split_target()
            res = self.client.call_json(H.pane_split_args(target, direction, cwd)) or {}
            pane = (res.get("pane") or {}).get("pane_id")
            if not pane:
                return False
        self.panes[cid] = {"pane_id": pane, "run": None, "state": None, "ended_at": None,
                           "created_at": self.clock()}
        self.client.call(H.pane_rename_args(pane, pane_label(cid)))
        self._type_tail(cid, run)
        return True

    def _close(self, cid) -> bool:
        p = self.panes.get(cid)
        if p is None:
            return True
        if self.client.call(H.pane_close_args(p["pane_id"])) or self._gone():
            self._forget(cid)
            return True
        return False

    def _evict_one(self) -> bool:
        lingering = sorted((p["ended_at"], cid) for cid, p in self.panes.items()
                           if p.get("ended_at") is not None)
        return bool(lingering) and self._close(lingering[0][1])

    def _run_cost(self, cid, run) -> tuple:
        """(cost so far or None, budget or None) of one run, fed incrementally."""
        rd = store.runs_dir(self.cfg, cid) / str(run)
        req = RP.read_json(rd / "request.json") or {}
        budget = req.get("max_budget_usd")
        key = str(rd)
        r, off = self._stats.get(key) or (TL.Renderer(budget_usd=budget,
                                                      price_lookup=TL._price_lookup(self.cfg)), 0)
        lines, off = TL.read_lines(rd / "stream.jsonl", off)
        for line in lines:
            r.feed_line(line)
        self._stats[key] = (r, off)
        cost = r.cost_usd if r.cost_usd is not None else r.est_usd() if r.turns else None
        return cost, budget, req.get("persona")

    def _final(self, cid, by_cid) -> tuple:
        """(state, park) of a campaign that is no longer active."""
        it = by_cid.get(cid)
        if it is not None:
            return it.get("state"), it.get("park")
        try:
            st = store.read_state(self.cfg, cid)
            return st.get("state"), st.get("park")
        except (ValueError, OSError):
            return None, None

    # ---- the push ----------------------------------------------------------------------------

    def reconcile(self, items, workspace) -> None:
        """One step: adopt, create, retarget, report, linger and close; then save."""
        from .queue import ACTIVE_STATES
        now = self.clock()
        if self.unverified:
            self._verify()
        by_cid = {i.get("campaign"): i for i in items if i.get("campaign")}
        active = {cid: i for cid, i in by_cid.items() if i.get("state") in ACTIVE_STATES}
        self.dismissed &= set(active)
        running = {cid: running_runs(self.cfg, cid) for cid in active}

        for cid, p in list(self.panes.items()):
            if cid in active:
                p["ended_at"] = None
                runs = running[cid]
                if runs and p.get("run") not in runs:
                    self._type_tail(cid, runs[0])          # the next run, in the same pane
                elif p.get("retype") and p.get("run"):
                    self._type_tail(cid, p["run"])
            else:
                if p.get("ended_at") is None:
                    p["ended_at"] = now
                if p.get("retype") and p.get("run") and cid in self.panes:
                    self._type_tail(cid, p["run"])
                if cid in self.panes and now - p["ended_at"] >= self.linger_s:
                    self._close(cid)

        need = [cid for cid in active if running[cid] and cid not in self.panes
                and cid not in self.dismissed]
        created = 0
        for cid in need:
            ws = self.workspace if self.panes else workspace
            if not ws:
                if not self._said_no_ws:
                    self._say("no workspace (no conductor placement, --workspace or "
                              "HERDR_WORKSPACE_ID); not opening run panes")
                    self._said_no_ws = True
                break
            if len(self.panes) >= self.max_panes and not self._evict_one():
                break
            if not self._create(cid, running[cid][0], active[cid], ws):
                break
            created += 1
        self.overflow = len(need) - created

        for cid in list(self.panes):
            self._report(cid, active, by_cid, running)

        want = TAB_LABEL if not self.overflow else f"{TAB_LABEL} (+{self.overflow})"
        if self.tab and want != self.tab_label:
            if self.client.call(H.tab_rename_args(self.tab, want)):
                self.tab_label = want
        self.save()

    def _report(self, cid, active, by_cid, running) -> None:
        p = self.panes[cid]
        item = active.get(cid) or by_cid.get(cid) or {}
        label = item.get("label") or item.get("spec_id") or p.get("label") or cid
        p["label"] = label
        runs = running.get(cid) or []
        if cid in active:
            state = agent_state(bool(runs), item.get("state"))
            if runs:
                cost, budget, persona = self._run_cost(cid, p.get("run") or runs[0])
                loop = str(item.get("loops") or "").split("/", 1)[0]
                display = run_display(label, persona=persona, loop=loop or None, cost=cost,
                                      budget=budget)
            else:
                display = run_display(label, status=status_text(item.get("state")))
        else:
            final, park = self._final(cid, by_cid)
            state = agent_state(False, final)
            display = run_display(label, status=status_text(final, park))
        # Every push, not only on change: herdr binds a reported agent to the pane's foreground
        # process and releases it when that process exits (the tail exits with its run); the
        # same state again is not a transition (no new state_change_seq, no sound).
        if self.client.call(H.report_agent_args(p["pane_id"], state, seq=self.client.next_seq())):
            p["state"] = state
        elif self._gone():
            self._forget(cid, dismiss=cid in active)
            return
        if not self.client.call(H.metadata_args("pane", p["pane_id"], {},
                                                seq=self.client.next_seq(), ttl_ms=self.ttl_ms,
                                                source=H.RUN_SOURCE, display_agent=display)):
            if self._gone():
                self._forget(cid, dismiss=cid in active)

    def close_all(self) -> None:
        """A clean exit: close every pane nw owns (herdr closes the tab with the last one)."""
        for cid in list(self.panes):
            self._close(cid)
        self.save()
