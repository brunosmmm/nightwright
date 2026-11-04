"""A fake `tailscale` for tests (SPEC-0416, SPEC-0417). Models `tailscale ip`, `tailscale status
--json` and `tailscale whois --json <ip>`; any other subcommand (or form) exits 2, so a test fails
if the server ever runs `up`, `set`, `serve`, `cert` or `login`. Every call appends its argv as one
JSON line to $FAKE_TAILSCALE_LOG; $FAKE_TAILSCALE_MODE = ok | fail (exit 1) | hang (sleeps past
the timeout) | garbage (output that is no address / no JSON), for every subcommand;
$FAKE_TAILSCALE_STATE names a JSON scenario:
  {"own_ips": [...]}                         what `ip` prints (and the host's own whois addresses)
  {"logged_out": true}                       `ip` prints `no current Tailscale IPs; state:
                                             NeedsLogin` (exit 1); `status --json` prints
                                             {"BackendState": "NeedsLogin"} (exit 0, no `Self`);
                                             `whois` exits 1 (all as the real binary does)
  {"self": {"user_id": 7, "login": "me@example.com", "dns_name": "host.tail1234.ts.net.",
            "tags": []}}                     the host (default as shown)
  {"peers": {"100.64.0.20": {"login": ..., "name": ..., "computed_name": ..., "tags": []}}}
                                             the known peers; an unknown peer exits 1; an own
                                             address is the host's node with its login
  {"modes": {"whois": "fail"}}               a per-subcommand mode overriding the global one

`install(tmp_path, scenario, mode)` writes an executable `tailscale` into `tmp_path/tsbin` and
returns the env additions (`PATH` prefix, the three variables); `calls(env)` reads the log."""
import json
import os
import sys
import time
from pathlib import Path

SRC = Path(__file__)

DEFAULT_SELF = {"user_id": 7, "login": "me@example.com", "dns_name": "host.tail1234.ts.net.",
                "tags": []}
DEFAULT_PEERS = {"100.64.0.20": {"login": "me@example.com", "name": "phone.tail1234.ts.net.",
                                 "computed_name": "phone", "tags": []}}


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


def _load_state():
    try:
        with open(os.environ["FAKE_TAILSCALE_STATE"]) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _status(st):
    if st.get("logged_out"):
        return {"BackendState": "NeedsLogin"}
    me = st.get("self", DEFAULT_SELF)
    uid = me.get("user_id")
    return {"BackendState": "Running",
            "Self": {"UserID": uid, "DNSName": me.get("dns_name", ""), "Tags": me.get("tags") or [],
                     "TailscaleIPs": st.get("own_ips", [])},
            "User": {str(uid): {"ID": uid, "LoginName": me.get("login", "")}}}


def _whois(st, ip):
    """The whois answer for `ip` (`Node` + `UserProfile`), or None for an unknown peer."""
    me = st.get("self", DEFAULT_SELF)
    if ip in st.get("own_ips", ["100.64.0.5"]):
        tags = me.get("tags") or []
        peer = {"login": "tagged-devices" if tags else me.get("login", ""),
                "name": me.get("dns_name", ""), "computed_name": me.get("dns_name", "").split(".")[0],
                "tags": tags}
    else:
        peer = st.get("peers", DEFAULT_PEERS).get(ip)
        if peer is None:
            return None
    tags = peer.get("tags") or []
    node = {"ComputedName": peer.get("computed_name", ""), "Name": peer.get("name", ""),
            "Addresses": [f"{ip}/32"]}
    if tags:
        node["Tags"] = tags
    return {"Node": node, "UserProfile": {"LoginName": peer.get("login", "")}}


def main():
    argv = sys.argv[1:]
    with open(os.environ["FAKE_TAILSCALE_LOG"], "a") as f:
        f.write(json.dumps(argv) + "\n")
    st = _load_state()
    sub = argv[0] if argv else ""
    if not (argv == ["ip"] or argv == ["status", "--json"]
            or (len(argv) == 3 and argv[:2] == ["whois", "--json"])):
        sys.stderr.write(f"fake tailscale: unsupported command {argv!r}\n")
        sys.exit(2)
    mode = (st.get("modes") or {}).get(sub) or os.environ.get("FAKE_TAILSCALE_MODE", "ok")
    if mode == "hang":
        time.sleep(30)
    if mode == "fail":
        sys.exit(1)
    if mode == "garbage":
        print("<<not an address>>\n{\"json\": true}\n8.8.8.8" if sub == "ip" else "<<not json>>")
        sys.exit(0)
    if sub == "status":
        print(json.dumps(_status(st)))
        return
    if sub == "whois":
        out = None if st.get("logged_out") else _whois(st, argv[2])
        if out is None:
            sys.stderr.write("fake tailscale: no match for IP\n")
            sys.exit(1)
        print(json.dumps(out))
        return
    if st.get("logged_out"):
        sys.stderr.write("no current Tailscale IPs; state: NeedsLogin\n")
        sys.exit(1)
    for ip in st.get("own_ips", []):
        print(ip)


if __name__ == "__main__":
    main()
