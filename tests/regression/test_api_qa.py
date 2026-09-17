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
from qa_agent.api_qa import runner as runner_module  # noqa: E402
from qa_agent.api_qa import (  # noqa: E402
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    SERVER_CRASHED,
    SERVER_SKIPPED,
    SERVER_STARTED,
    SERVER_UNREACHABLE,
    ApiCallResult,
    ApiEndpoint,
    ApiQaConfig,
    ApiTestResult,
    call_endpoint,
    discover_api_endpoints,
    render,
    run_api_qa,
    to_csv,
    to_html,
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


# --- discovery: Next.js Pages Router (docs/38) ------------------------------

def test_discover_pages_router_finds_method_via_req_method_check(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}, "dependencies": {"next": "14.0.0"}}),
        "pages/api/health.ts": (
            "export default function handler(req, res) {\n"
            "  if (req.method === 'GET') { res.status(200).json({ok: true}); return; }\n"
            "  res.status(405).end();\n"
            "}\n"
        ),
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("exactly one endpoint found", len(endpoints) == 1, " ({})".format(len(endpoints)))
        suite.check("method is GET, from the real req.method check", endpoints and endpoints[0].method == "GET")
        suite.check("path is /api/health", endpoints and endpoints[0].path == "/api/health")
        suite.check("no 'assumed GET' warning - a real check was found", not any("assuming" in w for w in warnings))
    finally:
        proj.__exit__(None, None, None)


def test_discover_pages_router_index_file_maps_to_parent_path(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}, "dependencies": {"next": "14.0.0"}}),
        "pages/api/companions/index.ts": (
            "export default function handler(req, res) { res.status(200).json([]); }\n"
        ),
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("index.ts maps to its parent directory's own path",
                     endpoints and endpoints[0].path == "/api/companions", " ({})".format(
                         endpoints[0].path if endpoints else None))
    finally:
        proj.__exit__(None, None, None)


def test_discover_pages_router_dynamic_segment(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}, "dependencies": {"next": "14.0.0"}}),
        "pages/api/companions/[id].ts": (
            "export default function handler(req, res) { res.status(200).json({}); }\n"
        ),
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("dynamic segment preserved literally",
                     endpoints and endpoints[0].path == "/api/companions/[id]")
        suite.check("marked dynamic", endpoints and endpoints[0].dynamic is True)
    finally:
        proj.__exit__(None, None, None)


def test_discover_pages_router_no_method_check_assumes_get_and_warns(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}, "dependencies": {"next": "14.0.0"}}),
        "pages/api/ping.ts": "export default function handler(req, res) { res.status(200).json({pong: true}); }\n",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("assumes GET when no method check exists",
                     endpoints and endpoints[0].method == "GET")
        suite.check("the assumption is explicitly flagged, not silent",
                     any("assuming" in w and "pages/api/ping.ts" in w for w in warnings), " ({})".format(warnings))
    finally:
        proj.__exit__(None, None, None)


def test_discover_pages_router_no_default_export_warns_and_skips(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}, "dependencies": {"next": "14.0.0"}}),
        "pages/api/helper.ts": "export function notAHandler() { return 1; }\n",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("no endpoint fabricated with no default export", endpoints == ())
        suite.check("a clear warning is recorded instead", any("no 'export default'" in w for w in warnings))
    finally:
        proj.__exit__(None, None, None)


def test_discover_pages_router_gated_on_real_nextjs_framework_fact(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}}),
        "pages/api/health.ts": "export default function handler(req, res) { res.status(200).json({}); }\n",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("nothing discovered without a real Next.js framework fact", endpoints == ())
        suite.check("no warnings either - the strategy never even looked", warnings == ())
    finally:
        proj.__exit__(None, None, None)


# --- discovery: Express (docs/38) -------------------------------------------

def test_discover_express_finds_a_simple_route(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "server.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "app.get('/api/health', (req, res) => res.json({ok: true}));\n"
        ),
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("exactly one endpoint found", len(endpoints) == 1, " ({})".format(len(endpoints)))
        suite.check("method is GET", endpoints and endpoints[0].method == "GET")
        suite.check("path is /api/health", endpoints and endpoints[0].path == "/api/health")
        suite.check("source_file points at the real file", endpoints and endpoints[0].source_file == "server.js")
    finally:
        proj.__exit__(None, None, None)


