"""API QA v1 (docs/30-api-qa-v1.md): discover real Next.js App Router API
route handlers, start the real dev server, make real HTTP calls against
them, and report structured pass/fail evidence.

Reuses `qa_agent.project`'s `RepositoryContext` as its one input - nothing
here re-walks a filesystem or re-detects a framework; every framework/
package-manager fact is reached via `context.project`, never re-derived.
Result shapes (`ApiEndpoint`, `ApiCallResult`, `ApiTestResult`) deliberately
mirror `qa_agent.runtime`'s own `RuntimeCheck`/`RuntimeCheckResult`/
`RuntimeExecutionResult` shape - `ai_bridge.py` (docs/31) is what actually
uses that resemblance: the one, narrow, explicit file in this package that
imports `qa_agent.ai` (G3 diagnosis, G4 verified repair) and `qa_agent.
runtime` (for the one shape they already read) to let a real failing
`ApiCallResult` be diagnosed and, optionally, verified-repaired through the
existing, unmodified G3/G4 pipeline - never a second diagnosis/repair
engine. `discovery.py`/`server.py`/`http_client.py`/`runner.py`/`models.py`/
`render.py` remain exactly as AI-free as they already were.

Extended in docs/33-api-qa-deterministic-verification.md with
`resolution.py`: a dynamic endpoint (`/api/users/{user_id}`) is no longer
always skipped - a real path-parameter value or request body is resolved
from real prior evidence (a real collection response, a real OpenAPI
schema default) when the evidence genuinely supports it, and honestly
`SKIPPED` otherwise. Never invents a value.

Public API: `run_api_qa(context, root, config=None)` -> `ApiTestResult`
(no AI). `diagnose_and_repair_api_failures(api_result, context, provider,
root, do_diagnose, do_repair, config=None)` -> `Tuple[ApiDiagnosisRepairEntry, ...]`
(opt-in AI, see `ai_bridge.py`).
"""

from .ai_bridge import (
    ApiDiagnosisRepairEntry,
    ApiRepairAttempt,
    diagnose_and_repair_api_failures,
    diagnose_api_failure,
    render_api_diagnosis_repair_entry,
    repair_api_failure,
)
from .analysis import (
    API_SEVERITIES,
    API_SEVERITY_CRITICAL,
    API_SEVERITY_HIGH,
    API_SEVERITY_LOW,
    API_SEVERITY_MEDIUM,
    CLASSIFICATION_FAILING,
    CLASSIFICATION_NOT_WORKING,
    CLASSIFICATION_SKIPPED,
    CLASSIFICATION_WORKING,
    CLASSIFICATIONS,
    classify_call_outcome,
    classify_call_severity,
    classify_negative_case_severity,
    classify_schema_validation_severity,
    expected_actual,
)
from .discovery import discover_api_endpoints
from .http_client import call_endpoint
from .models import (
    BODY_EVIDENCE_SOURCES,
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    CALL_STATUSES,
    EVIDENCE_OPENAPI,
    EVIDENCE_SCHEMA,
    EVIDENCE_SOURCE_HINT,
    EVIDENCE_SYNTHETIC,
    EVIDENCE_TEST_EXAMPLE,
    EVIDENCE_UNKNOWN,
    METHODS,
    SERVER_ALREADY_RUNNING,
    SERVER_CRASHED,
    SERVER_SKIPPED,
    SERVER_STARTED,
    SERVER_START_FAILED,
    SERVER_STATUSES,
    SERVER_UNREACHABLE,
    ApiCallResult,
    ApiEndpoint,
    ApiTestResult,
    NegativeCallResult,
    SchemaValidationResult,
)
from .render import render, to_csv, to_dict, to_html, to_json
from .resolution import (
    DEFAULT_ALLOW_SYNTHETIC_MUTATIONS,
    build_request_body,
    fetch_openapi_schema,
    find_static_openapi_schema,
    generate_and_execute_negative_cases,
    resolve_and_execute,
    resolve_path_parameter,
    validate_response_schemas,
)
from .test_evidence import build_test_evidence_registry, find_test_evidence_for_endpoint
from .runner import DEFAULT_CONFIG, ApiQaConfig, run_api_qa
from .synthesis import synthesize_value

__all__ = [
    "API_SEVERITIES",
    "API_SEVERITY_CRITICAL",
    "API_SEVERITY_HIGH",
    "API_SEVERITY_LOW",
    "API_SEVERITY_MEDIUM",
    "BODY_EVIDENCE_SOURCES",
    "CALL_FAIL",
    "CALL_PASS",
    "CALL_SKIPPED",
    "CALL_STATUSES",
    "CLASSIFICATION_FAILING",
    "CLASSIFICATION_NOT_WORKING",
    "CLASSIFICATION_SKIPPED",
    "CLASSIFICATION_WORKING",
    "CLASSIFICATIONS",
    "DEFAULT_ALLOW_SYNTHETIC_MUTATIONS",
    "DEFAULT_CONFIG",
    "EVIDENCE_OPENAPI",
    "EVIDENCE_SCHEMA",
    "EVIDENCE_SOURCE_HINT",
    "EVIDENCE_SYNTHETIC",
    "EVIDENCE_TEST_EXAMPLE",
    "EVIDENCE_UNKNOWN",
    "METHODS",
    "SERVER_ALREADY_RUNNING",
    "SERVER_CRASHED",
    "SERVER_SKIPPED",
    "SERVER_STARTED",
    "SERVER_START_FAILED",
    "SERVER_STATUSES",
    "SERVER_UNREACHABLE",
    "ApiCallResult",
    "ApiDiagnosisRepairEntry",
    "ApiEndpoint",
    "ApiQaConfig",
    "ApiRepairAttempt",
    "ApiTestResult",
    "NegativeCallResult",
    "SchemaValidationResult",
    "build_request_body",
    "build_test_evidence_registry",
    "call_endpoint",
    "classify_call_outcome",
    "classify_call_severity",
    "classify_negative_case_severity",
    "classify_schema_validation_severity",
    "diagnose_and_repair_api_failures",
    "diagnose_api_failure",
    "discover_api_endpoints",
    "expected_actual",
    "fetch_openapi_schema",
    "find_static_openapi_schema",
    "find_test_evidence_for_endpoint",
    "generate_and_execute_negative_cases",
    "render",
    "render_api_diagnosis_repair_entry",
    "repair_api_failure",
    "resolve_and_execute",
    "resolve_path_parameter",
    "run_api_qa",
    "synthesize_value",
    "to_csv",
    "to_dict",
    "to_html",
    "to_json",
    "validate_response_schemas",
]
