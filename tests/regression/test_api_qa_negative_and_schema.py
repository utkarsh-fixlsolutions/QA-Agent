"""Negative test cases, response-schema validation, severity classification,
and expected/actual (docs/43-negative-tests-schema-validation-severity.md):
`qa_agent.api_qa.analysis` (pure severity/expected-actual rules) and the two
new `resolution.py` functions, `generate_and_execute_negative_cases` and
`validate_response_schemas`.

Same techniques `test_api_qa_resolution.py` already established: mocked
`urllib.request.urlopen` (`_FakeResponse`/`_with_fake_urlopen`/
`_sequenced_urlopen`) for deterministic-logic proof, and one real,
guarded, real-Node-server end-to-end test for genuine integration proof.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import (  # noqa: E402
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    ApiEndpoint,
    ApiQaConfig,
    run_api_qa,
)
from qa_agent.api_qa import http_client as http_client_module  # noqa: E402
from qa_agent.api_qa.models import ApiCallResult  # noqa: E402
from qa_agent.api_qa.analysis import (  # noqa: E402
    API_SEVERITY_CRITICAL,
    API_SEVERITY_HIGH,
    API_SEVERITY_MEDIUM,
    classify_call_severity,
    classify_negative_case_severity,
    classify_schema_validation_severity,
    expected_actual,
)
from qa_agent.api_qa.resolution import (  # noqa: E402
    generate_and_execute_negative_cases,
    validate_response_schemas,
)


def _npm_available():
    return shutil.which("npm") is not None


def _endpoint(method="GET", path="/api/items", source_file="x", dynamic=False):
    return ApiEndpoint(method=method, path=path, source_file=source_file, dynamic=dynamic)


# --- analysis.py: severity classification (pure) ----------------------------

def test_severity_is_none_for_pass_and_skipped(suite):
    passed = ApiCallResult(endpoint=_endpoint(), status=CALL_PASS, status_code=200)
    skipped = ApiCallResult(endpoint=_endpoint(), status=CALL_SKIPPED, reason="x")
    suite.check("PASS has no severity", classify_call_severity(passed) is None)
    suite.check("SKIPPED has no severity", classify_call_severity(skipped) is None)


def test_severity_critical_for_unreachable_and_5xx(suite):
    unreachable = ApiCallResult(endpoint=_endpoint(), status=CALL_FAIL, status_code=None, error="refused")
    crashed = ApiCallResult(endpoint=_endpoint(), status=CALL_FAIL, status_code=500)
    suite.check("no response at all -> CRITICAL", classify_call_severity(unreachable) == API_SEVERITY_CRITICAL)
    suite.check("a real 500 -> CRITICAL", classify_call_severity(crashed) == API_SEVERITY_CRITICAL)


def test_severity_medium_for_4xx(suite):
    rejected = ApiCallResult(endpoint=_endpoint(), status=CALL_FAIL, status_code=404)
    suite.check("a real 4xx -> MEDIUM", classify_call_severity(rejected) == API_SEVERITY_MEDIUM)


def test_negative_case_severity_none_when_correctly_rejected(suite):
    suite.check("PASS (correctly rejected) has no severity",
                classify_negative_case_severity(CALL_PASS, 400) is None)


def test_negative_case_severity_high_when_accepted_critical_when_crashed(suite):
    suite.check("incorrectly accepted (2xx) -> HIGH",
                classify_negative_case_severity(CALL_FAIL, 200) == API_SEVERITY_HIGH)
    suite.check("crashed (5xx) instead of validating -> CRITICAL",
                classify_negative_case_severity(CALL_FAIL, 500) == API_SEVERITY_CRITICAL)


def test_schema_validation_severity_medium_on_mismatch_none_otherwise(suite):
    suite.check("a real mismatch -> MEDIUM", classify_schema_validation_severity(CALL_FAIL) == API_SEVERITY_MEDIUM)
    suite.check("PASS/SKIPPED -> no severity", classify_schema_validation_severity(CALL_PASS) is None)
    suite.check("PASS/SKIPPED -> no severity", classify_schema_validation_severity(CALL_SKIPPED) is None)


# --- analysis.py: expected/actual (pure) ------------------------------------

def test_expected_actual_for_pass(suite):
    call = ApiCallResult(endpoint=_endpoint(), status=CALL_PASS, status_code=200, content_type="application/json")
    expected, actual = expected_actual(call)
    suite.check("expected names a 2xx response", "2xx" in expected)
    suite.check("actual names the real status code", "200" in actual)


def test_expected_actual_for_fail_with_no_response(suite):
    call = ApiCallResult(endpoint=_endpoint(), status=CALL_FAIL, status_code=None, error="connection refused")
    expected, actual = expected_actual(call)
    suite.check("expected names a reachable server", "reachable" in expected)
    suite.check("actual is the real error, not invented", actual == "connection refused")


def test_expected_actual_is_none_for_skipped(suite):
    call = ApiCallResult(endpoint=_endpoint(), status=CALL_SKIPPED, reason="no evidence")
    expected, actual = expected_actual(call)
    suite.check("nothing was really attempted, so nothing is fabricated",
                expected is None and actual is None)


# --- generate_and_execute_negative_cases (mocked HTTP) ----------------------

class _FakeHeaders:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, key, default=None):
        return self._mapping.get(key, default)

    def get_all(self, key, default=None):
        value = self._mapping.get(key, default)
        if value is None:
            return default
        return value if isinstance(value, list) else [value]


class _FakeResponse:
    def __init__(self, status, body: bytes, headers=None):
        self.status = status
        self._body = body
        self.headers = _FakeHeaders(headers or {})

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _HTTPErrorLike(Exception):
    def __init__(self, code, body=b""):
        super().__init__("HTTP {}".format(code))
        self.code = code
        self._body = body
        self.headers = _FakeHeaders({})

    def read(self, n=-1):
        return self._body


def _with_fake_urlopen(fake, body):
    original = http_client_module.urllib.request.urlopen
    http_client_module.urllib.request.urlopen = fake
    try:
        return body()
    finally:
        http_client_module.urllib.request.urlopen = original


def _sequenced_urlopen(*fakes):
    state = {"n": 0}

    def urlopen(request, timeout=None):
        index = state["n"]
        state["n"] += 1
        fake = fakes[index] if index < len(fakes) else fakes[-1]
        if isinstance(fake, Exception):
            raise fake
        return fake(request)

    return urlopen


def _openapi_doc(paths=None, schemas=None):
    return {"paths": paths or {}, "components": {"schemas": schemas or {}}}


def test_negative_missing_required_field_correctly_rejected(suite):
    """The positive POST already passed with a real, schema-derived body;
    the negative case removes the one required field and expects (and
    here, really gets) a 4xx - PASS for the negative case, no severity.
    """
    post_endpoint = ApiEndpoint(method="POST", path="/api/items", source_file="x", dynamic=False)
    positive_call = ApiCallResult(endpoint=post_endpoint, status=CALL_PASS, status_code=201)
    schema_doc = _openapi_doc(
        paths={"/api/items": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Item"},
        }}}}}},
        schemas={"Item": {"required": ["name"], "properties": {"name": {"type": "string", "default": "x"}}}},
    )

    captured = {}

    def negative_fake(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(400, b'{"error": "name is required"}', {"Content-Type": "application/json"})

    results = _with_fake_urlopen(
        negative_fake,
        lambda: generate_and_execute_negative_cases(
            (post_endpoint,), (positive_call,), "http://x", timeout=5, schema_doc=schema_doc,
        ),
    )
    suite.check("exactly one negative case was generated", len(results) == 1)
    nc = results[0]
    suite.check("the required field was really removed from the sent body", "name" not in captured["body"])
    suite.check("correctly rejected -> PASS", nc.status == CALL_PASS)
    suite.check("no severity on a correct rejection", nc.severity == "")


def test_negative_missing_required_field_incorrectly_accepted_is_high_severity(suite):
    post_endpoint = ApiEndpoint(method="POST", path="/api/items", source_file="x", dynamic=False)
    positive_call = ApiCallResult(endpoint=post_endpoint, status=CALL_PASS, status_code=201)
    schema_doc = _openapi_doc(
        paths={"/api/items": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Item"},
        }}}}}},
        schemas={"Item": {"required": ["name"], "properties": {"name": {"type": "string", "default": "x"}}}},
    )

    def negative_fake(request, timeout=None):
        return _FakeResponse(200, b'{"ok": true}', {"Content-Type": "application/json"})

    results = _with_fake_urlopen(
        negative_fake,
        lambda: generate_and_execute_negative_cases(
            (post_endpoint,), (positive_call,), "http://x", timeout=5, schema_doc=schema_doc,
        ),
    )
    nc = results[0]
    suite.check("incorrectly accepted invalid input -> FAIL", nc.status == CALL_FAIL)
    suite.check("a real validation gap -> HIGH severity", nc.severity == API_SEVERITY_HIGH)


def test_negative_case_skipped_when_positive_call_never_passed(suite):
    """No positive evidence, no negative case - never invented."""
    post_endpoint = ApiEndpoint(method="POST", path="/api/items", source_file="x", dynamic=False)
    failed_positive = ApiCallResult(endpoint=post_endpoint, status=CALL_FAIL, status_code=500)

    def unreachable_fake(request):
        raise AssertionError("must never be called - the positive case never passed")

    results = _with_fake_urlopen(
        unreachable_fake,
        lambda: generate_and_execute_negative_cases(
            (post_endpoint,), (failed_positive,), "http://x", timeout=5, schema_doc=_openapi_doc(),
        ),
    )
    suite.check("no negative case generated for a never-passing positive call", results == ())


def test_negative_nonexistent_id_numeric(suite):
    detail_endpoint = ApiEndpoint(method="GET", path="/api/items/{id}", source_file="x", dynamic=True)
    positive_call = ApiCallResult(
        endpoint=detail_endpoint, status=CALL_PASS, status_code=200, resolved_path="/api/items/42",
    )

    captured = {}

    def negative_fake(request, timeout=None):
        captured["url"] = request.full_url
        return _FakeResponse(404, b"", {})

    results = _with_fake_urlopen(
        negative_fake,
        lambda: generate_and_execute_negative_cases(
            (detail_endpoint,), (positive_call,), "http://x", timeout=5, schema_doc=_openapi_doc(),
        ),
    )
    suite.check("exactly one negative case", len(results) == 1)
    suite.check("a real, guaranteed-nonexistent numeric id was substituted",
                captured["url"] == "http://x/api/items/999999999")
    suite.check("correctly rejected -> PASS", results[0].status == CALL_PASS)


# --- validate_response_schemas (mocked HTTP for the /openapi.json fetch) ----

def test_schema_validation_passes_when_response_matches(suite):
    list_endpoint = ApiEndpoint(method="GET", path="/api/items", source_file="x", dynamic=False)
    passing_call = ApiCallResult(
        endpoint=list_endpoint, status=CALL_PASS, status_code=200,
        response_json=[{"id": 1, "name": "a"}],
    )
    schema_doc = _openapi_doc(
        paths={"/api/items": {"get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Item"},
        }}}}}}},
        schemas={"Item": {"required": ["id", "name"], "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"},
        }}},
    )
    results = validate_response_schemas(
        (list_endpoint,), (passing_call,), "http://x", timeout=5, schema_doc=schema_doc,
    )
    suite.check("exactly one schema-validation result", len(results) == 1)
    suite.check("a real, matching response -> PASS", results[0].status == CALL_PASS)


def test_schema_validation_fails_on_missing_field_never_flips_the_original_call(suite):
    list_endpoint = ApiEndpoint(method="GET", path="/api/items", source_file="x", dynamic=False)
    passing_call = ApiCallResult(
        endpoint=list_endpoint, status=CALL_PASS, status_code=200,
        response_json=[{"id": 1}],  # missing the declared-required 'name'
    )
    schema_doc = _openapi_doc(
        paths={"/api/items": {"get": {"responses": {"200": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/Item"},
        }}}}}}},
        schemas={"Item": {"required": ["id", "name"], "properties": {
            "id": {"type": "integer"}, "name": {"type": "string"},
        }}},
    )
    results = validate_response_schemas(
        (list_endpoint,), (passing_call,), "http://x", timeout=5, schema_doc=schema_doc,
    )
    sv = results[0]
    suite.check("a real missing required field -> FAIL", sv.status == CALL_FAIL)
    suite.check("the real missing field is named", "name" in sv.missing_fields)
    suite.check("the original call's own status is never touched by this", passing_call.status == CALL_PASS)


def test_schema_validation_skipped_when_no_schema_declared(suite):
    list_endpoint = ApiEndpoint(method="GET", path="/api/items", source_file="x", dynamic=False)
    passing_call = ApiCallResult(endpoint=list_endpoint, status=CALL_PASS, status_code=200, response_json=[])
    results = validate_response_schemas(
        (list_endpoint,), (passing_call,), "http://x", timeout=5, schema_doc=_openapi_doc(),
    )
    suite.check("no schema at all -> SKIPPED, not guessed", results[0].status == CALL_SKIPPED)


def test_schema_validation_never_runs_for_a_failed_call(suite):
    list_endpoint = ApiEndpoint(method="GET", path="/api/items", source_file="x", dynamic=False)
    failed_call = ApiCallResult(endpoint=list_endpoint, status=CALL_FAIL, status_code=500)
    results = validate_response_schemas(
        (list_endpoint,), (failed_call,), "http://x", timeout=5,
        schema_doc=_openapi_doc(paths={"/api/items": {"get": {"responses": {}}}}),
    )
    suite.check("nothing to validate when there was no real passing response", results == ())


# --- one real, guarded, end-to-end proof (real Node server) -----------------

def test_real_end_to_end_negative_case_and_schema_validation(suite):
    """Full proof through `run_api_qa` against a real, running server: a
    real POST endpoint that genuinely validates required fields (proving
    the negative case correctly PASSes), and a real GET response that
    genuinely mismatches its own declared schema (proving the schema
    validator correctly FAILs) - not just mocked HTTP.
    """
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "node server.js"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("package-lock.json", "{}")
        proj.write("server.js", """
