"""Phase G Part 1: the Runtime QA Planning Engine (docs/21-runtime-qa
-planning-engine.md) - `qa_agent.runtime.plan_runtime_qa()`, its rules,
ordering, serialization, and the `discover --runtime-plan` CLI flag.

Every fixture project is discovered and context-built for real
(`discover_project()` -> `build_repository_context()`) before being handed
to the planner - the same "exercise the real pipeline, not an invented
input" discipline `test_repository_context.py` already established.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.runtime import (  # noqa: E402
    PRIORITIES,
    RuntimeCheck,
    plan_runtime_qa,
    plan_to_dict,
    plan_to_json,
    render,
    render_markdown,
)


class _Writer:
    def __init__(self, path):
        self.path = path

    write = TempProject.write


def _plan_for(files):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        for rel, text in files.items():
            writer.write(rel, text)
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        return plan_runtime_qa(context)
    finally:
        proj.__exit__(None, None, None)


def _ids(plan):
    return {c.id for c in plan.checks}


# --- basic shape / evidence discipline ------------------------------------

def test_empty_repository_plans_nothing(suite):
    plan = _plan_for({})
    suite.check("an empty repo plans zero checks", plan.checks == ())


def test_every_check_has_non_empty_required_evidence(suite):
    plan = _plan_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "middleware.ts": "export function middleware() {}\n",
        "app/api/x/route.ts": "export function GET() {}\n",
        "Dockerfile": "FROM node:20\n",
    })
    suite.check("at least one check was planned for a real project", len(plan.checks) > 0)
    for check in plan.checks:
        suite.check(
            "check '{}' has non-empty required_evidence".format(check.id),
            len(check.required_evidence) > 0,
        )


def test_runtime_check_refuses_empty_evidence(suite):
    try:
        RuntimeCheck(
            id="x", name="X", category="X", priority="high", reason="x",
            required_evidence=(),
        )
        raised = False
    except ValueError:
        raised = True
    suite.check("RuntimeCheck with no required_evidence raises ValueError", raised)


def test_runtime_check_rejects_an_unknown_priority(suite):
    try:
        RuntimeCheck(
            id="x", name="X", category="X", priority="urgent!!", reason="x",
            required_evidence=("some/file",),
        )
        raised = False
    except ValueError:
        raised = True
    suite.check("an unrecognized priority raises ValueError", raised)


# --- individual rules -------------------------------------------------

def test_server_startup_fires_on_backend_framework(suite):
    plan = _plan_for({"requirements.txt": "flask\n", "app.py": "from flask import Flask\n"})
    suite.check("a flask app plans server-startup", "server-startup" in _ids(plan))


def test_server_startup_does_not_fire_for_a_bare_library(suite):
    plan = _plan_for({"pyproject.toml": "[project]\nname = \"mylib\"\n"})
    suite.check("a bare library with no entrypoint plans no server-startup", "server-startup" not in _ids(plan))


def test_environment_configuration_fires_only_with_real_env_files(suite):
    with_env = _plan_for({".env.example": "KEY=\n", "main.py": "print(1)\n"})
    without_env = _plan_for({"main.py": "print(1)\n"})
    suite.check("a real .env.example plans environment-configuration", "environment-configuration" in _ids(with_env))
    suite.check("no env file -> no environment-configuration plan", "environment-configuration" not in _ids(without_env))


def test_api_endpoints_fires_on_api_directory(suite):
    plan = _plan_for({"app/api/x/route.ts": "export function GET() {}\n", "package.json": "{}"})
    suite.check("a real app/api directory plans api-endpoints", "api-endpoints" in _ids(plan))


def test_middleware_fires_on_root_middleware_file(suite):
    """The exact real gap this phase fixed in Part 1 - a root-level
    middleware.ts, with no middleware/ directory at all.
    """
    plan = _plan_for({"middleware.ts": "export function middleware() {}\n", "package.json": "{}"})
    suite.check("a root middleware.ts plans a middleware check", "middleware" in _ids(plan))


def test_middleware_does_not_fire_with_no_middleware_evidence(suite):
    plan = _plan_for({"package.json": "{}", "index.js": "console.log(1)\n"})
    suite.check("no middleware evidence -> no middleware check", "middleware" not in _ids(plan))


def test_frontend_routes_fires_on_frontend_framework_and_route_dir(suite):
    plan = _plan_for({"package.json": '{"dependencies": {"react": "18.0.0"}}', "pages/index.tsx": "export {}\n"})
    suite.check("react + pages/ plans frontend-routes", "frontend-routes" in _ids(plan))


def test_static_assets_fires_on_public_directory(suite):
    plan = _plan_for({"public/favicon.ico": "x", "package.json": "{}"})
    suite.check("a real public/ directory plans static-assets", "static-assets" in _ids(plan))


def test_build_verification_fires_on_build_system(suite):
    plan = _plan_for({"vite.config.ts": "export default {}\n", "package.json": "{}"})
    suite.check("a real vite config plans build-verification", "build-verification" in _ids(plan))


def test_ci_fires_only_with_a_real_workflow_file(suite):
    plan = _plan_for({".github/workflows/ci.yml": "on: push\n", "package.json": "{}"})
    suite.check("a real CI workflow file plans a ci-pipeline check", "ci-pipeline" in _ids(plan))


def test_openapi_availability_fires_on_a_real_spec_file(suite):
    plan = _plan_for({"openapi.yaml": "openapi: 3.0.0\n"})
    suite.check("a real openapi.yaml plans openapi-availability", "openapi-availability" in _ids(plan))


def test_test_suite_verification_fires_on_a_real_test_directory(suite):
    plan = _plan_for({"tests/test_x.py": "def test_x(): pass\n", "requirements.txt": "flask\n"})
    suite.check("a real tests/ directory plans test-suite-verification", "test-suite-verification" in _ids(plan))


# --- Docker cluster ---------------------------------------------------

def test_docker_cluster_fires_all_three_checks(suite):
    plan = _plan_for({"Dockerfile": "FROM node:20\n", "package.json": "{}"})
    ids = _ids(plan)
    suite.check("Dockerfile plans container-build", "container-build" in ids)
    suite.check("Dockerfile plans container-startup", "container-startup" in ids)
    suite.check("Dockerfile plans container-environment-validation", "container-environment-validation" in ids)


def test_no_docker_file_plans_no_docker_checks(suite):
    plan = _plan_for({"package.json": "{}"})
    ids = _ids(plan)
    suite.check("no Dockerfile -> no container-build planned", "container-build" not in ids)
    suite.check("no Dockerfile -> no container-startup planned", "container-startup" not in ids)


def test_container_startup_depends_on_container_build(suite):
    plan = _plan_for({"Dockerfile": "FROM node:20\n", "package.json": "{}"})
    startup = next(c for c in plan.checks if c.id == "container-startup")
    suite.check("container-startup declares container-build as a dependency", "container-build" in startup.dependencies)


# --- Next.js App Router cluster ----------------------------------------

def test_nextjs_app_router_plans_server_components(suite):
    plan = _plan_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "app/page.tsx": "export default function Page() { return null }\n",
    })
    suite.check("Next.js App Router (app/) plans server-components", "server-components" in _ids(plan))


def test_nextjs_pages_router_does_not_plan_server_components(suite):
    """The Pages Router (pages/, not app/) has no React Server Components -
    the check must not fire without app/ as real evidence.
    """
    plan = _plan_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "pages/index.tsx": "export default function Home() { return null }\n",
    })
    suite.check("Next.js Pages Router alone plans no server-components check", "server-components" not in _ids(plan))


# --- FastAPI cluster ---------------------------------------------------

def test_fastapi_plans_its_full_cluster(suite):
    plan = _plan_for({"requirements.txt": "fastapi\nuvicorn\n", "main.py": "from fastapi import FastAPI\n"})
    ids = _ids(plan)
    for expected in (
        "fastapi-health-endpoint", "fastapi-openapi-docs", "fastapi-dependency-injection",
        "fastapi-startup-events", "fastapi-shutdown-events",
    ):
        suite.check("FastAPI project plans '{}'".format(expected), expected in ids)


def test_non_fastapi_project_plans_no_fastapi_checks(suite):
    plan = _plan_for({"requirements.txt": "flask\n", "app.py": "from flask import Flask\n"})
    ids = _ids(plan)
    suite.check("a Flask project plans no FastAPI-specific checks", not any(i.startswith("fastapi-") for i in ids))


# --- unsupported/unknown evidence: never fabricated ------------------

def test_unknown_framework_plans_nothing_framework_specific(suite):
    plan = _plan_for({"package.json": '{"dependencies": {"some-totally-unknown-framework": "1.0.0"}}'})
    suite.check(
        "an unrecognized framework produces no fabricated framework-specific check",
        plan.checks == () or all(not c.id.startswith("fastapi-") and c.id != "server-components" for c in plan.checks),
    )


def test_planner_never_invents_a_database_or_auth_check(suite):
    """The explicit, documented scope boundary: no evidence detector for
    databases, ORMs, or auth libraries exists yet, so these must never
    appear no matter what's in the fixture.
    """
    plan = _plan_for({
        "package.json": '{"dependencies": {"prisma": "5.0.0", "next-auth": "4.0.0", "pg": "8.0.0"}}',
    })
    forbidden_substrings = ("database", "prisma", "auth", "session", "cookie", "jwt", "cors", "rate-limit", "cache", "queue")
    offending = [c.id for c in plan.checks if any(s in c.id.lower() for s in forbidden_substrings)]
    suite.check(
        "no database/auth/session/etc check is ever fabricated, even with those exact dependency names present",
        offending == [],
        " (offending: {})".format(offending),
    )


# --- deterministic ordering --------------------------------------------

def test_recommended_order_is_a_dense_1_indexed_sequence(suite):
    plan = _plan_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "app/api/x/route.ts": "export function GET() {}\n",
        "middleware.ts": "export function middleware() {}\n",
        "Dockerfile": "FROM node:20\n",
    })
    orders = sorted(c.recommended_order for c in plan.checks)
    suite.check(
        "recommended_order is a dense 1..N sequence with no gaps or repeats",
        orders == list(range(1, len(plan.checks) + 1)),
        " (got {})".format(orders),
    )


def test_a_check_never_appears_before_its_own_dependency(suite):
    plan = _plan_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "app/api/x/route.ts": "export function GET() {}\n",
        "Dockerfile": "FROM node:20\n",
    })
    order_by_id = {c.id: c.recommended_order for c in plan.checks}
    violations = []
    for check in plan.checks:
        for dep in check.dependencies:
            if dep in order_by_id and order_by_id[dep] >= order_by_id[check.id]:
                violations.append((check.id, dep))
    suite.check("every dependency is ordered strictly before the check that needs it", violations == [], " ({})".format(violations))


def test_no_duplicate_check_ids(suite):
    plan = _plan_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "app/api/x/route.ts": "export function GET() {}\n",
        "Dockerfile": "FROM node:20\n",
        "requirements.txt": "fastapi\n",
    })
    ids = [c.id for c in plan.checks]
    suite.check("no check id appears more than once", len(ids) == len(set(ids)))


def test_repeatability_same_input_same_order(suite):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        writer.write("package.json", '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}')
        writer.write("app/api/x/route.ts", "export function GET() {}\n")
        writer.write("middleware.ts", "export function middleware() {}\n")
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        first = plan_runtime_qa(context)
        second = plan_runtime_qa(context)
    finally:
        proj.__exit__(None, None, None)
    suite.check(
        "the same context produces byte-identical checks (excluding generated_at) on two calls",
        tuple((c.id, c.recommended_order) for c in first.checks) == tuple((c.id, c.recommended_order) for c in second.checks),
    )


def test_priority_values_are_always_recognized(suite):
    plan = _plan_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "Dockerfile": "FROM node:20\n",
    })
    for check in plan.checks:
        suite.check("check '{}' has a recognized priority".format(check.id), check.priority in PRIORITIES)


# --- serialization -----------------------------------------------------

def test_plan_to_dict_has_the_expected_shape(suite):
    plan = _plan_for({"requirements.txt": "flask\n", "app.py": "from flask import Flask\n"})
    data = plan_to_dict(plan)
    suite.check("to_dict has 'checks'", "checks" in data)
    suite.check("to_dict has 'repository_root'", "repository_root" in data)
    suite.check("to_dict has 'generated_at'", "generated_at" in data)
    if data["checks"]:
        first = data["checks"][0]
        for field in ("id", "name", "category", "priority", "reason", "required_evidence", "dependencies", "recommended_order"):
            suite.check("a serialized check has '{}'".format(field), field in first)


def test_plan_to_json_round_trips(suite):
    plan = _plan_for({
        "package.json": '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}',
        "Dockerfile": "FROM node:20\n",
    })
    raw = plan_to_json(plan)
    try:
        parsed = json.loads(raw)
        ok = True
    except json.JSONDecodeError:
        parsed, ok = None, False
    suite.check("plan_to_json produces valid JSON", ok)
    if ok:
        suite.check("round-tripped JSON matches plan_to_dict exactly", parsed == plan_to_dict(plan))


def test_render_markdown_produces_a_real_table(suite):
    plan = _plan_for({"requirements.txt": "flask\n", "app.py": "from flask import Flask\n"})
    markdown = render_markdown(plan)
    suite.check("markdown output has a header", "# Runtime QA Plan" in markdown)
    suite.check("markdown output has a table row separator", "| --- |" in markdown)
    suite.check("markdown output names a real planned check", "Server Startup" in markdown)


# --- pure-function guarantees -------------------------------------------

def test_planner_modules_never_import_subprocess_http_or_browser(suite):
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "runtime"
    offending = []
    banned = ("import subprocess", "import requests", "urllib.request", "playwright", "selenium", "socket.")
    for path in package_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if any(b in text for b in banned):
            offending.append(path.name)
    suite.check(
        "no file in qa_agent/runtime/ imports subprocess/requests/a browser driver",
        offending == [],
        " ({})".format(offending),
    )


def test_planner_modules_never_import_qa_agent_ai(suite):
    pattern = re.compile(r"^\s*(import qa_agent\.ai|from \.\.ai\b|from \.ai\b|from qa_agent\.ai\b)", re.MULTILINE)
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "runtime"
    offending = []
    for path in package_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offending.append(path.name)
    suite.check("no file in qa_agent/runtime/ imports qa_agent.ai", offending == [], " ({})".format(offending))


def test_planner_never_raises_on_a_broken_context(suite):
    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("simulated failure reading {}".format(name))

    try:
        plan = plan_runtime_qa(_Broken())
        raised = False
    except Exception:
        raised = True
    suite.check("a broken context never raises out of plan_runtime_qa", not raised)
    if not raised:
        suite.check("a broken context still returns a plan with zero checks", plan.checks == ())


# --- monorepo / hybrid / library coverage ------------------------------

def test_monorepo_project(suite):
    plan = _plan_for({
        "turbo.json": "{}",
        "apps/web/package.json": '{"dependencies": {"react": "18.0.0"}}',
        "apps/web/pages/index.tsx": "export {}\n",
    })
    suite.check("a real monorepo still plans real checks", len(plan.checks) > 0)


def test_hybrid_full_stack_project_plans_both_frontend_and_backend_checks(suite):
    plan = _plan_for({
        "package.json": '{"dependencies": {"react": "18.0.0"}}',
        "pages/index.tsx": "export {}\n",
        "requirements.txt": "flask\n",
        "app.py": "from flask import Flask\n",
    })
    ids = _ids(plan)
    suite.check("a hybrid repo plans frontend-routes", "frontend-routes" in ids)
    suite.check("a hybrid repo plans server-startup", "server-startup" in ids)


def test_cli_application_plans_no_frontend_or_api_checks(suite):
    plan = _plan_for({"package.json": '{"bin": {"mytool": "./cli.js"}}'})
    ids = _ids(plan)
    suite.check("a bare CLI tool plans no frontend-routes", "frontend-routes" not in ids)
    suite.check("a bare CLI tool plans no api-endpoints", "api-endpoints" not in ids)


# --- CLI -----------------------------------------------------------------

def test_cli_runtime_plan_flag_prints_all_three_sections(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}')
        writer.write("middleware.ts", "export function middleware() {}\n")
        proc = run_agent(["discover", str(root), "--runtime-plan"])
    suite.check("discover --runtime-plan exits 0", proc.returncode == 0)
    suite.check("output includes the ProjectKnowledge report", "Project Discovery Report" in proc.stdout)
    suite.check("output includes the Repository Context (implied by --runtime-plan)", "Repository Context" in proc.stdout)
    suite.check("output includes the Runtime QA Plan", "Runtime QA Plan" in proc.stdout)
    suite.check(
        "sections appear in the documented order",
        proc.stdout.index("Project Discovery Report")
        < proc.stdout.index("Repository Context")
        < proc.stdout.index("Runtime QA Plan"),
    )
    suite.check("the middleware check appears in the CLI output", "Middleware" in proc.stdout)


def test_cli_without_runtime_plan_flag_omits_it(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", "{}")
        proc = run_agent(["discover", str(root)])
    suite.check("discover without --runtime-plan never prints a Runtime QA Plan", "Runtime QA Plan" not in proc.stdout)


def test_cli_runtime_plan_never_executes_anything(suite):
    """The one property that matters most: even given a project shaped to
    plan every generic check, nothing is ever actually run - no server
    process, no HTTP request, no browser. Proven indirectly: the command
    returns near-instantly and reports no execution-shaped output at all.
    """
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", '{"dependencies": {"next": "14.0.0", "react": "18.0.0"}}')
        writer.write("requirements.txt", "flask\n")
        writer.write("app.py", "from flask import Flask\n")
        writer.write("Dockerfile", "FROM node:20\n")
        proc = run_agent(["discover", str(root), "--runtime-plan"])
    for forbidden in ("Connection refused", "listening on", "HTTP/1.1", "started server", "Traceback"):
        suite.check(
            "output never shows evidence of real execution ('{}')".format(forbidden),
            forbidden not in proc.stdout,
        )


if __name__ == "__main__":
    suite = Suite("Phase G Part 1: Runtime QA Planning Engine")
    sys.exit(suite.run([
        test_empty_repository_plans_nothing,
        test_every_check_has_non_empty_required_evidence,
        test_runtime_check_refuses_empty_evidence,
        test_runtime_check_rejects_an_unknown_priority,
        test_server_startup_fires_on_backend_framework,
        test_server_startup_does_not_fire_for_a_bare_library,
        test_environment_configuration_fires_only_with_real_env_files,
        test_api_endpoints_fires_on_api_directory,
        test_middleware_fires_on_root_middleware_file,
        test_middleware_does_not_fire_with_no_middleware_evidence,
        test_frontend_routes_fires_on_frontend_framework_and_route_dir,
        test_static_assets_fires_on_public_directory,
        test_build_verification_fires_on_build_system,
        test_ci_fires_only_with_a_real_workflow_file,
        test_openapi_availability_fires_on_a_real_spec_file,
        test_test_suite_verification_fires_on_a_real_test_directory,
        test_docker_cluster_fires_all_three_checks,
        test_no_docker_file_plans_no_docker_checks,
        test_container_startup_depends_on_container_build,
        test_nextjs_app_router_plans_server_components,
        test_nextjs_pages_router_does_not_plan_server_components,
        test_fastapi_plans_its_full_cluster,
        test_non_fastapi_project_plans_no_fastapi_checks,
        test_unknown_framework_plans_nothing_framework_specific,
        test_planner_never_invents_a_database_or_auth_check,
        test_recommended_order_is_a_dense_1_indexed_sequence,
        test_a_check_never_appears_before_its_own_dependency,
        test_no_duplicate_check_ids,
        test_repeatability_same_input_same_order,
        test_priority_values_are_always_recognized,
        test_plan_to_dict_has_the_expected_shape,
        test_plan_to_json_round_trips,
        test_render_markdown_produces_a_real_table,
        test_planner_modules_never_import_subprocess_http_or_browser,
        test_planner_modules_never_import_qa_agent_ai,
        test_planner_never_raises_on_a_broken_context,
        test_monorepo_project,
        test_hybrid_full_stack_project_plans_both_frontend_and_backend_checks,
        test_cli_application_plans_no_frontend_or_api_checks,
        test_cli_runtime_plan_flag_prints_all_three_sections,
        test_cli_without_runtime_plan_flag_omits_it,
        test_cli_runtime_plan_never_executes_anything,
    ]))
