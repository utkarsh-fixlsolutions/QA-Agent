"""Deterministic API verification (docs/33-api-qa-deterministic-verification
.md) - `qa_agent.api_qa.resolution`: endpoint testability, evidence-based
path-parameter resolution, conservative OpenAPI-schema-based request-body
construction, dependency-aware execution ordering, and the `http_client.
call_endpoint` extensions (`response_json`/`path_override`/`body`/
`resolution_evidence`) this all builds on.

Mocked-HTTP tests reuse the same `_FakeResponse`/`_with_fake_urlopen`
technique test_api_qa.py/test_ai_openrouter.py already established. One
real, guarded, real-subprocess end-to-end test (a plain Node `http` server
standing in for a JSON API - the same "framework-agnostic mechanism, proven
with a stand-in" precedent test_api_qa.py's own real end-to-end test
already uses) proves the full tiered flow with a genuine server and genuine
HTTP calls, without needing FastAPI/uvicorn specifically. The real,
authoritative proof against a genuine FastAPI application is the separate
`D:\\Working\\qa-agent-fastapi-demo` verification (docs/33's own dogfooding
section), not this suite.
"""

from __future__ import annotations

import json
import shutil
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import (  # noqa: E402
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    ApiEndpoint,
    build_request_body,
    call_endpoint,
    resolve_and_execute,
    resolve_path_parameter,
    run_api_qa,
)
from qa_agent.api_qa import http_client as http_client_module  # noqa: E402
from qa_agent.api_qa.resolution import _Evidence  # noqa: E402


def _npm_available():
    return shutil.which("npm") is not None


def _context_for(files):
    proj = TempProject()
    for rel, text in files.items():
        proj.write(rel, text)
    result = discover_project(proj.path)
    context = build_repository_context(result.project)
    return context, proj


def _endpoint(method="GET", path="/api/users/{user_id}", source_file="app/main.py", dynamic=True):
    return ApiEndpoint(method=method, path=path, source_file=source_file, dynamic=dynamic)


def _list_endpoint(path="/api/users", source_file="app/main.py"):
    return ApiEndpoint(method="GET", path=path, source_file=source_file, dynamic=False)


def _evidence(path, response_json, method="GET"):
    return _Evidence(endpoint=_list_endpoint(path=path, source_file="x"), response_json=response_json)


# --- resolve_path_parameter (pure logic) ------------------------------

def test_resolve_path_parameter_succeeds_with_real_evidence(suite):
    endpoint = _endpoint(path="/api/users/{user_id}")
    evidence = (_evidence("/api/users", [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]),)
    concrete, note, synthetic_field = resolve_path_parameter(endpoint, evidence)
    suite.check("resolves to the real, concrete path", concrete == "/api/users/1")
    suite.check("the evidence trace names the real source", "GET /api/users" in note and "user_id" in note)
    suite.check("real evidence is never marked synthetic", synthetic_field is None)


def test_resolve_path_parameter_prefers_exact_param_name_over_id_fallback(suite):
    endpoint = _endpoint(path="/api/users/{user_id}")
    # Real data has BOTH a generic 'id' and the exact 'user_id' field -
    # the exact name must win, never the fallback, when both are present.
    evidence = (_evidence("/api/users", [{"id": 999, "user_id": 1, "name": "Alice"}]),)
    concrete, note, _ = resolve_path_parameter(endpoint, evidence)
    suite.check("the exact field name wins over the generic 'id' fallback", concrete == "/api/users/1")


def test_resolve_path_parameter_falls_back_to_id_for_id_shaped_param(suite):
    endpoint = _endpoint(path="/api/users/{user_id}")
    evidence = (_evidence("/api/users", [{"id": 7, "name": "Alice"}]),)  # only 'id', no 'user_id'
    concrete, note, _ = resolve_path_parameter(endpoint, evidence)
    suite.check("falls back to the generic 'id' field for an _id-shaped param", concrete == "/api/users/7")


