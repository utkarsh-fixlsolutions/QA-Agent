"""Live reporting for watch mode: how a continuous stream of batches reads.

Presentation only. This module decides how a batch is announced and hands the
findings themselves to report.py, which remains the single implementation of
findings formatting. It runs nothing and analyzes nothing.

The output is deliberately spare. Someone reading this after hours of watching
wants to know which batch, when, what changed, and what is wrong - so anything
the batch header already states is not repeated by the report beneath it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .fsmonitor import DELETED, MODIFIED, RENAMED
from .report import render_findings, render_tool_error, render_unexpected_error

SEPARATOR = "-" * 50

# A single git checkout can change hundreds of files at once. Listing them all
# would bury the findings, so the list is capped and the remainder counted.
# Presentation only: every changed file is still analyzed.
MAX_LISTED_FILES = 10


def display_path(path, root=None):
    """Path as the reader knows it: relative to the watched root where possible."""
    if root is None:
        return path
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _describe(event, root):
    """One line for one changed file, annotated only when that adds something."""
    shown = display_path(event.path, root)
    if event.kind == DELETED:
        return "  - {} (deleted)".format(shown)
    if event.kind == RENAMED and event.old_path is not None:
        return "  - {} (renamed from {})".format(shown, display_path(event.old_path, root))
    if event.kind == MODIFIED:
        return "  - {}".format(shown)
    return "  - {} ({})".format(shown, event.kind)


def _flushing_print(text):
    # Watch output is often redirected to a log, where stdout is block buffered.
    # Batches must appear as they happen, not when the process finally exits.
    print(text, flush=True)


class LiveReporter:
    """Announces each analysed batch as one clearly separated entry."""

    def __init__(self, root=None, out=_flushing_print, max_listed=MAX_LISTED_FILES):
        self.root = Path(root) if root is not None else None
        self.max_listed = max_listed
        self._out = out
        self._batch_number = 0

    def report(self, batch, outcome=None, explanations=None, suggested_fixes=None):
        """Print one batch: its identity, what changed, and what was found.

        outcome is None when a batch contained nothing to analyze - a batch of
        deletions still deserves to be seen. `explanations`/`suggested_fixes`
        (Phase D Part 6) are the same optional {Finding: ...} mappings
        report.py's own render_findings() accepts - purely additive, and
        omitted or empty by every existing caller, so output is unchanged
        unless a caller opts in.
        """
        self._batch_number += 1
        lines = [
            "",
            SEPARATOR,
            "Batch #{}   {}".format(self._batch_number, datetime.now().strftime("%H:%M:%S")),
            "",
            "Files changed:",
        ]
        lines.extend(self._file_lines(batch.events))
        lines.append("")
        lines.append(self._body(outcome, explanations, suggested_fixes))
        self._out("\n".join(lines))

    def _file_lines(self, events):
        listed = [_describe(event, self.root) for event in events[: self.max_listed]]
        remaining = len(events) - len(listed)
        if remaining > 0:
            listed.append("  ...and {} more".format(remaining))
        return listed

    def _body(self, outcome, explanations=None, suggested_fixes=None):
        if outcome is None:
            return "Nothing to analyze."
        if outcome.ok:
            return render_findings(outcome.result, explanations, suggested_fixes)
        if outcome.expected:
            return render_tool_error(outcome.error)
        return render_unexpected_error("this batch", outcome.error)
