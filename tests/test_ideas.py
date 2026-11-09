"""Idea capture & model (SPEC-0012): config defaults, Task.is_idea, filter_tasks(is_idea=...),
add_idea file-creation + capture, `nw tasks` exclusion, `nw ideas` listing/filters, CLI."""
import os
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightwright import config as C
from nightwright import org_write as W
from nightwright import report as R
from nightwright.cli import cli
from nightwright.console import console
from nightwright.org import filter_tasks, load_tasks

pytestmark = pytest.mark.fast


def _cfg(tmp_path, ideas="ai/ideas.org"):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED", "DROPPED"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / ideas),
            "org_ideas_archive_file": str(org / "ai" / "ideas-archive.org"),
            "specs_dir": str(tmp_path / "specs"),
            "config_dir": str(config),
            "project_axis": "bucket"}


def _find(cfg, sub):
    return next(t for t in load_tasks(cfg) if sub in t.heading)


def _render(fn, cfg, **kw):
    # Rich 15: size overrides require BOTH _width and _height (width alone is ignored).
    prev_w, prev_h = console._width, console._height
    console._width, console._height = 320, 80
    try:
        with console.capture() as cap:
            fn(cfg, **kw)
    finally:
        console._width, console._height = prev_w, prev_h
    return cap.get()


# ---- config -----------------------------------------------------------------

def test_default_config_has_idea_keys():
    assert C.DEFAULT_CONFIG["org_ideas_file"] == "~/work/org/ai/ideas.org"
    assert C.DEFAULT_CONFIG["org_idea_keywords"][0] == "IDEA"
    assert "|" in C.DEFAULT_CONFIG["org_idea_keywords"]


# ---- add_idea -----------------------------------------------------------------

def test_add_idea_creates_file_with_todo_header(tmp_path):
    cfg = _cfg(tmp_path)
    assert not os.path.exists(cfg["org_ideas_file"])
    path, headline, idea_id = W.add_idea(cfg, "instant review agent")
    assert os.path.exists(path)
    header = open(path).readline().strip()
    assert header == "#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED DROPPED"
    assert headline == "* IDEA instant review agent"
    assert idea_id == "IDEA-001"


def test_add_idea_sets_is_idea_and_default_state(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "self-assessing AI usage")
    t = _find(cfg, "self-assessing AI usage")
    assert t.is_idea is True
    assert t.state == "IDEA"
    assert t.is_done is False


def test_add_idea_honors_explicit_state_and_tags(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "incubating idea", state="INCUBATE", tags=("ai",))
    t = _find(cfg, "incubating idea")
    assert t.state == "INCUBATE"
    assert "ai" in t.tags
    assert t.is_idea is True


