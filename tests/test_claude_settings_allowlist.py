"""SPEC-0496: the committed `.claude/settings.json` lets worktree sub-agents commit."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOW = json.loads((ROOT / ".claude" / "settings.json").read_text())["permissions"]["allow"]

REQUIRED = ["git add", "git commit", "git rebase", "git switch -c", "git checkout -b",
            "git restore --staged", "git status", "git diff", "git log", "nw", "uv run"]
FORBIDDEN = ["git push", "git reset", "git -C", "git stash", "git *", "git branch -D", "*"]


def _bodies():
    assert all(e.startswith("Bash(") and e.endswith(")") for e in ALLOW)
    return [e[5:-1] for e in ALLOW]


def test_required_prefixes_allowed():                                  # SPEC-0496 AC1
    bodies = _bodies()
    for req in REQUIRED:
        assert any(b == req or b.startswith(req + " ") for b in bodies), req


def test_dangerous_forms_not_allowed():                                # SPEC-0496 AC2
    bodies = _bodies()
    for bad in FORBIDDEN:
        assert not any(b == bad or b.startswith(bad + " ") for b in bodies), bad


def test_skills_document_worktree_contract():                          # SPEC-0496 AC3
    for name in ("nw-orient", "nw-conduct"):
        text = (ROOT / "skills" / name / "SKILL.md").read_text()
        assert "isolation: worktree" in text, name
    assert "permission denial" in (ROOT / "skills/nw-conduct/SKILL.md").read_text()
