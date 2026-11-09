"""Idea explore enrichment (SPEC-0024): Summary / Open questions / Log under an idea
headline. Mutates org only — never promotes (no :SPEC: / SPECCED).
"""
from __future__ import annotations

import datetime as dt
import os
import re

from .org_write import (_HEADLINE_RE, _atomic_backup_write, _locate_headline, org_writer,
                        resolve_selector, set_state, file_keywords, _inactive_stamp, set_property, ensure_idea_keyword,
                        stamp_updated, parse_inactive_stamp, _INACTIVE_STAMP_RE)
from .org_markup import to_org_body

SECTION_SUMMARY = "Summary"
SECTION_QUESTIONS = "Open questions"
SECTION_LOG = "Log"
_SECTIONS = (SECTION_SUMMARY, SECTION_QUESTIONS, SECTION_LOG)

_STAR_SECTION_RE = re.compile(r"^(\*+)\s+(Summary|Open questions|Log)\s*$")
# Plain Log heads from before SPEC-0032: *** YYYY-MM-DD[ HH:MM]
_PLAIN_LOG_HEAD_RE = re.compile(
    r"^(\*{3,})\s+(\d{4}-\d{2}-\d{2})(?:\s+(\d{2}:\d{2}))?\s*$"
)

# --- Open-questions state model (SPEC-0054) --------------------------------------------
# A question is a `*** OPEN|RESOLVED [#P] text` headline under `** Open questions`. The
# classifier recognizes the historical freeform forms so migration/display never lose text:
# checkboxes, `(resolved)` prefixes, `_Resolved:_` group headers, bare bullets, and leading
# status LABELS (RESOLVED/OPEN/LOCKED/OPTIONAL/Remaining) used in decision logs.
_QHEAD_RE = re.compile(r"^\*{3,}\s+(OPEN|RESOLVED)\b\s*(?:\[#([ABC])\]\s*)?(.*)$")
_QGROUP_HDR_RE = re.compile(r"^_+\s*resolved\b.*_\s*$", re.IGNORECASE)
_QCHECK_RE = re.compile(r"^[-+*]\s*\[([ xX])\]\s*(.*)$")
_QPAREN_RE = re.compile(r"^(?:[-+*]\s*)?\((resolved|open)\)\s*(.*)$", re.IGNORECASE)
_QBULLET_RE = re.compile(r"^[-+*]\s+(.*)$")
# Leading bare status labels; the word may be followed by a `—`/`-`/`:` separator we strip.
_QLABEL_RE = re.compile(r"^(RESOLVED|OPEN|LOCKED|OPTIONAL|Remaining)\b[ \t]*(?:[—:-][ \t]*)?(.*)$")
_QLABEL_MAP = {                           # label -> (state, org priority cookie or None)
    "RESOLVED": ("resolved", None),
    "LOCKED": ("resolved", None),         # a locked decision is closed
    "OPEN": ("open", None),
    "OPTIONAL": ("open", "C"),            # optional follow-up → low-priority open
    "Remaining": ("open", None),
}


def _section_is_normalized(text: str) -> bool:
    """True when the Open-questions section is already in `*** OPEN|RESOLVED` form (SPEC-0111).

    Requires at least one question headline **and** no content line before the first one. A file
    caught mid-migration therefore reads as *legacy*, which is the safe direction: it keeps
    migrating rather than silently reclassifying half-converted questions as prose.
    """
    seen_headline = False
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if _QHEAD_RE.match(s):
            seen_headline = True
            continue
        if not seen_headline:
            return False                  # content before any headline → still legacy
    return seen_headline


def _classify_question_lines(text: str) -> list[dict]:
    """Parse an Open-questions body (any historical form) into ordered
    `[{state, priority, text, body}]` items.

    **Mode-aware (SPEC-0111).** In an *already-normalized* section — every question a
    `*** OPEN|RESOLVED` headline — any other line is **prose belonging to the preceding
    question**, kept in `body` and never counted as an item. That is what stops a hand-written
    rationale from becoming a phantom question and shifting every later index, so `--resolve N`
    keeps hitting the Nth question.

    In a *legacy* section the historical behaviour is unchanged: unrecognized non-empty lines
    become OPEN items (verbatim) and indented lines continue the previous item — SPEC-0054's
    migration path, which is deliberate and must keep working.
    """
    normalized = _section_is_normalized(text)
    items: list[dict] = []
    group_state = None                       # sticky state from a `_Resolved…_` sub-header
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s:
            if normalized and items and items[-1].get("body"):
                items[-1]["body"] += "\n"    # keep paragraph breaks inside a body
            continue
        m = _QHEAD_RE.match(s)
        if m:
            items.append({"state": "resolved" if m.group(1) == "RESOLVED" else "open",
                          "priority": m.group(2), "text": m.group(3).strip()})
            continue
        if normalized:
            # Not a headline, in a normalized section => body prose, not a question.
            if items:
                prior = items[-1].get("body") or ""
                items[-1]["body"] = (prior + ("\n" if prior else "") + s).rstrip("\n")
            continue
        if _QGROUP_HDR_RE.match(s):
            group_state = "resolved"
            continue
        c = _QCHECK_RE.match(s)
        if c:
            items.append({"state": "resolved" if c.group(1) in ("x", "X") else "open",
                          "priority": None, "text": c.group(2).strip()})
            continue
        p = _QPAREN_RE.match(s)
        if p:
            items.append({"state": "resolved" if p.group(1).lower() == "resolved" else "open",
                          "priority": None, "text": p.group(2).strip()})
            continue
        b = _QBULLET_RE.match(s)
        content = b.group(1) if b else s
        lab = _QLABEL_RE.match(content)
        if lab and lab.group(1) in _QLABEL_MAP:
            st, pr = _QLABEL_MAP[lab.group(1)]
            items.append({"state": st, "priority": pr, "text": lab.group(2).strip()})
            continue
        if b:
            items.append({"state": group_state or "open", "priority": None,
                          "text": content.strip()})
            continue
        if raw[:1] in (" ", "\t") and items:    # indented continuation of prior item
            items[-1]["text"] = (items[-1]["text"] + " " + s).strip()
        else:
            items.append({"state": group_state or "open", "priority": None, "text": s})
    return items


