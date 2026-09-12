"""API QA v1 (docs/30-api-qa-v1.md): `qa_agent.api_qa` - Next.js App Router
route discovery, real server lifecycle management, the real HTTP client,
orchestration (`run_api_qa`), reporting, and the `discover --api-test` CLI
flag.

HTTP-layer tests fake `urllib.request.urlopen` (the same technique
test_ai_openrouter.py already uses for OpenRouterProvider) - no real
network call is required for the bulk of this suite. A smaller set of real,
end-to-end tests spin up a plain Node `http` server (standing in for `next
dev` - this environment has no full Next.js install) via a real `npm run
dev`, guarded by `_npm_available()` and skipped cleanly when npm isn't on
PATH, exactly mirroring test_runtime_execution.py's own precedent.
"""

from __future__ import annotations

import json
import shutil
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import discovery as discovery_module  # noqa: E402
from qa_agent.api_qa import http_client as http_client_module  # noqa: E402
from qa_agent.api_qa import server as server_module  # noqa: E402
from qa_agent.api_qa import (  # noqa: E402
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    SERVER_CRASHED,
    SERVER_SKIPPED,
    SERVER_STARTED,
    ApiCallResult,
    ApiEndpoint,
    ApiQaConfig,
    ApiTestResult,
    call_endpoint,
    discover_api_endpoints,
    render,
    run_api_qa,
)


def _npm_available():
    return shutil.which("npm") is not None


def _context_for(files):
    """Write `files` into a fresh TempProject and build a real
    RepositoryContext from it - the same "real discovery, not a hand-built
    fixture" precedent test_runtime_execution.py already established.
    """
    proj = TempProject()
    for rel, text in files.items():
        proj.write(rel, text)
    result = discover_project(proj.path)
    context = build_repository_context(result.project)
    return context, proj


# --- models --------------------------------------------------------------

def test_api_endpoint_rejects_unknown_method(suite):
    try:
        ApiEndpoint(method="TRACE", path="/x", source_file="app/api/x/route.ts")
        suite.check("rejects an unrecognized method", False)
    except ValueError:
        suite.check("rejects an unrecognized method", True)


def test_api_endpoint_requires_source_file(suite):
    try:
        ApiEndpoint(method="GET", path="/x", source_file="")
        suite.check("rejects an empty source_file", False)
    except ValueError:
        suite.check("rejects an empty source_file", True)


def test_api_call_result_rejects_unknown_status(suite):
    endpoint = ApiEndpoint(method="GET", path="/x", source_file="app/api/x/route.ts")
    try:
        ApiCallResult(endpoint=endpoint, status="maybe")
        suite.check("rejects an unrecognized call status", False)
    except ValueError:
        suite.check("rejects an unrecognized call status", True)


def test_api_test_result_rejects_unknown_server_status(suite):
    try:
        ApiTestResult(root_path="/x", server_status="???")
        suite.check("rejects an unrecognized server_status", False)
    except ValueError:
        suite.check("rejects an unrecognized server_status", True)


# --- discovery -------------------------------------------------------------

def test_discover_finds_a_simple_get_route(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}, "dependencies": {"next": "15.0.0"}}),
        "app/api/health/route.ts": "export async function GET() { return Response.json({ok: true}); }\n",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("exactly one endpoint found", len(endpoints) == 1, " ({})".format(len(endpoints)))
        suite.check("method is GET", endpoints and endpoints[0].method == "GET")
        suite.check("path is /api/health", endpoints and endpoints[0].path == "/api/health")
        suite.check("source_file points at the real route file",
                     endpoints and endpoints[0].source_file == "app/api/health/route.ts")
        suite.check("not dynamic", endpoints and endpoints[0].dynamic is False)
        suite.check("no warnings", warnings == ())
    finally:
        proj.__exit__(None, None, None)


