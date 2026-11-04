"""SPEC-0431: `store.sole_campaign_named` — the one existing campaign a text names, or None."""
import pytest

from nightwright.agentic import store as S

A = "SPEC-0001-20261101-abcd"
B = "SPEC-0002-20261102-0123"
GHOST = "SPEC-0009-20261103-ffff"


@pytest.fixture
def cfg(tmp_path):
    return {"data_dir": str(tmp_path / "data"), "config_dir": str(tmp_path / "config")}


def mk(cfg, cid):
    S.create_campaign(cfg, {"id": cid, "spec_id": cid[:9], "project": "P", "kind": "spec",
                            "state": "IMPLEMENTING", "counters": {}})


def test_one_match(cfg):
    mk(cfg, A)
    assert S.sole_campaign_named(cfg, f"waiting on {A} to finish") == A
    assert S.sole_campaign_named(cfg, f"({A}), then {A}.") == A      # repeated is still one


def test_two_none_unknown(cfg):
    mk(cfg, A)
    mk(cfg, B)
    assert S.sole_campaign_named(cfg, f"{A} and {B}") is None
    assert S.sole_campaign_named(cfg, "nothing here, SPEC-0001") is None
    assert S.sole_campaign_named(cfg, f"only {GHOST}") is None


def test_one_existing_one_unknown_stamps_the_existing(cfg):
    mk(cfg, A)
    assert S.sole_campaign_named(cfg, f"{GHOST} vs {A}") == A


def test_glued_to_other_characters(cfg):
    mk(cfg, A)
    assert S.sole_campaign_named(cfg, f"x-{A}y") is None
    assert S.sole_campaign_named(cfg, f"{A}y") is None
    assert S.sole_campaign_named(cfg, f"{A}-x") is None


def test_unreadable_state_drops_the_candidate(cfg):
    mk(cfg, A)
    mk(cfg, B)
    S.campaign_dir(cfg, B).joinpath("state.json").write_text("{not json")
    assert S.sole_campaign_named(cfg, f"{A} {B}") == A
    assert S.sole_campaign_named(cfg, B) is None


def test_empty_and_missing_dir(cfg):
    assert S.sole_campaign_named(cfg, None) is None
    assert S.sole_campaign_named(cfg, "") is None
    assert S.sole_campaign_named(cfg, f"{A}") is None                # no campaigns dir at all
