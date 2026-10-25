"""Post-ship fit log (SPEC-0049)."""
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from wt import export as E
from wt import explore as EX
from wt import org_write as W
from wt import specs as S
from wt.cli import cli
from wt.org import load_tasks


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        (specs / name).write_text((root / "docs" / "specs" / name).read_text())
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED", "DROPPED",
            "RESEARCHED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
        "outbox_dir": str(tmp_path / "outbox"),
        "outbox_targets": {"acme": {"repo_path": str(tmp_path / "target")}},
        "config_dir": str(tmp_path / "config"),
    }


def test_fit_log_appends_to_source_idea(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "ship me", project="acme")
    path, oid = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001", title="Ship me")
    assert "source_idea: IDEA-001" in path.read_text()

    idea_id, ideas_path = E.fit_log(cfg, oid, "used it; still too coarse")
    assert idea_id == "IDEA-001"
    body = EX.idea_show_payload(cfg, load_tasks(cfg)[0])["log"]
    assert "fit-log:" in body
    assert "still too coarse" in body
    assert ideas_path.endswith("ideas.org") or "ideas.org" in ideas_path


def test_fit_log_missing_idea_errors(tmp_path):
    cfg = _cfg(tmp_path)
    path, oid = S.scaffold_outbound(cfg, "acme", title="Orphan export")
    # strip source_idea if any
    text = path.read_text()
    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("source_idea:"))
    path.write_text(text + ("\n" if not text.endswith("\n") else ""))
    with pytest.raises(ValueError, match="no source idea"):
        E.fit_log(cfg, oid, "note")


def test_fit_log_cli(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "cli fit", project="acme")
    S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001", title="Cli fit")
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "fit-log", "ACME-0001", "--note", "missed filters"])
    assert r.exit_code == 0, r.output
    assert "fit-log" in r.output


def test_fit_log_works_on_internal_spec(tmp_path):
    """SPEC-0069: fit_log's resolution was outbound-only only because it called
    _find_outbound_spec directly; the shared _resolve_source_idea helper (via
    specs.resolve_spec_path) now makes it work for internal SPEC-NNNN ids too."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "internal fit target")
    _path, spec_id = S.scaffold_from_idea(cfg, "IDEA-001", title="Internal fit target")

    idea_id, _path2 = E.fit_log(cfg, spec_id, "worked; kept using it weekly")
    assert idea_id == "IDEA-001"
    body = EX.idea_show_payload(cfg, load_tasks(cfg)[0])["log"]
    assert "fit-log:" in body
    assert "kept using it weekly" in body


# ---- postmortem (SPEC-0069) ---------------------------------------------------------

def test_postmortem_appends_to_internal_spec_source_idea(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "internal postmortem target")
    _path, spec_id = S.scaffold_from_idea(cfg, "IDEA-001", title="Internal postmortem target")

    idea_id, _ = E.postmortem(cfg, spec_id, "parser silently dropped unbracketed timestamps")
    assert idea_id == "IDEA-001"
    body = EX.idea_show_payload(cfg, load_tasks(cfg)[0])["log"]
    assert "postmortem:" in body
    assert "dropped unbracketed timestamps" in body


def test_postmortem_appends_to_outbound_spec_source_idea(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "outbound postmortem target", project="acme")
    _path, oid = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001",
                                      title="Outbound postmortem target")

    idea_id, _ = E.postmortem(cfg, oid, "found a bug in the shipped export path")
    assert idea_id == "IDEA-001"
    body = EX.idea_show_payload(cfg, load_tasks(cfg)[0])["log"]
    assert "postmortem:" in body


def test_postmortem_missing_source_idea_errors(tmp_path):
    cfg = _cfg(tmp_path)
    path, oid = S.scaffold_outbound(cfg, "acme", title="Orphan export")
    text = path.read_text()
    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("source_idea:"))
    path.write_text(text + ("\n" if not text.endswith("\n") else ""))
    with pytest.raises(ValueError, match="no source idea"):
        E.postmortem(cfg, oid, "note")


# ---- automatic archival on EXPORTED-becomes-done (SPEC-0073) ----------------------------

def test_pull_status_archives_idea_when_status_first_becomes_done(tmp_path):
    import os
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "ship me", project="acme")
    _path, oid = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001", title="Ship me")
    dest_spec, _ = E.export_spec(cfg, oid)
    dest_spec.write_text(dest_spec.read_text().replace("status: draft", "status: done", 1))

    idea = load_tasks(cfg)[0]
    assert idea.state == "EXPORTED"

    E.pull_status(cfg, oid)

    archive_path = os.path.join(os.path.dirname(cfg["org_ideas_file"]), "ideas-archive.org")
    assert os.path.exists(archive_path)
    assert "ship me" not in open(cfg["org_ideas_file"]).read()
    assert "ship me" in open(archive_path).read()

    reloaded = load_tasks(cfg)[0]
    assert reloaded.properties.get("ID") == "IDEA-001"
    assert reloaded.state == "SHIPPED"          # SPEC-0135: done pull advances EXPORTED


def test_pull_status_second_call_does_not_rearchive(tmp_path):
    import os
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "ship me twice", project="acme")
    _path, oid = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001", title="Ship me twice")
    dest_spec, _ = E.export_spec(cfg, oid)
    dest_spec.write_text(dest_spec.read_text().replace("status: draft", "status: done", 1))

    E.pull_status(cfg, oid)          # first pull: archives
    _path2, status2 = E.pull_status(cfg, oid)  # second pull: must not error or re-move
    assert status2 == "done"

    archive_path = os.path.join(os.path.dirname(cfg["org_ideas_file"]), "ideas-archive.org")
    archived = open(archive_path).read()
    assert archived.count("ship me twice") == 1   # not duplicated


def test_pull_status_not_yet_done_does_not_archive(tmp_path):
    import os
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "still in progress", project="acme")
    _path, oid = S.scaffold_outbound(cfg, "acme", from_idea="IDEA-001", title="In progress")
    dest_spec, _ = E.export_spec(cfg, oid)
    dest_spec.write_text(dest_spec.read_text().replace("status: draft", "status: in-progress", 1))

    E.pull_status(cfg, oid)

    archive_path = os.path.join(os.path.dirname(cfg["org_ideas_file"]), "ideas-archive.org")
    assert not os.path.exists(archive_path)
    assert "still in progress" in open(cfg["org_ideas_file"]).read()


def test_pull_status_done_with_no_linked_idea_still_succeeds(tmp_path, capsys):
    """A done-status pull with no resolvable source idea must still return normally — the
    archival side effect failing is not allowed to break pull_status's own contract."""
    cfg = _cfg(tmp_path)
    _path, oid = S.scaffold_outbound(cfg, "acme", title="No linked idea")
    dest_spec, _ = E.export_spec(cfg, oid)
    dest_spec.write_text(dest_spec.read_text().replace("status: draft", "status: done", 1))

    out_path, status = E.pull_status(cfg, oid)
    assert status == "done"
    assert out_path is not None
    err = capsys.readouterr().err
    assert "could not advance linked idea to SHIPPED" in err


def test_postmortem_cli(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "cli postmortem")
    _path, spec_id = S.scaffold_from_idea(cfg, "IDEA-001", title="Cli postmortem")
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "postmortem", spec_id, "--note", "found a defect"])
    assert r.exit_code == 0, r.output
    assert "postmortem" in r.output
