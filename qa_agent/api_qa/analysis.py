"""Deterministic post-hoc analysis of already-real results (docs/43-negative
-tests-schema-validation-severity.md): severity classification and plain-
English "expected vs actual" text, for `ApiCallResult`/`NegativeCallResult`/
`SchemaValidationResult`. Pure functions only - no I/O, no AI.

Deliberately AI-independent, mirroring the Test Plan's own deterministic
rule table (`web/server.py`'s `_TEST_CASE_RULES`): severity and expected/
actual must be available every time, with or without an AI provider
configured, so they never depend on `GROQ_API_KEY`/Ollama/OpenRouter being
set up. The existing AI diagnosis feature (`ai_bridge.py`) is unrelated and
unaffected - it still runs as an optional, additional explanation layer on
top of a `CALL_FAIL` result; this module never calls it and never competes
with it.

Severity uses its own, distinct name (`API_SEVERITY_*`) rather than reusing
`RuntimeDiagnosis.severity` (`error`/`warning`/`info`, AI-assigned) or
`qa_agent.runtime.models.PRIORITY_*` (a different concept - deterministic
check-*planning* priority, unrelated to a live API call's outcome) - both
already exist under those names for their own, different purposes.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .models import CALL_FAIL, CALL_PASS, CALL_SKIPPED, ApiCallResult

API_SEVERITY_CRITICAL = "critical"
API_SEVERITY_HIGH = "high"
API_SEVERITY_MEDIUM = "medium"
API_SEVERITY_LOW = "low"

API_SEVERITIES = (API_SEVERITY_CRITICAL, API_SEVERITY_HIGH, API_SEVERITY_MEDIUM, API_SEVERITY_LOW)

_MAX_ACTUAL_SAMPLE_CHARS = 200

# Final, user-facing classification (docs/48-fail-fast-and-classification.md)
# - a closed set of exactly four labels every call is shown under, never a
# fifth. Deliberately distinct from `status` (`CALL_PASS`/`CALL_FAIL`/
# `CALL_SKIPPED`, this package's own internal result shape) and from
# severity above - this is the report-facing summary a QA engineer reads
# first, `status`/severity remain the underlying evidence it's built from.
CLASSIFICATION_WORKING = "Working"
CLASSIFICATION_FAILING = "Failing"
CLASSIFICATION_NOT_WORKING = "Not Working"
CLASSIFICATION_SKIPPED = "Skipped"

CLASSIFICATIONS = (
    CLASSIFICATION_WORKING, CLASSIFICATION_FAILING, CLASSIFICATION_NOT_WORKING, CLASSIFICATION_SKIPPED,
)


def classify_call_outcome(call: ApiCallResult) -> str:
    """One of the four `CLASSIFICATION_*` labels above - every real or
    skipped call gets exactly one, never zero, never more than one.

    - `CLASSIFICATION_SKIPPED`: never actually attempted (`CALL_SKIPPED`) -
      no real evidence exists to judge it by at all.
    - `CLASSIFICATION_WORKING`: a real 2xx, with a valid body when one was
      declared (`CALL_PASS`) - passed the check this package makes.
    - `CLASSIFICATION_NOT_WORKING`: reached the target's own real
      infrastructure but never got a real, complete response from it - no
      response at all (`status_code is None`: connection refused, timeout,
      DNS failure) or a real 5xx (the target received the request and
      itself crashed handling it). The same real-world distinction
      `classify_call_severity`'s own CRITICAL bucket already draws.
    - `CLASSIFICATION_FAILING`: reachable, and it responded - just not the
      way this package's own simple pass rule expects (a 4xx, an
      unexpected 3xx, or a declared-JSON body that didn't actually parse).
    """
    if call.status == CALL_SKIPPED:
        return CLASSIFICATION_SKIPPED
    if call.status == CALL_PASS:
        return CLASSIFICATION_WORKING
    if call.status_code is None or call.status_code >= 500:
        return CLASSIFICATION_NOT_WORKING
    return CLASSIFICATION_FAILING


def classify_call_severity(call: ApiCallResult) -> Optional[str]:
    """Severity only means something for a real problem - `None` for a
    `CALL_PASS`/`CALL_SKIPPED` result, always. For `CALL_FAIL`: CRITICAL
    when no response was ever received at all (`status_code is None` - a
    connection refused, DNS failure, or timeout: the target is unreachable
    or not running) or a real 5xx (the target received the request and
    itself crashed handling it); MEDIUM for a 4xx (reachable, but rejected
    the request); LOW for anything else this rule table doesn't have a
    sharper bucket for (e.g. an unexpected 3xx counted as a failure) -
    never silently unclassified.
    """
    if call.status != CALL_FAIL:
        return None
    if call.status_code is None or call.status_code >= 500:
        return API_SEVERITY_CRITICAL
    if 400 <= call.status_code < 500:
        return API_SEVERITY_MEDIUM
    return API_SEVERITY_LOW


def classify_negative_case_severity(status: str, actual_status_code: Optional[int]) -> Optional[str]:
    """For a negative test case, `CALL_PASS` (the target correctly rejected
    bad input) never has a severity - that's the target working correctly.
    A `CALL_FAIL` here means the target either silently accepted invalid
    input (HIGH - a real validation gap, not a crash) or crashed handling
    it (CRITICAL - worse than a validation gap: an unhandled exception on
    input any client could send).
    """
    if status != CALL_FAIL:
        return None
    if actual_status_code is not None and actual_status_code >= 500:
        return API_SEVERITY_CRITICAL
    return API_SEVERITY_HIGH


def classify_schema_validation_severity(status: str) -> Optional[str]:
    """A schema mismatch (`CALL_FAIL` here) is a real contract violation -
    the response the target actually sent does not match what it itself
    declares it returns - but not a crash, so MEDIUM; `CALL_PASS`/
    `CALL_SKIPPED` never carry a severity.
    """
    if status != CALL_FAIL:
        return None
    return API_SEVERITY_MEDIUM


def _truncate(text: str, limit: int = _MAX_ACTUAL_SAMPLE_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def expected_actual(call: ApiCallResult) -> Tuple[Optional[str], Optional[str]]:
    """Plain-English `(expected, actual)` strings built only from fields
    `call` already really carries - `status_code`/`error`/`response_sample`
    - never a second HTTP call, never fabricated. `(None, None)` for a
    `CALL_SKIPPED` result: nothing was ever actually attempted, so there is
    no real "actual" to report - the existing `call.reason` already
    explains the skip, and this module never duplicates or second-guesses it.
    """
    if call.status == CALL_PASS:
        expected = "a 2xx response"
        actual = "{} {}".format(call.status_code, (call.content_type or "").split(";")[0]).strip()
        return expected, actual

    if call.status == CALL_FAIL:
        expected = "a 2xx response"
        if call.status_code is None:
            return "a reachable, responding server", _truncate(call.error) or "no response was received"
        actual = "{} response".format(call.status_code)
        detail = _truncate(call.response_sample) or _truncate(call.error)
        if detail:
            actual = "{} - {}".format(actual, detail)
        return expected, actual

    return None, None
