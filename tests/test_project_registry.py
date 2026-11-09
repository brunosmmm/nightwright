"""SPEC-0189: one project registry — mapping buckets ∪ outbox_targets ∪ Meta-Tools — for the
capture warning, --project completion, idea_extensions, Breakdown annotations, promotion
routing and the `nw projects` table."""
import pytest
from click.testing import CliRunner

from test_assoc import _cfg
from nightwright import completion as C
from nightwright import extension_schema as X
from nightwright import org_write as W
from nightwright import rules as RU
from nightwright import specs as S
from nightwright.cli import cli

pytestmark = pytest.mark.fast


def _with_outbox(tmp_path, mappings=None):
    cfg = _cfg(tmp_path, mappings=mappings)
    repo = tmp_path / "comfort"
    repo.mkdir()
    cfg["outbox_targets"] = {"comfort-assistant": {"repo_path": str(repo)}}
    return cfg


def test_registered_projects_is_the_union(tmp_path):
    cfg = _with_outbox(tmp_path, mappings={"repoA": "Logging"})
    assert RU.registered_projects(cfg) == ["Logging", "Meta-Tools", "comfort-assistant"]
    assert RU.known_projects(cfg) == ["Logging"]          # mapping buckets keep their meaning
    assert [p["name"] for p in RU.projects_payload(cfg)["projects"]] == RU.registered_projects(cfg)


@pytest.mark.parametrize("project", ["comfort-assistant", "Meta-Tools", "Logging"])
def test_capture_on_a_registered_project_does_not_warn(tmp_path, capsys, project):
    cfg = _with_outbox(tmp_path, mappings={"repoA": "Logging"})
    W.add_idea(cfg, f"idea for {project}", project=project)
    assert "unknown project" not in capsys.readouterr().err


def test_capture_on_an_unknown_project_warns_with_the_registry(tmp_path, capsys):
    cfg = _with_outbox(tmp_path)
    W.add_idea(cfg, "stray", project="Nope")
    err = capsys.readouterr().err
    assert "unknown project 'Nope'" in err
    assert "Meta-Tools" in err and "comfort-assistant" in err


def test_project_completion_offers_outbox_and_internal(tmp_path, monkeypatch):
    cfg = _with_outbox(tmp_path)
    monkeypatch.setattr(C, "_cfg_from_ctx", lambda ctx: cfg)
    names = [getattr(i, "value", i) for i in C.complete_project(None, None, "")]
    assert "comfort-assistant" in names and "Meta-Tools" in names


def test_extension_schema_accepts_outbox_projects(tmp_path, capsys, monkeypatch):
    cfg = _with_outbox(tmp_path)
    cfg["idea_extensions"] = {"comfort-assistant": {}, "Ghost": {}}
    monkeypatch.setattr(X, "_WARNED_UNKNOWN", set())
    X._warn_unknown_extension_projects(cfg)
    err = capsys.readouterr().err
    assert "Ghost" in err and "comfort-assistant" not in err


def test_meta_tools_idea_promotes_internally_without_flag(tmp_path):
    cfg = _with_outbox(tmp_path)
    W.add_idea(cfg, "internal thing", project="Meta-Tools")
    path, spec_id, kind = S.promote_idea(cfg, "internal thing", force=True)
    assert kind == "internal" and spec_id.startswith("SPEC-") and path.exists()


def test_unconfigured_project_still_refuses_promotion(tmp_path):
    cfg = _with_outbox(tmp_path, mappings={"repoA": "Logging"})
    W.add_idea(cfg, "logging thing", project="Logging")
    with pytest.raises(ValueError, match="no outbound target"):
        S.promote_idea(cfg, "logging thing", force=True)


def test_cli_projects_table_lists_every_registered_project(tmp_path, monkeypatch):
    cfg = _with_outbox(tmp_path, mappings={"repoA": "Logging"})
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["projects"])
    assert r.exit_code == 0, r.output
    for name in ("Logging", "Meta-Tools", "comfort-assistant"):
        assert name in r.output
    row = next(line for line in r.output.splitlines() if "comfort-assistant" in line)
    flat = "".join(r.output.split())          # the root column folds long paths at 80 cols
    assert "✓" in row and str(tmp_path / "comfort") in flat
