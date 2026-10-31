"""Spec -> org task/epic generation (SPEC-0014): specs.spec_tasks extraction,
org_write.generate_tasks (idempotence, :SPEC:/:TOPIC: properties, join invariant via
org.join_time), specs.generate_from_spec (source_idea -> PROMOTED), and the CLI.

Tasks always land in a TMP org file; the fixture spec lives in a TMP specs_dir (never the real
docs/specs/ or ~/work/org) — see cfg["specs_dir"]/cfg["org_capture_file"]."""
import pathlib

from click.testing import CliRunner
from zoneinfo import ZoneInfo

from nightwright import org_write as W
from nightwright import specs as S
from nightwright.cli import cli
from nightwright.org import join_time, load_tasks

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL_SPECS = ROOT / "docs" / "specs"

EPIC_SPEC = """---
id: SPEC-0100
title: Sample epic
status: accepted
owner: user
created: 2026-07-16
kind: epic
---

## Context

An epic.

## Breakdown / sub-specs

- [ ] SPEC-0101 -- first sub-spec
- [ ] SPEC-0102 -- second sub-spec

## Acceptance criteria

- [ ] all children done

## Test plan

N/A
"""

FEATURE_SPEC = """---
id: SPEC-0101
title: Sample feature
status: accepted
owner: user
created: 2026-07-16
source_idea: idea-1
---

## Context

A feature.

## Acceptance criteria

- [ ] does the thing
- [ ] handles the edge case

## Test plan

N/A
"""

FEATURE_NO_AC_SPEC = """---
id: SPEC-0102
title: No AC feature
status: accepted
owner: user
created: 2026-07-16
---

## Context

No acceptance criteria section listed here.

## Test plan

N/A
"""


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ideas.org"), "specs_dir": str(specs_dir)}


def _find(cfg, sub):
    return next(t for t in load_tasks(cfg) if sub in t.heading)


# ---- spec_tasks ---------------------------------------------------------------

def test_spec_tasks_epic_uses_breakdown(tmp_path):
    p = tmp_path / "0100-sample-epic.md"
    p.write_text(EPIC_SPEC)
    title, items = S.spec_tasks(p)
    assert title == "Sample epic"
    assert items == ["SPEC-0101 -- first sub-spec", "SPEC-0102 -- second sub-spec"]


def test_spec_tasks_feature_uses_acceptance_criteria(tmp_path):
    p = tmp_path / "0101-sample-feature.md"
    p.write_text(FEATURE_SPEC)
    title, items = S.spec_tasks(p)
    assert title == "Sample feature"
    assert items == ["does the thing", "handles the edge case"]


def test_spec_tasks_feature_no_ac_falls_back_to_single_task(tmp_path):
    p = tmp_path / "0102-no-ac.md"
    p.write_text(FEATURE_NO_AC_SPEC)
    title, items = S.spec_tasks(p)
    assert title == "No AC feature"
    assert items == ["implement No AC feature"]


# ---- generate_from_spec / org_write.generate_tasks -----------------------------

def test_generate_creates_parent_and_children(tmp_path):
    cfg = _cfg(tmp_path)
    (pathlib.Path(cfg["specs_dir"]) / "0100-sample-epic.md").write_text(EPIC_SPEC)

    path, added = S.generate_from_spec(cfg, "SPEC-0100")
    assert added == 2

    tasks = load_tasks(cfg)
    parent = next(t for t in tasks if t.properties.get("SPEC") == "SPEC-0100" and t.level == 1)
    assert "SPEC-0100" in parent.heading and "Sample epic" in parent.heading
    assert "spec" in parent.tags

    children = [t for t in tasks if t.level == 2 and t.properties.get("SPEC") == "SPEC-0100"]
    assert len(children) == 2
    assert {t.state for t in children} == {"TODO"}
    assert {t.heading for t in children} == {
        "SPEC-0101 -- first sub-spec", "SPEC-0102 -- second sub-spec"}