def _items_to_question_body(items: list[dict], *, normalize_text: bool = True) -> str:
    """Render items to `*** OPEN|RESOLVED [#P] text` headlines. `normalize_text` runs the
    SPEC-0030 Markdown→org pass on each item's *text only* (never the assembled line, or the
    leading `***` would pair with `**` in the text and mangle both). Migration passes
    `normalize_text=False` so existing content is preserved byte-for-byte."""
    out = []
    for it in items:
        kw = "RESOLVED" if it.get("state") == "resolved" else "OPEN"
        text = to_org_body(it.get("text", "")) if normalize_text else it.get("text", "")
        text = text.strip()
        cookie = f"[#{it['priority']}] " if it.get("priority") else ""
        out.append(f"*** {kw} {cookie}{text}".rstrip() + "\n")
        # SPEC-0111: re-emit the question's body so a round trip through any mutator is
        # lossless. Never markup-normalized — it is the human's prose, not a headline.
        body = (it.get("body") or "").rstrip("\n")
        if body:
            out.extend(line.rstrip() + "\n" for line in body.split("\n"))
    return "".join(out)


@org_writer
def _ensure_question_header(cfg, path) -> bool:
    """Ensure the file declares `#+TODO: OPEN | RESOLVED` so orgparse recognizes question
    states (a file's own `#+TODO` wins over the env fallback). Inserted after the last
    existing `#+TODO:` line, else at the top. Idempotent; returns True if it wrote."""
    raw = open(path, encoding="utf-8").read()
    lines = raw.splitlines(keepends=True)
    insert_at = 0
    for i, ln in enumerate(lines):
        parts = ln.strip().split()
        if parts and parts[0] == "#+TODO:":
            insert_at = i + 1
            if "OPEN" in parts and "RESOLVED" in parts:
                return False
    lines[insert_at:insert_at] = ["#+TODO: OPEN | RESOLVED\n"]
    _atomic_backup_write(cfg, path, "".join(lines))
    return True


def _idea_span(lines, task):
    """Return (head_idx, body_start, body_end) for task's headline subtree.
    body_end is exclusive index of the next same-or-higher-level headline (or len)."""
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError(f"line {task.line} is not a headline")
    level = len(m.group(1))
    # drift: state token
    rest = m.group(3)
    first = rest.split(None, 1)[0] if rest.split() else ""
    if task.state and first != task.state:
        raise ValueError(f"drift: expected state {task.state!r}, found {first!r}")
    body_start = idx + 1
    body_end = len(lines)
    for j in range(body_start, len(lines)):
        hm = _HEADLINE_RE.match(lines[j].rstrip("\n"))
        if hm and len(hm.group(1)) <= level:
            body_end = j
            break
    return idx, body_start, body_end


def _skip_meta(lines, start, end):
    """Skip PLANNING lines and :PROPERTIES:…:END: drawer; return content start index."""
    j = start
    while j < end and re.match(r"^\s*(SCHEDULED|DEADLINE|CLOSED):", lines[j]):
        j += 1
    if j < end and lines[j].strip() == ":PROPERTIES:":
        j += 1
        while j < end and lines[j].strip() != ":END:":
            j += 1
        if j < end:
            j += 1  # past :END:
    return j


def _find_sections(lines, content_start, body_end):
    """Map section name -> (header_idx, body_start, body_end) within the idea content."""
    found = {}
    headers = []
    for j in range(content_start, body_end):
        m = _STAR_SECTION_RE.match(lines[j].rstrip("\n"))
        if m and len(m.group(1)) >= 2:
            headers.append((j, m.group(2)))
    for i, (hidx, name) in enumerate(headers):
        b_start = hidx + 1
        b_end = headers[i + 1][0] if i + 1 < len(headers) else body_end
        found[name] = (hidx, b_start, b_end)
    return found


def _ensure_skeleton(lines, content_start, body_end):
    """Ensure ** Summary / Open questions / Log exist; return updated lines + new body_end."""
    sections = _find_sections(lines, content_start, body_end)
    missing = [s for s in _SECTIONS if s not in sections]
    if not missing:
        return lines, body_end
    block = []
    for s in missing:
        block.append(f"** {s}\n")
        if s == SECTION_LOG:
            pass
        else:
            block.append("\n")
    # Insert at content_start (right after meta) so Summary is first
    lines[content_start:content_start] = block
    return lines, body_end + len(block)


def idea_summary_text(cfg, task, *, strict: bool = False) -> str:
    """Return Summary body text (stripped), or '' if missing/empty."""
    try:
        lines = open(task.file).read().splitlines(keepends=True)
        _, body_start, body_end = _idea_span(lines, task)
        content_start = _skip_meta(lines, body_start, body_end)
        sections = _find_sections(lines, content_start, body_end)
        if SECTION_SUMMARY not in sections:
            return ""
        _, b0, b1 = sections[SECTION_SUMMARY]
        return "".join(lines[b0:b1]).strip()
    except Exception:
        if strict:
            raise
        return ""


