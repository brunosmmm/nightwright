"""SPEC-0043: known_epics includes outbound; seed-children; outbound --epic scaffold."""
import pytest
from pathlib import Path
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightwright import org_write as W
from nightwright import specs as S
from nightwright.cli import cli
from nightwright.org import load_tasks
from nightwright.specmeta import split_frontmatter

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parent.parent


def _cfg(tmp_path, *, outbox_targets=None):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    specs = tmp_path / "specs"
    specs.mkdir()
    for name in ("TEMPLATE.md", "TEMPLATE-epic.md"):
        (specs / name).write_text((ROOT / "docs" / "specs" / name).read_text())
    outbox = tmp_path / "outbox"
    return {
        "org_files": [str(org)],
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": [
            "IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED",
        ],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "data_dir": str(data),
        "org_capture_file": str(org / "inbox.org"),
        "org_ideas_file": str(org / "ideas.org"),
        "specs_dir": str(specs),
        "config_dir": str(tmp_path / "config"),
        "outbox_dir": str(outbox),
        "outbox_targets": outbox_targets or {"acme": {"repo_path": "/tmp/acme"}},
        "project_axis": "bucket",
    }


def _write_outbound_epic(cfg, *, breakdown_lines):
    proj = Path(cfg["outbox_dir"]) / "acme"
    proj.mkdir(parents=True, exist_ok=True)
    boxes = "\n".join(f"- [ ] {line}" for line in breakdown_lines)
    path = proj / "ACME-0001-epic.md"
    path.write_text(
        "---\nid: ACME-0001\ntitle: Epic\nstatus: accepted\nowner: t\n"
        "created: 2026-07-24\nkind: epic\n"
        "target_project: acme\ntarget_repo: /tmp/acme\n---\n\n"
        "## Context\n\nx\n\n## Decision\n\nx\n\n"
        f"## Breakdown / sub-specs\n\n{boxes}\n\n"
        "## Acceptance criteria\n\n- [ ] done\n\n## Test plan\n\nN/A\n",
        encoding="utf-8",
    )
    return path


def test_known_epics_includes_outbound(tmp_path):
    cfg = _cfg(tmp_path)
    _write_outbound_epic(cfg, breakdown_lines=["Slice A"])
    ids = S.known_epics(cfg)
    assert "ACME-0001" in ids


def test_scaffold_outbound_epic_uses_template(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "big program", project="acme")
    # need Summary for promote without force - add via explore or force
    path, oid = S.scaffold_outbound(
        cfg, "acme", from_idea="big program", epic=True, force=True,
        title="Big program epic",
    )
    assert oid.startswith("ACME-")
    fm, body = split_frontmatter(path.read_text())
    assert fm["kind"] == "epic"
    assert fm["target_project"] == "acme"
    assert "## Breakdown / sub-specs" in body


def test_seed_ideas_from_epic_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    _write_outbound_epic(cfg, breakdown_lines=[
        "Critical-only alerting — retire digest",
        "Periodic AI mining",
    ])
    added = S.seed_ideas_from_epic(cfg, "ACME-0001")
    assert len(added) == 2
    ideas = [t for t in load_tasks(cfg) if t.is_idea]
    assert all(t.properties.get("EPIC") == "ACME-0001" for t in ideas if t.properties.get("ID") in added)
    assert all(t.properties.get("PROJECT") == "acme" for t in ideas if t.properties.get("ID") in added)
    headings = {t.heading for t in ideas if t.properties.get("ID") in added}
    assert "Critical-only alerting" in headings
    assert "Periodic AI mining" in headings

    again = S.seed_ideas_from_epic(cfg, "ACME-0001")
    assert again == []


def test_cli_seed_children(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_outbound_epic(cfg, breakdown_lines=["Only slice"])
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["spec", "seed-children", "ACME-0001"])
    assert r.exit_code == 0, r.output
    assert "1 child idea" in r.output
    r2 = CliRunner().invoke(cli, ["spec", "seed-children", "ACME-0001"])
    assert r2.exit_code == 0, r2.output
    assert "no new child" in r2.output.lower() or "0" in r2.output


def test_parse_breakdown_project():
    cleaned, proj = S.parse_breakdown_project(
        "Producer (project: Example) — fixture and sanitizer")
    assert proj == "Example"
    assert "(project:" not in cleaned
    assert "Producer" in cleaned
    cleaned2, proj2 = S.parse_breakdown_project("No annotation — still fine")
    assert proj2 is None
    assert cleaned2.startswith("No annotation")


