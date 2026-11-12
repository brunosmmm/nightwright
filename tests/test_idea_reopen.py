"""`nw idea state` refuses keywords outside the idea vocabulary; `nw idea reopen` recovers a
stranded idea (SPEC-0559)."""
import re
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import explore as EX
from nightwright import org_write as W
from nightwright.cli import cli
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast

IDEA_KW = ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "SHIPPED",
           "DROPPED", "RESEARCHED"]


def _cfg(tmp_path, keywords=None):
    org = tmp_path / "org"; org.mkdir()
    data = tmp_path / "data"; data.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": keywords or IDEA_KW,
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "org_ideas_archive_file": str(org / "ideas-archive.org"),
    }


def _raw(cfg):
    return open(cfg["org_ideas_file"]).read()


def _task(cfg, idea_id="IDEA-001"):
    return [t for t in load_tasks(cfg) if t.properties.get("ID") == idea_id][0]


def _strand(cfg, state="EXPLORE"):
    """Reproduce IDEA-1059: the stray keyword on the done side of `#+TODO`, stamped CLOSED."""
    W.add_idea(cfg, "stranded one")
    raw = _raw(cfg)
    raw = re.sub(r"^(#\+TODO:.*)$", lambda m: f"{m.group(1)} {state}", raw, count=1, flags=re.M)
    raw = re.sub(r"^(\*+) IDEA ", rf"\1 {state} ", raw, count=1, flags=re.M)
    raw = re.sub(r"^(\*+ .*\n)", lambda m: m.group(1) + "  CLOSED: [2026-11-09 Mon]\n", raw,
                 count=1, flags=re.M) if "CLOSED:" not in raw else raw
    open(cfg["org_ideas_file"], "w").write(raw)
    t = _task(cfg)
    assert t.state == state and not t.is_idea
    return t


