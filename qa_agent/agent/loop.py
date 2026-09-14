"""The bounded G5.2 autonomous execution loop (docs/29-g5-execution-loop.md)
- the one function that actually connects G5.1's decision-making
(`controller.select_next_action`) to G5.2's deterministic execution
(`executor.execute_action`):

    observe state -> ask G5.1 for next action -> validate deterministically
        -> resolve target if required -> execute deterministic action
        -> capture structured result -> update QA state -> repeat

`run_agent_loop()` never hard-codes a fixed action sequence - every
decision comes from `select_next_action(state, provider)`, and the loop
only ever does what that decision (once independently re-validated by
G5.1's own controller) actually says. The AI chooses; the deterministic
system executes; nothing here decides *what* should happen next on its
own - that would defeat the entire point of G5.

Bounded, always: `QAState.remaining_budget` and `actions.eligible_actions`
are checked by this loop itself, before ever asking the AI, exactly
mirroring the same two deterministic short-circuits `controller.py`
already performs internally (redundant with them, deliberately - defense
in depth, and the only way this loop can report an *accurate*, specific
termination reason instead of a single generic "stop"). Every iteration
consumes exactly one of `max_iterations`, regardless of whether an action
actually executed, was rejected, or failed - a session can never run
unbounded, no matter how the AI (or a broken mock) behaves.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Optional, Tuple

from .actions import eligible_actions
from .controller import select_next_action
from .executor import execute_action
from .models import (
    DECISION_CONTINUE,
    DECISION_ERROR,
    DECISION_STOP,
    ERROR_INELIGIBLE_ACTION,
    ERROR_INVALID_TARGET,
    QAState,
)

TERMINATION_AI_STOP = "ai_stop"
TERMINATION_MAX_ITERATIONS = "max_iterations"
TERMINATION_NO_ACTIONS = "no_eligible_actions"
TERMINATION_CONTROLLER_ERROR = "controller_error"
TERMINATIONS = (
    TERMINATION_AI_STOP, TERMINATION_MAX_ITERATIONS, TERMINATION_NO_ACTIONS, TERMINATION_CONTROLLER_ERROR,
)

# The two ControllerDecision.error sentinels the loop treats as "the AI
# made a well-formed but currently-wrong choice" - nothing executed, the
# decision is recorded as a rejected history entry, and the loop simply
# asks again next iteration (bounded by the same max_iterations either
# way). Every other DECISION_ERROR (a provider timeout/failure, malformed
# JSON, an invalid schema) is treated as genuinely fatal - "a controller/
# provider failure that prevents further reasoning" - and ends the session
# via TERMINATION_CONTROLLER_ERROR. This distinction is the loop's own,
# deliberate design choice, not something G5.1 itself expresses structurally
# (`ControllerDecision` has no separate "recoverable vs fatal" field) - see
# docs/29 for why: a bad but well-formed choice is a normal, expected part
# of an agent reasoning under uncertainty; a broken response pipeline is not.
_RECOVERABLE_ERRORS = (ERROR_INELIGIBLE_ACTION, ERROR_INVALID_TARGET)

HISTORY_REJECTED = "rejected"  # the decision was not executed - see _RECOVERABLE_ERRORS above
HISTORY_EXECUTED = "executed"  # the decision passed validation and execute_action() actually ran


@dataclass(frozen=True)
class ExecutionRecord:
    """One entry in `AgentResult.history` - one loop iteration's complete
    story, whether or not anything was actually executed.

    `outcome` is `HISTORY_REJECTED` (a decision the controller refused -
    `action_id`/`target` reflect what the AI *asked for*, `status`/`error`
    come from the rejected `ControllerDecision` itself, never from a real
    execution) or `HISTORY_EXECUTED` (`action_id`/`target` reflect what
    actually ran, `status` is the real `executor.EXECUTION_STATUSES` value,
    `summary`/`error` come from the real `ExecutionOutcome`).
    """

    iteration: int
    outcome: str
    action_id: Optional[str]
    target: Optional[str]
    status: str
    summary: str
    elapsed_seconds: float
    error: object = None


@dataclass(frozen=True)
class AgentResult:
    """The complete, final result of one bounded `run_agent_loop()` call.

    `final_state`: the last `QAState` reached - real evidence for a caller
    (or, eventually, G5.3) to read, never re-derived or summarized away.
    `history`: every `ExecutionRecord`, in order, rejected and executed
    alike - a complete account of the session, not just its successes.
    `termination_reason`: one of `TERMINATIONS` - always set, always
    specific (never a bare "stopped" with no explanation of why).
    `ai_stopped`: `True` only for `TERMINATION_AI_STOP` - the explicit
    distinction this step's own spec requires between the AI *choosing* to
    stop and the deterministic system stopping the session on its own
    (budget/no-actions/error). This is *not* a claim that the AI
    determined the QA objective was satisfied - only that it chose not to
    continue; deciding what "objectively satisfied" means is explicitly
    deferred to G5.4.
    """

    final_state: QAState
    history: Tuple[ExecutionRecord, ...] = field(default_factory=tuple)
    termination_reason: str = TERMINATION_NO_ACTIONS
    termination_detail: str = ""
    ai_stopped: bool = False

    def __post_init__(self):
        if self.termination_reason not in TERMINATIONS:
            raise ValueError("AgentResult has an unrecognized termination_reason {!r}".format(self.termination_reason))


def run_agent_loop(state: QAState, provider, root, config=None,
                    decide=select_next_action, execute=execute_action) -> AgentResult:
    """Run the bounded G5.2 loop to completion, starting from `state`.
    Never raises, never runs unbounded, never executes anything the
    deterministic controller has not already validated.

    `decide`/`execute` are injectable only so a test can control exactly
    what the "AI" decides and/or fake execution outcomes deterministically,
    without a real provider or real subprocesses - the same dependency-
    injection convention (`run=`, `extract=`, `execute_check=`) every other
    orchestrator in this project already uses; real callers never need to
    pass either.
    """
    history = []

    while True:
        if state.remaining_budget <= 0:
            return AgentResult(
                final_state=state, history=tuple(history), termination_reason=TERMINATION_MAX_ITERATIONS,
                termination_detail="reached the {}-iteration budget".format(state.max_iterations),
            )
        if not eligible_actions(state):
            return AgentResult(
                final_state=state, history=tuple(history), termination_reason=TERMINATION_NO_ACTIONS,
                termination_detail="no eligible action remains",
            )

        decision = decide(state, provider)

        if decision.decision == DECISION_STOP:
            return AgentResult(
                final_state=state, history=tuple(history), termination_reason=TERMINATION_AI_STOP,
                termination_detail=decision.reason, ai_stopped=True,
            )

        if decision.decision == DECISION_ERROR:
            if decision.error in _RECOVERABLE_ERRORS:
                history.append(ExecutionRecord(
                    iteration=state.iteration, outcome=HISTORY_REJECTED, action_id=None, target=None,
                    status=decision.error, summary=decision.reason, elapsed_seconds=0.0, error=decision.error,
                ))
                state = replace(state, iteration=state.iteration + 1)
                continue
            return AgentResult(
                final_state=state, history=tuple(history), termination_reason=TERMINATION_CONTROLLER_ERROR,
                termination_detail="{} ({})".format(decision.reason, decision.error),
            )

        # decision.decision == DECISION_CONTINUE: a real, currently-eligible
        # action (and, if required, a real, currently-valid target) - only
        # ever reached after controller.select_next_action's own two-tier
        # deterministic re-validation. execute_action() itself trusts this.
        started = time.perf_counter()
        outcome = execute(decision.next_action, decision.target, state, root, provider, config)
        elapsed = time.perf_counter() - started

        history.append(ExecutionRecord(
            iteration=state.iteration, outcome=HISTORY_EXECUTED, action_id=decision.next_action,
            target=decision.target, status=outcome.status, summary=outcome.summary,
            elapsed_seconds=elapsed, error=outcome.error,
        ))
        state = replace(outcome.new_state, iteration=state.iteration + 1)


__all__ = [
    "HISTORY_EXECUTED",
    "HISTORY_REJECTED",
    "TERMINATION_AI_STOP",
    "TERMINATION_CONTROLLER_ERROR",
    "TERMINATION_MAX_ITERATIONS",
    "TERMINATION_NO_ACTIONS",
    "TERMINATIONS",
    "AgentResult",
    "ExecutionRecord",
    "run_agent_loop",
]
