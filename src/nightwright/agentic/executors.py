"""The worker's executor seam (SPEC-0503): how a claimed job starts, is cancelled, reports its
live state and is admitted on the host, and how the synchronous `gate-job` runs.

`local` (the default) is today's behaviour: a detached `python -m nightwright.agentic.worker_job`
process in its own session (`worker_job._spawn`), a `cancel` file its `_watch` thread obeys, the
lease/heartbeat state of `worker_job._live_state`, the host gate slot of `gate_slots.acquire`,
and `gate-job` run in-process. The Slurm executor (SPEC-0510, `executor_slurm.py`) is
registered as `slurm`; a host uses it only when its own config opts in.

The worker host's own config chooses: `agentic.workers.executor` (the coordinator never does).
A job records the executor that started it in `job.json` (`executor`), so a later read, cancel
or run of that job goes through the same one even if the host's setting changed meanwhile."""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Protocol

from . import worker as W

DEFAULT = "local"


class Executor(Protocol):
    name: str
    lease_cancels: bool                 # the job process cancels itself on an expired lease

    def start(self, cfg, job: Path, h: dict, lock_fd: int) -> dict: ...
    def cancel(self, cfg, job: Path, rec: dict) -> None: ...
    def state(self, cfg, job: Path, rec: dict, sett: dict, now: float) -> tuple: ...
    def host_slot(self, cfg, h: dict, cancel, announce): ...
    def run_inline(self, cfg, argv: list, stdin, stdout) -> int: ...


class LocalExecutor:
    """Today's code paths, unchanged (looked up on their modules at call time, so a test's
    monkeypatch of `worker_job._spawn` or `gate_slots.acquire` still applies)."""

    name = "local"
    lease_cancels = True

    def start(self, cfg, job: Path, h: dict, lock_fd: int) -> dict:
        from . import runproc
        from . import worker_job as WJ
        try:
            child = WJ._spawn(cfg, job, lock_fd)
        except OSError as e:
            raise W.WorkerError("spawn", f"could not start the job process: {e}")
        return {"pid": child.pid, "pgid": child.pid, "start_ticks": runproc.start_ticks(child.pid)}

    def cancel(self, cfg, job: Path, rec: dict) -> None:
        from .store import now_iso
        (job / "cancel").write_text(now_iso() + "\n", encoding="utf-8")

    def state(self, cfg, job: Path, rec: dict, sett: dict, now: float) -> tuple:
        from . import worker_job as WJ
        return WJ._live_state(job, sett, now)

    @contextmanager
    def host_slot(self, cfg, h: dict, cancel, announce):
        from . import gate_slots
        n_slots = h["payload"].get("host_slots") or gate_slots.settings(cfg)["slots"]
        with gate_slots.acquire(cfg, W.SLOT_CAMPAIGN_PREFIX + h["campaign"], stop=cancel,
                                on_wait=announce, wait_event_s=W._slot_wait_event_s(),
                                pools=[(None, n_slots)]) as hold:
            yield hold

    def run_inline(self, cfg, argv: list, stdin, stdout) -> int:
        return W._gate_job_local(cfg, stdin, stdout)


def _slurm() -> Executor:
    from .executor_slurm import SlurmExecutor     # SPEC-0510 (opt-in: `executor: slurm`)
    return SlurmExecutor()


_REGISTRY: dict[str, Callable[[], Executor]] = {"local": LocalExecutor, "slurm": _slurm}


def register(name: str, factory: Callable[[], Executor]) -> None:
    """Make an executor available under `name` (SPEC-0510's `slurm`; tests' fakes)."""
    _REGISTRY[name] = factory


def unregister(name: str) -> None:
    if name != DEFAULT:
        _REGISTRY.pop(name, None)


def known() -> list[str]:
    return sorted(_REGISTRY)


def configured(cfg) -> str:
    """`agentic.workers.executor` as written (absent or empty: `local`)."""
    raw = ((cfg.get("agentic") or {}).get("workers") or {})
    raw = raw.get("executor") if isinstance(raw, dict) else None
    return raw if isinstance(raw, str) and raw.strip() else DEFAULT


def _make(name: str) -> Executor | None:
    factory = _REGISTRY.get(name)
    return factory() if factory is not None else None


def get(cfg) -> Executor:
    """The executor this host's config names; an unknown name raises
    `WorkerError(stage="executor")` (the job is then refused before any claim)."""
    name = configured(cfg)
    ex = _make(name)
    if ex is None:
        raise W.WorkerError("executor", f"unknown executor {name!r} (agentic.workers.executor); "
                                        f"this worker knows {', '.join(known())}")
    return ex


def for_job(cfg, job: Path, rec: dict | None = None) -> Executor:
    """The executor that started `job` (its `job.json` `executor`; a dir without one, or a
    name this build does not know, is `local`: every job before SPEC-0503 was)."""
    if rec is None:
        try:
            rec = json.loads((job / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rec = {}
    name = rec.get("executor") if isinstance(rec, dict) else None
    ex = _make(name) if isinstance(name, str) else None
    if ex is None and isinstance(name, str) and name != DEFAULT:
        print(f"nw worker: {job.name}: unknown executor {name!r}; treating it as local",
              file=sys.stderr)
    return ex or LocalExecutor()


def refuse_gate_job(stdin, stdout, err: W.WorkerError) -> int:
    """`gate-job` under an unknown executor: read the header line (for its job id), print one
    `error` frame (`stage: "executor"`) and claim nothing."""
    frames = W._Frames(None, stdout)
    try:
        h = json.loads(stdin.readline(W.MAX_HEADER_BYTES).decode("utf-8"))
        if isinstance(h, dict) and isinstance(h.get("job_id"), str) \
                and W.KEY_RE.match(h["job_id"]):
            frames.job_id = h["job_id"]
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    frames.emit("error", stage=err.stage, error=err.error)
    return W.EXIT_ERROR
