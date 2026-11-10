"""SPEC-0506: files a remote gate declares in `writes` (the regenerated ledger) come back to the
coordinator's worktree and the run stands; anything else keeps the local re-run (`tree`).
Offline, on SPEC-0338's loopback worker (`remote_support.Remote`) or a scripted `gate-job`."""
import base64
import gzip
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from nightwright.agentic import gates as G
from nightwright.agentic import project as P
from nightwright.agentic import remote_gates as RG
from nightwright.agentic import worker as W

from remote_support import Remote, World, _gate
from remote_support import _short_runtime_dir  # noqa: F401  (autouse fixture)
from test_remote_gates import _done, _fake_gate_job, _frame_line, _gate_result

pytestmark = pytest.mark.remote

ASYNC_INFO = {"protocol": "nw.job.v1", "features": ["async"], "git": "git version 2"}
REGEN = "printf 'regen\\n' > a.txt"


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


@pytest.fixture
def remote(world):
    r = Remote(world)
    r.register()
    return r


def _fell_back_on_tree(world, run):
    assert run.worker is None and run.ok
    (ev,) = world.ledger(["worker_failed"])
    assert ev["stage"] == "tree" and ev["cooldown"] is False
    assert run.placement["reason"] == "tree" and run.placement["where"] == "local"
    assert (world.repo / "a.txt").read_text() == "regen\n"      # the local re-run wrote it


def _notes(world):
    return [e["text"] for e in world.ledger(["note"]) if e.get("author") == "gates"]


def test_declared_change_is_applied_and_the_run_stands(world, remote):
    world.project["gates"] = [_gate("regen", REGEN, writes=["a.txt"])]
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box" and run.ok and run.changed == ("a.txt",)
    assert str(world.repo) not in run.results[0].cwd            # no local gate run started
    assert (world.repo / "a.txt").read_text() == "regen\n"
    assert not world.ledger(["worker_failed"])
    assert _notes(world) == ["applied 1 file(s) regenerated on box: a.txt"]
    assert G.as_payload(run)["placement"] == {"where": "remote", "worker": "box"}
    assert not list(world.repo.glob(".*.nw-writes.tmp"))


def test_an_undeclared_change_falls_back_with_reason_tree(world, remote):
    world.project["gates"] = [_gate("regen", f"{REGEN}; printf 'x\\n' > gen.txt",
                                    writes=["a.txt"])]
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _fell_back_on_tree(world, run)
    assert "undeclared write: gen.txt" in world.ledger(["worker_failed"])[0]["error"]
    assert not _notes(world)


def test_no_writes_declared_keeps_the_local_rerun(world, remote):
    world.project["gates"] = [_gate("regen", REGEN)]
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _fell_back_on_tree(world, run)


def test_a_moved_tree_falls_back_and_nothing_is_written(world, remote, monkeypatch):
    world.project["gates"] = [_gate("regen", REGEN, writes=["a.txt"])]
    seen = []

    def moved(worktree):
        seen.append(worktree)
        return "head", "not-the-tree-under-test"
    monkeypatch.setattr(RG, "tree_under_test", moved)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert seen
    _fell_back_on_tree(world, run)
    assert "worktree changed" in world.ledger(["worker_failed"])[0]["error"]


def test_oversize_content_falls_back(world, remote, monkeypatch):
    world.project["gates"] = [_gate("regen", REGEN, writes=["a.txt"])]
    monkeypatch.setattr(RG, "WRITE_FILE_MAX", 3)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    _fell_back_on_tree(world, run)
    assert "caps" in world.ledger(["worker_failed"])[0]["error"]


def _z(raw: bytes) -> str:
    return base64.b64encode(gzip.compress(raw)).decode()


def _scripted(world, remote, written):
    world.project["gates"] = [_gate("ok", "echo ok", writes=["a.txt"])]
    lines = [_frame_line("started", slot_wait_s=0.0),
             _frame_line("gate", files={}, result=_gate_result("ok")),
             _done(changed=["a.txt"], **({"written": written} if written is not None else {}))]
    _fake_gate_job(remote, "writes", lines)
    return G.run_all(world.project, world.state, world.cfg, seq=1)


