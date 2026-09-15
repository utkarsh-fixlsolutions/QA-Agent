"""Python/FastAPI support for API QA (docs/32-fastapi-discovery-and-startup
.md) - a second, additive discovery+server-start strategy alongside the
existing Next.js one (Step 30), added because a real capability audit
against `D:\\Working\\qa-agent-fastapi-demo` found the existing API QA
pipeline could not reach a real FastAPI project at all: `qa_agent.api_qa.
discovery` only ever looked for `route.ts`/`route.js`, and `qa_agent.api_qa.
server` only ever looked for an npm script.

Follows this package's own established fixture conventions: `_context_for`
builds a real `RepositoryContext` from real files via the real discovery
pipeline (the same technique test_api_qa.py/test_api_ai_bridge.py already
use), and a guarded real-subprocess test (`_fastapi_stack_available()`,
mirroring `test_runtime_execution.py`'s own `_npm_available()` precedent)
proves the full mechanism end-to-end when a real FastAPI/uvicorn install is
reachable, skipping cleanly otherwise - the real, external demo project is
the authoritative real-world proof either way (see docs/32's own dogfooding
section).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import discover_api_endpoints  # noqa: E402
from qa_agent.api_qa import server as server_module  # noqa: E402


def _context_for(files):
    proj = TempProject()
    for rel, text in files.items():
        proj.write(rel, text)
    result = discover_project(proj.path)
    context = build_repository_context(result.project)
    return context, proj


def _fastapi_requirements():
    return "fastapi>=0.110.0\nuvicorn[standard]>=0.28.0\n"


def _fastapi_stack_available():
    ok, _ = server_module.check_python_dependencies(sys.executable, modules=("fastapi", "uvicorn"))
    return ok


# --- FastAPI route discovery -------------------------------------------

MAIN_PY_SIMPLE = (
    '"""demo"""\n'
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n\n"
    '@app.get("/health")\n'
    "def health():\n"
    '    return {"status": "ok"}\n\n'
    '@app.post("/api/users")\n'
    "def create_user(payload: dict):\n"
    "    return payload\n\n"
    '@app.get("/api/users/{user_id}")\n'
    "def get_user(user_id: int):\n"
    "    return {}\n"
)


def test_fastapi_discovery_finds_get_route(suite):
    context, proj = _context_for({"requirements.txt": _fastapi_requirements(), "app/main.py": MAIN_PY_SIMPLE})
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        get_health = [e for e in endpoints if e.method == "GET" and e.path == "/health"]
        suite.check("GET /health discovered", len(get_health) == 1)
        suite.check("source file is real", get_health and get_health[0].source_file == "app/main.py")
        suite.check("no warnings", warnings == ())
    finally:
        proj.__exit__(None, None, None)


def test_fastapi_discovery_finds_post_route(suite):
    context, proj = _context_for({"requirements.txt": _fastapi_requirements(), "app/main.py": MAIN_PY_SIMPLE})
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        post_users = [e for e in endpoints if e.method == "POST" and e.path == "/api/users"]
        suite.check("POST /api/users discovered", len(post_users) == 1)
    finally:
        proj.__exit__(None, None, None)


ROUTER_PY = (
    "from fastapi import APIRouter\n"
    "router = APIRouter()\n\n"
    '@router.get("/items")\n'
    "def list_items():\n"
    "    return []\n\n"
    '@router.delete("/items/{item_id}")\n'
    "def delete_item(item_id: int):\n"
    "    return None\n"
)


def test_fastapi_discovery_finds_router_route(suite):
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "app/main.py": MAIN_PY_SIMPLE,
        "app/routers.py": ROUTER_PY,
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        items = [e for e in endpoints if e.path == "/items" and e.method == "GET"]
        suite.check("@router.get(...) is discovered", len(items) == 1)
        suite.check("from the real router file", items and items[0].source_file == "app/routers.py")
        delete_item = [e for e in endpoints if e.path == "/items/{item_id}" and e.method == "DELETE"]
        suite.check("@router.delete(...) is discovered too", len(delete_item) == 1)
    finally:
        proj.__exit__(None, None, None)


def test_fastapi_discovery_preserves_dynamic_path_parameter(suite):
    context, proj = _context_for({"requirements.txt": _fastapi_requirements(), "app/main.py": MAIN_PY_SIMPLE})
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        dynamic = [e for e in endpoints if e.path == "/api/users/{user_id}"]
        suite.check("dynamic route discovered with the literal {user_id} placeholder preserved",
                     len(dynamic) == 1 and dynamic[0].path == "/api/users/{user_id}")
        suite.check("marked dynamic", dynamic and dynamic[0].dynamic is True)
    finally:
        proj.__exit__(None, None, None)


def test_fastapi_discovery_never_invents_a_parameter_value(suite):
    context, proj = _context_for({"requirements.txt": _fastapi_requirements(), "app/main.py": MAIN_PY_SIMPLE})
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("no endpoint anywhere has a concrete, invented id in place of {user_id}",
                     all("/api/users/1" != e.path and "/api/users/0" != e.path for e in endpoints))
        suite.check("the dynamic placeholder is exactly what was written, not guessed at",
                     any(e.path == "/api/users/{user_id}" for e in endpoints))
    finally:
        proj.__exit__(None, None, None)


def test_fastapi_discovery_tracks_line_numbers(suite):
    context, proj = _context_for({"requirements.txt": _fastapi_requirements(), "app/main.py": MAIN_PY_SIMPLE})
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        health = next(e for e in endpoints if e.path == "/health")
        suite.check("a real, positive line number is recorded", health.line is not None and health.line >= 1)
        # MAIN_PY_SIMPLE's @app.get("/health") is on line 5 (1-based):
        # docstring, import, `app = FastAPI()`, blank, @app.get(...)
        suite.check("the line number is actually correct", health.line == 5, " (was {})".format(health.line))
    finally:
        proj.__exit__(None, None, None)


def test_fastapi_discovery_gated_behind_real_fastapi_detection(suite):
    """The same decorator shape (`@app.get(...)`) also matches modern
    Flask's own shorthand routes - this strategy must never fire just
    because the text looks right; it requires a real, already-detected
    FastAPI framework fact first.
    """
    context, proj = _context_for({"app/main.py": MAIN_PY_SIMPLE})  # no requirements.txt -> no FastAPI detected
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("nothing discovered without a real FastAPI fact", endpoints == ())
        suite.check("no warnings either - the strategy never even ran", warnings == ())
    finally:
        proj.__exit__(None, None, None)


def test_fastapi_discovery_ignores_venv_directories(suite):
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "app/main.py": MAIN_PY_SIMPLE,
        ".venv/Lib/site-packages/fastapi/routing.py": '@app.get("/should-never-be-found")\ndef x():\n    pass\n',
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        suite.check("nothing from inside .venv is ever discovered",
                     all("should-never-be-found" not in e.path for e in endpoints))
    finally:
        proj.__exit__(None, None, None)


def test_fastapi_discovery_does_not_affect_nextjs_discovery(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "app/api/health/route.ts": "export async function GET() { return Response.json({}); }\n",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("Next.js discovery is completely unaffected by the new strategy existing",
                     len(endpoints) == 1 and endpoints[0].path == "/api/health" and endpoints[0].method == "GET")
        suite.check("no warnings", warnings == ())
    finally:
        proj.__exit__(None, None, None)


def test_fastapi_discovery_against_the_real_demo_shaped_fixture(suite):
    """A fixture reproducing the real `D:\\Working\\qa-agent-fastapi-demo`
    route set exactly - the concrete acceptance check this whole step
    exists to satisfy.
    """
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "app/__init__.py": "",
        "app/main.py": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            '@app.get("/health")\n'
            "def health_check():\n    return {}\n\n"
            '@app.get("/api/users")\n'
            "def list_users():\n    return []\n\n"
            '@app.get("/api/users/{user_id}")\n'
            "def get_user(user_id: int):\n    return {}\n\n"
            '@app.post("/api/users")\n'
            "def create_user(payload: dict):\n    return {}\n\n"
            '@app.delete("/api/users/{user_id}")\n'
            "def delete_user(user_id: int):\n    return None\n"
        ),
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        found = {(e.method, e.path) for e in endpoints}
        expected = {
            ("GET", "/health"), ("GET", "/api/users"), ("GET", "/api/users/{user_id}"),
            ("POST", "/api/users"), ("DELETE", "/api/users/{user_id}"),
        }
        suite.check("exactly the 5 real demo routes are discovered, nothing more, nothing less",
                     found == expected, " (found {})".format(found))
        dynamic_ones = {e.path for e in endpoints if e.dynamic}
        suite.check("both {user_id} routes are marked dynamic", dynamic_ones == {"/api/users/{user_id}"})
        suite.check("no warnings", warnings == ())
    finally:
        proj.__exit__(None, None, None)


# --- Python/FastAPI server-start discovery ----------------------------

RUN_PY_WITH_UVICORN = (
    "import uvicorn\n\n"
    'if __name__ == "__main__":\n'
    '    uvicorn.run("app.main:app", host="127.0.0.1", port=8000)\n'
)


def test_python_start_command_finds_run_py_with_uvicorn_run(suite):
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "run.py": RUN_PY_WITH_UVICORN,
        "app/main.py": MAIN_PY_SIMPLE,
    })
    try:
        command, evidence, _cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("a real command was found", command is not None)
        suite.check("run.py is the file actually invoked", command is not None and command[-1] == "run.py")
        suite.check("evidence names the real reason", "uvicorn.run" in evidence)
    finally:
        proj.__exit__(None, None, None)


def test_python_start_command_never_picks_a_file_that_does_not_call_uvicorn_run(suite):
    """The exact real bug the audit found: a file merely named like an
    entry point (app/main.py) must never be assumed runnable just because
    of its name - only a file that really contains uvicorn.run(...).
    """
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "app/main.py": MAIN_PY_SIMPLE,  # defines `app`, never calls uvicorn.run()
    })
    try:
        entrypoint = server_module._find_uvicorn_run_entrypoint(proj.path, context.project)
        suite.check("app/main.py is correctly NOT selected as a uvicorn.run() entrypoint", entrypoint is None)
    finally:
        proj.__exit__(None, None, None)


def test_python_start_command_falls_back_to_module_style_app(suite):
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "app/main.py": MAIN_PY_SIMPLE,  # no run.py anywhere; main.py has `app = FastAPI()`
    })
    try:
        command, evidence, _cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("a real command was found via the module fallback", command is not None)
        suite.check("uses -m uvicorn", command is not None and "-m" in command and "uvicorn" in command)
        suite.check("names the real module:app target", command is not None and "app.main:app" in command)
        suite.check("evidence explains the fallback", "FastAPI() instantiation" in evidence)
    finally:
        proj.__exit__(None, None, None)


def test_python_start_command_prefers_uvicorn_run_entrypoint_over_module_fallback(suite):
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "run.py": RUN_PY_WITH_UVICORN,
        "app/main.py": MAIN_PY_SIMPLE,  # also has a real FastAPI() - fallback would also work
    })
    try:
        command, evidence, _cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("the real runner script wins, not the module fallback",
                     command is not None and command[-1] == "run.py")
    finally:
        proj.__exit__(None, None, None)


def test_python_start_command_reports_missing_entrypoint_evidence(suite):
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "app/models.py": "class X:\n    pass\n",  # FastAPI detected, but no runnable evidence anywhere
    })
    try:
        command, reason, _cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("no command found", command is None)
        suite.check("a clear, honest reason is given", "no runnable entrypoint" in reason)
    finally:
        proj.__exit__(None, None, None)


def test_discover_server_start_command_prefers_js_over_python_strategy(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}}),
        "package-lock.json": "{}",
        "requirements.txt": _fastapi_requirements(),  # both present - a hybrid/monorepo edge case
    })
    try:
        command, evidence, _cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("JS strategy wins when a JS package manager is detected", command == ["npm", "run", "dev"])
    finally:
        proj.__exit__(None, None, None)


def test_discover_server_start_command_falls_through_to_python_when_no_js(suite):
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "run.py": RUN_PY_WITH_UVICORN,
        "app/main.py": MAIN_PY_SIMPLE,
    })
    try:
        command, evidence, _cwd = server_module.discover_server_start_command(proj.path, context.project)
        suite.check("falls through to the Python strategy with no JS evidence at all",
                     command is not None and command[-1] == "run.py")
    finally:
        proj.__exit__(None, None, None)


# --- interpreter / virtualenv selection --------------------------------

def test_select_interpreter_prefers_dot_venv_when_present(suite):
    proj = TempProject()
    try:
        if server_module._IS_WINDOWS:
            venv_python = proj.path / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = proj.path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("", encoding="utf-8")
        selected = server_module._select_interpreter(proj.path)
        suite.check("the real, on-disk venv interpreter is selected", selected == str(venv_python))
    finally:
        proj.__exit__(None, None, None)


def test_select_interpreter_recognizes_plain_venv_directory_too(suite):
    proj = TempProject()
    try:
        if server_module._IS_WINDOWS:
            venv_python = proj.path / "venv" / "Scripts" / "python.exe"
        else:
            venv_python = proj.path / "venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("", encoding="utf-8")
        selected = server_module._select_interpreter(proj.path)
        suite.check("the plain venv/ layout is recognized too", selected == str(venv_python))
    finally:
        proj.__exit__(None, None, None)


def test_select_interpreter_falls_back_to_bare_python_without_a_venv(suite):
    proj = TempProject()
    try:
        selected = server_module._select_interpreter(proj.path)
        suite.check("falls back to bare 'python', never a hard failure", selected == "python")
    finally:
        proj.__exit__(None, None, None)


def test_python_start_command_uses_the_projects_own_venv_not_a_bare_python(suite):
    """Checked at the level of the two real building blocks
    (`_select_interpreter` + `_find_uvicorn_run_entrypoint`) rather than
    through the full `discover_server_start_command` pipeline, since a
    placeholder (non-executable) fake venv `python.exe` would correctly,
    honestly fail the real dependency-availability gate that function now
    also enforces - that gate is proven separately and correctly by
    `test_check_python_dependencies_missing`/`..._deterministically`; this
    test is specifically about interpreter *selection and preference*.
    """
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "run.py": RUN_PY_WITH_UVICORN,
        "app/main.py": MAIN_PY_SIMPLE,
    })
    try:
        if server_module._IS_WINDOWS:
            venv_python = proj.path / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = proj.path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("", encoding="utf-8")

        interpreter = server_module._select_interpreter(proj.path)
        entrypoint = server_module._find_uvicorn_run_entrypoint(proj.path, context.project)
        suite.check(
            "the project's own venv interpreter would be used, never a bare 'python'",
            interpreter == str(venv_python),
        )
        suite.check("run.py is the real entrypoint that would be invoked", entrypoint == "run.py")
    finally:
        proj.__exit__(None, None, None)


# --- dependency validation ----------------------------------------------

def test_check_python_dependencies_available(suite):
    ok, reason = server_module.check_python_dependencies(sys.executable, modules=("os", "sys"))
    suite.check("real, always-available stdlib modules report ok", ok is True)
    suite.check("a clear, positive reason is given", "import successfully" in reason)


def test_check_python_dependencies_missing(suite):
    ok, reason = server_module.check_python_dependencies(
        sys.executable, modules=("definitely_not_a_real_package_xyz",))
    suite.check("a genuinely missing package reports not ok", ok is False)
    suite.check("the real ModuleNotFoundError is captured as evidence", "definitely_not_a_real_package_xyz" in reason)


def test_check_python_dependencies_handles_a_nonexistent_interpreter(suite):
    ok, reason = server_module.check_python_dependencies("definitely-not-a-real-interpreter-xyz")
    suite.check("a missing interpreter reports not ok, never raises", ok is False)
    suite.check("a clear reason is given", "does not exist" in reason or "not runnable" in reason)


def test_python_start_command_reports_missing_dependency_deterministically(suite):
    """The real audit finding, reproduced deterministically regardless of
    what happens to be reachable via this machine's own ambient PATH (a
    real, separate per-user Python install with fastapi/uvicorn already on
    it was found reachable via bare "python" on the machine this was
    developed on - a real illustration of exactly why this gate matters):
    real entrypoint evidence exists, but the interpreter cannot actually
    import fastapi/uvicorn - must be reported as a clear, specific
    failure, never silently attempted anyway. `check_python_dependencies`
    itself is proven correct and deterministic separately, honestly, by
    `test_check_python_dependencies_missing` (a real subprocess call, a
    real guaranteed-missing module name) - this test proves `discover_
    server_start_command`'s own wiring to it, via a controlled substitute
    so the result never depends on this machine's own ambient installs.
    """
    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "run.py": RUN_PY_WITH_UVICORN,
        "app/main.py": MAIN_PY_SIMPLE,
    })
    try:
        original = server_module.check_python_dependencies
        server_module.check_python_dependencies = lambda interpreter, modules=(): (
            False, "fastapi, uvicorn not importable via '{}': No module named 'fastapi'".format(interpreter)
        )
        try:
            command, reason, _cwd = server_module.discover_server_start_command(proj.path, context.project)
        finally:
            server_module.check_python_dependencies = original
        suite.check("no command is returned when dependencies are missing", command is None)
        suite.check("the real entrypoint evidence is still named", "uvicorn.run" in reason)
        suite.check("the real missing-dependency reason is named", "not importable" in reason)
    finally:
        proj.__exit__(None, None, None)


# --- one real, guarded, full end-to-end proof ---------------------------

REAL_UVICORN_APP_PY = (
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n\n"
    '@app.get("/health")\n'
    "def health():\n"
    '    return {"status": "ok"}\n'
)

REAL_RUN_PY = (
    "import uvicorn\n\n"
    'if __name__ == "__main__":\n'
    '    uvicorn.run("app:app", host="127.0.0.1", port=8123)\n'
)


def test_real_end_to_end_fastapi_server_via_discovered_command(suite):
    """Guarded exactly like test_runtime_execution.py's own real npm-based
    tests: real only when a real FastAPI/uvicorn stack is actually
    reachable via `sys.executable` in whatever environment runs this
    suite; skipped cleanly otherwise. The authoritative real-world proof
    for this whole step is the separate, real external demo project
    verification (docs/32's own dogfooding section), not this test.
    """
    if not _fastapi_stack_available():
        suite.check("(skipped: fastapi/uvicorn not importable via this environment's own interpreter)", True)
        return

    from qa_agent.api_qa import discover_api_endpoints as _discover
    from qa_agent.api_qa import server as _server

    context, proj = _context_for({
        "requirements.txt": _fastapi_requirements(),
        "run.py": REAL_RUN_PY,
        "app.py": REAL_UVICORN_APP_PY,
    })
    try:
        endpoints, _ = _discover(context, proj.path)
        suite.check("the real /health route was discovered", any(e.path == "/health" for e in endpoints))

        command, evidence, _cwd = _server.discover_server_start_command(proj.path, context.project)
        suite.check("a real command was found", command is not None, " ({})".format(evidence))

        handle = _server.start_and_wait_ready(command, proj.path, timeout=20)
        try:
            suite.check("the real server became ready", handle.status == _server.STATUS_READY,
                         " (was {}: {})".format(handle.status, handle.reason))
            suite.check("the real base URL was observed", "8123" in handle.base_url)

            from qa_agent.api_qa.http_client import call_endpoint
            from qa_agent.api_qa.models import ApiEndpoint, CALL_PASS
            health_endpoint = ApiEndpoint(method="GET", path="/health", source_file="app.py")
            result = call_endpoint(handle.base_url, health_endpoint, timeout=10)
            suite.check("a real HTTP call reached the real server and passed", result.status == CALL_PASS,
                         " (was {}: {})".format(result.status, result.error or result.reason))
        finally:
            handle.stop()
        suite.check("the real server process was actually stopped", handle.proc.poll() is not None)
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: Python/FastAPI discovery and server startup")
    sys.exit(suite.run([
        test_fastapi_discovery_finds_get_route,
        test_fastapi_discovery_finds_post_route,
        test_fastapi_discovery_finds_router_route,
        test_fastapi_discovery_preserves_dynamic_path_parameter,
        test_fastapi_discovery_never_invents_a_parameter_value,
        test_fastapi_discovery_tracks_line_numbers,
        test_fastapi_discovery_gated_behind_real_fastapi_detection,
        test_fastapi_discovery_ignores_venv_directories,
        test_fastapi_discovery_does_not_affect_nextjs_discovery,
        test_fastapi_discovery_against_the_real_demo_shaped_fixture,
        test_python_start_command_finds_run_py_with_uvicorn_run,
        test_python_start_command_never_picks_a_file_that_does_not_call_uvicorn_run,
        test_python_start_command_falls_back_to_module_style_app,
        test_python_start_command_prefers_uvicorn_run_entrypoint_over_module_fallback,
        test_python_start_command_reports_missing_entrypoint_evidence,
        test_discover_server_start_command_prefers_js_over_python_strategy,
        test_discover_server_start_command_falls_through_to_python_when_no_js,
        test_select_interpreter_prefers_dot_venv_when_present,
        test_select_interpreter_recognizes_plain_venv_directory_too,
        test_select_interpreter_falls_back_to_bare_python_without_a_venv,
        test_python_start_command_uses_the_projects_own_venv_not_a_bare_python,
        test_check_python_dependencies_available,
        test_check_python_dependencies_missing,
        test_check_python_dependencies_handles_a_nonexistent_interpreter,
        test_python_start_command_reports_missing_dependency_deterministically,
        test_real_end_to_end_fastapi_server_via_discovered_command,
    ]))
