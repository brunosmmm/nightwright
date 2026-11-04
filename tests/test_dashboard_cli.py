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
    assert lines[1] == ("active 1 · queued 0 · parked 2 · waiting 0 · backlog 0 · "
                        "conductor-handling 0")
    assert any(ln.startswith("spend today $") for ln in lines)
    assert any(ln.startswith("ESC-0001 SPEC-0902-20261103-bbbb ship it?") for ln in lines)
    assert any(ln.startswith("park SPEC-0903 ") and "main_dirty" in ln for ln in lines)
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
