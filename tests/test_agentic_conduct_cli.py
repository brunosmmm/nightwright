"""SPEC-0233: the conductor surfaces (`wt queue conduct-status`, `wt queue events`, the ping
digest) show a parked merge campaign as `merge:<branch>`, never as a null spec id."""
import json

from test_agentic_queue_merge import (branch_wt, queue_merge, run_cli, run_until_idle, world,  # noqa: F401
                                      _git_env)  # noqa: F401
from wt.agentic import events as E
from wt.agentic import store as S


def parked_merge(world, monkeypatch):
    repo, cfg, tmp = world
    wt = branch_wt(tmp, repo)
    cid = queue_merge(cfg, monkeypatch)
    r = run_cli(cfg, monkeypatch, ["queue", "tick", "--json"])
    assert r.exit_code == 0, r.output
    (wt / "scratch.txt").write_text("x\n")
    run_until_idle(cfg, monkeypatch)
    assert S.read_state(cfg, cid)["park"]["reason"] == "worktree_dirty"
    return cfg, cid


def test_conduct_status_parked_table_shows_the_merge_label(world, monkeypatch):
    cfg, cid = parked_merge(world, monkeypatch)
    monkeypatch.setattr("wt.console.console._width", 200)
    r = run_cli(cfg, monkeypatch, ["queue", "conduct-status", "--json"])
    st = json.loads(r.output)
    assert st["parked"][0]["label"] == "merge:fix/typo"
    r = run_cli(cfg, monkeypatch, ["queue", "conduct-status"])
    assert r.exit_code == 0, r.output
    assert "merge:fix/typo" in r.output and "None" not in r.output


def test_events_and_digest_rows_carry_the_label(world, monkeypatch):
    cfg, cid = parked_merge(world, monkeypatch)
    E.sync(cfg)
    parked = [e for e in E.read_since(cfg, 0) if e["campaign"] == cid and e["type"] == "parked"]
    assert parked and parked[0]["spec_id"] is None and parked[0]["label"] == "merge:fix/typo"
    d = E.ping_digest(cfg)
    assert d["parked"][0]["label"] == "merge:fix/typo"
