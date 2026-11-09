"""Spec-markdown parsing lives in the nightwright package (nightwright.specmeta), NOT tools/spec_lint.py — so the
installed CLI never reaches into repo dev-tooling (which imports dev-only jsonschema) at runtime.
Regression guard for the ModuleNotFoundError the installed `nw spec new` hit."""
import pathlib

import nightwright.specs
from nightwright.specmeta import sections, split_frontmatter
import pytest

pytestmark = pytest.mark.fast


def test_split_frontmatter_and_sections():
    fm, body = split_frontmatter("---\nid: SPEC-0001\ntitle: X\n---\n## Context\n\nhi\n\n## Decision\n\nyo\n")
    assert fm["id"] == "SPEC-0001"
    s = sections(body)
    assert s["Context"] == "hi" and s["Decision"] == "yo"


def test_no_frontmatter():
    fm, body = split_frontmatter("# just markdown\n")
    assert fm is None and body == "# just markdown\n"


def test_specs_module_has_no_runtime_dep_on_tools_or_jsonschema():
    # check the actual mechanism, not word mentions: no dynamic exec of tools/spec_lint.py and
    # no dev-only imports. (The installed `nw` hit ModuleNotFoundError: jsonschema before this.)
    src = pathlib.Path(nightwright.specs.__file__).read_text()
    assert "importlib" not in src, "no dynamic module loading of the repo linter"
    assert "exec_module" not in src
    assert "import jsonschema" not in src


