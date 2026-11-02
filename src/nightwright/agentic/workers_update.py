"""`nw workers update NAME` (SPEC-0385): the coordinator side of SPEC-0369. Build a wheel of
this checkout, open ssh to the registered worker exactly as `nw workers check` does (the
record's `ssh_argv`, `identity` and `nw` path), and send `nw worker update [--force]` one
`nw.worker.update.v1` JSON line then the wheel bytes on stdin. The answer is one
`nw.worker.update.result.v1` line. No sudo, no key change, no registry change."""
from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .. import buildid
from . import workers as WK
from . import workers_bootstrap as WB

SCHEMA = "nw.worker.update.v1"
RESULT_SCHEMA = "nw.worker.update.result.v1"
EXIT_FAILED, EXIT_BUSY = 3, 5
TIMEOUT_S = 1200.0           # upload + the worker's uv install (its own limit is 900 s)
UV_LOG = "<worker dir>/update/uv.log"
BUSY_MSG = ("worker has live jobs; drain it (nw workers drain {name}) or retry; "
            "--force installs without killing jobs")

PULL_MSG = ("{name} is a pull worker (SPEC-0334): the coordinator has no ssh to it, so "
            "`nw workers update` cannot upgrade it; upgrade nightwright on that machine")


class UpdateError(Exception):
    pass


@dataclass
class Plan:
    name: str
    argv: list
    wheel: str
    size: int
    sha256: str
    build_id: str
    header: bytes
    force: bool

    def shown(self) -> str:
        import shlex
        return f"{shlex.join(self.argv)} < <header line + {self.size} wheel bytes>"


def command(w: dict, force: bool) -> list[str]:
    return w["ssh_argv"] + [w["ssh"], w["nw"], "worker", "update", *(["--force"] if force else [])]


def header_for(wheel: Path, data: bytes) -> tuple[bytes, str, str]:
    sha, bid = hashlib.sha256(data).hexdigest(), buildid.compute_wheel(wheel)
    h = {"schema": SCHEMA, "wheel": wheel.name, "size": len(data), "sha256": sha,
         "build_id": bid}
    return (json.dumps(h, separators=(",", ":")) + "\n").encode(), sha, bid


def parse_result(stdout: str) -> dict | None:
    for line in reversed((stdout or "").splitlines()):
        if line.strip():
            try:
                r = json.loads(line)
            except ValueError:
                return None
            return r if isinstance(r, dict) and r.get("schema") == RESULT_SCHEMA else None
    return None


HEADROOM = 3        # SPEC-0388: free space wanted on the worker, in wheel sizes


def _preflight(cfg, rec: dict, plan: Plan) -> None:
    """SPEC-0388: probe the worker's `info.free_bytes`; below `HEADROOM` x the wheel, fail
    before uploading anything. A worker that cannot be probed, or reports no number, is let
    through (the upload then fails on its own, as before)."""
    try:
        chk = WK.check(cfg, rec)
    except Exception:
        return
    free = (chk.get("info") or {}).get("free_bytes")
    if isinstance(free, int) and not isinstance(free, bool) and free < HEADROOM * plan.size:
        raise UpdateError(
            f"{plan.name}: not enough free disk for the update: {free / 2**20:.0f} MiB free, "
            f"need {HEADROOM} x the wheel ({HEADROOM * plan.size / 2**20:.0f} MiB); free space "
            "on the worker (nw worker gc, finished jobs) and retry")


def send(cfg, rec: dict, force: bool, dry_run: bool = False):
    """Build the wheel and (unless `dry_run`) upload it. Returns `(plan, result_or_None,
    exit_code_or_None, stderr)`; raises `UpdateError` for a failure before an answer."""
    import subprocess
    if rec.get("mode") == "pull":
        raise UpdateError(PULL_MSG.format(name=rec.get("name")))
    w = WK.normalize(cfg, rec)
    try:
        WK.ensure_record_ssh_dir(cfg, w)
    except OSError as e:
        raise UpdateError(f"ssh socket directory unusable: {e}")
    try:
        with tempfile.TemporaryDirectory(prefix="nw-wheel-") as out:
            wheel = WB.build_wheel(out)
            data = wheel.read_bytes()
            header, sha, bid = header_for(wheel, data)
            plan = Plan(w["name"], command(w, force), wheel.name, len(data), sha, bid,
                        header, force)
            if dry_run:
                return plan, None, None, ""
            _preflight(cfg, rec, plan)
            try:
                p = subprocess.run(plan.argv, input=header + data, capture_output=True,
                                   timeout=TIMEOUT_S)
            except subprocess.TimeoutExpired:
                raise UpdateError(f"ssh timed out after {TIMEOUT_S:g}s; the worker may "
                                  "still be installing: run nw workers check "
                                  f"{plan.name}")
            except OSError as e:
                raise UpdateError(f"`ssh` failed to start: {e}")
    except WB.StepFailed as e:
        raise UpdateError(str(e))
    out_s = p.stdout.decode("utf-8", "replace")
    err_s = p.stderr.decode("utf-8", "replace").strip()
    return plan, parse_result(out_s), p.returncode, err_s


@dataclass
class Outcome:
    """SPEC-0386: the result of one non-interactive update (the queue's scheduled path).
    `kind`: updated | unchanged | busy | failed | old (worker without the verb)."""
    kind: str
    detail: str = ""
    build_id: str | None = None
    live_jobs: list | None = None


def perform(cfg, rec: dict) -> Outcome:
    """SPEC-0386: `send` (never `--force`) then the post-update `check`, mapped to an
    `Outcome`. Shares the wire path and the build-id verification with `nw workers update`."""
    try:
        plan, res, code, err = send(cfg, rec, False)
    except UpdateError as e:
        return Outcome("failed", str(e))
    if res is None:
        why = err.splitlines()[-1] if err else "no result line"
        return Outcome("old" if "not allowed" in err else "failed",
                       f"ssh exit {code}: {why}")
    state = res.get("state")
    if state == "busy":
        return Outcome("busy", BUSY_MSG.format(name=plan.name), live_jobs=res.get("live_jobs"))
    if state == "failed":
        return Outcome("failed", f"failed at {res.get('stage')}: {res.get('error')}")
    chk = WK.check(cfg, rec)
    got = (chk.get("info") or {}).get("build_id")
    if not chk["ok"] or got != plan.build_id:
        why = "; ".join(chk["errors"]) or f"build {got} != {plan.build_id}"
        return Outcome("failed", f"check after update: {why}")
    return Outcome(str(state), f"build {str(got)[:12]}", build_id=got)