def test_a_bad_hash_falls_back(world, remote):
    raw = b"regen\n"
    run = _scripted(world, remote, {"a.txt": {"z": _z(raw), "sha256": "0" * 64}})
    assert run.worker is None and run.placement["reason"] == "tree"
    assert "sha256 mismatch" in world.ledger(["worker_failed"])[0]["error"]
    assert (world.repo / "a.txt").read_text() == "a\n"          # the local gate (`echo ok`) wrote nothing


def test_an_old_worker_without_written_falls_back(world, remote):
    run = _scripted(world, remote, None)
    assert run.worker is None and run.placement["reason"] == "tree"
    assert "no content" in world.ledger(["worker_failed"])[0]["error"]


def test_a_good_scripted_frame_is_applied(world, remote):
    raw = b"from the worker\n"
    run = _scripted(world, remote, {"a.txt": {"z": _z(raw),
                                              "sha256": hashlib.sha256(raw).hexdigest()}})
    assert run.worker == "box" and run.ok
    assert (world.repo / "a.txt").read_bytes() == raw


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", ".git/config"])
def test_unsafe_paths_never_apply(world, remote, path):
    world.project["gates"] = [_gate("ok", "echo ok", writes=["**"])]
    raw = b"x\n"
    lines = [_frame_line("started", slot_wait_s=0.0),
             _frame_line("gate", files={}, result=_gate_result("ok")),
             _done(changed=[path], written={path: {"z": _z(raw),
                                                   "sha256": hashlib.sha256(raw).hexdigest()}})]
    _fake_gate_job(remote, "unsafe", lines)
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker is None
    assert not (world.repo.parent / "escape.txt").exists()


def test_an_async_worker_applies_the_files_too(world, remote):
    remote.register(info=ASYNC_INFO)
    world.project["gates"] = [_gate("regen", REGEN, writes=["a.txt"])]
    run = G.run_all(world.project, world.state, world.cfg, seq=1)
    assert run.worker == "box" and run.ok and run.job_id
    assert (world.repo / "a.txt").read_text() == "regen\n"
    assert _notes(world) == ["applied 1 file(s) regenerated on box: a.txt"]


def test_the_header_announces_writes_ok_only_with_writes(world):
    gates = [_gate("a", "true")]
    world.project["gates"] = gates
    hdr = lambda: RG._header(world.project, world.state, world.cfg, 1, "j", "k", "0" * 40,  # noqa: E731
                             "0" * 40, [], 0)
    assert "writes_ok" not in hdr()["payload"]
    gates.append(_gate("b", "true", writes=["x"]))
    assert hdr()["payload"]["writes_ok"] is True


def test_worker_written_files_respects_globs_and_caps(tmp_path):
    (tmp_path / "a.txt").write_text("hello\n")
    (tmp_path / "b.txt").write_text("other\n")
    payload = {"writes_ok": True, "gates": [{"writes": ["a.txt"]}]}
    out = W.written_files(tmp_path, ["a.txt", "b.txt"], payload)
    assert list(out["written"]) == ["a.txt"]
    assert out["written"]["a.txt"]["sha256"] == hashlib.sha256(b"hello\n").hexdigest()
    assert W.written_files(tmp_path, ["a.txt"], {"gates": payload["gates"]}) == {}   # no writes_ok
    assert W.written_files(tmp_path, None, payload) == {}
    (tmp_path / "a.txt").write_bytes(b"x" * (W.WRITTEN_FILE_MAX + 1))
    assert W.written_files(tmp_path, ["a.txt"], payload) == {}


@pytest.mark.parametrize("bad", ["a.txt", [], [1]])
def test_config_rejects_bad_writes(bad):
    doc = {"schema": P.SCHEMA, "gates": [{"name": "g", "cmd": "true", "writes": bad}]}
    with pytest.raises(ValueError, match=r"gates\[0\]\.writes"):
        P.validate(doc)
    doc["gates"][0]["writes"] = ["docs/LEDGER.md"]
    P.validate(doc)


def test_nightwrights_spec_lint_gate_declares_the_ledger():
    cfg = yaml.safe_load((Path(__file__).parent.parent / ".nw" / "project.yaml").read_text())
    (gate,) = [g for g in cfg["gates"] if g["name"] == "spec-lint"]
    assert gate["writes"] == ["docs/LEDGER.md"]
