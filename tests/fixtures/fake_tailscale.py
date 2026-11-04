"""A fake `tailscale` for tests (SPEC-0416). Models only `tailscale ip`; any other subcommand
exits 2, so a test fails if the server ever runs `up`, `set`, `serve`, `cert`, `status` or
`whois`. Every call appends its argv as one JSON line to $FAKE_TAILSCALE_LOG;
$FAKE_TAILSCALE_MODE = ok | fail (exit 1) | hang (sleeps past the timeout) | garbage (output that
is no address); $FAKE_TAILSCALE_STATE names a JSON scenario: {"own_ips": [...]} or
{"logged_out": true} (`ip` prints `no current Tailscale IPs; state: NeedsLogin`, exit 1, as the
real binary does).

`install(tmp_path, scenario, mode)` writes an executable `tailscale` into `tmp_path/tsbin` and
returns the env additions (`PATH` prefix, the three variables); `calls(env)` reads the log."""
import json
import os
import sys
import time
from pathlib import Path

SRC = Path(__file__)


def install(tmp_path, scenario=None, mode="ok", base_env=None):
    """The env (a copy of `base_env` or `os.environ`) with the fake first on `PATH`."""
    bindir = Path(tmp_path) / "tsbin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "tailscale"
    exe.write_text(f"#!{sys.executable}\n" + SRC.read_text())
    exe.chmod(0o755)
    state = Path(tmp_path) / "tailscale-state.json"
    state.write_text(json.dumps(scenario or {"own_ips": ["100.64.0.5"]}))
    env = dict(os.environ if base_env is None else base_env)
    env.update(PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
               FAKE_TAILSCALE_STATE=str(state), FAKE_TAILSCALE_MODE=mode,
               FAKE_TAILSCALE_LOG=str(Path(tmp_path) / "tailscale.log"))
    return env


def calls(env):
    p = Path(env["FAKE_TAILSCALE_LOG"])
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def main():
    argv = sys.argv[1:]
    mode = os.environ.get("FAKE_TAILSCALE_MODE", "ok")
    with open(os.environ["FAKE_TAILSCALE_LOG"], "a") as f:
        f.write(json.dumps(argv) + "\n")
    if argv != ["ip"]:
        sys.stderr.write(f"fake tailscale: unsupported command {argv!r}\n")
        sys.exit(2)
    if mode == "hang":
        time.sleep(30)
    if mode == "fail":
        sys.exit(1)
    if mode == "garbage":
        print("<<not an address>>\n{\"json\": true}\n8.8.8.8")
        sys.exit(0)
    try:
        with open(os.environ["FAKE_TAILSCALE_STATE"]) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    if st.get("logged_out"):
        sys.stderr.write("no current Tailscale IPs; state: NeedsLogin\n")
        sys.exit(1)
    for ip in st.get("own_ips", []):
        print(ip)


if __name__ == "__main__":
    main()
