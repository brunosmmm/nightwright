"""Shared rich Console instance (kept in its own module to avoid import cycles)."""
from rich.console import Console, Group
from rich.text import Text


def _line_shaped(obj) -> bool:
    """True for renderables that are just lines of text: `str`, `Text`, or a `Group` of those."""
    if isinstance(obj, (str, Text)):
        return True
    if isinstance(obj, Group):
        return all(_line_shaped(r) for r in obj.renderables)
    return False


def stdout_is_tty(con: Console | None = None) -> bool:
    """Whether `con` is actually a terminal. Rich's `is_terminal` is also true when
    `FORCE_COLOR` is set to anything, including `0`, which would hard-wrap a pipe and
    put `nw queue watch` on the alternate screen (SPEC-0443). An explicit
    `force_terminal` still wins, so tests can choose either side."""
    con = console if con is None else con
    forced = getattr(con, "_force_terminal", None)
    if forced is not None:
        return bool(forced)
    isatty = getattr(con.file, "isatty", None)
    try:
        return bool(isatty and isatty())
    except (ValueError, OSError):
        return False


class NwConsole(Console):
    """A Console that never hard-wraps text lines when it is not a terminal (SPEC-0197), so
    piped output (`runner.log`, `| grep`) keeps one record per line. Tables, panels and the
    console width are left to Rich. A set `FORCE_COLOR` does not count as a terminal."""

    def print(self, *objects, soft_wrap=None, **kw):
        if soft_wrap is None and not stdout_is_tty(self) and objects \
                and all(_line_shaped(o) for o in objects):
            soft_wrap = True
        super().print(*objects, soft_wrap=soft_wrap, **kw)


console = NwConsole()
