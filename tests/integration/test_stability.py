"""Long-running stability, including the four defects found in Part 6.

Each of those was reproduced before being fixed; the reproductions are kept here
so a regression would be caught rather than rediscovered months later.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.debouncer import Debouncer  # noqa: E402
from qa_agent.fsmonitor import MODIFIED, FileEvent, FileSystemMonitor  # noqa: E402
from qa_agent.watch import WatchSession  # noqa: E402


def test_bad_event_does_not_deafen_watcher(suite):
    """Part 6 defect 1: an exception used to kill watchdog's dispatch thread."""
    with TempProject() as root:
        seen, errors = [], []

        def callback(event):
            seen.append(event)
            if len(seen) == 1:
                raise RuntimeError("simulated failure in the event callback")

        monitor = FileSystemMonitor(root, {".py"}, callback, on_error=errors.append)
        monitor.start()
        try:
            time.sleep(0.8)
            (root / "first.py").write_text("a = 1\n", encoding="utf-8")
            time.sleep(1.2)
            (root / "second.py").write_text("b = 2\n", encoding="utf-8")
            time.sleep(1.2)
            suite.check("observer survives a raising callback", monitor.is_alive())
            suite.check("events still delivered after the failure", len(seen) > 1,
                        "  [{} delivered]".format(len(seen) - 1))
            suite.check("the failure was reported, not swallowed", bool(errors))
        finally:
            monitor.stop()


def test_stop_waits_for_running_analysis(suite):
    """Part 6 defect 2: stop() used to return while an analysis was still running."""
    order = []

    def slow(batch):
        order.append("flush-start")
        time.sleep(1.2)
        order.append("flush-end")

    d = Debouncer(slow, interval=0.1)
    d.submit(FileEvent(kind=MODIFIED, path=Path("x.py")))
    time.sleep(0.5)
    d.stop()
    order.append("stop-returned")
    suite.check("stop() waits for the in-flight analysis",
                order == ["flush-start", "flush-end", "stop-returned"], "  {}".format(order))


def test_components_stop_source_first(suite):
    """Part 6 defect 3: the consumer used to be stopped before the event source."""
    order = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def start(self):
            order.append("start:" + self.name)

        def stop(self):
            order.append("stop:" + self.name)

    with TempProject() as root:
        session = WatchSession(root, ["ruff"], components=[Fake("monitor"), Fake("debouncer")])
        threading.Timer(0.6, session.stop).start()
        session.run()
    stops = [o for o in order if o.startswith("stop")]
    suite.check("the event source is stopped first", stops == ["stop:monitor", "stop:debouncer"],
                "  {}".format(stops))


def test_failing_stop_does_not_orphan(suite):
    """Part 6 defect 4: one failing stop() used to leave later components running."""
    order = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def start(self):
            pass

        def stop(self):
            order.append(self.name)

    class Boom(Fake):
        def stop(self):
            order.append(self.name)
            raise RuntimeError("stop failed")

    with TempProject() as root:
        session = WatchSession(root, ["ruff"], components=[Boom("first"), Fake("second")])
        threading.Timer(0.6, session.stop).start()
        code = session.run()
    suite.check("a failing stop() does not orphan later components", order == ["first", "second"],
                "  {}".format(order))
    suite.check("the failure is reported through the exit code", code == 2)


def test_dead_component_detected(suite):
    class Dying:
        def __init__(self):
            self._alive = True
            threading.Timer(0.8, self._die).start()

        def _die(self):
            self._alive = False

        def start(self):
            pass

        def is_alive(self):
            return self._alive

        def stop(self):
            pass

    with TempProject() as root:
        code = WatchSession(root, ["ruff"], components=[Dying()]).run()
    suite.check("a component that dies exits 2 instead of watching nothing", code == 2)


def test_monitor_lifecycle(suite):
    with TempProject() as root:
        monitor = FileSystemMonitor(root, {".py"}, lambda e: None)
        suite.check("not alive before start", not monitor.is_alive())
        monitor.start()
        suite.check("alive once started", monitor.is_alive())
        try:
            monitor.start()
            suite.check("double start() is refused", False)
        except RuntimeError:
            suite.check("double start() is refused", True)
        monitor.stop()
        suite.check("not alive after stop", not monitor.is_alive())


def test_no_leaks_across_restarts(suite):
    with TempProject() as root:
        before = threading.active_count()
        for _ in range(8):
            monitor = FileSystemMonitor(root, {".py"}, lambda e: None)
            debouncer = Debouncer(lambda b: None, interval=0.05)
            monitor.start()
            (root / "x.py").write_text("v = 1\n", encoding="utf-8")
            time.sleep(0.15)
            monitor.stop()
            debouncer.stop()
        time.sleep(0.5)
        after = threading.active_count()
        suite.check("8 start/stop cycles leak no threads", after <= before,
                    "  [{} -> {}]".format(before, after))


def test_failing_batch_keeps_debouncer_alive(suite):
    reported = []

    def boom(batch):
        raise ValueError("bad batch")

    d = Debouncer(boom, interval=0.1, on_error=reported.append)
    d.submit(FileEvent(kind=MODIFIED, path=Path("a.py")))
    time.sleep(0.5)
    suite.check("a failing batch is reported", bool(reported))
    ok = []
    d2 = Debouncer(ok.append, interval=0.1)
    d2.submit(FileEvent(kind=MODIFIED, path=Path("b.py")))
    time.sleep(0.5)
    suite.check("the debouncer keeps working afterwards", bool(ok))
    d.stop()
    d2.stop()


if __name__ == "__main__":
    suite = Suite("Long-running stability")
    sys.exit(suite.run([
        test_bad_event_does_not_deafen_watcher,
        test_stop_waits_for_running_analysis,
        test_components_stop_source_first,
        test_failing_stop_does_not_orphan,
        test_dead_component_detected,
        test_monitor_lifecycle,
        test_no_leaks_across_restarts,
        test_failing_batch_keeps_debouncer_alive,
    ]))
