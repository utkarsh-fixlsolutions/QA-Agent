"""Runtime Failure -> Verified Repair Integration (Phase G Part 4,
docs/24-runtime-repair-integration.md): the thin integration layer that lets
an already-diagnosed runtime failure (Phase G Part 3's `RuntimeDiagnosis`)
enter the existing, unmodified Phase E verified-repair pipeline.

    RuntimeDiagnosis -> check_repair_eligibility() [deterministic, no AI]
                      -> resolve_repair_target()    [deterministic, no AI]
                      -> propose_runtime_repair()    (new prompt, existing
                         RepairProposal/validate_repair_response contract)
                      -> apply_repair()              (Phase E Part 2, as-is)
                      -> validate_repair()           (Phase E Part 3, as-is)
                      -> decide_repair()             (Phase E Part 4, as-is)
                      -> [a real runtime check, re-run inside the same
                         temporary workspace against a patched candidate
                         copy - the primary signal a *runtime* repair
                         actually needs, decide_repair() alone cannot
                         provide it]
                      -> apply_verified_repair()     (Phase E Part 5, as-is)
                      -> [the real runtime check, re-run for real, against
                         the real repository - the one and only signal
                         VERIFIED is ever computed from]
                      -> RuntimeRepairResult

No new RepairProposal system, workspace system, validator, decision policy,
file-writing system, atomic-apply mechanism, or repair loop exists here -
every write anywhere in this module is Phase E's own, called unmodified.
This module's only new logic is: (1) a deterministic eligibility gate over
already-produced data, (2) a deterministic file/line localizer that never
invents a location, (3) a new prompt tailored to runtime evidence (reusing
Phase E Part 1's own JSON response schema so `validate_repair_response`
parses it unmodified), and (4) materializing a runnable *candidate* copy of
the project inside the same Phase E Part 2 `TemporaryWorkspace` so the real
runtime check itself - not just a static-analyzer rerun - can be tried
before anything real is written.

**Why decide_repair()'s own verdict is not treated as sufficient on its
own for a runtime repair, stated plainly rather than glossed over:**
`decide_repair()`'s "target resolved" signal (Phase E Part 4) is computed
from a `Finding`-shaped match key that includes `.tool` - and this module's
`RepairProposal.finding` is never a real static-analysis `Finding` (there
is no such thing for "the process crashed at startup"), so its `.tool` is
fixed to a sentinel (`"runtime-diagnosis"`) no real analyzer ever produces.
That makes `target_resolved` structurally always `True` for a runtime
repair - real, but degenerate: `decide_repair()`'s outcome for this module
collapses to "did the file's own static-analysis findings improve/stay
unchanged/worsen," never "was the specific runtime problem resolved,"
because no static tool detects a missing runtime module or a crashed
process in the first place. This module never hides that: `decide_repair()`
is still used, unmodified, as a **static-regression guard only** (a
`worsened` verdict always vetoes a candidate outright, real and correct);
when a candidate materializes and its own runtime check actually runs, that
real runtime outcome - not decide_repair()'s own "improved" branch - is
what drives ACCEPT vs REJECT. See `_combine_decision()` below.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path

from ..runner import IGNORED_DIRS as _RUNNER_IGNORED_DIRS
from ..runner import run as _default_run
from ..runtime import STATUS_PASS
from ..runtime import run_runtime_plan as _execute_runtime_plan
from .apply import apply_verified_repair
from .context import extract_context
from .decision import ACTION_ACCEPT_CANDIDATE, ACTION_HOLD, ACTION_REJECT, decide_repair
from .diagnosis_models import DIAGNOSIS_DIAGNOSED, DIAGNOSIS_NOT_APPLICABLE
from .repair import RepairProposal
from .response_parser import validate_repair_response
from .runtime_repair_models import (
    APPLY_NOT_ATTEMPTED,
    ELIGIBILITY_ELIGIBLE,
    ELIGIBILITY_NOT_ELIGIBLE,
    OUTCOME_ACCEPTED,
    OUTCOME_APPLIED,
    OUTCOME_APPLIED_BUT_STILL_FAILING,
    OUTCOME_ERROR,
    OUTCOME_HELD,
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_PROPOSAL_FAILED,
    OUTCOME_REJECTED,
    OUTCOME_VALIDATION_FAILED,
    OUTCOME_VERIFIED,
    VERIFICATION_NOT_APPLICABLE,
    VERIFICATION_STILL_FAILING,
    VERIFICATION_UNKNOWN,
    VERIFICATION_VERIFIED,
    EligibilityResult,
    RuntimeRepairResult,
    TargetResolution,
)
from .runtime_repair_prompts import build_runtime_repair_prompt
from .schemas import STATUS_SUCCESS, RepairResponse
from .validator import validate_repair
from .workspace import apply_repair, cleanup_workspace, create_workspace

# --- deterministic target denylist -----------------------------------------
#
# Extends runner.py's own IGNORED_DIRS (already reused as-is by
# qa_agent/project/discovery.py - the same established, one-directional
# "read one plain constant" precedent, applied a second time here) with
# build-output directories that IGNORED_DIRS has no reason to know about
# (it is about walking *source* files for analysis, not about identifying
# safe repair targets). A repair target inside any of these, or a
# lockfile by name, is never eligible - these are generated or dependency-
# managed, not source this project should ever ask an AI to rewrite.
_GENERATED_DIR_NAMES = frozenset(_RUNNER_IGNORED_DIRS) | {
    "dist", "build", "out", ".tox", ".pytest_cache", ".next", "coverage",
}
_LOCK_FILE_NAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
})

_QUOTED_RE = re.compile(r"['\"]([^'\"]{2,80})['\"]")


def _read_text_safe(path: Path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _resolve_within_root(candidate: str, root):
    """The one real file `candidate` names, if any - never a fuzzy search,
    never a basename match, never a guess. `None` if it does not exist, is
    not a plain file, or would resolve outside `root` (a path-traversal
    attempt is refused exactly like a nonexistent file, never followed).
    """
    root_resolved = Path(root).resolve()
    candidate_path = Path(candidate)
    attempt = candidate_path if candidate_path.is_absolute() else root_resolved / candidate_path
    try:
        attempt_resolved = attempt.resolve()
    except OSError:
        return None
    if not attempt_resolved.is_file():
        return None
    try:
        attempt_resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return attempt_resolved


def _is_generated_or_dependency_path(path: Path, root) -> bool:
    if path.name in _LOCK_FILE_NAMES:
        return True
    relative = path.relative_to(Path(root).resolve())
    return any(part in _GENERATED_DIR_NAMES for part in relative.parts[:-1])


def _evidence_tokens(diagnosis, check_result, runtime_check):
    """Real, quoted or named substrings from the actual evidence this
    diagnosis was given - candidates for a deterministic, literal search
    inside the target file (see `_locate_line`). Never a fabricated token:
    every entry here already appeared somewhere in real output.
    """
    tokens = list(diagnosis.affected_components)
    if runtime_check is not None:
        tokens.extend(runtime_check.required_evidence)
    sources = [
        check_result.reason or "", check_result.exception or "", " ".join(check_result.logs),
    ]
    sources.extend(diagnosis.observed_evidence)
    for text in sources:
        tokens.extend(_QUOTED_RE.findall(text))
    seen = set()
    result = []
    for token in tokens:
        token = token.strip()
        if len(token) < 2 or token.lower() in seen:
            continue
        seen.add(token.lower())
        result.append(token)
    return result


def _locate_line(text, tokens):
    """The first line in `text` containing a real evidence token, or `None`
    if no token matches anywhere - never an invented line number. Bounded,
    literal substring search only, matching this project's own "structural,
    not a fact-checker" precedent (`diagnosis_parser.response_is_grounded`).
    """
    if not tokens:
        return None
    lines = text.splitlines()
    for token in tokens:
        for index, line in enumerate(lines, start=1):
            if token in line:
                return index
    return None


def resolve_repair_target(diagnosis, check_result, root, runtime_check=None) -> TargetResolution:
    """The deterministic bridge from a `RuntimeDiagnosis`'s free-text
    `affected_files` claim to a precise, real repair target - never simply
    `diagnosis.affected_files[0]` handed straight to Phase E. Refuses
    (`ok=False`) rather than guesses for every ambiguous or unresolvable
    case: no file named, more than one file named, the named file does not
    exist, or it resolves to a generated/dependency artifact.
    """
    affected = diagnosis.affected_files
    if not affected:
        return TargetResolution(ok=False, reason="diagnosis names no affected file")
    if len(affected) > 1:
        return TargetResolution(
            ok=False,
            reason="diagnosis names multiple possible files ({}); ambiguous target, not guessing"
                   .format(", ".join(affected)),
        )
    candidate = affected[0]
    resolved = _resolve_within_root(candidate, root)
    if resolved is None:
        return TargetResolution(
            ok=False, reason="affected file '{}' does not exist in the repository".format(candidate),
        )
    if _is_generated_or_dependency_path(resolved, root):
        return TargetResolution(
            ok=False,
            reason="affected file '{}' is a generated/dependency artifact, not a source file "
                   "this project can safely repair".format(candidate),
        )
    text = _read_text_safe(resolved)
    if text is None:
        return TargetResolution(ok=False, reason="affected file '{}' could not be read".format(candidate))
    tokens = _evidence_tokens(diagnosis, check_result, runtime_check)
    line = _locate_line(text, tokens)
    localized = line is not None
    return TargetResolution(
        ok=True, file=str(resolved), relative_file=candidate, line=(line or 1), localized=localized,
        reason=("matched real evidence on line {}".format(line) if localized else
                "no evidence token matched a specific line in the target file; anchored at line 1, "
                "not invented"),
    )


def check_repair_eligibility(diagnosis, check_result, repository_context, root, runtime_check=None) -> EligibilityResult:
    """A deterministic gate over already-produced structured data only -
    the AI is never consulted to decide eligibility (docs/24's own explicit
    rule). Every `RuntimeDiagnosis` whose `diagnosis_status` is not
    `DIAGNOSED` (INSUFFICIENT_CONTEXT/INVALID_RESPONSE/AI_ERROR/
    NOT_APPLICABLE) is refused here, unconditionally.
    """
    if diagnosis is None:
        return EligibilityResult(eligible=False, reason="no diagnosis available")
    if diagnosis.diagnosis_status != DIAGNOSIS_DIAGNOSED:
        return EligibilityResult(
            eligible=False,
            reason="diagnosis status is '{}', not a completed diagnosis".format(diagnosis.diagnosis_status),
        )
    if not diagnosis.recommended_action.strip():
        return EligibilityResult(eligible=False, reason="diagnosis has no actionable recommendation")
    if not diagnosis.observed_evidence:
        return EligibilityResult(eligible=False, reason="diagnosis cites no observed evidence")
    target = resolve_repair_target(diagnosis, check_result, root, runtime_check=runtime_check)
    if not target.ok:
        return EligibilityResult(eligible=False, reason=target.reason, target=target)
    return EligibilityResult(
        eligible=True,
        reason="diagnosis is DIAGNOSED, cites evidence, and names one real, resolvable source file",
        target=target,
    )


# --- repair proposal (reuses Phase E Part 1's RepairProposal/validator) ----

@dataclass(frozen=True)
class _RuntimeRepairFinding:
    """The minimal duck-typed shape `RepairProposal.finding` and
    `build_repair_prompt`'s own convention need (`.file`/`.line`/
    `.severity`/`.message`/`.tool`) - deliberately not a real
    `adapters.Finding` (whose own docstring is explicit: "One real issue
    reported by a tool. Never constructed from anything else."). A runtime
    diagnosis is not a tool finding; `tool` is fixed to a sentinel no real
    static-analysis adapter could ever produce, so it can never be mistaken
    for one downstream (see this module's own docstring for why that
    matters to `decide_repair()`'s target-resolution signal).
    """

    file: str
    line: int
    severity: str
    message: str
    tool: str = "runtime-diagnosis"


def _prompt_text(prompt) -> str:
    return "{}\n\n{}".format(prompt.system, prompt.user)


def _line_range(response: RepairResponse, fallback_line: int):
    start, end = response.start_line, response.end_line
    if isinstance(start, int) and isinstance(end, int) and 1 <= start <= end:
        return start, end
    return fallback_line, fallback_line


def propose_runtime_repair(check_result, diagnosis, repository_context, target, provider,
                            runtime_check=None, extract=extract_context):
    """Try to propose a repair for one resolved `TargetResolution`. Returns
    a `RepairProposal` (Phase E Part 1's own type, unmodified), or `None` -
    never raises - for every required failure mode: an offline/timed-out
    provider, a malformed response, the model's own insufficient-context
    decline, or a schema-invalid response.
    """
    try:
        context = extract(target.file, target.line)
        prompt = build_runtime_repair_prompt(
            check_result, diagnosis, repository_context, context, target.file, runtime_check=runtime_check,
        )
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
        start_line, end_line = _line_range(result.value, target.line)
        finding = _RuntimeRepairFinding(
            file=target.file, line=target.line, severity=(diagnosis.severity or "error"),
            message=diagnosis.summary,
        )
        return RepairProposal(
            finding=finding, explanation=result.value.explanation, replacement=result.value.replacement,
            confidence=result.value.confidence, model=provider.name,
            file=target.file, start_line=start_line, end_line=end_line,
        )
    except Exception:  # noqa: BLE001 - AI must never fail the QA run
        return None


# --- candidate materialization (still the same, one Phase E Part 2 workspace) --

def _link_entry(source: Path, dest: Path) -> bool:
    """Make `dest` a cheap reference to `source` - a symlink, falling back
    to a hardlink for a plain file, falling back to a real copy as a last
    resort for an environment that permits neither. Never opens `source`
    for writing under any of the three; the real repository is never
    touched by this function.
    """
    try:
        if source.is_dir():
            os.symlink(str(source), str(dest), target_is_directory=True)
        else:
            os.symlink(str(source), str(dest))
        return True
    except OSError:
        pass
    try:
        if source.is_file():
            os.link(str(source), str(dest))
            return True
    except OSError:
        pass
    try:
        if source.is_dir():
            shutil.copytree(source, dest)
        else:
            shutil.copy2(source, dest)
        return True
    except OSError:
        return False


def _mirror_with_patch(source_dir: Path, dest_dir: Path, remaining_parts, patched_bytes: bytes) -> bool:
    """Recreate `source_dir` at `dest_dir`, cheaply linking every entry
    except the one named by `remaining_parts` (the repair target), which is
    written for real as `patched_bytes` - a distinct file the real
    repository never sees a write to. Only the directories actually on the
    path to the target are ever created for real; every sibling at every
    level is a link, not a copy, so this stays cheap even for a large
    `node_modules` next to the target file. Returns `False` (never raises)
    the moment any step cannot be completed - the caller then treats
    runtime candidate verification as unavailable in this environment,
    never guesses past a partial mirror.
    """
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    if not remaining_parts:
        return True
    head, rest = remaining_parts[0], remaining_parts[1:]
    try:
        entries = list(source_dir.iterdir())
    except OSError:
        return False
    for entry in entries:
        if entry.name == head:
            continue
        if not _link_entry(entry, dest_dir / entry.name):
            return False
    child_source, child_dest = source_dir / head, dest_dir / head
    if rest:
        if not child_source.is_dir():
            return False
        return _mirror_with_patch(child_source, child_dest, rest, patched_bytes)
    try:
        child_dest.write_bytes(patched_bytes)
        return True
    except OSError:
        return False


def _materialize_candidate_root(root, relative_parts, patched_bytes, workspace):
    if not relative_parts:
        return None
    dest_root = workspace.root / "runtime_candidate"
    ok = _mirror_with_patch(Path(root).resolve(), dest_root, relative_parts, patched_bytes)
    return dest_root if ok else None


def _run_single_check(plan, check, root, config=None):
    """Re-run exactly one `RuntimeCheck`, via the real, unmodified Phase G
    Part 2 executor (`run_runtime_plan`) - no second execution engine, no
    new subprocess logic anywhere in this module. `root` may be the real
    repository (mandatory post-apply verification) or a materialized
    candidate copy (best-effort pre-apply verification); either way this is
    the exact same call, with a plan holding only the one check.
    """
    try:
        single_plan = replace(plan, checks=(check,))
        execution = _execute_runtime_plan(single_plan, root, config)
        return execution.results[0] if execution.results else None
    except Exception:  # noqa: BLE001 - a re-check must never crash the caller
        return None


def _combine_decision(static_decision, runtime_candidate_status):
    """The one small piece of new decision logic this module adds, on top
    of - never instead of - `decide_repair()` (Phase E Part 4, called
    unmodified just before this). See this module's own docstring for why
    `decide_repair()`'s own "improved" verdict is not sufficient on its own
    for a runtime repair.

    Rules, in order:
    1. `decide_repair()` said `reject` (a real static regression) -> reject,
       always. A runtime fix that measurably worsens static analysis is
       never accepted regardless of what the runtime check reports.
    2. A runtime candidate check was actually run and it passed -> accept.
       This is the real ground truth for a *runtime* repair, and can
       override a static `hold`/`validation_failed` verdict - static tools
       have no way to detect "the process crashed at startup" in the first
       place, so "no measurable static change" must not block a genuinely
       verified runtime fix.
    3. A runtime candidate check was actually run and it did not pass ->
       reject. Strong, direct evidence the patch did not fix the real
       problem, regardless of what static analysis alone said.
    4. No runtime candidate check could be run (materialization was not
       possible in this environment, or no originating RuntimeCheck was
       known) -> fall back to `decide_repair()`'s own verdict unchanged,
       honestly: this module never claims a runtime-level confirmation it
       could not actually obtain.

    Returns the final action string (one of decide_repair()'s own
    `ACTION_*` constants) - never a new action vocabulary.
    """
    if static_decision.action == ACTION_REJECT:
        return ACTION_REJECT
    if runtime_candidate_status is not None:
        return ACTION_ACCEPT_CANDIDATE if runtime_candidate_status == STATUS_PASS else ACTION_REJECT
    return static_decision.action


def _effective_decision(static_decision, final_action):
    """The `RepairDecision` this module actually acts on - `static_decision`
    unchanged when `_combine_decision` agreed with it, or a new
    `RepairDecision` (via `dataclasses.replace`, never a hand-built one)
    carrying `final_action` and an explanatory reason otherwise. Necessary
    because `apply_verified_repair` (Phase E Part 5) reads `decision.action`
    directly - handing it `static_decision` unmodified after an override
    would make it refuse a candidate this module just decided to accept (or
    silently accept one it decided to reject).
    """
    if final_action == static_decision.action:
        return static_decision
    if final_action == ACTION_ACCEPT_CANDIDATE:
        reason = ("static validation alone said '{}', but the candidate's own real runtime check "
                   "passed after the patch - accepting on that stronger evidence"
                   .format(static_decision.action))
    else:
        reason = ("the candidate's own real runtime check did not pass after the patch, overriding "
                   "static validation's own '{}' verdict".format(static_decision.action))
    return replace(static_decision, action=final_action, reason=reason)


def repair_runtime_failure(check_result, diagnosis, execution_result, repository_context, provider, root,
                            runtime_check=None, config=None, extract=extract_context, run=_default_run,
                            execute_check=_run_single_check, apply=apply_verified_repair) -> RuntimeRepairResult:
    """Attempt exactly one repair for one already-diagnosed runtime
    failure. Always returns a `RuntimeRepairResult` - never raises, never
    retries, never loops - one runtime failure gets at most one repair
    attempt in G4 (docs/24's own bounded-attempts rule).

    `run`/`extract` are the same injection points `propose_repair`/
    `validate_repair` already accept, reused here unmodified. `execute_check`
    is this module's own equivalent for the one new kind of rerun G4 adds
    (re-executing the real runtime check, candidate and final) - defaults to
    `_run_single_check` (the real, unmodified Phase G Part 2 executor);
    injectable only so a test can control the candidate/final runtime
    outcomes deterministically without a real subprocess, the same role
    `run=` already plays for the static analyzer rerun. `apply` is
    `apply_verified_repair` (Phase E Part 5) itself by default; injectable
    only so a test can force a real-apply failure deterministically without
    needing to actually corrupt a real file mid-run.
    """
    base = dict(
        check_id=check_result.id, check_name=check_result.name,
        original_runtime_status=check_result.status,
        diagnosis_status=(diagnosis.diagnosis_status if diagnosis is not None else DIAGNOSIS_NOT_APPLICABLE),
        affected_files=(diagnosis.affected_files if diagnosis is not None else ()),
    )
    try:
        eligibility = check_repair_eligibility(diagnosis, check_result, repository_context, root,
                                                runtime_check=runtime_check)
        if not eligibility.eligible:
            return RuntimeRepairResult(
                outcome=OUTCOME_NOT_ELIGIBLE, eligibility=eligibility, target_resolution=eligibility.target,
                explanation="not eligible for repair: {}".format(eligibility.reason), **base,
            )
        target = eligibility.target
        assert target is not None and target.ok

        proposal = propose_runtime_repair(
            check_result, diagnosis, repository_context, target, provider,
            runtime_check=runtime_check, extract=extract,
        )
        if proposal is None:
            return RuntimeRepairResult(
                outcome=OUTCOME_PROPOSAL_FAILED, eligibility=eligibility, target_resolution=target,
                explanation="the AI provider produced no usable repair proposal for '{}'".format(target.file),
                **base,
            )

        workspace = create_workspace()
        if workspace is None:
            return RuntimeRepairResult(
                outcome=OUTCOME_ERROR, eligibility=eligibility, target_resolution=target,
                repair_proposal=proposal, explanation="could not create a temporary workspace",
                error="create_workspace() returned None", **base,
            )
        try:
            applied = apply_repair(workspace, proposal)
            if not applied.ok:
                return RuntimeRepairResult(
                    outcome=OUTCOME_PROPOSAL_FAILED, eligibility=eligibility, target_resolution=target,
                    repair_proposal=proposal, applied_repair=applied,
                    explanation="the proposed repair could not be applied in the temporary workspace: {}"
                                .format(applied.error),
                    **base,
                )

            before_result = run([target.file], config=config)
            validation = validate_repair(before_result, applied, config=config, run=run)
            static_decision = decide_repair(proposal, applied, validation)

            runtime_candidate_status = None
            if static_decision.action != ACTION_REJECT and runtime_check is not None:
                try:
                    patched_bytes = Path(applied.workspace_file).read_bytes()
                    relative_parts = Path(target.file).resolve().relative_to(Path(root).resolve()).parts
                    candidate_root = _materialize_candidate_root(root, relative_parts, patched_bytes, workspace)
                except (OSError, ValueError):
                    candidate_root = None
                if candidate_root is not None:
                    candidate_result = execute_check(execution_result.plan, runtime_check, candidate_root,
                                                       config=None)
                    if candidate_result is not None:
                        runtime_candidate_status = candidate_result.status

            final_action = _combine_decision(static_decision, runtime_candidate_status)
            effective_decision = _effective_decision(static_decision, final_action)

            if final_action == ACTION_REJECT:
                return RuntimeRepairResult(
                    outcome=OUTCOME_REJECTED, eligibility=eligibility, target_resolution=target,
                    repair_proposal=proposal, applied_repair=applied, validation_result=validation,
                    runtime_candidate_status=runtime_candidate_status, deterministic_decision=effective_decision,
                    explanation=effective_decision.reason, **base,
                )
            if final_action != ACTION_ACCEPT_CANDIDATE:
                outcome = OUTCOME_HELD if final_action == ACTION_HOLD else OUTCOME_VALIDATION_FAILED
                return RuntimeRepairResult(
                    outcome=outcome, eligibility=eligibility, target_resolution=target,
                    repair_proposal=proposal, applied_repair=applied, validation_result=validation,
                    runtime_candidate_status=runtime_candidate_status, deterministic_decision=effective_decision,
                    explanation=effective_decision.reason, **base,
                )

            # final_action == ACCEPT_CANDIDATE: static analysis did not
            # regress, and either the runtime candidate check actually
            # passed, or none could be attempted (honestly reported above).
            application = apply(effective_decision)
            if not application.success:
                return RuntimeRepairResult(
                    outcome=OUTCOME_ERROR, eligibility=eligibility, target_resolution=target,
                    repair_proposal=proposal, applied_repair=applied, validation_result=validation,
                    runtime_candidate_status=runtime_candidate_status, deterministic_decision=effective_decision,
                    apply_status=application.reason, apply_result=application,
                    explanation="accepted candidate could not be applied to the real repository: {}"
                                .format(application.reason),
                    error=application.error, **base,
                )

            # Mandatory real re-verification - the one and only signal
            # VERIFIED is ever computed from (docs/24's own central rule:
            # ACCEPTED is never reported as VERIFIED on its own).
            final_status = None
            if runtime_check is not None:
                post_result = execute_check(execution_result.plan, runtime_check, root, config=None)
                final_status = post_result.status if post_result is not None else None

            if final_status == STATUS_PASS:
                outcome, verification = OUTCOME_VERIFIED, VERIFICATION_VERIFIED
                explanation = "repair applied and verified: the real runtime check now passes"
            elif final_status is not None:
                outcome, verification = OUTCOME_APPLIED_BUT_STILL_FAILING, VERIFICATION_STILL_FAILING
                explanation = ("repair applied to the real repository, but the real runtime check still "
                                "reports '{}' - not reporting success".format(final_status))
            else:
                outcome, verification = OUTCOME_APPLIED, VERIFICATION_UNKNOWN
                explanation = ("repair applied to the real repository, but post-apply verification could not "
                                "be run (no originating RuntimeCheck was known) - not claiming success")

            return RuntimeRepairResult(
                outcome=outcome, eligibility=eligibility, target_resolution=target,
                repair_proposal=proposal, applied_repair=applied, validation_result=validation,
                runtime_candidate_status=runtime_candidate_status, deterministic_decision=effective_decision,
                apply_status=application.reason, apply_result=application,
                final_runtime_status=final_status, verification_status=verification,
                explanation=explanation, **base,
            )
        finally:
            cleanup_workspace(workspace)
    except Exception as exc:  # noqa: BLE001 - a repair attempt must never crash a caller
        return RuntimeRepairResult(
            outcome=OUTCOME_ERROR,
            eligibility=EligibilityResult(eligible=False, reason="an unexpected error occurred"),
            explanation="unexpected error during repair: {}: {}".format(type(exc).__name__, exc),
            error="{}: {}".format(type(exc).__name__, exc),
            **base,
        )


def repair_runtime_failures(execution_result, diagnoses, repository_context, provider, root, config=None):
    """Attempt a repair for every diagnosis whose check actually failed
    (`diagnosis_status != NOT_APPLICABLE`) - a PASS/SKIPPED/NOT_IMPLEMENTED
    check's `NOT_APPLICABLE` diagnosis is skipped entirely, matching this
    project's own established convention (`diagnose_runtime_failure` itself
    makes zero calls for those). Each attempted check gets exactly one
    `RuntimeRepairResult`, fully independent of every other - one check's
    repair attempt (or crash) never affects another's, the same execution-
    isolation principle every prior phase's engine already uses.
    """
    checks_by_id = {check.id: check for check in execution_result.plan.checks}
    results_by_id = {result.id: result for result in execution_result.results}
    outcomes = []
    for diagnosis in diagnoses:
        if diagnosis.diagnosis_status == DIAGNOSIS_NOT_APPLICABLE:
            continue
        check_result = results_by_id.get(diagnosis.check_id)
        if check_result is None:
            continue
        runtime_check = checks_by_id.get(diagnosis.check_id)
        outcomes.append(
            repair_runtime_failure(
                check_result, diagnosis, execution_result, repository_context, provider, root,
                runtime_check=runtime_check, config=config,
            )
        )
    return tuple(outcomes)


# --- rendering/serialization ------------------------------------------------

def render_runtime_repair_result(result: RuntimeRepairResult) -> str:
    """Plain-text rendering, always explicitly labeled "Runtime Repair" -
    never presented in a way that could be mistaken for the deterministic
    runtime result or the AI diagnosis it builds on.
    """
    if result.outcome == OUTCOME_NOT_ELIGIBLE:
        return "\n".join([
            "  Runtime Repair: not attempted",
            "    Reason: {}".format(result.eligibility.reason),
        ])
    lines = ["  Runtime Repair:", "    Outcome: {}".format(result.outcome)]
    if result.target_resolution is not None and result.target_resolution.ok:
        lines.append("    Target file: {} (line {})".format(
            result.target_resolution.file, result.target_resolution.line))
    if result.repair_proposal is not None:
        lines.append("    Proposal confidence: {:.2f}".format(result.repair_proposal.confidence))
    if result.runtime_candidate_status is not None:
        lines.append("    Candidate runtime check status: {}".format(result.runtime_candidate_status))
    if result.deterministic_decision is not None:
        lines.append("    Deterministic decision: {} ({})".format(
            result.deterministic_decision.action, result.deterministic_decision.reason))
    if result.apply_result is not None:
        lines.append("    Real apply: {}".format(result.apply_status))
    if result.final_runtime_status is not None:
        lines.append("    Final runtime status (real re-check): {}".format(result.final_runtime_status))
    lines.append("    Verification: {}".format(result.verification_status))
    lines.append("    {}".format(result.explanation))
    if result.error:
        lines.append("    Error: {}".format(result.error))
    return "\n".join(lines)


def runtime_repair_result_to_dict(result: RuntimeRepairResult) -> dict:
    return {
        "check_id": result.check_id,
        "check_name": result.check_name,
        "original_runtime_status": result.original_runtime_status,
        "diagnosis_status": result.diagnosis_status,
        "outcome": result.outcome,
        "eligible": result.eligibility.eligible,
        "eligibility_reason": result.eligibility.reason,
        "target_file": result.target_resolution.file if result.target_resolution else None,
        "target_line": result.target_resolution.line if result.target_resolution else None,
        "target_localized": result.target_resolution.localized if result.target_resolution else None,
        "proposal_confidence": result.repair_proposal.confidence if result.repair_proposal else None,
        "proposal_model": result.repair_proposal.model if result.repair_proposal else None,
        "static_validation_status": getattr(result.validation_result, "status", None),
        "runtime_candidate_status": result.runtime_candidate_status,
        "deterministic_decision": getattr(result.deterministic_decision, "action", None),
        "apply_status": result.apply_status,
        "final_runtime_status": result.final_runtime_status,
        "verification_status": result.verification_status,
        "affected_files": list(result.affected_files),
        "explanation": result.explanation,
        "error": result.error,
    }


def runtime_repair_results_to_json(results, indent=2) -> str:
    return json.dumps([runtime_repair_result_to_dict(r) for r in results], indent=indent, sort_keys=False)


# APPLY_NOT_ATTEMPTED / ELIGIBILITY_* re-exported for convenience; imported
# above purely so `qa_agent.ai.__init__` can re-export the whole surface
# from this one module without every caller reaching into
# runtime_repair_models directly.
__all__ = [
    "APPLY_NOT_ATTEMPTED",
    "ELIGIBILITY_ELIGIBLE",
    "ELIGIBILITY_NOT_ELIGIBLE",
    "OUTCOME_ACCEPTED",
    "OUTCOME_APPLIED",
    "OUTCOME_APPLIED_BUT_STILL_FAILING",
    "OUTCOME_ERROR",
    "OUTCOME_HELD",
    "OUTCOME_NOT_ELIGIBLE",
    "OUTCOME_PROPOSAL_FAILED",
    "OUTCOME_REJECTED",
    "OUTCOME_VALIDATION_FAILED",
    "OUTCOME_VERIFIED",
    "VERIFICATION_NOT_APPLICABLE",
    "VERIFICATION_STILL_FAILING",
    "VERIFICATION_UNKNOWN",
    "VERIFICATION_VERIFIED",
    "EligibilityResult",
    "RuntimeRepairResult",
    "TargetResolution",
    "check_repair_eligibility",
    "propose_runtime_repair",
    "render_runtime_repair_result",
    "repair_runtime_failure",
    "repair_runtime_failures",
    "resolve_repair_target",
    "runtime_repair_result_to_dict",
    "runtime_repair_results_to_json",
]
