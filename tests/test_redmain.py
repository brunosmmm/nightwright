"""SPEC-0420: red-main episodes (`redmain.py`): open/update, merges from real `close` events and
real ancestry, every `step()` branch, `holding()` per kind, `on_green` withdrawals, the
weakening check, the full-suite gate env and `--fixes-red` merges. Real git repos in tmp_path."""
import datetime as dt
import json
import subprocess
import sys

import pytest

from test_agentic_close import _commit, _git, _git_env, _pcfg, world  # noqa: F401
from nightwright.agentic import close as CL
from nightwright.agentic import escalations as ESC
from nightwright.agentic import events as E
from nightwright.agentic import gates as G
from nightwright.agentic import gitops as git
from nightwright.agentic import redmain as RM
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import sentinel as SN
from nightwright.agentic import store

pytestmark = pytest.mark.fast

PROJECT = "demo"
NOW = dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.timezone.utc).timestamp()


@pytest.fixture
def w(world):
    world["cfg"]["agentic"] = {"escalations": {"notify": "none"}}
    return world


def _merged(cfg, cid, target, merged, *, kind="implement", spec_id=None, project=PROJECT):
    d = store.campaign_dir(cfg, cid)
    d.mkdir(parents=True, exist_ok=True)
    store.write_state(cfg, cid, {"id": cid, "kind": kind, "spec_id": spec_id,
                                 "project": project, "state": "MERGED",
                                 "created_at": store.now_iso(),
                                 "close": {"merged_sha": merged}})
    store.append_event(cfg, cid, "close", {"mode": "merge", "merged_sha": merged,
                                           "target_sha": target})


def _red_events(cfg):
    return [e for e in E.read_since(cfg, 0, types=["red_main"]) if e["project"] == PROJECT]


def _legacy(cfg, project=PROJECT, sha="a" * 10):
    plan = ESC.plan_escalate(cfg, None, question=f"Sentinel red on {project} at {sha}: x",
                             options=("a", "b"), frm="conductor")
    plan["project"] = project
    return ESC.apply_escalate(cfg, plan)


# ---- settings -------------------------------------------------------------------------------

def test_settings_defaults_and_fallbacks():
    assert RM.settings({}) == RM.DEFAULTS
    s = RM.settings({"agentic": {"red_main": {"enabled": "yes", "fixforward_attempts": -1,
                                              "escalate_after_s": "x", "revert": False,
                                              "brief_log_lines": 5}}})
    assert s["enabled"] is True and s["fixforward_attempts"] == 2
    assert s["escalate_after_s"] == 14400 and s["revert"] is False and s["brief_log_lines"] == 5


# ---- AC1: open / update, fingerprint de-dup -------------------------------------------------

def test_red_opens_episode_with_merges_from_real_ancestry(w):
    cfg, root = w["cfg"], w["root"]
    green = git.rev_parse(root)
    SN._write(SN.state_path(cfg, PROJECT), {**SN.empty_state(), "last_green_sha": green})
    a = _commit(root, "a.txt", "a\n", "merge a")
    b = _commit(root, "b.txt", "b\n", "merge b")
    _merged(cfg, "OLD-1", "0" * 40, green)                 # the green sha itself: before
    _merged(cfg, "C-A", green, a, spec_id="SPEC-0001")
    _merged(cfg, "C-B", a, b, spec_id="SPEC-0002")
    _merged(cfg, "OTHER-1", green, a, project="other")
    _merged(cfg, "FIX-x", a, b, kind="fixforward")         # fix campaigns are never suspects
    rec = RM.on_red(cfg, PROJECT, b, ["tests/t.py::test_a"], root=str(root), now=NOW)
    assert rec["open"] and rec["episode"] == 1 and rec["last_green_sha"] == green
    assert [m["campaign"] for m in rec["merges"]] == ["C-A", "C-B"]
    assert rec["merges"][1] == {"campaign": "C-B", "spec_id": "SPEC-0002", "kind": "implement",
                                "target_sha": a, "merged_sha": b}
    assert rec["fingerprint"] == RM.fingerprint(["tests/t.py::test_a"])
    (ev,) = _red_events(cfg)
    assert ev["data"]["episode"] == 1 and ev["data"]["failing"] == ["tests/t.py::test_a"]
    assert ESC.list_all(cfg, "open") == [], "AC1: no escalation"
    # same failing set at a new sha: no event, no escalation; merges grow
    c = _commit(root, "c.txt", "c\n", "merge c")
    _merged(cfg, "C-C", b, c)
    rec = RM.on_red(cfg, PROJECT, c, ["tests/t.py::test_a"], root=str(root), now=NOW + 60)
    assert rec["runs"] == 2 and rec["last_red_sha"] == c and rec["episode"] == 1
    assert [m["campaign"] for m in rec["merges"]] == ["C-A", "C-B", "C-C"]
    assert len(_red_events(cfg)) == 1 and ESC.list_all(cfg, "open") == []
    # a changed failing set: one more event, the union kept
    rec = RM.on_red(cfg, PROJECT, c, ["tests/t.py::test_z"], root=str(root), now=NOW + 120)
    assert len(_red_events(cfg)) == 2
    assert rec["failing"] == ["tests/t.py::test_a", "tests/t.py::test_z"]


