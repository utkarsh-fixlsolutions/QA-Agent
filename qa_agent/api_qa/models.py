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
# docs/48-fail-fast-and-classification.md: distinct from SERVER_START_FAILED
# (the process itself never started) and SERVER_CRASHED (it started, then
# exited) - the process really did start and is still running, it just
# never accepted one single real TCP connection within the real connect-
# probe's own timeout. `run_api_qa` stops the whole run here, honestly,
# rather than proceeding to call every endpoint against a server that was
# never confirmed reachable.
SERVER_UNREACHABLE = "unreachable"

SERVER_STATUSES = (
    SERVER_STARTED,
    SERVER_ALREADY_RUNNING,
    SERVER_START_FAILED,
    SERVER_CRASHED,
    SERVER_SKIPPED,
    SERVER_UNREACHABLE,
)

# One call's outcome - PASS/FAIL mirror runtime/execution_models.py's own
# STATUS_PASS/STATUS_FAIL naming exactly, so a later phase connecting an API
# QA failure to G3 diagnosis sees a familiar shape, not a competing one.
CALL_PASS = "pass"
CALL_FAIL = "fail"
CALL_SKIPPED = "skipped"

CALL_STATUSES = (CALL_PASS, CALL_FAIL, CALL_SKIPPED)

# Phase 2 (API contract understanding, docs/53): the explicit provenance
# label for a mutating call's own constructed request body - which tier of
# evidence actually justified it, in this closed priority order (strongest
# first). A structured fact a report/test can check directly, alongside the
# existing human-readable `resolution_evidence` trace - never a replacement
# for it.
EVIDENCE_OPENAPI = "openapi"          # a live or static OpenAPI/Swagger schema
EVIDENCE_SCHEMA = "schema"            # an explicit validation/ORM schema (Zod, Mongoose, ...)
EVIDENCE_TEST_EXAMPLE = "test_example"  # a real example found in the project's own tests/collections
EVIDENCE_SOURCE_HINT = "source"       # route/controller source-code evidence
EVIDENCE_SYNTHETIC = "synthetic"      # no real evidence at all - an invented placeholder
EVIDENCE_UNKNOWN = "unknown"          # CONTRACT UNKNOWN - no body evidence, nothing sent

BODY_EVIDENCE_SOURCES = (
    EVIDENCE_OPENAPI, EVIDENCE_SCHEMA, EVIDENCE_TEST_EXAMPLE,
    EVIDENCE_SOURCE_HINT, EVIDENCE_SYNTHETIC, EVIDENCE_UNKNOWN,
)

# Phase 3 (functional API test planning, docs/54): a closed, small taxonomy
# for what a `PlannedTest` is actually for - deliberately not a large
# category tree (docs/54's own scope section names this explicitly).
TEST_CATEGORY_SMOKE = "smoke"
TEST_CATEGORY_FUNCTIONAL_POSITIVE = "functional_positive"
TEST_CATEGORY_FUNCTIONAL_WORKFLOW = "functional_workflow"
TEST_CATEGORY_EXPECTED_NEGATIVE = "expected_negative"

TEST_CATEGORIES = (
    TEST_CATEGORY_SMOKE, TEST_CATEGORY_FUNCTIONAL_POSITIVE,
    TEST_CATEGORY_FUNCTIONAL_WORKFLOW, TEST_CATEGORY_EXPECTED_NEGATIVE,
)

# Phase 3: what "correct" means for one planned test's real HTTP outcome -
# `EXPECTED_SUCCESS` is the same 2xx-and-valid-JSON rule `http_client.
# call_endpoint` already applies; `EXPECTED_NOT_FOUND` is the one, explicit,
# named exception (docs/54, section 9 of the phase spec: a 404 after a
# verified deletion is the *correct* outcome, not a failure) - a closed set,
# never an arbitrary caller-supplied status code, so this can never be
# (ab)used to declare an unrelated unexpected status "acceptable".
EXPECTED_SUCCESS = "success"
EXPECTED_NOT_FOUND = "not_found"

