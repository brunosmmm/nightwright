"""SPEC-0409: the TUI runtime budget plugin (tests/tui_budget.py)."""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import tui_budget as B

TESTS = Path(__file__).resolve().parent
LIM = B.Limits(test=10.0, file=30.0, total=60.0)


def _kinds(vs):
    return [(v.kind, v.name) for v in vs]


def test_defaults_are_the_documented_ones():
    assert B.limits_from_env({}) == B.Limits(30.0, 300.0, 1200.0)


def test_per_test_violation_and_scope():
    d = {"tests/test_tui_a.py::test_slow": 11.0, "tests/test_tui_a.py::test_ok": 1.0,
         "tests/test_other.py::test_slow": 500.0}  # not a TUI file: ignored
    assert _kinds(B.evaluate(d, LIM)) == [("test", "tests/test_tui_a.py::test_slow")]


def test_scope_is_the_file_basename_wherever_the_rootdir_is():
    assert B.in_scope("test_tui_x.py::test_a")
    assert B.in_scope("/tmp/abc/sub/test_tui_x.py::test_a")
    assert not B.in_scope("tests/test_tuix.py::test_a")
    assert not B.in_scope("tests/test_tui_x.txt::test_a")


def test_per_file_violation_when_no_single_test_is_over():
    d = {f"tests/test_tui_a.py::t{i}": 8.0 for i in range(4)}  # 32s > 30s, each < 10s
    assert _kinds(B.evaluate(d, LIM)) == [("file", "tests/test_tui_a.py")]


def test_total_violation_across_files():
    d = {f"tests/test_tui_f{f}.py::t{i}": 5.0 for f in range(4) for i in range(3)}  # 15s/file
    assert B.evaluate(d, LIM) == []  # 60s total is the limit itself; each file is 15 < 30
    d["tests/test_tui_f0.py::extra"] = 0.5
    assert _kinds(B.evaluate(d, LIM)) == [("total", "(all TUI files)")]


def test_worst_first_and_boundary_is_not_a_violation():
    d = {"tests/test_tui_a.py::a": 10.0, "tests/test_tui_b.py::b": 12.0,
         "tests/test_tui_c.py::c": 25.0}
    vs = B.evaluate(d, B.Limits(test=10.0, file=1000.0, total=1000.0))
    assert [v.name for v in vs] == ["tests/test_tui_c.py::c", "tests/test_tui_b.py::b"]


def test_nothing_to_judge():
    assert B.evaluate({}, LIM) == []
    assert B.evaluate({"tests/test_x.py::t": 999.0}, LIM) == []


def test_scale_multiplies_all_three_limits():
    assert B.limits_from_env({"NW_TUI_BUDGET_SCALE": "2"}) == B.Limits(60.0, 600.0, 2400.0)


def test_per_limit_overrides_and_scale_together():
    env = {"NW_TUI_TEST_BUDGET_S": "5", "NW_TUI_FILE_BUDGET_S": "7", "NW_TUI_TOTAL_BUDGET_S": "9"}
    assert B.limits_from_env(env) == B.Limits(5.0, 7.0, 9.0)
    assert B.limits_from_env({**env, "NW_TUI_BUDGET_SCALE": "3"}) == B.Limits(15.0, 21.0, 27.0)
    assert B.limits_from_env({"NW_TUI_FILE_BUDGET_S": "7"}) == B.Limits(30.0, 7.0, 1200.0)


@pytest.mark.parametrize("var", ["NW_TUI_BUDGET_SCALE", "NW_TUI_TEST_BUDGET_S",
                                 "NW_TUI_FILE_BUDGET_S", "NW_TUI_TOTAL_BUDGET_S"])
@pytest.mark.parametrize("bad", ["fast", "0", "-1", "nan", "inf"])
def test_malformed_value_fails_instead_of_disabling(var, bad):
    with pytest.raises(B.BudgetConfigError, match=var):
        B.limits_from_env({var: bad})