def read_idea_enrichment(cfg, task, *, lines=None) -> dict:
    """{summary, questions, log} string bodies (may be empty).

    `lines` (SPEC-0495): the file's `splitlines(keepends=True)` already in hand, so a caller
    walking many ideas of one file reads it once instead of once per idea."""
    if lines is None:
        lines = open(task.file).read().splitlines(keepends=True)
    _, body_start, body_end = _idea_span(lines, task)
    content_start = _skip_meta(lines, body_start, body_end)
    sections = _find_sections(lines, content_start, body_end)
    out = {"summary": "", "questions": "", "log": ""}
    key = {SECTION_SUMMARY: "summary", SECTION_QUESTIONS: "questions", SECTION_LOG: "log"}
    for name, slot in key.items():
        if name in sections:
            _, b0, b1 = sections[name]
            out[slot] = "".join(lines[b0:b1]).strip()
    return out


def _stars(lines, idx) -> str:
    """The leading stars of the headline at `lines[idx]` (for `stamp_updated`'s indent)."""
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    return m.group(1) if m else "*"


@org_writer
def _replace_section_body(cfg, task, section_name, body_text, *, apply_markup=True):
    # Question bodies are pre-rendered as `*** OPEN|RESOLVED` headlines with markup already
    # applied per item (SPEC-0054); re-running to_org_body over them would mangle the stars.
    body_text = to_org_body(body_text or "") if apply_markup else (body_text or "")
    lines = open(task.file).read().splitlines(keepends=True)
    head_idx, body_start, body_end = _idea_span(lines, task)
    content_start = _skip_meta(lines, body_start, body_end)
    lines, body_end = _ensure_skeleton(lines, content_start, body_end)
    content_start = _skip_meta(lines, body_start, body_end)
    sections = _find_sections(lines, content_start, body_end)
    if section_name not in sections:
        raise ValueError(f"section {section_name!r} missing after ensure")
    _hidx, b0, b1 = sections[section_name]
    if body_text.strip():
        text = body_text.rstrip("\n") + "\n"
        new_body_lines = [ln if ln.endswith("\n") else ln + "\n"
                          for ln in text.splitlines(keepends=True)]
    else:
        new_body_lines = ["\n"]
    lines[b0:b1] = new_body_lines
    # SPEC-0076: last, after every body edit — creating :UPDATED: shifts the offsets above.
    stamp_updated(cfg, lines, head_idx, _stars(lines, head_idx))
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


def set_summary(cfg, selector, text):
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    return _replace_section_body(cfg, task, SECTION_SUMMARY, text)


def read_idea_questions(cfg, task, *, lines=None) -> list[dict]:
    """Ordered `[{state, text}]` for the idea's Open questions (SPEC-0054)."""
    return _classify_question_lines(read_idea_enrichment(cfg, task, lines=lines)["questions"])


def _require_idea(cfg, selector):
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    return task


def set_questions(cfg, selector, text):
    """Replace the Open questions section. Each source line becomes an OPEN question
    headline (existing `*** OPEN|RESOLVED` / legacy forms are re-classified, keeping state)."""
    task = _require_idea(cfg, selector)
    items = _classify_question_lines(text or "")
    if _ensure_question_header(cfg, task.file):
        task = _require_idea(cfg, selector)          # header insert shifts line numbers
    return _replace_section_body(cfg, task, SECTION_QUESTIONS,
                                 _items_to_question_body(items), apply_markup=False)


def add_question(cfg, selector, bullet, state="open"):
    """Append one `*** OPEN|RESOLVED <text>` question headline; existing lines untouched."""
    if state not in ("open", "resolved"):
        raise ValueError(f"invalid state {state!r} (open|resolved)")
    task = _require_idea(cfg, selector)
    if _ensure_question_header(cfg, task.file):
        task = _require_idea(cfg, selector)
    cur = read_idea_enrichment(cfg, task)["questions"]
    head = _items_to_question_body([{"state": state, "text": bullet.strip()}]).rstrip("\n")
    new = (cur.rstrip() + "\n" + head + "\n") if cur.strip() else head + "\n"
    return _replace_section_body(cfg, task, SECTION_QUESTIONS, new, apply_markup=False)


def resolve_question(cfg, selector, index):
    """Flip the `index`-th (1-based) question to RESOLVED, text unchanged. Out-of-range →
    ValueError with no write."""
    task = _require_idea(cfg, selector)
    items = read_idea_questions(cfg, task)
    if not isinstance(index, int) or not (1 <= index <= len(items)):
        raise ValueError(f"no question #{index} (idea has {len(items)})")
    items[index - 1]["state"] = "resolved"
    if _ensure_question_header(cfg, task.file):
        task = _require_idea(cfg, selector)
    return _replace_section_body(cfg, task, SECTION_QUESTIONS,
                                 _items_to_question_body(items), apply_markup=False)


def resolve_questions(cfg, selector, indices=None):
    """Flip several questions to RESOLVED in one write (SPEC-0080).

    `indices` is a collection of 1-based positions, or None for "every currently-open question".
    Validated as a set *before* anything is written: a bulk resolve that half-applied would be
    worse than one that refused. Already-resolved positions are accepted as no-ops, so
    `--resolve-all` is idempotent. Returns the number of questions actually flipped."""
    task = _require_idea(cfg, selector)
    items = read_idea_questions(cfg, task)

    if indices is None:
        wanted = [i for i, it in enumerate(items, 1) if it["state"] != "resolved"]
    else:
        wanted = sorted({int(i) for i in indices})
        bad = [i for i in wanted if not (1 <= i <= len(items))]
        if bad:
            raise ValueError(f"no question {', '.join(f'#{i}' for i in bad)} "
                             f"(idea has {len(items)})")
    flipped = 0
    for i in wanted:
        if items[i - 1]["state"] != "resolved":
            items[i - 1]["state"] = "resolved"
            flipped += 1
    if not flipped:
        return 0

    if _ensure_question_header(cfg, task.file):
        task = _require_idea(cfg, selector)
    _replace_section_body(cfg, task, SECTION_QUESTIONS,
                          _items_to_question_body(items), apply_markup=False)
    return flipped


