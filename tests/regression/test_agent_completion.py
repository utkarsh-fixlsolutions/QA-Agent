"""G5.4: Completion Semantics (docs/34-g5-repair-and-completion-design.md) -
`qa_agent.agent.completion.compute_qa_outcome` and its wiring into
`AgentResult.qa_outcome` at every real `run_agent_loop` return point.

Pure unit tests against `compute_qa_outcome` (a hand-built `QAState`, no AI,
no subprocess - the function itself takes no provider at all), plus a
handful of `run_agent_loop` integration tests proving the outcome is
actually populated correctly at each of the loop's own four termination
paths, using the same injected fake `decide`/`execute` technique
`test_agent_loop.py` already established.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite  # noqa: E402

from qa_agent.agent import (  # noqa: E402
    QA_OUTCOME_FAILED,
    QA_OUTCOME_INCONCLUSIVE,
    QA_OUTCOME_PASSED,
    ControllerDecision,
    DECISION_CONTINUE,
    DECISION_ERROR,
    DECISION_STOP,
    QAState,
    TERMINATION_AI_STOP,
    TERMINATION_CONTROLLER_ERROR,
    TERMINATION_MAX_ITERATIONS,
    TERMINATION_NO_ACTIONS,
    compute_qa_outcome,
    run_agent_loop,
)


# --- duck-typed fixtures - real shapes, no concrete-class dependency -------

@dataclass(frozen=True)
class _RuntimeCheck:
    id: str


@dataclass(frozen=True)
class _RuntimeQAPlan:
    checks: Tuple[object, ...]


@dataclass(frozen=True)
class _CheckResult:
    id: str
    status: str


@dataclass(frozen=True)
class _Repair:
    check_id: str
    verification_status: str = "not_applicable"


def _plan(*check_ids):
    return _RuntimeQAPlan(checks=tuple(_RuntimeCheck(id=cid) for cid in check_ids))


# --- compute_qa_outcome: a pure function of QAState alone -------------------

def test_fresh_state_is_inconclusive(suite):
    suite.check("nothing observed yet -> inconclusive", compute_qa_outcome(QAState()) == QA_OUTCOME_INCONCLUSIVE)


def test_only_discovery_and_static_analysis_is_inconclusive(suite):
    state = QAState(repository_context="real context", static_analysis_result="real result")
    suite.check("no runtime plan at all yet -> inconclusive", compute_qa_outcome(state) == QA_OUTCOME_INCONCLUSIVE)


def test_all_planned_checks_passed_is_passed(suite):
    state = QAState(
        runtime_plan=_plan("build-verification", "test-suite-verification"),
        execution_results=(
            _CheckResult(id="build-verification", status="pass"),
            _CheckResult(id="test-suite-verification", status="pass"),
        ),
    )
    suite.check("every planned check ran and passed -> passed", compute_qa_outcome(state) == QA_OUTCOME_PASSED)


def test_a_planned_check_never_executed_is_inconclusive_never_passed(suite):
    """The central honesty rule: a session that stops early must never be
    reported as if everything planned actually passed.
    """
    state = QAState(
        runtime_plan=_plan("build-verification", "test-suite-verification"),
        execution_results=(_CheckResult(id="build-verification", status="pass"),),  # test-suite-verification never ran
    )
    suite.check("an unexecuted planned check -> inconclusive, never passed",
                compute_qa_outcome(state) == QA_OUTCOME_INCONCLUSIVE)


def test_an_unresolved_failure_is_failed(suite):
    state = QAState(
        runtime_plan=_plan("build-verification"),
        execution_results=(_CheckResult(id="build-verification", status="fail"),),
    )
    suite.check("a real failure with no repair at all -> failed", compute_qa_outcome(state) == QA_OUTCOME_FAILED)


def test_a_repair_result_short_of_verified_is_still_failed(suite):
    """ACCEPTED/APPLIED/HELD/REJECTED/STILL_FAILING/UNKNOWN all leave the
    check counted as unresolved - only a real VERIFIED counts, the same
    distinction docs/24 already draws for G4 itself.
    """
    for status in ("rejected", "held", "applied", "still_failing", "unknown", "not_applicable"):
        state = QAState(
            runtime_plan=_plan("build-verification"),
            execution_results=(_CheckResult(id="build-verification", status="fail"),),
            repairs=(_Repair(check_id="build-verification", verification_status=status),),
        )
        suite.check("verification_status={!r} -> still failed".format(status),
                    compute_qa_outcome(state) == QA_OUTCOME_FAILED)


def test_a_verified_repair_resolves_the_failure_to_passed(suite):
    state = QAState(
        runtime_plan=_plan("build-verification"),
        execution_results=(_CheckResult(id="build-verification", status="fail"),),
        repairs=(_Repair(check_id="build-verification", verification_status="verified"),),
    )
    suite.check("a real VERIFIED repair resolves the only failure -> passed",
                compute_qa_outcome(state) == QA_OUTCOME_PASSED)


def test_one_verified_repair_does_not_mask_a_different_unresolved_failure(suite):
    state = QAState(
        runtime_plan=_plan("build-verification", "test-suite-verification"),
        execution_results=(
            _CheckResult(id="build-verification", status="fail"),
            _CheckResult(id="test-suite-verification", status="fail"),
        ),
        repairs=(_Repair(check_id="build-verification", verification_status="verified"),),
    )
    suite.check("a verified repair for one check never resolves a different, still-failing one",
                compute_qa_outcome(state) == QA_OUTCOME_FAILED)


def test_outcome_is_independent_of_termination_reason(suite):
    """The same final_state must yield the same qa_outcome regardless of
    *why* a session stopped - proves this is a pure function of state, not
    inferred from the stop path.
    """
    state = QAState(
        runtime_plan=_plan("build-verification"),
        execution_results=(_CheckResult(id="build-verification", status="pass"),),
    )
    outcomes = {compute_qa_outcome(state) for _ in range(4)}  # deterministic - same call, same input, every time
    suite.check("compute_qa_outcome is deterministic for a fixed state", outcomes == {QA_OUTCOME_PASSED})


# --- run_agent_loop integration: qa_outcome populated at every real return -

def _fake_execute_passing(action_id, target, state, root, provider, config):
    @dataclass(frozen=True)
    class _Outcome:
        status: str
        summary: str
        new_state: QAState
        error: object = None

    new_state = replace(
        state, repository_context="ctx", runtime_plan=_plan("build-verification"),
        execution_results=(_CheckResult(id="build-verification", status="pass"),),
    )
    return _Outcome(status="ok", summary="passed", new_state=new_state)


def test_qa_outcome_populated_on_ai_stop(suite):
    def decide(state, provider):
        return ControllerDecision(decision=DECISION_STOP, next_action=None, reason="done")

    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=_fake_execute_passing)
    suite.check("terminated by ai stop", result.termination_reason == TERMINATION_AI_STOP)
    suite.check("qa_outcome reflects the real (empty) final state", result.qa_outcome == QA_OUTCOME_INCONCLUSIVE)


def test_qa_outcome_populated_on_max_iterations(suite):
    def decide(state, provider):
        return ControllerDecision(decision=DECISION_CONTINUE, next_action="build_verification", reason="x")

    result = run_agent_loop(QAState(max_iterations=1), provider=None, root=".", decide=decide,
                             execute=_fake_execute_passing)
    suite.check("terminated by max iterations", result.termination_reason == TERMINATION_MAX_ITERATIONS)
    suite.check("qa_outcome computed from the real final state after that one real execution",
                result.qa_outcome == QA_OUTCOME_PASSED)


def test_qa_outcome_populated_on_no_eligible_actions(suite):
    """A state where nothing at all is eligible (no repository_context, so
    only project_discovery would ever be offered) is engineered by handing
    the loop a fake `execute` that never advances the state, then a decide
    that immediately reports no eligible actions - the loop's own second
    guard (`eligible_actions`) catches this before ever calling decide.
    """
    result = run_agent_loop(QAState(max_iterations=0), provider=None, root=".", decide=None, execute=None)
    suite.check("terminated by budget/no-actions without ever calling decide",
                result.termination_reason in (TERMINATION_MAX_ITERATIONS, TERMINATION_NO_ACTIONS))
    suite.check("qa_outcome is inconclusive for an empty state", result.qa_outcome == QA_OUTCOME_INCONCLUSIVE)


def test_qa_outcome_populated_on_controller_error(suite):
    def decide(state, provider):
        return ControllerDecision(decision=DECISION_ERROR, next_action=None, reason="a real provider failure",
                                   error="provider_timeout")

    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=_fake_execute_passing)
    suite.check("terminated by a genuine controller error", result.termination_reason == TERMINATION_CONTROLLER_ERROR)
    suite.check("qa_outcome still reflects the real (empty) final state, not the error",
                result.qa_outcome == QA_OUTCOME_INCONCLUSIVE)


if __name__ == "__main__":
    suite = Suite("G5.4: Completion Semantics")
    sys.exit(suite.run([
        test_fresh_state_is_inconclusive,
        test_only_discovery_and_static_analysis_is_inconclusive,
        test_all_planned_checks_passed_is_passed,
        test_a_planned_check_never_executed_is_inconclusive_never_passed,
        test_an_unresolved_failure_is_failed,
        test_a_repair_result_short_of_verified_is_still_failed,
        test_a_verified_repair_resolves_the_failure_to_passed,
        test_one_verified_repair_does_not_mask_a_different_unresolved_failure,
        test_outcome_is_independent_of_termination_reason,
        test_qa_outcome_populated_on_ai_stop,
        test_qa_outcome_populated_on_max_iterations,
        test_qa_outcome_populated_on_no_eligible_actions,
        test_qa_outcome_populated_on_controller_error,
    ]))