def test_discover_express_finds_router_calls_and_dynamic_segments(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "routes/users.js": (
            "const router = require('express').Router();\n"
            "router.get('/api/users/:id', (req, res) => res.json({}));\n"
            "router.post(\"/api/users\", (req, res) => res.json({}));\n"
        ),
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        by_path = {(e.method, e.path): e for e in endpoints}
        suite.check("GET with a dynamic :id segment discovered",
                     ("GET", "/api/users/:id") in by_path and by_path[("GET", "/api/users/:id")].dynamic is True)
        suite.check("POST discovered too, not dynamic",
                     ("POST", "/api/users") in by_path and by_path[("POST", "/api/users")].dynamic is False)
    finally:
        proj.__exit__(None, None, None)


def test_discover_express_gated_on_real_express_framework_fact(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}}),
        "server.js": "app.get('/api/health', (req, res) => res.json({}));\n",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("nothing discovered without a real Express framework fact", endpoints == ())
        suite.check("no warnings either - the strategy never even looked", warnings == ())
    finally:
        proj.__exit__(None, None, None)


def test_discover_express_ignores_non_literal_first_argument(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "server.js": (
            "app.get(someMiddleware, (req, res) => res.json({}));\n"
            "app.get('/api/real', (req, res) => res.json({}));\n"
        ),
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("only the real literal-path call is discovered",
                     [e.path for e in endpoints] == ["/api/real"], " ({})".format([e.path for e in endpoints]))
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
        command, evidence, cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("dev script chosen over start", command is not None and "dev" in command)
        suite.check("evidence names the real script", "scripts.dev" in evidence)
        suite.check("cwd is the real project root", str(cwd) == str(proj.path))
    finally:
        proj.__exit__(None, None, None)


def test_discover_server_start_command_none_when_no_script(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"build": "next build"}}),
        "package-lock.json": "{}",
    })
    try:
        command, reason, cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("no command found", command is None)
        suite.check("a clear reason is given", "no npm dev/start script" in reason)
        suite.check("no cwd for a command that was never found", cwd is None)
    finally:
        proj.__exit__(None, None, None)


# --- server lifecycle: monorepo/workspace discovery (docs/40) -----------

def test_discover_server_start_command_falls_back_to_a_monorepo_package(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "root", "private": True}),  # no dev/start script here
        "package-lock.json": "{}",
        "client/package.json": json.dumps({
            "name": "client", "scripts": {"dev": "vite"}, "dependencies": {"react": "18.0.0"},
        }),
        "server/package.json": json.dumps({
            "name": "server", "scripts": {"dev": "node index.js"}, "dependencies": {"express": "4.19.0"},
        }),
    })
    try:
        command, evidence, cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("a command was found", command is not None, " (evidence/reason: {})".format(evidence))
        suite.check("the backend package (server) is chosen over the frontend-only one (client)",
                     "server" in evidence, " (evidence: {})".format(evidence))
        suite.check("cwd is the real package directory, not the workspace root",
                     cwd is not None and str(cwd).replace("\\", "/").endswith("/server"), " (cwd: {})".format(cwd))
    finally:
        proj.__exit__(None, None, None)


def test_discover_server_start_command_ambiguous_monorepo_is_never_guessed(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "root", "private": True}),
        "package-lock.json": "{}",
        "api/package.json": json.dumps({
            "name": "api", "scripts": {"dev": "node index.js"}, "dependencies": {"express": "4.19.0"},
        }),
        "worker/package.json": json.dumps({
            "name": "worker", "scripts": {"dev": "node worker.js"}, "dependencies": {"bullmq": "5.0.0"},
        }),
    })
    try:
        command, reason, cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("never guesses between two equally-real backend-looking candidates", command is None)
        suite.check("the real candidates are named in the reason, not hidden",
                     "api" in reason and "worker" in reason, " (reason: {})".format(reason))
        suite.check("no cwd for a command that was never found", cwd is None)
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


