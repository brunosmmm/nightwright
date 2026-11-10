"""SPEC-0538: coordinator `nw workers config` over SSH."""
from __future__ import annotations

import json
import subprocess

from . import worker_host_config as WHC
from . import workers as WK


def _ssh_run(w: dict, tail: list[str]) -> tuple[int, str, str]:
    argv = w["ssh_argv"] + [w["ssh"], w["nw"], "worker", "config-set", *tail]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=WK.CHECK_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 1, "", f"ssh timed out after {WK.CHECK_TIMEOUT_S:g}s"
    except OSError as e:
        return 1, "", f"ssh failed to start: {e}"
    return p.returncode, p.stdout or "", (p.stderr or "").strip()


def _last_stderr(stderr: str) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else stderr or "ssh failed"


def remote_config_set(w: dict, tail: list[str]) -> tuple[dict | None, str, int]:
    code, out, err = _ssh_run(w, tail)
    rec, perr = WHC.parse_config_set_result(out)
    if rec is None:
        reason = (_last_stderr(err) if err else perr) or f"ssh exit {code}"
        return None, reason, 1
    if not rec.get("ok"):
        return rec, rec.get("error") or f"ssh exit {code}", 1 if code else 3
    if code != 0:
        return rec, f"ssh exit {code}", 1
    return rec, "", 0


def remote_info(w: dict) -> tuple[dict | None, str]:
    argv = w["ssh_argv"] + [w["ssh"], w["nw"], "worker", "info", "--json"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=WK.CHECK_TIMEOUT_S,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None, f"ssh timed out after {WK.CHECK_TIMEOUT_S:g}s"
    except OSError as e:
        return None, f"ssh failed to start: {e}"
    if p.returncode != 0:
        return None, _last_stderr(p.stderr) or f"ssh exit {p.returncode}"
    try:
        info = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        return None, f"unreadable info json: {e}"
    if not isinstance(info, dict):
        return None, "info is not a mapping"
    return info, ""
