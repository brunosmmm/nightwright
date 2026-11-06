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


def test_non_object_state_is_not_a_campaign(cfg):
    mk(cfg, A)
    mk(cfg, B)
    path = S.campaign_dir(cfg, B).joinpath("state.json")
    for raw in ("[1, 2]", '"nope"', "null", "1"):
        path.write_text(raw)
        assert S.campaign_state(cfg, B) is None
        assert S.sole_campaign_named(cfg, B) is None
        assert S.sole_campaign_named(cfg, f"{A} vs {B}") == A
    assert S.campaign_state(cfg, 123) is None
    assert S.campaign_state(cfg, ["x"]) is None


def test_empty_and_missing_dir(cfg):
    assert S.sole_campaign_named(cfg, None) is None
    assert S.sole_campaign_named(cfg, "") is None
    assert S.sole_campaign_named(cfg, f"{A}") is None                # no campaigns dir at all


def test_redmain_ids_minted_for_dotted_and_unicode_projects(cfg):
    """IDs `new_campaign_id` actually mints for red-main (`FIX|REVERT-` + `events._safe`)."""
    import datetime as dt

    from nightwright.agentic import events as E

    day = dt.datetime(2026, 11, 5)
    assert "." in E._safe("acme.io")
    assert any(not c.isascii() for c in E._safe("München"))
    minted = []
    for prefix, project in (("FIX", "acme.io"), ("REVERT", "München"), ("FIX", "acme.München")):
        cid = S.new_campaign_id(cfg, f"{prefix}-{E._safe(project)}", now=day)
        assert E._safe(project) in cid
        mk(cfg, cid)
        minted.append(cid)
        assert S.sole_campaign_named(cfg, f"waiting on {cid} today") == cid
        assert S.sole_campaign_named(cfg, f"see {cid}.") == cid          # sentence period
        assert S.sole_campaign_named(cfg, f"x-{cid}y") is None
        assert S.sole_campaign_named(cfg, f"{cid}-x") is None
    assert S.sole_campaign_named(cfg, f"{minted[0]} and {minted[1]}") is None
    ghost = minted[0].replace(E._safe("acme.io"), "no.such", 1)
    assert ghost != minted[0]
    assert S.sole_campaign_named(cfg, f"{ghost} vs {minted[2]}") == minted[2]


def test_digit_leading_outbound_id_from_project_3m(cfg):
    """`_proj_code('3M')` is `3M`; `new_campaign_id` then mints `3M-0001-<date>-<hex>`."""
    import datetime as dt

    from nightwright.specs import _proj_code

    code = _proj_code("3M")
    assert code == "3M" and code[0].isdigit()
    day = dt.datetime(2026, 11, 6)
    cid = S.new_campaign_id(cfg, f"{code}-0001", now=day)
    other = S.new_campaign_id(cfg, f"{code}-0002", now=day)
    assert cid.startswith("3M-0001-") and other.startswith("3M-0002-")
    mk(cfg, cid)
    mk(cfg, other)
    assert S.sole_campaign_named(cfg, f"waiting on {cid} today") == cid
    assert S.sole_campaign_named(cfg, f"see {cid}.") == cid
    assert S.sole_campaign_named(cfg, f"({cid}), then {cid}") == cid
    assert S.sole_campaign_named(cfg, f"x-{cid}y") is None
    assert S.sole_campaign_named(cfg, f"{cid}-x") is None
    assert S.sole_campaign_named(cfg, f"9{cid}") is None
    assert S.sole_campaign_named(cfg, f"{cid} and {other}") is None
