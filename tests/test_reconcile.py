"""SPEC-0078: `wt spec reconcile` — sync idea state with the linked spec's status.

The core contract is the status→state table, so it is enumerated rather than sampled. Writes are
opt-in (`--apply`) because advancing to PROMOTED/DROPPED archives the subtree (SPEC-0073).
"""
import dataclasses
import datetime as dt
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


def _spec(cfg, spec_id, status, *, source_idea="__auto__", extra="", created=None):
    """Write a minimal internal spec with the given status. `created` defaults to today, the day
    `add_idea` stamps the fixture ideas with (SPEC-0267: an idea may not postdate its spec)."""
    created = created or dt.datetime.now(TZ).strftime("%Y-%m-%d")
    num = spec_id.split("-")[1]
    path = os.path.join(cfg["specs_dir"], f"{num}-fixture.md")
    si = "" if source_idea is None else f"source_idea: {source_idea}\n"
    with open(path, "w") as f:
        f.write(f"---\nid: {spec_id}\ntitle: \"fixture\"\nstatus: {status}\n"
                f"owner: t\ncreated: {created}\n{si}{extra}---\n\n## Context\n\nfixture.\n")
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


def test_link_property_is_written_before_the_state_advance(tmp_path, monkeypatch):
    """PROMOTED archives the subtree, so `:SPEC:` must land first. The archive file is loaded
    too, so the end state alone cannot tell the orders apart: record the writes."""
    from wt import explore as EX
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "ordered", state="INCUBATE")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    calls = []
    real_prop, real_state = S.W.set_property, EX.set_idea_state

    def prop(c, task, key, value, *a, **k):
        calls.append(("property", key))
        return real_prop(c, task, key, value, *a, **k)

    def state(c, iid, target, *a, **k):
        calls.append(("state", target))
        return real_state(c, iid, target, *a, **k)
    monkeypatch.setattr(S.W, "set_property", prop)
    monkeypatch.setattr(EX, "set_idea_state", state)
    S.reconcile_ideas(cfg, apply=True)
    assert calls == [("property", "SPEC"), ("state", "PROMOTED")]


def test_lone_superseded_claimant_is_ambiguous_and_writes_nothing(tmp_path):
    """No live spec names the idea: not a claimant by default, or it would be DROPPED."""
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "only a dead claimant")
    _spec(cfg, "SPEC-0001", "superseded", source_idea=idea_id)
    before = _bytes(cfg)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert [(r[0], r[1], r[2]) for r in plan] == [(idea_id, "SPEC-0001", "flag-ambiguous")]
    assert _bytes(cfg) == before
    assert _state_of(cfg, idea_id) == "IDEA" and _spec_prop(cfg, idea_id) == ""


def test_claim_is_attributed_by_filename_not_frontmatter_id(tmp_path):
    """`_load_spec_fm` finds specs by file number, so a copied file with a stale `id:` must not
    link the idea to (and take the status of) the spec it was copied from."""
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "copied spec")
    _spec(cfg, "SPEC-0001", "done", source_idea=None)
    path = _spec(cfg, "SPEC-0002", "accepted", source_idea=idea_id)
    text = open(path).read().replace("id: SPEC-0002", "id: SPEC-0001")
    open(path, "w").write(text)
    S.reconcile_ideas(cfg, apply=True)
    assert _spec_prop(cfg, idea_id) == "SPEC-0002"
    assert _state_of(cfg, idea_id) == "SPECCED"          # SPEC-0002's status, not SPEC-0001's


def test_link_skipped_on_drift_is_reported_as_a_conflict(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "raced again")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    real = S.W.resolve_selector
    raced = []

    def resolve(c, sel):
        if not raced:
            raced.append(1)
            W.set_property(c, real(c, sel), "SPEC", "SPEC-0002")
        return real(c, sel)
    monkeypatch.setattr(S.W, "resolve_selector", resolve)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert [r[2] for r in plan if r[0] == idea_id] == ["flag-conflict"]    # no link, no advance
    assert plan[0][3] == "idea links SPEC-0002"


