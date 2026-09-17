"""A thin web layer over the existing `qa_agent` engine (docs/39-web-frontend
.md): accept an uploaded project (a whole folder, via the browser's
`webkitdirectory` picker), run the *same* discovery/static-analysis/API-QA
functions the CLI already calls, and return one JSON result.

Deliberately kept separate from `qa_agent/` itself (Dependency philosophy,
docs/step-log.md): FastAPI/uvicorn/python-multipart are real, new
dependencies this file needs, but the CLI/engine stays exactly as
stdlib-only as it already was - nothing in `qa_agent/` imports anything
from here, or from FastAPI.

This module only ever calls already-existing, already-tested engine
functions (`discover_project`, `build_repository_context`, `runner.run`,
`run_api_qa`) and reshapes their already-real results into JSON - it never
re-implements any analysis itself.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa_agent.ai import GroqProvider  # noqa: E402
from qa_agent.api_qa import ApiQaConfig, diagnose_and_repair_api_failures, discover_api_endpoints, run_api_qa  # noqa: E402
from qa_agent.api_qa import to_dict as api_qa_to_dict  # noqa: E402
from qa_agent.api_qa import server as api_qa_server  # noqa: E402
from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.runner import collect_paths, run as run_static_checks  # noqa: E402

app = FastAPI(title="QA Agent")


@app.middleware("http")
async def _no_cache(request, call_next):
    # This UI's own JS/HTML is edited constantly during active development
    # right up to a live demo - StaticFiles' default headers (Last-Modified/
    # ETag only, no Cache-Control) let a browser skip revalidation and keep
    # serving an already-open tab's stale, in-memory copy of index.html
    # after a real fix has already landed on disk (confirmed directly: a
    # fresh curl saw the fix; the open browser tab did not). Forcing
    # no-store removes that whole failure mode for the price of one header.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


# A demo upload endpoint accepting arbitrary third-party project trees needs
# real, explicit resource limits - never an unbounded read (the same
# "cap it, never silently unbounded" discipline this project already
# applies everywhere else, e.g. http_client.py's MAX_RESPONSE_READ_BYTES).
MAX_FILES = 4000
MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB

# Never worth uploading/writing at all - dependency/build/VCS output a real
# project tree commonly contains, mirrored from runner.py's own
# IGNORED_DIRS so an uploaded node_modules/.git doesn't burn the whole
# request budget on files nobody wants analyzed anyway. The frontend
# already filters these client-side too; this is the server-side backstop.
_IGNORED_PATH_SEGMENTS = {
    ".git", ".venv", "venv", "__pycache__", ".mypy_cache", ".ruff_cache",
    "node_modules", ".next", ".turbo", "dist", "build", "coverage",
}

# `node_modules` is deliberately never uploaded (real size, real speed cost
# for no analytical benefit - static/API-QA analysis never reads it) - but
# that means a fresh upload's own dev/start script has no real dependencies
# to run against yet. `run_api_test` already means "execute this project's
# own code on this server, only for projects you trust" (the same
# real-code-execution trade-off `--api-test` always had); running that
# project's own install step first is the same category of trust, not a
# new one, and is what makes the opt-in actually work end-to-end for a
# freshly uploaded project instead of crashing in under a second.
NPM_INSTALL_TIMEOUT = 120.0
_IS_WINDOWS = os.name == "nt"

# One absolute ceiling on the whole pipeline (docs/43-web-reliability.md):
# `server_startup_timeout` + `connect_probe_timeout` + up to N per-call
# timeouts + npm install can otherwise stack sequentially with no overall
# cap - long enough that a browser, corporate proxy, or antivirus web
# filter kills the connection first (the real, observed `<!DOCTYPE...`-
# instead-of-JSON failure). Every route wraps its own real work in this
# deadline and guarantees a real, valid JSON response either way - never
# silence past what anything in between is willing to tolerate. Generous
# enough for a real small-to-medium project's own full run (confirmed
# today: install + start + a handful of calls comfortably under a minute);
# not a substitute for a real job-queue/streaming architecture, which
# stays explicitly out of scope for today.
OVERALL_DEADLINE_SECONDS = 240.0


def _stage(started: float, label: str) -> None:
    """A plain, real timestamp-and-label line to the uvicorn console for
    every real stage of a run - upload received, discovery done, npm
    install done, server started, API testing done, diagnosis done.
    Debugging "why is this slow" today meant manually reproducing with a
    throwaway script every single time; this alone would have shown the
    real answer directly, every time, for free.
    """
    print("[+{:6.1f}s] {}".format(time.perf_counter() - started, label), flush=True)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """The same tree-kill `qa_agent/api_qa/server.py`'s own
    `_kill_process_tree` already uses for the dev server, duplicated here
    rather than imported (this module stays independent of that one's
    internals) - `subprocess.run(..., timeout=...)`'s own default kill is
    single-process only, which on Windows means killing the `cmd.exe`
    wrapper `npm` runs through leaves the real `npm`/`node` children it
    spawned still running past our own stated timeout. `taskkill /T` kills
    the whole tree; never raises - cleanup must never itself fail loudly.
    """
    if proc.poll() is not None:
        return
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _npm_install(pkg_dir: Path) -> dict:
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if _IS_WINDOWS else {"preexec_fn": os.setsid}
    try:
        proc = subprocess.Popen(
            ["npm", "install"], cwd=str(pkg_dir), shell=_IS_WINDOWS,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs,
        )
    except OSError as exc:
        return {"dir": pkg_dir.name, "ok": False, "detail": "could not run npm install: {}".format(exc)}

    try:
        output, _ = proc.communicate(timeout=NPM_INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        return {
            "dir": pkg_dir.name, "ok": False,
            "detail": "npm install did not finish within {:.0f}s - the whole process tree was terminated"
                       .format(NPM_INSTALL_TIMEOUT),
        }

    ok = proc.returncode == 0
    tail = (output or "").strip()
    return {"dir": pkg_dir.name, "ok": ok, "detail": tail[-1200:] if not ok else "installed"}


def _safe_relative_path(raw_name: str) -> Optional[Path]:
    """A real, safe, relative destination path for one uploaded file's own
    browser-supplied name - `None` when it is not safe to use at all (an
    absolute path, a `..` traversal segment, or an ignored/vendor
    directory) - never trusted as-is. This is the one thing standing
    between an uploaded file and a real path-traversal write outside the
    request's own temp directory.
    """
    if not raw_name:
        return None
    parts = Path(raw_name.replace("\\", "/")).parts
    if not parts or any(p in ("..", "") for p in parts):
        return None
    if Path(raw_name).is_absolute():
        return None
    if any(p in _IGNORED_PATH_SEGMENTS for p in parts):
        return None
    return Path(*parts)


class _UploadError(Exception):
    """A real, reportable problem with the upload itself (too many files,
    too large, nothing usable) - raised by `_receive_upload` rather than
    building its own `JSONResponse`, so each route decides its own
    response shape while still sharing one real upload-handling
    implementation instead of duplicating it.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


