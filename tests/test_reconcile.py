"""SPEC-0078: `wt spec reconcile` — sync idea state with the linked spec's status.

The core contract is the status→state table, so it is enumerated rather than sampled. Writes are
opt-in (`--apply`) because advancing to PROMOTED/DROPPED archives the subtree (SPEC-0073).
"""
import os
import pathlib
from zoneinfo import ZoneInfo

import pytest

from wt import org_write as W
from wt import report as R
from wt import specs as S
from wt.org import load_tasks

TZ = ZoneInfo("America/New_York")


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    repo = tmp_path / "repo"
    (repo / "docs" / "specs").mkdir(parents=True)
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                                  "SHIPPED", "DROPPED", "RESEARCHED"],
            "timezone": "America/New_York", "_tz": TZ,
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ai" / "ideas.org"),
            "org_ideas_archive_file": str(org / "ai" / "ideas-archive.org"),
            "config_dir": str(tmp_path / "cfg"), "project_axis": "bucket",
            # `_specs_dir` reads this; without it the fixtures resolve against the real repo.
            "specs_dir": str(repo / "docs" / "specs")}


def _spec(cfg, spec_id, status, *, source_idea="__auto__", extra=""):
    """Write a minimal internal spec with the given status."""
    num = spec_id.split("-")[1]
    path = os.path.join(cfg["specs_dir"], f"{num}-fixture.md")
    si = "" if source_idea is None else f"source_idea: {source_idea}\n"
    with open(path, "w") as f:
        f.write(f"---\nid: {spec_id}\ntitle: \"fixture\"\nstatus: {status}\n"
                f"owner: t\ncreated: 2026-01-01\n{si}{extra}---\n\n## Context\n\nfixture.\n")
    return path


def _idea(cfg, text, *, state, spec_id):
    W.add_idea(cfg, text)
    task = [t for t in load_tasks(cfg) if t.is_idea and t.heading == text][0]
    idea_id = task.properties.get("ID")
    W.set_property(cfg, task, "SPEC", spec_id)
    if state != "IDEA":
        W.set_state_by_selector(cfg, idea_id, state)
    return idea_id


def _state_of(cfg, idea_id):
    return (W.resolve_selector(cfg, idea_id).state or "").upper()


def _actions(plan):
    return {(i, a) for i, _s, a, _d in plan}


def _bytes(cfg):
    root = os.path.dirname(cfg["org_ideas_file"])
    out = {}
    for p in sorted(os.listdir(root)):
        out[p] = open(os.path.join(root, p), "rb").read()
    return out


# ---- dry run is the default -------------------------------------------------------

def test_dry_run_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    _idea(cfg, "shipped work", state="SPECCED", spec_id="SPEC-0001")
    before = _bytes(cfg)

    plan = S.reconcile_ideas(cfg, apply=False)
    assert ("IDEA-001", "advance") in _actions(plan)
    assert _bytes(cfg) == before
    assert _state_of(cfg, "IDEA-001") == "SPECCED"


# ---- the status → state table -----------------------------------------------------

MATRIX = [
    # (spec status, starting idea state, expected state after --apply)
    ("done", "SPECCED", "PROMOTED"),
    ("done", "INCUBATE", "PROMOTED"),
    ("done", "IDEA", "PROMOTED"),
    ("superseded", "SPECCED", "DROPPED"),
    ("rejected", "SPECCED", "DROPPED"),
    ("accepted", "IDEA", "SPECCED"),
    ("accepted", "INCUBATE", "SPECCED"),
    ("accepted", "SPECCED", "SPECCED"),      # already right
    ("in-progress", "INCUBATE", "SPECCED"),
    ("in-progress", "SPECCED", "SPECCED"),
    ("draft", "INCUBATE", "INCUBATE"),       # still being written
    ("proposed", "INCUBATE", "INCUBATE"),
    ("draft", "IDEA", "IDEA"),
]


