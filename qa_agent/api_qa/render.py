"""Presentation for an `ApiTestResult` (docs/30-api-qa-v1.md) - plain text
(CLI) and JSON/dict. Mirrors `runtime/execution_render.py`'s own role for
`RuntimeExecutionResult`: this module only ever formats an already-finished
result, never runs anything itself.
"""

from __future__ import annotations

import json

_STATUS_SYMBOLS = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}


def _status_label(call):
    if call.status_code is not None:
        return str(call.status_code)
    return "-"


def _endpoint_column(call):
    return "{} {}".format(call.endpoint.method, call.endpoint.path)


def render(result):
    lines = ["API QA Results", "-" * len("API QA Results"), ""]
    lines.append("  Root:          {}".format(result.root_path))
    lines.append("  Server status: {}{}".format(
        result.server_status, " ({})".format(result.server_detail) if result.server_detail else "",
    ))
    if result.base_url:
        lines.append("  Base URL:      {}".format(result.base_url))
    lines.append("  Endpoints:     {} discovered".format(len(result.endpoints)))
    lines.append("")

    if result.warnings:
        lines.append("  Warnings:")
        lines.extend("    - {}".format(w) for w in result.warnings)
        lines.append("")

    if not result.calls:
        lines.append("  No endpoint calls were made.")
        return "\n".join(lines).rstrip() + "\n"

    endpoint_width = max(len(_endpoint_column(c)) for c in result.calls) + 2
    status_width = max(len(_status_label(c)) for c in result.calls) + 2

    counts = {}
    for call in result.calls:
        symbol = _STATUS_SYMBOLS.get(call.status, call.status.upper())
        timing = "({:.0f}ms)".format(call.response_time_ms) if call.response_time_ms is not None else "(-)"
        lines.append("  {:<{ew}} -> {:<{sw}} {:<8} {}".format(
            _endpoint_column(call), _status_label(call), timing, symbol,
            ew=endpoint_width, sw=status_width,
        ))
        if call.resolved_path:
            lines.append("        Concrete request: {} {}".format(call.endpoint.method, call.resolved_path))
        if call.status != "pass":
            evidence = call.error or call.reason
            if evidence:
                lines.append("        {}".format(evidence))
            if call.response_sample:
                lines.append("        body: {}".format(call.response_sample[:200]))
        if call.resolution_evidence:
            lines.append("        evidence: {}".format(call.resolution_evidence))
        counts[call.status] = counts.get(call.status, 0) + 1

    lines.append("")
    summary = ", ".join("{} {}".format(count, status) for status, count in sorted(counts.items()))
    lines.append("  {} call(s) - {}. Total time: {:.2f}s".format(
        len(result.calls), summary, result.total_duration,
    ))
    return "\n".join(lines).rstrip() + "\n"


def _call_to_dict(call):
    return {
        "method": call.endpoint.method,
        "path": call.endpoint.path,
        "source_file": call.endpoint.source_file,
        "dynamic": call.endpoint.dynamic,
        "status": call.status,
        "status_code": call.status_code,
        "response_time_ms": call.response_time_ms,
        "content_type": call.content_type,
        "valid_response": call.valid_response,
        "response_sample": call.response_sample,
        "resolved_path": call.resolved_path,
        "resolution_evidence": call.resolution_evidence,
        "error": call.error,
        "reason": call.reason,
    }


def to_dict(result):
    return {
        "root_path": result.root_path,
        "server_status": result.server_status,
        "server_detail": result.server_detail,
        "base_url": result.base_url,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "total_duration": result.total_duration,
        "warnings": list(result.warnings),
        "calls": [_call_to_dict(c) for c in result.calls],
    }


def to_json(result, indent=2):
    return json.dumps(to_dict(result), indent=indent, sort_keys=False)