def test_start_and_wait_ready_does_not_stop_on_a_keyword_only_line(suite):
    """docs/46-ready-signal-false-positive-fix.md: the exact bug found
    dogfooding a real project through the web frontend - a wrapper tool
    (nodemon, ts-node-dev, ...) prints its own ready-shaped line (matching
    one of the generic keyword patterns, but with no real URL/port in it)
    well before the real child process it spawns has actually bound
    anything. Reproduced directly: a first line matches a keyword with no
    URL, then - after a short, real delay - a second line carries the real
    URL. Before this fix, the loop broke on the first line and the real
    port was never observed at all (falling back to a wrong guess); after
    it, the loop keeps watching and correctly captures the real one.
    """
    proj = TempProject()
    try:
        handle = server_module.start_and_wait_ready(
            [sys.executable, "-c",
             "import sys, time; print('Compiled successfully'); sys.stdout.flush(); "
             "time.sleep(0.5); print('Server ready on http://localhost:4559'); sys.stdout.flush(); "
             "time.sleep(30)"],
            cwd=proj.path, timeout=10,
        )
        try:
            suite.check("status is ready", handle.status == server_module.STATUS_READY)
            suite.check(
                "the real URL from the SECOND line is observed, not left empty by an early exit",
                handle.base_url == "http://localhost:4559", " (got: {!r})".format(handle.base_url),
            )
        finally:
            handle.stop()
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
    """The real server must actually become reachable (docs/48's fail-fast
    now stops the whole run otherwise) - only once that's confirmed does
    this prove the real point: a dynamic route with no real evidence to
    resolve it from is honestly skipped, never guessed.
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
http.createServer((req, res) => { res.writeHead(404); res.end(); })
  .listen(4561, () => console.log('ready - Local:        http://localhost:4561'));
""")
        proj.write("app/api/companions/[id]/route.ts", "export async function GET() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=10))
        suite.check("dynamic endpoint present", len(api_result.endpoints) == 1 and api_result.endpoints[0].dynamic)
        suite.check("server reached a startable state so the skip is really about the target",
                     api_result.server_status == SERVER_STARTED, " (was {})".format(api_result.server_status))
        suite.check("its call reason explains why, without inventing a value",
                     "path parameter" in api_result.calls[0].reason)
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
        suite.check(
            "the HTTP readiness gate (Phase 1) really ran and preferred the real, "
            "already-discovered /api/health endpoint over a guessed candidate",
            "/api/health" in api_result.http_readiness_detail,
            " (was: {!r})".format(api_result.http_readiness_detail),
        )
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_captures_server_log_tail_during_the_run(suite):
    """docs/37-api-qa-server-log-capture.md: `server_log_tail` is real
    output the dev server printed to its own console *after* it became
    ready - not what `http_client.py` alone can see over HTTP (the "why" a
    real application-level 500 happened, e.g. a stack trace, when the HTTP
    response body itself is empty or unhelpful).
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
        # Prints a distinctive, stack-trace-shaped line to its own stdout
        # when the failing route is actually hit - standing in for what a
        # real `next dev` prints on an unhandled route exception, and never
        # put in the HTTP response body itself (which stays empty), so the
        # only way to see it is the server's own real console output.
        proj.write("server.js", """
const http = require('http');
const server = http.createServer((req, res) => {
  if (req.url === '/api/broken') {
    console.log('SENTINEL_STACK_TRACE_MARKER: something exploded');
    res.writeHead(500);
    res.end();
  } else {
    res.writeHead(404);
    res.end();
  }
});
server.listen(4126, () => console.log('ready - Local:        http://localhost:4126'));
""")
        proj.write("app/api/broken/route.ts", "export async function GET() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=20))

        suite.check("server really started", api_result.server_status == SERVER_STARTED,
                     " (was {}: {})".format(api_result.server_status, api_result.server_detail))
        suite.check(
            "the server's own real console output printed during the call is captured",
            "SENTINEL_STACK_TRACE_MARKER" in api_result.server_log_tail,
            " (got: {!r})".format(api_result.server_log_tail),
        )
        text = render(api_result)
        suite.check("the terminal report surfaces it too", "SENTINEL_STACK_TRACE_MARKER" in text)
    finally:
        proj.__exit__(None, None, None)


# --- connect probe (docs/39-connect-probe.md) -------------------------------

def test_wait_until_connectable_true_once_a_real_listener_binds(suite):
    """A real, minimal race: nothing is listening yet, then something
    really does bind the port shortly after - `wait_until_connectable`
    must notice it within its own timeout, not just at the very first poll.
    """
    import socket
    import threading
    import time

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # port is free again, nothing listening yet

    def bind_after_delay():
        time.sleep(0.3)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        time.sleep(2)
        listener.close()

    t = threading.Thread(target=bind_after_delay, daemon=True)
    t.start()
    try:
        start = time.perf_counter()
        ok = server_module.wait_until_connectable(
            "http://127.0.0.1:{}".format(port), timeout=5.0, poll_interval=0.05,
        )
        elapsed = time.perf_counter() - start
        suite.check("reports connectable once the real bind happens", ok is True)
        suite.check("returns promptly after the real bind, not only at the timeout", elapsed < 2.0,
                     " ({:.2f}s)".format(elapsed))
    finally:
        t.join(timeout=5)


def test_wait_until_connectable_false_when_nothing_ever_binds(suite):
    import socket
    import time

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # confirmed free; never bound again by anything

    start = time.perf_counter()
    ok = server_module.wait_until_connectable(
        "http://127.0.0.1:{}".format(port), timeout=0.5, poll_interval=0.05,
    )
    elapsed = time.perf_counter() - start
    suite.check("honestly reports not connectable", ok is False)
    suite.check("never blocks past its own timeout", elapsed < 2.0, " ({:.2f}s)".format(elapsed))


def test_run_api_qa_waits_out_a_delayed_port_bind_before_calling(suite):
    """The real bug this closes (found dogfooding a real Express project
    through the web frontend): a wrapper process (nodemon in that case)
    prints a real 'ready'-shaped log line before the real child process it
    spawns has actually finished binding the port - the very first real
    call then got a real, honest connection-refused. Reproduced directly:
    the server prints its ready line *immediately*, but only actually binds
    the port after a short, real delay.
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
console.log('ready - Local:        http://localhost:4557');
setTimeout(() => {
  const http = require('http');
  http.createServer((req, res) => {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({ok: true}));
  }).listen(4557);
}, 1200);
""")
        proj.write("app/api/health/route.ts", "export async function GET() { return Response.json({ok:true}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(
            context, proj.path,
            config=ApiQaConfig(server_startup_timeout=10, connect_probe_timeout=5),
        )
        suite.check("server really started", api_result.server_status == SERVER_STARTED)
        suite.check(
            "the real call succeeds instead of racing a connection-refused",
            len(api_result.calls) == 1 and api_result.calls[0].status == CALL_PASS,
            " (got: {})".format([(c.status, c.error or c.reason) for c in api_result.calls]),
        )
        suite.check("no false 'never connected' warning, since it really did connect in time",
                     not any("never accepted a real TCP connection" in w for w in api_result.warnings))
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_observes_the_real_port_past_a_wrapper_false_ready_line(suite):
    """docs/46: the full, real, end-to-end proof of the same fix
    `test_start_and_wait_ready_does_not_stop_on_a_keyword_only_line`
    already proves at the lower level - here through the real public
    entry point, confirming calls actually reach the real, later-observed
    port rather than a wrong guessed one.
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
        # A fake wrapper-style line first (keyword match, no URL/port at
        # all), then a real delay, then the real app's own URL-bearing
        # ready line, only after which the real listener actually binds.
        proj.write("server.js", """
console.log('watching for file changes');
setTimeout(() => {
  console.log('ready - Local:        http://localhost:4560');
  const http = require('http');
  http.createServer((req, res) => {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({ok: true}));
  }).listen(4560);
}, 500);
""")
        proj.write("app/api/health/route.ts", "export async function GET() { return Response.json({ok:true}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(
            context, proj.path,
            config=ApiQaConfig(server_startup_timeout=10, connect_probe_timeout=5),
        )
        suite.check("server really started", api_result.server_status == SERVER_STARTED)
        suite.check("the real, later-observed port was used - never a wrong guess",
                     api_result.base_url == "http://localhost:4560", " (got: {!r})".format(api_result.base_url))
        suite.check(
            "the real call succeeds against the real port, no connection-refused wall",
            len(api_result.calls) == 1 and api_result.calls[0].status == CALL_PASS,
            " (got: {})".format([(c.status, c.error or c.reason) for c in api_result.calls]),
        )
        suite.check("no 'assuming the common default' warning - a real port really was observed",
                     not any("assuming the common default" in w for w in api_result.warnings))
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_stops_fast_when_the_port_never_binds(suite):
    """docs/48-fail-fast-and-classification.md: replaces the old "warn,
    then proceed anyway" behavior. When the real connect probe never
    succeeds, the whole run stops immediately, honestly, with a single
    clear reason - never a wall of per-endpoint connection-refused
    failures that read exactly like a broken API when the real problem is
    upstream, in the environment itself.
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
        # Prints a real ready-shaped line but never actually binds anything.
        proj.write("server.js", "console.log('ready - Local:        http://localhost:4558');\nsetInterval(() => {}, 1000);\n")
        proj.write("app/api/health/route.ts", "export async function GET() { return Response.json({ok:true}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(
            context, proj.path,
            config=ApiQaConfig(server_startup_timeout=5, connect_probe_timeout=1),
        )
        suite.check("server_status honestly reports unreachable, not started",
                     api_result.server_status == "unreachable", " (was {})".format(api_result.server_status))
        suite.check(
            "the exact, clear stop message is shown",
            api_result.server_detail.startswith("Server is not reachable. Stopping the run."),
            " (was: {!r})".format(api_result.server_detail),
        )
        suite.check(
            "the run stopped immediately - no real HTTP call was ever attempted",
            len(api_result.calls) == 1 and api_result.calls[0].status == CALL_SKIPPED,
            " (got: {})".format([(c.status, c.reason) for c in api_result.calls]),
        )
        suite.check("the skip reason on the call itself names the same real gap",
                     "not reachable" in api_result.calls[0].reason)
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_blocks_on_timeout_when_no_ready_signal_and_nothing_binds(suite):
    """Case F (Phase 1): the process never prints anything ready-shaped at
    all, and never binds a port either - a real, distinct scenario from
    `test_run_api_qa_stops_fast_when_the_port_never_binds` (which does
    match a ready-shaped line), exercising the other half of runner.py's
    own signal_desc wording. The run must report one honest environment
    failure once the configured timeout elapses - never a wall of
    per-endpoint failures.
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
        # Stays alive, prints nothing recognizable, never binds anything.
        proj.write("server.js", "setInterval(() => {}, 1000);\n")
        proj.write("app/api/a/route.ts", "export async function GET() { return Response.json({}); }\n")
        proj.write("app/api/b/route.ts", "export async function GET() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(
            context, proj.path,
            config=ApiQaConfig(server_startup_timeout=2, connect_probe_timeout=1),
        )
        suite.check("server_status is unreachable, not started",
                     api_result.server_status == SERVER_UNREACHABLE, " (was {})".format(api_result.server_status))
        suite.check("the reason correctly says no ready signal was ever recognized",
                     "without a recognized ready signal" in api_result.server_detail,
                     " (was: {!r})".format(api_result.server_detail))
        suite.check("every endpoint is honestly skipped, never a wall of fabricated failures",
                     len(api_result.calls) == 2 and all(c.status == CALL_SKIPPED for c in api_result.calls),
                     " (got: {})".format([(c.status, c.reason) for c in api_result.calls]))
    finally:
        proj.__exit__(None, None, None)


