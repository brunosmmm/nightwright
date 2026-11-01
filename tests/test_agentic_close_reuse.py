"""SPEC-0359: a close gate with `reuse_from` reuses the last green loop result when the rebase is
clean (rules R1-R7), and only a merged reused close moves the sentinel's counter."""
import datetime as dt
import json
import threading

import pytest

from test_agentic_close import _commit, _events, _git, _git_env, _pcfg, world  # noqa: F401
from nightwright.agentic import close as CL
from nightwright.agentic import gitops as git
from nightwright.agentic import schemas as SC
from nightwright.agentic import sentinel as SN

PROJECT = "demo"
_REAL_AFFECTED = CL._affected


def _ago(seconds):
    return (dt.datetime.now().astimezone() - dt.timedelta(seconds=seconds)).isoformat(
        timespec="seconds")


def _green_loop(head, *, tests=("tests/test_feature.py", "tests/test_agentic_project.py"),
                full=False, report_head=None, started_at=None):
    report = {"head_sha": report_head or head, "tests": list(tests), "full": full,
              "reason": "affected"}
    return {"gate_seq": 3, "ok": True, "started_at": started_at or _ago(60), "duration_s": 1.0,
            "head": head, "tree": "t",
            "results": [{"name": "pytest-affected", "ok": True, "required": True,
                         "report": report}]}


FAKE_TESTS = {"feature.txt": ["tests/test_feature.py"], "other.txt": ["tests/test_other.py"]}
FAKE_FULL = {"pyproject.toml"}


@pytest.fixture
def rw(world, monkeypatch):
    """`world` plus: the branch tip gated green by `pytest-affected`, an unrelated commit on main,
    a close config whose `pytest` gate reuses the loop gate, and a fake `affected_tests.py`."""
    g0 = git.rev_parse(world["nw"])
    _commit(world["root"], "other.txt", "main\n", "unrelated main change")
    marker = world["tmp"] / "pytest-ran"
    pcfg = _pcfg(world)
    pcfg["gates"] = [
        {"name": "pytest-affected", "cmd": "true", "when": "loop", "required": True,
         "timeout_s": 30},
        {"name": "lint", "cmd": "true", "when": "close", "required": True, "timeout_s": 30},
        {"name": "pytest", "cmd": f"touch {marker}", "when": "close", "required": True,
         "timeout_s": 30, "reuse_from": "pytest-affected"},
    ]
    state = dict(world["state"], last_green_loop=_green_loop(g0))
    closures = {"tests/test_feature.py": ["tests/test_feature.py", "src/feature.py"],
                "tests/test_other.py": ["tests/test_other.py", "src/other.py"]}
    calls = []

    def fake(worktree, changed, *, closure=False):
        calls.append((tuple(changed), closure))
        if any(p in FAKE_FULL for p in changed):
            return {"full": True, "tests": [], "reason": "full trigger", "closure": None}
        tests = sorted({t for p in changed for t in FAKE_TESTS.get(p, [])})
        rec = {"full": False, "tests": tests, "reason": "fake selection"}
        if closure:
            rec["closure"] = sorted({f for t in tests for f in closures.get(t, [t])})
        return rec

    monkeypatch.setattr(CL, "_affected", fake)
    monkeypatch.delenv("NW_CLOSE_FULL", raising=False)
    return {**world, "g0": g0, "marker": marker, "pcfg": pcfg, "state": state,
            "closure": closures, "calls": calls}


def _declined(out, gate="pytest"):
    evs = [e for e in _events(out, "gate.reuse_declined") if e["gate"] == gate]
    assert len(evs) == 1, out.events
    return evs[0]["rule"], evs[0]["detail"]


def _state_file(rw):
    return SN.state_path(rw["cfg"], PROJECT)


# ---- the clean rebase ----------------------------------------------------------------------

def test_clean_rebase_reuses_the_loop_green(rw):
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    assert out.outcome is None
    assert not rw["marker"].exists(), "the reused gate did not run"
    assert [r.name for r in out.gate_run.results] == ["lint"]
    assert out.gate_run.reused == ("pytest",)
    (ev,) = _events(out, "gate.reused")
    assert all(k in ev for k in SC.EVENT_KEYS["gate.reused"])
    assert ev["gate"] == "pytest" and ev["from_gate"] == "pytest-affected"
    assert ev["from_gate_seq"] == 3 and ev["gated_head"] == rw["g0"]
    assert ev["close_head"] == out.head and ev["target_sha"] == out.target_sha
    assert ev["delta_count"] == 1 and ev["selection_count"] == 1
    assert ev["rules"] == {r: True for r in CL.REUSE_RULES}
    assert ev["full_source"] is False and ev["selection_reason"] == "fake selection"
    assert _events(out, "gate.reuse_declined") == []
    (gate,) = _events(out, "gate")
    assert gate["reused"] == ["pytest"]
    # evidence only: nothing counted before a merge
    assert SN.read(rw["cfg"], PROJECT)["reuses_since_green"] == 0


