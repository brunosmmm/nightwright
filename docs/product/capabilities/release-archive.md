# Release archive

Pack **git HEAD** plus a filtered ideas org slice into one `.tar.gz` for handoff
installs.

## Commands

```bash
nw release-archive
nw release-archive -o /tmp/nightwright.tgz --project Meta-Tools
nw release-archive --no-all          # omit done/archived ideas from the slice
```

Default output path is beside the repo: `../work-tracking-YYYYMMDD-<sha>.tar.gz`.
Project filter defaults to Meta-Tools for the ideas slice.