@pytest.mark.parametrize("status,start,expected", MATRIX,
                         ids=[f"{s}-{st}" for s, st, _e in MATRIX])
def test_status_to_state_matrix(tmp_path, status, start, expected):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", status, source_idea="IDEA-001")
    idea_id = _idea(cfg, "fixture idea", state=start, spec_id="SPEC-0001")

    S.reconcile_ideas(cfg, apply=True)
    assert _state_of(cfg, idea_id) == expected


def test_apply_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    _idea(cfg, "shipped work", state="SPECCED", spec_id="SPEC-0001")

    S.reconcile_ideas(cfg, apply=True)
    after_first = _bytes(cfg)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert not [r for r in plan if r[2] == "advance"]
    assert _bytes(cfg) == after_first


# ---- never rewind -----------------------------------------------------------------

def test_idea_ahead_of_spec_is_flagged_not_rewound(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "draft", source_idea="IDEA-001")
    idea_id = _idea(cfg, "ran ahead", state="PROMOTED", spec_id="SPEC-0001")
    before = _state_of(cfg, idea_id)

    plan = S.reconcile_ideas(cfg, apply=True)
    assert (idea_id, "flag-ahead") in _actions(plan)
    assert _state_of(cfg, idea_id) == before


@pytest.mark.parametrize("state,status", [
    ("EXPORTED", "accepted"),        # export advances the idea; outbox status lags (SPEC-0041)
    ("EXPORTED", "in-progress"),
    ("PROMOTED", "accepted"),        # `wt spec generate` runs off an accepted spec
    ("PROMOTED", "done"),
])
def test_legitimate_terminal_states_are_not_flagged(tmp_path, state, status):
    """These combinations are the normal workflow, not drift — flagging them would make the
    command cry wolf on every outbound idea."""
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", status, source_idea="IDEA-001")
    idea_id = _idea(cfg, "normal workflow", state=state, spec_id="SPEC-0001")

    plan = S.reconcile_ideas(cfg, apply=False)
    assert (idea_id, "flag-ahead") not in _actions(plan)


# ---- robustness -------------------------------------------------------------------

def test_missing_spec_does_not_abort_the_run(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    _spec(cfg, "SPEC-0003", "done", source_idea="IDEA-003")
    _idea(cfg, "good one", state="SPECCED", spec_id="SPEC-0001")
    _idea(cfg, "dangling link", state="SPECCED", spec_id="SPEC-0099")
    _idea(cfg, "another good one", state="SPECCED", spec_id="SPEC-0003")

    plan = S.reconcile_ideas(cfg, apply=True)
    acts = _actions(plan)
    assert ("IDEA-002", "missing") in acts
    assert _state_of(cfg, "IDEA-001") == "PROMOTED"
    assert _state_of(cfg, "IDEA-003") == "PROMOTED"
    assert _state_of(cfg, "IDEA-002") == "SPECCED"      # untouched


def test_ideas_without_a_spec_are_ignored(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "no spec at all")
    assert S.reconcile_ideas(cfg, apply=False) == []


def test_unknown_status_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "banana", source_idea="IDEA-001")
    idea_id = _idea(cfg, "weird status", state="SPECCED", spec_id="SPEC-0001")
    S.reconcile_ideas(cfg, apply=True)
    assert _state_of(cfg, idea_id) == "SPECCED"


# ---- back-links -------------------------------------------------------------------

def test_backlink_written_when_unambiguous(tmp_path):
    cfg = _cfg(tmp_path)
    path = _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    idea_id = _idea(cfg, "sole claimant", state="SPECCED", spec_id="SPEC-0001")

    plan = S.reconcile_ideas(cfg, apply=False)
    assert (idea_id, "flag-no-backlink") in _actions(plan)
    assert "source_idea" not in open(path).read()

    S.reconcile_ideas(cfg, apply=True)
    assert f"source_idea: {idea_id}" in open(path).read()


