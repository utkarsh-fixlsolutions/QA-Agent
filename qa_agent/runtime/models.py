"""Immutable data shapes for the Runtime QA Planning Engine (Phase G Part 1,
docs/21-runtime-qa-planning-engine.md).

A `RuntimeCheck` describes *what should be tested and why* - never *how*.
Nothing in this package executes anything: no HTTP, no subprocess, no
browser. `required_evidence` is never empty - the same "no fact without a
real file behind it" contract `DetectedItem` (qa_agent/project/models.py)
already enforces, applied here to "why this check is worth planning"
instead of "why this fact is true". See rules.py's own module docstring
for the one deliberate philosophical distinction that requires.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from ..project.context import RepositoryContext

PRIORITY_CRITICAL = "critical"
PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITIES = (PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW)

COST_LOW = "low"
COST_MEDIUM = "medium"
COST_HIGH = "high"
COSTS = (COST_LOW, COST_MEDIUM, COST_HIGH)

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISKS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)


@dataclass(frozen=True)
class RuntimeCheck:
    """One planned check. `id` is a stable, kebab-case identifier - the same
    check for the same kind of evidence always gets the same `id`, so a
    `dependencies` reference or a re-run's comparison never breaks on
    wording changes to `name`/`reason`.

    `recommended_order` is filled in by `planner.py` after every rule has
    run (a deterministic topological sort over `dependencies`) - every rule
    function in rules.py leaves it at its default `0`.
    """

    id: str
    name: str
    category: str
    priority: str
    reason: str
    required_evidence: Tuple[str, ...]
    dependencies: Tuple[str, ...] = ()
    estimated_cost: str = COST_MEDIUM
    risk_level: str = RISK_MEDIUM
    recommended_order: int = 0
    blocking: bool = False
    expected_result: str = ""

    def __post_init__(self):
        if not self.required_evidence:
            raise ValueError(
                "RuntimeCheck({!r}) constructed with no required_evidence - "
                "every planned check must be backed by real evidence".format(self.id)
            )
        if self.priority not in PRIORITIES:
            raise ValueError(
                "RuntimeCheck({!r}) has an unrecognized priority {!r}".format(self.id, self.priority)
            )
        if self.estimated_cost not in COSTS:
            raise ValueError(
                "RuntimeCheck({!r}) has an unrecognized estimated_cost {!r}".format(self.id, self.estimated_cost)
            )
        if self.risk_level not in RISKS:
            raise ValueError(
                "RuntimeCheck({!r}) has an unrecognized risk_level {!r}".format(self.id, self.risk_level)
            )


@dataclass(frozen=True)
class RuntimeQAPlan:
    """References the `RepositoryContext` it was built from rather than
    duplicating any of its fields - the same pattern `RepositoryContext`
    itself already established for `ProjectKnowledge` in Phase F Part 2.
    """

    context: RepositoryContext
    checks: Tuple[RuntimeCheck, ...] = ()
    generated_at: str = ""
