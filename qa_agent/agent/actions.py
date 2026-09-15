"""The deterministic action registry (docs/28-g5-action-selection-controller
.md) - the closed, hand-authored allowlist the AI selects from. The AI is
never trusted to invent an id, determine a dependency, or decide its own
eligibility; every entry here is fixed data, and `eligible_actions()` is a
pure function of `QAState` - no AI call, no randomness, no hidden state.

Ten actions were investigated (the nine this step's own spec named, plus
`static_assets`, a real, safe, already-implemented executor found during
this step's own repository audit that the spec's list happened to omit).
All ten are now `implemented=True` - `runtime_repair` was deliberately
`implemented=False` through G5.1/G5.2 (repair *selection* was explicitly
reserved for G5.3) and became real in G5.3 (docs/34), wired to the
existing, unmodified G4 repair pipeline via `executor.py`.
"""

from __future__ import annotations

from typing import Tuple

from .models import (
    SAFETY_ASSISTED,
    SAFETY_AUTONOMOUS,
    SAFETY_READ_ONLY,
    ActionDefinition,
    QAState,
)

# action id -> the real RuntimeCheck id it corresponds to, for the four
# (now five, with static_assets) actions that map 1:1 onto an executor
# already implemented in qa_agent/runtime/executor.py's own _EXECUTORS
# dict. Read by QAState.completed_action_ids too (models.py) - this is the
# one place that mapping is defined, never respelled a second time.
RUNTIME_CHECK_ACTION_IDS = {
    "server_startup": "server-startup",
    "build_verification": "build-verification",
    "test_suite_verification": "test-suite-verification",
    "environment_validation": "environment-configuration",
    "static_assets": "static-assets",
}

ACTION_REGISTRY: Tuple[ActionDefinition, ...] = (
    ActionDefinition(
        id="project_discovery",
        description="Build a deterministic, evidence-based profile of the repository - languages, "
                    "frameworks, package managers, important files (qa_agent.project.discover_project + "
                    "build_repository_context). The foundation every later action needs.",
        category="discovery", safety_level=SAFETY_READ_ONLY, modifies_files=False,
        requires=(), implemented=True,
    ),
    ActionDefinition(
        id="runtime_plan",
        description="Build a dependency-ordered RuntimeQAPlan from the repository context - what should "
                    "be runtime-tested and why (qa_agent.runtime.plan_runtime_qa). Nothing is executed.",
        category="planning", safety_level=SAFETY_READ_ONLY, modifies_files=False,
        requires=("project_discovery",), implemented=True,
    ),
    ActionDefinition(
        id="static_analysis",
        description="Run the deterministic static analyzers (ruff/pyright/mypy/eslint/shellcheck) against "
                    "the repository (qa_agent.runner.run) - real, tool-verified findings only.",
        category="static_analysis", safety_level=SAFETY_READ_ONLY, modifies_files=False,
        requires=("project_discovery",), implemented=True,
    ),
    ActionDefinition(
        id="server_startup",
        description="Run the planned Server Startup check - launches a real dev-server process and "
                    "confirms it stays alive, always cleanly terminated afterward (qa_agent.runtime, "
                    "check id 'server-startup').",
        category="runtime_check", safety_level=SAFETY_ASSISTED, modifies_files=False,
        requires=("runtime_plan",), implemented=True,
    ),
    ActionDefinition(
        id="build_verification",
        description="Run the planned Build Verification check - a real build command run to completion "
                    "(qa_agent.runtime, check id 'build-verification').",
        category="runtime_check", safety_level=SAFETY_ASSISTED, modifies_files=False,
        requires=("runtime_plan",), implemented=True,
    ),
    ActionDefinition(
        id="test_suite_verification",
        description="Run the planned Test Suite Verification check - a real test command run to "
                    "completion (qa_agent.runtime, check id 'test-suite-verification').",
        category="runtime_check", safety_level=SAFETY_ASSISTED, modifies_files=False,
        requires=("runtime_plan",), implemented=True,
    ),
    ActionDefinition(
        id="environment_validation",
        description="Run the planned Environment Configuration check - confirms previously-detected "
                    "environment files are still present (qa_agent.runtime, check id "
                    "'environment-configuration'). A pure filesystem read, no subprocess.",
        category="runtime_check", safety_level=SAFETY_READ_ONLY, modifies_files=False,
        requires=("runtime_plan",), implemented=True,
    ),
    ActionDefinition(
        id="static_assets",
        description="Run the planned Static Asset Verification check - confirms previously-detected "
                    "static asset directories are still present (qa_agent.runtime, check id "
                    "'static-assets'). A pure filesystem read, no subprocess.",
        category="runtime_check", safety_level=SAFETY_READ_ONLY, modifies_files=False,
        requires=("runtime_plan",), implemented=True,
    ),
    ActionDefinition(
        id="runtime_diagnosis",
        description="Ask AI to interpret why an already-failed runtime check failed - grounded in the "
                    "real evidence, never the pass/fail authority (qa_agent.ai.diagnose_runtime_failure, "
                    "Phase G Part 3). Only ever interprets a check whose real status is already "
                    "fail/timeout/error. Requires a target: which specific failed check id to interpret "
                    "(see valid_targets()) - the AI chooses among real, currently-undiagnosed failures "
                    "only, never an invented one.",
        category="diagnosis", safety_level=SAFETY_ASSISTED, modifies_files=False,
        requires=("runtime_plan",), implemented=True, requires_target=True,
    ),
    ActionDefinition(
        id="runtime_repair",
        description="Let a DIAGNOSED runtime failure enter the existing, verified Phase E/G4 repair "
                    "pipeline (qa_agent.ai.repair_runtime_failure) - propose, apply in a temporary "
                    "workspace, statically validate, re-run the real check against a candidate copy, and "
                    "only write to the real repository once accepted; the real check is then re-run again "
                    "for real before this is ever reported as verified (G5.3, docs/34). Requires a target: "
                    "which specific diagnosed failure to attempt repairing (see valid_targets()) - a check "
                    "id that has already received any repair attempt, success or failure, is never offered "
                    "again this session (at most one repair attempt per failure, docs/24's own rule).",
        category="repair", safety_level=SAFETY_AUTONOMOUS, modifies_files=True,
        requires=("runtime_diagnosis",), implemented=True, requires_target=True,
    ),
)

