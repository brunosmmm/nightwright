---
id: SPEC-0416
title: "Dashboard tailnet bind gate: own-address resolution, --bind tailscale and TailnetMissing (exit 3)"
status: proposed
owner: user
created: 2026-11-04
milestone: "M9: agentic workflow"
kind: feature
source_idea: IDEA-910
tags: [agentic, dashboard, remote, web, auth, tailscale]
depends_on: [SPEC-0414]
---

## Context

This is the first of two slices of the tailnet half of child 1 of the epic
[SPEC-0400](./0400-remote-web-dashboard-live-view-of-active-queue.md).
[SPEC-0414](./0414-dashboard-server-nw-serve-dashboard-spec-0400.md) (done) shipped the loopback
server and deliberately listed the tailnet as its sibling in its Non-goals: today
`nw serve --dashboard` refuses `tailscale`, tailnet addresses and every other non-loopback bind.
The human's decision (epic Decision 2) is **Tailscale only, nothing on the public internet**.

The tailnet work is a security boundary in two layers: *which socket the server opens* (the bind
gate) and *who may read through it* (the identity gate, `tailscale whois`). They are separable, and
the identity gate must never be missing while a tailnet socket is open. This spec is the **bind
gate slice**: it teaches the gate this host's own tailnet addresses, resolves `--bind tailscale`,
and defines `TailnetMissing` (exit 3). It deliberately **does not serve on the tailnet**: an
accepted own tailnet address is still refused, with a dedicated message, until the sibling identity
gate slice (see Non-goals) lifts that one refusal. So nothing this spec ships can expose the queue,
and every rule below is fail closed and pinned by a test that fails when the rule is removed.

SPEC-0404 (epic child 4, the lifecycle spec: systemd unit, `nw dashboard status`) has an
acceptance criterion that the server **exits 3 when this host has no tailnet address**, so a unit
can tell "tailnet not up yet" from a crash. That exit code and the exception behind it are defined
here, reachable with `--bind tailscale`, which is why SPEC-0404's AC3 depends on this spec.

What exists today (checked against the code on 2026-11-04):

- `src/nightwright/agentic/dashboard/auth.py`: `BindRefused(ValueError)`, `parse_bind` (a host
  that is not an IP, e.g. `tailscale`, is returned as given), `check_bind(host)` (loopback only;
  wildcard, non-IP and everything else refused), `validate_bind(spec)`, `local_token(cfg)`,
  `host_values` and `check_host_origin`.
- `src/nightwright/agentic/dashboard/server.py`: `make_server(cfg, bind=None, *, cache=None)`
  calls `validate_bind` itself, so no caller can skip the gate.
- `src/nightwright/agentic/cli_worker.py:252` `_serve_dashboard`: bind gate, then the TLS refusal,
  then `make_server`; `BindRefused` is exit 2, an `OSError` is exit 1; the default bind is the
  loopback `127.0.0.1:8791`.
- `settings(cfg)["bind"]` already returns `tailscale:8791` (SPEC-0401); the CLI does not read it
  and keeps doing so in this spec.
- `tailscale` 1.102.4 is installed on this host and is **logged out** (`tailscale ip` prints `no
  current Tailscale IPs; state: NeedsLogin`), so the failure path can be checked with the real
  binary today; the success path needs the human to log in (epic Rollout step 1).
- SPEC-0414's tests forbid running `tailscale` on a loopback bind: the CLI tests put a recording
  stub first on `PATH` (`tests/test_dashboard_serve_cli.py:24-29`) and assert its call file is
  absent. That stays true for every loopback, wildcard, LAN, public and host-name bind. This spec
  is the only one that runs `tailscale`, and only the read-only form `tailscale ip`.

SPEC-0414 named the new exception `TailscaleDown` (exit 1). This spec supersedes that sentence
only: the exception is `TailnetMissing` and its exit code is **3**, so a supervisor can tell it
from a bind failure (exit 1) and a usage or policy refusal (exit 2).

