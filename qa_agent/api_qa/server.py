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
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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
    "watching for file changes",
)
# Two patterns deliberately removed (docs/41), both real, previously
# latent false-positives never actually exercised until `fastapi`/
# `uvicorn` were installed in this environment for the first time today
# (previously silently skipped, not silently passing - found immediately
# once real end-to-end tests could finally run):
#   - "started server" matched uvicorn's own very first startup line,
#     "Started server process [PID]" - printed well before anything else.
#   - "application startup complete" matched uvicorn's own next line,
#     "Application startup complete." - which is *still* one line too
#     early: uvicorn's real URL-bearing line, "Uvicorn running on
#     http://...", is always printed last, one line after this. Removing
#     it is safe because "running on" (already a real pattern here) is
#     what actually matches that final, authoritative line for uvicorn -
#     the same signal, minus the premature break, and it directly yields
#     a real base URL from the very same line via `_LOCAL_URL_RE`.

# Matches the real "- Local:        http://localhost:3000" line `next dev`/
# `next start` print on startup - used to discover the real bound port
# rather than assuming a default port whenever it can be observed.
_LOCAL_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1):\d+")

# A second, weaker-but-still-real evidence source (docs/40-monorepo-server
# -discovery.md): plenty of real, common Express/FastAPI-style servers log
# a bare "Server running on port 5000" rather than a full URL - no
# `_LOCAL_URL_RE` match, but the port number itself is still a real fact
# the process actually printed, never a guess. Tried only when
# `_LOCAL_URL_RE` found nothing at all - a real, literal URL is always
# preferred evidence when both are somehow present.
_PORT_ONLY_RE = re.compile(r"\bport[:\s]+(\d{2,5})\b", re.IGNORECASE)

_JS_PACKAGE_MANAGER_PRIORITY = ("pnpm", "yarn", "bun", "npm")

# Manager name -> the one lockfile basename that is real evidence of it,
# restricted to the JS managers this module ever launches a server with.
# Duplicated narrowly from `project.detectors`'s own (broader,
# multi-language) `_PACKAGE_MANAGER_BASENAMES` rather than imported - same
# "duplicate a small list rather than reach into another module's private
# constant" precedent already used elsewhere in this file (see
# `_FRONTEND_FRAMEWORK_NAMES`'s own docstring).
_JS_LOCKFILE_BY_MANAGER = {
    "pnpm": "pnpm-lock.yaml",
    "yarn": "yarn.lock",
    "bun": "bun.lockb",
    "npm": "package-lock.json",
}

# How far up from a project root to look for a workspace's own lockfile -
# generous enough for any real monorepo nesting (e.g. `apps/web` two levels
# under the workspace root) without ever wandering into an unrelated parent
# directory indefinitely.
_MAX_ANCESTOR_LOCKFILE_LEVELS = 6


