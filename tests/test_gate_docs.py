"""SPEC-0383: `nw gate run` stays discoverable by agents (skill, AGENTS.md, CLI help)."""
from pathlib import Path

from click.testing import CliRunner

from nightwright.cli import cli

ROOT = Path(__file__).resolve().parent.parent


def test_orient_skill_documents_gate_run():
    t = (ROOT / "skills" / "nw-orient" / "SKILL.md").read_text(encoding="utf-8")
    assert "nw gate run" in t and "SPEC-0381" in t
    assert "130 cancelled" in t and "never falls back" in t


def test_agents_md_points_at_gate_run():
    t = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "nw gate run" in t and "SPEC-0381" in t


def test_cli_help_lists_gate_and_exit_codes():
    r = CliRunner().invoke(cli, ["--help"])
    assert any(ln.strip().startswith("gate ") for ln in r.output.splitlines())
    h = CliRunner().invoke(cli, ["gate", "run", "--help"]).output
    for needle in ("130 cancelled", "box unavailable", "Example:"):
        assert needle in h