## Goals / Non-goals

**Goals**
- G1. `--bind tailscale[:PORT]` resolves to this host's own tailnet address with
  `auth.own_tailnet_ips()`.
- G2. The bind gate classifies every address: loopback is accepted; one of this host's own tailnet
  addresses is recognised (and, in this slice, refused because the identity gate does not exist
  yet); wildcards, LAN and public addresses, another machine's tailnet address and host names are
  refused. TLS flags never lift or reorder a refusal.
- G3. A host with no tailnet address (tailscale missing, logged out, stopped, or failing) makes
  the server exit with code **3** through a named exception, `TailnetMissing`, with a message that
  says what to do. That includes an explicit address inside the tailnet ranges on such a host.
- G4. Nothing in this slice opens a non-loopback socket, and the CLI default stays loopback.
- G5. All of it is provable in CI with a fake `tailscale` executable, without a tailnet, plus a
  short manual check with the real binary.

**Non-goals** (the rest of IDEA-910; the sibling slice **"Dashboard tailnet whois identity gate
and tailnet default bind"**, to be written as its own spec with `depends_on: [SPEC-0416]`)
- Serving on the tailnet: the `tailscale whois --json` identity gate (`Verdict`/`Gate`),
  `allowed_logins` with its own-login default, tagged-node refusal, the 60 s / 5 s verdict cache,
  the exclusive token/whois auth modes, no `local.token` in whois mode, Host/Origin with the
  MagicDNS name, identity fields in the audit line, the `server.json` `auth: "whois"` value, the
  `gate`/`peer_addr`/`auth` test seams, the `tailscale status --json` and `tailscale whois`
  commands, and the real-phone check.
- Lifting this spec's refusal of an own tailnet address, and switching the CLI default bind from
  loopback to `settings(cfg)["bind"]` (`tailscale:8791`). Both happen together in the sibling.
- Writes, CSRF, plan tokens, the inbox payload (epic child 2). TLS, `tailscale cert`, MagicDNS HTTPS
  and push (epic child 5): `--tls-cert/--tls-key/--client-ca` with `--dashboard` stay refused,
  after the bind gate.
- The systemd unit, `nw dashboard status` and the `conduct-status` `dashboard` key (SPEC-0404). Until
  the sibling flips the default, a unit that wants exit 3 on a missing tailnet passes
  `--bind tailscale` explicitly.
- Any change to `snapshot.py`, `queue.dashboard_payload`, `pull_jobs.py` or the `nw serve --jobs`
  mode. The queue still never imports the dashboard.

## Decision

1. **The bind gate learns the tailnet; the socket still never leaves loopback.**
   `auth.check_bind(host, own=())` keeps its order (wildcard, non-IP, loopback pass) and adds
   one branch: an address inside the Tailscale ranges (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) is
   accepted by `check_bind` only if it is in `own`, the set from `own_tailnet_ips()`; otherwise it
   is "foreign tailnet address". Any other address (LAN, public, link-local) is refused without
   running `tailscale`.
2. **`resolve_bind` is the one entry point and holds the temporary refusal.**
   `resolve_bind(spec, own_fn=own_tailnet_ips) -> (host, port)` parses, resolves `tailscale`,
   calls `check_bind`, and then, for an accepted own tailnet address, raises `BindRefused`
   ("refusing to bind <addr>: tailnet serving needs the identity gate, which is not available
   yet; bind loopback with `--bind 127.0.0.1:PORT`"), exit 2. That single refusal is the only
   thing the sibling slice removes. `make_server` and the CLI both call `resolve_bind`, so no
   caller can skip it, and the CLI TLS check still runs after it.
3. **`--bind tailscale[:PORT]` is resolved, not parsed as a host name.** It resolves (case
   insensitive) to this host's first own IPv4 tailnet address (an IPv6 one if there is no IPv4),
   port default 8791, and is then checked like any address, so in this slice it ends in the
   Decision 2 refusal when the host has a tailnet and in `TailnetMissing` when it does not.
4. **No tailnet address is `TailnetMissing`, exit 3.** `own_tailnet_ips()` raises
   `auth.TailnetMissing(RuntimeError)` when `tailscale` is not on `PATH`, exits non-zero (logged
   out, stopped), times out (5 s), prints nothing usable, or prints no address in the Tailscale
   ranges. An explicit address inside the tailnet ranges on a host where `own_tailnet_ips()`
   fails is `TailnetMissing` (exit 3), the same as `--bind tailscale`. `serve_cmd` maps it to a
   `click.ClickException` whose `exit_code` is 3. `BindRefused` stays exit 2 and a bind `OSError`
   stays exit 1. No mapping inspects message text. The order of checks is: parse, resolution and
   bind gate (2, or 3), TLS refusal (2), bind (1).
5. **The CLI default stays loopback.** With no `--bind`, `nw serve --dashboard` binds
   `127.0.0.1:8791` with the bearer `local.token`, exactly as SPEC-0414, and never runs
   `tailscale`. `agentic.dashboard.bind` is still not read by the CLI.
6. **The server may run one `tailscale` form: `tailscale ip`.** Always an argv list (no shell),
   with a 5 s timeout, never with a value taken from a request.
7. **The test seam is keyword-only and unreachable from outside.**
   `make_server(cfg, bind=None, *, cache=None, own=None)`: `own` replaces `own_tailnet_ips`.
   `serve_cmd` does not pass it, and no flag, environment variable or config key reaches it.

## Design

### `auth.py` additions

- `class TailnetMissing(RuntimeError)`.
- `TAILNET_V4 = 100.64.0.0/10`, `TAILNET_V6 = fd7a:115c:a1e0::/48`; `is_tailnet_ip(addr)` (an
  IPv4-mapped IPv6 address is unmapped first).
- `run_tailscale(args, *, timeout=5.0) -> str`: `subprocess.run(["tailscale", *args],
  capture_output=True, text=True, timeout=timeout, check=False)`, no shell. Raises
  `TailscaleError(RuntimeError)` for a missing binary, a timeout, a non-zero exit or non-UTF-8
  output. `TailscaleError` is a private detail, turned into `TailnetMissing` by `own_tailnet_ips`.
- `own_tailnet_ips(run=run_tailscale) -> list[str]`: runs `tailscale ip`, one address per line,
  keeps the lines that are tailnet IPs (IPv4 first), de-duplicates. `TailscaleError` or an empty
  result raises `TailnetMissing` with "no tailnet address on this host (`tailscale` is missing,
  logged out or stopped): run `tailscale up`, or bind loopback with `--bind 127.0.0.1:PORT`".
- `check_bind(host, own=())`: as in Decision 1. The messages carry the rule words the tests assert:
  `wildcard`, `not an IP`, `not a loopback or tailnet address`, `foreign tailnet address`. Called
  with the default `own=()` it accepts loopback only plus the tailnet-range case above.
- `resolve_bind(spec, own_fn=own_tailnet_ips)`: as in Decisions 2-4. A host equal to `tailscale`
  and an address inside the tailnet ranges call `own_fn()` exactly once; every other address is
  decided by `check_bind` without calling `own_fn`. After `check_bind` accepts an address that is
  not loopback, the identity-gate refusal of Decision 2 is raised; it is one `if` with a comment
  naming the sibling slice.
- SPEC-0414's `validate_bind` stays as the loopback-only function it is (its unit tests are
  unchanged); `serve_cmd` and `make_server` call `resolve_bind` instead.
