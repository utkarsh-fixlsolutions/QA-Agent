"""The Runtime Execution Engine's one public entry point (Phase G Part 2,
docs/22-runtime-execution-engine.md): `run_runtime_plan(plan, root,
configuration=None)`.

**What "PASS" means here, stated plainly because it is easy to
over-read:** this engine never makes an HTTP request, opens a browser, or
speaks any application protocol - that boundary is explicit and permanent
for this phase (docs/22's own scope rule). "Server Startup" succeeding
means *the process stayed alive, without crashing, for its timeout window*
- optionally corroborated by a recognized "ready"-shaped log line - never
that it actually answers a real request. Genuine request-level
verification is later, deliberately deferred work.

**Command discovery never guesses.** Every executor below only ever runs a
command it found real, authored evidence for - an npm `package.json`
`scripts` entry the developer wrote themselves, or a real Python entry-
point file Part 1 already found and categorized. This module reads a
handful of manifest files directly (`package.json`'s `scripts` section,
`requirements.txt`/`pyproject.toml` for a real `pytest` dependency) - a
small, scoped read entirely inside this package, never an extension of
`qa_agent/project/`'s own detectors (this phase's DO-NOT-MODIFY rule for
Project Discovery). When no real command can be found, the check is
`SKIPPED` with the exact reason why - never a fabricated command.

One `RuntimeCheck` failing never stops the rest (docs/22's own execution
rule) - each check is executed inside its own `try/except`, exactly
mirroring `runner.py`'s own per-adapter isolation for the deterministic
analyzer pipeline.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .execution_errors import CheckExecutionError, CheckSkipped
from .execution_models import (
    STATUS_ERROR,
    STATUS_FAIL,
    STATUS_NOT_IMPLEMENTED,
    STATUS_PASS,
    STATUS_SKIPPED,
    STATUS_TIMEOUT,
    RuntimeCheckResult,
)
from .execution_result import RuntimeExecutionResult

_IS_WINDOWS = os.name == "nt"

# Every command this engine ever launches is either a package-manager shim
# (npm/pnpm/yarn/bun/poetry/uv - .cmd files on Windows, exactly like
# ESLint's own adapters.py precedent) or a plain interpreter that also
# works fine under a shell - so, uniformly, shell=True on Windows only,
# matching the same platform-specific reason adapters.py's ESLintAdapter
# already established, not a new decision invented here.
_USE_SHELL = _IS_WINDOWS


@dataclass(frozen=True)
class ExecutionConfig:
    """Every timeout is independently overridable; defaults are chosen to
    give a real dev server/build/test run a fair chance to finish without
    letting one hung check stall the whole plan indefinitely.
    """

    server_startup_timeout: float = 20.0
    build_timeout: float = 180.0
    test_timeout: float = 180.0
    env: dict | None = None


DEFAULT_CONFIG = ExecutionConfig()


def _now():
    return datetime.now(timezone.utc).isoformat()


# --- manifest reads scoped entirely to this module (see module docstring) -

def _read_json_safe(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text_safe(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _npm_script_command(root, package_manager, script_name):
    data = _read_json_safe(root / "package.json")
    if not isinstance(data, dict):
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict) or not scripts.get(script_name):
        return None
    return [package_manager, "run", script_name]


# Preference order when more than one JS package manager was somehow
# detected in the same project - arbitrary but fixed, so command discovery
# is deterministic regardless of dict/set iteration order.
_JS_PACKAGE_MANAGER_PRIORITY = ("pnpm", "yarn", "bun", "npm")


def _js_package_manager(project):
    names = {i.name for i in project.package_managers}
    for candidate in _JS_PACKAGE_MANAGER_PRIORITY:
        if candidate in names:
            return candidate
    return None


def _python_prefix(project):
    names = {i.name for i in project.package_managers}
    if "poetry" in names:
        return ["poetry", "run"]
    if "uv" in names:
        return ["uv", "run"]
    return []


def _has_pytest_dependency(root):
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml"):
        text = _read_text_safe(root / name)
        if text is not None and "pytest" in text.lower():
            return True
    return False


def _discover_start_command(root, project):
    manager = _js_package_manager(project)
    if manager is not None:
        for script in ("dev", "start"):
            command = _npm_script_command(root, manager, script)
            if command is not None:
                return command, "package.json scripts.{} (via {})".format(script, manager)
        raise CheckSkipped("no npm dev/start script found in package.json")

    entry_points = list(project.entry_points)
    if "manage.py" in entry_points:
        return _python_prefix(project) + ["python", "manage.py", "runserver", "--noreload"], \
            "Django manage.py entry point"
    remaining = [f for f in entry_points if f != "manage.py"]
    if len(remaining) == 1:
        return _python_prefix(project) + ["python", remaining[0]], \
            "single detected runtime entry point ({})".format(remaining[0])
    if not remaining:
        raise CheckSkipped("no runtime entry point or package.json script detected")
    raise CheckSkipped(
        "multiple entry points detected, cannot determine which to start ({})".format(", ".join(remaining))
    )


def _discover_build_command(root, project):
    manager = _js_package_manager(project)
    if manager is not None:
        command = _npm_script_command(root, manager, "build")
        if command is not None:
            return command, "package.json scripts.build (via {})".format(manager)
        raise CheckSkipped("no npm build script found in package.json")
    raise CheckSkipped("no evidence-based build command for this project's stack")


def _discover_test_command(root, project):
    manager = _js_package_manager(project)
    if manager is not None:
        command = _npm_script_command(root, manager, "test")
        if command is not None:
            return command, "package.json scripts.test (via {})".format(manager)
    if any(l.name == "Python" for l in project.languages) and _has_pytest_dependency(root):
        return _python_prefix(project) + ["pytest"], "pytest dependency detected"
    raise CheckSkipped("no evidence-based test command found (no npm test script, no pytest dependency)")


# --- process management ----------------------------------------------

def _start_process(command, cwd, env):
    kwargs = {}
    if not _IS_WINDOWS:
        kwargs["preexec_fn"] = os.setsid  # own process group -> os.killpg can reach every child
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        shell=_USE_SHELL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        **kwargs,
    )


def _kill_process_tree(proc):
    """Terminate the whole process tree, not just the immediate child.

    `Popen.terminate()` alone only signals the process this module directly
    launched - when that process is a shell wrapper (`shell=True`, or a
    `.cmd` package-manager shim on Windows), the *real* work (node.exe,
    python.exe, ...) is a grandchild that `terminate()` never reaches,
    leaving it running as an orphan. A well-known, real gotcha, not a
    hypothetical one - handled explicitly here so no execution path in this
    module can ever leave a process behind, on either platform.
    """
    if proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - cleanup must never itself raise
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _reader_thread(pipe, line_queue):
    try:
        for line in iter(pipe.readline, ""):
            line_queue.put(line)
    except Exception:  # noqa: BLE001 - a dying pipe is not this thread's problem to raise
        pass
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


# Common, best-effort "the server is ready" phrases across popular dev
# servers (Next.js, Vite, Flask, Django, uvicorn/FastAPI, generic Node
# "listening on"). Matched case-insensitively, substring - a heuristic
# early-exit only: a check that never sees one of these but also never
# crashes within the timeout still passes (see run_until_ready_or_timeout's
# own docstring) - this list only ever shortens a wait, never gates a pass.
DEFAULT_READY_PATTERNS = (
    "ready", "listening", "running on", "compiled successfully",
    "started server", "application startup complete", "watching for file changes",
)


def run_until_ready_or_timeout(command, cwd, timeout, ready_patterns=DEFAULT_READY_PATTERNS, env=None):
    """Launch `command`, watch its output for up to `timeout` seconds for a
    line matching `ready_patterns`, then **always** terminate the whole
    process tree before returning - a server check never leaves a real
    process running behind it, on any exit path (ready, crash, or timeout).

    Returns `(status, reason, logs_text, elapsed)` where `status` is one of
    `STATUS_PASS`/`STATUS_FAIL`/`STATUS_TIMEOUT`.
    """
    started = time.perf_counter()
    proc = _start_process(command, cwd, env)
    q = queue.Queue()
    reader = threading.Thread(target=_reader_thread, args=(proc.stdout, q), daemon=True)
    reader.start()

    lines = []
    matched_line = None
    crashed = False
    try:
        while True:
            elapsed = time.perf_counter() - started
            if elapsed >= timeout:
                break
            try:
                line = q.get(timeout=0.2)
            except queue.Empty:
                line = None
            if line is not None:
                lines.append(line)
                # npm (and pnpm/yarn) echoes the script's own command text
                # before running it ("> myapp@1.0 dev\n> next dev\n") - real
                # dogfooding caught this matching a ready-pattern against
                # that echo instead of real output, when a script's own
                # source text happened to contain a matched word. Excluded
                # from matching (still captured in `lines`, just not
                # treated as evidence of real startup) rather than trying
                # to guess a more precise pattern.
                if not line.lstrip().startswith(">"):
                    lowered = line.lower()
                    if any(p in lowered for p in ready_patterns):
                        matched_line = line.strip()
                        break
            if proc.poll() is not None:
                crashed = True
                break
    finally:
        # Drain whatever is already buffered before killing, so a crash's
        # real error output is still captured in the result.
        try:
            while True:
                lines.append(q.get_nowait())
        except queue.Empty:
            pass
        _kill_process_tree(proc)

    elapsed = time.perf_counter() - started
    logs_text = "".join(lines)

    if matched_line is not None:
        return STATUS_PASS, "matched ready signal: {!r}".format(matched_line), logs_text, elapsed
    if crashed:
        return (
            STATUS_FAIL,
            "process exited on its own after {:.1f}s (exit code {})".format(elapsed, proc.returncode),
            logs_text,
            elapsed,
        )
    # Neither a recognized ready line nor a crash - the process was still
    # alive when the timeout elapsed. Treated as a pass on liveness alone,
    # explicitly caveated in the reason (see module docstring: this engine
    # never confirms the server actually answers a request).
    return (
        STATUS_PASS,
        "process stayed running for {:.1f}s without crashing; no recognized "
        "'ready' log line matched - startup assumed successful based on "
        "process liveness alone, not a real request".format(elapsed),
        logs_text,
        elapsed,
    )


def run_to_completion(command, cwd, timeout, env=None):
    """Launch `command` and wait for it to exit on its own, within
    `timeout`. Always cleans up the whole process tree if it doesn't.
    Returns `(returncode, output_text, timed_out, elapsed)`.
    """
    started = time.perf_counter()
    proc = _start_process(command, cwd, env)
    timed_out = False
    output = ""
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if proc.poll() is None:
            _kill_process_tree(proc)
    if timed_out:
        try:
            remaining, _ = proc.communicate(timeout=2)
            output = (output or "") + (remaining or "")
        except Exception:  # noqa: BLE001
            pass
    elapsed = time.perf_counter() - started
    return proc.returncode, output or "", timed_out, elapsed


def _check_command_available(command):
    if shutil.which(command[0]) is None:
        raise CheckSkipped("'{}' is not installed or not on PATH".format(command[0]))


# --- the five implemented executors -------------------------------------

def _execute_server_startup(check, root, project, config):
    command, evidence = _discover_start_command(root, project)
    _check_command_available(command)
    status, reason, logs_text, elapsed = run_until_ready_or_timeout(
        command, root, config.server_startup_timeout, env=config.env,
    )
    if status == STATUS_PASS:
        reason = "{} ({})".format(reason, evidence)
    return status, reason, logs_text, elapsed


def _execute_build_verification(check, root, project, config):
    command, evidence = _discover_build_command(root, project)
    _check_command_available(command)
    returncode, output, timed_out, elapsed = run_to_completion(command, root, config.build_timeout, env=config.env)
    if timed_out:
        return STATUS_TIMEOUT, "build did not finish within {:.0f}s ({})".format(config.build_timeout, evidence), output, elapsed
    if returncode == 0:
        return STATUS_PASS, "build exited 0 ({})".format(evidence), output, elapsed
    return STATUS_FAIL, "build exited {} ({})".format(returncode, evidence), output, elapsed


def _execute_test_suite(check, root, project, config):
    command, evidence = _discover_test_command(root, project)
    _check_command_available(command)
    returncode, output, timed_out, elapsed = run_to_completion(command, root, config.test_timeout, env=config.env)
    if timed_out:
        return STATUS_TIMEOUT, "test run did not finish within {:.0f}s ({})".format(config.test_timeout, evidence), output, elapsed
    if returncode == 0:
        return STATUS_PASS, "test command exited 0 ({})".format(evidence), output, elapsed
    return STATUS_FAIL, "test command exited {} ({})".format(returncode, evidence), output, elapsed


def _execute_environment_validation(check, root, project, config):
    started = time.perf_counter()
    env_files = list(project.environment_files)
    if not env_files:
        raise CheckSkipped("no environment files were detected for this project")

    missing = [f for f in env_files if not (root / f).exists()]
    examples = [f for f in env_files if "example" in Path(f).name.lower()]
    real_present = any(
        Path(f).name == ".env" or (Path(f).name.startswith(".env.") and "example" not in Path(f).name.lower())
        for f in env_files if (root / f).exists()
    )
    elapsed = time.perf_counter() - started

    if missing:
        return STATUS_FAIL, "file(s) no longer present: {}".format(", ".join(missing)), "", elapsed
    if examples and not real_present:
        return (
            STATUS_FAIL,
            "an example env file exists ({}) but no real .env/.env.local was found - "
            "the application has no real configuration to start from".format(", ".join(examples)),
            "",
            elapsed,
        )
    return STATUS_PASS, "every previously-detected environment file is still present", "", elapsed


def _execute_static_asset_verification(check, root, project, config):
    started = time.perf_counter()
    directories = check.required_evidence
    missing = [d for d in directories if not (root / d).is_dir()]
    elapsed = time.perf_counter() - started
    if missing:
        return STATUS_FAIL, "director(y/ies) no longer present: {}".format(", ".join(missing)), "", elapsed
    return STATUS_PASS, "every planned static asset directory is present: {}".format(", ".join(directories)), "", elapsed


# check id -> executor function. Every id not listed here (14 of the 19
# check types Phase G Part 1 can plan) gets STATUS_NOT_IMPLEMENTED,
# deliberately - docs/22's own explicit, bounded scope.
_EXECUTORS = {
    "server-startup": _execute_server_startup,
    "build-verification": _execute_build_verification,
    "test-suite-verification": _execute_test_suite,
    "environment-configuration": _execute_environment_validation,
    "static-assets": _execute_static_asset_verification,
}


def _execute_one(check, root, project, config):
    start_time = _now()
    started = time.perf_counter()
    executor = _EXECUTORS.get(check.id)
    if executor is None:
        elapsed = time.perf_counter() - started
        return RuntimeCheckResult(
            id=check.id, name=check.name, status=STATUS_NOT_IMPLEMENTED,
            start_time=start_time, end_time=_now(), duration=elapsed,
            reason="no executor implemented yet for '{}'".format(check.id),
        )
    try:
        status, reason, logs_text, elapsed = executor(check, root, project, config)
        logs = tuple(logs_text.splitlines()) if logs_text else ()
        return RuntimeCheckResult(
            id=check.id, name=check.name, status=status,
            start_time=start_time, end_time=_now(), duration=elapsed,
            reason=reason, logs=logs,
        )
    except CheckSkipped as exc:
        elapsed = time.perf_counter() - started
        return RuntimeCheckResult(
            id=check.id, name=check.name, status=STATUS_SKIPPED,
            start_time=start_time, end_time=_now(), duration=elapsed,
            reason=str(exc),
        )
    except CheckExecutionError as exc:
        elapsed = time.perf_counter() - started
        return RuntimeCheckResult(
            id=check.id, name=check.name, status=STATUS_ERROR,
            start_time=start_time, end_time=_now(), duration=elapsed,
            reason=str(exc), exception=repr(exc),
        )
    except Exception as exc:  # noqa: BLE001 - one check's crash must never stop the rest
        elapsed = time.perf_counter() - started
        return RuntimeCheckResult(
            id=check.id, name=check.name, status=STATUS_ERROR,
            start_time=start_time, end_time=_now(), duration=elapsed,
            reason="unexpected error: {}".format(exc), exception=repr(exc),
        )


def run_runtime_plan(plan, root, configuration=None):
    """Execute every check in `plan`, in the plan's own order, each fully
    independently - one check's failure, timeout, or crash never stops the
    rest (docs/22's own execution rule, mirroring `runner.py`'s per-adapter
    isolation). Never raises.
    """
    config = configuration or DEFAULT_CONFIG
    root = Path(root)
    started_at = _now()
    started = time.perf_counter()
    results = []
    for check in plan.checks:
        results.append(_execute_one(check, root, plan.context.project, config))
    finished_at = _now()
    total_duration = time.perf_counter() - started
    return RuntimeExecutionResult(
        plan=plan, results=tuple(results), started_at=started_at,
        finished_at=finished_at, total_duration=total_duration,
    )