def test_unknown_last_green_takes_the_newest_merges(w):
    cfg, root = w["cfg"], w["root"]
    prev = git.rev_parse(root)
    for i in range(RM.MERGES_UNKNOWN_GREEN + 2):
        sha = _commit(root, f"f{i}.txt", "x\n", f"m{i}")
        _merged(cfg, f"C-{i:02d}", prev, sha)
        prev = sha
    out = RM.merges_between(cfg, PROJECT, str(root), prev, None)
    assert len(out) == RM.MERGES_UNKNOWN_GREEN and out[-1]["campaign"] == f"C-{i:02d}"


def test_opening_an_episode_withdraws_legacy_escalations(w):
    cfg, root = w["cfg"], w["root"]
    old = [_legacy(cfg), _legacy(cfg, sha="b" * 10)]
    keep = _legacy(cfg, project="other")
    RM.on_red(cfg, PROJECT, git.rev_parse(root), ["t::x"], root=str(root), now=NOW)
    open_ids = [r["id"] for r in ESC.list_all(cfg, "open")]
    assert open_ids == [keep["id"]]
    for rec in old:
        got = ESC.read(cfg, rec["id"])
        assert got["status"] == "withdrawn" and "superseded by red episode 1" in \
            got["withdrawn"]["reason"]


# ---- AC7: step() ----------------------------------------------------------------------------

def _rec(**kw):
    base = {**RM._blank(), "episode": 3, "open": True,
            "opened_at": dt.datetime.fromtimestamp(NOW).astimezone().isoformat()}
    return {**base, **kw}


def _cfg(**rm):
    return {"agentic": {"red_main": rm}}


