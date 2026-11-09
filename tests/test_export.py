"""Outbound spec store + cross-project export (SPEC-0015): scaffold_outbound (namespace,
numbering, idea prefill + reciprocal link), validate_outbound, write_outbox_index, export_spec
(portable spec + consumption contract + provenance, non-clobber guard), and the CLI.

Everything here uses a TMP outbox dir (cfg["outbox_dir"]) and a TMP --to target — never the
real docs/outbox/. A dedicated test also asserts the internal tools/spec_lint.py scan of the
real docs/specs/ is unaffected by anything under docs/outbox/."""
import importlib.util
import pathlib
import shutil
import subprocess

from click.testing import CliRunner
from zoneinfo import ZoneInfo

from nightwright import export as E
from nightwright import org_write as W
from nightwright import specs as S
from nightwright.cli import cli
from nightwright.org import load_tasks
import pytest

pytestmark = pytest.mark.fast

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_SPECS = ROOT / "docs" / "specs"
LINT = ROOT / "tools" / "spec_lint.py"


def _linter():
    spec = importlib.util.spec_from_file_location("spec_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cfg(tmp_path, outbox_targets=None):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    shutil.copy(REAL_SPECS / "TEMPLATE.md", specs_dir / "TEMPLATE.md")
    outbox = tmp_path / "outbox"
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                                  "SHIPPED", "DROPPED", "RESEARCHED"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ideas.org"), "specs_dir": str(specs_dir),
            "outbox_dir": str(outbox), "outbox_targets": outbox_targets or {}}


def _find(cfg, sub):
    return next(t for t in load_tasks(cfg) if sub in t.heading)


# ---- scaffold_outbound ----------------------------------------------------------

def test_scaffold_outbound_creates_lint_valid_spec(tmp_path):
    cfg = _cfg(tmp_path)
    path, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")

    assert path.exists()
    assert outbound_id == "ACME-0001"
    assert path.parent == pathlib.Path(cfg["outbox_dir"]) / "acme"
    text = path.read_text()
    assert "id: ACME-0001" in text
    assert "title: \"Adversarial reviewer\"" in text
    assert "target_project: acme" in text
    assert "target_repo: acme" in text
    assert "status: draft" in text

    problems = S.validate_outbound([path])
    assert problems == [], problems


def test_scaffold_outbound_uses_configured_target_repo(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"repo_path": "~/work/acme"}})
    path, _ = S.scaffold_outbound(cfg, "acme", title="Something")
    assert "target_repo: ~/work/acme" in path.read_text()


def test_scaffold_outbound_freezes_target_spec_path(tmp_path):
    """SPEC-0065: the resolved absolute portable-spec path is frozen at scaffold time, not
    just the repo — so spec_dir drift can't silently redirect it either."""
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    path, _ = S.scaffold_outbound(cfg, "acme", title="Something")
    expected = str(tmp_path / "target-repo" / "docs" / "specs" / path.name)
    assert f"target_spec_path: {expected}" in path.read_text()


def test_scaffold_outbound_numbering_is_per_project(tmp_path):
    cfg = _cfg(tmp_path)
    _, id1 = S.scaffold_outbound(cfg, "acme", title="First")
    _, id2 = S.scaffold_outbound(cfg, "acme", title="Second")
    _, id3 = S.scaffold_outbound(cfg, "othrepo", title="Other project first")
    assert (id1, id2, id3) == ("ACME-0001", "ACME-0002", "OTHREPO-0001")


def test_scaffold_outbound_requires_title_or_idea(tmp_path):
    cfg = _cfg(tmp_path)
    try:
        S.scaffold_outbound(cfg, "acme")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "title is required" in str(e)


def test_scaffold_outbound_quotes_title_with_colon_and_index_loads(tmp_path):
    """Titles with `:` must not break yaml.safe_load / write_outbox_index (SPEC-0038)."""
    from nightwright.specmeta import split_frontmatter
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "filters for session triage", tags=("session-triage",))
    path, outbound_id = S.scaffold_outbound(
        cfg, "acme", from_idea="filters for session triage",
        title="Cascading filters: product then branch")
    text = path.read_text()
    fm, _ = split_frontmatter(text)
    assert fm["title"] == "Cascading filters: product then branch"
    assert "title: \"Cascading filters: product then branch\"" in text
    # hyphenated tag stripped from heading → not stuck in default title either
    idea = _find(cfg, "filters for session triage")
    assert "session-triage" in idea.tags
    assert ":session-triage:" not in idea.heading
    # regenerating index must not raise ScannerError
    S.write_outbox_index(cfg)
    idx = pathlib.Path(cfg["outbox_dir"]) / "INDEX.md"
    assert outbound_id in idx.read_text()


