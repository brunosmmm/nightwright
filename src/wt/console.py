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


class WtConsole(Console):
    """A Console that never hard-wraps text lines when it is not a terminal (SPEC-0197), so
    piped output (`runner.log`, `| grep`) keeps one record per line. Tables, panels and the
    console width are left to Rich."""

    def print(self, *objects, soft_wrap=None, **kw):
        if soft_wrap is None and not self.is_terminal and objects \
                and all(_line_shaped(o) for o in objects):
            soft_wrap = True
        super().print(*objects, soft_wrap=soft_wrap, **kw)


console = WtConsole()
