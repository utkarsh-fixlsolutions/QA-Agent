"""Phase 2: API contract understanding (docs/53-api-contract-understanding
.md) - `qa_agent.api_qa.test_evidence` (existing test/example evidence
discovery), the new evidence-source provenance on `build_request_body`/
`ApiCallResult.body_evidence_source`, static OpenAPI/Swagger file discovery
(`resolution.find_static_openapi_schema`), and the generalized (non-
trailing) dynamic path-parameter resolution.

Mirrors test_api_qa_resolution.py's own mix: pure-logic unit tests plus a
smaller set of real, guarded, real-subprocess end-to-end tests (a plain
Node `http` server standing in for a JSON API) proving the discovered
contract evidence actually changes the real HTTP request the existing
executor sends - not just that the evidence was found.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import (  # noqa: E402
    CALL_PASS,
    EVIDENCE_OPENAPI,
    EVIDENCE_SCHEMA,
    EVIDENCE_SOURCE_HINT,
    EVIDENCE_TEST_EXAMPLE,
    EVIDENCE_UNKNOWN,
    SERVER_STARTED,
    SERVER_UNREACHABLE,
    ApiEndpoint,
    ApiQaConfig,
    build_request_body,
    find_static_openapi_schema,
    resolve_and_execute,
    resolve_path_parameter,
    run_api_qa,
)
from qa_agent.api_qa import test_evidence as test_evidence_module  # noqa: E402
from qa_agent.api_qa.resolution import _Evidence  # noqa: E402


def _npm_available():
    import shutil
    return shutil.which("npm") is not None


# --- test_evidence.py: extraction -----------------------------------------

def test_extract_js_test_evidence_supertest(suite):
    text = """
it('creates a user', async () => {
  const res = await request(app).post('/api/users').send({name: "Alice", email: "alice@example.com"});
  expect(res.status).toBe(201);
});
"""
    entries = test_evidence_module._extract_js_test_evidence(text)
    suite.check("one entry found", len(entries) == 1, " (entries: {})".format(entries))
    method, path, fields = entries[0]
    suite.check("method is POST", method == "POST")
    suite.check("path is real", path == "/api/users")
    suite.check("real fields extracted", fields == {"name": "Alice", "email": "alice@example.com"})


def test_extract_js_test_evidence_axios(suite):
    text = "await axios.post('/api/orders', {productId: 42, quantity: 3});"
    entries = test_evidence_module._extract_js_test_evidence(text)
    suite.check("one entry found", len(entries) == 1, " (entries: {})".format(entries))
    method, path, fields = entries[0]
    suite.check("method/path real", method == "POST" and path == "/api/orders")
    suite.check("real fields extracted", fields == {"productId": 42, "quantity": 3})


def test_extract_js_test_evidence_fetch(suite):
    text = """
await fetch('/api/widgets', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({title: "Widget", active: true}),
});
"""
    entries = test_evidence_module._extract_js_test_evidence(text)
    suite.check("one entry found", len(entries) == 1, " (entries: {})".format(entries))
    method, path, fields = entries[0]
    suite.check("method/path real", method == "POST" and path == "/api/widgets")
    suite.check("real fields extracted", fields == {"title": "Widget", "active": True})


def test_extract_python_test_evidence(suite):
    text = 'def test_create_user(client):\n    r = client.post("/api/users", json={"name": "Bob", "age": 30})\n'
    entries = test_evidence_module._extract_python_test_evidence(text)
    suite.check("one entry found", len(entries) == 1, " (entries: {})".format(entries))
    method, path, fields = entries[0]
    suite.check("method/path real", method == "POST" and path == "/api/users")
    suite.check("real fields extracted", fields == {"name": "Bob", "age": 30})


def test_extract_postman_evidence(suite):
    collection = {
        "item": [
            {"name": "folder", "item": [
                {"name": "Create user", "request": {
                    "method": "POST",
                    "url": {"raw": "{{baseUrl}}/api/users"},
                    "body": {"mode": "raw", "raw": json.dumps({"name": "Carol", "email": "carol@example.com"})},
                }},
            ]},
        ]
    }
    entries = test_evidence_module._extract_postman_evidence(json.dumps(collection))
    suite.check("one entry found", len(entries) == 1, " (entries: {})".format(entries))
    method, path, fields = entries[0]
    suite.check("method real", method == "POST")
    suite.check("path found inside a nested folder", path == "{{baseUrl}}/api/users")
    suite.check("real fields extracted", fields == {"name": "Carol", "email": "carol@example.com"})


def test_segments_match_treats_dynamic_segment_as_wildcard(suite):
    suite.check("literal value matches a template segment",
                test_evidence_module._segments_match("/api/users/{id}", "/api/users/1"))
    suite.check("a full URL prefix is stripped before comparing",
                test_evidence_module._segments_match("/api/users/{id}", "http://localhost:3000/api/users/1"))
    suite.check("a different literal segment never matches",
                not test_evidence_module._segments_match("/api/users/{id}", "/api/orders/1"))
    suite.check("segment count must match exactly",
                not test_evidence_module._segments_match("/api/users/{id}", "/api/users/1/profile"))


def test_build_test_evidence_registry_end_to_end(suite):
    proj = TempProject()
    try:
        proj.write("src/users.test.js", """