def test_resolve_path_parameter_never_falls_back_to_id_for_non_id_shaped_param(suite):
    """The core anti-guessing guarantee: a parameter whose own name does
    not signal it is an identifier (e.g. 'slug') must never be resolved
    from an unrelated 'id' field - that would be a semantic mismatch, not
    a genuine resolution. `allow_synthetic_mutations=False` here to prove
    the underlying evidence-based resolution itself never guesses,
    independent of docs/45's own separate synthetic-fallback behavior.
    """
    endpoint = _endpoint(path="/api/articles/{slug}")
    evidence = (_evidence("/api/articles", [{"id": 1, "title": "Hello"}]),)
    concrete, reason, _ = resolve_path_parameter(endpoint, evidence, allow_synthetic_mutations=False)
    suite.check("never resolved from an unrelated 'id' field", concrete is None)
    suite.check("a clear, honest reason is given", "slug" in reason)


def test_resolve_path_parameter_skips_when_no_matching_parent_endpoint_evidence(suite):
    endpoint = _endpoint(path="/api/users/{user_id}")
    concrete, reason, synthetic_field = resolve_path_parameter(endpoint, (), allow_synthetic_mutations=False)
    suite.check("no evidence at all -> not resolved", concrete is None)
    suite.check("a clear, honest reason is given", "no evidence-based value" in reason)
    suite.check("no synthetic field reported for a real skip", synthetic_field is None)


def test_resolve_path_parameter_skips_when_parent_response_has_no_usable_field(suite):
    endpoint = _endpoint(path="/api/users/{user_id}")
    evidence = (_evidence("/api/users", [{"name": "Alice"}]),)  # real list, but no id-shaped field
    concrete, reason, _ = resolve_path_parameter(endpoint, evidence, allow_synthetic_mutations=False)
    suite.check("no usable field -> not resolved, never guessed", concrete is None)
    suite.check("a clear reason names the real parent endpoint", "GET /api/users" in reason)


def test_resolve_path_parameter_rejects_boolean_values(suite):
    """bool is an int subclass in Python - the same trap this project's
    own response-schema validators already guard against elsewhere,
    guarded identically here.
    """
    endpoint = _endpoint(path="/api/users/{user_id}")
    evidence = (_evidence("/api/users", [{"id": True, "name": "Alice"}]),)
    concrete, _, _ = resolve_path_parameter(endpoint, evidence, allow_synthetic_mutations=False)
    suite.check("a boolean is never treated as a real id value", concrete is None)


def test_resolve_path_parameter_rejects_multi_param_paths(suite):
    endpoint = _endpoint(path="/api/users/{user_id}/posts/{post_id}")
    evidence = (_evidence("/api/users/{user_id}/posts", [{"id": 1}]),)
    concrete, reason, _ = resolve_path_parameter(endpoint, evidence)
    suite.check("more than one dynamic segment is not attempted", concrete is None)
    suite.check("a clear reason names the real limitation", "one dynamic segment" in reason)


def test_resolve_path_parameter_resolves_a_non_trailing_param(suite):
    """Phase 2 (docs/53): intentionally changed behavior, not a regression -
    a dynamic segment followed by real, literal segments (`/forms/{formId}
    /responses`-shaped nested resources) is now resolved exactly like a
    trailing one, using the same real parent-collection evidence; the
    literal suffix (`/profile` here) is preserved in the substituted path.
    Previously this shape was refused outright (`test_resolve_path_
    parameter_rejects_non_trailing_param`, before this change) - a real,
    named scope boundary this phase deliberately lifts, since the
    substitution logic itself never actually depended on trailing position.
    """
    endpoint = _endpoint(path="/api/users/{user_id}/profile", dynamic=True)
    evidence = (_evidence("/api/users", [{"id": 1}]),)
    concrete, reason, synthetic_field = resolve_path_parameter(endpoint, evidence)
    suite.check("a non-trailing dynamic segment is now resolved from real evidence",
                 concrete == "/api/users/1/profile", " (got: {!r})".format(concrete))
    suite.check("never marked synthetic - this came from a real prior response",
                 synthetic_field is None)
    suite.check("the evidence trail names the real source", "real response" in reason)


