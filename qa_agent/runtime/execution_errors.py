"""Internal-only exceptions for the Runtime Execution Engine (Phase G Part
2, docs/22-runtime-execution-engine.md). Neither one ever escapes
`run_runtime_plan()` - both are caught by `executor.py`'s own orchestration
loop and turned into a structured `RuntimeCheckResult` (`SKIPPED` for the
first, `ERROR` for anything else that goes genuinely wrong). Raising one is
simply a clean, local way for a single executor function to short-circuit
its own body with a specific, honest reason, instead of threading an
if/return chain through every command-discovery step.
"""

from __future__ import annotations


class CheckSkipped(Exception):
    """No real, evidence-based command could be found for this check -
    never a guessed/invented command in its place. The message is the
    human-readable reason, copied verbatim into the result's `reason`.
    """


class CheckExecutionError(Exception):
    """Something about actually launching or managing the subprocess went
    wrong in a way that is not a normal PASS/FAIL/TIMEOUT/SKIPPED outcome -
    e.g. the working directory disappeared mid-run. Distinct from an
    ordinary nonzero exit code, which is a real FAIL, not an ERROR.
    """
