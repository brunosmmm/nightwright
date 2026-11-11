"""SPEC-0546: the read-only main_dirty classifier `plan_clear` and `nw queue main-dirty-plan`."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nightwright.agentic import close as CL
from nightwright.agentic import main_dirty as MD
from nightwright.agentic import project as P
from nightwright.cli import cli

pytestmark = pytest.mark.fast

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def _put(repo, rel, content):
    p = Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8", "surrogateescape"))


def _repo(tmp_path, files=None, base="main", project=True, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", base)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    if project:
        _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T",
                                                       "base": base}))
    _put(repo, "README.md", "# hi\n")
    for rel, content in (files or {}).items():
        _put(repo, rel, content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _dirty(repo):
    return _git(repo, "status", "--porcelain", "--untracked-files=no").stdout


def _cli(*args, env=None):
    return CliRunner().invoke(cli, ["queue", "main-dirty-plan", *args], env=env)


# ---- case table: single-file edits (AC1, AC2) -------------------------------------------------

FENCE = "# T\n\nprose\n\n```\ncode\n```\n\nafter\n"
LOGIC = ("x = 1\n", "x = 2\n")
T, B, S = ("trivial", "commit"), ("branch", "save_branch"), ("substantive", "branch_and_escalate")

# id: (edits {path: (old, new|None)}, (class, action), file_class or None)
CASES = {
    "C01": ({"docs/specs/0001-x.md": ("---\nstatus: a\n---\n\n## C\n\nx\n",
                                      "---\nstatus: b\n---\n\n## C\n\nx\n")}, T, "frontmatter"),
    "C02": ({"docs/a.md": ("# T\n\nprose\n", "# T\n\nprose 2\n")}, T, "docs"),
    "C03": ({"README.md": ("# hi\n", "# hi\n\nmore\n")}, T, None),
    "C04": ({"CHANGELOG.md": ("# c\n\n- a\n", "# c\n\n- b\n")}, T, None),
    "C05": ({"src/x.py": ("x = 1\n# old\n", "x = 1\n# new\n")}, T, "comment"),
    "C06": ({"src/star_comment.c": ("/*\n * old note\n */\nint x;\n",
                                    "/*\n * kept comment\n */\nint x;\n")}, T, "comment"),
    "C07": ({"src/x.py": ("def a():\n    pass\ndef b():\n    pass\n",
                           "def a():\n    pass\n\ndef b():\n    pass\n")}, T, "comment"),
    "C08": ({"docs/LEDGER.md": ("| id |\n", "| id |\n\n")}, B, None),
    "C09": ({"docs/a.md": (FENCE, FENCE.replace("\ncode\n", "\ncode2\n"))}, B, None),
    "C10": ({"docs/a.md": (FENCE, FENCE.replace("\ncode\n", "\ncode\n\n"))}, B, None),
    "C11": ({"docs/a.md": ("# T\n\n```\ncode\nmore\n", "# T\n\n```\ncode\nmore2\n")}, B, None),
    "C12": ({"docs/a.md": ("# T\n\n```\ncode\n~~~\nx\n```\n", "# T\n\n```\ncode\n~~~\ny\n```\n")}, B, None),
    "C13": ({"docs/specs/x.md": ("---\nstatus: a\n\nbody\n", "---\nstatus: b\n\nbody\n")}, B, "other"),
    "C14": ({"config/x.json": ('{"a": 1}\n', '{"a": 1}\n\n')}, B, None),
    "C15": ({"skills/x.md": ("---\nname: x\n---\nbody\n", "---\nname: x\n---\nbody 2\n")}, B, None),
    "C16": ({"docs/a.md": ("# T\n", None)}, B, None),
    "C17": ({"assets/x.bin": (b"\0\x01", b"\0\x02")}, B, None),
    "C18": ({"docs/a.md": ("prose\n", "pro\0se\n")}, B, None),
    "C19": ({".claude/agents/x.md": ("---\nname: a\n---\nbody\n", "---\nname: b\n---\nbody\n")}, B, "other"),
    "C20": ({"AGENTS.md": ("# A\n\ntext\n", "# A\n\n\ntext\n")}, B, "other"),
    "C21": ({"src/x.c": ("int f(){\n  g();\n}\n", "int f(){\n/*\n  g();\n*/\n}\n")}, S, None),
    "C22": ({"web/x.css": ("a { b: c; }\n", "/*\na { b: c; }\n*/\n")}, B, None),
    "C23": ({"site/x.html": ("<p>x</p>\n", "<!--\n<p>x</p>\n-->\n")}, B, None),
    "C24": ({"src/x.py": ('x = 1\n"""\ndoc\n"""\n', 'x = 1\n"""\n# x\ndoc\n"""\n')}, S, None),
    "C25": ({"src/x.py": ('x = 1\n"""\ndoc\n"""\n', 'x = 1\n"""\n\ndoc\n"""\n')}, S, None),
    "C26": ({"src/x.sh": ("cat <<EOF\nbody\nEOF\n", "cat <<EOF\n# x\nbody\nEOF\n")}, S, None),
    "C27": ({".github/ci.yml": ("run: |\n  echo a\n", "run: |\n  # x\n  echo a\n")}, B, None),
    "C28": ({"src/star_deref.c": ("int *p;\n", "int *p;\n*ptr = 1;\n")}, S, None),
    "C29": ({"src/mul.c": ("x = a\n", "x = a\n    * b;\n")}, S, None),
    "C30": ({"src/x.py": LOGIC, "docs/a.md": ("# T\n\nprose\n", "# T\n\nprose 2\n")}, S, None),
    "C31": ({"src/x.py": ("x = 1\n", None)}, S, None),
    "C32": ({"src/x.bin": (b"\0\x01", b"\0\x02")}, S, None),
    "C33": ({"x.py": LOGIC}, S, None),
    "C34": ({"lib/x.rb": LOGIC}, S, None),
    "C35": ({"tests/test_x.py": LOGIC}, S, None),
    "C36": ({"skills/x/run.py": LOGIC}, S, None),
    "C39": ({"bin/x.py": ("#!/usr/bin/python\nx = 1\n", "#!/usr/bin/python3\nx = 1\n")}, S, None),
    "S20": ({"src/x.py": ("x = 1\n", "x = 1\n# note\rimport os\n")}, S, "code"),
    "S21": ({"src/x.js": ("f();\n", "f();\n// note\x0cfoo();\n")}, S, "code"),
    "S22": ({"src/x.py": ("x = 1\n", "x = 1\n\x0c\n")}, T, "comment"),
    "K01": ({"src/x.py": ("def f():\n    x = 1\n", "def f():\nx = 1\n")}, S, "code"),
    "K02": ({"src/x.sh": ("if a; then\n  x\nfi\n", "if a; then\nx\nfi\n")}, S, "code"),
    "K03": ({"src/x.py": ("a = 1\n\n# c\nb = 2\n", "\n# c\na = 1\nb = 2\n")}, T, "comment"),
    "K04": ({"docs/a.md": ("# T\n\n<!-- x -->\n\nprose\n", "# T\n\n<!-- y -->\n\nprose\n")}, T, "comment"),
    "K05": ({"site/x.html": ("<!-- a -->\n<p>x</p>\n", "<!-- b -->\n<p>x</p>\n")}, T, "comment"),
    "K06": ({"docs/specs/x.md": ("---\ns: a\n```\n---\nbody\n", "---\ns: b\n```\n---\nbody\n")},
            B, "other"),
    "K07": ({"docs/specs/x.md": ("---\ns: a\n---\n\nx\n", "---\ns: a\n\nx\n")}, B, "other"),
    "H01": ({"src/x.c": ("/*\n x\n*/\nint a;\n", "/*\n x\n*/ f(); /* y */\nint a;\n")}, S, "code"),
    "H02": ({"site/x.html": ("<!--\nx\n-->\n<p>a</p>\n",
                             "<!--\nx\n--> <script>x()</script> <!-- y -->\n<p>a</p>\n")}, B, "other"),
    "H03": ({"src/x.c": ("// n\nfoo();\n", "// n \\\nfoo();\n")}, S, "code"),
    "H04": ({"src/build.sh": ("cmd \\\n  --flag\n", "cmd \\\n# note\n  --flag\n")}, S, "code"),
    "H05": ({"src/x.js": ("f();\n", "f();\n// n\u2028foo();\n")}, S, "code"),
    "H06": ({"src/x.py": ("x = 1\n", "x = 1\n# n\x0bimport os\n")}, S, "code"),
    "S23a": ({"docs/a.md": ("# T\n\nprose\n", "# T\n\nprose \udcff\n")}, T, None),
    "S23b": ({"src/x.py": ("x = 1\n", "x = '\udcff'\n")}, S, None),
}