# --- build_request_body (pure logic) ------------------------------------

def _openapi_doc(paths=None, schemas=None):
    return {"paths": paths or {}, "components": {"schemas": schemas or {}}}


def test_build_request_body_empty_when_no_body_declared(suite):
    schema_doc = _openapi_doc(paths={"/api/reset": {"post": {}}})
    body, note, synthetic_fields, _evidence_source = build_request_body(schema_doc, "POST", "/api/reset")
    suite.check("an empty, valid body is constructed", body == {})
    suite.check("evidence explains why", "no request body" in note)
    suite.check("nothing synthesized", synthetic_fields == ())


def test_build_request_body_uses_schema_defaults(suite):
    schema_doc = _openapi_doc(
        paths={"/api/users": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/UserCreate"},
        }}}}}},
        schemas={"UserCreate": {
            "required": ["name"],
            "properties": {"name": {"type": "string", "default": "test"}},
        }},
    )
    body, note, synthetic_fields, _evidence_source = build_request_body(schema_doc, "POST", "/api/users")
    suite.check("the real schema default is used", body == {"name": "test"})
    suite.check("evidence names the real source", "schema defaults" in note)
    suite.check("a real default is never marked synthetic", synthetic_fields == ())


def test_build_request_body_skipped_when_required_field_has_no_default_and_synthetic_disallowed(suite):
    """`allow_synthetic_mutations=False` reproduces the original,
    evidence-only behavior exactly (docs/45's own opt-out guarantee) -
    the default (`True`) behavior is proven separately in
    test_api_qa_synthetic_mutations.py.
    """
    schema_doc = _openapi_doc(
        paths={"/api/users": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/UserCreate"},
        }}}}}},
        schemas={"UserCreate": {
            "required": ["name", "email"],
            "properties": {"name": {"type": "string"}, "email": {"type": "string"}},
        }},
    )
    body, reason, synthetic_fields, _evidence_source = build_request_body(
        schema_doc, "POST", "/api/users", allow_synthetic_mutations=False,
    )
    suite.check("no body is invented when a required field has no default", body is None)
    suite.check("the real missing fields are named", "'name'" in reason and "'email'" in reason)
    suite.check("nothing synthesized on the skip path", synthetic_fields == ())


def test_build_request_body_skipped_when_no_schema_available(suite):
    body, reason, synthetic_fields, _evidence_source = build_request_body(None, "POST", "/api/users")
    suite.check("no schema at all, and no endpoint to fall back on -> skipped", body is None)
    suite.check("a clear reason is given", "no OpenAPI schema" in reason)
    suite.check("nothing synthesized on the skip path", synthetic_fields == ())


def test_build_request_body_skipped_when_operation_not_in_schema(suite):
    schema_doc = _openapi_doc(paths={})
    body, reason, synthetic_fields, _evidence_source = build_request_body(schema_doc, "POST", "/api/users")
    suite.check("an operation absent from the real schema, with no endpoint to fall back on, "
                "is not attempted", body is None)
    suite.check("nothing synthesized on the skip path", synthetic_fields == ())


# --- http_client extensions ---------------------------------------------

class _FakeHeaders:
    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, key, default=None):
        return self._mapping.get(key, default)


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


def _with_fake_urlopen(fake, body):
    original = http_client_module.urllib.request.urlopen
    http_client_module.urllib.request.urlopen = fake
    try:
        return body()
    finally:
        http_client_module.urllib.request.urlopen = original


def test_call_endpoint_populates_response_json_on_pass(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, b'[{"id": 1}]', {"Content-Type": "application/json"})

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _list_endpoint()))
    suite.check("real parsed JSON is exposed", result.response_json == [{"id": 1}])


def test_call_endpoint_response_json_is_none_when_not_valid_json(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, b"not json", {"Content-Type": "application/json"})

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _list_endpoint()))
    suite.check("no parsed JSON when the body is not valid JSON", result.response_json is None)


