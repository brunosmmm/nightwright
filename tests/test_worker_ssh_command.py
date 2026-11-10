"""SPEC-0336: `nw worker ssh-command`, the SSH forced command, execs only the four worker
subcommands with their literal grammar and refuses everything else (exit 2, nothing run)."""
import json
import os
import subprocess
import sys

import pytest

from nightwright.agentic import worker as W

pytestmark = pytest.mark.fast

SHA = "a" * 40


@pytest.fixture
def own(monkeypatch, tmp_path):
    """The worker's own `nw` (the forced command's argv[0]): an executable tmp `nw`. Only
    `execvp` is patched, so `ssh_command` and `own_nw` are tested together."""
    nw = tmp_path / "bin" / "nw"
    nw.parent.mkdir()
    nw.write_text("#!/bin/sh\n")
    nw.chmod(0o755)
    monkeypatch.setattr(W.sys, "argv", [str(nw), "worker", "ssh-command"])
    return str(nw)


@pytest.fixture
def execs(monkeypatch, own):
    calls = []
    monkeypatch.setattr(W.os, "execvp", lambda file, argv: calls.append((file, argv)))
    return calls


@pytest.mark.parametrize("cmd, argv", [
    ("nw worker info", ["info"]),
    ("nw worker info --json", ["info", "--json"]),
    (f"/home/nw/.local/bin/nw worker have --repo proj.x_1-y {SHA} {'b' * 64}",
     ["have", "--repo", "proj.x_1-y", SHA, "b" * 64]),
    ("nw worker gate-job", ["gate-job"]),
    ("nw worker gc", ["gc"]),
    ("nw worker gc --keep-days 0", ["gc", "--keep-days", "0"]),
    ("nw worker update", ["update"]),                       # SPEC-0369
    ("nw worker update --force", ["update", "--force"]),
    ("nw worker config-set get", ["config-set", "get"]),
    ("nw worker config-set get executor", ["config-set", "get", "executor"]),
    ("nw worker config-set set executor local", ["config-set", "set", "executor", "local"]),
    ("nw worker config-set set executor slurm", ["config-set", "set", "executor", "slurm"]),
])
def test_allowed_commands_are_execd(execs, own, cmd, argv):
    W.ssh_command({"SSH_ORIGINAL_COMMAND": cmd})
    # the worker's own nw, never the client's words[0] (`nw` or `/home/nw/.local/bin/nw`)
    assert execs == [(own, [own, "worker", *argv])]


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
    "nw worker update --force --force",                     # SPEC-0369
    "nw worker update --reinstall",
    "nw worker update ;id",
    "nw worker update --force; id",
    "nw worker update $(id)",
    "nw worker config-set",
    "nw worker config-set set executor SLURM",
    "nw worker config-set set executor local; id",
    "nw worker update /tmp/x.whl",
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


@pytest.mark.parametrize("cmd", ["nw queue run", "nw worker info; id", ""])
def test_the_cli_command_exits_2_on_a_refusal(tmp_path, cmd):
    """The click wiring: `nw worker ssh-command` itself exits 2, says `refused:`, runs
    nothing (a `nw queue run` would print queue output on stdout)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("NW_")}
    env.update(NW_DATA_DIR=str(tmp_path / "d"), NW_CONFIG_DIR=str(tmp_path / "c"),
               SSH_ORIGINAL_COMMAND=cmd)
    p = subprocess.run([sys.executable, "-m", "nightwright", "worker", "ssh-command"], env=env,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 2, (p.stdout, p.stderr)
    assert p.stderr.startswith("refused: ") and p.stdout == ""


def test_the_cli_command_execs_an_allowed_command(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("NW_")}
    env.update(NW_DATA_DIR=str(tmp_path / "d"), NW_CONFIG_DIR=str(tmp_path / "c"),
               SSH_ORIGINAL_COMMAND="/elsewhere/nw worker info --json")
    p = subprocess.run([sys.executable, "-m", "nightwright", "worker", "ssh-command"], env=env,
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["schema"] == "nw.worker.info.v1"