def test_gate_without_reuse_from_is_untouched(rw):
    rw["pcfg"]["gates"][2].pop("reuse_from")
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    assert rw["marker"].exists()
    assert _events(out, "gate.reused") == _events(out, "gate.reuse_declined") == []


# ---- one decline per rule ------------------------------------------------------------------

def _overdue(rw):
    SN._write(_state_file(rw), {**SN.empty_state(), "reuses_since_green": 1,
                                "suspects": [{"campaign": "c0", "merge_sha": "a" * 40}],
                                "due_since": _ago(3 * 86400)})


def _red(rw):
    SN._write(_state_file(rw), {**SN.empty_state(), "force_full_close": True})


def _corrupt(rw):
    _state_file(rw).parent.mkdir(parents=True, exist_ok=True)
    _state_file(rw).write_text("{not json")


def _fix_commit(rw):
    _commit(rw["nw"], "feature.txt", "fixed\n", "fix after green")


def _rebase_changes_branch_file(rw):
    _commit(rw["root"], "lines.txt", "".join(f"{i}\n" for i in range(1, 10)), "lines")
    _git(rw["nw"], "rebase", "-q", "main")
    _commit(rw["nw"], "lines.txt", "one\n" + "".join(f"{i}\n" for i in range(2, 10)), "b line1")
    rw["state"]["last_green_loop"] = _green_loop(git.rev_parse(rw["nw"]))
    _commit(rw["root"], "lines.txt", "".join(f"{i}\n" for i in range(1, 9)) + "nine\n",
            "main line9")