def test_scaffold_outbound_from_idea_prefills_and_links_back(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "adversarial reviewer")
    idea = _find(cfg, "adversarial reviewer")
    text = open(idea.file).read()
    text = text.replace("* IDEA adversarial reviewer\n",
                        "* IDEA adversarial reviewer\n  Some notes about the reviewer.\n")
    open(idea.file, "w").write(text)

    path, outbound_id = S.scaffold_outbound(cfg, "acme", from_idea="adversarial reviewer")
    assert "Some notes about the reviewer." in path.read_text()
    assert "source_idea:" in path.read_text()

    idea = _find(cfg, "adversarial reviewer")
    assert idea.properties.get("SPEC") == outbound_id
    assert idea.state == "SPECCED"


def test_scaffold_outbound_from_idea_mirrors_target_properties(tmp_path):
    """SPEC-0066: the source idea gets TARGET_REPO/TARGET_SPEC_PATH mirrored, read-only display,
    matching the outbound spec's own frozen frontmatter (SPEC-0065)."""
    from nightwright.specmeta import split_frontmatter
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    W.add_idea(cfg, "adversarial reviewer")
    path, outbound_id = S.scaffold_outbound(cfg, "acme", from_idea="adversarial reviewer")
    fm, _ = split_frontmatter(path.read_text())

    idea = _find(cfg, "adversarial reviewer")
    assert idea.properties.get("TARGET_REPO") == fm["target_repo"]
    assert idea.properties.get("TARGET_SPEC_PATH") == fm["target_spec_path"]


def test_scaffold_outbound_rejects_non_idea(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "a regular task")
    try:
        S.scaffold_outbound(cfg, "acme", from_idea="a regular task")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "not an idea" in str(e)


# ---- validate_outbound / write_outbox_index --------------------------------------

def test_validate_outbound_detects_missing_section_and_frontmatter(tmp_path):
    p = tmp_path / "PROJ-0001-x.md"
    p.write_text("---\nid: PROJ-0001\ntitle: X\nstatus: draft\nowner: user\n"
                "created: 2026-07-16\n---\n\n## Context\n\nsomething\n")
    problems = S.validate_outbound([p])
    assert any("target_project" in x for x in problems)
    assert any("target_repo" in x for x in problems)
    assert any("Acceptance criteria" in x for x in problems)
    assert any("Test plan" in x for x in problems)


def test_write_outbox_index_lists_by_project(tmp_path):
    cfg = _cfg(tmp_path)
    S.scaffold_outbound(cfg, "acme", title="First")
    S.scaffold_outbound(cfg, "othrepo", title="Other project first")

    index_path = pathlib.Path(cfg["outbox_dir"]) / "INDEX.md"
    assert index_path.exists()
    text = index_path.read_text()
    assert "acme" in text and "othrepo" in text
    assert "ACME-0001" in text
    assert "OTHREPO-0001" in text
    assert S.OUTBOX_BEGIN in text and S.OUTBOX_END in text


# ---- export_spec ------------------------------------------------------------------

def test_export_spec_writes_portable_spec_contract_and_provenance(tmp_path):
    cfg = _cfg(tmp_path)
    src_path, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    target = tmp_path / "target-repo"

    dest_spec, dest_contract = E.export_spec(cfg, outbound_id, to=str(target))

    assert dest_spec == target / "docs" / "specs" / src_path.name
    assert dest_spec.exists()
    assert dest_contract.exists()
    assert dest_contract.name.endswith(".contract.md")

    spec_text = dest_spec.read_text()
    assert f"id: {outbound_id}" in spec_text
    assert "target_project" not in spec_text          # trimmed: nightwright-internal routing field
    assert "source_outbound_id" in spec_text
    assert "Adversarial reviewer" in spec_text
    assert "## Context" in spec_text

    contract_text = dest_contract.read_text()
    assert outbound_id in contract_text
    assert "consumption" in contract_text.lower() or "consuming" in contract_text.lower()
    assert "/nw-implement-spec" in contract_text
    assert "nw spec generate" in contract_text.lower()  # forbid generate on outbound

    provenance = pathlib.Path(cfg["outbox_dir"]) / "acme" / "PROVENANCE.md"
    assert provenance.exists()
    assert outbound_id in provenance.read_text()
    assert str(dest_spec) in provenance.read_text()


