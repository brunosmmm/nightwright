"""SPEC-0346: the masked snapshot of a whole fake campaign, and its recorder. Depends only on
the campaign store and `test_agentic_e2e`'s helpers, so it also runs on the code before the
SPEC-0310 epic (the commit before SPEC-0337), where `no_workers_golden.json` is recorded:

    git archive 1f0311c4^ | tar -x -C /tmp/pre          # a plain export, not a worktree
    cp tests/remote_workers_golden.py /tmp/pre/tests/
    PYTHONPATH=/tmp/pre/src:/tmp/pre/tests python /tmp/pre/tests/remote_workers_golden.py OUT.json
"""
import json
import re
import sys
import tempfile
from pathlib import Path

PRE_EPIC = "1f0311c4^"

_MASKS = (
    (re.compile(r"\b[0-9a-f]{40}\b"), "<SHA>"),
    (re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:\d\d|Z)?"), "<TS>"),
    (re.compile(r"SPEC-0001-\d{8}-[0-9a-f]{4}"), "<CID>"),
    # a gate sidecar's text is a string inside the snapshot: its timings are masked too
    (re.compile(r'("(?:duration_s|slot_wait_s|elapsed_s)": )[0-9.]+'), r"\1<X>"),
)
_MASKED_KEYS = frozenset({"ts", "duration_s", "nw_sha", "slot_wait_s", "started_at", "at",
                          "finished_at", "elapsed_s", "uid"})


def _walk(obj, tmp: str):
    if isinstance(obj, dict):
        return {k: ("<X>" if k in _MASKED_KEYS and v is not None else _walk(v, tmp))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(v, tmp) for v in obj]
    if isinstance(obj, str):
        obj = obj.replace(tmp, "<TMP>")
        for rx, sub in _MASKS:
            obj = rx.sub(sub, obj)
    return obj


# What other specs changed in the ledger since the golden was recorded (the code before
# SPEC-0337): keys they added or whose value follows the prompt text or the git history, and
# the `review_outcome` event (SPEC-0402). `comparable` drops them from both sides, so a diff in
# anything else (an event type or payload key the worker epic touched) fails the comparison.
DRIFT_KEYS = frozenset({"nw_sha", "profile_sha256", "prompt_sha256", "head", "tree", "seq"})
DRIFT_TYPES = frozenset({"review_outcome"})


def comparable(obj):
    """`obj` (a snapshot, an event or a list of them) without the drift keys and types."""
    if isinstance(obj, dict):
        return {k: comparable(v) for k, v in obj.items() if k not in DRIFT_KEYS}
    if isinstance(obj, list):
        return [comparable(v) for v in obj
                if not (isinstance(v, dict) and v.get("type") in DRIFT_TYPES)]
    return obj


def snapshot(cfg: dict, cid: str, tmp) -> dict:
    """The campaign's ledger events (types and payloads), its final state's close block and
    its gate artifacts, with timestamps, durations, ids, `nw_sha` and temp paths masked."""
    from nightwright.agentic import store as S
    tmp = str(tmp)
    st = S.read_state(cfg, cid)
    files = {}
    gdir = S.gates_dir(cfg, cid)
    if gdir.is_dir():
        for p in sorted(gdir.iterdir()):
            if p.is_file():
                files[_walk(p.name, tmp)] = _walk(p.read_text(errors="replace"), tmp)
    return {"events": _walk(S.read_ledger(cfg, cid), tmp), "files": files,
            "final": {"state": st["state"], "close": _walk(st.get("close"), tmp)}}


# The one gate the fixture campaign shares with SPEC-0338's golden (`remote_gates_golden.json`):
# the same name, command, timeout and `required` as that golden's `ok` gate.
SHARED_GATE = "  - {name: ok, cmd: 'echo hello; git rev-parse HEAD', timeout_s: 60, required: true}\n"


def add_shared_gate(repo: Path) -> None:
    """Append `SHARED_GATE` to the fixture repo's project gates and commit it."""
    from test_agentic_e2e import _git
    yml = Path(repo) / ".nw" / "project.yaml"
    text = yml.read_text(encoding="utf-8")
    assert "\nmerge:" in text
    yml.write_text(text.replace("\nmerge:", "\n" + SHARED_GATE.rstrip("\n") + "\nmerge:", 1),
                   encoding="utf-8")
    _git(repo, "commit", "-qam", "a gate shared with SPEC-0338's golden")


def record(out: Path) -> None:
    """Run the plain (no workers) fake campaign of `test_agentic_e2e` and write its snapshot."""
    import os
    from click.testing import CliRunner  # noqa: F401
    from test_agentic_e2e import ENV, added, impl_done, make_cfg, make_repo, review, run_cli, script
    for k, v in ENV.items():
        os.environ[k] = v
    with tempfile.TemporaryDirectory(prefix="nwg") as d:
        tmp = Path(d)

        class _MP:                                  # the two monkeypatch calls `run_cli` makes
            def setattr(self, target, value):
                mod, _, attr = target.rpartition(".")
                setattr(__import__(mod, fromlist=["x"]), attr, value)
        repo = make_repo(tmp)
        add_shared_gate(repo)
        cfg = make_cfg(tmp, repo, script(tmp, [impl_done(), review()]))
        cid = added(cfg, _MP())
        r = run_cli(cfg, _MP(), ["queue", "run", "--until-idle", "--json-lines"])
        assert r.exit_code == 0, r.output
        out.write_text(json.dumps(snapshot(cfg, cid, tmp), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    record(Path(sys.argv[1]))
