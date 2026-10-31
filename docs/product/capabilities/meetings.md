# Meetings

Outlook (M365) meetings feed the same hours pipeline as transcripts.

## Commands

```bash
nw refresh-meetings              # today
nw refresh-meetings --week
nw refresh-meetings --days N
nw ingest-meetings RAWFILE       # filter a raw calendar JSON dump
```

`refresh-meetings` pulls via the configured headless Claude + m365 MCP path into
`meetings.jsonl`. After refresh, hours show up in `nw` / `nw report` like any other
source. Date scopes match [Track time](../guides/track-time.md).
