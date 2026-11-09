"""Prefix facet inheritance (SPEC-0027): resolve_facets exact > first-colon parent."""
from pathlib import Path

from nightwright.rules import load_mappings, resolve_facets
from nightwright.report import _regroup
import pytest

pytestmark = pytest.mark.fast


def _cfg(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    return {"config_dir": str(config)}


def _write_mappings(cfg, text):
    Path(cfg["config_dir"], "mappings.yaml").write_text(text, encoding="utf-8")


def test_resolve_exact(tmp_path):
    cfg = _cfg(tmp_path)
    _write_mappings(cfg, "example-ats:\n  bucket: Example\n")
    assert resolve_facets(cfg, "example-ats") == {"bucket": "Example"}


def test_resolve_inherit_first_colon(tmp_path):
    cfg = _cfg(tmp_path)
    _write_mappings(cfg, "example-ats:\n  bucket: Example\n")
    m = load_mappings(cfg)
    assert resolve_facets(cfg, "example-ats:device-health", m) == {"bucket": "Example"}
    assert resolve_facets(cfg, "example-ats:ota-support", m)["bucket"] == "Example"


def test_resolve_exact_child_overrides(tmp_path):
    cfg = _cfg(tmp_path)
    _write_mappings(cfg, "example-ats:\n  bucket: Example\nexample-ats:ota:\n  bucket: Other\n")
    m = load_mappings(cfg)
    assert resolve_facets(cfg, "example-ats:ota", m) == {"bucket": "Other"}
    assert resolve_facets(cfg, "example-ats:other", m) == {"bucket": "Example"}


def test_resolve_no_parent_key(tmp_path):
    cfg = _cfg(tmp_path)
    _write_mappings(cfg, "example-ats:\n  bucket: Example\n")
    m = load_mappings(cfg)
    # parent key is example-ats-1, not example-ats
    assert resolve_facets(cfg, "example-ats-1:metric-fixes", m) == {}
    assert resolve_facets(cfg, "mystery:branch", m) == {}
    assert resolve_facets(cfg, "plain-topic", m) == {}


def test_resolve_returns_copy_not_alias(tmp_path):
    cfg = _cfg(tmp_path)
    _write_mappings(cfg, "example-ats:\n  bucket: Example\n")
    m = load_mappings(cfg)
    got = resolve_facets(cfg, "example-ats:x", m)
    got["bucket"] = "mutated"
    assert m["example-ats"]["bucket"] == "Example"


def test_regroup_inherits_into_bucket(tmp_path):
    cfg = _cfg(tmp_path)
    _write_mappings(cfg, "example-ats:\n  bucket: Example\n")
    m = load_mappings(cfg)
    topics = {
        "example-ats": 3600.0,
        "example-ats:device-health": 1800.0,
        "scratch": 900.0,
    }
    out = _regroup(topics, "bucket", m)
    assert out["Example"] == 3600.0 + 1800.0
    assert out["scratch"] == 900.0
    assert "example-ats:device-health" not in out