def _write_questions(cfg, selector, task, items):
    """Shared tail for the index-addressed mutators: ensure the `#+TODO` header, re-resolve if
    that insert shifted line numbers, rewrite the section from `items`."""
    if _ensure_question_header(cfg, task.file):
        task = _require_idea(cfg, selector)
    return _replace_section_body(cfg, task, SECTION_QUESTIONS,
                                 _items_to_question_body(items), apply_markup=False)


def _question_at(cfg, selector, index):
    """`(task, items, index)` for a validated 1-based question index.

    Validation happens *before* any write, so a bad index leaves the file untouched — same
    contract as `resolve_question` / `resolve_questions`."""
    task = _require_idea(cfg, selector)
    items = read_idea_questions(cfg, task)
    if not isinstance(index, int) or isinstance(index, bool) or not (1 <= index <= len(items)):
        raise ValueError(f"no question #{index} (idea has {len(items)})")
    return task, items, index


def unresolve_question(cfg, selector, index):
    """Flip the `index`-th (1-based) question back to OPEN, text and priority unchanged
    (SPEC-0101). Out-of-range → ValueError with no write. Already-open is a no-op write."""
    task, items, index = _question_at(cfg, selector, index)
    items[index - 1]["state"] = "open"
    return _write_questions(cfg, selector, task, items)


def edit_question(cfg, selector, index, text):
    """Replace the `index`-th (1-based) question's text, preserving its state and priority
    cookie (SPEC-0101). Empty/whitespace text → ValueError with no write (use
    `delete_question` to remove one)."""
    if not (text or "").strip():
        raise ValueError("question text must not be empty (use delete_question to remove one)")
    task, items, index = _question_at(cfg, selector, index)
    items[index - 1]["text"] = text.strip()
    return _write_questions(cfg, selector, task, items)


def delete_question(cfg, selector, index):
    """Remove the `index`-th (1-based) question headline (SPEC-0101). Out-of-range → ValueError
    with no write. Deleting the last question leaves an empty section, not a missing one."""
    task, items, index = _question_at(cfg, selector, index)
    del items[index - 1]
    return _write_questions(cfg, selector, task, items)


def set_question_body(cfg, selector, index, text):
    """Set or clear the `index`-th (1-based) question's rationale body (SPEC-0112).

    The body is the prose SPEC-0111 taught the parser to preserve — the *why* behind a
    resolution, attached structurally to its question rather than linked from the Log by
    convention. Empty text clears it. Out-of-range index raises with no write.
    """
    task, items, index = _question_at(cfg, selector, index)
    body = (text or "").strip()
    if body:
        items[index - 1]["body"] = body
    else:
        items[index - 1].pop("body", None)
    return _write_questions(cfg, selector, task, items)


def set_question_priority(cfg, selector, index, priority):
    """Set/clear the `[#A|B|C]` cookie on the `index`-th (1-based) question (SPEC-0101).
    `priority=None` (or "") clears it. Invalid letter → ValueError with no write."""
    if priority in (None, ""):
        pri = None
    else:
        pri = str(priority).strip().upper()
        if pri not in ("A", "B", "C"):
            raise ValueError(f"invalid priority {priority!r}; expected A, B, or C")
    task, items, index = _question_at(cfg, selector, index)
    items[index - 1]["priority"] = pri
    return _write_questions(cfg, selector, task, items)


def explored_count(task) -> int:
    """`:EXPLORED:` as an int (SPEC-0081). Missing or garbage reads as 0, so a hand-edited file
    degrades to "this is pass 1" instead of raising inside a listing command."""
    raw = (task.properties.get("EXPLORED") or "").strip()
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def mark_explored(cfg, selector):
    """Record an exploration pass on an idea (SPEC-0081): increment `:EXPLORED:` and refresh
    `:EXPLORED_AT:`. Returns the new count.

    Written *only* from here. Unlike `:UPDATED:` (SPEC-0076), which bumps on every content and
    lifecycle write, this must bump on nothing implicit: `nw idea summary`/`questions`/`log` are
    the writes an exploration performs, but they are equally the writes *seeding* performs, so an
    implicit bump would make a seeded idea look explored and defeat the whole point."""
    task = _require_idea(cfg, selector)
    n = explored_count(task) + 1
    set_property(cfg, task, "EXPLORED", str(n))
    task = _require_idea(cfg, selector)          # the first write may have created the drawer
    set_property(cfg, task, "EXPLORED_AT", _inactive_stamp(cfg, with_time=True))
    return n


def open_question_count(cfg, task, *, lines=None) -> int:
    """How many of `task`'s questions are still open (SPEC-0080). Reuses the SPEC-0054 parser so
    there is one definition of "open"."""
    try:
        return sum(1 for q in read_idea_questions(cfg, task, lines=lines)
                   if q["state"] != "resolved")
    except Exception:
        return 0


def open_question_counts(cfg, tasks) -> dict[int, int]:
    """`{id(task): open question count}` for many ideas, reading each org file once (SPEC-0495).

    Same parser and same failure rule as `open_question_count` (an unreadable idea counts 0);
    only the file reads are shared. Keyed by `id(task)` because idea ids can repeat across
    rotated files."""
    cache: dict[str, list | None] = {}
    out: dict[int, int] = {}
    for task in tasks:
        path = task.file
        if path not in cache:
            try:
                cache[path] = open(path).read().splitlines(keepends=True)
            except OSError:
                cache[path] = None
        lines = cache[path]
        out[id(task)] = 0 if lines is None else open_question_count(cfg, task, lines=lines)
    return out


