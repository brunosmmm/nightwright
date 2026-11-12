---
id: SPEC-0595
title: "Federation wire MAC: domain-separate request and response signatures"
status: accepted
owner: user
created: 2026-11-12
kind: feature
parent: SPEC-0310
depends_on: ["SPEC-0529"]
tags: [federation, security]
---

## Context

[SPEC-0529](./0529-federation-enrolment-and-the-signed-host-channel.md) (done, merged 55710717)
signs requests and enrolled responses with one construction, HMAC-SHA256 over
`METHOD|PATH|BODYSHA|TS`. Review loop 7 domain-separated the two directions. Run 099 reverted
that because 0529's Design is frozen on the four-field formula. As shipped, a signed response
cannot be replayed as a request only because the host refuses a GET or HEAD that carries a body,
and today the host channel serves only GET and HEAD.

[SPEC-0532](./0532-exactly-once-mail-between-hub-and-host.md) adds `POST /v1/mail`. A signed
response to `POST /v1/mail` with body R would then verify as a request `POST /v1/mail` with body
R inside the timestamp window. A bearer-token holder without the secret could forge requests
that way. This is the reflection oracle 0529's security loop set out to close.

The human chose, via ESC-0072 on 2026-11-12, to fix this with a new small spec that lands before
SPEC-0532 opens. This spec amends only the wire-MAC part of 0529's Design. The rest of 0529 stands,
and 0529 stays `done`.

## Goals / Non-goals

**Goals**
- A request MAC and a response MAC can never verify in each other's place, for any method,
  path, body or status.
- The construction is documented precisely enough that a client built from the docs alone works
  against the host.

**Non-goals**
- Enrolment, tokens, the secret format, the timestamp window and the replay rules from 0529 are
  unchanged.
- No dual-version acceptance. Accepting the old construction would keep the oracle open.

## Decision

Prefix each signed string with a direction tag. A response also binds the status code of the
request it answers.

The signed line is ASCII, with fields joined by `|` and no trailing newline:

- Request line: `nw-req|METHOD|PATH|BODYSHA|TS`
- Response line: `nw-resp|STATUS|METHOD|PATH|BODYSHA|TS`

The fields are defined as follows. Everything not listed here is exactly as in 0529's Design.

| Field | Definition |
|---|---|
| METHOD | The request method, uppercase. On a response, the method of the request being answered. |
| PATH | Exactly what 0529 signs today: the host's `_path()` result on the host side, and the `target` passed to `HubClient.request` on the client side. On a response, the target of the request being answered. |
| BODYSHA | Lowercase hex SHA-256 of the raw body bytes: the request body for a request, the response body as sent for a response. HEAD responses sign the empty body. |
| TS | Decimal unix seconds, as in 0529. |
| STATUS | The response's decimal HTTP status code, e.g. `200`, `401`, `405`. |

The MAC is lowercase hex HMAC-SHA256 of the line's UTF-8 bytes, keyed with the secret's UTF-8
bytes. The header names are unchanged from 0529.

**Why STATUS is bound:** a signed 4xx or 5xx body can't be presented as a signed 200, or the other
way round. That matters once a client acts on non-200 statuses. SPEC-0532 treats a 4xx as terminal
and retries a 5xx.

