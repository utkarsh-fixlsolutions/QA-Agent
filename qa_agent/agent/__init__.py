"""G5 — Autonomous QA Agent (Phase G Part 5). G5.1 (docs/28) decides what
QA action should happen next; G5.2 (docs/29) executes that decision,
collects evidence, updates state, and repeats - a bounded, real autonomous
loop over G1-G4's own existing capabilities. G5.3 (docs/34) wires
`runtime_repair` to the real, existing G4 verified-repair pipeline, so the
loop can now propose, apply, and re-verify a fix for a diagnosed failure,
not just observe and diagnose it. G5.4 (docs/34) adds `compute_qa_outcome`
- a deterministic, evidence-only verdict (`passed`/`failed`/`inconclusive`)
on one finished session's real QA result, independent of why it stopped.

`select_next_action(state, provider)` (G5.1) is the one decision-making
entry point: given a `QAState` and any `AIProvider`-shaped object, it
returns a structured `ControllerDecision` - `continue` with exactly one
deterministically-eligible next action (and, for `runtime_diagnosis`/
`runtime_repair`, a real, currently-valid target), `stop`, or `error`. It
never executes anything itself.

`run_agent_loop(state, provider, root, config=None)` (G5.2) is the one
execution entry point: it calls `select_next_action` repeatedly, validates
and executes whatever it returns via the real, existing G1-G4 engines
(`executor.execute_action`), and updates `QAState` from the real result
each time - bounded by `QAState.max_iterations`, terminating on an
AI-issued stop, a real budget/no-actions exhaustion, or a genuine
controller/provider failure. Returns a structured `AgentResult` - complete
execution history, final state, and a specific termination reason. Never
executes a subprocess, shell command, or file write outside of calling the
real, already-tested G1-G4 functions this project already ships; never
gives the AI direct access to any of them.

`models.py`/`actions.py`/`parser.py`/`prompts.py`/`controller.py` (G5.1)
remain completely execution-free - deliberately decoupled from
`qa_agent.runtime`, `qa_agent.project`, and `qa_agent.ai.diagnosis`/
`qa_agent.ai.runtime_repair`, the same zero-import precedent
`qa_agent/ai/diagnosis.py` already established for reading deterministic
output as plain data. `executor.py`/`loop.py` (G5.2) are the one, explicit,
narrow crossing point where a decision already validated by G5.1 becomes a
real call into those engines - confirmed by direct source-grep isolation
tests for both properties. `qa_agent/runtime/`, `qa_agent/project/`, and
the deterministic analyzer pipeline never import this package either.

Wired into `__main__.py` as `python -m qa_agent agent <path>` (G5.2, docs/29).
"""

from .actions import ACTION_REGISTRY, RUNTIME_CHECK_ACTION_IDS, eligible_actions, get_action, valid_targets
from .completion import (
    QA_OUTCOME_FAILED,
    QA_OUTCOME_INCONCLUSIVE,
    QA_OUTCOME_PASSED,
    QA_OUTCOMES,
    compute_qa_outcome,
)
from .controller import select_next_action
from .executor import (
    EXECUTION_STATUSES,
    STATUS_ERROR as EXECUTION_STATUS_ERROR,
    STATUS_NOT_EXECUTABLE,
    STATUS_OK as EXECUTION_STATUS_OK,
    ExecutionOutcome,
    execute_action,
)
from .loop import (
    HISTORY_EXECUTED,
    HISTORY_REJECTED,
    TERMINATION_AI_STOP,
    TERMINATION_CONTROLLER_ERROR,
    TERMINATION_MAX_ITERATIONS,
    TERMINATION_NO_ACTIONS,
    TERMINATIONS,
    AgentResult,
    ExecutionRecord,
    run_agent_loop,
)
from .models import (
    DECISION_CONTINUE,
    DECISION_ERROR,
    DECISION_STOP,
    DECISIONS,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_OBJECTIVE,
    ERROR_INELIGIBLE_ACTION,
    ERROR_INVALID_TARGET,
    SAFETY_ASSISTED,
    SAFETY_AUTONOMOUS,
    SAFETY_LEVELS,
    SAFETY_READ_ONLY,
    ActionDefinition,
    ControllerDecision,
    QAState,
)
from .parser import DecisionResponse, validate_decision_response
from .prompts import ACTION_SELECTION_GUARDRAILS, build_action_selection_prompt

__all__ = [
    "ACTION_REGISTRY",
    "ACTION_SELECTION_GUARDRAILS",
    "ActionDefinition",
    "AgentResult",
    "ControllerDecision",
    "DECISION_CONTINUE",
    "DECISION_ERROR",
    "DECISION_STOP",
    "DECISIONS",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_OBJECTIVE",
    "DecisionResponse",
    "ERROR_INELIGIBLE_ACTION",
    "ERROR_INVALID_TARGET",
    "EXECUTION_STATUSES",
    "EXECUTION_STATUS_ERROR",
    "EXECUTION_STATUS_OK",
    "ExecutionOutcome",
    "ExecutionRecord",
    "HISTORY_EXECUTED",
    "HISTORY_REJECTED",
    "QAState",
    "QA_OUTCOME_FAILED",
    "QA_OUTCOME_INCONCLUSIVE",
    "QA_OUTCOME_PASSED",
    "QA_OUTCOMES",
    "RUNTIME_CHECK_ACTION_IDS",
    "SAFETY_ASSISTED",
    "SAFETY_AUTONOMOUS",
    "SAFETY_LEVELS",
    "SAFETY_READ_ONLY",
    "STATUS_NOT_EXECUTABLE",
    "TERMINATION_AI_STOP",
    "TERMINATION_CONTROLLER_ERROR",
    "TERMINATION_MAX_ITERATIONS",
    "TERMINATION_NO_ACTIONS",
    "TERMINATIONS",
    "build_action_selection_prompt",
    "compute_qa_outcome",
    "eligible_actions",
    "execute_action",
    "get_action",
    "run_agent_loop",
    "select_next_action",
    "valid_targets",
    "validate_decision_response",
]