def normalize_idea_questions(cfg, selector=None) -> list[tuple[str, int]]:
    """Deterministically rewrite legacy Open-questions forms to `*** OPEN|RESOLVED` headlines
    (SPEC-0054). One idea (its file), or every idea across every idea-bearing file (SPEC-0074 —
    covers the active ideas file plus any rotated/archived files from SPEC-0071/0072). Lossless
    and idempotent; returns [(path, n_ideas_rewritten)] — one entry per file touched."""
    if selector:
        t = _require_idea(cfg, selector)
        file_groups = [(t.file, [t.properties.get("ID") or t.id])]
    else:
        from collections import defaultdict
        from .org import load_tasks
        by_file = defaultdict(list)
        for tk in load_tasks(cfg):
            if tk.is_idea:
                by_file[os.path.realpath(tk.file)].append(tk.properties.get("ID") or tk.id)
        file_groups = sorted(by_file.items())

    results = []
    for path, ids in file_groups:
        n = 0
        header_done = False
        for iid in ids:
            task = resolve_selector(cfg, iid)
            cur = read_idea_enrichment(cfg, task)["questions"]
            if not cur.strip():
                continue
            new_body = _items_to_question_body(
                _classify_question_lines(cur), normalize_text=False).strip()
            if new_body == cur.strip():
                continue                             # already normalized → idempotent no-op
            if not header_done:
                _ensure_question_header(cfg, task.file)
                header_done = True
                task = resolve_selector(cfg, iid)
            _replace_section_body(cfg, task, SECTION_QUESTIONS, new_body, apply_markup=False)
            n += 1
        results.append((path, n))
    return results


def normalize_plain_log_headlines(text: str) -> tuple[str, int]:
    """Rewrite plain `*** YYYY-MM-DD[ HH:MM]` Log heads to inactive org timestamps.

    Leaves already-bracketed heads untouched. Returns (new_text, n_rewritten).
    """
    out = []
    n = 0
    for line in text.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        core = line[:-1] if nl else line
        m = _PLAIN_LOG_HEAD_RE.match(core)
        if not m:
            out.append(line)
            continue
        stars, day_s, time_s = m.group(1), m.group(2), m.group(3)
        day = dt.datetime.strptime(day_s, "%Y-%m-%d")
        if time_s:
            hh, mm = time_s.split(":")
            day = day.replace(hour=int(hh), minute=int(mm))
            stamp = day.strftime("[%Y-%m-%d %a %H:%M]")
        else:
            stamp = day.strftime("[%Y-%m-%d %a]")
        out.append(f"{stars} {stamp}{nl}")
        n += 1
    return "".join(out), n


@org_writer
def normalize_log_stamps_in_file(cfg, path) -> int:
    """Normalize plain Log headlines in one org file; return count rewritten."""
    raw = open(path, encoding="utf-8").read()
    new, n = normalize_plain_log_headlines(raw)
    if n:
        _atomic_backup_write(cfg, path, new)
    return n


def normalize_idea_log_stamps(cfg, selector=None) -> list[tuple[str, int]]:
    """Normalize plain Log stamps for one idea (its file), or every idea-bearing file
    (SPEC-0074 — covers the active ideas file plus any rotated/archived files from
    SPEC-0071/0072).

    Returns list of (path, n_rewritten), one entry per file. Idempotent on already-inactive
    heads.
    """
    if selector:
        task = resolve_selector(cfg, selector)
        if not task.is_idea:
            raise ValueError(f"{selector!r} is not an idea")
        n = normalize_log_stamps_in_file(cfg, task.file)
        return [(task.file, n)]
    from .org import load_tasks
    paths = sorted({os.path.realpath(tk.file) for tk in load_tasks(cfg) if tk.is_idea})
    return [(p, normalize_log_stamps_in_file(cfg, p)) for p in paths]


@org_writer
def append_log(cfg, selector, note, *, when=None):
    """Append a Log entry; ensure skeleton; set state INCUBATE if still IDEA.

    Headline is an inactive org timestamp (SPEC-0032), e.g. `*** [2026-07-23 Thu 07:42]`.
    `when` may be None (now), a datetime, or an exact headline suffix string (for tests).
    """
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    if (task.state or "").upper() == "IDEA":
        active, done = file_keywords(cfg, task.file)
        if "INCUBATE" in set(active) | set(done):
            set_state(cfg, task, "INCUBATE", stamp_closed=False)
            task = resolve_selector(cfg, selector)

    if when is None or isinstance(when, dt.datetime):
        stamp = _inactive_stamp(cfg, when=when, with_time=True)
    else:
        stamp = str(when)
        if not (stamp.startswith("[") and stamp.endswith("]")):
            stamp = f"[{stamp}]"
    entry_title = f"*** {stamp}"
    entry_body = to_org_body(note.rstrip()) + "\n"

    lines = open(task.file).read().splitlines(keepends=True)
    head_idx, body_start, body_end = _idea_span(lines, task)
    content_start = _skip_meta(lines, body_start, body_end)
    lines, body_end = _ensure_skeleton(lines, content_start, body_end)
    content_start = _skip_meta(lines, body_start, body_end)
    sections = _find_sections(lines, content_start, body_end)
    _, b0, b1 = sections[SECTION_LOG]
    insert = [f"{entry_title}\n"]
    for ln in entry_body.splitlines(keepends=True):
        insert.append(ln if ln.endswith("\n") else ln + "\n")
    lines[b1:b1] = insert  # append at end of Log section
    stamp_updated(cfg, lines, head_idx, _stars(lines, head_idx))      # SPEC-0076, last
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


