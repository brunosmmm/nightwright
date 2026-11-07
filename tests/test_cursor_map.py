"""SPEC-0436: Cursor model map is YAML (builtin / user / config / project), not a Python table."""
import pytest

from nightwright.agentic import cursor_map as CM
from nightwright.agentic import profiles as P
from nightwright.agentic import runner as R


def test_builtin_map_covers_aliases_and_effort_fallback(tmp_path):
    cfg = {"config_dir": str(tmp_path)}
    assert CM.map_model("sonnet", "high", cfg) == "grok-4.7-high"
    assert CM.map_model("sonnet", None, cfg) == "grok-4.7-high"          # defaults.sonnet = high
    assert CM.map_model("haiku", None, cfg) == "grok-4.7-low"            # defaults.haiku = medium
    assert CM.map_model("haiku", "bogus", cfg) == "grok-4.7-low"
    assert CM.map_model("opus", "xhigh", cfg) == "gpt-5.6-sol-xhigh"


def test_user_yaml_and_config_and_project_overlay(tmp_path):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "cursor_map.yaml").write_text(
        "schema: nw.cursor_map.v1\naliases:\n  sonnet:\n    high: grok-user\n")
    project = tmp_path / "proj"
    (project / ".nw").mkdir(parents=True)
    (project / ".nw" / "cursor_map.yaml").write_text(
        "schema: nw.cursor_map.v1\naliases:\n  sonnet:\n    high: grok-project\n")
    cfg = {"config_dir": str(cfg_dir),
           "agentic": {"cursor": {"aliases": {"sonnet": {"medium": "grok-config"}}}}}
    assert CM.map_model("sonnet", "high", cfg) == "grok-user"
    assert CM.map_model("sonnet", "medium", cfg) == "grok-config"
    assert CM.map_model("sonnet", "high", cfg, project_root=str(project)) == "grok-project"
    assert CM.map_model("haiku", "high", cfg) == "grok-4.7-medium"       # builtin still under


def test_flat_alias_map_in_config(tmp_path):
    cfg = {"config_dir": str(tmp_path),
           "agentic": {"cursor": {"alias_map": {"sonnet/high": "grok-flat"}}}}
    assert CM.map_model("sonnet", "high", cfg) == "grok-flat"


def test_cursor_model_on_persona_wins(tmp_path):
    cfg = {"config_dir": str(tmp_path)}
    assert CM.map_model("sonnet", "high", cfg, cursor_model="gpt-5.6-sol-high") == "gpt-5.6-sol-high"
    with pytest.raises(ValueError, match="rejects Claude"):
        CM.map_model("sonnet", "high", cfg, cursor_model="claude-opus-4")


def test_rejects_claude_ids_and_missing_alias(tmp_path):
    cfg = {"config_dir": str(tmp_path)}
    with pytest.raises(ValueError, match="rejects Claude"):
        CM.map_model("claude-sonnet-4", "high", cfg)
    assert CM.map_model("grok-4.7-high", None, cfg) == "grok-4.7-high"   # pass-through


def test_profile_cursor_model_is_a_valid_persona_key(tmp_path):
    origin = "user:overlay.yaml"
    assert P.validate_profile_doc(
        {"personas": {"implementer": {"model": "sonnet", "cursor_model": "grok-4.7-high"}}},
        origin=origin, in_file=False) == []
    err = P.validate_profile_doc(
        {"personas": {"implementer": {"cursor_model": "claude-opus-4"}}},
        origin=origin, in_file=False)
    assert any("cursor_model" in e and "Claude" in e for e in err)


def test_map_launch_model_claude_sonnet_is_sonnet_default_effort(tmp_path):
    cfg = {"config_dir": str(tmp_path)}
    assert CM.map_launch_model("claude-sonnet-5-5", "medium", cfg) == "grok-4.7-high"
    assert CM.map_launch_model("claude-opus-4", None, cfg) == "gpt-5.6-sol-high"
    assert CM.map_launch_model("sonnet", "medium", cfg) == "grok-4.7-medium"


def test_sdk_selection_splits_effort_into_the_model_parameter(tmp_path):
    cfg = {"config_dir": str(tmp_path)}
    assert CM.sdk_selection(CM.map_model("sonnet", "high", cfg)) == {
        "id": "grok-4.7", "params": [{"id": "reasoning_effort", "value": "high"}]}
    assert CM.sdk_selection(CM.map_model("haiku", "medium", cfg)) == {
        "id": "grok-4.7", "params": [{"id": "reasoning_effort", "value": "low"}]}
    assert CM.sdk_selection(CM.map_model("opus", "xhigh", cfg)) == {
        "id": "gpt-5.6-sol", "params": [{"id": "reasoning", "value": "xhigh"}]}
    assert CM.sdk_selection("composer-2.5") == {"id": "composer-2.5", "params": []}
    assert CM.sdk_selection("grok-4.7-high")["id"] == "grok-4.7"


def test_sdk_selection_uses_each_model_familys_parameter():
    assert CM.sdk_selection("claude-sonnet-5-5-high") == {
        "id": "claude-sonnet-5-5",
        "params": [{"id": "reasoning_effort", "value": "high"}]}
    assert CM.sdk_selection("claude-opus-5-5-max") == {
        "id": "claude-opus-5-5", "params": [{"id": "effort", "value": "max"}]}
    assert CM.sdk_selection("grok-4.6-low") == {
        "id": "grok-4.6", "params": [{"id": "effort", "value": "low"}]}
    assert CM.sdk_selection("gpt-5.4-xhigh") == {
        "id": "gpt-5.4", "params": [{"id": "reasoning", "value": "extra-high"}]}
    assert CM.sdk_selection("claude-sonnet-4-6-xhigh") == {
        "id": "claude-sonnet-4-6", "params": [{"id": "effort", "value": "max"}]}
    assert CM.sdk_selection("kimi-k3-medium") == {
        "id": "kimi-k3", "params": [{"id": "reasoning", "value": "high"}]}
    assert CM.sdk_selection("composer-2.5-high") == {"id": "composer-2.5", "params": []}
    assert CM.sdk_selection("gpt-5.4-mini-high")["id"] == "gpt-5.4-mini"
    assert CM.sdk_selection("not-a-catalog-model-high") == {
        "id": "not-a-catalog-model-high", "params": []}


def test_mapped_model_only_translates_when_runner_is_cursor(tmp_path, monkeypatch):
    monkeypatch.delenv("NW_AGENTIC_RUNNER", raising=False)
    cfg = {"config_dir": str(tmp_path), "agentic": {"runner": "claude"}}
    model, alias = R.mapped_model(cfg, "sonnet", "high")
    assert model == "sonnet" and alias is None
    cfg["agentic"]["runner"] = "cursor"
    model, alias = R.mapped_model(cfg, "sonnet", "high")
    assert model == "grok-4.7-high" and alias == "sonnet"
