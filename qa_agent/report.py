"""Render a run as the structured report described in docs/01-definition.md section 6."""

from __future__ import annotations

import traceback
from datetime import datetime


def render(result, source, config_path=None):
    """The full one-shot report: a summary block followed by the findings."""
    lines = [
        "QA Agent report",
        "  Run at:  {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "  Input:   {}".format(source),
        "  Checked: {} file(s)".format(len(result.checked)),
        "  Tools:   {}".format(", ".join(result.tools_used) or "none"),
    ]
    if config_path is not None:
        lines.append("  Config:  {}".format(config_path))
    lines.append("")
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

    if result.filtered:
        lines.append(
            "{} finding(s) hidden by the config's severity filter (not lost - "
            "raise min_severity or remove it to see them).".format(result.filtered)
        )

    if result.tool_errors:
        lines.append("")
        lines.append(
            "Analyzer errors ({}) - findings from these tools may be incomplete:".format(
                len(result.tool_errors)
            )
        )
        for name, error in result.tool_errors:
            lines.append("  {}: {}".format(name, error))

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


def render_config_error(error):
    # A distinct message from render_tool_error(): this isn't a tool that
    # failed to run, it's the config that decides what should run at all
    # (docs/16-configuration-system.md) - nothing has been analyzed yet.
    return "QA Agent: configuration error - nothing was analyzed.\n  {}".format(error)


def _cell(text):
    # A literal '|' would break the surrounding Markdown table.
    return str(text).replace("|", r"\|")


def render_markdown(result, source, config_path=None):
    """The same run as render(), in Markdown. Same data, different presentation."""
    lines = [
        "# QA Agent Report",
        "",
        "- **Run at:** {}".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "- **Input:** {}".format(_cell(source)),
        "- **Checked:** {} file(s)".format(len(result.checked)),
        "- **Tools:** {}".format(", ".join(result.tools_used) or "none"),
    ]
    if config_path is not None:
        lines.append("- **Config:** {}".format(_cell(config_path)))
    lines.append("")

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

    if result.filtered:
        lines.append("")
        lines.append(
            "{} finding(s) hidden by the config's severity filter (not lost).".format(
                result.filtered
            )
        )

    if result.tool_errors:
        lines += ["", "## Analyzer errors ({}) - findings may be incomplete".format(
            len(result.tool_errors)), ""]
        for name, error in result.tool_errors:
            lines.append("- **{}:** {}".format(name, _cell(error)))

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