def test_export_spec_reexport_identical_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    _, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    target = tmp_path / "target-repo"

    dest1, _ = E.export_spec(cfg, outbound_id, to=str(target))
    dest2, _ = E.export_spec(cfg, outbound_id, to=str(target))
    assert dest1 == dest2
    assert dest1.exists()


def test_export_spec_refuses_to_clobber_divergent_dest_without_force(tmp_path):
    cfg = _cfg(tmp_path)
    _, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    target = tmp_path / "target-repo"
    dest_spec, _ = E.export_spec(cfg, outbound_id, to=str(target))

    # simulate the foreign repo having a divergent copy (different provenance)
    dest_spec.write_text(dest_spec.read_text().replace(
        "source_content_hash:", "source_content_hash: bogus\n# was:"))

    try:
        E.export_spec(cfg, outbound_id, to=str(target))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "different provenance" in str(e)

    # --force overwrites
    dest_spec2, _ = E.export_spec(cfg, outbound_id, to=str(target), force=True)
    assert dest_spec2 == dest_spec
    assert "bogus" not in dest_spec.read_text()


def test_export_spec_unknown_id_raises(tmp_path):
    cfg = _cfg(tmp_path)
    try:
        E.export_spec(cfg, "NOPE-0001", to=str(tmp_path / "target"))
        assert False, "expected ValueError"
    except ValueError as e:
        assert "no outbound spec found" in str(e)


def test_export_spec_uses_configured_target_when_no_to(tmp_path):
    target = tmp_path / "configured-target"
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"repo_path": str(target),
                                                      "spec_dir": "specs"}})
    _, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    dest_spec, dest_contract = E.export_spec(cfg, outbound_id)
    assert dest_spec.parent == target / "specs"


def test_export_advances_source_idea_to_exported(tmp_path):
    """SPEC-0041: successful export sets linked idea EXPORTED (not PROMOTED)."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "hand off externally")
    path, outbound_id = S.scaffold_outbound(cfg, "acme", from_idea="hand off externally")
    # Fill enough for nw-native validate (scaffold template placeholders are enough).
    target = tmp_path / "target-repo"
    E.export_spec(cfg, outbound_id, to=str(target))
    idea = _find(cfg, "hand off externally")
    assert idea.state == "EXPORTED"
    assert idea.properties.get("SPEC") == outbound_id


def test_export_preserves_dest_status_on_reexport(tmp_path):
    """SPEC-0041: re-export with same body hash keeps foreign agent's portable status."""
    from nightwright.specmeta import split_frontmatter
    cfg = _cfg(tmp_path)
    _, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    target = tmp_path / "target-repo"
    dest_spec, _ = E.export_spec(cfg, outbound_id, to=str(target))
    text = dest_spec.read_text()
    dest_spec.write_text(text.replace("status: draft", "status: in-progress", 1))
    E.export_spec(cfg, outbound_id, to=str(target))
    fm, _ = split_frontmatter(dest_spec.read_text())
    assert fm["status"] == "in-progress"
    # --force replaces with outbox status
    E.export_spec(cfg, outbound_id, to=str(target), force=True)
    fm2, _ = split_frontmatter(dest_spec.read_text())
    assert fm2["status"] == "draft"


def test_pull_status_done_advances_exported_idea_to_shipped(tmp_path):
    """SPEC-0135: first pull of portable `done` moves EXPORTED → SHIPPED (and archives)."""
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    W.add_idea(cfg, "finish me externally")
    _, outbound_id = S.scaffold_outbound(cfg, "acme", from_idea="finish me externally")
    dest_spec, _ = E.export_spec(cfg, outbound_id)
    assert _find(cfg, "finish me externally").state == "EXPORTED"
    dest_spec.write_text(dest_spec.read_text().replace("status: draft", "status: done", 1))
    E.pull_status(cfg, outbound_id)
    idea = _find(cfg, "finish me externally")
    assert idea.state == "SHIPPED"
    assert "ideas-archive" in idea.file or "archive" in idea.file


