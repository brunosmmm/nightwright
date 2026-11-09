"""SPEC-0076: per-idea `:CREATED:` / `:UPDATED:` stamps.

`:CREATED:` is written once at capture. `:UPDATED:` bumps on content + lifecycle changes, and
must NOT bump on clock activity, archival, rotation, or the formatting-only normalizers. The
whole risk is a missed (or spurious) hook, so the bump/no-bump matrix is tested per mutator.

Technique: rather than freezing the clock, each test seeds `:UPDATED:` with an obviously old
stamp and asserts whether the operation moved it. That needs no patching of shared modules.
"""
import datetime as dt
import os
from zoneinfo import ZoneInfo

import pytest

from nightwright import explore as E
from nightwright import org_write as W
from nightwright import report as R
from nightwright.org import load_tasks

pytestmark = pytest.mark.fast

OLD = "[2019-01-01 Tue 00:00]"


def _cfg(tmp_path):
    org = tmp_path / "org"
    org.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {"org_files": [str(org)], "org_todo_keywords": ["TODO", "|", "DONE"],
            "org_idea_keywords": ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "EXPORTED",
                                  "DROPPED"],
            "timezone": "America/New_York", "_tz": ZoneInfo("America/New_York"),
            "data_dir": str(data), "org_capture_file": str(org / "inbox.org"),
            "org_ideas_file": str(org / "ai" / "ideas.org"),
            # SPEC-0079: state the specs dir so spec lookups can never reach the live repo.
            "specs_dir": str(tmp_path / "specs"),
            "org_ideas_archive_file": str(org / "ai" / "ideas-archive.org"),
            "config_dir": str(tmp_path / "cfg"), "project_axis": "bucket"}


def _idea(cfg, sel="IDEA-001"):
    return W.resolve_selector(cfg, sel)


def _stamps(cfg, sel="IDEA-001"):
    p = _idea(cfg, sel).properties
    return (p.get("CREATED") or "").strip(), (p.get("UPDATED") or "").strip()


def _idea_files(cfg):
    root = os.path.dirname(cfg["org_ideas_file"])
    return [os.path.join(root, p) for p in os.listdir(root) if p.endswith(".org")]


def _rewrite_updated(cfg, value):
    """Force every `:UPDATED:` to `value` (or drop the line when value is None)."""
    for path in _idea_files(cfg):
        out = []
        for ln in open(path).read().splitlines(keepends=True):
            if ln.strip().startswith(":UPDATED:"):
                if value is None:
                    continue
                ln = ln.split(":UPDATED:")[0] + f":UPDATED: {value}\n"
            out.append(ln)
        with open(path, "w") as f:
            f.writelines(out)


def _seed(cfg, text="stamp me", **kw):
    W.add_idea(cfg, text, **kw)
    _rewrite_updated(cfg, OLD)


def _count_writes(monkeypatch, module):
    """Count `_atomic_backup_write` calls made through `module`. Counting backup *files* would
    undercount: their names carry a whole-second timestamp, so two writes in the same second
    collide onto one filename."""
    calls = []
    real = module._atomic_backup_write

    def spy(cfg, path, content):
        calls.append(path)
        return real(cfg, path, content)

    monkeypatch.setattr(module, "_atomic_backup_write", spy)
    return calls


# ---- capture ---------------------------------------------------------------------

def test_capture_writes_created_and_updated(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "stamp me")
    created, updated = _stamps(cfg)
    assert created and updated and created == updated
    assert created.startswith("[") and created.endswith("]")
    assert len(created.split()) == 3, created          # [YYYY-MM-DD Day HH:MM]
    assert created == W._inactive_stamp(cfg, with_time=True)


def test_created_comes_right_after_id(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "stamp me")
    props = [ln.strip() for ln in open(cfg["org_ideas_file"]).read().splitlines()
             if ln.strip().startswith(":")]
    assert props[1].startswith(":ID:") and props[2].startswith(":CREATED:")


# ---- the bump matrix -------------------------------------------------------------

BUMPERS = [
    ("summary", lambda cfg: E.set_summary(cfg, "IDEA-001", "new summary")),
    ("questions --set", lambda cfg: E.set_questions(cfg, "IDEA-001", "a question")),
    ("questions --add", lambda cfg: E.add_question(cfg, "IDEA-001", "another question")),
    ("questions --resolve", lambda cfg: (E.set_questions(cfg, "IDEA-001", "q"),
                                         E.resolve_question(cfg, "IDEA-001", 1))),
    ("log", lambda cfg: E.append_log(cfg, "IDEA-001", "a finding")),
    ("retitle", lambda cfg: E.retitle_idea(cfg, "IDEA-001", "a sharper title")),
    ("kind", lambda cfg: W.set_idea_kind(cfg, "IDEA-001", "bug")),
    ("state", lambda cfg: W.set_state_by_selector(cfg, "IDEA-001", "INCUBATE")),
    ("close", lambda cfg: E.close_idea(cfg, "IDEA-001", "drop", reason="not worth it")),
    ("set_property", lambda cfg: W.set_property(cfg, _idea(cfg), "SPEC", "SPEC-9999")),
]


