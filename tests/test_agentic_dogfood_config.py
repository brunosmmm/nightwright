"""SPEC-0177: the committed `.nw/project.yaml` for nightwright itself loads under the SPEC-0170 loader
and declares the repo's two DoD gates with merge-to-main."""
from pathlib import Path

from nightwright.agentic import project as P
import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def test_dogfood_project_config_loads_and_names_the_gates():
    doc = P.load_project_config(ROOT / ".nw" / "project.yaml")
    assert doc["project"] == "Meta-Tools"
    assert doc["base"] == "main"
    gates = {g["name"]: g for g in doc["gates"]}
    assert set(gates) == {"pytest", "pytest-affected", "spec-lint", "docs-tests"}   # SPEC-0328 tiers, SPEC-0350
    assert gates["pytest"]["when"] == "close" and gates["pytest-affected"]["when"] == "loop"
    assert "spec-lint" not in {n for n, g in gates.items() if g.get("when")}   # always
    assert gates["pytest-affected"]["failed_first"] is True          # SPEC-0358
    assert "failed_first" not in gates["pytest"]
    assert gates["pytest"]["reuse_from"] == "pytest-affected"     # SPEC-0359
    assert "reuse_from" not in gates["pytest-affected"]
    assert not doc.get("close", {}).get("force_full")              # SPEC-0359: no escape hatch
    # SPEC-0378: the close gate runs the affected selection, never the bare full suite
    assert gates["pytest"]["cmd"].startswith("python3 tools/affected_tests.py")
    assert "--exclude-marks integration,tui,remote,perf" in gates["pytest-affected"]["cmd"]
    assert "--exclude-marks integration,tui,remote,perf" in gates["pytest"]["cmd"]
    assert '-m "not (integration or tui or remote or perf) or tree_guard"' in gates["pytest"]["retry_cmd"]
    assert "--run uv run pytest" in gates["pytest"]["cmd"]
    assert gates["pytest"]["retries"] == 2 and "{failing}" in gates["pytest"]["retry_cmd"]
    assert gates["pytest"]["required"] and gates["spec-lint"]["required"]
    assert "spec_lint.py" in gates["spec-lint"]["cmd"]
    assert "--write-ledger" in gates["spec-lint"]["cmd"]   # regenerate, then lint
    assert doc["merge"]["policy"] == "merge"
    assert doc["merge"]["base"] == "main"
    assert doc["merge"]["push_after_merge"] is False
    assert doc["worktrees"]["setup"] == ["uv sync"]          # SPEC-0368: textual is core
    assert "AGENTS.md" in doc["agents"]["context_files"]


def test_dogfood_project_config_resolves_through_the_registry(tmp_path):
    cfg = {"nw_checkout": str(ROOT), "outbox_targets": {}, "config_dir": str(tmp_path),
           "data_dir": str(tmp_path)}
    doc = P.project_config(cfg, "Meta-Tools")
    assert Path(doc["_path"]) == ROOT / ".nw" / "project.yaml"
    assert Path(doc["_root"]) == ROOT


def test_agentic_capability_doc_names_the_reuse_knobs():
    """SPEC-0359: the close-tier reuse knobs and the sentinel CLI are documented."""
    text = (ROOT / "docs" / "product" / "capabilities" / "agentic.md").read_text()
    for needle in ("reuse_from", "close.force_full", "NW_CLOSE_FULL",
                   "nw queue sentinel run", "nw queue sentinel clear"):
        assert needle in text, needle


