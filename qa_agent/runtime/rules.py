"""Evidence -> `RuntimeCheck` rules (Phase G Part 1, docs/21-runtime-qa
-planning-engine.md). Every function here is pure: `RepositoryContext` in,
zero or more `RuntimeCheck`s out - no filesystem access, no subprocess, no
AI, no network, no global state, no randomness.

**One deliberate, documented distinction from Part 1/2's own evidence
philosophy, stated plainly because it matters:** in `ProjectKnowledge`/
`RepositoryContext`, evidence proves a fact is *true* ("React is used,
because package.json says so"). Here, evidence proves a check is *worth
planning* - it does not claim the checked behavior already exists or
already works. "FastAPI detected via requirements.txt" is real, honest
evidence that verifying a health endpoint is a *relevant* thing to plan for
this project - not a claim that a health endpoint already exists. That is
the entire point of a planning engine: it names what should be verified
before anything has been run, exactly like a QA engineer would from reading
a project's stack alone, never a claim about the outcome of a check that
has not happened yet.

**An equally deliberate, documented scope boundary:** roughly half of the
category list this phase's own specifying prompt named as examples -
Database Connectivity, Prisma/migration verification, Authentication/
Authorization specifics, WebSockets, Caching, Message Queues, Cron Jobs,
Feature Flags, Rate Limiting, CORS, Security Headers, Session/Cookie
Handling, File Uploads - have **no underlying evidence detector anywhere in
`ProjectKnowledge`/`RepositoryContext` today**. Building those rules here
would mean either inventing evidence that does not exist or silently
breaking the "never fabricate" rule this whole engine is built on. None of
them are implemented below; extending Part 1's evidence detectors (known
auth-library dependencies, ORM manifests, infra-service config) is real,
separate, deferred work - flagged explicitly, not silently skipped.
"""

from __future__ import annotations

from .models import (
    COST_HIGH,
    COST_LOW,
    COST_MEDIUM,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_MEDIUM,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RuntimeCheck,
)

_BACKEND_FRAMEWORKS = frozenset({
    "Express", "NestJS", "FastAPI", "Flask", "Django", "Spring Boot", "Laravel", "ASP.NET",
})
_FRONTEND_FRAMEWORKS = frozenset({"React", "Vue", "Angular", "Svelte", "Next.js", "Nuxt"})


def _basename(path):
    return path.rsplit("/", 1)[-1]


def _dir_names(project):
    return {_basename(d) for d in project.important_directories}


def server_startup_checks(context):
    project = context.project
    backend = sorted(i.name for i in project.frameworks if i.name in _BACKEND_FRAMEWORKS)
    evidence = tuple(project.runtime_files) or tuple(
        i.evidence[0] for i in project.frameworks if i.name in _BACKEND_FRAMEWORKS
    )
    if not evidence:
        return ()
    reason = (
        "Backend framework(s) detected ({}) and/or a real runtime entry point exists"
        .format(", ".join(backend)) if backend else
        "A real runtime entry point exists ({})".format(", ".join(project.runtime_files))
    )
    return (RuntimeCheck(
        id="server-startup",
        name="Server Startup",
        category="Infrastructure",
        priority=PRIORITY_CRITICAL,
        reason=reason,
        required_evidence=evidence,
        estimated_cost=COST_LOW,
        risk_level=RISK_HIGH,
        blocking=True,
        expected_result="The server process starts and stays running without an immediate crash or unhandled exception.",
    ),)


def environment_configuration_checks(context):
    project = context.project
    if not project.environment_files:
        return ()
    return (RuntimeCheck(
        id="environment-configuration",
        name="Environment Configuration",
        category="Configuration",
        priority=PRIORITY_HIGH,
        reason="Environment file(s) present: {}".format(", ".join(project.environment_files)),
        required_evidence=project.environment_files,
        estimated_cost=COST_LOW,
        risk_level=RISK_HIGH,
        blocking=True,
        expected_result="Every environment variable the application reads at startup has a real value available, with no silent fallback to an unset default.",
    ),)