@pytest.mark.parametrize("label,mutate", BUMPERS, ids=[b[0] for b in BUMPERS])
def test_mutator_bumps_updated_and_never_created(tmp_path, label, mutate):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    created_before = _stamps(cfg)[0]

    mutate(cfg)

    created_after, updated_after = _stamps(cfg)
    assert updated_after != OLD, f"{label} did not bump :UPDATED:"
    today = dt.datetime.now(cfg["_tz"]).strftime("%Y-%m-%d")
    assert today in updated_after, f"{label} bumped to {updated_after}, expected {today}"
    assert created_after == created_before, f"{label} modified :CREATED:"


NON_BUMPERS = [
    ("clock-in/out", lambda cfg: (W.clock_in(cfg, "IDEA-001"), W.clock_out(cfg, "IDEA-001"))),
    ("normalize-logs", lambda cfg: E.normalize_idea_log_stamps(cfg)),
    ("normalize-questions", lambda cfg: E.normalize_idea_questions(cfg)),
    ("reindex", lambda cfg: W.reindex_ideas(cfg)),
    ("archive", lambda cfg: W.archive_idea(cfg, _idea(cfg))),
]


@pytest.mark.parametrize("label,mutate", NON_BUMPERS, ids=[b[0] for b in NON_BUMPERS])
def test_excluded_writes_do_not_bump(tmp_path, label, mutate):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "stamp me")
    E.append_log(cfg, "IDEA-001", "seed a log for the normalizers to look at")
    _rewrite_updated(cfg, OLD)
    before = _stamps(cfg)

    mutate(cfg)
    assert _stamps(cfg) == before, f"{label} changed a stamp"


# ---- archival / rotation are verbatim moves --------------------------------------