def test_cli_reconcile_marks_flag_rows_and_summary(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from wt.cli import cli
    cfg = _cfg(tmp_path)
    amb = _bare_idea(cfg, "claimed twice")
    conflict = _idea(cfg, "linked elsewhere", state="SPECCED", spec_id="SPEC-0004")
    good = _bare_idea(cfg, "will link")
    _spec(cfg, "SPEC-0001", "done", source_idea=amb)
    _spec(cfg, "SPEC-0002", "accepted", source_idea=amb)
    _spec(cfg, "SPEC-0003", "accepted", source_idea=conflict)
    _spec(cfg, "SPEC-0004", "accepted", source_idea=conflict)
    _spec(cfg, "SPEC-0005", "done", source_idea=good)
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    out = CliRunner().invoke(cli, ["spec", "reconcile"]).output
    rows = {line.split()[1]: line for line in out.splitlines() if line.startswith("!") or
            line.startswith("→")}
    assert rows[amb].startswith("!") and "specs naming this idea: SPEC-0001, SPEC-0002" in rows[amb]
    assert rows[conflict].startswith("!") and "idea links SPEC-0004" in rows[conflict]
    assert "1 idea(s) would advance, 1 would be linked" in out and "--apply" in out

    out = CliRunner().invoke(cli, ["spec", "reconcile", "--apply"]).output
    assert "+ " + good in out
    assert "1 idea(s) advanced, 1 linked" in out
    assert _spec_prop(cfg, good) == "SPEC-0005" and _state_of(cfg, good) == "PROMOTED"


# ---- SPEC-0267: the source_idea walk verifies idea identity before linking -----------

def _stamp_idea(cfg, idea_id, stamp):
    """Set an idea's `:CREATED:` (`None` removes it)."""
    task = W.resolve_selector(cfg, idea_id)
    if stamp is None:
        W.set_property(cfg, task, "CREATED", "")
    else:
        W.set_property(cfg, task, "CREATED", stamp)


def _rows(plan, idea_id):
    return [(s, a) for i, s, a, _d in plan if i == idea_id]


def test_reused_id_is_flagged_not_linked(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "unrelated, reused id", state="INCUBATE")
    _stamp_idea(cfg, idea_id, "[2026-10-25 Sun 09:00]")
    path = _spec(cfg, "SPEC-0001", "done", source_idea=idea_id, created="2026-10-10")
    before, spec_before = _bytes(cfg), open(path, "rb").read()

    for apply in (False, True):
        plan = S.reconcile_ideas(cfg, apply=apply)
        assert _rows(plan, idea_id) == [("SPEC-0001", "flag-mismatch")]
        detail = plan[0][3]
        assert "2026-10-25" in detail and "2026-10-10" in detail
        assert _bytes(cfg) == before and open(path, "rb").read() == spec_before
    assert _state_of(cfg, idea_id) == "INCUBATE" and _spec_prop(cfg, idea_id) == ""


def test_recent_legit_link_still_links(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "legit", state="INCUBATE")
    _stamp_idea(cfg, idea_id, "[2026-10-25 Sun 09:00]")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id, created="2026-10-26")
    plan = S.reconcile_ideas(cfg, apply=True)
    assert _actions(plan) >= {(idea_id, "link"), (idea_id, "advance")}
    assert _spec_prop(cfg, idea_id) == "SPEC-0001" and _state_of(cfg, idea_id) == "PROMOTED"


def test_same_day_created_counts_as_legit(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "same day")
    _stamp_idea(cfg, idea_id, "[2026-10-25 Sun 23:59]")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=idea_id, created="2026-10-25")
    assert (idea_id, "link") in _actions(S.reconcile_ideas(cfg))


def test_missing_created_is_flagged(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "no stamp")
    _stamp_idea(cfg, idea_id, None)
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert _rows(plan, idea_id) == [("SPEC-0001", "flag-mismatch")]
    assert ":CREATED:" in plan[0][3]
    assert _spec_prop(cfg, idea_id) == ""


def test_unparseable_spec_created_is_flagged(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "odd spec date")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id, created="someday")
    plan = S.reconcile_ideas(cfg, apply=True)
    assert _rows(plan, idea_id) == [("SPEC-0001", "flag-mismatch")]
    assert _spec_prop(cfg, idea_id) == ""


