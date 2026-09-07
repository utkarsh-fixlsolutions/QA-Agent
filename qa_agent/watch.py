"""Watch mode lifecycle: startup, idle waiting, graceful shutdown.

WatchSession owns the process lifecycle only - startup validation, the idle
wait, and graceful shutdown. It knows nothing about analyzers, the filesystem,
batching or reporting: the analyzer names it announces are handed to it, and the
components it runs are opaque objects with start() and stop().

Everything the session displays about the work itself lives in live_report.py.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

# How long the idle loop blocks per iteration. Short enough that Ctrl+C is
# delivered promptly, long enough that idling costs nothing measurable.
POLL_INTERVAL_SECONDS = 0.5


class WatchSession:
    """Keeps the process alive until stopped, and reports both events clearly."""

    def __init__(
        self,
        project_path,
        analyzers,
        poll_interval=POLL_INTERVAL_SECONDS,
        components=(),
    ):
        self.project_path = Path(project_path)
        self.analyzers = list(analyzers)
        self.poll_interval = poll_interval
        # Anything with start()/stop() to run for the session's lifetime, started
        # in order and stopped in reverse. The session never asks what they do,
        # which keeps this class lifecycle-only.
        self.components = list(components)
        self._stop = threading.Event()

    def stop(self):
        """Request shutdown. Safe to call from any thread."""
        self._stop.set()

    def run(self):
        """Validate, announce, idle until stopped. Returns a process exit code."""
        try:
            root = self.project_path.resolve(strict=True)
        except OSError:
            return self._fail("path does not exist")
        if not root.is_dir():
            return self._fail("not a directory")

        started = []
        try:
            for component in self.components:
                if hasattr(component, "start"):
                    component.start()
                started.append(component)
        except Exception as exc:  # noqa: BLE001 - a failed start must still tidy up
            self._stop_all(started)
            return self._fail(exc)

        self._print_banner(root)
        died = None
        try:
            died = self._idle()
        except KeyboardInterrupt:
            # Ctrl+C is the expected way to end watch mode, not an error.
            pass
        finally:
            # In a finally, so the banner's promise holds even if the idle loop
            # raises. Components are listed source-first and stopped in that same
            # order: silencing the source before its consumers means the later
            # stops race nothing.
            left_running = self._stop_all(started)

        if died is not None:
            print(
                "\nQA Agent: {} stopped unexpectedly - watch mode cannot continue.".format(died),
                file=sys.stderr,
            )
            return 2
        if left_running:
            print(
                "\nQA Agent: watch mode stopped, but {} did not finish in time.".format(
                    ", ".join(left_running)
                ),
                file=sys.stderr,
            )
            return 2
        print("\nQA Agent: watch mode stopped. Nothing was left running.", flush=True)
        return 0

    def _stop_all(self, components):
        """Stop every component, reporting any that failed or overran.

        Each stop is isolated: one component failing must never leave the ones
        after it running, which is exactly how an observer thread gets orphaned.
        """
        unfinished = []
        for component in components:
            name = type(component).__name__
            try:
                if component.stop() is False:
                    unfinished.append(name)
            except Exception as error:  # noqa: BLE001
                print(
                    "QA Agent: error while stopping {}: {}".format(name, error),
                    file=sys.stderr,
                )
                unfinished.append(name)
        return unfinished

    def _fail(self, reason):
        print(
            "QA Agent: cannot watch '{}': {}".format(self.project_path, reason),
            file=sys.stderr,
        )
        return 2

    def _print_banner(self, root):
        print("QA Agent - watch mode")
        print("  Watching:  {}".format(root))
        print("  Analyzers: {}".format(", ".join(self.analyzers) or "none"))
        print("  Status:    watch mode started successfully")
        print("")
        print("  Changed files are analyzed as you save them.")
        # flush: stdout is block-buffered when redirected to a file or pipe, and
        # a long-running process must not withhold its banner until it exits.
        print("  Press Ctrl+C to stop.", flush=True)

    def _idle(self):
        """Wait until stopped. Returns the name of a component that died, if any.

        The liveness check exists because a component can die while the process
        stays alive - watchdog's dispatch thread is the real example. Without it
        the session would sit here for days looking healthy while watching
        nothing at all. Better to say so and exit than to pretend.
        """
        # wait() returns True as soon as stop() is set; the timeout keeps the
        # loop interruptible so KeyboardInterrupt is delivered promptly.
        while not self._stop.wait(self.poll_interval):
            for component in self.components:
                is_alive = getattr(component, "is_alive", None)
                if is_alive is not None and not is_alive():
                    return type(component).__name__
        return None
