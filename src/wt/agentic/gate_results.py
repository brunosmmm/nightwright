"""Gate result parsing (SPEC-0284): counts and failing test ids from a gate's own output.

A gate opts in with `results:` in `.wt/project.yaml`: `{format: pytest}` reads the gate log,
`{format: junit, path: <file>}` reads a JUnit XML file relative to the gate's cwd. The summary
is `{format, passed, failed, errors, skipped, failing}`; a parser that cannot read its input
yields `{format, error}` and never changes whether the gate passed."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

FORMATS = ("pytest", "junit")
MAX_FAILING = 50
MAX_JUNIT_BYTES = 20 * 1024 * 1024

_SUMMARY_RE = re.compile(r"^=*\s*(?P<body>.*?) in \d+(?:\.\d+)?s(?: \(\d+:\d+:\d+\))?\s*=*$")
_COUNT_RE = re.compile(r"(\d+) (passed|failed|errors?|skipped)")
_FAILING_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


def _summary(fmt: str, passed: int, failed: int, errors: int, skipped: int,
             failing: list[str]) -> dict:
    return {"format": fmt, "passed": passed, "failed": failed, "errors": errors,
            "skipped": skipped, "failing": failing[:MAX_FAILING]}


def parse_pytest(text: str) -> dict:
    """Counts from the last pytest summary line (`=== 1 failed, 2 passed in 0.1s ===` or the
    `-q` form); failing ids from the `FAILED`/`ERROR` lines of the short test summary."""
    body = None
    for line in text.splitlines():
        m = _SUMMARY_RE.match(line.strip())
        if m and _COUNT_RE.search(m.group("body")):
            body = m.group("body")
    if body is None:
        raise ValueError("no pytest summary line")
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for n, word in _COUNT_RE.findall(body):
        counts["errors" if word.startswith("error") else word] += int(n)
    failing = list(dict.fromkeys(_FAILING_RE.findall(text)))
    return _summary("pytest", counts["passed"], counts["failed"], counts["errors"],
                    counts["skipped"], failing)


def parse_junit(path) -> dict:
    """Counts from the `<testcase>` elements of a JUnit XML file (`testsuite` or
    `testsuites` root); failing ids are `classname::name`."""
    p = Path(path)
    if p.stat().st_size > MAX_JUNIT_BYTES:
        raise ValueError(f"junit file larger than {MAX_JUNIT_BYTES // (1024 * 1024)} MB")
    root = ET.parse(p).getroot()
    if root.tag not in ("testsuite", "testsuites"):
        raise ValueError(f"not a junit file: root element <{root.tag}>")
    passed = failed = errors = skipped = 0
    failing: list[str] = []
    for case in root.iter("testcase"):
        tid = "::".join(x for x in (case.get("classname"), case.get("name")) if x)
        if case.find("failure") is not None:
            failed += 1
            failing.append(tid)
        elif case.find("error") is not None:
            errors += 1
            failing.append(tid)
        elif case.find("skipped") is not None:
            skipped += 1
        else:
            passed += 1
    return _summary("junit", passed, failed, errors, skipped,
                    list(dict.fromkeys(failing)))


def parse(results_cfg, log_path, cwd) -> dict | None:
    """Summary for one gate, `{format, error}` when it cannot be read, None without `results`."""
    if not results_cfg:
        return None
    fmt = results_cfg.get("format")
    try:
        if fmt == "pytest":
            return parse_pytest(Path(log_path).read_text(encoding="utf-8", errors="replace"))
        if fmt == "junit":
            return parse_junit(Path(cwd) / results_cfg["path"])
        raise ValueError(f"unknown results format {fmt!r}")
    except Exception as e:      # a parser must never change whether the gate passed
        return {"format": fmt, "error": str(e) or type(e).__name__}
