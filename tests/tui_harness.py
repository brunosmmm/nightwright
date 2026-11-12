"""SPEC-0459: boot the real IdeaDesk on a copy of the campaign-history fixture.

`fixture_cfg` isolates that copy from the developer's data. `prepare_queue_window` moves
the copy's timestamps so the queue's 24 h delivered window shows every fixture campaign
in one order. The committed fixture is never written.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import history_scale as HS

SIZE = (160, 48)   # width, height; run_test uses this

# YYYY-MM-DDTHH:MM:SS with optional fraction and optional Z / numeric offset.
# Date-only strings, campaign ids, and bare numbers do not match.
_ISO = re.compile(
    rb"(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?(?!\d)"
)

_IDEA_KEYWORDS = ["IDEA", "INCUBATE", "SPECCED", "|", "PROMOTED", "DROPPED"]


def fixture_cfg(tmp_path) -> dict:
    """A cfg whose data dir is a plain copy of the fixture. Nothing is read from the
    developer's `NW_DATA_DIR`, `NW_CONFIG_DIR`, org, or data dir."""
    tmp_path = Path(tmp_path)
    data = HS.copy_fixture(tmp_path / "data")
    org = tmp_path / "org"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    specs = tmp_path / "specs"
    for directory in (org, config, cache, specs):
        directory.mkdir(parents=True, exist_ok=True)
    ideas = org / "ideas.org"
    ideas.write_text("#+TODO: IDEA | PROMOTED\n", encoding="utf-8")
    capture = org / "inbox.org"
    script = tmp_path / "fake-runner.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    return {
        "data_dir": str(data),
        "config_dir": str(config),
        "cache_dir": str(cache),
        "org_files": [str(org)],
        "org_ideas_file": str(ideas),
        "org_capture_file": str(capture),
        "specs_dir": str(specs),
        "org_todo_keywords": ["TODO", "|", "DONE"],
        "org_idea_keywords": list(_IDEA_KEYWORDS),
        "org_question_keywords": ["OPEN", "|", "RESOLVED"],
        "timezone": "America/New_York",
        "_tz": ZoneInfo("America/New_York"),
        "agentic": {"runner": "fake", "fake_script": str(script)},
    }


def visible_text(app) -> str:
    """The in-process screen, one rstripped row per line.

    Textual 8's `Screen.render_line` is the screen widget's own background. Child widgets
    (the queue table included) are drawn by the compositor, whose strips are the screen
    the pilot is looking at and expose the same `Strip.text`.
    """
    screen = app.screen
    strips = screen._compositor.render_strips(screen.size)
    return "\n".join(strip.text.rstrip() for strip in strips)


def prepare_queue_window(cfg, *, now=None) -> dt.datetime:
    """Shift `cfg["data_dir"]` so each manifest campaign finishes inside the 24 h window.

    For N manifest ids sorted lexicographically, campaign `i` is anchored at
    `now - (N - i) hours` (the lexicographically last id is the youngest). Returns `now`.
    """
    if now is None:
        now = dt.datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()
    data = Path(cfg["data_dir"]).resolve()
    fixture = HS.FIXTURE_DIR.resolve()
    try:
        data.relative_to(fixture)
    except ValueError:
        inside = False
    else:
        inside = True
    if inside:
        raise ValueError(f"{data} is inside the committed fixture {fixture}")
    campaigns = data / "agentic" / "campaigns"
    if not campaigns.is_dir():
        raise ValueError(f"{data} does not contain agentic/campaigns/")
    manifest = json.loads((fixture / "MANIFEST.json").read_text(encoding="utf-8"))
    cids = sorted(entry["cid"] for entry in manifest["campaigns"])
    camps = []
    for cid in cids:
        path = campaigns / cid
        if not path.is_dir():
            raise ValueError(f"manifest campaign {cid} has no directory under {campaigns}")
        camps.append(path)
    n = len(cids)
    # Anchors are read before any write so one missing timestamp does not leave a partial shift.
    deltas = []
    for i, path in enumerate(camps):
        target = now - dt.timedelta(hours=(n - i))
        deltas.append(target - _anchor(path))
    # Links are refused after the anchors and before the first write, so a refusal leaves the copy untouched.
    _refuse_links(data, camps)
    for cid, path, delta in zip(cids, camps, deltas):
        _shift_tree(path, delta)
        for log in _global_logs(data):
            _shift_global(log, cid, delta)
    return now


