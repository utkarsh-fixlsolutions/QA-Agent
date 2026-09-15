"""Prompt construction for the G5.1 Action Selection Controller
(docs/28-g5-action-selection-controller.md) - `diagnosis_prompts.py`'s own
role, one layer up: instead of one runtime failure, the whole current QA
state; instead of a diagnosis, a choice among a deterministically-computed
allowlist.

Every fact in the prompt comes from real, already-produced data
(`QAState`, the `ActionDefinition`s `actions.eligible_actions` already
computed) or from the small, curated `RepositoryContext` subset
`diagnosis_prompts.py`/`runtime_repair_prompts.py` already established as
the right amount of repository detail for a bounded prompt - never a full
repository dump, never raw logs (only short, already-produced status/
reason/summary strings, themselves already bounded by their own schemas
upstream).
"""

from __future__ import annotations

from typing import Tuple

from qa_agent.ai import CONTRADICTORY_EVIDENCE_CLAUSE, DETERMINISTIC_AUTHORITY_CLAUSE, Prompt

from .actions import valid_targets
from .models import DEFAULT_OBJECTIVE, QAState

_ROLE = (
    "You are a QA strategist deciding what should happen next in an "
    "automated QA session. You do not execute anything yourself - a "
    "separate, deterministic controller executes whatever action is "
    "actually selected, and only after independently re-validating that "
    "your choice is still allowed. Your only job is to recommend exactly "
    "one next action from the allowlist supplied below, or to recommend "
    "stopping if no useful, safe action remains."
)

ACTION_SELECTION_GUARDRAILS = (
    "You may select only an action id that appears in the AVAILABLE "
    "ACTIONS list below - never a shell command, a file path, a tool "
    "name, or any id not present there, even a highly plausible-sounding "
    "one; naming anything else is treated as an invalid selection, not "
    "honored. You cannot execute a command, modify a file, or apply a "
    "repair yourself under any circumstances - selecting an action only "
    "ever recommends it. Do not invent a capability, a tool, or an action "
    "that is not already listed. Do not select an action already listed "
    "under COMPLETED ACTIONS - if you believe repeating one is genuinely "
    "justified, you may only do so by explaining that justification in "
    "'reason', and only for an action id that still appears in AVAILABLE "
    "ACTIONS (the controller has already excluded ordinary repeats on its "
    "own). Prefer the action most likely to reduce real uncertainty about "
    "the QA state, not merely the first one listed. If no available "
    "action would meaningfully help, choose to stop rather than picking "
    "one arbitrarily. Some actions require a target, shown in parentheses "
    "next to that action below as its own real, valid id list - choose "
    "'target' only from that exact list for that action; never invent one "
    "(a file path, a made-up check name, anything not shown). For an "
    "action with no listed targets, 'target' must be null. "
    + CONTRADICTORY_EVIDENCE_CLAUSE + " " + DETERMINISTIC_AUTHORITY_CLAUSE + " "
    'Respond with JSON only, matching exactly one of these two shapes - '
    "no extra commentary outside the JSON, and no markdown other than the "
    'JSON itself: {"decision": "continue", "next_action": "<one action id '
    'from AVAILABLE ACTIONS>", "target": "<a valid target id for that '
    'action, or null if it lists none>", "reason": "<brief reason>", '
    '"evidence_needed": ["<what would help confirm this>", ...]} or '
    '{"decision": "stop", "next_action": null, "target": null, '
    '"reason": "<brief reason>", "evidence_needed": []}.'
)


def _repository_context_lines(context) -> list:
    if context is None:
        return ["(not yet discovered)"]
    project = context.project
    lines = [
        "Repository type: {}".format(project.repository_type),
        "Application type: {}".format(project.application_type),
    ]
    if project.languages:
        lines.append("Languages: {}".format(", ".join(i.name for i in project.languages)))
    if project.frameworks:
        lines.append("Frameworks: {}".format(", ".join(i.name for i in project.frameworks)))
    return lines


def _check_result_lines(results) -> list:
    if not results:
        return ["(none yet)"]
    lines = []
    for result in results:
        lines.append("  - {} [{}]: {}".format(
            getattr(result, "id", "?"), getattr(result, "status", "?"), getattr(result, "reason", "")))
    return lines


def _diagnosis_lines(diagnoses) -> list:
    if not diagnoses:
        return ["(none yet)"]
    lines = []
    for diagnosis in diagnoses:
        lines.append("  - {} [{}]: {}".format(
            getattr(diagnosis, "check_id", "?"), getattr(diagnosis, "diagnosis_status", "?"),
            getattr(diagnosis, "summary", "") or getattr(diagnosis, "error", "")))
    return lines


def _repair_lines(repairs) -> list:
    """Same shape as `_diagnosis_lines`, one layer later (G5.3, docs/34) -
    context only, never load-bearing: `valid_targets`/eligibility (not this
    prompt text) is what actually prevents re-selecting an already-repaired
    check id, so this section exists purely so the AI's own 'reason' text
    can refer to what already happened, the same way it can already refer
    to diagnoses.
    """
    if not repairs:
        return ["(none yet)"]
    lines = []
    for repair in repairs:
        lines.append("  - {} [{}]: {}".format(
            getattr(repair, "check_id", "?"), getattr(repair, "outcome", "?"),
            getattr(repair, "explanation", "")))
    return lines


def _action_lines(actions, state: QAState) -> list:
    if not actions:
        return ["(none - nothing is currently eligible)"]
    lines = []
    for action in actions:
        line = "  - {} [{}, {}]: {}".format(action.id, action.category, action.safety_level, action.description)
        if action.requires_target:
            targets = valid_targets(action, state)
            line += " (valid targets: {})".format(", ".join(targets) if targets else "none currently valid")
        lines.append(line)
    return lines


def build_action_selection_prompt(state: QAState, eligible_actions: Tuple[object, ...]) -> Prompt:
    """`state`: the current, real `QAState`. `eligible_actions`: exactly
    the `ActionDefinition`s `actions.eligible_actions(state)` already
    computed - passed in rather than recomputed here, so the prompt can
    never drift from what the controller will actually enforce afterward.
    """
    objective = state.objective.strip() or DEFAULT_OBJECTIVE
    completed = sorted(state.completed_action_ids)

    lines = ["QA OBJECTIVE", "", objective, "", "REPOSITORY CONTEXT", ""]
    lines += _repository_context_lines(state.repository_context)
    lines += ["", "CURRENT STATE", ""]
    lines.append("Iteration: {} of {} (remaining budget: {})".format(
        state.iteration, state.max_iterations, state.remaining_budget))
    lines.append("Completed actions: {}".format(", ".join(completed) if completed else "(none yet)"))
    lines += ["", "RUNTIME CHECK RESULTS SO FAR", ""]
    lines += _check_result_lines(state.execution_results)
    lines += ["", "DIAGNOSES SO FAR", ""]
    lines += _diagnosis_lines(state.diagnoses)
    lines += ["", "REPAIRS SO FAR", ""]
    lines += _repair_lines(state.repairs)
    lines += ["", "AVAILABLE ACTIONS (choose next_action from this list only, or stop)", ""]
    lines += _action_lines(eligible_actions, state)
    lines += [
        "",
        "Decide: should QA continue, and if so with exactly which one available action - or should it stop?",
    ]
    system = "{}\n\n{}".format(_ROLE, ACTION_SELECTION_GUARDRAILS)
    return Prompt(system=system, user="\n".join(lines))