def test_generate_is_idempotent_and_adds_only_new_items(tmp_path):
    cfg = _cfg(tmp_path)
    spec_path = pathlib.Path(cfg["specs_dir"]) / "0100-sample-epic.md"
    spec_path.write_text(EPIC_SPEC)

    path1, added1 = S.generate_from_spec(cfg, "SPEC-0100")
    assert added1 == 2
    path2, added2 = S.generate_from_spec(cfg, "SPEC-0100")
    assert added2 == 0

    tasks = load_tasks(cfg)
    parents = [t for t in tasks if t.level == 1 and t.properties.get("SPEC") == "SPEC-0100"]
    assert len(parents) == 1
    children = [t for t in tasks if t.level == 2 and t.properties.get("SPEC") == "SPEC-0100"]
    assert len(children) == 2

    # a spec whose breakdown grew: re-run adds only the new item
    spec_path.write_text(EPIC_SPEC.replace(
        "- [ ] SPEC-0102 -- second sub-spec",
        "- [ ] SPEC-0102 -- second sub-spec\n- [ ] SPEC-0103 -- third sub-spec"))
    path3, added3 = S.generate_from_spec(cfg, "SPEC-0100")
    assert added3 == 1
    children = [t for t in load_tasks(cfg)
               if t.level == 2 and t.properties.get("SPEC") == "SPEC-0100"]
    assert len(children) == 3
    assert any("third sub-spec" in t.heading for t in children)


def test_generate_feature_single_task_with_key_joins_time(tmp_path):
    cfg = _cfg(tmp_path)
    (pathlib.Path(cfg["specs_dir"]) / "0102-no-ac.md").write_text(FEATURE_NO_AC_SPEC)

    S.generate_from_spec(cfg, "SPEC-0102", key="DEMO-900")

    tasks = load_tasks(cfg)
    child = next(t for t in tasks if t.level == 2 and t.properties.get("SPEC") == "SPEC-0102")
    assert child.topic_key == "DEMO-900"

    # join invariant: a synthetic topic_secs entry keyed to DEMO-900 attributes verbatim
    hours_by_key, key_to_tasks, untracked = join_time(tasks, {"DEMO-900": 3600.0})
    assert hours_by_key["DEMO-900"] == 3600.0
    assert child in key_to_tasks["DEMO-900"]
    assert untracked == {}


def test_generate_advances_source_idea_to_promoted(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "instant review agent")
    idea = _find(cfg, "instant review agent")
    idea_id = idea.id

    feature = FEATURE_SPEC.replace("source_idea: idea-1", f"source_idea: {idea_id}")
    (pathlib.Path(cfg["specs_dir"]) / "0101-sample-feature.md").write_text(feature)

    S.generate_from_spec(cfg, "SPEC-0101")

    idea = _find(cfg, "instant review agent")
    assert idea.state == "PROMOTED"


def test_generate_custom_file_target(tmp_path):
    cfg = _cfg(tmp_path)
    (pathlib.Path(cfg["specs_dir"]) / "0100-sample-epic.md").write_text(EPIC_SPEC)
    target = str(pathlib.Path(cfg["org_files"][0]) / "agenda.org")

    path, added = S.generate_from_spec(cfg, "SPEC-0100", file=target)
    assert path == target
    assert added == 2
    assert not pathlib.Path(cfg["org_capture_file"]).exists()


# ---- CLI ------------------------------------------------------------------------

def test_cli_spec_generate(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (pathlib.Path(cfg["specs_dir"]) / "0100-sample-epic.md").write_text(EPIC_SPEC)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)

    r = CliRunner().invoke(cli, ["spec", "generate", "SPEC-0100"])
    assert r.exit_code == 0, r.output
    assert "2 new task" in r.output

    r2 = CliRunner().invoke(cli, ["spec", "generate", "SPEC-0100"])
    assert r2.exit_code == 0, r2.output
    assert "0 new task" in r2.output


def test_cli_spec_generate_unknown_spec(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "generate", "SPEC-9999"])
    assert r.exit_code != 0
    assert "no spec file found" in r.output


def test_generate_refuses_outbound_id(tmp_path):
    """SPEC-0041: generate must not run on PROJ-NNNN (use export)."""
    cfg = _cfg(tmp_path)
    try:
        S.generate_from_spec(cfg, "ACME-0001")
        assert False, "expected ValueError"
    except ValueError as e:
        assert "outbound" in str(e).lower()
        assert "export" in str(e).lower()


def test_cli_spec_generate_refuses_outbound(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "generate", "EXAMPLE-0001"])
    assert r.exit_code != 0
    assert "outbound" in r.output.lower()
