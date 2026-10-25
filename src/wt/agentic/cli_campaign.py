"""`wt campaign list|show|log|note` (SPEC-0171) and the hidden `prompt` (SPEC-0174). Filled by
those specs; `attach` is called by `wt.agentic.cli.register` with the `campaign` group."""


def attach(campaign_group):
    """Register the campaign subcommands on `campaign_group` (SPEC-0171)."""
    return campaign_group
