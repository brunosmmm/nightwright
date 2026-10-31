"""SPEC-0281: `nw idea spec-link IDEA SPEC` — set/replace an idea's :SPEC:, identity-checked.

Fixtures come from tests/test_reconcile.py (tmp org + specs dir). The identity rule is SPEC-0267's
`_pick_idea`; these tests pin that the manual link shares it, that `--yes` needs a matching
`--heading`, and that a spec file is never edited.
"""
import os
import pathlib

import pytest
from click.testing import CliRunner

from nightwright import specs as S
from nightwright.cli import cli
from nightwright.org import load_tasks

from test_reconcile import (_bare_idea, _bytes, _cfg, _second_file_idea, _spec, _spec_prop,
                            _stamp_idea, _state_of)

# The helper idea is created "now"; specs below default to today too, so it is date-valid.


@pytest.fixture
def run(monkeypatch):
    def _run(cfg, *args):
        monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
        return CliRunner().invoke(cli, ["idea", "spec-link", *args])
    return _run


def _dup(tmp_path, old_stamp, new_stamp, spec_created):
    """Same id in two org files (as after a rotation) plus one spec with no source_idea."""
    cfg = _cfg(tmp_path)
    shared = _bare_idea(cfg, "old idea")
    _stamp_idea(cfg, shared, old_stamp)
    _second_file_idea(cfg, "new idea", shared, new_stamp)
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None, created=spec_created)
    return cfg, shared


