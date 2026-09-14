"""API endpoint discovery (Phase G5-adjacent, docs/30-api-qa-v1.md; docs/32
-fastapi-discovery-and-startup.md): `discover_api_endpoints(context, root)`.

Two independent, additive strategies, combined by the one public function
at the bottom of this file - neither replaces the other:

- `_discover_nextjs_endpoints` (Step 30, unchanged): real `route.ts`/
  `route.js` files under an `app/`-named directory, read (capped, same
  `_MAX_READ_BYTES` philosophy as `project.detectors`'s own
  `_MAX_MANIFEST_BYTES`) only to find real exported HTTP method functions
  via a regex over source text - never a full TypeScript/JS parse.
- `_discover_fastapi_endpoints` (new): real `@app.<method>(...)`/
  `@router.<method>(...)` decorators in `.py` files, found the same
  "regex over text, never a full parse" way - gated behind a real,
  already-detected FastAPI framework fact (`project.frameworks`), so an
  unrelated Python project's own incidental `@something.get(...)`-shaped
  text is never mistaken for a real route.

A route with no recognized handler is reported as a warning, not silently
dropped and not fabricated into an endpoint that was never really there.

Pages Router (`pages/api/**`), Express (`app.get(...)` in a `.js`/`.ts`
file), and a FastAPI `APIRouter`'s own `prefix=` composition are real,
common patterns this module does not attempt to resolve, deliberately,
rather than half-supporting any of them unreliably - named here, not
silently gapped.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .models import METHODS, ApiEndpoint

_MAX_READ_BYTES = 2_000_000

# A route file living inside any of these directories is never a real,
# reachable Next.js route handler (build output, dependencies, VCS
# metadata) - excluded the same way project/discovery.py's own
# IGNORED_DIR_NAMES excludes them from the original repository walk,
# duplicated narrowly here rather than importing that module's private
# list into an unrelated package.
_IGNORED_DIR_NAMES = frozenset({
    "node_modules", ".git", ".next", ".turbo", "coverage", "dist", "build", "out",
})

_ROUTE_FILE_NAMES = ("route.ts", "route.js")

_EXPORT_FUNCTION_RE = re.compile(
    r"export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*\("
)
_EXPORT_CONST_RE = re.compile(
    r"export\s+const\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s*[:=]"
)

_ROUTE_GROUP_RE = re.compile(r"^\(.*\)$")
_DYNAMIC_SEGMENT_RE = re.compile(r"\[.*\]")


def _read_text_capped(path):
    try:
        if path.stat().st_size > _MAX_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _find_route_files(app_dir_abs):
    """Every `route.ts`/`route.js` under `app_dir_abs`, walked once,
    pruning ignored directories before descending - the same "prune, don't
    filter afterward" technique project/discovery.py's own `_walk_once`
    already established, applied here to a much smaller subtree.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(app_dir_abs, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        for name in filenames:
            if name in _ROUTE_FILE_NAMES:
                found.append(Path(dirpath) / name)
    return found


def _url_path_from_route_file(app_dir_abs, route_file_abs):
    """`app/api/companions/[id]/route.ts` -> `/api/companions/[id]`.
    Route-group segments (`(marketing)`) are stripped entirely - a
    documented Next.js convention (they organize files without affecting
    the URL), not a guess. Dynamic segments (`[id]`, `[...slug]`) are kept
    literally, exactly as they appear on disk, so `dynamic` detection and
    human-readable reporting both stay honest about what was really found.
    """
    rel_dir = route_file_abs.parent.relative_to(app_dir_abs)
    segments = [seg for seg in rel_dir.parts if not _ROUTE_GROUP_RE.match(seg)]
    return "/" + "/".join(segments)


def _extract_methods(text):
    found = set(_EXPORT_FUNCTION_RE.findall(text)) | set(_EXPORT_CONST_RE.findall(text))
    return tuple(m for m in METHODS if m in found)


def _discover_nextjs_endpoints(context, root):
    """Returns `(endpoints, warnings)`. Never raises - any per-file read
    failure is skipped with a warning, exactly like `run_runtime_plan`'s
    own "one check's failure never stops the rest" discipline, applied
    here to "one route file's failure never stops discovery of the rest".
    Unchanged from Step 30 - only its name changed, to make room for a
    second strategy below.
    """
    root = Path(root)
    project = context.project
    app_dirs = tuple(
        d for d in project.important_directories if Path(d).name == "app"
    )

    endpoints = []
    warnings = []
    seen = set()

    for app_dir_rel in app_dirs:
        app_dir_abs = (root / app_dir_rel).resolve()
        if not app_dir_abs.is_dir():
            # Detected during the original repository walk, but no longer
            # present now - reported honestly rather than silently skipped.
            warnings.append("app directory no longer present: {}".format(app_dir_rel))
            continue
        for route_file_abs in _find_route_files(app_dir_abs):
            text = _read_text_capped(route_file_abs)
            source_file = route_file_abs.relative_to(root).as_posix()
            if text is None:
                warnings.append("could not read route file: {}".format(source_file))
                continue
            methods = _extract_methods(text)
            if not methods:
                warnings.append(
                    "no recognized exported HTTP handler (GET/POST/...) in {}".format(source_file)
                )
                continue
            url_path = _url_path_from_route_file(app_dir_abs, route_file_abs)
            dynamic = bool(_DYNAMIC_SEGMENT_RE.search(url_path))
            for method in methods:
                key = (method, url_path)
                if key in seen:
                    continue
                seen.add(key)
                endpoints.append(ApiEndpoint(
                    method=method, path=url_path, source_file=source_file, dynamic=dynamic,
                ))

    endpoints.sort(key=lambda e: (e.path, e.method))
    return tuple(endpoints), tuple(warnings)


# --- Python / FastAPI strategy (new) ----------------------------------------

# Python-specific noise directories a route scan should never descend into -
# on top of the Next.js strategy's own `_IGNORED_DIR_NAMES`, which already
# covers the VCS/build-output cases shared by both ecosystems.
_PY_IGNORED_DIR_NAMES = frozenset({
    ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", "site-packages",
})

# `@app.get("/x")` / `@router.post('/y')` - a route decorator naming a real
# HTTP method and a real, literal path string. Deliberately does not match
# a keyword-only path (`@app.get(path="/x")`) or a decorator whose path
# argument is a variable/expression rather than a literal string - both
# real, documented scope boundaries (this module's own module docstring),
# not silently mishandled.
_PYTHON_ROUTE_DECORATOR_RE = re.compile(
    r"@(?:app|router)\.(get|post|put|patch|delete|options|head)\s*\(\s*[\"']([^\"']*)[\"']",
    re.IGNORECASE,
)
_FASTAPI_DYNAMIC_SEGMENT_RE = re.compile(r"\{[^}]+\}")


def _find_python_files(root_abs):
    """Every `.py` file under `root_abs`, walked once, pruning both the
    shared and the Python-specific ignored directories before descending -
    the same "prune, don't filter afterward" technique the Next.js
    strategy's own `_find_route_files` already uses.
    """
    ignored = _IGNORED_DIR_NAMES | _PY_IGNORED_DIR_NAMES
    found = []
    for dirpath, dirnames, filenames in os.walk(root_abs, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in ignored]
        for name in filenames:
            if name.endswith(".py"):
                found.append(Path(dirpath) / name)
    return found


def _is_fastapi_project(project):
    return any(item.name == "FastAPI" for item in project.frameworks)


def _discover_fastapi_endpoints(context, root):
    """Returns `(endpoints, warnings)`. Never attempted at all unless
    `project.frameworks` already, really contains "FastAPI" (Phase F's own
    detection, reused rather than re-implemented here) - the same
    evidence-gated scoping the Next.js strategy applies via its own
    `app`-named-directory rule. A real `.py` file's own content is read
    (capped) only to find real `@app.<method>(...)`/`@router.<method>(...)`
    decorators via a regex over source text - never a full AST parse, never
    a guessed route. `APIRouter(prefix=...)` composition is not resolved -
    a real, documented scope boundary (this module's own module docstring).
    """
    project = context.project
    if not _is_fastapi_project(project):
        return (), ()

    root = Path(root)
    endpoints = []
    warnings = []

    for py_file in _find_python_files(root):
        text = _read_text_capped(py_file)
        source_file = py_file.relative_to(root).as_posix()
        if text is None:
            warnings.append("could not read Python file: {}".format(source_file))
            continue
        for match in _PYTHON_ROUTE_DECORATOR_RE.finditer(text):
            path = match.group(2)
            if not path:
                # A decorator matched with an empty string literal path -
                # not a real, callable route; never fabricated into one.
                continue
            method = match.group(1).upper()
            line = text.count("\n", 0, match.start()) + 1
            dynamic = bool(_FASTAPI_DYNAMIC_SEGMENT_RE.search(path))
            endpoints.append(ApiEndpoint(
                method=method, path=path, source_file=source_file, dynamic=dynamic, line=line,
            ))

    endpoints.sort(key=lambda e: (e.path, e.method, e.source_file))
    return tuple(endpoints), tuple(warnings)


# --- combined entry point ----------------------------------------------

def discover_api_endpoints(context, root):
    """The one public entry point: runs every strategy above and merges
    the results - never one replacing the other. A strategy that finds
    nothing (its own gating evidence absent) contributes an empty result,
    so a pure Next.js project's own output is unaffected by the Python
    strategy existing at all, and vice versa - proven directly by the
    existing Next.js regression suite continuing to pass unmodified.

    Deduplicated by `(method, path)` across *both* strategies combined (in
    the unlikely event the same method+path were somehow found by each -
    never expected in practice, since they gate on mutually exclusive
    framework evidence, but guarded anyway rather than assumed impossible).
    """
    root = Path(root)
    nextjs_endpoints, nextjs_warnings = _discover_nextjs_endpoints(context, root)
    fastapi_endpoints, fastapi_warnings = _discover_fastapi_endpoints(context, root)

    endpoints = []
    seen = set()
    for endpoint in list(nextjs_endpoints) + list(fastapi_endpoints):
        key = (endpoint.method, endpoint.path)
        if key in seen:
            continue
        seen.add(key)
        endpoints.append(endpoint)

    endpoints.sort(key=lambda e: (e.path, e.method))
    warnings = tuple(nextjs_warnings) + tuple(fastapi_warnings)
    return tuple(endpoints), warnings