def test_step_branches():
    step = lambda rec, active=(), cfg=None, pcfg=None, now=NOW + 60: RM.step(  # noqa: E731
        cfg or {}, PROJECT, now=now, active=active, red=rec, project_cfg=pcfg)
    assert step({**_rec(), "open": False})[0] == "none"
    assert step(_rec()) == ("open_fixforward", "attempt 1 of 2")
    live = [{"id": "FIX-1", "project": PROJECT, "kind": "fixforward", "red_episode": 3,
             "state": "IMPLEMENTING"}]
    assert step(_rec(), live)[0] == "none"
    merge = [{"id": "M", "project": PROJECT, "kind": "merge", "fixes_red": True,
              "state": "CLOSING"}]
    assert step(_rec(), merge) == ("none", "fix in flight: M")
    other_ep = [{**live[0], "red_episode": 2}]
    assert step(_rec(), other_ep)[0] == "open_fixforward"
    one = _rec(fixes=[{"campaign": "F1", "kind": "fixforward", "outcome": "failed"}])
    assert step(one) == ("open_fixforward", "attempt 2 of 2")
    two = _rec(fixes=[{"campaign": f"F{i}", "kind": "fixforward", "outcome": "failed"}
                      for i in (1, 2)])
    assert step(two)[0] == "localize"
    assert step({**two, "resume_credit": 1})[0] == "open_fixforward"
    culprit = {"campaign": "C-B", "target_sha": "a", "merged_sha": "b"}
    assert step({**two, "culprit": culprit})[0] == "open_revert"
    nobody = {**two, "localize": {"done": True, "why": "nothing"}}
    action, why = step(nobody)
    assert action == "escalate" and "no culprit" in why
    assert step({**nobody, "escalation": "ESC-0009"})[0] == "none", "escalates once"
    reverted = {**two, "culprit": culprit,
                "fixes": two["fixes"] + [{"campaign": "R1", "kind": "revert",
                                          "outcome": "failed"}]}
    action, why = step(reverted)
    assert action == "escalate" and "R1 failed" in why
    assert step(two, cfg=_cfg(revert=False))[0] == "escalate"
    assert step(two, pcfg={"merge": {"policy": "pr"}})[0] == "escalate", "pr: no revert"
    # a merged fix awaits the sentinel; past escalate_after_s with no fix in flight: escalate
    waiting = _rec(fixes=[{"campaign": "F1", "kind": "fixforward", "outcome": "merged"}])
    assert step(waiting)[0] == "none"
    action, why = step(waiting, now=NOW + 14401)
    assert action == "escalate" and "no fix in flight" in why
    assert step({**waiting, "escalation": "ESC-1"}, now=NOW + 14401)[0] == "none"
    auto = [{"id": "F2", "project": PROJECT, "kind": "fixforward", "red_episode": 3,
             "state": "PARKED", "park": {"reason": "cost_cap", "auto": {"rule": "x"}}}]
    assert step(_rec(), auto)[0] == "none", "a queue-resumed park is not a failure"
    assert step(_rec(), cfg=_cfg(enabled=False))[0] == "none"


# ---- AC4: holding ---------------------------------------------------------------------------

def test_holding_per_kind():
    red = _rec()
    hold = lambda st, cfg=None, pcfg=None: RM.holding(cfg or {}, PROJECT, st, red,  # noqa: E731
                                                      project_cfg=pcfg)
    assert "episode 3" in hold({"kind": "implement"})
    assert hold({"kind": "idea"}) and hold({"kind": "merge", "merge": {}})
    assert hold({"kind": "fixforward"}) is None and hold({"kind": "revert"}) is None
    assert hold({"kind": "merge", "fixes_red": True}) is None
    assert hold({"kind": "merge", "merge": {"docs_only": True}}) is None
    assert hold({"kind": "implement"}, cfg=_cfg(hold_merges=False)) is None
    assert hold({"kind": "implement"}, cfg=_cfg(enabled=False)) is None
    assert hold({"kind": "implement"}, pcfg={"merge": {"policy": "pr"}}) is None
    assert RM.holding({}, PROJECT, {"kind": "implement"}, {**red, "open": False}) is None


def test_holding_releases_once_the_base_moves_past_the_red_sha(w):
    cfg, root = w["cfg"], w["root"]
    red_sha = git.rev_parse(root)
    RM.on_red(cfg, PROJECT, red_sha, ["t::x"], root=str(root), now=NOW)
    pcfg = {**_pcfg(w), "_root": str(root)}
    assert "red" in RM.holding(cfg, PROJECT, {"kind": "implement"}, project_cfg=pcfg)
    _commit(root, "later.txt", "x", "a fix landed")
    assert RM.holding(cfg, PROJECT, {"kind": "implement"}, project_cfg=pcfg) is None


def test_prepare_close_holds_a_spec_campaign_without_touching_anything(w):
    cfg, root = w["cfg"], w["root"]
    RM.on_red(cfg, PROJECT, git.rev_parse(root), ["t::x"], root=str(root), now=NOW)
    head = git.rev_parse(w["nw"])
    out = CL.prepare_close(cfg, w["state"], _pcfg(w))
    assert out.outcome is not None and out.outcome.next_state == "CLOSING"
    assert "red" in out.outcome.held and out.outcome.park is None
    assert git.rev_parse(w["nw"]) == head, "no rebase, no snapshot"


# ---- AC5: on_green --------------------------------------------------------------------------

