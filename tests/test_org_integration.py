"""Epic integration (SPEC-0004): parse the real ~/work/org tree end-to-end and exercise the
time→task join invariant on real tracked data. Skips cleanly when the org tree is absent, so
the suite stays green on any machine."""
import os

import pytest

from nightwright.config import load_config
from nightwright.org import join_time, load_tasks
from nightwright.topics import KEY_RE

ORG = os.path.expanduser("~/work/org")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.path.isdir(ORG), reason='no ~/work/org on this machine')]


def test_parses_real_tree_without_crashing():
    # Parses end-to-end and holds an environment-independent invariant (the tree's task
    # counts are user data — e.g. `nw`'s own wipe can empty it — so we don't assert on them).
    tasks = load_tasks(load_config())
    assert isinstance(tasks, list)
    # done-resolution is coherent: anything marked done must carry a TODO state keyword
    assert all(t.state is not None for t in tasks if t.is_done)


def test_headline_jira_key_becomes_topic_key():
    tasks = load_tasks(load_config())
    keyed = [t for t in tasks if t.topic_key and KEY_RE.fullmatch(t.topic_key)]
    # the real tree has JIRA-in-headline tasks (e.g. DEMO-100); if present, they must resolve
    for t in keyed:
        assert KEY_RE.search(t.heading) or any(
            t.properties.get(p) == t.topic_key for p in ("TOPIC", "JIRA", "REPO"))


def test_join_invariant_on_real_tracked_data():
    from nightwright.aggregate import assemble
    from nightwright.report import _scope_topic_secs
    cfg = load_config()
    data, _ = assemble(cfg)
    if not data:
        pytest.skip("no tracked time on this machine")
    topic_secs = _scope_topic_secs(cfg, sorted(data))
    # pick any tracked topic that is a bare JIRA key; synthesize a task for it and confirm the
    # join attributes exactly that topic's tracked seconds to the task's key (the invariant).
    jira_topics = [tp for tp in topic_secs if KEY_RE.fullmatch(tp)]
    if not jira_topics:
        pytest.skip("no bare JIRA-key topics tracked")
    from nightwright.org import Task
    key = jira_topics[0]
    t = Task(id="x", heading=f"{key} synthetic", state="TODO", is_done=False,
             tags=frozenset(), priority=None, topic_key=key, project="p")
    hours, _, _ = join_time([t], topic_secs)
    assert hours[key] == topic_secs[key]
