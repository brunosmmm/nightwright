"""SPEC-0369: the real `uv build` and `uv tool install --reinstall` through `worker_update.update`
(fakes do not prove the integration). Skipped without `uv`; needs the package index or a warm uv
cache for the build backend and the dependencies."""
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from nightwright import buildid
from nightwright.agentic import worker_update as WU

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.fast, pytest.mark.skipif(shutil.which('uv') is None, reason='uv is not installed')]


def test_a_real_wheel_installs_and_reports_its_build_id(tmp_path, monkeypatch):
    out = tmp_path / "dist"
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out)], cwd=ROOT, check=True,
                   capture_output=True)
    wheel = next(out.glob("nightwright-*-py3-none-any.whl"))
    build = buildid.compute_wheel(wheel)
    data = wheel.read_bytes()
    home, tools, tbin = tmp_path / "home", tmp_path / "tools", tmp_path / "bin"
    home.mkdir()
    for k, v in {"HOME": home, "UV_TOOL_DIR": tools, "UV_TOOL_BIN_DIR": tbin,
                 "NW_DATA_DIR": tmp_path / "data", "NW_CONFIG_DIR": tmp_path / "conf"}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setattr(buildid, "local", lambda: "b1:" + "0" * 64)   # an older install
    header = {"schema": WU.SCHEMA, "wheel": wheel.name, "size": len(data),
              "sha256": hashlib.sha256(data).hexdigest(), "build_id": build}
    stdout = io.BytesIO()
    code = WU.update({"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "conf")},
                     False, io.BytesIO(json.dumps(header).encode() + b"\n" + data), stdout,
                     nw_argv=[str(tbin / "nw")])
    res = json.loads(stdout.getvalue())
    assert code == 0 and res["state"] == "updated", res
    p = subprocess.run([str(tbin / "nw"), "worker", "info", "--json"], capture_output=True,
                       text=True, check=True)
    assert json.loads(p.stdout)["build_id"] == build

