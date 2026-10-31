# Completion

Click 8 shell completion for **fish**, **bash**, and **zsh** (commands, flags, and
dynamic values like projects / SPEC ids / idea selectors).

## Install

```bash
nw completion install --shell fish
nw completion install --shell bash
nw completion install --shell zsh
nw completion fish                  # print script only
```

PATH `nw` must be **this** package. If TAB completion or `--help` looks like a static-site
generator, reinstall from the nightwright checkout (`uv tool install --editable .` / `--editable '.[tui]'`).

New closed-set CLI parameters need `shell_complete=` wiring in code; free-text args do
not. See `nw completion --help`.
