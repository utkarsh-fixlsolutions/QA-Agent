"""Next.js App Router route-handler discovery (Phase G5-adjacent, docs/30
-api-qa-v1.md): `discover_api_endpoints(context, root)`.

Scoped, evidence-based, and deliberately narrow, exactly like `qa_agent.
project.detectors`'s own manifest reads: a real `route.ts`/`route.js` file
is read (capped, same `_MAX_READ_BYTES` philosophy as detectors.py's own
`_MAX_MANIFEST_BYTES`) only to find its real exported HTTP method
functions via a regex over source text - never a full TypeScript/JS parse,
and never a guess at a method that is not actually exported. A route file
with no recognized export is reported as a warning, not silently dropped
and not fabricated into an endpoint that was never really there.

Only Next.js **App Router** route handlers are discovered here (v1's
explicit, documented scope - see docs/30). Pages Router (`pages/api/**`)
and Express (`app.get(...)`, `router.post(...)`) are real, common patterns
this module does not attempt to detect at all in v1, deliberately, rather
than half-supporting either unreliably.
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


def discover_api_endpoints(context, root):
    """Returns `(endpoints, warnings)`. Never raises - any per-file read
    failure is skipped with a warning, exactly like `run_runtime_plan`'s
    own "one check's failure never stops the rest" discipline, applied
    here to "one route file's failure never stops discovery of the rest".
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
