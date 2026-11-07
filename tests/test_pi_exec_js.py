"""SPEC-0451: pytest gate for `tests/js/pi_exec.test.js` (node --test)."""
import shutil
import subprocess
import sys
from pathlib import Path

from waits import WAIT_S

ROOT = Path(__file__).resolve().parent.parent
JS_TEST = ROOT / "tests" / "js" / "pi_exec.test.js"
PI_EXEC = ROOT / "src" / "nightwright" / "agentic" / "pi_exec.mjs"


def test_pi_exec_under_node():
    node = shutil.which("node")
    assert node, "node is required to run tests/js/pi_exec.test.js"
    assert PI_EXEC.is_file()
    r = subprocess.run([node, "--test", str(JS_TEST)], cwd=ROOT, capture_output=True, text=True, timeout=WAIT_S)
    assert r.returncode == 0, f"node --test pi_exec.test.js failed:\n{r.stdout}\n{r.stderr}"


def test_pi_exec_js_edit_selects_this_module():
    r = subprocess.run(
        [sys.executable, "tools/affected_tests.py", "--changed", str(PI_EXEC), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=WAIT_S,
    )
    assert r.returncode == 0, r.stderr
    import json

    sel = json.loads(r.stdout)
    assert "tests/test_pi_exec_js.py" in sel.get("tests", [])