def test_backlink_ambiguity_is_reported_not_guessed(tmp_path):
    cfg = _cfg(tmp_path)
    path = _spec(cfg, "SPEC-0001", "accepted", source_idea=None)
    _idea(cfg, "claimant one", state="SPECCED", spec_id="SPEC-0001")
    _idea(cfg, "claimant two", state="SPECCED", spec_id="SPEC-0001")

    plan = S.reconcile_ideas(cfg, apply=True)
    details = [d for _i, _s, a, d in plan if a == "flag-no-backlink"]
    assert details and all("ambiguous" in d for d in details)
    assert "source_idea" not in open(path).read()


def test_existing_backlink_is_not_rewritten(tmp_path):
    cfg = _cfg(tmp_path)
    path = _spec(cfg, "SPEC-0001", "accepted", source_idea="IDEA-999")
    _idea(cfg, "different idea", state="SPECCED", spec_id="SPEC-0001")
    S.reconcile_ideas(cfg, apply=True)
    assert "source_idea: IDEA-999" in open(path).read()


# ---- archival ---------------------------------------------------------------------

def test_apply_archives_and_idea_still_resolves(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    idea_id = _idea(cfg, "will be archived", state="SPECCED", spec_id="SPEC-0001")

    S.reconcile_ideas(cfg, apply=True)
    assert os.path.exists(cfg["org_ideas_archive_file"])
    assert "will be archived" in open(cfg["org_ideas_archive_file"]).read()
    assert _state_of(cfg, idea_id) == "PROMOTED"        # still resolvable after the move


# ---- surfacing --------------------------------------------------------------------

def test_stale_count_reported_and_clears(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    _idea(cfg, "shipped work", state="SPECCED", spec_id="SPEC-0001")

    assert S.stale_idea_count(cfg) == 1
    S.reconcile_ideas(cfg, apply=True)
    assert S.stale_idea_count(cfg) == 0


def test_hub_exposes_stale_ideas(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    _idea(cfg, "shipped work", state="SPECCED", spec_id="SPEC-0001")

    payload = R.hub_payload(cfg)
    assert payload["stale_ideas"] == 1
    for key in ("schema", "ideas", "internal", "outbound"):
        assert key in payload            # existing keys unchanged


def test_next_shows_the_drift_line(tmp_path):
    from wt.console import console

    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    _idea(cfg, "shipped work", state="SPECCED", spec_id="SPEC-0001")

    prev_w, prev_h = console._width, console._height
    console._width, console._height = 200, 80
    try:
        with console.capture() as cap:
            R.next_ideas(cfg)
        out = cap.get()
    finally:
        console._width, console._height = prev_w, prev_h
    assert "out of sync" in out and "wt spec reconcile" in out


def test_stale_count_is_zero_when_clean(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "accepted", source_idea="IDEA-001")
    _idea(cfg, "in flight", state="SPECCED", spec_id="SPEC-0001")
    assert S.stale_idea_count(cfg) == 0


# ---- the implied-state helper directly --------------------------------------------

@pytest.mark.parametrize("status,state,outbound,expected", [
    ("done", "SPECCED", False, "PROMOTED"),
    ("done", "SPECCED", True, "SHIPPED"),
    ("done", "INCUBATE", False, "PROMOTED"),
    ("done", "IDEA", True, "SHIPPED"),
    ("superseded", "IDEA", False, "DROPPED"),
    ("rejected", "INCUBATE", False, "DROPPED"),
    ("accepted", "IDEA", False, "SPECCED"),
    ("accepted", "SPECCED", False, None),
    ("draft", "SPECCED", False, None),
    ("done", "PROMOTED", False, None),            # terminal: never rewritten
    ("done", "EXPORTED", False, "SHIPPED"),       # SPEC-0135: EXPORTED→SHIPPED
    ("done", "EXPORTED", True, "SHIPPED"),
    ("done", "SHIPPED", True, None),
    ("", "SPECCED", False, None),
])
def test_implied_state(status, state, outbound, expected):
    assert S._implied_state(status, state, outbound=outbound) == expected


def test_outbound_done_advances_to_shipped(tmp_path):
    """SPEC-0135: outbound portable `done` → SHIPPED (not PROMOTED)."""
    cfg = _cfg(tmp_path)
    cfg["outbox_dir"] = str(tmp_path / "outbox")
    proj = pathlib.Path(cfg["outbox_dir"]) / "Example"
    proj.mkdir(parents=True)
    path = proj / "EXAMPLE-0001-fixture.md"
    path.write_text(
        "---\nid: EXAMPLE-0001\ntitle: \"fixture\"\nstatus: done\n"
        "owner: t\ncreated: 2026-01-01\nsource_idea: IDEA-001\n"
        "target_project: Example\n---\n\n## Context\n\nfixture.\n")
    idea_id = _idea(cfg, "outbound shipped", state="SPECCED", spec_id="EXAMPLE-0001")
    S.reconcile_ideas(cfg, apply=True)
    assert _state_of(cfg, idea_id) == "SHIPPED"


def test_exported_plus_done_advances_to_shipped(tmp_path):
    """SPEC-0135: EXPORTED may advance to SHIPPED when the portable is done."""
    cfg = _cfg(tmp_path)
    cfg["outbox_dir"] = str(tmp_path / "outbox")
    proj = pathlib.Path(cfg["outbox_dir"]) / "Example"
    proj.mkdir(parents=True)
    (proj / "EXAMPLE-0002-fixture.md").write_text(
        "---\nid: EXAMPLE-0002\ntitle: \"fixture\"\nstatus: done\n"
        "owner: t\ncreated: 2026-01-01\nsource_idea: IDEA-001\n"
        "target_project: Example\n---\n\n## Context\n\nfixture.\n")
    idea_id = _idea(cfg, "was exported", state="EXPORTED", spec_id="EXAMPLE-0002")
    S.reconcile_ideas(cfg, apply=True)
    assert _state_of(cfg, idea_id) == "SHIPPED"


def test_backlink_write_preserves_frontmatter_formatting(tmp_path):
    """Regression guard: an early version re-dumped the parsed YAML, which rewrote flow-style
    lists into bullets and changed quote style on every spec it annotated. The back-link must be
    a single inserted line and nothing else."""
    cfg = _cfg(tmp_path)
    path = os.path.join(cfg["specs_dir"], "0001-fixture.md")
    original = ('---\n'
                'id: SPEC-0001\n'
                'title: "fixture"\n'
                'status: accepted\n'
                'owner: t\n'
                'created: 2026-01-01\n'
                'milestone: "M1: packaging"\n'
                'tags: [infra, packaging]\n'
                'depends_on: [SPEC-0000]\n'
                '---\n'
                '\n## Context\n\nfixture.\n')
    with open(path, "w") as f:
        f.write(original)
    idea_id = _idea(cfg, "sole claimant", state="SPECCED", spec_id="SPEC-0001")

    S.reconcile_ideas(cfg, apply=True)
    after = open(path).read()
    assert f"source_idea: {idea_id}\n" in after
    assert 'tags: [infra, packaging]\n' in after        # not rewritten to a bullet list
    assert 'milestone: "M1: packaging"\n' in after      # quote style untouched
    assert 'depends_on: [SPEC-0000]\n' in after
    # exactly one line added, nothing else changed
    assert after.splitlines() == (original.splitlines()[:9]
                                 + [f"source_idea: {idea_id}"]
                                 + original.splitlines()[9:])


def test_backlink_write_skips_file_without_frontmatter(tmp_path):
    cfg = _cfg(tmp_path)
    path = os.path.join(cfg["specs_dir"], "0001-fixture.md")
    with open(path, "w") as f:
        f.write("no frontmatter here\n")
    _idea(cfg, "claimant", state="SPECCED", spec_id="SPEC-0001")
    S.reconcile_ideas(cfg, apply=True)
    assert open(path).read() == "no frontmatter here\n"


# ---- SPEC-0080: open questions on a landed spec -----------------------------------

def _with_questions(cfg, idea_id, *questions):
    from wt import explore as E
    E.set_questions(cfg, idea_id, "\n".join(questions))


def test_questions_open_reported_when_spec_done(tmp_path):
    from wt import explore as E

    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    idea_id = _idea(cfg, "landed but unanswered", state="SPECCED", spec_id="SPEC-0001")
    _with_questions(cfg, idea_id, "first question", "second question")

    plan = S.reconcile_ideas(cfg, apply=False)
    rows = [d for i, _s, a, d in plan if a == "questions-open" and i == idea_id]
    assert rows and "2 question(s)" in rows[0]


def test_questions_open_not_reported_when_spec_unfinished(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "accepted", source_idea="IDEA-001")
    idea_id = _idea(cfg, "still building", state="SPECCED", spec_id="SPEC-0001")
    _with_questions(cfg, idea_id, "legitimately still open")

    assert (idea_id, "questions-open") not in _actions(S.reconcile_ideas(cfg, apply=False))


def test_questions_open_not_reported_when_all_resolved(tmp_path):
    from wt import explore as E

    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    idea_id = _idea(cfg, "tidy", state="SPECCED", spec_id="SPEC-0001")
    _with_questions(cfg, idea_id, "answered one")
    E.resolve_questions(cfg, idea_id, None)

    assert (idea_id, "questions-open") not in _actions(S.reconcile_ideas(cfg, apply=False))


def test_apply_never_touches_questions(tmp_path):
    """The one thing this spec must not do: resolution is not derivable, so --apply may advance
    state but must leave every question exactly as it was."""
    from wt import explore as E

    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    idea_id = _idea(cfg, "advance me, not my questions", state="SPECCED", spec_id="SPEC-0001")
    _with_questions(cfg, idea_id, "leave me open", "and me")
    before = [q["state"] for q in E.read_idea_questions(cfg, W.resolve_selector(cfg, idea_id))]

    S.reconcile_ideas(cfg, apply=True)
    task = W.resolve_selector(cfg, idea_id)
    assert (task.state or "").upper() == "PROMOTED"          # state did advance
    assert [q["state"] for q in E.read_idea_questions(cfg, task)] == before


def test_open_question_idea_count(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    idea_id = _idea(cfg, "counted", state="SPECCED", spec_id="SPEC-0001")
    _with_questions(cfg, idea_id, "one")
    assert S.open_question_idea_count(cfg) == 1


def test_hub_exposes_ideas_with_open_questions(tmp_path):
    cfg = _cfg(tmp_path)
    _spec(cfg, "SPEC-0001", "done", source_idea="IDEA-001")
    idea_id = _idea(cfg, "counted", state="SPECCED", spec_id="SPEC-0001")
    _with_questions(cfg, idea_id, "one")
    payload = R.hub_payload(cfg)
    assert payload["ideas_with_open_questions"] == 1
    assert "stale_ideas" in payload


def test_skill_documents_resolving_questions():
    """Doc drift is the original defect here, so the doc is asserted."""
    import pathlib

    text = pathlib.Path("skills/wt-new-work/SKILL.md").read_text(encoding="utf-8")
    assert "--resolve" in text
    assert "questions" in text.lower()


# ---- SPEC-0257: specs' source_idea without a :SPEC: on the idea ---------------------

def _bare_idea(cfg, text, *, state="IDEA"):
    """An idea with no `:SPEC:` property."""
    W.add_idea(cfg, text)
    task = [t for t in load_tasks(cfg) if t.is_idea and t.heading == text][0]
    idea_id = task.properties.get("ID")
    if state != "IDEA":
        W.set_state_by_selector(cfg, idea_id, state)
    return idea_id


def _spec_prop(cfg, idea_id):
    return (W.resolve_selector(cfg, idea_id).properties.get("SPEC") or "").strip()


def test_source_idea_without_spec_property_links_and_advances(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "shipped by hand", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    before = _bytes(cfg)

    plan = S.reconcile_ideas(cfg, apply=False)
    assert {(i, a) for i, _s, a, _d in plan} >= {(idea_id, "link"), (idea_id, "advance")}
    assert _bytes(cfg) == before                        # dry run writes nothing

    S.reconcile_ideas(cfg, apply=True)
    assert _spec_prop(cfg, idea_id) == "SPEC-0001"
    assert _state_of(cfg, idea_id) == "PROMOTED"
    assert "shipped by hand" in open(cfg["org_ideas_archive_file"]).read()


def test_accepted_spec_links_to_specced(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "being built")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=idea_id)
    S.reconcile_ideas(cfg, apply=True)
    assert _spec_prop(cfg, idea_id) == "SPEC-0001"
    assert _state_of(cfg, idea_id) == "SPECCED"


def test_conflicting_spec_property_flagged_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _idea(cfg, "linked elsewhere", state="SPECCED", spec_id="SPEC-0002")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=idea_id)
    before = _bytes(cfg)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert (idea_id, "SPEC-0001", "flag-conflict", "idea links SPEC-0002") in plan
    assert not [r for r in plan if r[2] == "link"]
    assert _bytes(cfg) == before


def test_terminal_idea_not_rewound(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "already dropped", state="DROPPED")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=idea_id)
    S.reconcile_ideas(cfg, apply=True)
    assert _spec_prop(cfg, idea_id) == "SPEC-0001"      # linked, but the state is never rewound
    assert _state_of(cfg, idea_id) == "DROPPED"


def test_reconcile_apply_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "once only")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    S.reconcile_ideas(cfg, apply=True)
    first = _bytes(cfg)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert not [r for r in plan if r[2] in ("link", "advance")]
    assert _bytes(cfg) == first


def test_spec_ids_scope(tmp_path):
    cfg = _cfg(tmp_path)
    a = _bare_idea(cfg, "idea a")
    b = _bare_idea(cfg, "idea b")
    _spec(cfg, "SPEC-0001", "done", source_idea=a)
    _spec(cfg, "SPEC-0002", "done", source_idea=b)
    S.reconcile_ideas(cfg, apply=True, spec_ids=["SPEC-0001"])
    assert _state_of(cfg, a) == "PROMOTED" and _spec_prop(cfg, a) == "SPEC-0001"
    assert _state_of(cfg, b) == "IDEA" and _spec_prop(cfg, b) == ""


def test_spec_ids_scope_leaves_other_linked_ideas(tmp_path):
    cfg = _cfg(tmp_path)
    a = _bare_idea(cfg, "idea a")
    linked = _idea(cfg, "stale linked", state="SPECCED", spec_id="SPEC-0002")
    _spec(cfg, "SPEC-0001", "done", source_idea=a)
    _spec(cfg, "SPEC-0002", "done", source_idea=linked)
    plan = S.reconcile_ideas(cfg, apply=True, spec_ids=["SPEC-0001"])
    assert not [r for r in plan if r[0] == linked]
    assert _state_of(cfg, linked) == "SPECCED"
    assert _state_of(cfg, a) == "PROMOTED"


def test_backlinks_false_never_writes_a_spec_file(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _idea(cfg, "hand linked", state="SPECCED", spec_id="SPEC-0001")
    path = _spec(cfg, "SPEC-0001", "done", source_idea=None)
    before = open(path, "rb").read()
    plan = S.reconcile_ideas(cfg, apply=True, spec_ids=["SPEC-0001"], backlinks=False)
    assert (idea_id, "flag-no-backlink") in _actions(plan)
    assert _state_of(cfg, idea_id) == "PROMOTED"
    assert open(path, "rb").read() == before


def test_two_live_specs_same_idea_ambiguous(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "claimed twice")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=idea_id)
    before = _bytes(cfg)
    plan = S.reconcile_ideas(cfg, apply=True)
    amb = [r for r in plan if r[2] == "flag-ambiguous"]
    assert len(amb) == 1 and "SPEC-0001" in amb[0][3] and "SPEC-0002" in amb[0][3]
    assert not [r for r in plan if r[2] in ("link", "advance")]
    assert _bytes(cfg) == before


def test_superseded_plus_live_links_live(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "supersede chain")
    _spec(cfg, "SPEC-0001", "superseded", source_idea=idea_id)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=idea_id)
    S.reconcile_ideas(cfg, apply=True)
    assert _spec_prop(cfg, idea_id) == "SPEC-0002"
    assert _state_of(cfg, idea_id) == "SPECCED"          # not DROPPED