# --- HTTP readiness check (Phase 1: environment readiness gate) ---------

def test_check_http_readiness_reached_on_a_real_200(suite):
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        result = server_module.check_http_readiness("http://127.0.0.1:{}".format(port), timeout=2)
        suite.check("reached is True on a real 200", result.reached is True)
        suite.check("status code captured", result.status_code == 200)
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def test_check_http_readiness_reached_on_a_real_404_not_confused_with_unreachable(suite):
    """Case E: a real application-level error response still proves the
    server is genuinely answering - must never be treated the same as a
    connection failure.
    """
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        result = server_module.check_http_readiness("http://127.0.0.1:{}".format(port), timeout=2)
        suite.check("reached is True even though every path 404s", result.reached is True,
                     " (result: {})".format(result))
        suite.check("status code is the real 404, not hidden", result.status_code == 404)
        suite.check("detail clearly says this is an application response, not a connection failure",
                     "not a connection failure" in result.detail, " (detail: {!r})".format(result.detail))
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def test_check_http_readiness_not_reached_when_nothing_is_listening(suite):
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # confirmed free, nothing listening
    result = server_module.check_http_readiness("http://127.0.0.1:{}".format(port), timeout=1)
    suite.check("reached is False when nothing is listening at all", result.reached is False)
    suite.check("status_code is None, never fabricated", result.status_code is None)


