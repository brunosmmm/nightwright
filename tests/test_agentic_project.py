"""SPEC-0170: `.wt/project.yaml` loader + `wt projects config|init`."""
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from wt.agentic import project as P
from wt.cli import cli

VALID = """\
schema: wt.project.v1
base: main
gates:
  - name: pytest
    cmd: uv run pytest -q -x
  - name: lint
    cmd: python3 tools/spec_lint.py
    timeout_s: 60
    required: false
merge:
  policy: pr
agents:
  context_files: [AGENTS.md]
"""


def _checkout(tmp_path, name="wt-checkout", text=VALID):
    root = tmp_path / name
    (root / "src" / "wt").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'wt'\n")
    if text is not None:
        (root / ".wt").mkdir()
        (root / ".wt" / "project.yaml").write_text(text)
    return root


def _cfg(tmp_path, **extra):
    conf = tmp_path / "config"
    conf.mkdir(exist_ok=True)
    (conf / "mappings.yaml").write_text("{}\n")
    cfg = {"config_dir": str(conf), "data_dir": str(tmp_path / "data"),
           "project_axis": "bucket", "outbox_targets": {}}
    cfg.update(extra)
    return cfg


# ---- loader ----------------------------------------------------------------------------

def test_valid_loads_with_defaults(tmp_path):
    p = tmp_path / "project.yaml"
    p.write_text(VALID)
    doc = P.load_project_config(p)
    assert doc["gates"][0] == {"name": "pytest", "cmd": "uv run pytest -q -x", "cwd": ".",
                               "timeout_s": 1200, "required": True}
    assert doc["gates"][1]["timeout_s"] == 60 and doc["gates"][1]["required"] is False
    assert doc["merge"]["policy"] == "pr" and doc["merge"]["base"] == "main"
    assert doc["merge"]["remote"] == "origin" and doc["merge"]["pr"]["labels"] == []
    assert doc["worktrees"] == {"root": None, "setup": [], "cleanup": "on_merge"}
    assert doc["agents"]["permission_mode"] == "bypassPermissions"
    assert doc["agents"]["context_files"] == ["AGENTS.md"]


@pytest.mark.parametrize("text,needle", [
    ("schema: wt.project.v2\n", "schema:"),
    ("schema: wt.project.v1\nbogus: 1\n", "project config.bogus: unknown key"),
    ("schema: wt.project.v1\ngates:\n  - name: x\n", "gates[0].cmd"),
    ("schema: wt.project.v1\ngates:\n  - name: x\n    cmd: y\n    timeout_s: 0\n", "gates[0].timeout_s"),
    ("schema: wt.project.v1\ngates:\n  - {name: x, cmd: y}\n  - {name: x, cmd: z}\n", "duplicate gate name"),
    ("schema: wt.project.v1\nmerge:\n  policy: rebase\n", "merge.policy"),
    ("schema: wt.project.v1\nmerge:\n  nope: 1\n", "merge.nope"),
    ("schema: wt.project.v1\nworktrees:\n  cleanup: sometimes\n", "worktrees.cleanup"),
    ("schema: wt.project.v1\nagents:\n  permission_mode: yolo\n", "agents.permission_mode"),
    ("schema: wt.project.v1\nagents:\n  allowed_tools: Bash\n", "agents.allowed_tools"),
])
def test_invalid_reports_key_path(tmp_path, text, needle):
    p = tmp_path / "project.yaml"
    p.write_text(text)
    with pytest.raises(ValueError, match=needle.replace("[", r"\[").replace("]", r"\]")):
        P.load_project_config(p)


def test_several_problems_in_one_error(tmp_path):
    p = tmp_path / "project.yaml"
    p.write_text("schema: nope\nbogus: 1\ngates:\n  - name: x\n")
    with pytest.raises(ValueError) as exc:
        P.load_project_config(p)
    msg = str(exc.value)
    assert "schema:" in msg and "bogus" in msg and "gates[0].cmd" in msg


def test_missing_and_bad_yaml(tmp_path):
    with pytest.raises(ValueError, match="no project config"):
        P.load_project_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema: [unclosed\n")
    with pytest.raises(ValueError, match="not valid YAML"):
        P.load_project_config(bad)


# ---- resolution -------------------------------------------------------------------------

def test_meta_tools_resolves_via_wt_checkout(tmp_path):
    root = _checkout(tmp_path)
    doc = P.project_config(_cfg(tmp_path, wt_checkout=str(root)), "Meta-Tools")
    assert doc["_root"] == str(root) and doc["_path"].endswith(".wt/project.yaml")


def test_outbound_resolves_via_repo_path(tmp_path):
    repo = tmp_path / "acme"
    (repo / ".wt").mkdir(parents=True)
    (repo / ".wt" / "project.yaml").write_text(VALID)
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"repo_path": str(repo)}})
    assert P.project_config(cfg, "acme")["_root"] == str(repo.resolve())


def test_missing_file_hints_init(tmp_path):
    root = _checkout(tmp_path, text=None)
    with pytest.raises(ValueError, match="wt projects init Meta-Tools"):
        P.project_config(_cfg(tmp_path, wt_checkout=str(root)), "Meta-Tools")


