"""G5.2: Autonomous QA Execution Loop (docs/29-g5-execution-loop.md) -
`qa_agent.agent.executor.execute_action` and `qa_agent.agent.loop.run_agent_loop`.

Two tiers, matching this project's own established split for orchestration
logic: fast, precise unit tests against `run_agent_loop` with injected fake
`decide`/`execute` callables (control exactly what "the AI" and "execution"
do, with no real provider, no real subprocess, no real fixture files - for
proving the loop's own termination/rejection/state-threading logic in
isolation), and a handful of real, end-to-end tests using a real
`TempProject` fixture, a real scripted `MockProvider`-shaped sequence, and
the real `execute_action`/`run_agent_loop` together (real npm/node
subprocesses, the same discipline G1-G4's own test suites already use for
their own real-execution proofs). Every automated scenario uses a fake or
`MockProvider` - no live Ollama/OpenRouter call anywhere in this file.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.agent import (  # noqa: E402
    DECISION_CONTINUE,
    DECISION_ERROR,
    DECISION_STOP,
    ERROR_INELIGIBLE_ACTION,
    ERROR_INVALID_TARGET,
    HISTORY_EXECUTED,
    HISTORY_REJECTED,
    TERMINATION_AI_STOP,
    TERMINATION_CONTROLLER_ERROR,
    TERMINATION_MAX_ITERATIONS,
    TERMINATION_NO_ACTIONS,
    AgentResult,
    ControllerDecision,
    QAState,
    execute_action,
    run_agent_loop,
)
from qa_agent.agent.executor import STATUS_ERROR, STATUS_NOT_EXECUTABLE, STATUS_OK  # noqa: E402
from qa_agent.ai import ConnectionResult, LLMResponse, MockProvider  # noqa: E402


# --- fakes for fast, precise loop-level unit tests --------------------------

class _Writer:
    def __init__(self, path):
        self.path = path

    write = TempProject.write


def _decision(decision=DECISION_CONTINUE, next_action=None, reason="x", target=None, error=None):
    return ControllerDecision(decision=decision, next_action=next_action, reason=reason, target=target, error=error)


def _scripted_decide(decisions):
    """A fake `decide(state, provider)` returning each of `decisions` in
    order, one per call - the exact same "control the sequence, not the
    loop" technique already used elsewhere in this project's own test
    suites (`test_ai_repair_loop.py`'s `_sequenced_run`).
    """
    calls = {"n": 0, "states": []}

    def decide(state, provider):
        calls["states"].append(state)
        index = calls["n"]
        calls["n"] += 1
        return decisions[index] if index < len(decisions) else decisions[-1]

    decide.calls = calls
    return decide


def _fake_execute(status=STATUS_OK, summary="did the thing", state_updater=None):
    def execute(action_id, target, state, root, provider, config):
        new_state = state_updater(state) if state_updater else state
        return _Outcome(status=status, summary=summary, new_state=new_state)

    return execute


@dataclass(frozen=True)
class _Outcome:
    status: str
    summary: str
    new_state: QAState
    error: object = None


# --- loop-level unit tests: termination, rejection, budget, threading ------

def test_stop_decision_terminates_with_ai_stop(suite):
    decide = _scripted_decide([_decision(decision=DECISION_STOP, reason="nothing left to do")])
    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=_fake_execute())
    suite.check("terminates with TERMINATION_AI_STOP", result.termination_reason == TERMINATION_AI_STOP)
    suite.check("ai_stopped is True", result.ai_stopped is True)
    suite.check("no execution happened", result.history == ())


def test_max_iterations_terminates_deterministically(suite):
    decide = _scripted_decide([_decision(next_action="project_discovery")])
    state = QAState(iteration=3, max_iterations=3)
    result = run_agent_loop(state, provider=None, root=".", decide=decide, execute=_fake_execute())
    suite.check("terminates with TERMINATION_MAX_ITERATIONS", result.termination_reason == TERMINATION_MAX_ITERATIONS)
    suite.check("the loop never even asked for a decision", decide.calls["n"] == 0)


def test_no_eligible_actions_terminates_deterministically(suite):
    # A state where every implemented action is already completed/not
    # applicable - the same "genuinely empty" state G5.1's own test suite
    # already proves is empty.
    state = QAState(
        repository_context=_ctx(), runtime_plan=_plan("build-verification"), static_analysis_result=object(),
        execution_results=(_result("build-verification", "pass"),),
    )
    decide = _scripted_decide([_decision(next_action="project_discovery")])
    result = run_agent_loop(state, provider=None, root=".", decide=decide, execute=_fake_execute())
    suite.check("terminates with TERMINATION_NO_ACTIONS", result.termination_reason == TERMINATION_NO_ACTIONS)
    suite.check("the loop never even asked for a decision", decide.calls["n"] == 0)


def test_provider_failure_terminates_with_controller_error(suite):
    decide = _scripted_decide([_decision(decision=DECISION_ERROR, reason="AI provider error", error="offline")])
    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=_fake_execute())
    suite.check("terminates with TERMINATION_CONTROLLER_ERROR", result.termination_reason == TERMINATION_CONTROLLER_ERROR)
    suite.check("the real provider error is preserved in the detail", "offline" in result.termination_detail)
    suite.check("nothing was executed", result.history == ())


def test_malformed_decision_terminates_with_controller_error(suite):
    decide = _scripted_decide([
        _decision(decision=DECISION_ERROR, reason="AI response could not be validated", error="not valid JSON: x")
    ])
    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=_fake_execute())
    suite.check("terminates with TERMINATION_CONTROLLER_ERROR", result.termination_reason == TERMINATION_CONTROLLER_ERROR)


def test_invalid_action_is_rejected_and_the_loop_continues(suite):
    """The key distinction this stage draws: an ineligible-but-well-formed
    selection is a recoverable, recorded rejection - not a fatal error -
    exactly mirroring "a QA failure is not automatically a controller
    failure" one level up (a bad *choice* is not a broken *pipeline*).
    """
    decide = _scripted_decide([
        _decision(decision=DECISION_ERROR, next_action="not_a_real_action", error=ERROR_INELIGIBLE_ACTION, reason="ineligible"),
        _decision(decision=DECISION_STOP, reason="giving up after the bad choice"),
    ])
    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=_fake_execute())
    suite.check("the loop asked for a second decision rather than dying", decide.calls["n"] == 2)
    suite.check("exactly one rejected history entry was recorded", len(result.history) == 1)
    suite.check("the rejected entry is marked HISTORY_REJECTED, never executed",
                result.history[0].outcome == HISTORY_REJECTED)
    suite.check("the loop still reaches a real termination", result.termination_reason == TERMINATION_AI_STOP)


def test_invalid_target_is_rejected_and_the_loop_continues(suite):
    decide = _scripted_decide([
        _decision(decision=DECISION_ERROR, next_action="runtime_diagnosis", target="nonexistent-check",
                   error=ERROR_INVALID_TARGET, reason="bad target"),
        _decision(decision=DECISION_STOP, reason="done"),
    ])
    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=_fake_execute())
    suite.check("recorded as rejected, not executed", result.history[0].outcome == HISTORY_REJECTED)
    suite.check("the loop continued to a real termination", result.termination_reason == TERMINATION_AI_STOP)


def test_repeated_rejections_are_still_bounded_by_max_iterations(suite):
    """Proves a broken/adversarial decider that never stops on its own
    still cannot run the loop unbounded - the exact "infinite execution is
    impossible" guarantee, exercised at its most adversarial: every single
    decision is a rejected one.
    """
    always_invalid = _decision(decision=DECISION_ERROR, next_action="not_a_real_action", error=ERROR_INELIGIBLE_ACTION, reason="x")

    def decide(state, provider):
        return always_invalid

    result = run_agent_loop(QAState(max_iterations=4), provider=None, root=".", decide=decide, execute=_fake_execute())
    suite.check("terminates with TERMINATION_MAX_ITERATIONS, never runs forever",
                result.termination_reason == TERMINATION_MAX_ITERATIONS)
    suite.check("exactly max_iterations rejected entries were recorded", len(result.history) == 4)
    suite.check("every entry is a rejection - nothing was ever executed", all(r.outcome == HISTORY_REJECTED for r in result.history))


def test_failed_qa_action_does_not_terminate_the_loop(suite):
    """The whole point of G5: a real QA FAILURE (build FAIL) becomes
    evidence, not a reason to give up - the agent gets another turn.
    """
    decide = _scripted_decide([
        _decision(next_action="build_verification"),
        _decision(decision=DECISION_STOP, reason="build failed, reporting and stopping"),
    ])
    execute = _fake_execute(status=STATUS_OK, summary="fail: build exited 1")  # STATUS_OK: the executor did its job; the real check failed
    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=execute)
    suite.check("the loop reached a second decision after a real QA failure", decide.calls["n"] == 2)
    suite.check("the failure is recorded as a normal, executed history entry",
                result.history[0].outcome == HISTORY_EXECUTED and "fail" in result.history[0].summary)
    suite.check("termination is the AI's own choice, not a forced failure", result.termination_reason == TERMINATION_AI_STOP)


def test_executor_level_error_does_not_terminate_the_loop_either(suite):
    """A precondition failure inside the executor itself (STATUS_ERROR) is
    still just evidence, per this step's own "only controller/provider
    failures... should terminate the loop" rule - an executor hiccup is
    neither.
    """
    decide = _scripted_decide([
        _decision(next_action="build_verification"),
        _decision(decision=DECISION_STOP, reason="done"),
    ])
    execute = _fake_execute(status=STATUS_ERROR, summary="build_verification requires a runtime plan")
    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=execute)
    suite.check("the loop continued past the executor error", decide.calls["n"] == 2)
    suite.check("the error is recorded, not swallowed", result.history[0].status == STATUS_ERROR)


def test_state_update_reaches_the_next_ai_decision(suite):
    """One of the most important properties in G5.2: the *second* call to
    `decide` must receive a state that actually reflects the *first*
    action's real result - not just "two calls happened".
    """
    def state_updater(state):
        return replace(state, runtime_plan=_plan("build-verification"))

    decide = _scripted_decide([
        _decision(next_action="project_discovery"),
        _decision(decision=DECISION_STOP, reason="done"),
    ])
    execute = _fake_execute(status=STATUS_OK, summary="discovered", state_updater=state_updater)
    run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=execute)

    first_call_state, second_call_state = decide.calls["states"]
    suite.check("the first decision saw no runtime plan yet", first_call_state.runtime_plan is None)
    suite.check("the second decision saw the real, updated runtime plan from the first action's result",
                second_call_state.runtime_plan is not None and second_call_state.runtime_plan.checks[0].id == "build-verification")
    suite.check("iteration was actually advanced between calls", second_call_state.iteration == first_call_state.iteration + 1)


def test_history_captures_the_required_fields(suite):
    decide = _scripted_decide([
        _decision(next_action="build_verification"),
        _decision(decision=DECISION_STOP, reason="done"),
    ])
    execute = _fake_execute(status=STATUS_OK, summary="pass: build exited 0")
    result = run_agent_loop(QAState(), provider=None, root=".", decide=decide, execute=execute)
    record = result.history[0]
    suite.check("iteration is recorded", record.iteration == 0)
    suite.check("action_id is recorded", record.action_id == "build_verification")
    suite.check("status is recorded", record.status == STATUS_OK)
    suite.check("summary is recorded", "build exited 0" in record.summary)
    suite.check("elapsed_seconds is recorded, not negative", record.elapsed_seconds >= 0)


# --- executor-level unit tests (real functions, hand-built state) ----------

def _ctx():
    @dataclass(frozen=True)
    class _P:
        repository_type: str = "single-package"
        application_type: str = "backend"
        languages: tuple = ()
        frameworks: tuple = ()
        package_managers: tuple = ()

    @dataclass(frozen=True)
    class _C:
        project: object

    return _C(project=_P())


def _plan(*check_ids):
    @dataclass(frozen=True)
    class _Check:
        id: str

    @dataclass(frozen=True)
    class _Plan:
        checks: Tuple[object, ...]

    return _Plan(checks=tuple(_Check(id=cid) for cid in check_ids))


def _result(check_id, status, reason=""):
    # Mirrors the real RuntimeCheckResult's full field set
    # (qa_agent/runtime/execution_models.py) - diagnosis_prompts.py/
    # runtime_repair.py both read several of these directly (duration,
    # details, start_time, end_time), so a duck-typed fixture missing any
    # of them fails deep inside G3/G4 with an AttributeError instead of
    # exercising the real diagnosis/repair path this file's own G5.3 tests
    # need to actually reach.
    @dataclass(frozen=True)
    class _R:
        id: str
        status: str
        reason: str = ""
        name: str = ""
        start_time: str = "2026-01-01T00:00:00"
        end_time: str = "2026-01-01T00:00:01"
        duration: float = 0.1
        details: str = ""
        logs: tuple = ()
        artifacts: tuple = ()
        exception: object = None

    return _R(id=check_id, status=status, reason=reason, name=check_id)


def _Diagnosis(check_id, diagnosis_status="diagnosed"):
    @dataclass(frozen=True)
    class _D:
        check_id: str
        diagnosis_status: str = "diagnosed"

    return _D(check_id=check_id, diagnosis_status=diagnosis_status)


def test_execute_action_unknown_id_is_not_executable(suite):
    outcome = execute_action("totally_made_up_action", None, QAState(), ".", None)
    suite.check("an id with no real executor is never a fabricated success",
                outcome.status == STATUS_NOT_EXECUTABLE)
    suite.check("state is unchanged", outcome.new_state == QAState())


def test_execute_project_discovery_real(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("main.py", "print('hi')\n")
        outcome = execute_action("project_discovery", None, QAState(), root, None)
    suite.check("a real discovery succeeded", outcome.status == STATUS_OK)
    suite.check("the real repository context is now in state", outcome.new_state.repository_context is not None)


def test_execute_runtime_plan_requires_discovery_first(suite):
    outcome = execute_action("runtime_plan", None, QAState(), ".", None)
    suite.check("a missing precondition is a real, reported error, not a crash", outcome.status == STATUS_ERROR)
    suite.check("state is unchanged on error", outcome.new_state.runtime_plan is None)


def test_execute_build_verification_real_pass(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({"scripts": {"build": "node build.js"}}))
        writer.write("build.js", "console.log('ok'); process.exit(0);\n")

        from qa_agent.project import build_repository_context, discover_project
        from qa_agent.runtime import plan_runtime_qa

        discovery = discover_project(root)
        context = build_repository_context(discovery.project)
        plan = plan_runtime_qa(context)
        state = QAState(repository_context=context, runtime_plan=plan)

        outcome = execute_action("build_verification", None, state, root, None)
    suite.check("a real build_verification run succeeded", outcome.status == STATUS_OK)
    suite.check("a real, passing RuntimeCheckResult is now in state",
                len(outcome.new_state.execution_results) == 1 and outcome.new_state.execution_results[0].status == "pass")


def test_execute_runtime_diagnosis_real_with_mock_provider(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({"scripts": {"build": "node -e \"console.error('boom'); process.exit(1)\""}}))

        from qa_agent.project import build_repository_context, discover_project
        from qa_agent.runtime import plan_runtime_qa

        discovery = discover_project(root)
        context = build_repository_context(discovery.project)
        plan = plan_runtime_qa(context)
        state = replace(QAState(repository_context=context, runtime_plan=plan),
                         execution_results=(_result("build-verification", "fail", "build exited 1"),))

        provider = MockProvider(response_text=json.dumps({
            "summary": "build failed", "severity": "error", "confidence": 0.5,
            "root_cause": "unclear", "evidence": ["build exited 1"],
            "affected_files": [], "affected_components": [], "recommended_action": "investigate",
        }))
        outcome = execute_action("runtime_diagnosis", "build-verification", state, root, provider)
    suite.check("a real diagnosis was produced via the real, unmodified G3 function", outcome.status == STATUS_OK)
    suite.check("a real RuntimeDiagnosis is now in state", len(outcome.new_state.diagnoses) == 1)


def _diagnosis_response(affected_files=("build.js",), evidence=("intentional failure",)):
    return json.dumps({
        "summary": "the build script exits with a non-zero status", "severity": "error", "confidence": 0.9,
        "root_cause": "build.js intentionally exits with a failure", "evidence": list(evidence),
        "affected_files": list(affected_files), "affected_components": ["build.js"],
        "recommended_action": "fix build.js so it exits 0",
    })


BROKEN_BUILD_JS = "console.error('Error: intentional failure');\nprocess.exit(1);\n"
FIXED_BUILD_JS = "console.log('build ok');\nprocess.exit(0);"


def _repair_response(replacement=FIXED_BUILD_JS, confidence=0.9, start_line=1, end_line=2):
    return json.dumps({
        "explanation": "fixes the build", "replacement": replacement,
        "confidence": confidence, "start_line": start_line, "end_line": end_line,
    })


def test_execute_runtime_repair_requires_a_target(suite):
    outcome = execute_action("runtime_repair", None, QAState(), ".", None)
    suite.check("a missing target is a real, reported error, not a crash", outcome.status == STATUS_ERROR)
    suite.check("state is unchanged on error", outcome.new_state == QAState())


def test_execute_runtime_repair_target_with_no_matching_evidence_in_state(suite):
    outcome = execute_action("runtime_repair", "build-verification", QAState(), ".", None)
    suite.check("a target with no execution result/diagnosis in state is a reported error, not a crash",
                outcome.status == STATUS_ERROR)
    suite.check("state is unchanged on error", outcome.new_state == QAState())


def test_execute_runtime_repair_wires_the_real_g4_call_signature(suite):
    """Proves the executor calls G4's real `repair_runtime_failure` with
    exactly the shape docs/34 designed - not just that *something* runs.
    """
    from qa_agent.agent import executor as executor_module
    from qa_agent.ai import runtime_repair_models

    captured = {}

    def fake_repair(check_result, diagnosis, execution_result, repository_context, provider, root,
                     runtime_check=None, config=None):
        captured.update(
            check_result=check_result, diagnosis=diagnosis, execution_result=execution_result,
            repository_context=repository_context, provider=provider, root=root,
            runtime_check=runtime_check, config=config,
        )
        return runtime_repair_models.RuntimeRepairResult(
            check_id=check_result.id, check_name=check_result.name,
            original_runtime_status=check_result.status, diagnosis_status=diagnosis.diagnosis_status,
            outcome=runtime_repair_models.OUTCOME_VERIFIED,
            eligibility=runtime_repair_models.EligibilityResult(eligible=True, reason="ok"),
            verification_status=runtime_repair_models.VERIFICATION_VERIFIED,
            explanation="fake verified for this test",
        )

    original = executor_module.repair_runtime_failure
    executor_module.repair_runtime_failure = fake_repair
    try:
        check_result = _result("build-verification", "fail", "build exited 1")
        diagnosis = _Diagnosis(check_id="build-verification")
        plan = _plan("build-verification")
        state = QAState(repository_context="a real context", runtime_plan=plan, execution_results=(check_result,),
                         diagnoses=(diagnosis,))
        outcome = execute_action("runtime_repair", "build-verification", state, "/some/root", "a real provider",
                                  config="a real config")
    finally:
        executor_module.repair_runtime_failure = original

    suite.check("executor reports STATUS_OK for a VERIFIED outcome", outcome.status == STATUS_OK)
    suite.check("the real check_result was passed through", captured["check_result"] is check_result)
    suite.check("the real diagnosis was passed through", captured["diagnosis"] is diagnosis)
    suite.check("the execution_result view exposes the real .plan", captured["execution_result"].plan is plan)
    suite.check("repository_context/provider/root/config were all passed through unchanged",
                captured["repository_context"] == "a real context" and captured["provider"] == "a real provider"
                and captured["root"] == "/some/root" and captured["config"] == "a real config")
    suite.check("the real result landed in state.repairs",
                len(outcome.new_state.repairs) == 1 and outcome.new_state.repairs[0].outcome == "verified")


def test_execute_runtime_repair_outcome_error_is_status_error(suite):
    from qa_agent.agent import executor as executor_module
    from qa_agent.ai import runtime_repair_models

    def fake_repair(check_result, diagnosis, execution_result, repository_context, provider, root,
                     runtime_check=None, config=None):
        return runtime_repair_models.RuntimeRepairResult(
            check_id=check_result.id, check_name=check_result.name,
            original_runtime_status=check_result.status, diagnosis_status=diagnosis.diagnosis_status,
            outcome=runtime_repair_models.OUTCOME_ERROR,
            eligibility=runtime_repair_models.EligibilityResult(eligible=True, reason="ok"),
            explanation="a genuine executor-level failure", error="RuntimeError: boom",
        )

    original = executor_module.repair_runtime_failure
    executor_module.repair_runtime_failure = fake_repair
    try:
        check_result = _result("build-verification", "fail", "build exited 1")
        state = QAState(runtime_plan=_plan("build-verification"), execution_results=(check_result,),
                         diagnoses=(_Diagnosis(check_id="build-verification"),))
        outcome = execute_action("runtime_repair", "build-verification", state, ".", None)
    finally:
        executor_module.repair_runtime_failure = original

    suite.check("OUTCOME_ERROR is the one outcome reported as STATUS_ERROR", outcome.status == STATUS_ERROR)
    suite.check("the result is still recorded, never dropped", len(outcome.new_state.repairs) == 1)


def test_execute_runtime_repair_rejected_is_still_status_ok_and_recorded(suite):
    """A REJECTED/HELD/etc. outcome is a complete, informative conclusion,
    not an executor failure - matches _execute_static_analysis's own
    "executor status is about the process, not the verdict" precedent.
    """
    from qa_agent.agent import executor as executor_module
    from qa_agent.ai import runtime_repair_models

    def fake_repair(check_result, diagnosis, execution_result, repository_context, provider, root,
                     runtime_check=None, config=None):
        return runtime_repair_models.RuntimeRepairResult(
            check_id=check_result.id, check_name=check_result.name,
            original_runtime_status=check_result.status, diagnosis_status=diagnosis.diagnosis_status,
            outcome=runtime_repair_models.OUTCOME_REJECTED,
            eligibility=runtime_repair_models.EligibilityResult(eligible=True, reason="ok"),
            explanation="a real static regression vetoed the candidate",
        )

    original = executor_module.repair_runtime_failure
    executor_module.repair_runtime_failure = fake_repair
    try:
        check_result = _result("build-verification", "fail", "build exited 1")
        state = QAState(runtime_plan=_plan("build-verification"), execution_results=(check_result,),
                         diagnoses=(_Diagnosis(check_id="build-verification"),))
        outcome = execute_action("runtime_repair", "build-verification", state, ".", None)
    finally:
        executor_module.repair_runtime_failure = original

    suite.check("a rejected repair is STATUS_OK - the executor completed its real job", outcome.status == STATUS_OK)
    suite.check("'rejected' is never rendered as if it were a success", "rejected" in outcome.summary)
    suite.check("the real result is still recorded", len(outcome.new_state.repairs) == 1)


def test_execute_runtime_repair_real_end_to_end_verified(suite):
    """The full real thing, no fakes anywhere: a real broken build.js, a
    real, actually-executed Build Verification check (so the diagnosis
    prompt's evidence is real captured subprocess output, not a hand-typed
    stand-in - required for the diagnosis to actually pass grounding), a
    real G3-shaped diagnosis, a real G4 repair proposal, a real
    temporary-workspace apply, a real candidate re-check, a real write to
    the real repository, and a real final re-verification.
    """
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({"scripts": {"build": "node build.js"}}))
        writer.write("build.js", BROKEN_BUILD_JS)

        from qa_agent.project import build_repository_context, discover_project
        from qa_agent.runtime import plan_runtime_qa

        discovery = discover_project(root)
        context = build_repository_context(discovery.project)
        plan = plan_runtime_qa(context)

        build_outcome = execute_action("build_verification", None, QAState(repository_context=context, runtime_plan=plan), root, None)
        suite.check("the real build actually failed, as scripted", build_outcome.new_state.execution_results[0].status == "fail")

        diagnosis_provider = MockProvider(response_text=_diagnosis_response())
        diagnosis_outcome = execute_action(
            "runtime_diagnosis", "build-verification", build_outcome.new_state, root, diagnosis_provider,
        )
        suite.check("the real diagnosis step succeeded first", diagnosis_outcome.status == STATUS_OK)

        repair_provider = MockProvider(response_text=_repair_response())
        outcome = execute_action("runtime_repair", "build-verification", diagnosis_outcome.new_state, root, repair_provider)
        patched_contents = Path(root, "build.js").read_text(encoding="utf-8")

    suite.check("the real end-to-end repair executed", outcome.status == STATUS_OK)
    suite.check("exactly one real RuntimeRepairResult is now in state", len(outcome.new_state.repairs) == 1)
    result = outcome.new_state.repairs[0]
    suite.check("it really reached VERIFIED - a real re-run of the real check actually passed",
                result.outcome == "verified" and result.verification_status == "verified")
    suite.check("the real file on disk was actually patched", "build ok" in patched_contents)


def test_execute_action_never_raises_on_a_genuine_crash(suite):
    def crashing_execute(target, state, root, provider, config):
        raise RuntimeError("boom")

    from qa_agent.agent import executor as executor_module
    original = executor_module._EXECUTORS.get("project_discovery")
    executor_module._EXECUTORS["project_discovery"] = crashing_execute
    try:
        outcome = execute_action("project_discovery", None, QAState(), ".", None)
    finally:
        executor_module._EXECUTORS["project_discovery"] = original
    suite.check("a genuine crash is caught, never raised", outcome.status == STATUS_ERROR)
    suite.check("the real exception message is preserved", "boom" in outcome.summary)


# --- adversarial / safety: nothing illegitimate is ever executed -----------

class _AdversarialProvider:
    """A provider that always tries to select something illegitimate -
    proves the loop's own end-to-end safety, not just the controller's own
    (already proven in test_agent_controller.py) in isolation.
    """

    name = "adversarial"

    def __init__(self, next_action, target=None):
        self._next_action = next_action
        self._target = target
        self.calls = 0

    def generate(self, prompt):
        self.calls += 1
        return LLMResponse(text=json.dumps({
            "decision": "continue", "next_action": self._next_action, "target": self._target,
            "reason": "adversarial", "evidence_needed": [],
        }), provider=self.name, model="adversarial-model")

    def test_connection(self):
        return ConnectionResult(ok=True)


def test_shell_command_never_executes(suite):
    with TempProject() as root:
        before = sorted(p.name for p in root.iterdir())
        provider = _AdversarialProvider("rm -rf .")
        result = run_agent_loop(QAState(), provider, root)
        after = sorted(p.name for p in root.iterdir())
    suite.check("nothing was ever executed", result.history == () or all(h.outcome == HISTORY_REJECTED for h in result.history))
    suite.check("the filesystem is completely unchanged", before == after)
    suite.check("the loop terminated safely (bounded), never hung", result.termination_reason == TERMINATION_MAX_ITERATIONS)


def test_arbitrary_python_command_never_executes(suite):
    provider = _AdversarialProvider("python -c \"import os; os.system('echo pwned')\"")
    result = run_agent_loop(QAState(max_iterations=2), provider, ".")
    suite.check("rejected every time, nothing executed", all(h.outcome == HISTORY_REJECTED for h in result.history))


def test_curl_command_never_executes(suite):
    provider = _AdversarialProvider("curl https://example.com/exfiltrate")
    result = run_agent_loop(QAState(max_iterations=2), provider, ".")
    suite.check("rejected every time, nothing executed", all(h.outcome == HISTORY_REJECTED for h in result.history))


def test_path_traversal_action_id_never_executes(suite):
    provider = _AdversarialProvider("../../../etc/passwd")
    result = run_agent_loop(QAState(max_iterations=2), provider, ".")
    suite.check("rejected every time, nothing executed", all(h.outcome == HISTORY_REJECTED for h in result.history))


def test_valid_action_invalid_target_never_executes(suite):
    """A structurally real action id, but a fabricated target - must still
    never reach the executor.
    """
    state = QAState(
        repository_context=_ctx(), runtime_plan=_plan("build-verification"),
        execution_results=(_result("build-verification", "fail"),),
    )
    provider = _AdversarialProvider("runtime_diagnosis", target="../../etc/passwd")
    result = run_agent_loop(state, provider, ".", config=None)
    suite.check("the invalid target was rejected, never executed", all(h.outcome == HISTORY_REJECTED for h in result.history))


def test_safety_executor_never_called_for_an_adversarial_selection(suite):
    """The explicit safety test this stage's own spec asks for: prove the
    executor itself is never invoked, not just that the end result looks
    safe.
    """
    calls = {"n": 0}

    def spy_execute(action_id, target, state, root, provider, config):
        calls["n"] += 1
        return _Outcome(status=STATUS_OK, summary="should never happen", new_state=state)

    decide_real = None  # use the real controller, so this is a genuine end-to-end proof
    provider = _AdversarialProvider("rm -rf .")
    result = run_agent_loop(QAState(max_iterations=3), provider, ".", execute=spy_execute)
    suite.check("execute() was never called", calls["n"] == 0)
    suite.check("every decision was rejected", all(h.outcome == HISTORY_REJECTED for h in result.history))


# --- MockProvider full multi-step loop integration (real execution) -------

class _SequencedMockProvider:
    """A real `AIProvider`-shaped object returning a fixed, scripted
    sequence of decisions - the exact mechanism this stage's own spec asks
    for ("the exact sequence should be controlled by the mock provider,
    not hard-coded into the loop").
    """

    name = "sequenced"

    def __init__(self, decisions):
        self._decisions = list(decisions)
        self.calls = 0

    def generate(self, prompt):
        payload = self._decisions[min(self.calls, len(self._decisions) - 1)]
        self.calls += 1
        return LLMResponse(text=json.dumps(payload), provider=self.name, model="sequenced-model")

    def test_connection(self):
        return ConnectionResult(ok=True)


def _build_fixture(root, script="console.log('ok'); process.exit(0);"):
    writer = _Writer(root)
    writer.write("package-lock.json", "{}")
    writer.write("package.json", json.dumps({"scripts": {
        "build": "node build.js", "test": "node -e \"process.exit(0)\"",
    }}))
    writer.write("build.js", script + "\n")
    # A real test directory is required for G1's own test_suite_checks rule
    # to plan a Test Suite Verification check at all (evidence-based
    # planning: package.json's own "test" script alone is not enough) -
    # matches this project's own existing fixture convention exactly
    # (test_runtime_diagnosis.py's own multi-failure fixture).
    writer.write("tests/x.test.js", "test('x', () => {})\n")


def _d(next_action, target=None, reason="x"):
    return {"decision": "continue", "next_action": next_action, "target": target, "reason": reason, "evidence_needed": []}


def _stop(reason="done"):
    return {"decision": "stop", "next_action": None, "target": None, "reason": reason, "evidence_needed": []}


def test_full_loop_build_then_stop(suite):
    with TempProject() as root:
        _build_fixture(root)
        provider = _SequencedMockProvider([
            _d("project_discovery"), _d("runtime_plan"), _d("build_verification"), _stop("build passed"),
        ])
        result = run_agent_loop(QAState(max_iterations=10), provider, root)
    suite.check("the exact scripted sequence drove real execution", [h.action_id for h in result.history] ==
                ["project_discovery", "runtime_plan", "build_verification"])
    suite.check("the build really passed", result.final_state.execution_results[0].status == "pass")
    suite.check("terminated by the AI's own stop", result.termination_reason == TERMINATION_AI_STOP)


def test_full_loop_build_test_then_stop(suite):
    with TempProject() as root:
        _build_fixture(root)
        provider = _SequencedMockProvider([
            _d("project_discovery"), _d("runtime_plan"), _d("build_verification"),
            _d("test_suite_verification"), _stop("both passed"),
        ])
        result = run_agent_loop(QAState(max_iterations=10), provider, root)
    suite.check("both checks actually ran, in the scripted order", [h.action_id for h in result.history[2:]] ==
                ["build_verification", "test_suite_verification"])
    suite.check("both real results are in final state",
                {r.id for r in result.final_state.execution_results} == {"build-verification", "test-suite-verification"})


def test_full_loop_server_startup_build_then_stop(suite):
    """A different, AI-chosen order than the previous test - proves the
    sequence genuinely comes from the provider, not a hard-coded loop.
    """
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({
            "scripts": {"build": "node build.js", "dev": "node server.js"},
        }))
        writer.write("build.js", "console.log('ok'); process.exit(0);\n")
        writer.write("server.js", "console.log('listening on 3000');\nsetInterval(() => {}, 1000);\n")

        provider = _SequencedMockProvider([
            _d("project_discovery"), _d("runtime_plan"), _d("server_startup"), _d("build_verification"),
            _stop("both checks are healthy"),
        ])
        result = run_agent_loop(QAState(max_iterations=10), provider, root)
    suite.check("server_startup ran before build_verification, exactly as scripted",
                [h.action_id for h in result.history if h.outcome == HISTORY_EXECUTED][2:4] ==
                ["server_startup", "build_verification"])


def test_repeated_successful_action_is_rejected_not_reexecuted(suite):
    """The AI (or a broken script) tries build_verification a second time
    after it already ran - G5.1's own eligibility rule must still hold
    inside the real G5.2 loop, not just in isolation.
    """
    with TempProject() as root:
        _build_fixture(root)
        provider = _SequencedMockProvider([
            _d("project_discovery"), _d("runtime_plan"), _d("build_verification"),
            _d("build_verification"),  # illegitimate repeat
            _stop("done"),
        ])
        result = run_agent_loop(QAState(max_iterations=10), provider, root)
    executed_build_count = sum(1 for h in result.history if h.action_id == "build_verification" and h.outcome == HISTORY_EXECUTED)
    suite.check("build_verification really executed only once", executed_build_count == 1)
    suite.check("the repeat attempt was rejected, not re-run", any(h.outcome == HISTORY_REJECTED for h in result.history))


def test_full_loop_diagnosis_then_repair_then_stop_real_end_to_end(suite):
    """The complete G5.3 story, driven entirely by a real, scripted
    sequence through the real loop - discovery -> plan -> a real failing
    build -> diagnosis -> repair -> stop - with every AI-facing step
    (action selection *and* the diagnosis/repair content itself) coming
    from the one scripted provider, in the exact call order the real code
    actually makes them.
    """
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({"scripts": {"build": "node build.js"}}))
        writer.write("build.js", BROKEN_BUILD_JS)

        provider = _SequencedMockProvider([
            _d("project_discovery"), _d("runtime_plan"), _d("build_verification"),
            _d("runtime_diagnosis", target="build-verification"),
            json.loads(_diagnosis_response()),
            _d("runtime_repair", target="build-verification"),
            json.loads(_repair_response()),
            _stop("repaired and verified"),
        ])
        result = run_agent_loop(QAState(max_iterations=10), provider, root)
        patched_contents = Path(root, "build.js").read_text(encoding="utf-8")

    suite.check("the exact scripted sequence drove real execution", [h.action_id for h in result.history] ==
                ["project_discovery", "runtime_plan", "build_verification", "runtime_diagnosis", "runtime_repair"])
    suite.check("a real diagnosis is in final state", len(result.final_state.diagnoses) == 1)
    suite.check("a real, verified repair is in final state",
                len(result.final_state.repairs) == 1 and result.final_state.repairs[0].outcome == "verified")
    suite.check("terminated by the AI's own stop", result.termination_reason == TERMINATION_AI_STOP)
    suite.check("the real file on disk was actually patched", "build ok" in patched_contents)


def test_repeated_repair_attempt_on_the_same_target_is_rejected_not_reattempted(suite):
    """The one-shot rule (docs/34's own "repeated failure" stop condition)
    holds inside the real loop, not just in a unit test of eligibility -
    mirrors test_repeated_successful_action_is_rejected_not_reexecuted's
    own style exactly, one action later in the pipeline.
    """
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package-lock.json", "{}")
        writer.write("package.json", json.dumps({"scripts": {"build": "node build.js"}}))
        writer.write("build.js", BROKEN_BUILD_JS)

        provider = _SequencedMockProvider([
            _d("project_discovery"), _d("runtime_plan"), _d("build_verification"),
            _d("runtime_diagnosis", target="build-verification"),
            json.loads(_diagnosis_response()),
            _d("runtime_repair", target="build-verification"),
            json.loads(_repair_response()),
            _d("runtime_repair", target="build-verification"),  # illegitimate repeat, same target
            _stop("done"),
        ])
        result = run_agent_loop(QAState(max_iterations=10), provider, root)

    executed_repair_count = sum(
        1 for h in result.history if h.action_id == "runtime_repair" and h.outcome == HISTORY_EXECUTED)
    suite.check("runtime_repair really executed only once for this target", executed_repair_count == 1)
    suite.check("the repeat attempt was rejected, not re-run", any(h.outcome == HISTORY_REJECTED for h in result.history))
    suite.check("still exactly one repair result in final state", len(result.final_state.repairs) == 1)


# --- CLI ---------------------------------------------------------------

def test_cli_agent_subcommand_exists_and_runs_cleanly(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("main.py", "print('hi')\n")
        proc = run_agent(["agent", str(root), "--ai-provider", "mock", "--max-iterations", "3"])
    suite.check("the agent subcommand exists and runs", "QA Agent Session" in proc.stdout)
    suite.check("no traceback leaks to the user", "Traceback" not in proc.stdout)
    suite.check("a malformed mock response safely terminates with a controller error",
                "controller_error" in proc.stdout and proc.returncode == 2)


def test_cli_agent_never_needs_a_live_ollama_or_openrouter_server(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("main.py", "print('hi')\n")
        proc = run_agent(["agent", str(root), "--ai-provider", "mock", "--objective", "Quick check.", "--max-iterations", "2"])
    suite.check("exits cleanly with only MockProvider - no live server needed", proc.returncode in (0, 2))
    suite.check("the real, custom objective is echoed", "Quick check." in proc.stdout)


if __name__ == "__main__":
    suite = Suite("G5.2: Autonomous QA Execution Loop")
    sys.exit(suite.run([
        test_stop_decision_terminates_with_ai_stop,
        test_max_iterations_terminates_deterministically,
        test_no_eligible_actions_terminates_deterministically,
        test_provider_failure_terminates_with_controller_error,
        test_malformed_decision_terminates_with_controller_error,
        test_invalid_action_is_rejected_and_the_loop_continues,
        test_invalid_target_is_rejected_and_the_loop_continues,
        test_repeated_rejections_are_still_bounded_by_max_iterations,
        test_failed_qa_action_does_not_terminate_the_loop,
        test_executor_level_error_does_not_terminate_the_loop_either,
        test_state_update_reaches_the_next_ai_decision,
        test_history_captures_the_required_fields,
        test_execute_action_unknown_id_is_not_executable,
        test_execute_project_discovery_real,
        test_execute_runtime_plan_requires_discovery_first,
        test_execute_build_verification_real_pass,
        test_execute_runtime_diagnosis_real_with_mock_provider,
        test_execute_runtime_repair_requires_a_target,
        test_execute_runtime_repair_target_with_no_matching_evidence_in_state,
        test_execute_runtime_repair_wires_the_real_g4_call_signature,
        test_execute_runtime_repair_outcome_error_is_status_error,
        test_execute_runtime_repair_rejected_is_still_status_ok_and_recorded,
        test_execute_runtime_repair_real_end_to_end_verified,
        test_execute_action_never_raises_on_a_genuine_crash,
        test_shell_command_never_executes,
        test_arbitrary_python_command_never_executes,
        test_curl_command_never_executes,
        test_path_traversal_action_id_never_executes,
        test_valid_action_invalid_target_never_executes,
        test_safety_executor_never_called_for_an_adversarial_selection,
        test_full_loop_build_then_stop,
        test_full_loop_build_test_then_stop,
        test_full_loop_server_startup_build_then_stop,
        test_repeated_successful_action_is_rejected_not_reexecuted,
        test_full_loop_diagnosis_then_repair_then_stop_real_end_to_end,
        test_repeated_repair_attempt_on_the_same_target_is_rejected_not_reattempted,
        test_cli_agent_subcommand_exists_and_runs_cleanly,
        test_cli_agent_never_needs_a_live_ollama_or_openrouter_server,
    ]))
