"""Agent workflow skills (SPEC-0017): wt skills install/list, frontmatter validity, and the
thinness guard (skills name wt commands + defer to --help, never hardcode flag lists).

ALL installs here use a tmp --dest — never the real ~/.claude/skills."""
import json
import re
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from wt import skills as SK
from wt.cli import cli

ROOT = Path(__file__).resolve().parent.parent
REPO_SKILLS_DIR = ROOT / "skills"


# ---- repo skill assets ---------------------------------------------------------------

def _repo_skill_names():
    return sorted(p.name for p in REPO_SKILLS_DIR.iterdir()
                  if p.is_dir() and (p / "SKILL.md").is_file())


def _frontmatter(text):
    assert text.startswith("---\n"), "SKILL.md must start with a YAML frontmatter block"
    _, fm, body = text.split("---\n", 2)
    return yaml.safe_load(fm), body


EXPECTED_SKILLS = ["wt-capture", "wt-seed", "wt-new-work", "wt-generate", "wt-export",
                   "wt-orient", "wt-explore", "wt-rework", "wt-implement-spec", "wt-related",
                   "wt-verify", "wt-sheet-sync", "wt-conduct", "wt-steward"]


def test_expected_skills_exist():
    names = _repo_skill_names()
    assert names == sorted(EXPECTED_SKILLS)


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_frontmatter_has_name_and_description(name):
    text = (REPO_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    fm, _ = _frontmatter(text)
    assert fm.get("name") == name
    assert isinstance(fm.get("description"), str) and fm["description"].strip()


@pytest.mark.parametrize("name", EXPECTED_SKILLS)
def test_thinness_references_help_not_flag_lists(name):
    """Skills must name real wt commands and defer to `wt <cmd> --help` for exact syntax,
    rather than hardcoding a full flag list that will rot when the CLI changes."""
    text = (REPO_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    assert "wt" in text
    assert "--help" in text, f"{name}/SKILL.md never points at `wt <cmd> --help`"

    # No fenced code block may enumerate a flag list (3+ lines starting with `--flag`) --
    # that's the CLI's own --help output, and duplicating it here is exactly what rots.
    for block in re.findall(r"```(?:bash)?\n(.*?)```", text, re.S):
        flag_lines = [ln for ln in block.splitlines() if re.match(r"^\s*--[\w-]+", ln)]
        assert len(flag_lines) < 3, (
            f"{name}/SKILL.md hardcodes a flag-list block:\n{block}")


def test_every_skill_names_a_real_wt_command():
    """Loose guardrail: each skill's procedure should mention at least one `wt <subcommand>`
    invocation, so it's grounded in the actual CLI surface rather than being pure prose."""
    for name in _repo_skill_names():
        text = (REPO_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert re.search(r"`wt [a-z]", text), f"{name}/SKILL.md never names a `wt <cmd>`"


# ---- installer ------------------------------------------------------------------------

def test_repo_skills_matches_disk():
    names = {n for n, _ in SK.repo_skills()}
    assert names == set(_repo_skill_names())


def test_install_symlinks_by_default(tmp_path):
    dest = tmp_path / "dest"
    results = SK.install(dest=dest)
    assert {n for n, _ in results} == set(_repo_skill_names())
    for name in _repo_skill_names():
        target = dest / name
        assert target.is_symlink()
        assert (target / "SKILL.md").is_file()
        assert target.resolve() == (REPO_SKILLS_DIR / name).resolve()


def test_install_copy_mode(tmp_path):
    dest = tmp_path / "dest"
    results = SK.install(dest=dest, mode="copy")
    assert {a for _, a in results} == {"copied"}
    for name in _repo_skill_names():
        target = dest / name
        assert not target.is_symlink()
        assert target.is_dir()
        assert (target / "SKILL.md").is_file()
        marker = json.loads((target / ".wt-skill.json").read_text())
        assert marker["hash"]


def test_install_idempotent_symlink(tmp_path):
    dest = tmp_path / "dest"
    SK.install(dest=dest)
    results = SK.install(dest=dest)
    assert all(action == "up-to-date" for _, action in results)


def test_install_idempotent_copy(tmp_path):
    dest = tmp_path / "dest"
    SK.install(dest=dest, mode="copy")
    results = SK.install(dest=dest, mode="copy")
    assert all(action == "up-to-date" for _, action in results)


def test_install_refuses_to_clobber_non_wt_dir(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "wt-capture").mkdir()
    (dest / "wt-capture" / "other.txt").write_text("not ours")
    with pytest.raises(ValueError):
        SK.install(dest=dest)


def test_install_force_clobbers_non_wt_dir(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "wt-capture").mkdir()
    (dest / "wt-capture" / "other.txt").write_text("not ours")
    results = SK.install(dest=dest, force=True)
    action_by_name = dict(results)
    assert action_by_name["wt-capture"] in ("linked", "relinked")
    assert (dest / "wt-capture").is_symlink()


def test_install_switch_link_to_copy_updates(tmp_path):
    dest = tmp_path / "dest"
    SK.install(dest=dest, mode="link")
    # Switching modes on an existing wt-managed symlink should be allowed without --force.
    results = SK.install(dest=dest, mode="copy")
    action_by_name = dict(results)
    assert action_by_name["wt-capture"] in ("copied", "updated")
    assert (dest / "wt-capture").is_dir()
    assert not (dest / "wt-capture").is_symlink()


def test_status_reports_not_installed(tmp_path):
    dest = tmp_path / "dest"
    entries = {e["name"]: e["state"] for e in SK.status(dest=dest)}
    assert all(state == "not installed" for state in entries.values())
    assert set(entries) == set(_repo_skill_names())


def test_status_reports_linked_and_copied(tmp_path):
    link_dest = tmp_path / "link_dest"
    SK.install(dest=link_dest)
    linked = {e["name"]: e["state"] for e in SK.status(dest=link_dest)}
    assert all(state == "linked" for state in linked.values())

    copy_dest = tmp_path / "copy_dest"
    SK.install(dest=copy_dest, mode="copy")
    copied = {e["name"]: e["state"] for e in SK.status(dest=copy_dest)}
    assert all(state == "copied" for state in copied.values())


def test_status_reports_stale_copy(tmp_path):
    dest = tmp_path / "dest"
    SK.install(dest=dest, mode="copy")
    # Tamper with the marker hash to simulate the repo source having moved on.
    marker_path = dest / "wt-capture" / ".wt-skill.json"
    marker = json.loads(marker_path.read_text())
    marker["hash"] = "deadbeef"
    marker_path.write_text(json.dumps(marker))
    entries = {e["name"]: e["state"] for e in SK.status(dest=dest)}
    assert entries["wt-capture"] == "copied (stale)"


def test_status_reports_occupied_not_ours(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "wt-capture").mkdir()
    entries = {e["name"]: e["state"] for e in SK.status(dest=dest)}
    assert entries["wt-capture"] == "occupied (not ours)"


# ---- CLI ------------------------------------------------------------------------------

def test_cli_skills_install(tmp_path):
    dest = tmp_path / "dest"
    r = CliRunner().invoke(cli, ["skills", "install", "--dest", str(dest)])
    assert r.exit_code == 0, r.output
    for name in _repo_skill_names():
        assert name in r.output
        assert (dest / name).is_symlink()


def test_cli_skills_install_copy(tmp_path):
    dest = tmp_path / "dest"
    r = CliRunner().invoke(cli, ["skills", "install", "--copy", "--dest", str(dest)])
    assert r.exit_code == 0, r.output
    assert not (dest / "wt-capture").is_symlink()
    assert (dest / "wt-capture" / "SKILL.md").is_file()


def test_cli_skills_install_clobber_guard(tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "wt-capture").mkdir()
    (dest / "wt-capture" / "other.txt").write_text("not ours")
    r = CliRunner().invoke(cli, ["skills", "install", "--dest", str(dest)])
    assert r.exit_code != 0
    r2 = CliRunner().invoke(cli, ["skills", "install", "--dest", str(dest), "--force"])
    assert r2.exit_code == 0, r2.output
    assert (dest / "wt-capture").is_symlink()


def test_cli_skills_list(tmp_path):
    dest = tmp_path / "dest"
    r = CliRunner().invoke(cli, ["skills", "list", "--dest", str(dest)])
    assert r.exit_code == 0, r.output
    assert "not installed" in r.output

    CliRunner().invoke(cli, ["skills", "install", "--dest", str(dest)])
    r2 = CliRunner().invoke(cli, ["skills", "list", "--dest", str(dest)])
    assert r2.exit_code == 0, r2.output
    assert "linked" in r2.output


def test_skills_never_claim_docs_outbox_path():
    """SPEC-0051: live skills must not point agents at the retired docs/outbox/ store."""
    for name in _repo_skill_names():
        text = (REPO_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert "docs/outbox" not in text, f"{name} still mentions docs/outbox"


def test_wt_generate_is_internal_only():
    """SPEC-0051: generate skill must not instruct generating outbound / PROJ- ids."""
    text = (REPO_SKILLS_DIR / "wt-generate" / "SKILL.md").read_text(encoding="utf-8")
    fm, body = _frontmatter(text)
    assert "internal" in body.lower()
    assert "outbound" in body.lower()
    # Must not claim generate works for outbound specs as a happy path
    assert "or an outbound" not in body.lower()
    assert re.search(r"do\s+\*\*not\*\*\s+generate|do not generate", body, re.I)


def test_superpowers_coexistence_guidance_spec_0138():
    """SPEC-0138: orient + AGENTS teach wt lifecycle vs Superpowers tactics."""
    orient = (REPO_SKILLS_DIR / "wt-orient" / "SKILL.md").read_text(encoding="utf-8")
    assert "Coexistence with Superpowers" in orient
    assert "docs/superpowers" in orient
    assert "wt hub" in orient or "`wt hub`" in orient
    assert "/wt-verify" in orient or "wt-verify" in orient
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Superpowers" in agents
    assert "docs/superpowers" in agents
    for name in ("wt-seed", "wt-explore", "wt-new-work"):
        text = (REPO_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert "docs/superpowers" in text or "Superpowers" in text, name
    impl = (REPO_SKILLS_DIR / "wt-implement-spec" / "SKILL.md").read_text(encoding="utf-8")
    verify = (REPO_SKILLS_DIR / "wt-verify" / "SKILL.md").read_text(encoding="utf-8")
    assert "Superpowers" in impl or "wt-verify" in impl
    assert "Superpowers" in verify


def test_orient_carries_working_rules_and_session_skills_require_it():
    """Standing rules live in /wt-orient; conductor and steward must load it first."""
    orient = (REPO_SKILLS_DIR / "wt-orient" / "SKILL.md").read_text(encoding="utf-8")
    assert "Working rules" in orient
    assert "Decide, don't defer" in orient
    agents_md = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "Autonomy" in agents_md and "/wt-orient" in agents_md
    for phrase in ("worktree", "sub-agent", "missing", "heading"):
        assert phrase in orient, phrase
    for name in ("wt-steward", "wt-conduct"):
        text = (REPO_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        fm, body = _frontmatter(text)
        assert "/wt-orient" in fm["description"], name
        procedure = body.split("## Procedure", 1)[1]
        first = procedure.strip().splitlines()[0]
        assert first.startswith("0.") and "/wt-orient" in first, name
    conduct = (REPO_SKILLS_DIR / "wt-conduct" / "SKILL.md").read_text(encoding="utf-8")
    assert "Clear blockers yourself" in conduct
    assert "review_escalated" in conduct
    row = next(r for r in conduct.splitlines() if r.startswith("| `parked` `rebase_conflict`"))
    assert "park_undecided" in conduct                                       # SPEC-0222
    assert "resume to `CLOSING`" in row and "conflict_resolver" in row      # SPEC-0221
    assert "resume to `GATING`" not in row
    assert "follows merges by itself" in conduct and "code_changed" in conduct   # SPEC-0194
    for name in ("wt-orient", "wt-conduct"):
        text = (REPO_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        assert "fresh sub-agent" in text, name
        assert "Never use forks" in text, name


def test_conduct_skill_has_runner_draining_row_spec_0224():
    """SPEC-0224: the conductor table handles `runner_draining`, and `ready_not_admitted`
    points to it."""
    text = (REPO_SKILLS_DIR / "wt-conduct" / "SKILL.md").read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith("| `")]
    (draining,) = [r for r in rows if r.startswith("| `runner_draining`")]
    for word in ("waiting_on", "ready", "lease_stale"):
        assert word in draining, word
    (rna,) = [r for r in rows if r.startswith("| `ready_not_admitted`")]
    assert "`runner_draining: true`" in rna


def test_conduct_skill_has_runner_config_invalid_row_spec_0226():
    """SPEC-0226: the conductor table handles `runner_config_invalid` for both scopes."""
    text = (REPO_SKILLS_DIR / "wt-conduct" / "SKILL.md").read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith("| `")]
    (row,) = [r for r in rows if r.startswith("| `runner_config_invalid`")]
    assert "`scope: user`" in row and "do not edit their config" in row
    assert "`scope: project:<P>`" in row and "`config_invalid`" in row
    assert "steward" in row and "wt mailbox send" in row


@pytest.mark.parametrize("name", ["wt-conduct", "wt-steward"])
def test_monitor_owner_skills_handle_code_changed(name):
    """SPEC-0231: the Monitor owners re-arm on `code_changed` and re-read changed skills."""
    text = (REPO_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
    assert "code_changed" in text and "skills_changed" in text
    assert f"/{name}" in text and "/wt-orient" in text


def _conduct_rows():
    text = (REPO_SKILLS_DIR / "wt-conduct" / "SKILL.md").read_text(encoding="utf-8")
    return [ln for ln in text.splitlines() if ln.startswith("| `")]


def _conduct_row(name):
    (row,) = [r for r in _conduct_rows() if r.startswith(f"| `{name}`")]
    return row


def test_new_watchdog_rows_present():
    """SPEC-0228: the conductor acts at once on both new watchdog events."""
    wna = _conduct_row("waiting_not_admitted")
    assert "within one event" in wna and "SIGTERM" in wna and "`drain`" in wna
    silent = _conduct_row("runner_journal_silent")
    assert "immediately" in silent and "SIGTERM" in silent and "`busy`" in silent


def test_close_guard_in_rows():
    for name in ("waiting_not_admitted", "runner_journal_silent", "ready_not_admitted"):
        row = _conduct_row(name)
        assert "`busy.kind`" in row and "`open` or `close`" in row, name
        assert "never SIGTERM" in row, name


def test_rows_mention_new_fields():
    assert "`drain`" in _conduct_row("ready_not_admitted")
    ping = _conduct_row("conductor_ping")
    assert "waiting_not_admitted" in ping and "`runner`" in ping


def test_runner_draining_row_points_to_silent_row():
    row = _conduct_row("runner_draining")
    assert "`runner_journal_silent`" in row and "follow that row" in row


def test_conduct_skill_escalates_through_records_spec_0232():
    conduct = (REPO_SKILLS_DIR / "wt-conduct" / "SKILL.md").read_text(encoding="utf-8")
    assert "wt queue escalate" in conduct and "escalated ESC-NNNN" in conduct
    row = next(r for r in conduct.splitlines() if r.startswith("| `parked` `human_decision`"))
    assert "wt escalations withdraw" in row and "wt queue resume" in row
    assert any(r.startswith("| `escalation` / `escalation_withdrawn`")
               for r in conduct.splitlines())
