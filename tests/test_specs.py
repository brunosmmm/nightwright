"""Enforce the spec system in CI: every spec must pass the linter (SPEC-0002 / SPEC-0003).

Loads the standalone dev linter (tools/spec_lint.py) and runs it over docs/specs/."""
import importlib.util
import pathlib
import pytest

pytestmark = pytest.mark.fast

ROOT = pathlib.Path(__file__).resolve().parent.parent
LINT = ROOT / "tools" / "spec_lint.py"


def _linter():
    spec = importlib.util.spec_from_file_location("spec_lint", LINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.integration
def test_all_specs_pass_lint():
    mod = _linter()
    problems = mod.validate(mod.load_specs())
    assert problems == [], "spec lint problems:\n  " + "\n  ".join(problems)
