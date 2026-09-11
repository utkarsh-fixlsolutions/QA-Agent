"""The AI Runtime Failure Diagnosis Engine's public entry points (Phase G
Part 3, docs/23-runtime-failure-diagnosis-engine.md):

    RuntimeCheckResult -> build_diagnosis_prompt() -> provider.generate()
                        -> validate_diagnosis_response() -> response_is_grounded()
                        -> RuntimeDiagnosis

Mirrors `explainer.py`'s own orchestration shape exactly (`explain_finding`/
`explain_findings` -> `diagnose_runtime_failure`/`diagnose_runtime_failures`):
this module builds no prompt, extracts no evidence, and validates no
response itself - those three steps are reused verbatim from
`diagnosis_prompts.py`/`diagnosis_parser.py`; this module only wires them
together and decides what counts as a usable diagnosis.

The deterministic runtime executor (Phase G Part 2) remains the sole
authority on whether a check passed or failed - nothing here ever changes
a `RuntimeCheckResult`, re-runs a check, or overrides its `status`. AI
diagnosis is optional, additive metadata: any failure along the way (an
offline or timed-out provider, a malformed or ungrounded response, or the
model's own "insufficient_context" decline) is not an error from this
module's point of view - it simply means a structured, explicit diagnosis
outcome, never a raised exception and never a fabricated one.
"""

from __future__ import annotations

import json

from .diagnosis_models import (
    DIAGNOSIS_AI_ERROR,
    DIAGNOSIS_DIAGNOSED,
    DIAGNOSIS_INSUFFICIENT_CONTEXT,
    DIAGNOSIS_INVALID_RESPONSE,
    DIAGNOSIS_NOT_APPLICABLE,
    RuntimeDiagnosis,
)
from .diagnosis_parser import DiagnosisResponse, response_is_grounded, validate_diagnosis_response
from .diagnosis_prompts import build_diagnosis_prompt
from .prompts import Prompt
from .schemas import STATUS_INSUFFICIENT_CONTEXT, STATUS_SUCCESS

# The real Phase G Part 2 status strings a check must have to be worth
# asking the AI about at all - a plain string tuple, not an import of
# qa_agent.runtime's own constants, so this module's only dependency on
# qa_agent.runtime stays exactly what it already is: reading the *data*
# (RuntimeCheckResult's own `.status` string) it is handed, never importing
# behavior from that package. The values themselves are qa_agent.runtime.
# execution_models.STATUS_FAIL/STATUS_TIMEOUT/STATUS_ERROR verbatim.
DIAGNOSABLE_STATUSES = ("fail", "timeout", "error")


def _prompt_text(prompt: Prompt) -> str:
    return "{}\n\n{}".format(prompt.system, prompt.user)


def diagnose_runtime_failure(check_result, repository_context, provider, runtime_check=None):
    """Try to diagnose one `RuntimeCheckResult`. Always returns a
    `RuntimeDiagnosis` - never `None`, never raises. A check whose real
    status is not diagnosable (PASS/SKIPPED/NOT_IMPLEMENTED) returns
    `NOT_APPLICABLE` immediately, with zero provider calls made.

    `runtime_check`: the originating `RuntimeCheck` (Phase G Part 1's own
    output), when known - enriches the prompt and the grounding check with
    real planning-time evidence (category, `required_evidence`). Optional;
    a bare `RuntimeCheckResult` is enough to call this directly.
    """
    if check_result.status not in DIAGNOSABLE_STATUSES:
        return RuntimeDiagnosis(
            check_id=check_result.id,
            check_name=check_result.name,
            execution_status=check_result.status,
            diagnosis_status=DIAGNOSIS_NOT_APPLICABLE,
        )

    try:
        prompt = build_diagnosis_prompt(check_result, repository_context, runtime_check=runtime_check)
        response = provider.generate(_prompt_text(prompt))
        if not response.ok:
            return RuntimeDiagnosis(
                check_id=check_result.id, check_name=check_result.name,
                execution_status=check_result.status, diagnosis_status=DIAGNOSIS_AI_ERROR,
                error=response.error, model=response.model,
            )

        result = validate_diagnosis_response(response.text)
        if result.status == STATUS_INSUFFICIENT_CONTEXT:
            return RuntimeDiagnosis(
                check_id=check_result.id, check_name=check_result.name,
                execution_status=check_result.status, diagnosis_status=DIAGNOSIS_INSUFFICIENT_CONTEXT,
                error=result.reason, model=response.model,
            )
        if result.status != STATUS_SUCCESS:
            return RuntimeDiagnosis(
                check_id=check_result.id, check_name=check_result.name,
                execution_status=check_result.status, diagnosis_status=DIAGNOSIS_INVALID_RESPONSE,
                error=result.reason, model=response.model,
            )
        # ValidationResult.value is typed as plain `object`; STATUS_SUCCESS
        # is diagnosis_parser.py's own guarantee that it is really a
        # DiagnosisResponse here - narrowed explicitly since pyright
        # cannot infer that from the status check alone.
        assert isinstance(result.value, DiagnosisResponse)

        if not response_is_grounded(result.value, check_result, repository_context, runtime_check=runtime_check):
            return RuntimeDiagnosis(
                check_id=check_result.id, check_name=check_result.name,
                execution_status=check_result.status, diagnosis_status=DIAGNOSIS_INVALID_RESPONSE,
                error="response named a file/component not present in the supplied evidence",
                model=response.model,
            )

        diagnosis_response = result.value
        return RuntimeDiagnosis(
            check_id=check_result.id,
            check_name=check_result.name,
            execution_status=check_result.status,
            diagnosis_status=DIAGNOSIS_DIAGNOSED,
            summary=diagnosis_response.summary,
            severity=diagnosis_response.severity,
            confidence=diagnosis_response.confidence,
            observed_evidence=diagnosis_response.evidence,
            likely_root_cause=diagnosis_response.root_cause,
            affected_files=diagnosis_response.affected_files,
            affected_components=diagnosis_response.affected_components,
            recommended_action=diagnosis_response.recommended_action,
            model=response.model,
        )
    except Exception as exc:  # noqa: BLE001 - AI must never fail the QA run
        return RuntimeDiagnosis(
            check_id=check_result.id, check_name=check_result.name,
            execution_status=check_result.status, diagnosis_status=DIAGNOSIS_AI_ERROR,
            error="unexpected error: {}".format(exc),
        )


