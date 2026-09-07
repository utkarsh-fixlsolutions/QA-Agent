"""Coalesces bursts of filesystem events into a single analysis pass.

Sits between the monitor and the analysis bridge. Owns event timing, collection,
coalescing and path deduplication - and nothing else. It knows nothing about
ruff, adapters, findings, reporting or rendering: it forwards a batch to an
injected callback and has no idea what that callback does with it.

Why a burst needs collapsing at all: a single save on Windows emits several
events. Measured gaps within a burst were 0.1-0.3 ms, so the default window
below has a wide margin over real editor behaviour.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .fsmonitor import merge_events, report_to_stderr, target_path

# Trailing-edge window. Chosen from measurement, not folklore: observed
# intra-burst gaps were 0.1-0.3 ms and a deliberately chunked write was 50 ms,
# so 300 ms coalesces real bursts with room to spare while staying below the
# threshold where feedback stops feeling immediate. Adjustable per instance.
DEBOUNCE_SECONDS = 0.3

# How long stop() waits for an analysis already in progress. Long enough for any
# realistic ruff run, short enough that Ctrl+C stays responsive if one hangs.
SHUTDOWN_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class EventBatch:
    """One quiet period's worth of change.

    events: the latest event per affected path, deletions included, so callers
            can report what happened.
    paths:  the unique paths those events left to be examined, deletions
            excluded. Never loses a distinct file.
    """

    events: tuple
    paths: tuple


class Debouncer:
    """Collects events, and forwards one deduplicated batch once things go quiet."""

    def __init__(self, on_flush, interval=DEBOUNCE_SECONDS, on_error=report_to_stderr):
        self._on_flush = on_flush
        self._on_error = on_error
        self.interval = interval
        # Keyed by the event's own path, so repeated events for one file collapse
        # to their most recent state: modified-then-deleted ends as deleted.
        self._pending = {}
        self._timer = None
        self._lock = threading.Lock()
        # Held for the duration of a flush, so a burst arriving mid-analysis
        # queues behind it instead of running alongside it.
        self._flush_lock = threading.Lock()
        self._stopped = False

    def submit(self, event):
        """Record an event and restart the quiet period."""
        with self._lock:
            if self._stopped:
                return
            previous = self._pending.get(event.path)
            # Coalescing rule lives with FileEvent, so it stays one definition.
            self._pending[event.path] = (
                event if previous is None else merge_events(previous, event)
            )
            self._restart_timer()

    def stop(self, timeout=SHUTDOWN_WAIT_SECONDS):
        """Stop accepting work and wait for any analysis already in progress.

        Returns True if nothing was left running. Waiting matters because the
        session announces that nothing is still running once this returns - a
        claim that was previously false while a flush was mid-analysis.

        The wait is bounded: a pathological analysis must not make Ctrl+C hang
        forever. If the bound is reached the caller is told, rather than the
        overrun passing silently.
        """
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

        # Acquiring this both waits for a flush already running and blocks one
        # that has snapshotted its batch but not yet started - that flush will
        # see _stopped once it gets the lock, and return without running.
        if not self._flush_lock.acquire(timeout=timeout):
            return False
        self._flush_lock.release()
        return True

    # Callers hold self._lock.
    def _restart_timer(self):
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.interval, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self):
        # Taken before the state lock so that stop() can use it as the single
        # point where "is a flush running, and may another start?" is decided.
        with self._flush_lock:
            with self._lock:
                # Re-checked here, not only when the timer was set: stop() may
                # have run while this thread was waiting for the flush lock.
                if self._stopped or not self._pending:
                    return
                events = tuple(self._pending[key] for key in sorted(self._pending))
                self._pending = {}
                self._timer = None

            # Deduplicated by construction: one event per path, deletions dropped
            # from the analysis set but kept in the batch so they stay visible.
            paths = tuple(
                path for path in (target_path(event) for event in events) if path is not None
            )

            # The state lock is released above, so events arriving during a long
            # analysis are collected into the next batch instead of blocking the
            # observer thread that delivers them.
            try:
                self._on_flush(EventBatch(events=events, paths=paths))
            except Exception as error:  # noqa: BLE001 - a bad batch must not end the session
                # This runs on a timer thread. An escaping exception would be
                # reported by threading's excepthook and lost among the reports,
                # so it is surfaced deliberately instead.
                self._on_error(error)