def test_pull_status_mirrors_portable_into_outbox(tmp_path):
    """SPEC-0041: pull-status copies dest status onto the outbox copy."""
    from nightwright.specmeta import split_frontmatter
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    src_path, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    dest_spec, _ = E.export_spec(cfg, outbound_id)
    dest_spec.write_text(dest_spec.read_text().replace("status: draft", "status: in-progress", 1))
    out_path, status = E.pull_status(cfg, outbound_id)
    assert status == "in-progress"
    assert out_path == src_path
    fm, _ = split_frontmatter(src_path.read_text())
    assert fm["status"] == "in-progress"


def test_pull_status_resolves_via_frozen_path_after_config_repointed(tmp_path):
    """SPEC-0065: repointing outbox_targets after export must not redirect where pull-status
    looks for an already-exported spec — the frozen target_spec_path wins."""
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    src_path, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    dest_spec, _ = E.export_spec(cfg, outbound_id)
    dest_spec.write_text(dest_spec.read_text().replace("status: draft", "status: in-progress", 1))

    # simulate the repo being repointed in config after the spec was already exported
    cfg["outbox_targets"]["acme"]["repo_path"] = str(tmp_path / "moved-repo")

    out_path, status = E.pull_status(cfg, outbound_id)
    assert status == "in-progress"
    assert out_path == src_path


def test_portable_dest_path_prefers_frozen_field(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "live-repo"), "spec_dir": "docs/specs"},
    })
    frozen = tmp_path / "frozen-repo" / "docs" / "specs" / "ACME-0001-x.md"
    fm = {"target_project": "acme", "target_spec_path": str(frozen)}
    dest = E._portable_dest_path(cfg, pathlib.Path("ACME-0001-x.md"), fm)
    assert dest == frozen


def test_portable_dest_path_falls_back_without_frozen_field(tmp_path):
    """Outbound specs scaffolded before SPEC-0065 have no target_spec_path — unchanged
    live-config derivation applies."""
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "live-repo"), "spec_dir": "docs/specs"},
    })
    fm = {"target_project": "acme"}
    dest = E._portable_dest_path(cfg, pathlib.Path("ACME-0001-x.md"), fm)
    assert dest == tmp_path / "live-repo" / "docs" / "specs" / "ACME-0001-x.md"


def test_portable_dest_path_to_override_wins_over_frozen_field(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "live-repo"), "spec_dir": "docs/specs"},
    })
    frozen = tmp_path / "frozen-repo" / "docs" / "specs" / "ACME-0001-x.md"
    fm = {"target_project": "acme", "target_spec_path": str(frozen)}
    dest = E._portable_dest_path(cfg, pathlib.Path("ACME-0001-x.md"), fm,
                                 to=str(tmp_path / "override-repo"))
    assert dest == tmp_path / "override-repo" / "docs" / "specs" / "ACME-0001-x.md"


def test_default_config_includes_exported_keyword():
    from nightwright.config import DEFAULT_CONFIG
    kw = DEFAULT_CONFIG["org_idea_keywords"]
    assert "EXPORTED" in kw
    assert "SHIPPED" in kw
    assert kw.index("PROMOTED") < kw.index("EXPORTED") < kw.index("SHIPPED") < kw.index("DROPPED")
    assert "|" in kw


# ---- internal ledger/lint isolation -----------------------------------------------

def test_internal_spec_lint_unaffected_by_outbox(tmp_path):
    """The real docs/outbox/ (if present) must never be scanned by tools/spec_lint.py; the
    real docs/specs/ scan must be identical regardless of anything scaffolded here."""
    mod = _linter()
    specs = mod.load_specs()
    problems = mod.validate(specs)
    assert problems == [], problems
    # every scanned spec is directly under docs/specs (the linter never reaches into an outbox)
    assert all(pathlib.Path(s["path"]).parent.name == "specs" for s in specs)


# ---- CLI ----------------------------------------------------------------------------

def test_cli_spec_new_target_outbound(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "new", "--target", "acme",
                                "--title", "Adversarial reviewer"])
    assert r.exit_code == 0, r.output
    assert "ACME-0001" in r.output


def test_cli_spec_new_requires_from_idea_without_target(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "new"])
    assert r.exit_code != 0


