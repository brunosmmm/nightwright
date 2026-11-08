"""SPEC-0415 / SPEC-0460: helpers over the committed campaign-history fixture.

`copy_fixture(dst)` gives a writable data dir (`dst/agentic/...`) usable as `cfg["data_dir"]`.
It uses plain copies, never links, so a test may write to them without touching the committed
fixture. No network or git calls; nothing imported from `nightwright`."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "agentic_history"


def copy_fixture(dst: Path) -> Path:
    """Copy `FIXTURE_DIR/agentic/` into `dst/agentic/` (MANIFEST.json is not copied) and return
    `dst`. `dst` must not exist or must be empty, else `FileExistsError`."""
    dst = Path(dst)
    if dst.exists() and any(dst.iterdir()):
        raise FileExistsError(f"{dst} is not empty")
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_DIR / "agentic", dst / "agentic", copy_function=shutil.copyfile)
    return dst


def place_bytes(src: Path, dst: Path) -> None:
    dst = Path(dst)
    src = Path(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FileExistsError(dst)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def retarget_record(obj: dict, cid: str) -> dict:
    out = json.loads(json.dumps(obj))
    if "campaign" in out:
        out["campaign"] = cid
        return out
    source = out.get("source")
    if isinstance(source, dict) and "campaign" in source:
        source["campaign"] = cid
        return out
    raise ValueError("no campaign locator")


def shift_timestamps(obj, delta: dt.timedelta):
    if isinstance(obj, dict):
        return {key: shift_timestamps(value, delta) for key, value in obj.items()}
    if isinstance(obj, list):
        return [shift_timestamps(value, delta) for value in obj]
    if isinstance(obj, str):
        try:
            stamp = dt.datetime.fromisoformat(obj)
        except (TypeError, ValueError):
            return obj
        shifted = stamp + delta
        if "." in obj:
            return shifted.isoformat()
        return shifted.isoformat(timespec="seconds")
    return obj


_DATE_HEX = re.compile(r"-(\d{8})-([0-9a-f]{4})$")
_ISO_IN_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}(?::\d{2})?)?)?"
)


def _id_char(ch: str) -> bool:
    return ch.isalnum() or ch in "._-"


def _replace_cid(text: str, old: str, new: str) -> str:
    if old not in text:
        return text
    out: list[str] = []
    start = 0
    while True:
        pos = text.find(old, start)
        if pos < 0:
            out.append(text[start:])
            break
        before = text[pos - 1] if pos else ""
        after = text[pos + len(old)] if pos + len(old) < len(text) else ""
        if (not before or not _id_char(before)) and (not after or not _id_char(after)):
            out.append(text[start:pos])
            out.append(new)
            start = pos + len(old)
        else:
            out.append(text[start : pos + 1])
            start = pos + 1
    return "".join(out)


def _shift_text_timestamps(text: str, delta: dt.timedelta) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            stamp = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
        shifted = stamp + delta
        if "." in raw:
            return shifted.isoformat()
        if "T" in raw:
            return shifted.isoformat(timespec="seconds")
        return shifted.date().isoformat()

    return _ISO_IN_TEXT.sub(repl, text)


def _transform_json(obj, source_id: str, clone_id: str, delta: dt.timedelta):
    obj = shift_timestamps(obj, delta)

    def walk(value, *, root_dict: bool = False):
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                if root_dict and key == "campaign" and isinstance(item, str):
                    out[key] = clone_id
                elif root_dict and key == "source" and isinstance(item, dict):
                    src = dict(item)
                    if isinstance(src.get("campaign"), str):
                        src["campaign"] = clone_id
                    out[key] = walk(src, root_dict=False)
                elif key == "id" and item == source_id:
                    out[key] = clone_id
                else:
                    out[key] = walk(item, root_dict=False)
            return out
        if isinstance(value, list):
            return [walk(item, root_dict=False) for item in value]
        if isinstance(value, str):
            return _replace_cid(value, source_id, clone_id)
        return value

    return walk(obj, root_dict=True)


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.is_file():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def _max_seq(rows: list[dict], key: str = "seq") -> int:
    best = 0
    for row in rows:
        val = row.get(key)
        if isinstance(val, int) and val > best:
            best = val
    return best


def _max_event_id(rows: list[dict]) -> int:
    best = 0
    for row in rows:
        val = row.get("id")
        if isinstance(val, int) and val > best:
            best = val
    return best


def _mint_id(source_id: str, index: int, seed: int, used: set[str]) -> str:
    match = _DATE_HEX.search(source_id)
    if match:
        prefix = source_id[: match.start()]
        date = match.group(1)
    else:
        date_match = re.search(r"(\d{8})", source_id)
        if not date_match:
            raise ValueError(f"cannot mint id from {source_id}")
        prefix = source_id
        date = date_match.group(1)
    hex_val = (seed + index) & 0xFFFF
    for _ in range(65536):
        cid = f"{prefix}-{date}-{hex_val:04x}"
        if cid not in used:
            used.add(cid)
            return cid
        hex_val = (hex_val + 1) & 0xFFFF
    raise ValueError("exhausted minted campaign ids")


def _campaign_sources(fixture_agentic: Path) -> list[str]:
    root = fixture_agentic / "campaigns"
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def _pick_unit(kind: str, source_id: str, sources: list[str], fixture_agentic: Path) -> dict:
    agentic = fixture_agentic

    def with_campaign(row: dict) -> bool:
        return isinstance(row.get("campaign"), str) and row["campaign"] != ""

    if kind == "finding":
        for cid in sources:
            for row in _read_jsonl(agentic / "campaigns" / cid / "ledger.jsonl"):
                if row.get("type") == "finding" and row.get("campaign") == source_id:
                    return row
        for cid in sources:
            for row in _read_jsonl(agentic / "campaigns" / cid / "ledger.jsonl"):
                if row.get("type") == "finding" and with_campaign(row):
                    return row
        raise ValueError(f"no finding unit for {source_id}")

    if kind == "decision":
        rows = _read_jsonl(agentic / "conductor" / "decisions.jsonl")
        for row in rows:
            if row.get("campaign") == source_id:
                return row
        for row in rows:
            if with_campaign(row):
                return row
        raise ValueError("no decision unit")

    if kind == "event":
        rows = _read_jsonl(agentic / "events.jsonl")
        for row in rows:
            if row.get("campaign") == source_id:
                return row
        for row in rows:
            if with_campaign(row):
                return row
        raise ValueError("no event unit")

    if kind == "mail":
        for path in sorted((agentic / "mailbox").glob("*.jsonl")):
            for row in _read_jsonl(path):
                if row.get("campaign") == source_id:
                    return row | {"__mailbox__": path.name}
        for path in sorted((agentic / "mailbox").glob("*.jsonl")):
            for row in _read_jsonl(path):
                if with_campaign(row):
                    return row | {"__mailbox__": path.name}
        raise ValueError("no mail unit")

    if kind == "escalation":
        for path in sorted((agentic / "escalations").glob("ESC-*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            if row.get("campaign") == source_id:
                return row
        for path in sorted((agentic / "escalations").glob("ESC-*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            if with_campaign(row):
                return row
        raise ValueError("no escalation unit")

    raise ValueError(kind)


def _write_campaign_file(
    src: Path,
    dst: Path,
    *,
    source_id: str,
    clone_id: str,
    delta: dt.timedelta,
    identity_path: Path | None,
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    name = src.name
    if name.endswith(".json") or name.endswith(".jsonl"):
        if name.endswith(".jsonl"):
            lines = []
            for raw in src.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    text = _shift_text_timestamps(_replace_cid(raw, source_id, clone_id), delta)
                    lines.append(text.encode("utf-8"))
                    continue
                lines.append(_json_bytes(_transform_json(obj, source_id, clone_id, delta)))
            payload = b"".join(lines)
        else:
            obj = json.loads(src.read_text(encoding="utf-8"))
            payload = _json_bytes(_transform_json(obj, source_id, clone_id, delta))
    else:
        text = src.read_text(encoding="utf-8")
        text = _shift_text_timestamps(_replace_cid(text, source_id, clone_id), delta)
        payload = text.encode("utf-8")

    if identity_path is not None and identity_path.is_file() and payload == identity_path.read_bytes():
        place_bytes(identity_path, dst)
    else:
        dst.write_bytes(payload)


def _clone_minted_campaign(
    fixture_agentic: Path,
    dst_agentic: Path,
    *,
    source_id: str,
    clone_id: str,
    delta: dt.timedelta,
) -> None:
    src_root = fixture_agentic / "campaigns" / source_id
    dst_root = dst_agentic / "campaigns" / clone_id
    identity_root = dst_agentic / "campaigns" / source_id
    for src in sorted(src_root.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_root)
        identity_path = identity_root / rel if identity_root.is_dir() else None
        _write_campaign_file(
            src, dst_root / rel,
            source_id=source_id, clone_id=clone_id, delta=delta,
            identity_path=identity_path,
        )


def _append_finding(agentic: Path, clone_id: str, source_id: str, index: int,
                    unit: dict, delta: dt.timedelta) -> None:
    ledger = agentic / "campaigns" / clone_id / "ledger.jsonl"
    rows = _read_jsonl(ledger)
    line = json.loads(json.dumps(unit))
    line["type"] = "finding"
    line = _transform_json(line, source_id, clone_id, delta)
    line["seq"] = _max_seq(rows) + 1
    line["campaign"] = clone_id
    with ledger.open("ab") as stream:
        stream.write(_json_bytes(line))


def _append_decision(agentic: Path, clone_id: str, source_id: str, index: int,
                     unit: dict, delta: dt.timedelta) -> None:
    path = agentic / "conductor" / "decisions.jsonl"
    rows = _read_jsonl(path)
    line = json.loads(json.dumps(unit))
    line = _transform_json(line, source_id, clone_id, delta)
    line["campaign"] = clone_id
    line["seq"] = _max_seq(rows) + 1
    event_id = unit.get("event_id")
    if event_id is None:
        line["event_id"] = None
    else:
        line["event_id"] = f"{event_id}:{index}"
    with path.open("ab") as stream:
        stream.write(_json_bytes(line))


def _append_event(agentic: Path, clone_id: str, source_id: str, index: int,
                  unit: dict, delta: dt.timedelta) -> None:
    path = agentic / "events.jsonl"
    rows = _read_jsonl(path)
    line = json.loads(json.dumps(unit))
    line = _transform_json(line, source_id, clone_id, delta)
    line["campaign"] = clone_id
    line["id"] = _max_event_id(rows) + 1
    with path.open("ab") as stream:
        stream.write(_json_bytes(line))


def _inbox_name(mailbox_file: str) -> str:
    stem = Path(mailbox_file).stem
    return stem.replace("_", ":", 1) if "_" in stem else stem


def _append_mail(agentic: Path, clone_id: str, source_id: str, index: int,
                 unit: dict, delta: dt.timedelta) -> None:
    mailbox_file = unit.pop("__mailbox__")
    path = agentic / "mailbox" / mailbox_file
    rows = _read_jsonl(path)
    line = json.loads(json.dumps(unit))
    line = _transform_json(line, source_id, clone_id, delta)
    line["campaign"] = clone_id
    line["re"] = None
    seq = _max_seq(rows) + 1
    line["seq"] = seq
    inbox = _inbox_name(mailbox_file)
    line["id"] = f"{inbox}:{seq}"
    with path.open("ab") as stream:
        stream.write(_json_bytes(line))


def _append_escalation(agentic: Path, clone_id: str, source_id: str, index: int,
                       unit: dict, delta: dt.timedelta) -> None:
    esc_dir = agentic / "escalations"
    nums = []
    for path in esc_dir.glob("ESC-*.json"):
        try:
            nums.append(int(path.stem.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    num = (max(nums) if nums else 0) + 1
    esc_id = f"ESC-{num:04d}"
    doc = json.loads(json.dumps(unit))
    doc = _transform_json(doc, source_id, clone_id, delta)
    doc["id"] = esc_id
    doc["campaign"] = clone_id
    path = esc_dir / f"{esc_id}.json"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _count_findings(agentic: Path) -> int:
    total = 0
    camps = agentic / "campaigns"
    if camps.is_dir():
        for camp in camps.iterdir():
            if not camp.is_dir():
                continue
            for row in _read_jsonl(camp / "ledger.jsonl"):
                if row.get("type") == "finding":
                    total += 1
    return total


def _clone_per_kind(
    agentic: Path,
    fixture_agentic: Path,
    sources: list[str],
    *,
    clone_id: str,
    source_id: str,
    index: int,
    delta: dt.timedelta,
) -> None:
    finding = _pick_unit("finding", source_id, sources, fixture_agentic)
    _append_finding(agentic, clone_id, source_id, index, finding, delta)
    decision = _pick_unit("decision", source_id, sources, fixture_agentic)
    _append_decision(agentic, clone_id, source_id, index, decision, delta)
    event = _pick_unit("event", source_id, sources, fixture_agentic)
    _append_event(agentic, clone_id, source_id, index, event, delta)
    mail = _pick_unit("mail", source_id, sources, fixture_agentic)
    _append_mail(agentic, clone_id, source_id, index, mail, delta)
    escalation = _pick_unit("escalation", source_id, sources, fixture_agentic)
    _append_escalation(agentic, clone_id, source_id, index, escalation, delta)


def clone_fixture(
    dst: Path,
    *,
    campaigns: int = 500,
    records: int = 50_000,
    seed: int = 0,
) -> Path:
    """Build a scaled data dir at ``dst`` (``dst/agentic/…``) from the committed fixture."""
    dst = Path(dst).resolve()
    fixture_root = FIXTURE_DIR.resolve()
    if dst == fixture_root or fixture_root in dst.parents:
        raise ValueError("dst must not be inside FIXTURE_DIR")
    if campaigns < 1 or records < 0:
        raise ValueError("invalid campaigns or records")
    if dst.exists() and any(dst.iterdir()):
        raise FileExistsError(f"{dst} is not empty")

    fixture_agentic = FIXTURE_DIR / "agentic"
    sources = _campaign_sources(fixture_agentic)
    n = len(sources)

    dst.mkdir(parents=True, exist_ok=True)
    agentic = dst / "agentic"
    agentic.mkdir(parents=True)
    shutil.copyfile(fixture_agentic / "runner.jsonl", agentic / "runner.jsonl")
    shutil.copyfile(fixture_agentic / "spec-reviews.jsonl", agentic / "spec-reviews.jsonl")
    shutil.copyfile(fixture_agentic / "flaky-tests.json", agentic / "flaky-tests.json")
    shutil.copytree(fixture_agentic / "reviews", agentic / "reviews", copy_function=shutil.copyfile)
    shutil.copytree(fixture_agentic / "sentinel", agentic / "sentinel", copy_function=shutil.copyfile)
    (agentic / "conductor").mkdir(parents=True)
    shutil.copyfile(fixture_agentic / "conductor" / "decisions.jsonl",
                    agentic / "conductor" / "decisions.jsonl")
    shutil.copyfile(fixture_agentic / "events.jsonl", agentic / "events.jsonl")
    (agentic / "mailbox").mkdir(parents=True)
    for path in fixture_agentic.glob("mailbox/*.jsonl"):
        shutil.copyfile(path, agentic / "mailbox" / path.name)
    shutil.copytree(fixture_agentic / "escalations", agentic / "escalations", copy_function=shutil.copyfile)
    (agentic / "campaigns").mkdir(parents=True)

    used_ids: set[str] = set()
    extra: list[tuple[int, str, str, dt.timedelta]] = []

    for i in range(campaigns):
        source_id = sources[i % n]
        if i < n:
            src = fixture_agentic / "campaigns" / source_id
            out = agentic / "campaigns" / source_id
            shutil.copytree(src, out, copy_function=shutil.copyfile)
            used_ids.add(source_id)
            continue
        clone_id = _mint_id(source_id, i, seed, used_ids)
        delta = dt.timedelta(days=i)
        _clone_minted_campaign(
            fixture_agentic, agentic,
            source_id=source_id, clone_id=clone_id, delta=delta,
        )
        extra.append((i, clone_id, source_id, delta))

    fixture_decisions = (fixture_agentic / "conductor" / "decisions.jsonl").read_bytes()
    assert (agentic / "conductor" / "decisions.jsonl").read_bytes().startswith(fixture_decisions)
    fixture_events = (fixture_agentic / "events.jsonl").read_bytes()
    assert (agentic / "events.jsonl").read_bytes().startswith(fixture_events)
    for path in fixture_agentic.glob("mailbox/*.jsonl"):
        fb = path.read_bytes()
        assert (agentic / "mailbox" / path.name).read_bytes().startswith(fb)

    for index, clone_id, source_id, delta in extra:
        _clone_per_kind(agentic, fixture_agentic, sources, clone_id=clone_id,
                        source_id=source_id, index=index, delta=delta)

    count = _count_findings(agentic)
    if count < records:
        if not extra:
            raise ValueError("cannot reach records count")
        pos = 0
        while count < records:
            index, clone_id, source_id, delta = extra[pos % len(extra)]
            unit = _pick_unit("finding", source_id, sources, fixture_agentic)
            _append_finding(agentic, clone_id, source_id, index, unit, delta)
            count += 1
            pos += 1

    return dst


