"""General auth/session propagation (`qa_agent.api_qa.auth_context`): a real
bearer token or session cookie found in any real, passing response is
captured and attached to every later real call in the same run - across
`resolution.resolve_and_execute`, `resolution.generate_and_execute_
negative_cases`, and `planning.execute_test_plan`, which all share one
`AuthContext` instance (`runner.py`). Never targets a specific endpoint
name/path - any real response can be the source.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite  # noqa: E402

from qa_agent.api_qa.auth_context import AuthContext, find_auth_token  # noqa: E402
from qa_agent.api_qa.models import ApiCallResult, ApiEndpoint, CALL_FAIL, CALL_PASS, CALL_SKIPPED  # noqa: E402


def _endpoint(method="POST", path="/api/login"):
    return ApiEndpoint(method=method, path=path, source_file="x")


def _result(status, response_json=None, response_cookies=()):
    return ApiCallResult(
        endpoint=_endpoint(), status=status, status_code=200 if status == CALL_PASS else 401,
        response_json=response_json, response_cookies=response_cookies,
    )


# --- find_auth_token (pure) --------------------------------------------

def test_top_level_token_field_is_found(suite):
    suite.check("a plain top-level 'token' field is found",
                 find_auth_token({"token": "abcdef1234567890"}) == "abcdef1234567890")


def test_nested_result_wrapper_is_found(suite):
    """idurar-erp-crm's own real login response shape: the token nested one
    level down inside a `result` wrapper, not top-level.
    """
    payload = {"success": True, "result": {"_id": "1", "name": "Alice", "token": "eyFakeJwt.abc.def"}}
    suite.check("a token nested inside a real wrapper field is found",
                 find_auth_token(payload) == "eyFakeJwt.abc.def")


def test_various_common_field_names_are_recognized(suite):
    suite.check("accessToken", find_auth_token({"accessToken": "0123456789abcdef"}) == "0123456789abcdef")
    suite.check("access_token", find_auth_token({"access_token": "0123456789abcdef"}) == "0123456789abcdef")
    suite.check("jwt", find_auth_token({"jwt": "0123456789abcdef"}) == "0123456789abcdef")
    suite.check("sessionToken", find_auth_token({"sessionToken": "0123456789abcdef"}) == "0123456789abcdef")


def test_short_or_non_string_values_are_never_treated_as_a_token(suite):
    suite.check("a too-short string is not plausible", find_auth_token({"token": "abc"}) is None)
    suite.check("a non-string value is not plausible", find_auth_token({"token": 12345678}) is None)
    suite.check("a non-dict response has nothing to find", find_auth_token(["not", "a", "dict"]) is None)
    suite.check("None response has nothing to find", find_auth_token(None) is None)


def test_unrelated_field_names_are_never_mistaken_for_a_token(suite):
    payload = {"success": True, "result": {"name": "Alice", "email": "a@example.com"}}
    suite.check("no recognized token-shaped field exists - nothing is guessed",
                 find_auth_token(payload) is None)


# --- AuthContext.observe / headers ---------------------------------------

def test_no_credential_yet_means_no_headers(suite):
    ctx = AuthContext()
    suite.check("empty headers before anything is observed", ctx.headers() == {})


def test_observing_a_pass_with_a_token_produces_an_authorization_header(suite):
    ctx = AuthContext()
    ctx.observe(_endpoint(), _result(CALL_PASS, response_json={"token": "abcdef1234567890"}))
    suite.check("a real Authorization: Bearer header is now produced",
                 ctx.headers() == {"Authorization": "Bearer abcdef1234567890"})


def test_a_failed_or_skipped_call_never_seeds_a_credential(suite):
    ctx = AuthContext()
    ctx.observe(_endpoint(), _result(CALL_FAIL, response_json={"token": "abcdef1234567890"}))
    suite.check("a FAIL response is never trusted as real credential evidence", ctx.headers() == {})
    ctx.observe(_endpoint(), ApiCallResult(endpoint=_endpoint(), status=CALL_SKIPPED))
    suite.check("a SKIPPED call is never trusted either", ctx.headers() == {})


def test_first_real_token_is_kept_not_replaced_by_a_later_one(suite):
    ctx = AuthContext()
    ctx.observe(_endpoint(path="/api/login"), _result(CALL_PASS, response_json={"token": "firsttoken12345"}))
    ctx.observe(_endpoint(path="/api/refresh"), _result(CALL_PASS, response_json={"token": "secondtoken6789"}))
    suite.check("the first real token found in the run is kept for its whole duration",
                 ctx.headers()["Authorization"] == "Bearer firsttoken12345")


def test_set_cookie_is_captured_and_forwarded(suite):
    ctx = AuthContext()
    ctx.observe(_endpoint(), _result(
        CALL_PASS, response_json={"ok": True},
        response_cookies=("connect.sid=s%3Aabc123; Path=/; HttpOnly",),
    ))
    suite.check("a real session cookie is captured and forwarded as a Cookie header",
                 ctx.headers().get("Cookie") == "connect.sid=s%3Aabc123")


def test_token_and_cookie_can_both_be_attached_together(suite):
    ctx = AuthContext()
    ctx.observe(_endpoint(), _result(
        CALL_PASS, response_json={"token": "abcdef1234567890"},
        response_cookies=("session=xyz",),
    ))
    headers = ctx.headers()
    suite.check("both real credentials are attached at once",
                 headers.get("Authorization") == "Bearer abcdef1234567890" and headers.get("Cookie") == "session=xyz")


def test_evidence_text_is_empty_until_something_is_captured(suite):
    ctx = AuthContext()
    suite.check("no evidence text before anything is captured", ctx.evidence_for_attached_call() == "")
    ctx.observe(_endpoint(path="/api/login"), _result(CALL_PASS, response_json={"token": "abcdef1234567890"}))
    suite.check("evidence names the real source endpoint",
                 "POST /api/login" in ctx.evidence_for_attached_call())


# --- real, end-to-end wiring through resolve_and_execute -----------------

def test_end_to_end_login_token_reaches_a_later_call(suite):
    """The concrete idurar-erp-crm shape: a login endpoint returns a real
    token nested in `result`, and a later, otherwise-401-only endpoint
    receives it automatically - proven against a real HTTP server, not a
    mock, so the real Authorization header is genuinely sent and read back.
    """
    import http.server
    import json as _json
    import threading

    from qa_agent.api_qa.resolution import resolve_and_execute

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = _json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/login":
                self._send(200, {"success": True, "result": {"token": "realtoken1234567890"}})
                return
            if self.path == "/api/profile":
                auth = self.headers.get("Authorization", "")
                if auth == "Bearer realtoken1234567890":
                    self._send(200, {"ok": True})
                else:
                    self._send(401, {"message": "No authentication token, authorization denied."})
                return
            self._send(404, {})

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoints = (
            ApiEndpoint(method="GET", path="/api/login", source_file="x"),
            ApiEndpoint(method="GET", path="/api/profile", source_file="x"),
        )
        calls = resolve_and_execute(endpoints, "http://127.0.0.1:{}".format(port), timeout=5)
        by_path = {c.endpoint.path: c for c in calls}
        suite.check("the login call itself passed", by_path["/api/login"].status == CALL_PASS)
        suite.check("the profile call, which requires the token, also passed - the real "
                     "captured token was really sent",
                     by_path["/api/profile"].status == CALL_PASS,
                     " (was {}, {})".format(by_path["/api/profile"].status, by_path["/api/profile"].reason))
        suite.check("the profile call's own auth_evidence names where the credential came from",
                     "login" in by_path["/api/profile"].auth_evidence)
        suite.check("the login call itself carries no auth_evidence - it produced the "
                     "credential, it did not receive one",
                     by_path["/api/login"].auth_evidence == "")
    finally:
        server.shutdown()
        thread.join(timeout=5)


if __name__ == "__main__":
    suite = Suite("Auth/session propagation (auth_context.py)")
    exit_code = suite.run([
        test_top_level_token_field_is_found,
        test_nested_result_wrapper_is_found,
        test_various_common_field_names_are_recognized,
        test_short_or_non_string_values_are_never_treated_as_a_token,
        test_unrelated_field_names_are_never_mistaken_for_a_token,
        test_no_credential_yet_means_no_headers,
        test_observing_a_pass_with_a_token_produces_an_authorization_header,
        test_a_failed_or_skipped_call_never_seeds_a_credential,
        test_first_real_token_is_kept_not_replaced_by_a_later_one,
        test_set_cookie_is_captured_and_forwarded,
        test_token_and_cookie_can_both_be_attached_together,
        test_evidence_text_is_empty_until_something_is_captured,
        test_end_to_end_login_token_reaches_a_later_call,
    ])
    raise SystemExit(exit_code)
