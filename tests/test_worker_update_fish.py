"""SPEC-0369: the forced command accepts `nw worker update` under a `fish` login shell (sshd
runs the forced command through the account's shell). Skipped without `fish`."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.fast, pytest.mark.skipif(shutil.which('fish') is None, reason='fish is not installed')]


def test_update_through_a_fish_forced_command(tmp_path):
    bins = tmp_path / "bin"
    bins.mkdir()
    # the worker's own `nw`: a wrapper that runs this checkout's code, so `ssh-command` execs it
    nw = bins / "nw"
    nw.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m nightwright "$@"\n')
    nw.chmod(nw.stat().st_mode | stat.S_IXUSR)
    uv = bins / "uv"
    uv.write_text("#!/bin/sh\necho \"$@\" >> \"$FAKE_DIR/uv.argv\"\n")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    fake = tmp_path / "fake"
    fake.mkdir()
    # a stub wheel whose build id is not this checkout's: it gets as far as the (fake) install
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("nightwright/__init__.py", "")
    data = buf.getvalue()
    from nightwright import buildid
    wheel = tmp_path / "w.whl"
    wheel.write_bytes(data)
    header = {"schema": "nw.worker.update.v1", "wheel": "nightwright-0.1.0-py3-none-any.whl",
              "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
              "build_id": buildid.compute_wheel(wheel)}
    env = {**os.environ, "PATH": f"{bins}{os.pathsep}{os.environ['PATH']}", "FAKE_DIR": str(fake),
           "NW_DATA_DIR": str(tmp_path / "data"), "NW_CONFIG_DIR": str(tmp_path / "conf"),
           "SSH_ORIGINAL_COMMAND": "nw worker update", "HOME": str(tmp_path)}
    p = subprocess.run(["fish", "-c", f"exec {nw} worker ssh-command"],
                       input=json.dumps(header).encode() + b"\n" + data, env=env,
                       capture_output=True, cwd=tmp_path)
    # the fake uv installs nothing, so the check afterwards (the real `nw`) reports this
    # checkout's build id: the update ran up to and including `verify`
    res = json.loads(p.stdout.decode().splitlines()[-1])
    assert res["schema"] == "nw.worker.update.result.v1"
    assert res["stage"] == "verify", (res, p.stderr)
    assert (fake / "uv.argv").read_text().splitlines()[0].split()[:3] == ["tool", "install", "--reinstall"]