@pytest.fixture
def cli_cfg(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return cfg


def _run(*args):
    return CliRunner().invoke(cli, list(args))


# --- validation ----------------------------------------------------------------------------

def test_state_rejects_unknown_keyword_and_leaves_file_identical(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea")
    before = _raw(cfg)
    with pytest.raises(ValueError, match="not an idea state.*IDEA INCUBATE SPECCED"):
        EX.set_idea_state(cfg, "IDEA-001", "explore")
    assert _raw(cfg) == before


@pytest.mark.parametrize("kw", [k for k in IDEA_KW if k != "|"])
def test_state_accepts_every_configured_keyword(tmp_path, kw):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea")
    EX.set_idea_state(cfg, "IDEA-001", kw)           # DROPPED etc. archive cleanly (archive file set)


def test_state_accepts_custom_vocabulary_keyword(tmp_path):
    cfg = _cfg(tmp_path, ["IDEA", "INCUBATE", "SPECCED", "PARKED", "|", "DROPPED"])
    W.add_idea(cfg, "an idea")
    EX.set_idea_state(cfg, "IDEA-001", "PARKED")
    assert _task(cfg).state == "PARKED"
    with pytest.raises(ValueError):
        EX.set_idea_state(cfg, "IDEA-001", "EXPLORE")


def test_tui_state_mutation_rejects_unknown_keyword(tmp_path):
    from nightwright.tui import model as M
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea")
    before = _raw(cfg)
    with pytest.raises(ValueError):
        M.apply_mutation(cfg, "IDEA-001", "state", value="EXPLORE")
    assert _raw(cfg) == before


# --- reopen --------------------------------------------------------------------------------

def test_reopen_stranded_idea(tmp_path):
    cfg = _cfg(tmp_path)
    _strand(cfg)
    assert "CLOSED:" in _raw(cfg)
    assert W.resolve_selector(cfg, "IDEA-001").is_idea is False
    EX.reopen_idea(cfg, "IDEA-001")
    t = _task(cfg)
    assert t.state == "IDEA" and t.is_idea
    raw = _raw(cfg)
    assert "CLOSED:" not in raw
    assert ":UPDATED:" in raw


def test_reopen_into_incubate(tmp_path):
    cfg = _cfg(tmp_path)
    _strand(cfg)
    EX.reopen_idea(cfg, "IDEA-001", state="incubate")
    assert _task(cfg).state == "INCUBATE"


@pytest.mark.parametrize("state", ["SHIPPED", "NOPE"])
def test_reopen_rejects_non_active_state_without_writing(tmp_path, state):
    cfg = _cfg(tmp_path)
    _strand(cfg)
    before = _raw(cfg)
    with pytest.raises(ValueError):
        EX.reopen_idea(cfg, "IDEA-001", state=state)
    assert _raw(cfg) == before


def test_reopen_already_open_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "healthy")
    before = _raw(cfg)
    with pytest.raises(ValueError, match="already open"):
        EX.reopen_idea(cfg, "IDEA-001")
    assert _raw(cfg) == before


def test_reopen_rejects_ordinary_task(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "plain task")
    task = [t for t in load_tasks(cfg) if not t.is_idea][0]
    before = open(task.file).read()
    with pytest.raises(ValueError, match="not an idea entry"):
        EX.reopen_idea(cfg, task.id)
    assert open(task.file).read() == before


def test_reopen_from_hand_set_dropped(tmp_path):
    """The normal DROPPED path archives, so the fixture hand-sets DROPPED in the live file."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "dropped one")
    raw = re.sub(r"^(\*+) IDEA ", r"\1 DROPPED ", _raw(cfg), count=1, flags=re.M)
    open(cfg["org_ideas_file"], "w").write(raw)
    assert _task(cfg).state == "DROPPED" and _task(cfg).is_idea
    EX.reopen_idea(cfg, "IDEA-001")
    assert _task(cfg).state == "IDEA"


def test_reopen_missing_file_keyword_rejected_without_write(tmp_path):
    cfg = _cfg(tmp_path)
    _strand(cfg)
    raw = re.sub(r"^(#\+TODO:.*?)\bSPECCED\b ?", r"\1", _raw(cfg), count=1, flags=re.M)
    open(cfg["org_ideas_file"], "w").write(raw)
    assert "SPECCED" not in re.search(r"^#\+TODO:.*$", raw, re.M).group(0)
    before = _raw(cfg)
    with pytest.raises(ValueError, match="SPECCED") as ei:
        EX.reopen_idea(cfg, "IDEA-001", state="SPECCED")
    assert cfg["org_ideas_file"] in str(ei.value)
    assert _raw(cfg) == before


def test_set_state_touch_updated(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "plain task")
    task = [t for t in load_tasks(cfg) if not t.is_idea][0]
    W.set_state(cfg, task, "DONE")
    assert ":UPDATED:" not in open(task.file).read()
    task = [t for t in load_tasks(cfg) if not t.is_idea][0]
    W.set_state(cfg, task, "TODO", touch_updated=True)
    assert ":UPDATED:" in open(task.file).read()


# --- CLI -----------------------------------------------------------------------------------

def test_cli_state_explore_rejected(cli_cfg):
    W.add_idea(cli_cfg, "an idea")
    before = _raw(cli_cfg)
    r = _run("idea", "state", "IDEA-001", "EXPLORE")
    assert r.exit_code != 0 and "not an idea state" in r.output
    assert _raw(cli_cfg) == before


def test_cli_reopen_recovers_stranded_idea(cli_cfg):
    _strand(cli_cfg)
    for cmd in (["show", "IDEA-001"], ["log", "IDEA-001", "--note", "x"], ["state", "IDEA-001", "IDEA"]):
        r = _run("idea", *cmd)
        assert r.exit_code != 0
        assert "nw idea reopen IDEA-001" in r.output, (cmd, r.output)
    r = _run("idea", "reopen", "IDEA-001")
    assert r.exit_code == 0, r.output
    assert "reopened IDEA-001" in r.output
    assert _run("idea", "show", "IDEA-001", "--plain").exit_code == 0
    assert _run("idea", "log", "IDEA-001", "--note", "back").exit_code == 0
    assert _run("idea", "state", "IDEA-001", "INCUBATE").exit_code == 0


def test_cli_reopen_bad_state_and_plain_message_for_tasks(cli_cfg):
    _strand(cli_cfg)
    before = _raw(cli_cfg)
    assert _run("idea", "reopen", "IDEA-001", "--state", "SHIPPED").exit_code != 0
    assert _raw(cli_cfg) == before
    W.add_task(cli_cfg, "plain task")
    task = [t for t in load_tasks(cli_cfg) if not t.is_idea and not t.id.startswith("IDEA")][0]
    r = _run("idea", "show", task.id)
    assert r.exit_code != 0 and "is not an idea" in r.output and "nw idea reopen" not in r.output