def test_reused_id_two_claimants_picks_genuine(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "reused once")
    _stamp_idea(cfg, idea_id, "[2026-10-25 Sun 09:00]")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id, created="2026-10-01")     # old: dropped
    _spec(cfg, "SPEC-0002", "accepted", source_idea=idea_id, created="2026-10-26")  # genuine
    plan = S.reconcile_ideas(cfg)
    assert (idea_id, "flag-ambiguous") not in _actions(plan)
    assert ("SPEC-0002", "link") in _rows(plan, idea_id)
    assert ("SPEC-0001", "flag-mismatch") in _rows(plan, idea_id)
    assert ("SPEC-0001", "link") not in _rows(plan, idea_id)
    S.reconcile_ideas(cfg, apply=True)
    assert _spec_prop(cfg, idea_id) == "SPEC-0002" and _state_of(cfg, idea_id) == "SPECCED"


def test_all_claimants_failing_is_mismatch_not_ambiguous(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "reused twice")
    _stamp_idea(cfg, idea_id, "[2026-10-25 Sun 09:00]")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id, created="2026-10-01")
    _spec(cfg, "SPEC-0002", "accepted", source_idea=idea_id, created="2026-10-02")
    plan = S.reconcile_ideas(cfg, apply=True)
    assert sorted(_rows(plan, idea_id)) == [("SPEC-0001", "flag-mismatch"),
                                            ("SPEC-0002", "flag-mismatch")]
    assert _spec_prop(cfg, idea_id) == ""


def test_reused_id_on_an_already_linked_idea_stays_conflict(tmp_path):
    cfg = _cfg(tmp_path)
    idea_id = _idea(cfg, "new idea, same id", state="SPECCED", spec_id="SPEC-0009")
    _stamp_idea(cfg, idea_id, "[2026-10-25 Sun 09:00]")
    _spec(cfg, "SPEC-0009", "accepted", source_idea=None)
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id, created="2026-10-01")
    rows = _rows(S.reconcile_ideas(cfg), idea_id)
    assert ("SPEC-0001", "flag-conflict") in rows
    assert not [r for r in rows if r[0] == "SPEC-0001" and r[1] != "flag-conflict"]


def _second_file_idea(cfg, text, idea_id, stamp):
    """An idea with a chosen `:ID:` in a second org file, as after an ideas-file rotation."""
    path = pathlib.Path(cfg["org_ideas_file"]).with_name("ideas-20261025.org")
    path.write_text("#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED SHIPPED DROPPED RESEARCHED\n"
                    f"* IDEA {text}\n:PROPERTIES:\n:ID: {idea_id}\n:CREATED: {stamp}\n"
                    ":END:\n")


def _dup_pair(tmp_path, spec_created):
    """Old idea (2026-09-01) and a reused-id new idea (2026-10-25) in a second org file, plus one
    spec naming the shared id."""
    cfg = _cfg(tmp_path)
    shared = _bare_idea(cfg, "old idea")
    _stamp_idea(cfg, shared, "[2026-09-01 Tue 09:00]")
    _second_file_idea(cfg, "new idea", shared, "[2026-10-25 Sun 09:00]")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=shared, created=spec_created)
    return cfg, shared


def _linked_headings(cfg):
    return [t.heading for t in load_tasks(cfg) if t.is_idea and t.properties.get("SPEC")]


def _states(cfg):
    return {t.heading: t.state for t in load_tasks(cfg) if t.is_idea}


def _files(cfg):
    return {t.heading: os.path.basename(t.file) for t in load_tasks(cfg) if t.is_idea}


def test_duplicate_idea_ids_resolve_or_flag(tmp_path):
    # a new spec resolves to the new idea (both are date-valid, newest wins), never the old one
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    plan = S.reconcile_ideas(cfg, apply=True)
    assert ("SPEC-0001", "link") in _rows(plan, shared)
    assert _linked_headings(cfg) == ["new idea"]
    assert _states(cfg) == {"old idea": "IDEA", "new idea": "SPECCED"}