def test_cli_spec_export(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r1 = CliRunner().invoke(cli, ["spec", "new", "--target", "acme",
                                 "--title", "Adversarial reviewer"])
    assert r1.exit_code == 0, r1.output
    import re
    outbound_id = re.search(r"ACME-\d{4}", r1.output).group(0)

    target = tmp_path / "target-repo"
    r2 = CliRunner().invoke(cli, ["spec", "export", outbound_id, "--to", str(target)])
    assert r2.exit_code == 0, r2.output
    assert "exported" in r2.output

    r3 = CliRunner().invoke(cli, ["spec", "export", "NOPE-0001", "--to", str(target)])
    assert r3.exit_code != 0


def test_outbox_defaults_to_data_dir_not_repo():
    """SPEC-0021: the outbox lives under the nightwright data dir (user data), never the nightwright repo/cwd."""
    d = S.outbox_dir({"data_dir": "/some/data"})
    assert d == pathlib.Path("/some/data/outbox")
    assert "docs" not in d.parts and d.parts[-1] == "outbox"


# ---- pull_clock (SPEC-0063) --------------------------------------------------------

def test_pull_clock_reconciles_markdown_log_onto_linked_idea(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    W.add_idea(cfg, "linked idea")
    _src_path, outbound_id = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001",
                                                  title="Adversarial reviewer")
    dest_spec, _ = E.export_spec(cfg, outbound_id)
    text = dest_spec.read_text()
    text = text.replace(
        "## Clock Log\n\n<Supplemental wall-time record for implementation sessions "
        "(SPEC-0060/0062). For internal\nwork, prefer `nw idea clock-in`/`nw idea clock-out` "
        "on the linked idea instead — this section\nis mainly for outbound/foreign-repo "
        "implementation, where no org access exists. One\nCLOCK-IN/CLOCK-OUT pair per "
        "session:>\n\nCLOCK-IN: [timestamp]\nCLOCK-OUT: [timestamp]",
        "## Clock Log\n\n"
        "CLOCK-IN: [2026-07-25T09:00:00]\n"
        "CLOCK-OUT: [2026-07-25T10:30:00]\n")
    dest_spec.write_text(text)

    idea_id, added = E.pull_clock(cfg, outbound_id)
    assert idea_id == "IDEA-001"
    assert added == 1
    idea = _find(cfg, "linked idea")
    assert len(idea.clock) == 1
    assert idea.clock[0][0] == __import__("datetime").datetime(2026, 7, 25, 9, 0)

    # idempotent re-run
    _idea_id2, added2 = E.pull_clock(cfg, outbound_id)
    assert added2 == 0


def test_pull_clock_reconciles_unbracketed_timestamps(tmp_path):
    """Bug found against a real portable spec (DEMO-0001): agents in practice write
    plain, unbracketed CLOCK-IN/CLOCK-OUT timestamps and pull_clock used to silently report
    0 added for them."""
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    W.add_idea(cfg, "linked idea")
    _src_path, outbound_id = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001",
                                                  title="Adversarial reviewer")
    dest_spec, _ = E.export_spec(cfg, outbound_id)
    text = dest_spec.read_text()
    text = text.replace(
        "## Clock Log\n\n<Supplemental wall-time record for implementation sessions "
        "(SPEC-0060/0062). For internal\nwork, prefer `nw idea clock-in`/`nw idea clock-out` "
        "on the linked idea instead — this section\nis mainly for outbound/foreign-repo "
        "implementation, where no org access exists. One\nCLOCK-IN/CLOCK-OUT pair per "
        "session:>\n\nCLOCK-IN: [timestamp]\nCLOCK-OUT: [timestamp]",
        "## Clock Log\n\n"
        "CLOCK-IN: 2026-07-26 12:01\n"
        "CLOCK-OUT: 2026-07-26 12:07\n"
        "CLOCK-IN: 2026-07-26 12:07\n"
        "CLOCK-OUT: 2026-07-26 12:18\n")
    dest_spec.write_text(text)

    idea_id, added = E.pull_clock(cfg, outbound_id)
    assert idea_id == "IDEA-001"
    assert added == 2
    idea = _find(cfg, "linked idea")
    assert len(idea.clock) == 2


