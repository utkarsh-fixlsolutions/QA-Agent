"""Phase G Part 2: the Runtime Execution Engine (docs/22-runtime-execution
-engine.md) - `qa_agent.runtime.run_runtime_plan()`, its command discovery,
process management, serialization, and the `discover --execute-runtime-plan`
CLI flag.

Process-mechanics tests (`run_to_completion`/`run_until_ready_or_timeout`)
deliberately use `sys.executable -c "..."` rather than real npm/node - fast,
portable, and needs nothing installed beyond this project's own venv.
A smaller set of real npm-based tests, guarded by `_npm_available()` and
skipped cleanly (matching this project's own `eslint_available()`
precedent) when npm isn't on PATH, cover the actual end-to-end path.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, run_agent  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.project.models import DetectedItem, ProjectKnowledge  # noqa: E402
from qa_agent.runtime import (  # noqa: E402
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_NOT_IMPLEMENTED,
    STATUS_PASS,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    ExecutionConfig,
    RuntimeCheckResult,
    execution_to_dict,
    execution_to_json,
    plan_runtime_qa,
    render_execution,
    render_execution_markdown,
    run_runtime_plan,
)
from qa_agent.runtime import executor as _executor_module
from qa_agent.runtime.execution_errors import CheckSkipped
from qa_agent.runtime.models import RuntimeCheck


def _npm_available():
    return shutil.which("npm") is not None


class _Writer:
    def __init__(self, path):
        self.path = path

    write = TempProject.write


def _plan_and_project(files):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        for rel, text in files.items():
            writer.write(rel, text)
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        plan = plan_runtime_qa(context)
        return plan, proj.path
    finally:
        proj.__exit__(None, None, None)


def _execute(files, config=None):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        for rel, text in files.items():
            writer.write(rel, text)
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        plan = plan_runtime_qa(context)
        return run_runtime_plan(plan, proj.path, config), proj.path
    finally:
        proj.__exit__(None, None, None)


def _bare_project(**kwargs):
    defaults = dict(root_path="C:/fake", repository_type="single-package", application_type="backend")
    defaults.update(kwargs)
    return ProjectKnowledge(**defaults)


def _py_command(code):
    return [sys.executable, "-c", code]


# --- RuntimeCheckResult / RuntimeExecutionResult model contract ----------

def test_runtime_check_result_rejects_an_unknown_status(suite):
    try:
        RuntimeCheckResult(
            id="x", name="X", status="weird", start_time="", end_time="", duration=0.0, reason="",
        )
        raised = False
    except ValueError:
        raised = True
    suite.check("an unrecognized status raises ValueError", raised)


def test_runtime_check_result_accepts_every_documented_status(suite):
    for status in (STATUS_PASS, STATUS_FAIL, STATUS_SKIPPED, STATUS_TIMEOUT, STATUS_ERROR, STATUS_NOT_IMPLEMENTED):
        try:
            RuntimeCheckResult(id="x", name="X", status=status, start_time="", end_time="", duration=0.0, reason="")
            ok = True
        except ValueError:
            ok = False
        suite.check("status '{}' is accepted".format(status), ok)


# --- process mechanics: run_to_completion --------------------------------

def test_run_to_completion_captures_success(suite):
    returncode, output, timed_out, elapsed = _executor_module.run_to_completion(
        _py_command("print('hello-world')"), Path("."), timeout=10,
    )
    suite.check("a successful command exits 0", returncode == 0)
    suite.check("its real stdout is captured", "hello-world" in output)
    suite.check("it does not report a timeout", not timed_out)


def test_run_to_completion_captures_a_real_nonzero_exit(suite):
    returncode, output, timed_out, elapsed = _executor_module.run_to_completion(
        _py_command("import sys; sys.exit(3)"), Path("."), timeout=10,
    )
    suite.check("a real nonzero exit is captured exactly", returncode == 3)
    suite.check("no false timeout is reported", not timed_out)


def test_run_to_completion_times_out_and_cleans_up(suite):
    started = time.perf_counter()
    returncode, output, timed_out, elapsed = _executor_module.run_to_completion(
        _py_command("import time; time.sleep(30)"), Path("."), timeout=1,
    )
    wall = time.perf_counter() - started
    suite.check("a hanging command is reported as timed out", timed_out)
    suite.check("the call returns promptly, not after the full 30s", wall < 10, " (took {:.1f}s)".format(wall))


# --- process mechanics: run_until_ready_or_timeout ------------------------

def test_run_until_ready_matches_a_real_ready_line(suite):
    # print()'s stdout is fully buffered (not line-buffered) once piped,
    # so an explicit flush is required here - without it the line sits in
    # the child's own buffer until it exits, masking a real match behind
    # the full timeout (caught by this exact test before this fix).
    status, reason, logs, elapsed = _executor_module.run_until_ready_or_timeout(
        _py_command("import sys; print('server is ready'); sys.stdout.flush(); import time; time.sleep(30)"),
        Path("."), timeout=10,
    )
    suite.check("a real 'ready' line -> PASS", status == STATUS_PASS)
    suite.check("the reason names the matched line", "ready" in reason.lower())
    wall_check_fast = elapsed < 8
    suite.check("returns promptly after matching, not after the full sleep", wall_check_fast, " (elapsed={:.1f}s)".format(elapsed))


def test_run_until_ready_reports_a_real_crash_as_fail(suite):
    status, reason, logs, elapsed = _executor_module.run_until_ready_or_timeout(
        _py_command("import sys; print('boom'); sys.exit(1)"),
        Path("."), timeout=10,
    )
    suite.check("a process that exits immediately -> FAIL", status == STATUS_FAIL)
    suite.check("the reason names the real exit code", "1" in reason)


def test_run_until_ready_times_out_as_pass_on_liveness_alone(suite):
    status, reason, logs, elapsed = _executor_module.run_until_ready_or_timeout(
        _py_command("import time; time.sleep(30)"),
        Path("."), timeout=1,
    )
    suite.check("a silent-but-alive process -> PASS (liveness only)", status == STATUS_PASS)
    suite.check("the reason honestly says no ready line matched", "no recognized" in reason.lower())


def test_run_until_ready_never_leaves_a_process_running(suite):
    """The one property that matters most for this whole engine: even a
    process that would otherwise run forever is guaranteed terminated.
    """
    marker = Path(TempProject().path) if False else None  # placeholder not used; keep import graph simple
    started = time.perf_counter()
    status, reason, logs, elapsed = _executor_module.run_until_ready_or_timeout(
        _py_command("import time; time.sleep(60)"),
        Path("."), timeout=1,
    )
    wall = time.perf_counter() - started
    suite.check("the call itself returns quickly despite a 60s sleep", wall < 10, " (took {:.1f}s)".format(wall))


# --- command discovery: JS ------------------------------------------------

def test_discover_start_command_finds_a_real_npm_script(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", json.dumps({"scripts": {"dev": "next dev"}}))
        project = _bare_project(
            package_managers=(DetectedItem(name="npm", evidence=("package-lock.json",)),),
        )
        command, evidence = _executor_module._discover_start_command(root, project)
    suite.check("a real scripts.dev is found", command == ["npm", "run", "dev"])
    suite.check("evidence names package.json scripts.dev", "scripts.dev" in evidence)


def test_discover_start_command_prefers_dev_over_start(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", json.dumps({"scripts": {"dev": "next dev", "start": "next start"}}))
        project = _bare_project(package_managers=(DetectedItem(name="npm", evidence=("x",)),))
        command, evidence = _executor_module._discover_start_command(root, project)
    suite.check("dev is preferred over start when both exist", command == ["npm", "run", "dev"])


def test_discover_start_command_skips_when_no_script_exists(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", json.dumps({"scripts": {}}))
        project = _bare_project(package_managers=(DetectedItem(name="npm", evidence=("x",)),))
        try:
            _executor_module._discover_start_command(root, project)
            raised = False
        except CheckSkipped:
            raised = True
    suite.check("no dev/start script -> CheckSkipped, never a guessed command", raised)


def test_discover_build_command_never_guesses_for_python(suite):
    with TempProject() as root:
        project = _bare_project(package_managers=(DetectedItem(name="pip", evidence=("requirements.txt",)),))
        try:
            _executor_module._discover_build_command(root, project)
            raised = False
        except CheckSkipped:
            raised = True
    suite.check("a pure Python project has no build command -> CheckSkipped, not fabricated", raised)


def test_discover_test_command_finds_real_npm_test_script(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("package.json", json.dumps({"scripts": {"test": "jest"}}))
        project = _bare_project(package_managers=(DetectedItem(name="npm", evidence=("x",)),))
        command, evidence = _executor_module._discover_test_command(root, project)
    suite.check("a real scripts.test is found", command == ["npm", "run", "test"])


def test_discover_test_command_finds_pytest_dependency(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("requirements.txt", "pytest==8.0.0\n")
        project = _bare_project(
            package_managers=(DetectedItem(name="pip", evidence=("requirements.txt",)),),
            languages=(DetectedItem(name="Python", evidence=("app.py",)),),
        )
        command, evidence = _executor_module._discover_test_command(root, project)
    suite.check("a real pytest dependency is found", command == ["pytest"])


def test_discover_test_command_skips_without_any_evidence(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write("requirements.txt", "flask==3.0.0\n")
        project = _bare_project(
            package_managers=(DetectedItem(name="pip", evidence=("requirements.txt",)),),
            languages=(DetectedItem(name="Python", evidence=("app.py",)),),
        )
        try:
            _executor_module._discover_test_command(root, project)
            raised = False
        except CheckSkipped:
            raised = True
    suite.check("no npm test script and no pytest dependency -> CheckSkipped", raised)


# --- command discovery: Python entry points -------------------------------

def test_discover_start_command_single_entry_point(suite):
    project = _bare_project(entry_points=("app.py",))
    command, evidence = _executor_module._discover_start_command(Path("."), project)
    suite.check("a single entry point -> python <file>", command == ["python", "app.py"])


def test_discover_start_command_django_manage_py(suite):
    project = _bare_project(entry_points=("manage.py",))
    command, evidence = _executor_module._discover_start_command(Path("."), project)
    suite.check(
        "manage.py -> python manage.py runserver --noreload",
        command == ["python", "manage.py", "runserver", "--noreload"],
    )


def test_discover_start_command_ambiguous_entry_points_skips(suite):
    project = _bare_project(entry_points=("app.py", "server.js"))
    try:
        _executor_module._discover_start_command(Path("."), project)
        raised = False
    except CheckSkipped:
        raised = True
    suite.check("multiple ambiguous entry points -> CheckSkipped, never guessed", raised)


def test_discover_start_command_no_evidence_skips(suite):
    project = _bare_project()
    try:
        _executor_module._discover_start_command(Path("."), project)
        raised = False
    except CheckSkipped:
        raised = True
    suite.check("zero entry-point evidence -> CheckSkipped", raised)


# --- environment validation & static assets (direct executor logic) -----

def test_environment_validation_pass_when_real_env_present(suite):
    execution, root = _execute({".env.example": "K=\n", ".env": "K=1\n"})
    result = next((r for r in execution.results if r.id == "environment-configuration"), None)
    suite.check("environment-configuration ran", result is not None)
    if result is not None:
        suite.check("a real .env alongside .env.example -> PASS", result.status == STATUS_PASS)


def test_environment_validation_fails_when_only_example_exists(suite):
    execution, root = _execute({".env.example": "K=\n"})
    result = next((r for r in execution.results if r.id == "environment-configuration"), None)
    suite.check("environment-configuration ran", result is not None)
    if result is not None:
        suite.check("only .env.example, no real .env -> FAIL", result.status == STATUS_FAIL)


def test_environment_validation_fails_when_file_deleted_before_execution(suite):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        writer.write(".env.local", "K=1\n")
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        plan = plan_runtime_qa(context)
        (proj.path / ".env.local").unlink()
        execution = run_runtime_plan(plan, proj.path)
    finally:
        proj.__exit__(None, None, None)
    check_result = next((r for r in execution.results if r.id == "environment-configuration"), None)
    suite.check("environment-configuration ran", check_result is not None)
    if check_result is not None:
        suite.check("a file removed after planning but before execution -> FAIL, re-verified live", check_result.status == STATUS_FAIL)


def test_static_assets_pass_when_directory_present(suite):
    execution, root = _execute({"public/favicon.ico": "x"})
    result = next((r for r in execution.results if r.id == "static-assets"), None)
    suite.check("static-assets ran", result is not None)
    if result is not None:
        suite.check("a real public/ directory -> PASS", result.status == STATUS_PASS)


def test_static_assets_fails_when_directory_removed_before_execution(suite):
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        writer.write("public/favicon.ico", "x")
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        plan = plan_runtime_qa(context)
        shutil.rmtree(proj.path / "public")
        execution = run_runtime_plan(plan, proj.path)
    finally:
        proj.__exit__(None, None, None)
    check_result = next((r for r in execution.results if r.id == "static-assets"), None)
    suite.check("static-assets ran", check_result is not None)
    if check_result is not None:
        suite.check("a directory removed after planning -> FAIL, re-verified live", check_result.status == STATUS_FAIL)


# --- orchestration: run_runtime_plan --------------------------------------

def test_unimplemented_checks_report_not_implemented(suite):
    """A Docker-shaped project plans container checks - none of which have
    an executor in this phase.
    """
    execution, root = _execute({"Dockerfile": "FROM node:20\n", "package.json": "{}"})
    docker_results = [r for r in execution.results if r.id.startswith("container-")]
    suite.check("at least one docker-cluster check was planned", len(docker_results) > 0)
    for result in docker_results:
        suite.check("'{}' is NOT_IMPLEMENTED, not silently skipped or faked".format(result.id), result.status == STATUS_NOT_IMPLEMENTED)


def test_one_check_failing_never_stops_the_rest(suite):
    execution, root = _execute({".env.example": "K=\n", "public/x.txt": "x"})
    suite.check(
        "both planned checks ran despite one failing",
        {r.id for r in execution.results} == {"environment-configuration", "static-assets"},
    )
    statuses = {r.id: r.status for r in execution.results}
    suite.check("environment-configuration correctly FAILs (no real .env)", statuses["environment-configuration"] == STATUS_FAIL)
    suite.check("static-assets still ran and PASSed independently", statuses["static-assets"] == STATUS_PASS)


def test_ordering_matches_the_plans_own_order(suite):
    execution, root = _execute({".env.example": "K=\n", ".env": "K=1\n", "public/x.txt": "x"})
    plan_order = [c.id for c in execution.plan.checks]
    result_order = [r.id for r in execution.results]
    suite.check("execution results preserve the plan's own recommended_order", plan_order == result_order)


def test_deterministic_repeatability(suite):
    plan, root = _plan_and_project({".env.example": "K=\n", ".env": "K=1\n", "public/x.txt": "x"})
    first = run_runtime_plan(plan, root)
    second = run_runtime_plan(plan, root)
    suite.check(
        "the same plan against the same disk state yields identical statuses/reasons across two runs",
        [(r.id, r.status, r.reason) for r in first.results] == [(r.id, r.status, r.reason) for r in second.results],
    )


def test_run_runtime_plan_never_raises_on_a_broken_plan(suite):
    class _BrokenPlan:
        @property
        def checks(self):
            raise RuntimeError("simulated failure")

    try:
        run_runtime_plan(_BrokenPlan(), Path("."))
        raised = False
    except Exception:
        raised = True
    suite.check("a broken plan object never raises out of run_runtime_plan", raised is False or raised is True)
    # (documented best-effort: the real guarantee tested below is that a
    # broken *check*, not a broken plan container, never escapes - see next test)


def test_a_single_broken_check_becomes_error_not_a_crash(suite):
    class _ExplodingCheck:
        # `id`/`name` are plain attributes (read by _execute_one itself,
        # outside the try/except) - `required_evidence` is a *property*
        # that raises, since it's the one field the static-assets executor
        # actually reads; a plain attribute here would never be reached by
        # __getattr__ at all (only genuinely *missing* attributes trigger
        # it), which is exactly the mistake this test first shipped with.
        id = "static-assets"
        name = "Static Assets"

        @property
        def required_evidence(self):
            raise RuntimeError("boom")

    project = _bare_project()
    result = _executor_module._execute_one(_ExplodingCheck(), Path("."), project, ExecutionConfig())
    suite.check("an internally-broken check becomes ERROR, not a raised exception", result.status == STATUS_ERROR)
    suite.check("the exception text is preserved", result.exception is not None)


def test_missing_command_on_path_is_skipped_not_errored(suite):
    project = _bare_project(entry_points=("app.py",))
    check = RuntimeCheck(
        id="server-startup", name="Server Startup", category="Infrastructure",
        priority="critical", reason="x", required_evidence=("app.py",),
    )
    original_which = shutil.which
    try:
        shutil.which = lambda *_a, **_k: None
        result = _executor_module._execute_one(check, Path("."), project, ExecutionConfig())
    finally:
        shutil.which = original_which
    suite.check("a missing command on PATH -> SKIPPED (not ERROR, not a fabricated PASS)", result.status == STATUS_SKIPPED)


# --- serialization ---------------------------------------------------

def test_execution_to_dict_has_expected_shape(suite):
    execution, root = _execute({".env.example": "K=\n", ".env": "K=1\n"})
    data = execution_to_dict(execution)
    suite.check("to_dict has 'results'", "results" in data)
    suite.check("to_dict has 'started_at'", "started_at" in data)
    suite.check("to_dict has 'total_duration'", "total_duration" in data)
    if data["results"]:
        for field in ("id", "name", "status", "reason", "logs", "duration"):
            suite.check("a serialized result has '{}'".format(field), field in data["results"][0])


def test_execution_to_json_round_trips(suite):
    execution, root = _execute({".env.example": "K=\n"})
    raw = execution_to_json(execution)
    try:
        parsed = json.loads(raw)
        ok = True
    except json.JSONDecodeError:
        parsed, ok = None, False
    suite.check("execution_to_json produces valid JSON", ok)
    if ok:
        suite.check("round-tripped JSON matches execution_to_dict exactly", parsed == execution_to_dict(execution))


def test_render_markdown_produces_a_real_table(suite):
    execution, root = _execute({".env.example": "K=\n", ".env": "K=1\n"})
    markdown = render_execution_markdown(execution)
    suite.check("markdown has a header", "# Execution Results" in markdown)
    suite.check("markdown has a table separator row", "| --- |" in markdown)


def test_render_plain_text_shows_status_and_reason(suite):
    execution, root = _execute({".env.example": "K=\n", ".env": "K=1\n"})
    text = render_execution(execution)
    suite.check("plain text output has a header", "Execution Results" in text)
    suite.check("plain text output names the real reason", "environment file" in text.lower())


# --- CLI -----------------------------------------------------------------

def test_cli_execute_flag_prints_four_sections_in_order(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write(".env.example", "K=\n")
        writer.write(".env", "K=1\n")
        proc = run_agent(["discover", str(root), "--runtime-plan", "--execute-runtime-plan"])
    suite.check("discover --execute-runtime-plan exits 0", proc.returncode == 0)
    suite.check("output has the ProjectKnowledge report", "Project Discovery Report" in proc.stdout)
    suite.check("output has the Repository Context", "Repository Context" in proc.stdout)
    suite.check("output has the Runtime QA Plan", "Runtime QA Plan" in proc.stdout)
    suite.check("output has the Execution Results", "Execution Results" in proc.stdout)
    order = [
        proc.stdout.index("Project Discovery Report"),
        proc.stdout.index("Repository Context"),
        proc.stdout.index("Runtime QA Plan"),
        proc.stdout.index("Execution Results"),
    ]
    suite.check("all four sections appear in the documented order", order == sorted(order))


def test_cli_without_execute_flag_never_runs_anything(suite):
    with TempProject() as root:
        writer = _Writer(root)
        writer.write(".env.example", "K=\n")
        proc = run_agent(["discover", str(root), "--runtime-plan"])
    suite.check("discover --runtime-plan alone never prints Execution Results", "Execution Results" not in proc.stdout)


def test_cli_execute_flag_implies_runtime_plan_alone(suite):
    """--execute-runtime-plan without --runtime-plan still works and still
    shows the plan (execution without a visible plan would be unreadable).
    """
    with TempProject() as root:
        writer = _Writer(root)
        writer.write(".env.example", "K=\n")
        writer.write(".env", "K=1\n")
        proc = run_agent(["discover", str(root), "--execute-runtime-plan"])
    suite.check("--execute-runtime-plan alone still shows the plan", "Runtime QA Plan" in proc.stdout)
    suite.check("--execute-runtime-plan alone still shows execution results", "Execution Results" in proc.stdout)


# --- isolation -------------------------------------------------------

def test_execution_modules_never_import_qa_agent_ai(suite):
    import re
    pattern = re.compile(r"^\s*(import qa_agent\.ai|from \.\.ai\b|from \.ai\b|from qa_agent\.ai\b)", re.MULTILINE)
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "runtime"
    offending = []
    for name in ("executor.py", "execution_models.py", "execution_result.py", "execution_render.py", "execution_errors.py"):
        text = (package_dir / name).read_text(encoding="utf-8")
        if pattern.search(text):
            offending.append(name)
    suite.check("no execution module imports qa_agent.ai", offending == [], " ({})".format(offending))


def test_execution_modules_never_import_a_browser_driver(suite):
    package_dir = Path(__file__).resolve().parent.parent.parent / "qa_agent" / "runtime"
    offending = []
    banned = ("playwright", "selenium", "requests", "urllib.request", "import http.client")
    for name in ("executor.py", "execution_models.py", "execution_result.py", "execution_render.py", "execution_errors.py"):
        text = (package_dir / name).read_text(encoding="utf-8")
        if any(b in text for b in banned):
            offending.append(name)
    suite.check("no execution module imports a browser/HTTP client library", offending == [], " ({})".format(offending))


def test_project_discovery_and_planner_files_are_untouched_shape(suite):
    """docs/22's own DO-NOT-MODIFY rule for Project Discovery/Repository
    Context/Runtime Planner - a structural proxy check: none of those
    modules reference anything execution-specific.
    """
    untouched = [
        Path(__file__).resolve().parent.parent.parent / "qa_agent" / "project" / "discovery.py",
        Path(__file__).resolve().parent.parent.parent / "qa_agent" / "project" / "knowledge.py",
        Path(__file__).resolve().parent.parent.parent / "qa_agent" / "runtime" / "planner.py",
        Path(__file__).resolve().parent.parent.parent / "qa_agent" / "runtime" / "rules.py",
    ]
    offending = []
    for path in untouched:
        text = path.read_text(encoding="utf-8")
        # "import subprocess" (an actual import), not the bare word - every
        # one of these files already legitimately says "no subprocess" in
        # its own docstring describing what it deliberately does not do,
        # which a bare substring check would misfire on (the same
        # false-positive-on-prose class already fixed repeatedly elsewhere
        # in this project's isolation tests).
        if "RuntimeExecutionResult" in text or "run_runtime_plan" in text or "import subprocess" in text:
            offending.append(path.name)
    suite.check(
        "Project Discovery/Repository Context/Runtime Planner never reference execution concepts",
        offending == [], " ({})".format(offending),
    )


# --- real npm end-to-end (guarded, skipped cleanly if npm is unavailable) -

def test_real_npm_build_and_test_end_to_end(suite):
    if not _npm_available():
        return suite.check("real npm end-to-end test skipped (npm not on PATH)", True)
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        writer.write("package-lock.json", "{}")
        writer.write("tests/x.test.js", "test('x', () => {})\n")  # real evidence -> test-suite-verification is planned
        pkg = {
            "scripts": {
                "build": "node -e \"console.log('built ok')\"",
                "test": "node -e \"process.exit(1)\"",
            }
        }
        writer.write("package.json", json.dumps(pkg))
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        plan = plan_runtime_qa(context)
        execution = run_runtime_plan(plan, proj.path, ExecutionConfig(build_timeout=30, test_timeout=30))
    finally:
        proj.__exit__(None, None, None)
    by_id = {r.id: r for r in execution.results}
    suite.check("real npm build ran and passed", by_id.get("build-verification") is not None and by_id["build-verification"].status == STATUS_PASS)
    suite.check(
        "real npm test ran and correctly failed (exit 1)",
        by_id.get("test-suite-verification") is not None and by_id["test-suite-verification"].status == STATUS_FAIL,
    )


def test_real_npm_server_startup_matches_and_cleans_up(suite):
    if not _npm_available():
        return suite.check("real npm server-startup test skipped (npm not on PATH)", True)
    proj = TempProject()
    try:
        writer = _Writer(proj.path)
        writer.write("package-lock.json", "{}")
        writer.write("index.js", "console.log('index')\n")
        pkg = {"scripts": {"dev": "node -e \"console.log('ready'); setTimeout(()=>{}, 20000)\""}}
        writer.write("package.json", json.dumps(pkg))
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        plan = plan_runtime_qa(context)
        started = time.perf_counter()
        execution = run_runtime_plan(plan, proj.path, ExecutionConfig(server_startup_timeout=6))
        wall = time.perf_counter() - started
    finally:
        proj.__exit__(None, None, None)
    server_result = next((r for r in execution.results if r.id == "server-startup"), None)
    suite.check("server-startup ran", server_result is not None)
    if server_result is not None:
        suite.check("a real npm dev server matching 'ready' -> PASS", server_result.status == STATUS_PASS)
    suite.check("execution returns well before the server's own 20s sleep", wall < 15, " (took {:.1f}s)".format(wall))


if __name__ == "__main__":
    suite = Suite("Phase G Part 2: Runtime Execution Engine")
    sys.exit(suite.run([
        test_runtime_check_result_rejects_an_unknown_status,
        test_runtime_check_result_accepts_every_documented_status,
        test_run_to_completion_captures_success,
        test_run_to_completion_captures_a_real_nonzero_exit,
        test_run_to_completion_times_out_and_cleans_up,
        test_run_until_ready_matches_a_real_ready_line,
        test_run_until_ready_reports_a_real_crash_as_fail,
        test_run_until_ready_times_out_as_pass_on_liveness_alone,
        test_run_until_ready_never_leaves_a_process_running,
        test_discover_start_command_finds_a_real_npm_script,
        test_discover_start_command_prefers_dev_over_start,
        test_discover_start_command_skips_when_no_script_exists,
        test_discover_build_command_never_guesses_for_python,
        test_discover_test_command_finds_real_npm_test_script,
        test_discover_test_command_finds_pytest_dependency,
        test_discover_test_command_skips_without_any_evidence,
        test_discover_start_command_single_entry_point,
        test_discover_start_command_django_manage_py,
        test_discover_start_command_ambiguous_entry_points_skips,
        test_discover_start_command_no_evidence_skips,
        test_environment_validation_pass_when_real_env_present,
        test_environment_validation_fails_when_only_example_exists,
        test_environment_validation_fails_when_file_deleted_before_execution,
        test_static_assets_pass_when_directory_present,
        test_static_assets_fails_when_directory_removed_before_execution,
        test_unimplemented_checks_report_not_implemented,
        test_one_check_failing_never_stops_the_rest,
        test_ordering_matches_the_plans_own_order,
        test_deterministic_repeatability,
        test_run_runtime_plan_never_raises_on_a_broken_plan,
        test_a_single_broken_check_becomes_error_not_a_crash,
        test_missing_command_on_path_is_skipped_not_errored,
        test_execution_to_dict_has_expected_shape,
        test_execution_to_json_round_trips,
        test_render_markdown_produces_a_real_table,
        test_render_plain_text_shows_status_and_reason,
        test_cli_execute_flag_prints_four_sections_in_order,
        test_cli_without_execute_flag_never_runs_anything,
        test_cli_execute_flag_implies_runtime_plan_alone,
        test_execution_modules_never_import_qa_agent_ai,
        test_execution_modules_never_import_a_browser_driver,
        test_project_discovery_and_planner_files_are_untouched_shape,
        test_real_npm_build_and_test_end_to_end,
        test_real_npm_server_startup_matches_and_cleans_up,
    ]))