def _sub(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


def _linked(cfg):
    return [t.heading for t in load_tasks(cfg) if t.is_idea and t.properties.get("SPEC")]


def test_dry_run_writes_nothing(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "shipped by hand", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    before = _bytes(cfg)
    out = run(cfg, idea, "SPEC-0001")
    assert out.exit_code == 0, out.output
    assert "shipped by hand" in out.output and "link" in out.output
    assert "ideas.org:" in out.output and ":CREATED:" in out.output
    assert _bytes(cfg) == before


def test_yes_links_and_reconciles(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "to be specced", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    out = run(cfg, idea, "SPEC-0001", "--yes", "--heading", "be spec")
    assert out.exit_code == 0, out.output
    assert _spec_prop(cfg, idea) == "SPEC-0001"
    assert _state_of(cfg, idea) == "SPECCED"
    assert "advance" in out.output


def test_yes_with_done_spec_makes_the_done_transition(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "already shipped", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "done", source_idea=None)
    out = run(cfg, idea, "SPEC-0001", "--yes", "--heading", "shipped")
    assert out.exit_code == 0, out.output
    # a done internal spec archives its idea (PROMOTED); the link row proves it ran
    assert "advance" in out.output and "PROMOTED" in out.output


def test_replace_existing_spec(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "relinked", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=None)
    assert run(cfg, idea, "SPEC-0001", "--yes", "--heading", "relinked").exit_code == 0
    plan = S.plan_spec_link(cfg, idea, "SPEC-0002")
    assert plan["action"] == "replace" and plan["current_spec"] == "SPEC-0001"
    assert run(cfg, idea, "SPEC-0002", "--yes", "--heading", "relinked").exit_code == 0
    assert _spec_prop(cfg, idea) == "SPEC-0002"


def test_unchanged_writes_nothing(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "same link", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    assert run(cfg, idea, "SPEC-0001", "--yes", "--heading", "same").exit_code == 0
    before = _bytes(cfg)
    assert S.plan_spec_link(cfg, idea, "SPEC-0001")["action"] == "unchanged"
    out = run(cfg, idea, "SPEC-0001", "--yes", "--heading", "same")
    assert out.exit_code == 0 and _bytes(cfg) == before


def test_yes_requires_matching_heading(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "guarded idea", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    before = _bytes(cfg)
    missing = run(cfg, idea, "SPEC-0001", "--yes")
    assert missing.exit_code == 2 and "--heading" in missing.output
    wrong = run(cfg, idea, "SPEC-0001", "--yes", "--heading", "something else")
    assert wrong.exit_code == 1 and "guarded idea" in wrong.output
    assert _bytes(cfg) == before


def test_rotated_duplicate_ids_pick_newest_valid(tmp_path, run):
    # old idea predates the spec, new one postdates it: only the old one is linked
    cfg, shared = _dup(tmp_path, "[2026-09-01 Tue 09:00]", "[2026-10-25 Sun 09:00]", "2026-09-15")
    plan = S.plan_spec_link(cfg, shared, "SPEC-0001")
    assert plan["heading"] == "old idea" and "pin" in plan
    out = run(cfg, shared, "SPEC-0001", "--yes", "--heading", "old")
    assert out.exit_code == 0, out.output
    assert _linked(cfg) == ["old idea"]
    # variant: both valid, different dates: the newer one is linked
    cfg, shared = _dup(_sub(tmp_path, "v"), "[2026-09-01 Tue 09:00]", "[2026-10-25 Sun 09:00]",
                       "2026-10-26")
    out = run(cfg, shared, "SPEC-0001", "--yes", "--heading", "new")
    assert out.exit_code == 0, out.output
    assert _linked(cfg) == ["new idea"]


@pytest.mark.parametrize("stamp", ["[2026-10-25 Sun 09:00]", None])
def test_refuses_reused_id_mismatch(tmp_path, run, stamp):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "reused id")
    _stamp_idea(cfg, idea, stamp)
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None, created="2026-10-01")
    before = _bytes(cfg)
    out = run(cfg, idea, "SPEC-0001", "--yes", "--heading", "reused")
    assert out.exit_code == 1 and "flag-mismatch" in out.output
    assert _bytes(cfg) == before


def test_refuses_unparseable_spec_date(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "dateless spec")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None, created="soon")
    out = run(cfg, idea, "SPEC-0001", "--yes", "--heading", "dateless")
    assert out.exit_code == 1 and _spec_prop(cfg, idea) == ""


def test_refuses_ambiguous_and_foreign_source_idea(tmp_path, run):
    cfg, shared = _dup(tmp_path, "[2026-10-25 Sun 09:00]", "[2026-10-25 Sun 09:00]", "2026-10-26")
    before = _bytes(cfg)
    out = run(cfg, shared, "SPEC-0001", "--yes", "--heading", "idea")
    assert out.exit_code == 1 and "flag-ambiguous" in out.output
    assert _bytes(cfg) == before
    cfg = _cfg(_sub(tmp_path, "f"))
    idea = _bare_idea(cfg, "foreign claim")
    other = _bare_idea(cfg, "other idea")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=other)
    before = _bytes(cfg)
    out = run(cfg, idea, "SPEC-0001", "--yes", "--heading", "foreign")
    assert out.exit_code == 1 and other in out.output
    assert _bytes(cfg) == before


def test_refuses_missing_spec_and_idea(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "no spec yet")
    assert run(cfg, idea, "SPEC-0099").exit_code == 1
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    assert run(cfg, "IDEA-999", "SPEC-0001").exit_code == 1


def test_drift_between_plan_and_apply(tmp_path):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "drifting", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=None)
    plan = S.plan_spec_link(cfg, idea, "SPEC-0001")
    from nightwright import org_write as W
    W.set_property(cfg, W.resolve_selector(cfg, idea), "SPEC", "SPEC-0002")
    before = _bytes(cfg)
    with pytest.raises(ValueError, match="changed since the plan"):
        S.apply_spec_link(cfg, plan)
    assert _bytes(cfg) == before


def test_spec_files_untouched(tmp_path, run):
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "spec stays put", state="INCUBATE")
    path = pathlib.Path(_spec(cfg, "SPEC-0001", "done", source_idea=None))
    before = path.read_bytes()
    out = run(cfg, idea, "SPEC-0001", "--yes", "--heading", "stays")
    assert out.exit_code == 0, out.output
    assert path.read_bytes() == before
    assert "flag-no-backlink" in out.output          # printed as a hint instead
    assert sorted(os.listdir(cfg["specs_dir"])) == [path.name]


def test_json_output(tmp_path, run):
    import json
    cfg = _cfg(tmp_path)
    idea = _bare_idea(cfg, "json idea", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    data = json.loads(run(cfg, idea, "SPEC-0001", "--json").output)
    assert data["applied"] is False and data["plan"]["action"] == "link"
    data = json.loads(run(cfg, idea, "SPEC-0001", "--yes", "--heading", "json", "--json").output)
    assert data["applied"] is True and data["reconcile"]
