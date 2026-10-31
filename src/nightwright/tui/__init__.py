"""Optional interactive desk (SPEC-0098 epic; entry point SPEC-0099).

Textual is an **optional extra** (`nightwright[tui]`). Nothing in this package may be imported at
`nightwright.cli` module scope, and nothing here imports Textual until `run()` is called, so a base
install without the extra keeps working.
"""

INSTALL_HINT = (
    "nw tui needs Textual, which ships as an optional extra.\n"
    "  from a checkout:  uv sync --extra tui\n"
    "  installed tool:   uv tool install 'nightwright[tui]'  (or: pip install 'nightwright[tui]')"
)


class MissingTextual(RuntimeError):
    """Raised by `run()` when the optional `tui` extra is not installed."""

    def __init__(self, message: str = INSTALL_HINT):
        super().__init__(message)


def textual_available() -> bool:
    """True when the optional extra is importable. Never imports Textual itself."""
    from importlib.util import find_spec

    try:
        return find_spec("textual") is not None
    except (ImportError, ValueError):
        return False


def run(cfg, **kwargs):
    """Launch the ideas desk. Raises `MissingTextual` when the extra is absent.

    The availability probe is deliberately separate from the import: an `ImportError` from a
    real bug inside `nightwright.tui.app` must not masquerade as a missing dependency.
    """
    if not textual_available():
        raise MissingTextual()
    from .app import run_app

    return run_app(cfg, **kwargs)
