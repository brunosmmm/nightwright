"""herdr run tabs (SPEC-0366, superseding SPEC-0364; epic SPEC-0360): with
`nw herdr watch --run-panes`, one herdr tab per campaign that has a running persona run,
labelled after the work (`SPEC-0349 nw doctor`), whose one pane runs
`nw campaign tail CID --follow-campaign` for its whole life, with herdr's agent state and a
display agent.

Used only by `herdr_watch`; every herdr call goes through the `agentic/herdr.py` client it is
given. nw touches only the tabs and panes it created: their ids are recorded (per herdr socket)
in `<data_dir>/agentic/conductor/herdr_run_panes.json`, each pane is renamed
`nw-run:<campaign>`, and a recorded pane is adopted only after `pane get` shows that label in
the pane's recorded tab. Nothing else is ever renamed or closed. Panes recorded by SPEC-0364's
code (schema v1, one shared `nw runs` tab) are verified the same way and then closed, so their
campaigns get tabs of their own."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

from . import conductor_cost as CC
from . import herdr as H
from . import runproc as RP
from . import store
from . import tail as TL

LINGER_S = 60.0
MAX_PANES = 6
SEP = " · "
DISPLAY_MAX = 64
PART_MAX = 24
TITLE_MAX = 24
SCHEMA = "nw.herdr.run_panes.v2"
SCHEMA_V1 = "nw.herdr.run_panes.v1"         # SPEC-0364: one shared tab per session

running_runs = TL.running_runs              # shared with the viewer


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


def linger_default(cfg) -> float:
    """`agentic.herdr.run_pane_linger_s`, else 60 s (SPEC-0366 D6)."""
    v = (((cfg or {}).get("agentic") or {}).get("herdr") or {}).get("run_pane_linger_s")
    try:
        return max(0.0, float(v)) if v is not None else LINGER_S
    except (TypeError, ValueError):
        return LINGER_S


# ---- naming ------------------------------------------------------------------------------

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_EMPH = re.compile(r"(\*\*|__|\*|`)")
_UNDERSCORE_EMPH = re.compile(r"(?<!\w)_([^_]+)_(?!\w)")


def short_title(text) -> str:
    """Markdown stripped, the first clause (before `:` / `—` / `–`), at most 24 chars (cut on a
    word boundary when one falls in the second half, then `…`)."""
    t = _LINK.sub(r"\1", str(text or ""))
    t = _UNDERSCORE_EMPH.sub(r"\1", _EMPH.sub("", t))
    t = re.split(r"\s*[:—–]\s*", t, maxsplit=1)[0]
    t = " ".join(t.split())
    if len(t) <= TITLE_MAX:
        return t
    cut = t[:TITLE_MAX - 1]
    if " " in cut[TITLE_MAX // 2:]:                 # end on a word when one ends late enough
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip() + "…"


def title_of(cfg, cid, item=None) -> str:
    """The campaign's title: `spec_at_open.md` frontmatter (internal and outbound specs), else
    an idea campaign's heading; "" when there is none (merge requests)."""
    from .. import specmeta
    try:
        text = (store.campaign_dir(cfg, cid) / "spec_at_open.md").read_text(encoding="utf-8")
        fm, _ = specmeta.split_frontmatter(text)
        if isinstance(fm, dict) and fm.get("title"):
            return str(fm["title"])
    except Exception:                       # unreadable / bad YAML: fall back
        pass
    idea = (item or {}).get("idea") or {}
    if idea.get("heading"):
        return str(idea["heading"])
    try:
        return str(((store.read_state(cfg, cid).get("idea") or {}).get("heading")) or "")
    except (ValueError, OSError):
        return ""


def tab_label(label, title) -> str:
    """`SPEC-0349 nw doctor`; the label alone without a title."""
    short = short_title(title)
    return f"{label} {short}" if short else str(label)


