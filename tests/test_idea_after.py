"""SPEC-0562: `nw idea after` / `org_write.set_idea_after` — add/remove/set/clear an idea's
`:AFTER:` prerequisites, refusing malformed ids, self-references and cycles, and round-tripping
through the SPEC-0423 queue reader. Uses the SPEC-0176 end-to-end harness (temp repo + temp org)."""
import re
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nightwright import org as ORG
from nightwright import org_write as OW
from nightwright.agentic import queue as Q
from nightwright.cli import cli
from nightwright.specs import seed_ideas_from_epic  # noqa: F401  (documents the other writer)

from test_agentic_e2e import make_cfg, make_repo, script

pytestmark = pytest.mark.fast


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cfg = make_cfg(tmp_path, repo, script(tmp_path, []))
    monkeypatch.setattr("nightwright.cli.load_config", lambda: cfg)
    a = OW.add_idea(cfg, "idea a")[2]
    b = OW.add_idea(cfg, "idea b")[2]
    c = OW.add_idea(cfg, "idea c")[2]
    return cfg, a, b, c


def run(*args):
    return CliRunner().invoke(cli, ["idea", "after", *args])


def body(cfg):
    return Path(cfg["org_ideas_file"]).read_text()


def prop(cfg, iid, name="AFTER"):
    ORG.invalidate_tasks_cache()
    return [t for t in ORG.load_tasks(cfg) if t.properties.get("ID") == iid][0].properties.get(name)


def test_add_creates_property_bumps_updated_and_is_idempotent(env):
    cfg, a, b, _ = env
    OW.set_property(cfg, OW.resolve_selector(cfg, a), "UPDATED", "[2020-01-01 Wed 00:00]")
    r = run(a, "--add", b)
    assert r.exit_code == 0, r.output
    assert prop(cfg, a) == b
    assert f"after ← {a}" in r.output and "nw queue after" in r.output
    assert prop(cfg, a, "UPDATED") != "[2020-01-01 Wed 00:00]"
    snap = body(cfg)
    assert run(a, "--add", b).exit_code == 0
    assert body(cfg) == snap


def test_repeat_add_orders_dedupes_and_keeps_seeded(env):
    cfg, a, b, c = env
    OW.set_property(cfg, OW.resolve_selector(cfg, a), "AFTER", f"{b} SPEC-0001")
    r = run(a, "--add", c, "--add", b.lower(), "--add", "idea-9999")
    assert r.exit_code == 0, r.output
    assert prop(cfg, a) == f"{b} SPEC-0001 {c} IDEA-9999"


def test_remove_missing_ok_and_last_deletes_property(env):
    cfg, a, b, c = env
    assert run(a, "--add", b, "--add", c).exit_code == 0
    assert run(a, "--remove", c, "--remove", "IDEA-9999").exit_code == 0
    assert prop(cfg, a) == b
    assert run(a, "--remove", b).exit_code == 0
    assert ":AFTER:" not in body(cfg)
    assert run(a, "--remove", b).exit_code == 0


def test_set_and_clear(env):
    cfg, a, b, c = env
    assert run(a, "--set", f"{b}, {c}").exit_code == 0
    assert prop(cfg, a) == f"{b} {c}"
    assert run(a, "--set", b).exit_code == 0
    assert prop(cfg, a) == b
    assert run(a, "--set", "").exit_code == 0
    assert ":AFTER:" not in body(cfg)
    run(a, "--add", b)
    assert run(a, "--clear").exit_code == 0
    assert ":AFTER:" not in body(cfg)


def test_outside_prerequisites_accepted(env):
    cfg, a, b, _ = env
    r = run(a, "--add", "SPEC-0003", "--add", b, "--add", "PROJ-0007")
    assert r.exit_code == 0, r.output
    assert prop(cfg, a) == f"SPEC-0003 {b} PROJ-0007"


@pytest.mark.parametrize("bad", ["foo", "IDEA", "1051", "IDEA-x", "IDEA-1 IDEA-2"])
def test_malformed_ref_refused(env, bad):
    cfg, a, *_ = env
    snap = body(cfg)
    r = run(a, "--add", bad)
    assert r.exit_code == 1 and "invalid id" in r.output
    assert body(cfg) == snap
    r = run(a, "--set", "foo")
    assert r.exit_code == 1 and "foo" in r.output
    assert body(cfg) == snap


def test_self_reference_refused(env):
    cfg, a, *_ = env
    snap = body(cfg)
    r = run(a, "--add", a)
    assert r.exit_code == 1 and a in r.output
    assert body(cfg) == snap