def test_green_closes_withdraws_episode_and_legacy_escalations(w):
    cfg, root = w["cfg"], w["root"]
    sha = git.rev_parse(root)
    RM.on_red(cfg, PROJECT, sha, ["t::x"], root=str(root), now=NOW)
    own = _legacy(cfg, project="x-other")          # stands in for the episode's own record
    RM.set_escalation(cfg, PROJECT, own["id"])
    legacy = _legacy(cfg)                          # filed by an old runner after the open
    RM.set_due(cfg, PROJECT, "fix merged")
    closed = RM.on_green(cfg, PROJECT, sha, now=NOW + 3600)
    assert closed["episode"] == 1 and not RM.is_red(cfg, PROJECT)
    assert RM.due_reason(cfg, PROJECT) is None
    assert ESC.read(cfg, own["id"])["status"] == "withdrawn"
    assert ESC.read(cfg, legacy["id"])["status"] == "withdrawn"
    (ev,) = E.read_since(cfg, 0, types=["red_main_cleared"])
    assert ev["data"]["episode"] == 1 and ev["data"]["duration_s"] == 3600
    assert RM.on_green(cfg, PROJECT, sha) is None, "nothing open: no second event"


def test_legacy_red_is_adopted_by_marking_the_sentinel_due(w):
    cfg = w["cfg"]
    assert not RM.adopt_legacy(cfg, PROJECT, {"force_full_close": False})
    assert RM.adopt_legacy(cfg, PROJECT, {"force_full_close": True})
    assert "legacy" in RM.due_reason(cfg, PROJECT) and RM.read(cfg, PROJECT) is None
    assert not RM.adopt_legacy(cfg, PROJECT, {"force_full_close": True}), "once"
    SN._write(SN.state_path(cfg, PROJECT), {**SN.empty_state(), "last_green_at": "2026-11-04"})
    assert SN.check_due(cfg, PROJECT), "due with zero reuses (mark_due)"


# ---- AC6: bisect (pure) ---------------------------------------------------------------------

def test_bisect_finds_the_first_failing_merge_in_few_probes():
    merges = [{"campaign": f"C{i}", "target_sha": f"s{i - 1}", "merged_sha": f"s{i}"}
              for i in range(1, 5)]
    calls = []

    def fails(sha):
        calls.append(sha)
        return int(sha[1:]) >= 3
    c, why, n = RM.bisect(merges, fails)
    assert c["campaign"] == "C3" and n == len(set(calls)) <= 3
    assert RM.bisect(merges[:1], lambda s: pytest.fail("no probe"))[0]["campaign"] == "C1"
    assert RM.bisect(merges, lambda s: False)[0] is None
    assert RM.bisect(merges, lambda s: True)[0] is None, "the base before C1 fails too"


# ---- AC3: the weakening check ---------------------------------------------------------------

TESTS = "import pytest\n\n\ndef test_a():\n    assert 1\n\n\ndef test_b():\n    assert 1\n"


@pytest.fixture
def wk(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "tests/test_t.py", TESTS, "tests")
    _commit(repo, "tests/test_g.py", "# tree_guard\ndef test_g():\n    pass\n", "guard")
    _commit(repo, "src.py", "x = 1\n", "src")
    return repo, git.rev_parse(repo)


@pytest.mark.parametrize("change,expect", [
    ({"src.py": "x = 2\n"}, None),
    ({"tests/test_t.py": TESTS + "\n\ndef test_c():\n    assert 1\n"}, None),
    ({"tests/test_t.py": None}, "deletes test file tests/test_t.py"),
    ({"tests/test_t.py": TESTS.split("\n\n\ndef test_b")[0] + "\n"}, "removes 1 test"),
    ({"tests/test_t.py": TESTS.replace("def test_b", "@pytest.mark.skip\ndef test_b")},
     "adds pytest.mark.skip"),
    ({"tests/test_t.py": TESTS.replace("def test_b", "@pytest.mark.xfail(strict=False)\n"
                                       "def test_b")}, "adds pytest.mark.xfail"),
    ({"tests/test_t.py": TESTS.replace("import pytest", "x = pytest.importorskip('y')")},
     "adds importorskip"),
    ({"tests/conftest.py": "collect_ignore = ['test_t.py']\n"}, "adds collect_ignore"),
    ({"tests/test_g.py": "# tree_guard\ndef test_g():\n    return\n"}, "tree_guard module"),
])
def test_fixforward_weakens(wk, change, expect):
    repo, base = wk
    for rel, text in change.items():
        p = repo / rel
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix")
    out = CL.fixforward_weakens(repo, base)
    if expect is None:
        assert out == []
    else:
        assert any(expect in o for o in out), out