def test_call_endpoint_path_override_and_body_are_used(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return _FakeResponse(200, b"{}", {"Content-Type": "application/json"})

    endpoint = _endpoint(method="POST", path="/api/users/{user_id}", dynamic=True)
    _with_fake_urlopen(fake_urlopen, lambda: call_endpoint(
        "http://x", endpoint, path_override="/api/users/1", body={"name": "test"},
        resolution_evidence="user_id=1 (test)",
    ))
    suite.check("the concrete, overridden path is really requested", captured["url"] == "http://x/api/users/1")
    suite.check("the real body is really sent", json.loads(captured["data"]) == {"name": "test"})
    suite.check("a JSON content-type header is set", captured["headers"].get("content-type") == "application/json")


def test_call_endpoint_records_resolved_path_and_evidence(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, b"{}", {"Content-Type": "application/json"})

    endpoint = _endpoint(path="/api/users/{user_id}")
    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint(
        "http://x", endpoint, path_override="/api/users/1", resolution_evidence="user_id=1 (from GET /api/users)",
    ))
    suite.check("the concrete URL actually requested is recorded", result.resolved_path == "/api/users/1")
    suite.check("the resolution evidence is recorded", "user_id=1" in result.resolution_evidence)


def test_call_endpoint_without_override_leaves_resolved_path_empty(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, b"{}", {"Content-Type": "application/json"})

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _list_endpoint()))
    suite.check("a plain, non-dynamic call has no separate resolved_path", result.resolved_path == "")


# --- resolve_and_execute: static endpoints, classification -----------------

def _sequenced_urlopen(*fakes):
    calls = {"n": 0}

    def urlopen(request, timeout=None):
        index = calls["n"]
        calls["n"] += 1
        fake = fakes[index] if index < len(fakes) else fakes[-1]
        if isinstance(fake, Exception):
            raise fake
        return fake(request)

    return urlopen


def test_static_get_executes_and_passes(suite):
    health = ApiEndpoint(method="GET", path="/health", source_file="app/main.py", dynamic=False)

    def fake(request):
        return _FakeResponse(200, b'{"status": "ok"}', {"Content-Type": "application/json"})

    calls = _with_fake_urlopen(
        _sequenced_urlopen(fake), lambda: resolve_and_execute((health,), "http://x", timeout=5),
    )
    suite.check("exactly one result for one endpoint", len(calls) == 1)
    suite.check("a static GET is really called and passes", calls[0].status == CALL_PASS and calls[0].status_code == 200)


def test_http_500_classified_as_fail(suite):
    endpoint = ApiEndpoint(method="GET", path="/api/broken", source_file="x", dynamic=False)

    def fake(request):
        raise urllib.error.HTTPError("http://x/api/broken", 500, "Internal Server Error",
                                      hdrs={"Content-Type": "text/plain"}, fp=_FakeResponse(500, b"boom"))

    calls = _with_fake_urlopen(
        _sequenced_urlopen(fake), lambda: resolve_and_execute((endpoint,), "http://x", timeout=5),
    )
    suite.check("a real HTTP 500 is classified as FAIL", calls[0].status == CALL_FAIL and calls[0].status_code == 500)


def test_connection_failure_classified_as_fail(suite):
    endpoint = ApiEndpoint(method="GET", path="/health", source_file="x", dynamic=False)

    def fake(request):
        raise urllib.error.URLError(ConnectionRefusedError("nobody listening"))

    calls = _with_fake_urlopen(
        _sequenced_urlopen(fake), lambda: resolve_and_execute((endpoint,), "http://x", timeout=5),
    )
    suite.check("a real connection failure is classified as FAIL", calls[0].status == CALL_FAIL)
    suite.check("no status code was ever received - never invented", calls[0].status_code is None)


