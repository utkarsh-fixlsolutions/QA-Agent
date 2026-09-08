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

from .runner import run


@dataclass(frozen=True)
class AnalysisOutcome:
    """What one analysis produced: a result, or the failure that prevented it.

    A failing analyzer is no longer a failure at this level (docs/15-unified-
    reporting.md): run() records it in the result's own `tool_errors` instead
    of raising, so `result` is populated and `ok` is true even when one of
    several tools failed - the reporting layer renders both. `error`/
    `expected` remain for something genuinely unforeseen escaping run()
    itself, which is the only way to reach them now.
    """

    result: object = None
    error: object = None
    expected: bool = True

    @property
    def ok(self):
        return self.error is None


class AnalysisBridge:
    """Runs the existing pipeline over a set of changed files.

    config (docs/16-configuration-system.md) is resolved once at watch-session
    startup and held here for the session's lifetime - not re-discovered per
    batch, and not hot-reloaded if the file changes mid-session (a config
    change needs a restart, exactly like the registered-adapter set already
    does).
    """

    def __init__(self, config=None):
        self._config = config

    def analyze_paths(self, paths):
        """Analyze exactly these paths and report what happened.

        Failures are returned rather than raised: watch mode runs continuously,
        and one bad analysis must never end the session.
        """
        try:
            return AnalysisOutcome(result=run(list(paths), config=self._config))
        except Exception as exc:  # noqa: BLE001 - deliberate last line of defence
            # Anything unforeseen is handed back with its traceback intact so the
            # reporting layer can show it in full. A silently dead watcher is the
            # worse bug. Analyzer failures never reach here - run() records them
            # in the result instead of raising (docs/15).
            return AnalysisOutcome(error=exc, expected=False)