def test_add_idea_hyphenated_tag_parses_out_of_heading(tmp_path):
    """orgparse leaves :session-triage: in the heading; we strip it (SPEC-0038)."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "cascading filters", tags=("session-triage",))
    t = _find(cfg, "cascading filters")
    assert "session-triage" in t.tags
    assert ":session-triage:" not in t.heading
    assert t.heading == "cascading filters"


# ---- ideas.org rotation (SPEC-0071) ------------------------------------------------

def test_maybe_rotate_ideas_file_noop_when_missing(tmp_path):
    cfg = _cfg(tmp_path)
    assert W.maybe_rotate_ideas_file(cfg) is None


def test_maybe_rotate_ideas_file_noop_under_threshold(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["idea_rotation_max_lines"] = 50
    W.add_idea(cfg, "first idea")
    original = open(cfg["org_ideas_file"]).read()
    assert W.maybe_rotate_ideas_file(cfg) is None
    assert open(cfg["org_ideas_file"]).read() == original


def test_maybe_rotate_ideas_file_rotates_over_threshold(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["idea_rotation_max_lines"] = 3
    path = cfg["org_ideas_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    original = "#+TODO: IDEA | DONE\n\n* IDEA one\n* IDEA two\n* IDEA three\n"
    with open(path, "w") as f:
        f.write(original)

    dest = W.maybe_rotate_ideas_file(cfg)
    assert dest is not None
    assert os.path.exists(dest)
    assert open(dest).read() == original          # old content preserved byte-for-byte
    assert os.path.exists(path)                   # fresh file at the original path
    fresh = open(path).read()
    assert fresh.startswith("#+TODO:")
    assert "IDEA one" not in fresh


def test_maybe_rotate_ideas_file_config_path_never_mutated(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["idea_rotation_max_lines"] = 1
    configured_path = cfg["org_ideas_file"]
    os.makedirs(os.path.dirname(configured_path), exist_ok=True)
    with open(configured_path, "w") as f:
        f.write("#+TODO: IDEA | DONE\n\n* IDEA one\n* IDEA two\n")
    W.maybe_rotate_ideas_file(cfg)
    assert cfg["org_ideas_file"] == configured_path


def test_maybe_rotate_ideas_file_collision_gets_distinct_name(tmp_path):
    """Two rotations that would land on the same dated filename get distinct names."""
    cfg = _cfg(tmp_path)
    cfg["idea_rotation_max_lines"] = 1
    path = cfg["org_ideas_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#+TODO: IDEA | DONE\n\n* IDEA one\n* IDEA two\n")

    dest1 = W.maybe_rotate_ideas_file(cfg)
    # push the fresh file back over threshold and rotate again
    with open(path, "a") as f:
        f.write("* IDEA three\n* IDEA four\n")
    dest2 = W.maybe_rotate_ideas_file(cfg)

    assert dest1 != dest2
    assert os.path.exists(dest1)
    assert os.path.exists(dest2)


def test_add_idea_triggers_rotation_transparently(tmp_path):
    """A capture that pushes the file over threshold rotates first, then the new idea lands
    in the fresh file."""
    cfg = _cfg(tmp_path)
    cfg["idea_rotation_max_lines"] = 3
    path = cfg["org_ideas_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED DROPPED\n\n"
                 "* IDEA one\n* IDEA two\n* IDEA three\n")

    new_path, _headline, idea_id = W.add_idea(cfg, "fresh capture after rotation")
    assert new_path == path                        # still lands at the configured path
    fresh = open(path).read()
    assert "fresh capture after rotation" in fresh
    assert "IDEA one" not in fresh                  # old ideas rotated out
    assert idea_id == "IDEA-001"                    # numbering unaffected by rotation

    # the rotated historical file is still discoverable via the normal glob
    t = _find(cfg, "one")
    assert t.heading == "one"


# ---- idea archival primitive (SPEC-0072) -------------------------------------------

def test_archive_idea_moves_full_subtree(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "keep me")
    W.add_idea(cfg, "archive me")
    W.set_property(cfg, _find(cfg, "archive me"), "TAG_CHECK", "present")

    task = _find(cfg, "archive me")
    archive_path = W.archive_idea(cfg, task)
    assert os.path.exists(archive_path)

    remaining = open(cfg["org_ideas_file"]).read()
    assert "archive me" not in remaining
    assert "keep me" in remaining              # sibling untouched

    archived = open(archive_path).read()
    assert "archive me" in archived
    assert ":TAG_CHECK: present" in archived   # properties moved verbatim
    assert archived.startswith("#+TODO:")      # header bootstrapped


def test_archive_idea_appends_on_second_call(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "first to archive")
    W.add_idea(cfg, "second to archive")

    W.archive_idea(cfg, _find(cfg, "first to archive"))
    W.archive_idea(cfg, _find(cfg, "second to archive"))

    archived = open(cfg["org_ideas_archive_file"]).read()
    assert "first to archive" in archived
    assert "second to archive" in archived
    assert archived.count("#+TODO:") == 1      # header written once, not duplicated


def test_archive_idea_preserves_summary_and_log(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "rich idea", state="INCUBATE")
    task = _find(cfg, "rich idea")
    from nightwright import explore as EX
    EX.set_summary(cfg, task.properties.get("ID") or task.id, "some real understanding")
    EX.append_log(cfg, task.properties.get("ID") or task.id, "a finding worth keeping")

    task = _find(cfg, "rich idea")
    archive_path = W.archive_idea(cfg, task)
    archived = open(archive_path).read()
    assert "some real understanding" in archived
    assert "a finding worth keeping" in archived


def test_archive_idea_rejects_non_idea(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "a plain task", file=cfg["org_capture_file"])
    task = _find(cfg, "a plain task")
    with pytest.raises(ValueError, match="not an idea"):
        W.archive_idea(cfg, task)


def test_archive_idea_drift_guard(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "drifting idea")
    task = _find(cfg, "drifting idea")

    # mutate the on-disk headline's state after task was parsed, simulating a stale reference
    path = cfg["org_ideas_file"]
    text = open(path).read()
    text = text.replace("* IDEA drifting idea", "* INCUBATE drifting idea")
    open(path, "w").write(text)

    with pytest.raises(ValueError, match="drift"):
        W.archive_idea(cfg, task)
    # neither file was touched
    assert "drifting idea" in open(path).read()
    assert not os.path.exists(cfg["org_ideas_archive_file"])


def test_archived_idea_still_discoverable_via_load_tasks(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "discoverable after archive")
    task = _find(cfg, "discoverable after archive")
    idea_id = task.properties.get("ID") or task.id
    W.archive_idea(cfg, task)

    reloaded = _find(cfg, "discoverable after archive")
    assert (reloaded.properties.get("ID") or reloaded.id) == idea_id
    assert reloaded.is_idea


def test_add_idea_appends_to_existing_file(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "first idea")
    W.add_idea(cfg, "second idea")
    tasks = load_tasks(cfg)
    headings = [t.heading for t in tasks]
    assert any("first idea" in h for h in headings)
    assert any("second idea" in h for h in headings)
    # header line appears exactly once
    content = open(cfg["org_ideas_file"]).read()
    assert content.count("#+TODO:") == 1


# ---- Task.is_idea / filter_tasks --------------------------------------------

def test_is_idea_true_for_idea_states_false_for_regular_tasks(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea")
    W.add_task(cfg, "a regular task")
    tasks = load_tasks(cfg)
    idea = _find(cfg, "an idea")
    task = _find(cfg, "a regular task")
    assert idea.is_idea is True
    assert task.is_idea is False
    assert filter_tasks(tasks, is_idea=True) == [idea]
    assert filter_tasks(tasks, is_idea=False) == [task]


def test_promoted_and_dropped_are_idea_done_states(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "promoted one", state="PROMOTED")
    W.add_idea(cfg, "dropped one", state="DROPPED")
    tasks = load_tasks(cfg)
    promoted = _find(cfg, "promoted one")
    dropped = _find(cfg, "dropped one")
    assert promoted.is_idea and promoted.is_done
    assert dropped.is_idea and dropped.is_done


# ---- nw tasks excludes ideas --------------------------------------------------

def test_nw_tasks_excludes_ideas(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea should not show")
    W.add_task(cfg, "a regular task should show")
    out = _render(R.tasks, cfg)
    assert "a regular task should show" in out
    assert "an idea should not show" not in out
    out_all = _render(R.tasks, cfg, all_done=True)
    assert "an idea should not show" not in out_all
    assert "a regular task should show" in out_all


# ---- nw ideas ------------------------------------------------------------------

def test_nw_ideas_lists_open_by_default(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "open idea")
    W.add_idea(cfg, "promoted idea", state="PROMOTED")
    out = _render(R.ideas, cfg)
    assert "open idea" in out
    assert "promoted idea" not in out


def test_nw_ideas_all_includes_done_states(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "open idea")
    W.add_idea(cfg, "promoted idea", state="PROMOTED")
    out = _render(R.ideas, cfg, all_done=True)
    assert "open idea" in out
    assert "promoted idea" in out


def test_nw_ideas_state_filter(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an incubating idea", state="INCUBATE")
    W.add_idea(cfg, "a fresh idea")
    out = _render(R.ideas, cfg, state="INCUBATE")
    assert "incubating" in out
    assert "a fresh idea" not in out


# ---- SPEC-0089: --tag / --project parity ----------------------------------------------

def test_nw_ideas_tag_and_project_filters(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "alpha tagged", tags=("workflow",), project="Meta-Tools")
    W.add_idea(cfg, "beta other", tags=("other",), project="Example")
    W.add_idea(cfg, "gamma meta no tag", project="Meta-Tools")
    rows = R.collect_idea_rows(cfg, tag="workflow")
    assert [r["heading"] for r in rows] == ["alpha tagged"]
    rows = R.collect_idea_rows(cfg, project="Meta-Tools")
    assert {r["heading"] for r in rows} == {"alpha tagged", "gamma meta no tag"}
    rows = R.collect_idea_rows(cfg, tag="workflow", project="Example")
    assert rows == []


def test_nw_ideas_query_composes_with_project(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "needle in meta", project="Meta-Tools")
    W.add_idea(cfg, "needle in example", project="Example")
    W.add_idea(cfg, "other in meta", project="Meta-Tools")
    hits = R.search_idea_tasks(cfg, "needle", project="Meta-Tools")
    assert [t.heading for t, *_ in hits] == ["needle in meta"]


def test_cli_ideas_unknown_project_warns(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg["project_axis"] = "bucket"
    cfg["config_dir"] = str(tmp_path / "config")
    (tmp_path / "config").mkdir(exist_ok=True)
    W.add_idea(cfg, "has meta", project="Meta-Tools")
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["ideas", "--project", "NoSuchProject", "--json"])
    assert r.exit_code == 0, r.output
    combined = (r.output or "") + (r.stderr or "")
    assert "unknown project" in combined.lower()
    import json
    text = r.stdout or r.output
    start = text.find("{")
    data = json.loads(text[start:])
    assert data["schema"] == "nw.ideas.v1"
    assert data["ideas"] == []


def test_nw_ideas_truncates_long_headlines(tmp_path):
    """List is a triage UI: long essay headlines must not fold into multi-line rows.

    SPEC-0075: the clip is the terminal's leftover width, not a hard 40 — so a wide terminal
    shows far more of the headline, while a narrow one still clips and marks it."""
    cfg = _cfg(tmp_path)
    long = ("I want to create an interactive web interface that is capable of using "
            "the Example investigative tools in the backend to perform real-time queries "
            "and show archaeological digests and a large wealth of data continuously.")
    W.add_idea(cfg, long)
    out = _render(R.ideas, cfg)                      # 320 columns
    assert "IDEA-001" in out
    assert out.count("\n") < 20
    assert long[:120] in out                         # no longer capped at 40

    prev_w, prev_h = console._width, console._height  # narrow: must clip, and say so
    console._width, console._height = 100, 80
    try:
        with console.capture() as cap:
            R.ideas(cfg)
        narrow = cap.get()
    finally:
        console._width, console._height = prev_w, prev_h
    assert long not in narrow and "…" in narrow
    assert max(len(ln) for ln in narrow.splitlines()) <= 100


def test_nw_ideas_empty_is_friendly(tmp_path):
    cfg = _cfg(tmp_path)
    out = _render(R.ideas, cfg)
    assert "no matching ideas" in out


# ---- CLI ------------------------------------------------------------------------

def test_cli_idea_and_ideas(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "investigate", "self-assessing", "AI", "usage"])
    assert r.exit_code == 0, r.output
    assert "investigate self-assessing AI usage" in r.output
    t = _find(cfg, "investigate self-assessing AI usage")
    assert t.is_idea and t.state == "IDEA"

    prev_w, prev_h = console._width, console._height
    console._width, console._height = 320, 80
    try:
        r2 = CliRunner().invoke(cli, ["ideas"])
    finally:
        console._width, console._height = prev_w, prev_h
    assert r2.exit_code == 0, r2.output
    assert "investigate" in r2.output and "self-assessing" in r2.output
    assert "next" in r2.output.lower()


def test_cli_idea_invalid_state(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    W.add_idea(cfg, "seed")   # create the ideas file
    r = CliRunner().invoke(cli, ["idea", "bad", "--state", "NOPE"])
    assert r.exit_code != 0 and "invalid state" in r.output


def test_ideas_listing_shows_project_and_refs(tmp_path):
    """Rich triage shows project; key/epic stay on the row model (and --json), not the
    clipped table (tags/refs columns dropped so long headlines don't crush id/state)."""
    cfg = _cfg(tmp_path)
    cfg["config_dir"] = cfg["data_dir"]
    cfg["specs_dir"] = str(tmp_path / "specs")
    cfg["project_axis"] = "bucket"
    W.add_idea(cfg, "web dashboard for Example tools", project="AI-workstreams",
               epic="SPEC-0011", key="DEMO-100")
    out = _render(R.ideas, cfg)
    assert "AI-workstreams" in out
    assert "IDEA-001" in out
    rows = R.collect_idea_rows(cfg)
    assert rows[0].get("project") == "AI-workstreams"
    # key/epic live on the Task; JSON consumers use idea_row + org fields
    from nightwright.org import load_tasks
    idea = next(t for t in load_tasks(cfg) if t.is_idea)
    assert idea.topic_key == "DEMO-100"
    assert idea.epic == "SPEC-0011"


# ---- SPEC-0019: auto-assigned stable idea IDs -------------------------------------

def test_add_idea_assigns_sequential_ids(tmp_path):
    cfg = _cfg(tmp_path)
    _, _, id1 = W.add_idea(cfg, "first idea")
    _, _, id2 = W.add_idea(cfg, "second idea")
    _, _, id3 = W.add_idea(cfg, "third idea")
    assert (id1, id2, id3) == ("IDEA-001", "IDEA-002", "IDEA-003")
    t1 = _find(cfg, "first idea")
    assert t1.id == "IDEA-001"
    assert t1.properties["ID"] == "IDEA-001"


def test_add_idea_id_is_first_property(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["config_dir"] = cfg["data_dir"]
    cfg["specs_dir"] = str(tmp_path / "specs")
    cfg["project_axis"] = "bucket"
    W.add_idea(cfg, "with associations", project="Logging", key="DEMO-100")
    content = open(cfg["org_ideas_file"]).read()
    props_block = content.split(":PROPERTIES:")[1].split(":END:")[0]
    lines = [l.strip() for l in props_block.strip().splitlines()]
    assert lines[0].startswith(":ID: IDEA-001")


def test_next_idea_number_respects_existing_ids_after_a_gap(tmp_path):
    cfg = _cfg(tmp_path)
    # write ideas with IDEA-001 and IDEA-003 directly, no IDEA-002 (gap) — next must be 004
    d = os.path.dirname(cfg["org_ideas_file"])
    os.makedirs(d, exist_ok=True)
    with open(cfg["org_ideas_file"], "w") as f:
        f.write("#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED DROPPED\n\n")
        f.write("* IDEA one\n  :PROPERTIES:\n  :ID: IDEA-001\n  :END:\n")
        f.write("* IDEA three\n  :PROPERTIES:\n  :ID: IDEA-003\n  :END:\n")
    assert W.next_idea_number(cfg) == 4   # not 3 — must not collide with existing IDEA-003


def test_resolve_selector_matches_idea_id_case_and_padding(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea to resolve")
    t = W.resolve_selector(cfg, "IDEA-001")
    assert t.heading.endswith("an idea to resolve")
    t2 = W.resolve_selector(cfg, "idea-1")
    assert t2.id == "IDEA-001"


def test_reindex_ideas_backfills_only_idless_ideas_and_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "not an idea, ignored by reindex")
    # simulate a pre-existing id-less idea by writing directly (no :ID:)
    W.add_idea(cfg, "already has id")             # IDEA-001
    with open(cfg["org_ideas_file"], "a") as f:
        f.write("* IDEA an idea with no id yet\n")
        f.write("* IDEA another id-less idea\n")

    assigned = W.reindex_ideas(cfg)
    assert [a[0] for a in assigned] == ["IDEA-002", "IDEA-003"]
    assert [a[1] for a in assigned] == ["an idea with no id yet", "another id-less idea"]

    tasks = load_tasks(cfg)
    ids = {t.heading: t.properties.get("ID") for t in tasks if t.is_idea}
    assert ids["already has id"] == "IDEA-001"
    assert ids["an idea with no id yet"] == "IDEA-002"
    assert ids["another id-less idea"] == "IDEA-003"

    # idempotent: a second run assigns nothing, existing ids untouched
    assert W.reindex_ideas(cfg) == []
    tasks2 = load_tasks(cfg)
    ids2 = {t.heading: t.properties.get("ID") for t in tasks2 if t.is_idea}
    assert ids2 == ids


def test_nw_ideas_shows_id_column(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "an idea with an id")
    out = _render(R.ideas, cfg)
    assert "IDEA-001" in out


def test_cli_idea_echoes_assigned_id(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    r = CliRunner().invoke(cli, ["idea", "some", "new", "idea"])
    assert r.exit_code == 0, r.output
    assert "IDEA-001" in r.output


def test_cli_ideas_reindex_backfills_and_lists(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    import nightwright.cli as cli_mod
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    W.add_idea(cfg, "seed idea")
    with open(cfg["org_ideas_file"], "a") as f:
        f.write("* IDEA an old id-less idea\n")

    prev_w, prev_h = console._width, console._height
    console._width, console._height = 320, 80
    try:
        r = CliRunner().invoke(cli, ["ideas", "--reindex"])
        assert r.exit_code == 0, r.output
        assert "IDEA-002" in r.output
        assert "id-less idea" in r.output

        # idempotent second run: no new assignment messages, still lists
        r2 = CliRunner().invoke(cli, ["ideas", "--reindex"])
    finally:
        console._width, console._height = prev_w, prev_h
    assert r2.exit_code == 0, r2.output
    assert "no id-less ideas to reindex" in r2.output


def test_idea_row_and_next_show_handed_off(tmp_path):
    """SPEC-0280: a SPECCED idea with `:HANDED_OFF:` shows in the row, the show payload, the
    `nw ideas` state cell, and `next_step_for_idea` says the PR is open."""
    from nightwright import explore as EX
    from nightwright import workflow as WF
    cfg = _cfg(tmp_path)
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "0001-x.md").write_text(
        "---\nid: SPEC-0001\ntitle: x\nstatus: accepted\nowner: t\ncreated: 2026-10-27\n---\n\nx\n")
    W.add_idea(cfg, "pending a pr")
    W.set_property(cfg, _find(cfg, "pending a pr"), "SPEC", "SPEC-0001")
    W.set_state_by_selector(cfg, "IDEA-001", "SPECCED")
    plain = R.idea_row(cfg, _find(cfg, "pending a pr"))
    assert "handed_off" not in plain and not plain["next"].startswith("PR open")
    W.set_property(cfg, _find(cfg, "pending a pr"), "HANDED_OFF", "https://x/pull/3")
    task = _find(cfg, "pending a pr")
    row = R.idea_row(cfg, task)
    assert row["handed_off"] == "https://x/pull/3"
    assert row["next"] == ("PR open (https://x/pull/3); after it merges: nw spec reconcile "
                           "--apply; if it was closed: nw queue add SPEC-0001")
    assert WF.next_step_for_idea(cfg, task) == row["next"]
    assert EX.idea_show_payload(cfg, task)["handed_off"] == "https://x/pull/3"
    out = _render(R.ideas, cfg)
    assert "SPECCED ⇢ PR" in out
