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

from .analysis import CLASSIFICATIONS, classify_call_outcome, classify_call_severity, expected_actual

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

    for call in result.calls:
        # docs/48-fail-fast-and-classification.md: the final, user-facing
        # label every endpoint is shown under - one of exactly four
        # (Working/Failing/Not Working/Skipped) - replaces the old bare
        # PASS/FAIL/SKIP symbol here; `status`/severity remain the
        # underlying evidence it's computed from, unchanged everywhere else.
        symbol = classify_call_outcome(call)
        if call.synthetic:
            symbol = "{} [SYNTHETIC: {}]".format(symbol, ", ".join(call.synthetic_fields))
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

    lines.append("")
    # docs/45: real-evidence and synthetic outcomes are always reported as
    # two separate tallies - never combined into one number that would
    # overstate how much of a "pass" count came from real evidence.
    counts = _summary_counts(result.calls)
    real, synthetic = counts["real_evidence"], counts["synthetic"]
    real_summary = ", ".join(
        "{} {}".format(n, status) for status, n in sorted(real.items()) if n
    ) or "none"
    summary_line = "  {} call(s) - {} (real evidence)".format(len(result.calls), real_summary)
    if synthetic["pass"] or synthetic["fail"]:
        synthetic_summary = ", ".join(
            "{} {}".format(n, status) for status, n in sorted(synthetic.items()) if n
        )
        summary_line += "; {} (synthetic data)".format(synthetic_summary)
    summary_line += ". Total time: {:.2f}s".format(result.total_duration)
    lines.append(summary_line)

    # docs/48: the same four-label classification every call line above is
    # already shown under, tallied once so a reader gets the shape of the
    # whole run at a glance without counting lines.
    classification_counts = _classification_counts(result.calls)
    lines.append("  Classification: {}".format(
        ", ".join("{} {}".format(classification_counts[label], label) for label in CLASSIFICATIONS)
    ))

    if result.server_log_tail and any(c.status != "pass" for c in result.calls):
        lines.append("")
        lines.append("  Server log (during this run):")
        tail_lines = result.server_log_tail.splitlines()[-_MAX_LOG_TAIL_LINES_SHOWN:]
        lines.extend("    | {}".format(line) for line in tail_lines)
    return "\n".join(lines).rstrip() + "\n"


def _call_to_dict(call):
    expected, actual = expected_actual(call)
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
        "severity": classify_call_severity(call),
        # docs/48-fail-fast-and-classification.md: the final, closed-set
        # user-facing label (Working/Failing/Not Working/Skipped) - see
        # `classify_call_outcome`'s own docstring for exactly what each
        # one means and how it's derived from `status`/`status_code`.
        "classification": classify_call_outcome(call),
        "expected": expected,
        "actual": actual,
        # docs/45-synthetic-mutation-testing.md: `True` only when at least
        # one value used for this call's path/body was invented rather than
        # real evidence/a schema default - `synthetic_fields` names exactly
        # which ones. Never blended into the pass/fail meaning of `status`
        # itself - a synthetic PASS is still a real HTTP PASS against a
        # real server, just built from a placeholder value, and reporting
        # layers are expected to show that distinction, never hide it.
        "synthetic": call.synthetic,
        "synthetic_fields": list(call.synthetic_fields),
    }


def _negative_call_to_dict(nc):
    return {
        "method": nc.endpoint.method,
        "path": nc.endpoint.path,
        "source_file": nc.endpoint.source_file,
        "case_name": nc.case_name,
        "expected": nc.expected,
        "status": nc.status,
        "actual_status_code": nc.actual_status_code,
        "actual_summary": nc.actual_summary,
        "severity": nc.severity or None,
        "synthetic": nc.synthetic,
        "synthetic_fields": list(nc.synthetic_fields),
    }


def _schema_validation_to_dict(sv):
    return {
        "method": sv.endpoint.method,
        "path": sv.endpoint.path,
        "source_file": sv.endpoint.source_file,
        "status": sv.status,
        "missing_fields": list(sv.missing_fields),
        "type_mismatches": list(sv.type_mismatches),
        "severity": sv.severity or None,
        "reason": sv.reason,
    }


