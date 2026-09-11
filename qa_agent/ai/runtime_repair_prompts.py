"""Prompt construction for Runtime Failure -> Verified Repair Integration
(Phase G Part 4, docs/24-runtime-repair-integration.md).

A new prompt builder, not a reuse of `build_repair_prompt` (Phase E Part 1) -
that builder only ever renders a bare `Finding` (tool/file/line/severity/
message) plus surrounding code, which cannot carry the runtime-specific
evidence this phase's own spec requires (check name/status, captured
output, the full `RuntimeDiagnosis`). What *is* reused, verbatim and
unmodified, is the part that actually matters for staying one system, not
two: the exact same JSON response schema `build_repair_prompt` already
requests (`explanation`/`replacement`/`confidence`/`start_line`/
`end_line`), so `response_parser.validate_repair_response` - Phase E Part
1's own validator - parses this prompt's response exactly as it parses any
other repair proposal, with no new schema or parser anywhere in this
module. This mirrors `diagnosis_prompts.py`'s own precedent exactly: a new
prompt builder for new evidence, reusing the shared `Prompt` shape and the
project's existing validation machinery rather than inventing either.

Log bounding/redaction here is a small, deliberate duplication of
`diagnosis_prompts.py`'s own `_bounded_logs`/`_redact` - the same
"a small, explicitly-flagged duplication this project already accepts
elsewhere" this project's own modules already practice (repair.py's
`_prompt_text`, duplicated across explainer.py/summarizer.py/fixer.py)
rather than importing another module's underscore-prefixed internals.
"""

from __future__ import annotations

import re

from .context import CodeContext
from .prompts import Prompt

_ROLE = (
    "You are proposing a concrete, minimal source-code repair for one "
    "already-diagnosed runtime QA failure. A deterministic runtime executor "
    "already determined this check's real outcome, and a separate AI "
    "diagnosis step already identified the likely cause and the affected "
    "file - you are not re-diagnosing the problem, only proposing a "
    "specific, reviewable code change to fix it."
)

REPAIR_GUARDRAILS = (
    "Only propose a change to the exact file and surrounding code shown "
    "below - never a different file, never a file not shown here. Base your "
    "replacement strictly on the runtime evidence, the diagnosis, and the "
    "surrounding code actually given to you; never invent a function, "
    "class, import, configuration value, dependency, or API that is not "
    "already visible in the evidence or the code below. If you are not "
    "confident a safe, minimal fix can be made from what is shown, respond "
    "with the insufficient-context decline below rather than guessing. "
    'Respond with JSON only, matching exactly the shape requested below - '
    "no extra commentary outside the JSON, and no markdown other than the "
    "JSON itself. If the supplied context is not sufficient to propose a "
    'confident repair, respond with exactly this instead: '
    '{"insufficient_context": true, "reason": "<brief reason>"}.'
)

_RESPONSE_SCHEMA = (
    'Respond with JSON only: {"explanation": "<why this change fixes the '
    'diagnosed problem>", "replacement": "<the replacement code for the '
    'affected line range>", "confidence": <a number from 0.0 to 1.0>, '
    '"start_line": <first line number (from the numbered lines below) the '
    'replacement covers>, "end_line": <last line number the replacement '
    'covers>}'
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b([\w.]*(?:key|token|secret|password|passwd)[\w.]*\s*[:=]\s*)([^\s'\"]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]+=*)"),
    re.compile(r"(?i)(://[^:/\s]+:)([^@\s]+)(@)"),
)


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda m: m.group(1) + "[REDACTED]" + (m.group(3) if m.lastindex and m.lastindex >= 3 else ""),
            text,
        )
    return text


MAX_LOG_CHARS = 3000


def _bounded_logs(lines) -> str:
    text = _redact("\n".join(lines))
    if len(text) <= MAX_LOG_CHARS:
        return text if text else "(no output was captured)"
    half = MAX_LOG_CHARS // 2
    return "{}\n[... {} character(s) truncated ...]\n{}".format(
        text[:half], len(text) - MAX_LOG_CHARS, text[-half:]
    )


def _diagnosis_lines(diagnosis) -> list:
    lines = [
        "AI DIAGNOSIS (a hypothesis, produced by a separate step - verify "
        "it against the evidence above, do not treat it as certain)",
        "",
        "Summary: {}".format(diagnosis.summary),
        "Likely root cause: {}".format(diagnosis.likely_root_cause),
        "Diagnosis confidence: {:.2f}".format(diagnosis.confidence),
    ]
    if diagnosis.observed_evidence:
        lines.append("Observed evidence: {}".format("; ".join(diagnosis.observed_evidence)))
    if diagnosis.affected_components:
        lines.append("Affected component(s): {}".format(", ".join(diagnosis.affected_components)))
    lines.append("Recommended action: {}".format(diagnosis.recommended_action))
    return lines


def _repository_context_lines(context) -> list:
    project = context.project
    lines = [
        "Repository type: {}".format(project.repository_type),
        "Application type: {}".format(project.application_type),
    ]
    if project.languages:
        lines.append("Languages: {}".format(", ".join(i.name for i in project.languages)))
    if project.frameworks:
        lines.append("Frameworks: {}".format(", ".join(i.name for i in project.frameworks)))
    return lines


def _code_context_lines(target_file, code_context: CodeContext) -> list:
    lines = ["Target file: {}".format(target_file)]
    if code_context.ok:
        lines.append("Surrounding code (lines {}-{}):".format(code_context.start_line, code_context.end_line))
        lines.append("```")
        for number, text in code_context.lines:
            marker = ">>" if number == code_context.target_line else "  "
            lines.append("{} {:>5}: {}".format(marker, number, text))
        lines.append("```")
    else:
        lines.append("(No surrounding code is available: {})".format(code_context.error))
    return lines


def build_runtime_repair_prompt(check_result, diagnosis, repository_context, code_context, target_file,
                                 runtime_check=None) -> Prompt:
    """`check_result`: the real `RuntimeCheckResult` (Phase G Part 2) this
    failure came from. `diagnosis`: the `RuntimeDiagnosis` (Phase G Part 3)
    already produced for it - a hypothesis, presented as such, never as
    settled fact (docs/24's own "DIAGNOSED is not proof" rule). `code_context`:
    a real `CodeContext` (Phase D Part 2's own `extract_context`) around a
    deterministically resolved target line - never a fabricated one.
    """
    lines = ["RUNTIME CHECK", ""]
    lines.append("Check: {}".format(check_result.name))
    if runtime_check is not None:
        lines.append("Category: {}".format(runtime_check.category))
    lines.append("Execution status: {}".format(check_result.status))
    lines.append("Deterministic reason (from the runtime executor): {}".format(check_result.reason))
    if check_result.exception:
        lines.append("Exception: {}".format(check_result.exception))
    lines += [
        "",
        "Captured output (stdout+stderr, merged, in order):",
        "```",
        _bounded_logs(check_result.logs),
        "```",
        "",
    ]
    lines += _diagnosis_lines(diagnosis)
    lines += ["", "REPOSITORY CONTEXT", ""]
    lines += _repository_context_lines(repository_context)
    lines += ["", "TARGET FILE", ""]
    lines += _code_context_lines(target_file, code_context)
    lines += [
        "",
        "Propose the smallest safe change to the target file above that "
        "would plausibly fix the diagnosed problem. Name exactly which line "
        "numbers (from the numbered lines above) your replacement covers.",
        "",
        _RESPONSE_SCHEMA,
    ]
    system = "{}\n\n{}".format(_ROLE, REPAIR_GUARDRAILS)
    return Prompt(system=system, user="\n".join(lines))