def api_endpoint_checks(context):
    project = context.project
    backend = sorted(i.name for i in project.frameworks if i.name in _BACKEND_FRAMEWORKS)
    api_dirs = tuple(d for d in project.important_directories if _basename(d) == "api")
    api_route_files = tuple(f for f in project.important_files if "/api/" in ("/" + f))
    backend_evidence = tuple(i.evidence[0] for i in project.frameworks if i.name in _BACKEND_FRAMEWORKS)

    evidence = api_route_files or api_dirs or backend_evidence
    if not evidence:
        return ()

    reason_parts = []
    if "Next.js" in {i.name for i in project.frameworks} and api_route_files:
        reason_parts.append("Next.js API routes found under app/api or pages/api")
    if backend:
        reason_parts.append("backend framework(s) detected: {}".format(", ".join(backend)))
    if api_dirs and not reason_parts:
        reason_parts.append("an api/ directory exists")

    return (RuntimeCheck(
        id="api-endpoints",
        name="API Endpoints",
        category="API",
        priority=PRIORITY_CRITICAL,
        reason="; ".join(reason_parts) or "API-shaped evidence present",
        required_evidence=evidence,
        dependencies=("server-startup",),
        estimated_cost=COST_MEDIUM,
        risk_level=RISK_HIGH,
        expected_result="Every discovered endpoint returns its documented/expected status code for a valid request and a well-formed error for an invalid one.",
    ),)


def middleware_checks(context):
    project = context.project
    middleware_files = tuple(f for f in project.runtime_files if _basename(f) in ("middleware.ts", "middleware.js"))
    middleware_dirs = tuple(d for d in project.important_directories if _basename(d) == "middleware")
    evidence = middleware_files + middleware_dirs
    if not evidence:
        return ()
    return (RuntimeCheck(
        id="middleware",
        name="Middleware",
        category="API",
        priority=PRIORITY_HIGH,
        reason="Middleware evidence found: {}".format(", ".join(evidence)),
        required_evidence=evidence,
        dependencies=("server-startup",),
        estimated_cost=COST_MEDIUM,
        risk_level=RISK_HIGH,
        expected_result="Middleware executes in the correct order for a real request, applying auth/redirect/rewrite logic exactly as configured - verified against both an allowed and a denied case.",
    ),)


def frontend_route_checks(context):
    project = context.project
    frontend = sorted(i.name for i in project.frameworks if i.name in _FRONTEND_FRAMEWORKS)
    route_dirs = tuple(d for d in project.important_directories if d.rsplit("/", 1)[-1] in ("pages", "app"))
    if not (frontend and route_dirs):
        return ()
    return (RuntimeCheck(
        id="frontend-routes",
        name="Frontend Routes",
        category="Frontend",
        priority=PRIORITY_HIGH,
        reason="Frontend framework(s) {} with route directory evidence: {}".format(
            ", ".join(frontend), ", ".join(route_dirs)
        ),
        required_evidence=route_dirs,
        dependencies=("server-startup",),
        estimated_cost=COST_MEDIUM,
        risk_level=RISK_MEDIUM,
        expected_result="Every discovered route renders without a client- or server-side error, for both a signed-in and an anonymous visitor where relevant.",
    ),)


def static_asset_checks(context):
    project = context.project
    dirs = tuple(d for d in project.important_directories if d.rsplit("/", 1)[-1] in ("public", "assets", "static"))
    if not dirs:
        return ()
    return (RuntimeCheck(
        id="static-assets",
        name="Static Assets",
        category="Frontend",
        priority=PRIORITY_LOW,
        reason="Static asset director(y/ies) present: {}".format(", ".join(dirs)),
        required_evidence=dirs,
        estimated_cost=COST_LOW,
        risk_level=RISK_LOW,
        expected_result="Every static asset referenced by the application is served with a 200 and the correct content type.",
    ),)


