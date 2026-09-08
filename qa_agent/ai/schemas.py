"""Reusable schema objects for validated AI responses (Phase D Part 2).

These represent what a later part's explanation/summary/fix feature will
consume once it exists - the shapes are built now so response_parser.py has
something concrete to validate into. Nothing here calls a provider or
renders anything; that is explicitly a later Phase D part.
"""

from __future__ import annotations

from dataclasses import dataclass

# The three states a response_parser.py validator can produce for any
# response kind - shared constants so "success"/"insufficient_context"/
# "invalid" are never respelled differently in two places.
STATUS_SUCCESS = "success"
STATUS_INSUFFICIENT_CONTEXT = "insufficient_context"
STATUS_INVALID = "invalid"


@dataclass(frozen=True)
class ExplanationResponse:
    """A validated explanation of exactly one supplied finding."""

    explanation: str


@dataclass(frozen=True)
class SummaryResponse:
    """A validated summary of a whole run's supplied findings."""

    summary: str


@dataclass(frozen=True)
class FixResponse:
    """A validated suggested fix for exactly one supplied finding."""

    explanation: str
    suggested_fix: str


@dataclass(frozen=True)
class RepairResponse:
    """A validated repair proposal for exactly one supplied finding
    (Phase E Part 1) - `build_repair_prompt`'s counterpart to `FixResponse`,
    kept as a separate shape rather than added onto `FixResponse` because a
    repair proposal needs two fields a `SuggestedFix` does not: a
    self-reported `confidence`, and the line range the replacement covers.
    `start_line`/`end_line` are the model's own claim, unvalidated against
    the real file here - this module only checks shape (present and an
    int, or absent); repair.py decides whether to trust them.
    """

    explanation: str
    replacement: str
    confidence: float
    start_line: int | None = None
    end_line: int | None = None


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of parsing and validating one raw LLM response.

    Three distinct states, not a plain ok/error pair - `ok=False` alone
    cannot tell an explicit "insufficient context" decline apart from a
    malformed response, and later parts need to react to those two cases
    differently (docs/step-log.md, Phase D Part 2 - anti-hallucination
    guardrails: a model that lacks enough context must say so, not guess).
    """

    status: str
    value: object = None  # an Explanation/Summary/FixResponse, only when status is success
    reason: str | None = None  # why, when status is not success

    @property
    def ok(self):
        return self.status == STATUS_SUCCESS