# ---- AC2: full-suite env --------------------------------------------------------------------

def test_full_suite_env_local_and_remote(tmp_path):
    cfg = {"data_dir": str(tmp_path), "config_dir": ""}
    st = {"id": "FIX-x-20261104-abcd", "spec_id": None, "repo": {"base": "main"}}
    assert "NW_AFFECTED_FULL" not in G.gate_env(cfg, st)
    assert G.gate_env(cfg, {**st, "full_suite": True})["NW_AFFECTED_FULL"] == "1"
    h = RG._header({"gates": []}, {**st, "full_suite": True}, cfg, 1, "j", "k", "0" * 40,
                   "0" * 40, [], 0)
    assert h["env"]["NW_AFFECTED_FULL"] == "1"
    from nightwright.agentic import worker as W
    W.validate_header(h)                                    # the worker accepts the key
    h = RG._header({"gates": []}, st, cfg, 1, "j", "k", "0" * 40, "0" * 40, [], 0)
    assert "NW_AFFECTED_FULL" not in h["env"]


def test_affected_tool_runs_the_full_suite_under_the_flag(tmp_path):
    """The env var the gate env sets is the one `tools/affected_tests.py` honours."""
    from pathlib import Path
    tool = Path(__file__).resolve().parent.parent / "tools" / "affected_tests.py"
    code = ("import importlib.util,os,sys;s=importlib.util.spec_from_file_location('a',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.forced_full())")
    out = subprocess.run([sys.executable, "-c", code, str(tool)], capture_output=True, text=True,
                         env={"NW_AFFECTED_FULL": "1", "PATH": "/usr/bin:/bin"})
    assert "NW_AFFECTED_FULL" in out.stdout, out.stderr


# ---- status / resume-fix / clear --------------------------------------------------------------

def test_status_resume_fix_and_clear(w):
    cfg, root = w["cfg"], w["root"]
    RM.on_red(cfg, PROJECT, git.rev_parse(root), ["t::x"], root=str(root), now=NOW)
    with RM.locked(cfg, PROJECT):
        rec = RM._load(cfg, PROJECT)
        rec["fixes"] = [{"campaign": f"F{i}", "kind": "fixforward", "outcome": "failed"}
                        for i in (1, 2)]
        RM._save(cfg, PROJECT, rec)
    st = RM.status(cfg, PROJECT, now=NOW + 10)
    assert st["schema"] == "nw.queue.red.v1" and st["failed_fixforwards"] == 2
    assert st["age_s"] == 10 and st["episode"]["open"]
    assert RM.failed_fixforwards(RM.resume_fix(cfg, PROJECT)) == 1
    assert RM.clear(cfg, PROJECT, by="human")["open"] is False
    assert RM.clear(cfg, PROJECT) is None
    assert json.loads(RM.path(cfg, PROJECT).read_text())["history"][-1]["what"] == "cleared"


# ---- SPEC-0493: episode sources (sentinel / cadence:<job>) ----------------------------------

def _cad_row(cfg, job):
    from nightwright.agentic import cadence as CAD
    doc = CAD._read_doc(CAD.state_path(cfg, PROJECT)) or {}
    return (doc.get("jobs") or {}).get(job) or {}


def test_source_helpers_and_legacy_episode():
    assert RM.source_of(None) == "sentinel" and RM.source_of({"open": True}) == "sentinel"
    assert RM.source_of({"source": "cadence:tui"}) == "cadence:tui"
    assert RM.cadence_job("cadence:tui") == "tui" and RM.cadence_job("sentinel") is None
    assert RM.cadence_job("cadence:") is None and RM.cadence_job(None) is None
    assert RM.source_label("cadence:perf") == "cadence job perf"
    assert RM.source_label("sentinel") == "sentinel"


