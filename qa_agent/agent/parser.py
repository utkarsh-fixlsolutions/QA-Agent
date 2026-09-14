"""Parsing and validating a raw AI action-selection response
(docs/28-g5-action-selection-controller.md).

Mirrors `diagnosis_parser.py`'s own shape exactly: reuses `ValidationResult`
and `STATUS_SUCCESS`/`STATUS_INVALID` from `qa_agent.ai.schemas` (the same
shared, three-state result contract every response validator in this
project already returns) and `parse_json_response`/`strip_markdown_fence`
from `qa_agent.ai.response_parser` (the same generic JSON/fence handling
every validator in this project already reuses rather than reimplementing).
No `insufficient_context` decline exists for this schema - "stop" already
serves that role ("nothing useful to do right now"), so this module never
imports `STATUS_INSUFFICIENT_CONTEXT` at all.

This module only ever performs *structural* validation - is the JSON
well-formed, are the required fields present and correctly typed. Whether
a structurally-valid `next_action` is actually an eligible action right
now is a stateful question this module has no way to answer (and no
business answering) - that check belongs to `controller.py` alone,
mirroring exactly how `diagnosis_parser.py`'s own `validate_diagnosis_
response` (schema) and `response_is_grounded` (evidence) stay two
separate steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .models import DECISION_CONTINUE, DECISION_STOP
from qa_agent.ai import STATUS_INVALID, STATUS_SUCCESS, ValidationResult, parse_json_response


@dataclass(frozen=True)
class DecisionResponse:
    """A structurally-validated action-selection response - raw material
    for `controller.py` to turn into a `ControllerDecision` once it has
    also checked `next_action` (and, for an action that needs one,
    `target`) against the actually-eligible/valid sets. Mirrors
    `DiagnosisResponse`/`RepairResponse`'s own role in their respective
    parsers exactly: the model's own response is not part of this shape
    (known to the caller already, attached afterward).
    """

    decision: str
    next_action: object  # str | None
    reason: str
    evidence_needed: Tuple[str, ...]
    target: object = None  # str | None - structurally validated here; eligibility checked in controller.py


def _string_list(value):
    """`None` for anything that is not a list of non-empty strings - the
    same small, deliberately duplicated rule `diagnosis_parser.py`'s own
    `_string_list` already enforces (this package's own zero-import
    convention for `qa_agent.ai`'s internals - see models.py's own
    docstring; only the truly shared, package-level `qa_agent.ai` exports
    are reused, never another module's private helpers).
    """
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        result.append(item)
    return tuple(result)


def validate_decision_response(text: str) -> ValidationResult:
    """Validate a raw response against the action-selection schema:

        {"decision": "continue", "next_action": "<action id>",
         "target": "<target id, or null if the action needs none>",
         "reason": "<text>", "evidence_needed": ["...", ...]}
        {"decision": "stop", "next_action": null, "target": null,
         "reason": "<text>", "evidence_needed": []}

    Rejected, always as `STATUS_INVALID`, never silently coerced or
    guessed past: an unrecognized `decision`, a missing/empty `reason`, a
    non-list-of-strings `evidence_needed`, a missing/empty/wrongly-typed
    `next_action` for `continue` (including a list - the structural form
    "multiple actions" would take), any non-null `next_action` for `stop`,
    a non-null non-string `target`, or any non-null `target` for `stop`.
    Never raises - a malformed response becomes `STATUS_INVALID`, exactly
    like every other validator in this project.

    `target` is intentionally optional in the raw JSON (a model answering
    an action that needs none may simply omit the key) - absent is treated
    identically to an explicit `null`. Whether a given, structurally-valid
    `target` is actually correct for the selected action (required-but-
    missing, or naming something that doesn't exist) is, like `next_action`
    itself, a stateful question this module has no business answering -
    see `controller.py`.
    """
    data, error = parse_json_response(text)
    if error is not None:
        return ValidationResult(status=STATUS_INVALID, reason=error)
    if not isinstance(data, dict):
        return ValidationResult(status=STATUS_INVALID, reason="response is not a JSON object")

    decision = data.get("decision")
    if decision not in (DECISION_CONTINUE, DECISION_STOP):
        # Deliberately rejects "error" too, alongside anything else the
        # model might invent - "error" is this controller's own output
        # state for something *it* could not resolve; the model is never
        # allowed to claim it on its own behalf.
        return ValidationResult(
            status=STATUS_INVALID,
            reason="'decision' must be 'continue' or 'stop', got {!r}".format(decision),
        )

    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return ValidationResult(status=STATUS_INVALID, reason="missing or empty required field: 'reason'")

    evidence_needed = _string_list(data.get("evidence_needed", []))
    if evidence_needed is None:
        return ValidationResult(status=STATUS_INVALID, reason="'evidence_needed' must be a list of strings")

    next_action = data.get("next_action")
    if decision == DECISION_STOP:
        if next_action is not None:
            return ValidationResult(
                status=STATUS_INVALID, reason="'next_action' must be null when decision is 'stop'"
            )
    else:  # DECISION_CONTINUE
        if not isinstance(next_action, str) or not next_action.strip():
            return ValidationResult(
                status=STATUS_INVALID,
                reason="'next_action' must be a non-empty string when decision is 'continue'",
            )

    target = data.get("target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        return ValidationResult(status=STATUS_INVALID, reason="'target', when present, must be a non-empty string or null")
    if decision == DECISION_STOP and target is not None:
        return ValidationResult(status=STATUS_INVALID, reason="'target' must be null when decision is 'stop'")

    return ValidationResult(
        status=STATUS_SUCCESS,
        value=DecisionResponse(
            decision=decision, next_action=next_action, reason=reason, evidence_needed=evidence_needed,
            target=target,
        ),
    )
