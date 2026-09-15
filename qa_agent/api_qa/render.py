"""Presentation for an `ApiTestResult` (docs/30-api-qa-v1.md) - plain text
(CLI), JSON/dict, and file exports (docs/36-api-qa-report-export.md: CSV,
HTML). Mirrors `runtime/execution_render.py`'s own role for
`RuntimeExecutionResult`: this module only ever formats an already-finished
result, never runs anything itself.
"""

from __future__ import annotations

import csv
import html
import io
import json

_STATUS_SYMBOLS = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}

# Only the most recent lines are worth showing inline in a terminal report -
# the full, still-bounded `server_log_tail` remains available via to_dict/
# to_json/to_html for anyone who wants more than a terminal-sized glance.
_MAX_LOG_TAIL_LINES_SHOWN = 40


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

    if result.server_log_tail and any(c.status != "pass" for c in result.calls):
        lines.append("")
        lines.append("  Server log (during this run):")
        tail_lines = result.server_log_tail.splitlines()[-_MAX_LOG_TAIL_LINES_SHOWN:]
        lines.extend("    | {}".format(line) for line in tail_lines)
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
        "server_log_tail": result.server_log_tail,
    }


def to_json(result, indent=2):
    return json.dumps(to_dict(result), indent=indent, sort_keys=False)


# --- CSV/HTML file export (docs/36-api-qa-report-export.md) ----------------

_CSV_FIELDNAMES = (
    "method", "path", "status", "status_code", "response_time_ms",
    "reason", "error", "response_sample", "resolved_path", "resolution_evidence",
    "source_file",
)


def to_csv(result):
    """One row per call, in `result.calls`' own order - the same "one call,
    one honest outcome" data `to_dict` already exposes, just reshaped for a
    spreadsheet. No row is ever added for an endpoint that was never called;
    `ApiTestResult` never has one anyway (`calls` has exactly one entry per
    `endpoints`, docs/30).
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDNAMES)
    writer.writeheader()
    for call in result.calls:
        row = _call_to_dict(call)
        row["source_file"] = call.endpoint.source_file
        writer.writerow({name: row.get(name, "") for name in _CSV_FIELDNAMES})
    return buffer.getvalue()


# One real, fixed color per real outcome bucket - never a computed/guessed
# color, the same "closed, named set" convention `CALL_STATUSES`/
# `SERVER_STATUSES` already follow. A row's bucket is decided by the most
# specific real fact available: `call.status` first (skipped is always
# gray, regardless of any stale status_code), then the real HTTP status
# code's own class, then "no response was ever received at all" last.
_BUCKET_SKIPPED = ("SKIPPED", "#6b7280", "#f3f4f6")
_BUCKET_2XX = ("2xx", "#15803d", "#dcfce7")
_BUCKET_3XX = ("3xx", "#1d4ed8", "#dbeafe")
_BUCKET_4XX = ("4xx", "#b45309", "#fef3c7")
_BUCKET_5XX = ("5xx", "#b91c1c", "#fee2e2")
_BUCKET_NO_RESPONSE = ("NO RESPONSE", "#7f1d1d", "#fecaca")


def _status_bucket(call):
    if call.status == "skipped":
        return _BUCKET_SKIPPED
    code = call.status_code
    if code is None:
        return _BUCKET_NO_RESPONSE
    if 200 <= code < 300:
        return _BUCKET_2XX
    if 300 <= code < 400:
        return _BUCKET_3XX
    if 400 <= code < 500:
        return _BUCKET_4XX
    return _BUCKET_5XX


def _esc(value):
    return html.escape(str(value)) if value else ""


def _html_row(call):
    label, fg, bg = _status_bucket(call)
    code = call.status_code if call.status_code is not None else "-"
    timing = "{:.0f}ms".format(call.response_time_ms) if call.response_time_ms is not None else "-"
    detail = call.error or call.reason or ""
    sample = call.response_sample[:200] if call.response_sample else ""
    return (
        '<tr>'
        '<td class="method">{method}</td>'
        '<td class="path">{path}</td>'
        '<td><span class="badge" style="color:{fg};background:{bg}">{label}</span></td>'
        '<td class="code">{code}</td>'
        '<td class="timing">{timing}</td>'
        '<td class="detail">{detail}{sample}</td>'
        '</tr>'
    ).format(
        method=_esc(call.endpoint.method), path=_esc(call.endpoint.path),
        fg=fg, bg=bg, label=label, code=_esc(code), timing=_esc(timing),
        detail=_esc(detail),
        sample=(" &mdash; <code>{}</code>".format(_esc(sample)) if sample else ""),
    )


_HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>API QA Report</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 2rem; color: #111827; }}
  h1 {{ font-size: 1.25rem; margin-bottom: 0.25rem; }}
  .meta {{ color: #4b5563; font-size: 0.875rem; margin-bottom: 1rem; }}
  .meta div {{ margin: 0.1rem 0; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.875rem; }}
  th, td {{ text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #e5e7eb; vertical-align: top; }}
  th {{ background: #f9fafb; }}
  .method {{ font-weight: 600; white-space: nowrap; }}
  .path {{ font-family: ui-monospace, Consolas, monospace; }}
  .code {{ text-align: right; }}
  .badge {{ display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-weight: 600; font-size: 0.75rem; }}
  .detail {{ color: #374151; }}
  .detail code {{ background: #f3f4f6; padding: 0 0.25rem; }}
  .summary {{ margin-top: 1rem; font-size: 0.875rem; color: #374151; }}
  .warnings {{ margin: 1rem 0; padding: 0.75rem 1rem; background: #fffbeb; border: 1px solid #fde68a; font-size: 0.8125rem; }}
  .server-log {{ margin-top: 1.5rem; }}
  .server-log h2 {{ font-size: 0.9375rem; margin-bottom: 0.5rem; }}
  .server-log pre {{ background: #111827; color: #e5e7eb; padding: 0.75rem 1rem; overflow-x: auto; font-size: 0.75rem; border-radius: 4px; }}
</style>
</head>
<body>
<h1>API QA Report</h1>
<div class="meta">
  <div><strong>Root:</strong> {root}</div>
  <div><strong>Server status:</strong> {server_status}</div>
  <div><strong>Base URL:</strong> {base_url}</div>
  <div><strong>Run:</strong> {started_at} &rarr; {finished_at} ({duration:.2f}s)</div>
</div>
{warnings_block}
<table>
  <thead><tr><th>Method</th><th>Path</th><th>Result</th><th>HTTP</th><th>Time</th><th>Detail</th></tr></thead>
  <tbody>
    {rows}
  </tbody>
</table>
<div class="summary">{summary}</div>
{server_log_block}
</body>
</html>
"""