def test_check_http_readiness_prefers_a_real_health_shaped_endpoint(suite):
    import http.server
    import threading

    seen_paths = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen_paths.append(self.path)
            self.send_response(200 if self.path == "/api/keep-alive" else 500)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        endpoint = ApiEndpoint(method="GET", path="/api/keep-alive", source_file="app/api/keep-alive/route.ts")
        result = server_module.check_http_readiness(
            "http://127.0.0.1:{}".format(port), endpoints=[endpoint], timeout=2,
        )
        suite.check("the real, already-discovered health-shaped endpoint is tried first",
                     result.path == "/api/keep-alive", " (result: {})".format(result))
        suite.check("that real endpoint's own path is what was actually requested first",
                     seen_paths[:1] == ["/api/keep-alive"], " (seen: {})".format(seen_paths))
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def test_run_api_qa_blocks_when_tcp_connects_but_nothing_ever_speaks_http(suite):
    """The genuinely new Phase 1 gate, proven through the full public entry
    point: something really does bind the port and accept real TCP
    connections (the pre-Phase-1 TCP-only gate would have let this
    straight through to API execution), but it never speaks HTTP at all -
    a real, distinct failure only the new HTTP readiness check can catch.
    """
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "{} raw_socket_server.py".format(sys.executable)},
        }))
        proj.write("package-lock.json", "{}")
        # A raw TCP sink: binds a real port, accepts real connections, and
        # closes each one immediately without ever sending an HTTP response.
        proj.write("raw_socket_server.py", """
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.bind(('127.0.0.1', 0))
port = s.getsockname()[1]
s.listen(5)
print('Server ready on http://localhost:{}'.format(port))
sys.stdout.flush()
while True:
    conn, _ = s.accept()
    conn.close()
""")
        proj.write("app/api/x/route.ts", "export async function GET() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(
            context, proj.path,
            config=ApiQaConfig(server_startup_timeout=10, connect_probe_timeout=5, http_readiness_timeout=3),
        )
        suite.check("server_status is unreachable - TCP connecting alone was not enough",
                     api_result.server_status == SERVER_UNREACHABLE, " (was {})".format(api_result.server_status))
        suite.check(
            "zero real API calls were attempted",
            len(api_result.calls) == 1 and api_result.calls[0].status == CALL_SKIPPED,
            " (got: {})".format([(c.status, c.reason) for c in api_result.calls]),
        )
        suite.check(
            "the reason distinguishes TCP success from the HTTP-level failure, never conflating them",
            "TCP connected" in api_result.server_detail, " (was: {!r})".format(api_result.server_detail),
        )
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_proceeds_when_the_readiness_probe_only_gets_a_real_404(suite):
    """Case E, full stack: TCP connects and every readiness-probe candidate
    path (none of which are real endpoints in this fixture) 404s - that
    must never be confused with the server being unreachable; the one real,
    actually-discovered endpoint must still be called and pass.
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
  if (req.url === '/api/widgets') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({ok: true}));
  } else {
    res.writeHead(404);
    res.end();
  }
}).listen(4562, () => console.log('ready - Local:        http://localhost:4562'));
""")
        proj.write("app/api/widgets/route.ts", "export async function GET() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(
            context, proj.path, config=ApiQaConfig(server_startup_timeout=10, connect_probe_timeout=5),
        )
        suite.check("server status is started, never blocked by the readiness probe's own 404s",
                     api_result.server_status == SERVER_STARTED, " (was {})".format(api_result.server_status))
        suite.check(
            "the real endpoint was actually called and passed",
            len(api_result.calls) == 1 and api_result.calls[0].status == CALL_PASS,
            " (got: {})".format([(c.status, c.reason) for c in api_result.calls]),
        )
        suite.check(
            "the readiness detail names a real application-level response, not a connection failure",
            "not a connection failure" in api_result.http_readiness_detail,
            " (was: {!r})".format(api_result.http_readiness_detail),
        )
    finally:
        proj.__exit__(None, None, None)


