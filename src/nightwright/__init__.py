"""nightwright — passive work-time tracker.

Turns Claude Code / Cursor transcripts (+ a cached meetings file) into daily/weekly
per-topic hour reports. Three-layer, idempotent design:

  sources (immutable: transcripts, meetings.jsonl)
    -> derived (recomputed every run: events, blocks, topics)
    -> rules   (human-owned YAML: mappings.yaml, overrides.yaml)

The parser only ever (re)derives from immutable sources; all human classification lives in
the rules YAML and is applied at report time. So re-running over old transcripts can never
clobber manual work.
"""

__version__ = "0.1.0"
