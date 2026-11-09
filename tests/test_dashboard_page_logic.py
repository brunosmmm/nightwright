"""SPEC-0413: the gate's entry to the page's node tests (pytest does not run `.js` files).
Runs `node --test tests/js/page_logic.test.js` with the real snapshot builder's output on the
SPEC-0401 fixture handed over as JSON. Imports `assets` and names `app.js`, `fakedom.js` and
`page_logic.test.js` below, so `affected_tests.py` selects this file when `assets.py`, a file
under `static/` or a file under `tests/js/` changes."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from fixtures.dashboard_data import seed
from nightwright.agentic import cli_dashboard
from nightwright.agentic.dashboard import assets
from nightwright.agentic.dashboard import snapshot as SN
from waits import WAIT_S
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent
NODE_TESTS = ("page_logic.test.js", "fakedom.js")          # tests/js/
PAGE_SOURCE = "app.js"                                      # .../dashboard/static/
NODE_DRIVERS = ("tests/test_dashboard_page_logic.py", "tests/test_dashboard_page.py")


def test_page_logic_under_node(tmp_path):
    node = shutil.which("node")
    assert node, "node is required to run tests/js/page_logic.test.js (the page's JS logic tests)"
    assert assets.read_asset("/app.js") is not None
    doc = SN.build(seed(tmp_path)["cfg"])
    fixture = tmp_path / "snapshot.json"
    fixture.write_text(json.dumps({"snapshot": doc, "counts_line": cli_dashboard.text_lines(doc)[1]},
                                  default=str))
    env = {"NW_DASH_FIXTURE": str(fixture), "PATH": str(Path(node).parent)}
    r = subprocess.run([node, "--test", str(ROOT / "tests" / "js" / NODE_TESTS[0])], cwd=ROOT,
                       env=env, capture_output=True, text=True, timeout=WAIT_S)
    assert r.returncode == 0, f"node --test {NODE_TESTS[0]} failed:\n{r.stdout}\n{r.stderr}"
    assert re.search(r"skipped\s+0\b", r.stdout), "the real-snapshot test must run, not skip"


def test_an_app_js_edit_selects_the_node_driving_tests():
    r = subprocess.run([sys.executable, "tools/affected_tests.py", "--changed",
                        f"src/nightwright/agentic/dashboard/static/{PAGE_SOURCE}", "--json"],
                       cwd=ROOT, capture_output=True, text=True, timeout=WAIT_S)
    assert r.returncode == 0, r.stderr
    sel = json.loads(r.stdout)
    assert sel["full"] or set(NODE_DRIVERS) <= set(sel["tests"]), sel