# --- base URL resolution (Phase 1) ---------------------------------------

def test_resolve_base_url_prefers_a_real_observed_url_over_guessing(suite):
    handle = server_module.ServerHandle(
        proc=None, status=server_module.STATUS_READY, reason="matched ready signal",
        logs=(), elapsed=0.1, base_url="http://localhost:4321",
    )
    warnings = []
    base_url, was_guessed = runner_module._resolve_base_url(handle, warnings)
    suite.check("the real observed URL is used", base_url == "http://localhost:4321")
    suite.check("never marked as guessed when it is real evidence", was_guessed is False)
    suite.check("no warning is added when real evidence was found", warnings == [])


def test_resolve_base_url_falls_back_to_the_documented_default_and_says_so(suite):
    handle = server_module.ServerHandle(
        proc=None, status=server_module.STATUS_ALIVE_NO_READY_SIGNAL, reason="stayed running",
        logs=(), elapsed=0.1, base_url="",
    )
    warnings = []
    base_url, was_guessed = runner_module._resolve_base_url(handle, warnings)
    suite.check("falls back to the documented default port", base_url == "http://localhost:3000")
    suite.check("explicitly marked as guessed, never presented as observed", was_guessed is True)
    suite.check("a clear warning explains the fallback was used",
                 len(warnings) == 1 and "assuming the common default" in warnings[0], " (warnings: {})".format(warnings))


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
    suite.check("shows the Working classification (docs/48)", "Working" in text)


