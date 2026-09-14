"""API QA -> G3 Diagnosis -> G4 Verified Repair bridge (docs/31-api-qa-ai
-bridge.md). The one, narrow, explicit place in `qa_agent/api_qa/` that
imports `qa_agent.ai` (G3/G4) and `qa_agent.runtime` (for the one shape G3/
G4 already speak) - every other file in this package stays exactly as
untouched and AI-free as it already was (`runner.py`'s own module docstring
still says "No AI", and it is still true).

    ApiCallResult (a real, failed HTTP call)
        -> _build_check_result()      [adapts it into a real RuntimeCheckResult -
                                        no new diagnosis/repair engine, the
                                        exact existing shape G3/G4 already read]
        -> diagnose_runtime_failure()  (G3, unmodified)
        -> repair_runtime_failure()    (G4, unmodified, runtime_check=None -
                                        see this module's own docstring below
                                        for exactly what that does and does not
                                        change about G4's own behavior)
        -> [a real write happened?] -> re-call the same real endpoint
                                        (api_qa's own, already-existing
                                        server/http_client machinery) ->
                                        report whether it is actually fixed

**Why `runtime_check=None` for every call into G4, explained plainly rather
than glossed over:** G4's own pre-apply candidate-runtime-check and
post-apply re-verification (the mechanism that can produce `VERIFIED`) both
require a real `RuntimeCheck` (Phase G Part 1's own planned-check shape) to
re-run via `run_runtime_plan` - there is no such thing as "the planned
check for one specific HTTP endpoint" (`api-endpoints` is a real planned
check, but it has no executor at all - see docs/21/22 - and re-running it
would say nothing about this one endpoint specifically). Every place inside
`repair_runtime_failure` that touches its own `execution_result` argument is
already gated behind `if runtime_check is not None:` - passing `None` for
both makes G4 safely skip its own runtime-candidate verification and
post-apply re-check entirely, falling back to `decide_repair()`'s own
static-analysis verdict alone (Phase E Part 4, called unmodified) for
ACCEPT/REJECT, and reporting `OUTCOME_APPLIED`/`VERIFICATION_UNKNOWN` - never
a fabricated `OUTCOME_VERIFIED` - for any repair this module actually
writes to the real repository. `execution_result` itself is passed as
`None`, deliberately, not a hand-built stand-in: every one of its own uses
inside G4 is already conditioned on `runtime_check is not None`, so it is
provably never dereferenced by any path this module ever takes, and a
future change to G4 that broke that assumption would raise there, which
`repair_runtime_failure`'s own top-level `except Exception` already turns
into a safe, structured `OUTCOME_ERROR` rather than a crash.

**The real, final "was it fixed?" answer is this module's own, not G4's:**
because G4's own verification is structurally unavailable for a single API
endpoint (see above), the actual re-test - starting the real dev server
again (it was already stopped after the original test) and making one more
real HTTP call against the same endpoint - is this module's own job,
reusing `api_qa.server`/`api_qa.http_client` exactly as `runner.py` already
does for the very first call. This is never presented as G4's own
`VERIFIED` status; it is a distinct, explicitly-labeled "Re-test" result.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from ..ai import (
    DIAGNOSIS_DIAGNOSED,
    DIAGNOSIS_NOT_APPLICABLE,
    OUTCOME_APPLIED,
    RuntimeDiagnosis,
    RuntimeRepairResult,
    diagnose_runtime_failure,
    render_diagnosis,
    render_runtime_repair_result,
    repair_runtime_failure,
)
from ..runtime import STATUS_FAIL, RuntimeCheckResult
from . import server as _server
from .http_client import DEFAULT_TIMEOUT_SECONDS as _DEFAULT_REQUEST_TIMEOUT
from .http_client import call_endpoint
from .models import CALL_FAIL, CALL_PASS, ApiCallResult
from .runner import DEFAULT_CONFIG as _DEFAULT_API_QA_CONFIG

# The one G4 outcome that means "a real write to the real repository
# happened" when this module is the caller (runtime_check is always None
# here, so OUTCOME_VERIFIED/OUTCOME_APPLIED_BUT_STILL_FAILING - both of
# which require a real post-apply RuntimeCheck re-run - are structurally
# unreachable; asserted directly by a dedicated test, not just assumed).
_REPAIR_WROTE_REAL_FILE = (OUTCOME_APPLIED,)


# The real route source file is read only up to this many raw bytes - a
# file larger than this (a minified bundle, a generated file) is reported
# as "not available", never partially read and silently misrepresented as
# the whole file. Small compared to detectors.py's own 2MB manifest cap
# (this content is inlined into a prompt, not merely scanned for a marker
# substring) but generous for a real route handler, which this project's
# own evidence (lms-ai's real route.ts files) shows is typically well under
# 1KB.
_MAX_SOURCE_READ_BYTES = 200_000

# The excerpt actually offered as evidence is capped independently and
# tighter than the read cap above - this is what actually reaches the
# prompt, before `diagnosis_prompts.build_diagnosis_prompt`'s own
# `_bounded_logs` (redaction + a second, whole-payload truncation) runs
# over the combined evidence. Two independent bounds, not one, so a huge
# route file can never dominate the combined evidence budget before that
# second pass even gets to see the real HTTP call evidence placed after it.
_MAX_SOURCE_EXCERPT_CHARS = 2000


def _read_source_excerpt(root, source_file: str) -> Optional[str]:
    """The real content of `root/source_file`, head-truncated and marked
    if too long - `None` (never an empty string standing in for "nothing to
    show") when `root` is unknown, the file does not exist, is not a real
    file, exceeds the raw read cap, or cannot be decoded. The caller is
    responsible for turning `None` into an honest, explicit "not available"
    line rather than silently omitting the whole idea of source evidence.
    """
    if root is None:
        return None
    try:
        path = Path(root) / source_file
        if not path.is_file():
            return None
        if path.stat().st_size > _MAX_SOURCE_READ_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(text) <= _MAX_SOURCE_EXCERPT_CHARS:
        return text
    return "{}\n[... {} character(s) truncated ...]".format(
        text[:_MAX_SOURCE_EXCERPT_CHARS], len(text) - _MAX_SOURCE_EXCERPT_CHARS,
    )


def _build_check_result(call: ApiCallResult, root=None) -> RuntimeCheckResult:
    """The one adapter step: a real, failed `ApiCallResult` becomes a real
    `RuntimeCheckResult` (Phase G Part 2's own type, unmodified) - the exact
    shape `diagnose_runtime_failure`/`repair_runtime_failure` already know
    how to read. Every field is either real evidence from `call` itself (or
    a real read of the real file it names, when `root` is given) or left at
    an honest, empty/neutral default - never invented.

    The real source file content, when it can be read, is the single most
    important addition here over the plain HTTP-call evidence alone: it is
    what lets a diagnosis correctly recognize an intentionally-faulty demo
    route, or name the exact construct responsible, instead of a vague
    "a server error occurred" - and it flows through `.logs` into
    `diagnosis_prompts.build_diagnosis_prompt`'s own existing secret
    redaction and truncation unchanged, gaining that safety net for free
    rather than needing a second one built here.
    """
    endpoint = call.endpoint
    lines = [
        "API call: {} {}".format(endpoint.method, endpoint.path),
        "Source file: {}".format(endpoint.source_file),
        "HTTP status code: {}".format(call.status_code if call.status_code is not None else "none received"),
    ]
    if call.content_type:
        lines.append("Content-Type: {}".format(call.content_type))
    if call.valid_response is not None:
        lines.append("Response body valid JSON: {}".format(call.valid_response))
    if call.response_sample:
        lines.append("Response body sample: {}".format(call.response_sample))
    if call.response_time_ms is not None:
        lines.append("Response time: {:.0f}ms".format(call.response_time_ms))
    if call.error:
        lines.append("Error: {}".format(call.error))
    lines.append(
        "Note: no separate server log capture is available for this individual API call - "
        "the evidence above is the real HTTP request/response only."
    )

    excerpt = _read_source_excerpt(root, endpoint.source_file)
    lines.append("")
    if excerpt is not None:
        lines.append("Source file content ({}):".format(endpoint.source_file))
        lines.append("```")
        lines.append(excerpt)
        lines.append("```")
    else:
        lines.append(
            "Source file content ({}): not available (the file could not be read, does not exist, "
            "or exceeds the size limit for inline evidence).".format(endpoint.source_file)
        )

    return RuntimeCheckResult(
        id="api:{}:{}".format(endpoint.method, endpoint.path),
        name="{} {}".format(endpoint.method, endpoint.path),
        status=STATUS_FAIL,
        start_time="",
        end_time="",
        duration=(call.response_time_ms / 1000.0) if call.response_time_ms is not None else 0.0,
        reason=call.reason or "API call failed",
        details="endpoint: {} {}; source file: {}".format(endpoint.method, endpoint.path, endpoint.source_file),
        logs=tuple(lines),
        exception=call.error or None,
    )


def diagnose_api_failure(call: ApiCallResult, repository_context, provider, root=None) -> RuntimeDiagnosis:
    """Ask G3 to diagnose one failing `ApiCallResult`, unmodified - this
    function builds no prompt and validates no response itself, exactly
    like `diagnose_runtime_failure` already does for a real runtime check.
    Safe to call on a passing/skipped call too (returns `NOT_APPLICABLE`,
    zero provider calls, matching `diagnose_runtime_failure`'s own
    behavior for a PASS/SKIPPED check) - callers are not required to
    pre-filter. Checked here explicitly, before `_build_check_result` even
    runs, since that adapter is built to represent a real failure and
    always maps onto the real runtime `"fail"` status.

    `root`: the real project root, so the endpoint's own real source file
    can be read and included as evidence (see `_build_check_result`) -
    optional, matching this project's own graceful-degradation convention:
    omitted, diagnosis still runs, honestly noting the source is
    unavailable rather than refusing to diagnose at all.
    """
    if call.status != CALL_FAIL:
        return RuntimeDiagnosis(
            check_id="api:{}:{}".format(call.endpoint.method, call.endpoint.path),
            check_name="{} {}".format(call.endpoint.method, call.endpoint.path),
            execution_status=call.status, diagnosis_status=DIAGNOSIS_NOT_APPLICABLE,
        )
    check_result = _build_check_result(call, root=root)
    return diagnose_runtime_failure(check_result, repository_context, provider, runtime_check=None)


@dataclass(frozen=True)
class ApiRepairAttempt:
    """One G4 repair attempt for one diagnosed API failure, plus this
    module's own real re-test when a write actually happened.

    `retest` is `None` whenever no real repository write occurred (nothing
    to re-test) or the server could not be restarted for the re-test -
    `retest_skipped_reason` explains which, honestly, never silently.
    """

    repair_result: RuntimeRepairResult
    retest: Optional[ApiCallResult] = None
    retest_skipped_reason: str = ""


def repair_api_failure(
    call: ApiCallResult, diagnosis: RuntimeDiagnosis, repository_context, provider, root, config=None,
    run=None, apply=None,
) -> ApiRepairAttempt:
    """Attempt exactly one G4 repair for one already-diagnosed API failure,
    then, only if a real write actually happened, re-test the same real
    endpoint. Never called unless `diagnosis.diagnosis_status ==
    DIAGNOSIS_DIAGNOSED` (this module's own caller enforces that - G4's own
    `check_repair_eligibility` enforces it again, independently, matching
    this project's established defense-in-depth precedent).

    `run`/`apply`: the same injection points `repair_runtime_failure` itself
    already accepts (a fake static-analyzer rerun, a fake real-apply) -
    forwarded through only when given, so tests can control them
    deterministically without a real linter install or a real file write;
    omitted, G4's own real defaults apply unchanged.
    """
    config = config or _DEFAULT_API_QA_CONFIG
    check_result = _build_check_result(call, root=root)
    overrides = {}
    if run is not None:
        overrides["run"] = run
    if apply is not None:
        overrides["apply"] = apply
    repair_result = repair_runtime_failure(
        check_result, diagnosis, None, repository_context, provider, root, runtime_check=None, **overrides,
    )

    if repair_result.outcome not in _REPAIR_WROTE_REAL_FILE:
        return ApiRepairAttempt(
            repair_result=repair_result,
            retest_skipped_reason="no real repository write occurred (outcome: {}) - re-test skipped"
                                   .format(repair_result.outcome),
        )

    command, evidence = _server.discover_server_start_command(root, repository_context.project)
    if command is None:
        return ApiRepairAttempt(
            repair_result=repair_result,
            retest_skipped_reason="repair was applied, but the server could not be restarted for a "
                                   "re-test: {}".format(evidence),
        )

    handle = _server.start_and_wait_ready(command, root, config.server_startup_timeout, env=config.env)
    try:
        if handle.status in (_server.STATUS_NOT_FOUND, _server.STATUS_CRASHED):
            return ApiRepairAttempt(
                repair_result=repair_result,
                retest_skipped_reason="repair was applied, but the server did not restart cleanly for a "
                                       "re-test: {}".format(handle.reason),
            )
        base_url = handle.base_url or _server_default_base_url()
        retest = call_endpoint(base_url, call.endpoint, timeout=config.request_timeout)
        return ApiRepairAttempt(repair_result=repair_result, retest=retest)
    finally:
        handle.stop()


# Next.js's own real default when no port is otherwise observed in the
# restarted server's own startup output - the same honest fallback
# `runner.py`'s own `_resolve_base_url` already uses for the original
# call, duplicated here rather than imported (a small, private helper
# inside another module in this same package - the same "small,
# deliberately duplicated helper" convention this project has used before,
# e.g. `qa_agent/ai/diagnosis.py` and `qa_agent/ai/runtime_repair.py` each
# keep their own private `_prompt_text`).
def _server_default_base_url() -> str:
    return "http://localhost:3000"


@dataclass(frozen=True)
class ApiDiagnosisRepairEntry:
    """One failing `ApiCallResult`, plus whatever diagnosis/repair work was
    actually requested and actually happened for it - `diagnosis`/
    `repair_attempt` are `None` exactly when that stage was never reached
    (not requested, or the previous stage did not qualify), never a
    fabricated placeholder.
    """

    call: ApiCallResult
    diagnosis: Optional[RuntimeDiagnosis] = None
    repair_attempt: Optional[ApiRepairAttempt] = None
    repair_skipped_reason: str = ""


def diagnose_and_repair_api_failures(
    api_result, repository_context, provider, root, do_diagnose: bool, do_repair: bool, config=None,
    run=None, apply=None,
) -> Tuple[ApiDiagnosisRepairEntry, ...]:
    """The one orchestration entry point this bridge offers. Processes only
    the calls that actually failed (`CALL_FAIL`) - a passing or skipped
    call is never sent to the AI, matching `--api-test`'s own unaffected
    behavior when neither flag is given (this function is not even called
    in that case - see `__main__.py`'s own wiring).

    Safety rule enforced here, structurally, not just by convention: a
    repair is only ever attempted when `do_repair` was asked for AND a real
    diagnosis actually completed (`diagnosis_status == DIAGNOSIS_DIAGNOSED`)
    - G4's own `check_repair_eligibility` enforces the same rule again,
    independently, so this is defense in depth, not the only gate.
    """
    entries = []
    for call in api_result.calls:
        if call.status != CALL_FAIL:
            continue
        diagnosis = diagnose_api_failure(call, repository_context, provider, root=root) if do_diagnose else None
        repair_attempt = None
        repair_skipped_reason = ""
        if do_repair:
            if diagnosis is None:
                repair_skipped_reason = "repair requested, but diagnosis was not run - never repairing undiagnosed"
            elif diagnosis.diagnosis_status != DIAGNOSIS_DIAGNOSED:
                repair_skipped_reason = (
                    "repair requested, but diagnosis did not complete (status: {}) - "
                    "never repairing on an incomplete diagnosis".format(diagnosis.diagnosis_status)
                )
            else:
                repair_attempt = repair_api_failure(
                    call, diagnosis, repository_context, provider, root, config=config, run=run, apply=apply,
                )
        entries.append(ApiDiagnosisRepairEntry(
            call=call, diagnosis=diagnosis, repair_attempt=repair_attempt,
            repair_skipped_reason=repair_skipped_reason,
        ))
    return tuple(entries)


# --- rendering --------------------------------------------------------------
#
# Reuses G3/G4's own render_diagnosis()/render_runtime_repair_result()
# unmodified for their own sections - only the "Re-test" section is new.

def _render_retest(attempt: ApiRepairAttempt) -> str:
    if attempt.retest is None:
        return "\n".join(["  Re-test: not performed", "    Reason: {}".format(attempt.retest_skipped_reason)])
    call = attempt.retest
    endpoint = call.endpoint
    code = str(call.status_code) if call.status_code is not None else "-"
    verdict = "PASS" if call.status == CALL_PASS else "FAIL"
    outcome_word = "fixed" if call.status == CALL_PASS else "still broken"
    lines = [
        "  Re-test:",
        "    {} {}  -> {}  {}  ({})".format(endpoint.method, endpoint.path, code, verdict, outcome_word),
    ]
    if call.status != CALL_PASS:
        evidence = call.error or call.reason
        if evidence:
            lines.append("    {}".format(evidence))
    return "\n".join(lines)


def render_api_diagnosis_repair_entry(entry: ApiDiagnosisRepairEntry) -> str:
    """Plain-text rendering of one failing endpoint's full diagnose/repair
    story - always headed by which real endpoint this is about, so it can
    never be mistaken for a different call's evidence once several are
    printed one after another.
    """
    lines = ["{} {}".format(entry.call.endpoint.method, entry.call.endpoint.path), ""]
    if entry.diagnosis is None:
        lines.append("  AI Diagnosis: not requested")
    else:
        rendered = render_diagnosis(entry.diagnosis)
        lines.append(rendered if rendered else "  AI Diagnosis: not applicable")
    lines.append("")
    if entry.repair_attempt is None:
        lines.append("  Repair Attempt: not attempted")
        if entry.repair_skipped_reason:
            lines.append("    Reason: {}".format(entry.repair_skipped_reason))
    else:
        lines.append(render_runtime_repair_result(entry.repair_attempt.repair_result))
        lines.append("")
        lines.append(_render_retest(entry.repair_attempt))
    return "\n".join(lines)
