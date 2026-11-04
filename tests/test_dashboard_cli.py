"""SPEC-0401: `nw dashboard snapshot [--json]` as a real subprocess against the seeded data dir,
with no server, no `tailscale` on PATH and no token."""
import json
import os
import subprocess
import sys
from pathlib import Path

from fixtures.dashboard_data import seed
from test_dashboard_snapshot import check_schema


def nw_argv():
    """The venv's `nw` entry point (what `uv run nw` runs), else `python -m nightwright`."""
    exe = Path(sys.executable).parent / "nw"
    return [str(exe)] if exe.is_file() else [sys.executable, "-m", "nightwright"]


def nw_env(fx):
    path = [p for p in os.environ.get("PATH", "").split(os.pathsep)
            if p and not (Path(p) / "tailscale").exists()]
    env = {k: v for k, v in os.environ.items() if not k.startswith("NW_DASHBOARD")}
    env.update(fx["env"], PATH=os.pathsep.join(path))
    return env


def run_nw(fx, *args):
    return subprocess.run([*nw_argv(), "dashboard", "snapshot", *args], env=nw_env(fx),
                          capture_output=True, text=True, timeout=120)


def test_json_form_passes_the_schema_check(tmp_path):
    fx = seed(tmp_path)
    r = run_nw(fx, "--json")
    assert r.returncode == 0, r.stderr
    doc = json.loads(r.stdout)
    check_schema(doc)
    assert [e["id"] for e in doc["inbox"]["escalations"]] == ["ESC-0001"]


def test_text_form_has_health_counts_spend_and_needs_you(tmp_path):
    fx = seed(tmp_path)
    r = run_nw(fx)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0].startswith("runner down") and "conductor down" in lines[0]
    assert lines[1] == ("active 1 · queued 0 · parked 3 · waiting 0 · backlog 0 · "
                        "conductor-handling 0")
    assert any(ln.startswith("spend today $") for ln in lines)
    assert any(ln.startswith("ESC-0001 SPEC-0902-20261103-bbbb ship it?") for ln in lines)
    assert any(ln.startswith("park SPEC-0903 ") and "main_dirty" in ln for ln in lines)
    assert any(ln.startswith("park SPEC-0905 ") and "main_not_on_base" in ln for ln in lines)
    assert "ESC-0002" not in r.stdout and "ESC-0003" not in r.stdout
    assert "nothing needs you" not in r.stdout


def test_text_form_reports_a_failed_section(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from nightwright.agentic import escalations as ESC
    from nightwright.cli import cli
    fx = seed(tmp_path)
    monkeypatch.setattr("nightwright.cli.load_config", lambda: fx["cfg"])

    def boom(*a, **k):
        raise OSError("gone")
    monkeypatch.setattr(ESC, "list_all", boom)
    r = CliRunner().invoke(cli, ["dashboard", "snapshot"])
    assert r.exit_code == 0, r.output
    assert "inbox.escalations: error: OSError: gone" in r.output


def test_a_failing_build_exits_1(monkeypatch):
    from click.testing import CliRunner
    from nightwright.agentic.dashboard import snapshot as SN
    from nightwright.cli import cli
    monkeypatch.setattr("nightwright.cli.load_config", lambda: {"data_dir": "/x"})

    def boom(*a, **k):
        raise KeyError("data_dir")
    monkeypatch.setattr(SN, "build", boom)
    r = CliRunner().invoke(cli, ["dashboard", "snapshot", "--json"])
    assert r.exit_code == 1 and "KeyError" in r.output


def test_a_config_without_a_data_dir_exits_1(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from nightwright.cli import cli
    monkeypatch.setattr("nightwright.cli.load_config", lambda: {"agentic": {}})
    r = CliRunner().invoke(cli, ["dashboard", "snapshot"])
    assert r.exit_code == 1 and "no data_dir" in r.output
    missing = tmp_path / "nope"
    monkeypatch.setattr("nightwright.cli.load_config",
                        lambda: {"data_dir": str(missing), "agentic": {}})
    r = CliRunner().invoke(cli, ["dashboard", "snapshot", "--json"])
    assert r.exit_code == 1 and "does not exist" in r.output and not missing.exists()


# ---- the text summary's branches (`text_lines` on crafted documents) --------------------

def _doc(lease=None, runner_code=None, cond_pid=None, escalations=(), parks=()):
    return {"health": {"runner": {"lease": lease or {"held": False},
                                  "runner_code": runner_code,
                                  "draining": (lease or {}).get("draining")},
                       "conductor": {"pid": cond_pid, "lag": 0, "cursor_age_s": 1}},
            "totals": {"active": 2, "ready": 1, "attention": 0, "waiting": 0, "backlog": 4,
                       "conductor_handling": 0, "spend_today_usd": 3.5},
            "inbox": {"escalations": list(escalations), "parks": list(parks)}}


def test_text_held_runner_draining_stale_code_and_conductor_pid():
    from nightwright.agentic.cli_dashboard import text_lines
    lines = text_lines(_doc(lease={"held": True, "pid": 77, "host": "box",
                                   "heartbeat_age_s": 4.5, "draining": {"since": "t"}},
                            runner_code={"stale": True}, cond_pid=1234))
    assert lines[0] == "runner 77@box heartbeat 4.5s · draining · stale code · conductor 1234"
    assert lines[2] == "spend today $3.50"
    assert lines[-1] == "nothing needs you"


def test_text_stale_lease_without_host():
    from nightwright.agentic.cli_dashboard import text_lines
    lines = text_lines(_doc(lease={"held": True, "pid": 77, "heartbeat_age_s": 300,
                                   "stale": True}))
    assert lines[0] == "runner 77 heartbeat 300s · stale lease · conductor down"


def test_text_park_lines_keep_the_detail_verbatim():
    from nightwright.agentic.cli_dashboard import text_lines
    parks = [{"campaign": "C1", "label": "SPEC-1", "park": {"reason": "main_dirty",
                                                           "detail": "see: "}},
             {"campaign": "C2", "label": None, "park": {"reason": "main_not_on_base",
                                                       "detail": None}}]
    lines = text_lines(_doc(parks=parks, escalations=[
        {"id": "ESC-0009", "campaign": None, "question": "go?"}]))
    assert "ESC-0009 - go?" in lines
    assert "park SPEC-1 C1 main_dirty: see: " in lines
    assert "park C2 C2 main_not_on_base" in lines
    assert "nothing needs you" not in lines


def test_text_prints_every_failed_section():
    from nightwright.agentic.cli_dashboard import text_lines
    doc = _doc()
    doc["health"]["stewards"] = {"error": "ValueError: bad inbox"}
    doc["health"]["herdr_watch"] = {"error": "OSError: gone"}
    doc["needs_you"] = {"error": "KeyError: x"}
    doc["conductor_handling"] = {"error": "KeyError: y"}
    lines = text_lines(doc)
    for want in ("health.stewards: error: ValueError: bad inbox",
                 "health.herdr_watch: error: OSError: gone", "needs_you: error: KeyError: x",
                 "conductor_handling: error: KeyError: y"):
        assert want in lines