def test_404_is_not_automatically_treated_as_unexpected(suite):
    """A 404 for a real, valid 'not found' case is still a FAIL by this
    project's own simple, documented 2xx-only pass rule (unchanged from
    Step 30) - this test exists to make that explicit and intentional,
    not silently assumed: docs/33's own "keep expectations conservative"
    scope, never a claim that every non-2xx is a genuine server defect.
    """
    endpoint = ApiEndpoint(method="GET", path="/api/users/999", source_file="x", dynamic=False)

    def fake(request):
        raise urllib.error.HTTPError("http://x/api/users/999", 404, "Not Found",
                                      hdrs={"Content-Type": "application/json"},
                                      fp=_FakeResponse(404, b'{"detail": "not found"}'))

    calls = _with_fake_urlopen(
        _sequenced_urlopen(fake), lambda: resolve_and_execute((endpoint,), "http://x", timeout=5),
    )
    suite.check("a real 404 is reported with its own real status code, not hidden", calls[0].status_code == 404)


# --- resolve_and_execute: the critical dynamic-parameter proof -----------

def test_dynamic_get_resolved_from_prior_evidence_end_to_end(suite):
    """The exact scenario this whole step exists to prove, at the
    resolve_and_execute level (not hard-coding "1" or the endpoint name -
    the value comes from a real, fake-HTTP response, resolved generically).
    """
    list_endpoint = ApiEndpoint(method="GET", path="/api/users", source_file="x", dynamic=False)
    detail_endpoint = ApiEndpoint(method="GET", path="/api/users/{user_id}", source_file="x", dynamic=True)

    def list_fake(request):
        return _FakeResponse(200, b'[{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]',
                              {"Content-Type": "application/json"})

    def detail_fake(request):
        suite.check("the concrete, resolved URL is really requested",
                     request.full_url == "http://x/api/users/1")
        raise urllib.error.HTTPError("http://x/api/users/1", 500, "Internal Server Error",
                                      hdrs={"Content-Type": "text/plain"},
                                      fp=_FakeResponse(500, b"Internal Server Error"))

    calls = _with_fake_urlopen(
        _sequenced_urlopen(list_fake, detail_fake),
        lambda: resolve_and_execute((list_endpoint, detail_endpoint), "http://x", timeout=5),
    )
    by_path = {c.endpoint.path: c for c in calls}
    suite.check("the list endpoint passed", by_path["/api/users"].status == CALL_PASS)
    suite.check("the dynamic endpoint was actually called, not skipped", by_path["/api/users/{user_id}"].status == CALL_FAIL)
    suite.check("its real status code is the real 500", by_path["/api/users/{user_id}"].status_code == 500)
    suite.check("the concrete request URL is recorded", by_path["/api/users/{user_id}"].resolved_path == "/api/users/1")
    suite.check("resolution evidence traces back to the real prior response",
                "GET /api/users" in by_path["/api/users/{user_id}"].resolution_evidence)


def test_dynamic_get_skipped_without_evidence_when_synthetic_disallowed(suite):
    """`allow_synthetic_mutations=False` reproduces the original
    evidence-only skip exactly - the default (`True`) behavior (a
    synthetic id is used instead of skipping) is proven separately in
    test_api_qa_synthetic_mutations.py.
    """
    detail_endpoint = ApiEndpoint(method="GET", path="/api/users/{user_id}", source_file="x", dynamic=True)
    calls = _with_fake_urlopen(
        _sequenced_urlopen(),
        lambda: resolve_and_execute((detail_endpoint,), "http://x", timeout=5, allow_synthetic_mutations=False),
    )
    suite.check("skipped, never guessed", calls[0].status == CALL_SKIPPED)
    suite.check("a clear reason is given", "no evidence-based value" in calls[0].reason)
    suite.check("not marked synthetic", calls[0].synthetic is False)