def test_pull_clock_warns_on_unparsed_clock_lines(tmp_path, capsys):
    """A Clock Log with CLOCK-IN lines that don't resolve into complete pairs must warn, not
    silently report 0 added as if there was nothing to reconcile — the safety net added after
    the bracket-format bug, for whatever future format mismatch slips through the parser."""
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    W.add_idea(cfg, "linked idea")
    _src_path, outbound_id = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001",
                                                  title="Adversarial reviewer")
    dest_spec, _ = E.export_spec(cfg, outbound_id)
    text = dest_spec.read_text()
    text = text.replace(
        "## Clock Log\n\n<Supplemental wall-time record for implementation sessions "
        "(SPEC-0060/0062). For internal\nwork, prefer `nw idea clock-in`/`nw idea clock-out` "
        "on the linked idea instead — this section\nis mainly for outbound/foreign-repo "
        "implementation, where no org access exists. One\nCLOCK-IN/CLOCK-OUT pair per "
        "session:>\n\nCLOCK-IN: [timestamp]\nCLOCK-OUT: [timestamp]",
        # two lines that count as CLOCK-IN by prefix but carry no parseable value — the
        # parser correctly declines both (no pairs), but the raw count (2) must not silently
        # read as "nothing to reconcile"
        "## Clock Log\n\n"
        "CLOCK-IN:\n"
        "CLOCK-IN:\n")
    dest_spec.write_text(text)

    idea_id, added = E.pull_clock(cfg, outbound_id)
    assert added == 0
    err = capsys.readouterr().err
    assert outbound_id in err
    assert "CLOCK-IN line(s)" in err


def test_pull_clock_without_source_idea_raises():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tdp = pathlib.Path(td)
        cfg = _cfg(tdp, outbox_targets={
            "acme": {"repo_path": str(tdp / "target-repo"), "spec_dir": "docs/specs"},
        })
        _path, outbound_id = S.scaffold_outbound(cfg, "acme", title="No idea link")
        E.export_spec(cfg, outbound_id)
        try:
            E.pull_clock(cfg, outbound_id)
            assert False, "expected ValueError"
        except ValueError as e:
            assert "source_idea" in str(e)


# ---- sweep (SPEC-0067) --------------------------------------------------------------

def test_pending_exported_ideas_excludes_done_and_non_exported(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    W.add_idea(cfg, "in progress idea")
    W.add_idea(cfg, "already done idea")
    W.add_idea(cfg, "not exported idea")

    _p1, ob1 = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001", title="In progress")
    E.export_spec(cfg, ob1)

    p2, ob2 = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-002", title="Already done")
    E.export_spec(cfg, ob2)
    p2.write_text(p2.read_text().replace("status: draft", "status: done", 1))

    # IDEA-003 is never scaffolded/exported — stays plain INCUBATE/IDEA, not EXPORTED.

    pending = E.pending_exported_ideas(cfg)
    outbound_ids = {ob for _t, ob, _proj in pending}
    assert ob1 in outbound_ids
    assert ob2 not in outbound_ids
    assert len(pending) == 1


def test_sweep_reconciles_and_logs_missing_destination(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    W.add_idea(cfg, "healthy idea")
    W.add_idea(cfg, "vanished idea")
    W.add_idea(cfg, "untouched idea")

    _p1, ob1 = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001", title="Healthy")
    dest1, _ = E.export_spec(cfg, ob1)
    dest1.write_text(dest1.read_text().replace("status: draft", "status: in-progress", 1))

    _p2, ob2 = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-002", title="Vanished")
    dest2, _ = E.export_spec(cfg, ob2)
    dest2.unlink()  # destination disappears after export — simulates a moved/deleted repo

    _p3, ob3 = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-003", title="Untouched")
    E.export_spec(cfg, ob3)  # portable status left at draft — nothing to reconcile

    results = E.sweep(cfg)
    by_outbound = {ob: text for _idea_id, ob, text in results}
    assert by_outbound[ob1].startswith("updated:")
    assert "status=in-progress" in by_outbound[ob1]
    assert by_outbound[ob2].startswith("missing:")
    assert by_outbound[ob3].startswith("unchanged:")

    provenance = (pathlib.Path(cfg["outbox_dir"]) / "acme" / "PROVENANCE.md").read_text()
    miss_lines = [line for line in provenance.splitlines() if "destination missing" in line]
    assert len(miss_lines) == 1
    assert ob2 in miss_lines[0]
    # the healthy idea's reconciliation was not aborted by the other one's miss
    assert ob1 not in miss_lines[0]


def test_cli_spec_sweep(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs"},
    })
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    W.add_idea(cfg, "cli sweep idea")
    _p, ob = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001", title="CLI sweep")
    E.export_spec(cfg, ob)

    r = CliRunner().invoke(cli, ["spec", "sweep"])
    assert r.exit_code == 0, r.output
    assert ob in r.output


def test_cli_spec_sweep_nothing_pending(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "sweep"])
    assert r.exit_code == 0, r.output
    assert "nothing pending" in r.output