def _summary_counts(calls):
    """docs/45-synthetic-mutation-testing.md: real-evidence and synthetic
    results are counted separately so a report never blends "the target
    really handled a real request correctly" with "the target handled a
    request we had to invent placeholder data for" into one combined
    number. A `CALL_SKIPPED` result is never synthetic (nothing was ever
    actually invented and sent - the call simply never happened), so the
    synthetic bucket only ever needs pass/fail.
    """
    real = {"pass": 0, "fail": 0, "skipped": 0}
    synthetic = {"pass": 0, "fail": 0}
    for call in calls:
        if call.synthetic:
            if call.status in synthetic:
                synthetic[call.status] += 1
        else:
            real[call.status] = real.get(call.status, 0) + 1
    return {"real_evidence": real, "synthetic": synthetic}


def _classification_counts(calls):
    """docs/48-fail-fast-and-classification.md: one tally across the four
    `CLASSIFICATIONS` labels - every key always present, `0` when a bucket
    is genuinely empty, never omitted (so a caller can always print all
    four without a `KeyError`).
    """
    counts = {label: 0 for label in CLASSIFICATIONS}
    for call in calls:
        counts[classify_call_outcome(call)] += 1
    return counts


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
        "negative_calls": [_negative_call_to_dict(nc) for nc in result.negative_calls],
        "schema_validations": [_schema_validation_to_dict(sv) for sv in result.schema_validations],
        "summary": dict(_summary_counts(result.calls), classification=_classification_counts(result.calls)),
    }


def to_json(result, indent=2):
    return json.dumps(to_dict(result), indent=indent, sort_keys=False)


# --- CSV/HTML file export (docs/36-api-qa-report-export.md) ----------------

_CSV_FIELDNAMES = (
    "method", "path", "classification", "status", "status_code", "response_time_ms",
    "reason", "error", "response_sample", "resolved_path", "resolution_evidence",
    "source_file", "severity", "expected", "actual", "synthetic", "synthetic_fields",
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
    if call.synthetic:
        # docs/45: a synthetic result is never left visually identical to a
        # real-evidence one - same color bucket (it really is a real HTTP
        # PASS/FAIL against the real server), but always an explicit,
        # un-missable suffix naming exactly which field(s) were invented.
        detail = "{} [Synthetic Data: {}]".format(detail, ", ".join(call.synthetic_fields)).strip()
    sample = call.response_sample[:200] if call.response_sample else ""
    return (
        '<tr>'
        '<td class="method">{method}</td>'
        '<td class="path">{path}</td>'
        '<td><strong>{classification}</strong></td>'
        '<td><span class="badge" style="color:{fg};background:{bg}">{label}</span></td>'
        '<td class="code">{code}</td>'
        '<td class="timing">{timing}</td>'
        '<td class="detail">{detail}{sample}</td>'
        '</tr>'
    ).format(
        method=_esc(call.endpoint.method), path=_esc(call.endpoint.path),
        classification=_esc(classify_call_outcome(call)),
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
  <thead><tr><th>Method</th><th>Path</th><th>Classification</th><th>Result</th><th>HTTP</th><th>Time</th><th>Detail</th></tr></thead>
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
        '<tr><td colspan="7">No endpoint calls were made.</td></tr>'
    )
    counts = _summary_counts(result.calls)
    real, synthetic = counts["real_evidence"], counts["synthetic"]
    real_summary = ", ".join("{} {}".format(n, s) for s, n in sorted(real.items()) if n) or "none"
    summary = "{} call(s) - {} (real evidence)".format(len(result.calls), real_summary)
    if synthetic["pass"] or synthetic["fail"]:
        synthetic_summary = ", ".join("{} {}".format(n, s) for s, n in sorted(synthetic.items()) if n)
        summary += "; {} (synthetic data)".format(synthetic_summary)
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