def test_render_shows_evidence_on_failure(suite):
    endpoint = _endpoint(path="/api/broken")
    call = ApiCallResult(
        endpoint=endpoint, status=CALL_FAIL, status_code=500,
        response_time_ms=10.0, reason="HTTP 500 response", response_sample='{"error": "boom"}',
    )
    result = ApiTestResult(root_path="/x", endpoints=(endpoint,), calls=(call,), server_status=SERVER_STARTED)
    text = render(result)
    suite.check("shows the Not Working classification (docs/48, a real 5xx)", "Not Working" in text)
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


# --- to_csv / to_html (docs/36-api-qa-report-export.md) -------------------

def test_to_csv_has_one_row_per_call_with_real_fields(suite):
    ok = _endpoint(path="/api/health")
    bad = _endpoint(method="POST", path="/api/broken")
    calls = (
        ApiCallResult(endpoint=ok, status=CALL_PASS, status_code=200, response_time_ms=12.5, reason="HTTP 200"),
        ApiCallResult(endpoint=bad, status=CALL_FAIL, status_code=500, response_time_ms=8.0, reason="HTTP 500 response"),
    )
    result = ApiTestResult(root_path="/x", endpoints=(ok, bad), calls=calls, server_status=SERVER_STARTED)
    text = to_csv(result)
    rows = text.strip().splitlines()
    suite.check("header row present", rows[0].startswith("method,path,classification,status,status_code"))
    suite.check("2 data rows (one per call)", len(rows) == 3)
    suite.check("passing call's real status code present", "200" in rows[1])
    suite.check("failing call's real reason present", "HTTP 500 response" in rows[2])


def test_to_csv_handles_no_calls_gracefully(suite):
    result = ApiTestResult(root_path="/x", server_status=SERVER_SKIPPED)
    text = to_csv(result)
    suite.check("only the header row, no crash", len(text.strip().splitlines()) == 1)


def test_to_html_color_codes_each_status_bucket(suite):
    passing = _endpoint(path="/api/ok")
    client_err = _endpoint(method="GET", path="/api/missing")
    server_err = _endpoint(method="GET", path="/api/broken")
    unreachable = _endpoint(method="GET", path="/api/down")
    skipped = _endpoint(method="GET", path="/api/dyn/[id]")
    calls = (
        ApiCallResult(endpoint=passing, status=CALL_PASS, status_code=200),
        ApiCallResult(endpoint=client_err, status=CALL_FAIL, status_code=404, reason="HTTP 404 response"),
        ApiCallResult(endpoint=server_err, status=CALL_FAIL, status_code=500, reason="HTTP 500 response"),
        ApiCallResult(endpoint=unreachable, status=CALL_FAIL, error="could not reach it"),
        ApiCallResult(endpoint=skipped, status=CALL_SKIPPED, reason="dynamic, not resolved"),
    )
    result = ApiTestResult(
        root_path="/x", endpoints=(passing, client_err, server_err, unreachable, skipped),
        calls=calls, server_status=SERVER_STARTED, base_url="http://localhost:3000",
    )
    text = to_html(result)
    suite.check("well-formed enough to open", text.startswith("<!doctype html>"))
    suite.check("every endpoint path appears", all(
        e.path.replace("[", "[").replace("]", "]") in text for e in result.endpoints
    ))
    suite.check("2xx uses the pass color", '#dcfce7' in text and '200' in text)
    suite.check("4xx bucket labeled", ">4xx<" in text)
    suite.check("5xx bucket labeled", ">5xx<" in text)
    suite.check("no-response call labeled distinctly from a real 5xx", ">NO RESPONSE<" in text)
    suite.check("skipped call labeled", ">SKIPPED<" in text)
    suite.check("the real failure reason is shown, not fabricated", "could not reach it" in text)


def test_to_html_handles_no_calls_gracefully(suite):
    result = ApiTestResult(root_path="/x", server_status=SERVER_SKIPPED)
    text = to_html(result)
    suite.check("does not crash and says nothing was called", "No endpoint calls were made" in text)


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
        suite.check("real pass reported in CLI output", "GET /api/health" in completed.stdout and "Working" in completed.stdout)
    finally:
        proj.__exit__(None, None, None)