# ---- SPEC-0130: worktree-aware portable spec resolution ------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def test_git_plumbing_lookup_returns_none_for_non_git_path(tmp_path):
    assert E._git_plumbing_lookup(tmp_path / "not-a-repo", "docs/specs/x.md") is None


def test_pull_status_falls_back_to_git_history_when_missing_at_configured_path(tmp_path):
    """The EXAMPLE-0026 scenario (SPEC-0130): the portable spec was committed and then removed
    from the working tree (deliberately, or because it only exists on an unmerged branch
    elsewhere) -- pull_status should still find its last committed content via git history."""
    from nightwright.specmeta import split_frontmatter

    repo = tmp_path / "target-repo"
    _init_repo(repo)
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(repo), "spec_dir": "docs/specs"},
    })
    src_path, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    dest_spec, _ = E.export_spec(cfg, outbound_id)
    dest_spec.write_text(dest_spec.read_text().replace("status: draft", "status: in-progress", 1))

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add portable spec")
    dest_spec.unlink()   # simulate deliberate removal / never checked out here at all

    out_path, status = E.pull_status(cfg, outbound_id)
    assert status == "in-progress"
    assert out_path == src_path
    fm, _ = split_frontmatter(src_path.read_text())
    assert fm["status"] == "in-progress"


def test_pull_clock_falls_back_to_git_history(tmp_path):
    repo = tmp_path / "target-repo"
    _init_repo(repo)
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(repo), "spec_dir": "docs/specs"},
    })
    src_path, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    idea_path, _headline, idea_id = W.add_idea(cfg, "clocked idea")
    text = src_path.read_text()
    text = text.replace("---\n", f"---\nsource_idea: {idea_id}\n", 1)
    src_path.write_text(text)
    dest_spec, _ = E.export_spec(cfg, outbound_id, force=True)
    body = dest_spec.read_text()
    body = body.replace("CLOCK-IN: [timestamp]\nCLOCK-OUT: [timestamp]",
                        "CLOCK-IN: 2026-08-06T09:00:00\nCLOCK-OUT: 2026-08-06T10:00:00", 1)
    dest_spec.write_text(body)

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add clock log")
    dest_spec.unlink()

    got_idea_id, added = E.pull_clock(cfg, outbound_id)
    assert got_idea_id == idea_id
    assert added == 1


def test_pull_status_extra_search_paths_hit_before_git(tmp_path):
    """outbox_targets[project].extra_search_paths is checked (and preferred, no git needed)
    before falling back to git plumbing."""
    from nightwright.specmeta import split_frontmatter

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "target-repo"), "spec_dir": "docs/specs",
                    "extra_search_paths": [str(elsewhere)]},
    })
    src_path, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    dest_spec, _ = E.export_spec(cfg, outbound_id, to=str(elsewhere.parent / "unused"))
    # Place the exported file where extra_search_paths points, matching its own basename.
    relocated = elsewhere / dest_spec.name
    relocated.write_text(dest_spec.read_text().replace("status: draft", "status: done", 1))

    out_path, status = E.pull_status(cfg, outbound_id)
    assert status == "done"
    fm, _ = split_frontmatter(src_path.read_text())
    assert fm["status"] == "done"


def test_pull_status_raises_original_error_when_nowhere_to_be_found(tmp_path):
    """Non-git repo_path, no extra_search_paths, no file anywhere: unchanged error message."""
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": str(tmp_path / "no-such-repo"), "spec_dir": "docs/specs"},
    })
    _src_path, outbound_id = S.scaffold_outbound(cfg, "acme", title="Adversarial reviewer")
    try:
        E.pull_status(cfg, outbound_id)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "portable spec not found at" in str(e)