async def _receive_upload(files: List[UploadFile], work_dir: Path) -> Path:
    """Receive one multipart upload into `work_dir` (sanitized via
    `_safe_relative_path`, capped by `MAX_FILES`/`MAX_TOTAL_BYTES`) and
    return the real project root - shared by every route that accepts an
    upload (`/api/plan`, `/api/analyze`) so this logic exists exactly
    once. Raises `_UploadError` on any real problem with the upload
    itself; never returns a response of its own.
    """
    if len(files) > MAX_FILES:
        raise _UploadError("too many files ({}, max {})".format(len(files), MAX_FILES))

    total_bytes = 0
    saved_rel_paths: List[Path] = []
    for upload in files:
        rel_path = _safe_relative_path(upload.filename or "")
        content = await upload.read()
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_BYTES:
            raise _UploadError("upload exceeds {} MB limit".format(MAX_TOTAL_BYTES // (1024 * 1024)))
        if rel_path is None:
            continue  # unsafe/ignored path - silently excluded, never written
        dest = work_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        saved_rel_paths.append(rel_path)

    if not saved_rel_paths:
        raise _UploadError("no usable files were uploaded")

    top = _common_top_level_dir(saved_rel_paths)
    candidate_root = (work_dir / top) if top else work_dir
    # The shared top segment is only trusted as a real project root once
    # actually confirmed to be a directory on disk - for a single loose
    # file (no real folder wrapper), that "segment" is the file itself,
    # and `work_dir` (the file's real parent) is the honest project root
    # instead.
    return candidate_root if candidate_root.is_dir() else work_dir


def _common_top_level_dir(rel_paths: List[Path]) -> Optional[str]:
    """When every uploaded file shares the same first path segment (always
    true for a real `webkitdirectory` folder pick - the browser prefixes
    every file with the chosen folder's own name), that segment is
    *usually* the wrapper the picker added, not part of the project's own
    structure - stripped so `discover_project` sees the real project root,
    not one level too deep. `None` when the files do not share one (a
    plain multi-file selection, not a folder pick), and the raw upload
    root is used as-is.

    That shared segment is only ever a real *guess* here - for a single
    loose file with no folder wrapper at all (`webkitRelativePath` empty,
    so the frontend falls back to the bare filename), the "shared segment"
    this computes is actually the filename itself, not a directory. The
    caller is responsible for verifying it is really a directory on disk
    before trusting it (never assumed true just because every path agreed
    on it).
    """
    tops = {p.parts[0] for p in rel_paths if p.parts}
    return tops.pop() if len(tops) == 1 else None


def _detected_items(items):
    return [{"name": i.name, "evidence": list(i.evidence)} for i in items]


def _discovery_to_dict(result):
    data = {
        "status": result.status,
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "scanned_files": result.scanned_files,
    }
    project = result.project
    if project is not None:
        data["project"] = {
            "repository_type": project.repository_type,
            "application_type": project.application_type,
            "languages": _detected_items(project.languages),
            "frameworks": _detected_items(project.frameworks),
            "package_managers": _detected_items(project.package_managers),
            "important_directories": list(project.important_directories),
        }
    return data


def _relativize(path_str: str, project_root: Path) -> str:
    """A finding's own file path, shown relative to the uploaded project's
    root rather than this server's real temp-directory path - the
    temp-dir path is a real implementation detail of this request, never
    something the person who uploaded the project should have to see.
    Falls back to the original string if it is not really under
    `project_root` (never fabricated into a path that was never real).
    """
    try:
        return Path(path_str).resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path_str


def _finding_to_dict(f, project_root: Path):
    return {
        "file": _relativize(f.file, project_root), "line": f.line,
        "severity": f.severity, "message": f.message, "tool": f.tool,
    }


def _static_result_to_dict(result, project_root: Path):
    return {
        "checked": len(result.checked),
        "missing": list(result.missing),
        "tools_used": list(result.tools_used),
        "tool_errors": [{"tool": name, "error": str(err)} for name, err in result.tool_errors],
        "filtered": result.filtered,
        "findings": [_finding_to_dict(f, project_root) for f in result.findings],
    }


def _diagnosis_entry_to_dict(entry):
    """One failing call's own real AI diagnosis - `entry.diagnosis` is
    guaranteed non-`None` by the caller (only entries that actually got a
    real diagnosis are passed in here). Never invents a field the real
    `RuntimeDiagnosis` doesn't have.
    """
    d = entry.diagnosis
    return {
        "method": entry.call.endpoint.method,
        "path": entry.call.endpoint.path,
        "diagnosis_status": d.diagnosis_status,
        "summary": d.summary,
        "severity": d.severity,
        "confidence": d.confidence,
        "likely_root_cause": d.likely_root_cause,
        "recommended_action": d.recommended_action,
        "affected_files": list(d.affected_files),
        "model": d.model,
        "error": d.error,
    }


# A real "QA engineer" workflow names its test cases and what each one
# checks *before* running anything slow (docs/43-web-reliability.md) -
# this rule table is that step, made fully deterministic: every string
# describes what will be *attempted* and why, never a guaranteed outcome.
# No LLM involved. Keyed on the same `(method, dynamic)` shape
# `resolution.py`'s own tiering already uses internally.
#
# Real, confirmed gap in the execution engine (`resolution.py`'s
# `resolve_and_execute`): only GET/POST/PUT/PATCH/DELETE have an execution
# tier. A discovered HEAD/OPTIONS route (all three discovery strategies
# can match one) would never get a `results[id(endpoint)]` entry there -
# `resolve_and_execute`'s own final line would raise `KeyError` if the
# server actually starts and such a route exists. Not fixed here (a
# pre-existing engine bug, out of today's scope) - described honestly
# below instead of promising a PASS/FAIL that can't actually happen.
_MUTATION_BODY_DESC = (
    "Build a request body from the target's OpenAPI schema when one is reachable (real defaults "
    "first; a required field with no default is synthesized - clearly labeled 'synthetic data' in "
    "the result, docs/45), or from field names found directly in the route's own source code when "
    "no OpenAPI schema exists. Skipped only if neither source gives any real evidence the endpoint "
    "reads a body at all."
)
_MUTATION_BODY_DYNAMIC_DESC = (
    "Resolve the dynamic path parameter from a prior passing GET response when one exists, or a "
    "synthesized id otherwise (docs/45) - and build a request body the same way "
    "_MUTATION_BODY_DESC does. Skipped only when nothing - real or synthetic - can be produced."
)
_NOT_EXECUTED_DESC = (
    "Discovered as a real route, but the live test run's execution engine does not currently attempt "
    "{} calls (only GET/POST/PUT/PATCH/DELETE are executed) - listed here for visibility only; expect "
    "no PASS/FAIL outcome after a full run."
)
_TEST_CASE_RULES = {
    ("GET", False): "Call directly and verify a 2xx response with valid JSON (when the response "
                    "claims a JSON content-type).",
    ("GET", True): "Resolve the dynamic path parameter from a prior passing GET response on the "
                   "parent collection endpoint when one exists, or a synthesized id otherwise "
                   "(docs/45) - then call and verify a 2xx response. Skipped only if the path names "
                   "more than one dynamic segment (never attempted for that shape).",
    ("POST", False): _MUTATION_BODY_DESC,
    ("POST", True): _MUTATION_BODY_DYNAMIC_DESC,
    ("PUT", False): _MUTATION_BODY_DESC,
    ("PUT", True): _MUTATION_BODY_DYNAMIC_DESC,
    ("PATCH", False): _MUTATION_BODY_DESC,
    ("PATCH", True): _MUTATION_BODY_DYNAMIC_DESC,
    ("DELETE", False): "Call directly (no request body needed for DELETE). Mutates state on the "
                       "target if it actually succeeds - only test against a disposable target.",
    ("DELETE", True): "Resolve the dynamic path parameter from a prior passing GET response when one "
                      "exists, or a synthesized id otherwise (docs/45), then call. Mutates state on "
                      "the target if it actually succeeds - only test against a disposable target.",
    ("HEAD", False): _NOT_EXECUTED_DESC.format("HEAD"),
    ("HEAD", True): _NOT_EXECUTED_DESC.format("HEAD"),
    ("OPTIONS", False): _NOT_EXECUTED_DESC.format("OPTIONS"),
    ("OPTIONS", True): _NOT_EXECUTED_DESC.format("OPTIONS"),
}


def _describe_test_case(method: str, dynamic: bool) -> str:
    return _TEST_CASE_RULES.get((method, dynamic), "Call directly and verify the response.")


def _test_case_to_dict(endpoint):
    return {
        "method": endpoint.method,
        "path": endpoint.path,
        "dynamic": endpoint.dynamic,
        "source_file": endpoint.source_file,
        "description": _describe_test_case(endpoint.method, endpoint.dynamic),
    }


def _run_plan(project_root: Path, started: float) -> dict:
    """Discovery-only (docs/43): `discover_project` + `discover_api_
    endpoints` - no npm install, no server start, no real HTTP call.
    Already proven sub-second even against a real project. Runs inside
    `asyncio.to_thread` from the route below, same as `_run_analysis`.
    """
    discovery = discover_project(str(project_root))
    _stage(started, "discovery done")
    response = {"discovery": _discovery_to_dict(discovery)}

    if discovery.project is not None:
        context = build_repository_context(discovery.project)
        endpoints, warnings = discover_api_endpoints(context, str(project_root))
        _stage(started, "test plan generated ({} case(s))".format(len(endpoints)))
        response["test_plan"] = {
            "endpoints": len(endpoints),
            "warnings": list(warnings),
            "cases": [_test_case_to_dict(e) for e in endpoints],
        }
    else:
        response["test_plan"] = {"endpoints": 0, "warnings": [], "cases": []}
    return response


@app.post("/api/plan")
async def plan(files: List[UploadFile] = File(...)):
    started = time.perf_counter()
    work_dir = Path(tempfile.gettempdir()) / "qa_agent_web" / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        project_root = await _receive_upload(files, work_dir)
    except _UploadError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    _stage(started, "upload received ({} file(s))".format(len(files)))

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_run_plan, project_root, started), timeout=OVERALL_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        _stage(started, "TIMEOUT - exceeded {:.0f}s ceiling; work_dir left in place: {}".format(
            OVERALL_DEADLINE_SECONDS, work_dir))
        return JSONResponse(
            {"error": "test plan generation exceeded the {:.0f}s time limit.".format(OVERALL_DEADLINE_SECONDS),
             "timed_out": True},
            status_code=504,
        )
    except Exception as exc:  # noqa: BLE001 - guarantee real JSON, never an unhandled-exception page
        _stage(started, "ERROR: {}".format(exc))
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": "test plan generation failed: {}".format(exc)}, status_code=500)

    shutil.rmtree(work_dir, ignore_errors=True)
    return JSONResponse(response)


def _run_analysis(
    project_root: Path, run_api_test: bool, diagnose: bool, started: float, on_progress=None,
) -> dict:
    """Everything `/api/analyze` actually does, moved out of the route
    itself (docs/43) so it can run inside `asyncio.to_thread` - FastAPI's
    single event loop would otherwise block on this module's own
    synchronous, potentially multi-minute calls (`run_api_qa`, `subprocess
    .run`) for the whole duration of every request.

    `on_progress` (optional, docs/44-live-progress.md): forwarded straight
    to `run_api_qa` - `None` (the default, what `/api/analyze` itself still
    passes) leaves this function's behavior completely unchanged; only the
    new job-based `/api/analyze/start` flow provides a real callback.
    """
    discovery = discover_project(str(project_root))
    _stage(started, "discovery done")
    response = {"discovery": _discovery_to_dict(discovery)}

    static_files, _missing = collect_paths([str(project_root)])
    static_result = run_static_checks(static_files)
    _stage(started, "static analysis done ({} finding(s))".format(len(static_result.findings)))
    response["static_analysis"] = _static_result_to_dict(static_result, project_root)

    if run_api_test and discovery.project is not None:
        context = build_repository_context(discovery.project)

        # Find the exact directory `run_api_qa` will itself launch the
        # server from (root, or a monorepo package - docs/40) and, if
        # it has its own package.json, install its real dependencies
        # first - a fresh upload never carries `node_modules` (see
        # `_npm_install`'s own docstring), so without this the server
        # would crash in under a second, every time, for any real
        # Node project.
        install_result = None
        _cmd, _evidence, server_cwd = api_qa_server.discover_server_start_command(
            str(project_root), discovery.project,
        )
        if server_cwd is not None and (Path(server_cwd) / "package.json").is_file():
            _stage(started, "running npm install in {}".format(Path(server_cwd).name))
            install_result = _npm_install(Path(server_cwd))
            _stage(started, "npm install {}".format("ok" if install_result["ok"] else "FAILED"))
            if install_result["ok"]:
                install_result = None  # nothing worth reporting when it just worked

        # A fresh upload just ran a real `npm install` moments ago -
        # give the server itself (and, on Windows, real-time antivirus
        # scanning a batch of freshly-written executables) more room
        # than the CLI's own leaner defaults assume, rather than racing
        # a cold start that the CLI's defaults were never sized for.
        api_result = run_api_qa(
            context, str(project_root),
            config=ApiQaConfig(server_startup_timeout=45.0, connect_probe_timeout=20.0),
            on_progress=on_progress,
        )
        _stage(started, "API testing done ({} call(s), {} negative case(s), {} schema check(s), server {})".format(
            len(api_result.calls), len(api_result.negative_calls), len(api_result.schema_validations),
            api_result.server_status))
        api_data = api_qa_to_dict(api_result)
        api_data["root_path"] = "."  # the real temp path is a server-side detail, never shown
        for call in api_data["calls"] + api_data["negative_calls"] + api_data["schema_validations"]:
            call["source_file"] = _relativize(call["source_file"], project_root) if Path(call["source_file"]).is_absolute() else call["source_file"]
        if install_result is not None:
            api_data["install_error"] = install_result
        response["api_qa"] = api_data

        # AI diagnosis (docs/42-groq-cloud-provider.md) - strictly
        # opt-in, and only ever sent the real evidence for calls that
        # really failed (`diagnose_and_repair_api_failures` itself
        # filters to CALL_FAIL - a pass/skip is never sent to the AI).
        # Repair is deliberately not offered here yet - diagnosis only.
        if diagnose and any(c.status == "fail" for c in api_result.calls):
            _stage(started, "running AI diagnosis (Groq)")
            provider = GroqProvider()
            entries = diagnose_and_repair_api_failures(
                api_result, context, provider, str(project_root),
                do_diagnose=True, do_repair=False,
            )
            response["diagnoses"] = [_diagnosis_entry_to_dict(e) for e in entries if e.diagnosis is not None]
            _stage(started, "AI diagnosis done ({} entr{})".format(
                len(response["diagnoses"]), "y" if len(response["diagnoses"]) == 1 else "ies"))

    return response


@app.post("/api/analyze")
async def analyze(
    files: List[UploadFile] = File(...),
    run_api_test: bool = Form(False),
    diagnose: bool = Form(False),
):
    started = time.perf_counter()
    work_dir = Path(tempfile.gettempdir()) / "qa_agent_web" / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        project_root = await _receive_upload(files, work_dir)
    except _UploadError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    _stage(started, "upload received ({} file(s))".format(len(files)))

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_run_analysis, project_root, run_api_test, diagnose, started),
            timeout=OVERALL_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        # The background thread is still running (a plain Python thread
        # cannot be forcibly stopped mid-subprocess-call, and killing
        # work_dir out from under it would just be a different, new
        # failure mode) - left in place for the OS to eventually reclaim,
        # a real accepted limitation of "no job-queue today", not hidden:
        # this response says exactly that, plainly, as real JSON.
        _stage(started, "TIMEOUT - exceeded {:.0f}s ceiling; work_dir left in place: {}".format(
            OVERALL_DEADLINE_SECONDS, work_dir))
        return JSONResponse(
            {
                "error": "analysis exceeded the {:.0f}s time limit and was stopped from the caller's "
                         "side. This can happen on a very large project or a very slow/blocked server "
                         "start. Try again with \"Also run live API tests\" unchecked, or on a smaller "
                         "project.".format(OVERALL_DEADLINE_SECONDS),
                "timed_out": True,
            },
            status_code=504,
        )
    except Exception as exc:  # noqa: BLE001 - guarantee real JSON, never an unhandled-exception page
        _stage(started, "ERROR: {}".format(exc))
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": "analysis failed: {}".format(exc)}, status_code=500)

    shutil.rmtree(work_dir, ignore_errors=True)
    return JSONResponse(response)


