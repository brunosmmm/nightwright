"""`nw hosts` (SPEC-0529): federation enrolment and the hub host registry."""
from __future__ import annotations

import json
import socket
import sys

import click

from . import federation as F
from . import federation_host as FH
from . import hosts as H
from . import pull_jobs as PJ
from .store import now_iso


@click.group("hosts")
def hosts_group():
    """Federated project hosts (SPEC-0529).

    \b
    nw hosts add NAME --url URL     register and print a one-time enrolment blob
    nw hosts list                   names, URLs, and enabled state
    nw hosts check                  overlap and reachability of enabled hosts
    nw hosts on|off NAME            enable or disable a host
    nw hosts rm NAME                unregister (reminder to unenrol on the host)
    nw hosts enrol                  read the blob from stdin on a host
    nw hosts unenrol                remove host-side federation credentials
    """


def _update(cfg, fn):
    try:
        return H.update(cfg, fn)
    except (OSError, ValueError) as e:
        raise click.ClickException(str(e))


@hosts_group.command("add")
@click.argument("name")
@click.option("--url", required=True, metavar="URL", help="base URL of `nw serve --federate`")
@click.pass_obj
def hosts_add(cfg, name, url):
    if not H.NAME_RE.fullmatch(name):
        raise click.UsageError(f"NAME must match {H.NAME_RE.pattern}")
    try:
        url = H.validate_url(url)
    except ValueError as e:
        raise click.UsageError(str(e))
    if any(r["name"] == name for r in H.load(cfg)):
        raise click.ClickException(f"host {name!r} already registered")
    token, secret = F.new_host_credential()
    rec = {"name": name, "url": url, "token_sha256": PJ.token_hash(token),
           "secret_ref": F.host_secret_ref(name), "enabled": True, "status_cache": None}

    def add(recs):
        if any(r["name"] == name for r in recs):
            raise click.ClickException(f"host {name!r} already registered")
        recs.append(rec)

    _update(cfg, add)
    F.write_host_secrets(cfg, name, token, secret)
    blob = {"hub_name": socket.gethostname(), "host_name": name, "token": token, "secret": secret}
    click.echo(json.dumps(blob, separators=(",", ":")))


@hosts_group.command("list")
@click.pass_obj
def hosts_list(cfg):
    for r in H.load(cfg):
        en = "on" if r.get("enabled", True) else "off"
        click.echo(f"{r['name']}\t{r.get('url', '')}\t{en}")


@hosts_group.command("rm")
@click.argument("name")
@click.pass_obj
def hosts_rm(cfg, name):
    if not H.NAME_RE.fullmatch(name):
        raise click.UsageError(f"NAME must match {H.NAME_RE.pattern}")
    if not any(r.get("name") == name for r in H.load(cfg)):
        raise click.ClickException(f"no host {name!r}")

    def rm(recs):
        recs[:] = [r for r in recs if r.get("name") != name]

    _update(cfg, rm)
    F.drop_host_secrets(cfg, name)
    click.echo("run nw hosts unenrol on the host")


@hosts_group.command("on")
@click.argument("name")
@click.pass_obj
def hosts_on(cfg, name):

    def fn(recs):
        H.find(recs, name)["enabled"] = True

    try:
        _update(cfg, fn)
    except ValueError as e:
        raise click.ClickException(str(e))


@hosts_group.command("off")
@click.argument("name")
@click.pass_obj
def hosts_off(cfg, name):

    def fn(recs):
        H.find(recs, name)["enabled"] = False

    try:
        _update(cfg, fn)
    except ValueError as e:
        raise click.ClickException(str(e))


@hosts_group.command("check")
@click.pass_obj
def hosts_check(cfg):
    from . import federation as Fed
    recs = H.load(cfg)
    if not recs:
        return
    hub_projects = set(FH.status_document(cfg)["projects"])
    host_projects: dict[str, set[str]] = {}
    exit_code = 0
    for rec in recs:
        name = rec["name"]
        if not rec.get("enabled", True):
            click.echo(f"{name} off")
            continue
        try:
            client = Fed.HubClient(rec, cfg)
            doc = client.get_json("/v1/status")
        except (ConnectionError, ValueError, OSError) as e:
            click.echo(f"{name} unreachable: {e}")
            exit_code = 1
            continue
        if doc.get("schema") != "nw.federation.status.v1":
            click.echo(f"{name} unreachable: bad schema")
            exit_code = 1
            continue
        projects = [p for p in (doc.get("projects") or []) if isinstance(p, str)]
        campaigns = doc.get("campaigns") if isinstance(doc.get("campaigns"), list) else []

        def store_cache(recs_):
            for r in recs_:
                if r.get("name") == name:
                    r["status_cache"] = {"fetched_at": now_iso(), "projects": projects,
                                         "campaigns": campaigns}

        try:
            H.update(cfg, store_cache)
        except (OSError, ValueError) as e:
            click.echo(f"{name} unreachable: {e}")
            exit_code = 1
            continue
        click.echo(f"{name} ok")
        host_projects[name] = set(projects)
    for p in sorted(hub_projects):
        for name, projs in host_projects.items():
            if p in projs:
                click.echo(f"project {p} on hub and {name}")
                exit_code = 1
    seen = sorted(host_projects)
    for i, a in enumerate(seen):
        for b in seen[i + 1:]:
            shared = host_projects[a] & host_projects[b]
            for p in sorted(shared):
                pair = sorted([a, b])
                click.echo(f"project {p} on {pair[0]} and {pair[1]}")
                exit_code = 1
    if exit_code:
        raise SystemExit(1)


@hosts_group.command("enrol")
@click.pass_obj
def hosts_enrol(cfg):
    line = sys.stdin.readline()
    if not line.strip():
        raise click.ClickException("empty enrolment blob on stdin")
    try:
        doc = json.loads(line)
    except ValueError as e:
        raise click.ClickException(f"not JSON: {e}")
    if not isinstance(doc, dict):
        raise click.ClickException("enrolment blob must be a JSON object")
    keys = ("hub_name", "host_name", "token", "secret")
    if set(doc) != set(keys) or not all(isinstance(doc[k], str) and doc[k] for k in keys):
        raise click.ClickException("enrolment blob needs hub_name, host_name, token, secret")
    F.write_federation(cfg, doc)


@hosts_group.command("unenrol")
@click.pass_obj
def hosts_unenrol(cfg):
    F.federation_path(cfg).unlink(missing_ok=True)
