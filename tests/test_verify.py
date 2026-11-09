"""SPEC-0117: nw spec verify mechanical DoD audit."""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightwright.cli import cli
from nightwright.verify import verify_spec

pytestmark = pytest.mark.fast


def _write_spec(specs_dir: Path, num: str, body: str, **fm):
    fm_lines = ["---", f"id: SPEC-{num}", f'title: "t{num}"',
                f"status: {fm.get('status', 'accepted')}", "owner: t",
                f"created: 2026-08-04"]
    if "kind" in fm:
        fm_lines.append(f"kind: {fm['kind']}")
    if "parent" in fm:
        fm_lines.append(f"parent: {fm['parent']}")
    fm_lines.append("---")
    path = specs_dir / f"{num}-t.md"
    path.write_text("\n".join(fm_lines) + "\n\n" + body + "\n", encoding="utf-8")
    return path


@pytest.fixture
def cfg_specs(tmp_path, monkeypatch):
    specs = tmp_path / "specs"
    specs.mkdir()
    cfg = {
        "specs_dir": str(specs),
        "_root": str(tmp_path),
        "data_dir": str(tmp_path / "data"),
    }
    return cfg, specs


def test_verify_pass_done_feature(cfg_specs):
    cfg, specs = cfg_specs
    _write_spec(specs, "9001", """## Context
x
## Acceptance criteria
- [x] one
## Test plan
- run tests
## Open questions
(none)
""", status="done")
    report = verify_spec(cfg, "SPEC-9001")
    assert report["schema"] == "nw.spec.verify.v1"
    assert report["ok"] is True
    assert report["judgment"]
    assert all(c["status"] != "fail" for c in report["checks"])


def test_verify_fail_unchecked_ac_when_done(cfg_specs):
    cfg, specs = cfg_specs
    _write_spec(specs, "9002", """## Acceptance criteria
- [ ] missing
## Test plan
ok
""", status="done")
    report = verify_spec(cfg, "SPEC-9002")
    assert report["ok"] is False
    assert any(c["name"] == "acceptance" and c["status"] == "fail" for c in report["checks"])


def test_verify_fail_missing_test_plan(cfg_specs):
    cfg, specs = cfg_specs
    _write_spec(specs, "9003", """## Acceptance criteria
- [x] a
## Open questions
(none)
""", status="accepted")
    report = verify_spec(cfg, "SPEC-9003")
    assert report["ok"] is False
    assert any(c["name"] == "test_plan" and c["status"] == "fail" for c in report["checks"])


def test_verify_epic_unfinished_child(cfg_specs):
    cfg, specs = cfg_specs
    _write_spec(specs, "9004", """## Acceptance criteria
- [x] epic outcome
## Breakdown / sub-specs
- [x] child
## Test plan
integration
## Open questions
(none)
""", status="done", kind="epic")
    _write_spec(specs, "9005", """## Acceptance criteria
- [x] c
## Test plan
t
""", status="accepted", parent="SPEC-9004")
    report = verify_spec(cfg, "SPEC-9004")
    assert report["ok"] is False
    assert any(c["name"] == "epic_children" and c["status"] == "fail" for c in report["checks"])


def test_verify_outbound_refused(cfg_specs):
    cfg, _ = cfg_specs
    with pytest.raises(ValueError, match="outbound|/nw-verify"):
        verify_spec(cfg, "EXAMPLE-0001")


def test_cli_verify_json_and_strict(cfg_specs, monkeypatch):
    cfg, specs = cfg_specs
    _write_spec(specs, "9006", """## Acceptance criteria
- [ ] open
## Test plan
t
""", status="done")

    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    runner = CliRunner()

    r = runner.invoke(cli, ["spec", "verify", "SPEC-9006", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data["schema"] == "nw.spec.verify.v1"
    assert data["ok"] is False

    r2 = runner.invoke(cli, ["spec", "verify", "SPEC-9006", "--strict"])
    assert r2.exit_code == 1

