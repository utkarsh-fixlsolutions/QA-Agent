"""Filesystem storms: git checkout, archive extraction, bulk copies.

Measures rather than asserts vibes. Scaled to finish in about a minute; the
figures quoted in docs/11 came from larger runs of the same shapes.
"""

from __future__ import annotations

import gc
import statistics
import sys
import threading
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.debouncer import Debouncer  # noqa: E402
from qa_agent.fsmonitor import MODIFIED, FileEvent, FileSystemMonitor  # noqa: E402
from qa_agent.runner import run  # noqa: E402

STORM_FILES = 800


def test_submit_throughput(suite):
    """submit() must stay comfortably ahead of watchdog's delivery rate."""
    d = Debouncer(lambda b: None, interval=0.3)
    latencies = []
    peak = 0
    for i in range(5000):
        event = FileEvent(kind=MODIFIED, path=Path("f{}.py".format(i % 200)))
        start = time.perf_counter()
        d.submit(event)
        latencies.append((time.perf_counter() - start) * 1e6)
        peak = max(peak, threading.active_count())
    d.stop()
    latencies.sort()
    median = statistics.median(latencies)
    rate = 1e6 / median
    suite.check("submit() sustains well above watchdog's ~2,500 events/sec",
                rate > 4000, "  [{:,.0f}/sec, median {:.0f}us]".format(rate, median))
    suite.check("cancelled timers do not pile up", peak < 25, "  [peak {} threads]".format(peak))


def test_storm_loses_nothing(suite):
    """A bulk write must reach analysis with no file dropped."""
    with TempProject() as root:
        batches = []
        debouncer = Debouncer(batches.append, interval=0.3)
        monitor = FileSystemMonitor(root, {".py"}, debouncer.submit)
        monitor.start()
        try:
            time.sleep(1.0)
            start = time.perf_counter()
            for i in range(STORM_FILES):
                (root / "mod{:04d}.py".format(i)).write_text("v = {}\n".format(i),
                                                             encoding="utf-8")
            write_time = time.perf_counter() - start

            deadline = time.perf_counter() + 30
            while time.perf_counter() < deadline:
                time.sleep(0.3)
                if batches and sum(len(b.paths) for b in batches) >= STORM_FILES:
                    break
        finally:
            monitor.stop()
            debouncer.stop()

        analysed = set()
        for batch in batches:
            analysed.update(p.name for p in batch.paths)
        on_disk = {p.name for p in root.glob("*.py")}

        suite.check("every file written reached a batch", analysed >= on_disk,
                    "  [{}/{} files, {} batch(es)]".format(len(analysed), len(on_disk),
                                                           len(batches)))
        suite.check("the storm coalesced into few batches", len(batches) <= 5,
                    "  [{} batches for {} files in {:.1f}s]".format(
                        len(batches), STORM_FILES, write_time))


def test_analysis_latency(suite):
    """Latency should stay usable from one file up to a whole checkout."""
    with TempProject() as root:
        files = []
        for i in range(600):
            path = root / "m{:03d}.py".format(i)
            path.write_text("value = {}\n".format(i), encoding="utf-8")
            files.append(path)

        timings = {}
        for n in (1, 50, 600):
            start = time.perf_counter()
            run(files[:n])
            timings[n] = time.perf_counter() - start

        # Threshold raised from 1.0s (Phase C Part 7): a single .py file now
        # pays three sequential cold tool starts, not two - ruff (near-
        # instant), pyright (~0.8-1s), and mypy (~1s), since mypy also
        # claims .py. Measured steady-state ~1.2-1.3s, occasionally ~2.0s;
        # 3.0s keeps real, generous headroom rather than just-barely-passing.
        suite.check("a single-file analysis is fast", timings[1] < 3.0,
                    "  [{:.0f} ms]".format(timings[1] * 1000))
        suite.check("a checkout-sized batch stays reasonable", timings[600] < 10.0,
                    "  [{:.2f}s for 600 files]".format(timings[600]))


def test_memory_is_flat(suite):
    """Thousands of events must not accumulate anything."""
    d = Debouncer(lambda b: None, interval=0.02)
    gc.collect()
    tracemalloc.start()
    base = tracemalloc.take_snapshot()
    objects_before = len(gc.get_objects())

    for cycle in range(100):
        for i in range(10):
            d.submit(FileEvent(kind=MODIFIED, path=Path("f{}.py".format(cycle % 20))))
        time.sleep(0.03)

    time.sleep(0.5)
    d.stop()
    gc.collect()
    growth = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(base, "filename"))
    tracemalloc.stop()
    objects_after = len(gc.get_objects())

    suite.check("memory does not grow across 1,000 events", growth < 200 * 1024,
                "  [{:+.1f} KB]".format(growth / 1024))
    suite.check("object count does not grow", objects_after - objects_before < 500,
                "  [{:+d} objects]".format(objects_after - objects_before))
    suite.check("pending map drains to empty", len(d._pending) == 0)


def test_known_limitation_sustained_activity(suite):
    """Documents, rather than asserts away, the starvation limitation.

    Trailing-edge debouncing means continuous writes keep deferring the window.
    Nothing is lost, but no feedback appears until activity pauses. Recorded here
    so the behaviour is visible in test output instead of being a surprise.
    """
    flushes = []
    d = Debouncer(flushes.append, interval=0.3)
    stop = time.perf_counter() + 3.0
    while time.perf_counter() < stop:
        d.submit(FileEvent(kind=MODIFIED, path=Path("busy.py")))
        time.sleep(0.05)
    during = len(flushes)
    time.sleep(1.0)
    after = len(flushes)
    d.stop()
    suite.check("KNOWN LIMITATION: no analysis during sustained activity", during == 0,
                "  [{} during 3s of continuous writes]".format(during))
    suite.check("...but the work is analysed once activity pauses", after > during)


if __name__ == "__main__":
    suite = Suite("Filesystem storms and load")
    sys.exit(suite.run([
        test_submit_throughput,
        test_storm_loses_nothing,
        test_analysis_latency,
        test_memory_is_flat,
        test_known_limitation_sustained_activity,
    ]))
