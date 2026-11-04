"""SPEC-0409: a runtime budget for the TUI test files, enforced by the suite itself.

A pytest plugin (module-level hooks) registered from `conftest.py`. It sums each `test_tui_*.py`
test's setup+call+teardown time and fails the run when a test, a file, or all TUI files together
go over their limit. Summed test durations, not wall time, so it is stable under xdist and the
host-wide gate-slot pool. Every limit scales with `NW_TUI_BUDGET_SCALE` (like `NW_TEST_WAIT_S`
in waits.py); `NW_TUI_TEST_BUDGET_S` / `NW_TUI_FILE_BUDGET_S` / `NW_TUI_TOTAL_BUDGET_S` replace
one default each (the scale still applies to the replaced value).
"""
import fnmatch
import math
import os
from collections import defaultdict
from dataclasses import dataclass

import pytest

SCOPE_GLOB = "test_tui_*.py"  # matched on the file basename, wherever the rootdir is
DEFAULTS = {"test": 30.0, "file": 180.0, "total": 900.0}
ENV_LIMIT = {"test": "NW_TUI_TEST_BUDGET_S", "file": "NW_TUI_FILE_BUDGET_S",
             "total": "NW_TUI_TOTAL_BUDGET_S"}
ENV_SCALE = "NW_TUI_BUDGET_SCALE"
HEADING = "TUI runtime budget exceeded"


class BudgetConfigError(ValueError):
    """A budget env var is not a positive finite number."""


@dataclass(frozen=True)
class Limits:
    test: float
    file: float
    total: float


@dataclass(frozen=True)
class Violation:
    kind: str  # "test" | "file" | "total"
    name: str
    seconds: float
    limit: float

    @property
    def ratio(self) -> float:
        return self.seconds / self.limit


def _positive(env, name, default) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except ValueError:
        val = float("nan")
    if not math.isfinite(val) or val <= 0:
        raise BudgetConfigError(f"{name}={raw!r} is not a positive number")
    return val


def limits_from_env(env=None) -> Limits:
    env = os.environ if env is None else env
    scale = _positive(env, ENV_SCALE, 1.0)
    got = {k: _positive(env, ENV_LIMIT[k], DEFAULTS[k]) * scale for k in DEFAULTS}
    return Limits(**got)


def file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def in_scope(nodeid: str) -> bool:
    return fnmatch.fnmatch(os.path.basename(file_of(nodeid)), SCOPE_GLOB)


def per_file(durations) -> dict[str, float]:
    files: dict[str, float] = defaultdict(float)
    for nodeid, secs in durations.items():
        if in_scope(nodeid):
            files[file_of(nodeid)] += secs
    return dict(files)


def evaluate(durations, limits: Limits) -> list[Violation]:
    """`durations`: nodeid -> summed seconds. Non-TUI nodeids are ignored. Worst first (by how far
    over the limit)."""
    scoped = {n: s for n, s in durations.items() if in_scope(n)}
    found = [Violation("test", n, s, limits.test) for n, s in scoped.items() if s > limits.test]
    files = per_file(scoped)
    found += [Violation("file", f, s, limits.file) for f, s in files.items() if s > limits.file]
    total = sum(scoped.values())
    if total > limits.total:
        found.append(Violation("total", "(all TUI files)", total, limits.total))
    return sorted(found, key=lambda v: (-v.ratio, v.kind, v.name))


class _State:
    def __init__(self, limits: Limits, verbose: int):
        self.limits = limits
        self.verbose = verbose
        self.durations: dict[str, float] = defaultdict(float)
        self.violations: list[Violation] = []


_state: _State | None = None


def pytest_configure(config):
    global _state
    _state = None
    if hasattr(config, "workerinput"):  # an xdist worker: the controller judges
        return
    try:
        limits = limits_from_env()
    except BudgetConfigError as e:
        raise pytest.UsageError(f"tui_budget: {e}") from None
    _state = _State(limits, getattr(config.option, "verbose", 0))
    if getattr(config.option, "collectonly", False):
        _state = None


def pytest_runtest_logreport(report):
    if _state is not None and in_scope(report.nodeid):
        _state.durations[report.nodeid] += report.duration


def pytest_sessionfinish(session, exitstatus):
    if _state is None or not _state.durations:
        return
    _state.violations = evaluate(_state.durations, _state.limits)
    if _state.violations and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if _state is None or not _state.durations:
        return
    if _state.violations:
        terminalreporter.section(HEADING, sep="=", red=True, bold=True)
        for v in _state.violations:
            terminalreporter.write_line(
                f"{v.kind:<5} {v.seconds:8.1f}s > {v.limit:.1f}s  {v.name}")
        terminalreporter.write_line(
            f"limits scale with {ENV_SCALE}; individual overrides: "
            + ", ".join(ENV_LIMIT.values()))
        return
    files = per_file(_state.durations)
    slow = max(files, key=files.get)
    terminalreporter.write_line(
        f"tui budget ok: slowest TUI file {slow} {files[slow]:.1f}s "
        f"(limit {_state.limits.file:.0f}s); {len(files)} files, "
        f"{sum(files.values()):.1f}s total (limit {_state.limits.total:.0f}s)")
    if _state.verbose > 0:
        for f in sorted(files, key=files.get, reverse=True):
            terminalreporter.write_line(f"  {files[f]:8.1f}s  {f}")
