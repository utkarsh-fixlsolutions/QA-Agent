"""Probe-based body synthesis (docs/49-probe-based-body-synthesis.md):
`qa_agent.api_qa.resolution._extract_missing_fields_from_error_response`/
`_probe_and_synthesize_body`, and their wiring into `resolve_and_execute`'s
tier-3 mutation loop.

The true last resort, tried only once `build_request_body` has already
found nothing (no OpenAPI schema, no source-derived field hints, docs/45):
send a real, empty `{}` body, and read the target's own real validation
error as evidence of which fields it actually requires, rather than give
up with a skip. This closes the remaining, most common real-world gap: a
hand-rolled project with no live schema and a body-parsing style our
source-regex strategies don't recognize (`JSON.parse` on an accumulated
raw body, a custom body-parsing helper, etc.).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import ApiQaConfig, run_api_qa  # noqa: E402
from qa_agent.api_qa import http_client as http_client_module  # noqa: E402
from qa_agent.api_qa.models import ApiEndpoint, CALL_FAIL, CALL_PASS, CALL_SKIPPED
from qa_agent.api_qa.resolution import (  # noqa: E402
    _extract_missing_fields_from_error_response,
    _probe_and_synthesize_body,
    resolve_and_execute,
)


def _npm_available():
    return shutil.which("npm") is not None


def _endpoint(method="POST", path="/api/users", source_file="x", dynamic=False):
    return ApiEndpoint(method=method, path=path, source_file=source_file, dynamic=dynamic)


# --- _extract_missing_fields_from_error_response (pure) ---------------------

def test_extract_fastapi_pydantic_detail_shape(suite):
    body = {"detail": [
        {"loc": ["body", "email"], "msg": "field required", "type": "missing"},
        {"loc": ["body", "name"], "msg": "field required", "type": "missing"},
    ]}
    suite.check("both real field names extracted from the real loc arrays",
                _extract_missing_fields_from_error_response(body) == ("email", "name"))


def test_extract_express_validator_param_shape(suite):
    body = {"errors": [{"param": "email", "msg": "required"}, {"param": "name", "msg": "required"}]}
    suite.check("both real field names extracted from 'param'",
                _extract_missing_fields_from_error_response(body) == ("email", "name"))


def test_extract_plain_field_keyed_errors_dict(suite):
    body = {"errors": {"email": "is required", "name": "is required"}}
    suite.check("both real field names extracted as dict keys",
                _extract_missing_fields_from_error_response(body) == ("email", "name"))


def test_extract_zod_flatten_shape_including_nested(suite):
    suite.check("top-level fieldErrors", _extract_missing_fields_from_error_response(
        {"fieldErrors": {"email": ["Required"]}}) == ("email",))
    suite.check("nested under 'error'", _extract_missing_fields_from_error_response(
        {"error": {"fieldErrors": {"name": ["Required"]}}}) == ("name",))


def test_extract_returns_empty_for_unrecognized_shapes(suite):
    suite.check("a generic message with no field list -> nothing extracted, never guessed",
                _extract_missing_fields_from_error_response({"message": "bad request"}) == ())
    suite.check("a non-dict body -> nothing extracted", _extract_missing_fields_from_error_response("oops") == ())
    suite.check("a real body with no error info at all -> nothing extracted",
                _extract_missing_fields_from_error_response({}) == ())


def test_extract_deduplicates_and_skips_stop_names(suite):
    body = {"detail": [
        {"loc": ["body", "email"]}, {"loc": ["body", "email"]}, {"loc": ["body"]},
    ]}
    suite.check("the repeated field appears once, and the bare 'body' segment is never treated as a field name",
                _extract_missing_fields_from_error_response(body) == ("email",))


# --- _probe_and_synthesize_body (mocked HTTP) --------------------------------

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


def test_probe_returns_the_probe_call_directly_when_empty_body_is_accepted(suite):
    def fake(request):
        return _FakeResponse(201, b'{"id": 1}', {"Content-Type": "application/json"})

    result = _with_fake_urlopen(_sequenced_urlopen(fake), lambda: _probe_and_synthesize_body(
        "http://x", _endpoint(), None, timeout=5,
    ))
    suite.check("a real 2xx on the empty-body probe is used directly", result.status == CALL_PASS)
    suite.check("never marked synthetic - the empty body genuinely worked, a real fact", result.synthetic is False)


def test_probe_synthesizes_and_retries_when_fields_are_named(suite):
    captured = []

    def fake(request):
        body = request.data.decode("utf-8") if request.data else "{}"
        captured.append(json.loads(body))
        if body == "{}":
            return _FakeResponse(
                400, b'{"errors":[{"field":"email","msg":"required"},{"field":"name","msg":"required"}]}',
                {"Content-Type": "application/json"},
            )
        return _FakeResponse(201, b'{"id": 1}', {"Content-Type": "application/json"})

    result = _with_fake_urlopen(_sequenced_urlopen(fake), lambda: _probe_and_synthesize_body(
        "http://x", _endpoint(), None, timeout=5,
    ))
    suite.check("exactly two real calls were made - the probe, then the retry", len(captured) == 2)
    suite.check("the first call really sent an empty body", captured[0] == {})
    suite.check("the retry really sent synthesized values for the real named fields",
                set(captured[1].keys()) == {"email", "name"} and captured[1]["email"] == "qa-agent-test@example.com")
    suite.check("the real retry result is returned", result.status == CALL_PASS and result.status_code == 201)
    suite.check("clearly marked synthetic", result.synthetic is True)
    suite.check("the real field names are listed", set(result.synthetic_fields) == {"email", "name"})


def test_probe_gives_up_honestly_when_no_fields_are_named(suite):
    def fake(request):
        return _FakeResponse(400, b'{"message": "bad request"}', {"Content-Type": "application/json"})

    result = _with_fake_urlopen(_sequenced_urlopen(fake), lambda: _probe_and_synthesize_body(
        "http://x", _endpoint(), None, timeout=5,
    ))
    suite.check("no usable field list -> None, never a guess", result is None)


def test_probe_gives_up_honestly_on_a_connection_failure(suite):
    import urllib.error

    def fake(request):
        raise urllib.error.URLError(ConnectionRefusedError("nobody listening"))

    result = _with_fake_urlopen(_sequenced_urlopen(fake), lambda: _probe_and_synthesize_body(
        "http://x", _endpoint(), None, timeout=5,
    ))
    suite.check("no response at all -> None, never a guess", result is None)


# --- resolve_and_execute wiring (mocked HTTP) --------------------------------

def test_resolve_and_execute_fires_a_previously_skipped_post_via_probe(suite):
    """The exact scenario this feature exists for: no OpenAPI schema, and
    no source-derived field hints either (this endpoint's own
    `body_field_hints`/`reads_request_body` are both left at their default,
    empty/False - simulating a body-parsing style our source-regex
    strategies don't recognize) - `build_request_body` alone would skip
    this every time. The probe closes the gap.
    """
    endpoint = _endpoint()
    captured = []

    def fake(request):
        # `resolve_and_execute` always tries a real `/openapi.json` fetch
        # first (existing, pre-existing behavior, unrelated to this
        # feature) - answered here as "not found" so schema stays honestly
        # None, and only the endpoint's own real calls are tracked below.
        if request.full_url.endswith("/openapi.json"):
            return _FakeResponse(404, b"", {})
        body = request.data.decode("utf-8") if request.data else "{}"
        captured.append(json.loads(body))
        if body == "{}":
            return _FakeResponse(422, b'{"errors":{"email":"is required"}}', {"Content-Type": "application/json"})
        return _FakeResponse(201, b'{"id": 1}', {"Content-Type": "application/json"})

    calls = _with_fake_urlopen(
        _sequenced_urlopen(fake), lambda: resolve_and_execute((endpoint,), "http://x", timeout=5),
    )
    call = calls[0]
    suite.check("really executed via the probe, not skipped", call.status == CALL_PASS)
    suite.check("marked synthetic", call.synthetic is True)
    suite.check("the real field the target itself named is listed", call.synthetic_fields == ("email",))
    suite.check("two real calls were really made (the empty-body probe, then the synthesized retry)",
                len(captured) == 2, " (was {})".format(captured))


def test_resolve_and_execute_still_skips_when_probe_finds_nothing_usable(suite):
    endpoint = _endpoint()

    def fake(request):
        return _FakeResponse(500, b"Internal Server Error", {"Content-Type": "text/plain"})

    calls = _with_fake_urlopen(
        _sequenced_urlopen(fake), lambda: resolve_and_execute((endpoint,), "http://x", timeout=5),
    )
    suite.check("still an honest skip when the probe itself gives nothing usable", calls[0].status == CALL_SKIPPED)
    suite.check("the skip reason mentions the probe was tried",
                "probe" in calls[0].reason)


def test_probe_never_attempted_when_synthetic_mutations_disabled(suite):
    endpoint = _endpoint()
    attempts = {"n": 0}

    def fake(request):
        # Only count real calls to the endpoint itself - the pre-existing,
        # unconditional `/openapi.json` schema-discovery attempt is real
        # but unrelated to whether *this* feature's probe ever fires.
        if not request.full_url.endswith("/openapi.json"):
            attempts["n"] += 1
        return _FakeResponse(400, b'{"errors":{"email":"required"}}', {"Content-Type": "application/json"})

    calls = _with_fake_urlopen(
        _sequenced_urlopen(fake), lambda: resolve_and_execute(
            (endpoint,), "http://x", timeout=5, allow_synthetic_mutations=False,
        ),
    )
    suite.check("still skipped, opt-out respected", calls[0].status == CALL_SKIPPED)
    suite.check("no probe call was ever made", attempts["n"] == 0)


# --- one real, guarded, end-to-end proof -------------------------------------

def test_real_end_to_end_probe_fires_a_previously_skipped_post(suite):
    """A hand-rolled Express-shaped server with NO OpenAPI schema and a
    body-parsing style (`JSON.parse` on a manually-accumulated raw body)
    our source-regex strategies don't recognize at all - proving
    `build_request_body` alone would skip this for real, and the probe
    genuinely closes the gap against a real running server.
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
  if (req.url === '/api/users' && req.method === 'POST') {
    let raw = '';
    req.on('data', c => raw += c);
    req.on('end', () => {
      const parsed = JSON.parse(raw || '{}');
      const missing = ['email', 'name'].filter(f => !(f in parsed));
      if (missing.length) {
        res.writeHead(422, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({errors: missing.map(f => ({field: f, msg: 'required'}))}));
      } else {
        res.writeHead(201, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({id: 1, ...parsed}));
      }
    });
  } else {
    res.writeHead(404); res.end();
  }
}).listen(4326, () => console.log('ready - Local:        http://localhost:4326'));
""")
        # Deliberately NOT `req.body.x`/`await request.json()` - this
        # route's own body access (raw accumulation + JSON.parse) matches
        # none of discovery.py's recognized source-derived shapes, so
        # `body_field_hints`/`reads_request_body` are both left empty -
        # only the probe can make this endpoint testable.
        proj.write("app/api/users/route.ts",
                   "export async function POST() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=20))

        suite.check("the real server started", api_result.server_status == "started",
                    " (was {}: {})".format(api_result.server_status, api_result.server_detail))
        post_call = next((c for c in api_result.calls if c.endpoint.method == "POST"), None)
        suite.check("the POST endpoint was discovered", post_call is not None)
        if post_call is not None:
            suite.check("it was really executed via the probe, not skipped",
                        post_call.status != CALL_SKIPPED, " (was {}: {})".format(post_call.status, post_call.reason))
            suite.check("it passed for real against the real server",
                        post_call.status == CALL_PASS, " (was {}: {})".format(post_call.status, post_call.reason))
            suite.check("clearly labeled synthetic", post_call.synthetic is True)
            suite.check("the real fields the server itself named are listed",
                        set(post_call.synthetic_fields) == {"email", "name"},
                        " (was {})".format(post_call.synthetic_fields))
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: probe-based body synthesis (docs/49)")
    sys.exit(suite.run([
        test_extract_fastapi_pydantic_detail_shape,
        test_extract_express_validator_param_shape,
        test_extract_plain_field_keyed_errors_dict,
        test_extract_zod_flatten_shape_including_nested,
        test_extract_returns_empty_for_unrecognized_shapes,
        test_extract_deduplicates_and_skips_stop_names,
        test_probe_returns_the_probe_call_directly_when_empty_body_is_accepted,
        test_probe_synthesizes_and_retries_when_fields_are_named,
        test_probe_gives_up_honestly_when_no_fields_are_named,
        test_probe_gives_up_honestly_on_a_connection_failure,
        test_resolve_and_execute_fires_a_previously_skipped_post_via_probe,
        test_resolve_and_execute_still_skips_when_probe_finds_nothing_usable,
        test_probe_never_attempted_when_synthetic_mutations_disabled,
        test_real_end_to_end_probe_fires_a_previously_skipped_post,
    ]))
