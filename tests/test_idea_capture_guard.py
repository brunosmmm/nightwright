"""SPEC-0272: verb-like one-word captures refused; persona runs cannot write the org."""
import os
from zoneinfo import ZoneInfo

import click
import pytest
from click.testing import CliRunner

from nightwright import org_write as OW
from nightwright.cli import cli
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast


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
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    return cfg


def _run(*args):
    return CliRunner().invoke(cli, list(args))


def _ideas_bytes(cfg):
    p = cfg["org_ideas_file"]
    return open(p, "rb").read() if os.path.exists(p) else None


def _snapshot(cfg):
    files = {}
    for top in (os.path.dirname(cfg["org_ideas_file"]), cfg["data_dir"]):
        for root, dirs, names in os.walk(top):
            files[root] = sorted(dirs + names)      # listings catch a new (backup) file or dir
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


def test_list_hint_points_to_nw_ideas(env):
    r = _run("idea", "list")
    assert r.exit_code == 1 and "nw ideas" in r.output
    r = _run("idea", "delete")
    assert "nw idea --help" in r.output


def test_force_and_real_word_capture(env):
    assert _run("idea", "list", "--force").exit_code == 0
    assert _run("idea", "refactor").exit_code == 0
    heads = [t.heading for t in load_tasks(env)]
    assert "list" in heads and "refactor" in heads


_MULTIWORD_REFUSED = [
    (["find", "x", "y"], "find"),
    (["list", "the", "ideas"], "list"),
    (["LS", "all"], "ls"),
    (["rm", "IDEA-1", "now"], "rm"),
    (["delete", "foo", "bar"], "delete"),
    (["del", "foo", "bar"], "del"),
    (["list the ideas"], "list"),            # one quoted argument
    (["find", "x", "--tag", "foo"], "find"),  # option on the line
]


@pytest.mark.parametrize("argv,verb", _MULTIWORD_REFUSED)
def test_multiword_verb_capture_refused(env, argv, verb):
    OW.add_idea(env, "seed idea")
    before = _ideas_bytes(env)
    r = _run("idea", *argv)
    assert r.exit_code == 1, r.output
    assert f"'{verb}'" in r.output and "--force" in r.output
    assert _ideas_bytes(env) == before


def test_multiword_verb_quoted_and_with_options_refused(env):
    before = _ideas_bytes(env)
    for argv in (["idea", "list the ideas"], ["idea", "list", "the", "ideas"],
                 ["idea", "find", "x", "--tag", "foo"], ["idea", "find", "--tag", "foo", "x"]):
        r = _run(*argv)
        assert r.exit_code == 1, (argv, r.output)
    assert _ideas_bytes(env) == before


def test_multiword_verb_hints(env):
    for verb in ("list", "ls", "find"):
        out = _run("idea", verb, "some", "text").output
        assert "`nw idea search TEXT`" in out and "`nw ideas`" in out
    for verb in ("rm", "delete", "del"):
        out = _run("idea", verb, "some", "text").output
        assert "nw idea close" in out and "--as drop" in out
    assert _run("idea", "close", "--help").exit_code == 0


@pytest.mark.parametrize("argv", [["find", "x", "y"], ["list", "all", "the", "things"],
                                  ["rm", "IDEA-1", "now"]])
def test_multiword_verb_force_captures(env, argv):
    assert _run("idea", *argv, "--force").exit_code == 0
    assert [t.heading for t in load_tasks(env)] == [" ".join(argv)]


@pytest.mark.parametrize("argv", [
    ["refactor", "the", "parser"], ["add", "retry", "to", "the", "queue"],
    ["status", "page", "for", "the", "dashboard"], ["listing", "of", "the", "queue"],
    ["delegate", "the", "lease"], ["capture", "search", "the", "ideas", "file", "faster"]])
def test_ordinary_multiword_captures_unchanged(env, argv):
    assert _run("idea", *argv).exit_code == 0
    heads = [t.heading for t in load_tasks(env)]
    assert heads == [" ".join(a for a in argv if a != "capture") if argv[0] == "capture"
                     else " ".join(argv)]


def test_single_word_guard_unchanged(env):
    r = _run("idea", "list")
    assert r.exit_code == 1 and "`nw ideas`" in r.output
    assert _run("idea", "list", "--force").exit_code == 0
    assert [t.heading for t in load_tasks(env)] == ["list"]


def test_search_led_text_no_longer_captures(env):
    OW.add_idea(env, "seed idea")
    before = _ideas_bytes(env)
    r = _run("idea", "search", "the", "ideas", "file", "faster")
    assert r.exit_code == 0
    assert _ideas_bytes(env) == before


def test_idea_search_read_only_under_persona(env, monkeypatch):
    OW.add_idea(env, "seed queue idea")
    monkeypatch.setenv("NW_PERSONA_RUN", "1")
    before = _snapshot(env)
    r = _run("idea", "search", "queue")
    assert r.exit_code == 0, r.output
    assert "seed queue idea" in r.output
    assert _snapshot(env) == before


def test_persona_run_refuses_org_writes(env, monkeypatch):
    OW.add_idea(env, "seed idea")
    idea_id = load_tasks(env)[0].properties["ID"]
    monkeypatch.setenv("NW_PERSONA_RUN", "1")
    before = _snapshot(env)
    for args in (["idea", "some text"],
                 ["idea", "log", idea_id, "--note", "n"],
                 ["idea", "clock-in", idea_id],
                 ["add", "t"]):
        r = _run(*args)
        assert r.exit_code == 1, (args, r.output)
        assert "nw campaign note" in r.output
        assert "campaign agent" in r.output
        assert "Traceback" not in r.output
        assert _snapshot(env) == before, args


def test_persona_run_allows_reads(env, monkeypatch):
    OW.add_idea(env, "seed idea")
    idea_id = load_tasks(env)[0].properties["ID"]
    monkeypatch.setenv("NW_PERSONA_RUN", "1")
    assert _run("ideas").exit_code == 0
    assert _run("idea", "show", idea_id).exit_code == 0


def test_atomic_write_primitive_refuses(env, tmp_path, monkeypatch):
    monkeypatch.setenv("NW_PERSONA_RUN", "1")
    target = tmp_path / "org" / "new.org"
    with pytest.raises(OW.PersonaOrgWriteRefused):
        OW._atomic_backup_write(env, str(target), "* x\n")
    assert not target.exists()


def test_root_group_converts_refusal_without_command_catching_it(env, monkeypatch):
    @click.command("zz-throwaway")
    def throwaway():
        raise OW.PersonaOrgWriteRefused("nw: refusing to write the org from a campaign agent")

    monkeypatch.setitem(cli.commands, "zz-throwaway", throwaway)
    monkeypatch.setenv("NW_PERSONA_RUN", "1")
    r = _run("zz-throwaway")
    assert r.exit_code == 1
    assert "campaign agent" in r.output
    assert isinstance(r.exception, SystemExit)
    assert not isinstance(r.exception, OW.PersonaOrgWriteRefused)


def test_autouse_fixture_leaves_persona_flag_unset():
    assert "NW_PERSONA_RUN" not in os.environ


def test_idea_help_lists_search_and_capture(env):
    r = _run("idea", "--help")
    assert r.exit_code == 0, r.output
    assert "search" in r.output.split("Commands:", 1)[1]
    assert "nw idea capture" in r.output


def test_idea_search_bare_and_blank_exit_2_no_capture(env):
    OW.add_idea(env, "seed idea")
    before = _ideas_bytes(env)
    assert _run("idea", "search").exit_code == 2
    assert _run("idea", "search", "   ").exit_code == 2
    assert _ideas_bytes(env) == before