def test_discover_finds_multiple_methods_in_one_file(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "app/api/users/route.ts": (
            "export async function GET() { return Response.json([]); }\n"
            "export async function POST(req: Request) { return Response.json({}); }\n"
        ),
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        methods = sorted(e.method for e in endpoints)
        suite.check("both GET and POST discovered", methods == ["GET", "POST"], " ({})".format(methods))
        suite.check("both share the same path", {e.path for e in endpoints} == {"/api/users"})
    finally:
        proj.__exit__(None, None, None)


def test_discover_supports_export_const_arrow_handlers(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "app/api/ping/route.ts": "export const GET = async () => Response.json({pong: true});\n",
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("export const handler is discovered", len(endpoints) == 1)
        suite.check("method is GET", endpoints and endpoints[0].method == "GET")
    finally:
        proj.__exit__(None, None, None)


def test_discover_strips_route_group_segments(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "app/(marketing)/api/health/route.ts": "export async function GET() { return Response.json({}); }\n",
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("route-group segment never appears in the URL",
                     endpoints and endpoints[0].path == "/api/health", " ({})".format(
                         endpoints[0].path if endpoints else None))
    finally:
        proj.__exit__(None, None, None)


def test_discover_marks_dynamic_segment_routes(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "app/api/companions/[id]/route.ts": "export async function GET() { return Response.json({}); }\n",
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("dynamic segment path preserved literally",
                     endpoints and endpoints[0].path == "/api/companions/[id]")
        suite.check("marked dynamic", endpoints and endpoints[0].dynamic is True)
    finally:
        proj.__exit__(None, None, None)


def test_discover_warns_on_route_file_with_no_recognized_export(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "app/api/mystery/route.ts": "const helper = () => 1;\nexport default helper;\n",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("no endpoint fabricated for an unrecognized export", endpoints == ())
        suite.check("a clear warning is recorded instead",
                     any("no recognized exported HTTP handler" in w for w in warnings))
    finally:
        proj.__exit__(None, None, None)


def test_discover_ignores_node_modules_and_next_build_output(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "app/api/real/route.ts": "export async function GET() { return Response.json({}); }\n",
        "app/node_modules/some-pkg/route.ts": "export async function GET() { return Response.json({}); }\n",
        "app/.next/server/route.ts": "export async function GET() { return Response.json({}); }\n",
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("only the real route file is discovered", [e.source_file for e in endpoints] == ["app/api/real/route.ts"])
    finally:
        proj.__exit__(None, None, None)


def test_discover_returns_empty_for_a_non_nextjs_project(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}}),
        "server.js": "console.log('hi');\n",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("no endpoints when there is no app/ directory", endpoints == ())
        suite.check("no warnings either - there was nothing to look at", warnings == ())
    finally:
        proj.__exit__(None, None, None)


def test_discover_dedupes_identical_method_path_pairs(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "app/api/health/route.ts": "export async function GET() { return Response.json({}); }\n"
                                    "export async function GET() { return Response.json({}); }\n",
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("a duplicate export in the same file is not double-counted", len(endpoints) == 1)
    finally:
        proj.__exit__(None, None, None)


# --- http client -----------------------------------------------------------

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


def _endpoint(method="GET", path="/api/health"):
    return ApiEndpoint(method=method, path=path, source_file="app/api/health/route.ts")


def test_call_endpoint_pass_on_2xx_valid_json(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, b'{"ok": true}', {"Content-Type": "application/json"})

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint()))
    suite.check("status is pass", result.status == CALL_PASS)
    suite.check("status_code captured", result.status_code == 200)
    suite.check("content type captured", result.content_type == "application/json")
    suite.check("valid_response is True", result.valid_response is True)
    suite.check("response_time_ms measured", result.response_time_ms is not None and result.response_time_ms >= 0)


def test_call_endpoint_pass_on_2xx_non_json_content_type(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, b"hello world", {"Content-Type": "text/plain"})

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint()))
    suite.check("non-JSON 2xx still passes", result.status == CALL_PASS)
    suite.check("valid_response is None (not applicable)", result.valid_response is None)


def test_call_endpoint_fails_on_500_http_error(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "http://x/api/health", 500, "Internal Server Error",
            hdrs={"Content-Type": "application/json"},
            fp=_FakeResponse(500, b'{"error": "boom"}'),
        )

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint()))
    suite.check("status is fail", result.status == CALL_FAIL)
    suite.check("the real status code is captured, not hidden", result.status_code == 500)
    suite.check("the real error body is captured as evidence", "boom" in result.response_sample)