def test_archive_moves_stamps_verbatim(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    created, updated = _stamps(cfg)

    W.archive_idea(cfg, _idea(cfg))
    archived = open(cfg["org_ideas_archive_file"]).read()
    assert f":CREATED: {created}" in archived
    assert f":UPDATED: {updated}" in archived


def test_rotation_preserves_stamps(tmp_path):
    cfg = dict(_cfg(tmp_path), idea_rotation_max_lines=1)
    _seed(cfg)
    before = _stamps(cfg)
    W.add_idea(cfg, "second idea forces the rotation")
    assert [p for p in os.listdir(os.path.dirname(cfg["org_ideas_file"]))
            if p.startswith("ideas-2")], "expected a rotated file"
    assert _stamps(cfg) == before, "rotation must not touch stamps"


# ---- one write per mutation -------------------------------------------------------

def test_stamping_adds_no_extra_write(tmp_path, monkeypatch):
    """The stamp must ride the mutator's existing write, not trigger a second read-modify-write
    (which would also double the backup churn on every `nw idea log`)."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "stamp me")

    writes = _count_writes(monkeypatch, E)
    E.append_log(cfg, "IDEA-001", "a finding")
    assert len(writes) == 1, writes
    assert (_stamps(cfg)[1]), "…and it still stamped"


def test_state_change_stamps_in_one_write(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    writes = _count_writes(monkeypatch, W)
    W.set_state_by_selector(cfg, "IDEA-001", "INCUBATE")
    assert len(writes) == 1, writes
    assert _stamps(cfg)[1] != OLD


# ---- ordinary tasks are untouched -------------------------------------------------

def test_non_idea_task_is_not_stamped(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "an ordinary task")
    task = next(t for t in load_tasks(cfg) if not t.is_idea)
    W.set_state(cfg, task, "DONE")
    assert "UPDATED" not in open(cfg["org_capture_file"]).read()


def test_non_idea_set_property_is_not_stamped(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_task(cfg, "an ordinary task")
    task = next(t for t in load_tasks(cfg) if not t.is_idea)
    W.set_property(cfg, task, "TOPIC", "DEMO-1")
    assert "UPDATED" not in open(cfg["org_capture_file"]).read()


# ---- drawer creation must not corrupt the body -----------------------------------

def test_stamp_creates_missing_drawer_without_corrupting_sections(tmp_path):
    """Hand-authxxed idea with no :PROPERTIES: drawer — creating one inserts a line above the
    body, which is exactly why stamping happens last."""
    cfg = _cfg(tmp_path)
    path = cfg["org_ideas_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED DROPPED\n\n"
                "* IDEA hand written\n"
                "** Summary\nkeep me\n"
                "** Open questions\n*** OPEN keep this question\n"
                "** Log\n*** [2026-01-02 Fri 08:00]\nkeep this finding\n")
    E.append_log(cfg, "hand written", "a new finding")

    fresh = next(t for t in load_tasks(cfg) if t.is_idea)
    assert (fresh.properties.get("UPDATED") or "").strip()
    enr = E.read_idea_enrichment(cfg, fresh)
    assert "keep me" in enr["summary"]
    assert "keep this question" in enr["questions"]
    assert "keep this finding" in enr["log"] and "a new finding" in enr["log"]


# ---- backfill ---------------------------------------------------------------------

def test_backfill_derives_from_newest_log_stamp(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "has a log")
    E.append_log(cfg, "IDEA-001", "older", when="2026-01-02 Fri 08:00")
    E.append_log(cfg, "IDEA-001", "newer", when="2026-04-05 Sun 17:45")
    _rewrite_updated(cfg, None)

    assert E.backfill_idea_stamps(cfg) == [("IDEA-001", "from-log")]
    assert "2026-04-05" in _stamps(cfg)[1]


def test_backfill_uses_max_not_last_log_stamp(tmp_path):
    """Out-of-order Log entries must not misdate the idea."""
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "out of order logs")
    E.append_log(cfg, "IDEA-001", "newest", when="2026-04-05 Sun 17:45")
    E.append_log(cfg, "IDEA-001", "older, appended after", when="2026-01-02 Fri 08:00")
    _rewrite_updated(cfg, None)

    E.backfill_idea_stamps(cfg)
    assert "2026-04-05" in _stamps(cfg)[1]


def test_backfill_falls_back_to_closed(tmp_path):
    """No Log, but a CLOSED: stamp — hand-authxxed so no auto-archive is involved."""
    cfg = _cfg(tmp_path)
    path = cfg["org_ideas_file"]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("#+TODO: IDEA INCUBATE SPECCED | PROMOTED EXPORTED DROPPED\n\n"
                "* DROPPED closed but never logged\n"
                "  CLOSED: [2026-02-03 Tue]\n"
                "  :PROPERTIES:\n  :ID: IDEA-001\n  :END:\n")
    assert E.backfill_idea_stamps(cfg) == [("IDEA-001", "from-closed")]
    assert "2026-02-03" in _stamps(cfg)[1]


def test_backfill_skips_ideas_with_no_signal(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "no signal at all")
    _rewrite_updated(cfg, None)

    assert E.backfill_idea_stamps(cfg) == [("IDEA-001", "skipped")]
    assert not _stamps(cfg)[1], "skipped ideas must stay blank, not invented"


def test_backfill_is_idempotent(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "has a log")
    E.append_log(cfg, "IDEA-001", "a finding", when="2026-04-05 Sun 17:45")
    _rewrite_updated(cfg, None)

    assert E.backfill_idea_stamps(cfg) == [("IDEA-001", "from-log")]
    body = open(cfg["org_ideas_file"]).read()
    assert E.backfill_idea_stamps(cfg) == []
    assert open(cfg["org_ideas_file"]).read() == body, "second run rewrote the file"


def test_backfill_spans_rotated_files(tmp_path):
    """SPEC-0074: enumerate every file an idea lives in, not just org_ideas_file."""
    cfg = dict(_cfg(tmp_path), idea_rotation_max_lines=1)
    W.add_idea(cfg, "will be rotated away")
    E.append_log(cfg, "IDEA-001", "a finding", when="2026-04-05 Sun 17:45")
    W.add_idea(cfg, "forces rotation")
    assert [p for p in os.listdir(os.path.dirname(cfg["org_ideas_file"]))
            if p.startswith("ideas-2")]
    _rewrite_updated(cfg, None)

    assert ("IDEA-001", "from-log") in E.backfill_idea_stamps(cfg)
    assert "2026-04-05" in _stamps(cfg, "IDEA-001")[1]


def test_newest_log_stamp_handles_dateless_and_empty():
    assert E.newest_log_stamp("") is None
    assert E.newest_log_stamp("*** no stamp here\nprose") is None
    assert E.newest_log_stamp("*** [2026-04-05 Sun]") == dt.datetime(2026, 4, 5)


# ---- row payload ------------------------------------------------------------------

def test_idea_row_exposes_stamps(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "stamp me")
    row = R.idea_row(cfg, _idea(cfg))
    created, updated = _stamps(cfg)
    assert row["created"] == created and row["updated"] == updated
    for key in ("id", "state", "heading", "tags", "kind", "next"):
        assert key in row, f"pre-existing key {key} disappeared"


def test_idea_row_omits_absent_stamps(tmp_path):
    cfg = _cfg(tmp_path)
    W.add_idea(cfg, "stamp me")
    _rewrite_updated(cfg, None)
    row = R.idea_row(cfg, _idea(cfg))
    assert "updated" not in row and "created" in row
