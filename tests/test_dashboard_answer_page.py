"""SPEC-0429 / SPEC-0430 / SPEC-0433: the gate's entry to tests/js/answer.test.js (the page's inbox answering
flow and campaign drilldown under node, on the fake DOM). Names `answer.test.js` and `app.js` so
`affected_tests.py` selects this file when either changes."""
import re
import shutil
import subprocess
from pathlib import Path

from waits import WAIT_S
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def test_answer_flow_and_drilldown_under_node():
    node = shutil.which("node")
    assert node, "node is required to run tests/js/answer.test.js"
    r = subprocess.run([node, "--test", str(ROOT / "tests" / "js" / "answer.test.js")], cwd=ROOT,
                       capture_output=True, text=True, timeout=WAIT_S)
    assert r.returncode == 0, f"node --test answer.test.js failed:\n{r.stdout}\n{r.stderr}"
    assert re.search(r"skipped\s+0\b", r.stdout) and re.search(r"pass\s+17\b", r.stdout)  # +4 SPEC-0433, +6 SPEC-0434


