"""SPEC-0499: the suite must not spawn real or fake `notify-send` on the desktop."""
import os
import shutil
import subprocess
import sys

import pytest

from nightwright.agentic import escalations as ESC


@pytest.fixture
def fake_notify_send(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "notify-send-ran.marker"
    script = bindir / "notify-send"
    script.write_text(f"#!/bin/sh\n touch '{marker}'\n exit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    return script, marker


def _assert_which_is_fake(fake_script):
    assert shutil.which("notify-send") == str(fake_script)


def test_notify_auto_does_not_spawn_notify_send(fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    cfg = {"data_dir": "/tmp/unused"}
    rec = {"id": "ESC-0001", "question": "ship it?"}
    out = ESC.notify(cfg, rec)
    assert out["via"] == "notify-send" and out["ok"] is True
    assert not marker.exists()


def test_apply_escalate_in_tmp_store_does_not_spawn_notify_send(tmp_path, fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    cfg = {"data_dir": str(tmp_path / "data")}
    plan = ESC.plan_escalate(cfg, None, question="runner broken?", options=["wait", "restart"],
                             recommend=1, topic="scope")
    rec = ESC.apply_escalate(cfg, plan)
    assert rec["id"] == "ESC-0001"
    assert ESC.read(cfg, "ESC-0001") is not None
    assert not marker.exists()


def test_subprocess_notify_send_fails_closed(fake_notify_send):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    fake = str(fake_script)
    with pytest.raises(AssertionError, match="notify-send"):
        subprocess.run([fake, "t", "b"])
    assert not marker.exists()
    with pytest.raises(AssertionError, match="notify-send"):
        subprocess.Popen([fake, "t", "b"])
    assert not marker.exists()
    with pytest.raises(AssertionError, match="notify-send"):
        subprocess.run("notify-send t b", shell=True)
    assert not marker.exists()


def test_other_subprocess_commands_still_run():
    r = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "1\n"


def test_popen_of_other_commands_stays_a_popen():
    proc = subprocess.Popen([sys.executable, "-c", "print(1)"], stdout=subprocess.PIPE, text=True)
    assert isinstance(proc, subprocess.Popen)
    out, _ = proc.communicate()
    assert out == "1\n"


def test_module_stub_still_observes_argv(fake_notify_send, monkeypatch):
    fake_script, marker = fake_notify_send
    _assert_which_is_fake(fake_script)
    seen = []

    class _Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(ESC, "_run_notify", lambda argv: seen.append(list(argv)) or _Done())
    cfg = {"data_dir": "/tmp/unused"}
    rec = {"id": "ESC-0001", "question": "q?"}
    ESC.notify(cfg, rec)
    assert os.path.basename(seen[0][0]) == "notify-send"
    assert not marker.exists()
