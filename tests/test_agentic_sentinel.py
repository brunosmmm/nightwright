"""SPEC-0359 §4-5: the full-suite sentinel on the base branch and its `gate_reuse.json` state."""
import datetime as dt
import json
import os
import threading
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from test_agentic_close import _commit, _git_env, _pcfg, world  # noqa: F401
from test_agentic_close_reuse import _declined, rw  # noqa: F401
from nightwright.agentic import close as CL
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import gate_slots
from nightwright.agentic import gitops as git
from nightwright.agentic import project as P
from nightwright.agentic import queue as Q
from nightwright.agentic import sentinel as SN
from nightwright.agentic import store
from nightwright.cli import cli

PROJECT = "demo"
SCFG = {"enabled": True, "after_reuses": 3, "max_age_s": 86400}
NOW = dt.datetime(2026, 11, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp()


def _iso(offset_s):
    return dt.datetime.fromtimestamp(NOW + offset_s, dt.timezone.utc).isoformat()


def _st(**kw):
    return {**SN.empty_state(), **kw}


# ---- due / overdue -------------------------------------------------------------------------

def test_due_by_count_and_age():
    assert not SN.is_due(_st(), SCFG, NOW)
    assert not SN.is_due(_st(reuses_since_green=2, last_green_at=_iso(-60)), SCFG, NOW)
    assert SN.is_due(_st(reuses_since_green=3, last_green_at=_iso(-60)), SCFG, NOW)
    assert SN.is_due(_st(reuses_since_green=1, last_green_at=_iso(-86401)), SCFG, NOW)
    assert not SN.is_due(_st(reuses_since_green=0, last_green_at=_iso(-10 * 86400)), SCFG, NOW)
    # an unknown last_green_at counts as older than max_age_s
    assert SN.is_due(_st(reuses_since_green=1), SCFG, NOW)


def test_overdue_by_due_since_and_by_disabled():
    assert not SN.is_overdue(_st(reuses_since_green=3, due_since=_iso(-3600)), SCFG, NOW)
    assert SN.is_overdue(_st(reuses_since_green=3, due_since=_iso(-86401)), SCFG, NOW)
    off = {**SCFG, "enabled": False}
    assert not SN.is_overdue(_st(reuses_since_green=2), off, NOW)
    assert SN.is_overdue(_st(reuses_since_green=3), off, NOW)


def test_due_since_set_when_due_first_holds_and_cleared_on_green(world):
    cfg, root = world["cfg"], world["root"]
    pcfg = {"sentinel": SCFG}
    SN._write(SN.state_path(cfg, PROJECT), _st(last_green_at=_iso(-60)))
    for i in range(3):
        SN.record_merge(cfg, PROJECT, f"c{i}", f"{i:040x}", root=root, project_cfg=pcfg,
                        now=NOW)
        due_since = SN.read(cfg, PROJECT, project_cfg=pcfg, now=NOW)["due_since"]
        assert (due_since is None) == (i < 2), (i, due_since)
    st = SN.read(cfg, PROJECT, project_cfg=pcfg, now=NOW)
    assert st["due_since"] == SN._iso(NOW)
    sha = git.rev_parse(root)
    SN._write(SN.state_path(cfg, PROJECT), {**st, "suspects": [
        {"campaign": "c0", "merge_sha": sha}]})
    SN.settle(cfg, PROJECT, sha, True, root=root, project_cfg=pcfg, now=NOW + 5)
    st = SN.read(cfg, PROJECT, project_cfg=pcfg)
    assert st["due_since"] is None and st["suspects"] == [] and st["reuses_since_green"] == 0
    assert st["last_green_sha"] == sha and st["force_full_close"] is False


def test_check_due_marks_due_since_and_respects_enabled(world):
    cfg = world["cfg"]
    SN._write(SN.state_path(cfg, PROJECT), _st(reuses_since_green=1, last_green_at=_iso(-60)))
    assert not SN.check_due(cfg, PROJECT, project_cfg={"sentinel": SCFG}, now=NOW)
    assert SN.read(cfg, PROJECT)["due_since"] is None
    assert SN.check_due(cfg, PROJECT, project_cfg={"sentinel": SCFG}, now=NOW + 86400)
    assert SN.read(cfg, PROJECT)["due_since"] == SN._iso(NOW + 86400)
    assert not SN.check_due(cfg, PROJECT, project_cfg={"sentinel": {**SCFG, "enabled": False}},
                            now=NOW + 86400)


# ---- runs ----------------------------------------------------------------------------------

@pytest.fixture
def sw(world):
    """`world` with a close config, `_root` set and escalation notifications off."""
    world["cfg"]["agentic"] = {"escalations": {"notify": "none"}}
    pcfg = _pcfg(world)
    pcfg["_root"] = str(world["root"])
    pcfg["gates"] = [
        {"name": "loop", "cmd": "false", "when": "loop", "required": True, "timeout_s": 30},
        {"name": "full", "cmd": "test ! -f broken.txt", "when": "close", "required": True,
         "timeout_s": 30, "paths": ["nothing/**"]},
    ]
    return {**world, "pcfg": pcfg}


def _sentinel_events(cfg):
    return [e for e in E.read_since(cfg, 0, types=["sentinel"]) if e["project"] == PROJECT]


def _merge_on_main(sw, name):
    return _commit(sw["root"], name, f"{name}\n", f"merge {name}")


def test_green_run_settles_only_ancestors_merge_during_run_survives(sw):
    cfg, root = sw["cfg"], sw["root"]
    first = _merge_on_main(sw, "a.txt")
    SN.record_merge(cfg, PROJECT, "c-a", first, root=root, project_cfg=sw["pcfg"])
    late = {}

    def merge_during_run(x):
        assert x == first
        late["sha"] = _merge_on_main(sw, "b.txt")       # lands after the reset to X
        SN.record_merge(cfg, PROJECT, "c-b", late["sha"], root=root, project_cfg=sw["pcfg"])

    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"], _hooks={"after_reset": merge_during_run})
    assert out["ok"] and out["sha"] == first and not out["interrupted"]
    assert [r["name"] for r in out["results"]] == ["full"], "close gates only, no paths skip"
    assert out["suspects"] == [{"campaign": "c-a", "merge_sha": first}]
    st = SN.read(cfg, PROJECT)
    assert st["suspects"] == [{"campaign": "c-b", "merge_sha": late["sha"]}]
    assert st["reuses_since_green"] == 1
    assert st["last_green_sha"] == first and st["force_full_close"] is False
    assert st["due_since"] is None, "one survivor after a fresh green is not due yet"
    (ev,) = _sentinel_events(cfg)
    assert ev["data"] == {"project": PROJECT, "ok": True, "sha": first,
                          "suspects": [{"campaign": "c-a", "merge_sha": first}]}
    wt = SN.worktree_path(cfg, PROJECT)
    assert git.rev_parse(wt) == first and git.current_branch(wt) is None


def test_red_run_flags_escalates_names_only_ancestors(sw):
    cfg, root = sw["cfg"], sw["root"]
    first = _merge_on_main(sw, "broken.txt")
    SN.record_merge(cfg, PROJECT, "c-a", first, root=root, project_cfg=sw["pcfg"])
    late = {}

    def merge_during_run(x):
        late["sha"] = _merge_on_main(sw, "b.txt")
        SN.record_merge(cfg, PROJECT, "c-b", late["sha"], root=root, project_cfg=sw["pcfg"])

    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"], _hooks={"after_reset": merge_during_run})
    assert not out["ok"] and out["failing"] == ["full"]
    assert out["suspects"] == [{"campaign": "c-a", "merge_sha": first}]
    st = SN.read(cfg, PROJECT)
    assert st["force_full_close"] is True
    assert [s["campaign"] for s in st["suspects"]] == ["c-a", "c-b"], "unchanged on red"
    (ev,) = _sentinel_events(cfg)
    assert ev["data"]["ok"] is False and ev["data"]["failing"] == ["full"]
    assert ev["data"]["suspects"] == [{"campaign": "c-a", "merge_sha": first}]
    (esc,) = ESC.list_all(cfg, "open")
    assert "c-a" in esc["question"] and "c-b" not in esc["question"]
    assert esc["project"] == PROJECT and esc["campaign"] is None
    assert any(first in f for f in esc["facts"])
    assert "force_full_close" in SN.reuse_blocker(cfg, PROJECT)


def test_red_sentinel_makes_the_next_close_decline_with_r1(rw):
    rw["cfg"]["agentic"] = {"escalations": {"notify": "none"}}
    SN.settle(rw["cfg"], PROJECT, git.rev_parse(rw["root"]), False, ["t::x"],
              root=str(rw["root"]))
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    rule, detail = _declined(out)
    assert rule == "R1" and "force_full_close" in detail and rw["marker"].exists()


def test_deleted_after_red_restores_force_full_close(rw):
    rw["cfg"]["agentic"] = {"escalations": {"notify": "none"}}
    SN.settle(rw["cfg"], PROJECT, git.rev_parse(rw["root"]), False, ["t::x"],
              root=str(rw["root"]))
    SN.state_path(rw["cfg"], PROJECT).unlink()
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    assert _declined(out)[0] == "R1"
    assert json.loads(SN.state_path(rw["cfg"], PROJECT).read_text())["force_full_close"] is True


def test_deleted_without_sentinel_history_rebuilt_from_events(world):
    cfg = world["cfg"]
    cid = world["state"]["id"]
    store.write_state(cfg, cid, dict(world["state"], state="MERGED"))
    store.append_event(cfg, cid, "close", {"mode": "merge", "merged_sha": "d" * 40,
                                           "reused": ["pytest"]})
    store.append_event(cfg, cid, "close", {"mode": "merge", "merged_sha": "e" * 40})
    st = SN.read(cfg, PROJECT, root=world["root"])
    assert st["suspects"] == [{"campaign": cid, "merge_sha": "d" * 40}]
    assert st["reuses_since_green"] == 1 and st["last_green_at"] is None
    assert st["force_full_close"] is True, "reused merges but no sentinel ever ran"
    assert SN.is_due(st, SCFG) and st["due_since"] is not None
    assert SN.state_path(cfg, PROJECT).is_file()


def test_rebuild_after_green_drops_contained_merges(sw):
    cfg, root = sw["cfg"], str(sw["root"])
    first = _merge_on_main(sw, "a.txt")
    cid = sw["state"]["id"]
    store.write_state(cfg, cid, dict(sw["state"], state="MERGED"))
    store.append_event(cfg, cid, "close", {"mode": "merge", "merged_sha": first,
                                           "reused": ["pytest"]})
    SN.settle(cfg, PROJECT, first, True, root=root)
    SN.state_path(cfg, PROJECT).unlink()
    st = SN.read(cfg, PROJECT, root=root)
    assert st["suspects"] == [] and st["force_full_close"] is False
    assert st["last_green_sha"] == first


def test_corrupt_file_declines_until_a_sentinel_rewrites_it(sw):
    cfg = sw["cfg"]
    path = SN.state_path(cfg, PROJECT)
    path.parent.mkdir(parents=True)
    path.write_text("{nope")
    assert SN.reuse_blocker(cfg, PROJECT) == SN.UNREADABLE
    assert SN.record_merge(cfg, PROJECT, "c", "f" * 40) is None
    assert path.read_text() == "{nope", "left in place"
    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"])
    assert out["ok"]
    assert SN.reuse_blocker(cfg, PROJECT) is None
    assert json.loads(path.read_text())["last_green_sha"] == out["sha"]


def test_interrupted_run_settles_nothing(sw):
    cfg = sw["cfg"]
    stop = threading.Event()
    stop.set()
    out = SN.run(cfg, PROJECT, project_cfg=sw["pcfg"], stop=stop)
    assert out["interrupted"] and _sentinel_events(cfg) == []


# ---- scheduling ----------------------------------------------------------------------------

def _due(cfg):
    SN._write(SN.state_path(cfg, PROJECT), _st(reuses_since_green=3))


@pytest.fixture
def sched(sw, monkeypatch):
    started = []
    monkeypatch.setattr(P, "project_config", lambda cfg, p: sw["pcfg"])
    monkeypatch.setattr(SN, "run", lambda cfg, p, **kw: started.append(p) or {"ok": True})
    ctx = SimpleNamespace(cfg=sw["cfg"], stop=threading.Event())
    return sw, ctx, started


def test_scheduled_when_due_and_no_slot_waiter(sched):
    sw, ctx, started = sched
    pool = Q.StepPool()
    assert Q._sentinel_step(ctx, pool, []) is None          # nothing recorded: not due
    _due(sw["cfg"])
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    pool.sentinel[1].result()
    assert started == [PROJECT]
    assert Q._sentinel_step(ctx, pool, []) == PROJECT        # harvested, still due: again
    pool.shutdown()


def test_not_scheduled_while_a_campaign_waits_for_a_slot(sched):
    sw, ctx, started = sched
    _due(sw["cfg"])
    marker = gate_slots.slots_dir(sw["cfg"]) / "waiting-X.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"campaign": "X", "pid": os.getpid(), "since": "now"}))
    pool = Q.StepPool()
    assert Q._sentinel_step(ctx, pool, []) is None
    marker.unlink()
    assert Q._sentinel_step(ctx, pool, [{"id": "G1", "state": "GATING"}]) is None
    assert Q._sentinel_step(ctx, pool, []) == PROJECT
    pool.sentinel[1].result()
    pool.shutdown()


def test_not_scheduled_when_disabled(sched):
    sw, ctx, started = sched
    sw["pcfg"]["sentinel"] = {**SCFG, "enabled": False}
    _due(sw["cfg"])
    assert Q._sentinel_step(ctx, Q.StepPool(), []) is None


# ---- CLI -----------------------------------------------------------------------------------

def _cli(cfg, monkeypatch, args):
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return CliRunner().invoke(cli, args)


def test_cli_run_and_clear(sw, monkeypatch):
    cfg = sw["cfg"]
    monkeypatch.setattr(P, "project_config", lambda c, p: sw["pcfg"])
    _merge_on_main(sw, "broken.txt")
    r = _cli(cfg, monkeypatch, ["queue", "sentinel", "run", "--project", PROJECT, "--json"])
    assert r.exit_code == 1, r.output
    out = json.loads(r.output)
    assert out["schema"] == "nw.queue.sentinel.v1" and out["ok"] is False
    assert out["state"]["force_full_close"] is True
    r = _cli(cfg, monkeypatch, ["queue", "sentinel", "clear", "--project", PROJECT])
    assert r.exit_code == 0 and "cleared" in r.output
    assert SN.read(cfg, PROJECT)["force_full_close"] is False
    git.git(sw["root"], "rm", "-q", "broken.txt")
    git.git(sw["root"], "commit", "-q", "-m", "fix")
    r = _cli(cfg, monkeypatch, ["queue", "sentinel", "run", "--project", PROJECT])
    assert r.exit_code == 0 and "green" in r.output, r.output


def test_drain_sees_a_running_sentinel():
    pool = Q.StepPool()
    assert not Q._sentinel_busy(pool)
    gate = threading.Event()
    pool.sentinel = (PROJECT, pool.submit(gate.wait))
    assert Q._sentinel_busy(pool)
    gate.set()
    pool.sentinel[1].result()
    assert not Q._sentinel_busy(pool)
    pool.shutdown()
