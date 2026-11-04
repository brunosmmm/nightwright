"""SPEC-0413: the dashboard phone page's static files and the one lookup the server routes
through. `ROUTES` is the allowlist (exact paths only), `read_asset` reads from the package with
`importlib.resources` (so it works from a wheel), `CSP` is the policy the server sends with them.

Opens no socket and writes nothing; no queue-side module imports it (SPEC-0400 invariant 2)."""
from __future__ import annotations

from importlib import resources

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

# request path -> (file under `static/`, content type)
ROUTES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}


CSRF_PLACEHOLDER = b"__NW_CSRF__"


def render_index(body: bytes, csrf: str) -> bytes:
    """SPEC-0428: `index.html` with this page's CSRF value in `<meta name="nw-csrf">`. The value
    is `[A-Za-z0-9_-.]` only (`auth.Csrf.mint`), so it needs no escaping."""
    if not all(c.isalnum() or c in "-_." for c in csrf):
        raise ValueError("csrf value has unexpected characters")
    return body.replace(CSRF_PLACEHOLDER, csrf.encode("ascii"), 1)


def read_asset(path: str) -> tuple[bytes, str] | None:
    """`(bytes, content_type)` for an exact `ROUTES` path, else None: no query string, no `..`,
    no `/index.html`, no other path ever reaches the filesystem."""
    route = ROUTES.get(path)
    if route is None:
        return None
    name, content_type = route
    data = (resources.files(__package__) / "static" / name).read_bytes()
    return data, content_type
