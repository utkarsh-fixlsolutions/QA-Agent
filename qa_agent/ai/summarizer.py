"""AI-generated run summaries (Phase D Part 4).

Orchestrates the pipeline Parts 1-2 already built:

    RunResult -> build_summary_prompt() -> provider.generate()
             -> validate_summary_response() -> Summary

Nothing here builds a prompt or validates a response - both steps are
reused verbatim from prompts.py/response_parser.py; this module only wires
them together and decides what counts as a usable summary. The shape here
deliberately mirrors explainer.py (Phase D Part 3) exactly, one level up:
a finding's explanation and a whole run's summary are the same kind of
optional enrichment, just at different granularity.

AI is optional enrichment only: any failure along the way - an offline or
timed-out provider, a malformed or incomplete response, or the model's own
"insufficient_context" decline - is not an error from this module's point
of view. It simply means no summary for this run, never a raised exception
and never a fabricated one. The deterministic pipeline (Parts 1-7) never
imports this module, is never modified by it, and completes exactly the
same with or without it - this module only ever consumes an already-
completed RunResult and hands back a separate value, never touching
RunResult itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from .prompts import Prompt, build_summary_prompt
from .response_parser import validate_summary_response
from .schemas import STATUS_SUCCESS, SummaryResponse


@dataclass(frozen=True)
class Summary:
    """One successfully validated AI summary of a whole run."""

    text: str


def _prompt_text(prompt: Prompt) -> str:
    """The same one-line bridge explainer.py already established between
    D1's single-string provider interface and D2's system/user-separated
    Prompt - duplicated here rather than imported from explainer.py, so
    this module stays independent of its sibling (the same small,
    explicitly-flagged duplication this project already accepts elsewhere,
    e.g. SEVERITY_LEVELS between runner.py and config.py).
    """
    return "{}\n\n{}".format(prompt.system, prompt.user)


def summarize_run(result, provider):
    """Try to summarize one completed run. Returns a `Summary`, or `None` -
    never raises, never invents a placeholder - for every required failure
    mode: an offline or timed-out provider, a malformed or incomplete
    response, or the model's own "insufficient_context" decline.
    """
    try:
        prompt = build_summary_prompt(result)
        response = provider.generate(_prompt_text(prompt))
        if not response.ok:
            return None
        validated = validate_summary_response(response.text)
        if validated.status != STATUS_SUCCESS:
            return None
        # ValidationResult.value is typed as plain `object`; STATUS_SUCCESS
        # is response_parser.py's own guarantee that it is really a
        # SummaryResponse here - narrowed explicitly since pyright cannot
        # infer that from the status check alone.
        assert isinstance(validated.value, SummaryResponse)
        return Summary(text=validated.value.summary)
    except Exception:  # noqa: BLE001 - AI must never fail the QA run
        return None