def test_seed_per_bullet_project_annotation(tmp_path):
    cfg = _cfg(tmp_path, outbox_targets={
        "acme": {"repo_path": "/tmp/acme"},
        "Example": {"repo_path": "/tmp/example"},
        "PFW-Intelligence": {"repo_path": "/tmp/pfw"},
    })
    _write_outbound_epic(cfg, breakdown_lines=[
        "Producer (project: Example) — fixture",
        "Consumer (project: PFW-Intelligence) — renderer",
        "Default slice — uses epic target_project",
    ])
    added = S.seed_ideas_from_epic(cfg, "ACME-0001")
    assert len(added) == 3
    by_head = {t.heading: t for t in load_tasks(cfg) if t.is_idea}
    assert by_head["Producer"].properties.get("PROJECT") == "Example"
    assert by_head["Consumer"].properties.get("PROJECT") == "PFW-Intelligence"
    assert by_head["Default slice"].properties.get("PROJECT") == "acme"
    assert "(project:" not in by_head["Producer"].heading
    # Idempotent after annotated seed
    assert S.seed_ideas_from_epic(cfg, "ACME-0001") == []


def test_seed_unknown_project_annotation_hard_fails(tmp_path):
    import pytest
    cfg = _cfg(tmp_path)
    _write_outbound_epic(cfg, breakdown_lines=[
        "Bad (project: NotARealProject) — nope",
    ])
    with pytest.raises(ValueError, match="unknown project"):
        S.seed_ideas_from_epic(cfg, "ACME-0001")


# ---- SPEC-0423: `(depends_on child N)` -> `:AFTER:` ------------------------------------------

def _after_of(cfg, iid):
    from nightwright.org import invalidate_tasks_cache
    invalidate_tasks_cache()
    t = next(t for t in load_tasks(cfg) if t.properties.get("ID") == iid)
    return (t.properties.get("AFTER") or "").split(), t.heading


def test_seed_stamps_after_from_depends_on_annotations(tmp_path):
    cfg = _cfg(tmp_path)
    _write_outbound_epic(cfg, breakdown_lines=[
        "First slice — do it",
        "Second slice — more (depends_on child 1)",
        "Third slice (depends_on child 1 and child 2)",
    ])
    a, b, c = S.seed_ideas_from_epic(cfg, "ACME-0001")
    assert _after_of(cfg, a) == ([], "First slice")
    assert _after_of(cfg, b) == ([a], "Second slice")
    assert _after_of(cfg, c) == ([a, b], "Third slice")          # the annotation stays out of titles
    before = open(cfg["org_ideas_file"]).read()
    assert S.seed_ideas_from_epic(cfg, "ACME-0001") == []
    assert open(cfg["org_ideas_file"]).read() == before           # idempotent, byte for byte


def test_seed_fills_missing_after_but_never_overwrites(tmp_path):
    cfg = _cfg(tmp_path)
    _write_outbound_epic(cfg, breakdown_lines=["One", "Two", "Three (depends_on child 1)"])
    a, b, c = S.seed_ideas_from_epic(cfg, "ACME-0001")
    from nightwright.org import invalidate_tasks_cache
    invalidate_tasks_cache()
    t = next(t for t in load_tasks(cfg) if t.properties.get("ID") == c)
    W.set_property(cfg, t, "AFTER", b)                            # a hand edit wins
    assert S.seed_ideas_from_epic(cfg, "ACME-0001") == []
    assert _after_of(cfg, c)[0] == [b]
    # an idea seeded before this change (no :AFTER:) gets it on the next run
    _write_outbound_epic(cfg, breakdown_lines=["One", "Two (depends_on child 1)", "Three"])
    S.seed_ideas_from_epic(cfg, "ACME-0001")
    assert _after_of(cfg, b)[0] == [a]


def test_seed_bad_ordinal_is_a_hard_error_naming_the_bullet(tmp_path):
    cfg = _cfg(tmp_path)
    for line, frag in (("Two (depends_on child 9)", "out of range"),
                       ("Two (depends_on child 2)", "itself")):
        _write_outbound_epic(cfg, breakdown_lines=["One", line])
        with pytest.raises(ValueError, match=f"bullet 2.*{frag}"):
            S.seed_ideas_from_epic(cfg, "ACME-0001")
    assert [t for t in load_tasks(cfg) if t.is_idea] == []        # nothing seeded

