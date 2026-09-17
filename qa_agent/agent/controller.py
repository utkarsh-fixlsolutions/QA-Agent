"""The G5.1 Autonomous QA Action Selection Controller's one public entry
point (docs/28-g5-action-selection-controller.md):

    QAState -> eligible_actions() [deterministic, no AI]
            -> build_action_selection_prompt() -> provider.generate()
            -> validate_decision_response() -> eligibility re-check [deterministic, no AI]
            -> ControllerDecision

G5.1 is CHOOSE NEXT QA ACTION only - the first link of the eventual G5 loop
(OBSERVE -> REASON -> CHOOSE NEXT QA ACTION -> EXECUTE DETERMINISTIC ACTION
-> OBSERVE RESULT -> ...). Nothing in this module - or anywhere in
`qa_agent/agent/` - executes a subprocess, a shell command, an HTTP
request, a static analyzer, a runtime check, or a repair. It never writes
to a file. It never calls `discover_project`, `plan_runtime_qa`,
`run_runtime_plan`, `runner.run`, `diagnose_runtime_failure`, or
`repair_runtime_failure` - those are the deterministic engines G5.1
*reasons about*, never invokes. Executing whatever this controller
recommends is explicitly G5.2's job, a later, separate step; repair
selection specifically is explicitly G5.3's job - see docs/28's own
roadmap.

The AI is a strategist, never an authority: `select_next_action()` computes
the real, deterministic allowlist *before* ever consulting a provider, and
independently re-checks the AI's own selection against that exact same
allowlist afterward - the AI cannot expand, redefine, or bypass it by
naming something else, no matter how plausible. Every required failure
mode (a zero/negative action budget, no eligible action, a provider
failure, a malformed response, a structurally-valid-but-ineligible
selection, or a genuinely unexpected exception) returns a `ControllerDecision`
with `decision="error"` (or a deterministic `"stop"`, for the two cases
that need no AI opinion at all) - never raises, never invents a fallback
action, matching the "AI must never fail the QA run" contract this
project's every other AI-facing module already upholds.
"""

from __future__ import annotations

from qa_agent.ai import STATUS_SUCCESS

from .actions import eligible_actions, get_action, valid_targets
from .models import (
    DECISION_CONTINUE,
    DECISION_ERROR,
    DECISION_STOP,
    ERROR_INELIGIBLE_ACTION,
    ERROR_INVALID_TARGET,
    ControllerDecision,
    QAState,
)
from .parser import validate_decision_response
from .prompts import build_action_selection_prompt


def _prompt_text(prompt) -> str:
    return "{}\n\n{}".format(prompt.system, prompt.user)


def select_next_action(state: QAState, provider) -> ControllerDecision:
    """Decide what should happen next, or that QA should stop - never
    executes anything. `provider` is any `AIProvider`-shaped object
    (`MockProvider`/`OllamaProvider`/`OpenRouterProvider`, or a test
    double) - this module never constructs one itself and never imports a
    concrete provider class, matching `diagnose_runtime_failure`'s/
    `propose_runtime_repair`'s own convention exactly.
    """
    try:
        if state.remaining_budget <= 0:
            return ControllerDecision(
                decision=DECISION_STOP, next_action=None,
                reason="action budget exhausted ({} of {} iteration(s) used) - stopping without "
                       "consulting the AI".format(state.iteration, state.max_iterations),
            )

        eligible = eligible_actions(state)
        eligible_ids = tuple(action.id for action in eligible)
        if not eligible_ids:
            return ControllerDecision(
                decision=DECISION_STOP, next_action=None,
                reason="no eligible action remains - stopping without consulting the AI",
            )

        prompt = build_action_selection_prompt(state, eligible)
        response = provider.generate(_prompt_text(prompt))
        if not response.ok:
            return ControllerDecision(
                decision=DECISION_ERROR, next_action=None, reason="AI provider error",
                eligible_actions=eligible_ids, model=response.model, error=response.error,
            )

        result = validate_decision_response(response.text)
        if result.status != STATUS_SUCCESS:
            return ControllerDecision(
                decision=DECISION_ERROR, next_action=None,
                reason="AI response could not be validated",
                eligible_actions=eligible_ids, model=response.model, error=result.reason,
            )
        parsed = result.value

        if parsed.decision == DECISION_CONTINUE and parsed.next_action not in eligible_ids:
            # The one check that actually matters most: no matter how
            # plausible-sounding parsed.next_action is (a shell command, a
            # file path, a tool name, an action that exists in the
            # registry but is not eligible right now), it is rejected here
            # unless it is exactly one of the ids this controller itself
            # already decided to offer.
            return ControllerDecision(
                decision=DECISION_ERROR, next_action=None,
                reason="AI selected an action that is not currently eligible: {!r}".format(parsed.next_action),
                eligible_actions=eligible_ids, model=response.model, error=ERROR_INELIGIBLE_ACTION,
            )

        if parsed.decision == DECISION_CONTINUE:
            # Same principle, one level deeper: a target is only ever
            # trusted when it is exactly one of the real, currently-valid
            # ids this controller computes itself - never the AI's own
            # unchecked claim (a file path, an invented check id, anything
            # not actually present in the real state).
            action = get_action(parsed.next_action)
            targets = valid_targets(action, state) if action is not None else ()
            if action is not None and action.requires_target:
                if parsed.target is None or parsed.target not in targets:
                    return ControllerDecision(
                        decision=DECISION_ERROR, next_action=None,
                        reason="AI selected an invalid or missing target {!r} for action {!r} "
                               "(valid targets: {})".format(parsed.target, parsed.next_action, list(targets)),
                        eligible_actions=eligible_ids, model=response.model, error=ERROR_INVALID_TARGET,
                    )
            elif parsed.target is not None:
                return ControllerDecision(
                    decision=DECISION_ERROR, next_action=None,
                    reason="AI supplied a target for action {!r}, which does not use one".format(parsed.next_action),
                    eligible_actions=eligible_ids, model=response.model, error=ERROR_INVALID_TARGET,
                )

        return ControllerDecision(
            decision=parsed.decision, next_action=parsed.next_action, reason=parsed.reason,
            target=parsed.target, evidence_needed=parsed.evidence_needed,
            eligible_actions=eligible_ids, model=response.model,
        )
    except Exception as exc:  # noqa: BLE001 - action selection must never crash the caller
        return ControllerDecision(
            decision=DECISION_ERROR, next_action=None,
            reason="unexpected error during action selection",
            error="{}: {}".format(type(exc).__name__, exc),
        )
