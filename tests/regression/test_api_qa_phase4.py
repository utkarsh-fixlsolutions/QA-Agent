"""Phase 4 (generalized API discovery and understanding): Express
`.route().verb()` chains, honest unresolved-route reporting, Express/
FastAPI router-mount composition, OpenAPI as an independent discovery
source (JSON and YAML), unsupported-framework reporting, and the runner-
level changes (zero discovery never blocks server startup; a known-bad
server precondition - e.g. a failed dependency install - is reported as
the primary failure rather than masked by a doomed startup attempt).

Every fixture here is synthetic and hand-built to exercise one general
programming idiom - never copied from, or named after, any specific real
project (docs/56's own explicit anti-special-case principle). Real
projects (idurar-erp-crm, lms-ai) are validation targets for this same
code, exercised separately, not hard-coded into this suite's own logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import discovery as discovery_module  # noqa: E402
from qa_agent.api_qa import route_composition  # noqa: E402
from qa_agent.api_qa import (  # noqa: E402
    SERVER_SKIPPED,
    SERVER_START_FAILED,
    discover_api_endpoints,
    run_api_qa,
)


def _context_for(files):
    proj = TempProject()
    for rel, text in files.items():
        proj.write(rel, text)
    result = discover_project(proj.path)
    context = build_repository_context(result.project)
    return context, proj


def _detailed_for(files):
    context, proj = _context_for(files)
    outcome = discovery_module.discover_api_endpoints_detailed(context, proj.path)
    return outcome, proj


# --- Express .route().verb() chains -----------------------------------

def test_express_route_chain_get_and_post(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "server.js": (
            "const router = require('express').Router();\n"
            "router.route('/items').get(list).post(create);\n"
            "function list(req, res) {}\n"
            "function create(req, res) {}\n"
        ),
    })
    try:
        by_key = {(e.method, e.path): e for e in outcome.endpoints}
        suite.check("GET /items discovered via .route() chain", ("GET", "/items") in by_key)
        suite.check("POST /items discovered via the same chain", ("POST", "/items") in by_key)
    finally:
        proj.__exit__(None, None, None)


def test_express_route_chain_patch_and_delete_with_dynamic_segment(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "server.js": (
            "const router = require('express').Router();\n"
            "router.route('/items/:id').patch(update).delete(remove);\n"
            "function update(req, res) {}\n"
            "function remove(req, res) {}\n"
        ),
    })
    try:
        by_key = {(e.method, e.path): e for e in outcome.endpoints}
        suite.check("PATCH /items/:id discovered", ("PATCH", "/items/:id") in by_key)
        suite.check("DELETE /items/:id discovered", ("DELETE", "/items/:id") in by_key)
        suite.check("both are marked dynamic", all(e.dynamic for e in outcome.endpoints))
    finally:
        proj.__exit__(None, None, None)


def test_express_route_chain_does_not_break_existing_direct_calls(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "server.js": (
            "const app = require('express')();\n"
            "app.get('/health', (req, res) => res.json({}));\n"
            "app.route('/items').post(create);\n"
            "function create(req, res) {}\n"
        ),
    })
    try:
        by_key = {(e.method, e.path): e for e in outcome.endpoints}
        suite.check("direct app.get(...) still discovered", ("GET", "/health") in by_key)
        suite.check("chained app.route(...).post(...) also discovered", ("POST", "/items") in by_key)
    finally:
        proj.__exit__(None, None, None)


# --- unresolved routes --------------------------------------------------

def test_unresolved_template_literal_route(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "server.js": (
            "const router = require('express').Router();\n"
            "const entity = 'widgets';\n"
            "router.route(`/${entity}/create`).post(create);\n"
            "function create(req, res) {}\n"
        ),
    })
    try:
        suite.check("no fabricated endpoint was produced", outcome.endpoints == ())
        suite.check("exactly one unresolved route recorded", len(outcome.unresolved_routes) == 1,
                    " ({})".format(len(outcome.unresolved_routes)))
        if outcome.unresolved_routes:
            r = outcome.unresolved_routes[0]
            suite.check("method is known (POST)", r.method == "POST")
            suite.check("raw expression is the real template text", "${entity}" in r.raw_expression)
            suite.check("reason explains why", "static" in r.reason.lower())
    finally:
        proj.__exit__(None, None, None)


def test_unresolved_variable_path_route(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "server.js": (
            "const router = require('express').Router();\n"
            "const pathVariable = computePath();\n"
            "router.get(pathVariable, handler);\n"
            "function handler(req, res) {}\n"
        ),
    })
    try:
        suite.check("no fabricated endpoint was produced", outcome.endpoints == ())
        suite.check("exactly one unresolved route recorded", len(outcome.unresolved_routes) == 1,
                    " ({})".format(len(outcome.unresolved_routes)))
        if outcome.unresolved_routes:
            r = outcome.unresolved_routes[0]
            suite.check("method is known (GET)", r.method == "GET")
            suite.check("raw expression names the real variable", r.raw_expression == "pathVariable")
    finally:
        proj.__exit__(None, None, None)


# --- Express router composition -----------------------------------------

def test_express_simple_mount_prefix(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node app.js"}, "dependencies": {"express": "4.19.0"}}),
        "app.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "const userRouter = require('./routes/users');\n"
            "app.use('/api', userRouter);\n"
        ),
        "routes/users.js": (
            "const router = require('express').Router();\n"
            "router.get('/users', (req, res) => res.json([]));\n"
            "module.exports = router;\n"
        ),
    })
    try:
        by_key = {(e.method, e.path): e for e in outcome.endpoints}
        suite.check("mounted route resolved with its real prefix", ("GET", "/api/users") in by_key,
                    " ({})".format(list(by_key)))
        suite.check("raw, unprefixed path is not also reported", ("GET", "/users") not in by_key)
        if ("GET", "/api/users") in by_key:
            suite.check("provenance names the composition pass",
                        "express-composition" in by_key[("GET", "/api/users")].discovered_by)
    finally:
        proj.__exit__(None, None, None)


def test_express_mount_with_middleware_argument(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node app.js"}, "dependencies": {"express": "4.19.0"}}),
        "app.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "const auth = require('./middleware/auth');\n"
            "const userRouter = require('./routes/users');\n"
            "app.use('/api', auth.check, userRouter);\n"
        ),
        "middleware/auth.js": "module.exports = { check: (req, res, next) => next() };\n",
        "routes/users.js": (
            "const router = require('express').Router();\n"
            "router.get('/users', (req, res) => res.json([]));\n"
            "module.exports = router;\n"
        ),
    })
    try:
        by_key = {(e.method, e.path): e for e in outcome.endpoints}
        suite.check("middleware argument is not mistaken for the router",
                    ("GET", "/api/users") in by_key, " ({})".format(list(by_key)))
    finally:
        proj.__exit__(None, None, None)


def test_express_nested_router_composition(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node app.js"}, "dependencies": {"express": "4.19.0"}}),
        "app.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "const apiRouter = require('./routes/api');\n"
            "app.use('/api', apiRouter);\n"
        ),
        "routes/api.js": (
            "const router = require('express').Router();\n"
            "const userRouter = require('./users');\n"
            "router.use('/v1', userRouter);\n"
            "module.exports = router;\n"
        ),
        "routes/users.js": (
            "const router = require('express').Router();\n"
            "router.get('/users', (req, res) => res.json([]));\n"
            "module.exports = router;\n"
        ),
    })
    try:
        by_key = {(e.method, e.path): e for e in outcome.endpoints}
        suite.check("two levels of mount prefix both resolved", ("GET", "/api/v1/users") in by_key,
                    " ({})".format(list(by_key)))
    finally:
        proj.__exit__(None, None, None)


def test_express_circular_composition_terminates(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node app.js"}, "dependencies": {"express": "4.19.0"}}),
        "app.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "const aRouter = require('./routes/a');\n"
            "app.use('/root', aRouter);\n"
        ),
        "routes/a.js": (
            "const router = require('express').Router();\n"
            "const bRouter = require('./b');\n"
            "router.get('/a-route', (req, res) => res.json({}));\n"
            "router.use('/b', bRouter);\n"
            "module.exports = router;\n"
        ),
        "routes/b.js": (
            "const router = require('express').Router();\n"
            "const aRouter = require('./a');\n"
            "router.get('/b-route', (req, res) => res.json({}));\n"
            "router.use('/a', aRouter);\n"
            "module.exports = router;\n"
        ),
    })
    try:
        by_key = {(e.method, e.path): e for e in outcome.endpoints}
        suite.check("did not hang - discovery returned", True)
        suite.check("real route from the first file resolved", ("GET", "/root/a-route") in by_key,
                    " ({})".format(list(by_key)))
        suite.check("real route from the second file resolved", ("GET", "/root/b/b-route") in by_key,
                    " ({})".format(list(by_key)))
        suite.check("no duplicate/infinite entries", len(outcome.endpoints) == 2, " ({})".format(len(outcome.endpoints)))
        suite.check("a circular-mount warning was recorded",
                    any("circular" in w.lower() for w in outcome.warnings), " ({})".format(outcome.warnings))
    finally:
        proj.__exit__(None, None, None)


# --- FastAPI router composition ------------------------------------------

def test_fastapi_include_router_with_prefix(suite):
    outcome, proj = _detailed_for({
        "requirements.txt": "fastapi\nuvicorn\n",
        "main.py": (
            "from fastapi import FastAPI\n"
            "from .routers import users\n"
            "app = FastAPI()\n"
            "app.include_router(users.router, prefix=\"/api/users\")\n"
        ),
        "routers/__init__.py": "",
        "routers/users.py": (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get(\"/{id}\")\n"
            "def get_user(id: int):\n"
            "    return {\"id\": id}\n"
        ),
    })
    try:
        by_key = {(e.method, e.path): e for e in outcome.endpoints}
        suite.check("mounted FastAPI route resolved with its real prefix",
                    ("GET", "/api/users/{id}") in by_key, " ({})".format(list(by_key)))
        suite.check("raw, unprefixed path is not also reported", ("GET", "/{id}") not in by_key)
    finally:
        proj.__exit__(None, None, None)


# --- OpenAPI as an independent discovery source --------------------------

_OPENAPI_DOC = {
    "openapi": "3.0.0",
    "paths": {
        "/pets": {
            "get": {"summary": "list pets"},
            "post": {"summary": "create a pet"},
        },
        "/pets/{id}": {
            "get": {"summary": "get one pet"},
        },
    },
}


def test_openapi_json_discovers_endpoints_with_zero_source_discovery(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}}),
        "server.js": "// no express, no routes - a project this module cannot read source routes from\n",
        "openapi.json": json.dumps(_OPENAPI_DOC),
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        by_key = {(e.method, e.path): e for e in endpoints}
        suite.check("GET /pets discovered from OpenAPI alone", ("GET", "/pets") in by_key)
        suite.check("POST /pets discovered from OpenAPI alone", ("POST", "/pets") in by_key)
        suite.check("dynamic GET /pets/{id} discovered and marked dynamic",
                    ("GET", "/pets/{id}") in by_key and by_key[("GET", "/pets/{id}")].dynamic is True)
    finally:
        proj.__exit__(None, None, None)


def test_openapi_yaml_discovers_endpoints(suite):
    yaml_text = (
        "openapi: 3.0.0\n"
        "paths:\n"
        "  /pets:\n"
        "    get:\n"
        "      summary: list pets\n"
        "  /pets/{id}:\n"
        "    delete:\n"
        "      summary: remove a pet\n"
    )
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}}),
        "server.js": "// no express\n",
        "openapi.yaml": yaml_text,
    })
    try:
        endpoints, _ = discover_api_endpoints(context, proj.path)
        by_key = {(e.method, e.path): e for e in endpoints}
        suite.check("GET /pets discovered from YAML", ("GET", "/pets") in by_key)
        suite.check("DELETE /pets/{id} discovered from YAML", ("DELETE", "/pets/{id}") in by_key)
    finally:
        proj.__exit__(None, None, None)


def test_source_and_openapi_endpoints_are_fused_not_duplicated(suite):
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0"}}),
        "server.js": (
            "const app = require('express')();\n"
            "app.get('/pets', (req, res) => res.json([]));\n"
        ),
        "openapi.json": json.dumps({"openapi": "3.0.0", "paths": {"/pets": {"get": {}}}}),
    })
    try:
        matches = [e for e in outcome.endpoints if (e.method, e.path) == ("GET", "/pets")]
        suite.check("exactly one endpoint, not two", len(matches) == 1, " ({})".format(len(matches)))
        if matches:
            suite.check("provenance names both sources",
                        "express-source" in matches[0].discovered_by and "openapi" in matches[0].discovered_by,
                        " ({})".format(matches[0].discovered_by))
    finally:
        proj.__exit__(None, None, None)


def test_malformed_openapi_file_reports_a_warning_not_a_crash(suite):
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}}),
        "openapi.json": "{ this is not valid json",
    })
    try:
        endpoints, warnings = discover_api_endpoints(context, proj.path)
        suite.check("no crash, no endpoints fabricated", endpoints == ())
        suite.check("a real warning names the malformed file",
                    any("openapi.json" in w and "not valid" in w for w in warnings), " ({})".format(warnings))
    finally:
        proj.__exit__(None, None, None)


# --- unsupported-framework reporting --------------------------------------

def test_unsupported_framework_reported_distinctly_from_no_apis(suite):
    outcome, proj = _detailed_for({
        "requirements.txt": "Flask==3.0.0\n",
        "app.py": "from flask import Flask\napp = Flask(__name__)\n",
    })
    try:
        suite.check("Flask is named as unsupported", "Flask" in outcome.unsupported_frameworks,
                    " ({})".format(outcome.unsupported_frameworks))
        suite.check("zero endpoints, but for a distinct, explained reason", outcome.endpoints == ())
    finally:
        proj.__exit__(None, None, None)


# --- runner-level behavior -------------------------------------------------

def test_zero_discovered_endpoints_does_not_block_server_startup(suite):
    """item 8, Case A: 0 static endpoints must not, by itself, prevent a
    real startable command from actually being attempted - a plain Node
    project with a real `start` script and no recognizable routes.
    """
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}}),
        "package-lock.json": json.dumps({"name": "x", "lockfileVersion": 3}),
        "server.js": (
            "const http = require('http');\n"
            "http.createServer((req, res) => { res.end('ok'); }).listen(0, () => console.log('listening'));\n"
        ),
    })
    try:
        result = run_api_qa(context, proj.path)
        suite.check("zero endpoints were discovered", result.endpoints == ())
        suite.check("server status is not the old blanket SKIPPED - a real command was actually tried",
                     result.server_status != SERVER_SKIPPED, " (was {})".format(result.server_status))
        suite.check("the server_detail is framework-neutral, no hardcoded 'Next.js App Router' text",
                     "Next.js App Router" not in (result.server_detail or ""))
    finally:
        proj.__exit__(None, None, None)


def test_precondition_failure_skips_server_start_and_preserves_the_real_reason(suite):
    """item 9: a known-bad precondition (a failed dependency install) must
    report as the primary failure, never masked by a doomed startup
    attempt against missing dependencies.
    """
    context, proj = _context_for({
        "package.json": json.dumps({"name": "x", "scripts": {"dev": "next dev"}, "dependencies": {"next": "15.0.0"}}),
        "package-lock.json": json.dumps({"name": "x", "lockfileVersion": 3}),
        "app/api/health/route.ts": "export async function GET() { return Response.json({ok: true}); }\n",
    })
    try:
        real_reason = "dependency installation failed (npm install in x): npm ERR! network timeout"
        result = run_api_qa(context, proj.path, precondition_failure=real_reason)
        suite.check("discovery still ran and is still honestly reported", len(result.endpoints) == 1)
        suite.check("server_status reflects the real precondition failure", result.server_status == SERVER_START_FAILED)
        suite.check("the real install failure text is preserved verbatim", result.server_detail == real_reason)
        suite.check("no secondary, unrelated startup error replaced it",
                     "is not recognized" not in result.server_detail)
        suite.check("every endpoint's call is skipped with the same real reason",
                     all(c.status == "skipped" and c.reason == real_reason for c in result.calls))
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA Phase 4: generalized discovery")
    raise SystemExit(suite.run([
        test_express_route_chain_get_and_post,
        test_express_route_chain_patch_and_delete_with_dynamic_segment,
        test_express_route_chain_does_not_break_existing_direct_calls,
        test_unresolved_template_literal_route,
        test_unresolved_variable_path_route,
        test_express_simple_mount_prefix,
        test_express_mount_with_middleware_argument,
        test_express_nested_router_composition,
        test_express_circular_composition_terminates,
        test_fastapi_include_router_with_prefix,
        test_openapi_json_discovers_endpoints_with_zero_source_discovery,
        test_openapi_yaml_discovers_endpoints,
        test_source_and_openapi_endpoints_are_fused_not_duplicated,
        test_malformed_openapi_file_reports_a_warning_not_a_crash,
        test_unsupported_framework_reported_distinctly_from_no_apis,
        test_zero_discovered_endpoints_does_not_block_server_startup,
        test_precondition_failure_skips_server_start_and_preserves_the_real_reason,
    ]))