def test_duplicate_idea_ids_old_spec_resolves_to_old_idea(tmp_path):
    cfg, shared = _dup_pair(tmp_path, "2026-09-15")
    S.reconcile_ideas(cfg, apply=True)
    assert _linked_headings(cfg) == ["old idea"]
    assert _states(cfg) == {"old idea": "SPECCED", "new idea": "IDEA"}


def test_duplicate_idea_ids_done_spec_advances_and_archives_the_right_one(tmp_path):
    # PROMOTED auto-archives; the archive re-find must not trip on the id both ideas carry
    for spec_created, promoted, other in (("2026-10-26", "new idea", "old idea"),
                                          ("2026-09-15", "old idea", "new idea")):
        base = tmp_path / spec_created
        base.mkdir()
        cfg, shared = _dup_pair(base, spec_created)
        _spec(cfg, "SPEC-0001", "done", source_idea=shared, created=spec_created)
        S.reconcile_ideas(cfg, apply=True)
        assert _states(cfg) == {promoted: "PROMOTED", other: "IDEA"}
        assert _files(cfg)[promoted] == "ideas-archive.org"
        assert _files(cfg)[other] != "ideas-archive.org"
        assert _linked_headings(cfg) == [promoted]


def test_duplicate_id_existing_link_settles_the_spec_and_newer_twin_stays_unlinked(tmp_path):
    # the old twin already carries :SPEC: SPEC-0001; the newer unlinked twin (date-valid for the
    # spec) must not be linked, advanced or archived because of the shared id
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    _spec(cfg, "SPEC-0001", "done", source_idea=shared, created="2026-10-26")
    old = next(t for t in load_tasks(cfg) if t.is_idea and t.heading == "old idea")
    W.set_property(cfg, old, "SPEC", "SPEC-0001")
    plan = S.reconcile_ideas(cfg, apply=True)
    assert "link" not in {a for _s, a in _rows(plan, shared)}
    assert not [r for r in _rows(plan, shared) if r[1].startswith("flag-")]
    assert _states(cfg) == {"old idea": "PROMOTED", "new idea": "IDEA"}
    assert _linked_headings(cfg) == ["old idea"]
    assert _files(cfg)["new idea"] != "ideas-archive.org"


def test_duplicate_id_twin_linked_elsewhere_does_not_hide_a_valid_unlinked_one(tmp_path):
    # the newer twin belongs to another spec: SPEC-0001 (new enough for both) takes the old one
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    _spec(cfg, "SPEC-0002", "accepted", source_idea=None)
    new = next(t for t in load_tasks(cfg) if t.is_idea and t.heading == "new idea")
    W.set_property(cfg, new, "SPEC", "SPEC-0002")
    S.reconcile_ideas(cfg, apply=True)
    assert sorted(_linked_headings(cfg)) == ["new idea", "old idea"]
    old = next(t for t in load_tasks(cfg) if t.is_idea and t.heading == "old idea")
    assert old.properties["SPEC"] == "SPEC-0001"


def test_unresolvable_pin_is_not_guessed(tmp_path):
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    assert S._resolve_idea(cfg, shared, ("/nowhere.org", "gone")) is None


def test_duplicate_idea_ids_none_date_valid_flags(tmp_path):
    cfg, shared = _dup_pair(tmp_path, "2026-08-01")
    plan = S.reconcile_ideas(cfg, apply=True)
    assert _rows(plan, shared) == [("SPEC-0001", "flag-mismatch")]
    assert not _linked_headings(cfg)


def test_duplicate_ids_with_same_created_are_ambiguous(tmp_path):
    cfg = _cfg(tmp_path)
    a = _bare_idea(cfg, "twin a")
    _stamp_idea(cfg, a, "[2026-10-01 Thu 09:00]")
    _second_file_idea(cfg, "twin b", a, "[2026-10-01 Thu 09:00]")
    _spec(cfg, "SPEC-0001", "accepted", source_idea=a, created="2026-10-26")
    plan = S.reconcile_ideas(cfg, apply=True)
    assert _rows(plan, a) == [("SPEC-0001", "flag-ambiguous")]
    assert not [t for t in load_tasks(cfg) if t.is_idea and t.properties.get("SPEC")]


