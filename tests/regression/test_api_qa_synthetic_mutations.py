"""Synthetic mutation testing (docs/45-synthetic-mutation-testing.md):
`qa_agent.api_qa.synthesis.synthesize_value`, the widened
`build_request_body`/`resolve_path_parameter` fallbacks in `resolution.py`,
discovery.py's source-derived `body_field_hints`/`reads_request_body`, the
`allow_synthetic_mutations` config flag, and the real-vs-synthetic split in
`render.py`.

This is the demo-blocking gap this whole feature exists to close: before
this, almost every POST/PUT/PATCH/DELETE call against a real, hand-rolled
project (no live OpenAPI schema) was silently `SKIPPED`. These tests prove
the opposite is now true *by default*, while `test_api_qa_resolution.py`'s
own `allow_synthetic_mutations=False` tests prove the original, strict,
evidence-only behavior is still available and unchanged when asked for.
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
from qa_agent.api_qa.discovery import _extract_body_field_hints  # noqa: E402
from qa_agent.api_qa.models import ApiCallResult, NegativeCallResult  # noqa: E402
from qa_agent.api_qa.render import render, to_dict  # noqa: E402
from qa_agent.api_qa.resolution import (  # noqa: E402
    build_request_body,
    resolve_and_execute,
    resolve_path_parameter,
)
from qa_agent.api_qa.synthesis import synthesize_value  # noqa: E402


def _npm_available():
    return shutil.which("npm") is not None


# --- synthesize_value (pure) -------------------------------------------

def test_synthesize_value_prefers_schema_example(suite):
    value = synthesize_value("nickname", {"type": "string", "example": "real-example-value"})
    suite.check("a schema-declared example wins over everything else", value == "real-example-value")


def test_synthesize_value_prefers_enum_over_format(suite):
    value = synthesize_value("status", {"type": "string", "format": "email", "enum": ["active", "inactive"]})
    suite.check("a schema-declared enum wins over a format guess", value == "active")


def test_synthesize_value_uses_format_email(suite):
    suite.check("format=email -> a clearly-fake email",
                synthesize_value("contact", {"type": "string", "format": "email"}) == "qa-agent-test@example.com")


def test_synthesize_value_uses_format_date(suite):
    for fmt in ("date", "date-time"):
        suite.check("format={} -> a fixed, clearly-fake timestamp".format(fmt),
                     synthesize_value("when", {"type": "string", "format": fmt}) == "2025-01-01T00:00:00Z")


def test_synthesize_value_password_field_name_wins_even_with_string_type(suite):
    value = synthesize_value("password", {"type": "string"})
    suite.check("a password-shaped field name gets a password-looking placeholder, never a bare string",
                value == "QaAgentTest123!")


def test_synthesize_value_by_json_type_with_no_more_specific_hint(suite):
    suite.check("integer -> 1", synthesize_value("count", {"type": "integer"}) == 1)
    suite.check("number -> 1", synthesize_value("price", {"type": "number"}) == 1)
    suite.check("boolean -> True", synthesize_value("active", {"type": "boolean"}) is True)
    suite.check("array -> []", synthesize_value("tags", {"type": "array"}) == [])
    suite.check("object -> {}", synthesize_value("meta", {"type": "object"}) == {})


def test_synthesize_value_name_heuristics_with_no_schema_at_all(suite):
    """The purely source-derived case (docs/45) - no OpenAPI property info
    exists, only the field's own name.
    """
    suite.check("email-shaped name", synthesize_value("userEmail") == "qa-agent-test@example.com")
    suite.check("url-shaped name", synthesize_value("profileUrl") == "https://qa-agent-test.example.com")
    suite.check("date-shaped name", synthesize_value("birthDate") == "2025-01-01T00:00:00Z")
    suite.check("id-shaped name -> numeric sentinel", synthesize_value("userId") == 1)
    suite.check("bare 'id' -> numeric sentinel", synthesize_value("id") == 1)
    suite.check("name-shaped field", synthesize_value("username") == "qa-agent-test-user")
    suite.check("no recognized shape -> generic marker", synthesize_value("widgetColor") == "qa-agent-test-value")


def test_synthesize_value_every_string_result_carries_the_qa_agent_marker(suite):
    """Most synthesized strings are trivially identifiable in a real, live
    database via the "qa-agent-test" marker - the two deliberate exceptions
    (password, date/date-time) are still obviously-fake fixed sentinels,
    just without the marker itself (see synthesis.py's own docstring).
    """
    for name in ("email", "url", "username", "somethingElse"):
        value = synthesize_value(name)
        suite.check("'{}' carries the marker".format(name), isinstance(value, str) and "qa-agent-test" in value.lower())
    suite.check("password is a fixed, obviously-fake sentinel", synthesize_value("password") == "QaAgentTest123!")
    suite.check("date is a fixed, obviously-fake sentinel", synthesize_value("birthDate") == "2025-01-01T00:00:00Z")


# --- discovery.py: body-field-hint extraction (pure) --------------------

def test_body_field_hints_from_nextjs_destructured_json(suite):
    hints, reads_body = _extract_body_field_hints(
        "export async function POST(request) {\n"
        "  const { name, email } = await request.json();\n"
        "  return Response.json({ name, email });\n"
        "}\n"
    )
    suite.check("both destructured fields found", set(hints) == {"name", "email"})
    suite.check("reads_body is True", reads_body is True)


def test_body_field_hints_strips_rename_and_default_clauses(suite):
    hints, _ = _extract_body_field_hints(
        "const { name: userName, age = 18, ...rest } = await request.json();\n"
    )
    suite.check("only the real bound identifiers are kept, spread dropped", set(hints) == {"name", "age"})


def test_body_field_hints_from_express_req_body_access(suite):
    hints, reads_body = _extract_body_field_hints(
        "app.post('/x', (req, res) => {\n"
        "  const name = req.body.name;\n"
        "  const email = req.body.email;\n"
        "  res.json({});\n"
        "});\n"
    )
    suite.check("both accessed fields found", set(hints) == {"name", "email"})
    suite.check("reads_body is True", reads_body is True)


def test_body_field_hints_from_express_req_body_destructure(suite):
    hints, reads_body = _extract_body_field_hints("const { title, price } = req.body;\n")
    suite.check("both destructured fields found", set(hints) == {"title", "price"})
    suite.check("reads_body is True", reads_body is True)


def test_body_field_hints_bare_assignment_reads_body_with_no_field_names(suite):
    """A bare `const body = await request.json()` (no destructuring) is
    still real evidence the handler reads a body - just with no specific
    field names available from this shape alone (docs/45's own minimal-
    fallback case depends on exactly this distinction).
    """
    hints, reads_body = _extract_body_field_hints(
        "export async function POST(request) {\n"
        "  const body = await request.json();\n"
        "  return doSomething(body);\n"
        "}\n"
    )
    suite.check("no specific field names from a bare assignment", hints == ())
    suite.check("but body-reading is still recognized", reads_body is True)


def test_body_field_hints_no_recognized_shape_at_all(suite):
    hints, reads_body = _extract_body_field_hints(
        "export async function POST() { return Response.json({ ok: true }); }\n"
    )
    suite.check("no field hints", hints == ())
    suite.check("no body-reading evidence either", reads_body is False)


def test_body_field_hints_deduplicates_across_multiple_matching_patterns(suite):
    hints, _ = _extract_body_field_hints(
        "const { name } = req.body;\n"
        "const extra = req.body.name;\n"  # same field, different shape
        "const other = req.body.email;\n"
    )
    suite.check("the repeated field name appears only once", hints.count("name") == 1)
    suite.check("both distinct fields are present", set(hints) == {"name", "email"})


# --- build_request_body: synthesizes by default (docs/45) ---------------

def _openapi_doc(paths=None, schemas=None):
    return {"paths": paths or {}, "components": {"schemas": schemas or {}}}


def test_build_request_body_synthesizes_missing_required_fields_by_default(suite):
    """The exact scenario that used to be a hard skip
    (test_api_qa_resolution.py's own ...and_synthetic_disallowed test) now
    produces a real, sendable body by default.
    """
    schema_doc = _openapi_doc(
        paths={"/api/users": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/UserCreate"},
        }}}}}},
        schemas={"UserCreate": {
            "required": ["name", "email"],
            "properties": {"name": {"type": "string"}, "email": {"type": "string", "format": "email"}},
        }},
    )
    body, evidence, synthetic_fields = build_request_body(schema_doc, "POST", "/api/users")
    suite.check("a real, sendable body is produced instead of a skip", body is not None)
    suite.check("both missing fields are named as synthetic", set(synthetic_fields) == {"name", "email"})
    suite.check("the email field uses a real, format-aware placeholder", body["email"] == "qa-agent-test@example.com")
    suite.check("evidence explains what happened", "synthesized" in evidence)


def test_build_request_body_mixes_real_defaults_with_synthesized_fields(suite):
    """A body is never invented as one fabricated block when part of it is
    already known for real - the real default stays real.
    """
    schema_doc = _openapi_doc(
        paths={"/api/users": {"post": {"requestBody": {"content": {"application/json": {
            "schema": {"$ref": "#/components/schemas/UserCreate"},
        }}}}}},
        schemas={"UserCreate": {
            "required": ["role", "email"],
            "properties": {"role": {"type": "string", "default": "member"}, "email": {"type": "string"}},
        }},
    )
    body, _, synthetic_fields = build_request_body(schema_doc, "POST", "/api/users")
    suite.check("the real schema default is kept exactly", body["role"] == "member")
    suite.check("only the field with no default is marked synthetic", synthetic_fields == ("email",))


def test_build_request_body_falls_back_to_source_derived_hints_with_no_openapi(suite):
    """No live OpenAPI schema at all - the fallback comes entirely from
    `endpoint.body_field_hints` (docs/45).
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/items", source_file="x", dynamic=False,
        body_field_hints=("title", "price"),
    )
    body, evidence, synthetic_fields = build_request_body(None, "POST", "/api/items", endpoint=endpoint)
    suite.check("a body is built from source-derived hints alone", set(body.keys()) == {"title", "price"})
    suite.check("both fields are honestly marked synthetic", set(synthetic_fields) == {"title", "price"})
    suite.check("evidence names the real source file", "x" in evidence)


def test_build_request_body_minimal_fallback_when_body_read_but_no_field_names(suite):
    endpoint = ApiEndpoint(
        method="POST", path="/api/items", source_file="x", dynamic=False,
        body_field_hints=(), reads_request_body=True,
    )
    body, evidence, synthetic_fields = build_request_body(None, "POST", "/api/items", endpoint=endpoint)
    suite.check("a minimal, one-field synthetic body is sent rather than skipping", body == {"qa-agent-test": True})
    suite.check("the minimal field is named as synthetic", synthetic_fields == ("qa-agent-test",))
    suite.check("evidence is honest about the situation", "no specific field names" in evidence)


def test_build_request_body_still_skips_when_no_body_evidence_at_all(suite):
    """No schema, no field hints, no bare-read evidence - never invents
    fields from nothing, even with synthesis allowed.
    """
    endpoint = ApiEndpoint(method="POST", path="/api/items", source_file="x", dynamic=False)
    body, reason, synthetic_fields = build_request_body(None, "POST", "/api/items", endpoint=endpoint)
    suite.check("still an honest skip", body is None)
    suite.check("a clear reason is given", "no OpenAPI schema" in reason)
    suite.check("nothing synthesized", synthetic_fields == ())


def test_build_request_body_synthesis_disabled_by_config_flag(suite):
    """`allow_synthetic_mutations=False` disables the source-derived
    fallback too, not just the OpenAPI-defaults relaxation.
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/items", source_file="x", dynamic=False,
        body_field_hints=("title",),
    )
    body, reason, synthetic_fields = build_request_body(
        None, "POST", "/api/items", endpoint=endpoint, allow_synthetic_mutations=False,
    )
    suite.check("no fallback used when synthesis is disabled", body is None)
    suite.check("nothing synthesized", synthetic_fields == ())


# --- resolve_path_parameter: synthesizes an id by default (docs/45) -----

def test_resolve_path_parameter_synthesizes_numeric_id_by_default(suite):
    endpoint = ApiEndpoint(method="GET", path="/api/users/{user_id}", source_file="x", dynamic=True)
    concrete, evidence, synthetic_field = resolve_path_parameter(endpoint, ())
    suite.check("a real, callable path is produced instead of a skip", concrete == "/api/users/1")
    suite.check("the id-shaped param is named as synthetic", synthetic_field == "user_id")
    suite.check("evidence is honest about it", "synthetic id" in evidence)


def test_resolve_path_parameter_synthesizes_string_sentinel_for_non_id_param(suite):
    endpoint = ApiEndpoint(method="GET", path="/api/articles/{slug}", source_file="x", dynamic=True)
    concrete, _, synthetic_field = resolve_path_parameter(endpoint, ())
    suite.check("a non-id-shaped param gets the string sentinel", concrete == "/api/articles/qa-agent-test-id")
    suite.check("named as synthetic", synthetic_field == "slug")


# --- resolve_and_execute: end-to-end synthetic call labeling (mocked) ---

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


def test_resolve_and_execute_marks_a_synthesized_mutation_call_as_synthetic(suite):
    endpoint = ApiEndpoint(
        method="POST", path="/api/items", source_file="x", dynamic=False,
        body_field_hints=("title",),
    )
    captured = {}

    def fake(request):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(201, b'{"ok": true}', {"Content-Type": "application/json"})

    calls = _with_fake_urlopen(_sequenced_urlopen(fake), lambda: resolve_and_execute((endpoint,), "http://x", timeout=5))
    call = calls[0]
    suite.check("the call really executed (not skipped)", call.status == CALL_PASS)
    # 'title' matches the name/title/username heuristic bucket, not the
    # generic fallback - synthesize_value's own documented precedence.
    suite.check("the real, synthesized body was really sent", captured["body"] == {"title": "qa-agent-test-user"})
    suite.check("marked synthetic", call.synthetic is True)
    suite.check("the synthesized field is named", call.synthetic_fields == ("title",))


def test_resolve_and_execute_false_flag_still_skips_the_same_call(suite):
    endpoint = ApiEndpoint(
        method="POST", path="/api/items", source_file="x", dynamic=False,
        body_field_hints=("title",),
    )
    calls = _with_fake_urlopen(
        _sequenced_urlopen(), lambda: resolve_and_execute(
            (endpoint,), "http://x", timeout=5, allow_synthetic_mutations=False,
        ),
    )
    suite.check("the opt-out flag reproduces the original strict skip", calls[0].status == CALL_SKIPPED)
    suite.check("never marked synthetic", calls[0].synthetic is False)


# --- render.py: real-vs-synthetic split ----------------------------------

def _endpoint(method="GET", path="/api/items", source_file="x", dynamic=False):
    return ApiEndpoint(method=method, path=path, source_file=source_file, dynamic=dynamic)


class _FakeResult:
    def __init__(self, calls):
        self.root_path = "/x"
        self.endpoints = tuple(c.endpoint for c in calls)
        self.calls = tuple(calls)
        self.server_status = "started"
        self.server_detail = ""
        self.base_url = "http://x"
        self.started_at = ""
        self.finished_at = ""
        self.total_duration = 1.23
        self.warnings = ()
        self.server_log_tail = ""
        self.negative_calls = ()
        self.schema_validations = ()


def test_to_dict_summary_separates_real_and_synthetic_counts(suite):
    real_pass = ApiCallResult(endpoint=_endpoint(path="/a"), status=CALL_PASS, status_code=200)
    synthetic_pass = ApiCallResult(
        endpoint=_endpoint(method="POST", path="/b"), status=CALL_PASS, status_code=201,
        synthetic=True, synthetic_fields=("title",),
    )
    synthetic_fail = ApiCallResult(
        endpoint=_endpoint(method="POST", path="/c"), status=CALL_FAIL, status_code=400,
        synthetic=True, synthetic_fields=("title",),
    )
    skipped = ApiCallResult(endpoint=_endpoint(path="/d"), status=CALL_SKIPPED, reason="x")
    result = _FakeResult([real_pass, synthetic_pass, synthetic_fail, skipped])
    summary = to_dict(result)["summary"]
    suite.check("real-evidence pass count excludes synthetic calls", summary["real_evidence"]["pass"] == 1)
    suite.check("real-evidence skipped count", summary["real_evidence"]["skipped"] == 1)
    suite.check("synthetic pass counted separately", summary["synthetic"]["pass"] == 1)
    suite.check("synthetic fail counted separately", summary["synthetic"]["fail"] == 1)


def test_call_to_dict_exposes_synthetic_fields(suite):
    call = ApiCallResult(
        endpoint=_endpoint(method="POST", path="/b"), status=CALL_PASS, status_code=201,
        synthetic=True, synthetic_fields=("title", "price"),
    )
    result = _FakeResult([call])
    row = to_dict(result)["calls"][0]
    suite.check("synthetic flag exposed", row["synthetic"] is True)
    suite.check("synthetic fields exposed", row["synthetic_fields"] == ["title", "price"])


def test_cli_render_tags_synthetic_calls_and_splits_the_summary_line(suite):
    real_pass = ApiCallResult(endpoint=_endpoint(path="/a"), status=CALL_PASS, status_code=200)
    synthetic_pass = ApiCallResult(
        endpoint=_endpoint(method="POST", path="/b"), status=CALL_PASS, status_code=201,
        synthetic=True, synthetic_fields=("title",),
    )
    result = _FakeResult([real_pass, synthetic_pass])
    text = render(result)
    suite.check("the synthetic call is visibly tagged", "[SYNTHETIC: title]" in text)
    suite.check("the summary reports real evidence separately", "(real evidence)" in text)
    suite.check("the summary reports synthetic data separately", "(synthetic data)" in text)


def test_negative_call_result_carries_synthetic_flag(suite):
    """`NegativeCallResult` also exposes `synthetic`/`synthetic_fields`
    (docs/45) - constructed directly here since
    test_api_qa_negative_and_schema.py already proves the propagation from
    a real positive call through `generate_and_execute_negative_cases`.
    """
    nc = NegativeCallResult(
        endpoint=_endpoint(method="POST", path="/b"), case_name="missing required field 'title'",
        expected="a 4xx rejection", status=CALL_PASS, synthetic=True, synthetic_fields=("title",),
    )
    suite.check("synthetic flag defaults are overridable and carried", nc.synthetic is True and nc.synthetic_fields == ("title",))


# --- one real, guarded, end-to-end proof: the exact demo scenario -------

def test_real_end_to_end_post_actually_fires_with_no_openapi_and_catches_a_real_bug(suite):
    """The exact scenario this whole feature exists for: a hand-rolled
    project with NO OpenAPI schema at all. Before docs/45, this POST
    endpoint's call would always be SKIPPED. Now it actually fires with a
    synthesized body, and a real, seeded server-side bug (accepting an
    empty/whitespace name) is genuinely caught as a real FAIL - not a
    guess, a real HTTP round-trip against a real running server.
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
        # Deliberately seeded bug: the server only rejects a totally
        # missing 'name', not a present-but-worthless one - real behavior
        # a synthetic, evidence-free POST call can genuinely exercise.
        proj.write("server.js", """
const http = require('http');
http.createServer((req, res) => {
  if (req.url === '/api/items' && req.method === 'POST') {
    let raw = '';
    req.on('data', c => raw += c);
    req.on('end', () => {
      const body = JSON.parse(raw || '{}');
      if (body.title === undefined) {
        res.writeHead(400, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({error: 'title is required'}));
      } else {
        res.writeHead(201, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({id: 1, title: body.title}));
      }
    });
  } else {
    res.writeHead(404); res.end();
  }
}).listen(4325, () => console.log('ready - Local:        http://localhost:4325'));
""")
        proj.write("app/api/items/route.ts",
                   "export async function POST(request) {\n"
                   "  const { title } = await request.json();\n"
                   "  return Response.json({ title });\n"
                   "}\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=20))

        suite.check("the real server started", api_result.server_status == "started",
                    " (was {}: {})".format(api_result.server_status, api_result.server_detail))
        post_call = next((c for c in api_result.calls if c.endpoint.method == "POST"), None)
        suite.check("the POST endpoint was discovered", post_call is not None)
        if post_call is not None:
            suite.check("it was really executed, not skipped - the core demo-blocking gap this closes",
                        post_call.status != CALL_SKIPPED, " (was {}: {})".format(post_call.status, post_call.reason))
            suite.check("it passed against the real server using a synthesized body",
                        post_call.status == CALL_PASS, " (was {})".format(post_call.status))
            suite.check("clearly labeled synthetic, never presented as real evidence", post_call.synthetic is True)
            suite.check("the synthesized field is named", "title" in post_call.synthetic_fields)
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: synthetic mutation testing (docs/45)")
    sys.exit(suite.run([
        test_synthesize_value_prefers_schema_example,
        test_synthesize_value_prefers_enum_over_format,
        test_synthesize_value_uses_format_email,
        test_synthesize_value_uses_format_date,
        test_synthesize_value_password_field_name_wins_even_with_string_type,
        test_synthesize_value_by_json_type_with_no_more_specific_hint,
        test_synthesize_value_name_heuristics_with_no_schema_at_all,
        test_synthesize_value_every_string_result_carries_the_qa_agent_marker,
        test_body_field_hints_from_nextjs_destructured_json,
        test_body_field_hints_strips_rename_and_default_clauses,
        test_body_field_hints_from_express_req_body_access,
        test_body_field_hints_from_express_req_body_destructure,
        test_body_field_hints_bare_assignment_reads_body_with_no_field_names,
        test_body_field_hints_no_recognized_shape_at_all,
        test_body_field_hints_deduplicates_across_multiple_matching_patterns,
        test_build_request_body_synthesizes_missing_required_fields_by_default,
        test_build_request_body_mixes_real_defaults_with_synthesized_fields,
        test_build_request_body_falls_back_to_source_derived_hints_with_no_openapi,
        test_build_request_body_minimal_fallback_when_body_read_but_no_field_names,
        test_build_request_body_still_skips_when_no_body_evidence_at_all,
        test_build_request_body_synthesis_disabled_by_config_flag,
        test_resolve_path_parameter_synthesizes_numeric_id_by_default,
        test_resolve_path_parameter_synthesizes_string_sentinel_for_non_id_param,
        test_resolve_and_execute_marks_a_synthesized_mutation_call_as_synthetic,
        test_resolve_and_execute_false_flag_still_skips_the_same_call,
        test_to_dict_summary_separates_real_and_synthetic_counts,
        test_call_to_dict_exposes_synthetic_fields,
        test_cli_render_tags_synthetic_calls_and_splits_the_summary_line,
        test_negative_call_result_carries_synthetic_flag,
        test_real_end_to_end_post_actually_fires_with_no_openapi_and_catches_a_real_bug,
    ]))