def _read_json_safe(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ancestor_js_package_manager(root):
    """A monorepo/workspace *package* (e.g. `apps/web` in a pnpm workspace)
    commonly carries no lockfile of its own - the workspace root's lockfile,
    one or more levels up, is what actually governs it, exactly like
    `node_modules` resolution itself walks upward. `detect_package_managers`
    (project/detectors.py) only ever scans *downward* from whatever root it
    is given, so pointing this tool directly at such a package produces zero
    package-manager evidence even though a completely real lockfile exists
    just outside that root - this is real evidence too, just found by
    walking the other direction.

    Checks each ancestor's own immediate directory entries only (never a
    recursive scan) for one of the same real lockfile basenames
    `detect_package_managers` already trusts. Stops at the first match, at
    the first ancestor that itself contains a `.git` directory (the real
    repository boundary - never searched past it), or after
    `_MAX_ANCESTOR_LOCKFILE_LEVELS`, whichever comes first.
    """
    current = Path(root).resolve()
    for _ in range(_MAX_ANCESTOR_LOCKFILE_LEVELS):
        parent = current.parent
        if parent == current:  # reached the filesystem root
            return None
        current = parent
        try:
            entries = {entry.name for entry in current.iterdir()}
        except OSError:
            return None
        for candidate in _JS_PACKAGE_MANAGER_PRIORITY:
            if _JS_LOCKFILE_BY_MANAGER[candidate] in entries:
                return candidate
        if ".git" in entries:
            return None
    return None


def _js_package_manager(project, root=None):
    names = {i.name for i in project.package_managers}
    for candidate in _JS_PACKAGE_MANAGER_PRIORITY:
        if candidate in names:
            return candidate
    if root is not None:
        return _ancestor_js_package_manager(root)
    return None


def _npm_script_command(pkg_dir, package_manager, script_name):
    data = _read_json_safe(Path(pkg_dir) / "package.json")
    if not isinstance(data, dict):
        return None
    scripts = data.get("scripts")
    if not isinstance(scripts, dict) or not scripts.get(script_name):
        return None
    return [package_manager, "run", script_name]


# A monorepo/workspace package's own framework - real evidence already
# gathered by Phase F discovery (`project.frameworks`, each with the real
# file(s) that prove it) - is "frontend-only" when every framework whose
# evidence lives under that package is one of these. Duplicated, not
# imported, from `project.detectors`'s own private `_FRONTEND_FRAMEWORKS`
# list - the same "duplicate a small list narrowly rather than import
# another module's private constant into an unrelated package" precedent
# `discovery.py`'s own `_IGNORED_DIR_NAMES` docstring already established.
_FRONTEND_FRAMEWORK_NAMES = frozenset({"React", "Vue", "Angular", "Svelte", "Next.js", "Nuxt"})


def _package_looks_frontend_only(project, pkg_rel_dir):
    prefix = pkg_rel_dir.rstrip("/") + "/"
    evidenced = [
        item for item in project.frameworks
        if any(ev.startswith(prefix) for ev in item.evidence)
    ]
    return bool(evidenced) and all(item.name in _FRONTEND_FRAMEWORK_NAMES for item in evidenced)


def _monorepo_start_command(root, project, manager):
    """Real, evidence-based only, for a workspace/monorepo with no runnable
    script at its own root (docs/40-monorepo-server-discovery.md): tries
    each real, already-detected `project.monorepo_packages` directory's own
    `package.json` for a real `dev`/`start` script - the same two-script
    preference the root-level strategy already has, just checked one level
    into each real package directory instead of assuming the root is the
    only place a server could live.

    A monorepo commonly has more than one runnable package (a frontend dev
    server *and* a backend API server, e.g. `client`/`server`) - starting
    the wrong one would silently test nothing real, so this never guesses
    among genuine candidates. A candidate package whose own framework
    evidence (`project.frameworks`) is entirely frontend-only
    (`_FRONTEND_FRAMEWORK_NAMES`) is deprioritized in favor of any
    candidate that is not; if that still leaves more than one real
    candidate, or leaves none at all, the whole thing is honestly reported
    as ambiguous/absent - never an arbitrary pick. Returns `(command,
    evidence, cwd)` or `(None, reason, None)`.
    """
    candidates = []  # (pkg, command, script)
    for pkg in sorted(project.monorepo_packages):
        pkg_dir = Path(root) / pkg
        for script in ("dev", "start"):
            command = _npm_script_command(pkg_dir, manager, script)
            if command is not None:
                candidates.append((pkg, command, script))
                break

    if not candidates:
        return None, "no npm dev/start script found in the root package.json, nor in any monorepo package ({})".format(
            ", ".join(sorted(project.monorepo_packages)) or "none"
        ), None

    non_frontend = [c for c in candidates if not _package_looks_frontend_only(project, c[0])]
    chosen = None
    if len(non_frontend) == 1:
        chosen = non_frontend[0]
    elif len(candidates) == 1:
        chosen = candidates[0]

    if chosen is None:
        return None, (
            "root package.json has no dev/start script, and which monorepo package is the real "
            "server could not be determined without guessing - candidates with a dev/start script: {}"
            .format(", ".join(pkg for pkg, _cmd, _script in candidates))
        ), None

    pkg, command, script = chosen
    evidence = "monorepo package '{}/package.json' scripts.{} (via {})".format(pkg, script, manager)
    return command, evidence, Path(root) / pkg


def discover_server_start_command(root, project):
    """Real, evidence-based only. Tries the Node/npm strategy first (Step
    30's own original behavior, unchanged for a single-package project - an
    npm `dev`/`start` script the developer wrote themselves at the real
    project root); when the root has none *and* this is a real, already-
    detected workspace/monorepo (`project.monorepo_packages`, docs/40),
    tries each real package directory next rather than giving up
    immediately - a workspace/monorepo's own runnable server commonly
    lives one level down (`server/package.json`), never at the workspace
    root itself. If no JS package manager is even detected, tries the
    Python/FastAPI strategy (new, gated behind a real, already-detected
    FastAPI framework fact). Returns `(command, evidence, cwd)` or `(None,
    reason, None)` - `cwd` is the real directory the command must actually
    be launched from (the workspace root and a monorepo package's own
    directory are not the same thing; running a package's own `npm run
    dev` from the wrong cwd would fail to find that package's own
    `package.json` at all). No fallback to a guessed command.
    """
    manager = _js_package_manager(project, root)
    if manager is not None:
        for script in ("dev", "start"):
            command = _npm_script_command(root, manager, script)
            if command is not None:
                command = _prefer_installed_manager_binary(command)
                return command, "package.json scripts.{} (via {})".format(script, manager), Path(root)
        if project.monorepo_packages:
            command, evidence, cwd = _monorepo_start_command(root, project, manager)
            return _prefer_installed_manager_binary(command), evidence, cwd
        return None, "no npm dev/start script found in package.json", None

    if _is_fastapi_project(project):
        command, evidence = _discover_python_start_command(root, project)
        return command, evidence, (Path(root) if command is not None else None)

    return None, "no JS package manager detected for this project", None


def _prefer_installed_manager_binary(command):
    """The manager chosen for `command` (real evidence: its own lockfile,
    found either in the project or in an ancestor workspace root - see
    `_ancestor_js_package_manager`) is still the right *fact* about the
    project, but a package manager's binary is only strictly required for
    its own `install`; *running* an already-defined `package.json`
    `scripts.<name>` entry against `node_modules` that already exists on
    disk works identically through any of them. So if the evidenced
    manager's own binary is not actually installed on this machine's PATH,
    fall back to `npm` - which ships with any Node.js install and is
    virtually always present - to run that same script, rather than
    reporting a real, already-discovered, already-runnable server as
    unstartable just because one specific binary happens to be missing.
    Never invents a command; only ever substitutes its first element, and
    only when the original genuinely is not runnable.
    """
    if not command:
        return command
    if shutil.which(command[0]) is not None:
        return command
    if command[0] != "npm" and shutil.which("npm") is not None:
        return ["npm"] + list(command[1:])
    return command


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

    `log_queue` (docs/37-api-qa-server-log-capture.md): the same live
    `queue.Queue` `_reader_thread` keeps appending real stdout/stderr lines
    to for as long as the process stays alive - kept on the handle (rather
    than discarded once `start_and_wait_ready` returns) so a caller can
    drain whatever the server prints *after* it became ready, e.g. while
    real HTTP calls are being made against it. `None` only when `proc` is
    also `None` (nothing was ever started, so nothing was ever queued).
    """

    proc: Optional[subprocess.Popen]
    status: str
    reason: str
    logs: Tuple[str, ...]
    elapsed: float
    base_url: str = ""
    log_queue: Optional["queue.Queue"] = None

    def stop(self):
        if self.proc is not None:
            _kill_process_tree(self.proc)


# A log tail is only ever useful as recent, human-scannable context - never
# an unbounded dump. The same "cap the displayed evidence, never silently
# drop it all" convention `http_client.py`'s own `MAX_RESPONSE_SAMPLE_CHARS`
# already established.
MAX_LOG_TAIL_CHARS = 4_000


def drain_log_tail(handle: ServerHandle, max_chars: int = MAX_LOG_TAIL_CHARS) -> str:
    """Every real line the server has printed since the last time this (or
    `start_and_wait_ready`) drained its queue, joined and trimmed to the
    last `max_chars` characters - never the full, unbounded history.
    Returns `""` when the server was never started, or printed nothing new.
    Non-blocking: only ever reads what is already queued, never waits for
    more output to arrive.
    """
    if handle.log_queue is None:
        return ""
    lines = []
    try:
        while True:
            lines.append(handle.log_queue.get_nowait())
    except queue.Empty:
        pass
    text = "".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


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
                if matched_line is None and any(p in lowered for p in ready_patterns):
                    matched_line = line.strip()
                # docs/46-ready-signal-false-positive-fix.md: a bare
                # keyword match (above) is remembered, never trusted by
                # itself to stop watching - a wrapper tool (nodemon,
                # ts-node-dev, ...) commonly prints its own ready-shaped
                # line well before the real child process it spawns has
                # actually bound a port, and stopping right there means
                # the real app's own, authoritative URL/port line - often
                # printed moments later - is never even seen, leaving
                # nothing but a guessed port for every real call to fail
                # against. Only a real, observed URL/port (the strongest
                # evidence this loop can get) is allowed to end the wait
                # early; a keyword-only match still lets the loop keep
                # reading, right up to the same overall `timeout`, in case
                # the real line is still coming.
                if _observed_base_url("".join(lines)):
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
            logs=tuple(lines), elapsed=elapsed, log_queue=q,
        )
    if matched_line is not None:
        return ServerHandle(
            proc=proc, status=STATUS_READY,
            reason="matched ready signal: {!r}".format(matched_line),
            logs=tuple(lines), elapsed=elapsed, base_url=base_url, log_queue=q,
        )
    return ServerHandle(
        proc=proc, status=STATUS_ALIVE_NO_READY_SIGNAL,
        reason="process stayed running for {:.1f}s without crashing; no recognized "
               "'ready' log line matched".format(elapsed),
        logs=tuple(lines), elapsed=elapsed, base_url=base_url, log_queue=q,
    )


def _observed_base_url(logs_text):
    match = _LOCAL_URL_RE.search(logs_text)
    if match:
        return match.group(0)
    port_match = _PORT_ONLY_RE.search(logs_text)
    if port_match:
        return "http://localhost:{}".format(port_match.group(1))
    return ""


# A dev tool's own printed "ready"/"listening"-shaped log line only proves
# it printed something ready-shaped - never that a listener socket is
# actually bound and accepting connections yet. A real race found
# dogfooding twice, with two different frameworks: a cold-compiling Next.js
# dev server, and a nodemon-wrapped Express server whose wrapper process
# prints its own status text before the real child process it spawns has
# finished binding - both matched a real "ready" log line, and the very
# first real HTTP call still got a real, honest connection-refused.
DEFAULT_CONNECT_PROBE_TIMEOUT = 10.0
_CONNECT_PROBE_POLL_INTERVAL = 0.1


def wait_until_connectable(base_url, timeout=DEFAULT_CONNECT_PROBE_TIMEOUT,
                            poll_interval=_CONNECT_PROBE_POLL_INTERVAL):
    """Real confirmation that *something* is actually accepting TCP
    connections at `base_url`, polled every `poll_interval` seconds up to
    `timeout` - a real, live check, never inferred from log text. Returns
    `True` the moment a real connect succeeds, `False` once `timeout`
    elapses without one - never raises, never blocks past `timeout`.
    Deliberately only a TCP connect, not a real HTTP request - checking
    reachability should never itself count as one of the real calls
    `resolve_and_execute` goes on to make and report.
    """
    parsed = urllib.parse.urlsplit(base_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    deadline = time.perf_counter() + timeout
    while True:
        try:
            with socket.create_connection((host, port), timeout=poll_interval):
                return True
        except OSError:
            pass
        if time.perf_counter() >= deadline:
            return False
        time.sleep(poll_interval)


# --- HTTP readiness check (Phase 1: environment readiness gate) ------------
#
# A successful TCP connect only proves *something* accepted the connection -
# never that it actually speaks HTTP, or that this is really the app's own
# server and not, say, a stray unrelated process that happened to bind the
# port first. This is a second, still-real (never inferred from log text)
# piece of evidence, deliberately lenient about *what* it receives: any real
# HTTP response at all - including a 404 or 500 - proves the server is
# genuinely answering requests, and must never be confused with the server
# being unreachable. Only a real connection-level failure on every candidate
# path (refused, reset, timed out) counts as a readiness failure.

DEFAULT_HTTP_READINESS_TIMEOUT = 5.0

# Tried in order; a real, already-discovered health/keep-alive-shaped
# endpoint (see `_health_shaped_endpoint_path`) is always tried first when
# one exists - real evidence outranks every one of these plain guesses.
_DEFAULT_READINESS_CANDIDATE_PATHS = ("/health", "/api/health", "/")

_HEALTH_PATH_MARKERS = ("health", "keep-alive", "keepalive")


def _health_shaped_endpoint_path(endpoints):
    """A real, already-discovered endpoint whose own path looks like a
    health/liveness check - reused as the readiness probe's first candidate
    when one exists, never invented. `endpoints` is whatever `discover_api_
    endpoints` already found; this never does any discovery of its own.
    """
    for endpoint in endpoints:
        lowered = endpoint.path.lower()
        if any(marker in lowered for marker in _HEALTH_PATH_MARKERS):
            return endpoint.path
    return None


@dataclass(frozen=True)
class HttpReadinessResult:
    """`reached=True` means a real HTTP response (any status code) was
    received on at least one candidate path - the server is genuinely
    answering HTTP requests, whatever that specific path's own status was.
    `reached=False` means every candidate path failed at the connection
    level - a real, distinct fact from an application-level 404/500.
    """

    reached: bool
    path: str
    status_code: Optional[int]
    detail: str


def check_http_readiness(base_url, endpoints=(), timeout=DEFAULT_HTTP_READINESS_TIMEOUT):
    """One lightweight real GET against the first candidate path that
    answers at all - never part of `resolve_and_execute`'s own real test
    calls, and never itself a second copy of the TCP gate: this only runs
    after `wait_until_connectable` has already succeeded. Real evidence
    only; never raises.
    """
    candidates = []
    health_path = _health_shaped_endpoint_path(endpoints)
    if health_path is not None:
        candidates.append(health_path)
    for path in _DEFAULT_READINESS_CANDIDATE_PATHS:
        if path not in candidates:
            candidates.append(path)

    base = base_url.rstrip("/")
    last_error = None
    for path in candidates:
        url = base + path if path.startswith("/") else base + "/" + path
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpReadinessResult(
                    True, path, response.getcode(),
                    "received a real HTTP {} from {} - the server is answering".format(
                        response.getcode(), path),
                )
        except urllib.error.HTTPError as exc:
            # A real HTTP response, even an error status - the server really
            # is answering; an application-level error is not a readiness
            # failure (see this function's own docstring).
            return HttpReadinessResult(
                True, path, exc.code,
                "received a real HTTP {} from {} (an application-level response, "
                "not a connection failure - the server is answering)".format(exc.code, path),
            )
        except (urllib.error.URLError, OSError):
            last_error = "no response from {}".format(path)
            continue

    return HttpReadinessResult(
        False, candidates[-1] if candidates else "", None,
        "TCP connected, but no real HTTP response was received on any candidate path ({}): {}".format(
            ", ".join(candidates), last_error or "no candidate paths to try"),
    )
