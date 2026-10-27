"""SPEC-0272: verb-like one-word captures refused; persona runs cannot write the org."""
import os
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from wt import org_write as OW
from wt.cli import cli
from wt.org import load_tasks


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                              "DROPPED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(tmp_path / "specs"),
        "config_dir": str(tmp_path / "config"),
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    return cfg


def _run(*args):
    return CliRunner().invoke(cli, list(args))


def _ideas_bytes(cfg):
    p = cfg["org_ideas_file"]
    return open(p, "rb").read() if os.path.exists(p) else None


def _snapshot(cfg):
    files = {}
    for root, _dirs, names in os.walk(os.path.dirname(cfg["org_ideas_file"])):
        for n in names:
            fp = os.path.join(root, n)
            files[fp] = open(fp, "rb").read()
    return files


@pytest.mark.parametrize("word", ["list", "LS", "help", "delete", "status", "campaign"])
def test_verb_capture_refused(env, word):
    OW.add_idea(env, "seed idea")
    before = _ideas_bytes(env)
    r = _run("idea", word)
    assert r.exit_code == 1
    assert "--force" in r.output
    assert _ideas_bytes(env) == before


def test_list_hint_points_to_wt_ideas(env):
    r = _run("idea", "list")
    assert r.exit_code == 1 and "wt ideas" in r.output
    r = _run("idea", "delete")
    assert "wt idea --help" in r.output


def test_force_and_real_word_capture(env):
    assert _run("idea", "list", "--force").exit_code == 0
    assert _run("idea", "refactor").exit_code == 0
    heads = [t.heading for t in load_tasks(env)]
    assert "list" in heads and "refactor" in heads


def test_multi_word_verb_text_captures(env):
    assert _run("idea", "list", "all", "the", "things").exit_code == 0
    assert [t.heading for t in load_tasks(env)] == ["list all the things"]


def test_persona_run_refuses_org_writes(env, monkeypatch):
    OW.add_idea(env, "seed idea")
    idea_id = load_tasks(env)[0].properties["ID"]
    monkeypatch.setenv("WT_PERSONA_RUN", "1")
    before = _snapshot(env)
    for args in (["idea", "some text"],
                 ["idea", "log", idea_id, "--note", "n"],
                 ["idea", "clock-in", idea_id],
                 ["add", "t"]):
        r = _run(*args)
        assert r.exit_code == 1, (args, r.output)
        assert "wt campaign note" in r.output
        assert "campaign agent" in r.output
        assert "Traceback" not in r.output
        assert _snapshot(env) == before, args


def test_persona_run_allows_reads(env, monkeypatch):
    OW.add_idea(env, "seed idea")
    idea_id = load_tasks(env)[0].properties["ID"]
    monkeypatch.setenv("WT_PERSONA_RUN", "1")
    assert _run("ideas").exit_code == 0
    assert _run("idea", "show", idea_id).exit_code == 0


def test_atomic_write_primitive_refuses(env, tmp_path, monkeypatch):
    monkeypatch.setenv("WT_PERSONA_RUN", "1")
    target = tmp_path / "org" / "new.org"
    with pytest.raises(OW.PersonaOrgWriteRefused):
        OW._atomic_backup_write(env, str(target), "* x\n")
    assert not target.exists()
