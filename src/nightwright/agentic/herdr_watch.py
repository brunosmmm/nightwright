"""`nw herdr watch` (SPEC-0361, epic SPEC-0360): push queue state to herdr as sidebar tokens
and raise herdr notifications for the events that need the human.

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
CONDUCTOR_TOKENS = ("nw_role", "nw_attn", "nw_cost")
STEWARD_TOKENS = ("nw_role", "nw_spec", "nw_loop", "nw_cost")


def _usd(x) -> str:
    return f"${float(x or 0.0):.2f}"


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
                 workspace=None, env=None, now=None):
        self.cfg = cfg
        self.client = client
        self.interval_s = float(interval_s)
        self.conductor_pane = conductor_pane
        self.workspace = workspace
        self.env = os.environ if env is None else env
        self.now = now or (lambda: dt.datetime.now().astimezone())
        self.pushed: dict[tuple[str, str], set] = {}       # (kind, target) -> token names
        self._cost_cache: dict = {}                        # session id -> (monotonic, usd|None)

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
        for st in store.list_campaigns(self.cfg, include_terminal=True):
            updated = st.get("updated_at") or st.get("created_at")
            try:
                if updated and dt.datetime.fromisoformat(updated).astimezone().date() == now.date():
                    spend += float((st.get("cost") or {}).get("total_usd") or 0.0)
            except (TypeError, ValueError):
                pass
        return {"items": items, "active_states": ACTIVE_STATES, "spend_today": spend,
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
        return {"nw_queue": f"{snap['active']} active · {snap['queued']} queued · "
                            f"{snap['parked']} parked",
                "nw_attn": self.attn(snap), "nw_cost": _usd(snap["spend_today"])}

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
            val = _usd(usd) if usd and not rep.get("unpriced_models") else ""
        except Exception:
            val = ""
        self._cost_cache[session_id] = (time.monotonic(), val)
        return val

    def conductor_tokens(self, snap, session_id=None) -> dict:
        return {"nw_role": "conductor", "nw_attn": self.attn(snap),
                "nw_cost": self.conductor_cost(session_id)}

    @staticmethod
    def steward_tokens(snap, project) -> dict:
        mine = [i for i in snap["items"] if i.get("project") == project
                and i.get("state") in snap["active_states"]]
        tok = {"nw_role": f"steward:{project}", "nw_spec": "idle", "nw_loop": "", "nw_cost": ""}
        if len(mine) == 1:
            it = mine[0]
            tok["nw_spec"] = str(it.get("label") or it.get("spec_id") or it.get("campaign"))
            tok["nw_loop"] = f"{str(it.get('state') or '').lower()} {it.get('loops') or ''}".strip()
            tok["nw_cost"] = f"{_usd(it.get('cost_usd'))}/{_usd(it.get('cap_usd'))}"
        elif mine:
            tok["nw_spec"] = f"{len(mine)} active"
            tok["nw_cost"] = (f"{_usd(sum(float(i.get('cost_usd') or 0) for i in mine))}/"
                              f"{_usd(sum(float(i.get('cap_usd') or 0) for i in mine))}")
        return tok

    # ---- targets -------------------------------------------------------------------------

    def placements(self) -> dict:
        """Live placements of the readers the watcher reports to (`conductor`, `steward:P`)."""
        return {r: p for r, p in CC.herdr_panes(self.cfg).items()
                if (r == "conductor" or r.startswith("steward:")) and p.get("pane_id")
                and CC.placement_live(p)}

    def targets(self, snap) -> list[tuple[str, str, dict]]:
        """`[(kind, explicit id, tokens)]` for this push."""
        live = self.placements()
        out = []
        if self.workspace:
            workspaces = [self.workspace]
        else:
            workspaces = [self.env.get("HERDR_WORKSPACE_ID")] + [p.get("workspace_id")
                                                                 for p in live.values()]
        for ws in dict.fromkeys(w for w in workspaces if w):
            out.append(("workspace", ws, self.workspace_tokens(snap)))
        cond = live.get("conductor")
        if self.conductor_pane:
            out.append(("pane", self.conductor_pane,
                        self.conductor_tokens(snap, (cond or {}).get("session_id"))))
        elif cond:
            out.append(("pane", cond["pane_id"], self.conductor_tokens(snap, cond.get("session_id"))))
        for reader, p in sorted(live.items()):
            if reader.startswith("steward:"):
                out.append(("pane", p["pane_id"],
                            self.steward_tokens(snap, reader.split(":", 1)[1])))
        return out

    # ---- actions -------------------------------------------------------------------------

    def push(self) -> int:
        """One metadata push to every target; returns the number of accepted calls."""
        ok = 0
        for kind, target, tokens in self.targets(self.snapshot()):
            args = H.metadata_args(kind, target, tokens, seq=self.client.next_seq(),
                                   ttl_ms=self.ttl_ms)
            self.pushed.setdefault((kind, target), set()).update(tokens)
            ok += bool(self.client.call(args))
        return ok

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
        for (kind, target), names in self.pushed.items():
            self.client.call(H.clear_args(kind, target, sorted(names),
                                          seq=self.client.next_seq()))
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

    def run(self, *, once=False, stop=None, sleep=time.sleep) -> None:
        """Hold the `herdr` reader lock; poll events every `min(POLL_S, interval)` and push
        tokens every interval until `stop()` (or a signal). A clean exit clears what it pushed;
        `once` does one poll + one push and leaves the tokens to their TTL."""
        with E.reader_lock(self.cfg, READER):
            self.start_cursor()
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
