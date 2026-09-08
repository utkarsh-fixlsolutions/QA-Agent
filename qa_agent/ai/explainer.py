"""AI-generated finding explanations (Phase D Part 3).

Orchestrates the pipeline Parts 1-2 already built:

    Finding -> extract_context() -> build_explanation_prompt()
            -> provider.generate() -> validate_explanation_response()
            -> Explanation

Nothing here builds a prompt, extracts context, or validates a response -
those three steps are reused verbatim from context.py/prompts.py/
response_parser.py; this module only wires them together and decides what
counts as a usable explanation.

AI is optional enrichment only: any failure along the way - an offline or
timed-out provider, a malformed or incomplete response, or the model's own
"insufficient_context" decline - is not an error from this module's point
of view. It simply means no explanation for that finding, never a raised
exception and never a fabricated one. The deterministic pipeline (Parts
1-7) never imports this module, is never modified by it, and completes
exactly the same with or without it - this module only ever consumes an
already-completed Finding and hands back an external mapping, never
touching the Finding itself (docs/step-log.md, Phase D Part 3).
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import extract_context
from .prompts import Prompt, build_explanation_prompt
from .response_parser import validate_explanation_response
from .schemas import STATUS_SUCCESS, ExplanationResponse


@dataclass(frozen=True)
class Explanation:
    """One successfully validated AI explanation for one finding."""

    text: str


def _prompt_text(prompt: Prompt) -> str:
    """The D1 provider interface takes a single prompt string; D2's Prompt
    keeps system and user text separate for a future chat-style API. This
    is the whole bridge between them - joining the two here, the one place
    a provider is actually called - so neither layer needed to change.
    """
    return "{}\n\n{}".format(prompt.system, prompt.user)


def explain_finding(finding, provider, extract=extract_context):
    """Try to explain one finding. Returns an `Explanation`, or `None` -
    never raises, never invents a placeholder - for every required failure
    mode: an offline or timed-out provider, a malformed or incomplete
    response, or the model's own "insufficient_context" decline.

    `extract` is injectable only so a test can substitute a fixed
    `CodeContext` without touching the real filesystem; real callers never
    need to pass it.
    """
    try:
        context = extract(finding.file, finding.line)
        prompt = build_explanation_prompt(finding, context)
        response = provider.generate(_prompt_text(prompt))
        if not response.ok:
            return None
        result = validate_explanation_response(response.text)
        if result.status != STATUS_SUCCESS:
            return None
        # ValidationResult.value is typed as plain `object`; STATUS_SUCCESS
        # is response_parser.py's own guarantee that it is really an
        # ExplanationResponse here - narrowed explicitly since pyright
        # cannot infer that from the status check alone.
        assert isinstance(result.value, ExplanationResponse)
        return Explanation(text=result.value.explanation)
    except Exception:  # noqa: BLE001 - AI must never fail the QA run
        return None


def explain_findings(findings, provider, extract=extract_context):
    """Try to explain each finding independently. Returns a dict mapping
    only the findings that were successfully explained to their
    `Explanation` - a finding absent from the result was not explained,
    for any reason. Findings are attempted in the given order, but each is
    fully independent: one failing never affects another, the same
    execution-isolation principle the deterministic engine already uses
    for tools (docs/13-multi-analyzer-foundation.md).
    """
    explanations = {}
    for finding in findings:
        explanation = explain_finding(finding, provider, extract=extract)
        if explanation is not None:
            explanations[finding] = explanation
    return explanations
