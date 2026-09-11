"""Prompt construction for the AI Runtime Failure Diagnosis Engine (Phase G
Part 3, docs/23-runtime-failure-diagnosis-engine.md) - `prompts.py`'s own
role, extended to runtime evidence instead of a static-analysis `Finding`.

Every fact in the prompt comes from real, already-produced data: a
`RuntimeCheckResult` (Phase G Part 2's real execution output), the
`RuntimeCheck` that was planned for it (Phase G Part 1's own output, when
known), and a `RepositoryContext` (Phase F Part 2). Nothing here scans a
filesystem, runs a command, or invents a fact to fill a gap - a genuinely
missing piece (no known `RuntimeCheck`, an empty log) is simply omitted or
stated as absent, never guessed.
"""

from __future__ import annotations

import re

from .prompts import Prompt

_ROLE = (
    "You are diagnosing one already-observed runtime QA failure. The "
    "deterministic runtime executor below has already determined this "
    "check's real outcome - you are not the authority on whether it passed "
    "or failed, and must not second-guess that determination. Your only "
    "job is to interpret the runtime evidence supplied below: what "
    "happened, what most likely caused it, which part of the project "
    "appears affected, and what should be investigated or fixed next."
)

DIAGNOSIS_GUARDRAILS = (
    "Separate what the evidence actually shows (observed evidence) from "
    "what you are inferring (a likely cause) - never present an inference "
    "as an observed fact. Never invent a file, directory, function, class, "
    "endpoint, HTTP status, stack trace, dependency, package, technology, "
    "framework behavior, middleware, database, service, environment "
    "variable, configuration value, error message, root cause, or affected "
    "component that is not explicitly present in the evidence or "
    "repository context below - naming something not present there, even a "
    "highly plausible guess, is treated as a fabrication, not a diagnosis. "
    "Lower your confidence when logs are incomplete, when several causes "
    "are equally plausible, when no stack trace is available, when the "
    "output is ambiguous, or when the repository context is thin - a high "
    "confidence requires the evidence to actually support one specific "
    "cause, not just a plausible-sounding one. "
    'Respond with JSON only, matching exactly the shape requested below - '
    "no extra commentary outside the JSON, and no markdown other than the "
    "JSON itself. If the supplied evidence is not sufficient to determine a "
    'reliable cause, respond with exactly this instead: {"insufficient_context": '
    'true, "reason": "<brief reason>"}.'
)

_RESPONSE_SCHEMA = (
    'Respond with JSON only: {"summary": "<one or two sentences on what '
    'happened>", "severity": "<error|warning|info>", "confidence": '
    '<a number from 0.0 to 1.0>, "root_cause": "<the most likely cause, '
    'clearly framed as inference, not certainty, unless the evidence '
    'proves it>", "evidence": ["<a short quote or paraphrase of the '
    'specific observed evidence this diagnosis rests on>", ...], '
    '"affected_files": ["<only files/paths that actually appear in the '
    'evidence or repository context above>"], "affected_components": '
    '["<only components/technologies that actually appear above>"], '
    '"recommended_action": "<what to investigate or try next>"}'
)

# Redact obviously secret-shaped values before they ever reach a prompt -
# best-effort, not an exhaustive secret scanner (this phase's own "at
# minimum, do not introduce any new mechanism that intentionally exposes
# secrets" requirement). KEY=/TOKEN=/SECRET=/PASSWORD=-shaped assignments,
# a bearer token, and a credential embedded in a URL are the shapes most
# likely to appear in real build/startup output.
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b([\w.]*(?:key|token|secret|password|passwd)[\w.]*\s*[:=]\s*)([^\s'\"]+)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]+=*)"),
    re.compile(r"(?i)(://[^:/\s]+:)([^@\s]+)(@)"),
)


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]" + (m.group(3) if m.lastindex and m.lastindex >= 3 else ""), text)
    return text


# Bounded well under prompts.py's own per-finding budget (context.py's
# MAX_CHARS=8000 for one code snippet) - runtime logs can be far larger
# than a code snippet ever is, and this is the one prompt in this project
# built from genuinely unbounded real-world text (a real build/dev-server's
# own stdout+stderr), not source code this project already controls the
# shape of.
MAX_LOG_CHARS = 3000


def _bounded_logs(lines) -> str:
    """Head+tail truncation, explicit marker when truncated - this phase's
    own "never silently pretend truncated logs are complete" rule.
    `RuntimeCheckResult.logs` is already a single merged stdout+stderr
    stream (Phase G Part 2's own design - the two are not captured
    separately), so "prioritize stderr" is honored as far as the data
    actually allows: the real exception/reason fields (Python-side,
    captured independently of the subprocess stream) are always included
    in full elsewhere in this prompt, never subject to this truncation.
    """
    text = _redact("\n".join(lines))
    if len(text) <= MAX_LOG_CHARS:
        return text if text else "(no output was captured)"
    half = MAX_LOG_CHARS // 2
    return "{}\n[... {} character(s) truncated ...]\n{}".format(
        text[:half], len(text) - MAX_LOG_CHARS, text[-half:]
    )


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
    if context.constraints:
        lines.append("Constraints: {}".format(", ".join(context.constraints)))
    return lines


def build_diagnosis_prompt(check_result, context, runtime_check=None) -> Prompt:
    """`check_result`: a real `RuntimeCheckResult` (Phase G Part 2).
    `context`: a real `RepositoryContext` (Phase F Part 2). `runtime_check`:
    the originating `RuntimeCheck` (Phase G Part 1), when known - adds
    category and the planner's own reason/evidence for planning this check
    at all; omitted gracefully (not guessed) when unavailable.
    """
    lines = ["RUNTIME CHECK", ""]
    lines.append("Check: {}".format(check_result.name))
    if runtime_check is not None:
        lines.append("Category: {}".format(runtime_check.category))
        lines.append("Why this check was planned: {}".format(runtime_check.reason))
    lines.append("Execution status: {}".format(check_result.status))
    lines.append("Duration: {:.2f}s".format(check_result.duration))
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
        "REPOSITORY CONTEXT",
        "",
    ]
    lines += _repository_context_lines(context)
    lines += [
        "",
        "Now answer, from the evidence above only:",
        "1. What actually happened?",
        "2. What is the most likely cause supported by the evidence?",
        "3. Which component appears affected?",
        "4. What should be investigated next?",
        "5. Is the evidence above sufficient to answer confidently?",
        "",
        _RESPONSE_SCHEMA,
    ]
    system = "{}\n\n{}".format(_ROLE, DIAGNOSIS_GUARDRAILS)
    return Prompt(system=system, user="\n".join(lines))
