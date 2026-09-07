"""Render a run as the structured report described in docs/01-definition.md section 6."""

from __future__ import annotations

import traceback
from datetime import datetime


def render(result, source):
    """The full one-shot report: a summary block followed by the findings."""
    lines = [
        "QA Agent report",
        "  Run at:  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "  Input:   {}".format(source),
        "  Checked: {} file(s)".format(len(result.checked)),
        "  Tools:   {}".format(", ".join(result.tools_used) or "none"),
        "",
    ]
    return "\n".join(lines + _findings_lines(result))


def render_findings(result):
    """Just the findings, without the summary block.

    Watch mode announces run time and the files involved in its own batch
    header, so repeating them per report would duplicate information in a
    stream meant to stay readable for hours. Both callers share these lines -
    there is only one implementation of findings formatting.
    """
    return "\n".join(_findings_lines(result))


def _findings_lines(result):
    lines = []

    if result.findings:
        lines.append("Findings ({}):".format(len(result.findings)))
        for finding in result.findings:
            lines.append(
                "  {}:{}  [{}] {}  ({})".format(
                    finding.file, finding.line, finding.severity, finding.message, finding.tool
                )
            )
    elif result.checked:
        lines.append("Findings: no issues found.")
    else:
        lines.append("Findings: none - no files were checked.")

    if result.skipped:
        lines.append("")
        lines.append("Skipped ({}) - not checked:".format(len(result.skipped)))
        for path, reason in result.skipped:
            lines.append("  {}  ({})".format(path, reason))

    if result.missing:
        lines.append("")
        lines.append("Not found ({}):".format(len(result.missing)))
        for raw in result.missing:
            lines.append("  {}".format(raw))

    return lines


def render_tool_error(error):
    return "QA Agent: tool error - no code issues were reported.\n  {}".format(error)


def _cell(text):
    # A literal '|' would break the surrounding Markdown table.
    return str(text).replace("|", r"\|")


def render_markdown(result, source):
    """The same run as render(), in Markdown. Same data, different presentation."""
    lines = [
        "# QA Agent Report",
        "",
        "- **Run at:** {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "- **Input:** {}".format(_cell(source)),
        "- **Checked:** {} file(s)".format(len(result.checked)),
        "- **Tools:** {}".format(", ".join(result.tools_used) or "none"),
        "",
    ]

    if result.findings:
        lines.append("## Findings ({})".format(len(result.findings)))
        lines.append("")
        lines.append("| File | Line | Severity | Message | Tool |")
        lines.append("| --- | ---: | --- | --- | --- |")
        for finding in result.findings:
            lines.append(
                "| `{}` | {} | {} | {} | {} |".format(
                    _cell(finding.file),
                    finding.line,
                    _cell(finding.severity),
                    _cell(finding.message),
                    _cell(finding.tool),
                )
            )
    else:
        lines.append("## Findings")
        lines.append("")
        lines.append(
            "No issues found." if result.checked else "None - no files were checked."
        )

    if result.skipped:
        lines += ["", "## Skipped ({}) - not checked".format(len(result.skipped)), ""]
        lines.append("| File | Reason |")
        lines.append("| --- | --- |")
        for path, reason in result.skipped:
            lines.append("| `{}` | {} |".format(_cell(path), _cell(reason)))

    if result.missing:
        lines += ["", "## Not found ({})".format(len(result.missing)), ""]
        for raw in result.missing:
            lines.append("- `{}`".format(_cell(raw)))

    return "\n".join(lines) + "\n"


def render_unexpected_error(source, error):
    """An analysis failed for a reason we did not anticipate.

    Long-running watch mode survives these, so the traceback is printed in full:
    a swallowed error on an always-on process is worse than a noisy one.
    """
    details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return "QA Agent: unexpected error while analyzing {} - watch mode is still running.\n{}".format(
        source, details.rstrip()
    )


def render_write_error(path, error):
    return "QA Agent: report ran, but could not write it to '{}': {}".format(path, error)
