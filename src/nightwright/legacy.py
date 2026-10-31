"""Legacy names that nightwright still READS after the wt -> nightwright rename (SPEC-0330).

Stored data is a format, not a user-facing name: ledgers, events, mailbox, decisions, reviews,
campaign state, exports, the spec-reservation store, `.nw/project.yaml` and profile files written
before the rename carry `wt.<x>.v1` schema ids. Readers accept those; every write uses
`nw.<x>.v1`. Nothing here is a user-facing alias: the CLI, env vars, config dirs and skills are
hard-cut (no `wt` fallback)."""
from __future__ import annotations

PREFIX = "nw."
LEGACY_PREFIX = "wt."


def legacy_schema(schema: str) -> str:
    """The pre-rename spelling of a schema id: `nw.<x>.v1` -> `wt.<x>.v1`."""
    if isinstance(schema, str) and schema.startswith(PREFIX):
        return LEGACY_PREFIX + schema[len(PREFIX):]
    return schema


def schema_ids(schema: str) -> tuple[str, str]:
    """(current, legacy) ids a reader accepts for `schema`."""
    return (schema, legacy_schema(schema))


def schema_ok(value, schema: str) -> bool:
    """True when a stored `schema` value is `schema` or its legacy `wt.` spelling."""
    return value in schema_ids(schema)
