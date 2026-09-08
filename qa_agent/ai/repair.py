"""AI repair proposal generation (Phase E Part 1).

Orchestrates the pipeline this project's provider/prompt/validation
infrastructure already supports - nothing here builds a prompt, extracts
context, calls a network transport, or parses JSON; those steps are reused
verbatim from context.py/prompts.py/response_parser.py exactly as
fixer.py's suggest_fix() already does (Phase D Part 5), just against
build_repair_prompt()/validate_repair_response() (Phase E Part 1's own
additions to that same architecture) instead of the fix schema:

    Finding -> extract_context() -> build_repair_prompt() -> provider.generate()
            -> validate_repair_response() -> RepairProposal | None

Hard rules, restated here because this module's whole purpose is code
repair, where the temptation to go further is highest:
- This module NEVER invents a finding. It only ever proposes a repair for
  a Finding a deterministic analyzer already produced - the analyzers
  remain the sole source of truth for what is wrong.
- It NEVER scans a repository for new issues; it has no path-walking or
  tool-invocation code of any kind.
- It NEVER edits a file, writes a patch to disk, re-runs an analyzer to
  validate a repair, or performs a git operation - a RepairProposal is
  data to review, nothing here or anywhere else in this project applies
  it automatically.
- It is not wired into the normal analyze path: runner.py, adapters.py,
  analysis_bridge.py, watch.py, and __main__.py's existing CLI behaviour
  are all untouched by this part and do not import this module. Patch
  application, an approval workflow, a validation loop, and any --repair
  CLI/config surface are all deferred to a later Phase E part because
  implementing them now would expand this step.
"""

from __future__ import annotations

from dataclasses import dataclass

from .context import extract_context
from .prompts import Prompt, build_repair_prompt
from .response_parser import validate_repair_response
from .schemas import STATUS_SUCCESS, RepairResponse


@dataclass(frozen=True)
class RepairProposal:
    """One AI-proposed repair for one already-detected Finding - advisory
    only, suitable for a later phase (patch application, ranking, review
    UI) to build on, but this project never applies one automatically.

    `finding`: the original Finding this proposes to repair - a proposal
    is never generated except in response to a real, already-detected
    finding from a deterministic analyzer.
    `explanation`: the model's own reasoning for why the change helps.
    `replacement`: the model's proposed replacement code for the line
    range [`start_line`, `end_line`] in `file` - text to review, never
    applied by this module or any caller of it.
    `confidence`: the model's own self-reported confidence, clamped to
    [0.0, 1.0] by validate_repair_response - not a QA-Agent-computed score.
    `model`: the provider's `.name` that generated this proposal, so a
    later phase can distinguish or filter proposals by source.
    `file`: the affected file - duplicated from `finding.file` so a caller
    holding only the proposal (not the finding) still knows what it
    affects.
    `start_line` / `end_line`: the 1-indexed, inclusive line range the
    replacement covers. Always set - falls back to the finding's own
    flagged line when the model did not give a usable range, never left
    as a fabricated guess beyond what was actually shown to the model.
    """

    finding: object
    explanation: str
    replacement: str
    confidence: float
    model: str
    file: str
    start_line: int
    end_line: int


def _prompt_text(prompt: Prompt) -> str:
    """The same D1-to-D2 bridge already duplicated across
    explainer.py/summarizer.py/fixer.py - duplicated again rather than
    imported, so this module stays independent of its siblings (the same
    small, explicitly-flagged duplication this project already accepts
    elsewhere, e.g. SEVERITY_LEVELS between runner.py and config.py).
    """
    return "{}\n\n{}".format(prompt.system, prompt.user)


def _line_range(response: RepairResponse, finding):
    """The model's own start_line/end_line when it gave a sane one -
    positive integers with start <= end - else the finding's own flagged
    line for both. A malformed or backwards range is not trusted merely
    because the model claimed it; the finding's own line is real,
    already-trusted data, never invented.
    """
    start, end = response.start_line, response.end_line
    if isinstance(start, int) and isinstance(end, int) and 1 <= start <= end:
        return start, end
    return finding.line, finding.line


def propose_repair(finding, provider, extract=extract_context):
    """Try to propose a repair for one finding. Returns a `RepairProposal`,
    or `None` - never raises - for every required failure mode: an offline
    or timed-out provider, a malformed or incomplete response, the
    model's own "insufficient_context" decline, or a schema-invalid
    response (e.g. missing confidence).

    `extract` is injectable only so a test can substitute a fixed
    `CodeContext` without touching the real filesystem; real callers never
    need to pass it.
    """
    try:
        context = extract(finding.file, finding.line)
        prompt = build_repair_prompt(finding, context)
        response = provider.generate(_prompt_text(prompt))
        if not response.ok:
            return None
        result = validate_repair_response(response.text)
        if result.status != STATUS_SUCCESS:
            return None
        # ValidationResult.value is typed as plain `object`; STATUS_SUCCESS
        # is response_parser.py's own guarantee that it is really a
        # RepairResponse here - narrowed explicitly since pyright cannot
        # infer that from the status check alone.
        assert isinstance(result.value, RepairResponse)
        start_line, end_line = _line_range(result.value, finding)
        return RepairProposal(
            finding=finding,
            explanation=result.value.explanation,
            replacement=result.value.replacement,
            confidence=result.value.confidence,
            model=provider.name,
            file=finding.file,
            start_line=start_line,
            end_line=end_line,
        )
    except Exception:  # noqa: BLE001 - AI must never fail the QA run
        return None


def propose_repairs(findings, provider, extract=extract_context):
    """Try to propose a repair for each finding independently. Returns a
    dict mapping only the findings that got a usable proposal to their
    `RepairProposal` - a finding absent from the result was not given one,
    for any reason. Mirrors suggest_fixes()/explain_findings() exactly
    (Phase D Parts 3/5): the same per-finding execution isolation the
    deterministic engine itself uses for tools
    (docs/13-multi-analyzer-foundation.md) - one finding's repair failing
    never affects another's.
    """
    proposals = {}
    for finding in findings:
        proposal = propose_repair(finding, provider, extract=extract)
        if proposal is not None:
            proposals[finding] = proposal
    return proposals
