"""SPEC-0346: the masked snapshot of a whole fake campaign, and its recorder. Depends only on
the campaign store and `test_agentic_e2e`'s helpers, so it also runs on the code before the
SPEC-0310 epic (the commit before SPEC-0337). `no_workers_golden.json` is recorded from that
commit checked out in a temporary worktree, with `NW_RECORD_GOLDEN=1`:

    git worktree add --detach /tmp/pre 1f0311c4^
    cp tests/remote_workers_golden.py /tmp/pre/tests/
    NW_RECORD_GOLDEN=1 PYTHONPATH=/tmp/pre/src:/tmp/pre/tests \\
        python /tmp/pre/tests/remote_workers_golden.py OUT.json
    git worktree remove --force /tmp/pre

`reproduce` runs that procedure and removes the worktree. The pin predates this module, so
the file is copied in; the campaign it runs is that commit's code.
"""
import json
import os
import re
import shutil
import subprocess
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
# `prompt_sha256` / `profile_sha256` hash text that contains the temp path, so they are masked
# like `nw_sha`. `head`, `tree` and `seq` are stable once shas are masked and are not dropped.
_MASKED_KEYS = frozenset({"ts", "duration_s", "nw_sha", "slot_wait_s", "started_at", "at",
                          "finished_at", "elapsed_s", "uid", "prompt_sha256", "profile_sha256"})


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


def comparable(obj):
    """The snapshot the golden comparison uses. Masking is done in `snapshot`; `review_outcome`
    and the stable fields (`head`, `tree`, `seq`) are kept, so a change in any of them fails."""
    return obj


# The one `review_outcome` a passing conformance review appends (SPEC-0422), after the
# pre-epic fixture was recorded. The comparison keeps it: a different score or outcome fails.
_REVIEW_OUTCOME = {
    "by": "tick", "campaign": "<CID>", "loop": 0, "major": 0, "must_fix": 0,
    "outcome": "pass", "pass_score": 80, "schema": "nw.ledger.event.v1",
    "score": 100, "ts": "<X>", "type": "review_outcome",
}


def with_kept_fields(golden: dict) -> dict:
    """The fixture recorded at `1f0311c4^`, plus the stable fields the ledger gained after that
    commit and that the comparison keeps: `nw_sha` and `profile_sha256` on `open` (masked),
    `head` and `tree` on each `gate` (masked), and the `review_outcome` before the review
    note. `seq` is renumbered. Anything else that differs still fails the comparison."""
    events = []
    for e in golden["events"]:
        e = dict(e)
        if e.get("type") == "open":
            e["nw_sha"] = "<X>"
            e["profile_sha256"] = "<X>"
        if e.get("type") == "gate":
            e["head"] = "<SHA>"
            e["tree"] = "<SHA>"
        if e.get("type") == "note" and not any(x.get("type") == "review_outcome" for x in events):
            events.append(dict(_REVIEW_OUTCOME))
        events.append(e)
    for i, e in enumerate(events, 1):
        e["seq"] = i
    return {"events": events, "files": golden["files"], "final": golden["final"]}


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
    """Run the plain (no workers) fake campaign of `test_agentic_e2e` and write its snapshot.

    Refuses to write unless `NW_RECORD_GOLDEN=1`, which is how the pre-epic worktree records
    the fixture (`reproduce`)."""
    if os.environ.get("NW_RECORD_GOLDEN") != "1":
        raise SystemExit("NW_RECORD_GOLDEN=1 is required to record the pre-epic golden")
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


def _repo_root() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"], text=True,
        cwd=Path(__file__).resolve().parents[1]).strip()


def reproduce(out: Path) -> None:
    """Record `out` from `PRE_EPIC` with `NW_RECORD_GOLDEN=1` in a temporary worktree.

    The worktree is removed before this returns. The bytes are the masked snapshot
    `record` writes at that commit (this module copied in, because the pin predates it)."""
    repo = _repo_root()
    parent = Path(tempfile.mkdtemp(prefix="nw-pre-epic-"))
    dest = parent / "wt"
    try:
        subprocess.run(["git", "-C", repo, "worktree", "add", "--detach", str(dest),
                        PRE_EPIC], check=True)
        target = dest / "tests" / Path(__file__).name
        target.write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("NW_")}
        env["NW_RECORD_GOLDEN"] = "1"
        env["PYTHONPATH"] = os.pathsep.join((str(dest / "src"), str(dest / "tests")))
        subprocess.run([sys.executable, str(target), str(out)], cwd=dest, env=env, check=True)
    finally:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", str(dest)],
                       check=False)
        subprocess.run(["git", "-C", repo, "worktree", "prune"], check=False)
        shutil.rmtree(parent, ignore_errors=True)


if __name__ == "__main__":
    record(Path(sys.argv[1]))
