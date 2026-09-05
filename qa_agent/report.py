"""Render a run as the structured report described in docs/01-definition.md section 6."""

from __future__ import annotations

from datetime import datetime


def render(result, source):
    lines = [
        "QA Agent report",
        "  Run at:  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "  Input:   {}".format(source),
        "  Checked: {} file(s)".format(len(result.checked)),
        "  Tools:   {}".format(", ".join(result.tools_used) or "none"),
        "",
    ]

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

    return "\n".join(lines)


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


def render_write_error(path, error):
    return "QA Agent: report ran, but could not write it to '{}': {}".format(path, error)