def test_call_endpoint_fails_on_2xx_with_invalid_json_body(suite):
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, b"{not valid json", {"Content-Type": "application/json"})

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint()))
    suite.check("a 2xx with a broken JSON body is still a fail", result.status == CALL_FAIL)
    suite.check("valid_response is False", result.valid_response is False)
    suite.check("status_code is still honestly reported", result.status_code == 200)


def test_call_endpoint_connection_refused(suite):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("nobody listening"))

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint()))
    suite.check("status is fail", result.status == CALL_FAIL)
    suite.check("status_code stays None - no response was ever received", result.status_code is None)
    suite.check("a clear error is given", "could not reach" in result.error)


def test_call_endpoint_timeout(suite):
    def fake_urlopen(request, timeout=None):
        raise TimeoutError("timed out")

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint(), timeout=3))
    suite.check("status is fail", result.status == CALL_FAIL)
    suite.check("status_code stays None", result.status_code is None)
    suite.check("the configured timeout is named", "3" in result.error)


def test_call_endpoint_response_sample_is_bounded(suite):
    huge = b'{"data": "' + (b"x" * 10_000) + b'"}'

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, huge, {"Content-Type": "application/json"})

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint()))
    suite.check(
        "response_sample never exceeds MAX_RESPONSE_SAMPLE_CHARS",
        len(result.response_sample) <= http_client_module.MAX_RESPONSE_SAMPLE_CHARS,
    )


def test_call_endpoint_never_reads_past_the_read_cap(suite):
    huge = b"x" * (http_client_module.MAX_RESPONSE_READ_BYTES * 5)
    captured = {}

    class _CappingResponse(_FakeResponse):
        def read(self, n=-1):
            captured["n"] = n
            return super().read(n)

    def fake_urlopen(request, timeout=None):
        return _CappingResponse(200, huge, {"Content-Type": "text/plain"})

    _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint()))
    suite.check("read() was called with the bounded cap, not unbounded",
                captured.get("n") == http_client_module.MAX_RESPONSE_READ_BYTES)


def test_call_endpoint_truncated_body_skips_json_validity_claim(suite):
    huge = b"{" + (b'"a":1,' * 20_000)  # well past MAX_RESPONSE_READ_BYTES, never valid JSON on its own

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(200, huge, {"Content-Type": "application/json"})

    result = _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint()))
    suite.check(
        "a truncated body is never claimed valid or invalid",
        result.valid_response is None,
    )


def test_call_endpoint_uses_the_endpoint_method(suite):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["method"] = request.get_method()
        return _FakeResponse(200, b"{}", {"Content-Type": "application/json"})

    _with_fake_urlopen(fake_urlopen, lambda: call_endpoint("http://x", _endpoint(method="POST"), timeout=5))
    suite.check("POST is really sent, not always GET", captured["method"] == "POST")


# --- server lifecycle --------------------------------------------------

def test_discover_server_start_command_prefers_dev_over_start(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev", "start": "next start"}}),
        "package-lock.json": "{}",
    })
    try:
        command, evidence = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("dev script chosen over start", command is not None and "dev" in command)
        suite.check("evidence names the real script", "scripts.dev" in evidence)
    finally:
        proj.__exit__(None, None, None)


def test_discover_server_start_command_none_when_no_script(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"build": "next build"}}),
        "package-lock.json": "{}",
    })
    try:
        command, reason = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("no command found", command is None)
        suite.check("a clear reason is given", "no npm dev/start script" in reason)
    finally:
        proj.__exit__(None, None, None)