def diagnose_runtime_failures(execution_result, repository_context, provider):
    """Diagnose every check in `execution_result`, in order, each fully
    independently - the same execution-isolation principle the deterministic
    engine already uses for tools (docs/13-multi-analyzer-foundation.md):
    one check's diagnosis (or its failure to produce one) never affects
    another's, and no relationship between two failures is ever fabricated
    unless the evidence for each individually supports it.

    Returns one `RuntimeDiagnosis` per check in `execution_result.results`,
    in the same order - a check that was never diagnosed (PASS/SKIPPED/
    NOT_IMPLEMENTED) still gets an explicit `NOT_APPLICABLE` entry, so the
    result is always a complete 1:1 mapping, never a partial list a caller
    has to reconcile against the original results by hand.
    """
    checks_by_id = {check.id: check for check in execution_result.plan.checks}
    diagnoses = []
    for check_result in execution_result.results:
        runtime_check = checks_by_id.get(check_result.id)
        diagnoses.append(
            diagnose_runtime_failure(check_result, repository_context, provider, runtime_check=runtime_check)
        )
    return tuple(diagnoses)


# --- rendering/serialization ----------------------------------------------
#
# Kept here rather than in a fifth file (docs/23's own "smallest clean set
# of files necessary") - the same "one module presents what it just
# computed" shape qa_agent/runtime/render.py and execution_render.py
# already use one layer down, just not split out separately since this
# phase's own render surface is small.


def diagnosis_to_dict(diagnosis):
    return {
        "check_id": diagnosis.check_id,
        "check_name": diagnosis.check_name,
        "execution_status": diagnosis.execution_status,
        "diagnosis_status": diagnosis.diagnosis_status,
        "summary": diagnosis.summary,
        "severity": diagnosis.severity,
        "confidence": diagnosis.confidence,
        "observed_evidence": list(diagnosis.observed_evidence),
        "likely_root_cause": diagnosis.likely_root_cause,
        "affected_files": list(diagnosis.affected_files),
        "affected_components": list(diagnosis.affected_components),
        "recommended_action": diagnosis.recommended_action,
        "model": diagnosis.model,
        "error": diagnosis.error,
    }


def diagnoses_to_json(diagnoses, indent=2):
    return json.dumps([diagnosis_to_dict(d) for d in diagnoses], indent=indent, sort_keys=False)


def render_diagnosis(diagnosis):
    """Plain-text rendering of one `RuntimeDiagnosis` - always explicitly
    labeled "AI Diagnosis", never rendered in a way that could be mistaken
    for the deterministic runtime result it is about (this phase's own
    "never present AI diagnosis as deterministic runtime fact" rule).
    Returns `""` for a `NOT_APPLICABLE` diagnosis - nothing to show for a
    check that was never sent to the AI at all.
    """
    if diagnosis.diagnosis_status == DIAGNOSIS_NOT_APPLICABLE:
        return ""
    lines = ["  AI Diagnosis:"]
    if diagnosis.diagnosis_status == DIAGNOSIS_DIAGNOSED:
        lines.append("    Summary: {}".format(diagnosis.summary))
        lines.append("    Likely root cause: {}".format(diagnosis.likely_root_cause))
        lines.append("    Confidence: {:.2f}".format(diagnosis.confidence))
        lines.append("    Severity: {}".format(diagnosis.severity))
        if diagnosis.observed_evidence:
            lines.append("    Evidence: {}".format("; ".join(diagnosis.observed_evidence)))
        if diagnosis.affected_files:
            lines.append("    Affected files: {}".format(", ".join(diagnosis.affected_files)))
        if diagnosis.affected_components:
            lines.append("    Affected components: {}".format(", ".join(diagnosis.affected_components)))
        lines.append("    Recommended action: {}".format(diagnosis.recommended_action))
    elif diagnosis.diagnosis_status == DIAGNOSIS_INSUFFICIENT_CONTEXT:
        lines.append("    The evidence captured for this failure was not sufficient for a reliable diagnosis.")
        if diagnosis.error:
            lines.append("    Reason: {}".format(diagnosis.error))
    elif diagnosis.diagnosis_status == DIAGNOSIS_INVALID_RESPONSE:
        lines.append("    The AI response could not be validated and was discarded.")
        if diagnosis.error:
            lines.append("    Reason: {}".format(diagnosis.error))
    elif diagnosis.diagnosis_status == DIAGNOSIS_AI_ERROR:
        lines.append("    No diagnosis is available (AI provider error).")
        if diagnosis.error:
            lines.append("    Reason: {}".format(diagnosis.error))
    return "\n".join(lines)
