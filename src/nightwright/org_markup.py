"""Light Markdown ↔ org markup helpers for idea enrichment (SPEC-0030, SPEC-0164).

Not a full pandoc: only the common agent idioms that otherwise pollute ideas.org.

SPEC-0164: conversions are span-aware. Fenced blocks ↔ `#+begin_src`/`#+begin_example`
blocks are cut out first (content verbatim, org comma-escaped), then per line the code /
verbatim spans are masked behind placeholders before any emphasis rewrite, and each emphasis
rewrite checks its source dialect's border rules — so code, paths and identifiers survive.
"""
from __future__ import annotations

import re

# Markdown emphasis (CommonMark-ish flanking): `**x**` closes only when not followed by a word
# char (keeps `f(*args, **kwargs)` intact); `__x__` never opens/closes inside a word
# (keeps `snake__case__name` intact).
_MD_BOLD_STAR_RE = re.compile(r"\*\*(?=[^\s*])([^*\n]*?[^\s*])\*\*(?![\w*])")
_MD_BOLD_UNDER_RE = re.compile(r"(?<!\w)__(?=[^\s_])([^_\n]*?[^\s_])__(?!\w)")
# ATX headings at line start
_ATX_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*)$")
# Fenced code blocks (line-based): opener carries an optional info-string language
_FENCE_OPEN_RE = re.compile(r"^ {0,3}```[ \t]*([^\s`]*)[^`]*$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}```[ \t]*$")
# Org blocks: `#+begin_NAME [lang …]` … `#+end_NAME`
_ORG_BLOCK_OPEN_RE = re.compile(r"^[ \t]*#\+begin_(\w+)(?:[ \t]+(\S+))?.*$", re.IGNORECASE)
# Org's in-block escape (org-escape-code-in-string): a leading `*` or `#+` gets a `,` so it is
# not read as a headline / keyword; already-escaped lines gain one more comma.
_ORG_ESCAPE_RE = re.compile(r"^([ \t]*)(,*(?:\*|#\+))")
_ORG_UNESCAPE_RE = re.compile(r"^([ \t]*),(,*(?:\*|#\+))")

# Org emphasis border rules (Org manual, "Emphasis and Monospace")
_ORG_PRE = " \t-('\"{"
_ORG_POST = " \t-.,;:!?')\"}[\\"
_ORG_BOLD_RE = re.compile(
    r"(?:^|(?<=[\s\-('\"{]))\*(?=[^\s*])([^*\n]*?[^\s*])\*(?=$|[\s\-.,;:!?')\"}\[\\])")
_ORG_HEADLINE_RE = re.compile(r"^\*+\s")
# Org drawers (`:PROPERTIES:`, `:LOGBOOK:`, …) and planning lines (SPEC-0195)
_DRAWER_OPEN_RE = re.compile(r"^\s*:(?!END:)[A-Z_]+:\s*$")
_DRAWER_END_RE = re.compile(r"^\s*:END:\s*$")
_PLANNING_RE = re.compile(r"^\s*(?:SCHEDULED|DEADLINE|CLOSED):")

# Placeholders for masked spans: no `*`, `_`, `~`, `=` or backtick, so no rewrite can reach in.
_PH_RE = re.compile("\x00(\\d+)\x01")


def _lines(text):
    """[(core, nl)] per line; `nl` is "\\n" or "" and a trailing CR is dropped."""
    out = []
    for line in text.splitlines(keepends=True):
        nl = "\n" if line.endswith("\n") else ""
        core = line[:-1] if nl else line
        if core.endswith("\r"):
            core = core[:-1]
        out.append((core, nl))
    return out


def _block_end(lines, start, end_re):
    """Index of the first line at/after `start` matching `end_re`, or None (unterminated)."""
    for k in range(start, len(lines)):
        if end_re.match(lines[k][0]):
            return k
    return None


def _org_end_re(name):
    return re.compile(rf"^[ \t]*#\+end_{re.escape(name)}[ \t]*$", re.IGNORECASE)


def _org_span_end(core, i, marker):
    """Index of the closing `marker` of an org `~x~` / `=x=` span opening at `i`, or None if
    org's border rules say no span starts there."""
    if i > 0 and core[i - 1] not in _ORG_PRE:
        return None
    if i + 1 >= len(core) or core[i + 1].isspace():
        return None
    j = core.find(marker, i + 2)
    while j != -1:
        if not core[j - 1].isspace() and (j + 1 == len(core) or core[j + 1] in _ORG_POST):
            return j
        j = core.find(marker, j + 1)
    return None


def _protect(core, span_at):
    """Mask every span `span_at(core, i) -> (end, replacement) | None` finds, left to right.
    Returns (masked line, stash of replacements)."""
    stash, out, i = [], [], 0
    while i < len(core):
        hit = span_at(core, i)
        if hit:
            end, repl = hit
            out.append(f"\x00{len(stash)}\x01")
            stash.append(repl)
            i = end + 1
        else:
            out.append(core[i])
            i += 1
    return "".join(out), stash


