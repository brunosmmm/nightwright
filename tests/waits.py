"""SPEC-0269: load-tolerant waits for tests. Poll with a generous bound (it costs nothing on
the green path) instead of a fixed sleep before a positive assertion. `WT_TEST_WAIT_S`
overrides the default bound."""
import os
import time

WAIT_S = float(os.environ.get("WT_TEST_WAIT_S", "60"))


def until(cond, timeout=None, interval=0.05):
    """Poll `cond` until it is true (return True) or the bound passes (return False)."""
    end = time.monotonic() + (WAIT_S if timeout is None else timeout)
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(interval)
    return bool(cond())


def join(thread, timeout=None):
    """Join `thread`; raise AssertionError if it is still running after the bound."""
    bound = WAIT_S if timeout is None else timeout
    thread.join(bound)
    if thread.is_alive():
        raise AssertionError(f"thread still running after {bound:g}s")


async def pilot_until(pilot, cond, timeout=None, step=0.05):
    """`until` for Textual tests: poll with `await pilot.pause(step)`."""
    end = time.monotonic() + (WAIT_S if timeout is None else timeout)
    while time.monotonic() < end:
        if cond():
            return True
        await pilot.pause(step)
    return bool(cond())
