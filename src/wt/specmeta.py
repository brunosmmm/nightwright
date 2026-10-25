"""Tiny, dependency-light spec-markdown parsing: split YAML frontmatter from the body, and
map `## Heading` -> section text. Lives in the package (not tools/spec_lint.py) so the
installed `wt` never has to reach into the repo's dev tooling — which pulls in dev-only deps
like jsonschema — at runtime. `tools/spec_lint.py` keeps its own copies to stay standalone."""
import re

import yaml

_FENCE_LINE_RE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_PLACEHOLDER_RES = (
    (re.compile(r"^<The chosen approach"), "a line starting '<The chosen approach'"),
    (re.compile(r"^\s*-\s*…\s*$"), "a bullet that is only '- …'"),
    (re.compile(r"<option>"), "'<option>'"),
    (re.compile(r'^\s*#\s*milestone:\s*"M\?'), "a '# milestone: \"M?' comment line"),
)


def split_frontmatter(text):
    """(frontmatter_dict_or_None, body). A file without a `---`-delimited block -> (None, text)."""
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        return None, text
    return yaml.safe_load(m.group(1)), m.group(2)


def sections(body):
    """Map '## Heading' -> section text (until the next '## ')."""
    out, cur, buf = {}, None, []
    for line in body.splitlines():
        h = re.match(r"^##\s+(.*?)\s*$", line)
        if h:
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = h.group(1), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def first_placeholder(text):
    """(line number, description) of the first leftover template placeholder in a spec file,
    or None. Fenced code blocks and inline code spans are ignored, so a spec may talk about
    the placeholders it forbids. Same rule as `tools/spec_lint.py` (a test keeps them equal)."""
    fenced = False
    for n, line in enumerate(text.splitlines(), 1):
        if _FENCE_LINE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = _INLINE_CODE_RE.sub("", line)
        for rx, what in _PLACEHOLDER_RES:
            if rx.search(line):
                return n, what
    return None
