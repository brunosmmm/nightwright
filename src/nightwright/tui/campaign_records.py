"""Pure model for the campaign detail popup's Findings & decisions and Mail panes (SPEC-0477).

Everything here reads `history.records` (`nw.history.record.v1`) and, for mail, the one mailbox
line a record points at. No widget, no write. Record text is data: callers wrap it in Rich `Text`.
"""
import json
from dataclasses import dataclass

from rich.text import Text

from ..agentic import history as H
from ..agentic import store

KEYED = ("agent", "kind", "severity", "status")
FIXED_LABEL = "not re-raised"
LINK_KEYS = ("run_id", "resolved_by", "followup_idea", "escalation", "thread")


def sort_records(records) -> list[dict]:
    """`ts` descending, then `id` ascending (equal timestamps do not swap on reload)."""
    unique = {str(r["id"]): r for r in records or [] if isinstance(r, dict) and r.get("id")}
    rows = [unique[k] for k in sorted(unique)]
    rows.sort(key=lambda r: H._time_key(r.get("ts")), reverse=True)    # stable
    return rows


def status_cell(rec: dict) -> str:
    status = rec.get("status")
    return FIXED_LABEL if status == "fixed" else ("" if status is None else str(status))


def parse_filter(text: str) -> tuple[list[tuple[str | None, str]], list[str]]:
    """`(applied, unmatched)`. A token splits on its first colon only; a keyed token whose key is
    not one of `KEYED` (or whose value is empty) is not applied and comes back as unmatched."""
    applied, unmatched = [], []
    for tok in (text or "").split():
        key, sep, value = tok.partition(":")
        if not sep:
            applied.append((None, tok))
        elif key in KEYED and value:
            applied.append((key, value))
        else:
            unmatched.append(tok)
    return applied, unmatched


def match_record(rec: dict, applied) -> bool:
    for key, value in applied:
        if key is None:
            low = value.lower()
            if low not in str(rec.get("title") or "").lower() \
                    and low not in str(rec.get("text") or "").lower():
                return False
        elif str(rec.get(key)) != value:
            return False
    return True


def filter_records(records, text: str) -> tuple[list[dict], list[str]]:
    applied, unmatched = parse_filter(text)
    return [r for r in records if match_record(r, applied)], unmatched


def nearest_index(rows, rec_id, old_index: int) -> int:
    """Row to select after a reload: the same id, else the nearest remaining row."""
    if not rows:
        return -1
    for i, r in enumerate(rows):
        if r.get("id") == rec_id:
            return i
    return max(0, min(old_index, len(rows) - 1))


def record_links(rec: dict) -> dict:
    links = rec.get("links")
    return links if isinstance(links, dict) else {}


def detail_text(rec: dict) -> Text:
    """Title, text, status and the link ids, each labeled by its key; always literal."""
    t = Text()
    t.append(str(rec.get("title") or ""), style="bold")
    t.append("\n")
    if rec.get("text"):
        t.append(str(rec["text"]))
        t.append("\n")
    t.append("\n")
    for key, val in (("status", rec.get("status")), ("linked_by", rec.get("linked_by"))):
        t.append(f"{key}: ", style="dim")
        t.append("" if val is None else str(val))
        t.append("\n")
    links = record_links(rec)
    for key in LINK_KEYS:
        if links.get(key) is not None:
            t.append(f"{key}: ", style="dim")
            t.append(str(links[key]))
            t.append("\n")
    t.rstrip()
    return t


# --- mail ---------------------------------------------------------------------------------


def mailbox_line(cfg, rec_id) -> dict | None:
    """The mailbox JSON object a `mailbox:<stem>:<byte offset>` record id points at, or None
    (never raises: a missing, moved or unparsable line is the caller's fallback)."""
    try:
        head, _, offset = str(rec_id).rpartition(":")
        source, _, stem = head.partition(":")
        if source != "mailbox" or not stem or "/" in stem or "\\" in stem:
            return None
        path = store.agentic_dir(cfg) / "mailbox" / f"{stem}.jsonl"
        with path.open("rb") as f:
            f.seek(int(offset))
            value = json.loads(f.readline().decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


@dataclass
class MailMessage:
    id: str
    ts: str | None
    frm: str
    to: str
    kind: str
    text: str
    status: str | None


def mail_message(cfg, rec: dict) -> MailMessage:
    line = mailbox_line(cfg, rec.get("id"))
    if line is None:
        return MailMessage(str(rec.get("id")), rec.get("ts"), str(rec.get("agent") or ""), "",
                           "", str(rec.get("text") or ""), rec.get("status"))
    return MailMessage(str(rec.get("id")), rec.get("ts") or line.get("ts"),
                       str(line.get("from") or ""), str(line.get("to") or ""),
                       str(line.get("kind") or ""),
                       str(line.get("text") if line.get("text") is not None
                           else rec.get("text") or ""),
                       rec.get("status"))


@dataclass
class MailThread:
    key: str                    # `links.thread`, or the record id when it is null
    records: list               # this campaign's records in the thread, `ts` ascending
    latest: dict


def mail_threads(records) -> list[MailThread]:
    """One thread per `links.thread` among `kind: mail` records (a null thread is its own row),
    latest `ts` descending then thread id ascending."""
    groups: dict[str, list] = {}
    for rec in records or []:
        if not isinstance(rec, dict) or rec.get("kind") != "mail" or not rec.get("id"):
            continue
        key = record_links(rec).get("thread") or rec["id"]
        groups.setdefault(str(key), []).append(rec)
    out = []
    for key, recs in groups.items():
        recs = sorted(sorted(recs, key=lambda r: str(r["id"])),
                      key=lambda r: H._time_key(r.get("ts")))
        out.append(MailThread(key, recs, recs[-1]))
    out.sort(key=lambda th: th.key)
    out.sort(key=lambda th: H._time_key(th.latest.get("ts")), reverse=True)
    return out


def thread_detail(cfg, thread: MailThread) -> Text:
    t = Text()
    for rec in thread.records:
        m = mail_message(cfg, rec)
        t.append(f"{m.ts or ''}  ", style="dim")
        t.append(f"from {m.frm or '-'} to {m.to or '-'}  ", style="bold")
        t.append(f"{m.kind or '-'}\n", style="cyan")
        t.append(m.text)
        t.append("\n\n")
    t.rstrip()
    return t


def idea_title(cfg, idea_id) -> str | None:
    """The heading of idea `idea_id` when its file can be read, else None."""
    try:
        from ..org import load_tasks
        return next((t.heading for t in load_tasks(cfg) if t.id == idea_id), None)
    except Exception:
        return None
