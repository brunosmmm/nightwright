"""`python -m nightwright` (SPEC-0291: `nw queue run --detach` falls back to it when `sys.argv[0]` is
not an executable)."""
from .cli import cli

cli()
