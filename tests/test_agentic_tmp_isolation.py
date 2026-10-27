"""SPEC-0253: per-child TMPDIR for gates and persona runs, its cleanup, and the temp-filesystem
admission guard. Fake runner and harness as in `test_agentic_queue`; `os.statvfs` is
monkeypatched for the pressure cases."""
import os
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_agentic_e2e import added, impl_done, make_cfg, make_repo, script, write_review
from wt.agentic import events as E
from wt.agentic import gates as G
from wt.agentic import personas as PS
from wt.agentic import queue as Q
from wt.agentic import store as S
from wt.agentic.profiles import resolve_profile
from wt.agentic.runner import RunResult

CID = "SPEC-0900-20261026-aaaa"
_GATE = {"ok": True, "started_at": "2026-10-26T10:00:00+00:00", "duration_s": 1.0, "results": []}


def _cfg(tmp_path, **events):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config"),
            "agentic": {"lease_ttl_s": 120, "events": events}, "outbox_targets": {},
            "specs_dir": str(tmp_path)}


def _campaign(cfg, cid=CID, state="IMPLEMENTING", worktree=None):
    S.create_campaign(cfg, {"id": cid, "spec_id": cid[:9], "project": "Meta-Tools",
                            "profile": "balanced", "state": state,
                            "repo": {"base": "main", "worktree": str(worktree or "")},
                            "cost": {"total_usd": 0.0, "cap_usd": 10.0}})
    return cid


def _vfs(free_mb=10_000, files=1_000_000, ffree=900_000):
    return lambda p: SimpleNamespace(f_bavail=free_mb * 1024, f_frsize=1024, f_files=files,
                                     f_ffree=ffree)


