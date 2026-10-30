"""SPEC-0211: spec phases and review rounds in `wt queue watch` and `wt queue list`."""
import shutil

import pytest
from rich.console import Console

from test_agentic_dashboard import _campaign, _ev
from test_agentic_e2e import make_cfg, make_repo, run_cli, script
from wt.agentic import cli_queue as CQ
from wt.agentic import queue as Q
from wt.agentic import store as S

HEADING = "H" * 100


@pytest.fixture
def env(tmp_path):
    repo = make_repo(tmp_path)
    return repo, make_cfg(tmp_path, repo, script(tmp_path, []))


def _idea(cfg, repo, cid, state, *, spec=True, heading="Show spec phases", **extra):
    spec_block = ({"id": "SPEC-0001", "rel_path": "docs/specs/0001-trivial.md", "internal": True}
                  if spec else {"rel_path": None})
    return _campaign(cfg, repo, cid, state, kind="idea", spec_id="SPEC-0001" if spec else None,
                     idea={"id": "IDEA-078", "heading": heading}, spec=spec_block, **extra)


def _review(cfg, cid, rnd, verdict):
    _ev(cfg, cid, "spec_review", {"verdict": verdict, "round": rnd, "spec_sha256": "ab"})


def _item(cfg, cid, section="active"):
    return next(it for it in Q.dashboard_payload(cfg)[section] if it["campaign"] == cid)