test('create user', async () => {
  await request(app).post('/api/users').send({name: "Alice", email: "alice@example.com"});
});
""")
        registry = test_evidence_module.build_test_evidence_registry(proj.path)
        suite.check("real entry found in the registry", ("POST", "/api/users") in registry)
        fields = test_evidence_module.find_test_evidence_for_endpoint("POST", "/api/users", registry)
        suite.check("real fields matched for the real endpoint",
                     fields == {"name": "Alice", "email": "alice@example.com"})
    finally:
        proj.__exit__(None, None, None)


# --- Case A/B/C/D/E/F: build_request_body's evidence_source ---------------

def _openapi_doc(paths):
    return {"openapi": "3.0.0", "paths": paths}


def test_build_request_body_openapi_evidence_source(suite):
    """Case A: OpenAPI evidence - required fields correctly extracted, and
    labeled as such.
    """
    schema_doc = _openapi_doc({"/api/users": {"post": {"requestBody": {"content": {"application/json": {
        "schema": {"type": "object", "required": ["name"], "properties": {"name": {"default": "test"}}},
    }}}}}})
    body, evidence, synthetic_fields, source = build_request_body(schema_doc, "POST", "/api/users")
    suite.check("the real schema default is used", body == {"name": "test"})
    suite.check("evidence_source is OpenAPI", source == EVIDENCE_OPENAPI, " (was {!r})".format(source))


def test_build_request_body_source_hint_evidence_source(suite):
    """Case B: source evidence - straightforward request-body fields
    extracted from route source, labeled distinctly from OpenAPI/schema
    evidence.
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/items", source_file="app/api/items/route.ts",
        body_field_hints=("title", "price"),
    )
    body, evidence, synthetic_fields, source = build_request_body(None, "POST", "/api/items", endpoint=endpoint)
    suite.check("real field names become the body's own keys", set(body.keys()) == {"title", "price"})
    suite.check("evidence_source is the source-hint tier", source == EVIDENCE_SOURCE_HINT,
                 " (was {!r})".format(source))


def test_build_request_body_test_example_evidence_source(suite):
    """Case C: existing API test evidence - a valid request example found
    in the project's own tests is used to construct the real body, and
    labeled as such - distinct from a bare source-derived field name.
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/users", source_file="app/api/users/route.ts",
        test_evidence_fields=(("name", "Alice"), ("email", "alice@example.com")),
    )
    body, evidence, synthetic_fields, source = build_request_body(None, "POST", "/api/users", endpoint=endpoint)
    suite.check("the real example values are reused verbatim",
                 body == {"name": "Alice", "email": "alice@example.com"}, " (body: {})".format(body))
    suite.check("evidence_source is the test-example tier", source == EVIDENCE_TEST_EXAMPLE,
                 " (was {!r})".format(source))
    suite.check("evidence text names the real source",
                 "existing tests/collections" in evidence, " (was: {!r})".format(evidence))


def test_build_request_body_evidence_precedence_schema_over_test_example(suite):
    """Case D: evidence precedence - a stronger evidence source (an
    explicit validation schema) wins over a weaker one (a test example)
    when both exist for the same endpoint.
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/users", source_file="app/api/users/route.ts",
        zod_fields=(("age", "number"),),
        test_evidence_fields=(("name", "Alice"),),
    )
    body, evidence, synthetic_fields, source = build_request_body(None, "POST", "/api/users", endpoint=endpoint)
    suite.check("the schema (Zod) evidence wins, not the test example",
                 source == EVIDENCE_SCHEMA, " (was {!r})".format(source))
    suite.check("the schema's own real field is what's actually used", set(body.keys()) == {"age"})