def test_start_and_wait_ready_reports_not_found_for_a_missing_binary(suite):
    handle = server_module.start_and_wait_ready(
        ["definitely-not-a-real-binary-xyz"], cwd=".", timeout=1,
    )
    suite.check("status is not_found", handle.status == server_module.STATUS_NOT_FOUND)
    suite.check("proc is None", handle.proc is None)
    handle.stop()  # must be safe even when nothing was ever started
    suite.check("stop() on a never-started handle does not raise", True)


def test_start_and_wait_ready_detects_a_ready_signal(suite):
    proj = TempProject()
    try:
        handle = server_module.start_and_wait_ready(
            [sys.executable, "-c",
             "import sys; print('Server ready on http://localhost:4321'); sys.stdout.flush(); "
             "import time; time.sleep(30)"],
            cwd=proj.path, timeout=10,
        )
        try:
            suite.check("status is ready", handle.status == server_module.STATUS_READY)
            suite.check("real process handle returned", handle.proc is not None and handle.proc.poll() is None)
            suite.check("the observed base URL is parsed from real output",
                         handle.base_url == "http://localhost:4321")
        finally:
            handle.stop()
        suite.check("process actually stopped", handle.proc.poll() is not None)
    finally:
        proj.__exit__(None, None, None)


def test_start_and_wait_ready_detects_a_crash(suite):
    proj = TempProject()
    try:
        handle = server_module.start_and_wait_ready(
            [sys.executable, "-c", "import sys; sys.exit(7)"], cwd=proj.path, timeout=10,
        )
        suite.check("status is crashed", handle.status == server_module.STATUS_CRASHED)
        suite.check("exit code named in the reason", "7" in handle.reason)
        handle.stop()
    finally:
        proj.__exit__(None, None, None)


# --- orchestration (run_api_qa) -----------------------------------------

