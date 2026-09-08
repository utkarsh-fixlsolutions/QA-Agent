"""Repair validation: rerunning the real, deterministic analyzers against a
Phase E Part 2 temporary workspace to measure whether an applied repair
actually helped (Phase E Part 3).

This is the one module in `qa_agent.ai` that deliberately imports the
deterministic engine (`from ..runner import run`) rather than the other way
around - the isolation rule proven throughout Phase D/E1/E2 (`runner.py`,
`adapters.py`, `config.py`, `analysis_bridge.py`, `watch.py`, and their
supporting modules never import `qa_agent.ai`) is one-directional. Reusing
the real runner, unmodified, against a real workspace file is this part's
whole purpose - a second, parallel analysis engine is explicitly out of
scope.

Pipeline:

    before_result.findings (frozen "before" snapshot, filtered to the
    repaired file)                       -> the baseline
    AppliedRepair.workspace_file          -> run() again, for real
    compare_results()                     -> ValidationComparison
    _classify()                           -> RepairValidationResult.status

Hard rules restated here because this module's whole purpose is judging a
repair, where the temptation for the AI to grade its own work is highest:
the analyzers - via the same `runner.run()` Phase C already built - are the
only authority; nothing here ever reads `RepairProposal.confidence` or
`.explanation`, calls a provider, or otherwise lets the AI decide its own
outcome. This module only classifies a *measured* result; it never accepts,
rejects, applies, retries, or ranks a repair - all explicitly later Phase E
parts.

Named `RepairValidationResult`, not the bare `ValidationResult` this part's
own spec suggests: `schemas.py` (Phase D Part 2) already exports a
`ValidationResult` for a different concept entirely (validating a raw LLM
response's JSON shape). Reusing that name here would silently shadow it in
`qa_agent.ai`'s namespace - so this result type is named for what it
actually is, one word longer, rather than colliding with an existing public
name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..runner import run as _default_run

STATUS_IMPROVED = "improved"
STATUS_UNCHANGED = "unchanged"
STATUS_WORSENED = "worsened"
STATUS_VALIDATION_FAILED = "validation_failed"


@dataclass(frozen=True)
class ValidationComparison:
    """The measured before/after picture for one applied repair - every
    field here is a plain count or a plain tuple of real `Finding` objects,
    nothing scored or weighted (Phase E Part 3's own explicit boundary;
    scoring/ranking is E4+).

    `before_count`/`after_count`: total findings on the repaired file,
    before (from the frozen baseline) and after (from a real rerun).
    `removed_count`/`introduced_count`: findings present on one side only,
    by the match rule `_match_key()` documents.
    `unchanged_count`: findings present on both sides by that same rule.
    `target_resolved`: whether the specific finding this repair targeted
    disappeared - `True`/`False`, or `None` when no target finding was
    given (validate_repair always gives one when `proposal.finding` is
    set; `None` only for a directly-called `compare_results()`).
    `before_by_tool`/`after_by_tool`, `before_by_severity`/
    `after_by_severity`: plain counts, keyed by the finding's own `tool`/
    `severity` string - never renamed or reclassified.
    `before_tool_errors`/`after_tool_errors`: tool names that had a
    `ToolError` on the "before"/"after" side - `before` reflects the
    original whole-project run exactly as given; `after` is scoped to the
    single-file rerun this part performs.
    `removed_findings`/`introduced_findings`: the actual `Finding` objects
    behind the counts above, in the same deterministic order `runner.run()`
    already sorts them into.
    """

    before_count: int
    after_count: int
    removed_count: int
    introduced_count: int
    unchanged_count: int
    target_resolved: object = None  # bool | None
    before_by_tool: dict = field(default_factory=dict)
    after_by_tool: dict = field(default_factory=dict)
    before_by_severity: dict = field(default_factory=dict)
    after_by_severity: dict = field(default_factory=dict)
    before_tool_errors: tuple = ()
    after_tool_errors: tuple = ()
    removed_findings: tuple = ()
    introduced_findings: tuple = ()


@dataclass(frozen=True)
class RepairValidationResult:
    """The outcome of trying to validate one applied repair.

    `status` is one of the four module-level constants:
    - `improved` - the measured comparison has at least one removed
      finding and no introduced ones (`_classify()`'s exact rule).
    - `unchanged` - nothing removed, nothing introduced.
    - `worsened` - at least one new finding was introduced (checked first,
      regardless of whether anything else also improved - introducing
      anything new outweighs an unrelated improvement, a deliberate,
      documented choice, not a hidden judgment call).
    - `validation_failed` - the check itself could not be completed (a
      malformed/unapplied repair, a missing workspace file, the target
      finding's own tool failing during the rerun, or a genuinely
      unexpected exception) - never a claim about the repair's quality.

    `comparison` is a `ValidationComparison`, present for every status
    except `validation_failed`. `error` explains a `validation_failed`
    outcome; `None` otherwise.
    """

    status: str
    comparison: object = None
    error: object = None


def _match_key(finding):
    """The deterministic identity used to decide whether "the same"
    finding appears on both sides of a comparison: `(basename(file), line,
    tool, message)`.

    `file` is normalized to its basename, not compared in full, because
    the two sides of every comparison this module makes are deliberately
    in different locations - the real project's file on the "before" side,
    a Phase E Part 2 workspace copy of that same file on the "after" side.
    Comparing full paths would make every single "after" finding look new.
    `line`/`tool`/`message` are used exactly as `Finding` already reports
    them - no fuzzy matching, no substring/similarity comparison. `severity`
    is deliberately excluded: a repair could plausibly cause the same
    underlying issue to be reported at a different severity without that
    meaning it is a different issue.

    Simple and deterministic by design (this part's own explicit
    preference over "clever fuzzy matching") - not immune to a genuine
    edge case: two distinct findings that happen to share every one of
    these four values collapse to one entry in `compare_results()`'s
    dict-based matching. Realistic tool output does not do this (the same
    tool does not report the identical message on the identical line
    twice), so this is a documented limitation, not a silent correctness
    bug for real analyzer output.
    """
    return (Path(finding.file).name, finding.line, finding.tool, finding.message)


def _counts_by(findings, attr):
    counts = {}
    for finding in findings:
        key = getattr(finding, attr)
        counts[key] = counts.get(key, 0) + 1
    return counts


def compare_results(before_findings, after_findings, target_finding=None,
                     before_tool_errors=(), after_tool_errors=()):
    """Build a `ValidationComparison` from two already-obtained finding
    lists. Pure data comparison - no analyzer is run here; `validate_repair`
    is the function that actually reruns anything. Exposed on its own so a
    caller that already has both finding lists (e.g. a test, or a future
    phase with its own baseline) never has to re-run analysis just to get
    the comparison math.

    `before_findings`/`after_findings`: iterables of `Finding`-shaped
    objects (`.file`, `.line`, `.tool`, `.message`, `.severity`).
    `target_finding`: the specific finding the repair being validated was
    meant to address, if known - sets `target_resolved` on the result.
    `before_tool_errors`/`after_tool_errors`: `(tool_name, ToolError)`
    pairs, exactly `RunResult.tool_errors`' own shape - only the tool name
    is kept in the result; the exception object itself is a `validate_repair`
    concern (it decides whether a specific tool error blocks validation).
    """
    before_findings = list(before_findings)
    after_findings = list(after_findings)

    # Dict-comprehension order preserves each input list's own order, which
    # is already runner.run()'s deterministic sort - so every derived tuple
    # below stays deterministic with no extra sort needed here.
    before_keys = {_match_key(f): f for f in before_findings}
    after_keys = {_match_key(f): f for f in after_findings}

    removed = [f for key, f in before_keys.items() if key not in after_keys]
    introduced = [f for key, f in after_keys.items() if key not in before_keys]

    target_resolved = None
    if target_finding is not None:
        target_resolved = _match_key(target_finding) not in after_keys

    return ValidationComparison(
        before_count=len(before_findings),
        after_count=len(after_findings),
        removed_count=len(removed),
        introduced_count=len(introduced),
        unchanged_count=len(before_keys) - len(removed),
        target_resolved=target_resolved,
        before_by_tool=_counts_by(before_findings, "tool"),
        after_by_tool=_counts_by(after_findings, "tool"),
        before_by_severity=_counts_by(before_findings, "severity"),
        after_by_severity=_counts_by(after_findings, "severity"),
        before_tool_errors=tuple(name for name, _error in before_tool_errors),
        after_tool_errors=tuple(name for name, _error in after_tool_errors),
        removed_findings=tuple(removed),
        introduced_findings=tuple(introduced),
    )


def _classify(comparison):
    """The measured verdict, from counts alone - no weighting, no policy.

    Order matters: a new finding anywhere marks the outcome `worsened`
    even if something else was also removed in the same comparison -
    introducing a real, new, analyzer-verified problem is treated as
    outweighing an unrelated improvement. This is a deliberate, stated
    rule for this classification step, not an acceptance policy (E4+
    decides what to *do* about a `worsened` result).
    """
    if comparison.introduced_count > 0:
        return STATUS_WORSENED
    if comparison.removed_count > 0:
        return STATUS_IMPROVED
    return STATUS_UNCHANGED


def validate_repair(before_result, applied_repair, config=None, run=_default_run):
    """Validate one Phase E Part 2 `AppliedRepair` by rerunning the real
    analyzers against its workspace copy and comparing to `before_result`.

    `before_result`: the original `RunResult` (or any object with a
    `.findings` list of `Finding`-shaped items) from *before* any repair -
    a frozen snapshot, never re-derived from the patch text and never a
    second AI opinion. Only its findings for the repaired file (matched by
    `Finding.file == applied_repair.proposal.file`, the same path string
    the original analysis produced) are used as the baseline - the rerun
    below is scoped the same way, so both sides compare like with like.
    `applied_repair`: a Phase E Part 2 `AppliedRepair` with `ok=True`.
    `config`: an optional Config (docs/16-configuration-system.md), passed
    straight through to `run()` unchanged - this module never imports
    config.py, matching runner.py's own convention, so validation uses
    whatever adapters/severity floor the original analysis used.
    `run`: injectable only so a test can supply a fake `RunResult` without
    invoking a real analyzer; real callers never need to pass it.

    Analyzer run scope: exactly `[applied_repair.workspace_file]` - the one
    file this repair touched, not the whole workspace. A workspace can
    hold copies of several unrelated files across several repairs (Phase E
    Part 2's own "reuse an existing copy" design for sequential repairs);
    analyzing the whole workspace directory would mix another repair's
    findings into this one's comparison. Scoping to the single patched
    file keeps this bounded and keeps "before" and "after" comparable.

    Never raises: every required failure mode - a repair that was not
    successfully applied, a missing workspace file, a missing baseline, the
    target finding's own tool failing during the rerun (see below), or any
    unexpected exception - returns `RepairValidationResult(status=
    "validation_failed", error=...)` instead.
    """
    try:
        if applied_repair is None or not getattr(applied_repair, "ok", False):
            return RepairValidationResult(
                status=STATUS_VALIDATION_FAILED,
                error="no successfully applied repair to validate",
            )
        proposal = getattr(applied_repair, "proposal", None)
        workspace_file = getattr(applied_repair, "workspace_file", None)
        if proposal is None or workspace_file is None:
            return RepairValidationResult(
                status=STATUS_VALIDATION_FAILED,
                error="applied repair is missing its proposal or workspace_file",
            )
        if not Path(workspace_file).is_file():
            return RepairValidationResult(
                status=STATUS_VALIDATION_FAILED,
                error="workspace file not found: '{}'".format(workspace_file),
            )
        if before_result is None or not hasattr(before_result, "findings"):
            return RepairValidationResult(
                status=STATUS_VALIDATION_FAILED,
                error="no baseline RunResult/snapshot given",
            )

        original_file = getattr(proposal, "file", None)
        before_findings = [
            finding for finding in before_result.findings
            if getattr(finding, "file", None) == original_file
        ]
        before_tool_errors = getattr(before_result, "tool_errors", None) or []

        after_result = run([workspace_file], config=config)

        target_finding = getattr(proposal, "finding", None)
        target_tool = getattr(target_finding, "tool", None) if target_finding is not None else None
        after_tool_error_names = {name for name, _error in (after_result.tool_errors or [])}
        if target_tool is not None and target_tool in after_tool_error_names:
            # The one tool whose output we need to know whether the target
            # finding is gone did not run at all - "no finding" and "the
            # tool never ran" are indistinguishable from the findings list
            # alone, so this must not be silently read as "improved"
            # (docs/step-log.md, Phase E Part 3 - discovered via dogfooding:
            # a workspace copy has no sibling eslint.config.js, so an
            # eslint-authored repair genuinely fails to validate this way).
            failed_error = next(
                error for name, error in after_result.tool_errors if name == target_tool
            )
            return RepairValidationResult(
                status=STATUS_VALIDATION_FAILED,
                error="'{}' failed during validation, so it is unknown whether the "
                      "target finding was resolved: {}".format(target_tool, failed_error),
            )

        comparison = compare_results(
            before_findings, after_result.findings, target_finding=target_finding,
            before_tool_errors=before_tool_errors, after_tool_errors=after_result.tool_errors,
        )
        return RepairValidationResult(status=_classify(comparison), comparison=comparison)
    except Exception as exc:  # noqa: BLE001 - validation must never crash a caller
        return RepairValidationResult(
            status=STATUS_VALIDATION_FAILED,
            error="{}: {}".format(type(exc).__name__, exc),
        )