def _apply(repo, edits):
    for rel, (_old, new) in edits.items():
        if new is None:
            (repo / rel).unlink()
        else:
            _put(repo, rel, new)


@pytest.mark.parametrize("cid", list(CASES))
def test_case_table(tmp_path, cid):
    edits, (klass, action), fclass = CASES[cid]
    repo = _repo(tmp_path, {rel: old for rel, (old, _n) in edits.items()})
    _apply(repo, edits)
    want = {(" D" if new is None else " M", rel) for rel, (_o, new) in edits.items()}
    assert {(ln[:2], ln[3:]) for ln in _dirty(repo).splitlines()} == want, _dirty(repo)
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == (klass, action), p["files"]
    if fclass:
        assert p["files"][0]["file_class"] == fclass
    assert sorted(p["paths"]) == sorted(edits)


def test_c40_clean_and_c41_untracked_only(tmp_path):
    repo = _repo(tmp_path)
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["paths"], p["recommended"]) == ("clean", "resume", [], [])
    _put(repo, "new.md", "x\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["paths"]) == ("clean", "resume", [])


def test_c42_untracked_never_listed(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    _put(repo, "docs/a.md", "# T\n\nx\n")
    _put(repo, "axb.md", "u\n")
    p = MD.plan_clear(repo)
    assert p["paths"] == ["docs/a.md"] and [f["path"] for f in p["files"]] == ["docs/a.md"]


@pytest.mark.parametrize("src,dst", [("src/a.py", "docs/a.md"),     # C37
                                    ("docs/a.md", "src/a.py")])    # C38
def test_c37_c38_rename_across_code_boundary(tmp_path, src, dst):
    repo = _repo(tmp_path, {src: "x = 1\n" if src.endswith("py") else "# T\n"})
    (repo / dst).parent.mkdir(exist_ok=True)
    _git(repo, "mv", src, dst)
    assert _git(repo, "status", "--porcelain", "-z").stdout.split("\0")[0] == f"R  {dst}"
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == S
    assert set(p["paths"]) == {src, dst}
    assert set(p["substantive_paths"]) == {src, dst}


# ---- _line_kinds (S01-S10) ---------------------------------------------------------------------

def _k(path, text):
    return MD._line_kinds(path, text)


@pytest.mark.parametrize("suf", [".py", ".pyi", ".sh", ".bash", ".yml", ".yaml", ".toml", ".rb", ".pl"])
def test_s01_hash_suffixes(suf):
    assert _k("x" + suf, "# c\n#!/x\n  \n") == (["comment", "text", "blank"], False)


@pytest.mark.parametrize("suf", [".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".rs", ".c", ".h",
                                 ".cc", ".cpp", ".cxx", ".hpp", ".java"])
def test_s02_s03_clike_suffixes(suf):
    kinds, _ = _k("x" + suf, "// c\n*out\n*=\n* b;\n")
    assert kinds == ["comment", "text", "text", "text"]
    kinds, hz = _k("x" + suf, "*/\n")
    assert kinds == ["text"] and hz
    kinds, hz = _k("x" + suf, "/*\n * x\n */\n")
    assert kinds == ["comment"] * 3 and not hz
    kinds, hz = _k("x" + suf, "/*\ncode();\n*/\n")
    assert kinds == ["comment"] * 3 and not hz


def test_s02_star_deref_is_text():
    assert _k("x.c", "*ptr = 1;\n")[0] == ["text"]


def test_s04_css():
    assert _k("x.css", "// x\n/* x */\n\n")[0] == ["text", "comment", "blank"]


def test_s05_html_comments_and_heading():
    assert _k("x.md", "<!-- x -->\n")[0] == ["comment"]
    assert _k("x.md", "<!--\nt\n-->\n")[0] == ["comment"] * 3
    assert _k("x.md", "# Heading\n")[0] == ["text"]


def test_s06_unknown_suffix_has_no_comments():
    assert _k("x.json", "\n// x\n")[0] == ["text", "text"]


def test_s07_triple_quoted_strings():
    assert _k("x.py", '"""\n# x\n\n"""\n')[0] == ["text"] * 4
    assert _k("x.toml", "'''\n# x\n'''\n")[0] == ["text"] * 3


def test_s08_tilde_does_not_close_a_backtick_fence():
    assert _k("x.md", "```\n# x\n~~~\ny\n```\n")[0] == ["fenced"] * 5


BREAKS = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]


def test_s09_blank_and_crlf_are_not_hazards():
    assert _k("x.py", "\x0c\n") == (["blank"], False)
    assert _k("x.py", "# x\r\n") == (["comment"], False)


@pytest.mark.parametrize("ch", BREAKS)
def test_s09_every_line_break_character_is_a_hazard(ch):
    assert _k("x.py", f"# n{ch}import os\n")[1]
    assert _k("x.js", f"// n{ch}foo();\n")[1]
    assert MD._LINE_BREAKS == "".join(BREAKS)


_JS = [".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go"]
_CL = _JS + [".rs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".java", ".css"]
HAZARDS = (
    [(f"x{s}", "cat <<EOF\n") for s in (".sh", ".bash", ".rb", ".pl")]
    + [(f"x{s}", f"k: {e}\n") for s in (".yml", ".yaml") for e in ("|", "|-", "|+", ">", ">-", ">+")]
    + [(f"x{s}", "const a = `x`;\n") for s in _JS]
    + [(f"x{s}", f'x = "a{c}b";\n') for s in _CL for c in ("/*", "*/")]
    + [(f"x{s}", f"a {c} b\n") for s in (".md", ".html", ".xml") for c in ("<!--", "-->")]
    + [(f"x{s}", "/*\nx */ code();\n") for s in _CL]                    # closer line not ending in */
    + [(f"x{s}", "/*\nx\n*/ f(); /* y */\n") for s in _CL]             # two closers on one line
    + [(f"x{s}", "<!--\nx --> text\n") for s in (".md", ".html", ".xml")]
    + [(f"x{s}", "<!--\nx\n--> <b>y</b> <!-- z -->\n") for s in (".md", ".html", ".xml")]
    + [(f"x{s}", "<!--\n```\n-->\n") for s in (".md", ".html", ".xml")]  # fence inside an open comment
    + [(f"x{s}", "x = 1 \\\n") for s in (".py", ".pyi", ".sh", ".bash", ".rb", ".pl")]
    + [(f"x{s}", "// n \\\n") for s in _CL if s != ".css"]            # backslash splice
)


@pytest.mark.parametrize("path,text", HAZARDS)
def test_s10_hazards(path, text):
    assert _k(path, text)[1], (path, text)


@pytest.mark.parametrize("path,text", [("x.json", "a \\\n"), ("x.md", "a \\\n"), ("x.py", "# c\n"),
                                       ("x.c", "/* a */\n"), ("x.md", "<!-- a -->\n")])
def test_non_hazards(path, text):
    assert not _k(path, text)[1]


@pytest.mark.parametrize("path", ["x.c", "x.js", "x.css"])
def test_block_comment_then_code_on_one_line_is_text(path):
    kinds, hz = _k(path, "/* a */ code();\n")
    assert kinds == ["text"] and hz


# ---- git states (S11-S19, S24-S27) -------------------------------------------------------------

def test_s11_s12_mode_change(tmp_path):
    repo = _repo(tmp_path, {"docs/tool.txt": "x\n", "src/x.py": "x = 1\n"})
    os.chmod(repo / "docs/tool.txt", 0o755)
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["files"][0]["file_class"]) == ("branch", "save_branch", "other")
    _git(repo, "checkout", "--", "docs/tool.txt")
    os.chmod(repo / "src/x.py", 0o755)
    p = MD.plan_clear(repo)
    assert (p["class"], p["files"][0]["file_class"]) == ("substantive", "code")


def test_s13_typechange_and_s14_retargeted_symlink(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n", "docs/link": "a.md\n"})
    os.symlink("a.md", repo / "docs/lnk")
    _git(repo, "add", "docs/lnk")
    _git(repo, "commit", "-qm", "lnk")
    (repo / "docs/a.md").unlink()
    os.symlink("elsewhere", repo / "docs/a.md")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == B and p["files"][0]["status"] == " T"
    _git(repo, "checkout", "--", "docs/a.md")
    (repo / "docs/lnk").unlink()
    os.symlink("other", repo / "docs/lnk")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == B


def test_s15_dirty_submodule_and_staged_pointer(tmp_path):
    sub = _repo(tmp_path, {"f.txt": "x\n"}, name="sub")
    repo = _repo(tmp_path, {}, name="sup")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
    _git(repo, "commit", "-qm", "sub")
    _put(repo / "vendor/sub", "f.txt", "changed\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["recommended"]) == ("branch", "escalate", [])
    assert p["paths"] == ["vendor/sub"]
    _git(repo / "vendor/sub", "config", "user.email", "t@t")
    _git(repo / "vendor/sub", "config", "user.name", "t")
    _git(repo / "vendor/sub", "commit", "-qam", "bump")
    _git(repo, "add", "vendor/sub")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == B and p["files"][0]["status"] == "M "
    assert not any("add --" in c for c in p["recommended"])


def test_s16_staged_only_comment_edit(tmp_path):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n# a\n"})
    _put(repo, "src/x.py", "x = 1\n# b\n")
    _git(repo, "add", "src/x.py")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == T
    assert not any("add --" in c for c in p["recommended"])


def test_s17_staged_fence_edit(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": FENCE})
    _put(repo, "docs/a.md", FENCE.replace("\ncode\n", "\ncode2\n"))
    _git(repo, "add", "docs/a.md")
    assert (MD.plan_clear(repo)["class"]) == "branch"


def test_s18_staged_comment_plus_unstaged_logic(tmp_path):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n# a\n"})
    _put(repo, "src/x.py", "x = 1\n# b\n")
    _git(repo, "add", "src/x.py")
    _put(repo, "src/x.py", "x = 2\n# b\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == S and p["files"][0]["status"] == "MM"


def test_s19_staged_prose_plus_unstaged_fence_edit(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": FENCE})
    _put(repo, "docs/a.md", FENCE.replace("prose", "prose2"))
    _git(repo, "add", "docs/a.md")
    _put(repo, "docs/a.md", FENCE.replace("prose", "prose2").replace("\ncode\n", "\ncode2\n"))
    assert MD.plan_clear(repo)["class"] == "branch"


def test_old_new_pairs_are_the_table_in_inputs(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n"})
    _put(repo, "src/x.py", "x = 2\n")
    _git(repo, "add", "src/x.py")
    _put(repo, "src/x.py", "x = 3\n")
    seen = []
    orig = MD._half_class

    def spy(path, orig_name, **kw):
        seen.append((kw["old"], kw["new"]))
        return orig(path, orig_name, **kw)

    monkeypatch.setattr(MD, "_half_class", spy)
    MD.plan_clear(repo)
    assert seen == [(b"x = 1\n", b"x = 2\n"), (b"x = 2\n", b"x = 3\n")]
    seen.clear()
    _git(repo, "reset", "-q")
    MD.plan_clear(repo)
    assert seen == [(b"x = 1\n", b"x = 3\n")]


def test_s24_user_diff_config_does_not_change_the_plan(tmp_path):
    def build(name, cfg):
        repo = _repo(tmp_path, {"docs/specs/0001-x.md": "---\ns: a\n---\n\nx\n", "src/a.py": "x = 1\n",
                                "docs/a.md": FENCE, "docs/s26.md": "# T\n\nprose\n"}, name=name)
        _put(repo, ".gitattributes", "*.md diff=bad\n")
        if cfg:
            for k, v in (("diff.external", "false"), ("diff.bad.textconv", "false"),
                         ("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"),
                         ("diff.renames", "false"), ("status.renames", "false")):
                _git(repo, "config", k, v)
        _put(repo, "docs/specs/0001-x.md", "---\ns: b\n---\n\nx\n")
        _git(repo, "mv", "src/a.py", "docs/b.md")
        _put(repo, "docs/a.md", FENCE.replace("prose", "prose2"))
        _git(repo, "add", "docs/a.md")
        _put(repo, "docs/a.md", FENCE.replace("prose", "prose3"))
        _git(repo, "mv", "docs/s26.md", "docs/s26b.md")                # S26 under the same config
        _put(repo, "docs/s26b.md", "# T\n\nprose 2\n")
        return MD.plan_clear(repo)

    plain, configured = build("p", False), build("c", True)
    strip = lambda p: (p["class"], p["action"], p["paths"], p["files"])
    assert strip(plain) == strip(configured)
    assert (plain["class"], plain["action"]) == S
    assert {"path": "docs/s26b.md", "orig_path": "docs/s26.md", "status": "RM",
            "file_class": "other"} in configured["files"]


def test_s25_staged_git_mv_is_one_row(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    _git(repo, "mv", "docs/a.md", "docs/b.md")
    p = MD.plan_clear(repo)
    assert p["files"] == [{"path": "docs/b.md", "orig_path": "docs/a.md", "status": "R ",
                           "file_class": "other"}]
    assert p["paths"] == ["docs/b.md", "docs/a.md"]


def test_s26_staged_mv_plus_unstaged_edit(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n\nprose\n"})
    _git(repo, "mv", "docs/a.md", "docs/b.md")
    _put(repo, "docs/b.md", "# T\n\nprose 2\n")
    p = MD.plan_clear(repo)
    assert p["files"][0]["status"] == "RM" and (p["class"], p["action"]) == B
    assert p["recommended"][1].endswith("add -- docs/b.md")


def test_s27_unstaged_mv_lists_only_the_delete(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    (repo / "docs/a.md").rename(repo / "docs/b.md")
    p = MD.plan_clear(repo)
    assert p["paths"] == ["docs/a.md"] and p["files"][0]["status"] == " D"
    assert "docs/b.md" not in p["paths"]


# ---- operation in progress (AC3) ---------------------------------------------------------------

def _conflict(repo):
    _put(repo, "f.txt", "a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "f")
    _git(repo, "switch", "-qc", "side")
    _put(repo, "f.txt", "side\n")
    _git(repo, "commit", "-qam", "side")
    _git(repo, "switch", "-q", "main")
    _put(repo, "f.txt", "main\n")
    _git(repo, "commit", "-qam", "main")
    _git(repo, "merge", "side", check=False)


def test_merge_head_and_uu(tmp_path):
    repo = _repo(tmp_path)
    _conflict(repo)
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["in_progress"]) == ("merge", "escalate", "merge")
    assert p["recommended"] == [] and p["files"][0]["status"] == "UU"


def test_merge_in_a_linked_worktree_and_off_base(tmp_path):
    repo = _repo(tmp_path)
    _put(repo, "f.txt", "a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "f")
    _git(repo, "branch", "side")
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "other")
    _put(wt, "f.txt", "o\n")
    _git(wt, "commit", "-qam", "o")
    _git(repo, "switch", "-q", "side")
    _put(repo, "f.txt", "s\n")
    _git(repo, "commit", "-qam", "s")
    _git(repo, "switch", "-q", "main")
    _git(wt, "merge", "side", check=False)
    p = MD.plan_clear(wt)
    assert (p["class"], p["action"], p["in_progress"], p["on_base"]) == ("merge", "escalate", "merge", False)


@pytest.mark.parametrize("marker,label,is_dir", [("CHERRY_PICK_HEAD", "cherry-pick", False),
                                                 ("REVERT_HEAD", "revert", False),
                                                 ("rebase-merge", "rebase", True),
                                                 ("rebase-apply", "rebase", True)])
def test_sequencer_markers(tmp_path, marker, label, is_dir):
    repo = _repo(tmp_path)
    m = repo / ".git" / marker
    m.mkdir() if is_dir else m.write_text("0" * 40 + "\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["in_progress"]) == ("merge", "escalate", label)


def test_two_markers_merge_wins(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".git/rebase-merge").mkdir()
    (repo / ".git/MERGE_HEAD").write_text("0" * 40 + "\n")
    assert MD.plan_clear(repo)["in_progress"] == "merge"


@pytest.mark.parametrize("xy", sorted(MD._UNMERGED))
def test_each_unmerged_code_is_a_conflict(tmp_path, monkeypatch, xy):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n"})
    monkeypatch.setattr(MD, "_status", lambda root: [{"xy": xy, "path": "src/x.py", "orig": None}])
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["in_progress"]) == ("merge", "escalate", "conflict")


def _real_unmerged(tmp_path, kind):
    """A real `git merge side` conflict leaving `kind` (UU, AA, DU or UD) on one path."""
    repo = _repo(tmp_path, {} if kind == "AA" else {"f.txt": "a\n"})
    _git(repo, "switch", "-qc", "side")
    if kind == "UD":
        _git(repo, "rm", "-q", "f.txt")
    else:
        _put(repo, "f.txt", "side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "switch", "-q", "main")
    if kind == "DU":
        _git(repo, "rm", "-q", "f.txt")
    else:
        _put(repo, "f.txt", "main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "merge", "side", check=False)
    return repo


@pytest.mark.parametrize("kind", ["UU", "AA", "DU", "UD"])
def test_real_unmerged_states(tmp_path, kind):
    repo = _real_unmerged(tmp_path, kind)
    p = MD.plan_clear(repo)
    assert [f["status"] for f in p["files"]] == [kind]
    assert (p["class"], p["action"], p["in_progress"], p["recommended"]) == ("merge", "escalate", "merge", [])
    (repo / ".git/MERGE_HEAD").unlink()                 # no marker file: the codes alone decide
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["in_progress"]) == ("merge", "escalate", "conflict")


def test_cherry_pick_no_commit_is_not_merge(tmp_path):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n"})
    _git(repo, "switch", "-qc", "side")
    _put(repo, "src/x.py", "x = 2\n")
    _git(repo, "commit", "-qam", "side")
    _git(repo, "switch", "-q", "main")
    _git(repo, "cherry-pick", "-n", "side")
    p = MD.plan_clear(repo)
    assert p["in_progress"] is None and p["class"] == "substantive"


# ---- base (AC4) --------------------------------------------------------------------------------

def _cfg(repo, doc):
    _put(repo, ".nw/project.yaml", yaml.safe_dump({"schema": "nw.project.v1", "project": "T", **doc}))


def test_b01_to_b03_base_chain(tmp_path):
    repo = _repo(tmp_path)
    _cfg(repo, {"merge": {"base": "x"}, "base": "y"})
    assert MD.plan_clear(repo)["base"] == "x"
    _cfg(repo, {"base": "y"})
    assert MD.plan_clear(repo)["base"] == "y"
    _cfg(repo, {})
    assert MD.plan_clear(repo)["base"] == "main"


def test_b04_base_flag_ignored_with_a_config_and_matches_close_cfg(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"}, base="develop")
    _cfg(repo, {})
    _git(repo, "commit", "-qam", "cfg")
    _put(repo, "docs/a.md", "# T\n\nx\n")
    p = MD.plan_clear(repo, base="develop")
    assert (p["base"], p["on_base"], p["action"], p["class"]) == ("main", False, "hold", "trivial")
    state = {"repo": {"root": str(repo), "worktree": str(repo), "branch": "b", "base": "develop"}}
    cfg = P.load_project_config(repo / ".nw/project.yaml")
    assert CL._close_cfg(state, cfg)["base"] == p["base"]


def test_b05_b06_no_config(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"}, base="develop", project=False)
    _put(repo, "docs/a.md", "# T\n\nx\n")
    p = MD.plan_clear(repo, base="develop")
    assert (p["base"], p["on_base"], p["action"]) == ("develop", True, "commit")
    assert MD.plan_clear(repo)["base"] == "main"


@pytest.mark.parametrize("body", ["a: [unclosed\n", "schema: nw.project.v1\nproject: T\nmerge: not-a-mapping\n"])
def test_b07_b08_invalid_config_exits_3(tmp_path, body):
    repo = _repo(tmp_path)
    _put(repo, ".nw/project.yaml", body)
    with pytest.raises(ValueError) as ei:
        P.load_project_config(repo / ".nw/project.yaml")
    msg = str(ei.value).splitlines()[0]
    assert msg
    for args in (("--root", str(repo)), ("--root", str(repo), "--json")):
        r = _cli(*args)
        assert r.exit_code == 3 and msg in r.stderr and "error:" in r.stderr, r.stderr
        assert r.stdout == ""


def test_non_string_base_exits_3(tmp_path):
    repo = _repo(tmp_path)
    _cfg(repo, {"base": 5})
    with pytest.raises(ValueError) as ei:
        P.load_project_config(repo / ".nw/project.yaml")
    r = _cli("--root", str(repo), "--json")
    assert r.exit_code == 3 and str(ei.value).splitlines()[0] in r.stderr and r.stdout == ""


def test_non_main_base_commit_hold_and_detached(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"}, base="develop")
    _put(repo, "docs/a.md", "# T\n\nx\n")
    assert MD.plan_clear(repo)["action"] == "commit"
    _git(repo, "switch", "-qc", "feat")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["on_base"]) == ("trivial", "hold", False)
    _git(repo, "switch", "-q", "--detach")
    assert (MD.plan_clear(repo)["on_base"], MD.plan_clear(repo)["action"]) == (False, "hold")


@pytest.mark.parametrize("code_side", [False, True])
def test_dirty_gitlink_escalates_with_no_commands(tmp_path, code_side):
    sub = _repo(tmp_path, {"f.txt": "x\n"}, name="sub")
    repo = _repo(tmp_path, {}, name="sup")
    at = "src/sub" if code_side else "vendor/sub"
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), at)
    _git(repo, "commit", "-qm", "sub")
    _put(repo / at, "f.txt", "changed\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["recommended"]) == (
        ("substantive" if code_side else "branch"), "escalate", [])
    assert [f["file_class"] for f in p["files"]] == (["code"] if code_side else ["other"])
    assert p["paths"] == [at]


@pytest.mark.parametrize("path,klass,fclass", [("docs/a.md", "branch", "other"),
                                               ("src/x.py", "substantive", "code")])
def test_directory_replacing_a_file_is_never_trivial(tmp_path, path, klass, fclass):
    repo = _repo(tmp_path, {path: "# T\n" if path.endswith("md") else "x = 1\n"})
    (repo / path).unlink()
    _put(repo, path + "/inner.txt", "i\n")
    assert _dirty(repo).strip().startswith(("D ", " D", "T ", " T"))
    p = MD.plan_clear(repo)
    assert (p["class"], [f["file_class"] for f in p["files"]]) == (klass, [fclass])


def test_dirty_gitlink_off_base_is_escalate(tmp_path):
    sub = _repo(tmp_path, {"f.txt": "x\n"}, name="sub")
    repo = _repo(tmp_path, {}, name="sup")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
    _git(repo, "commit", "-qm", "sub")
    _put(repo / "vendor/sub", "f.txt", "changed\n")
    _git(repo, "switch", "-qc", "feat")
    assert MD.plan_clear(repo)["action"] == "escalate"


# ---- recommended (AC5) -------------------------------------------------------------------------

def _run_list(repo, cmds):
    for c in cmds:
        r = subprocess.run(shlex.split(c), capture_output=True, text=True)
        assert r.returncode == 0, (c, r.stderr)


def _after_run_checks(repo):
    assert _dirty(repo) == ""


def _tree(repo, rev):
    return set(_git(repo, "ls-tree", "-r", "--name-only", rev).stdout.splitlines())


def _show(repo, rev, path):
    return _git(repo, "show", f"{rev}:{path}").stdout


def _wip_branches(repo):
    return [b.strip() for b in _git(repo, "branch", "--list", "wip/main-dirty-*").stdout.splitlines()]


def _save_list(repo, wip, klass, upaths, base="main"):
    """The whole `save_branch` / `branch_and_escalate` / dirty-`hold` list from the Design table."""
    pre = f"git -C {shlex.quote(str(repo))}"
    add = ([f"{pre} --literal-pathspecs add -- " + " ".join(shlex.quote(u) for u in upaths)]
           if upaths else [])
    return ([f"{pre} switch -c {wip}"] + add
            + [f"{pre} commit -m 'WIP: stray main-checkout edits ({klass})'", f"{pre} switch {base}"])


def test_recommended_commit_whole_list_and_run(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    _put(repo, "docs/a.md", "# T\n\nx\n")
    _put(repo, "untracked.md", "u\n")
    p = MD.plan_clear(repo)
    R = shlex.quote(str(repo))
    assert p["recommended"] == [f"git -C {R} --literal-pathspecs add -- docs/a.md",
                                f"git -C {R} commit -m 'chore: land stray trivial main-checkout edits'"]
    _run_list(repo, p["recommended"])
    _after_run_checks(repo)
    assert (repo / "untracked.md").exists()
    assert _git(repo, "status", "--porcelain").stdout == "?? untracked.md\n"
    assert _show(repo, "HEAD", "docs/a.md") == "# T\n\nx\n"
    assert "untracked.md" not in _tree(repo, "HEAD")
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == "chore: land stray trivial main-checkout edits"


# variant: (files, edit, unstaged paths named by the add line)
SAVE_VARIANTS = {
    "rm": ({"src/a.py": "x = 1\n", "src/b.py": "y = 1\n"}, lambda r: _git(r, "rm", "-q", "src/b.py"), []),
    "mv": ({"src/a.py": "x = 1\n", "src/b.py": "y = 1\n"},
           lambda r: _git(r, "mv", "src/a.py", "src/c.py"), []),
    "unstaged_delete": ({"src/a.py": "x = 1\n", "src/b.py": "y = 1\n"},
                        lambda r: (r / "src/b.py").unlink(), ["src/b.py"]),
    "logic": ({"src/a.py": "x = 1\n", "src/b.py": "y = 1\n"},
              lambda r: _put(r, "src/a.py", "x = 2\n"), ["src/a.py"]),
    "mixed": ({"src/a.py": "x = 1\n", "src/b.py": "y = 1\n"},          # one staged-only, one unstaged
              lambda r: (_git(r, "rm", "-q", "src/b.py"), _put(r, "src/a.py", "x = 2\n")), ["src/a.py"]),
}


@pytest.mark.parametrize("variant", list(SAVE_VARIANTS))
def test_recommended_save_list_runs(tmp_path, variant):
    files, edit, upaths = SAVE_VARIANTS[variant]
    repo = _repo(tmp_path, files)
    edit(repo)
    _put(repo, "untracked.txt", "u\n")
    p = MD.plan_clear(repo)
    assert p["action"] == "branch_and_escalate"
    wip = p["recommended"][0].split()[-1]
    assert re.fullmatch(r"wip/main-dirty-\d{8}T\d{6}Z", wip)
    assert p["recommended"] == _save_list(repo, wip, "substantive", upaths)
    base_head = _git(repo, "rev-parse", "main").stdout
    _run_list(repo, p["recommended"])
    _after_run_checks(repo)
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert _git(repo, "rev-parse", "main").stdout == base_head
    assert _wip_branches(repo) == [wip]
    assert _git(repo, "rev-parse", f"{wip}^").stdout == base_head
    tree = _tree(repo, wip)
    assert "untracked.txt" not in tree
    assert (repo / "untracked.txt").exists()
    assert _git(repo, "status", "--porcelain").stdout == "?? untracked.txt\n"
    changes = set(_git(repo, "diff", "--name-status", "--no-renames", "main", wip).stdout.splitlines())
    if variant in ("rm", "unstaged_delete"):
        assert changes == {"D\tsrc/b.py"} and "src/b.py" not in tree
    elif variant == "mv":
        assert changes == {"D\tsrc/a.py", "A\tsrc/c.py"}
        assert "src/c.py" in tree and "src/a.py" not in tree
    elif variant == "logic":
        assert changes == {"M\tsrc/a.py"} and _show(repo, wip, "src/a.py") == "x = 2\n"
    else:
        assert changes == {"D\tsrc/b.py", "M\tsrc/a.py"} and _show(repo, wip, "src/a.py") == "x = 2\n"


def test_recommended_branch_class_save_list(tmp_path):
    repo = _repo(tmp_path, {"config/x.json": "{}\n"})
    _put(repo, "config/x.json", '{"a": 1}\n')
    _put(repo, "untracked.txt", "u\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == B
    wip = p["recommended"][0].split()[-1]
    assert p["recommended"] == _save_list(repo, wip, "branch", ["config/x.json"])
    _run_list(repo, p["recommended"])
    _after_run_checks(repo)
    assert _show(repo, wip, "config/x.json") == '{"a": 1}\n'
    assert "untracked.txt" not in _tree(repo, wip) and (repo / "untracked.txt").exists()


@pytest.mark.parametrize("path,untracked,klass", [("a*b.md", "axb.md", "branch"),
                                                  ("src/a*b.py", "src/axb.py", "substantive")])
def test_literal_pathspecs_protect_a_star_name(tmp_path, path, untracked, klass):
    repo = _repo(tmp_path, {path: "x = 1\n"})
    _put(repo, path, "x = 2\n")
    _put(repo, untracked, "u\n")
    p = MD.plan_clear(repo)
    assert p["class"] == klass
    wip = p["recommended"][0].split()[-1]
    assert p["recommended"] == _save_list(repo, wip, klass, [path])
    assert "'" + path + "'" in p["recommended"][1]
    _run_list(repo, p["recommended"])
    _after_run_checks(repo)
    tree = _tree(repo, wip)
    assert path in tree and untracked not in tree
    assert _show(repo, wip, path) == "x = 2\n"
    assert (repo / untracked).exists()
    assert f"?? {untracked}" in _git(repo, "status", "--porcelain").stdout.splitlines()


def test_literal_pathspecs_star_name_commit_list(tmp_path):
    repo = _repo(tmp_path, {"docs/a*b.md": "# T\n"})
    _put(repo, "docs/a*b.md", "# T\n\nx\n")
    _put(repo, "docs/axb.md", "u\n")
    p = MD.plan_clear(repo)
    assert p["action"] == "commit"
    _run_list(repo, p["recommended"])
    assert _show(repo, "HEAD", "docs/a*b.md") == "# T\n\nx\n"
    assert "docs/axb.md" not in _tree(repo, "HEAD") and (repo / "docs/axb.md").exists()


def test_path_with_a_space_is_quoted(tmp_path):
    repo = _repo(tmp_path, {"docs/a b.md": "# T\n"})
    _put(repo, "docs/a b.md", "# T\n\nx\n")
    assert "'docs/a b.md'" in MD.plan_clear(repo)["recommended"][0]


def test_hold_dirty_runs_and_ends_on_the_base(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    _git(repo, "switch", "-qc", "feat")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feat work")
    _put(repo, "docs/a.md", "# T\n\nx\n")
    _put(repo, "untracked.md", "u\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == ("trivial", "hold")
    wip = p["recommended"][0].split()[-1]
    assert p["recommended"] == _save_list(repo, wip, "trivial", ["docs/a.md"])
    _run_list(repo, p["recommended"])
    _after_run_checks(repo)
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert _git(repo, "merge-base", "--is-ancestor", "feat", wip, check=False).returncode == 0
    assert _git(repo, "rev-parse", f"{wip}^").stdout == _git(repo, "rev-parse", "feat").stdout
    assert _show(repo, wip, "docs/a.md") == "# T\n\nx\n"
    assert "untracked.md" not in _tree(repo, wip) and (repo / "untracked.md").exists()


def test_hold_clean_and_escalate_lists(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "switch", "-qc", "feat")
    p = MD.plan_clear(repo)
    R = shlex.quote(str(repo))
    assert (p["class"], p["action"], p["recommended"]) == ("clean", "hold", [f"git -C {R} switch main"])
    assert MD.plan_clear(_repo(tmp_path, name="z"))["recommended"] == []


def test_wip_name_takes_a_suffix_when_it_exists(tmp_path, monkeypatch):
    import datetime
    import types

    class FixedDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(MD, "dt", types.SimpleNamespace(datetime=FixedDT, timezone=datetime.timezone))
    repo = _repo(tmp_path, {"src/a.py": "x = 1\n"})
    _put(repo, "src/a.py", "x = 2\n")
    assert MD.plan_clear(repo)["recommended"][0].endswith("wip/main-dirty-20260101T000000Z")
    _git(repo, "branch", "wip/main-dirty-20260101T000000Z")
    assert MD.plan_clear(repo)["recommended"][0].endswith("wip/main-dirty-20260101T000000Z-2")


def test_no_add_line_when_nothing_is_unstaged(tmp_path):
    repo = _repo(tmp_path, {"src/a.py": "x = 1\n"})
    _git(repo, "rm", "-q", "src/a.py")
    p = MD.plan_clear(repo)
    assert not any(" add " in c for c in p["recommended"])
    assert not any(w in c for c in p["recommended"] for w in ("checkout --", "reset", "clean", "push", "stash"))


# ---- read-only (AC6) ---------------------------------------------------------------------------

def _snapshot(repo):
    files = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob("*")
             if p.is_file() and ".git" not in p.relative_to(repo).parts}
    return (_git(repo, "rev-parse", "HEAD").stdout, _git(repo, "branch", "--list").stdout,
            (repo / ".git/index").read_bytes(), files)


def _state_merge(tmp_path):
    repo = _repo(tmp_path)
    _conflict(repo)
    return repo


def _state_hold(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    _git(repo, "switch", "-qc", "feat")
    _put(repo, "docs/a.md", "# T\n\nx\n")
    return repo


def _state_staged(tmp_path):
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n# c\n"})
    _put(repo, "src/x.py", "x = 1\n# d\n")
    _git(repo, "add", "src/x.py")
    _put(repo, "src/x.py", "x = 2\n# d\n")
    return repo


def _state_rename(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n\nprose\n"})
    _git(repo, "mv", "docs/a.md", "docs/b.md")
    _put(repo, "docs/b.md", "# T\n\nprose 2\n")
    return repo


def _state_gitlink(tmp_path):
    sub = _repo(tmp_path, {"f.txt": "x\n"}, name="sub")
    repo = _repo(tmp_path, {}, name="sup")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
    _git(repo, "commit", "-qm", "sub")
    _put(repo / "vendor/sub", "f.txt", "changed\n")
    return repo


STATES = {"merge": _state_merge, "hold": _state_hold, "staged": _state_staged,
          "rename": _state_rename, "gitlink": _state_gitlink}


@pytest.mark.parametrize("cid", ["C02", "C09", "C21", "C40"] + list(STATES))
def test_plan_clear_writes_nothing(tmp_path, cid):
    if cid in STATES:
        repo = STATES[cid](tmp_path)
    elif cid == "C40":
        repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    else:
        edits = CASES[cid][0]
        repo = _repo(tmp_path, {rel: old for rel, (old, _n) in edits.items()})
        _apply(repo, edits)
    for p in repo.rglob("*"):                         # stale stat data: same bytes, new mtime
        if p.is_file() and ".git" not in p.relative_to(repo).parts:
            os.utime(p, (1_000_000_000, 1_000_000_000))
    before = _snapshot(repo)
    MD.plan_clear(repo)
    assert _snapshot(repo) == before


# ---- CLI (AC7) ---------------------------------------------------------------------------------

def test_cli_json_files_rows_and_base_reaches_the_plan(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n\nprose\n", "docs/m.md": "# M\n"}, project=False)
    _put(repo, "docs/a.md", "# T\n\nprose 2\n")
    r = _cli("--root", str(repo), "--json")
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["schema"] == "nw.main_dirty_plan.v1"
    assert doc["files"] == [{"path": "docs/a.md", "orig_path": None, "status": " M", "file_class": "docs"}]
    _git(repo, "checkout", "--", "docs/a.md")
    (repo / "src").mkdir()
    _git(repo, "mv", "docs/m.md", "src/m.py")
    doc = json.loads(_cli("--root", str(repo), "--json").output)
    assert doc["files"] == [{"path": "src/m.py", "orig_path": "docs/m.md", "status": "R ",
                             "file_class": "code"}]
    assert json.loads(_cli("--root", str(repo), "--base", "develop", "--json").output)["base"] == "develop"


@pytest.mark.parametrize("cid,klass", [("C02", "trivial"), ("C08", "branch"), ("C21", "substantive"),
                                       ("C40", "clean"), ("MERGE", "merge")])
def test_cli_json_exits_0_on_each_class(tmp_path, cid, klass):
    if cid == "MERGE":
        repo = _repo(tmp_path)
        _conflict(repo)
    elif cid == "C40":
        repo = _repo(tmp_path)
    else:
        edits = CASES[cid][0]
        repo = _repo(tmp_path, {rel: old for rel, (old, _n) in edits.items()})
        _apply(repo, edits)
    r = _cli("--root", str(repo), "--json")
    assert r.exit_code == 0 and r.stderr == ""
    doc = json.loads(r.stdout)
    assert doc["class"] == klass and doc["schema"] == "nw.main_dirty_plan.v1"
    assert set(doc) == {"schema", "root", "base", "on_base", "class", "action", "in_progress", "paths",
                        "substantive_paths", "files", "recommended"}


def test_cli_text_mode(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    _put(repo, "docs/a.md", "# T\n\nx\n")
    r = _cli("--root", str(repo))
    lines = r.output.splitlines()
    assert r.exit_code == 0 and lines[0] == "commit trivial 1 paths" and lines[1] == "docs/a.md"
    assert lines[2].startswith("git -C ") and len(lines) == 4


def test_cli_text_mode_never_wraps_on_a_terminal(tmp_path, monkeypatch):
    from nightwright import console as C
    monkeypatch.setattr(C, "stdout_is_tty", lambda _c: True)      # a terminal folds at its width
    repo = _repo(tmp_path, {"docs/a long directory name/a long file name.md": "# T\n"})
    _put(repo, "docs/a long directory name/a long file name.md", "# T\n\nx\n")
    p = MD.plan_clear(repo)
    r = _cli("--root", str(repo), env={"COLUMNS": "40"})
    assert r.exit_code == 0
    assert r.stdout.splitlines()[1:] == [*p["paths"], *p["recommended"]]


def test_cli_exit_2_for_non_worktrees(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    for root in (plain, bare, repo / "docs", repo / ".git"):
        for env in (None, {"GIT_DIR": str(repo / ".git")}):
            r = _cli("--root", str(root), env=env)
            assert r.exit_code == 2 and "error:" in r.stderr and "worktree" in r.stderr, (root, env, r.output)
            assert r.stdout == ""
    for args in (("--root", str(tmp_path / "nope")), ("--root", str(tmp_path / "nope"), "--json")):
        r = _cli(*args)
        assert r.exit_code == 2 and "error:" in r.stderr and r.stdout == ""


def test_cli_symlinked_root_and_trailing_slash(tmp_path):
    repo = _repo(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(repo)
    assert _cli("--root", str(link)).exit_code == 0
    assert _cli("--root", str(repo) + "/").exit_code == 0
    assert _cli("--root", str(repo) + "/", env={"GIT_DIR": str(tmp_path / "nope")}).exit_code == 0
    stray = {"GIT_DIR": str(tmp_path / "nope"), "GIT_WORK_TREE": str(tmp_path / "elsewhere"),
             "GIT_INDEX_FILE": str(tmp_path / "no-index")}
    _put(repo, "README.md", "# hi\n\nmore\n")
    r = _cli("--root", str(repo), "--json", env=stray)
    assert r.exit_code == 0 and json.loads(r.stdout)["paths"] == ["README.md"]


def test_cli_git_failure_exits_3(tmp_path):
    repo = tmp_path / "unborn"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _put(repo, "a.txt", "x\n")
    _git(repo, "add", "a.txt")
    r = _cli("--root", str(repo), "--json")
    assert r.exit_code == 3 and "error: git " in r.stderr and r.stdout == ""


def test_reference_index_names_the_command():
    text = (REPO_ROOT / "docs/product/reference/index.md").read_text()
    section = text.split("### `nw queue …`", 1)[1].split("\n### ", 1)[0]
    assert "main-dirty-plan" in section


# ---- review loop 3: false-`trivial` guards -----------------------------------------------------

def _fc(p):
    return {f["path"]: f["file_class"] for f in p["files"]}


def test_staged_delete_with_the_file_still_in_the_worktree_is_a_delete(tmp_path):
    """`git rm --cached`: `new` is absent even though a same-name worktree file differs only in comments."""
    repo = _repo(tmp_path, {"src/x.py": "x = 1\n# a\n"})
    _git(repo, "rm", "-q", "--cached", "src/x.py")
    _put(repo, "src/x.py", "x = 1\n# b\n")
    assert [ln[:2] for ln in _dirty(repo).splitlines()] == ["D "]
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == S and _fc(p) == {"src/x.py": "code"}
    assert p["substantive_paths"] == ["src/x.py"]
    assert p["recommended"] == _save_list(repo, p["recommended"][0].split()[-1], "substantive", [])


def test_staged_delete_with_a_prose_copy_in_the_worktree_is_a_delete(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n\nprose\n"})
    _git(repo, "rm", "-q", "--cached", "docs/a.md")
    _put(repo, "docs/a.md", "# T\n\nprose 2\n")
    assert [ln[:2] for ln in _dirty(repo).splitlines()] == ["D "]
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == B and _fc(p) == {"docs/a.md": "other"}


@pytest.mark.parametrize("path,body,fclass,want", [
    ("src/new.py", "# only a comment\n\n# another\n", "code", S),
    ("src/new.py", "\n\n", "code", S),
    ("src/new.py", "# no final newline", "code", S),
    ("src/new.py", "", "code", S),
    ("docs/new.md", "# T\n\nsome prose\n", "other", B),
    ("docs/new.md", "---\nk: v\n---\n", "other", B),
])
def test_newly_added_tracked_file_has_no_old(tmp_path, path, body, fclass, want):
    """An absent `old` is never an empty string: a new all-comment file is not comment-only."""
    repo = _repo(tmp_path)
    _put(repo, path, body)
    _git(repo, "add", path)
    assert [ln[:2] for ln in _dirty(repo).splitlines()] == ["A "]
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == want and _fc(p) == {path: fclass}


@pytest.mark.parametrize("path,staged,edited,fclass,want", [
    ("src/new.py", "# c\n", "# c\n# d\n", "code", S),
    ("docs/new.md", "# T\n\nprose\n", "# T\n\nprose 2\n", "other", B),
])
def test_added_then_modified_file_has_no_old(tmp_path, path, staged, edited, fclass, want):
    repo = _repo(tmp_path)
    _put(repo, path, staged)
    _git(repo, "add", path)
    _put(repo, path, edited)
    assert [ln[:2] for ln in _dirty(repo).splitlines()] == ["AM"]
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"]) == want and _fc(p) == {path: fclass}
    assert p["recommended"][1].endswith(f"add -- {path}")


# ---- a non-main base reaches every list --------------------------------------------------------

def _R(repo):
    return shlex.quote(str(repo))


def test_non_main_base_lists_use_the_computed_base(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"}, base="develop")
    _put(repo, "untracked.md", "u\n")
    R = _R(repo)
    _git(repo, "switch", "-qc", "feat")
    assert MD.plan_clear(repo)["recommended"] == [f"git -C {R} switch develop"]        # hold, clean
    _put(repo, "docs/a.md", "# T\n\nx\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["base"]) == ("trivial", "hold", "develop")
    wip = p["recommended"][0].split()[-1]
    assert p["recommended"] == _save_list(repo, wip, "trivial", ["docs/a.md"], base="develop")
    assert p["recommended"][-1] == f"git -C {R} switch develop"
    _run_list(repo, p["recommended"])
    _after_run_checks(repo)
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "develop"
    assert _show(repo, wip, "docs/a.md") == "# T\n\nx\n"
    assert (repo / "untracked.md").exists()


@pytest.mark.parametrize("variant", ["save_branch", "branch_and_escalate"])
def test_non_main_base_save_lists_run_and_end_on_the_base(tmp_path, variant):
    path, old, new, want = {"save_branch": ("config/x.json", "{}\n", '{"a": 1}\n', B),
                            "branch_and_escalate": ("src/x.py", "x = 1\n", "x = 2\n", S)}[variant]
    repo = _repo(tmp_path, {path: old}, base="develop")
    _put(repo, path, new)
    _put(repo, "untracked.txt", "u\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["base"], p["on_base"]) == (*want, "develop", True)
    wip = p["recommended"][0].split()[-1]
    assert p["recommended"] == _save_list(repo, wip, p["class"], [path], base="develop")
    _run_list(repo, p["recommended"])
    _after_run_checks(repo)
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "develop"
    assert _show(repo, wip, path) == new
    assert "untracked.txt" not in _tree(repo, wip) and (repo / "untracked.txt").exists()


@pytest.mark.parametrize("key", ["merge", "base"])
def test_base_from_the_project_config_reaches_the_lists(tmp_path, key):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"}, base="release")
    _cfg(repo, {"merge": {"base": "release"}} if key == "merge" else {"base": "release"})
    _git(repo, "commit", "-qam", "cfg", "--allow-empty")
    _git(repo, "switch", "-qc", "feat")
    R = _R(repo)
    p = MD.plan_clear(repo)
    assert (p["base"], p["on_base"], p["action"], p["recommended"]) == (
        "release", False, "hold", [f"git -C {R} switch release"])
    _put(repo, "docs/a.md", "# T\n\nx\n")
    p = MD.plan_clear(repo)
    wip = p["recommended"][0].split()[-1]
    assert p["recommended"] == _save_list(repo, wip, "trivial", ["docs/a.md"], base="release")
    _run_list(repo, p["recommended"])
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "release"


# ---- cheap minors ------------------------------------------------------------------------------

def test_relative_root_gives_absolute_root_and_commands(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n"})
    _put(repo, "docs/a.md", "# T\n\nx\n")
    monkeypatch.chdir(repo)
    doc = json.loads(_cli("--root", ".", "--json").stdout)
    assert os.path.isabs(doc["root"]) and os.path.realpath(doc["root"]) == os.path.realpath(repo)
    assert doc["recommended"] and all(c.startswith(f"git -C {shlex.quote(doc['root'])} ")
                                      for c in doc["recommended"])
    assert not any(" -C . " in c for c in doc["recommended"])


def test_root_through_a_symlink_dotdot_is_canonical(tmp_path):
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "e" / "deep"
    elsewhere.mkdir(parents=True)
    (elsewhere / "lnk").symlink_to(repo)
    p = MD.plan_clear(elsewhere / "lnk" / ".." / "r")      # lexically tmp/e/deep/r: not a repo
    assert os.path.realpath(p["root"]) == os.path.realpath(repo)
    assert os.path.exists(os.path.join(p["root"], ".git"))


def test_wip_name_takes_the_third_suffix(tmp_path, monkeypatch):
    import datetime
    import types

    class FixedDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(MD, "dt", types.SimpleNamespace(datetime=FixedDT, timezone=datetime.timezone))
    repo = _repo(tmp_path, {"src/a.py": "x = 1\n"})
    _put(repo, "src/a.py", "x = 2\n")
    for b in ("wip/main-dirty-20260101T000000Z", "wip/main-dirty-20260101T000000Z-2"):
        _git(repo, "branch", b)
    assert MD.plan_clear(repo)["recommended"][0].endswith("wip/main-dirty-20260101T000000Z-3")


@pytest.mark.parametrize("quote", ["true", "false"])
def test_non_ascii_path_with_and_without_quotepath(tmp_path, quote):
    repo = _repo(tmp_path, {"docs/café.md": "# T\n", "src/ü.py": "x = 1\n"})
    _git(repo, "config", "core.quotePath", quote)
    _put(repo, "docs/café.md", "# T\n\nx\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["paths"], p["recommended"][0].endswith("add -- " + shlex.quote("docs/café.md"))) == (
        "trivial", ["docs/café.md"], True)
    _put(repo, "src/ü.py", "x = 2\n")
    p = MD.plan_clear(repo)
    assert (p["class"], sorted(p["paths"]), _fc(p)["src/ü.py"]) == (
        "substantive", ["docs/café.md", "src/ü.py"], "code")


def test_frontmatter_and_body_edited_together_is_docs(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "---\nk: v\n---\n\nbody\n"})
    _put(repo, "docs/a.md", "---\nk: w\n---\n\nbody 2\n")
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], _fc(p)) == ("trivial", "commit", {"docs/a.md": "docs"})


@pytest.mark.parametrize("new", ["---\nk: w\n---\nbody", "---\nk: v\n---\nbody"])
def test_frontmatter_eof_newline_after_the_closing_fence(tmp_path, new):
    """A frontmatter edit that also drops the body's final newline is not `frontmatter`."""
    repo = _repo(tmp_path, {"docs/a.md": "---\nk: v\n---\nbody\n"})
    _put(repo, "docs/a.md", new)
    p = MD.plan_clear(repo)
    assert (p["class"], _fc(p)["docs/a.md"]) == ("trivial", "docs")


def test_docs_prose_requires_a_visible_diff_and_keeps_the_eof_newline(tmp_path):
    repo = _repo(tmp_path, {"docs/a.md": "# T\n\nprose\n"})
    _git(repo, "update-index", "--refresh")
    (repo / "docs/a.md").write_bytes(b"# T\n\nprose\n")             # touched, identical
    assert MD.plan_clear(repo)["class"] == "clean"
    _put(repo, "docs/a.md", "# T\n\nprose")                          # only the final newline went
    p = MD.plan_clear(repo)
    assert (p["class"], _fc(p)["docs/a.md"]) == ("trivial", "docs")


def test_dirty_gitlink_with_a_merge_marker_is_merge(tmp_path):
    sub = _repo(tmp_path, {"f.txt": "x\n"}, name="sub")
    repo = _repo(tmp_path, {}, name="sup")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/sub")
    _git(repo, "commit", "-qm", "sub")
    _put(repo / "vendor/sub", "f.txt", "changed\n")
    marker = _git(repo, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip()
    (repo / marker).write_text(_git(repo, "rev-parse", "HEAD").stdout)
    p = MD.plan_clear(repo)
    assert (p["class"], p["action"], p["in_progress"], p["recommended"]) == ("merge", "escalate", "merge", [])


# ---- EOF newline, unknown suffix, extra hazards (adversarial review) ---------------------------

@pytest.mark.parametrize("path,fclass", [("src/x.py", "code"), ("tests/fixtures/x.txt", "code"),
                                         ("config/x.json", "other"), ("src/x.c", "code"),
                                         ("docs/x.yml", "other")])
@pytest.mark.parametrize("drop", [True, False])
def test_eof_newline_only_edit_is_never_comment(tmp_path, path, fclass, drop):
    old, new = ("a: 1\n", "a: 1") if drop else ("a: 1", "a: 1\n")
    repo = _repo(tmp_path, {path: old})
    _put(repo, path, new)
    p = MD.plan_clear(repo)
    assert _fc(p) == {path: fclass} and p["class"] != "trivial", p["files"]


@pytest.mark.parametrize("path", ["config/x.json", "tests/fixtures/x.txt", "web/x.svg"])
def test_unknown_suffix_is_never_comment_only(tmp_path, path):
    assert MD._comment_only(path, b"a\nb\n", b"a\nb\n\n") is False
    assert MD._comment_only(path, b"a\nb\n", b"a \nb\n") is False


@pytest.mark.parametrize("path,old,new", [
    ("src/x.rb", "x = 1\n__END__\ndata\n", "x = 1\n__END__\n# data\n"),
    ("src/x.pl", "x();\n__DATA__\nd\n", "x();\n__DATA__\n# d\n"),
    ("src/X.java", 's = """\nt\n""";\n', 's = """\n# t\n""";\n'),
    (".github/ci.yml", "run: |  # note\n  a\n", "run: |  # note\n  a\n  # b\n"),
    (".github/ci.yml", "run: |2\n  a\n", "run: |2\n  a\n  # b\n"),
    ("src/x.py", 'x = 1\ns = """\nopen\n', 'x = 1\ns = """\nopen\n# more\n'),
])
def test_more_hazards_are_never_trivial(tmp_path, path, old, new):
    repo = _repo(tmp_path, {path: old})
    _put(repo, path, new)
    assert MD.plan_clear(repo)["class"] != "trivial"


def test_cli_non_utf8_path_exits_3_without_output(tmp_path):
    repo = _repo(tmp_path)
    bad = os.fsencode(str(repo)) + b"/docs/bad\xff.md"
    os.makedirs(os.path.dirname(bad), exist_ok=True)
    with open(bad, "wb") as f:
        f.write(b"# T\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bad")
    with open(bad, "wb") as f:
        f.write(b"# T\n\nx\n")
    for args in (("--root", str(repo)), ("--root", str(repo), "--json")):
        r = _cli(*args)
        assert r.exit_code == 3 and "error:" in r.stderr and r.stdout == "", (r.stderr, r.stdout)