def test_build_request_body_evidence_precedence_test_example_over_source_hint(suite):
    """Case D continued: a real test example wins over a bare source-derived
    field name when both exist.
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/users", source_file="app/api/users/route.ts",
        body_field_hints=("username",),
        test_evidence_fields=(("name", "Alice"),),
    )
    body, evidence, synthetic_fields, source = build_request_body(None, "POST", "/api/users", endpoint=endpoint)
    suite.check("the test-example evidence wins, not the bare source hint",
                 source == EVIDENCE_TEST_EXAMPLE, " (was {!r})".format(source))
    suite.check("the real example field is what's actually used", set(body.keys()) == {"name"})


def test_build_request_body_contract_unknown_when_nothing_found(suite):
    """Case F: unknown contract - explicitly recorded as CONTRACT UNKNOWN
    rather than pretending the contract is known, when no OpenAPI/schema/
    test-example/source evidence exists at all.
    """
    endpoint = ApiEndpoint(method="POST", path="/api/mystery", source_file="app/api/mystery/route.ts")
    body, evidence, synthetic_fields, source = build_request_body(None, "POST", "/api/mystery", endpoint=endpoint)
    suite.check("no body could be constructed", body is None)
    suite.check("the reason literally says CONTRACT UNKNOWN", "CONTRACT UNKNOWN" in evidence,
                 " (was: {!r})".format(evidence))
    suite.check("evidence_source is the unknown tier", source == EVIDENCE_UNKNOWN, " (was {!r})".format(source))


# --- Case G: nested dynamic route -------------------------------------------

def test_resolve_path_parameter_nested_dynamic_route(suite):
    """Case G: a known id can populate `/forms/{formId}/responses` - the
    dynamic segment is not the trailing one, a real, common nested-resource
    shape (Phase 2, docs/53).
    """
    endpoint = ApiEndpoint(method="GET", path="/forms/{formId}/responses", source_file="x", dynamic=True)
    forms_endpoint = ApiEndpoint(method="GET", path="/forms", source_file="y")
    evidence = (_Evidence(endpoint=forms_endpoint, response_json=[{"id": 7, "name": "Survey"}]),)
    concrete, reason, synthetic_field = resolve_path_parameter(endpoint, evidence)
    suite.check("the real id is substituted, the literal suffix preserved",
                 concrete == "/forms/7/responses", " (got: {!r})".format(concrete))
    suite.check("never synthetic - a real prior response resolved it", synthetic_field is None)


# --- static OpenAPI/Swagger file discovery ----------------------------------

def test_find_static_openapi_schema_finds_root_file(suite):
    proj = TempProject()
    try:
        proj.write("openapi.json", json.dumps({"openapi": "3.0.0", "paths": {"/api/x": {}}}))
        doc = find_static_openapi_schema(proj.path)
        suite.check("the real static file is found and parsed", doc is not None and "paths" in doc)
    finally:
        proj.__exit__(None, None, None)


def test_find_static_openapi_schema_none_when_absent(suite):
    proj = TempProject()
    try:
        doc = find_static_openapi_schema(proj.path)
        suite.check("honestly None when no static spec file exists", doc is None)
    finally:
        proj.__exit__(None, None, None)


def test_find_static_openapi_schema_ignores_a_non_openapi_json_file(suite):
    proj = TempProject()
    try:
        proj.write("openapi.json", json.dumps({"unrelated": True}))
        doc = find_static_openapi_schema(proj.path)
        suite.check("a real JSON file with no real 'paths' key is never mistaken for a spec", doc is None)
    finally:
        proj.__exit__(None, None, None)


# --- Case H/J: real, end-to-end integration ---------------------------------

def test_run_api_qa_uses_test_evidence_to_construct_a_real_request(suite):
    """Case H: the discovered contract actually changes the real request the
    existing executor sends - a real server that only accepts a POST when
    real fields (found in the project's own existing test file, not any
    OpenAPI/Zod schema) are present in the body; a blind `{"qa-agent-test":
    true}` fallback would fail this real server's own real check.
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
const server = http.createServer((req, res) => {
  if (req.method === 'POST' && req.url === '/api/users') {
    let raw = '';
    req.on('data', c => raw += c);
    req.on('end', () => {
      let body = {};
      try { body = JSON.parse(raw); } catch (e) {}
      if (body.name === 'Alice' && body.email === 'alice@example.com') {
        res.writeHead(201, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({ok: true}));
      } else {
        res.writeHead(400, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({error: 'unexpected body', got: body}));
      }
    });
  } else {
    res.writeHead(404); res.end();
  }
});
server.listen(4703, () => console.log('ready - Local:        http://localhost:4703'));
""")
        # A route handler that reads a body with NO extractable field names
        # (a bare, undestructured read) - without real test evidence, only
        # the minimal {"qa-agent-test": true} fallback would ever be tried,
        # which this real server would correctly reject.
        proj.write("app/api/users/route.ts", """
