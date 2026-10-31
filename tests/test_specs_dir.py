"""SPEC-0079: `_specs_dir` must not resolve against the current working directory.

The old fallback was `Path.cwd() / "docs" / "specs"`. Because pytest runs from the repo root, a
test whose config fixture omitted `specs_dir` silently resolved — and wrote — against the live
`docs/specs` while believing it was sandboxed in `tmp_path`. That actually happened during
SPEC-0078 (two tracked spec files were modified and reverted), and `promote_idea` would have
created a brand-new tracked spec file the same way.
"""
import os
import re
from pathlib import Path

import pytest

from nightwright import specs as S


def test_explicit_specs_dir_wins(tmp_path):
    want = tmp_path / "elsewhere"
    assert S._specs_dir({"specs_dir": str(want)}) == want


def test_default_is_independent_of_cwd(tmp_path, monkeypatch):
    """The actual bug: the answer must not change with the working directory."""
    here = S._specs_dir({})
    monkeypatch.chdir(tmp_path)
    assert S._specs_dir({}) == here
    assert Path.cwd() != here.parent.parent          # genuinely ran from somewhere else


def test_default_is_wts_own_specs_dir():
    resolved = S._specs_dir({})
    assert resolved.is_dir()
    assert resolved.name == "specs" and resolved.parent.name == "docs"
    # located from the package, not the cwd
    assert resolved == Path(S.__file__).resolve().parents[2] / "docs" / "specs"


def test_missing_docs_specs_raises(tmp_path, monkeypatch):
    """An installed nightwright with no spec corpus must say so, not guess a path."""
    monkeypatch.setattr(S, "_package_specs_dir", lambda: tmp_path / "nope" / "docs" / "specs")
    with pytest.raises(ValueError, match="specs_dir"):
        S._specs_dir({})


def test_empty_string_specs_dir_falls_back_not_crashes():
    assert S._specs_dir({"specs_dir": ""}) == S._specs_dir({})


# ---- meta: no fixture may omit the key -------------------------------------------

# Config-key form (dict key or YAML key), not a bare mention such as a docs phrase.
_CFG_MARKER = re.compile(r"org_ideas_file[\"']?\s*:")


def test_no_test_fixture_omits_specs_dir():
    """A runtime assertion cannot catch this — the failure mode is a *new* fixture forgetting
    the key, in a test that may not even touch spec resolution today. So scan the suite."""
    offenders = []
    for path in sorted(Path("tests").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _CFG_MARKER.search(text) and "specs_dir" not in text:
            offenders.append(path.name)
    assert not offenders, (
        "these fixtures build a nightwright config without `specs_dir`, so spec lookups would resolve "
        f"against the live docs/specs: {offenders}")


def test_specs_dir_has_no_cwd_fallback_in_source():
    """Guard the fix itself: re-introducing a cwd-relative default would silently re-arm the
    trap, and every existing test would still pass."""
    src = Path(S.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _specs_dir("):]
    body = body[:body.index("\ndef ", 1)]
    assert "cwd()" not in body and "getcwd" not in body, body
