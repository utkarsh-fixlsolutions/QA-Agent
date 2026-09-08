"""Autonomous verified repair loop: the orchestrator that runs Parts 1-5's
already-built pipeline, end to end, over a bounded set of findings from one
`RunResult` (Phase E Part 6).

This module invents no repair logic of its own - it only calls, in order,
exactly the functions Parts 1-5 already built and already proved correct:

    Finding
        -> propose_repair()          (Part 1)
        -> create_workspace() + apply_repair()   (Part 2)
        -> validate_repair()         (Part 3)
        -> decide_repair()           (Part 4)
        -> apply_verified_repair()   (Part 5, only when decision.action ==
           "accept_candidate" - every other action is recorded and left
           alone, never written)

Every finding is processed exactly once, in `RunResult.findings`' own
deterministic order, up to `max_iterations` - never retried, never re-
proposed, never re-validated. "Stop immediately if: no proposal generated /
validation failed / repair rejected" (this part's own spec) means stop
*that finding's* pipeline at the stage that failed and record why - not
abandon the whole run; "iteration limit reached" is the one condition that
does stop the whole run early, by design, so a repair session can never run
unbounded. Each finding's entire attempt is wrapped in its own error
boundary, so a broken attempt for one finding can never prevent another
finding - possibly in a different file entirely - from being attempted.

Only a `decision.action == "accept_candidate"` outcome ever reaches
`apply_verified_repair()`, and only `apply_verified_repair()`'s own,
already-verified write path ever touches a real project file - this module
performs no writes, no analyzer reruns, and no AI calls of its own; it only
calls the modules that do, and only ever in the safe order above. The LLM
never decides success: every status this module records comes from a
deterministic analyzer (Part 3) or a deterministic policy over that
analyzer's own output (Part 4) - never from a provider's confidence or
explanation text.

A known, deliberate scope boundary, documented rather than engineered
around: two different findings in the same file, both selected in one run,
are each given their own fresh Part 2 workspace and processed completely
independently - if the first one is genuinely applied to the real project,
the second one's own `propose_repair()` (which re-reads the real file
fresh) will see that already-updated file, while the *Finding* object it
was given still carries whatever line number the original, pre-repair
`RunResult` recorded. This can only ever produce a wasted attempt for that
second finding (correctly rejected, held, or failing validation, since
every stage past the proposal is grounded in a real rerun of the real
analyzers against real, current file content) - never an incorrect write.
Tracking or renumbering shifted findings across repairs in the same run
would mean re-analyzing between iterations, which is a repair loop in the
"multiple attempts" sense this part explicitly excludes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..runner import run as _default_run
from .apply import apply_verified_repair
from .context import extract_context
from .decision import ACTION_ACCEPT_CANDIDATE, ACTION_HOLD, ACTION_REJECT, decide_repair
from .repair import propose_repair
from .validator import validate_repair
from .workspace import apply_repair, cleanup_workspace, create_workspace

# Never allow an unbounded run - a caller that gives a real bug (negative,
# wrong type) gets this safe default instead of "no limit"; see
# _effective_max_iterations().
DEFAULT_MAX_ITERATIONS = 3

# Per-finding outcome statuses - every RepairAttemptOutcome has exactly one.
STATUS_NO_PROPOSAL = "no_proposal"          # Part 1 produced no usable proposal
STATUS_WORKSPACE_FAILED = "workspace_failed"  # Part 2 could not create/apply the repair
STATUS_VALIDATION_FAILED = "validation_failed"  # Part 3/4 could not complete a reliable check
STATUS_REJECTED = "rejected"                # Part 4: measurably worse, or improved-with-new-findings
STATUS_HELD = "held"                        # Part 4: no measurable change, or unconfirmed
STATUS_APPLIED = "applied"                  # Part 5 wrote the repair to the real project
STATUS_APPLY_FAILED = "apply_failed"        # Part 4 said accept, but Part 5's write itself failed
STATUS_ERROR = "error"                      # a genuinely unexpected exception during this attempt

# "failed repairs" (RepairLoopStatistics.failed): every outcome that is
# neither a successful application nor a deterministic, correct non-
# acceptance (rejected/held) nor an incomplete validation - i.e. the
# pipeline itself broke down for this finding, or an accepted repair's
# write itself failed.
_FAILED_STATUSES = (STATUS_NO_PROPOSAL, STATUS_WORKSPACE_FAILED, STATUS_APPLY_FAILED, STATUS_ERROR)


@dataclass(frozen=True)
class RepairAttemptOutcome:
    """What happened when this loop tried to repair exactly one finding.

    `finding`: the real `Finding` this attempt was for.
    `status`: one of this module's own `STATUS_*` constants above.
    `detail`: a short, human-readable reason - the same reason string
    `validate_repair`/`decide_repair`/`apply_verified_repair` themselves
    already produced, never rewritten or reinterpreted here.
    `proposal`/`applied_repair`/`validation_result`/`decision`/
    `application_result`: whichever of Parts 1-5's own result objects this
    attempt actually reached before stopping - `None` for any stage never
    reached (e.g. `applied_repair` is `None` when `status` is
    `no_proposal`, since Part 2 was never even called).
    """

    finding: object
    status: str
    detail: str
    proposal: object = None
    applied_repair: object = None
    validation_result: object = None
    decision: object = None
    application_result: object = None


@dataclass(frozen=True)
class RepairLoopStatistics:
    """The structured counts this part requires, all derived from
    `RepairLoopResult.outcomes` - never tracked separately, so they can
    never drift from the actual per-finding results.

    `total_findings`: `len(run_result.findings)` - the full set this run
    had to select from, regardless of how many were actually attempted.
    `attempted`: findings this run actually ran the pipeline for (bounded
    by `max_iterations`) - equal to `iterations_used` in this
    implementation, since one finding is always exactly one iteration
    (no retries) - both are exposed because this part's spec names both.
    `successful`: `status == "applied"` count - the only status that means
    a real file was actually written.
    `failed`: `_FAILED_STATUSES` count - see that constant's own comment.
    `rejected`/`held`/`validation_failed`: their own status counts.
    `iterations_used`: how many findings this run actually processed.
    `elapsed_seconds`: real wall-clock time for the whole run, measured
    with `time.perf_counter()`.
    """

    total_findings: int
    attempted: int
    successful: int
    failed: int
    rejected: int
    held: int
    validation_failed: int
    iterations_used: int
    elapsed_seconds: float


@dataclass(frozen=True)
class RepairLoopResult:
    """The complete outcome of one autonomous repair loop run.

    `overall_success`: whether the *orchestration process itself* ran to
    completion - `True` even when zero repairs were applied (every finding
    correctly rejected/held is a successful run of a working pipeline);
    `False` only when the run could not even begin (no usable
    `RunResult` was given). This is deliberately independent of how many
    repairs were actually applied - that question is answered by
    `statistics.successful`, not by this flag; conflating "the process
    worked" with "everything got fixed" would make a run of all-correctly-
    rejected findings look like a failure when it is not.
    `statistics`: the `RepairLoopStatistics` for this run.
    `outcomes`: every `RepairAttemptOutcome`, in the order attempted.
    `repaired_files`: the distinct real files this run actually wrote to
    (sorted, deduplicated) - derived from `outcomes`, never tracked apart
    from them.
    `failed_files`: the distinct files with at least one attempted-but-not-
    applied finding (any status other than `applied`) - a file can appear
    in both `repaired_files` and `failed_files` if it had more than one
    finding attempted in this run with mixed outcomes.
    """

    overall_success: bool
    statistics: RepairLoopStatistics
    outcomes: tuple = ()
    repaired_files: tuple = ()
    failed_files: tuple = ()


def _effective_max_iterations(max_iterations):
    """A non-negative real int is used as given (0 is a valid, deliberate
    "select findings but attempt none" choice); anything else - negative,
    wrong type, or a bool (an int subclass in Python, the same trap
    guarded against throughout this project) - falls back to
    `DEFAULT_MAX_ITERATIONS`, never to "no limit".
    """
    if isinstance(max_iterations, int) and not isinstance(max_iterations, bool) and max_iterations >= 0:
        return max_iterations
    return DEFAULT_MAX_ITERATIONS


def _process_finding(finding, run_result, provider, config, extract, run):
    """Run the complete verified-repair pipeline for exactly one finding,
    exactly once - propose, apply-to-workspace, validate, decide, and
    (only for an accept_candidate decision) apply for real. Always cleans
    up its own temporary workspace. Never raises: any unexpected exception
    at any stage is caught here and recorded as `STATUS_ERROR`, so one
    finding's failure can never prevent another finding from being
    attempted (this part's own graceful-degradation requirement).
    """
    try:
        proposal = propose_repair(finding, provider, extract=extract)
        if proposal is None:
            return RepairAttemptOutcome(
                finding=finding, status=STATUS_NO_PROPOSAL,
                detail="the AI provider produced no usable repair proposal",
            )

        workspace = create_workspace()
        if workspace is None:
            return RepairAttemptOutcome(
                finding=finding, status=STATUS_WORKSPACE_FAILED,
                detail="could not create a temporary workspace", proposal=proposal,
            )
        try:
            applied = apply_repair(workspace, proposal)
            if not applied.ok:
                return RepairAttemptOutcome(
                    finding=finding, status=STATUS_WORKSPACE_FAILED,
                    detail=applied.error or "workspace repair could not be applied",
                    proposal=proposal, applied_repair=applied,
                )

            validation = validate_repair(run_result, applied, config=config, run=run)
            # decide_repair() (Part 4) is the only place that interprets
            # `validation` - never re-implemented or second-guessed here.
            decision = decide_repair(proposal, applied, validation)

            if decision.action == ACTION_REJECT:
                return RepairAttemptOutcome(
                    finding=finding, status=STATUS_REJECTED, detail=decision.reason,
                    proposal=proposal, applied_repair=applied,
                    validation_result=validation, decision=decision,
                )
            if decision.action == ACTION_HOLD:
                return RepairAttemptOutcome(
                    finding=finding, status=STATUS_HELD, detail=decision.reason,
                    proposal=proposal, applied_repair=applied,
                    validation_result=validation, decision=decision,
                )
            if decision.action != ACTION_ACCEPT_CANDIDATE:
                # decide_repair()'s own fail-closed branch (missing/unusable
                # validation data) - action == "validation_failed".
                return RepairAttemptOutcome(
                    finding=finding, status=STATUS_VALIDATION_FAILED, detail=decision.reason,
                    proposal=proposal, applied_repair=applied,
                    validation_result=validation, decision=decision,
                )

            # decision.action == ACTION_ACCEPT_CANDIDATE: the only outcome
            # that may ever reach a real write (Part 5's own hard rule).
            application = apply_verified_repair(decision)
            status = STATUS_APPLIED if application.success else STATUS_APPLY_FAILED
            return RepairAttemptOutcome(
                finding=finding, status=status, detail=application.reason,
                proposal=proposal, applied_repair=applied,
                validation_result=validation, decision=decision, application_result=application,
            )
        finally:
            cleanup_workspace(workspace)
    except Exception as exc:  # noqa: BLE001 - one finding's failure must never abort the loop
        return RepairAttemptOutcome(
            finding=finding, status=STATUS_ERROR,
            detail="{}: {}".format(type(exc).__name__, exc),
        )


def run_repair_loop(run_result, provider, max_iterations: object = DEFAULT_MAX_ITERATIONS,
                     config=None, extract=extract_context, run=_default_run):
    """Run the autonomous verified-repair loop over `run_result.findings`,
    in their existing deterministic order, up to `max_iterations` findings.

    `run_result`: a real (or duck-typed) `RunResult` - the frozen "before"
    baseline every attempted finding's validation is measured against
    (Part 3's own requirement), and the source of the findings selected.
    `provider`: any `AIProvider`-shaped object (Ollama, Mock, or anything
    else satisfying the Part 1 provider protocol) - this module never
    constructs or chooses one itself.
    `max_iterations`: see `_effective_max_iterations()`.
    `config`: an optional Config, passed straight through to `validate_repair`
    (and from there to the real analyzer rerun) unchanged.
    `extract`/`run`: injectable only so a test can control context
    extraction and the analyzer rerun without touching the real filesystem
    or a real provider - real callers never need to pass either. `run`
    defaults to the real `runner.run` - the same deliberate, narrow
    exception to "qa_agent.ai never imports the pipeline" that
    validator.py itself already established (this module just reuses it).

    Never raises: an invalid `run_result` (missing, or one whose own
    `.findings` cannot even be read - `hasattr()` alone only catches
    `AttributeError`, not an arbitrary exception a broken property might
    raise, so this is checked explicitly, not assumed) returns a
    `RepairLoopResult` with `overall_success=False` and empty statistics
    rather than crashing. A genuinely unexpected exception during any one
    finding's own attempt is caught by `_process_finding` itself and
    recorded as that finding's outcome; this function's own outer
    boundary exists for anything outside a single finding's own attempt -
    reading `run_result.findings` itself, or computing final statistics.
    """
    started = time.perf_counter()

    def _empty_result(overall_success):
        return RepairLoopResult(
            overall_success=overall_success,
            statistics=RepairLoopStatistics(
                total_findings=0, attempted=0, successful=0, failed=0,
                rejected=0, held=0, validation_failed=0,
                iterations_used=0, elapsed_seconds=time.perf_counter() - started,
            ),
        )

    if run_result is None:
        return _empty_result(False)

    try:
        findings = list(run_result.findings)
    except Exception:  # noqa: BLE001 - a broken run_result must not crash the loop
        return _empty_result(False)

    try:
        limit = _effective_max_iterations(max_iterations)

        outcomes: list = []
        for finding in findings:
            if len(outcomes) >= limit:
                break
            outcomes.append(_process_finding(finding, run_result, provider, config, extract, run))

        successful = sum(1 for o in outcomes if o.status == STATUS_APPLIED)
        rejected = sum(1 for o in outcomes if o.status == STATUS_REJECTED)
        held = sum(1 for o in outcomes if o.status == STATUS_HELD)
        validation_failed = sum(1 for o in outcomes if o.status == STATUS_VALIDATION_FAILED)
        failed = sum(1 for o in outcomes if o.status in _FAILED_STATUSES)

        statistics = RepairLoopStatistics(
            total_findings=len(findings), attempted=len(outcomes), successful=successful,
            failed=failed, rejected=rejected, held=held, validation_failed=validation_failed,
            iterations_used=len(outcomes), elapsed_seconds=time.perf_counter() - started,
        )

        repaired_files = tuple(sorted({o.finding.file for o in outcomes if o.status == STATUS_APPLIED}))
        failed_files = tuple(sorted({o.finding.file for o in outcomes if o.status != STATUS_APPLIED}))

        return RepairLoopResult(
            overall_success=True, statistics=statistics, outcomes=tuple(outcomes),
            repaired_files=repaired_files, failed_files=failed_files,
        )
    except Exception:  # noqa: BLE001 - this orchestrator must never crash a caller
        return _empty_result(False)