_CLOSE_OUTCOMES = {"drop": "DROPPED", "researched": "RESEARCHED"}


def newest_log_stamp(log_text: str):
    """The latest timestamp among a Log section's headlines, or None (SPEC-0076).

    Takes the maximum rather than the last line: Log entries are appended in order today, but a
    hand-edited or merged section can be out of order, and a backfill must not misdate an idea
    because of it. Parsing is `org_write.parse_inactive_stamp`, shared with the `:UPDATED:`
    property reader so the two can't drift (SPEC-0077)."""
    best = None
    for m in _INACTIVE_STAMP_RE.finditer(log_text or ""):
        when = parse_inactive_stamp(m.group(0))
        if when is not None and (best is None or when > best):
            best = when
    return best


def backfill_idea_stamps(cfg) -> list[tuple[str, str]]:
    """Give pre-SPEC-0076 ideas a best-effort `:UPDATED:`. Returns `[(idea_id, source)]` with
    source in `from-log` / `from-closed` / `skipped`.

    Derivation order: newest Log headline stamp, else the `CLOSED:` stamp, else skip — an idea
    with no time signal at all is left blank rather than invented. Idempotent: an idea that
    already carries `:UPDATED:` is never rewritten, so a second run reports nothing. Covers
    every file any idea lives in (rotated + archived, per SPEC-0074), not just
    `cfg['org_ideas_file']`."""
    from .org import filter_tasks, load_tasks

    out = []
    seen = set()
    while True:                                   # re-parse per write: line numbers shift
        todo = [t for t in filter_tasks(load_tasks(cfg), is_idea=True)
                if not (t.properties.get("UPDATED") or "").strip()
                and (t.properties.get("ID") or t.id) not in seen]
        if not todo:
            break
        task = sorted(todo, key=lambda t: (t.file, t.line))[0]
        idea_id = task.properties.get("ID") or task.id
        seen.add(idea_id)

        when = newest_log_stamp(read_idea_enrichment(cfg, task)["log"])
        source = "from-log"
        if when is None and task.closed:
            when = dt.datetime.combine(task.closed, dt.time())
            source = "from-closed"
        if when is None:
            out.append((idea_id, "skipped"))
            continue
        set_property(cfg, task, "UPDATED", _inactive_stamp(cfg, when=when, with_time=True))
        out.append((idea_id, source))
    return out


def _resolve_idea_ref(cfg, ref):
    """Canonicalize a pointer target: an idea selector → its `IDEA-NNN` id (must resolve);
    a `SPEC-NNNN`/outbound `PROJ-NNNN` id → verbatim (uppercased for SPEC). Raises if an idea
    ref does not resolve."""
    ref = (ref or "").strip()
    if re.match(r"^IDEA-\d+$", ref, re.I):
        return resolve_selector(cfg, ref).properties.get("ID") or ref.upper()
    if re.match(r"^[A-Za-z]+-\d+$", ref):        # SPEC-NNNN / PROJ-NNNN — accept verbatim
        return ref.upper() if ref.lower().startswith("spec-") else ref
    return resolve_selector(cfg, ref).properties.get("ID") or ref   # free-text → idea


def _close_log_note(outcome, reason, links):
    bits = [f"Closed as {outcome}."]
    if reason:
        bits.append(reason.strip())
    if links.get("FOLDED_INTO"):
        bits.append(f"Folded into [[{links['FOLDED_INTO']}]].")
    if links.get("SUPERSEDED_BY"):
        bits.append(f"Superseded by [[{links['SUPERSEDED_BY']}]].")
    return " ".join(bits)


def close_idea(cfg, selector, outcome, *, reason=None, folded_into=None, superseded_by=None):
    """Terminate an idea with an explicit outcome (SPEC-0055/0056/0057). `outcome` in
    {drop, researched} → DROPPED / RESEARCHED via set_state; records CLOSE_REASON and any
    FOLDED_INTO / SUPERSEDED_BY pointer as properties + a dated Log line. Pointers are resolved
    (and validated) before any write, so a bad target fails cleanly."""
    outcome = (outcome or "").lower()
    if outcome not in _CLOSE_OUTCOMES:
        raise ValueError(f"invalid outcome {outcome!r} (drop|researched)")
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    state = _CLOSE_OUTCOMES[outcome]

    links = {}
    if folded_into:
        links["FOLDED_INTO"] = _resolve_idea_ref(cfg, folded_into)
    if superseded_by:
        links["SUPERSEDED_BY"] = _resolve_idea_ref(cfg, superseded_by)

    if ensure_idea_keyword(cfg, task.file, state):      # make the keyword settable first
        task = resolve_selector(cfg, selector)
    set_state(cfg, task, state)
    task = resolve_selector(cfg, selector)
    if reason:
        set_property(cfg, task, "CLOSE_REASON", " ".join(reason.split()))
        task = resolve_selector(cfg, selector)
    for key, ref in links.items():
        set_property(cfg, task, key, ref)
        task = resolve_selector(cfg, selector)
    append_log(cfg, selector, _close_log_note(outcome, reason, links))
    return task.file


def set_idea_state(cfg, selector, new_state):
    """Force an idea's lifecycle keyword (SPEC-0135). Bare flip — no CLOSE_REASON.

    Ensures the keyword exists on the idea file's `#+TODO` line, then `set_state`. Used by
    `nw idea state` and the TUI metadata state picker; not a substitute for `close_idea` when
    drop/researched + reason/pointers are wanted.
    """
    new_state = (new_state or "").strip().upper()
    if not new_state:
        raise ValueError("state is empty")
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    if ensure_idea_keyword(cfg, task.file, new_state):
        task = resolve_selector(cfg, selector)
    set_state(cfg, task, new_state)
    return task.file


