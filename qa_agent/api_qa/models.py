"""Immutable data shapes for API QA v1 (Phase G5-adjacent, docs/30-api-qa
-v1.md). First target: Next.js App Router route handlers - real files under
an `app/` directory named `route.ts`/`route.js` that export one or more
HTTP method functions (`GET`, `POST`, ...). Express/Node and Pages Router
support are explicitly out of scope for v1 (see docs/30's own scope
section) - not silently half-supported, just not attempted. Extended in
docs/32-fastapi-discovery-and-startup.md with a second, independent Python/
FastAPI discovery strategy (`@app.get(...)`/`@router.get(...)`-style
decorators) - these shapes are shared by both strategies unchanged; only
`discovery.py`/`server.py` gained new, additive strategy functions.

Every `ApiEndpoint` traces back to a real file and a real exported handler
name - the same "no fact without evidence" discipline `DetectedItem`
(qa_agent/project/models.py) and `RuntimeCheck` (qa_agent/runtime/models.py)
already established for their own layers, applied here to "this URL/method
pair is real, discovered code" instead of "this language/framework is
present" or "this check is worth planning".

`ApiCallResult` never claims more than it actually observed: `status_code`
is `None` only when no HTTP response was ever received at all (a real
connection failure, a timeout, or the call being skipped before it was
attempted) - a real response, even a 500 or a malformed body, always has a
real, non-`None` `status_code`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# Server lifecycle outcomes for one ApiTestResult - closed set, matching this
# project's established "plain string constants, not an enum" convention
# (project/models.py's own docstring explains why: STATUS_* everywhere else
# already follows this, no new pattern invented here).
SERVER_STARTED = "started"
SERVER_ALREADY_RUNNING = "already_running"
SERVER_START_FAILED = "start_failed"
SERVER_CRASHED = "crashed"
SERVER_SKIPPED = "skipped"

SERVER_STATUSES = (
    SERVER_STARTED,
    SERVER_ALREADY_RUNNING,
    SERVER_START_FAILED,
    SERVER_CRASHED,
    SERVER_SKIPPED,
)

# One call's outcome - PASS/FAIL mirror runtime/execution_models.py's own
# STATUS_PASS/STATUS_FAIL naming exactly, so a later phase connecting an API
# QA failure to G3 diagnosis sees a familiar shape, not a competing one.
CALL_PASS = "pass"
CALL_FAIL = "fail"
CALL_SKIPPED = "skipped"

CALL_STATUSES = (CALL_PASS, CALL_FAIL, CALL_SKIPPED)


@dataclass(frozen=True)
class ApiEndpoint:
    """One discovered endpoint. `path` is the real URL path the framework
    would route to. For Next.js, route-group segments like `(marketing)`
    are already stripped (they never appear in the real URL - a documented
    convention, not a guess) and `dynamic` reflects an unresolved
    `[segment]`/`[...segment]` placeholder. For FastAPI, `path` is the
    real string literal passed to the route decorator, unmodified, and
    `dynamic` reflects an unresolved `{segment}` placeholder. Either way, a
    dynamic endpoint is discovered and reported, but never called
    automatically (see docs/30's "no invented path parameters" rule).

    `line`: the real 1-based source line the route was declared on, when
    the discovering strategy tracks it (currently only the FastAPI
    strategy does - the Next.js strategy finds a whole exported function,
    not one decorator line, so `line` stays `None` there, honestly, never
    a guessed value).

    `source_file` is the real, repository-relative `route.ts`/`route.js`/
    `.py` file this endpoint was found in - the one piece of evidence every
    `ApiEndpoint` must carry.
    """

    method: str
    path: str
    source_file: str
    dynamic: bool = False
    line: Optional[int] = None

    def __post_init__(self):
        if self.method not in METHODS:
            raise ValueError(
                "ApiEndpoint({!r} {!r}) has an unrecognized method".format(self.method, self.path)
            )
        if not self.source_file:
            raise ValueError(
                "ApiEndpoint({!r} {!r}) constructed with no source_file - every discovered "
                "endpoint must be backed by a real file".format(self.method, self.path)
            )
        if self.line is not None and self.line < 1:
            raise ValueError(
                "ApiEndpoint({!r} {!r}) has a non-positive line number {!r}"
                .format(self.method, self.path, self.line)
            )


@dataclass(frozen=True)
class ApiCallResult:
    """One real (or deliberately skipped) HTTP call against one
    `ApiEndpoint`. References the endpoint rather than duplicating its
    fields - the same "reference, don't duplicate" pattern
    `RuntimeExecutionResult` already established for `RuntimeQAPlan`.

    `response_sample` is a bounded prefix of the real response body -
    never the full body (see http_client.py's `MAX_RESPONSE_SAMPLE_CHARS`) -
    and is empty for any call that never received a body at all.
    `valid_response` is `None` when JSON validity does not apply (the
    response's content-type was not JSON, or no response was received);
    `True`/`False` only when a real JSON-parse attempt was actually made.

    `response_json` (docs/33-api-qa-deterministic-verification.md): the
    real, already-parsed JSON value, populated only when `valid_response
    is True` - the same already-validated parse `call_endpoint` performs
    internally to decide pass/fail, exposed here rather than re-parsed a
    second time. This is what makes evidence-based path-parameter
    resolution possible without a second HTTP call or a second, competing
    HTTP layer: a later endpoint's resolver reads a real, already-received
    response's own real data, never the truncated display `response_sample`.

    `resolved_path`/`resolution_evidence` (docs/33): only set when this
    call's concrete request differed from `endpoint.path`'s own literal
    template (a dynamic path parameter substituted with a real,
    evidence-derived value, or a request body constructed from a real
    OpenAPI schema default) - `resolved_path` is the real, concrete path
    actually requested (e.g. `/api/users/1`) and `resolution_evidence` is
    a short, human-readable trace of exactly which prior fact made that
    substitution possible. Both stay empty for a plain, non-dynamic call -
    `endpoint.path` alone is already the concrete path there, and nothing
    needs explaining.
    """

    endpoint: ApiEndpoint
    status: str
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    content_type: str = ""
    valid_response: Optional[bool] = None
    response_sample: str = ""
    response_json: Optional[object] = None
    resolved_path: str = ""
    resolution_evidence: str = ""
    error: str = ""
    reason: str = ""

    def __post_init__(self):
        if self.status not in CALL_STATUSES:
            raise ValueError(
                "ApiCallResult({!r} {!r}) has an unrecognized status {!r}".format(
                    self.endpoint.method, self.endpoint.path, self.status
                )
            )


@dataclass(frozen=True)
class ApiTestResult:
    """The whole-session outcome of one `run_api_qa()` call. `endpoints` is
    every endpoint discovered, whether or not it was ever called (a dynamic
    route, or one skipped because the server never started, still appears
    here - honestly, not silently dropped). `calls` has exactly one entry
    per endpoint in `endpoints`, in the same order.

    `server_log_tail` (docs/37-api-qa-server-log-capture.md): a real,
    bounded tail of whatever the dev server itself printed to its own
    stdout/stderr while `calls` were being made - empty when the server was
    never actually started, or printed nothing during that window. This is
    what makes a failure's real *why* visible for cases `http_client.py`
    alone cannot explain (an application-level exception whose response
    body was empty, e.g. an unhandled Next.js route error) - the server's
    own real console output, never fabricated or summarized.
    """

    root_path: str
    endpoints: Tuple[ApiEndpoint, ...] = ()
    calls: Tuple[ApiCallResult, ...] = ()
    server_status: str = SERVER_SKIPPED
    server_detail: str = ""
    base_url: str = ""
    started_at: str = ""
    finished_at: str = ""
    total_duration: float = 0.0
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    server_log_tail: str = ""

    def __post_init__(self):
        if self.server_status not in SERVER_STATUSES:
            raise ValueError(
                "ApiTestResult has an unrecognized server_status {!r}".format(self.server_status)
            )
