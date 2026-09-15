"""G5.4 — Completion Semantics (docs/34-g5-repair-and-completion-design.md):
the one function that decides what a finished `run_agent_loop()` session's
real QA result was.

Deliberately a new, separate file rather than logic folded into `loop.py` -
the same "one file, one clear job" precedent `executor.py` already set
alongside `controller.py` in G5.2. `compute_qa_outcome()` takes no AI
provider at all and is a pure function of one `QAState` - it never reads
`termination_reason`/`ai_stopped` (why the session stopped is irrelevant to
what it actually found), and it never asks the AI what it thinks: the same
"AI is a strategist, never an authority" rule every other module in this
package already enforces, applied here to the session's own final verdict
instead of to one action selection.
"""

from __future__ import annotations

from .models import QAState

QA_OUTCOME_PASSED = "passed"
QA_OUTCOME_FAILED = "failed"
QA_OUTCOME_INCONCLUSIVE = "inconclusive"
QA_OUTCOMES = (QA_OUTCOME_PASSED, QA_OUTCOME_FAILED, QA_OUTCOME_INCONCLUSIVE)

# "verified" duplicated as a literal string - this package's own established
# zero-import rule (the same one models.py's failed_check_ids/
# diagnosed_repairable_check_ids already apply): the real constant is
# qa_agent.ai.runtime_repair_models.VERIFICATION_VERIFIED.
_VERIFICATION_VERIFIED = "verified"


def compute_qa_outcome(state: QAState) -> str:
    """`QA_OUTCOME_INCONCLUSIVE` whenever there is not yet enough real,
    observed evidence to say anything conclusive: no runtime plan at all,
    or a plan exists but at least one of its own planned checks never
    actually produced an execution result (the session stopped before
    finding out) - this project never reports a check as effectively
    passing just because it was never actually run.

    Otherwise, `QA_OUTCOME_FAILED` if any check whose real status is
    fail/timeout/error is still unresolved - "resolved" meaning a real
    `RuntimeRepairResult` for that exact check id whose own
    `verification_status` is `VERIFICATION_VERIFIED` specifically.
    `ACCEPTED`/`APPLIED`/`HELD`/`REJECTED` alone never count as resolving
    it - only a real post-apply re-check that actually passed does (G4's
    own central rule, docs/24, inherited here rather than re-decided).

    `QA_OUTCOME_PASSED` otherwise: every planned check ran, and every
    failure among them - if any - was genuinely, verifiably fixed.
    """
    if state.runtime_plan is None:
        return QA_OUTCOME_INCONCLUSIVE
    planned_ids = {getattr(check, "id", None) for check in state.runtime_plan.checks}
    if not planned_ids <= state.executed_check_ids:
        return QA_OUTCOME_INCONCLUSIVE
    verified_check_ids = {
        getattr(repair, "check_id", None) for repair in state.repairs
        if getattr(repair, "verification_status", None) == _VERIFICATION_VERIFIED
    }
    unresolved = state.failed_check_ids - verified_check_ids
    return QA_OUTCOME_FAILED if unresolved else QA_OUTCOME_PASSED


__all__ = [
    "QA_OUTCOME_FAILED",
    "QA_OUTCOME_INCONCLUSIVE",
    "QA_OUTCOME_PASSED",
    "QA_OUTCOMES",
    "compute_qa_outcome",
]