def _anchor(camp: Path) -> dt.datetime:
    """The ts of the last ledger transition into the current state, else updated_at, else created_at."""
    state = json.loads((camp / "state.json").read_text(encoding="utf-8"))
    current = state.get("state")
    found = None
    ledger = camp / "ledger.jsonl"
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "transition" and event.get("to") == current and event.get("ts"):
                found = event["ts"]
    if found is None:
        found = state.get("updated_at") or state.get("created_at")
    if not found:
        raise ValueError(f"{camp.name} has no anchor timestamp")
    return _parse_dt(found)


def _parse_dt(text: str) -> dt.datetime:
    raw = str(text).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _refuse_links(data: Path, camps: list[Path]) -> None:
    """Raise `ValueError` naming the first symlink, hardlinked file, or path that resolves outside
    `data` among what the shift would touch: the campaign trees (directories included) and the
    global-log candidates. The candidates are checked before `_global_logs` drops non-files, so a
    dangling link is seen; the directories those logs live in are checked too."""
    root = data.resolve()
    agentic = data / "agentic"
    paths = [agentic, agentic / "campaigns", agentic / "conductor",
             agentic / "mailbox", agentic / "escalations"]
    paths.extend(_global_candidates(data))
    for camp in camps:
        paths.append(camp)
        paths.extend(camp.rglob("*"))
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"{path} is a symlink; the queue window is shifted on a plain copy")
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"{path} resolves outside the copy {root}")
        if path.is_file() and path.stat().st_nlink > 1:
            raise ValueError(f"{path} is hardlinked (st_nlink > 1); a write would change its twin")


def _global_candidates(data: Path) -> list[Path]:
    """Every global-log path the shift may write, whether or not it exists as a file."""
    agentic = data / "agentic"
    logs = [
        agentic / "conductor" / "decisions.jsonl",
        agentic / "events.jsonl",
        agentic / "spec-reviews.jsonl",
    ]
    mailbox = agentic / "mailbox"
    if mailbox.is_dir():
        logs.extend(sorted(mailbox.glob("*.jsonl")))
    escalations = agentic / "escalations"
    if escalations.is_dir():
        logs.extend(sorted(escalations.glob("*.json")))
    return logs


def _global_logs(data: Path) -> list[Path]:
    """Global logs whose records are shifted when they name the campaign. `runner.jsonl` is not
    one of them: the shifted copy is for the queue, not for runner-down history."""
    return [path for path in _global_candidates(data) if path.is_file()]


def _shift_tree(root: Path, delta: dt.timedelta) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            _shift_file(path, delta)


def _shift_file(path: Path, delta: dt.timedelta) -> None:
    raw = path.read_bytes()
    shifted = _shift_datetimes(raw, delta)
    if shifted != raw:
        path.write_bytes(shifted)


def _shift_global(path: Path, cid: str, delta: dt.timedelta) -> None:
    """Move datetimes only on records that name `cid`. Every other line is kept byte for byte."""
    raw = path.read_bytes()
    if path.suffix == ".jsonl":
        shifted = _shift_jsonl(raw, cid, delta)
    else:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not _names_cid(document, cid):
            return
        shifted = _shift_datetimes(raw, delta)
    if shifted != raw:
        path.write_bytes(shifted)


def _shift_jsonl(data: bytes, cid: str, delta: dt.timedelta) -> bytes:
    if not data:
        return data
    changed = False
    out = []
    for line in data.splitlines(keepends=True):
        body = line.rstrip(b"\r\n")
        if _line_names_cid(body, cid):
            shifted = _shift_datetimes(body, delta)
            if shifted != body:
                changed = True
            out.append(shifted + line[len(body):])
        else:
            out.append(line)
    if not changed:
        return data
    return b"".join(out)


def _line_names_cid(body: bytes, cid: str) -> bool:
    if not body.strip():
        return False
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _names_cid(obj, cid)


def _names_cid(obj, cid: str) -> bool:
    if not isinstance(obj, dict):
        return False
    if obj.get("campaign") == cid:
        return True
    source = obj.get("source")
    return isinstance(source, dict) and source.get("campaign") == cid


def _shift_datetimes(data: bytes, delta: dt.timedelta) -> bytes:
    def repl(match: re.Match) -> bytes:
        raw = match.group(0).decode("ascii")
        zulu = raw.endswith("Z")
        try:
            parsed = dt.datetime.fromisoformat(raw[:-1] + "+00:00" if zulu else raw)
        except ValueError:
            return match.group(0)
        out = (parsed + delta).isoformat(timespec="seconds")
        if zulu and out.endswith("+00:00"):
            out = out[:-6] + "Z"
        return out.encode("ascii")

    return _ISO.sub(repl, data)