def test_post_skipped_when_no_safe_body_exists_and_synthetic_disallowed(suite):
    """`allow_synthetic_mutations=False` reproduces the original
    evidence-only skip exactly - the default (`True`) behavior (the
    missing fields are synthesized instead of skipping) is proven
    separately in test_api_qa_synthetic_mutations.py.
    """
    post_endpoint = ApiEndpoint(method="POST", path="/api/users", source_file="x", dynamic=False)

    def openapi_fake(request):
        doc = _openapi_doc(
            paths={"/api/users": {"post": {"requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/UserCreate"},
            }}}}}},
            schemas={"UserCreate": {"required": ["name", "email"],
                                     "properties": {"name": {"type": "string"}, "email": {"type": "string"}}}},
        )
        return _FakeResponse(200, json.dumps(doc).encode("utf-8"), {"Content-Type": "application/json"})

    calls = _with_fake_urlopen(
        _sequenced_urlopen(openapi_fake),
        lambda: resolve_and_execute((post_endpoint,), "http://x", timeout=5, allow_synthetic_mutations=False),
    )
    suite.check("skipped, never given an invented body", calls[0].status == CALL_SKIPPED)
    suite.check("a clear, specific reason is given", "no deterministic request body" in calls[0].reason)
    suite.check("not marked synthetic", calls[0].synthetic is False)


def test_mutation_executed_when_safe_deterministic_input_available(suite):
    list_endpoint = ApiEndpoint(method="GET", path="/api/users", source_file="x", dynamic=False)
    delete_endpoint = ApiEndpoint(method="DELETE", path="/api/users/{user_id}", source_file="x", dynamic=True)

    def list_fake(request):
        return _FakeResponse(200, b'[{"id": 1, "name": "Alice"}]', {"Content-Type": "application/json"})

    def delete_fake(request):
        suite.check("DELETE really targets the resolved, concrete URL",
                     request.full_url == "http://x/api/users/1")
        suite.check("DELETE really uses the DELETE method", request.get_method() == "DELETE")
        return _FakeResponse(204, b"", {})

    calls = _with_fake_urlopen(
        _sequenced_urlopen(list_fake, delete_fake),
        lambda: resolve_and_execute((list_endpoint, delete_endpoint), "http://x", timeout=5),
    )
    by_method = {c.endpoint.method: c for c in calls}
    suite.check("DELETE was actually executed, not skipped", by_method["DELETE"].status == CALL_PASS)


def test_execution_order_resolves_dynamic_get_before_mutation_consumes_the_same_evidence(suite):
    """The real, demo-relevant ordering guarantee: if a GET and a DELETE
    both resolve to the same real id, the GET must run first - otherwise
    a DELETE running first could remove the very data the GET needed to
    observe, masking a real defect behind a false 404.
    """
    list_endpoint = ApiEndpoint(method="GET", path="/api/users", source_file="x", dynamic=False)
    detail_get = ApiEndpoint(method="GET", path="/api/users/{user_id}", source_file="x", dynamic=True)
    delete_endpoint = ApiEndpoint(method="DELETE", path="/api/users/{user_id}", source_file="x", dynamic=True)

    order = []

    def list_fake(request):
        order.append("list")
        return _FakeResponse(200, b'[{"id": 1}]', {"Content-Type": "application/json"})

    def get_fake(request):
        order.append("get")
        return _FakeResponse(200, b"{}", {"Content-Type": "application/json"})

    def delete_fake(request):
        order.append("delete")
        return _FakeResponse(204, b"", {})

    # Endpoints deliberately given to resolve_and_execute in a DIFFERENT
    # order than the required execution order, to prove the function
    # itself enforces the right order, not just accidental input order.
    _with_fake_urlopen(
        _sequenced_urlopen(list_fake, get_fake, delete_fake),
        lambda: resolve_and_execute((delete_endpoint, detail_get, list_endpoint), "http://x", timeout=5),
    )
    suite.check("list before get before delete", order == ["list", "get", "delete"], " (was {})".format(order))


def test_results_returned_in_same_order_as_input_endpoints(suite):
    list_endpoint = ApiEndpoint(method="GET", path="/api/users", source_file="x", dynamic=False)
    delete_endpoint = ApiEndpoint(method="DELETE", path="/api/users/{user_id}", source_file="x", dynamic=True)
    health = ApiEndpoint(method="GET", path="/health", source_file="x", dynamic=False)

    def list_fake(request):
        return _FakeResponse(200, b'[{"id": 1}]', {"Content-Type": "application/json"})

    def health_fake(request):
        return _FakeResponse(200, b"{}", {"Content-Type": "application/json"})

    def delete_fake(request):
        return _FakeResponse(204, b"", {})

    given_order = (delete_endpoint, health, list_endpoint)
    calls = _with_fake_urlopen(
        _sequenced_urlopen(health_fake, list_fake, delete_fake),
        lambda: resolve_and_execute(given_order, "http://x", timeout=5),
    )
    suite.check("the result order exactly matches the given endpoint order, regardless of execution order",
                tuple(c.endpoint for c in calls) == given_order)