CASES = {
    "force_full": ("R1", "force_full", lambda rw, mp: rw["pcfg"]["close"].update(force_full=True)),
    "env": ("R1", "NW_CLOSE_FULL", lambda rw, mp: mp.setenv("NW_CLOSE_FULL", "1")),
    "pr_policy": ("R1", "policy", lambda rw, mp: rw["pcfg"]["merge"].update(policy="pr")),
    "unreadable": ("R1", "unreadable", lambda rw, mp: _corrupt(rw)),
    "overdue": ("R1", "overdue", lambda rw, mp: _overdue(rw)),
    "red_sentinel": ("R1", "force_full_close", lambda rw, mp: _red(rw)),
    "no_green": ("R2", "no green", lambda rw, mp: rw["state"].pop("last_green_loop")),
    "stale_green": ("R2", "is not the gated head", lambda rw, mp: rw["state"].update(
        last_green_loop=_green_loop(rw["g0"], report_head="b" * 40))),
    "invalid_g": ("R2", "not a valid commit", lambda rw, mp: rw["state"].update(
        last_green_loop=_green_loop("c" * 40))),
    "fix_commit": ("R3", "feature.txt", lambda rw, mp: _fix_commit(rw)),
    "rebase_changed_branch_file": ("R3", "lines.txt",
                                   lambda rw, mp: _rebase_changes_branch_file(rw)),
    "pyproject": ("R4", "full suite", lambda rw, mp: _commit(rw["root"], "pyproject.toml",
                                                             "[x]\n", "py")),
    "nw_config": ("R4", ".nw/extra.yaml", lambda rw, mp: _commit(rw["root"], ".nw/extra.yaml",
                                                                 "x: 1\n", "nw")),
    "grown": ("R5", "tests/test_feature.py", lambda rw, mp: rw["state"].update(
        last_green_loop=_green_loop(rw["g0"], tests=["tests/test_agentic_project.py"]))),
    "branch_test_reads_main": ("R6", "other.txt", lambda rw, mp: rw["closure"].update(
        {"tests/test_feature.py": ["tests/test_feature.py", "other.txt"]})),
    "main_test_reads_branch": ("R6", "feature.txt", lambda rw, mp: rw["closure"].update(
        {"tests/test_other.py": ["tests/test_other.py", "feature.txt"]})),
    "old": ("R7", "old", lambda rw, mp: rw["state"].update(
        last_green_loop=_green_loop(rw["g0"], started_at=_ago(2 * 86400)))),
    "large": ("R7", "paths differ", lambda rw, mp: rw["pcfg"]["close"].update(
        reuse_max_delta=0)),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_each_failing_rule_declines_and_the_gate_runs(rw, monkeypatch, case):
    rule, needle, setup = CASES[case]
    setup(rw, monkeypatch)
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    assert out.outcome is None, out.events
    got_rule, detail = _declined(out)
    assert got_rule == rule and needle in detail, (got_rule, detail)
    assert rw["marker"].exists(), "a declined gate runs"
    assert _events(out, "gate.reused") == [] and out.gate_run.reused == ()


def test_merge_request_declines_with_r1(rw):
    st = dict(rw["state"], kind="merge", merge={"own_worktree": True})
    out = CL.prepare_close(rw["cfg"], st, rw["pcfg"])
    assert _declined(out)[0] == "R1" and rw["marker"].exists()


# ---- tool errors fail closed ---------------------------------------------------------------

STUB = """import sys, time, json
if "--closure" in sys.argv:
    {closure}
print(json.dumps({{"full": False, "tests": [], "reason": "stub"}}))
"""


@pytest.mark.parametrize("closure,needle", [("sys.exit(3)", "exited 3"),
                                            ("time.sleep(5)", "timed out")])
def test_affected_tool_error_or_timeout_declines_with_r6(rw, monkeypatch, closure, needle):
    monkeypatch.setattr(CL, "_affected", _REAL_AFFECTED)
    monkeypatch.setattr(CL, "AFFECTED_TIMEOUT_S", 1)
    _commit(rw["root"], CL.AFFECTED_TOOL, STUB.format(closure=closure), "stub selection tool")
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    rule, detail = _declined(out)
    assert rule == "R6" and needle in detail, detail
    assert rw["marker"].exists()


def test_missing_tool_declines_with_r4(rw, monkeypatch):
    monkeypatch.setattr(CL, "_affected", _REAL_AFFECTED)
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    rule, detail = _declined(out)
    assert rule == "R4" and "not found" in detail


# ---- the counter moves only on a merged reused close ---------------------------------------

def test_reused_close_that_merges_counts_once(rw):
    out = CL.close_campaign(rw["cfg"], rw["state"], rw["pcfg"])
    assert out.next_state == "MERGED", out
    assert out.close["reused"] == ["pytest"]
    st = SN.read(rw["cfg"], PROJECT)
    assert st["reuses_since_green"] == 1
    assert st["suspects"] == [{"campaign": rw["state"]["id"],
                               "merge_sha": out.close["merged_sha"]}]


def test_retried_prepare_then_merge_counts_once(rw):
    moved = []

    def move_base():
        if not moved:
            moved.append(_commit(rw["root"], "late.txt", "late\n", "base moves"))

    out = CL.close_campaign(rw["cfg"], rw["state"], rw["pcfg"],
                            _hooks={"before_merge": move_base})
    assert out.next_state == "MERGED" and out.attempts == 2, out
    assert len(_events(out, "gate.reused")) == 2           # evidence from both prepares
    st = SN.read(rw["cfg"], PROJECT)
    assert st["reuses_since_green"] == 1 and len(st["suspects"]) == 1


def test_parked_closes_leave_the_counter_alone(rw):
    (rw["root"] / "README.md").write_text("dirty\n")
    out = CL.close_campaign(rw["cfg"], rw["state"], rw["pcfg"])
    assert out.next_state == "PARKED" and out.park["reason"] == "main_dirty"
    assert _events(out, "gate.reused")
    _git(rw["root"], "checkout", "README.md")

    def move_base():
        _commit(rw["root"], f"late-{len(list(rw['root'].glob('late-*')))}.txt", "x\n", "move")

    out = CL.close_campaign(rw["cfg"], rw["state"], rw["pcfg"], max_attempts=1,
                            _hooks={"before_merge": move_base})
    assert out.next_state == "PARKED" and out.park["reason"] == "base_moving"
    st = SN.read(rw["cfg"], PROJECT)
    assert st["reuses_since_green"] == 0 and st["suspects"] == []


def test_pr_policy_close_leaves_the_counter_alone(rw):
    rw["pcfg"]["merge"]["policy"] = "pr"
    out = CL.prepare_close(rw["cfg"], rw["state"], rw["pcfg"])
    assert _declined(out)[0] == "R1" and out.gate_run.reused == ()
    assert SN.read(rw["cfg"], PROJECT)["reuses_since_green"] == 0


def test_concurrent_increments_under_the_lock_all_land(rw):
    cfg = rw["cfg"]
    SN.read(cfg, PROJECT)
    shas = [f"{i:040x}" for i in range(8)]
    threads = [threading.Thread(target=SN.record_merge, args=(cfg, PROJECT, f"c{i}", sha))
               for i, sha in enumerate(shas)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    st = json.loads(_state_file(rw).read_text())
    assert st["reuses_since_green"] == 8
    assert sorted(s["merge_sha"] for s in st["suspects"]) == shas
