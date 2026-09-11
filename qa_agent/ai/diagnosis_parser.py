"""Parsing and validating a raw diagnosis LLM response (Phase G Part 3,
docs/23-runtime-failure-diagnosis-engine.md).

Mirrors `response_parser.py`'s own shape exactly: reuses `ValidationResult`
and `STATUS_SUCCESS`/`STATUS_INSUFFICIENT_CONTEXT`/`STATUS_INVALID` from
`schemas.py` rather than inventing a second parallel status set, reuses
`parse_json_response`/`strip_markdown_fence`, and returns a validated
`DiagnosisResponse` the same way `validate_repair_response` returns a
`RepairResponse`. Never raises - a malformed response becomes
`STATUS_INVALID`, never an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .diagnosis_models import SEVERITIES
from .response_parser import parse_json_response
from .schemas import STATUS_INSUFFICIENT_CONTEXT, STATUS_INVALID, STATUS_SUCCESS, ValidationResult


@dataclass(frozen=True)
class DiagnosisResponse:
    """A validated diagnosis for exactly one supplied runtime failure -
    `diagnosis.py`'s raw material for building the richer `RuntimeDiagnosis`
    (check identity, execution status, and model name are not part of the
    model's own response - they are known to the caller already and
    attached afterward, the same split `RepairResponse`/`RepairProposal`
    already established in Phase E Part 1).
    """

    summary: str
    severity: str
    confidence: float
    root_cause: str
    evidence: Tuple[str, ...]
    affected_files: Tuple[str, ...]
    affected_components: Tuple[str, ...]
    recommended_action: str


def _is_insufficient_context(data) -> bool:
    return isinstance(data, dict) and data.get("insufficient_context") is True


def _reason(data, default: str) -> str:
    if isinstance(data, dict) and isinstance(data.get("reason"), str) and data["reason"].strip():
        return data["reason"]
    return default


def _is_number_not_bool(value) -> bool:
    """`bool` is an `int` subclass in Python - the same trap
    `response_parser.py`'s own `_is_number_not_bool` already guards
    against for `confidence` there; guarded identically here.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _string_list(value):
    """`None` for anything that is not a list of non-empty strings -
    `evidence`/`affected_files`/`affected_components` must each be a real
    list (never a bare string, never a list containing a non-string), or
    the whole response is rejected rather than silently coerced.
    """
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        result.append(item)
    return tuple(result)


def validate_diagnosis_response(text: str) -> ValidationResult:
    """Validate a raw response against the diagnosis schema:
    `{"summary": "<text>", "severity": "error|warning|info",
    "confidence": <0.0-1.0>, "root_cause": "<text>", "evidence": ["..."],
    "affected_files": [...], "affected_components": [...],
    "recommended_action": "<text>"}`.

    Stricter than `validate_repair_response`'s own `confidence` handling
    (which clamps an out-of-range value): here, an out-of-range or wrongly
    typed `confidence` is rejected outright, matching this phase's own
    explicit "reject, never let the model bypass validation" requirement -
    a diagnosis is advisory text a person reads, not a value a later phase
    programmatically clamps and acts on the way a repair's confidence is.
    """
    data, error = parse_json_response(text)
    if error is not None:
        return ValidationResult(status=STATUS_INVALID, reason=error)
    if _is_insufficient_context(data):
        return ValidationResult(
            status=STATUS_INSUFFICIENT_CONTEXT,
            reason=_reason(data, "model reported insufficient context"),
        )
    if not isinstance(data, dict):
        return ValidationResult(status=STATUS_INVALID, reason="response is not a JSON object")

    missing_or_blank = [
        name for name in ("summary", "severity", "root_cause", "recommended_action")
        if not isinstance(data.get(name), str) or not data[name].strip()
    ]
    if missing_or_blank:
        return ValidationResult(
            status=STATUS_INVALID,
            reason="missing or empty required field(s): {}".format(", ".join(missing_or_blank)),
        )

    if data["severity"] not in SEVERITIES:
        return ValidationResult(
            status=STATUS_INVALID,
            reason="'severity' must be one of {}, got {!r}".format(SEVERITIES, data["severity"]),
        )

    confidence = data.get("confidence")
    if not _is_number_not_bool(confidence) or not (0.0 <= float(confidence) <= 1.0):
        return ValidationResult(
            status=STATUS_INVALID,
            reason="'confidence' must be a number between 0.0 and 1.0, got {!r}".format(confidence),
        )

    lists = {}
    for name in ("evidence", "affected_files", "affected_components"):
        parsed = _string_list(data.get(name, []))
        if parsed is None:
            return ValidationResult(
                status=STATUS_INVALID,
                reason="'{}' must be a list of non-empty strings".format(name),
            )
        lists[name] = parsed

    return ValidationResult(
        status=STATUS_SUCCESS,
        value=DiagnosisResponse(
            summary=data["summary"],
            severity=data["severity"],
            confidence=float(confidence),
            root_cause=data["root_cause"],
            evidence=lists["evidence"],
            affected_files=lists["affected_files"],
            affected_components=lists["affected_components"],
            recommended_action=data["recommended_action"],
        ),
    )


def response_is_grounded(response: DiagnosisResponse, check_result, repository_context, runtime_check=None) -> bool:
    """Structural grounding check, not a natural-language fact-checker (per
    this phase's own explicit instruction not to build one): every claim in
    `affected_files`/`affected_components` must appear, verbatim as a
    substring, somewhere in the real evidence this diagnosis was actually
    given - the check's own captured logs/reason/exception, the originating
    `RuntimeCheck`'s own `required_evidence` (when known - `RuntimeCheckResult`
    itself carries no evidence-path field of its own), or `RepositoryContext`'s
    own known languages/frameworks/files/directories. A claim naming
    something never mentioned anywhere in the real input is rejected as
    unsupported, never silently accepted.
    """
    project = repository_context.project
    haystack_parts = [
        " ".join(check_result.logs),
        check_result.reason or "",
        check_result.details or "",
        check_result.exception or "",
        " ".join(runtime_check.required_evidence) if runtime_check is not None else "",
        " ".join(item.name for item in project.languages),
        " ".join(item.name for item in project.frameworks),
        " ".join(project.important_files),
        " ".join(project.important_directories),
        " ".join(project.runtime_files),
    ]
    haystack = " ".join(haystack_parts).lower()
    for claim in list(response.affected_files) + list(response.affected_components):
        if claim.lower() not in haystack:
            return False
    return True