EXPECTED_STATUS_KINDS = (EXPECTED_SUCCESS, EXPECTED_NOT_FOUND)


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
    # Source-derived request-body field names (docs/45-synthetic-mutation
    # -testing.md), populated only for a mutating endpoint whose handler's
    # own source was recognized reading `request.json()`/`req.body` in one
    # of discovery.py's own documented shapes - never guessed from a method
    # alone. Used by resolution.py's `build_request_body` only as a
    # fallback when no live OpenAPI schema describes this operation at all.
    body_field_hints: Tuple[str, ...] = ()
    # `True` when the handler's own source was recognized reading a request
    # body at all (`await request.json()`/`req.body`, bare or destructured)
    # even when no specific field names could be extracted from that read
    # (e.g. `const body = await request.json()` with no destructuring) -
    # lets `build_request_body` tell "this endpoint definitely reads a
    # body, but we don't know its shape" (a minimal synthetic body is
    # reasonable) apart from "no evidence this endpoint reads a body at
    # all" (an honest skip stays the only reasonable outcome).
    reads_request_body: bool = False
    # Real (field_name, zod_type_token) pairs (docs/50-zod-schema-discovery
    # .md), resolved from a real `z.object({...})` schema definition this
    # endpoint's own handler actually references (`x.parse(...)`/
    # `x.safeParse(...)`/a `validate(x)` middleware call) - required fields
    # only (`.optional()`/`.nullable()`/`.default(...)` fields are real
    # evidence they are *not* required, so they're excluded, never
    # invented into a body that doesn't need them). Stronger evidence than
    # `body_field_hints` (a real name *and* a real type, not just a name)
    # - checked first in resolution.py's fallback chain when present.
    zod_fields: Tuple[Tuple[str, str], ...] = ()
    # Real (field_name, type_token) pairs found in a real Mongoose model
    # (`model_schema.py`) this endpoint's own real handler actually uses -
    # required fields only, resolved through the project's own real
    # require/import graph (never a guess at which model "probably"
    # applies). The same real-type, same-tier evidence `zod_fields` already
    # provides, just sourced from the ORM/DB layer instead of an
    # application-level validation schema - checked in resolution.py's
    # fallback chain alongside it, never a separate, weaker tier.
    model_fields: Tuple[Tuple[str, str], ...] = ()
    # Real (field_name, real_value) pairs (Phase 2, docs/53) found in the
    # project's own existing test files or Postman-style collections
    # (`test_evidence.py`) for a real HTTP call this endpoint's own
    # (method, path) structurally matches - stronger evidence than a bare
    # `body_field_hints` name alone (a real, previously-working value, not
    # just a name to guess a type for), checked ahead of it in resolution.py's
    # fallback chain but behind explicit schema evidence (OpenAPI/Zod).
    test_evidence_fields: Tuple[Tuple[str, object], ...] = ()
    # Phase 4 (generalized discovery): which real discovery strategy/
    # strategies actually produced this endpoint - e.g. `("express-source",)`,
    # or `("express-source", "openapi")` when a live source-code route and an
    # independent OpenAPI document both describe the exact same (method,
    # path) and were fused into one entry rather than reported twice (see
    # `discovery.discover_api_endpoints_detailed`'s own merge step).
    # `"express-composition"`/`"fastapi-composition"` is appended (never
    # replacing the original source tag) when a router-mount prefix was
    # resolved and prepended to this endpoint's own raw, per-file path.
    # Empty only for endpoints built before this field existed (never
    # produced by discovery.py itself since this phase) - purely additive,
    # never required by any existing caller.
    discovered_by: Tuple[str, ...] = ()

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
class UnresolvedRoute:
    r"""Phase 4 (generalized discovery): a real route-defining construct
    whose HTTP method may be known but whose concrete path could not be
    established by static analysis alone - `router.route(`/${entity}/create`)
    .post(...)` (a template literal with runtime interpolation),
    `router.get(pathVariable, handler)` (a bare variable/expression instead
    of a string literal), and their Python/FastAPI equivalents.

    Never fabricates a path, never invents a value for the unresolved
    segment, and is never fed into HTTP execution as if it were a real,
    callable `ApiEndpoint` - see discovery.py's own module docstring for
    the full "why" behind this shape. Exists purely so the real, honest
    fact "this project defines more routes than could be statically
    resolved" is visible in reporting, instead of the route silently
    vanishing the way an unmatched call shape always used to.

    `method` is `None` only on the rare path where a route-defining call
    was recognized at all but its own HTTP verb could not be determined
    either (never expected in practice - every current producer of this
    shape already knows the verb by construction).
    """

    raw_expression: str
    source_file: str
    reason: str
    method: Optional[str] = None
    line: Optional[int] = None


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
    # docs/45-synthetic-mutation-testing.md: `synthetic` is `True` only when
    # at least one value used for this call's path or body was invented
    # (qa_agent/api_qa/synthesis.py) rather than found as real evidence or a
    # real schema-declared default/example/enum. `synthetic_fields` names
    # exactly which field(s) (or the path parameter) were invented - never
    # just a bare yes/no - so a report can say precisely what was faked
    # rather than casting doubt on the whole call. Both stay at their
    # default (`False`/`()`) for every real-evidence call, unchanged from
    # today - this is a strictly additive fact, never a replacement for
    # `resolution_evidence`'s own human-readable trace.
    synthetic: bool = False
    synthetic_fields: Tuple[str, ...] = ()
    # Phase 2 (API contract understanding, docs/53): which evidence tier
    # actually justified this call's own request body, one of
    # `BODY_EVIDENCE_SOURCES` - `""` for a call with no body at all (a GET,
    # or a mutation whose body was never constructed). Orthogonal to
    # `synthetic`: a body can be sourced from real OpenAPI/test evidence and
    # still be marked `synthetic=True` (this project's own existing,
    # conservative rule - see `build_request_body`'s own docstring for why
    # even a schema-`example`-derived value is marked synthetic today) -
    # `body_evidence_source` records *why* the value was chosen, `synthetic`
    # records whether it is safe to treat as a verified default.
    body_evidence_source: str = ""
    # Auth/session propagation (`auth_context.py`): every real, raw
    # `Set-Cookie` header value this call's own real response actually sent
    # - `()` for a call with none. Read by `AuthContext.observe` to forward
    # a real session cookie into later calls; never parsed or acted on
    # anywhere else.
    response_cookies: Tuple[str, ...] = ()
    # A short, human-readable trace of a real credential (`Authorization`/
    # `Cookie`) this call carried, captured from an earlier call's own real
    # response in this same run (`auth_context.AuthContext`) - `""` when
    # this call carried no such credential, the same "empty when there is
    # none" convention `resolution_evidence` already follows. Never
    # invented; only ever describes a header that was actually attached.
    auth_evidence: str = ""

    def __post_init__(self):
        if self.status not in CALL_STATUSES:
            raise ValueError(
                "ApiCallResult({!r} {!r}) has an unrecognized status {!r}".format(
                    self.endpoint.method, self.endpoint.path, self.status
                )
            )