def _restore(core, stash):
    return _PH_RE.sub(lambda m: stash[int(m.group(1))], core)


def _md_span_at(core, i):
    """Markdown backtick span → org code (`~x~`, or `=x=` if x has a `~`; left as backticks if
    it has both); existing org `~x~` / `=x=` spans are kept as-is. Both are masked."""
    c = core[i]
    if c == "`":
        j = core.find("`", i + 1)
        if j <= i + 1:
            return None
        x = core[i + 1:j]
        if "~" not in x:
            return j, f"~{x}~"
        if "=" not in x:
            return j, f"={x}="
        return j, core[i:j + 1]
    if c in "~=":
        j = _org_span_end(core, i, c)
        if j is not None:
            return j, core[i:j + 1]
    return None


def _org_span_at(core, i):
    """Org `~x~` / `=x=` span → Markdown backtick span (masked)."""
    c = core[i]
    if c in "~=":
        j = _org_span_end(core, i, c)
        if j is not None:
            return j, f"`{core[i + 1:j]}`"
    return None


def _line_to_org(core):
    m = _ATX_RE.match(core)
    if m:
        core = m.group(1)
    core, stash = _protect(core, _md_span_at)
    core = _MD_BOLD_STAR_RE.sub(r"*\1*", core)
    core = _MD_BOLD_UNDER_RE.sub(r"*\1*", core)
    return _restore(core, stash)


def _line_to_md(core):
    core, stash = _protect(core, _org_span_at)
    core = _ORG_BOLD_RE.sub(r"**\1**", core)
    return _restore(core, stash)


def to_org_body(text: str) -> str:
    """Convert common Markdown idioms in enrichment prose to org-native markup."""
    if not text:
        return text
    lines = _lines(text)
    out, i = [], 0
    while i < len(lines):
        core, nl = lines[i]
        m = _FENCE_OPEN_RE.match(core)
        end = _block_end(lines, i + 1, _FENCE_CLOSE_RE) if m else None
        if end is not None:
            lang = m.group(1)
            name = "src" if lang else "example"
            out.append(f"#+begin_{name}{' ' + lang if lang else ''}\n")
            out.extend(_ORG_ESCAPE_RE.sub(r"\1,\2", c) + n for c, n in lines[i + 1:end])
            out.append(f"#+end_{name}{lines[end][1]}")
            i = end + 1
            continue
        m = _ORG_BLOCK_OPEN_RE.match(core)
        end = _block_end(lines, i + 1, _org_end_re(m.group(1))) if m else None
        if end is not None:
            out.extend(c + n for c, n in lines[i:end + 1])
            i = end + 1
            continue
        out.append(_line_to_org(core) + nl)
        i += 1
    return "".join(out)


def from_org_body(text: str) -> str:
    """Convert common org emphasis to Markdown for spec Context prefills.

    Org headlines (`*`, `**`, …) are left untouched so `** Summary` survives promote.
    `#+begin_src`/`#+begin_example` blocks become fences (content unescaped, verbatim).
    """
    if not text:
        return text
    lines = _lines(text)
    out, i = [], 0
    while i < len(lines):
        core, nl = lines[i]
        m = _ORG_BLOCK_OPEN_RE.match(core)
        kind = m.group(1).lower() if m else None
        end = (_block_end(lines, i + 1, _org_end_re(kind))
               if kind in ("src", "example") else None)
        if end is not None:
            lang = (m.group(2) or "") if kind == "src" else ""
            out.append(f"```{lang}\n")
            out.extend(_ORG_UNESCAPE_RE.sub(r"\1\2", c) + n for c, n in lines[i + 1:end])
            out.append(f"```{lines[end][1]}")
            i = end + 1
            continue
        if _ORG_HEADLINE_RE.match(core):
            out.append(core + nl)
        else:
            out.append(_line_to_md(core) + nl)
        i += 1
    return "".join(out)


def strip_drawers(text: str) -> str:
    """Drop org drawers (`:NAME:` … `:END:`) and planning lines (`SCHEDULED:`, `DEADLINE:`,
    `CLOSED:`) from an idea body (SPEC-0195).

    Lines inside terminated `#+begin_…` blocks are never touched, and an unterminated drawer
    is left in place rather than eating the rest of the body.
    """
    if not text:
        return text
    lines = _lines(text)
    out, i = [], 0
    while i < len(lines):
        core, nl = lines[i]
        m = _ORG_BLOCK_OPEN_RE.match(core)
        end = _block_end(lines, i + 1, _org_end_re(m.group(1))) if m else None
        if end is not None:
            out.extend(c + n for c, n in lines[i:end + 1])
            i = end + 1
            continue
        if _DRAWER_OPEN_RE.match(core):
            end = _block_end(lines, i + 1, _DRAWER_END_RE)
            if end is not None:
                i = end + 1
                continue
        elif _PLANNING_RE.match(core):
            i += 1
            continue
        out.append(core + nl)
        i += 1
    return "".join(out)
