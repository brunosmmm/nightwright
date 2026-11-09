"""SPEC-0413: the asset allowlist and the static checks over the shipped page files."""
import re
from importlib import resources

import pytest

from nightwright.agentic.dashboard import assets

pytestmark = pytest.mark.fast

EXPECTED_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
FILES = {"index.html": "text/html; charset=utf-8", "app.js": "application/javascript; charset=utf-8",
         "app.css": "text/css; charset=utf-8"}


def shipped(name):
    return (resources.files("nightwright.agentic.dashboard") / "static" / name).read_text("utf-8")


def test_read_asset_allow_table():
    for path, name in (("/", "index.html"), ("/app.js", "app.js"), ("/app.css", "app.css")):
        data, ctype = assets.read_asset(path)
        assert data == shipped(name).encode("utf-8") and data
        assert ctype == FILES[name]
    assert set(assets.ROUTES) == {"/", "/app.js", "/app.css"}


@pytest.mark.parametrize("path", ["/index.html", "/../x", "/app.js?x=1", "/static/app.js", "",
                                  "app.js", "/app.js/", "//app.js", "/App.js", "/app.css#x"])
def test_read_asset_denies_everything_else(path):
    assert assets.read_asset(path) is None


def test_csp_is_one_line_and_exact():
    assert assets.CSP == EXPECTED_CSP and "\n" not in assets.CSP


FORBIDDEN_JS = ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(",
                "new Function", "EventSource", "setInterval", "XMLHttpRequest",
                'setAttribute("style"', "setAttribute('style'"]


def test_app_js_forbidden_tokens():
    js = shipped("app.js")
    assert [t for t in FORBIDDEN_JS if t in js] == []
    assert not re.search(r"""setAttribute\(\s*["'](href|src|action)["']""", js)


def test_no_external_urls_in_any_file():
    for name in FILES:
        text = shipped(name)
        assert "http://" not in text and "https://" not in text, name
        assert not re.search(r"""(?:["'(=]\s*)//[A-Za-z0-9]""", text), name       # protocol-relative
        assert "@import" not in text, name


def test_index_html_has_no_inline_code():
    html = shipped("index.html")
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S):
        assert "src=" in m.group(1) and m.group(2).strip() == ""
    assert "<style" not in html
    assert not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert 'href="/app.css"' in html and 'src="/app.js"' in html


def test_css_layout_rules():
    css = shipped("app.css")
    for m in re.finditer(r"(?<![-\w])(min-width|width)\s*:\s*(\d+(?:\.\d+)?)px", css):
        assert float(m.group(2)) <= 360, m.group(0)
    assert re.search(r"overflow-wrap\s*:\s*anywhere", css)
    assert re.search(r"min-height\s*:\s*44px", css)
    assert re.search(r"@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)", css)
    assert "url(" not in css