def test_cadence_red_opens_with_source_and_its_own_green_closes(w):
    cfg, root = w["cfg"], w["root"]
    sha = git.rev_parse(root)
    rec = RM.on_red(cfg, PROJECT, sha, ["t::a"], root=str(root), now=NOW, source="cadence:tui")
    assert rec["open"] and rec["source"] == "cadence:tui"
    assert not SN.state_path(cfg, PROJECT).exists(), "a cadence red reads no sentinel state"
    (ev,) = _red_events(cfg)
    assert ev["data"]["source"] == "cadence:tui" and ev["data"]["opened"]
    assert RM.on_green(cfg, PROJECT, sha, now=NOW, source="sentinel") is None
    assert RM.read(cfg, PROJECT)["open"], "another source's green never closes it"
    assert RM.on_green(cfg, PROJECT, sha, now=NOW, source="cadence:perf") is None
    closed = RM.on_green(cfg, PROJECT, sha, now=NOW, source="cadence:tui")
    assert closed and not RM.read(cfg, PROJECT)["open"]
    (cl,) = [e for e in E.read_since(cfg, 0, types=["red_main_cleared"])]
    assert cl["data"]["source"] == "cadence:tui"
    # a cadence green with no open episode writes nothing
    before = RM.read(cfg, PROJECT)
    assert RM.on_green(cfg, PROJECT, sha, now=NOW, source="cadence:tui") is None
    assert RM.read(cfg, PROJECT) == before


def test_cross_source_red_is_deferred_as_a_pending_triple(w):
    cfg, root = w["cfg"], w["root"]
    sha = git.rev_parse(root)
    RM.on_red(cfg, PROJECT, sha, ["t::a"], root=str(root), now=NOW)
    rec = RM.on_red(cfg, PROJECT, "b" * 40, ["t::tui"], root=str(root), now=NOW,
                    source="cadence:tui")
    assert rec["source"] == "sentinel" and rec["failing"] == ["t::a"]
    assert (rec["pending_source"], rec["pending_failing"], rec["pending_sha"]) == \
        ("cadence:tui", ["t::tui"], "b" * 40)
    # the first pending source wins; a repeat of it refreshes the triple
    RM.on_red(cfg, PROJECT, "c" * 40, ["t::perf"], root=str(root), now=NOW,
              source="cadence:perf")
    assert RM.read(cfg, PROJECT)["pending_source"] == "cadence:tui"
    RM.on_red(cfg, PROJECT, "d" * 40, ["t::tui2"], root=str(root), now=NOW,
              source="cadence:tui")
    rec = RM.read(cfg, PROJECT)
    assert rec["pending_sha"] == "d" * 40 and rec["pending_failing"] == ["t::tui2"]
    assert len(_red_events(cfg)) == 1, "a deferred red appends no red_main event"
    from nightwright.agentic import queue as Q
    rows = [r for r in Q.read_journal(cfg) if r.get("kind") == "cadence_deferred"]
    assert len(rows) == 3 and rows[0]["episode_source"] == "sentinel"
    # the pending source's green clears the triple; the episode stays open
    assert RM.on_green(cfg, PROJECT, "e" * 40, now=NOW, source="cadence:tui") is None
    rec = RM.read(cfg, PROJECT)
    assert rec["open"] and "pending_source" not in rec


def test_own_green_promotes_the_pending_red_awaiting_refresh(w):
    cfg, root = w["cfg"], w["root"]
    sha = git.rev_parse(root)
    RM.on_red(cfg, PROJECT, sha, ["t::a"], root=str(root), now=NOW)
    RM.on_red(cfg, PROJECT, "b" * 40, ["t::tui"], root=str(root), now=NOW,
              source="cadence:tui")
    closed = RM.on_green(cfg, PROJECT, sha, now=NOW + 60)
    assert closed["episode"] == 1
    rec = RM.read(cfg, PROJECT)
    assert rec["open"] and rec["episode"] == 2 and rec["source"] == "cadence:tui"
    assert rec["awaiting_refresh"] and rec["first_red_sha"] == "b" * 40
    assert "pending_source" not in rec
    assert _cad_row(cfg, "tui").get("forced") is True, "the promoted job is forced"
    assert [e["data"]["source"] for e in _red_events(cfg)] == ["sentinel", "cadence:tui"]
    assert RM.step(cfg, PROJECT, now=NOW + 120, red=rec)[0] == "none"
    assert "awaiting a refreshing cadence job tui" in RM.step(cfg, PROJECT, now=NOW + 120,
                                                              red=rec)[1]
    # the job's red refreshes it: fixes may open
    rec = RM.on_red(cfg, PROJECT, "f" * 40, ["t::tui"], root=str(root), now=NOW + 180,
                    source="cadence:tui")
    assert "awaiting_refresh" not in rec
    assert RM.step(cfg, PROJECT, now=NOW + 240, red=rec)[0] == "open_fixforward"