export async function POST(request) {
  const body = await request.json();
  return Response.json({ok: true});
}
""")
        # The real existing test file this endpoint's own contract evidence
        # is discovered from.
        proj.write("tests/users.test.js", """
test('creates a user', async () => {
  await request(app).post('/api/users').send({name: "Alice", email: "alice@example.com"});
});
""")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=15))

        suite.check("server really started", api_result.server_status == SERVER_STARTED,
                     " (was {}: {})".format(api_result.server_status, api_result.server_detail))
        suite.check("exactly one call was made", len(api_result.calls) == 1)
        call = api_result.calls[0]
        suite.check(
            "the real request succeeded because it used the real test-evidence fields, "
            "not a blind synthetic fallback",
            call.status == CALL_PASS, " (was: {} {} {})".format(call.status, call.status_code, call.response_sample),
        )
        suite.check("body_evidence_source names the real evidence tier used",
                     call.body_evidence_source == EVIDENCE_TEST_EXAMPLE,
                     " (was {!r})".format(call.body_evidence_source))
        suite.check("the report explains why this request was sent",
                     "existing tests/collections" in call.resolution_evidence,
                     " (was: {!r})".format(call.resolution_evidence))
    finally:
        proj.__exit__(None, None, None)


# --- Case I: Phase 1 regression (light touch - see test_api_qa.py for the
# full, dedicated Phase 1 suite; this only confirms the gate still sits in
# front of Phase 2's own new contract-understanding work). -----------------

def test_run_api_qa_still_blocks_on_an_unreachable_server(suite):
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "node server.js"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("package-lock.json", "{}")
        proj.write("server.js", "console.log('ready - Local:        http://localhost:4704');\nsetInterval(() => {}, 1000);\n")
        proj.write("app/api/users/route.ts", "export async function POST() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(
            context, proj.path,
            config=ApiQaConfig(server_startup_timeout=5, connect_probe_timeout=1),
        )
        suite.check("Phase 1's readiness gate still blocks API testing (contract understanding never bypasses it)",
                     api_result.server_status == SERVER_UNREACHABLE, " (was {})".format(api_result.server_status))
        suite.check("zero real calls were attempted",
                     len(api_result.calls) == 1 and api_result.calls[0].status == "skipped")
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: contract evidence & understanding (Phase 2, docs/53)")
    sys.exit(suite.run([
        test_extract_js_test_evidence_supertest,
        test_extract_js_test_evidence_axios,
        test_extract_js_test_evidence_fetch,
        test_extract_python_test_evidence,
        test_extract_postman_evidence,
        test_segments_match_treats_dynamic_segment_as_wildcard,
        test_build_test_evidence_registry_end_to_end,
        test_build_request_body_openapi_evidence_source,
        test_build_request_body_source_hint_evidence_source,
        test_build_request_body_test_example_evidence_source,
        test_build_request_body_evidence_precedence_schema_over_test_example,
        test_build_request_body_evidence_precedence_test_example_over_source_hint,
        test_build_request_body_contract_unknown_when_nothing_found,
        test_resolve_path_parameter_nested_dynamic_route,
        test_find_static_openapi_schema_finds_root_file,
        test_find_static_openapi_schema_none_when_absent,
        test_find_static_openapi_schema_ignores_a_non_openapi_json_file,
        test_run_api_qa_uses_test_evidence_to_construct_a_real_request,
        test_run_api_qa_still_blocks_on_an_unreachable_server,
    ]))