def to_html(result):
    """A single self-contained HTML file - inline CSS only, no external
    assets/network fetches (consistent with this project's own "no new
    dependency when the stdlib already does the job" convention, docs/step
    -log.md's Dependency philosophy) - one color-coded row per call.
    """
    rows = "\n    ".join(_html_row(c) for c in result.calls) or (
        '<tr><td colspan="6">No endpoint calls were made.</td></tr>'
    )
    counts = {}
    for call in result.calls:
        counts[call.status] = counts.get(call.status, 0) + 1
    summary = "{} call(s) - {}".format(
        len(result.calls),
        ", ".join("{} {}".format(n, s) for s, n in sorted(counts.items())) or "none",
    )
    warnings_block = ""
    if result.warnings:
        items = "".join("<div>&bull; {}</div>".format(_esc(w)) for w in result.warnings)
        warnings_block = '<div class="warnings">{}</div>'.format(items)
    server_log_block = ""
    if result.server_log_tail:
        server_log_block = (
            '<div class="server-log"><h2>Server log (during this run)</h2><pre>{}</pre></div>'
            .format(_esc(result.server_log_tail))
        )
    return _HTML_TEMPLATE.format(
        root=_esc(result.root_path),
        server_status=_esc(result.server_status + (" ({})".format(result.server_detail) if result.server_detail else "")),
        base_url=_esc(result.base_url) or "-",
        started_at=_esc(result.started_at), finished_at=_esc(result.finished_at),
        duration=result.total_duration,
        warnings_block=warnings_block,
        rows=rows,
        summary=_esc(summary),
        server_log_block=server_log_block,
    )