@dataclass(frozen=True)
class NegativeCallResult:
    """One deterministic, evidence-gated negative test case (docs/43-negative
    -tests-schema-validation-severity.md) - always derived from a real prior
    fact this same session already observed (a schema-derived valid body
    that actually worked, or a real id resolved from evidence), never a
    guess. Kept deliberately separate from `ApiCallResult` rather than mixed
    into `ApiTestResult.calls`: here `status == CALL_PASS` means "the target
    correctly rejected bad input" and `CALL_FAIL` means "it incorrectly
    accepted bad input, or crashed" - the exact inverse of what those same
    constants mean for a normal `ApiCallResult`, so folding the two together
    would make every existing renderer misread this one silently.

    `raw_call` is the real, underlying `ApiCallResult` this case's own HTTP
    call actually produced - full detail (response_sample, error, etc.) is
    always available there rather than duplicated onto this shape.
    """

    endpoint: ApiEndpoint
    case_name: str
    expected: str
    status: str
    actual_status_code: Optional[int] = None
    actual_summary: str = ""
    severity: str = ""
    raw_call: Optional[ApiCallResult] = None
    # docs/45: mirrors `ApiCallResult.synthetic`/`synthetic_fields` - `True`
    # when the *positive* call this negative case was derived from was
    # itself synthetic (its body/path used an invented value), so a
    # negative-case verdict is never presented with more confidence than
    # the positive evidence it was actually built from.
    synthetic: bool = False
    synthetic_fields: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.status not in CALL_STATUSES:
            raise ValueError(
                "NegativeCallResult({!r} {!r} {!r}) has an unrecognized status {!r}".format(
                    self.endpoint.method, self.endpoint.path, self.case_name, self.status
                )
            )


