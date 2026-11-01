"""SPEC-0336: `nw worker ssh-command`, the SSH forced command, execs only the four worker
subcommands with their literal grammar and refuses everything else (exit 2, nothing run)."""
import pytest

from nightwright.agentic import worker as W

SHA = "a" * 40


@pytest.fixture
def execs(monkeypatch):
    calls = []
    monkeypatch.setattr(W.os, "execvp", lambda file, argv: calls.append((file, argv)))
    monkeypatch.setattr(W, "own_nw", lambda: ["/home/nw/.local/bin/nw"])
    return calls


@pytest.mark.parametrize("cmd, argv", [
    ("nw worker info", ["info"]),
    ("nw worker info --json", ["info", "--json"]),
    (f"/home/nw/.local/bin/nw worker have --repo proj.x_1-y {SHA} {'b' * 64}",
     ["have", "--repo", "proj.x_1-y", SHA, "b" * 64]),
    ("nw worker gate-job", ["gate-job"]),
    ("nw worker gc", ["gc"]),
    ("nw worker gc --keep-days 0", ["gc", "--keep-days", "0"]),
])
def test_allowed_commands_are_execd(execs, cmd, argv):
    W.ssh_command({"SSH_ORIGINAL_COMMAND": cmd})
    assert execs == [("/home/nw/.local/bin/nw", ["/home/nw/.local/bin/nw", "worker", *argv])]


@pytest.mark.parametrize("cmd", [
    None, "", "   ",
    "nw queue run",
    "nw worker ssh-command",
    "nw worker frobnicate",
    "nw worker info; rm -rf ~",
    "nw worker info && id",
    "nw worker info | sh",
    "nw worker gate-job $(id)",
    "nw worker info --verbose",
    "nw worker info --json --json",
    "nw worker gate-job --force",
    "nw worker gc --keep-days -1",
    "nw worker gc --keep-days 2 --all",
    "nw worker gc --prune",
    "nw worker have --repo ../x " + SHA,
    "nw worker have --repo proj",
    "nw worker have --repo proj notasha",
    "nw worker have " + SHA,
    "bash -c 'nw worker info'",
    "/tmp/evil worker info",
    "nwx worker info",
    "nw worker 'info",
    # `$` would match before a trailing newline; the grammar is literal
    f"nw worker have --repo 'proj\n' {SHA}",
    f"nw worker have --repo proj '{SHA}\n'",
    "nw worker gc --keep-days '5\n'",
])
def test_refused_commands_run_nothing(execs, capsys, cmd):
    environ = {} if cmd is None else {"SSH_ORIGINAL_COMMAND": cmd}
    assert W.ssh_command(environ) == 2
    assert execs == []
    assert capsys.readouterr().err.startswith("refused: ")


def test_own_nw_never_uses_the_client_path(monkeypatch):
    monkeypatch.setattr(W.sys, "argv", ["/usr/bin/pytest"])
    assert W.own_nw()[1:] == ["-m", "nightwright"]


def test_own_nw_execs_the_forced_command_path(monkeypatch, tmp_path):
    nw = tmp_path / "nw"
    nw.write_text("#!/bin/sh\n")
    nw.chmod(0o755)
    monkeypatch.setattr(W.sys, "argv", [str(nw)])
    assert W.own_nw() == [str(nw)]
    calls = []
    monkeypatch.setattr(W.os, "execvp", lambda file, argv: calls.append((file, argv)))
    W.ssh_command({"SSH_ORIGINAL_COMMAND": "/elsewhere/nw worker info"})
    assert calls == [(str(nw), [str(nw), "worker", "info"])]


def test_own_nw_ignores_a_non_executable_nw(monkeypatch, tmp_path):
    nw = tmp_path / "nw"
    nw.write_text("")
    nw.chmod(0o644)
    monkeypatch.setattr(W.sys, "argv", [str(nw)])
    assert W.own_nw()[1:] == ["-m", "nightwright"]
