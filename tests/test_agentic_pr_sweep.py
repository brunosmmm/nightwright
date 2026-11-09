"""SPEC-0283: `sweep_handed_off` / `nw queue sweep-prs` and the follower's periodic sweep, with an
injected `view` (nothing calls the real gh)."""
import datetime as dt
import json

import pytest
from click.testing import CliRunner

from test_agentic_events import _campaign, _cfg, _set_state
from test_agentic_queue import _merge_with_idea, opened  # noqa: F401 — fixture
from nightwright.agentic import events as E
from nightwright.agentic import queue as Q
from nightwright.agentic import schemas as SC
from nightwright.agentic import store as S
from nightwright.agentic.gitops import GitError
from nightwright.cli import cli

pytestmark = pytest.mark.fast

MERGED = {"state": "MERGED", "merged_at": "2026-10-27T10:00:00Z", "merge_sha": "abc123"}
CLOSED = {"state": "CLOSED", "merged_at": None, "merge_sha": None}
OPEN = {"state": "OPEN", "merged_at": None, "merge_sha": None}


def _handed(cfg, n=1, *, created=None, **close):
    """`n` HANDED_OFF campaigns, each with its own PR url; returns their ids."""
    ids = []
    for i in range(n):
        cid = f"SPEC-09{i:02d}-20261025-aaaa"
        _campaign(cfg, cid, state="HANDED_OFF")
        _set_state(cfg, cid, created_at=created or f"2026-10-25T10:{i:02d}:00+00:00",
                   close={"mode": "pr", "merged_sha": None, "pr_url": f"https://gh/p/{i}", **close})
        ids.append(cid)
    return ids


def _view(table, calls=None):
    def view(url):
        if calls is not None:
            calls.append(url)
        r = table if isinstance(table, Exception) or "state" in table else table[url]
        if isinstance(r, Exception):
            raise r
        return r
    return view


def _snap(cfg, cid):
    return S.read_state(cfg, cid), S.read_ledger(cfg, cid)


def test_sweep_dry_run_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    a, b = _handed(cfg, 2)
    before = [_snap(cfg, c) for c in (a, b)]
    rows = Q.sweep_handed_off(cfg, view=_view({"https://gh/p/0": MERGED, "https://gh/p/1": CLOSED}))
    assert sorted((r["campaign"], r["action"]) for r in rows) == [(a, "merge"), (b, "closed")]
    assert [_snap(cfg, c) for c in (a, b)] == before


def test_sweep_merged_transitions_and_reconciles(tmp_path):
    cfg = _cfg(tmp_path)
    (cid,) = _handed(cfg)
    (row,) = Q.sweep_handed_off(cfg, apply=True, view=_view(MERGED))
    assert row["action"] == "merge" and row["pr_state"] == "MERGED"
    st = S.read_state(cfg, cid)
    assert st["state"] == "MERGED" and st["close"]["merged_sha"] == "abc123"
    assert st["close"]["pr_state"] == "MERGED"
    (tr,) = S.read_ledger(cfg, cid, types=["transition"])
    assert (tr["from"], tr["to"], tr["reason"], tr["by"]) == ("HANDED_OFF", "MERGED", "pr_merged", "sweep")
    assert all(SC.ledger_problems(e) == [] for e in S.read_ledger(cfg, cid))
    assert "merged" in [e["type"] for e in E.sync(cfg)]


def test_sweep_merged_writes_idea_reconciled(opened):  # noqa: F811
    cfg, cid, _ctx = opened
    iid, _oc = _merge_with_idea(opened)
    st = S.read_state(cfg, cid)
    S.write_state(cfg, cid, dict(st, state="HANDED_OFF",
                                 close={"mode": "pr", "merged_sha": None, "pr_url": "https://gh/p/9"}))
    (row,) = Q.sweep_handed_off(cfg, apply=True, view=_view(MERGED))
    assert row["action"] == "merge" and S.read_state(cfg, cid)["state"] == "MERGED"
    (ev,) = S.read_ledger(cfg, cid, types=["idea_reconciled"])
    assert (ev["spec_id"], ev["idea_id"]) == ("SPEC-0001", iid)


def test_sweep_skips_state_changed_under_lock(tmp_path):
    cfg = _cfg(tmp_path)
    (cid,) = _handed(cfg)

    def view(url):
        _set_state(cfg, cid, state="ABANDONED")
        return MERGED
    (row,) = Q.sweep_handed_off(cfg, apply=True, view=view)
    assert row["action"] == "skipped" and "ABANDONED" in row["detail"]
    st = S.read_state(cfg, cid)
    assert st["state"] == "ABANDONED" and st["close"]["merged_sha"] is None
    assert not S.read_ledger(cfg, cid, types=["transition"])


def test_sweep_closed_finding_once(tmp_path):
    cfg = _cfg(tmp_path)
    (cid,) = _handed(cfg)
    for _ in range(2):
        Q.sweep_handed_off(cfg, apply=True, view=_view(CLOSED))
    (f,) = S.read_ledger(cfg, cid, types=["finding"])
    assert f["severity"] == "major" and f["title"] == "PR closed without merging"
    assert f["detail"] == "https://gh/p/0" and SC.ledger_problems(f) == []
    assert S.read_state(cfg, cid)["state"] == "HANDED_OFF"
    assert S.read_state(cfg, cid)["close"]["pr_state"] == "CLOSED"