def build_verification_checks(context):
    project = context.project
    evidence = tuple(item.evidence[0] for item in project.build_systems)
    if not evidence and project.configuration_files and project.package_managers:
        evidence = project.configuration_files
    if not evidence:
        return ()
    return (RuntimeCheck(
        id="build-verification",
        name="Build Verification",
        category="Build & CI",
        priority=PRIORITY_HIGH,
        reason="Build tooling and/or package manifest(s) present: {}".format(", ".join(evidence)),
        required_evidence=evidence,
        estimated_cost=COST_MEDIUM,
        risk_level=RISK_HIGH,
        blocking=True,
        expected_result="A clean build completes with exit code 0 and produces the expected output artifact(s).",
    ),)


def ci_checks(context):
    project = context.project
    if not project.ci_files:
        return ()
    return (RuntimeCheck(
        id="ci-pipeline",
        name="CI",
        category="Build & CI",
        priority=PRIORITY_MEDIUM,
        reason="CI configuration present: {}".format(", ".join(project.ci_files)),
        required_evidence=project.ci_files,
        dependencies=("build-verification",),
        estimated_cost=COST_LOW,
        risk_level=RISK_MEDIUM,
        expected_result="The configured CI pipeline runs to completion on a real commit, with every declared step passing or failing for a real, attributable reason.",
    ),)


def openapi_checks(context):
    project = context.project
    if not project.specification_files:
        return ()
    return (RuntimeCheck(
        id="openapi-availability",
        name="OpenAPI Availability",
        category="API",
        priority=PRIORITY_MEDIUM,
        reason="Specification file(s) present: {}".format(", ".join(project.specification_files)),
        required_evidence=project.specification_files,
        estimated_cost=COST_LOW,
        risk_level=RISK_MEDIUM,
        expected_result="The specification file is valid (parses as OpenAPI/Swagger) and matches the API's real, currently-implemented routes.",
    ),)


def test_suite_checks(context):
    project = context.project
    if not project.test_directories:
        return ()
    return (RuntimeCheck(
        id="test-suite-verification",
        name="Test Suite Verification",
        category="Build & CI",
        priority=PRIORITY_MEDIUM,
        reason="Test director(y/ies) present: {}".format(", ".join(project.test_directories)),
        required_evidence=project.test_directories,
        dependencies=("build-verification",),
        estimated_cost=COST_MEDIUM,
        risk_level=RISK_MEDIUM,
        expected_result="The existing test suite runs to completion and every test's real pass/fail outcome is captured.",
    ),)


# --- Docker cluster --------------------------------------------------

def docker_checks(context):
    project = context.project
    if not project.docker_files:
        return ()
    evidence = project.docker_files
    return (
        RuntimeCheck(
            id="container-build",
            name="Container Build",
            category="Infrastructure",
            priority=PRIORITY_HIGH,
            reason="Docker configuration present: {}".format(", ".join(evidence)),
            required_evidence=evidence,
            estimated_cost=COST_HIGH,
            risk_level=RISK_HIGH,
            blocking=True,
            expected_result="`docker build` (or `docker compose build`) completes with exit code 0.",
        ),
        RuntimeCheck(
            id="container-startup",
            name="Container Startup",
            category="Infrastructure",
            priority=PRIORITY_CRITICAL,
            reason="Docker configuration present: {}".format(", ".join(evidence)),
            required_evidence=evidence,
            dependencies=("container-build",),
            estimated_cost=COST_HIGH,
            risk_level=RISK_HIGH,
            blocking=True,
            expected_result="The container starts, stays running, and its declared health check (if any) reports healthy.",
        ),
        RuntimeCheck(
            id="container-environment-validation",
            name="Container Environment Validation",
            category="Infrastructure",
            priority=PRIORITY_HIGH,
            reason="Docker configuration present: {}".format(", ".join(evidence)),
            required_evidence=evidence,
            dependencies=("container-startup",),
            estimated_cost=COST_MEDIUM,
            risk_level=RISK_MEDIUM,
            expected_result="Every environment variable the container's own config references is actually supplied to it at runtime.",
        ),
    )