# --- Next.js behavior is unaffected (regression) -------------------------

def test_nextjs_dynamic_segment_is_never_resolved_by_this_module(suite):
    """`[id]`-style dynamic segments (Next.js) are a different, real
    syntax this module does not attempt to resolve - a documented scope
    boundary. Proven directly: even with plausible-looking evidence
    available, a `[id]` endpoint is still always skipped.
    """
    nextjs_endpoint = ApiEndpoint(method="GET", path="/api/users/[id]", source_file="x", dynamic=True)
    evidence = (_evidence("/api/users", [{"id": 1}]),)
    concrete, reason, synthetic_field = resolve_path_parameter(nextjs_endpoint, evidence)
    suite.check("a [id]-style segment is never resolved (curly-brace syntax only)", concrete is None)
    suite.check("never marked synthetic either - not attempted at all", synthetic_field is None)


def test_run_api_qa_end_to_end_still_works_for_nextjs(suite):
    """Full regression proof through the real public entry point
    (`run_api_qa`), not just the resolver in isolation - Step 30's own
    Next.js behavior (every dynamic route skipped, every static route
    called) must be completely unaffected by this whole step existing.
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
http.createServer((req, res) => {
  if (req.url === '/api/health') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({ok: true}));
  } else {
    res.writeHead(404); res.end();
  }
}).listen(4321, () => console.log('ready - Local:        http://localhost:4321'));
""")
        proj.write("app/api/health/route.ts", "export async function GET() { return Response.json({}); }\n")
        proj.write("app/api/companions/[id]/route.ts",
                    "export async function GET() { return Response.json({}); }\n")

        from qa_agent.api_qa import ApiQaConfig
        result = discover_project(proj.path)
        real_context = build_repository_context(result.project)
        api_result = run_api_qa(real_context, proj.path, config=ApiQaConfig(server_startup_timeout=20))

        by_path = {c.endpoint.path: c for c in api_result.calls}
        suite.check("the static Next.js route still passes for real",
                     by_path["/api/health"].status == CALL_PASS)
        suite.check("the dynamic [id] Next.js route is still always skipped",
                     by_path["/api/companions/[id]"].status == CALL_SKIPPED)
    finally:
        proj.__exit__(None, None, None)


# --- one real, guarded, end-to-end proof (framework-agnostic server) -----