- The module still imports nothing from the queue, and `snapshot.py` still does not import `auth`.

### `server.py`

`make_server(cfg, bind=None, *, cache=None, own=None)` resolves with
`resolve_bind(bind or DEFAULT_BIND, own_fn=own or own_tailnet_ips)` instead of `validate_bind`.
Everything after that is SPEC-0414 unchanged (loopback only, token mode, `server.json` with
`auth: "token"`).

### `cli_worker._serve_dashboard`

1. `host, port = DA.resolve_bind(bind or DEFAULT_BIND)` (the loopback default): `BindRefused` →
   `click.UsageError` (exit 2); `TailnetMissing` → a `click.ClickException` subclass with
   `exit_code = 3` (message printed to stderr, no traceback).
2. A TLS flag set → `click.UsageError("TLS is not supported with --dashboard")` (exit 2). Because
   step 1 ran first, a refused or missing-tailnet bind always reports its own message, never the
   TLS one.
3. `make_server(cfg, f"{host}:{port}")` (it resolves again, so a caller cannot skip the gate);
   `OSError` → exit 1.
4. Start line and shutdown as today.

Docs: `docs/product/reference/index.md` documents `--bind tailscale[:PORT]` (resolution, and that
serving on the tailnet is refused until the identity gate lands) and exit 3; `docs/WORKFLOW.md`
(the `agentic.dashboard` section SPEC-0401 started) notes that `bind` is not read by the server yet.

