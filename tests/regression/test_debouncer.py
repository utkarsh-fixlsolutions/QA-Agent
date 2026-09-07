"""Debounce semantics: coalescing, no lost files, deletions, renames, ordering."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite  # noqa: E402

from qa_agent.debouncer import Debouncer  # noqa: E402
from qa_agent.fsmonitor import DELETED, MODIFIED, RENAMED, FileEvent  # noqa: E402

INTERVAL = 0.15
A, B, C = Path("a.py"), Path("b.py"), Path("c.py")


def collector():
    batches = []
    return batches, batches.append


def test_duplicates_collapse(suite):
    batches, sink = collector()
    d = Debouncer(sink, interval=INTERVAL)
    for _ in range(3):
        d.submit(FileEvent(kind=MODIFIED, path=A))
    time.sleep(INTERVAL * 4)
    suite.check("3 events for one file -> 1 analysis", len(batches) == 1)
    suite.check("...of exactly 1 path", batches and batches[0].paths == (A,))
    d.stop()


def test_distinct_files_survive(suite):
    batches, sink = collector()
    d = Debouncer(sink, interval=INTERVAL)
    for path in (A, B, C):
        d.submit(FileEvent(kind=MODIFIED, path=path))
        d.submit(FileEvent(kind=MODIFIED, path=path))
    time.sleep(INTERVAL * 4)
    suite.check("3 duplicated files -> 1 analysis", len(batches) == 1)
    suite.check("no distinct file lost", batches and set(batches[0].paths) == {A, B, C})
    suite.check("no duplicate leaked into the batch", batches and len(batches[0].paths) == 3)
    d.stop()


def test_deletions(suite):
    batches, sink = collector()
    d = Debouncer(sink, interval=INTERVAL)
    d.submit(FileEvent(kind=MODIFIED, path=A))
    d.submit(FileEvent(kind=DELETED, path=B))
    time.sleep(INTERVAL * 4)
    suite.check("deleted file is not analysed", batches and batches[0].paths == (A,))
    suite.check("deleted file still visible in the batch",
                batches and {e.path for e in batches[0].events} == {A, B})
    d.stop()


def test_modified_then_deleted(suite):
    batches, sink = collector()
    d = Debouncer(sink, interval=INTERVAL)
    d.submit(FileEvent(kind=MODIFIED, path=A))
    d.submit(FileEvent(kind=DELETED, path=A))
    time.sleep(INTERVAL * 4)
    suite.check("modified-then-deleted is not analysed", batches and batches[0].paths == ())
    d.stop()


def test_rename_destination_only(suite):
    batches, sink = collector()
    d = Debouncer(sink, interval=INTERVAL)
    d.submit(FileEvent(kind=RENAMED, path=B, old_path=A))
    d.submit(FileEvent(kind=MODIFIED, path=B))
    time.sleep(INTERVAL * 4)
    suite.check("rename analyses the destination only", batches and batches[0].paths == (B,))
    suite.check("rename keeps its origin for reporting",
                batches and batches[0].events[0].old_path == A)
    d.stop()


def test_trailing_edge(suite):
    batches, sink = collector()
    d = Debouncer(sink, interval=INTERVAL)
    for _ in range(4):
        d.submit(FileEvent(kind=MODIFIED, path=A))
        time.sleep(INTERVAL * 0.5)
    suite.check("does not flush while events keep arriving", batches == [])
    time.sleep(INTERVAL * 3)
    suite.check("flushes once things go quiet", len(batches) == 1)
    d.stop()


def test_events_during_analysis(suite):
    batches = []
    started = threading.Event()

    def slow(batch):
        batches.append(batch)
        started.set()
        time.sleep(INTERVAL * 3)

    d = Debouncer(slow, interval=INTERVAL)
    d.submit(FileEvent(kind=MODIFIED, path=A))
    started.wait(2)
    d.submit(FileEvent(kind=MODIFIED, path=B))
    time.sleep(INTERVAL * 6)
    suite.check("events during an analysis are not lost", len(batches) == 2)
    suite.check("...they arrive in the next batch", len(batches) == 2 and batches[1].paths == (B,))
    d.stop()


def test_no_overlapping_flushes(suite):
    active, overlapped = [], []

    def slow(batch):
        active.append(1)
        if len(active) > 1:
            overlapped.append(True)
        time.sleep(INTERVAL * 2)
        active.pop()

    d = Debouncer(slow, interval=INTERVAL)
    for path in (A, B, C):
        d.submit(FileEvent(kind=MODIFIED, path=path))
        time.sleep(INTERVAL * 1.6)
    time.sleep(INTERVAL * 8)
    suite.check("analyses never run concurrently", not overlapped)
    d.stop()


def test_stop_semantics(suite):
    batches, sink = collector()
    d = Debouncer(sink, interval=INTERVAL)
    d.submit(FileEvent(kind=MODIFIED, path=A))
    d.stop()
    time.sleep(INTERVAL * 4)
    suite.check("stop() cancels the pending window", batches == [])
    d.submit(FileEvent(kind=MODIFIED, path=B))
    time.sleep(INTERVAL * 3)
    suite.check("events after stop() are ignored", batches == [])


if __name__ == "__main__":
    suite = Suite("Debouncer semantics")
    sys.exit(suite.run([
        test_duplicates_collapse,
        test_distinct_files_survive,
        test_deletions,
        test_modified_then_deleted,
        test_rename_destination_only,
        test_trailing_edge,
        test_events_during_analysis,
        test_no_overlapping_flushes,
        test_stop_semantics,
    ]))
