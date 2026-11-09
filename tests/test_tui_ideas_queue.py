"""SPEC-0494: ideas board ↔ queue campaigns — matching rules and the `w` popup.

The matching half (`nightwright.tui.ideas_queue`) imports no Textual and is tested over plain
`state.json`-shaped dicts. One pilot test boots the desk on a throwaway org + campaign store and
checks `w` opens `CampaignDetail` for the selected idea and Escape returns to the board.
"""
import asyncio
import datetime as dt

from test_tui_ideas_board import _cfg, _row, _spec, _task, _write_corpus
from waits import pilot_until

from nightwright.tui import ideas_board as B
from nightwright.tui import ideas_queue as IQ

import pytest

pytestmark = pytest.mark.tui


def _st(cid, state="MERGED", *, spec=None, idea=None, project="Meta-Tools", created="",
        updated="", nested_spec=False):
    st = {"id": cid, "state": state, "project": project, "created_at": created,
          "updated_at": updated}
    if spec and nested_spec:
        st["spec"] = {"id": spec}
    elif spec:
        st["spec_id"] = spec
    if idea:
        st["idea"] = {"id": idea}
    return st


def _item(iid, *, spec="", project="Meta-Tools", specs=None):
    board = B.build_board([(_row(iid, "PROMOTED", project=project), _task(spec=spec))],
                          specs or {}, now=dt.datetime(2026, 11, 9))
    return board.by_id[iid]


def test_idea_key_normalises_padding_and_campaign_suffixes():
    assert IQ.idea_key("IDEA-1041-20261109-c0c6") == "IDEA-1041"
    assert IQ.idea_key("IDEA-0587") == IQ.idea_key("IDEA-587") == "IDEA-587"
    assert IQ.idea_key("SPEC-0488-20261109-c0c6") == ""
    assert IQ.idea_key(None) == ""


def test_match_by_idea_id_including_a_suffixed_campaign_id():
    index = IQ.CampaignIndex.from_states([
        _st("IDEA-1041-20261109-c0c6", spec="SPEC-0488", idea="IDEA-1041",
            created="2026-11-09T08:00:00-05:00"),
        # no idea block at all: the campaign id's own IDEA prefix still matches
        _st("IDEA-1041-20261101-aaaa", "ABANDONED", created="2026-11-01T08:00:00-05:00"),
        _st("IDEA-1042-20261109-bbbb", idea="IDEA-1042"),
    ])
    got = index.for_item(_item("IDEA-1041"))
    assert [r.id for r in got] == ["IDEA-1041-20261109-c0c6", "IDEA-1041-20261101-aaaa"]


def test_match_by_spec_only_is_project_scoped_and_follows_the_successor():
    specs = {"SPEC-0010": _spec("SPEC-0010", "superseded", superseded_by="SPEC-0011"),
             "SPEC-0011": _spec("SPEC-0011", "done")}
    index = IQ.CampaignIndex.from_states([
        _st("SPEC-0010-20261101-aaaa", spec="SPEC-0010", created="2026-11-01T00:00:00"),
        _st("SPEC-0011-20261105-bbbb", spec="SPEC-0011", nested_spec=True,
            created="2026-11-05T00:00:00"),
        # same bare spec id in another project's repo: not this idea's spec (SPEC-0327)
        _st("IDEA-9-20261106-cccc", spec="SPEC-0011", project="comfort-assistant",
            created="2026-11-06T00:00:00"),
    ])
    got = index.for_item(_item("IDEA-001", spec="SPEC-0010", specs=specs))
    assert [r.id for r in got] == ["SPEC-0011-20261105-bbbb", "SPEC-0010-20261101-aaaa"]
    # a project-less idea counts as the internal project, as does a campaign without one
    loose = IQ.CampaignIndex.from_states([_st("SPEC-0011-x", spec="SPEC-0011", project="")])
    assert [r.id for r in loose.for_item(_item("IDEA-002", spec="SPEC-0011", project="",
                                                specs=specs))] == ["SPEC-0011-x"]


