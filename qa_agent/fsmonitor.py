"""Filesystem event detection for watch mode.

This is the integration layer for watchdog, and the only module that imports it.
Everything downstream consumes our own FileEvent, so replacing the backend later
means rewriting this file and nothing else - the same boundary Finding draws
around ruff's JSON.

Responsibilities stop at observing: this module knows nothing about analyzers,
findings, reports, or ruff. Supported extensions and the callback are injected
by the caller, so the adapter registry stays the single source of truth without
being imported here.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Directories whose contents are never interesting. Injectable per monitor, so
# extending this later does not mean editing this module.
DEFAULT_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
    }
)

CREATED = "created"
MODIFIED = "modified"
DELETED = "deleted"
RENAMED = "renamed"


@dataclass(frozen=True)
class FileEvent:
    """One filesystem change worth telling someone about.

    Deliberately independent of watchdog's event classes: this is the contract
    later parts consume, so the backend can change without touching them.
    """

    kind: str
    path: Path
    old_path: Path | None = None  # renames only; where the file came from


def target_path(event):
    """The file an event leaves behind to be examined, or None if it leaves none.

    Event semantics, defined once and shared: a deletion leaves nothing, and a
    rename leaves its destination - never its source. Both the analysis bridge
    and the debouncer read this rule from here rather than each keeping a copy.
    """
    if event.kind == DELETED:
        return None
    return event.path


def merge_events(existing, incoming):
    """Pick the event that best describes what happened to one path in a burst.

    A rename or creation is usually followed by a Modified event for the same
    file, from the same user action. Keeping the later one would report a rename
    as a plain modification and lose where the file came from, so the more
    specific event wins - except a deletion, which is the file's final state and
    always wins.
    """
    if incoming.kind == DELETED:
        return incoming
    if existing.kind in (CREATED, RENAMED) and incoming.kind == MODIFIED:
        return existing
    return incoming


def report_to_stderr(error):
    """Last-resort error sink, used when the caller injects none."""
    traceback.print_exception(type(error), error, error.__traceback__)


class _Handler(FileSystemEventHandler):
    """Translates watchdog events into FileEvents, dropping everything else."""

    def __init__(self, root, extensions, ignored_dirs, on_event, on_error):
        self._root = root
        self._extensions = extensions
        self._ignored_dirs = ignored_dirs
        self._on_event = on_event
        self._on_error = on_error

    def _wanted(self, raw_path):
        """A real, non-ignored file whose extension some adapter handles."""
        path = Path(raw_path)
        if path.suffix.lower() not in self._extensions:
            return None
        try:
            relative_parts = path.relative_to(self._root).parts
        except ValueError:
            # Outside the watched tree; nothing sensible to report.
            return None
        if any(part in self._ignored_dirs for part in relative_parts):
            return None
        return path

    def _emit(self, kind, path, old_path=None):
        try:
            self._on_event(FileEvent(kind=kind, path=path, old_path=old_path))
        except Exception as error:  # noqa: BLE001 - see below; this must catch everything
            # watchdog dispatches events on its own thread, and an exception that
            # escapes here kills that thread: the process stays alive and the
            # banner still claims to be watching, but no further event is ever
            # delivered. A silently deaf watcher is the worst failure mode for a
            # process meant to run for weeks, so one bad event is reported and
            # the next one is still handled.
            self._on_error(error)

    # watchdog calls the hooks below from its own thread.

    def on_created(self, event):
        if event.is_directory:
            return
        path = self._wanted(event.src_path)
        if path is not None:
            self._emit(CREATED, path)

    def on_modified(self, event):
        if event.is_directory:
            return
        path = self._wanted(event.src_path)
        if path is not None:
            self._emit(MODIFIED, path)

    def on_deleted(self, event):
        if event.is_directory:
            return
        path = self._wanted(event.src_path)
        if path is not None:
            self._emit(DELETED, path)

    def on_moved(self, event):
        if event.is_directory:
            return
        source = self._wanted(event.src_path)
        destination = self._wanted(event.dest_path)
        if destination is not None:
            # A rename from an unsupported name (notes.txt -> notes.py) has no
            # meaningful "from", so it is reported as a plain creation.
            if source is None:
                self._emit(CREATED, destination)
            else:
                self._emit(RENAMED, destination, old_path=source)
        elif source is not None:
            # Moved out of view, or renamed to a type nothing handles.
            self._emit(DELETED, source)


class FileSystemMonitor:
    """Watches a directory tree recursively and reports interesting changes.

    Lifecycle matches WatchSession's expectations: start() then stop().
    """

    def __init__(
        self,
        root,
        extensions,
        on_event,
        ignored_dirs=DEFAULT_IGNORED_DIRS,
        on_error=report_to_stderr,
    ):
        self.root = Path(root)
        self.extensions = {ext.lower() for ext in extensions}
        self.ignored_dirs = frozenset(ignored_dirs)
        self._on_event = on_event
        self._on_error = on_error
        self._observer = None

    def start(self):
        if self._observer is not None:
            # Starting twice would strand the first observer: still running, no
            # longer referenced, impossible to stop.
            raise RuntimeError("monitor is already running")
        handler = _Handler(
            self.root, self.extensions, self.ignored_dirs, self._on_event, self._on_error
        )
        self._observer = Observer()
        self._observer.schedule(handler, str(self.root), recursive=True)
        self._observer.start()

    def is_alive(self):
        """False once watchdog's dispatch thread has died for any reason."""
        return self._observer is not None and self._observer.is_alive()

    def stop(self):
        """Stop observing and wait for the thread, so nothing is left running."""
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join()
        self._observer = None