def test_run_api_qa_skips_when_no_endpoints_discovered(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
    })
    try:
        result = run_api_qa(context, proj.path)
        suite.check("server_status is skipped", result.server_status == SERVER_SKIPPED)
        suite.check("no endpoints, no calls", result.endpoints == () and result.calls == ())
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_skips_when_no_start_command(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"build": "next build"}}),
        "app/api/health/route.ts": "export async function GET() { return Response.json({}); }\n",
    })
    try:
        result = run_api_qa(context, proj.path)
        suite.check("server_status is skipped", result.server_status == SERVER_SKIPPED)
        suite.check("endpoint still reported, honestly, even though it was never called", len(result.endpoints) == 1)
        suite.check("its call is recorded as skipped, with a reason", result.calls[0].status == CALL_SKIPPED and result.calls[0].reason)
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_reports_a_crashed_server(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "python -c \"import sys; sys.exit(3)\""}}),
        "package-lock.json": "{}",
        "app/api/health/route.ts": "export async function GET() { return Response.json({}); }\n",
    })
    try:
        config = ApiQaConfig(server_startup_timeout=10)
        result = run_api_qa(context, proj.path, config=config)
        suite.check("server_status is crashed", result.server_status == SERVER_CRASHED, " (was {})".format(result.server_status))
        suite.check("every endpoint's call is skipped, none fabricated", all(c.status == CALL_SKIPPED for c in result.calls))
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_dynamic_routes_are_never_called(suite):
    context, proj = _context_for({
        "package.json": json.dumps({
            "name": "x",
            "scripts": {"dev": "python -c \"import time; time.sleep(30)\""},
        }),
        "package-lock.json": "{}",
        "app/api/companions/[id]/route.ts": "export async function GET() { return Response.json({}); }\n",
    })
    try:
        result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=5))
        suite.check("dynamic endpoint present", len(result.endpoints) == 1 and result.endpoints[0].dynamic)
        suite.check("server reached a startable state so the skip is really about the target",
                     result.server_status == SERVER_STARTED, " (was {})".format(result.server_status))
        suite.check("its call reason explains why, without inventing a value",
                     "path parameter" in result.calls[0].reason)
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_end_to_end_real_server(suite):
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "node server.js"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("package-lock.json", "{}")
        # A plain Node http server standing in for `next dev` - this test
        # environment has no full Next.js install. It deliberately answers
        # exactly the two routes app/api discovery below will find, so the
        # real HTTP calls this test proves happened have somewhere real to
        # land, both a pass and a real, honest fail.
        proj.write("server.js", """
const http = require('http');
const server = http.createServer((req, res) => {
  if (req.url === '/api/health') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({ok: true}));
  } else if (req.url === '/api/broken') {
    res.writeHead(500, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({error: 'boom'}));
  } else {
    res.writeHead(404);
    res.end();
  }
});
server.listen(4123, () => console.log('ready - Local:        http://localhost:4123'));
""")
        proj.write("app/api/health/route.ts", "export async function GET() { return Response.json({ok: true}); }\n")
        proj.write("app/api/broken/route.ts", "export async function GET() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=20))

        suite.check("server really started", api_result.server_status == SERVER_STARTED,
                     " (was {}: {})".format(api_result.server_status, api_result.server_detail))
        suite.check("real port observed from real startup output", "4123" in api_result.base_url)
        by_path = {c.endpoint.path: c for c in api_result.calls}
        suite.check("a real passing endpoint is reported as pass",
                     by_path.get("/api/health") is not None and by_path["/api/health"].status == CALL_PASS)
        suite.check("a real failing endpoint is reported as fail, with its real status code",
                     by_path.get("/api/broken") is not None and by_path["/api/broken"].status == CALL_FAIL
                     and by_path["/api/broken"].status_code == 500)
    finally:
        proj.__exit__(None, None, None)


# --- reporting ----------------------------------------------------------

def test_render_shows_endpoint_method_path_status_and_timing(suite):
    endpoint = _endpoint()
    call = ApiCallResult(endpoint=endpoint, status=CALL_PASS, status_code=200, response_time_ms=42.0, reason="HTTP 200")
    result = ApiTestResult(
        root_path="/x", endpoints=(endpoint,), calls=(call,),
        server_status=SERVER_STARTED, base_url="http://localhost:3000",
    )
    text = render(result)
    suite.check("shows the method and path", "GET /api/health" in text)
    suite.check("shows the status code", "200" in text)
    suite.check("shows the timing", "42ms" in text)
    suite.check("shows PASS", "PASS" in text)


def test_render_shows_evidence_on_failure(suite):
    endpoint = _endpoint(path="/api/broken")
    call = ApiCallResult(
        endpoint=endpoint, status=CALL_FAIL, status_code=500,
        response_time_ms=10.0, reason="HTTP 500 response", response_sample='{"error": "boom"}',
    )
    result = ApiTestResult(root_path="/x", endpoints=(endpoint,), calls=(call,), server_status=SERVER_STARTED)
    text = render(result)
    suite.check("shows FAIL", "FAIL" in text)
    suite.check("shows the real evidence (reason)", "HTTP 500 response" in text)
    suite.check("shows a body sample", "boom" in text)


def test_render_handles_no_calls_gracefully(suite):
    result = ApiTestResult(root_path="/x", server_status=SERVER_SKIPPED, server_detail="no endpoints")
    text = render(result)
    suite.check("does not crash and says nothing was called", "No endpoint calls were made" in text)


def test_render_shows_warnings(suite):
    result = ApiTestResult(root_path="/x", server_status=SERVER_SKIPPED, warnings=("a real warning",))
    text = render(result)
    suite.check("warnings are surfaced", "a real warning" in text)


# --- CLI wiring ----------------------------------------------------------

def test_cli_api_test_flag_reports_when_no_app_router_present(suite):
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({"name": "x", "scripts": {"start": "node server.js"}}))
        proj.write("server.js", "console.log('hi');\n")
        completed = run_agent(["discover", str(proj.path), "--api-test"])
        suite.check("exits cleanly", completed.returncode == 0, " (rc={})".format(completed.returncode))
        suite.check("prints the API QA section", "API QA Results" in completed.stdout)
        suite.check("honestly reports nothing was discovered", "skipped" in completed.stdout)
    finally:
        proj.__exit__(None, None, None)


