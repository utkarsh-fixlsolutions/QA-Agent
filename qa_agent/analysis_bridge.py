"""The bridge from filesystem events to the existing analysis pipeline.

Pure orchestration: given paths, it runs the pipeline Phase A already built and
returns what happened. It performs no analysis of its own, opens no second ruff
path, and prints nothing - presentation belongs to the reporting layer, which is
why this module imports neither report nor any renderer.

Analysis is incremental because run() is given exactly the changed files, never
the project. That contract has existed since Step 3 and needed no change here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .adapters import ToolError
from .runner import run


@dataclass(frozen=True)
class AnalysisOutcome:
    """What one analysis produced: a result, or the failure that prevented it.

    `expected` distinguishes a tool failure we anticipate (ruff missing, a file
    that vanished mid-run) from something unforeseen. Classifying the failure is
    orchestration's job because it caught it; deciding how to show it is not.
    """

    result: object = None
    error: object = None
    expected: bool = True

    @property
    def ok(self):
        return self.error is None


class AnalysisBridge:
    """Runs the existing pipeline over a set of changed files."""

    def analyze_paths(self, paths):
        """Analyze exactly these paths and report what happened.

        Failures are returned rather than raised: watch mode runs continuously,
        and one bad analysis must never end the session.
        """
        try:
            return AnalysisOutcome(result=run(list(paths)))
        except ToolError as exc:
            return AnalysisOutcome(error=exc, expected=True)
        except Exception as exc:  # noqa: BLE001 - deliberate last line of defence
            # Anything unforeseen is handed back with its traceback intact so the
            # reporting layer can show it in full. A silently dead watcher is the
            # worse bug.
            return AnalysisOutcome(error=exc, expected=False)