### Fake `tailscale` fixture

`tests/fixtures/fake_tailscale.py`, in the style of `fake_herdr.py`. A helper
`install(tmp_path, scenario) -> env` writes an executable `tailscale` first on `PATH` and a JSON
scenario file named by `$FAKE_TAILSCALE_STATE`; every call appends its argv as one JSON line to
`$FAKE_TAILSCALE_LOG`; `$FAKE_TAILSCALE_MODE` is `ok` | `fail` (exit 1) | `hang` (sleeps past the
timeout) | `garbage` (non-JSON or non-address output). The scenario holds `own_ips`, or
`logged_out: true`, which makes `ip` print `no current Tailscale IPs; state: NeedsLogin` and exit
1, as the real binary does. In this slice it models only `ip` and **rejects any other subcommand**
with exit 2, so a test fails if the server ever runs `up`, `set`, `serve`, `cert`, `status` or
`whois`. The sibling slice extends the fake with `status --json` and `whois --json <ip>`.

## Alternatives considered

- **Serve on the tailnet in this slice behind the existing bearer token.** Two ways in later, and
  a shipped tailnet socket whose authentication is replaced in the next slice; the temporary
  refusal costs nothing and cannot expose the queue.
- **Ship the whole feature as one spec.** Sixteen criteria across two security layers is more than
  one campaign can verify; the layers have a clean seam (the single refusal in `resolve_bind`).
- **Make the tailnet the CLI default now.** With the refusal in place the default would make every
  `nw serve --dashboard` fail; the default flips together with the refusal in the sibling.
- **Keep the loopback default and make the tailnet opt-in forever.** The epic decided `tailscale`
  is the default; this spec only defers the flip.
- **Bind the wildcard and filter by source address.** One mistake in the filter exposes the LAN; the
  bind gate keeps the socket itself off every other interface. Rejected in the epic as well.
- **A separate `TailscaleDown` (exit 1) and `TailnetMissing`.** One exception, one exit code, one
  remedy; the message distinguishes the cause.
- **Pass `own` through an environment variable for tests.** An env var is reachable from outside the
  test; a keyword-only seam on `make_server` is not.

## Acceptance criteria

- [ ] `auth.own_tailnet_ips()` returns this host's tailnet addresses from `tailscale ip` (IPv4
      first, only addresses in `100.64.0.0/10` or `fd7a:115c:a1e0::/48`), and raises
      `TailnetMissing` when `tailscale` is not on `PATH`, exits non-zero, times out, prints
      garbage, or prints no tailnet address; `is_tailnet_ip` is correct at both range edges and
      for IPv4-mapped IPv6.