**Version marker:** every authenticated request and every signed response carries
`X-NW-Sig-V: 2`. The header name is case-insensitive and the value must be exactly `2`. Unenrolled
replies (0529's unsigned `401 unauthorized`) are unchanged and carry no marker. There is no dual
acceptance; hub and hosts are the operator's own machines and upgrade together.

**Host check order** (each step answers with 0529's existing reply unless stated otherwise):
1. Not enrolled: unsigned `401 unauthorized`, as in 0529.
2. Bearer token wrong: as in 0529.
3. Marker missing or not `2`: `401 {"error":"sig_version"}`, signed with the new construction.
4. Timestamp skew, then MAC: as in 0529. An old four-field MAC fails here as a bad MAC.
5. GET/HEAD with a body: as in 0529.

An old client only ever sees a 401, the same as any auth failure. The version error is for new
clients and for anyone reading the raw reply.

**`HubClient` check order** for every response. Today it raises `HTTP <n>` for a non-200 before
checking anything. The new order replaces that:
1. No `X-NW-Sig` headers on a 401 with body `{"error":"unauthorized"}`: raise
   `ConnectionError("host not enrolled")`. This is not terminal for callers; it is an operator
   fix.
2. Marker missing or not `2`: raise `ConnectionError("host uses an old signature version")`.
3. Signature headers missing, or a bad timestamp or MAC: keep 0529's existing messages
   (`missing response signature`, `stale signature`, `bad signature`). These are signature
   failures and never terminal outcomes, so a forged 4xx can't end a retry loop.
4. Verified non-200: raise `ConnectionError("HTTP <n>")`, as today.
5. Verified 200: return.

The GET/HEAD-with-body refusal from 0529 stays as defence in depth.

## Design

- `federation.sign` and `verify` take a required keyword `direction`, with no default.
  - A missing direction raises `TypeError`.
  - A value other than `"req"` or `"resp"` raises `ValueError`.
  - `status` is required (an int from 100 to 599) when `direction="resp"` and forbidden when
    `direction="req"`. Breaking either rule raises `ValueError`.
- An AST test parses every module under `src/nightwright`, including `federation.py` itself.
  It finds calls to `sign` or `verify` that refer to `nightwright.agentic.federation`: bare names
  inside `federation.py`, and `F.sign`/`F.verify` or `federation.sign`/`federation.verify` where
  the module is imported. It asserts that each call passes `direction=` as a keyword. Other
  `sign`/`verify` look-alikes (e.g. `hmac`) are ignored.
- `request_headers`, `response_headers`, the host handler and `HubClient` pass the direction and
  status.
- The protocol reference (`docs/product/reference/`, next to the existing host-channel section)
  documents the field table, both line formats and two vectors: secret `abc`, `GET /v1/status`,
  empty body, ts `1700000000`, giving one request vector and one response vector at status 200.
  The implementer computes both vectors with a standalone one-liner that does not import
  nightwright (`python3 -c 'import hmac,hashlib; …'`). The one-liner and its output go in As shipped
  and in the reference. The pinned tests compare `federation.sign` to those literal strings. The
  old `abc` vector test is replaced.
- `test_wire_mac_is_the_documented_four_field_formula` is replaced by a test that a client
  hand-built from the documented formula gets 200. Requests use the new tagged formula.
- **Ordering:** SPEC-0532's frontmatter `depends_on` now includes `SPEC-0595`, and its Context
  has one sentence that links here. The steward added both as a docs-only commit when this spec
  was accepted. This campaign does not edit 0532.
- **Reflection test route:** the host has no signed POST endpoint today. The test monkeypatches
  the host handler's route table in the fixture to add `POST /v1/_test_echo`, which returns a
  signed 200 JSON body. This route is never shipped.

## Alternatives considered

- **Put the requirement into SPEC-0532:** rejected by the human (ESC-0072 choice 2). It would mix
  a security change into the mail work.
- **Keep the four-field MAC and refuse bodied requests on every path that also returns signed
  bodies:** fragile. Every new POST endpoint would reopen the oracle.
- **Accept both constructions for a transition window:** keeps the oracle open for that window.
  Not needed for a single operator.

## Acceptance criteria

- [ ] Requests and responses are signed with the tagged formulas above. The request and
      response vectors are pinned by tests, and the vectors were computed independently, with the
      one-liner recorded. A client built only from the documented formula gets 200.
- [ ] A signed response replayed as a request fails verification. This is tested for a GET (the
      0529 case), for a bodied POST to the test-only route, and for a signed 401.
- [ ] A response MAC for status S does not verify for status T ≠ S, and a signed 401 whose status
      is changed to 200 fails in `HubClient`.
- [ ] The host check order is as listed. A missing or wrong marker gets the signed
      `sig_version` 401. A v2-marked request with an old four-field MAC gets `401 unauthorized`.
- [ ] `HubClient` applies its check order as listed, so a forged or unsigned 4xx raises a
      signature error, never `HTTP 4xx`.
- [ ] `sign` and `verify` reject a missing direction (`TypeError`), a bad direction or bad
      `status` usage (`ValueError`). The AST test finds no call site without `direction=`.
- [ ] The 0529 tests still pass apart from the replaced formula test, and the reference docs
      show the field table, both formulas, both vectors and both check orders.

## Test plan

- **Automated:** extend `tests/test_federation_channel.py` with the direction, reflection
  (GET, POST and 401), status-binding, vector, doc-built-client and old-construction cases.
  Run the touched tests and the docs tests.
- **Gate:** the campaign's close gate on the box.
- **Manual:** none. Everything is checkable from the worktree.

## Rollout / migration

Hub and hosts upgrade together. A host enrolled under 0529 keeps its token and secret; only the
signing construction changes. If a peer is not upgraded, its requests fail with the construction
error until it is.

## Definition of done

Per AGENTS.md.

## Open questions

None.

## As shipped

_To be filled in by the implementing campaign._
