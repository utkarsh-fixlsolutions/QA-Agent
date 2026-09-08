"""AI-generated suggested fixes (Phase D Part 5).

Orchestrates the pipeline Parts 1-2 already built:

    Finding -> extract_context() -> build_fix_prompt() -> provider.generate()
            -> validate_fix_response() -> SuggestedFix

Nothing here builds a prompt, extracts context, or validates a response -
those three steps are reused verbatim from context.py/prompts.py/
response_parser.py; this module only wires them together and decides what
counts as a usable fix. The shape mirrors explainer.py (Phase D Part 3)
exactly - the same optional, per-finding enrichment, just a different kind.

A suggested fix is advisory only: it is never applied, never guaranteed
correct, and always requires developer review before use - QA-Agent never
edits a source file, here or anywhere else in this project. Any failure
along the way - an offline or timed-out provider, a malformed or
incomplete response, or the model's own "insufficient_context" decline -
is not an error from this module's point of view; it simply means no
suggested fix for that finding, never a raised exception and never a
fabricated one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import extract_context
from .prompts import Prompt, build_fix_prompt
from .response_parser import validate_fix_response
from .schemas import STATUS_SUCCESS, FixResponse


@dataclass(frozen=True)
class SuggestedFix:
    """One successfully validated, advisory-only AI suggestion for one
    finding - never applied automatically, never guaranteed correct.

    `title`: what this fix addresses - the finding's own message, not
    model-generated, so it is always grounded in real, already-trusted
    data rather than something the model would otherwise need to restate.
    `explanation`: the model's own reasoning for why the change helps.
    `replacement`: the model's own suggested code or text - a suggestion
    to review, never a patch QA-Agent applies on its own.
    """

    title: str
    explanation: str
    replacement: str


def _prompt_text(prompt: Prompt) -> str:
    """The same one-line bridge explainer.py and summarizer.py already
    established between D1's single-string provider interface and D2's
    system/user-separated Prompt - duplicated here rather than imported,
    so this module stays independent of its siblings (the same small,
    explicitly-flagged duplication this project already accepts
    elsewhere, e.g. SEVERITY_LEVELS between runner.py and config.py).
    """
    return "{}\n\n{}".format(prompt.system, prompt.user)


def suggest_fix(finding, provider, extract=extract_context):
    """Try to suggest a fix for one finding. Returns a `SuggestedFix`, or
    `None` - never raises, never invents a placeholder - for every
    required failure mode: an offline or timed-out provider, a malformed
    or incomplete response, or the model's own "insufficient_context"
    decline.

    `extract` is injectable only so a test can substitute a fixed
    `CodeContext` without touching the real filesystem; real callers never
    need to pass it.
    """
    try:
        context = extract(finding.file, finding.line)
        prompt = build_fix_prompt(finding, context)
        response = provider.generate(_prompt_text(prompt))
        if not response.ok:
            return None
        result = validate_fix_response(response.text)
        if result.status != STATUS_SUCCESS:
            return None
        # ValidationResult.value is typed as plain `object`; STATUS_SUCCESS
        # is response_parser.py's own guarantee that it is really a
        # FixResponse here - narrowed explicitly since pyright cannot
        # infer that from the status check alone.
        assert isinstance(result.value, FixResponse)
        return SuggestedFix(
            title=finding.message,
            explanation=result.value.explanation,
            replacement=result.value.suggested_fix,
        )
    except Exception:  # noqa: BLE001 - AI must never fail the QA run
        return None


def suggest_fixes(findings, provider, extract=extract_context):
    """Try to suggest a fix for each finding independently. Returns a dict
    mapping only the findings that got a usable suggestion to their
    `SuggestedFix` - a finding absent from the result was not given one,
    for any reason. Each finding is fully independent of the others, the
    same execution-isolation principle the deterministic engine already
    uses for tools (docs/13-multi-analyzer-foundation.md).
    """
    fixes = {}
    for finding in findings:
        fix = suggest_fix(finding, provider, extract=extract)
        if fix is not None:
            fixes[finding] = fix
    return fixes