def test_unresolvable_project_carries_registry_note(tmp_path):
    with pytest.raises(ValueError, match="wt projects add nowhere"):
        P.project_config(_cfg(tmp_path), "nowhere")


def test_project_name_mismatch_errors(tmp_path):
    root = _checkout(tmp_path, text=VALID.replace("base: main", "project: other\nbase: main"))
    with pytest.raises(ValueError, match="does not match"):
        P.project_config(_cfg(tmp_path, wt_checkout=str(root)), "Meta-Tools")


# ---- helpers ---------------------------------------------------------------------------

def test_worktree_root_default_and_overrides(tmp_path):
    repo = tmp_path / "work" / "wt"
    repo.mkdir(parents=True)
    assert P.worktree_root({"worktrees": {"root": None}}, repo, "Meta-Tools") == \
        tmp_path / "work" / ".wt-worktrees" / "Meta-Tools"
    assert P.worktree_root({"worktrees": {"root": "/abs/wts"}}, repo, "x") == Path("/abs/wts")
    assert P.worktree_root({"worktrees": {"root": "../wts"}}, repo, "x") == \
        (tmp_path / "work" / "wts").resolve()


def test_expand_cmd_only_known_placeholders():
    out = P.expand_cmd("run {spec_id} in {worktree} on {base} {campaign} {other}",
                       spec_id="SPEC-0001", worktree="/w", base="main", campaign="C")
    assert out == "run SPEC-0001 in /w on main C {other}"
    assert P.expand_cmd("echo {spec_id}") == "echo {spec_id}"


def test_find_project_for_cwd(tmp_path):
    repo = tmp_path / "acme"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    sub = repo / "deep"
    sub.mkdir()
    cfg = _cfg(tmp_path, outbox_targets={"acme": {"repo_path": str(repo)}})
    assert P.find_project_for_cwd(cfg, cwd=sub) == "acme"
    assert P.find_project_for_cwd(cfg, cwd=tmp_path) is None


# ---- CLI --------------------------------------------------------------------------------

def test_cli_config_json_and_human(tmp_path, monkeypatch):
    root = _checkout(tmp_path)
    cfg = _cfg(tmp_path, wt_checkout=str(root))
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects", "config", "Meta-Tools", "--json"])
    assert r.exit_code == 0, r.output
    payload = json.loads(r.output)
    assert payload["schema"] == "wt.project.config.v1"
    assert payload["project"] == "Meta-Tools" and payload["root"] == str(root)
    assert payload["config"]["gates"][0]["timeout_s"] == 1200
    assert "_root" not in payload["config"]
    r = CliRunner().invoke(cli, ["projects", "config", "Meta-Tools"])
    assert r.exit_code == 0, r.output
    assert "project.yaml" in r.output and "policy: pr" in r.output


def test_cli_config_errors(tmp_path, monkeypatch):
    root = _checkout(tmp_path, text=None)
    cfg = _cfg(tmp_path, wt_checkout=str(root))
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects", "config", "Meta-Tools"])
    assert r.exit_code != 0 and "wt projects init" in r.output
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["projects", "config"])
    assert r.exit_code != 0 and "no project given" in r.output


def test_cli_init_dry_run_then_write_then_refuse(tmp_path, monkeypatch):
    root = _checkout(tmp_path, text=None)
    cfg = _cfg(tmp_path, wt_checkout=str(root))
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    path = root / ".wt" / "project.yaml"
    r = CliRunner().invoke(cli, ["projects", "init", "Meta-Tools"])
    assert r.exit_code == 0, r.output
    assert "not written" in r.output and "schema: wt.project.v1" in r.output
    assert not path.exists()
    r = CliRunner().invoke(cli, ["projects", "init", "Meta-Tools", "--yes"])
    assert r.exit_code == 0, r.output
    assert path.is_file()
    doc = P.load_project_config(path)                 # the starter validates
    assert doc["project"] == "Meta-Tools" and doc["gates"][0]["name"] == "tests"
    r = CliRunner().invoke(cli, ["projects", "init", "Meta-Tools", "--yes"])
    assert r.exit_code != 0 and "already exists" in r.output
    r = CliRunner().invoke(cli, ["projects", "init", "Meta-Tools", "--yes", "--force"])
    assert r.exit_code == 0, r.output


def test_starter_is_valid_yaml_and_strict():
    doc = yaml.safe_load(P.STARTER.format(project="X"))
    P.validate(doc)


def _regen_doc(regen):
    return {"schema": P.SCHEMA, "merge": {"regenerate": regen}}


def test_merge_regenerate_default_and_valid():
    assert P.PROJECT_DEFAULTS["merge"]["regenerate"] == []
    P.validate(_regen_doc([{"path": "docs/LEDGER.md", "cmd": "make ledger"}]))
    P.validate(_regen_doc([]))


@pytest.mark.parametrize("bad", [
    "nope", ["x"], [{"path": "a"}], [{"path": "", "cmd": "c"}],
    [{"path": "a", "cmd": "c", "extra": 1}], [{"path": 1, "cmd": "c"}]])
def test_merge_regenerate_rejects_bad(bad):
    with pytest.raises(ValueError, match="merge.regenerate"):
        P.validate(_regen_doc(bad))