def _text(renderable, width):
    console = Console(width=width, record=True, force_terminal=False, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_spec_review_after_revise(env):
    repo, cfg = env
    cid = _idea(cfg, repo, "IDEA-078-r2", "SPEC_REVIEW")
    _review(cfg, cid, 1, "revise")
    it = _item(cfg, cid)
    assert it["spec_round"] == 2
    assert it["last_spec_review"] == {"round": 1, "verdict": "revise"}
    assert str(CQ._spec_review_text(it)) == "r2 · reviewing · last: revise"
    assert Q.queue_payload(cfg)["items"][0]["spec_round"] == 2


def test_spec_writing_rounds_and_exhausted_park(env):
    repo, cfg = env
    a = _idea(cfg, repo, "IDEA-078-a", "SPEC_WRITING", spec=False, heading=HEADING)
    b = _idea(cfg, repo, "IDEA-078-b", "SPEC_WRITING")
    _review(cfg, b, 1, "revise")
    c = _idea(cfg, repo, "IDEA-078-c", "PARKED",
              park={"reason": "spec_review_exhausted", "resume_to": "SPEC_WRITING"})
    _review(cfg, c, 1, "revise")
    ia = _item(cfg, a)
    assert ia["spec_round"] is None and str(CQ._spec_review_text(ia)) == "writing"
    ib = _item(cfg, b)
    assert ib["spec_round"] == 1
    assert str(CQ._spec_review_text(ib)) == "r1 · revising · last: revise"
    assert _item(cfg, c, "attention")["spec_round"] == 1


def test_first_review(env):
    repo, cfg = env
    cid = _idea(cfg, repo, "IDEA-078-first", "SPEC_REVIEW")
    it = _item(cfg, cid)
    assert it["spec_round"] == 1 and it["last_spec_review"] is None
    assert str(CQ._spec_review_text(it)) == "r1 · reviewing"


def test_other_states_and_spec_campaign(env):
    repo, cfg = env
    impl = _idea(cfg, repo, "IDEA-078-impl", "IMPLEMENTING")
    _review(cfg, impl, 1, "revise")
    _review(cfg, impl, 2, "approve")
    parked = _idea(cfg, repo, "IDEA-078-park", "PARKED",
                   park={"reason": "rate_limited", "resume_to": "IMPLEMENTING"})
    _review(cfg, parked, 1, "revise")
    _review(cfg, parked, 2, "approve")
    waiting = _idea(cfg, repo, "IDEA-078-wait", "WAITING", waiting={"resume_to": "SPEC_REVIEW"})
    _review(cfg, waiting, 1, "revise")
    it = _item(cfg, impl)
    assert it["spec_round"] == 2 and str(CQ._spec_review_text(it)) == "r2 · last: approve"
    assert _item(cfg, parked, "attention")["spec_round"] == 2
    assert _item(cfg, waiting, "waiting")["spec_round"] == 2
    for state in ("QUEUED", "IMPLEMENTING", "GATING", "PARKED", "MERGED"):
        sid = _campaign(cfg, repo, f"SPEC-0001-{state}", state,
                        **({"park": {"reason": "x", "resume_to": "IMPLEMENTING"}}
                           if state == "PARKED" else {}))
        row = next(i for i in Q.queue_payload(cfg, include_terminal=True)["items"]
                   if i["campaign"] == sid)
        assert row["spec_round"] is None and "idea" not in row
    spec = _item(cfg, "SPEC-0001-IMPLEMENTING")
    assert CQ._spec_review_text(spec) is None


def test_title_sources(env):
    repo, cfg = env
    wt = repo.parent / "wt-title"
    (wt / "docs/specs").mkdir(parents=True)
    cid = _idea(cfg, repo, "IDEA-078-title", "IMPLEMENTING")
    st = S.read_state(cfg, cid)
    st["repo"]["worktree"] = str(wt)
    assert Q._spec_title(st) == "Trivial feature"        # worktree has no file: repo.root
    (wt / "docs/specs/0001-trivial.md").write_text(
        (repo / "docs/specs/0001-trivial.md").read_text().replace("Trivial feature", "In worktree"))
    assert Q._spec_title(st) == "In worktree"            # the worktree first
    shutil.rmtree(wt)
    assert Q._spec_title(st) == "Trivial feature"        # merged: the worktree is gone
    st["spec"]["rel_path"] = "docs/specs/nope.md"
    assert Q._spec_title(st) is None
    assert str(CQ._idea_spec_cell({"spec_id": "SPEC-0001", "spec_title": None})) == "SPEC-0001"


def test_spec_cell_cut(env):
    repo, cfg = env
    cid = _idea(cfg, repo, "IDEA-078-long", "SPEC_WRITING", spec=False, heading=HEADING)
    cell = str(CQ._idea_spec_cell(_item(cfg, cid)))
    assert len(cell) == 60 and cell.endswith("…") and cell.startswith("writing… HHH")
    long_spec = str(CQ._idea_spec_cell({"spec_id": "SPEC-0001", "spec_title": HEADING}))
    assert len(long_spec) == 60 and long_spec.endswith("…")


@pytest.mark.parametrize("width", [80, 160])
def test_rendering(env, monkeypatch, width):
    repo, cfg = env
    cid = _idea(cfg, repo, "IDEA-078-render", "SPEC_REVIEW")
    _review(cfg, cid, 1, "revise")
    out = _text(CQ._active_panel(Q.dashboard_payload(cfg)["active"][0]), width)
    assert "from IDEA-078" in out and "spec review" in out and "r2 · reviewing" in out
    assert "spec_writing" in out and "spec_review" in out
    assert "SPEC_REVIEW r2" in _text(CQ._queue_table(Q.queue_payload(cfg), width), width)
    r = run_cli(cfg, monkeypatch, ["queue", "list"])
    assert r.exit_code == 0 and "SPEC_REVIEW r2" in r.output.replace("\n", " ")


def test_list_state_cells(env):
    repo, cfg = env
    _idea(cfg, repo, "IDEA-078-w1", "SPEC_WRITING", spec=False)
    w = _idea(cfg, repo, "IDEA-078-w2", "SPEC_WRITING")
    _review(cfg, w, 1, "revise")
    out = _text(CQ._queue_table(Q.queue_payload(cfg), 160), 160)
    assert "SPEC_WRITING r1" in out
    assert out.count("SPEC_WRITING") == 2


def test_spec_campaign_phases_unchanged(env):
    repo, cfg = env
    _campaign(cfg, repo, "SPEC-0001-impl", "IMPLEMENTING")
    it = _item(cfg, "SPEC-0001-impl")
    assert it["phases"] == ["OPENING", "IMPLEMENTING", "GATING", "TRIAGING", "REVIEWING", "CLOSING"]
    assert it["phase_index"] == 1
    assert "spec_review" not in str(CQ._stepper("IMPLEMENTING", 1, it["phases"]))