@pytest.fixture
def shared_dev(tmp_path, monkeypatch):
    """$TMPDIR on the same filesystem as the data dir, so one statvfs covers both."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


# ---- TMPDIR for gates ---------------------------------------------------------------------

def test_gate_env_sets_tmpdir_under_campaign_dir(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    env = G.gate_env(cfg, S.read_state(cfg, cid), 7)
    want = str(S.campaign_dir(cfg, cid) / "tmp" / "gate-007")
    assert env["TMPDIR"] == env["TEMP"] == env["TMP"] == want
    assert "TMPDIR" not in G.gate_env(cfg, S.read_state(cfg, cid))      # no seq, no private dir


def test_run_all_gives_gate_its_tmpdir_and_removes_it(tmp_path):
    cfg = _cfg(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    cid = _campaign(cfg, worktree=wt)
    state = S.read_state(cfg, cid)
    pcfg = {"gates": [{"name": "t", "cmd": 'touch "$TMPDIR/x" && echo $TMPDIR && test -f "$TMPDIR/x"',
                       "timeout_s": 30}, {"name": "bad", "cmd": 'touch "$TMPDIR/y"; exit 1',
                                          "timeout_s": 30, "required": False}]}
    run = G.run_all(pcfg, state, cfg, seq=3)
    assert run.ok is True
    child = S.campaign_dir(cfg, cid) / "tmp" / "gate-003"
    assert str(child) in (S.campaign_dir(cfg, cid) / run.results[0].log).read_text()
    assert not child.exists()                                          # also after a failing gate


def test_run_all_removes_tmpdir_when_a_gate_raises(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    cid = _campaign(cfg, worktree=wt)
    seen = {}

    def boom(gate, worktree, env, *a, **kw):
        Path(env["TMPDIR"], "f").write_text("x")
        seen["dir"] = Path(env["TMPDIR"])
        raise RuntimeError("boom")

    monkeypatch.setattr(G, "run_gate", boom)
    with pytest.raises(RuntimeError):
        G.run_all({"gates": [{"name": "t", "cmd": "true"}]}, S.read_state(cfg, cid), cfg, seq=1)
    assert not seen["dir"].exists()


def test_concurrent_gate_runs_with_the_same_seq_get_distinct_tmpdirs(tmp_path):
    """`next_gate_seq` can hand the same seq to a runner gate and a human `wt gate run`; the
    second `run_all` must move on instead of sharing (and wiping) the first one's TMPDIR."""
    cfg = _cfg(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    cid = _campaign(cfg, worktree=wt)
    sibling = S.child_tmp(cfg, cid, "gate-001", create=True)       # a live run holding seq 1
    (sibling / "live").write_text("x")
    pcfg = {"gates": [{"name": "t", "cmd": 'echo $TMPDIR; touch "$TMPDIR/x"', "timeout_s": 30}]}
    run = G.run_all(pcfg, S.read_state(cfg, cid), cfg, seq=1)
    assert run.seq == 2
    log = S.campaign_dir(cfg, cid) / run.results[0].log
    assert log.name == "002-t.log" and str(S.child_tmp(cfg, cid, "gate-002")) in log.read_text()
    assert (sibling / "live").is_file() and not S.child_tmp(cfg, cid, "gate-002").exists()


# ---- TMPDIR for persona runs ----------------------------------------------------------------

def _request(cfg, cid, persona, run_id):
    state = S.read_state(cfg, cid)
    profile = resolve_profile(cfg, "balanced")
    return PS.build_request(cfg, profile, {}, state, persona,
                            run_dir=S.runs_dir(cfg, cid) / run_id)


def _end(cfg, cid, run_id, persona="implementer", **over):
    """Record a finished run the way the queue does (`_record_run` → ledger)."""
    rr = RunResult(**{"exit_code": 0, "timed_out": False, "interrupted": False,
                      "duration_s": 1.0, "is_error": False, "cost_usd": 0.1, **over})
    for etype, payload in Q._record_run(S.read_state(cfg, cid), persona, run_id, rr, 1.0):
        S.append_event(cfg, cid, etype, payload)


def _start(cfg, cid, run_id, persona="implementer"):
    S.append_event(cfg, cid, "run.start", {
        "persona": persona, "run_id": run_id, "model": "m", "effort": None, "max_budget_usd": 1.0,
        "timeout_s": 60, "permission_mode": "x", "prompt_sha256": "0"})


def test_persona_request_env_has_private_tmpdir_removed_on_run_end(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    req = _request(cfg, cid, "implementer", "001-implementer")
    want = S.campaign_dir(cfg, cid) / "tmp" / "run-001-implementer"
    assert req.env["TMPDIR"] == req.env["TEMP"] == req.env["TMP"] == str(want)
    assert want.is_dir()
    (want / "f").write_text("x")
    _end(cfg, cid, "001-implementer")
    assert not want.exists()


def test_concurrent_reviewers_keep_their_own_tmp_until_they_finish(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, state="REVIEWING")
    a = _request(cfg, cid, "conformance_reviewer", "002-conformance_reviewer")
    b = _request(cfg, cid, "test_reviewer", "002-test_reviewer")
    assert a.env["TMPDIR"] != b.env["TMPDIR"]
    for r in (a, b):
        Path(r.env["TMPDIR"], "mine").write_text("x")
    _end(cfg, cid, "002-conformance_reviewer", "conformance_reviewer")
    assert not Path(a.env["TMPDIR"]).exists()
    assert (Path(b.env["TMPDIR"]) / "mine").is_file()                  # sibling untouched
    _end(cfg, cid, "002-test_reviewer", "test_reviewer")
    assert not Path(b.env["TMPDIR"]).exists()


def test_error_run_result_also_clears_tmp(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    req = _request(cfg, cid, "implementer", "001-implementer")
    _end(cfg, cid, "001-implementer", is_error=True)
    assert not Path(req.env["TMPDIR"]).exists()


def test_fake_runner_sees_tmpdir_and_it_is_gone_after_the_tick(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    entry = impl_done(shell=['echo "$TMPDIR" > ../seen.txt; touch "$TMPDIR/f"'])
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [entry]))
    cid = added(cfg, monkeypatch)
    Q.tick(cfg)
    Q.tick(cfg)
    seen = Path(repo.parent / "seen.txt")
    if not seen.exists():                               # worktree layout differs: find it
        seen = next(tmp_path.rglob("seen.txt"))
    assert S.campaign_dir(cfg, cid) / "tmp" / "run-001-implementer" == Path(seen.read_text().strip())
    assert not (S.campaign_dir(cfg, cid) / "tmp" / "run-001-implementer").exists()


# ---- removal paths ------------------------------------------------------------------------

@pytest.mark.parametrize("to", ["MERGED", "ABANDONED"])
def test_terminal_transition_removes_campaign_tmp(tmp_path, to):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    child = S.child_tmp(cfg, cid, "run-009-x", create=True)
    (child / "f").write_text("x")
    Q._transition(cfg, S.read_state(cfg, cid), to, "test")
    assert not S.campaign_tmp(cfg, cid).exists()
    assert (S.campaign_dir(cfg, cid) / "state.json").is_file()


def test_non_terminal_transition_keeps_tmp(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    child = S.child_tmp(cfg, cid, "run-009-x", create=True)
    Q._transition(cfg, S.read_state(cfg, cid), "GATING", "test")
    assert child.is_dir()


def test_sweep_removes_only_recorded_finished_owners(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    for owner in ("gate-001", "gate-002", "run-001-implementer", "run-002-implementer",
                  "run-003-unknown", "odd"):
        S.child_tmp(cfg, cid, owner, create=True)
    S.append_event(cfg, cid, "gate", _GATE | {"gate_seq": 1})
    _start(cfg, cid, "001-implementer")
    _start(cfg, cid, "002-implementer")
    # a run.end clears its own dir; re-create it to look like a killed runner's residue
    _end(cfg, cid, "001-implementer")
    S.child_tmp(cfg, cid, "run-001-implementer", create=True)
    removed = S.sweep_campaign_tmp(cfg, cid)
    assert sorted(removed) == ["gate-001", "run-001-implementer"]
    left = sorted(p.name for p in S.campaign_tmp(cfg, cid).iterdir())
    assert left == ["gate-002", "odd", "run-002-implementer", "run-003-unknown"]


def test_tick_sweeps_residue(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg, state="PARKED")
    S.append_event(cfg, cid, "gate", _GATE | {"gate_seq": 4})
    child = S.child_tmp(cfg, cid, "gate-004", create=True)
    live = S.child_tmp(cfg, cid, "gate-005", create=True)
    monkeypatch.setattr(Q, "events_temp_pressure", lambda cfg: None)
    Q.tick(cfg, runner=SimpleNamespace(detachable=False))
    assert not child.exists() and live.is_dir()


@pytest.mark.parametrize("owner", ["", ".", "..", "../..", "/etc", "a/b", None])
def test_clear_child_tmp_refuses_bad_owner(tmp_path, owner):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    S.append_event(cfg, cid, "note", {"text": "x", "author": "t"})
    with pytest.raises(ValueError):
        S.clear_child_tmp(cfg, cid, owner)
    cdir = S.campaign_dir(cfg, cid)
    assert (cdir / "state.json").is_file() and (cdir / "ledger.jsonl").is_file()


def test_clear_child_tmp_refuses_symlink_escape(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    root = S.campaign_tmp(cfg, cid)
    root.mkdir()
    (root / "run-evil").symlink_to(outside)
    S.clear_child_tmp(cfg, cid, "run-evil")                 # removes the link, never its target
    assert (outside / "keep").is_file() and not (root / "run-evil").exists()


def test_clear_child_tmp_refuses_a_symlinked_campaign_tmp(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    outside = tmp_path / "outside"
    (outside / "run-001-implementer").mkdir(parents=True)
    (outside / "run-001-implementer" / "keep").write_text("x")
    S.campaign_tmp(cfg, cid).symlink_to(outside)
    with pytest.raises(ValueError):
        S.clear_child_tmp(cfg, cid, "run-001-implementer")
    with pytest.raises(ValueError):
        S.child_tmp(cfg, cid, "run-002-implementer", create=True)
    assert S.sweep_campaign_tmp(cfg, cid) == []
    assert (outside / "run-001-implementer" / "keep").is_file()
    assert not (outside / "run-002-implementer").exists()
    _end(cfg, cid, "001-implementer")                                # run.end hook: same guard
    assert (outside / "run-001-implementer" / "keep").is_file()


def test_clear_campaign_tmp_refuses_empty_campaign_id(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    with pytest.raises(ValueError):
        S.clear_campaign_tmp(cfg, "")
    assert (S.campaign_dir(cfg, cid) / "state.json").is_file()


def test_clear_campaign_tmp_unlinks_symlink_and_keeps_target(tmp_path):
    cfg = _cfg(tmp_path)
    cid = _campaign(cfg)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    link = S.campaign_dir(cfg, cid) / "tmp"
    link.symlink_to(outside)
    S.clear_campaign_tmp(cfg, cid)
    assert (outside / "keep").is_file() and not link.exists() and not link.is_symlink()


# ---- temp_pressure -------------------------------------------------------------------------

def test_temp_pressure_free_space_then_inodes_then_none(tmp_path, monkeypatch, shared_dev):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(os, "statvfs", _vfs(free_mb=100))
    p = E.temp_pressure(cfg)
    assert p["path"] == str(tmp_path) and p["free_mb"] == 100
    assert p["thresholds"] == {"tmp_min_free_mb": 512, "tmp_min_free_inodes_pct": 10}
    monkeypatch.setattr(os, "statvfs", _vfs(ffree=50_000))           # 5% < 10%
    p = E.temp_pressure(cfg)
    assert p["free_inodes"] == 50_000 and p["free_inodes_pct"] == 5.0
    monkeypatch.setattr(os, "statvfs", _vfs(files=0, ffree=0))        # btrfs-style: no inode cap
    assert E.temp_pressure(cfg) is None
    monkeypatch.setattr(os, "statvfs", _vfs())
    assert E.temp_pressure(cfg) is None


def test_temp_pressure_statvfs_error_is_no_pressure(tmp_path, monkeypatch, shared_dev):
    def bad(p):
        raise OSError("nope")
    monkeypatch.setattr(os, "statvfs", bad)
    assert E.temp_pressure(_cfg(tmp_path)) is None


def test_temp_pressure_does_not_probe_gettempdir_when_tmp_is_full(tmp_path, monkeypatch):
    """gettempdir() falls back to /var/tmp (or raises) on ENOSPC; /tmp must still be checked."""
    def broken():
        raise FileNotFoundError("No usable temporary directory found")
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(tempfile, "gettempdir", broken)
    monkeypatch.delenv("TMPDIR", raising=False)
    seen = []

    def vfs(p):
        seen.append(str(p))
        return _vfs(free_mb=1)(p) if str(p) == "/tmp" else _vfs()(p)
    monkeypatch.setattr(os, "statvfs", vfs)
    p = E.temp_pressure(_cfg(tmp_path))
    assert p is not None and p["path"] == "/tmp" and seen[0] == "/tmp"


def test_temp_pressure_checks_a_shared_filesystem_once(tmp_path, monkeypatch, shared_dev):
    calls = []
    monkeypatch.setattr(os, "statvfs", lambda p: calls.append(str(p)) or _vfs()(p))
    E.temp_pressure(_cfg(tmp_path))
    assert len(calls) == 1


def test_temp_pressure_checks_the_agentic_dir_on_another_device(tmp_path, monkeypatch, shared_dev):
    cfg = _cfg(tmp_path)
    agentic = str(S.agentic_dir(cfg))
    real = os.stat

    def fake_stat(p, *a, **kw):
        r = real(p, *a, **kw)
        if str(p).startswith(agentic) or Path(p) == Path(agentic):
            return SimpleNamespace(st_dev=r.st_dev + 1)
        return r

    monkeypatch.setattr(os, "stat", fake_stat)
    S.agentic_dir(cfg).mkdir(parents=True)
    monkeypatch.setattr(os, "statvfs", lambda p: _vfs(free_mb=1)(p) if str(p) == agentic
                        else _vfs()(p))
    assert E.temp_pressure(cfg)["path"] == agentic


@pytest.mark.parametrize("bad", [-1, True, float("nan"), "x"])
def test_settings_fall_back_on_bad_thresholds(tmp_path, bad):
    s = E.settings(_cfg(tmp_path, tmp_min_free_inodes_pct=bad, tmp_min_free_mb=bad))
    assert s["tmp_min_free_inodes_pct"] == 10 and s["tmp_min_free_mb"] == 512


def test_inode_percent_above_100_falls_back(tmp_path):
    s = E.settings(_cfg(tmp_path, tmp_min_free_inodes_pct=150, tmp_min_free_mb=150))
    assert s["tmp_min_free_inodes_pct"] == 10 and s["tmp_min_free_mb"] == 150
    s = E.settings(_cfg(tmp_path, tmp_min_free_inodes_pct=100, tmp_min_free_mb=2048))
    assert s["tmp_min_free_inodes_pct"] == 100 and s["tmp_min_free_mb"] == 2048


def test_temp_pressure_compares_against_the_configured_thresholds(tmp_path, monkeypatch,
                                                                  shared_dev):
    monkeypatch.setattr(os, "statvfs", _vfs(free_mb=100))
    assert E.temp_pressure(_cfg(tmp_path, tmp_min_free_mb=50)) is None       # below default 512
    p = E.temp_pressure(_cfg(tmp_path, tmp_min_free_mb=200))
    assert p["path"] == str(tmp_path) and p["thresholds"]["tmp_min_free_mb"] == 200
    monkeypatch.setattr(os, "statvfs", _vfs(ffree=50_000))                   # 5% free inodes
    assert E.temp_pressure(_cfg(tmp_path, tmp_min_free_inodes_pct=3)) is None
    p = E.temp_pressure(_cfg(tmp_path, tmp_min_free_inodes_pct=20))
    assert p["thresholds"]["tmp_min_free_inodes_pct"] == 20
    # a bad value falls back to the default inside the decision, not just in settings()
    assert E.temp_pressure(_cfg(tmp_path, tmp_min_free_inodes_pct=150))["free_inodes_pct"] == 5.0


def test_watchdog_emits_temp_pressure_only_under_pressure(tmp_path, monkeypatch, shared_dev):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(os, "statvfs", _vfs(free_mb=1))
    checks = {k: (t, d) for k, t, c, d in E.watchdog_checks(cfg)}
    key = f"temp_pressure:{tmp_path}"
    assert checks[key][0] == "temp_pressure"
    ev = E.append(cfg, "temp_pressure", data=checks[key][1])          # passes validation
    assert ev["type"] == "temp_pressure"
    monkeypatch.setattr(os, "statvfs", _vfs())
    assert not any(k.startswith("temp_pressure") for k, *_ in E.watchdog_checks(cfg))


# ---- admission guard -----------------------------------------------------------------------

def _two_specs(tmp_path, monkeypatch, entries=2):
    repo = make_repo(tmp_path)
    spec2 = repo / "docs" / "specs" / "0002-second.md"
    spec2.write_text((repo / "docs" / "specs" / "0001-trivial.md").read_text()
                     .replace("SPEC-0001", "SPEC-0002"))
    write_review({"data_dir": str(tmp_path / "data")}, spec2, "SPEC-0002")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "second spec"], check=True)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, [impl_done()] * entries), profile={"slots": 2})
    return cfg


def test_pressure_pauses_admission_but_not_in_flight_campaigns(tmp_path, monkeypatch, shared_dev):
    cfg = _two_specs(tmp_path, monkeypatch)
    first = added(cfg, monkeypatch, "SPEC-0001")
    assert Q.tick(cfg).admitted == 1                      # first: OPENING → IMPLEMENTING
    assert S.read_state(cfg, first)["state"] == "IMPLEMENTING"
    second = added(cfg, monkeypatch, "SPEC-0002")
    monkeypatch.setattr(os, "statvfs", _vfs(free_mb=1))
    report = Q.tick(cfg)
    assert report.admitted == 0 and report.admission_paused == "temp_pressure"
    assert report.payload()["admission_paused"] == "temp_pressure"
    assert S.read_state(cfg, second)["state"] == "QUEUED"
    assert [s["campaign"] for s in report.steps] == [first]            # its next step ran
    assert S.read_state(cfg, first)["state"] != "IMPLEMENTING"
    monkeypatch.setattr(os, "statvfs", _vfs())
    report = Q.tick(cfg)
    assert report.admitted == 1 and report.admission_paused is None
    assert "admission_paused" not in report.payload()
    assert S.read_state(cfg, second)["state"] != "QUEUED"


# ---- docs ----------------------------------------------------------------------------------

def test_conduct_skill_has_temp_pressure_row():
    skill = Path(__file__).resolve().parents[1] / "skills" / "wt-conduct" / "SKILL.md"
    assert any(l.startswith("| `temp_pressure`") for l in skill.read_text().splitlines())
