"""API QA v1 (docs/30-api-qa-v1.md): discover real Next.js App Router API
route handlers, start the real dev server, make real HTTP calls against
them, and report structured pass/fail evidence.

Reuses `qa_agent.project`'s `RepositoryContext` as its one input - nothing
here re-walks a filesystem or re-detects a framework; every framework/
package-manager fact is reached via `context.project`, never re-derived.
Result shapes (`ApiEndpoint`, `ApiCallResult`, `ApiTestResult`) deliberately
mirror `qa_agent.runtime`'s own `RuntimeCheck`/`RuntimeCheckResult`/
`RuntimeExecutionResult` shape so a later phase connecting an API QA
failure to G3 diagnosis/G4 repair has a familiar structure to work from -
not a promise that such a connection exists yet (it does not; see docs/30's
own "what this does not do" section).

Public API: `run_api_qa(context, root, config=None)` -> `ApiTestResult`.
"""

from .discovery import discover_api_endpoints
from .http_client import call_endpoint
from .models import (
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    CALL_STATUSES,
    METHODS,
    SERVER_ALREADY_RUNNING,
    SERVER_CRASHED,
    SERVER_SKIPPED,
    SERVER_STARTED,
    SERVER_START_FAILED,
    SERVER_STATUSES,
    ApiCallResult,
    ApiEndpoint,
    ApiTestResult,
)
from .render import render, to_dict, to_json
from .runner import DEFAULT_CONFIG, ApiQaConfig, run_api_qa

__all__ = [
    "CALL_FAIL",
    "CALL_PASS",
    "CALL_SKIPPED",
    "CALL_STATUSES",
    "DEFAULT_CONFIG",
    "METHODS",
    "SERVER_ALREADY_RUNNING",
    "SERVER_CRASHED",
    "SERVER_SKIPPED",
    "SERVER_STARTED",
    "SERVER_START_FAILED",
    "SERVER_STATUSES",
    "ApiCallResult",
    "ApiEndpoint",
    "ApiQaConfig",
    "ApiTestResult",
    "call_endpoint",
    "discover_api_endpoints",
    "render",
    "run_api_qa",
    "to_dict",
    "to_json",
]
