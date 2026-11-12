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
| PATH | The request target as sent: path plus `?query` if any, not normalised. On a response, the target of the request being answered. |
| BODYSHA | Lowercase hex SHA-256 of the raw body bytes: the request body for a request, the response body as sent for a response. HEAD responses sign the empty body. |
| TS | Decimal unix seconds, as in 0529. |
| STATUS | The response's decimal HTTP status code, e.g. `200`, `401`, `405`. |

The MAC is lowercase hex HMAC-SHA256 of the line's UTF-8 bytes, keyed with the secret's UTF-8
bytes. The header names are unchanged from 0529.

**Why STATUS is bound:** a signed 4xx or 5xx body can't be presented as a signed 200, or the other
way round. `HubClient` verifies the response MAC against the status it actually received, for
every enrolled response including non-200 ones, before it acts on the status or the body.

**Version marker and cutover:** every signed request and signed response also carries
`X-NW-Sig-V: 2`.
- A host that receives an authenticated request without `X-NW-Sig-V: 2` answers
  `401 {"error":"sig_version"}`, signed with the new construction. That is the documented
  delivery path for a version skew. It tells an old client to upgrade and reveals nothing about
  its MAC.
- `HubClient` treats an enrolled response without `X-NW-Sig-V: 2` as the error
  `host uses an old signature version; upgrade it`.
- There is no dual acceptance. Hub and hosts are the operator's own machines and upgrade together.

The GET/HEAD-with-body refusal from 0529 stays as defence in depth.

## Design

- `federation.sign` takes a required keyword `direction` (`"req"` or `"resp"`) and, for
  responses, a required `status`. `verify` takes the same arguments. Neither has a default, so a
  missing direction is a `TypeError`. A test checks that `sign("abc", "GET", "/v1/status", b"",
  1700000000)` without `direction` raises. A second test greps `src/nightwright` and asserts that
  every `sign(` and `verify(` call passes `direction=`.
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
- This campaign's diff also adds one sentence to SPEC-0532's Context. The sentence links here and
  says that new endpoints sign with `direction=`. Nothing else in 0532 changes.
- A signed POST endpoint for the reflection test: the host has none today. The test registers a
  test-only POST route on the host handler through the test fixture. It is not shipped. It
  returns a signed 200 JSON body.

## Alternatives considered

- **Put the requirement into SPEC-0532:** rejected by the human (ESC-0072 choice 2). It would mix
  a security change into the mail work.
- **Keep the four-field MAC and refuse bodied requests on every path that also returns signed
  bodies:** fragile. Every new POST endpoint would reopen the oracle.
- **Accept both constructions for a transition window:** keeps the oracle open for that window.
  Not needed for a single operator.

## Acceptance criteria

- [ ] Requests and responses are signed with the tagged formulas above, and `verify` rejects a
      MAC signed for the other direction.
- [ ] A signed response replayed as a request fails verification. This is tested for a GET
      (the 0529 case), for a bodied POST to the test-only signed POST route, and for a signed 401.
- [ ] A response MAC for status S does not verify for status T ≠ S.
- [ ] The documented request and response vectors are pinned by tests.
- [ ] A client built from the documented formula alone gets 200.
- [ ] A request without `X-NW-Sig-V: 2` gets a signed `401 {"error":"sig_version"}`.
      `HubClient` reports the old-host error for a response without the marker.
- [ ] Calling `sign` or `verify` without `direction` raises, and every call site in
      `src/nightwright` passes it (grep test).
- [ ] The 0529 tests still pass, apart from the replaced four-field formula test: request-body
      binding, replay window and GET/HEAD-with-body refusal.
- [ ] The reference docs show the field table, both formulas and both vectors. The vectors were
      computed independently of nightwright, and the one-liner is recorded.
- [ ] SPEC-0532's Context has the one-sentence link to this spec. A docs test asserts that the
      link is present.

## Test plan

- **Automated:** extend `tests/test_federation_channel.py` with the direction, reflection
  (GET, POST and 401), status-binding, vector, doc-built-client and old-construction cases.
  Run the touched tests and the docs tests.
- **Gate:** the campaign's close gate on the box.
- **Manual:** none. Everything is checkable from the worktree.

## Clock Log

## Rollout / migration

Hub and hosts upgrade together. A host enrolled under 0529 keeps its token and secret; only the
signing construction changes. If a peer is not upgraded, its requests fail with the construction
error until it is.

## Definition of done

Per AGENTS.md.

## Open questions

None.
