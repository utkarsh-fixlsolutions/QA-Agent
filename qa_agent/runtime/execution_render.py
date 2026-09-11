"""Presentation for a `RuntimeExecutionResult` (Phase G Part 2, docs/22
-runtime-execution-engine.md) - plain-text (CLI), Markdown, and JSON/dict.
Mirrors `render.py`/`execution_render.py`'s siblings one layer up: this
module only ever formats an already-finished `RuntimeExecutionResult`,
never runs anything itself.
"""

from __future__ import annotations

import json


def _result_to_dict(result):
    return {
        "id": result.id,
        "name": result.name,
        "status": result.status,
        "start_time": result.start_time,
        "end_time": result.end_time,
        "duration": result.duration,
        "reason": result.reason,
        "details": result.details,
        "logs": list(result.logs),
        "artifacts": list(result.artifacts),
        "exception": result.exception,
        "retryable": result.retryable,
    }


def to_dict(execution_result):
    return {
        "repository_root": execution_result.plan.context.project.root_path,
        "started_at": execution_result.started_at,
        "finished_at": execution_result.finished_at,
        "total_duration": execution_result.total_duration,
        "results": [_result_to_dict(r) for r in execution_result.results],
    }


def to_json(execution_result, indent=2):
    return json.dumps(to_dict(execution_result), indent=indent, sort_keys=False)


_STATUS_SYMBOLS = {
    "pass": "PASS", "fail": "FAIL", "skipped": "SKIP",
    "timeout": "TIME", "error": "ERR ", "not_implemented": "N/I ",
}


def render(execution_result):
    lines = ["Execution Results", ""]
    if not execution_result.results:
        lines.append("  No checks were executed - the plan had none to run.")
        return "\n".join(lines).rstrip() + "\n"

    counts = {}
    for result in execution_result.results:
        symbol = _STATUS_SYMBOLS.get(result.status, result.status.upper())
        lines.append("  [{}] {}  ({:.2f}s)".format(symbol, result.name, result.duration))
        lines.append("        {}".format(result.reason))
        if result.logs:
            lines.append("        last output: {}".format(result.logs[-1].strip()))
        lines.append("")
        counts[result.status] = counts.get(result.status, 0) + 1

    summary = ", ".join("{} {}".format(count, status) for status, count in sorted(counts.items()))
    lines.append("  {} check(s) executed - {}. Total time: {:.2f}s".format(
        len(execution_result.results), summary, execution_result.total_duration
    ))
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(execution_result):
    lines = ["# Execution Results", ""]
    lines.append("- **Repository:** {}".format(execution_result.plan.context.project.root_path))
    lines.append("- **Started:** {}".format(execution_result.started_at))
    lines.append("- **Total duration:** {:.2f}s".format(execution_result.total_duration))
    lines.append("")
    if not execution_result.results:
        lines.append("No checks were executed.")
        return "\n".join(lines).rstrip() + "\n"

    lines.append("| Check | Status | Duration | Reason |")
    lines.append("| --- | --- | --- | --- |")
    for result in execution_result.results:
        lines.append("| {} | {} | {:.2f}s | {} |".format(
            result.name, result.status, result.duration, result.reason
        ))
    return "\n".join(lines).rstrip() + "\n"