def test_sweep_closed_finding_once_interleaved(tmp_path):
    cfg = _cfg(tmp_path)
    (cid,) = _handed(cfg)
    inner = []

    def view(url):
        if not inner:                      # the second sweep runs from inside the first's view
            inner.append(Q.sweep_handed_off(cfg, apply=True, view=_view(CLOSED)))
        return CLOSED
    (row,) = Q.sweep_handed_off(cfg, apply=True, view=view)
    assert inner[0][0]["action"] == "closed" and row["action"] == "skipped"
    assert len(S.read_ledger(cfg, cid, types=["finding"])) == 1


def test_sweep_error_isolated(tmp_path):
    cfg = _cfg(tmp_path)
    a, b = _handed(cfg, 2)
    table = {"https://gh/p/0": GitError(["gh"], 1, "boom"), "https://gh/p/1": MERGED}
    rows = {r["campaign"]: r for r in Q.sweep_handed_off(cfg, apply=True, view=_view(table))}
    assert rows[a]["action"] == "error" and rows[a]["pr_state"] is None and "boom" in rows[a]["detail"]
    assert rows[b]["action"] == "merge" and S.read_state(cfg, b)["state"] == "MERGED"
    st = S.read_state(cfg, a)
    assert st["state"] == "HANDED_OFF" and st["close"]["pr_checked_at"]


def test_sweep_budget_stops(tmp_path):
    cfg = _cfg(tmp_path)
    _handed(cfg, 3)
    ticks = iter([0, 0, 100, 100, 100])
    calls = []
    rows = Q.sweep_handed_off(cfg, view=_view(OPEN, calls), budget_s=60, clock=lambda: next(ticks))
    assert len(rows) == 1 and len(calls) == 1


def test_sweep_fair_selection(tmp_path):
    cfg = _cfg(tmp_path)
    ids = _handed(cfg, 8)
    seen = []
    for _ in range(2):
        rows = Q.sweep_handed_off(cfg, apply=True, limit=5, view=_view(OPEN, seen))
        assert len(rows) == 5
    assert len(set(seen)) == 8
    # and the least recently checked goes next: the sweep after that restarts at the oldest stamp
    assert {r["campaign"] for r in Q.sweep_handed_off(cfg, limit=5, view=_view(OPEN))} <= set(ids)


def test_sweep_leaves_closed_pr_alone(tmp_path):
    cfg = _cfg(tmp_path)
    _handed(cfg, 1, pr_state="CLOSED")
    assert Q.sweep_handed_off(cfg, apply=True, view=_view(OPEN)) == []


def test_cli_sweep_prs(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg["org_files"] = []
    (cid,) = _handed(cfg)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    monkeypatch.setattr("nightwright.agentic.gitops.gh_pr_view", lambda url: MERGED)
    r = CliRunner().invoke(cli, ["queue", "sweep-prs", "--json"])
    assert r.exit_code == 0, r.output
    (row,) = json.loads(r.output)
    assert set(row) == {"campaign", "pr_url", "pr_state", "action", "detail"} and row["action"] == "merge"
    assert S.read_state(cfg, cid)["state"] == "HANDED_OFF"
    r = CliRunner().invoke(cli, ["queue", "sweep-prs", "--yes", "--json"])
    assert r.exit_code == 0 and S.read_state(cfg, cid)["state"] == "MERGED"


# ---- follower ------------------------------------------------------------------------------

T0 = dt.datetime(2026, 10, 27, 12, 0, 0).astimezone()


@pytest.fixture
def swept(monkeypatch):
    calls = []
    monkeypatch.setattr(Q, "sweep_handed_off", lambda cfg, **kw: calls.append(kw))
    return calls


def test_sync_ping_runs_sweep_on_interval(tmp_path, swept):
    cfg = _cfg(tmp_path)
    E.sync(cfg, now=T0, ping=True)
    E.sync(cfg, now=T0 + dt.timedelta(seconds=60), ping=True)      # a second follower
    E.sync(cfg, now=T0 + dt.timedelta(seconds=899), ping=True)
    assert swept == [{"apply": True}]
    E.sync(cfg, now=T0 + dt.timedelta(seconds=900), ping=True)
    assert len(swept) == 2
    E.sync(cfg, now=T0 + dt.timedelta(seconds=5000))               # no ping: no sweep
    assert len(swept) == 2


def test_sync_ping_sweep_disabled_and_default(tmp_path, swept):
    cfg = _cfg(tmp_path)
    cfg["agentic"]["pr_sweep"] = {"interval_s": 0}
    E.sync(cfg, now=T0, ping=True)
    assert swept == []
    cfg["agentic"].pop("pr_sweep")                     # a user `agentic:` block without pr_sweep
    E.sync(cfg, now=T0, ping=True)
    E.sync(cfg, now=T0 + dt.timedelta(seconds=899), ping=True)
    assert len(swept) == 1
    E.sync(cfg, now=T0 + dt.timedelta(seconds=900), ping=True)
    assert len(swept) == 2


def test_sync_ping_sweep_failure_does_not_break_sync(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    def boom(cfg, **kw):
        raise RuntimeError("gh exploded")
    monkeypatch.setattr(Q, "sweep_handed_off", boom)
    assert isinstance(E.sync(cfg, now=T0, ping=True), list)

