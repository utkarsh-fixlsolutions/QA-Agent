"""Centralized prompt construction (Phase D Part 2).

Every prompt QA-Agent will ever send an LLM is built here - no prompt text
lives anywhere else in the codebase. Each builder returns a `Prompt`
(system text kept separate from user text, matching how a chat-style model
API is actually structured) and does nothing else: no provider is called,
nothing is rendered, nothing is written anywhere.

Builders take plain, duck-typed arguments (an object with `.file`, `.line`,
`.severity`, `.message`, `.tool` for a finding; one with `.checked`,
`.tools_used`, `.findings` for a run) rather than importing `Finding` or
`RunResult` from the analyzer engine - the same "give it the right shape,
not the right base class" convention `adapters.py`'s own tool contract
already uses, and it keeps this module trivially testable with plain fakes,
no real analyzer run required. The architectural rule this still satisfies
is behavioural, not an import: every prompt is built strictly from
completed analyzer output (a real Finding, a real RunResult) and the code
context extracted from a real file - never anything invented.
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import CodeContext

# Every system prompt includes this, verbatim, so the anti-hallucination
# rules are never respelled per builder (docs/step-log.md, Phase D Part 2):
# findings already come from deterministic tools, the model must only
# discuss what it was given, and it must decline explicitly - via the
# insufficient_context shape response_parser.py knows how to recognize -
# rather than guess when the given context is not enough.
GUARDRAILS = (
    "All findings you are given were already detected by deterministic "
    "static-analysis tools (for example ruff, eslint, pyright, mypy, or "
    "shellcheck) - you did not find them and must not act as though you did. "
    "Only discuss the specific finding(s) supplied below. Do not invent "
    "additional bugs, files, rules, or recommendations that are not present "
    "in the supplied findings or code context. "
    'Respond with JSON only, matching exactly the shape requested below - '
    "no extra commentary outside the JSON, and no markdown other than the "
    "JSON itself. "
    "If the supplied context is not sufficient to answer confidently, "
    'respond with exactly this instead: {"insufficient_context": true, '
    '"reason": "<brief reason>"}.'
)

_EXPLANATION_ROLE = (
    "Your task: explain, in plain language a developer can act on, why the "
    "single finding below was flagged."
)
_SUMMARY_ROLE = (
    "Your task: summarize the findings below for a developer scanning a QA "
    "report - what kinds of issues showed up, and roughly how many."
)
_FIX_ROLE = "Your task: suggest a concrete fix for the single finding below."
_REPAIR_ROLE = (
    "Your task: propose a structured repair for the single finding below - "
    "a replacement for the affected line range in the file, together with "
    "your own confidence in it."
)

# More than this would bury the summary prompt in tokens for little benefit -
# the same "cap it, count the remainder" precedent live_report.py's
# MAX_LISTED_FILES already established for a different kind of long list.
MAX_SUMMARY_FINDINGS = 50


@dataclass(frozen=True)
class Prompt:
    """One prompt, ready to hand a provider - system and user text kept
    separate, matching how a chat-style model API is actually structured.
    """

    system: str
    user: str


def _system_prompt(role: str) -> str:
    return "{}\n\n{}".format(role, GUARDRAILS)


def _finding_and_context_lines(finding, context: CodeContext) -> list:
    lines = [
        "Tool: {}".format(finding.tool),
        "File: {}".format(finding.file),
        "Line: {}".format(finding.line),
        "Severity: {}".format(finding.severity),
        "Message: {}".format(finding.message),
        "",
    ]
    if context.ok:
        lines.append("Surrounding code (lines {}-{}):".format(context.start_line, context.end_line))
        lines.append("```")
        for number, text in context.lines:
            marker = ">>" if number == finding.line else "  "
            lines.append("{} {:>5}: {}".format(marker, number, text))
        lines.append("```")
    else:
        lines.append("(No surrounding code is available: {})".format(context.error))
    return lines


def build_explanation_prompt(finding, context: CodeContext) -> Prompt:
    """A prompt asking the model to explain one specific finding.

    `finding`: anything with `.tool`, `.file`, `.line`, `.severity`,
    `.message` (a real `Finding`, or a test double shaped like one).
    `context`: a `CodeContext` from `context.extract_context()` - `ok=False`
    is handled explicitly, not silently swallowed, so the model is told
    plainly when there is no surrounding code rather than being handed an
    empty block.
    """
    lines = _finding_and_context_lines(finding, context)
    lines += [
        "",
        'Respond with JSON only: {"explanation": "<your explanation of this finding>"}',
    ]
    return Prompt(system=_system_prompt(_EXPLANATION_ROLE), user="\n".join(lines))


def build_fix_prompt(finding, context: CodeContext) -> Prompt:
    """A prompt asking the model to suggest a fix for one specific finding.
    Same inputs as `build_explanation_prompt`.
    """
    lines = _finding_and_context_lines(finding, context)
    lines += [
        "",
        'Respond with JSON only: {"explanation": "<why this is an issue>", '
        '"suggested_fix": "<a concrete fix, as text or a short code snippet>"}',
    ]
    return Prompt(system=_system_prompt(_FIX_ROLE), user="\n".join(lines))


def build_repair_prompt(finding, context: CodeContext) -> Prompt:
    """A prompt asking the model to propose a structured repair for one
    specific finding - Phase E Part 1's extension of `build_fix_prompt`
    (Phase D Part 5): same finding/context rendering and guardrails, but
    additionally requesting a self-reported confidence and the precise
    line range the replacement covers, since a `RepairProposal` (unlike a
    `SuggestedFix`) is meant to be precise enough for a later phase to act
    on. Same inputs as `build_explanation_prompt`.
    """
    lines = _finding_and_context_lines(finding, context)
    lines += [
        "",
        "If you propose a replacement, name exactly which line numbers "
        "(from the numbered lines above) it replaces.",
        "",
        'Respond with JSON only: {"explanation": "<why this change fixes the finding>", '
        '"replacement": "<the replacement code for the affected line range>", '
        '"confidence": <a number from 0.0 to 1.0 for how confident you are this repair '
        'is correct>, "start_line": <first line number the replacement covers>, '
        '"end_line": <last line number the replacement covers>}'
    ]
    return Prompt(system=_system_prompt(_REPAIR_ROLE), user="\n".join(lines))


def build_summary_prompt(result) -> Prompt:
    """A prompt asking the model to summarize a whole run's findings.

    `result`: anything with `.checked`, `.tools_used`, `.findings` (a real
    `RunResult`, or a test double shaped like one). No surrounding code is
    included - a run-level summary works from the tabulated findings only,
    matching "avoid loading entire files unnecessarily": there is no
    single file to extract context from at run granularity, and code
    context is a per-finding concern (`build_explanation_prompt`,
    `build_fix_prompt`), not a per-run one.
    """
    findings = result.findings
    lines = [
        "Run summary:",
        "  Files checked: {}".format(len(result.checked)),
        "  Tools used: {}".format(", ".join(result.tools_used) or "none"),
        "  Total findings: {}".format(len(findings)),
        "",
    ]
    if not findings:
        lines.append("No findings were reported.")
    else:
        lines.append("Findings:")
        shown = findings[:MAX_SUMMARY_FINDINGS]
        for finding in shown:
            lines.append(
                "  - [{}] {}:{} {} ({})".format(
                    finding.severity, finding.file, finding.line, finding.message, finding.tool
                )
            )
        remaining = len(findings) - len(shown)
        if remaining:
            lines.append("  ...and {} more".format(remaining))
    lines += [
        "",
        'Respond with JSON only: {"summary": "<a short summary of these findings for a developer>"}',
    ]
    return Prompt(system=_system_prompt(_SUMMARY_ROLE), user="\n".join(lines))
