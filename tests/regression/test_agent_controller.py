"""G5.1: Autonomous QA Action Selection Controller
(docs/28-g5-action-selection-controller.md) - `qa_agent.agent.select_next_action`,
its deterministic action registry/eligibility, its parser, and its prompt
builder.

Pure unit tests throughout: `MockProvider` drives every scenario, no live
Ollama/OpenRouter call anywhere in this file, matching this project's own
established convention. Fixtures are hand-built, duck-typed stand-ins for
real `RepositoryContext`/`RuntimeQAPlan`/`RuntimeCheckResult`/
`RuntimeDiagnosis` objects - this package itself never imports those
concrete types (see qa_agent/agent/models.py's own docstring), so the
tests exercise it exactly the way it is actually used: real shape, no
concrete-class dependency.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite  # noqa: E402

from qa_agent.agent import (  # noqa: E402
    ACTION_REGISTRY,
    ControllerDecision,
    DECISION_CONTINUE,
    DECISION_ERROR,
    DECISION_STOP,
    QAState,
    eligible_actions,
    get_action,
    select_next_action,
    validate_decision_response,
)
from qa_agent.ai import ConnectionResult, LLMResponse, MockProvider, OllamaProvider, OpenRouterProvider  # noqa: E402


# --- duck-typed fixtures - real shapes, no concrete-class dependency -------

@dataclass(frozen=True)
class _Item:
    name: str
    evidence: tuple = ()


@dataclass(frozen=True)
class _Project:
    repository_type: str = "single-package"
    application_type: str = "backend"
    languages: tuple = ()
    frameworks: tuple = ()


@dataclass(frozen=True)
class _RepositoryContext:
    project: object


@dataclass(frozen=True)
class _RuntimeCheck:
    id: str
    name: str = ""


@dataclass(frozen=True)
class _RuntimeQAPlan:
    checks: Tuple[object, ...]


@dataclass(frozen=True)
class _CheckResult:
    id: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class _Diagnosis:
    check_id: str
    diagnosis_status: str = "diagnosed"
    summary: str = "a summary"
    error: object = None


@dataclass(frozen=True)
class _Repair:
    check_id: str
    outcome: str = "rejected"
    verification_status: str = "not_applicable"
    explanation: str = "a repair result"


def _context():
    return _RepositoryContext(project=_Project())


def _plan(*check_ids):
    return _RuntimeQAPlan(checks=tuple(_RuntimeCheck(id=cid) for cid in check_ids))


def _discovered_state(**overrides):
    kwargs = dict(repository_context=_context())
    kwargs.update(overrides)
    return QAState(**kwargs)


def _planned_state(*check_ids, **overrides):
    kwargs = dict(repository_context=_context(), runtime_plan=_plan(*check_ids))
    kwargs.update(overrides)
    return QAState(**kwargs)


def _continue_json(next_action, reason="a reason", evidence=None):
    return json.dumps({
        "decision": "continue", "next_action": next_action, "reason": reason,
        "evidence_needed": evidence or [],
    })


def _stop_json(reason="nothing left to do"):
    return json.dumps({"decision": "stop", "next_action": None, "reason": reason, "evidence_needed": []})


# --- action registry: deterministic data, never AI-determined --------------

def test_registry_has_ten_actions_all_implemented(suite):
    suite.check("ten actions were investigated", len(ACTION_REGISTRY) == 10)
    implemented = [a for a in ACTION_REGISTRY if a.implemented]
    suite.check("all ten are implemented as of G5.3", len(implemented) == 10)
    suite.check("runtime_repair exists and requires a target (G5.3)",
                get_action("runtime_repair") is not None and get_action("runtime_repair").requires_target)


def test_get_action_returns_none_for_an_unknown_id(suite):
    suite.check("an unknown id returns None, never raises", get_action("not_a_real_action") is None)


def test_registry_ids_are_unique(suite):
    ids = [a.id for a in ACTION_REGISTRY]
    suite.check("no duplicate action ids", len(ids) == len(set(ids)))


# --- eligibility: pure, deterministic function of state ---------------------

def test_empty_state_offers_only_project_discovery(suite):
    state = QAState()
    ids = [a.id for a in eligible_actions(state)]
    suite.check("only project_discovery is eligible with nothing known yet", ids == ["project_discovery"])


def test_after_discovery_runtime_plan_and_static_analysis_become_eligible(suite):
    state = _discovered_state()
    ids = {a.id for a in eligible_actions(state)}
    suite.check("runtime_plan is now eligible", "runtime_plan" in ids)
    suite.check("static_analysis is now eligible", "static_analysis" in ids)
    suite.check("project_discovery is no longer offered (already completed)", "project_discovery" not in ids)


def test_runtime_check_actions_require_being_in_the_actual_plan(suite):
    state = _planned_state("build-verification")  # only one check actually planned
    ids = {a.id for a in eligible_actions(state)}
    suite.check("build_verification is eligible - it was actually planned", "build_verification" in ids)
    suite.check("server_startup is NOT eligible - never planned for this repository", "server_startup" not in ids)
    suite.check("test_suite_verification is NOT eligible either", "test_suite_verification" not in ids)


def test_runtime_check_already_executed_is_not_offered_again(suite):
    state = _planned_state(
        "build-verification",
        execution_results=(_CheckResult(id="build-verification", status="pass"),),
    )
    ids = {a.id for a in eligible_actions(state)}
    suite.check("a check that already ran is not offered again", "build_verification" not in ids)


def test_runtime_diagnosis_eligible_only_when_a_real_undiagnosed_failure_exists(suite):
    state_no_failure = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="pass"),),
    )
    suite.check("no failure -> runtime_diagnosis not eligible",
                "runtime_diagnosis" not in {a.id for a in eligible_actions(state_no_failure)})

    state_with_failure = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="fail"),),
    )
    suite.check("a real, undiagnosed failure -> runtime_diagnosis is eligible",
                "runtime_diagnosis" in {a.id for a in eligible_actions(state_with_failure)})

    state_already_diagnosed = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="fail"),),
        diagnoses=(_Diagnosis(check_id="build-verification"),),
    )
    suite.check("that same failure, already diagnosed -> no longer eligible",
                "runtime_diagnosis" not in {a.id for a in eligible_actions(state_already_diagnosed)})


def test_runtime_repair_eligible_only_with_a_real_diagnosed_unrepaired_failure(suite):
    """G5.3: the mirror image of the old G5.1 "never eligible" guarantee -
    now eligible exactly when a real, DIAGNOSED, not-yet-repaired failure
    exists, and no longer eligible the moment that check id gets any
    repair result at all (see `test_agent_loop.py` for the one-shot proof
    inside the real loop).
    """
    state_no_diagnosis = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="fail"),),
    )
    suite.check("no diagnosis at all -> runtime_repair not eligible",
                "runtime_repair" not in {a.id for a in eligible_actions(state_no_diagnosis)})

    state_insufficient_context = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="fail"),),
        diagnoses=(_Diagnosis(check_id="build-verification", diagnosis_status="insufficient_context"),),
    )
    suite.check("a non-DIAGNOSED diagnosis status -> runtime_repair still not eligible",
                "runtime_repair" not in {a.id for a in eligible_actions(state_insufficient_context)})

    state_diagnosed = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="fail"),),
        diagnoses=(_Diagnosis(check_id="build-verification", diagnosis_status="diagnosed"),),
    )
    suite.check("a real, DIAGNOSED, unrepaired failure -> runtime_repair is eligible",
                "runtime_repair" in {a.id for a in eligible_actions(state_diagnosed)})

    state_already_repaired = replace(state_diagnosed, repairs=(_Repair(check_id="build-verification", outcome="rejected"),))
    suite.check("that same failure, already given a repair attempt (even a rejected one) -> no longer eligible",
                "runtime_repair" not in {a.id for a in eligible_actions(state_already_repaired)})


# --- the parser: structural validation only ---------------------------------

def test_parser_accepts_a_valid_continue_response(suite):
    result = validate_decision_response(_continue_json("build_verification"))
    suite.check("a well-formed continue response validates", result.ok)


def test_parser_accepts_a_valid_stop_response(suite):
    result = validate_decision_response(_stop_json())
    suite.check("a well-formed stop response validates", result.ok)


def test_parser_rejects_an_unknown_decision(suite):
    raw = json.dumps({"decision": "maybe", "next_action": None, "reason": "x", "evidence_needed": []})
    suite.check("an unknown decision value is rejected", not validate_decision_response(raw).ok)


def test_parser_rejects_decision_error_from_the_model_itself(suite):
    raw = json.dumps({"decision": "error", "next_action": None, "reason": "x", "evidence_needed": []})
    suite.check("'error' is never something the model is allowed to claim", not validate_decision_response(raw).ok)


def test_parser_rejects_malformed_json(suite):
    suite.check("malformed JSON is rejected, not silently accepted", not validate_decision_response("not json").ok)


def test_parser_rejects_missing_next_action_for_continue(suite):
    raw = json.dumps({"decision": "continue", "reason": "x", "evidence_needed": []})
    suite.check("a missing next_action for continue is rejected", not validate_decision_response(raw).ok)


def test_parser_rejects_next_action_supplied_for_stop(suite):
    raw = json.dumps({"decision": "stop", "next_action": "build_verification", "reason": "x", "evidence_needed": []})
    suite.check("a non-null next_action for stop is rejected", not validate_decision_response(raw).ok)


def test_parser_rejects_empty_action_id(suite):
    raw = json.dumps({"decision": "continue", "next_action": "", "reason": "x", "evidence_needed": []})
    suite.check("an empty-string action id is rejected", not validate_decision_response(raw).ok)


def test_parser_rejects_multiple_actions(suite):
    raw = json.dumps({
        "decision": "continue", "next_action": ["build_verification", "static_analysis"],
        "reason": "x", "evidence_needed": [],
    })
    suite.check("a list of actions (structurally 'multiple actions') is rejected",
                not validate_decision_response(raw).ok)


def test_parser_rejects_missing_reason(suite):
    raw = json.dumps({"decision": "stop", "next_action": None, "evidence_needed": []})
    suite.check("a missing 'reason' is rejected", not validate_decision_response(raw).ok)


def test_parser_rejects_non_list_evidence_needed(suite):
    raw = json.dumps({"decision": "stop", "next_action": None, "reason": "x", "evidence_needed": "not a list"})
    suite.check("a non-list 'evidence_needed' is rejected", not validate_decision_response(raw).ok)


# --- the controller: deterministic allowlist cannot be bypassed ------------

def test_valid_continue_decision_end_to_end(suite):
    state = _discovered_state()
    provider = MockProvider(response_text=_continue_json("runtime_plan"))
    decision = select_next_action(state, provider)
    suite.check("decision is 'continue'", decision.decision == DECISION_CONTINUE)
    suite.check("the selected action is the one the model named", decision.next_action == "runtime_plan")
    suite.check("eligible_actions on the result matches what was actually offered",
                set(decision.eligible_actions) == {a.id for a in eligible_actions(state)})


def test_valid_stop_decision_end_to_end(suite):
    state = _discovered_state()
    provider = MockProvider(response_text=_stop_json("nothing useful remains"))
    decision = select_next_action(state, provider)
    suite.check("decision is 'stop'", decision.decision == DECISION_STOP)
    suite.check("next_action is None", decision.next_action is None)


def test_unknown_action_is_rejected(suite):
    state = _discovered_state()
    provider = MockProvider(response_text=_continue_json("totally_made_up_action"))
    decision = select_next_action(state, provider)
    suite.check("outcome is error", decision.decision == DECISION_ERROR)
    suite.check("reason names the real problem", "not currently eligible" in decision.reason)


def test_unavailable_but_real_action_is_rejected(suite):
    """A real registry id, but not eligible in this specific state - the
    exact scenario the task's own spec calls out by name.
    """
    state = QAState()  # nothing discovered yet
    provider = MockProvider(response_text=_continue_json("build_verification"))
    decision = select_next_action(state, provider)
    suite.check("outcome is error, not silently allowed", decision.decision == DECISION_ERROR)


def test_dependency_unsatisfied_action_is_rejected(suite):
    state = QAState()  # project_discovery has not completed
    provider = MockProvider(response_text=_continue_json("runtime_plan"))
    decision = select_next_action(state, provider)
    suite.check("runtime_plan cannot be selected before its dependency completes", decision.decision == DECISION_ERROR)


def test_malformed_json_from_provider_is_rejected_safely(suite):
    state = _discovered_state()
    provider = MockProvider(response_text="not json at all")
    decision = select_next_action(state, provider)
    suite.check("outcome is error, never raised", decision.decision == DECISION_ERROR)
    suite.check("no next_action is ever set on an error", decision.next_action is None)


def test_invalid_schema_is_rejected_safely(suite):
    state = _discovered_state()
    provider = MockProvider(response_text=json.dumps({"decision": "continue"}))  # missing everything else
    decision = select_next_action(state, provider)
    suite.check("outcome is error", decision.decision == DECISION_ERROR)


def test_provider_failure_is_handled_gracefully(suite):
    state = _discovered_state()
    provider = MockProvider(fail=True, failure_message="offline")
    decision = select_next_action(state, provider)
    suite.check("outcome is error, never raised", decision.decision == DECISION_ERROR)
    suite.check("the real provider error is preserved", decision.error == "offline")


def test_deterministic_allowlist_cannot_be_bypassed_by_naming_a_real_action_from_a_different_state(suite):
    """The allowlist offered is always recomputed from the *current* state,
    never trusted from an earlier call - proven by using a state where the
    action is genuinely ineligible even though it is a perfectly real id.
    """
    state = _discovered_state()  # runtime_plan not yet built
    provider = MockProvider(response_text=_continue_json("build_verification"))
    decision = select_next_action(state, provider)
    suite.check("a real action id, ineligible for this exact state, is still rejected",
                decision.decision == DECISION_ERROR
                and "build_verification" not in decision.eligible_actions)


def test_completed_action_is_handled_correctly(suite):
    state = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="pass"),),
    )
    provider = MockProvider(response_text=_continue_json("build_verification"))
    decision = select_next_action(state, provider)
    suite.check("a completed action can no longer be selected", decision.decision == DECISION_ERROR)


def test_zero_remaining_budget_stops_deterministically_without_an_ai_call(suite):
    calls = {"n": 0}

    class _CountingProvider:
        name = "counting"

        def generate(self, prompt):
            calls["n"] += 1
            return LLMResponse(text=_continue_json("project_discovery"), provider=self.name, model="m")

        def test_connection(self):
            return ConnectionResult(ok=True)

    state = QAState(iteration=5, max_iterations=5)
    decision = select_next_action(state, _CountingProvider())
    suite.check("decision is stop", decision.decision == DECISION_STOP)
    suite.check("budget exhaustion is named in the reason", "budget exhausted" in decision.reason)
    suite.check("zero AI calls were made - the controller decided this on its own", calls["n"] == 0)


def test_no_eligible_actions_stops_deterministically_without_an_ai_call(suite):
    calls = {"n": 0}

    class _CountingProvider:
        name = "counting"

        def generate(self, prompt):
            calls["n"] += 1
            return LLMResponse(text=_stop_json(), provider=self.name, model="m")

        def test_connection(self):
            return ConnectionResult(ok=True)

    # A state where every implemented action is already completed and
    # nothing failed (so runtime_diagnosis is not eligible either).
    state = _planned_state(
        "build-verification",
        static_analysis_result=object(),
        execution_results=(_CheckResult(id="build-verification", status="pass"),),
    )
    suite.check("sanity: eligible_actions is genuinely empty for this state", eligible_actions(state) == ())
    decision = select_next_action(state, _CountingProvider())
    suite.check("decision is stop", decision.decision == DECISION_STOP)
    suite.check("zero AI calls were made", calls["n"] == 0)


# --- adversarial: misleading AI outputs -------------------------------------

def test_arbitrary_shell_command_is_rejected(suite):
    state = _discovered_state()
    provider = MockProvider(response_text=_continue_json("rm -rf ."))
    decision = select_next_action(state, provider)
    suite.check("a shell command is never treated as a valid action", decision.decision == DECISION_ERROR)


def test_arbitrary_file_modification_request_is_rejected(suite):
    state = _discovered_state()
    provider = MockProvider(response_text=_continue_json("edit_file"))
    decision = select_next_action(state, provider)
    suite.check("a file-edit-shaped action id is never valid", decision.decision == DECISION_ERROR)


def test_run_arbitrary_command_action_name_is_rejected(suite):
    state = _discovered_state()
    provider = MockProvider(response_text=_continue_json("run_arbitrary_command"))
    decision = select_next_action(state, provider)
    suite.check("a generic 'run arbitrary command'-shaped id is never valid", decision.decision == DECISION_ERROR)


def test_a_real_but_currently_unavailable_registry_action_is_rejected(suite):
    """Distinct from the totally-invented cases above: this uses a genuine
    registry id that simply is not eligible right now.
    """
    state = QAState()
    provider = MockProvider(response_text=_continue_json("static_analysis"))
    decision = select_next_action(state, provider)
    suite.check("a real-but-currently-unavailable action is rejected", decision.decision == DECISION_ERROR)


def test_runtime_repair_requires_a_valid_target_even_in_a_favorable_state(suite):
    """G5.3: `runtime_repair` is a real, selectable action now - but only
    with a real, currently-valid target, the same two-tier rule
    `runtime_diagnosis` already established in G5.2.
    """
    state = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="fail"),),
        diagnoses=(_Diagnosis(check_id="build-verification"),),
    )
    no_target = select_next_action(state, MockProvider(response_text=_continue_json("runtime_repair")))
    suite.check("no target supplied -> rejected", no_target.decision == DECISION_ERROR)

    wrong_target = select_next_action(state, MockProvider(response_text=json.dumps({
        "decision": "continue", "next_action": "runtime_repair", "target": "not-a-real-check-id",
        "reason": "x", "evidence_needed": [],
    })))
    suite.check("a fabricated target -> rejected", wrong_target.decision == DECISION_ERROR)

    valid_target = select_next_action(state, MockProvider(response_text=json.dumps({
        "decision": "continue", "next_action": "runtime_repair", "target": "build-verification",
        "reason": "x", "evidence_needed": [],
    })))
    suite.check("the real, currently-valid target -> accepted", valid_target.decision == DECISION_CONTINUE)
    suite.check("the accepted decision carries the real target", valid_target.target == "build-verification")


# --- deterministic authority: AI output cannot change controller state -----

def test_ai_output_cannot_change_action_availability(suite):
    state = QAState()  # only project_discovery eligible
    before = {a.id for a in eligible_actions(state)}
    provider = MockProvider(response_text=_continue_json("build_verification"))
    select_next_action(state, provider)
    after = {a.id for a in eligible_actions(state)}
    suite.check("eligibility is unchanged by an AI call - it is a pure function of state alone", before == after)


def test_ai_output_cannot_change_previous_execution_results(suite):
    result = _CheckResult(id="build-verification", status="fail")
    state = _planned_state("build-verification", execution_results=(result,))
    provider = MockProvider(response_text=_continue_json("runtime_diagnosis"))
    select_next_action(state, provider)
    suite.check("the real execution result object is untouched",
                state.execution_results[0].status == "fail" and state.execution_results[0] is result)


def test_ai_output_cannot_mutate_registry_metadata(suite):
    """The general guarantee `test_ai_output_cannot_change_action_availability`
    already proves for eligibility, one level deeper: no AI call, however
    it responds, ever mutates a real `ActionDefinition`'s own fixed fields.
    """
    before = replace(get_action("runtime_repair"))
    state = _planned_state(
        "build-verification", execution_results=(_CheckResult(id="build-verification", status="fail"),),
        diagnoses=(_Diagnosis(check_id="build-verification"),),
    )
    provider = MockProvider(response_text=json.dumps({
        "decision": "continue", "next_action": "runtime_repair", "target": "build-verification",
        "reason": "x", "evidence_needed": [],
    }))
    select_next_action(state, provider)
    after = get_action("runtime_repair")
    suite.check("runtime_repair's own registry metadata is untouched by a real AI call",
                after.implemented == before.implemented and after.requires_target == before.requires_target
                and after.requires == before.requires and after.safety_level == before.safety_level)


# --- provider compatibility (no live network call in any of these) ---------

def test_mockprovider_integration(suite):
    state = _discovered_state()
    decision = select_next_action(state, MockProvider(response_text=_continue_json("runtime_plan")))
    suite.check("MockProvider works end to end", decision.decision == DECISION_CONTINUE)


def test_ollamaprovider_shape_compatible_without_a_live_call(suite):
    """Proves the controller only ever depends on the AIProvider *shape*
    (`.generate(prompt) -> LLMResponse`) - not a live server. A fake
    replacing OllamaProvider's own `generate` in place is enough; no
    network call is made anywhere in this test.
    """
    provider = OllamaProvider(endpoint="http://localhost:11434", model="llama3", timeout=1)
    provider.generate = lambda prompt: LLMResponse(
        text=_continue_json("runtime_plan"), provider="ollama", model="llama3",
    )
    state = _discovered_state()
    decision = select_next_action(state, provider)
    suite.check("an OllamaProvider instance (transport replaced) works identically", decision.decision == DECISION_CONTINUE)


def test_openrouterprovider_shape_compatible_without_a_live_call(suite):
    provider = OpenRouterProvider(api_key="fake-key-for-this-test")
    provider.generate = lambda prompt: LLMResponse(
        text=_stop_json(), provider="openrouter", model="poolside/laguna-s-2.1:free",
    )
    state = _discovered_state()
    decision = select_next_action(state, provider)
    suite.check("an OpenRouterProvider instance (transport replaced) works identically", decision.decision == DECISION_STOP)


# --- bounded prompt/state -----------------------------------------------

def test_prompt_is_bounded_even_with_a_large_state(suite):
    from qa_agent.agent.prompts import build_action_selection_prompt

    many_results = tuple(_CheckResult(id="check-{}".format(i), status="pass", reason="ok") for i in range(50))
    state = _planned_state("build-verification", execution_results=many_results)
    prompt = build_action_selection_prompt(state, eligible_actions(state))
    suite.check("the prompt stays a reasonable size even with 50 accumulated results", len(prompt.user) < 8000)


def test_prompt_never_includes_raw_logs(suite):
    """RuntimeCheckResult.logs is deliberately never read anywhere in
    prompts.py - only the already-short .status/.reason fields are.
    """
    from qa_agent.agent import prompts as agent_prompts
    source = Path(agent_prompts.__file__).read_text(encoding="utf-8")
    suite.check("prompts.py never reads .logs", ".logs" not in source)


def test_prompt_states_the_qa_objective(suite):
    from qa_agent.agent.prompts import build_action_selection_prompt

    state = _discovered_state(objective="Ship a green build.")
    text = build_action_selection_prompt(state, eligible_actions(state)).user
    suite.check("the real, custom objective appears in the prompt", "Ship a green build." in text)


# --- isolation: this package never executes anything, and is never
# imported by the deterministic engines it reasons about -------------------

# G5.1's own five files - these, and only these, must stay completely
# execution-free (see their own module docstrings). `executor.py`/`loop.py`
# (G5.2, docs/29) are the one, deliberate, narrow crossing point where an
# already-validated decision becomes a real call into `qa_agent.runtime`/
# `qa_agent.project`/`qa_agent.runner`/`qa_agent.ai`'s execution functions -
# rescoping this test to name them explicitly, rather than checking the
# whole directory, is the same fix this project has already made twice
# before for the identical reason (Phase G Part 2's own subprocess-isolation
# test when `executor.py` first joined `qa_agent/runtime/`; Phase G Part 3's
# own RepositoryContext-isolation test when `diagnosis.py` first joined
# `qa_agent/ai/`) - a legitimate new file extending what the directory does,
# not a violation of what the *existing* files are still required not to do.
# `__init__.py` is included: it only ever imports its own sibling
# submodules (`.actions`, `.controller`, `.executor`, `.loop`, ...) to
# re-export their names - it contains no direct reference to
# `qa_agent.runtime`/`qa_agent.project`/an execution function itself.
_G51_ONLY_FILES = ("__init__.py", "models.py", "actions.py", "parser.py", "prompts.py", "controller.py")


def test_agent_package_never_imports_execution_functions(suite):
    """The one architectural rule G5.1 itself exists to satisfy: it
    represents, it never executes. Checked directly against the real
    source - specifically, that no execution function is ever *called*
    (`name(` in real code) or imported. Docstrings/descriptions naming
    these functions in plain prose (e.g. "maps to `discover_project`",
    exactly what actions.py's own registry descriptions legitimately do
    to document where each action plugs in) are not a violation and are
    deliberately not flagged - only an actual call or import is.
    """
    import re

    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "agent"
    docstring_re = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')
    forbidden_calls = (
        "discover_project(", "plan_runtime_qa(", "run_runtime_plan(", "runner.run(",
        "diagnose_runtime_failure(", "repair_runtime_failure(",
        "subprocess.run(", "subprocess.Popen(", "subprocess.call(", "os.system(",
    )
    import_re = re.compile(r"^\s*(from|import)\s+\S*(runner|subprocess)\b", re.MULTILINE)
    offending = []
    for name in _G51_ONLY_FILES:
        path = package_dir / name
        # Docstrings are stripped first - they legitimately document, in
        # prose, exactly which real function each action *maps to* (see
        # actions.py's own registry descriptions); only real code (import
        # statements, actual call syntax) outside a docstring counts here.
        code_only = docstring_re.sub("", path.read_text(encoding="utf-8"))
        for forbidden in forbidden_calls:
            if forbidden in code_only:
                offending.append("{}: {}".format(path.name, forbidden))
        if import_re.search(code_only):
            offending.append("{}: imports runner/subprocess".format(path.name))
    suite.check("none of G5.1's own five files call or import an execution function/subprocess",
                offending == [], " ({})".format(offending))


def test_agent_package_never_imports_runtime_or_project_packages(suite):
    """Real import statements only - a docstring sentence like "a real
    `RepositoryContext` (`qa_agent.project`)" documents the duck-typed
    shape a field expects (exactly this module's own stated convention,
    see models.py's docstring) and is not itself a dependency. Scoped to
    G5.1's own five files - see `_G51_ONLY_FILES`'s own comment above for
    why `executor.py`/`loop.py` (G5.2) are correctly excluded here.
    """
    import re

    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "agent"
    import_re = re.compile(
        r"^\s*(from\s+(qa_agent\.(runtime|project)|\.\.(runtime|project))\b|import\s+qa_agent\.(runtime|project)\b)",
        re.MULTILINE,
    )
    offending = []
    for name in _G51_ONLY_FILES:
        path = package_dir / name
        if import_re.search(path.read_text(encoding="utf-8")):
            offending.append(path.name)
    suite.check("none of G5.1's own five files actually import qa_agent.runtime or qa_agent.project",
                offending == [], " ({})".format(offending))


def test_g52_executor_is_the_only_place_that_crosses_the_execution_boundary(suite):
    """The complement of the two tests above - proves the boundary is real
    on *both* sides: `executor.py` genuinely does what G5.1's own files are
    forbidden from doing (this is expected and correct, not a leak), and
    `loop.py` itself never reaches around `executor.py` to call an
    execution function directly.
    """
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "agent"
    executor_text = (package_dir / "executor.py").read_text(encoding="utf-8")
    loop_text = (package_dir / "loop.py").read_text(encoding="utf-8")
    suite.check("executor.py really does call the real G1-G4 functions (that is its entire purpose)",
                "discover_project(" in executor_text and "run_runtime_plan(" in executor_text
                and "diagnose_runtime_failure(" in executor_text)
    suite.check("loop.py itself never calls an execution function directly - only through executor.execute_action",
                "discover_project(" not in loop_text and "run_runtime_plan(" not in loop_text
                and "runner.run(" not in loop_text and "diagnose_runtime_failure(" not in loop_text)


def test_deterministic_engines_never_import_the_agent_package(suite):
    repo_root = Path(__file__).resolve().parent.parent.parent
    pipeline_dirs = [repo_root / "qa_agent" / "runtime", repo_root / "qa_agent" / "project"]
    pipeline_files = [
        repo_root / "qa_agent" / "runner.py", repo_root / "qa_agent" / "adapters.py",
        repo_root / "qa_agent" / "report.py", repo_root / "qa_agent" / "config.py",
    ]
    offending = []
    for directory in pipeline_dirs:
        for path in directory.glob("*.py"):
            if "qa_agent.agent" in path.read_text(encoding="utf-8") or "from ..agent" in path.read_text(encoding="utf-8"):
                offending.append(str(path))
    for path in pipeline_files:
        text = path.read_text(encoding="utf-8")
        if "qa_agent.agent" in text or "from .agent" in text:
            offending.append(str(path))
    suite.check("no deterministic module imports qa_agent.agent", offending == [], " ({})".format(offending))


def test_agent_package_makes_no_subprocess_or_file_write_calls(suite):
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "agent"
    offending = []
    for path in package_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".write_text(" in text or ".write_bytes(" in text or "open(" in text:
            offending.append(path.name)
    suite.check("no file in qa_agent/agent/ ever writes a file", offending == [], " ({})".format(offending))


if __name__ == "__main__":
    suite = Suite("G5.1: Autonomous QA Action Selection Controller")
    sys.exit(suite.run([
        test_registry_has_ten_actions_all_implemented,
        test_get_action_returns_none_for_an_unknown_id,
        test_registry_ids_are_unique,
        test_empty_state_offers_only_project_discovery,
        test_after_discovery_runtime_plan_and_static_analysis_become_eligible,
        test_runtime_check_actions_require_being_in_the_actual_plan,
        test_runtime_check_already_executed_is_not_offered_again,
        test_runtime_diagnosis_eligible_only_when_a_real_undiagnosed_failure_exists,
        test_runtime_repair_eligible_only_with_a_real_diagnosed_unrepaired_failure,
        test_parser_accepts_a_valid_continue_response,
        test_parser_accepts_a_valid_stop_response,
        test_parser_rejects_an_unknown_decision,
        test_parser_rejects_decision_error_from_the_model_itself,
        test_parser_rejects_malformed_json,
        test_parser_rejects_missing_next_action_for_continue,
        test_parser_rejects_next_action_supplied_for_stop,
        test_parser_rejects_empty_action_id,
        test_parser_rejects_multiple_actions,
        test_parser_rejects_missing_reason,
        test_parser_rejects_non_list_evidence_needed,
        test_valid_continue_decision_end_to_end,
        test_valid_stop_decision_end_to_end,
        test_unknown_action_is_rejected,
        test_unavailable_but_real_action_is_rejected,
        test_dependency_unsatisfied_action_is_rejected,
        test_malformed_json_from_provider_is_rejected_safely,
        test_invalid_schema_is_rejected_safely,
        test_provider_failure_is_handled_gracefully,
        test_deterministic_allowlist_cannot_be_bypassed_by_naming_a_real_action_from_a_different_state,
        test_completed_action_is_handled_correctly,
        test_zero_remaining_budget_stops_deterministically_without_an_ai_call,
        test_no_eligible_actions_stops_deterministically_without_an_ai_call,
        test_arbitrary_shell_command_is_rejected,
        test_arbitrary_file_modification_request_is_rejected,
        test_run_arbitrary_command_action_name_is_rejected,
        test_a_real_but_currently_unavailable_registry_action_is_rejected,
        test_runtime_repair_requires_a_valid_target_even_in_a_favorable_state,
        test_ai_output_cannot_change_action_availability,
        test_ai_output_cannot_change_previous_execution_results,
        test_ai_output_cannot_mutate_registry_metadata,
        test_mockprovider_integration,
        test_ollamaprovider_shape_compatible_without_a_live_call,
        test_openrouterprovider_shape_compatible_without_a_live_call,
        test_prompt_is_bounded_even_with_a_large_state,
        test_prompt_never_includes_raw_logs,
        test_prompt_states_the_qa_objective,
        test_agent_package_never_imports_execution_functions,
        test_agent_package_never_imports_runtime_or_project_packages,
        test_g52_executor_is_the_only_place_that_crosses_the_execution_boundary,
        test_deterministic_engines_never_import_the_agent_package,
        test_agent_package_makes_no_subprocess_or_file_write_calls,
    ]))
