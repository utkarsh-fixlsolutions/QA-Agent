"""Functional API test planning + dependency-aware execution (Phase 3,
docs/54-functional-api-test-planning.md) - `qa_agent.api_qa.planning`:
`build_test_plan`/`execute_test_plan`.

Mocked-HTTP tests reuse the same `_FakeResponse`/`_with_fake_urlopen`
technique test_api_qa.py/test_api_qa_resolution.py already established, but
with a small, real, *stateful* fake backend (an in-memory user store) so a
create -> read -> update -> delete -> verify workflow can be exercised
deterministically without a real subprocess. One real, guarded, real-
subprocess end-to-end test at the bottom proves the same workflow against a
genuine running server and genuine HTTP calls, matching the "at least one
real E2E, not just mocks" precedent every other resolution.py-adjacent test
file already follows.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.api_qa import (  # noqa: E402
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    EXPECTED_NOT_FOUND,
    EXPECTED_SUCCESS,
    TEST_CATEGORY_EXPECTED_NEGATIVE,
    TEST_CATEGORY_FUNCTIONAL_POSITIVE,
    TEST_CATEGORY_FUNCTIONAL_WORKFLOW,
    ApiEndpoint,
    ApiQaConfig,
    build_test_plan,
    execute_test_plan,
    run_api_qa,
)
from qa_agent.api_qa import http_client as http_client_module  # noqa: E402
from qa_agent.api_qa.render import render_test_plan  # noqa: E402


def _npm_available():
    return shutil.which("npm") is not None


# --- shared fixture endpoints -------------------------------------------

def _list_ep():
    return ApiEndpoint(method="GET", path="/api/users", source_file="app.py")


def _get_ep():
    return ApiEndpoint(method="GET", path="/api/users/{id}", source_file="app.py", dynamic=True)


def _create_ep():
    return ApiEndpoint(
        method="POST", path="/api/users", source_file="app.py",
        test_evidence_fields=(("name", "Alice"),),
    )


def _update_ep():
    return ApiEndpoint(
        method="PUT", path="/api/users/{id}", source_file="app.py", dynamic=True,
        test_evidence_fields=(("name", "Alice Updated"),),
    )


def _delete_ep():
    return ApiEndpoint(method="DELETE", path="/api/users/{id}", source_file="app.py", dynamic=True)


def _crud_endpoints():
    return (_list_ep(), _get_ep(), _create_ep(), _update_ep(), _delete_ep())


# --- A/B: plan generation + dependency ordering --------------------------

def test_build_test_plan_produces_the_expected_crud_workflow(suite):
    plan = build_test_plan(_crud_endpoints())
    suite.check("exactly 6 tests were planned (list, get, create, update, delete, verify)", len(plan) == 6,
                 " (got {})".format(len(plan)))
    by_id = {t.test_id: t for t in plan}
    t1, t2, t3, t4, t5, t6 = plan
    suite.check("T1 is the collection list", (t1.endpoint.method, t1.endpoint.path) == ("GET", "/api/users"))
    suite.check("T1 has no dependency", t1.depends_on == ())
    suite.check("T2 is get-by-id", (t2.endpoint.method, t2.endpoint.path) == ("GET", "/api/users/{id}"))
    suite.check("T2 depends on T1 (the list)", t2.depends_on == ("T1",))
    suite.check("T3 is create", (t3.endpoint.method, t3.endpoint.path) == ("POST", "/api/users"))
    suite.check("T3 has no runtime dependency (contract evidence only)", t3.depends_on == ())
    suite.check("T3 is categorized as a workflow step (update+delete exist)",
                 t3.category == TEST_CATEGORY_FUNCTIONAL_WORKFLOW)
    suite.check("T4 is update", (t4.endpoint.method, t4.endpoint.path) == ("PUT", "/api/users/{id}"))
    suite.check("T4 depends on T3 (uses the created id)", t4.depends_on == ("T3",))
    suite.check("T4's id_source_test_id points at T3", t4.id_source_test_id == "T3")
    suite.check("T5 is delete", (t5.endpoint.method, t5.endpoint.path) == ("DELETE", "/api/users/{id}"))
    suite.check("T5 depends on T4 (chains off the update, not straight from create)", t5.depends_on == ("T4",))
    suite.check("T6 is the verify-deletion step", (t6.endpoint.method, t6.endpoint.path) == ("GET", "/api/users/{id}"))
    suite.check("T6 depends on T5 (the delete)", t6.depends_on == ("T5",))
    suite.check("T6 expects 404, not a generic success", t6.expected_status_kind == EXPECTED_NOT_FOUND)
    suite.check("T6 is categorized as an expected-negative test", t6.category == TEST_CATEGORY_EXPECTED_NEGATIVE)
    suite.check("every other test expects an ordinary success", all(
        t.expected_status_kind == EXPECTED_SUCCESS for t in (t1, t2, t3, t4, t5)
    ))


def test_build_test_plan_every_dependency_is_declared_before_its_dependent(suite):
    plan = build_test_plan(_crud_endpoints())
    seen = set()
    ok = True
    for test in plan:
        for dep in test.depends_on:
            if dep not in seen:
                ok = False
        seen.add(test.test_id)
    suite.check("every test's dependency already appeared earlier in the plan (dependency-ordered)", ok)


def test_build_test_plan_get_only_api_produces_meaningful_get_tests_no_fabricated_workflow(suite):
    endpoints = (_list_ep(), _get_ep())
    plan = build_test_plan(endpoints)
    suite.check("exactly 2 tests for a GET-only API (no invented create/delete)", len(plan) == 2,
                 " (got {})".format(len(plan)))
    suite.check("no test claims a workflow category (nothing to chain)", all(
        t.category != TEST_CATEGORY_FUNCTIONAL_WORKFLOW for t in plan
    ))


def test_build_test_plan_covers_every_discovered_endpoint_including_unrelated_ones(suite):
    login_ep = ApiEndpoint(method="POST", path="/api/login", source_file="app.py", reads_request_body=True)
    endpoints = _crud_endpoints() + (login_ep,)
    plan = build_test_plan(endpoints)
    planned_endpoint_ids = {id(t.endpoint) for t in plan}
    suite.check("every discovered endpoint got at least one planned test",
                 all(id(e) in planned_endpoint_ids for e in endpoints))
    suite.check("the unrelated standalone POST got its own test with no fabricated dependency",
                 any(t.endpoint is login_ep and t.depends_on == () for t in plan))


# --- stateful fake HTTP backend for execute_test_plan ---------------------

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


def _json_response(status, payload):
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"})


def _not_found(url):
    return urllib.error.HTTPError(
        url, 404, "Not Found", hdrs={"Content-Type": "application/json"},
        fp=_FakeResponse(404, b'{"error": "not found"}'),
    )


class _UsersStore:
    """A real, tiny, in-memory stand-in server (Phase 3's own execution
    logic never knows this isn't a real HTTP process - it only ever sees
    real status codes/bodies through `call_endpoint`'s own, unmodified
    urlopen-based mechanism)."""

    def __init__(self, broken_delete=False):
        self.users = {1: {"id": 1, "name": "Alice"}}
        self.next_id = 2
        self.broken_delete = broken_delete

    def urlopen(self, request, timeout=None):
        method = request.get_method()
        path = "/" + request.full_url.split("://", 1)[1].split("/", 1)[1]
        payload = json.loads(request.data.decode("utf-8")) if request.data else {}

        if path == "/api/users" and method == "GET":
            return _json_response(200, list(self.users.values()))
        if path == "/api/users" and method == "POST":
            uid = self.next_id
            self.next_id += 1
            self.users[uid] = {"id": uid, "name": payload.get("name", "unknown")}
            return _json_response(201, self.users[uid])
        if path.startswith("/api/users/"):
            raw = path[len("/api/users/"):]
            uid = int(raw) if raw.isdigit() else raw
            if method == "GET":
                if uid in self.users:
                    return _json_response(200, self.users[uid])
                raise _not_found(request.full_url)
            if method == "PUT":
                if uid in self.users:
                    self.users[uid]["name"] = payload.get("name", self.users[uid]["name"])
                    return _json_response(200, self.users[uid])
                raise _not_found(request.full_url)
            if method == "DELETE":
                if uid in self.users:
                    if not self.broken_delete:
                        del self.users[uid]
                    return _FakeResponse(204, b"", {})
                raise _not_found(request.full_url)
        raise _not_found(request.full_url)


# --- C/D/E/F: runtime id extraction, path resolution, CRUD workflow, ------
# --- expected-negative outcome --------------------------------------------

def test_execute_test_plan_runs_the_full_crud_workflow_with_real_runtime_ids(suite):
    plan = build_test_plan(_crud_endpoints())
    store = _UsersStore()
    results = _with_fake_urlopen(store.urlopen, lambda: execute_test_plan(plan, "http://x", timeout=5))
    by_id = {r.test_id: r for r in results}

    suite.check("T1 (list) passed", by_id["T1"].status == CALL_PASS)
    suite.check("T2 (get by id) passed, resolved from T1's real response",
                 by_id["T2"].status == CALL_PASS and by_id["T2"].call.resolved_path == "/api/users/1")
    suite.check("T3 (create) passed", by_id["T3"].status == CALL_PASS)
    suite.check("T3's body came from real Phase 2 test-evidence, not a blind placeholder",
                 by_id["T3"].call.response_json.get("name") == "Alice")
    created_id = by_id["T3"].call.response_json["id"]
    suite.check("T3 really created a new user with its own new id (not id=1)", created_id == 2)

    suite.check("T4 (update) used the REAL id created by T3, not an invented one",
                 by_id["T4"].call.resolved_path == "/api/users/{}".format(created_id))
    suite.check("T4 passed", by_id["T4"].status == CALL_PASS)
    suite.check(
        "T4's own real response shows the update really took effect on the real server",
        by_id["T4"].call.response_json.get("name") == "Alice Updated",
    )
    suite.check(
        "T4's body is marked synthetic per this project's own existing convention (built from "
        "test-evidence, not a live OpenAPI default) - but the path/id itself was real, runtime evidence",
        by_id["T4"].call.synthetic and "name" in by_id["T4"].call.synthetic_fields,
    )

    suite.check("T5 (delete) used the same real created id", by_id["T5"].call.resolved_path == "/api/users/{}".format(created_id))
    suite.check("T5 passed", by_id["T5"].status == CALL_PASS)
    suite.check("the real server no longer has the user after T5", created_id not in store.users)

    suite.check("T6 (verify) reused the exact same id T5 just deleted",
                 by_id["T6"].call.resolved_path == "/api/users/{}".format(created_id))
    suite.check("T6 got a real 404", by_id["T6"].call.status_code == 404)
    suite.check("T6's TEST verdict is PASS (a 404 here is the correct, expected outcome)",
                 by_id["T6"].status == CALL_PASS)
    suite.check("T6's underlying raw call.status is FAIL (never silently reinterpreted)",
                 by_id["T6"].call.status == CALL_FAIL)
    suite.check("the report explains why 404 counted as a pass",
                 "expected" in by_id["T6"].verdict_reason.lower())


def test_execute_test_plan_catches_a_real_defect_get_after_delete_returns_200(suite):
    # docs/54, section 13: a deliberately broken DELETE that returns success
    # but never actually removes the resource - the same real defect the
    # phase spec's own failure demonstration requires.
    plan = build_test_plan(_crud_endpoints())
    store = _UsersStore(broken_delete=True)
    results = _with_fake_urlopen(store.urlopen, lambda: execute_test_plan(plan, "http://x", timeout=5))
    by_id = {r.test_id: r for r in results}

    suite.check("T5 (delete) still reports a real 204 pass (the bug is not here)", by_id["T5"].status == CALL_PASS)
    suite.check("T6 (verify) got a real 200 instead of the expected 404",
                 by_id["T6"].call.status_code == 200)
    suite.check("T6's TEST verdict is FAIL - the broken delete was really caught",
                 by_id["T6"].status == CALL_FAIL)
    suite.check("the failure reason names the real, unexpected status code",
                 "200" in by_id["T6"].verdict_reason)


# --- G: unrelated 500 after a successful workflow is still a real fail ----

def test_execute_test_plan_marks_success_kind_test_failed_on_a_real_500(suite):
    list_ep, get_ep = _list_ep(), _get_ep()
    plan = build_test_plan((list_ep, get_ep))

    class _Boom:
        def urlopen(self, request, timeout=None):
            if request.get_method() == "GET" and request.full_url.endswith("/api/users"):
                return _json_response(200, [{"id": 1, "name": "Alice"}])
            raise urllib.error.HTTPError(
                request.full_url, 500, "Internal Server Error", hdrs={"Content-Type": "application/json"},
                fp=_FakeResponse(500, b'{"error": "boom"}'),
            )

    boom = _Boom()
    results = _with_fake_urlopen(boom.urlopen, lambda: execute_test_plan(plan, "http://x", timeout=5))
    by_id = {r.test_id: r for r in results}
    suite.check("the get-by-id test really failed on the real 500", by_id["T2"].status == CALL_FAIL)
    suite.check("status_code 500 is the real evidence", by_id["T2"].call.status_code == 500)


# --- H: contract integration (Phase 2 evidence feeds the plan's bodies) ---

def test_execute_test_plan_uses_phase2_evidence_for_request_bodies(suite):
    create_ep = _create_ep()
    plan = build_test_plan((create_ep,))
    store = _UsersStore()
    results = _with_fake_urlopen(store.urlopen, lambda: execute_test_plan(plan, "http://x", timeout=5))
    suite.check("exactly one test was planned for the standalone create endpoint", len(results) == 1)
    result = results[0]
    suite.check("the create call passed", result.status == CALL_PASS)
    suite.check("the request body used the endpoint's own real test-evidence value, not a generic placeholder",
                 result.call.response_json.get("name") == "Alice")
    suite.check("body_evidence_source records the real evidence tier used", result.call.body_evidence_source == "test_example")


# --- render_test_plan smoke check -----------------------------------------

def test_render_test_plan_shows_every_test_and_the_final_tally(suite):
    plan = build_test_plan(_crud_endpoints())
    store = _UsersStore()
    results = _with_fake_urlopen(store.urlopen, lambda: execute_test_plan(plan, "http://x", timeout=5))

    from qa_agent.api_qa import ApiTestResult
    fake_result = ApiTestResult(root_path="x", test_plan=plan, functional_results=results)
    text = render_test_plan(fake_result)
    suite.check("every test id appears in the rendered report", all(t.test_id in text for t in plan))
    suite.check("the final tally line is present", "test(s)" in text and "passed" in text)


# --- I/J: real end-to-end against a genuine running server ---------------

def test_real_end_to_end_crud_workflow_against_a_real_server(suite):
    """A genuine Node `http` server (the same framework-agnostic stand-in
    test_api_qa_resolution.py's own real end-to-end test already uses) with
    real, in-memory CRUD state - proves the full plan is executed with
    genuine HTTP calls against a genuine process, not just mocks.
    """
    if shutil.which("node") is None:
        suite.check("node is available for the real end-to-end CRUD test (skipped - node not found)", True)
        return

    server_js = """
const http = require('http');
let users = {1: {id: 1, name: 'Alice'}};
let nextId = 2;
const server = http.createServer((req, res) => {
  let chunks = [];
  req.on('data', c => chunks.push(c));
  req.on('end', () => {
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {};
    const parts = req.url.split('/').filter(Boolean);
    const sendJson = (code, payload) => {
      res.writeHead(code, {'Content-Type': 'application/json'});
      res.end(JSON.stringify(payload));
    };
    if (req.url === '/api/users' && req.method === 'GET') {
      sendJson(200, Object.values(users)); return;
    }
    if (req.url === '/api/users' && req.method === 'POST') {
      const id = nextId++;
      users[id] = {id: id, name: body.name || 'unknown'};
      sendJson(201, users[id]); return;
    }
    if (parts[0] === 'api' && parts[1] === 'users' && parts[2]) {
      const id = parseInt(parts[2], 10);
      if (req.method === 'GET') {
        if (users[id]) { sendJson(200, users[id]); }
        else { sendJson(404, {error: 'not found'}); }
        return;
      }
      if (req.method === 'PUT') {
        if (users[id]) { users[id].name = body.name || users[id].name; sendJson(200, users[id]); }
        else { sendJson(404, {error: 'not found'}); }
        return;
      }
      if (req.method === 'DELETE') {
        if (users[id]) { delete users[id]; res.writeHead(204); res.end(); }
        else { sendJson(404, {error: 'not found'}); }
        return;
      }
    }
    sendJson(404, {error: 'not found'});
  });
});
server.listen(process.env.PORT || 0, () => {
  console.log('listening on port ' + server.address().port);
});
"""
    proj = TempProject()
    try:
        script = proj.path / "server.js"
        script.write_text(server_js, encoding="utf-8")
        import subprocess
        import socket
        import time as _time

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        proc = subprocess.Popen(
            ["node", str(script)], cwd=str(proj.path),
            env={**__import__("os").environ, "PORT": str(port)},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            base_url = "http://127.0.0.1:{}".format(port)
            deadline = _time.time() + 10
            ready = False
            while _time.time() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        ready = True
                        break
                except OSError:
                    _time.sleep(0.1)
            suite.check("the real Node server became reachable", ready)
            if not ready:
                return

            plan = build_test_plan(_crud_endpoints())
            results = execute_test_plan(plan, base_url, timeout=10)
            by_id = {r.test_id: r for r in results}
            suite.check("T1..T5 all really passed against the real server", all(
                by_id[t].status == CALL_PASS for t in ("T1", "T2", "T3", "T4", "T5")
            ))
            suite.check("T6 really got a real 404 from the real server and PASSed as expected",
                         by_id["T6"].call.status_code == 404 and by_id["T6"].status == CALL_PASS)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        proj.__exit__(None, None, None)


def test_run_api_qa_still_blocks_when_server_is_unreachable(suite):
    """Phase 1 regression (docs/52): the environment readiness gate still
    stops the whole run - including the new Phase 3 stage - before any real
    endpoint call is made, when the server never becomes reachable.
    """
    from qa_agent.project import build_repository_context, discover_project

    proj = TempProject()
    try:
        (proj.path / "package.json").write_text(json.dumps({
            "name": "unreachable-demo", "scripts": {"dev": "node -e \"setTimeout(()=>{}, 30000)\""},
        }), encoding="utf-8")
        (proj.path / "package-lock.json").write_text("{}", encoding="utf-8")
        (proj.path / "app").mkdir()
        (proj.path / "app" / "api").mkdir()
        (proj.path / "app" / "api" / "users").mkdir()
        (proj.path / "app" / "api" / "users" / "route.ts").write_text(
            "export async function GET() { return Response.json([]); }\n", encoding="utf-8",
        )
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(
            server_startup_timeout=3.0, connect_probe_timeout=2.0, http_readiness_timeout=2.0,
        ))
        suite.check("server_status is unreachable (or a real, honest failure before it)",
                     api_result.server_status in ("unreachable", "start_failed", "crashed"),
                     " (was {})".format(api_result.server_status))
        suite.check("no functional test plan was ever executed - the run was blocked upstream",
                     api_result.functional_results == ())
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: Phase 3 functional test planning (planning.py)")
    sys.exit(suite.run([
        test_build_test_plan_produces_the_expected_crud_workflow,
        test_build_test_plan_every_dependency_is_declared_before_its_dependent,
        test_build_test_plan_get_only_api_produces_meaningful_get_tests_no_fabricated_workflow,
        test_build_test_plan_covers_every_discovered_endpoint_including_unrelated_ones,
        test_execute_test_plan_runs_the_full_crud_workflow_with_real_runtime_ids,
        test_execute_test_plan_catches_a_real_defect_get_after_delete_returns_200,
        test_execute_test_plan_marks_success_kind_test_failed_on_a_real_500,
        test_execute_test_plan_uses_phase2_evidence_for_request_bodies,
        test_render_test_plan_shows_every_test_and_the_final_tally,
        test_real_end_to_end_crud_workflow_against_a_real_server,
        test_run_api_qa_still_blocks_when_server_is_unreachable,
    ]))