@org_writer
def retitle_idea(cfg, selector, new_title):
    """Rewrite the idea headline text; keep stars, state, tags, properties."""
    task = resolve_selector(cfg, selector)
    if not task.is_idea:
        raise ValueError(f"{selector!r} is not an idea")
    new_title = " ".join(str(new_title).split())
    if not new_title:
        raise ValueError("title is empty")
    lines = open(task.file).read().splitlines(keepends=True)
    idx = _locate_headline(task, lines)
    m = _HEADLINE_RE.match(lines[idx].rstrip("\n"))
    if not m:
        raise ValueError("not a headline")
    stars, gap, rest = m.group(1), m.group(2), m.group(3)
    tm = re.search(r"\s(:(\w+:)+)\s*$", rest)
    tag_part = f" {tm.group(1)}" if tm else ""
    newline = "\n" if lines[idx].endswith("\n") else ""
    if task.state:
        lines[idx] = f"{stars}{gap}{task.state} {new_title}{tag_part}".rstrip() + newline
    else:
        lines[idx] = f"{stars}{gap}{new_title}{tag_part}".rstrip() + newline
    stamp_updated(cfg, lines, idx, stars)                              # SPEC-0076
    _atomic_backup_write(cfg, task.file, "".join(lines))
    return task.file


def idea_show_payload(cfg, task) -> dict:
    """nw.idea.v1 envelope (SPEC-0026 + SPEC-0042 research + SPEC-0044 kind).
    Omits empty project/spec; always includes resolved `kind`."""
    from .org_write import idea_kind
    from .rules import resolve_research_context

    en = read_idea_enrichment(cfg, task)
    out = {
        "schema": "nw.idea.v1",
        "id": task.properties.get("ID") or task.id,
        "state": task.state or "",
        "heading": task.heading,
        "tags": sorted(task.tags),
        "kind": idea_kind(task),
        "summary": en["summary"],
        "questions": en["questions"],
        "question_items": _classify_question_lines(en["questions"]),
        "log": en["log"],
    }
    proj = task.properties.get("PROJECT") or ""
    if proj:
        out["project"] = proj
    spec = task.properties.get("SPEC") or ""
    if spec:
        out["spec"] = spec
    ws = (task.properties.get("WORKSTREAM") or "").strip()
    if ws:
        out["workstream"] = ws
    handed = (task.properties.get("HANDED_OFF") or "").strip()      # SPEC-0280
    if handed:
        out["handed_off"] = handed
    if task.priority:
        out["priority"] = task.priority
    for prop_key, out_key in (("CLOSE_REASON", "close_reason"),
                              ("FOLDED_INTO", "folded_into"),
                              ("SUPERSEDED_BY", "superseded_by"),     # SPEC-0056/0057
                              ("EXPLORED_AT", "explored_at")):        # SPEC-0081
        v = task.properties.get(prop_key)
        if v:
            out[out_key] = v
    if explored_count(task):                                          # SPEC-0081, int not str
        out["explored"] = explored_count(task)
    out.update(clock_fields(task))                                    # SPEC-0103
    out["open_questions"] = sum(1 for q in out["question_items"]      # SPEC-0103
                                if q.get("state") != "resolved")
    out["research"] = resolve_research_context(cfg, proj or None)
    from .idea_ext import read_idea_extensions
    exts = read_idea_extensions(cfg, task)
    if exts:
        out["extensions"] = exts
    return out


def clock_fields(task) -> dict:
    """Additive clock signals for `nw.idea.v1` / row envelopes (SPEC-0103).

    Shared by agents and the TUI — epic Q3 ruled these belong in JSON, not a TUI-only `Task`
    read. `clock_open` says a session is running *now* (an entry with no end); `clocked_hours`
    sums only **closed** intervals, matching `report._clocked_hours`, so an open session never
    inflates a total. Both are always present: an absent key would be ambiguous with "no clock".
    """
    entries = list(getattr(task, "clock", ()) or ())
    closed = [(s, e) for s, e in entries if e is not None]
    return {
        "clock_open": any(e is None for _s, e in entries),
        "clocked_hours": round(sum((e - s).total_seconds() for s, e in closed) / 3600.0, 4),
    }