const http = require('http');
const url = require('url');
let body = '';
http.createServer((req, res) => {
  const parsed = url.parse(req.url);
  if (parsed.pathname === '/openapi.json') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({
      paths: {
        '/api/items': {
          post: { requestBody: { content: { 'application/json': { schema: {
            required: ['name'], properties: { name: { type: 'string', default: 'x' } },
          } } } } },
          get: { responses: { '200': { content: { 'application/json': { schema: {
            required: ['id', 'name', 'price'], properties: {
              id: { type: 'integer' }, name: { type: 'string' }, price: { type: 'number' },
            },
          } } } } } },
        },
      },
    }));
  } else if (parsed.pathname === '/api/items' && req.method === 'GET') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    // Deliberately missing the declared-required 'price' field.
    res.end(JSON.stringify([{id: 1, name: 'widget'}]));
  } else if (parsed.pathname === '/api/items' && req.method === 'POST') {
    let raw = '';
    req.on('data', c => raw += c);
    req.on('end', () => {
      const parsedBody = JSON.parse(raw || '{}');
      if (!parsedBody.name) {
        res.writeHead(400, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({error: 'name is required'}));
      } else {
        res.writeHead(201, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({id: 2, ...parsedBody}));
      }
    });
  } else {
    res.writeHead(404); res.end();
  }
}).listen(4323, () => console.log('ready - Local:        http://localhost:4323'));
""")
        proj.write("app/api/items/route.ts",
                    "export async function GET() { return Response.json([]); }\n"
                    "export async function POST() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=20))

        suite.check("the real server started", api_result.server_status == "started",
                     " (was {}: {})".format(api_result.server_status, api_result.server_detail))

        neg_by_endpoint = {(nc.endpoint.method, nc.endpoint.path): nc for nc in api_result.negative_calls}
        neg = neg_by_endpoint.get(("POST", "/api/items"))
        suite.check("a real negative case was generated for the real POST endpoint", neg is not None)
        if neg is not None:
            suite.check("the real server correctly rejected the missing-field request -> PASS",
                         neg.status == CALL_PASS, " (was {}: {})".format(neg.status, neg.actual_summary))

        sv_by_endpoint = {(sv.endpoint.method, sv.endpoint.path): sv for sv in api_result.schema_validations}
        sv = sv_by_endpoint.get(("GET", "/api/items"))
        suite.check("a real schema-validation result was generated for the real GET endpoint", sv is not None)
        if sv is not None:
            suite.check("the real, genuinely mismatched response -> FAIL",
                         sv.status == CALL_FAIL, " (was {})".format(sv.status))
            suite.check("the real missing field is named", "price" in sv.missing_fields)

        list_call = next(c for c in api_result.calls if c.endpoint.path == "/api/items" and c.endpoint.method == "GET")
        suite.check("the original GET call's own status is never flipped by schema validation",
                     list_call.status == CALL_PASS)
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: negative test cases, schema validation, severity, expected/actual")
    sys.exit(suite.run([
        test_severity_is_none_for_pass_and_skipped,
        test_severity_critical_for_unreachable_and_5xx,
        test_severity_medium_for_4xx,
        test_negative_case_severity_none_when_correctly_rejected,
        test_negative_case_severity_high_when_accepted_critical_when_crashed,
        test_schema_validation_severity_medium_on_mismatch_none_otherwise,
        test_expected_actual_for_pass,
        test_expected_actual_for_fail_with_no_response,
        test_expected_actual_is_none_for_skipped,
        test_negative_missing_required_field_correctly_rejected,
        test_negative_missing_required_field_incorrectly_accepted_is_high_severity,
        test_negative_case_skipped_when_positive_call_never_passed,
        test_negative_nonexistent_id_numeric,
        test_schema_validation_passes_when_response_matches,
        test_schema_validation_fails_on_missing_field_never_flips_the_original_call,
        test_schema_validation_skipped_when_no_schema_declared,
        test_schema_validation_never_runs_for_a_failed_call,
        test_real_end_to_end_negative_case_and_schema_validation,
    ]))
