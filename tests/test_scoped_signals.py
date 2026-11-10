"""SPEC-0528: a CLI command's signal handlers end with the command. Run in-process (a test's
CliRunner in an xdist worker's main thread) a command must not leave its SIGTERM/SIGINT handler
on the caller's process."""
import signal
import threading

import pytest

from nightwright.agentic import cli_gate as CG
from nightwright.agentic.scoped_signals import scoped
import waits

pytestmark = pytest.mark.fast


def _hup_before():
    return signal.getsignal(signal.SIGHUP)


def test_scoped_installs_then_restores_even_on_error():
    before = _hup_before()
    seen = []
    with pytest.raises(RuntimeError):
        with scoped({signal.SIGHUP: lambda *a: seen.append(a[0])}) as done:
            assert done == {signal.SIGHUP}
            signal.raise_signal(signal.SIGHUP)
            raise RuntimeError("boom")
    assert seen == [signal.SIGHUP]
    assert signal.getsignal(signal.SIGHUP) is before


def test_scoped_off_the_main_thread_leaves_handlers_alone():
    before = _hup_before()
    out = {}

    def body():
        with scoped({signal.SIGHUP: signal.SIG_IGN}) as done:
            out["done"] = done
            out["during"] = signal.getsignal(signal.SIGHUP)
    t = threading.Thread(target=body, daemon=True)
    t.start()
    waits.join(t)
    assert out == {"done": set(), "during": before}
    assert signal.getsignal(signal.SIGHUP) is before


def test_scoped_restores_a_non_python_handler_as_default(monkeypatch):
    calls = []
    real = signal.signal
    before = _hup_before()

    def fake_signal(sig, h):
        calls.append((sig, h))
        return None if len(calls) == 1 else real(sig, h)  # None: set outside Python
    monkeypatch.setattr(signal, "signal", fake_signal)
    try:
        with scoped({signal.SIGHUP: signal.SIG_IGN}):
            pass
        assert calls[-1] == (signal.SIGHUP, signal.SIG_DFL)
    finally:
        real(signal.SIGHUP, before)


def test_gate_stop_on_signals_sets_stop_and_restores():
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    with CG._stop_on_signals() as stop:
        assert not stop.is_set()
        signal.raise_signal(signal.SIGTERM)          # `nw gate cancel`: the job stops cleanly
        assert stop.is_set()
    assert {s: signal.getsignal(s) for s in before} == before
