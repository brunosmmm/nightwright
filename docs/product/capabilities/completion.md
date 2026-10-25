# Completion

Click 8 shell completion for **fish**, **bash**, and **zsh** (commands, flags, and
dynamic values like projects / SPEC ids / idea selectors).

## Install

```bash
wt completion install --shell fish
wt completion install --shell bash
wt completion install --shell zsh
wt completion fish                  # print script only
```

PATH `wt` must be **this** package. If TAB completion or `--help` looks like a static-site
generator, reinstall from the wt checkout (`uv tool install --editable .` / `--editable '.[tui]'`).

New closed-set CLI parameters need `shell_complete=` wiring in code; free-text args do
not. See `wt completion --help`.
