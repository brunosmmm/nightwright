"""`wt projects config|init` (SPEC-0170). Filled by that spec; `attach` is called by
`wt.agentic.cli.register` with the existing `projects` group."""


def attach(projects_group):
    """Register the per-project config subcommands on `projects_group` (SPEC-0170)."""
    return projects_group
