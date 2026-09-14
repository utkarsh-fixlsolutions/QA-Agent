"""API QA -> G3 Diagnosis -> G4 Verified Repair bridge (docs/31-api-qa-ai
-bridge.md) - `qa_agent.api_qa.ai_bridge`: the adapter from a failing
`ApiCallResult` to G3/G4's own existing input shapes, opt-in diagnose/repair
orchestration, the real re-test after a real write, and the `--api-diagnose`/
`--api-repair` CLI flags.

Follows test_runtime_repair.py's own established fixture conventions
directly: a hand-built `RuntimeDiagnosis` for repair-focused tests (already
exhaustively tested elsewhere how a real diagnosis gets produced), a fake
`run` callable for the static-analyzer rerun (no real linter install
needed), and real `npm`/`node` subprocesses (guarded by `_npm_available()`)
for the one full, real happy-path repair+re-test proof.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.ai import (  # noqa: E402
    DIAGNOSIS_AI_ERROR,
    DIAGNOSIS_DIAGNOSED,
    DIAGNOSIS_INSUFFICIENT_CONTEXT,
    DIAGNOSIS_INVALID_RESPONSE,
    DIAGNOSIS_NOT_APPLICABLE,
    OUTCOME_APPLIED,
    OUTCOME_NOT_ELIGIBLE,
    MockProvider,
    RuntimeDiagnosis,
)
from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import (  # noqa: E402
    ApiQaConfig,
    CALL_FAIL,
    CALL_PASS,
    diagnose_and_repair_api_failures,
    diagnose_api_failure,
    render_api_diagnosis_repair_entry,
    repair_api_failure,
    run_api_qa,
)
from qa_agent.api_qa.ai_bridge import (  # noqa: E402
    _MAX_SOURCE_EXCERPT_CHARS,
    _build_check_result,
    _read_source_excerpt,
)
from qa_agent.api_qa.models import ApiCallResult, ApiEndpoint  # noqa: E402


def _npm_available():
    return shutil.which("npm") is not None


def _context_for(files):
    proj = TempProject()
    for rel, text in files.items():
        proj.write(rel, text)
    result = discover_project(proj.path)
    context = build_repository_context(result.project)
    return context, proj


def _endpoint(method="GET", path="/api/broken", source_file="app/api/broken/route.ts"):
    return ApiEndpoint(method=method, path=path, source_file=source_file)


def _failed_call(status_code=500, error="", response_sample='{"error": "boom"}', reason="HTTP 500 response",
                  content_type="application/json", valid_response=True, response_time_ms=42.0):
    return ApiCallResult(
        endpoint=_endpoint(), status=CALL_FAIL, status_code=status_code, error=error,
        response_sample=response_sample, reason=reason, content_type=content_type,
        valid_response=valid_response, response_time_ms=response_time_ms,
    )


# --- the adapter: ApiCallResult -> RuntimeCheckResult -----------------------

def test_build_check_result_maps_fail_status_and_real_evidence(suite):
    call = _failed_call()
    check_result = _build_check_result(call)
    suite.check("status maps to the real runtime 'fail' string", check_result.status == "fail")
    suite.check("id/name mention the real endpoint", "GET" in check_result.id and "/api/broken" in check_result.name)
    suite.check("reason is the real call reason", check_result.reason == "HTTP 500 response")
    suite.check("status code appears in real evidence", "500" in " ".join(check_result.logs))
    suite.check("response sample appears in real evidence", "boom" in " ".join(check_result.logs))
    suite.check("source file appears in details (for grounding)", "app/api/broken/route.ts" in check_result.details)


def test_build_check_result_handles_a_connection_failure_honestly(suite):
    call = ApiCallResult(endpoint=_endpoint(), status=CALL_FAIL, status_code=None,
                          error="could not reach 'http://x': refused", reason="")
    check_result = _build_check_result(call)
    suite.check("no status code -> reported as none received, never invented", "none received" in " ".join(check_result.logs))
    suite.check("the real error is carried as the exception", check_result.exception == "could not reach 'http://x': refused")


# --- source-code evidence upgrade -------------------------------------------

SENTRY_EXAMPLE_ROUTE_TS = (
    'import { NextResponse } from "next/server";\n'
    'export const dynamic = "force-dynamic";\n'
    "class SentryExampleAPIError extends Error {\n"
    "  constructor(message) { super(message); this.name = \"SentryExampleAPIError\"; }\n"
    "}\n"
    "// A faulty API route to test Sentry's error monitoring\n"
    "export function GET() {\n"
    '  throw new SentryExampleAPIError("This error is raised on the backend called by the example page.");\n'
    "}\n"
)


def test_read_source_excerpt_returns_real_content(suite):
    proj = TempProject()
    try:
        proj.write("app/api/broken/route.ts", SENTRY_EXAMPLE_ROUTE_TS)
        excerpt = _read_source_excerpt(proj.path, "app/api/broken/route.ts")
        suite.check("the real file content is returned", excerpt is not None and "SentryExampleAPIError" in excerpt)
    finally:
        proj.__exit__(None, None, None)


def test_read_source_excerpt_none_when_root_is_none(suite):
    suite.check("no root -> None, never a crash", _read_source_excerpt(None, "app/api/broken/route.ts") is None)


def test_read_source_excerpt_none_when_file_missing(suite):
    proj = TempProject()
    try:
        suite.check("missing file -> None, never invented content",
                     _read_source_excerpt(proj.path, "app/api/does-not-exist/route.ts") is None)
    finally:
        proj.__exit__(None, None, None)


def test_read_source_excerpt_truncates_large_files_with_a_clear_marker(suite):
    proj = TempProject()
    try:
        huge = "// line\n" * 10000
        proj.write("app/api/huge/route.ts", huge)
        excerpt = _read_source_excerpt(proj.path, "app/api/huge/route.ts")
        suite.check("the excerpt is bounded", excerpt is not None and len(excerpt) < len(huge))
        suite.check("a clear truncation marker is present, never silent", "truncated" in excerpt)
        suite.check("the excerpt never exceeds the documented cap plus its own marker text",
                     excerpt is not None and len(excerpt) < _MAX_SOURCE_EXCERPT_CHARS + 100)
    finally:
        proj.__exit__(None, None, None)


def test_build_check_result_includes_real_source_content_when_root_given(suite):
    proj = TempProject()
    try:
        proj.write("app/api/broken/route.ts", SENTRY_EXAMPLE_ROUTE_TS)
        call = _failed_call()
        check_result = _build_check_result(call, root=proj.path)
        haystack = " ".join(check_result.logs)
        suite.check("the real source class name appears in evidence", "SentryExampleAPIError" in haystack)
        suite.check("the real throw statement appears in evidence", "throw new SentryExampleAPIError" in haystack)
        suite.check("the demo-route comment appears in evidence", "faulty API route to test Sentry" in haystack)
    finally:
        proj.__exit__(None, None, None)


def test_build_check_result_notes_source_unavailable_without_root(suite):
    call = _failed_call()
    check_result = _build_check_result(call)  # no root given
    suite.check("honestly notes the source is unavailable, never silently omitted",
                 "not available" in " ".join(check_result.logs))


def test_build_check_result_notes_source_unavailable_when_file_missing(suite):
    proj = TempProject()
    try:
        call = _failed_call()  # names app/api/broken/route.ts, which is never written in this fixture
        check_result = _build_check_result(call, root=proj.path)
        suite.check("a missing file is reported honestly, not silently skipped",
                     "not available" in " ".join(check_result.logs))
    finally:
        proj.__exit__(None, None, None)


def test_diagnosis_prompt_includes_the_real_source_content(suite):
    from qa_agent.ai.diagnosis_prompts import build_diagnosis_prompt
    proj = TempProject()
    try:
        proj.write("app/api/broken/route.ts", SENTRY_EXAMPLE_ROUTE_TS)
        context, proj2 = _context_for({"package.json": json.dumps({"name": "x"})})
        try:
            call = _failed_call()
            check_result = _build_check_result(call, root=proj.path)
            prompt = build_diagnosis_prompt(check_result, context)
            suite.check("the real source class name reaches the actual prompt text",
                         "SentryExampleAPIError" in prompt.user)
            suite.check("the real file path reaches the actual prompt text",
                         "app/api/broken/route.ts" in prompt.user)
        finally:
            proj2.__exit__(None, None, None)
    finally:
        proj.__exit__(None, None, None)


# --- diagnose_api_failure (G3, unmodified) ----------------------------------

def _diagnosis_response_json(affected_files=(), affected_components=(), confidence=0.85):
    return json.dumps({
        "summary": "the endpoint responds with a server error",
        "severity": "error",
        "confidence": confidence,
        "root_cause": "the route handler appears to always fail",
        "evidence": ["HTTP status code: 500"],
        "affected_files": list(affected_files),
        "affected_components": list(affected_components),
        "recommended_action": "inspect the handler for an unconditional failure",
    })


def test_diagnose_api_failure_grounded_success(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        call = _failed_call()
        provider = MockProvider(response_text=_diagnosis_response_json(
            affected_files=["app/api/broken/route.ts"],
        ))
        diagnosis = diagnose_api_failure(call, context, provider)
        suite.check("diagnosis_status is DIAGNOSED", diagnosis.diagnosis_status == DIAGNOSIS_DIAGNOSED,
                     " (was {}: {})".format(diagnosis.diagnosis_status, diagnosis.error))
        suite.check("summary carried through", diagnosis.summary)
        suite.check("affected file carried through", diagnosis.affected_files == ("app/api/broken/route.ts",))
    finally:
        proj.__exit__(None, None, None)


def test_diagnose_api_failure_rejects_ungrounded_claim(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        call = _failed_call()
        provider = MockProvider(response_text=_diagnosis_response_json(
            affected_files=["some/totally/invented/file.ts"],
        ))
        diagnosis = diagnose_api_failure(call, context, provider)
        suite.check("an unsupported file claim is rejected, not accepted",
                     diagnosis.diagnosis_status == DIAGNOSIS_INVALID_RESPONSE)
    finally:
        proj.__exit__(None, None, None)


def test_diagnose_api_failure_grounds_on_a_claim_that_only_appears_in_source_code(suite):
    """The concrete improvement this step exists to prove: a claim naming
    a real construct that appears ONLY inside the route's own source code
    (never in the plain HTTP-call evidence alone) is now groundable,
    because that source code is now part of the real evidence supplied -
    proven by first showing the identical claim was NOT groundable before
    the source file existed on disk at all.
    """
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        call = _failed_call()
        provider = MockProvider(response_text=_diagnosis_response_json(
            affected_files=["app/api/broken/route.ts"],
            affected_components=["SentryExampleAPIError"],
        ))

        without_source = diagnose_api_failure(call, context, provider)  # root=None - no source available
        suite.check("without the source file, the same claim is rejected as ungrounded",
                     without_source.diagnosis_status == DIAGNOSIS_INVALID_RESPONSE)

        proj.write("app/api/broken/route.ts", SENTRY_EXAMPLE_ROUTE_TS)
        with_source = diagnose_api_failure(call, context, provider, root=proj.path)
        suite.check("with the real source file present, the identical claim is now grounded and accepted",
                     with_source.diagnosis_status == DIAGNOSIS_DIAGNOSED,
                     " (was {}: {})".format(with_source.diagnosis_status, with_source.error))
        suite.check("the real component name is carried through", with_source.affected_components == ("SentryExampleAPIError",))
    finally:
        proj.__exit__(None, None, None)


def test_diagnose_api_failure_still_rejects_a_claim_absent_from_source_too(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        proj.write("app/api/broken/route.ts", SENTRY_EXAMPLE_ROUTE_TS)
        call = _failed_call()
        provider = MockProvider(response_text=_diagnosis_response_json(
            affected_files=["app/api/broken/route.ts"],
            affected_components=["SomeCompletelyInventedHelper"],
        ))
        diagnosis = diagnose_api_failure(call, context, provider, root=proj.path)
        suite.check("a claim absent from the real source (and everywhere else) is still rejected, "
                     "grounding safety is not loosened by adding more evidence",
                     diagnosis.diagnosis_status == DIAGNOSIS_INVALID_RESPONSE)
    finally:
        proj.__exit__(None, None, None)


def test_diagnose_api_failure_not_applicable_for_a_passing_call(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        calls = {"n": 0}

        class _CountingProvider:
            name = "counting"

            def generate(self, prompt):
                calls["n"] += 1
                return MockProvider().generate(prompt)

        passing_call = ApiCallResult(endpoint=_endpoint(), status=CALL_PASS, status_code=200)
        diagnosis = diagnose_api_failure(passing_call, context, _CountingProvider())
        suite.check("a passing call is NOT_APPLICABLE", diagnosis.diagnosis_status == DIAGNOSIS_NOT_APPLICABLE)
        suite.check("zero provider calls were made for a passing call", calls["n"] == 0)
    finally:
        proj.__exit__(None, None, None)


def test_diagnose_api_failure_handles_provider_failure(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        call = _failed_call()
        diagnosis = diagnose_api_failure(call, context, MockProvider(fail=True, failure_message="offline"))
        suite.check("provider failure -> AI_ERROR, never a crash", diagnosis.diagnosis_status == DIAGNOSIS_AI_ERROR)
        suite.check("the real error is carried", diagnosis.error == "offline")
    finally:
        proj.__exit__(None, None, None)


def test_diagnose_api_failure_handles_malformed_response(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        call = _failed_call()
        diagnosis = diagnose_api_failure(call, context, MockProvider())  # default text is not JSON
        suite.check("a malformed response -> INVALID_RESPONSE, never a crash",
                     diagnosis.diagnosis_status == DIAGNOSIS_INVALID_RESPONSE)
    finally:
        proj.__exit__(None, None, None)


# --- repair_api_failure (G4, unmodified, runtime_check=None) ---------------

def _diagnosed(affected_files=("server.js",), status=DIAGNOSIS_DIAGNOSED):
    return RuntimeDiagnosis(
        check_id="api:GET:/api/broken", check_name="GET /api/broken",
        execution_status="fail", diagnosis_status=status,
        summary="the endpoint always returns a server error", severity="error", confidence=0.9,
        observed_evidence=("HTTP status code: 500",), likely_root_cause="server.js always responds 500",
        affected_files=affected_files, affected_components=(),
        recommended_action="fix server.js so the endpoint responds successfully",
    )


def test_repair_not_eligible_when_diagnosis_names_no_file(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        call = _failed_call()
        diagnosis = _diagnosed(affected_files=())
        attempt = repair_api_failure(call, diagnosis, context, MockProvider(), proj.path)
        suite.check("NOT_ELIGIBLE, no file named", attempt.repair_result.outcome == OUTCOME_NOT_ELIGIBLE)
        suite.check("re-test is skipped, with a clear reason", attempt.retest is None and "no real repository write" in attempt.retest_skipped_reason)
    finally:
        proj.__exit__(None, None, None)


def test_repair_not_eligible_when_diagnosis_names_a_nonexistent_file(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        call = _failed_call()
        diagnosis = _diagnosed(affected_files=("does/not/exist.js",))
        attempt = repair_api_failure(call, diagnosis, context, MockProvider(), proj.path)
        suite.check("NOT_ELIGIBLE, file does not exist", attempt.repair_result.outcome == OUTCOME_NOT_ELIGIBLE)
    finally:
        proj.__exit__(None, None, None)


def test_repair_offline_provider_is_proposal_failed_and_never_writes(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"}), "server.js": "console.log(1);\n"})
    try:
        call = _failed_call()
        diagnosis = _diagnosed(affected_files=("server.js",))
        attempt = repair_api_failure(call, diagnosis, context, MockProvider(fail=True), proj.path)
        suite.check("an offline provider never crashes the run", attempt.repair_result.error is None)
        suite.check("no real write occurred", attempt.retest is None)
        suite.check("the real file is untouched", (proj.path / "server.js").read_text(encoding="utf-8") == "console.log(1);\n")
    finally:
        proj.__exit__(None, None, None)


class _RunResult:
    def __init__(self, findings=()):
        self.findings = list(findings)
        self.tool_errors = []


class _Finding:
    def __init__(self, file, line=1, tool="ruff", message="issue", severity="error"):
        self.file, self.line, self.tool, self.message, self.severity = file, line, tool, message, severity


def _sequenced_run(*results):
    calls = {"n": 0}

    def run(inputs, config=None):
        index = calls["n"]
        calls["n"] += 1
        return results[index] if index < len(results) else results[-1]

    return run


def _repair_response(replacement, start_line=1, end_line=5, confidence=0.9):
    return json.dumps({
        "explanation": "fixes the endpoint so it responds successfully",
        "replacement": replacement, "confidence": confidence,
        "start_line": start_line, "end_line": end_line,
    })


BROKEN_SERVER_JS = (
    "const http = require('http');\n"
    "http.createServer((req, res) => {\n"
    "  res.writeHead(500, {'Content-Type': 'application/json'});\n"
    "  res.end(JSON.stringify({error: 'boom'}));\n"
    "}).listen(4200, () => console.log('ready - Local:        http://localhost:4200'));\n"
)

FIXED_SERVER_JS = (
    "const http = require('http');\n"
    "http.createServer((req, res) => {\n"
    "  res.writeHead(200, {'Content-Type': 'application/json'});\n"
    "  res.end(JSON.stringify({ok: true}));\n"
    "}).listen(4200, () => console.log('ready - Local:        http://localhost:4200'));\n"
)


def test_repair_and_retest_full_happy_path(suite):
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "node server.js"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("package-lock.json", "{}")
        proj.write("server.js", BROKEN_SERVER_JS)
        proj.write("app/api/broken/route.ts", "export async function GET() { return Response.json({}); }\n")

        discovery = discover_project(proj.path)
        context = build_repository_context(discovery.project)
        config = ApiQaConfig(server_startup_timeout=20)
        api_result = run_api_qa(context, proj.path, config=config)
        suite.check("the fixture really fails first", api_result.calls and api_result.calls[0].status == CALL_FAIL,
                     " (server_status={})".format(api_result.server_status))
        call = api_result.calls[0]

        diagnosis = _diagnosed(affected_files=("server.js",))
        target_file = str((proj.path / "server.js").resolve())
        run_fn = _sequenced_run(_RunResult(findings=[_Finding(target_file)]), _RunResult(findings=[]))
        repair_provider = MockProvider(response_text=_repair_response(FIXED_SERVER_JS))

        attempt = repair_api_failure(call, diagnosis, context, repair_provider, proj.path, config=config, run=run_fn)

        suite.check("outcome is APPLIED (a real write happened)", attempt.repair_result.outcome == OUTCOME_APPLIED,
                     " (was {}: {})".format(attempt.repair_result.outcome, attempt.repair_result.explanation))
        suite.check("the real file was actually patched", "ok: true" in Path(target_file).read_text(encoding="utf-8"))
        suite.check("a re-test was performed", attempt.retest is not None)
        if attempt.retest is not None:
            suite.check("the re-test shows it is really fixed", attempt.retest.status == CALL_PASS,
                         " (was {}: {})".format(attempt.retest.status, attempt.retest.error or attempt.retest.reason))
            suite.check("the re-test really hit the same endpoint", attempt.retest.endpoint.path == "/api/broken")
    finally:
        proj.__exit__(None, None, None)


# --- orchestration: diagnose_and_repair_api_failures ------------------------

def _api_result_with(calls):
    from qa_agent.api_qa.models import ApiTestResult, SERVER_STARTED
    return ApiTestResult(root_path="/x", endpoints=tuple(c.endpoint for c in calls), calls=tuple(calls),
                          server_status=SERVER_STARTED)


def test_orchestration_only_processes_failed_calls(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        passing = ApiCallResult(endpoint=_endpoint(path="/api/ok"), status=CALL_PASS, status_code=200)
        failing = _failed_call()
        api_result = _api_result_with([passing, failing])
        entries = diagnose_and_repair_api_failures(
            api_result, context, MockProvider(), proj.path, do_diagnose=True, do_repair=False,
        )
        suite.check("only the failing call gets an entry", len(entries) == 1)
        suite.check("it is really the failing one", entries[0].call.endpoint.path == "/api/broken")
    finally:
        proj.__exit__(None, None, None)


def test_orchestration_diagnose_opt_out_makes_zero_provider_calls(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        calls = {"n": 0}

        class _CountingProvider:
            name = "counting"

            def generate(self, prompt):
                calls["n"] += 1
                return MockProvider().generate(prompt)

        api_result = _api_result_with([_failed_call()])
        entries = diagnose_and_repair_api_failures(
            api_result, context, _CountingProvider(), proj.path, do_diagnose=False, do_repair=False,
        )
        suite.check("diagnosis is None when not requested", entries[0].diagnosis is None)
        suite.check("repair_attempt is None when not requested", entries[0].repair_attempt is None)
        suite.check("zero AI calls were made at all", calls["n"] == 0)
    finally:
        proj.__exit__(None, None, None)


def test_orchestration_never_repairs_without_diagnosis(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        api_result = _api_result_with([_failed_call()])
        entries = diagnose_and_repair_api_failures(
            api_result, context, MockProvider(), proj.path, do_diagnose=False, do_repair=True,
        )
        suite.check("repair is never attempted without a diagnosis, even if asked for", entries[0].repair_attempt is None)
        suite.check("a clear reason is recorded", "diagnosis was not run" in entries[0].repair_skipped_reason)
    finally:
        proj.__exit__(None, None, None)


def test_orchestration_never_repairs_an_incomplete_diagnosis(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        api_result = _api_result_with([_failed_call()])
        # MockProvider's default response is not valid JSON -> INVALID_RESPONSE, never DIAGNOSED
        entries = diagnose_and_repair_api_failures(
            api_result, context, MockProvider(), proj.path, do_diagnose=True, do_repair=True,
        )
        suite.check("diagnosis did not complete", entries[0].diagnosis.diagnosis_status != DIAGNOSIS_DIAGNOSED)
        suite.check("repair was never attempted on an incomplete diagnosis", entries[0].repair_attempt is None)
        suite.check("a clear reason is recorded", "did not complete" in entries[0].repair_skipped_reason)
    finally:
        proj.__exit__(None, None, None)


# --- rendering ---------------------------------------------------------------

def test_render_entry_shows_no_diagnosis_requested(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        api_result = _api_result_with([_failed_call()])
        entries = diagnose_and_repair_api_failures(
            api_result, context, MockProvider(), proj.path, do_diagnose=False, do_repair=False,
        )
        text = render_api_diagnosis_repair_entry(entries[0])
        suite.check("shows the endpoint", "GET /api/broken" in text)
        suite.check("shows diagnosis not requested", "not requested" in text)
        suite.check("shows repair not attempted", "not attempted" in text)
    finally:
        proj.__exit__(None, None, None)


def test_render_entry_shows_diagnosis_and_repair_sections(suite):
    context, proj = _context_for({"package.json": json.dumps({"name": "x"})})
    try:
        api_result = _api_result_with([_failed_call()])
        entries = diagnose_and_repair_api_failures(
            api_result, context, MockProvider(response_text=_diagnosis_response_json(
                affected_files=["app/api/broken/route.ts"],
            )), proj.path, do_diagnose=True, do_repair=True,
        )
        text = render_api_diagnosis_repair_entry(entries[0])
        suite.check("AI Diagnosis section present", "AI Diagnosis" in text)
        suite.check("Runtime Repair section present", "Runtime Repair" in text)
    finally:
        proj.__exit__(None, None, None)


# --- CLI wiring --------------------------------------------------------------

def test_cli_api_test_alone_shows_no_diagnosis_or_repair_section(suite):
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({"name": "x", "scripts": {"start": "node server.js"}}))
        proj.write("server.js", "console.log('hi');\n")
        completed = run_agent(["discover", str(proj.path), "--api-test"])
        suite.check("exits cleanly", completed.returncode == 0)
        suite.check("no AI Diagnosis section without --api-diagnose", "AI Diagnosis" not in completed.stdout)
        suite.check("no Runtime Repair section without --api-repair", "Runtime Repair" not in completed.stdout)
    finally:
        proj.__exit__(None, None, None)


def test_cli_api_diagnose_flag_runs_diagnosis_on_a_real_failure(suite):
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "node server.js"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("package-lock.json", "{}")
        proj.write("server.js", BROKEN_SERVER_JS)
        proj.write("app/api/broken/route.ts", "export async function GET() { return Response.json({}); }\n")
        completed = run_agent(["discover", str(proj.path), "--api-test", "--api-diagnose", "--ai-provider", "mock"])
        suite.check("exits cleanly", completed.returncode == 0, " (rc={}, stderr={})".format(
            completed.returncode, completed.stderr[-500:]))
        suite.check("the AI Diagnosis section is printed", "AI Diagnosis" in completed.stdout)
        suite.check("still no Runtime Repair section without --api-repair", "Runtime Repair" not in completed.stdout)
    finally:
        proj.__exit__(None, None, None)


def test_cli_api_repair_flag_wires_through_without_crashing(suite):
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "node server.js"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("package-lock.json", "{}")
        proj.write("server.js", BROKEN_SERVER_JS)
        proj.write("app/api/broken/route.ts", "export async function GET() { return Response.json({}); }\n")
        completed = run_agent(["discover", str(proj.path), "--api-test", "--api-diagnose", "--api-repair",
                                "--ai-provider", "mock"])
        suite.check("exits cleanly even though MockProvider cannot produce a usable diagnosis",
                     completed.returncode == 0, " (rc={}, stderr={})".format(completed.returncode, completed.stderr[-500:]))
        suite.check("a Repair Attempt section is printed", "Repair Attempt" in completed.stdout)
        suite.check("repair was honestly reported as not attempted", "not attempted" in completed.stdout)
        suite.check("the real file was never touched by a mock diagnosis", (proj.path / "server.js").read_text(encoding="utf-8") == BROKEN_SERVER_JS)
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA -> G3/G4 Bridge")
    sys.exit(suite.run([
        test_build_check_result_maps_fail_status_and_real_evidence,
        test_build_check_result_handles_a_connection_failure_honestly,
        test_read_source_excerpt_returns_real_content,
        test_read_source_excerpt_none_when_root_is_none,
        test_read_source_excerpt_none_when_file_missing,
        test_read_source_excerpt_truncates_large_files_with_a_clear_marker,
        test_build_check_result_includes_real_source_content_when_root_given,
        test_build_check_result_notes_source_unavailable_without_root,
        test_build_check_result_notes_source_unavailable_when_file_missing,
        test_diagnosis_prompt_includes_the_real_source_content,
        test_diagnose_api_failure_grounded_success,
        test_diagnose_api_failure_rejects_ungrounded_claim,
        test_diagnose_api_failure_grounds_on_a_claim_that_only_appears_in_source_code,
        test_diagnose_api_failure_still_rejects_a_claim_absent_from_source_too,
        test_diagnose_api_failure_not_applicable_for_a_passing_call,
        test_diagnose_api_failure_handles_provider_failure,
        test_diagnose_api_failure_handles_malformed_response,
        test_repair_not_eligible_when_diagnosis_names_no_file,
        test_repair_not_eligible_when_diagnosis_names_a_nonexistent_file,
        test_repair_offline_provider_is_proposal_failed_and_never_writes,
        test_repair_and_retest_full_happy_path,
        test_orchestration_only_processes_failed_calls,
        test_orchestration_diagnose_opt_out_makes_zero_provider_calls,
        test_orchestration_never_repairs_without_diagnosis,
        test_orchestration_never_repairs_an_incomplete_diagnosis,
        test_render_entry_shows_no_diagnosis_requested,
        test_render_entry_shows_diagnosis_and_repair_sections,
        test_cli_api_test_alone_shows_no_diagnosis_or_repair_section,
        test_cli_api_diagnose_flag_runs_diagnosis_on_a_real_failure,
        test_cli_api_repair_flag_wires_through_without_crashing,
    ]))
