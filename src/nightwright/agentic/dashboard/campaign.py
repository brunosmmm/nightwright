"""The per-campaign drilldown (SPEC-0430): `GET /api/campaigns/<cid>` (`nw.dashboard.campaign.v1`:
state, the ledger timeline as `nw campaign log` renders it, the runs as `nw campaign runs` lists
them, the current run) and the live run tail behind `GET /api/campaigns/<cid>/tail` (`Tailer`,
the `nw campaign tail` renderer over `runs/<run>/stream.jsonl`).

Built per request, read-only (no lock, no write), every section failing alone. Not imported by
any queue-side module."""
from __future__ import annotations

import re

from ... import render as R
from .. import runs as RN
from .. import store
from .. import tail as TL

SCHEMA = "nw.dashboard.campaign.v1"
TIMELINE_N = 40
RUNS_N = 20
BACKLOG_LINES = 400
CID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
RUN_KEYS = ("persona", "model", "status", "started_at", "duration_s", "cost_usd", "exit_code",
            "is_error", "timed_out", "verdict_status")


def clean(text) -> str:
    """Plain text safe to show: control characters other than tab removed (ANSI escapes too)."""
    return _CTRL.sub("", str(text if text is not None else ""))


def valid_cid(cid) -> bool:
    return isinstance(cid, str) and bool(CID_RE.match(cid))


def read_state(cfg, cid) -> dict | None:
    """The campaign's state, or None when `cid` is invalid or names no campaign."""
    if not valid_cid(cid):
        return None
    try:
        st = store.read_state(cfg, cid)
    except (ValueError, OSError):
        return None
    return st if isinstance(st, dict) else None


def _error(e: BaseException) -> dict:
    return {"error": f"{type(e).__name__}: {e}"}


def _state(cfg, st: dict) -> dict:
    """The state block: `queue.row_view` (the fields every list view shares, SPEC-0356) plus the
    state, loop and park."""
    from .. import queue as Q
    rv = Q.row_view(cfg, st)
    park = st.get("park") if st.get("state") == "PARKED" else None
    return {"state": st.get("state"), "spec_id": st.get("spec_id"), "project": st.get("project"),
            "label": Q.campaign_label(st), "title": rv.get("title"),
            "loop": int((st.get("counters") or {}).get("loops", 0) or 0),
            "cost_usd": rv.get("cost_usd"), "cap_usd": rv.get("cap_usd"),
            "park": ({k: park.get(k) for k in ("reason", "detail", "escalated_from", "resume_to")}
                     if isinstance(park, dict) else None)}


def _timeline(cfg, cid) -> list[dict]:
    return [{"seq": ev.get("seq"), "ts": ev.get("ts"), "type": ev.get("type"),
             "by": ev.get("by"), "text": clean(R.campaign_event(ev).plain)}
            for ev in store.read_ledger(cfg, cid, tail=TIMELINE_N)]


def _runs(cfg, cid) -> list[dict]:
    rows = sorted(RN.campaign_runs(cfg, cid), key=lambda r: str(r.get("run_id")))[-RUNS_N:]
    return [{"run": r.get("run_id"), **{k: r.get(k) for k in RUN_KEYS}} for r in reversed(rows)]


def current_run(cfg, cid) -> tuple[str | None, bool]:
    """`(run id, running)`: the newest running run, else the newest run, else None."""
    running = TL.running_runs(cfg, cid)
    if running:
        return running[0], True
    dirs = RN._run_dirs(cfg, cid)
    return (sorted(dirs)[-1] if dirs else None), False


def build(cfg, cid) -> dict | None:
    """The drilldown document, or None for an invalid or unknown campaign."""
    st = read_state(cfg, cid)
    if st is None:
        return None
    doc = {"schema": SCHEMA, "generated_at": store.now_iso(), "campaign": cid}
    for key, fn in (("state", lambda: _state(cfg, st)), ("timeline", lambda: _timeline(cfg, cid)),
                    ("runs", lambda: _runs(cfg, cid))):
        try:
            doc[key] = fn()
        except Exception as e:                           # noqa: BLE001 - one section fails alone
            doc[key] = _error(e)
    try:
        doc["current_run"], doc["running"] = current_run(cfg, cid)
    except Exception as e:                               # noqa: BLE001
        doc["current_run"], doc["running"] = None, False
        doc["current_run_error"] = _error(e)["error"]
    return doc


class Tailer:
    """One live tail: `tail.Renderer` over a run's `stream.jsonl` from `offset`, as `nw campaign
    tail -f` renders it, in plain text. `poll()` returns the new lines; `ended()` is the CLI's
    end test. ValueError for an unknown run."""

    def __init__(self, cfg, cid, run=None, offset: int = 0):
        self.run_dir = TL.resolve(cfg, cid, run)
        self.run = self.run_dir.name
        req = TL.RP.read_json(self.run_dir / "request.json") or {}
        rj = TL.RP.read_json(self.run_dir / "run.json") or {}
        budget = req.get("max_budget_usd") or rj.get("max_budget_usd")
        self.renderer = TL.Renderer(cwd=req.get("cwd"), budget_usd=budget,
                                    price_lookup=TL._price_lookup(cfg))
        self.stream = self.run_dir / "stream.jsonl"
        self.offset = max(0, int(offset or 0))
        self.persona = req.get("persona") or rj.get("persona")
        self.header = clean(f"run {self.run} · persona {self.persona or '?'} · model "
                            f"{req.get('model') or rj.get('model') or '?'}"
                            + (f" · budget {TL._usd(budget)}" if budget else ""))
        self.streaming = TL.RP.is_stream(self.run_dir) or self.stream.is_file()

    def ended(self) -> bool:
        try:
            return TL._ended(self.run_dir)
        except OSError:
            return True

    def status(self) -> str:
        try:
            return "done" if (self.run_dir / "run.json").is_file() else \
                str(TL.RP.run_status(self.run_dir))
        except OSError:
            return "?"

    def poll(self, *, final: bool = False) -> list[str]:
        """Rendered lines appended since the last poll (on `final`, a parseable last fragment
        too, as the CLI does once the run ended)."""
        raw, self.offset = TL.read_lines(self.stream, self.offset)
        if final:
            frag = TL._fragment(self.stream, self.offset)
            if frag:
                try:
                    import json
                    if isinstance(json.loads(frag), dict):
                        raw.append(frag)
                except ValueError:
                    pass
        out: list[str] = []
        for line in raw:
            out.extend(clean(t.plain) for t in self.renderer.feed_line(line))
        return out

    def summary_lines(self) -> list[str]:
        """For a run without stream capture: its result line, as the CLI shows it."""
        obj = TL.RP.read_json(self.run_dir / "result.json")
        if isinstance(obj, dict):
            return [clean(self.renderer.result_line(obj).plain)]
        return ["no stream.jsonl: this run was launched without stream capture"]