def test_scoped_run_respects_other_claimant(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "claimed twice")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=idea_id)
    before = _bytes(cfg)
    plan = S.reconcile_ideas(cfg, apply=True, spec_ids=["SPEC-0001"])
    assert [r[2] for r in plan] == ["flag-ambiguous"] and plan[0][1] == "SPEC-0001"
    assert _bytes(cfg) == before
    # the superseded spec's own scoped run neither claims nor flags: the live one owns the idea
    _spec(cfg, "SPEC-0002", "superseded", source_idea=idea_id)
    plan = S.reconcile_ideas(cfg, apply=False, spec_ids=["SPEC-0002"])
    assert plan == []


def test_stale_idea_count_exact(tmp_path):
    cfg = _cfg(tmp_path)
    linkadv = _bare_idea(cfg, "link and advance")
    linkonly = _bare_idea(cfg, "link only", state="SPECCED")
    plain = _idea(cfg, "plain advance", state="SPECCED", spec_id="SPEC-0003")
    _spec(cfg, "SPEC-0001", "done", source_idea=linkadv)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=linkonly)
    _spec(cfg, "SPEC-0003", "done", source_idea=plain)
    assert S.stale_idea_count(cfg) == 3                  # one per idea, never twice
    S.reconcile_ideas(cfg, apply=True)
    assert S.stale_idea_count(cfg) == 0


