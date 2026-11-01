"""SPEC-0177: the committed `.nw/project.yaml` for nightwright itself loads under the SPEC-0170 loader
and declares the repo's two DoD gates with merge-to-main."""
from pathlib import Path

from nightwright.agentic import project as P

ROOT = Path(__file__).resolve().parent.parent


def test_dogfood_project_config_loads_and_names_the_gates():
    doc = P.load_project_config(ROOT / ".nw" / "project.yaml")
    assert doc["project"] == "Meta-Tools"
    assert doc["base"] == "main"
    gates = {g["name"]: g for g in doc["gates"]}
    assert set(gates) == {"pytest", "pytest-affected", "spec-lint", "docs-tests"}   # SPEC-0328 tiers, SPEC-0350
    assert gates["pytest"]["when"] == "close" and gates["pytest-affected"]["when"] == "loop"
    assert "spec-lint" not in {n for n, g in gates.items() if g.get("when")}   # always
    assert gates["pytest"]["cmd"].startswith("uv run pytest")
    assert gates["pytest"]["required"] and gates["spec-lint"]["required"]
    assert "spec_lint.py" in gates["spec-lint"]["cmd"]
    assert "--write-ledger" in gates["spec-lint"]["cmd"]   # regenerate, then lint
    assert doc["merge"]["policy"] == "merge"
    assert doc["merge"]["base"] == "main"
    assert doc["merge"]["push_after_merge"] is False
    assert "uv sync --extra tui" in doc["worktrees"]["setup"]
    assert "AGENTS.md" in doc["agents"]["context_files"]


def test_dogfood_project_config_resolves_through_the_registry(tmp_path):
    cfg = {"nw_checkout": str(ROOT), "outbox_targets": {}, "config_dir": str(tmp_path),
           "data_dir": str(tmp_path)}
    doc = P.project_config(cfg, "Meta-Tools")
    assert Path(doc["_path"]) == ROOT / ".nw" / "project.yaml"
    assert Path(doc["_root"]) == ROOT
