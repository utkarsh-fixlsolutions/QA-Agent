"""Runtime QA Planning Engine (Phase G Part 1). See
docs/21-runtime-qa-planning-engine.md and docs/step-log.md.

Deterministic only - no AI, no HTTP, no browser, no subprocess, no network,
no filesystem walking of its own. Consumes a `qa_agent.project`
`RepositoryContext` and produces a `RuntimeQAPlan` naming what should be
tested, why, in what order, and with what dependencies - never executing
anything. `qa_agent.ai` and the deterministic analyzer pipeline never
import this package; this package imports only `qa_agent.project` (its
one, one-directional input).

Public API: `plan_runtime_qa(context)` -> `RuntimeQAPlan`.
"""

from .models import (
    COST_HIGH,
    COST_LOW,
    COST_MEDIUM,
    PRIORITIES,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RuntimeCheck,
    RuntimeQAPlan,
)
from .planner import plan_runtime_qa
from .render import render, render_markdown
from .render import to_dict as plan_to_dict
from .render import to_json as plan_to_json

__all__ = [
    "COST_HIGH",
    "COST_LOW",
    "COST_MEDIUM",
    "PRIORITIES",
    "PRIORITY_CRITICAL",
    "PRIORITY_HIGH",
    "PRIORITY_LOW",
    "PRIORITY_MEDIUM",
    "RISK_HIGH",
    "RISK_LOW",
    "RISK_MEDIUM",
    "RuntimeCheck",
    "RuntimeQAPlan",
    "plan_runtime_qa",
    "plan_to_dict",
    "plan_to_json",
    "render",
    "render_markdown",
]
