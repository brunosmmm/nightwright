"""SPEC-0284: gate result parsing (pytest summary line, JUnit XML)."""
import pytest

from nightwright.agentic import gate_results as GR

pytestmark = pytest.mark.fast


def test_pytest_full_summary_line():
    log = ("FAILED tests/a.py::test_x - assert 0\nFAILED tests/a.py::test_y\n"
           "=== 2 failed, 120 passed, 3 skipped in 4.50s ===\n")
    s = GR.parse_pytest(log)
    assert (s["format"], s["failed"], s["passed"], s["skipped"], s["errors"]) == \
        ("pytest", 2, 120, 3, 0)
    assert s["failing"] == ["tests/a.py::test_x", "tests/a.py::test_y"]


def test_pytest_quiet_summary_line_with_duration_suffix():
    s = GR.parse_pytest("1 failed, 4 passed in 0.12s\n")
    assert (s["failed"], s["passed"]) == (1, 4)
    s = GR.parse_pytest("2 passed in 1.00s (0:00:01)\n")
    assert (s["failed"], s["passed"]) == (0, 2)


def test_pytest_xdist_summary_line():
    """SPEC-0309: `-n 4` keeps the final summary line in the serial format; the per-worker
    banner and progress lines around it must not be taken for counts."""
    log = ("created: 4/4 workers\n4 workers [4077 items]\n"
           "...s......................................................... [  1%]\n"
           "FAILED tests/a.py::test_x - assert 0\n"
           "=========== 1 failed, 4076 passed, 3 skipped in 301.22s (0:05:01) ===========\n")
    s = GR.parse_pytest(log)
    assert (s["failed"], s["passed"], s["skipped"], s["errors"]) == (1, 4076, 3, 0)
    assert s["failing"] == ["tests/a.py::test_x"]
    s = GR.parse_pytest("4077 passed, 3 skipped in 301.22s (0:05:01)\n")      # the `-q` form
    assert (s["failed"], s["passed"], s["skipped"]) == (0, 4077, 3)


@pytest.mark.parametrize("line,n", [("=== 1 error in 0.1s ===", 1),
                                    ("=== 2 errors in 0.1s ===", 2),
                                    ("1 passed, 2 errors in 0.1s", 2)])
def test_pytest_error_forms(line, n):
    assert GR.parse_pytest(line)["errors"] == n


def test_pytest_last_summary_wins_and_no_summary_raises():
    s = GR.parse_pytest("=== 1 failed in 1s ===\nnoise\n=== 3 passed in 2s ===\n")
    assert (s["failed"], s["passed"]) == (0, 3)
    with pytest.raises(ValueError, match="no pytest summary line"):
        GR.parse_pytest("Traceback\nboom\n")
    with pytest.raises(ValueError):
        GR.parse_pytest("=== no tests ran in 0.01s ===")


def test_pytest_failing_ids_error_dedupe_and_cap():
    lines = ["FAILED t.py::a - x", "ERROR t.py::b", "FAILED t.py::a - again"]
    lines += [f"FAILED t.py::t{i}" for i in range(80)]
    s = GR.parse_pytest("\n".join(lines) + "\n=== 82 failed in 1s ===\n")
    assert s["failing"][:3] == ["t.py::a", "t.py::b", "t.py::t0"]
    assert len(s["failing"]) == GR.MAX_FAILING == 50


JUNIT_SUITE = """<testsuite name="s" tests="5">
  <testcase classname="m.A" name="ok"/>
  <testcase classname="m.A" name="bad"><failure message="x"/></testcase>
  <testcase classname="m.B" name="boom"><error message="y"/></testcase>
  <testcase classname="m.B" name="skip"><skipped/></testcase>
  <testcase classname="m.B" name="ok2"/>
</testsuite>"""


def test_junit_testsuite_and_testsuites(tmp_path):
    f = tmp_path / "r.xml"
    f.write_text(JUNIT_SUITE)
    s = GR.parse_junit(f)
    assert (s["passed"], s["failed"], s["errors"], s["skipped"]) == (2, 1, 1, 1)
    assert s["failing"] == ["m.A::bad", "m.B::boom"]
    f.write_text(f"<testsuites>{JUNIT_SUITE}{JUNIT_SUITE}</testsuites>")
    s = GR.parse_junit(f)
    assert (s["passed"], s["failed"]) == (4, 2)


def test_junit_cap_and_size_limit(tmp_path):
    f = tmp_path / "r.xml"
    cases = "".join(f'<testcase classname="c" name="t{i}"><failure/></testcase>' for i in range(70))
    f.write_text(f"<testsuite>{cases}</testsuite>")
    s = GR.parse_junit(f)
    assert s["failed"] == 70 and len(s["failing"]) == 50
    f.write_text(JUNIT_SUITE)
    with pytest.raises(ValueError, match="larger than"):
        GR.MAX_JUNIT_BYTES, old = 10, GR.MAX_JUNIT_BYTES
        try:
            GR.parse_junit(f)
        finally:
            GR.MAX_JUNIT_BYTES = old


def test_parse_dispatch_and_error_form(tmp_path):
    log = tmp_path / "g.log"
    log.write_text("garbage\n")
    assert GR.parse(None, log, tmp_path) is None
    err = GR.parse({"format": "pytest"}, log, tmp_path)
    assert err == {"format": "pytest", "error": "no pytest summary line"}
    (tmp_path / "bad.xml").write_text("<testsuite><oops")
    err = GR.parse({"format": "junit", "path": "bad.xml"}, log, tmp_path)
    assert err["format"] == "junit" and err["error"] and "failed" not in err
    err = GR.parse({"format": "junit", "path": "missing.xml"}, log, tmp_path)
    assert err["error"]
    (tmp_path / "ok.xml").write_text(JUNIT_SUITE)
    assert GR.parse({"format": "junit", "path": "ok.xml"}, log, tmp_path)["failed"] == 1


def test_pytest_xfail_xpass_warnings_are_not_counted():
    s = GR.parse_pytest("=== 1 failed, 2 passed, 3 xfailed, 1 xpassed, 4 warnings in 0.1s ===")
    assert (s["failed"], s["passed"], s["errors"], s["skipped"]) == (1, 2, 0, 0)


def test_junit_stale_file_is_refused(tmp_path):
    f = tmp_path / "r.xml"
    f.write_text("<testsuite><testcase classname='c' name='t'/></testsuite>")
    mtime = f.stat().st_mtime_ns
    assert GR.parse_junit(f, mtime)["passed"] == 1
    with pytest.raises(ValueError, match="stale"):
        GR.parse_junit(f, mtime + 1)
    assert "stale" in GR.parse({"format": "junit", "path": "r.xml"}, tmp_path / "x.log",
                               tmp_path, mtime + 1)["error"]