def test_scoped_run_only_reports_in_scope_mismatch(tmp_path):
    cfg = _cfg(tmp_path)
    a = _bare_idea(cfg, "reused a")
    b = _bare_idea(cfg, "reused b")
    for iid in (a, b):
        _stamp_idea(cfg, iid, "[2026-10-25 Sun 09:00]")
    _spec(cfg, "SPEC-0001", "done", source_idea=a, created="2026-10-01")
    _spec(cfg, "SPEC-0002", "done", source_idea=b, created="2026-10-01")
    plan = S.reconcile_ideas(cfg, spec_ids=["SPEC-0002"])
    assert [(i, s, x) for i, s, x, _d in plan] == [(b, "SPEC-0002", "flag-mismatch")]


def test_cli_reconcile_renders_flag_mismatch(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from wt.cli import cli
    cfg = _cfg(tmp_path)
    idea_id = _bare_idea(cfg, "reused cli")
    _stamp_idea(cfg, idea_id, "[2026-10-25 Sun 09:00]")
    _spec(cfg, "SPEC-0001", "done", source_idea=idea_id, created="2026-10-01")
    monkeypatch.setattr("wt.cli.load_config", lambda: cfg)
    out = CliRunner().invoke(cli, ["spec", "reconcile"])
    assert out.exit_code == 0, out.output
    (row,) = [ln for ln in out.output.splitlines() if idea_id in ln]
    assert row.startswith("≠") and "id likely reused" in row


# ---- SPEC-0267 review follow-ups: date forms, duplicate-id edge cases, drift ---------

@pytest.mark.parametrize("created", ['"2026-10-10"', "'2026-10-10'", "2026-10-10 08:30:00"])
def test_spec_created_string_and_datetime_forms_compare_like_a_date(tmp_path, created):
    """A quoted `YYYY-MM-DD` parses as a str and an unquoted timestamp as a datetime; both must
    gate exactly like the plain YAML date the other fixtures write."""
    cfg = _cfg(tmp_path)
    reused = _bare_idea(cfg, "reused, odd date form")
    _stamp_idea(cfg, reused, "[2026-10-25 Sun 09:00]")
    _spec(cfg, "SPEC-0001", "done", source_idea=reused, created=created)
    legit = _bare_idea(cfg, "legit, odd date form")
    _stamp_idea(cfg, legit, "[2026-10-10 Sat 23:59]")       # same day, later hour: still legit
    _spec(cfg, "SPEC-0002", "accepted", source_idea=legit, created=created)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert _rows(plan, reused) == [("SPEC-0001", "flag-mismatch")]
    assert ("SPEC-0002", "link") in _rows(plan, legit)
    assert _spec_prop(cfg, reused) == "" and _spec_prop(cfg, legit) == "SPEC-0002"


def test_as_date_forms():
    assert S._as_date(dt.date(2026, 10, 10)) == dt.date(2026, 10, 10)
    assert S._as_date(dt.datetime(2026, 10, 10, 23, 59)) == dt.date(2026, 10, 10)
    assert S._as_date("2026-10-10") == dt.date(2026, 10, 10)
    assert S._as_date(None) is None and S._as_date("someday") is None


def test_duplicate_ids_only_dead_claimants_judge_the_newest_idea(tmp_path):
    """No live spec names the shared id: `flag-ambiguous` only while the newest idea is unlinked."""
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    _spec(cfg, "SPEC-0001", "superseded", source_idea=shared, created="2026-10-26")
    before = _bytes(cfg)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert _rows(plan, shared) == [("SPEC-0001", "flag-ambiguous")]
    assert _bytes(cfg) == before

    # the newest idea (the 2026-10-25 one) is linked elsewhere: nothing to flag
    new = [t for t in load_tasks(cfg) if t.is_idea and t.heading == "new idea"][0]
    W.set_property(cfg, new, "SPEC", "SPEC-0009")
    assert "flag-ambiguous" not in [a for _s, a in _rows(S.reconcile_ideas(cfg), shared)]


def test_duplicate_ids_dead_claimant_ignores_an_older_twins_link(tmp_path):
    """The link state that matters is the newest idea's, not whichever twin is found first."""
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    _spec(cfg, "SPEC-0001", "rejected", source_idea=shared, created="2026-10-26")
    old = [t for t in load_tasks(cfg) if t.is_idea and t.heading == "old idea"][0]
    W.set_property(cfg, old, "SPEC", "SPEC-0009")
    assert ("SPEC-0001", "flag-ambiguous") in _rows(S.reconcile_ideas(cfg), shared)


def _twin_specs(cfg, shared):
    """Both twins planned in one run: the old spec -> old idea, the new spec -> new idea."""
    _spec(cfg, "SPEC-0001", "done", source_idea=shared, created="2026-09-15")
    _spec(cfg, "SPEC-0002", "accepted", source_idea=shared, created="2026-10-26")


def test_duplicate_id_twins_each_link_and_advance_in_one_run(tmp_path):
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    _twin_specs(cfg, shared)
    plan = S.reconcile_ideas(cfg, apply=True)
    assert {("SPEC-0001", "link"), ("SPEC-0001", "advance"), ("SPEC-0002", "link")} \
        <= set(_rows(plan, shared))
    assert _states(cfg) == {"old idea": "PROMOTED", "new idea": "SPECCED"}
    assert sorted(_linked_headings(cfg)) == ["new idea", "old idea"]


@pytest.mark.parametrize("raced_heading,survivor", [("old idea", "new idea"),
                                                    ("new idea", "old idea")])
def test_drift_on_one_duplicate_id_twin_leaves_the_other_alone(tmp_path, monkeypatch,
                                                              raced_heading, survivor):
    """Drift is scoped to the planned (idea, spec), not the bare id both twins carry."""
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    _twin_specs(cfg, shared)
    real, raced = S._resolve_idea, []
    spec_of = {"old idea": "SPEC-0001", "new idea": "SPEC-0002"}

    def resolve(c, idea_id, pin):
        task = real(c, idea_id, pin)
        if task.heading == raced_heading and not raced:
            raced.append(1)
            W.set_property(c, task, "SPEC", "SPEC-0009")        # linked elsewhere since the plan
            return real(c, idea_id, pin)
        return task
    monkeypatch.setattr(S, "_resolve_idea", resolve)
    plan = S.reconcile_ideas(cfg, apply=True)

    rows = _rows(plan, shared)
    lost = spec_of[raced_heading]
    assert (lost, "flag-conflict") in rows
    assert (lost, "link") not in rows and (lost, "advance") not in rows
    kept = spec_of[survivor]
    assert (kept, "link") in rows
    spec = {t.heading: t.properties.get("SPEC") for t in load_tasks(cfg) if t.is_idea}
    assert spec[raced_heading] == "SPEC-0009" and spec[survivor] == kept
    if survivor == "old idea":      # SPEC-0001 is done: the surviving twin still advances
        assert (kept, "advance") in rows and _states(cfg)["old idea"] == "PROMOTED"
    assert _states(cfg)[raced_heading] == "IDEA"


def test_fresh_idea_reraises_without_exactly_one_file_heading_match(tmp_path):
    cfg, shared = _dup_pair(tmp_path, "2026-10-26")
    task = [t for t in load_tasks(cfg) if t.is_idea and t.heading == "new idea"][0]
    assert W._fresh_idea(cfg, task).heading == "new idea"           # one hit: resolved by pin
    ghost = dataclasses.replace(task, heading="vanished")           # zero hits
    with pytest.raises(ValueError):
        W._fresh_idea(cfg, ghost)
    # several hits: the same file repeats both the id and the heading
    path = pathlib.Path(task.file)
    path.write_text(path.read_text() + f"* IDEA new idea\n:PROPERTIES:\n:ID: {shared}\n:END:\n")
    twins = [t for t in load_tasks(cfg) if t.is_idea and t.heading == "new idea"
             and t.file == task.file]
    assert len(twins) == 2
    with pytest.raises(ValueError):
        W._fresh_idea(cfg, twins[0])