# --- Next.js App Router cluster ---------------------------------------

def nextjs_app_router_checks(context):
    project = context.project
    framework_names = {i.name for i in project.frameworks}
    has_app_dir = "app" in _dir_names(project)
    if "Next.js" not in framework_names or not has_app_dir:
        return ()
    evidence = tuple(d for d in project.important_directories if d.rsplit("/", 1)[-1] == "app")
    return (RuntimeCheck(
        id="server-components",
        name="Server Component Rendering",
        category="Frontend",
        priority=PRIORITY_MEDIUM,
        reason="Next.js App Router detected (app/ directory present)",
        required_evidence=evidence,
        dependencies=("server-startup",),
        estimated_cost=COST_MEDIUM,
        risk_level=RISK_MEDIUM,
        expected_result="Every server component renders on the server without leaking a server-only value to the client bundle, and without an unhandled render-time error.",
    ),)


# --- FastAPI cluster ---------------------------------------------------

def fastapi_checks(context):
    project = context.project
    fastapi_items = [i for i in project.frameworks if i.name == "FastAPI"]
    if not fastapi_items:
        return ()
    evidence = fastapi_items[0].evidence
    common = dict(required_evidence=evidence, dependencies=("server-startup",), category="API")
    return (
        RuntimeCheck(
            id="fastapi-health-endpoint",
            name="Health Endpoint",
            priority=PRIORITY_HIGH,
            reason="FastAPI detected - a health endpoint is standard FastAPI production practice",
            estimated_cost=COST_LOW,
            risk_level=RISK_MEDIUM,
            expected_result="A health-check route (if one exists) responds 200 with no dependency on downstream services being up.",
            **common,
        ),
        RuntimeCheck(
            id="fastapi-openapi-docs",
            name="OpenAPI (auto-generated)",
            priority=PRIORITY_LOW,
            reason="FastAPI auto-generates an OpenAPI schema and /docs UI for every app",
            estimated_cost=COST_LOW,
            risk_level=RISK_LOW,
            expected_result="/openapi.json and /docs are reachable and the schema reflects the app's real routes.",
            **common,
        ),
        RuntimeCheck(
            id="fastapi-dependency-injection",
            name="Dependency Injection",
            priority=PRIORITY_MEDIUM,
            reason="FastAPI's dependency-injection system (Depends(...)) is core to how most FastAPI apps wire request-scoped resources",
            estimated_cost=COST_MEDIUM,
            risk_level=RISK_MEDIUM,
            expected_result="A route using Depends(...) resolves its dependencies correctly for both a valid and an invalid input.",
            **common,
        ),
        RuntimeCheck(
            id="fastapi-startup-events",
            name="Startup Events",
            priority=PRIORITY_MEDIUM,
            reason="FastAPI supports startup event handlers commonly used for resource initialization",
            estimated_cost=COST_LOW,
            risk_level=RISK_MEDIUM,
            expected_result="Any registered startup handler completes without raising before the app begins serving requests.",
            **common,
        ),
        RuntimeCheck(
            id="fastapi-shutdown-events",
            name="Shutdown Events",
            priority=PRIORITY_LOW,
            reason="FastAPI supports shutdown event handlers commonly used for resource cleanup",
            estimated_cost=COST_LOW,
            risk_level=RISK_LOW,
            expected_result="Any registered shutdown handler completes without raising when the app is stopped.",
            **common,
        ),
    )


# Every rule function, in the fixed order the planner runs them - order here
# has no effect on the final plan's recommended_order (planner.py computes
# that from priority + dependency depth), only on iteration, so this list
# itself is not a correctness-sensitive ordering.
ALL_RULES = (
    server_startup_checks,
    environment_configuration_checks,
    api_endpoint_checks,
    middleware_checks,
    frontend_route_checks,
    static_asset_checks,
    build_verification_checks,
    ci_checks,
    openapi_checks,
    test_suite_checks,
    docker_checks,
    nextjs_app_router_checks,
    fastapi_checks,
)
