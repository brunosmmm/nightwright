"""Org ↔ Markdown body helpers (SPEC-0030)."""
import pytest

from wt.org_markup import from_org_body, to_org_body


def test_to_org_backticks_and_bold():
    assert to_org_body("use `wt-native` here") == "use ~wt-native~ here"
    assert to_org_body("This is **not** inventing") == "This is *not* inventing"
    assert to_org_body("also __bold__ ok") == "also *bold* ok"


def test_to_org_strips_atx_and_converts_fences():
    assert to_org_body("## Summary title\n") == "Summary title\n"
    src = "before\n```bash\necho hi\n```\nafter\n"
    # SPEC-0164: fences become org src blocks instead of being unwrapped
    assert to_org_body(src) == "before\n#+begin_src bash\necho hi\n#+end_src\nafter\n"


def test_to_org_idempotent_on_org():
    org = "use ~wt-native~ and *not* inventing\n"
    assert to_org_body(org) == org


def test_from_org_preserves_headlines():
    text = "** Summary\nuse ~code~ and *bold* here\n"
    out = from_org_body(text)
    assert out.startswith("** Summary\n")
    assert "`code`" in out
    assert "**bold**" in out


def test_from_org_verbatim():
    assert "`x`" in from_org_body("=x=\n")


# --- SPEC-0164: span-aware normalization -------------------------------------------------

TO_ORG = [
    # code / verbatim content is byte-identical
    ("=Path(__file__)=", "=Path(__file__)="),
    ("`Path(__file__)`", "~Path(__file__)~"),
    ("~__init__~", "~__init__~"),
    ("see `**kwargs` here", "see ~**kwargs~ here"),
    # a `~` in backtick content falls back to =verbatim=; both `~` and `=` keep the backticks
    ("`a~b`", "=a~b="),
    ("`a~b=c`", "`a~b=c`"),
    # no intraword `__` bold, no lone `**kwargs` bold
    ("snake__case__name", "snake__case__name"),
    ("f(*args, **kwargs) and g(**kw)", "f(*args, **kwargs) and g(**kw)"),
    # documented CommonMark-faithful remainder: a bare dunder word still bolds
    ("plain __init__.py file", "plain *init*.py file"),
    # happy paths carried forward from SPEC-0030
    ("use `wt-native` here", "use ~wt-native~ here"),
    ("This is **not** inventing", "This is *not* inventing"),
    ("also __bold__ ok", "also *bold* ok"),
    ("**see `x`**", "*see ~x~*"),
    ("### Heading `x`", "Heading ~x~"),
]

FROM_ORG = [
    ("a = b = c", "a = b = c"),
    ("2*3*4", "2*3*4"),
    ("x *bold* y", "x **bold** y"),
    ("use ~code~ and *bold* here", "use `code` and **bold** here"),
    ("=Path(__file__)=", "`Path(__file__)`"),
    ("~*not bold*~", "`*not bold*`"),
    ("(*bold*), next", "(**bold**), next"),
    # documented org-faithful remainder: org itself reads this as one code span
    ("path ~/foo and ~/bar~", "path `/foo and ~/bar`"),
    ("** Summary", "** Summary"),
]

FENCED = "intro\n```python\ndef __init__(self):\n    x = a**b  # **not bold**\n* star\n```\nout\n"
BARE_FENCE = "```\nplain __text__\n```\n"
CORPUS = [src for src, _ in TO_ORG] + [FENCED, BARE_FENCE, "```py\nunterminated `x`\n"]


@pytest.mark.parametrize("src,want", TO_ORG)
def test_to_org_table(src, want):
    assert to_org_body(src) == want


@pytest.mark.parametrize("src,want", FROM_ORG)
def test_from_org_table(src, want):
    assert from_org_body(src) == want


def test_fence_to_src_block_verbatim_and_escaped():
    out = to_org_body(FENCED)
    assert out == ("intro\n#+begin_src python\ndef __init__(self):\n"
                   "    x = a**b  # **not bold**\n,* star\n#+end_src\nout\n")
    assert to_org_body(BARE_FENCE) == "#+begin_example\nplain __text__\n#+end_example\n"


def test_unterminated_fence_is_prose():
    assert to_org_body("```py\nunterminated `x`\n") == "```py\nunterminated ~x~\n"


def test_existing_org_block_passes_through():
    org = "#+begin_src sh\n__x__ `y` **z**\n#+end_src\n"
    assert to_org_body(org) == org


def test_src_block_to_fence_unescapes():
    org = "#+BEGIN_SRC python :results output\n,* star\n,,#+kw\n#+END_SRC\n"
    assert from_org_body(org) == "```python\n* star\n,#+kw\n```\n"


@pytest.mark.parametrize("md", ["use `code` here\n", "`a~b`\n", FENCED, BARE_FENCE])
def test_round_trip_code_spans_and_fences(md):
    assert from_org_body(to_org_body(md)) == md


@pytest.mark.parametrize("src", CORPUS)
def test_to_org_idempotent(src):
    once = to_org_body(src)
    assert to_org_body(once) == once
