"""Click surface for the agentic workflow (SPEC-0169). The groups are created here; each child
spec fills its own submodule via `attach(group)` so parallel branches never edit the same
file: `cli_projects` (SPEC-0170), `cli_campaign` (SPEC-0171), `cli_queue` (SPEC-0173/0176).
`register(cli, projects_group)` is called once from `wt.cli`."""
import click


@click.group("queue")
def queue_group():
    """Agentic work queue (SPEC-0176): add accepted specs, tick/run the loop, park/resume.

    \b
    wt queue add SPEC-0170 --yes       queue an accepted spec (dry-run without --yes)
    wt queue list                      what is in flight, in which state, at what cost
    wt queue run                       the loop (one process; lease-guarded)
    """


@click.group("campaign")
def campaign_group():
    """Campaign ledgers (SPEC-0171): show state, read the event log, append notes.

    \b
    wt campaign list                   all campaigns
    wt campaign show CAMPAIGN          state + last events
    wt campaign log CAMPAIGN           the append-only ledger
    """


def register(cli, projects_group):
    """Attach the agentic commands to the root `cli` and the existing `projects` group."""
    from . import cli_campaign, cli_projects, cli_queue

    cli_projects.attach(projects_group)
    cli_campaign.attach(campaign_group)
    cli_queue.attach(queue_group)
    cli.add_command(queue_group)
    cli.add_command(campaign_group)
