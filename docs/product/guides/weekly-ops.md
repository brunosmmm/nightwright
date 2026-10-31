# Weekly ops

Close the week: name unknown topics, sanity-check segments, archive history, and don’t
leave idea/spec loops half-closed.

## 1. Hours this week

```bash
nw report --week
nw report --week --by bucket    # or project / other facet axis you use
```

![Weekly report](../assets/captures/cli-report-week.svg)

## 2. Map the unknowns

```bash
nw topics --unmapped
nw map <base-topic> bucket=Acme area=docs
```

![Unmapped topics](../assets/captures/cli-topics-unmapped.svg)

![map](../assets/captures/cli-map.svg)

Bare `nw map topic Acme` is shorthand for `bucket=Acme`.

## 3. Review segments

```bash
nw review --week            # timeline + unclassified inbox
nw review --week --fix   # map interactively when you want
```

![review](../assets/captures/cli-review-week.svg)

## 4. Keep history

Claude Code deletes old transcripts. Snapshot closed days before they vanish:

```bash
nw snapshot                 # through yesterday by default
```

## 5. Idea / outbound hygiene

```bash
nw hub --json               # stale ideas, open questions, outbound debt
nw next
nw idea questions IDEA-… --resolve N   # clear what the accepted spec decided
```

![hub](../assets/captures/cli-hub.svg)

For work exported to another repo, treat **pull-status / sweep → idea SHIPPED** as required
close-the-loop (see [Outbound](outbound.md)) — not an optional footnote. Multi-repo epics:
[Multi-project](multi-project.md) (`seed-children`).

## Related

- Day rhythm: [Daily ops](daily-ops.md)
- Deeper time model: [Track time](track-time.md) · [Time & retention](../concepts/time-and-retention.md)