_BY_ID = {action.id: action for action in ACTION_REGISTRY}


def get_action(action_id: str):
    """The real `ActionDefinition` for `action_id`, or `None` if it is not
    in the registry at all - the deterministic ground truth `eligible_actions`
    and the controller's own eligibility check both build on.
    """
    return _BY_ID.get(action_id)


def _runtime_check_eligible(action: ActionDefinition, state: QAState) -> bool:
    """A planned-but-not-yet-run check: the plan must exist, must actually
    include this specific check id (a plan can legitimately omit a check -
    e.g. no Server Startup check exists for a repository with no runnable
    entry point), and it must not already have a real execution result.
    """
    if state.runtime_plan is None:
        return False
    check_id = RUNTIME_CHECK_ACTION_IDS[action.id]
    planned_ids = {getattr(c, "id", None) for c in getattr(state.runtime_plan, "checks", ())}
    if check_id not in planned_ids:
        return False
    return check_id not in state.executed_check_ids


# action id -> an extra, dynamic eligibility check beyond "not already
# completed and every static dependency satisfied" - only the five
# runtime-check actions need one (whether this *specific* check id was
# actually planned, a fact "requires"/"completed" alone cannot express).
# `runtime_diagnosis` needs no entry here: `QAState.completed_action_ids`
# already encodes its own dynamic "nothing left to diagnose" rule directly,
# so the generic "not already completed" check alone is sufficient - a
# second, separate eligibility function here would only ever repeat that
# same formula, not add anything.
_DYNAMIC_ELIGIBILITY = {
    "server_startup": _runtime_check_eligible,
    "build_verification": _runtime_check_eligible,
    "test_suite_verification": _runtime_check_eligible,
    "environment_validation": _runtime_check_eligible,
    "static_assets": _runtime_check_eligible,
}


def eligible_actions(state: QAState) -> Tuple[ActionDefinition, ...]:
    """The complete, deterministic allowlist for one decision - every
    action the AI is even allowed to see, let alone select. Pure function
    of `state`; calling this twice with the same state always returns the
    same result, and nothing about it is influenced by any AI output.

    An action is eligible only when all of:
    - `implemented` is `True` (never expose something with no real backing
      capability, or one this step deliberately excludes - `runtime_repair`).
    - it is not already in `state.completed_action_ids`.
    - every id in `requires` is already in `state.completed_action_ids`.
    - its own dynamic eligibility check (if any) passes.
    """
    completed = state.completed_action_ids
    result = []
    for action in ACTION_REGISTRY:
        if not action.implemented:
            continue
        if action.id in completed:
            continue
        if not all(dep in completed for dep in action.requires):
            continue
        dynamic_check = _DYNAMIC_ELIGIBILITY.get(action.id)
        if dynamic_check is not None and not dynamic_check(action, state):
            continue
        result.append(action)
    return tuple(result)


def valid_targets(action: ActionDefinition, state: QAState) -> Tuple[str, ...]:
    """The deterministically-valid target ids for `action` given `state` -
    `()` for every action with `requires_target=False` (target selection
    does not apply). For `runtime_diagnosis` (G5.2, docs/29), the real,
    currently-undiagnosed failed check ids - the exact same set
    `QAState.completed_action_ids` already derives its own "nothing left
    to diagnose" rule from, so this can never disagree with whether
    `runtime_diagnosis` itself is eligible in the first place. Sorted for
    a deterministic, stable prompt/test ordering - never insertion order,
    which would depend on execution timing.
    """
    if not action.requires_target:
        return ()
    if action.id == "runtime_diagnosis":
        return tuple(sorted(state.failed_check_ids - state.diagnosed_check_ids))
    if action.id == "runtime_repair":
        return tuple(sorted(state.diagnosed_repairable_check_ids - state.repaired_check_ids))
    return ()  # pragma: no cover - no other action currently requires a target