@dataclass(frozen=True)
class SchemaValidationResult:
    """Whether one GET endpoint's real, already-captured 2xx response
    (`ApiCallResult.response_json`) actually matches its own declared
    OpenAPI response schema - a pure check over data this session already
    has, never a new HTTP call. `status` is `CALL_SKIPPED` (never guessed
    pass/fail) whenever no response schema is declared for that operation.
    Deliberately never flips the originating `ApiCallResult.status` - a
    schema mismatch is additional information alongside an existing PASS,
    never a retroactive fail.
    """

    endpoint: ApiEndpoint
    status: str
    missing_fields: Tuple[str, ...] = ()
    type_mismatches: Tuple[str, ...] = ()
    severity: str = ""
    reason: str = ""

    def __post_init__(self):
        if self.status not in CALL_STATUSES:
            raise ValueError(
                "SchemaValidationResult({!r} {!r}) has an unrecognized status {!r}".format(
                    self.endpoint.method, self.endpoint.path, self.status
                )
            )


@dataclass(frozen=True)
class PlannedTest:
    """One entry in a functional test plan (Phase 3, docs/54) -
    deterministic, built from the real discovered API inventory alone
    (`planning.build_test_plan`), never from AI. `test_id` is a real,
    stable, human-readable label (`"T1"`, `"T2"`, ...) assigned in the
    exact order tests are planned - the same order `planning.
    execute_test_plan` executes them in, so a dependency is always already
    finished by the time a dependent test runs (docs/54's own "the plan is
    built in dependency order, execution just walks it" simplification -
    no separate topological sort is needed or built).

    `depends_on` names the real, human-facing prerequisite test id(s) for
    reporting - `id_source_test_id` is the one, specific prior test whose
    own real response a dynamic path parameter's value should be resolved
    from at execution time (`None` means "resolve from the general pool of
    already-executed non-dynamic GET evidence instead", the same mechanism
    `resolution.resolve_path_parameter` already uses - not a second,
    competing resolution strategy).

    `expected_status_kind` is one of `EXPECTED_STATUS_KINDS` - almost always
    `EXPECTED_SUCCESS`; `EXPECTED_NOT_FOUND` only for a test explicitly
    planned to verify a resource no longer exists after a real deletion
    (docs/54, section 9's own "a 404 can be the correct answer" rule).
    """

    test_id: str
    endpoint: ApiEndpoint
    purpose: str
    category: str
    depends_on: Tuple[str, ...] = ()
    expected_status_kind: str = EXPECTED_SUCCESS
    request_source: str = ""
    id_source_test_id: Optional[str] = None

    def __post_init__(self):
        if self.category not in TEST_CATEGORIES:
            raise ValueError(
                "PlannedTest({!r}: {!r} {!r}) has an unrecognized category {!r}".format(
                    self.test_id, self.endpoint.method, self.endpoint.path, self.category
                )
            )
        if self.expected_status_kind not in EXPECTED_STATUS_KINDS:
            raise ValueError(
                "PlannedTest({!r}: {!r} {!r}) has an unrecognized expected_status_kind {!r}".format(
                    self.test_id, self.endpoint.method, self.endpoint.path, self.expected_status_kind
                )
            )


