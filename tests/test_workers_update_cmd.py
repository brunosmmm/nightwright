"""SPEC-0385: `nw workers update`. Offline: `build_wheel` is replaced by a tiny real zip, and the
record's `ssh_argv` is a stub executable that checks the wire framing of SPEC-0369 (one header
line then `size` bytes, sha256) and answers from the scenario file `$FAKE_STATE/scenario`."""
import hashlib
import json
import stat
import sys
import zipfile

import pytest
from click.testing import CliRunner

from nightwright import buildid
from nightwright.agentic import workers as WK
from nightwright.agentic import workers_bootstrap as WB
from nightwright.cli import cli

from test_remote_gates import _short_runtime_dir  # noqa: F401  (autouse fixture)

pytestmark = pytest.mark.fast

FAKE_SSH = f"""#!{sys.executable}
import hashlib, json, os, sys
S = os.environ["FAKE_STATE"]
scn = open(S + "/scenario").read().strip()
args = sys.argv[1:]
open(S + "/argv", "a").write(json.dumps(args) + "\\n")
if "info" in args:
    b = open(S + "/build").read().strip()
    print(json.dumps({{"protocol": "nw.job.v1", "git": "/usr/bin/git", "version": "0.0.1",
                      "build_id": b, "features": ["async", "update"]}}))
    sys.exit(0)
assert args[args.index("worker") + 1] == "update"
if scn == "notallowed":
    print("nw worker: not allowed", file=sys.stderr)
    sys.exit(126)
h = json.loads(sys.stdin.buffer.readline())
data = sys.stdin.buffer.read()
assert h["schema"] == "nw.worker.update.v1" and h["size"] == len(data), (h, len(data))
assert hashlib.sha256(data).hexdigest() == h["sha256"]
open(S + "/got", "w").write(json.dumps(h))
res = {{"schema": "nw.worker.update.result.v1", "state": scn, "stage": None, "error": None,
       "live_jobs": [], "build_id_before": "b1:" + "0" * 64, "build_id_after": h["build_id"],
       "version": "0.0.1", "forced": "--force" in args}}
code = 0
if scn == "busy":
    res["live_jobs"], code = ["GATEJOB-0002"], 5
if scn == "failed":
    res.update(stage="install", error="uv exploded"); code = 3
if scn in ("updated", "unchanged") and os.path.exists(S + "/stale"):
    open(S + "/build", "w").write(h["build_id"])
print(json.dumps(res))
sys.exit(code)
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    st = tmp_path / "state"
    st.mkdir()
    ssh = tmp_path / "ssh"
    ssh.write_text(FAKE_SSH)
    ssh.chmod(ssh.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_STATE", str(st))
    # NEVER reach the user's real registry (`obj=` is ignored by the cli group)

    def fake_build(out):
        w = __import__("pathlib").Path(out) / "nightwright-0.0.1-py3-none-any.whl"
        with zipfile.ZipFile(w, "w") as z:
            z.writestr("nightwright/__init__.py", "x = 1\n")
        return w
    monkeypatch.setattr(WB, "build_wheel", fake_build)
    cfg = {"data_dir": str(tmp_path / "data"), "agentic": {}}
    p = WK.path(cfg)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"schema": WK.SCHEMA, "workers": [
        {"name": "box", "ssh": "nw@box", "slots": 1, "nw": "/b/nw", "ssh_argv": [str(ssh)]}]}))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    assert str(tmp_path) in str(WK.path(cfg))
    (st / "build").write_text("b1:" + "0" * 64)

    def run(scn, *args):
        (st / "scenario").write_text(scn)
        return CliRunner().invoke(cli, ["workers", "update", "box", *args])
    run.st, run.cfg = st, cfg
    return run


def _calls(world):
    f = world.st / "argv"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_dry_run_sends_nothing(world):
    r = world("updated", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "nightwright-0.0.1-py3-none-any.whl" in r.output and "bytes" in r.output
    assert "nothing sent" in r.output
    assert _calls(world) == []


def test_updated_streams_wire_format_and_checks(world, monkeypatch):
    (world.st / "stale").write_text("")
    r = world("updated")
    assert r.exit_code == 0, r.output
    h = json.loads((world.st / "got").read_text())
    assert h["build_id"].startswith("b1:") and len(h["sha256"]) == 64
    upd = [c for c in _calls(world) if "update" in c]
    assert upd[0][-4:] == ["/b/nw", "worker", "update"][-3:] or upd[0][-3:] == \
        ["/b/nw", "worker", "update"]
    assert "--force" not in upd[0]
    assert "check ok" in r.output and "matches" in r.output


def test_force_only_when_typed(world):
    (world.st / "stale").write_text("")
    r = world("unchanged", "--force")
    assert r.exit_code == 0, r.output
    assert "--force" in [c for c in _calls(world) if "update" in c][0]


def test_busy_exit_5_message(world):
    r = world("busy")
    assert r.exit_code == 5
    assert "worker has live jobs; drain it (nw workers drain box) or retry; " \
           "--force installs without killing jobs" in r.output
    assert "GATEJOB-0002" in r.output
    assert "info" not in _calls(world)[-1]      # no check after a refusal (SPEC-0388: the
    # only `info` probe is the preflight before the upload)


def test_failed_shows_uv_log_hint(world):
    r = world("failed")
    assert r.exit_code == 1
    assert "uv exploded" in r.output and "uv.log" in r.output


def test_old_worker_not_allowed(world):
    r = world("notallowed")
    assert r.exit_code == 1
    assert "not allowed" in r.output and "older worker" in r.output


def test_check_mismatch_after_update_fails(world):
    r = world("updated")                # the fake keeps its old build: check disagrees
    assert r.exit_code == 1
    assert "check after update" in r.output


def test_unknown_worker(world):
    r = CliRunner().invoke(cli, ["workers", "update", "nope"])
    assert r.exit_code != 0 and "no worker named" in r.output

