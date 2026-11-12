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

- Request: `HMAC-SHA256(secret, "nw-req|" + METHOD + "|" + PATH + "|" + BODYSHA + "|" + TS)`
- Response: `HMAC-SHA256(secret, "nw-resp|" + STATUS + "|" + METHOD + "|" + PATH + "|" + BODYSHA + "|" + TS)`
  - METHOD and PATH are those of the request being answered.
  - BODYSHA is the hex SHA-256 of the response body.
  - STATUS is the decimal HTTP status.

This is a one-shot cutover. Hub and hosts are the operator's own machines and upgrade together.
A peer that sends an old-construction MAC fails verification with a distinct, documented error
(`bad signature (expected nw-req/nw-resp construction)`), so a version skew is obvious. The
GET/HEAD-with-body refusal from 0529 stays as defence in depth.

## Design

- `federation.sign` takes a required `direction` (`"req"` or `"resp"`) and, for responses,
  `status`. `verify` also requires them. No call site may omit the direction. A test enumerates
  the call sites.
- `request_headers`, `response_headers`, the host handler and `HubClient` pass the direction and
  status.
- The command and protocol reference documents both formulas, with one known vector each (secret
  `abc`, `GET /v1/status`, empty body, ts `1700000000`; the response vector at status 200). The
  existing `abc` vector test becomes the request vector.
- `test_wire_mac_is_the_documented_four_field_formula` is replaced by a test that a client
  hand-built from the documented formula gets 200. Requests use the new tagged formula.
- SPEC-0532 and any later endpoint use `sign(..., direction=...)`. A note in 0532's Context
  points here.

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
      (the 0529 case), for a bodied POST to a signed endpoint (e.g. the signed 405 on
      `POST /v1/events`, or a test-only POST route), and for a signed 401.
- [ ] A response MAC for status S does not verify for status T ≠ S.
- [ ] The documented request and response vectors are pinned by tests.
- [ ] A client built from the documented formula alone gets 200.
- [ ] An old four-field MAC gets the distinct construction error.
- [ ] The 0529 tests still pass, apart from the replaced four-field formula test: request-body
      binding, replay window and GET/HEAD-with-body refusal.
- [ ] The reference docs show both formulas and both vectors. SPEC-0532's Context links here.

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
