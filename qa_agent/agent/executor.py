"""Deterministic action executor (G5.2, docs/29-g5-execution-loop.md) - the
one file in `qa_agent/agent/` that actually calls the real, existing
deterministic QA engines. `models.py`/`actions.py`/`parser.py`/`prompts.py`/
`controller.py` (G5.1) stay completely execution-free, by design (see their
own module docstrings) - `execute_action()` is the one, explicit, narrow
crossing point where an already-validated action id becomes a real call
into `qa_agent.project`/`qa_agent.runtime`/`qa_agent.runner`/`qa_agent.ai`.

No new engine exists here. Every action id maps to exactly one existing,
unmodified function this project already built and already tested (G1-G4):

    project_discovery          -> discover_project + build_repository_context
    runtime_plan                -> plan_runtime_qa
    static_analysis              -> runner.run
    server_startup/
    build_verification/
    test_suite_verification/
    environment_validation/
    static_assets                -> run_runtime_plan, scoped to one planned check
    runtime_diagnosis            -> diagnose_runtime_failure (G3), for one target check
    runtime_repair                -> repair_runtime_failure (G4), for one target check (G5.3, docs/34)

An unrecognized action id still has no entry in `_EXECUTORS` at all -
`_EXECUTORS.get` returns `None` and `STATUS_NOT_EXECUTABLE` is returned,
never a silent no-op success - but this is now a genuinely impossible case
for every id `eligible_actions()` can ever offer (every registry entry is
`implemented=True` as of G5.3); it remains as a defensive fallback only.

Every handler returns a structured `ExecutionOutcome`, never raises out of
`execute_action` itself - a genuine crash inside one handler is caught and
reported as `STATUS_ERROR`, the same "one action's failure never crashes
the caller" contract every other per-item execution loop in this project
(runner.py's own per-adapter isolation, `diagnose_runtime_failures`'s own
per-check isolation) already upholds.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from qa_agent.ai import diagnose_runtime_failure, repair_runtime_failure
from qa_agent.ai import OUTCOME_ERROR as _REPAIR_OUTCOME_ERROR
from qa_agent.project import build_repository_context, discover_project
from qa_agent.runner import run as run_static_analysis
from qa_agent.runtime import plan_runtime_qa, run_runtime_plan

from .actions import RUNTIME_CHECK_ACTION_IDS
from .models import QAState

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_NOT_EXECUTABLE = "not_executable"
EXECUTION_STATUSES = (STATUS_OK, STATUS_ERROR, STATUS_NOT_EXECUTABLE)


@dataclass(frozen=True)
class ExecutionOutcome:
    """What happened when `execute_action` actually ran one action.

    `status` is about the *executor's own process*, never a verdict on the
    underlying QA check - `STATUS_OK` covers a real build FAIL just as much
    as a real build PASS (the executor did its one job: run the real thing
    and capture what really happened). `STATUS_ERROR` means the executor
    itself could not even complete the attempt (a missing precondition, a
    genuine exception) - a different kind of problem from a QA failure,
    and `loop.py`'s own job to decide what that means for the session, not
    this module's.
    `new_state`: the state to use going forward. On `STATUS_OK` this is a
    real, updated `QAState` (via `dataclasses.replace`, never a mutation of
    the original - every G1-G4 result type this project uses is itself
    frozen/immutable, so nothing here needs to defend against that
    separately). On `STATUS_ERROR`/`STATUS_NOT_EXECUTABLE`, `new_state` is
    the *original*, unchanged `state` - a failed or impossible execution
    never silently advances the session's facts.
    """

    status: str
    summary: str
    new_state: QAState
    error: object = None

    def __post_init__(self):
        if self.status not in EXECUTION_STATUSES:
            raise ValueError("ExecutionOutcome has an unrecognized status {!r}".format(self.status))


def _execute_project_discovery(target, state: QAState, root, provider, config) -> ExecutionOutcome:
    result = discover_project(root)
    if result.project is None:
        return ExecutionOutcome(
            status=STATUS_ERROR, summary="project discovery could not complete: {}".format(result.status),
            new_state=state,
        )
    context = build_repository_context(result.project)
    new_state = replace(state, repository_context=context)
    summary = "discovered {} language(s), {} framework(s), {} package manager(s)".format(
        len(context.project.languages), len(context.project.frameworks), len(context.project.package_managers),
    )
    return ExecutionOutcome(status=STATUS_OK, summary=summary, new_state=new_state)


def _execute_runtime_plan(target, state: QAState, root, provider, config) -> ExecutionOutcome:
    if state.repository_context is None:
        return ExecutionOutcome(
            status=STATUS_ERROR, summary="runtime_plan requires a repository context; none in state",
            new_state=state,
        )
    plan = plan_runtime_qa(state.repository_context)
    new_state = replace(state, runtime_plan=plan)
    return ExecutionOutcome(status=STATUS_OK, summary="planned {} check(s)".format(len(plan.checks)), new_state=new_state)


def _execute_static_analysis(target, state: QAState, root, provider, config) -> ExecutionOutcome:
    result = run_static_analysis([str(root)], config=config)
    new_state = replace(state, static_analysis_result=result)
    status = STATUS_ERROR if result.tool_errors else STATUS_OK
    summary = "{} finding(s) across {} tool(s), {} tool error(s)".format(
        len(result.findings), len(result.tools_used), len(result.tool_errors),
    )
    return ExecutionOutcome(status=status, summary=summary, new_state=new_state)


def _make_runtime_check_executor(check_id: str):
    """One real Phase G Part 2 executor call, scoped to exactly the one
    planned check named by `check_id` - the same "a plan with only the one
    check" technique `qa_agent/ai/runtime_repair.py`'s own `_run_single_check`
    already established for exactly this purpose (re-running one check in
    isolation via the real, unmodified `run_runtime_plan`), reused here
    rather than reimplemented.
    """

    def handler(target, state: QAState, root, provider, config) -> ExecutionOutcome:
        if state.runtime_plan is None:
            return ExecutionOutcome(
                status=STATUS_ERROR, summary="'{}' requires a runtime plan; none in state".format(check_id),
                new_state=state,
            )
        check = next((c for c in state.runtime_plan.checks if c.id == check_id), None)
        if check is None:
            return ExecutionOutcome(
                status=STATUS_ERROR, summary="'{}' was not planned for this repository".format(check_id),
                new_state=state,
            )
        single_plan = replace(state.runtime_plan, checks=(check,))
        execution = run_runtime_plan(single_plan, root, config)
        result = execution.results[0] if execution.results else None
        if result is None:
            return ExecutionOutcome(
                status=STATUS_ERROR, summary="execution produced no result for '{}'".format(check_id),
                new_state=state,
            )
        new_state = replace(state, execution_results=state.execution_results + (result,))
        summary = "{}: {}".format(result.status, result.reason)
        return ExecutionOutcome(status=STATUS_OK, summary=summary, new_state=new_state)

    return handler


def _execute_runtime_diagnosis(target, state: QAState, root, provider, config) -> ExecutionOutcome:
    """One real G3 `diagnose_runtime_failure` call, for exactly the one
    target check id the controller already validated. This is a thin
    execution of an existing, unmodified G3 function - it does not decide
    *when* to diagnose or *what* happens after (that is the AI's next
    decision, via the loop) - never diagnosis orchestration, which is
    explicitly G5.3's own job.
    """
    if target is None:
        return ExecutionOutcome(status=STATUS_ERROR, summary="runtime_diagnosis requires a target check id", new_state=state)
    check_result = next((r for r in state.execution_results if getattr(r, "id", None) == target), None)
    if check_result is None:
        return ExecutionOutcome(
            status=STATUS_ERROR, summary="no execution result exists for target '{}'".format(target),
            new_state=state,
        )
    runtime_check = None
    if state.runtime_plan is not None:
        runtime_check = next((c for c in state.runtime_plan.checks if c.id == target), None)
    diagnosis = diagnose_runtime_failure(check_result, state.repository_context, provider, runtime_check=runtime_check)
    new_state = replace(state, diagnoses=state.diagnoses + (diagnosis,))
    summary = "{}: {}".format(diagnosis.diagnosis_status, diagnosis.summary or diagnosis.error or "")
    return ExecutionOutcome(status=STATUS_OK, summary=summary, new_state=new_state)


@dataclass(frozen=True)
class _ExecutionResultView:
    """The one field `repair_runtime_failure()` (G4) actually reads off its
    `execution_result` argument - `.plan`, used only to re-run one check in
    isolation via `_run_single_check`/`run_runtime_plan`. `QAState` has no
    `RuntimeExecutionResult` of its own (G5.2 accumulates individual
    `RuntimeCheckResult`s one at a time instead), so this is a minimal,
    real, non-guessed stand-in - the same duck-typing precedent
    `runtime_repair.py`'s own `_RuntimeRepairFinding` already established
    for handing G4 exactly the shape it needs, nothing more.
    """

    plan: object


def _execute_runtime_repair(target, state: QAState, root, provider, config) -> ExecutionOutcome:
    """One real G4 `repair_runtime_failure` call (docs/24), for exactly the
    one target check id the controller already validated (G5.3, docs/34).
    This is a thin execution of an existing, unmodified G4 function - every
    eligibility/proposal/apply/validate/verify decision inside it is G4's
    own, untouched; this function only resolves the real inputs G4 needs
    out of `QAState` and records the real result.
    """
    if target is None:
        return ExecutionOutcome(status=STATUS_ERROR, summary="runtime_repair requires a target check id", new_state=state)
    check_result = next((r for r in state.execution_results if getattr(r, "id", None) == target), None)
    diagnosis = next((d for d in state.diagnoses if getattr(d, "check_id", None) == target), None)
    if check_result is None or diagnosis is None:
        return ExecutionOutcome(
            status=STATUS_ERROR,
            summary="runtime_repair target '{}' has no matching execution result and/or diagnosis in state"
                    .format(target),
            new_state=state,
        )
    runtime_check = None
    if state.runtime_plan is not None:
        runtime_check = next((c for c in state.runtime_plan.checks if c.id == target), None)
    result = repair_runtime_failure(
        check_result, diagnosis, _ExecutionResultView(plan=state.runtime_plan),
        state.repository_context, provider, root, runtime_check=runtime_check, config=config,
    )
    new_state = replace(state, repairs=state.repairs + (result,))
    # STATUS_ERROR only for the one outcome that is a genuine executor-level
    # failure (no workspace, an apply crash, an unexpected exception inside
    # G4 itself) - every other outcome (not_eligible/rejected/held/
    # validation_failed/applied_but_still_failing/verified) is STATUS_OK:
    # the executor did its real job and produced a real, informative
    # conclusion, exactly mirroring _execute_static_analysis's own
    # "executor status is about the executor's own process, never a verdict
    # on the underlying result" rule. Never claim success here beyond what
    # `result.outcome`/`result.explanation` themselves say - G4's own
    # VERIFIED-only-on-a-real-re-check rule is what this summary reports
    # verbatim, never re-interpreted.
    status = STATUS_ERROR if result.outcome == _REPAIR_OUTCOME_ERROR else STATUS_OK
    summary = "{}: {}".format(result.outcome, result.explanation)
    return ExecutionOutcome(status=status, summary=summary, new_state=new_state)


_EXECUTORS = {
    "project_discovery": _execute_project_discovery,
    "runtime_plan": _execute_runtime_plan,
    "static_analysis": _execute_static_analysis,
    "runtime_diagnosis": _execute_runtime_diagnosis,
    "runtime_repair": _execute_runtime_repair,
}
_EXECUTORS.update({
    action_id: _make_runtime_check_executor(check_id)
    for action_id, check_id in RUNTIME_CHECK_ACTION_IDS.items()
})


def execute_action(action_id: str, target, state: QAState, root, provider, config=None) -> ExecutionOutcome:
    """Execute exactly one already-validated action. Never raises - a
    genuinely unexpected exception inside a handler is caught here and
    returned as `STATUS_ERROR`, never propagated to the caller.

    This function trusts its caller (`loop.py`) to have already validated
    `action_id`/`target` deterministically (eligibility, dependencies,
    target validity) - it does not re-derive eligibility itself, matching
    `apply_repair`'s own precedent (Phase E Part 2: the workspace applies
    what it's given, the caller is responsible for having already decided
    it should). An unrecognized `action_id` (one with no entry in
    `_EXECUTORS` at all - in practice, only `runtime_repair` can reach this
    point, since every other unimplemented/ineligible id is already refused
    by `controller.select_next_action` before this is ever called) returns
    `STATUS_NOT_EXECUTABLE`, never a fabricated success.
    """
    handler = _EXECUTORS.get(action_id)
    if handler is None:
        return ExecutionOutcome(
            status=STATUS_NOT_EXECUTABLE, summary="no executor is wired up for '{}'".format(action_id),
            new_state=state,
        )
    try:
        return handler(target, state, root, provider, config)
    except Exception as exc:  # noqa: BLE001 - one action's crash must never abort the loop
        return ExecutionOutcome(
            status=STATUS_ERROR, summary="unexpected error executing '{}': {}: {}".format(
                action_id, type(exc).__name__, exc),
            new_state=state, error="{}: {}".format(type(exc).__name__, exc),
        )


__all__ = [
    "EXECUTION_STATUSES",
    "STATUS_ERROR",
    "STATUS_NOT_EXECUTABLE",
    "STATUS_OK",
    "ExecutionOutcome",
    "execute_action",
]