@pytest.fixture
def fresh_state(monkeypatch):
    monkeypatch.setattr(B, "_state", None)
    for k in ("NW_TUI_BUDGET_SCALE", "NW_TUI_TEST_BUDGET_S", "NW_TUI_FILE_BUDGET_S",
              "NW_TUI_TOTAL_BUDGET_S"):
        monkeypatch.delenv(k, raising=False)


def _config(**attrs):
    return SimpleNamespace(option=SimpleNamespace(verbose=0, collectonly=False), **attrs)


def _report(nodeid, duration):
    return SimpleNamespace(nodeid=nodeid, duration=duration)


def test_hooks_inert_under_xdist_worker(fresh_state):
    B.pytest_configure(_config(workerinput={"workerid": "gw0"}))
    assert B._state is None
    B.pytest_runtest_logreport(_report("tests/test_tui_a.py::t", 999.0))  # must not raise
    session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)
    B.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.OK


def test_hooks_inert_on_collect_only(fresh_state):
    cfg = _config()
    cfg.option.collectonly = True
    B.pytest_configure(cfg)
    assert B._state is None


def test_hooks_pass_when_no_tui_item_reported(fresh_state):
    B.pytest_configure(_config())
    B.pytest_runtest_logreport(_report("tests/test_other.py::t", 999.0))
    session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)
    B.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.OK


def test_hooks_fail_the_session_on_violation_and_sum_all_phases(fresh_state, monkeypatch):
    monkeypatch.setenv("NW_TUI_TEST_BUDGET_S", "1")
    B.pytest_configure(_config())
    for phase_secs in (0.4, 0.4, 0.4):  # setup + call + teardown each under the limit
        B.pytest_runtest_logreport(_report("tests/test_tui_a.py::t", phase_secs))
    session = SimpleNamespace(exitstatus=pytest.ExitCode.OK)
    B.pytest_sessionfinish(session, 0)
    assert session.exitstatus == pytest.ExitCode.TESTS_FAILED
    assert _kinds(B._state.violations) == [("test", "tests/test_tui_a.py::t")]


def test_malformed_env_is_a_usage_error(fresh_state, monkeypatch):
    monkeypatch.setenv("NW_TUI_BUDGET_SCALE", "oops")
    with pytest.raises(pytest.UsageError, match="NW_TUI_BUDGET_SCALE"):
        B.pytest_configure(_config())


def _run(tmp_path, body, **env):
    (tmp_path / "test_tui_x.py").write_text(body)
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("NW_TUI_", "PYTEST_", "NW_PERSONA"))}
    e["PYTHONPATH"] = str(TESTS) + os.pathsep + e.get("PYTHONPATH", "")
    e.update(env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tui_budget", "-p", "no:cacheprovider", "-p",
         "no:xdist", "test_tui_x.py"],
        cwd=tmp_path, env=e, capture_output=True, text=True, timeout=120)


SLEEPER = "import time\n\n\ndef test_sleeps():\n    time.sleep(0.5)\n"


def test_subprocess_run_over_budget_fails_and_names_the_offender(tmp_path):
    r = _run(tmp_path, SLEEPER, NW_TUI_TEST_BUDGET_S="0.05")
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "1 passed" in out  # the test itself passed: the budget failed the run
    assert B.HEADING in out
    assert "test_tui_x.py::test_sleeps" in out


def test_subprocess_run_within_default_budget_passes_and_reports_slowest_file(tmp_path):
    r = _run(tmp_path, SLEEPER)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert "tui budget ok: slowest TUI file test_tui_x.py" in out


def test_subprocess_scale_loosens_the_limits(tmp_path):
    r = _run(tmp_path, SLEEPER, NW_TUI_TEST_BUDGET_S="0.05", NW_TUI_BUDGET_SCALE="100")
    assert r.returncode == 0, r.stdout + r.stderr


def test_subprocess_malformed_env_fails_clearly(tmp_path):
    r = _run(tmp_path, SLEEPER, NW_TUI_TEST_BUDGET_S="soon")
    assert r.returncode != 0
    assert "NW_TUI_TEST_BUDGET_S" in r.stdout + r.stderr
