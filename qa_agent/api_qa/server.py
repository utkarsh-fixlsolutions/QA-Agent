"""Server lifecycle for API QA v1 (docs/30-api-qa-v1.md): discover a real
start command, launch it, wait until it looks ready (or a timeout elapses)
**without** killing it, so real HTTP calls can be made against it - then,
always, stop it.

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
    """Real, evidence-based only - an npm `dev`/`start` script the
    developer wrote themselves. Returns `(command, evidence)` or
    `(None, reason)`. No fallback to a guessed command.
    """
    manager = _js_package_manager(project)
    if manager is None:
        return None, "no JS package manager detected for this project"
    for script in ("dev", "start"):
        command = _npm_script_command(root, manager, script)
        if command is not None:
            return command, "package.json scripts.{} (via {})".format(script, manager)
    return None, "no npm dev/start script found in package.json"


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