def test_direct_and_transitive_cycles_refused(env):
    cfg, a, b, c = env
    assert run(b, "--add", a).exit_code == 0
    snap = body(cfg)
    r = run(a, "--add", b)
    assert r.exit_code == 1 and f"{b} -> {a}" in r.output
    assert run(c, "--add", b).exit_code == 0
    snap = body(cfg)
    r = run(a, "--add", c)
    assert r.exit_code == 1 and f"{c} -> {b} -> {a}" in r.output
    assert run(a, "--set", c).exit_code == 1
    assert body(cfg) == snap


def test_unknown_id_warns_and_writes(env):
    cfg, a, *_ = env
    r = run(a, "--add", "IDEA-9999")
    assert r.exit_code == 0
    assert "unknown prerequisite" in r.output and "IDEA-9999" in r.output
    assert prop(cfg, a) == "IDEA-9999"


def test_usage_errors_do_not_write(env):
    cfg, a, b, _ = env
    snap = body(cfg)
    for args in ([a], [a, "--set", b, "--add", b], [a, "--clear", "--remove", b],
                 [a, "--set", b, "--clear"]):
        assert run(*args).exit_code == 2, args
    assert body(cfg) == snap


def test_non_idea_selector_refused(env):
    cfg, a, b, _ = env
    OW.add_task(cfg, "a todo task")
    snap = body(cfg)
    r = run("a todo task", "--add", b)
    assert r.exit_code == 1
    assert body(cfg) == snap


def test_round_trip_through_queue_reader_and_plan(env):
    cfg, a, b, _ = env
    assert run(a, "--add", b, "--add", "SPEC-0003").exit_code == 0
    ORG.invalidate_tasks_cache()
    assert Q._idea_after_refs(Q._idea_task(cfg, a)) == [b, "SPEC-0003"]
    _, refs = OW.set_idea_after(cfg, a, remove=["SPEC-0003"], add=["IDEA-9999"])
    ORG.invalidate_tasks_cache()
    assert Q._idea_after_refs(Q._idea_task(cfg, a)) == refs
    OW.set_idea_after(cfg, a, remove=["IDEA-9999"])
    ORG.invalidate_tasks_cache()
    plan = Q.plan_add_idea(cfg, a)
    assert b in [e["ref"] for e in plan["after"]]


def test_one_tokenizer(env):
    assert OW.parse_after_refs("IDEA-1, SPEC-0002  PROJ-7") == ["IDEA-1", "SPEC-0002", "PROJ-7"]
    assert not re.search(r'findall\(r"\[A-Za-z\]', Path(Q.__file__).read_text().split(
        "def _idea_after_refs")[1].split("def _note_after")[0])


def test_completion_lists_idea_ids(env):
    cfg, a, b, _ = env
    ctx = click.Context(cli, obj=cfg)
    for name in ("selector", "add", "remove"):
        param = next(p for p in cli.commands["idea"].commands["after"].params if p.name == name)
        assert {i.value for i in param.shell_complete(ctx, "IDEA-")} >= {a, b}


def test_unpadded_ids_are_normalised(env):
    cfg, a, b, c = env                      # IDEA-001, IDEA-002, IDEA-003
    assert run(a, "--add", "idea-2").exit_code == 0
    assert prop(cfg, a) == b
    r = run(a, "--add", "IDEA-1")
    assert r.exit_code == 1 and "itself" in r.output          # self-reference, unpadded
    assert run(b, "--add", "IDEA-3").exit_code == 0
    snap = body(cfg)
    r = run(c, "--add", "IDEA-1")                              # c -> a -> b -> c
    assert r.exit_code == 1 and "cycle" in r.output
    assert body(cfg) == snap


def test_hand_edited_stored_refs_are_matched(env):
    cfg, a, b, c = env
    OW.set_property(cfg, OW.resolve_selector(cfg, a), "AFTER", "idea-2, IDEA-3 SPEC-0003")
    assert run(a, "--add", "IDEA-2").exit_code == 0            # no duplicate appended
    assert prop(cfg, a) == f"{b} {c} SPEC-0003"                # and the value is normalised
    OW.set_property(cfg, OW.resolve_selector(cfg, a), "AFTER", "idea-2 IDEA-3")
    assert run(a, "--remove", "IDEA-2").exit_code == 0
    assert prop(cfg, a) == c
    # a stored unpadded edge cannot hide a cycle
    OW.set_property(cfg, OW.resolve_selector(cfg, b), "AFTER", "idea-1")
    r = run(a, "--add", "IDEA-2")
    assert r.exit_code == 1 and "cycle" in r.output


def test_stored_junk_value_is_cleared(env):
    cfg, a, *_ = env
    for junk in ("TBD", "1051"):
        OW.set_property(cfg, OW.resolve_selector(cfg, a), "AFTER", junk)
        assert run(a, "--clear").exit_code == 0
        assert ":AFTER:" not in body(cfg)
    OW.set_property(cfg, OW.resolve_selector(cfg, a), "AFTER", "TBD")
    assert run(a, "--set", "").exit_code == 0
    assert ":AFTER:" not in body(cfg)
