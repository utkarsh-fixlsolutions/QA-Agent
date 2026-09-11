"""Result shapes for Runtime Failure -> Verified Repair Integration (Phase G
Part 4, docs/24-runtime-repair-integration.md).

Lives in `qa_agent/ai/`, alongside `diagnosis_models.py` - the same
one-directional exception `validator.py`/`diagnosis.py` already established
for reading the deterministic engines' own output as plain input data.
Nothing here executes anything; these are plain result dataclasses only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# --- eligibility -----------------------------------------------------------

ELIGIBILITY_ELIGIBLE = "eligible"
ELIGIBILITY_NOT_ELIGIBLE = "not_eligible"


@dataclass(frozen=True)
class TargetResolution:
    """The outcome of deterministically resolving one `RuntimeDiagnosis`'s
    `affected_files` claim into a precise, real repair target - never a
    guess. `ok=False` means no safe target could be established (missing,
    ambiguous, outside the repository, or a generated/dependency artifact);
    `reason` explains why, and no `line` is ever invented in that case.

    `ok=True`: `file` is the resolved, real, absolute path; `relative_file`
    is the diagnosis's own original claim (kept for display); `line` is a
    real line number the deterministic evidence search actually matched, or
    `1` as an honest, un-invented anchor when no match was found -
    `localized` distinguishes the two so a caller never mistakes an anchor
    for a real match.
    """

    ok: bool
    reason: str
    file: Optional[str] = None
    relative_file: Optional[str] = None
    line: int = 0
    localized: bool = False


@dataclass(frozen=True)
class EligibilityResult:
    """A deterministic verdict on whether one `RuntimeDiagnosis` may enter
    repair at all - computed entirely from already-produced structured data
    (the diagnosis, the real `RuntimeCheckResult`, the real repository on
    disk). The AI is never consulted to make this decision.
    """

    eligible: bool
    reason: str
    target: Optional[TargetResolution] = None


# --- overall repair outcome -------------------------------------------------

OUTCOME_NOT_ELIGIBLE = "not_eligible"
OUTCOME_PROPOSAL_FAILED = "proposal_failed"
OUTCOME_VALIDATION_FAILED = "validation_failed"
OUTCOME_REJECTED = "rejected"
OUTCOME_HELD = "held"
OUTCOME_ACCEPTED = "accepted"
OUTCOME_APPLIED = "applied"
OUTCOME_VERIFIED = "verified"
OUTCOME_APPLIED_BUT_STILL_FAILING = "applied_but_still_failing"
OUTCOME_ERROR = "error"

OUTCOMES = (
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_PROPOSAL_FAILED,
    OUTCOME_VALIDATION_FAILED,
    OUTCOME_REJECTED,
    OUTCOME_HELD,
    OUTCOME_ACCEPTED,
    OUTCOME_APPLIED,
    OUTCOME_VERIFIED,
    OUTCOME_APPLIED_BUT_STILL_FAILING,
    OUTCOME_ERROR,
)

APPLY_NOT_ATTEMPTED = "not_attempted"

VERIFICATION_NOT_APPLICABLE = "not_applicable"
VERIFICATION_VERIFIED = "verified"
VERIFICATION_STILL_FAILING = "still_failing"
VERIFICATION_UNKNOWN = "unknown"


@dataclass(frozen=True)
class RuntimeRepairResult:
    """The complete outcome of one G4 repair attempt for one diagnosed
    runtime failure - always exactly one attempt (docs/24's own bounded-
    attempts rule), never retried or re-proposed within this result.

    `outcome` is the single authoritative terminal status, one of the
    `OUTCOME_*` constants above - `ACCEPTED` and `VERIFIED` are always
    distinct (docs/24's own central rule): `ACCEPTED` means Phase E's
    deterministic decision approved the candidate; only `VERIFIED` means
    the real runtime check actually passed after a real repository write.

    `eligibility`/`target_resolution`: the deterministic gate's own verdict
    - `target_resolution` is `None` only when `eligibility.eligible` is
    `False` for a reason that never reached target resolution (e.g. no
    diagnosis at all).
    `repair_proposal`: the AI's structured proposal (Phase E Part 1's own
    `RepairProposal` shape, unmodified), or `None` if none was produced.
    `applied_repair`: the Phase E Part 2 `AppliedRepair` inside the
    temporary workspace, or `None`.
    `validation_result`: Phase E Part 3's own `RepairValidationResult`
    (static analyzer rerun), or `None`.
    `runtime_candidate_status`: the *candidate* runtime check's own status
    string, from re-running the check against a patched copy materialized
    inside the temporary workspace - `None` when materialization was not
    possible in this environment (reported honestly, never guessed past).
    `deterministic_decision`: Phase E Part 4's own `RepairDecision`,
    unmodified - the AI never makes this decision.
    `apply_status`: `APPLY_NOT_ATTEMPTED`, or Phase E Part 5's own
    `RepairApplicationResult.reason` once a real write was attempted.
    `apply_result`: Phase E Part 5's own `RepairApplicationResult`, or
    `None`.
    `final_runtime_status`: the *real* runtime check's status, re-run
    against the real repository after a real write - the one and only
    signal `verification_status` is computed from.
    `verification_status`: `VERIFICATION_VERIFIED` only when
    `final_runtime_status` is a real `pass`; `VERIFICATION_STILL_FAILING`
    when the real re-run completed but did not pass;
    `VERIFICATION_UNKNOWN` when a real write happened but the re-run itself
    could not be completed (never silently reported as verified);
    `VERIFICATION_NOT_APPLICABLE` whenever no real write happened at all.
    `affected_files`: the diagnosis's own original claim, carried through
    for display regardless of what was actually resolved.
    `explanation`: a short, human-readable summary of what happened and why
    - built entirely from this result's own fields, never AI-generated text.
    `error`: set only for `OUTCOME_ERROR` - an unexpected failure, never a
    normal decision outcome.
    """

    check_id: str
    check_name: str
    original_runtime_status: str
    diagnosis_status: str
    outcome: str
    eligibility: EligibilityResult
    target_resolution: Optional[TargetResolution] = None
    repair_proposal: object = None
    applied_repair: object = None
    validation_result: object = None
    runtime_candidate_status: Optional[str] = None
    deterministic_decision: object = None
    apply_status: str = APPLY_NOT_ATTEMPTED
    apply_result: object = None
    final_runtime_status: Optional[str] = None
    verification_status: str = VERIFICATION_NOT_APPLICABLE
    affected_files: Tuple[str, ...] = ()
    explanation: str = ""
    error: Optional[str] = None

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(
                "RuntimeRepairResult({!r}) has an unrecognized outcome {!r}"
                .format(self.check_id, self.outcome)
            )