# --- live progress: background job + polling (docs/44-live-progress.md) ----
#
# `/api/analyze` above stays exactly as it always has - one blocking request,
# one final JSON response - so nothing that already depends on it changes.
# This is a second, additive way to run the same underlying `_run_analysis`:
# the frontend gets a job id immediately, then polls for real progress while
# the real work continues in the background. A plain in-memory registry is
# enough for a single-process local dev tool (not a durable job queue) -
# `_JOBS_LOCK` guards it since `on_progress` is invoked from the worker
# thread `asyncio.to_thread` runs `_run_analysis` on, not the event loop.

_JOBS_LOCK = threading.Lock()
_JOBS: "OrderedDict[str, dict]" = OrderedDict()
_MAX_JOBS = 30  # bounded so a long-running session never grows this unboundedly
_background_tasks: set = set()  # strong refs - an unreferenced asyncio.Task can be GC'd mid-flight


def _new_job() -> str:
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "status": "running", "done": 0, "total": 0, "label": "", "result": None, "error": None,
        }
        while len(_JOBS) > _MAX_JOBS:
            _JOBS.popitem(last=False)
    return job_id


def _update_job(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job.update(fields)


def _get_job(job_id: str) -> Optional[dict]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job is not None else None


async def _run_analysis_job(
    job_id: str, project_root: Path, run_api_test: bool, diagnose: bool, started: float, work_dir: Path,
) -> None:
    def on_progress(done, total, label):
        _update_job(job_id, done=done, total=total, label=label)

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(_run_analysis, project_root, run_api_test, diagnose, started, on_progress),
            timeout=OVERALL_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        # Same accepted limitation `/api/analyze` itself already documents -
        # a plain thread cannot be forcibly stopped mid-call, so work_dir is
        # left in place rather than risking a new failure mode underneath it.
        _stage(started, "TIMEOUT - exceeded {:.0f}s ceiling; work_dir left in place: {}".format(
            OVERALL_DEADLINE_SECONDS, work_dir))
        _update_job(
            job_id, status="error",
            error="analysis exceeded the {:.0f}s time limit and was stopped from the caller's side. "
                  "This can happen on a very large project or a very slow/blocked server start. Try "
                  "again with \"Also run live API tests\" unchecked, or on a smaller "
                  "project.".format(OVERALL_DEADLINE_SECONDS),
        )
        return
    except Exception as exc:  # noqa: BLE001 - guarantee a real, terminal job state, never a stuck "running"
        _stage(started, "ERROR: {}".format(exc))
        shutil.rmtree(work_dir, ignore_errors=True)
        _update_job(job_id, status="error", error="analysis failed: {}".format(exc))
        return

    shutil.rmtree(work_dir, ignore_errors=True)
    _update_job(job_id, status="done", result=response)


@app.post("/api/analyze/start")
async def analyze_start(
    files: List[UploadFile] = File(...),
    run_api_test: bool = Form(False),
    diagnose: bool = Form(False),
):
    started = time.perf_counter()
    work_dir = Path(tempfile.gettempdir()) / "qa_agent_web" / uuid.uuid4().hex
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        project_root = await _receive_upload(files, work_dir)
    except _UploadError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return JSONResponse({"error": str(exc)}, status_code=exc.status_code)
    _stage(started, "upload received ({} file(s))".format(len(files)))

    job_id = _new_job()
    task = asyncio.create_task(
        _run_analysis_job(job_id, project_root, run_api_test, diagnose, started, work_dir)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return JSONResponse({"job_id": job_id})


@app.get("/api/analyze/status/{job_id}")
async def analyze_status(job_id: str):
    job = _get_job(job_id)
    if job is None:
        return JSONResponse({"error": "unknown job id"}, status_code=404)
    payload = {"status": job["status"], "done": job["done"], "total": job["total"], "label": job["label"]}
    if job["status"] == "done":
        payload["result"] = job["result"]
    elif job["status"] == "error":
        payload["error"] = job["error"]
    return JSONResponse(payload)


_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