def test_a_promoted_sentinel_red_is_marked_due(w):
    cfg, root = w["cfg"], w["root"]
    sha = git.rev_parse(root)
    RM.on_red(cfg, PROJECT, sha, ["t::tui"], root=str(root), now=NOW, source="cadence:tui")
    RM.on_red(cfg, PROJECT, "b" * 40, ["t::a"], root=str(root), now=NOW)
    RM.on_green(cfg, PROJECT, sha, now=NOW + 60, source="cadence:tui")
    rec = RM.read(cfg, PROJECT)
    assert rec["source"] == "sentinel" and rec["awaiting_refresh"]
    assert rec["due_reason"] == "deferred sentinel red" and rec["first_red_sha"] == "b" * 40


def test_record_merge_forces_the_cadence_job(w):
    cfg, root = w["cfg"], w["root"]
    sha = git.rev_parse(root)
    RM.on_red(cfg, PROJECT, sha, ["t::tui"], root=str(root), now=NOW, source="cadence:tui")
    st = {"id": "FIX-1", "project": PROJECT, "kind": "fixforward"}
    assert RM.record_merge(cfg, st, "a" * 40, now=NOW)
    rec = RM.read(cfg, PROJECT)
    assert not rec.get("due_reason") and _cad_row(cfg, "tui")["forced"] is True
    assert rec["fixes"][0]["outcome"] == "merged"


def test_holding_brief_and_step_name_the_cadence_source(w):
    cfg = w["cfg"]
    red = _rec(source="cadence:tui", failing=["t::x"], first_red_sha="a" * 40,
               last_red_sha="a" * 40)
    msg = RM.holding(cfg, PROJECT, {"kind": "implement", "project": PROJECT}, red=red)
    assert msg and "cadence job tui" in msg and "green cadence job tui" in msg
    text = RM.brief(cfg, red, project=PROJECT)
    assert "cadence job tui" in text and "full test suite" not in text
    assert "Merges since the last green cadence job tui" in text
    legacy = _rec(failing=["t::x"])
    assert "full test suite" in RM.brief(cfg, legacy, project=PROJECT)
    two = _rec(source="cadence:tui", fixes=[{"campaign": f"F{i}", "kind": "fixforward",
                                             "outcome": "failed"} for i in (1, 2)])
    action, why = RM.step({}, PROJECT, now=NOW + 60, red=two)
    assert action == "escalate" and "no revert for a cadence red" in why
    assert RM.step({}, PROJECT, now=NOW + 60, red={**two, "escalation": "ESC-1"})[0] == "none"
    culprit = {"campaign": "C-B", "target_sha": "a", "merged_sha": "b"}
    assert RM.step(_cfg(revert=True), PROJECT, now=NOW + 60,
                   red={**two, "culprit": culprit})[0] == "escalate", "never open_revert"
    live = [{"id": "FIX-1", "project": PROJECT, "kind": "fixforward", "red_episode": 3,
             "state": "IMPLEMENTING"}]
    one = _rec(source="cadence:tui")
    assert RM.step({}, PROJECT, now=NOW + 60, red=one, active=live)[0] == "none"
    assert RM.step({}, PROJECT, now=NOW + 60, red=one) == ("open_fixforward", "attempt 1 of 2")
    # a legacy episode (no source) still localizes
    assert RM.step({}, PROJECT, now=NOW + 60, red=_rec(fixes=two["fixes"]))[0] == "localize"