def test_cli_without_api_test_flag_never_runs_api_qa(suite):
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({"name": "x", "scripts": {"dev": "next dev"}}))
        proj.write("app/api/health/route.ts", "export async function GET() { return Response.json({}); }\n")
        completed = run_agent(["discover", str(proj.path)])
        suite.check("exits cleanly", completed.returncode == 0)
        suite.check("no API QA section printed without the flag", "API QA Results" not in completed.stdout)
    finally:
        proj.__exit__(None, None, None)


def test_cli_api_test_end_to_end(suite):
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
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify({ok: true}));
}).listen(4124, () => console.log('ready - Local:        http://localhost:4124'));
""")
        proj.write("app/api/health/route.ts", "export async function GET() { return Response.json({}); }\n")
        completed = run_agent(["discover", str(proj.path), "--api-test"])
        suite.check("exits cleanly", completed.returncode == 0, " (rc={}, stderr={})".format(
            completed.returncode, completed.stderr[-500:]))
        suite.check("real pass reported in CLI output", "GET /api/health" in completed.stdout and "PASS" in completed.stdout)
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA v1")
    sys.exit(suite.run([
        test_api_endpoint_rejects_unknown_method,
        test_api_endpoint_requires_source_file,
        test_api_call_result_rejects_unknown_status,
        test_api_test_result_rejects_unknown_server_status,
        test_discover_finds_a_simple_get_route,
        test_discover_finds_multiple_methods_in_one_file,
        test_discover_supports_export_const_arrow_handlers,
        test_discover_strips_route_group_segments,
        test_discover_marks_dynamic_segment_routes,
        test_discover_warns_on_route_file_with_no_recognized_export,
        test_discover_ignores_node_modules_and_next_build_output,
        test_discover_returns_empty_for_a_non_nextjs_project,
        test_discover_dedupes_identical_method_path_pairs,
        test_call_endpoint_pass_on_2xx_valid_json,
        test_call_endpoint_pass_on_2xx_non_json_content_type,
        test_call_endpoint_fails_on_500_http_error,
        test_call_endpoint_fails_on_2xx_with_invalid_json_body,
        test_call_endpoint_connection_refused,
        test_call_endpoint_timeout,
        test_call_endpoint_response_sample_is_bounded,
        test_call_endpoint_never_reads_past_the_read_cap,
        test_call_endpoint_truncated_body_skips_json_validity_claim,
        test_call_endpoint_uses_the_endpoint_method,
        test_discover_server_start_command_prefers_dev_over_start,
        test_discover_server_start_command_none_when_no_script,
        test_start_and_wait_ready_reports_not_found_for_a_missing_binary,
        test_start_and_wait_ready_detects_a_ready_signal,
        test_start_and_wait_ready_detects_a_crash,
        test_run_api_qa_skips_when_no_endpoints_discovered,
        test_run_api_qa_skips_when_no_start_command,
        test_run_api_qa_reports_a_crashed_server,
        test_run_api_qa_dynamic_routes_are_never_called,
        test_run_api_qa_end_to_end_real_server,
        test_render_shows_endpoint_method_path_status_and_timing,
        test_render_shows_evidence_on_failure,
        test_render_handles_no_calls_gracefully,
        test_render_shows_warnings,
        test_cli_api_test_flag_reports_when_no_app_router_present,
        test_cli_without_api_test_flag_never_runs_api_qa,
        test_cli_api_test_end_to_end,
    ]))