def test_cli_api_report_writes_html_by_default(suite):
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({"name": "x", "scripts": {"start": "node server.js"}}))
        proj.write("server.js", "console.log('hi');\n")
        report_path = proj.path / "report.html"
        completed = run_agent(["discover", str(proj.path), "--api-report", str(report_path)])
        suite.check("exits cleanly", completed.returncode == 0, " (rc={})".format(completed.returncode))
        suite.check("--api-report alone implies --api-test", "API QA Results" in completed.stdout)
        suite.check("report file was written", report_path.is_file())
        text = report_path.read_text(encoding="utf-8")
        suite.check("it's the HTML report", text.startswith("<!doctype html>"))
    finally:
        proj.__exit__(None, None, None)


def test_cli_api_report_writes_csv_for_csv_extension(suite):
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({"name": "x", "scripts": {"start": "node server.js"}}))
        proj.write("server.js", "console.log('hi');\n")
        report_path = proj.path / "report.csv"
        completed = run_agent(["discover", str(proj.path), "--api-report", str(report_path)])
        suite.check("exits cleanly", completed.returncode == 0)
        text = report_path.read_text(encoding="utf-8")
        suite.check("it's the CSV report", text.startswith("method,path,classification,status,status_code"))
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
        test_discover_pages_router_finds_method_via_req_method_check,
        test_discover_pages_router_index_file_maps_to_parent_path,
        test_discover_pages_router_dynamic_segment,
        test_discover_pages_router_no_method_check_assumes_get_and_warns,
        test_discover_pages_router_no_default_export_warns_and_skips,
        test_discover_pages_router_gated_on_real_nextjs_framework_fact,
        test_discover_express_finds_a_simple_route,
        test_discover_express_finds_router_calls_and_dynamic_segments,
        test_discover_express_gated_on_real_express_framework_fact,
        test_discover_express_ignores_non_literal_first_argument,
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
        test_discover_server_start_command_falls_back_to_a_monorepo_package,
        test_discover_server_start_command_ambiguous_monorepo_is_never_guessed,
        test_start_and_wait_ready_reports_not_found_for_a_missing_binary,
        test_start_and_wait_ready_detects_a_ready_signal,
        test_start_and_wait_ready_does_not_stop_on_a_keyword_only_line,
        test_start_and_wait_ready_detects_a_crash,
        test_run_api_qa_skips_when_no_endpoints_discovered,
        test_run_api_qa_skips_when_no_start_command,
        test_run_api_qa_reports_a_crashed_server,
        test_run_api_qa_dynamic_routes_are_never_called,
        test_run_api_qa_end_to_end_real_server,
        test_run_api_qa_captures_server_log_tail_during_the_run,
        test_wait_until_connectable_true_once_a_real_listener_binds,
        test_wait_until_connectable_false_when_nothing_ever_binds,
        test_run_api_qa_waits_out_a_delayed_port_bind_before_calling,
        test_run_api_qa_observes_the_real_port_past_a_wrapper_false_ready_line,
        test_run_api_qa_stops_fast_when_the_port_never_binds,
        test_run_api_qa_blocks_on_timeout_when_no_ready_signal_and_nothing_binds,
        test_check_http_readiness_reached_on_a_real_200,
        test_check_http_readiness_reached_on_a_real_404_not_confused_with_unreachable,
        test_check_http_readiness_not_reached_when_nothing_is_listening,
        test_check_http_readiness_prefers_a_real_health_shaped_endpoint,
        test_run_api_qa_blocks_when_tcp_connects_but_nothing_ever_speaks_http,
        test_run_api_qa_proceeds_when_the_readiness_probe_only_gets_a_real_404,
        test_resolve_base_url_prefers_a_real_observed_url_over_guessing,
        test_resolve_base_url_falls_back_to_the_documented_default_and_says_so,
        test_render_shows_endpoint_method_path_status_and_timing,
        test_render_shows_evidence_on_failure,
        test_render_handles_no_calls_gracefully,
        test_render_shows_warnings,
        test_to_csv_has_one_row_per_call_with_real_fields,
        test_to_csv_handles_no_calls_gracefully,
        test_to_html_color_codes_each_status_bucket,
        test_to_html_handles_no_calls_gracefully,
        test_cli_api_test_flag_reports_when_no_app_router_present,
        test_cli_without_api_test_flag_never_runs_api_qa,
        test_cli_api_test_end_to_end,
        test_cli_api_report_writes_html_by_default,
        test_cli_api_report_writes_csv_for_csv_extension,
    ]))
