"""Runtime QA Planning & Execution Engine (Phase G Parts 1-2). See
docs/21-runtime-qa-planning-engine.md, docs/22-runtime-execution-engine.md,
and docs/step-log.md.

Part 1 (`plan_runtime_qa`) is deterministic and never executes anything -
no AI, no HTTP, no browser, no subprocess, no network, no filesystem
walking of its own. Part 2 (`run_runtime_plan`) is the one place in this
package that actually launches real processes - still no AI, no HTTP, no
browser; every command it runs is real, evidence-based, and always
terminated, never left running. `qa_agent.ai` and the deterministic
analyzer pipeline never import this package; this package imports only
`qa_agent.project` (its one, one-directional input).

Public API: `plan_runtime_qa(context)` -> `RuntimeQAPlan` (Part 1);
`run_runtime_plan(plan, root, configuration=None)` -> `RuntimeExecutionResult`
(Part 2).
"""

from .execution_errors import CheckExecutionError, CheckSkipped
from .execution_models import (
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_NOT_IMPLEMENTED,
    STATUS_PASS,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    RuntimeCheckResult,
)
from .execution_render import render as render_execution
from .execution_render import render_markdown as render_execution_markdown
from .execution_render import to_dict as execution_to_dict
from .execution_render import to_json as execution_to_json
from .execution_result import RuntimeExecutionResult
from .executor import DEFAULT_CONFIG, ExecutionConfig, run_runtime_plan
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
    "DEFAULT_CONFIG",
    "CheckExecutionError",
    "CheckSkipped",
    "ExecutionConfig",
    "PRIORITIES",
    "PRIORITY_CRITICAL",
    "PRIORITY_HIGH",
    "PRIORITY_LOW",
    "PRIORITY_MEDIUM",
    "RISK_HIGH",
    "RISK_LOW",
    "RISK_MEDIUM",
    "RuntimeCheck",
    "RuntimeCheckResult",
    "RuntimeExecutionResult",
    "RuntimeQAPlan",
    "STATUS_ERROR",
    "STATUS_FAIL",
    "STATUS_NOT_IMPLEMENTED",
    "STATUS_PASS",
    "STATUS_SKIPPED",
    "STATUS_TIMEOUT",
    "execution_to_dict",
    "execution_to_json",
    "plan_runtime_qa",
    "plan_to_dict",
    "plan_to_json",
    "render",
    "render_execution",
    "render_execution_markdown",
    "render_markdown",
    "run_runtime_plan",
]