def test_real_end_to_end_dynamic_resolution_against_a_real_server(suite):
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "node server.js"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("package-lock.json", "{}")
        # A real Node http server standing in for a real backend - the
        # same "framework-agnostic mechanism, proven with a real stand-in"
        # precedent test_api_qa.py's own real end-to-end test established.
        # Deliberately returns a real 500 for id=1 specifically, so a real
        # resolved call proves the whole chain, not just a passing one.
        proj.write("server.js", """
const http = require('http');
const url = require('url');
http.createServer((req, res) => {
  const parsed = url.parse(req.url);
  if (parsed.pathname === '/api/items') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify([{id: 1, name: 'broken-item'}, {id: 2, name: 'ok-item'}]));
  } else if (parsed.pathname === '/api/items/1') {
    res.writeHead(500, {'Content-Type': 'text/plain'});
    res.end('Internal Server Error');
  } else if (/^\\/api\\/items\\/\\d+$/.test(parsed.pathname)) {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({id: 2, name: 'ok-item'}));
  } else {
    res.writeHead(404); res.end();
  }
}).listen(4322, () => console.log('ready - Local:        http://localhost:4322'));
""")
        proj.write("app/api/items/route.ts", "export async function GET() { return Response.json([]); }\n")
        proj.write("app/api/items/[item_id]/route.ts", "export async function GET() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)

        # The real discovery layer still reports this as a [id]-style
        # (Next.js) dynamic endpoint - to genuinely exercise resolution
        # end-to-end here, call resolve_and_execute directly against a
        # hand-described {item_id}-style endpoint list pointed at the
        # same real, running server, exactly mirroring what a real
        # FastAPI project's own discovery would have produced.
        from qa_agent.api_qa import server as server_module
        command, _evidence, _cwd = server_module.discover_server_start_command(proj.path, context.project)
        handle = server_module.start_and_wait_ready(command, proj.path, timeout=20)
        try:
            suite.check("the real server became ready", handle.status == server_module.STATUS_READY,
                         " (was {}: {})".format(handle.status, handle.reason))
            base_url = handle.base_url
            list_endpoint = ApiEndpoint(method="GET", path="/api/items", source_file="x", dynamic=False)
            detail_endpoint = ApiEndpoint(method="GET", path="/api/items/{item_id}", source_file="x", dynamic=True)
            calls = resolve_and_execute((list_endpoint, detail_endpoint), base_url, timeout=10)
            by_path = {c.endpoint.path: c for c in calls}
            suite.check("the real list endpoint passed", by_path["/api/items"].status == CALL_PASS)
            suite.check("the dynamic endpoint was really resolved and called (not skipped)",
                         by_path["/api/items/{item_id}"].status == CALL_FAIL)
            suite.check("it really hit the real broken id=1, not an invented one",
                         by_path["/api/items/{item_id}"].resolved_path == "/api/items/1")
            suite.check("the real 500 was really observed", by_path["/api/items/{item_id}"].status_code == 500)
        finally:
            handle.stop()
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: deterministic verification (resolution.py)")
    sys.exit(suite.run([
        test_resolve_path_parameter_succeeds_with_real_evidence,
        test_resolve_path_parameter_prefers_exact_param_name_over_id_fallback,
        test_resolve_path_parameter_falls_back_to_id_for_id_shaped_param,
        test_resolve_path_parameter_never_falls_back_to_id_for_non_id_shaped_param,
        test_resolve_path_parameter_skips_when_no_matching_parent_endpoint_evidence,
        test_resolve_path_parameter_skips_when_parent_response_has_no_usable_field,
        test_resolve_path_parameter_rejects_boolean_values,
        test_resolve_path_parameter_rejects_multi_param_paths,
        test_resolve_path_parameter_resolves_a_non_trailing_param,
        test_build_request_body_empty_when_no_body_declared,
        test_build_request_body_uses_schema_defaults,
        test_build_request_body_skipped_when_required_field_has_no_default_and_synthetic_disallowed,
        test_build_request_body_skipped_when_no_schema_available,
        test_build_request_body_skipped_when_operation_not_in_schema,
        test_call_endpoint_populates_response_json_on_pass,
        test_call_endpoint_response_json_is_none_when_not_valid_json,
        test_call_endpoint_path_override_and_body_are_used,
        test_call_endpoint_records_resolved_path_and_evidence,
        test_call_endpoint_without_override_leaves_resolved_path_empty,
        test_static_get_executes_and_passes,
        test_http_500_classified_as_fail,
        test_connection_failure_classified_as_fail,
        test_404_is_not_automatically_treated_as_unexpected,
        test_dynamic_get_resolved_from_prior_evidence_end_to_end,
        test_dynamic_get_skipped_without_evidence_when_synthetic_disallowed,
        test_post_skipped_when_no_safe_body_exists_and_synthetic_disallowed,
        test_mutation_executed_when_safe_deterministic_input_available,
        test_execution_order_resolves_dynamic_get_before_mutation_consumes_the_same_evidence,
        test_results_returned_in_same_order_as_input_endpoints,
        test_nextjs_dynamic_segment_is_never_resolved_by_this_module,
        test_run_api_qa_end_to_end_still_works_for_nextjs,
        test_real_end_to_end_dynamic_resolution_against_a_real_server,
    ]))