- [ ] `resolve_bind` table: loopback → accepted without calling `own_fn`; wildcard (`0.0.0.0`,
      `::`, `[::]`), LAN, public, link-local and host-name binds → `BindRefused` with the rule
      words `wildcard` / `not a loopback or tailnet address` / `not an IP` without calling `own_fn`;
      a foreign tailnet address (`100.64.0.9` when own is `100.64.0.5`) → `BindRefused` "foreign
      tailnet address"; `tailscale`, `TAILSCALE`, `tailscale:9000` and an own tailnet address →
      `BindRefused` "tailnet serving needs the identity gate"; `own_fn` is called exactly once for
      `tailscale` and for tailnet-range addresses.
- [ ] A host with no tailnet address (`tailscale` missing from `PATH`, logged out, mode `fail`,
      `hang`, `garbage`) makes `nw serve --dashboard --bind tailscale` and
      `--bind 100.64.0.9:0` exit **3** with a message that names `tailscale up`; nothing listens
      and no `server.json` exists. The exit code comes from `TailnetMissing`, not from message
      text (a unit test raises it directly from a stubbed `own`), and combining the bind with TLS
      flags still exits 3.
- [ ] No socket is ever opened on a non-loopback address: with the fake reporting own address
      `100.64.0.5`, `--bind tailscale` and `--bind 100.64.0.5:0` exit **2** naming "identity
      gate", with and without TLS flags (the gate message, never the TLS one), nothing listening
      and no `server.json`; `make_server(cfg, "100.64.0.5:0", own=...)` raises `BindRefused`
      before a socket is created.