def test_none_and_several_dedupe_and_order_newest_first():
    index = IQ.CampaignIndex.from_states([
        _st("IDEA-5-20261102-aaaa", "ABANDONED", spec="SPEC-0005", idea="IDEA-005",
            created="2026-11-02T00:00:00", updated="2026-11-03T09:00:00"),
        _st("IDEA-5-20261104-bbbb", "QUEUED", spec="SPEC-0005", idea="IDEA-5",
            created="2026-11-04T00:00:00", updated="2026-11-04T01:00:00"),
    ])
    specs = {"SPEC-0005": _spec("SPEC-0005", "accepted")}
    got = index.for_item(_item("IDEA-005", spec="SPEC-0005", specs=specs))
    assert [r.id for r in got] == ["IDEA-5-20261104-bbbb", "IDEA-5-20261102-aaaa"]  # once each
    assert got[0].label == "IDEA-5-20261104-bbbb  queued  2026-11-04"        # open day
    assert got[1].label == "IDEA-5-20261102-aaaa  abandoned  2026-11-03"     # close day
    assert index.for_item(_item("IDEA-777")) == []
    assert IQ.CampaignIndex().for_item(_item("IDEA-005")) == []


def test_load_board_attaches_terminal_campaigns(tmp_path):
    from nightwright.agentic import store

    cfg = _cfg(tmp_path)
    _write_corpus(cfg)
    store.create_campaign(cfg, _st("SPEC-0002-20261105-aaaa", spec="SPEC-0002",
                                   created="2026-11-05T10:00:00-05:00"))
    board = B.load_board(cfg)
    assert [r.id for r in board.campaigns_for("IDEA-002")] == ["SPEC-0002-20261105-aaaa"]
    assert board.campaigns_for("IDEA-003") == []
    other = tmp_path / "no-store"
    other.mkdir()
    bare = _cfg(other)
    _write_corpus(bare)
    assert B.load_board(bare).campaigns_for("IDEA-002") == []     # no store: empty, no error


def test_w_opens_campaign_detail_and_escape_returns_to_the_same_idea(tmp_path):
    from test_tui_desk import _grid

    from nightwright.agentic import store
    from nightwright.tui.app import ChoiceScreen, IdeaDesk
    from nightwright.tui.campaign_detail import CampaignDetail

    cfg = _cfg(tmp_path)
    _write_corpus(cfg)
    # IDEA-002 (BUILT via SPEC-0002) went through the queue once; IDEA-001 twice.
    store.create_campaign(cfg, _st("SPEC-0002-20261105-aaaa", spec="SPEC-0002",
                                   created="2026-11-05T10:00:00-05:00"))
    for cid, day in (("IDEA-1-20261101-bbbb", "01"), ("IDEA-1-20261103-cccc", "03")):
        store.create_campaign(cfg, _st(cid, spec="SPEC-0001", idea="IDEA-001",
                                       created=f"2026-11-{day}T10:00:00-05:00"))

    async def drive():
        app = IdeaDesk(cfg)
        async with app.run_test(size=(180, 40)) as pilot:
            assert await pilot_until(pilot, lambda: app._pairs)
            app.jump_to("IDEA-002")
            assert app.selected_id == "IDEA-002"
            # The detail pane repaints after the jump; wait for it rather than reading once.
            assert await pilot_until(pilot, lambda: "Queue" in (s := "\n".join(_grid(app)))
                                     and "SPEC-0002-20261105-aaaa" in s)

            await pilot.press("w")
            assert await pilot_until(pilot, lambda: isinstance(app.screen, CampaignDetail))
            assert app.screen.campaign_id == "SPEC-0002-20261105-aaaa"
            await pilot.press("escape")
            assert await pilot_until(pilot, lambda: not isinstance(app.screen, CampaignDetail))
            assert app.selected_id == "IDEA-002" and app.project == "Meta-Tools"
            assert app.query_one("#board").has_focus

            # several campaigns: a newest-first chooser, then the picked one opens
            app.jump_to("IDEA-001")
            await pilot.press("w")
            assert await pilot_until(pilot, lambda: isinstance(app.screen, ChoiceScreen))
            assert app.screen.choices[0].startswith("IDEA-1-20261103-cccc")
            await pilot.press("down", "enter")
            assert await pilot_until(pilot, lambda: isinstance(app.screen, CampaignDetail))
            assert app.screen.campaign_id == "IDEA-1-20261101-bbbb"
            await pilot.press("escape")
            assert await pilot_until(pilot, lambda: not isinstance(app.screen, CampaignDetail))
            assert app.selected_id == "IDEA-001"

            # none: nothing opens
            app.jump_to("IDEA-003")
            assert "not built through the queue" in "\n".join(_grid(app))
            await pilot.press("w")
            await pilot.pause()
            assert len(app.screen_stack) == 1 and app.selected_id == "IDEA-003"

    asyncio.run(drive())
