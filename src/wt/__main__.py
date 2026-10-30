"""`python -m wt` (SPEC-0291: `wt queue run --detach` falls back to it when `sys.argv[0]` is
not an executable)."""
from .cli import cli

cli()
