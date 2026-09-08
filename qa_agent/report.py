"""Render a run as the structured report described in docs/01-definition.md section 6."""

from __future__ import annotations

import traceback
from datetime import datetime


def render(result, source, config_path=None, explanations=None, summary=None,
           suggested_fixes=None):
    """The full one-shot report: a run summary block, then the findings.

    `explanations` (Phase D Part 3): an optional {Finding: Explanation}
    mapping, as produced by the AI package's explainer. `summary` (Phase D
    Part 4): an optional AI-generated Summary of the whole run, as produced
    by the AI package's summarizer. `suggested_fixes` (Phase D Part 5): an
    optional {Finding: SuggestedFix} mapping, as produced by the AI
    package's fixer - always advisory, never applied, and rendered beneath
    a finding's own explanation. All three are purely additive
    presentation - omitted or None (the default for all three), output is
    byte-for-byte identical to every Phase C report; nothing here calls the
    AI layer or requires it, and this module imports nothing from that
    package.
    """
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
    lines += _summary_lines(summary)
    return "\n".join(lines + _findings_lines(result, explanations, suggested_fixes))


def _summary_lines(summary):
    """The AI run summary block, rendered before the findings - clearly
    labeled, visually distinct from the deterministic metadata above it and
    the findings below it, and collapsed to one line the same way an AI
    explanation is (Phase D Part 3's `_single_line()`) so a multi-line
    reply can never blur into the section beneath it. Empty when there is
    no summary to show - never a placeholder, never invented.
    """
    if summary is None:
        return []
    header = "AI Summary"
    return [header, "-" * len(header), _single_line(summary.text), ""]


def render_findings(result, explanations=None, suggested_fixes=None):
    """Just the findings, without the run summary block.

    Watch mode announces run time and the files involved in its own batch
    header, so repeating them per report would duplicate information in a
    stream meant to stay readable for hours. Both callers share these lines -
    there is only one implementation of findings formatting. `summary`
    (Phase D Part 4) is deliberately not accepted here: a whole-run
    executive summary has no natural meaning for one incremental watch-mode
    batch, unlike `explanations`/`suggested_fixes`, which stay per-finding
    at any granularity.
    """
    return "\n".join(_findings_lines(result, explanations, suggested_fixes))


def _single_line(text):
    """Collapse an AI explanation to one clean line regardless of what the
    model actually returned - deterministic formatting (docs/step-log.md,
    Phase D Part 3), and never lets a multi-line reply blur into the next
    finding or invent an ambiguous indent scheme.
    """
    return " ".join(text.split())


def _fix_lines(fix):
    """The [AI Suggested Fix] block, rendered beneath a finding's own
    explanation (Phase D Part 5) - clearly labeled as advisory, requiring
    developer review, never a claim of certainty. The explanation line is
    collapsed to one line like every other AI-generated prose in this
    module; the replacement is a real, possibly multi-line piece of code,
    so its own line breaks are preserved (indented, not collapsed) rather
    than mashed into an unreadable single line.
    """
    lines = ["      [AI Suggested Fix] (advisory only - review before applying)"]
    lines.append("          {}".format(_single_line(fix.explanation)))
    lines.append("")
    for line in fix.replacement.splitlines() or [""]:
        lines.append("          {}".format(line))
    return lines


def _findings_lines(result, explanations=None, suggested_fixes=None):
    lines = []

    if result.findings:
        lines.append("Findings ({}):".format(len(result.findings)))
        for finding in result.findings:
            lines.append(
                "  {}:{}  [{}] {}  ({})".format(
                    finding.file, finding.line, finding.severity, finding.message, finding.tool
                )
            )
            explanation = explanations.get(finding) if explanations else None
            if explanation is not None:
                # Clearly labeled and visually subordinate (indented past
                # the finding line itself), on its own line, never merged
                # into the tool's own message and never replacing it.
                lines.append("      [AI Explanation] {}".format(_single_line(explanation.text)))
            fix = suggested_fixes.get(finding) if suggested_fixes else None
            if fix is not None:
                # Beneath the explanation, exactly as required - never
                # rendered in its place, and never when neither exists.
                lines.append("")
                lines += _fix_lines(fix)
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


def render_markdown(result, source, config_path=None, explanations=None, summary=None,
                     suggested_fixes=None):
    """The same run as render(), in Markdown. Same data, different
    presentation. `explanations`, `summary`, and `suggested_fixes` are the
    same optional values `render()` accepts (Phase D Parts 3-5) - all
    omitted or None (the default), output is byte-for-byte identical to
    every Phase C report.
    """
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

    if summary is not None:
        # Its own section, before Findings, exactly like the terminal
        # report - never folded into the metadata list above or the
        # findings table below.
        lines += ["## AI Summary", "", _single_line(summary.text), ""]

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

    if explanations:
        # A separate section, not extra table columns or cells: the
        # findings table stays exactly what Phase C already produces, and
        # explanations are additive, clearly labeled, and never mixed into
        # a finding's own row (docs/step-log.md, Phase D Part 3). Same
        # deterministic order as the findings themselves, filtered to only
        # those actually explained.
        explained = [f for f in result.findings if f in explanations]
        if explained:
            lines += ["", "## AI Explanations ({})".format(len(explained)), ""]
            for finding in explained:
                lines.append(
                    "- **`{}:{}`** ({}): {}".format(
                        _cell(finding.file),
                        finding.line,
                        _cell(finding.tool),
                        _cell(_single_line(explanations[finding].text)),
                    )
                )

    if suggested_fixes:
        # A separate section again, not a table column: a suggested fix
        # can be a real, multi-line piece of code, which a table cell
        # cannot represent cleanly. Its own subheading per finding rather
        # than a list item, since a fenced code block inside a Markdown
        # list item is indentation-sensitive and easy to render wrong;
        # this way is unambiguous. Same deterministic order as the
        # findings themselves, filtered to only those actually given one.
        fixed = [f for f in result.findings if f in suggested_fixes]
        if fixed:
            lines += ["", "## AI Suggested Fixes ({}) - advisory, review before applying".format(
                len(fixed)), ""]
            for finding in fixed:
                fix = suggested_fixes[finding]
                lines += [
                    "### `{}:{}` ({})".format(_cell(finding.file), finding.line,
                                               _cell(finding.tool)),
                    "",
                    _cell(_single_line(fix.explanation)),
                    "",
                    "```",
                    fix.replacement,
                    "```",
                    "",
                ]

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
