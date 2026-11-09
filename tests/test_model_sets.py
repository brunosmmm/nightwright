"""SPEC-0455: a model set binds each persona to a runner and a concrete model."""
import json
from types import SimpleNamespace

import pytest

from nightwright.agentic import queue as Q
from nightwright.agentic import runner as R
from nightwright.agentic.model_sets import bind, resolve_model_set

pytestmark = pytest.mark.fast


def _set(name):
    return resolve_model_set({"agentic": {"model_set": name}})


def test_builtins_are_claude_grok_and_mixed():
    assert resolve_model_set({"agentic": {}}) is None
    assert resolve_model_set({}) is None
    names = { _set(name)["name"] for name in ("claude", "grok", "mixed") }
    assert names == {"claude", "grok", "mixed"}


def test_unknown_model_set_names_the_known_ones():
    with pytest.raises(ValueError, match="unknown model set 'nope'") as raised:
        resolve_model_set({"agentic": {"model_set": "nope"}})
    text = str(raised.value)
    assert "claude" in text and "grok" in text and "mixed" in text


def test_grok_appends_the_profile_effort_and_leaves_composer_bare():
    grok = _set("grok")
    assert bind(grok, "implementer", "sonnet", "high") == {
        "runner": "cursor", "model": "composer-2.5", "model_alias": "sonnet"}
    assert bind(grok, "conflict_resolver", "sonnet", "high")["model"] == "composer-2.5"
    assert bind(grok, "conformance_reviewer", "opus", "high") == {
        "runner": "cursor", "model": "grok-4.7-high", "model_alias": "opus"}
    assert bind(grok, "gate_triager", "sonnet", "medium")["model"] == "grok-4.7-medium"
    assert bind(grok, "spec_reviewer", "opus", "max")["model"] == "grok-4.7-xhigh"


def test_mixed_sends_writers_to_claude_and_reviewers_to_grok():
    mixed = _set("mixed")
    assert bind(mixed, "implementer", "sonnet", "high") == {
        "runner": "claude", "model": "sonnet", "model_alias": None}
    assert bind(mixed, "spec_writer", "opus", "high")["runner"] == "claude"
    assert bind(mixed, "conflict_resolver", "sonnet", "high")["model"] == "sonnet"
    for persona, alias, effort, model in (
            ("conformance_reviewer", "opus", "high", "grok-4.7-high"),
            ("test_reviewer", "opus", "high", "grok-4.7-high"),
            ("adversarial_reviewer", "opus", "high", "grok-4.7-high"),
            ("spec_reviewer", "sonnet", "high", "grok-4.7-high"),
            ("review_triager", "sonnet", "medium", "grok-4.7-medium"),
            ("gate_triager", "sonnet", "medium", "grok-4.7-medium"),
            ("divergence_triager", "sonnet", "medium", "grok-4.7-medium")):
        assert bind(mixed, persona, alias, effort) == {
            "runner": "cursor", "model": model, "model_alias": alias}


def test_gpt_set_binds_every_persona_to_pi_and_openai():
    from nightwright.agentic.personas import PERSONAS
    from nightwright.agentic.pi_map import probe_providers
    selected = _set("gpt")
    for persona in PERSONAS:
        bound = bind(selected, persona, "sonnet", "high")
        assert bound["runner"] == "pi"
        assert bound["model"].startswith("openai/gpt-")
    assert bind(selected, "implementer", "sonnet", "high")["model"] == "openai/gpt-6.1-terra"
    assert bind(selected, "conformance_reviewer", "opus", "high")["model"] == "openai/gpt-5.6-sol"
    assert bind(selected, "test_reviewer", "opus", "high")["model"] == "openai/gpt-5.6-luna"
    cfg = {"agentic": {"runner": "claude", "model_set": "gpt"}}
    assert R.pi_needed(cfg, campaigns=[])
    assert probe_providers(cfg, campaigns=[]) == ["openai"]


def test_pi_set_rejects_invalid_provider_model(tmp_path):
    dest = tmp_path / ".nw" / "model_sets"
    dest.mkdir(parents=True)
    (dest / "bad.yaml").write_text("schema: nw.model_set.v1\nrunner: pi\nmodel: nope\n")
    with pytest.raises(ValueError, match="provider/id"):
        resolve_model_set({"agentic": {"model_set": "bad"}}, project_root=tmp_path)


def test_claude_set_keeps_the_alias_on_claude_code():
    claude = _set("claude")
    assert bind(claude, "conformance_reviewer", "opus", "high") == {
        "runner": "claude", "model": "opus", "model_alias": None}
    assert bind(claude, "implementer", "sonnet", "high")["runner"] == "claude"


def test_a_project_file_replaces_a_builtin(tmp_path):
    (tmp_path / ".nw" / "model_sets").mkdir(parents=True)
    (tmp_path / ".nw" / "model_sets" / "grok.yaml").write_text(
        "schema: nw.model_set.v1\nrunner: cursor\nmodel: grok-4.7\n", encoding="utf-8")
    doc = resolve_model_set({"agentic": {"model_set": "grok"}}, project_root=tmp_path)
    assert "personas" not in doc or not doc.get("personas")
    assert bind(doc, "implementer", "sonnet", "high")["model"] == "grok-4.7-high"


def test_cursor_needed_follows_the_set():
    assert R.cursor_needed({"agentic": {"runner": "claude", "model_set": "grok"}},
                           campaigns=[]) is True
    assert R.cursor_needed({"agentic": {"runner": "claude", "model_set": "mixed"}},
                           campaigns=[]) is True
    assert R.cursor_needed({"agentic": {"runner": "claude", "model_set": "claude"}},
                           campaigns=[]) is False
    assert R.cursor_needed({"agentic": {"runner": "claude"}}, campaigns=[]) is False


def test_request_roundtrip_keeps_the_runner(tmp_path):
    req = R.RunRequest(
        persona="implementer", prompt="p", system_append="", cwd=tmp_path, model="composer-2.5",
        effort="high", max_budget_usd=1, timeout_s=10, permission_mode="dontAsk",
        read_only=False, allowed_tools=(), add_dirs=(), json_schema={}, env={},
        run_dir=tmp_path, runner="cursor")
    doc = R._request_doc(req, "t")
    assert doc["runner"] == "cursor"
    (tmp_path / "request.json").write_text(json.dumps(doc), encoding="utf-8")
    (tmp_path / "prompt.md").write_text("p", encoding="utf-8")
    assert R.rebuild_request(tmp_path).runner == "cursor"


def test_local_runner_passes_the_request_runner():
    class Ctx:
        def runner_for(self, state=None, *, kind=None):
            self.kind = kind
            return kind or "host"

    ctx = Ctx()
    assert Q._local_runner(ctx, {"id": "x"}, SimpleNamespace(runner="cursor")) == "cursor"
    assert ctx.kind == "cursor"
    assert Q._local_runner(ctx, {"id": "x"}, SimpleNamespace(runner=None)) == "host"
    assert ctx.kind is None

    class Old:
        def runner_for(self, state=None):
            return "old"

    assert Q._local_runner(Old(), {}, SimpleNamespace(runner="cursor")) == "old"
