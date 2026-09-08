"""Parsing and validating raw LLM text into the schema objects in
schemas.py (Phase D Part 2).

Independent of the provider and of reporting: this module only ever sees a
raw string (whatever an LLMResponse.text happened to be) and returns a
structured ValidationResult - never raises, matching every "did it work,
what happened" boundary already used throughout this project (ToolError in
RunResult, AnalysisOutcome, LLMResponse).
"""

from __future__ import annotations

import json
import re

from .schemas import (
    STATUS_INSUFFICIENT_CONTEXT,
    STATUS_INVALID,
    STATUS_SUCCESS,
    ExplanationResponse,
    FixResponse,
    RepairResponse,
    SummaryResponse,
    ValidationResult,
)

# ```json ... ``` or bare ``` ... ``` , searched for anywhere in the text
# (not anchored) - a model told to "respond with JSON only" realistically
# still wraps it in a fence, and sometimes adds a stray sentence before or
# after it despite being told not to.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def strip_markdown_fence(text: str) -> str:
    """Remove a ```json / ``` fence if present anywhere in `text`;
    otherwise return the text unchanged (a model that already replied with
    bare JSON is not an error).
    """
    stripped = text.strip()
    match = _FENCE_RE.search(stripped)
    return match.group(1).strip() if match else stripped


def parse_json_response(text: str):
    """Fence-strip, then json.loads. Returns (data, error): `data` is the
    parsed value on success, `error` is a clear string on failure. Never
    raises - the caller decides what to do with a failure.
    """
    candidate = strip_markdown_fence(text)
    if not candidate:
        return None, "empty response"
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError as exc:
        return None, "not valid JSON: {}".format(exc)


def _is_insufficient_context(data) -> bool:
    """True if the model used the sanctioned decline shape rather than the
    requested schema (docs/step-log.md, Phase D Part 2 - anti-hallucination
    guardrails).
    """
    return isinstance(data, dict) and data.get("insufficient_context") is True


def _reason(data, default: str) -> str:
    if isinstance(data, dict) and isinstance(data.get("reason"), str) and data["reason"].strip():
        return data["reason"]
    return default


def _parse_and_check_common(text: str):
    """The shared prefix every validator below needs: parse JSON, check for
    the sanctioned insufficient-context decline, check the result is an
    object at all. Returns (data, early_result) - `early_result` is a
    completed ValidationResult when validation must stop here (a parse
    error, an insufficient-context decline, or a non-object response);
    otherwise it is None and `data` is ready for the caller's own
    required-field checks.
    """
    data, error = parse_json_response(text)
    if error is not None:
        return None, ValidationResult(status=STATUS_INVALID, reason=error)
    if _is_insufficient_context(data):
        return None, ValidationResult(
            status=STATUS_INSUFFICIENT_CONTEXT,
            reason=_reason(data, "model reported insufficient context"),
        )
    if not isinstance(data, dict):
        return None, ValidationResult(
            status=STATUS_INVALID, reason="response is not a JSON object"
        )
    return data, None


def _required_string_fields(data, *names):
    """The names among `names` that are missing, not a string, or blank."""
    return [
        name for name in names
        if not isinstance(data.get(name), str) or not data[name].strip()
    ]


def validate_explanation_response(text: str) -> ValidationResult:
    """Validate a raw response against the explanation schema:
    `{"explanation": "<text>"}`.
    """
    data, early = _parse_and_check_common(text)
    if early is not None:
        return early
    assert data is not None  # _parse_and_check_common: early is None implies data is not
    missing = _required_string_fields(data, "explanation")
    if missing:
        return ValidationResult(
            status=STATUS_INVALID,
            reason="missing or empty required field(s): {}".format(", ".join(missing)),
        )
    return ValidationResult(
        status=STATUS_SUCCESS, value=ExplanationResponse(explanation=data["explanation"])
    )


def validate_summary_response(text: str) -> ValidationResult:
    """Validate a raw response against the summary schema:
    `{"summary": "<text>"}`.
    """
    data, early = _parse_and_check_common(text)
    if early is not None:
        return early
    assert data is not None  # _parse_and_check_common: early is None implies data is not
    missing = _required_string_fields(data, "summary")
    if missing:
        return ValidationResult(
            status=STATUS_INVALID,
            reason="missing or empty required field(s): {}".format(", ".join(missing)),
        )
    return ValidationResult(status=STATUS_SUCCESS, value=SummaryResponse(summary=data["summary"]))


def validate_fix_response(text: str) -> ValidationResult:
    """Validate a raw response against the fix schema:
    `{"explanation": "<text>", "suggested_fix": "<text>"}`.
    """
    data, early = _parse_and_check_common(text)
    if early is not None:
        return early
    assert data is not None  # _parse_and_check_common: early is None implies data is not
    missing = _required_string_fields(data, "explanation", "suggested_fix")
    if missing:
        return ValidationResult(
            status=STATUS_INVALID,
            reason="missing or empty required field(s): {}".format(", ".join(missing)),
        )
    return ValidationResult(
        status=STATUS_SUCCESS,
        value=FixResponse(
            explanation=data["explanation"], suggested_fix=data["suggested_fix"]
        ),
    )


def _is_number_not_bool(value) -> bool:
    """`bool` is an `int` subclass in Python - a bare `isinstance(x, (int,
    float))` would silently accept `"confidence": true` as `1`. The same
    check config.py already applies to `ai.timeout` (Phase D Part 6).
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_optional_line(value) -> bool:
    """`start_line`/`end_line` are optional (Phase E Part 1: "if
    available") - absent is fine, present must be a real int.
    """
    return value is None or (isinstance(value, int) and not isinstance(value, bool))


def validate_repair_response(text: str) -> ValidationResult:
    """Validate a raw response against the repair-proposal schema:
    `{"explanation": "<text>", "replacement": "<text>", "confidence": <0.0-1.0>,
    "start_line": <int, optional>, "end_line": <int, optional>}`.

    `confidence` is required (a repair proposal without a self-reported
    confidence is not a usable proposal for E1), but a value outside
    [0.0, 1.0] is clamped rather than rejected - the shape is what must be
    right, not a model's precision picking a number. `start_line`/
    `end_line` may be omitted entirely; repair.py falls back to the
    finding's own line when they are.
    """
    data, early = _parse_and_check_common(text)
    if early is not None:
        return early
    assert data is not None  # _parse_and_check_common: early is None implies data is not
    missing = _required_string_fields(data, "explanation", "replacement")
    if missing:
        return ValidationResult(
            status=STATUS_INVALID,
            reason="missing or empty required field(s): {}".format(", ".join(missing)),
        )
    if "confidence" not in data or not _is_number_not_bool(data["confidence"]):
        return ValidationResult(
            status=STATUS_INVALID,
            reason="missing or invalid required field: 'confidence' (must be a number)",
        )
    start_line, end_line = data.get("start_line"), data.get("end_line")
    if not _valid_optional_line(start_line) or not _valid_optional_line(end_line):
        return ValidationResult(
            status=STATUS_INVALID,
            reason="'start_line'/'end_line', when present, must be integers",
        )
    return ValidationResult(
        status=STATUS_SUCCESS,
        value=RepairResponse(
            explanation=data["explanation"],
            replacement=data["replacement"],
            confidence=max(0.0, min(1.0, float(data["confidence"]))),
            start_line=start_line,
            end_line=end_line,
        ),
    )