def test_cli_reconcile_prints_link_row(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from wt.cli import cli
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "cli visible")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    out = CliRunner().invoke(cli, ["spec", "reconcile"])
    assert out.exit_code == 0, out.output
    assert idea_id in out.output and "set :SPEC: SPEC-0001" in out.output
    assert _spec_prop(cfg, idea_id) == ""               # dry run


def test_superseded_spec_naming_a_successor_linked_idea_is_quiet(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _idea(cfg, "successor linked", state="SPECCED", spec_id="SPEC-0002")
    _spec(cfg, "SPEC-0001", "superseded", source_idea=idea_id)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=idea_id)
    plan = S.reconcile_ideas(cfg, apply=False)
    assert not [r for r in plan if r[2] == "flag-conflict"]


def test_link_skipped_on_drift_does_not_advance(tmp_path, monkeypatch):
    """The idea gains a different `:SPEC:` between plan and write: no link, and no state change
    derived from the spec that lost the race."""
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "raced")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    real = S.W.resolve_selector
    raced = []

    def resolve(c, sel):
        if not raced:
            raced.append(1)
            W.set_property(c, real(c, sel), "SPEC", "SPEC-0002")
        return real(c, sel)
    monkeypatch.setattr(S.W, "resolve_selector", resolve)
    S.reconcile_ideas(cfg, apply=True)
    assert _spec_prop(cfg, idea_id) == "SPEC-0002"
    assert _state_of(cfg, idea_id) == "IDEA"            # not PROMOTED/archived by SPEC-0001
