"""`RuntimeDiagnosis`: the structured output of the AI Runtime Failure
Diagnosis Engine (Phase G Part 3, docs/23-runtime-failure-diagnosis-engine
.md).

Lives in `qa_agent/ai/`, not `qa_agent/runtime/` - `qa_agent/runtime/`'s
entire identity, since Phase G Part 1, has been "deterministic only, no
AI," enforced by its own source-grep isolation tests. This module (and its
three siblings, `diagnosis_prompts.py`/`diagnosis_parser.py`/`diagnosis.py`)
is the one place that reads a `RuntimeCheckResult`/`RuntimeExecutionResult`
as *input data* - the same one-directional exception `validator.py`
(Phase E Part 3) already established for reading `runner.run`'s real
output, applied here to `qa_agent.runtime`'s output instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

DIAGNOSIS_DIAGNOSED = "diagnosed"
DIAGNOSIS_INSUFFICIENT_CONTEXT = "insufficient_context"
DIAGNOSIS_INVALID_RESPONSE = "invalid_response"
DIAGNOSIS_AI_ERROR = "ai_error"
DIAGNOSIS_NOT_APPLICABLE = "not_applicable"

DIAGNOSIS_STATUSES = (
    DIAGNOSIS_DIAGNOSED,
    DIAGNOSIS_INSUFFICIENT_CONTEXT,
    DIAGNOSIS_INVALID_RESPONSE,
    DIAGNOSIS_AI_ERROR,
    DIAGNOSIS_NOT_APPLICABLE,
)

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
SEVERITIES = (SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO)


@dataclass(frozen=True)
class RuntimeDiagnosis:
    """One check's diagnosis - always produced, one per diagnosed
    `RuntimeCheckResult`, even when no LLM call was made at all
    (`diagnosis_status=NOT_APPLICABLE` for a PASS/SKIPPED/NOT_IMPLEMENTED
    check) or the call failed in some structured way.

    `execution_status` mirrors the real `RuntimeCheckResult.status` this
    diagnosis is about - a plain string copy, not a reference, so a
    diagnosis remains meaningful on its own (rendered, serialized, or kept
    around after the originating `RuntimeExecutionResult` is gone).

    No separate `insufficient_context: bool` field - `diagnosis_status`
    already carries that state exactly (`DIAGNOSIS_INSUFFICIENT_CONTEXT`);
    a second, redundant field could only ever agree or silently disagree
    with the first, never add information (docs/22's own precedent for
    dropping a field a status value already makes redundant).
    """

    check_id: str
    check_name: str
    execution_status: str
    diagnosis_status: str
    summary: str = ""
    severity: str = ""
    confidence: float = 0.0
    observed_evidence: Tuple[str, ...] = ()
    likely_root_cause: str = ""
    affected_files: Tuple[str, ...] = ()
    affected_components: Tuple[str, ...] = ()
    recommended_action: str = ""
    model: str = ""
    error: Optional[str] = None

    def __post_init__(self):
        if self.diagnosis_status not in DIAGNOSIS_STATUSES:
            raise ValueError(
                "RuntimeDiagnosis({!r}) has an unrecognized diagnosis_status {!r}"
                .format(self.check_id, self.diagnosis_status)
            )
        if self.severity and self.severity not in SEVERITIES:
            raise ValueError(
                "RuntimeDiagnosis({!r}) has an unrecognized severity {!r}"
                .format(self.check_id, self.severity)
            )
