"""Live progress reporting (docs/44-live-progress.md): the optional
`on_progress` callback threaded through `resolve_and_execute`,
`generate_and_execute_negative_cases`, `validate_response_schemas`, and
`run_api_qa`'s own three-phase combiner (`_combine_progress`). Purely
additive - `on_progress=None` (the default) must leave every existing
behavior completely unaffected, already proven by the full, unmodified
regression suite in `test_api_qa_resolution.py`/`test_api_qa_negative_and
_schema.py` still passing; this file only proves the new callback itself
behaves honestly.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import ApiEndpoint, ApiQaConfig, run_api_qa  # noqa: E402
from qa_agent.api_qa import http_client as http_client_module  # noqa: E402
from qa_agent.api_qa.resolution import resolve_and_execute  # noqa: E402
from qa_agent.api_qa.runner import _combine_progress  # noqa: E402


def _npm_available():
    return shutil.which("npm") is not None


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


# --- resolve_and_execute's on_progress (mocked) -----------------------------

def test_resolve_and_execute_on_progress_reports_every_endpoint_once(suite):
    endpoints = (
        ApiEndpoint(method="GET", path="/health", source_file="x", dynamic=False),
        ApiEndpoint(method="GET", path="/status", source_file="x", dynamic=False),
    )
    events = []

    def fake(request, timeout=None):
        return _FakeResponse(200, b"{}", {"Content-Type": "application/json"})

    _with_fake_urlopen(fake, lambda: resolve_and_execute(
        endpoints, "http://x", timeout=5, on_progress=lambda d, t, l: events.append((d, t, l)),
    ))
    suite.check("exactly one event per endpoint", len(events) == 2)
    suite.check("total is fixed at the real endpoint count, from the first event",
                all(t == 2 for _, t, _ in events))
    suite.check("done counts up 1, 2 - never skips, never repeats",
                [d for d, _, _ in events] == [1, 2])


def test_resolve_and_execute_on_progress_none_is_a_true_no_op(suite):
    """The default - proves the additive parameter changes nothing about
    the function's own real behavior when a caller doesn't use it.
    """
    endpoint = ApiEndpoint(method="GET", path="/health", source_file="x", dynamic=False)

    def fake(request, timeout=None):
        return _FakeResponse(200, b"{}", {"Content-Type": "application/json"})

    calls = _with_fake_urlopen(fake, lambda: resolve_and_execute((endpoint,), "http://x", timeout=5))
    suite.check("still works with no on_progress at all", len(calls) == 1 and calls[0].status == "pass")


# --- _combine_progress (pure) -----------------------------------------------

def test_combine_progress_returns_three_none_callbacks_when_on_progress_is_none(suite):
    primary, negative, schema = _combine_progress(None)
    suite.check("all three are None - no-op, matching each phase's own default",
                primary is None and negative is None and schema is None)


def test_combine_progress_total_only_grows_as_each_phase_starts_reporting(suite):
    events = []
    primary, negative, schema = _combine_progress(lambda d, t, l: events.append((d, t, l)))

    primary(1, 2, "GET /a")
    primary(2, 2, "GET /b")
    suite.check("overall total during phase 1 is only phase 1's own total (2)",
                events[-1][1] == 2)
    suite.check("overall done reflects phase 1 alone so far", events[-1][0] == 2)

    negative(1, 1, "negative: POST /a")
    suite.check("total grows once phase 2 starts reporting its own real total",
                events[-1][1] == 3)
    suite.check("done accumulates across phases, never resets", events[-1][0] == 3)

    schema(1, 1, "schema: GET /a")
    suite.check("total grows again for phase 3", events[-1][1] == 4)
    suite.check("done reaches the final combined total", events[-1][0] == 4)


# --- one real, guarded, end-to-end proof (real Node server) -----------------

def test_real_end_to_end_progress_stream_is_honest_and_reaches_completion(suite):
    """Full proof through `run_api_qa` against a real, running server: the
    real progress stream must never report `done > total`, must end with
    `done == total` at the real final event, and must actually advance
    (not just fire once at the end) across a project with enough real
    endpoints to exercise all three phases.
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
http.createServer((req, res) => {
  const parsed = url.parse(req.url);
  if (parsed.pathname === '/openapi.json') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({ paths: { '/api/items': { post: { requestBody: { content: {
      'application/json': { schema: { required: ['name'], properties: {
        name: { type: 'string', default: 'x' } } } } } } } } } }));
  } else if (parsed.pathname === '/api/items' && req.method === 'GET') {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify([{id: 1, name: 'a'}]));
  } else if (parsed.pathname === '/api/items' && req.method === 'POST') {
    let raw = ''; req.on('data', c => raw += c);
    req.on('end', () => {
      const body = JSON.parse(raw || '{}');
      if (!body.name) { res.writeHead(400); res.end('{}'); }
      else { res.writeHead(201, {'Content-Type': 'application/json'}); res.end(JSON.stringify({id: 2})); }
    });
  } else { res.writeHead(404); res.end(); }
}).listen(4324, () => console.log('ready - Local:        http://localhost:4324'));
""")
        proj.write("app/api/items/route.ts",
                    "export async function GET() { return Response.json([]); }\n"
                    "export async function POST() { return Response.json({}); }\n")

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        events = []
        api_result = run_api_qa(
            context, proj.path, config=ApiQaConfig(server_startup_timeout=20),
            on_progress=lambda d, t, l: events.append((d, t, l)),
        )

        suite.check("the real server started", api_result.server_status == "started",
                     " (was {}: {})".format(api_result.server_status, api_result.server_detail))
        suite.check("at least one real progress event was reported", len(events) > 0)
        if events:
            suite.check("done never exceeds total at any real event",
                         all(d <= t for d, t, _ in events))
            suite.check("the final event's done equals its own total - the stream really finishes",
                         events[-1][0] == events[-1][1])
            suite.check("total really grew across phases (more than just the 2 primary endpoints)",
                         events[-1][1] > 2, " (was {})".format(events[-1][1]))
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: live progress reporting")
    sys.exit(suite.run([
        test_resolve_and_execute_on_progress_reports_every_endpoint_once,
        test_resolve_and_execute_on_progress_none_is_a_true_no_op,
        test_combine_progress_returns_three_none_callbacks_when_on_progress_is_none,
        test_combine_progress_total_only_grows_as_each_phase_starts_reporting,
        test_real_end_to_end_progress_stream_is_honest_and_reaches_completion,
    ]))