@dataclass(frozen=True)
class PlannedTestResult:
    """One `PlannedTest`'s real, executed outcome (Phase 3, docs/54).
    `call` is the real, underlying `ApiCallResult` this test's own HTTP
    call actually produced (full detail - response_sample, error,
    resolution_evidence, synthetic flags - is always available there rather
    than duplicated onto this shape), or `None` when the test could not be
    attempted at all (no real/synthetic id was available and synthetic
    fallback was disabled, or no request body could be constructed) - the
    same "a real skip, never a guessed pass or fail" discipline
    `resolution.py`'s own `_skip` already established.

    `status` is this *test's* own verdict (`CALL_PASS`/`CALL_FAIL`/
    `CALL_SKIPPED`) - deliberately separate from `call.status`, because for
    an `EXPECTED_NOT_FOUND` test a real HTTP 404 makes `call.status ==
    CALL_FAIL` (the target's own, correct, unrelated 2xx-only pass rule)
    while the *test* itself correctly PASSes (docs/54, section 9). Never
    flips or reinterprets `call.status` itself - that remains exactly what
    `http_client.call_endpoint` actually observed.

    `resolved_param_value` is the real (or, when synthesized, clearly-
    labeled-elsewhere-via-`call.synthetic`) value substituted for this
    test's own dynamic path parameter, when it has one - kept here
    (a plain string, not re-derived from `call.resolved_path` each time)
    specifically so a *later* dependent test in the same workflow
    (e.g. "verify deletion" reusing "delete"'s own id) can reuse the exact
    same value without re-extracting it from a response a second time.
    """

    test_id: str
    test: PlannedTest
    call: Optional[ApiCallResult]
    resolved_param_value: Optional[str]
    status: str
    verdict_reason: str = ""

    def __post_init__(self):
        if self.status not in CALL_STATUSES:
            raise ValueError(
                "PlannedTestResult({!r}) has an unrecognized status {!r}".format(self.test_id, self.status)
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
    negative_calls: Tuple[NegativeCallResult, ...] = ()
    schema_validations: Tuple[SchemaValidationResult, ...] = ()
    # Phase 1 (environment readiness gate): a real GET's own outcome against
    # a health-shaped/`/health`/`/api/health`/`/` candidate, run once TCP
    # connectivity is confirmed - "" only when the server was never reached
    # at all (no readiness probe was ever attempted).
    http_readiness_detail: str = ""
    # Phase 3 (functional API test planning, docs/54): the real, structured
    # test plan `planning.build_test_plan` produced from `endpoints` (empty
    # only when no endpoints were discovered at all - the server-blocked
    # cases above never reach this far) and its own real execution outcome,
    # one `PlannedTestResult` per `PlannedTest` in `test_plan`, same order.
    # Deliberately additive and separate from `calls`: `calls` remains the
    # existing "one real (or skipped) call per discovered endpoint"
    # verification pass (docs/33) unchanged; `test_plan`/`functional_results`
    # is a distinct, dependency-aware pass that may call the same endpoint
    # more than once when a real workflow requires it (e.g. the same
    # `GET /items/{id}` endpoint verified once with a real id and once,
    # separately, to confirm it is gone after a real deletion) - something
    # `calls`' own "exactly one entry per endpoint" contract cannot express.
    test_plan: Tuple["PlannedTest", ...] = ()
    functional_results: Tuple["PlannedTestResult", ...] = ()
    # Phase 4 (generalized discovery): every real route-defining construct
    # discovery found but could not statically resolve to a concrete path -
    # see `UnresolvedRoute`'s own docstring. Never fed into `endpoints`/
    # `calls` - a distinct, honest "we saw this, but can't call it" bucket.
    unresolved_routes: Tuple[UnresolvedRoute, ...] = ()
    # Real (strategy_name, endpoint_count) pairs, one per discovery
    # strategy that actually ran (a strategy whose own gating evidence was
    # absent - e.g. Express strategies on a pure FastAPI project - is
    # omitted entirely rather than reported as a misleading "0 found").
    # Lets a report say plainly "Express source: 3, OpenAPI: 5, Tests: 0"
    # instead of a single opaque total.
    discovery_strategy_counts: Tuple[Tuple[str, int], ...] = ()
    # Real, already-detected (`project.frameworks`) backend/API framework
    # names for which discovery.py has no discovery strategy at all today
    # (e.g. "Flask", "Django", "NestJS") - reported explicitly so a project
    # using one of these reads as "framework detected, no discovery
    # strategy implemented", never silently collapsed into the same "0
    # endpoints" a framework-less or truly API-less project would also show.
    unsupported_frameworks: Tuple[str, ...] = ()

    def __post_init__(self):
        if self.server_status not in SERVER_STATUSES:
            raise ValueError(
                "ApiTestResult has an unrecognized server_status {!r}".format(self.server_status)
            )