- [ ] The CLI bind gate exits **2**, stderr naming its rule word and `refusing to bind`, nothing
      listening and no `server.json`, for `0.0.0.0`, `::`, `[::]`, a LAN address
      (`192.168.1.20`), a public address (`8.8.8.8`), a foreign tailnet address (`100.64.0.9` with
      the fake's own `100.64.0.5`) and a host name, each alone and each combined with
      `--tls-cert/--tls-key` (existing files), in which case stderr is the bind-rule message and
      not the TLS one. A wildcard, LAN, public or host-name bind never runs `tailscale` (the
      fake's log is empty).
- [ ] A loopback bind combined with TLS flags still exits 2 with `TLS is not supported with
      --dashboard`; `--bind 127.0.0.1:PORT` and the no-`--bind` default still serve with
      `auth: "token"` and never run `tailscale` (the fake's log is empty), and the CLI default bind
      is still `127.0.0.1:8791`.
- [ ] `make_server` applies the same gate: calling it directly with each refused bind raises
      `BindRefused` (or `TailnetMissing`) before a socket is created; `own` is keyword-only on
      `make_server`, is not passed by `serve_cmd` / `_serve_dashboard` (checked by an AST test)
      and is not reachable from a flag, an environment variable or a config key.
- [ ] The only `tailscale` subcommand the server runs is `tailscale ip`, with an argv list and no
      shell: over a full CLI/server test run the fake's log holds no other subcommand.
- [ ] The SPEC-0414 loopback guarantees still hold: all of its tests pass **except the rows this
      spec deliberately changes**, which are listed in the Test plan and move to the tailnet
      tests; read-only over HTTP, the no-import invariants and `nw serve --jobs` behavior are
      unchanged.
- [ ] `tests/fixtures/fake_tailscale.py` has a self-test and rejects every subcommand other than
      `ip`; docs list `--bind tailscale` and exit 3; `python3 tools/spec_lint.py` exits 0.
- [ ] Manual, real binary: on this host (logged out) `uv run nw serve --dashboard --bind
      tailscale` exits 3 with the real `tailscale`; after the human logs in, the same command
      exits 2 naming the identity gate and the address it names equals `tailscale ip -4`. Anything
      not possible yet is recorded as pending in As shipped, not claimed.

## Test plan

**Automated tests.** Waits use `tests/waits.py` (`NW_TEST_WAIT_S`), never fixed sleeps. CLI cases
run the venv's `nw` as a real subprocess with `NW_DATA_DIR` set to the
`tests/fixtures/dashboard_data.py` seed and the fake `tailscale` first on `PATH`.

- `tests/test_fake_tailscale.py` (fixture self-test): `ip` (own IPs, `logged_out`), modes
  `fail`/`hang`/`garbage`, an unknown subcommand (`up`, `set`, `serve`, `cert`, `status`, `whois`)
  → exit 2, argv log.
- `tests/test_dashboard_tailnet_auth.py` (unit, no sockets): `is_tailnet_ip` table (both ranges,
  edges `100.63.255.255`/`100.64.0.0`/`100.127.255.255`/`100.128.0.0`, IPv4-mapped);
  `own_tailnet_ips` for each failure mode (missing binary, logged out, fail, hang, garbage, only
  non-tailnet lines) → `TailnetMissing`, success ordering IPv4 first, IPv6 only; the
  `check_bind`/`resolve_bind` table from the second acceptance criterion (loopback, wildcard
  v4/v6/`[::]`, LAN, public, link-local, own tailnet, foreign tailnet, host name, `tailscale`,
  `tailscale:0`, `TAILSCALE`) with the rule word in each message and a counter proving which rows
  call `own_fn`; a tailnet-range address and `tailscale` with a failing `own_fn` →
  `TailnetMissing` (the explicit-address row of Decision 4); `TailnetMissing` is not a
  `BindRefused`.
- `tests/test_dashboard_tailnet_server.py` (`make_server` directly, `own=` stubs): every refused
  bind raises `BindRefused` / `TailnetMissing` with no listener on 8791 and no `server.json`; own
  tailnet address → "identity gate" `BindRefused` and no socket; loopback serves with
  `auth: "token"` and `own` is never called; `own` is keyword-only (`inspect.signature`).
- `tests/test_dashboard_tailnet_cli.py` (real subprocess): exit 3 for logged out, missing binary
  (empty `PATH` dir), `fail`, `hang`, `garbage`, with `--bind tailscale` and with `--bind
  100.64.0.9:0` (the new logged-out row), each alone and with TLS flags (still 3); exit 2 with
  the rule word for `0.0.0.0:0`, `[::]:0`, `192.168.1.20:0`, `8.8.8.8:0`, `example.com:8791` and
  `100.64.0.9:0` (fake ok, own `100.64.0.5` → "foreign tailnet address"), each alone and with TLS
  flags, stderr lacking `TLS is not supported`, the fake's log empty for the non-tailnet ones;
  `--bind tailscale` and `--bind 100.64.0.5:0` (fake ok) → exit 2 "identity gate", with and
  without TLS flags, nothing listening; `--bind 127.0.0.1:0` and no `--bind` serve with
  `auth: "token"` and an empty fake log.
- `tests/test_dashboard_tailnet_seams.py`: an `ast` check that `serve_cmd`/`_serve_dashboard` pass
  no `own` and that `make_server` has it keyword-only; grep that no `os.environ` read feeds it.
- **Updated SPEC-0414 tests (every row whose expected result changes, and nothing else):**
  - `tests/test_dashboard_server.py::test_make_server_runs_the_bind_gate_itself`: the rows
    `100.64.0.9:0` (was "loopback") and `tailscale` (was "not an IP") are removed here and move to
    `tests/test_dashboard_tailnet_server.py`: `100.64.0.9:0` → `BindRefused` "foreign tailnet
    address" with an `own` stub returning `100.64.0.5`; `tailscale` → `TailnetMissing` with an
    `own` stub that raises it. The rows `0.0.0.0:0`, `[::]:0`, `192.168.1.20:0` ("loopback" is
    still in "not a loopback or tailnet address") and `example.com:8791` are unchanged.
  - `tests/test_dashboard_serve_cli.py` (`REFUSED`, `test_refused_binds_exit_2_with_the_rule`):
    the rows `100.64.0.9:0` and `tailscale` are removed here and move to
    `tests/test_dashboard_tailnet_cli.py`, which uses the fake (`100.64.0.9:0` fake ok → exit 2
    "foreign tailnet address"; logged out → exit 3; `tailscale` logged out → exit 3, fake ok →
    exit 2 "identity gate"). The recording stub exits 1 and would now yield exit 3, and
    `100.64.0.9:0` / `tailscale` now run it, so the `not (tailscale.calls).exists()` assertion
    stays only for the remaining rows (`0.0.0.0:0`, `[::]:0`, `::`, `192.168.1.20:0`,
    `example.com:8791`), which still never run `tailscale`.
  - `tests/test_dashboard_auth.py::test_check_bind`: the row `("100.64.0.9", "loopback")` moves to
    the new table with the word "foreign tailnet address" (the default `own=()` makes every
    tailnet address foreign). The `validate_bind` rows (lines 50-51, still loopback-only), the
    `parse_bind` rows and `("tailscale", "not an IP")` for `check_bind` are unchanged.
  - `test_serve_snapshot_token_sigterm_and_no_tailscale` and every other SPEC-0414 case are
    unchanged: all already pass `--bind` or use the loopback default, which this spec keeps.
- Docs: a grep of `docs/product/reference/index.md` finds `--bind tailscale` and the exit code 3;
  `python3 tools/spec_lint.py` exits 0.
- Regression guard: the SPEC-0414 and SPEC-0401 dashboard tests, the SPEC-0334 `--jobs` tests and
  `python3 tools/affected_tests.py --run uv run pytest -q`. The queue's close gate runs the
  affected set.

**Manual verification (this host, recorded in As shipped; the real-binary rule).**
1. Logged out (today): `uv run nw serve --dashboard --bind tailscale` and `uv run nw serve
   --dashboard --bind 100.64.0.9:8791` print the `tailscale up` message and `echo $?` is 3;
   `ss -ltn` shows nothing on 8791.
2. `uv run nw serve --dashboard --bind 0.0.0.0:8791` exits 2 naming the wildcard rule; `uv run nw
   serve --dashboard` still serves on `127.0.0.1:8791` with the token.
3. After the human runs `sudo tailscale up` and `sudo tailscale set --operator=$USER`: `uv run nw
   serve --dashboard --bind tailscale` exits 2 naming the identity gate and the resolved address,
   which equals `tailscale ip -4`; `ss -ltn` shows nothing on it. Step 3 needs the human's login;
   if not done yet it stays pending, and this spec's shipped record says so.

## Rollout / migration

1. Land the code, the fake fixture and the tests; move the SPEC-0414 rows listed in the Test plan.
2. Nothing running changes: the default bind is still loopback, no unit is installed yet
   (SPEC-0404), and no tailnet socket can be opened. Anyone passing `--bind tailscale` now gets
   exit 3 (no tailnet) or exit 2 (identity gate not available yet), both with a message naming the
   loopback alternative.
3. The sibling identity-gate slice removes the single refusal in `resolve_bind`, adds the gate and
   flips the default; it supersedes only that sentence of this spec.

## Definition of done

- [ ] Acceptance criteria all met.
- [ ] Test plan executed; affected tests pass (`python3 tools/affected_tests.py --run uv run pytest
      -q`) plus the files touched; manual steps 1-2 performed and step 3 recorded as done or pending.
- [ ] No regressions (SPEC-0414 / SPEC-0401 dashboard tests apart from the listed moved rows,
      SPEC-0334 `--jobs` tests).
- [ ] Spec body updated to match what shipped.
- [ ] `docs/LEDGER.md` regenerated (`python3 tools/spec_lint.py --write-ledger`) and
      `python3 tools/spec_lint.py` exits 0.

## Open questions

- None. The `tailscale whois` JSON shapes and whether it works without root belong to the sibling
  identity-gate slice; this slice only runs `tailscale ip`.
