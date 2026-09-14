"""Server lifecycle for API QA v1 (docs/30-api-qa-v1.md; docs/32-fastapi
-discovery-and-startup.md): discover a real start command, launch it, wait
until it looks ready (or a timeout elapses) **without** killing it, so real
HTTP calls can be made against it - then, always, stop it.

`discover_server_start_command` tries two independent, additive strategies
- Node/npm (Step 30, unchanged) first, then Python/FastAPI (new) - never
one replacing the other; see that function's own docstring.

This deliberately duplicates a small amount of process-management logic
that already exists in `qa_agent/runtime/executor.py`
(`_start_process`/`_kill_process_tree`/the ready-log-watching loop) rather
than importing or modifying that module. Two reasons, both explicit
project convention (docs/step-log.md, Dependency philosophy; the same call
already made for `_prompt_text`/`_string_list`-style helpers elsewhere in
this codebase): first, `runtime/executor.py`'s own `run_until_ready_or_
timeout` always terminates the process before returning - exactly correct
for its own job (a liveness check) but wrong for this one, which needs the
process to keep running afterward; second, Phase G Part 2's engine is
already shipped and tested, and this task's own acceptance criteria
require "existing G1-G4 behavior remains intact" - not modifying that file
at all is the simplest way to guarantee that completely.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

_IS_WINDOWS = os.name == "nt"
_USE_SHELL = _IS_WINDOWS

STATUS_READY = "ready"
STATUS_ALIVE_NO_READY_SIGNAL = "alive_no_ready_signal"
STATUS_CRASHED = "crashed"
STATUS_NOT_FOUND = "not_found"

DEFAULT_READY_PATTERNS = (
    "ready", "listening", "running on", "compiled successfully",
    "started server", "application startup complete", "watching for file changes",
)

# Matches the real "- Local:        http://localhost:3000" line `next dev`/
# `next start` print on startup - used to discover the real bound port
# rather than assuming Next.js's default 3000 whenever it can be observed.
_LOCAL_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1):\d+")

_JS_PACKAGE_MANAGER_PRIORITY = ("pnpm", "yarn", "bun", "npm")


def _read_json_safe(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _js_package_manager(project):
    names = {i.name for i in project.package_managers}
    for candidate in _JS_PACKAGE_MANAGER_PRIORITY:
        if candidate in names:
            return candidate
    return None


def _npm_script_command(root, package_manager, script_name):
    data = _read_json_safe(root / "package.json")
    if not isinstance(data, dict):
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict) or not scripts.get(script_name):
        return None
    return [package_manager, "run", script_name]


def discover_server_start_command(root, project):
    """Real, evidence-based only. Tries the Node/npm strategy first (Step
    30's own original behavior, completely unchanged - an npm `dev`/`start`
    script the developer wrote themselves); if no JS package manager is
    even detected, tries the Python/FastAPI strategy (new, gated behind a
    real, already-detected FastAPI framework fact). Returns `(command,
    evidence)` or `(None, reason)`. No fallback to a guessed command.
    """
    manager = _js_package_manager(project)
    if manager is not None:
        for script in ("dev", "start"):
            command = _npm_script_command(root, manager, script)
            if command is not None:
                return command, "package.json scripts.{} (via {})".format(script, manager)
        return None, "no npm dev/start script found in package.json"

    if _is_fastapi_project(project):
        return _discover_python_start_command(root, project)

    return None, "no JS package manager detected for this project"


# --- Python / FastAPI strategy (new) ----------------------------------------

def _is_fastapi_project(project):
    return any(item.name == "FastAPI" for item in project.frameworks)


# On Windows, a project-local venv's interpreter lives under Scripts\; on
# every other platform, under bin/ - both real, standard layouts `python -m
# venv` itself creates, checked for directly (never activated via a shell
# script - "invoke the interpreter executable directly" is this step's own
# explicit requirement).
_VENV_DIR_NAMES = (".venv", "venv")


def _venv_python(root):
    """The real, on-disk interpreter inside the target project's own
    virtual environment, when one exists - `None` otherwise, never guessed.
    Checked in `_VENV_DIR_NAMES` order; the first real match wins.
    """
    root = Path(root)
    subpath = ("Scripts", "python.exe") if _IS_WINDOWS else ("bin", "python")
    for venv_name in _VENV_DIR_NAMES:
        candidate = root.joinpath(venv_name, *subpath)
        if candidate.is_file():
            return str(candidate)
    return None


def _select_interpreter(root):
    """The target project's own venv interpreter when one exists on disk;
    otherwise whatever `python` resolves to on this process's own PATH -
    the same graceful-degradation convention every other evidence-based
    lookup in this package already follows (never a hard failure just
    because the preferred, more-precise option is not available).
    """
    return _venv_python(root) or "python"


# A real file that really calls `uvicorn.run(...)` when executed directly -
# never guessed from a filename alone (the exact bug found auditing this
# project: `qa_agent/runtime/executor.py`'s own generic entry-point
# heuristic picks a file by name only, and would have picked `app/main.py`
# here, which never calls `uvicorn.run(...)` at all). Checked by real
# content, not by name - the names below are only where evidence is looked
# for, never assumed to already be true.
_UVICORN_RUN_RE = re.compile(r"uvicorn\s*\.\s*run\s*\(")
_FASTAPI_APP_VAR_RE = re.compile(r"(\w+)\s*=\s*FastAPI\s*\(")

_PY_ENTRY_CANDIDATE_NAMES = ("run.py", "main.py", "app.py", "asgi.py", "wsgi.py")

_MAX_PY_PROBE_BYTES = 500_000


def _read_py_text_safe(path):
    try:
        if path.stat().st_size > _MAX_PY_PROBE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _find_uvicorn_run_entrypoint(root, project):
    """The real, relative path of a file that really contains a
    `uvicorn.run(...)` call - checked against common runner-script names
    plus every real entry point Project Discovery already found
    (`project.entry_points`, e.g. `app/main.py`) - or `None` if no real
    file actually contains one.
    """
    root = Path(root)
    candidates = list(_PY_ENTRY_CANDIDATE_NAMES) + list(project.entry_points)
    checked = set()
    for rel in candidates:
        if rel in checked or not rel.endswith(".py"):
            continue
        checked.add(rel)
        text = _read_py_text_safe(root / rel)
        if text is not None and _UVICORN_RUN_RE.search(text):
            return rel
    return None


def _find_fastapi_app_module(root, project):
    """A real, already-known `.py` file (`project.entry_points`/
    `project.important_files` - never an unbounded whole-repository scan)
    that really assigns `<name> = FastAPI(...)` - returns `(module, app_
    var)` (e.g. `("app.main", "app")`) or `None`. Never guesses a module
    path that does not correspond to a real file actually found this way.
    """
    root = Path(root)
    candidates = list(project.entry_points) + list(project.important_files)
    checked = set()
    for rel in candidates:
        if rel in checked or not rel.endswith(".py"):
            continue
        checked.add(rel)
        text = _read_py_text_safe(root / rel)
        if text is None:
            continue
        match = _FASTAPI_APP_VAR_RE.search(text)
        if match:
            module = rel[:-3].replace("\\", "/").replace("/", ".")
            return module, match.group(1)
    return None


# The real packages a FastAPI app needs to even import - checked, never
# assumed, before any real startup attempt (docs/32's own explicit
# "understand why startup cannot proceed" requirement). A fixed, literal
# probe script this project itself controls - never code read from or
# supplied by the target project - so this is not arbitrary execution.
_DEPENDENCY_PROBE_MODULES = ("fastapi", "uvicorn")
_DEPENDENCY_PROBE_TIMEOUT = 15.0


def check_python_dependencies(interpreter, modules=_DEPENDENCY_PROBE_MODULES):
    """Runs `<interpreter> -c "import <modules>"` - direct interpreter
    invocation, `shell=False`, no activation script of any kind. Returns
    `(ok, reason)`; never raises - a missing/broken interpreter is reported
    as `ok=False` with a clear reason, exactly like every other real
    failure mode in this module.
    """
    probe = "import " + ", ".join(modules)
    try:
        completed = subprocess.run(
            [interpreter, "-c", probe],
            capture_output=True, text=True, timeout=_DEPENDENCY_PROBE_TIMEOUT, shell=False,
        )
    except FileNotFoundError:
        return False, "interpreter '{}' does not exist or is not runnable".format(interpreter)
    except subprocess.TimeoutExpired:
        return False, "checking dependencies via '{}' did not finish within {:.0f}s".format(
            interpreter, _DEPENDENCY_PROBE_TIMEOUT)
    except OSError as exc:
        return False, "could not run '{}' to check dependencies: {}".format(interpreter, exc)

    if completed.returncode == 0:
        return True, "{} import successfully via '{}'".format(", ".join(modules), interpreter)
    detail_lines = (completed.stderr or completed.stdout or "").strip().splitlines()
    last_line = detail_lines[-1] if detail_lines else "import failed with exit code {}".format(completed.returncode)
    return False, "{} not importable via '{}': {}".format(", ".join(modules), interpreter, last_line)


def _discover_python_start_command(root, project):
    """Real, evidence-based only, for a project with a real, already-
    detected FastAPI dependency. Prefers a real file that genuinely calls
    `uvicorn.run(...)`; falls back to a real `<module>:<app>` uvicorn
    target only when a real `FastAPI()` instantiation can be found in an
    already-known file. The interpreter is the project's own venv when one
    exists on disk, else whatever `python` resolves to - see `_select_
    interpreter`. Dependency availability is checked before this function
    ever returns a command, so a missing-package failure is reported here,
    deterministically, rather than surfacing later as an opaque process
    crash.
    """
    root = Path(root)
    interpreter = _select_interpreter(root)

    entrypoint = _find_uvicorn_run_entrypoint(root, project)
    if entrypoint is not None:
        command = [interpreter, entrypoint]
        evidence = "{} calls uvicorn.run(...) directly (interpreter: {})".format(entrypoint, interpreter)
    else:
        module_target = _find_fastapi_app_module(root, project)
        if module_target is None:
            return None, (
                "FastAPI detected, but no runnable entrypoint could be found - neither a real file "
                "calling uvicorn.run(...) nor a real 'FastAPI()' instantiation in any already-known file"
            )
        module, app_var = module_target
        command = [interpreter, "-m", "uvicorn", "{}:{}".format(module, app_var),
                   "--host", "127.0.0.1", "--port", "8000"]
        evidence = "uvicorn {}:{} (real FastAPI() instantiation found; interpreter: {})".format(
            module, app_var, interpreter)

    deps_ok, deps_reason = check_python_dependencies(interpreter)
    if not deps_ok:
        return None, "found a Python/FastAPI startup command ({}), but {}".format(evidence, deps_reason)

    return command, evidence


def _command_available(command):
    return shutil.which(command[0]) is not None


def _start_process(command, cwd, env):
    kwargs = {}
    if not _IS_WINDOWS:
        kwargs["preexec_fn"] = os.setsid
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
    except Exception:  # noqa: BLE001 - a dying pipe is not this thread's problem
        pass
    finally:
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


@dataclass
class ServerHandle:
    """A real, possibly-still-running process plus what was observed while
    waiting for it to become ready. `proc` is `None` only when the server
    was never started at all (`STATUS_NOT_FOUND`) - `stop()` is always
    safe to call regardless.
    """

    proc: Optional[subprocess.Popen]
    status: str
    reason: str
    logs: Tuple[str, ...]
    elapsed: float
    base_url: str = ""

    def stop(self):
        if self.proc is not None:
            _kill_process_tree(self.proc)


def start_and_wait_ready(command, cwd, timeout, env=None, ready_patterns=DEFAULT_READY_PATTERNS):
    """Launch `command` and watch its output for up to `timeout` seconds.
    Unlike `runtime/executor.py`'s own `run_until_ready_or_timeout`, the
    process is **not** killed on a ready/no-signal outcome - only on a
    genuine crash. The caller is responsible for calling `.stop()` on the
    returned handle exactly once, always (see `run_api_qa`'s own `finally`).
    """
    if not _command_available(command):
        return ServerHandle(
            proc=None, status=STATUS_NOT_FOUND,
            reason="'{}' is not installed or not on PATH".format(command[0]),
            logs=(), elapsed=0.0,
        )

    started = time.perf_counter()
    proc = _start_process(command, cwd, env)
    q = queue.Queue()
    reader = threading.Thread(target=_reader_thread, args=(proc.stdout, q), daemon=True)
    reader.start()

    lines = []
    matched_line = None
    crashed = False
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
            if not line.lstrip().startswith(">"):
                lowered = line.lower()
                if any(p in lowered for p in ready_patterns):
                    matched_line = line.strip()
                    break
        if proc.poll() is not None:
            crashed = True
            break

    try:
        while True:
            lines.append(q.get_nowait())
    except queue.Empty:
        pass

    elapsed = time.perf_counter() - started
    logs_text = "".join(lines)
    base_url = _observed_base_url(logs_text)

    if crashed:
        return ServerHandle(
            proc=proc, status=STATUS_CRASHED,
            reason="process exited on its own after {:.1f}s (exit code {})".format(elapsed, proc.returncode),
            logs=tuple(lines), elapsed=elapsed,
        )
    if matched_line is not None:
        return ServerHandle(
            proc=proc, status=STATUS_READY,
            reason="matched ready signal: {!r}".format(matched_line),
            logs=tuple(lines), elapsed=elapsed, base_url=base_url,
        )
    return ServerHandle(
        proc=proc, status=STATUS_ALIVE_NO_READY_SIGNAL,
        reason="process stayed running for {:.1f}s without crashing; no recognized "
               "'ready' log line matched".format(elapsed),
        logs=tuple(lines), elapsed=elapsed, base_url=base_url,
    )


def _observed_base_url(logs_text):
    match = _LOCAL_URL_RE.search(logs_text)
    return match.group(0) if match else ""
