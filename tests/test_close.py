"""Idea terminal-outcome model: `nw idea close`, RESEARCHED state, reason + pointers
(SPEC-0055 / 0056 / 0057)."""
from zoneinfo import ZoneInfo

import pytest

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright.org import load_tasks
from nightwright.workflow import next_step_for_idea

pytestmark = pytest.mark.fast

IDEA_KW = ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED",
           "DROPPED", "RESEARCHED"]


def _cfg(tmp_path):
    org = tmp_path / "org"; org.mkdir()
    data = tmp_path / "data"; data.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": IDEA_KW,
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
        "specs_dir": str(tmp_path / "specs"),
        "org_ideas_archive_file": str(org / "ideas-archive.org"),
    }


def _idea(cfg, idea_id="IDEA-001"):
    return [t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id][0]


# --- close: drop / researched + reason (SPEC-0056) -------------------------------------

def test_close_drop_sets_state_reason_and_log(tmp_path):
    """SPEC-0073: DROPPED auto-archives the idea, so its content now lives in the archive
    file, not the active ideas file."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "won't do this")
    EX.close_idea(cfg, "IDEA-001", "drop", reason="duplicate of IDEA-002")
    t = _idea(cfg)
    assert t.state == "DROPPED" and t.is_done
    assert "won't do this" not in open(cfg["org_ideas_file"]).read()
    raw = open(cfg["org_ideas_archive_file"]).read()
    assert ":CLOSE_REASON: duplicate of IDEA-002" in raw
    assert "CLOSED:" in raw
    assert "Closed as drop. duplicate of IDEA-002" in raw     # Log line


def test_close_researched_state_is_settable(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "explored, no spec")
    EX.close_idea(cfg, "IDEA-001", "researched", reason="predicate folded elsewhere")
    t = _idea(cfg)
    assert t.state == "RESEARCHED" and t.is_done
    assert (":CLOSE_REASON: predicate folded elsewhere"
            in open(cfg["org_ideas_archive_file"]).read())


def test_close_bad_outcome_and_non_idea_error_without_write(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "keep me")
    before = open(cfg["org_ideas_file"]).read()
    with pytest.raises(ValueError):
        EX.close_idea(cfg, "IDEA-001", "bogus")
    assert open(cfg["org_ideas_file"]).read() == before


def test_researched_hidden_from_next_and_default_ideas_shown_all(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "terminal")
    EX.close_idea(cfg, "IDEA-001", "researched", reason="done exploring")
    t = _idea(cfg)
    assert next_step_for_idea(cfg, t) == ""                   # no hint
    from nightwright.report import collect_next_tasks, collect_idea_tasks
    assert not any(x.properties.get("ID") == "IDEA-001" for x in collect_next_tasks(cfg))
    assert not any(x.properties.get("ID") == "IDEA-001"
                   for x in collect_idea_tasks(cfg, all_done=False))
    assert any(x.properties.get("ID") == "IDEA-001"
               for x in collect_idea_tasks(cfg, all_done=True))


def test_close_is_reversible(tmp_path):
    """SPEC-0073: closing archives the idea, so its CLOSED: stamp lives in the archive file;
    reopening clears the stamp wherever the idea currently lives (moving it back to the active
    file on reopen is out of scope for SPEC-0073 — see its Non-goals)."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "reopen me")
    EX.close_idea(cfg, "IDEA-001", "drop", reason="mistake")
    assert "CLOSED:" in open(cfg["org_ideas_archive_file"]).read()
    W.set_state_by_selector(cfg, "IDEA-001", "INCUBATE")
    t = _idea(cfg)
    assert t.state == "INCUBATE" and not t.is_done
    assert "CLOSED:" not in open(cfg["org_ideas_archive_file"]).read()


# --- primitives ------------------------------------------------------------------------

def test_ensure_idea_keyword_adds_and_is_idempotent(tmp_path):
    p = tmp_path / "ideas.org"
    p.write_text("#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED DROPPED\n\n* IDEA x\n")
    cfg = _cfg(tmp_path)
    assert W.ensure_idea_keyword(cfg, str(p), "RESEARCHED") is True
    assert "DROPPED RESEARCHED" in p.read_text()
    assert W.ensure_idea_keyword(cfg, str(p), "RESEARCHED") is False


def test_set_property_add_then_update(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "props")
    W.set_property(cfg, _idea(cfg), "CLOSE_REASON", "first")
    W.set_property(cfg, _idea(cfg), "CLOSE_REASON", "second")
    raw = open(cfg["org_ideas_file"]).read()
    assert ":CLOSE_REASON: second" in raw and "first" not in raw
    assert ":ID: IDEA-001" in raw                              # existing key untouched


# --- pointers (SPEC-0057) --------------------------------------------------------------

def test_close_folded_into_writes_property_link_and_surfaces(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "source")            # IDEA-001
    W.add_idea(cfg, "target")            # IDEA-002
    EX.close_idea(cfg, "IDEA-001", "researched", reason="value moved",
                  folded_into="IDEA-002")
    raw = open(cfg["org_ideas_archive_file"]).read()
    assert ":FOLDED_INTO: IDEA-002" in raw
    assert "Folded into [[IDEA-002]]." in raw
    payload = EX.idea_show_payload(cfg, _idea(cfg))
    assert payload["folded_into"] == "IDEA-002"
    assert "→ folded into [[IDEA-002]]" in EX.format_idea_show(cfg, _idea(cfg))


def test_close_superseded_by_accepts_spec_id(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "old design")
    EX.close_idea(cfg, "IDEA-001", "drop", superseded_by="SPEC-0033")
    assert ":SUPERSEDED_BY: SPEC-0033" in open(cfg["org_ideas_archive_file"]).read()
    assert EX.idea_show_payload(cfg, _idea(cfg))["superseded_by"] == "SPEC-0033"


def test_close_unresolvable_pointer_errors_without_write(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "src")
    before = open(cfg["org_ideas_file"]).read()
    with pytest.raises(ValueError):
        EX.close_idea(cfg, "IDEA-001", "researched", folded_into="IDEA-999")
    assert open(cfg["org_ideas_file"]).read() == before       # resolved before any write


# --- force state (SPEC-0135) -----------------------------------------------------------

def test_set_idea_state_to_shipped_archives(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "force ship")
    EX.set_idea_state(cfg, "IDEA-001", "SHIPPED")
    t = _idea(cfg)
    assert t.state == "SHIPPED" and t.is_done
    assert "force ship" not in open(cfg["org_ideas_file"]).read()
    assert "force ship" in open(cfg["org_ideas_archive_file"]).read()
    assert next_step_for_idea(cfg, t) == ""


def test_cli_idea_state(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from nightwright.cli import cli

    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "cli force")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    result = CliRunner().invoke(cli, ["idea", "state", "IDEA-001", "SHIPPED"])
    assert result.exit_code == 0, result.output
    assert "SHIPPED" in result.output
    assert _idea(cfg).state == "SHIPPED"