def dump_idea_show_json(cfg, task):
    """Print nw.idea.v1 to stdout."""
    import json
    import sys

    json.dump(idea_show_payload(cfg, task), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _format_research_line(research: dict) -> str:
    """Plain-show one-liner for SPEC-0042 research context."""
    root = research.get("root")
    source = research.get("source") or "none"
    if root:
        return f"  research: {root} ({source})"
    note = research.get("note") or "no research root"
    return f"  research: (none) — {note}"


def format_idea_show(cfg, task) -> str:
    """Human-readable show output for an idea + enrichment."""
    payload = idea_show_payload(cfg, task)
    parts = [
        f"{payload['id']}  {payload['state'] or '·'}  {payload['heading']}",
    ]
    parts.append(f"  kind: {payload.get('kind') or 'idea'}")
    if payload.get("explored"):                                       # SPEC-0081
        n = payload["explored"]
        at = payload.get("explored_at")
        parts.append(f"  explored: {n} pass{'es' if n != 1 else ''}"
                     + (f" (latest {at})" if at else ""))
    if payload.get("project"):
        parts.append(f"  project: {payload['project']}")
    if payload.get("spec"):
        parts.append(f"  spec: {payload['spec']}")
    if payload.get("research"):
        parts.append(_format_research_line(payload["research"]))
    if payload.get("folded_into"):
        parts.append(f"  → folded into [[{payload['folded_into']}]]")
    if payload.get("superseded_by"):
        parts.append(f"  → superseded by [[{payload['superseded_by']}]]")
    if payload.get("close_reason"):
        parts.append(f"  close reason: {payload['close_reason']}")
    parts.append("")
    parts.append("** Summary")
    parts.append(payload["summary"] or "(empty)")
    parts.append("")
    parts.append("** Open questions")
    qitems = payload.get("question_items") or []
    if qitems:
        for it in qitems:
            kw = "RESOLVED" if it["state"] == "resolved" else "OPEN"
            cookie = f"[#{it['priority']}] " if it.get("priority") else ""
            parts.append(f"*** {kw} {cookie}{it['text']}".rstrip())
            # SPEC-0112: the rationale, indented under its question. The data reached the JSON
            # with SPEC-0111; this renderer was simply dropping it.
            for line in (it.get("body") or "").splitlines():
                parts.append(f"    {line}" if line.strip() else "")
    else:
        parts.append("(empty)")
    parts.append("")
    parts.append("** Log")
    parts.append(payload["log"] or "(empty)")
    return "\n".join(parts) + "\n"


def idea_show_use_pretty(*, plain: bool = False, as_json: bool = False,
                         is_terminal: bool | None = None) -> bool:
    """SPEC-0035: pretty Rich+Pygments show unless --json, --plain, or non-TTY."""
    if as_json or plain:
        return False
    if is_terminal is None:
        from .console import console
        is_terminal = bool(console.is_terminal)
    return bool(is_terminal)


def idea_show_theme(cfg) -> str:
    """SPEC-0036: Pygments theme for pretty idea show; blank/missing → nord."""
    raw = cfg.get("idea_show_theme") if cfg else None
    if raw is None:
        return "nord"
    theme = str(raw).strip()
    return theme or "nord"


def print_idea_show(cfg, task, *, plain: bool = False, console=None) -> None:
    """Print idea show: plain text, or Rich Syntax(org) when pretty mode applies."""
    import sys

    text = format_idea_show(cfg, task)
    if console is None:
        from .console import console as console
    if not idea_show_use_pretty(plain=plain, is_terminal=bool(console.is_terminal)):
        sys.stdout.write(text)
        return
    from rich.syntax import Syntax
    # background_color="default" keeps the terminal bg (avoids a painted theme panel).
    console.print(Syntax(
        text, "org", theme=idea_show_theme(cfg),
        word_wrap=True, background_color="default",
    ))


# --- Markdown Clock Log parsing (SPEC-0063) ---------------------------------------------

_CLOCK_IN_RE = re.compile(r"^CLOCK-IN:\s*(.+?)\s*$")
_CLOCK_OUT_RE = re.compile(r"^CLOCK-OUT:\s*(.+?)\s*$")
_ORG_WEEKDAY_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+[A-Za-z]+\.?\s+(\d{2}:\d{2}(?::\d{2})?)$"
)


def _strip_brackets(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    return raw


_PLACEHOLDER_TOKENS = {"timestamp"}


def _parse_clock_timestamp(raw: str) -> dt.datetime | None:
    """Parse a Clock Log timestamp, tolerating org-mode's `YYYY-MM-DD Day HH:MM` form (a
    weekday abbreviation between date and time) in addition to plain ISO 8601 — agents
    following `/nw-implement-spec`'s `CLOCK-IN: [timestamp]` guidance write org-style
    timestamps in practice (bug found in real usage against EXAMPLE-0026/IDEA-212: the
    weekday token made `datetime.fromisoformat` raise, and `nw spec sweep` silently
    swallowed it as `+0 clock` instead of surfacing the failure). Returns `None` for the
    guidance's own unfilled `[timestamp]` placeholder (found left verbatim in several real
    portable specs, e.g. DEMO-0001 — a template that was never actually used, not
    a parse error worth surfacing)."""
    text = _strip_brackets(raw)
    if text.lower() in _PLACEHOLDER_TOKENS:
        return None
    m = _ORG_WEEKDAY_RE.match(text)
    if m:
        text = f"{m.group(1)} {m.group(2)}"
    return dt.datetime.fromisoformat(text)


def parse_clock_log(text: str) -> list[tuple[dt.datetime, dt.datetime]]:
    """Parse a portable spec's `## Clock Log` body (SPEC-0062's `CLOCK-IN:`/`CLOCK-OUT:`
    convention) into complete `(start, end)` pairs, in order. Pairs sequentially; an unmatched
    trailing `CLOCK-IN` (session still open) is skipped — nothing to reconcile yet, not an
    error. Timestamps are ISO 8601 (`datetime.fromisoformat`), with a tolerant fallback for
    org-mode's `YYYY-MM-DD Day HH:MM` form; the guidance's own example wraps them in `[...]`
    but agents in practice also write them plain (bug found in real usage against
    DEMO-0001 — brackets silently made every line invisible to a stricter,
    bracket-required regex) — both forms are accepted here. An unfilled `[timestamp]`
    placeholder (the guidance's own literal example, sometimes left verbatim) resets any
    pending `CLOCK-IN` and is skipped, not parsed."""
    pairs: list[tuple[dt.datetime, dt.datetime]] = []
    pending_start: dt.datetime | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        m_in = _CLOCK_IN_RE.match(line)
        if m_in:
            pending_start = _parse_clock_timestamp(m_in.group(1))
            continue
        m_out = _CLOCK_OUT_RE.match(line)
        if m_out and pending_start is not None:
            end = _parse_clock_timestamp(m_out.group(1))
            if end is not None:
                pairs.append((pending_start, end))
            pending_start = None
    return pairs
