"""Presentation for a `RuntimeQAPlan` (Phase G Part 1, docs/21-runtime-qa
-planning-engine.md) - plain-text (CLI), Markdown, and JSON/dict. Mirrors
`qa_agent/project/knowledge.py` and `context_builder.py`'s own role one
layer up: this module only ever formats an already-built `RuntimeQAPlan`,
never computes one.
"""

from __future__ import annotations

import json


def _check_to_dict(check):
    return {
        "id": check.id,
        "name": check.name,
        "category": check.category,
        "priority": check.priority,
        "reason": check.reason,
        "required_evidence": list(check.required_evidence),
        "dependencies": list(check.dependencies),
        "estimated_cost": check.estimated_cost,
        "risk_level": check.risk_level,
        "recommended_order": check.recommended_order,
        "blocking": check.blocking,
        "expected_result": check.expected_result,
    }


def to_dict(plan):
    """A plain, JSON-safe `dict` - `context` is deliberately summarized (its
    own `root_path`/`repository_type`/`application_type`), not fully
    inlined via `context_to_dict()`, to keep a rendered plan focused on the
    plan itself rather than re-printing everything Part 1/2 already show.
    """
    return {
        "repository_root": plan.context.project.root_path,
        "repository_type": plan.context.project.repository_type,
        "application_type": plan.context.project.application_type,
        "generated_at": plan.generated_at,
        "checks": [_check_to_dict(c) for c in plan.checks],
    }


def to_json(plan, indent=2):
    return json.dumps(to_dict(plan), indent=indent, sort_keys=False)


def render(plan):
    """Plain-text rendering for the `discover --runtime-plan` CLI flag,
    ordered by each check's own `recommended_order` (already the plan's
    own tuple order - `plan_runtime_qa()` never returns checks in any
    other order).
    """
    lines = ["Runtime QA Plan", ""]
    if not plan.checks:
        lines.append("  No runtime checks could be planned - insufficient evidence.")
        lines.append("  Generated at: {}".format(plan.generated_at))
        return "\n".join(lines).rstrip() + "\n"

    for check in plan.checks:
        lines.append("  [{:>2}] {} ({})".format(check.recommended_order, check.name, check.category))
        lines.append("       priority: {}  cost: {}  risk: {}  blocking: {}".format(
            check.priority, check.estimated_cost, check.risk_level, check.blocking
        ))
        lines.append("       reason: {}".format(check.reason))
        if check.dependencies:
            lines.append("       depends on: {}".format(", ".join(check.dependencies)))
        lines.append("       expected: {}".format(check.expected_result))
        lines.append("")
    lines.append("  {} check(s) planned. Generated at: {}".format(len(plan.checks), plan.generated_at))
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(plan):
    """Markdown rendering - same content as `render()`, table-shaped, for
    a future `--output`-style consumer (none exists yet; this exists
    because docs/21's own testing requirements ask for it directly).
    """
    lines = ["# Runtime QA Plan", ""]
    lines.append("- **Repository:** {}".format(plan.context.project.root_path))
    lines.append("- **Generated at:** {}".format(plan.generated_at))
    lines.append("")
    if not plan.checks:
        lines.append("No runtime checks could be planned - insufficient evidence.")
        return "\n".join(lines).rstrip() + "\n"

    lines.append("| # | Check | Category | Priority | Risk | Blocking | Reason |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for check in plan.checks:
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            check.recommended_order, check.name, check.category, check.priority,
            check.risk_level, "yes" if check.blocking else "no", check.reason,
        ))
    return "\n".join(lines).rstrip() + "\n"
