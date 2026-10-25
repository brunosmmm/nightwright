"""`wt queue gates` (SPEC-0173) and `wt queue add|list|watch|tick|run|park|resume|abandon`
(SPEC-0176). Filled by those specs; `attach` is called by `wt.agentic.cli.register` with the
`queue` group."""


def attach(queue_group):
    """Register the queue subcommands on `queue_group` (SPEC-0173 / SPEC-0176)."""
    return queue_group