def tail_command(cfg, cid) -> str:
    """What is typed into the pane, once. Its shell is the herdr server's, with the server's
    env, so the config/data dirs are passed explicitly and this interpreter runs the viewer.
    Quoted with single quotes, which fish and POSIX shells read alike. The viewer clears the
    screen first, so the echoed line does not stay."""
    from .. import paths
    words = ["env", f"NW_CONFIG_DIR={cfg.get('config_dir') or paths.config_dir()}",
             f"NW_DATA_DIR={cfg.get('data_dir') or paths.data_dir()}",
             sys.executable, "-m", "nightwright", "campaign", "tail", str(cid),
             "--follow-campaign"]
    return " ".join(shlex.quote(w) for w in words)


def agent_state(running: bool, campaign_state) -> str:
    """working while a persona run runs; blocked when parked; idle otherwise."""
    if running:
        return "working"
    return "blocked" if campaign_state == "PARKED" else "idle"


def status_text(campaign_state, park=None) -> str:
    st = str(campaign_state or "?").lower().replace("_", " ")
    if campaign_state == "PARKED" and (park or {}).get("reason"):
        return f"parked: {park['reason']}"
    return st


def run_display(*, persona=None, loop=None, cost=None, budget=None, status=None) -> str:
    """`nw run · implementer · loop 3 · $1.20/$10` while a run runs; `nw run · gating` /
    `nw run · parked: cost_cap` otherwise. The tab names the campaign (SPEC-0366 D4)."""
    parts = ["nw run"]
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
    def __init__(self, cfg, client, *, ttl_ms, linger_s=None, max_panes=MAX_PANES,
                 env=None, clock=time.time, err=None):
        self.cfg = cfg
        self.client = client
        self.ttl_ms = int(ttl_ms)
        self.linger_s = linger_default(cfg) if linger_s is None else float(linger_s)
        self.max_panes = int(max_panes)
        self.env = os.environ if env is None else env
        self.clock = clock
        self.err = err
        self.socket = self.env.get("HERDR_SOCKET_PATH") or ""
        self.workspace: str | None = None
        self.panes: dict[str, dict] = {}     # cid -> {pane_id, tab_id, tab_label, shown_label, ...}
        self.unverified: dict[str, dict] = {}       # recorded, not yet confirmed as ours
        self.dismissed: set[str] = set()            # the human closed its pane
        self.overflow = 0
        self._stats: dict[str, tuple] = {}          # run dir -> (Renderer, byte offset)
        self._said_no_ws = False

    # ---- persistence -------------------------------------------------------------------------

    def _read_all(self) -> dict:
        """Every socket's entry, v1 entries converted: each pane gets its session's shared tab
        as `tab_id` and `legacy: true` (SPEC-0366 D10)."""
        try:
            data = json.loads(state_path(self.cfg).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("schema") not in (SCHEMA, SCHEMA_V1):
            return {}
        sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
        if data.get("schema") == SCHEMA_V1:
            out = {}
            for sock, mine in sessions.items():
                if not isinstance(mine, dict):
                    continue
                panes = {}
                for cid, p in (mine.get("panes") or {}).items():
                    if isinstance(p, dict) and p.get("pane_id"):
                        panes[cid] = {**p, "tab_id": mine.get("tab"), "legacy": True}
                out[sock] = {"workspace": mine.get("workspace"), "panes": panes,
                             "dismissed": []}
            return out
        return sessions

    def save(self) -> None:
        """Atomic replace; only this socket's entry changes (one watcher holds the lock)."""
        sessions = self._read_all()
        mine = {"workspace": self.workspace, "panes": {**self.unverified, **self.panes},
                "dismissed": sorted(self.dismissed)}
        if mine["panes"] or mine["dismissed"]:
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
        self.workspace = mine.get("workspace")
        self.dismissed = set(mine.get("dismissed") or [])
        self.unverified = {cid: dict(p) for cid, p in (mine.get("panes") or {}).items()
                           if isinstance(p, dict) and p.get("pane_id")}
        self.panes = {}
        self._verify()

    def _verify(self) -> None:
        """Adopt a recorded pane only when `pane get` shows our label in its recorded tab;
        forget it when herdr says it is gone or it is not ours; keep it unverified on any other
        failure. A verified SPEC-0364 pane (shared tab) is closed: its campaign gets its own tab
        on this push if its run still runs."""
        for cid, p in list(self.unverified.items()):
            info = self.client.call_json(H.pane_get_args(p["pane_id"]))
            pane = (info or {}).get("pane") if info else None
            if not isinstance(pane, dict):
                if "not_found" in (self.client.last_error or ""):
                    del self.unverified[cid]
                continue
            del self.unverified[cid]
            if pane.get("label") != pane_label(cid) or not p.get("tab_id") \
                    or pane.get("tab_id") != p.get("tab_id"):
                continue                                   # not ours (any more): never touched
            if p.get("legacy"):
                if self.client.call(H.pane_close_args(p["pane_id"])) or self._gone():
                    self._say(f"closed {p['pane_id']} of the old shared tab ({cid}); "
                              f"it gets a tab of its own")
                else:
                    self.unverified[cid] = p                # try again next push
                continue
            proc = self.client.call_json(H.pane_process_info_args(p["pane_id"]))
            pi = (proc or {}).get("process_info") or {}
            if pi.get("shell_pid") and pi.get("foreground_process_group_id") == pi.get("shell_pid"):
                p["retype"] = True                        # a fresh shell (herdr restored it)
            self.panes[cid] = p

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

    def _cwd(self, item) -> str:
        wt = (item or {}).get("worktree")
        return wt if wt and os.path.isdir(wt) else os.path.expanduser("~")

    def _type_viewer(self, cid) -> bool:
        p = self.panes[cid]
        ok = self.client.call(H.pane_run_args(p["pane_id"], tail_command(self.cfg, cid)))
        if ok:
            p.pop("retype", None)
        elif self._gone():
            self._forget(cid, dismiss=True)
        return ok

    def _unique_label(self, base, cid) -> str:
        taken = {p.get("tab_label") for c, p in self.panes.items() if c != cid}
        if base not in taken:
            return base
        alt = f"{base} {str(cid).rsplit('-', 1)[-1]}"
        n = 2
        label = alt
        while label in taken:
            label, n = f"{alt} {n}", n + 1
        return label

    def _create(self, cid, run, item, workspace) -> bool:
        label = item.get("label") or item.get("spec_id") or cid
        tlabel = self._unique_label(tab_label(label, title_of(self.cfg, cid, item)), cid)
        res = self.client.call_json(H.tab_create_args(workspace, tlabel, self._cwd(item))) or {}
        pane = (res.get("root_pane") or {}).get("pane_id")
        tab = (res.get("tab") or {}).get("tab_id")
        if not pane or not tab:
            return False
        self.workspace = workspace
        self.panes[cid] = {"pane_id": pane, "tab_id": tab, "tab_label": tlabel,
                           "shown_label": tlabel, "run": run, "state": None, "ended_at": None,
                           "created_at": self.clock(),
                           "order": 1 + max((q.get("order") or 0 for q in self.panes.values()),
                                            default=0)}
        self.client.call(H.pane_rename_args(pane, pane_label(cid)))
        self._type_viewer(cid)
        return True

    def _close(self, cid) -> bool:
        p = self.panes.get(cid)
        if p is None:
            return True
        if self.client.call(H.pane_close_args(p["pane_id"])) or self._gone():
            self._forget(cid)                  # herdr closes the tab with its only pane
            return True
        return False

    def _evict_one(self) -> bool:
        lingering = sorted((p["ended_at"], cid) for cid, p in self.panes.items()
                           if p.get("ended_at") is not None)
        return bool(lingering) and self._close(lingering[0][1])

    def _run_cost(self, cid, run) -> tuple:
        """(cost so far or None, budget or None, persona) of one run, fed incrementally."""
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

    def _campaign(self, cid, by_cid) -> tuple:
        """(state, park) of a campaign: its queue item, else its state file."""
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
        """One step: adopt, create, report, linger and close; then save."""
        now = self.clock()
        if self.unverified:
            self._verify()
        by_cid = {i.get("campaign"): i for i in items if i.get("campaign")}
        running = {cid: running_runs(self.cfg, cid) for cid in {*by_cid, *self.panes}}
        self.dismissed = {cid for cid in self.dismissed if running.get(cid)}

        for cid, p in list(self.panes.items()):
            runs = running.get(cid) or []
            if runs:
                p["ended_at"] = None
                if p.get("run") not in runs:
                    self._stats.pop(str(store.runs_dir(self.cfg, cid) / str(p.get("run"))), None)
                    p["run"] = runs[0]                 # the viewer switches by itself
            elif p.get("ended_at") is None:
                p["ended_at"] = now
            if p.get("retype"):
                self._type_viewer(cid)
            if cid in self.panes and p.get("ended_at") is not None \
                    and now - p["ended_at"] >= self.linger_s:
                self._close(cid)

        need = [cid for cid in by_cid if running.get(cid) and cid not in self.panes
                and cid not in self.dismissed]
        created = 0
        for cid in need:
            if not workspace:
                if not self._said_no_ws:
                    self._say("no workspace (no conductor placement, --workspace or "
                              "HERDR_WORKSPACE_ID); not opening run tabs")
                    self._said_no_ws = True
                break
            if len(self.panes) >= self.max_panes and not self._evict_one():
                break
            if not self._create(cid, running[cid][0], by_cid[cid], workspace):
                break
            created += 1
        self.overflow = len(need) - created

        for cid in list(self.panes):
            self._report(cid, by_cid, running)
        self._label_overflow()
        self.save()

    def _label_overflow(self) -> None:
        """` (+N)` on nw's newest run tab while N campaigns wait for one (D8)."""
        newest = max(self.panes, key=lambda c: (self.panes[c].get("created_at") or 0,
                                                self.panes[c].get("order") or 0), default=None)
        for cid, p in self.panes.items():
            want = p.get("tab_label") or ""
            if cid == newest and self.overflow:
                want = f"{want} (+{self.overflow})"
            if want and p.get("shown_label") != want and p.get("tab_id"):
                if self.client.call(H.tab_rename_args(p["tab_id"], want)):
                    p["shown_label"] = want

    def _report(self, cid, by_cid, running) -> None:
        p = self.panes[cid]
        runs = running.get(cid) or []
        cstate, park = self._campaign(cid, by_cid)
        item = by_cid.get(cid) or {}
        if runs:
            state = "working"
            cost, budget, persona = self._run_cost(cid, p.get("run") or runs[0])
            loop = str(item.get("loops") or "").split("/", 1)[0]
            display = run_display(persona=persona, loop=loop or None, cost=cost, budget=budget)
        else:
            state = agent_state(False, cstate)
            display = run_display(status=status_text(cstate, park))
        # Every push, not only on change: a report made while the pane's shell or viewer is
        # starting can be released by herdr; the same state again is not a transition.
        if self.client.call(H.report_agent_args(p["pane_id"], state, seq=self.client.next_seq())):
            p["state"] = state
        elif self._gone():
            self._forget(cid, dismiss=bool(runs))
            return
        if not self.client.call(H.metadata_args("pane", p["pane_id"], {},
                                                seq=self.client.next_seq(), ttl_ms=self.ttl_ms,
                                                source=H.RUN_SOURCE, display_agent=display)):
            if self._gone():
                self._forget(cid, dismiss=bool(runs))

    def close_all(self) -> None:
        """A clean exit: close every pane nw owns (herdr closes each tab with its pane)."""
        for cid in list(self.panes):
            self._close(cid)
        self.save()
