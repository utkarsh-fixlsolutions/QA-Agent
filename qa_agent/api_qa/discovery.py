"""API endpoint discovery (Phase G5-adjacent, docs/30-api-qa-v1.md; docs/32
-fastapi-discovery-and-startup.md; docs/38-broader-route-discovery.md;
Phase 4, generalized discovery): `discover_api_endpoints(context, root)`,
and the richer `discover_api_endpoints_detailed(context, root)` it wraps.

Five independent, additive discovery strategies, combined by
`discover_api_endpoints_detailed` at the bottom of this file - none
replaces any other, and each is isolated behind its own function boundary
so a future AST/parser-based strategy could supplement or replace one
without redesigning this pipeline:

- `_discover_nextjs_endpoints` (Step 30, unchanged): real `route.ts`/
  `route.js` files under an `app/`-named directory, read (capped, same
  `_MAX_READ_BYTES` philosophy as `project.detectors`'s own
  `_MAX_MANIFEST_BYTES`) only to find real exported HTTP method functions
  via a regex over source text - never a full TypeScript/JS parse.
- `_discover_fastapi_endpoints` (new in docs/32): real `@app.<method>(...)`/
  `@router.<method>(...)` decorators in `.py` files, found the same
  "regex over text, never a full parse" way - gated behind a real,
  already-detected FastAPI framework fact (`project.frameworks`), so an
  unrelated Python project's own incidental `@something.get(...)`-shaped
  text is never mistaken for a real route.
- `_discover_nextjs_pages_endpoints` (new in docs/38): real `.ts`/`.js`
  files under a `pages/api/`-named directory with a real `export default`
  handler - gated behind a real, already-detected Next.js framework fact
  (unlike the App Router strategy, a bare `pages`-named directory alone is
  far too common outside Next.js to be trustworthy evidence by itself).
  Method(s) are only ever reported when the handler's own source contains a
  real `req.method === 'X'`/`case 'X':` check for that method - Pages
  Router's one-handler-for-every-method shape means "no method check
  found" is reported as a real, honest warning (GET assumed, explicitly
  flagged as an assumption) rather than silently guessed.
- `_discover_express_endpoints` (new in docs/38): real
  `app.<method>('/path', ...)`/`router.<method>('/path', ...)` calls in
  `.js`/`.ts` files, gated behind a real, already-detected Express
  framework fact - the same decorator-shaped "one real call site, one real
  method, one real literal path string" discipline the FastAPI strategy
  already established, applied to Express's own real API shape. Phase 4
  added recognition of `router.route(path).verb(...)` chains (including
  multi-verb chains) alongside the original direct-call shape, and a path
  argument that is a template literal with real interpolation or a bare
  variable/expression is reported as an `UnresolvedRoute` - a real,
  distinct fact - instead of being silently dropped or fabricated into a
  fake concrete path.
- `_discover_openapi_endpoints` (Phase 4): a static, on-disk OpenAPI/
  Swagger document's own `paths` object (JSON or YAML, found by
  `resolution.find_static_openapi_schema_detailed`), walked to create
  endpoints independently of any source-code discovery - it runs and can
  find endpoints even when every source strategy above finds zero, and a
  malformed/oversized/unusable document is reported as a real warning,
  never silently ignored.

A route with no recognized handler is reported as a warning, not silently
dropped and not fabricated into an endpoint that was never really there.

Router mount/composition - Express `app.use(prefix, router)` and FastAPI
`include_router(router, prefix=...)` - is resolved by a separate, additive
pass (`route_composition.py`) applied after the source strategies above:
it builds each file's own import/require graph, resolves mount call sites
to the file they mount, and walks that graph (bounded depth, cycle-safe)
from every real root to compute each route's true, accumulated prefix.
Only relative Python imports (`from .x import y`) are resolved - resolving
project-absolute imports would require guessing `sys.path`/package-root
conventions this module has no way to know, so it does not try.

When two independent strategies (e.g. Express source and an OpenAPI
document) describe the same `(method, path)`, `_merge_endpoints_with_
provenance` folds them into one `ApiEndpoint` with combined `discovered_by`
provenance rather than reporting a duplicate.

`DiscoveryOutcome` (returned by the `_detailed` entry point) also reports,
honestly and separately: `unresolved_routes` (real route-defining
constructs whose path could not be resolved - never fed into HTTP
execution), `strategy_counts` (how many endpoints each strategy actually
contributed), and `unsupported_frameworks` (a detected framework with no
discovery strategy at all, e.g. Flask/NestJS/Django) - three genuinely
different reasons a project can show zero endpoints, never collapsed into
one misleading message. Discovery finding zero endpoints never by itself
prevents the server-startup attempt that follows it in `runner.py`.

Phase 2 (API contract understanding, docs/53) adds one more, additive
whole-project pass after the four strategies above merge their results:
`test_evidence.build_test_evidence_registry` finds real example request
bodies in the project's own existing test files/Postman collections and
attaches them to every mutating endpoint they structurally match
(`ApiEndpoint.test_evidence_fields`) - the same "registry built once,
attached to the endpoints that reference it" shape the Zod registry below
already established for schema evidence.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

from . import model_schema as _model_schema
from . import route_composition as _route_composition
from . import test_evidence as _test_evidence
from . import zod_schema as _zod_schema
from .models import METHODS, ApiEndpoint, UnresolvedRoute
from .resolution import find_static_openapi_schema_detailed

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

# --- request-body field hints (docs/45-synthetic-mutation-testing.md) ------
#
# A narrow, named regex-over-text scope - not a JS/TS parser - matching the
# handful of overwhelmingly common ways a Next.js/Express handler reads its
# own request body. Anything outside these shapes (a custom body-parsing
# helper, an unusual variable name deep inside a helper function) is not
# recognized - `reads_request_body` simply stays `False` for that endpoint,
# the same "named scope boundary, never silently mishandled" discipline
# every other strategy in this module already follows.
MUTATION_METHODS = ("POST", "PUT", "PATCH", "DELETE")

# `const { a, b } = await request.json()` / `await req.json()` (Next.js
# App Router's own real request object, or a common `req` alias for it).
# The `(?:const|let|var)` anchor is required, not cosmetic: without it,
# `[^}]+` happily matches across an *unrelated* earlier `{` (e.g. a
# function body's own opening brace) all the way to the destructure's own
# closing `}`, capturing "function POST(request) {\n  const { title" as one
# bogus "field name" instead of the real destructured identifiers - found
# via a real end-to-end test (docs/45) that caught exactly this.
_JSON_DESTRUCTURE_RE = re.compile(
    r"(?:const|let|var)\s*\{\s*([^}]+)\}\s*=\s*await\s+(?:request|req)\.json\(\s*\)"
)
# Bare (non-destructured) form of the same read - no field names available
# from this shape alone, but still real evidence the handler reads a body.
_JSON_BARE_RE = re.compile(
    r"=\s*await\s+(?:request|req)\.json\(\s*\)"
)
# `const { a, b } = req.body` (Express / Next.js Pages Router) - same
# `const`/`let`/`var` anchor, for the same reason as `_JSON_DESTRUCTURE_RE`.
_BODY_DESTRUCTURE_RE = re.compile(r"(?:const|let|var)\s*\{\s*([^}]+)\}\s*=\s*req\.body\b")
# `req.body.fieldName` direct property access (Express / Pages Router).
_BODY_ACCESS_RE = re.compile(r"req\.body\.([A-Za-z_$][A-Za-z0-9_$]*)")
# Bare `req.body` used on its own (no destructure, no `.field` access) -
# still real evidence of a body read, no field names from this shape alone.
_BODY_BARE_RE = re.compile(r"req\.body\b(?!\s*\.\s*[A-Za-z_$])")

_DESTRUCTURE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*")


def _names_from_destructure(group_text):
    """`{ a, b: renamed, c = defaultVal, ...rest }` -> `("a", "b", "c")` -
    each comma-separated entry's own real bound identifier, a `: rename`
    or `= default` clause discarded (the real request field is the name
    *before* either), and a `...rest` spread entry dropped entirely (it
    names no single real field). Never guesses a name that isn't literally
    present in the source text.
    """
    names = []
    for part in group_text.split(","):
        part = part.strip()
        if not part or part.startswith("..."):
            continue
        match = _DESTRUCTURE_IDENTIFIER_RE.match(part)
        if match:
            names.append(match.group(0))
    return names


def _extract_body_field_hints(text):
    """Returns `(field_hints, reads_body)` for one route file's full text -
    every recognized request-body read pattern is checked (not just the
    first match), field names deduplicated in first-seen order across all
    of them. `reads_body` is `True` whenever any recognized shape (bare or
    destructured) was found, even if no specific field name could be
    extracted from it - `build_request_body`'s own minimal-fallback case
    (docs/45) depends on this distinction.
    """
    seen = set()
    hints = []

    def _add(name):
        if name not in seen:
            seen.add(name)
            hints.append(name)

    reads_body = False
    for match in _JSON_DESTRUCTURE_RE.finditer(text):
        reads_body = True
        for name in _names_from_destructure(match.group(1)):
            _add(name)
    for match in _BODY_DESTRUCTURE_RE.finditer(text):
        reads_body = True
        for name in _names_from_destructure(match.group(1)):
            _add(name)
    for match in _BODY_ACCESS_RE.finditer(text):
        reads_body = True
        _add(match.group(1))
    if not hints:
        if _JSON_BARE_RE.search(text) or _BODY_BARE_RE.search(text):
            reads_body = True

    return tuple(hints), reads_body


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


def _discover_nextjs_endpoints(context, root, zod_registry=None):
    """Returns `(endpoints, warnings)`. Never raises - any per-file read
    failure is skipped with a warning, exactly like `run_runtime_plan`'s
    own "one check's failure never stops the rest" discipline, applied
    here to "one route file's failure never stops discovery of the rest".
    Unchanged from Step 30 - only its name changed, to make room for a
    second strategy below.

    `zod_registry` (docs/50-zod-schema-discovery.md, optional): a real,
    whole-project map of Zod schema variable name -> its own real required
    fields (`zod_schema.build_zod_schema_registry`), built once by the
    combined entry point below. When a mutating route's own handler text
    references one of those real schema names, its real fields are
    attached as `ApiEndpoint.zod_fields` - stronger evidence than
    `body_field_hints` alone (a real type, not just a name).
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
            hints, reads_body = _extract_body_field_hints(text)
            zod_fields = _zod_schema.find_referenced_schema_fields(text, zod_registry) if zod_registry else ()
            for method in methods:
                key = (method, url_path)
                if key in seen:
                    continue
                seen.add(key)
                is_mutation = method in MUTATION_METHODS
                endpoints.append(ApiEndpoint(
                    method=method, path=url_path, source_file=source_file, dynamic=dynamic,
                    body_field_hints=hints if is_mutation else (),
                    reads_request_body=reads_body if is_mutation else False,
                    zod_fields=zod_fields if is_mutation else (),
                    discovered_by=("nextjs-app-router",),
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
# Phase 4: the same broadened, classify-then-decide shape
# `_EXPRESS_DIRECT_CALL_RE` established - a decorator whose path argument is
# not a plain string literal (an f-string with `{...}` interpolation, or a
# bare variable/constant) is no longer simply invisible; it is recognized
# and, when unresolvable, reported as a real `UnresolvedRoute` instead of
# silently dropped.
_PYTHON_ROUTE_DECORATOR_ANY_ARG_RE = re.compile(
    r"@(?:app|router)\.(get|post|put|patch|delete|options|head)\s*\(\s*([^,)]+)",
    re.IGNORECASE,
)
_FASTAPI_DYNAMIC_SEGMENT_RE = re.compile(r"\{[^}]+\}")


def _classify_python_route_path_arg(raw):
    """The Python/FastAPI equivalent of `_classify_route_path_arg`
    (Express's own version, later in this file): a real single/double-
    quoted literal, an f-string (classified as a template only when it
    actually contains `{...}` interpolation - a plain `f"/x"` with no
    interpolation at all is really just a literal), or a bare variable/
    expression.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return "literal", raw[1:-1]
    if len(raw) >= 3 and raw[0] in "fF" and raw[1] in "\"'" and raw[-1] == raw[1]:
        inner = raw[2:-1]
        return ("template", inner) if "{" in inner else ("literal", inner)
    return "variable", raw

# --- request-body-read evidence, Python/FastAPI (docs/47) ------------------
#
# Deliberately narrower than the JS/TS strategies' own `body_field_hints`
# (docs/45): this only ever detects THAT a handler reads a request body,
# never WHICH fields - resolving a `payload: SomeModel` parameter's own
# real field names would need locating and parsing that model's class
# definition (possibly in another file entirely), a real, named scope
# boundary this module does not attempt (the same "no Zod/Joi/Pydantic-
# model AST parsing" boundary docs/45 already drew for the JS side, applied
# here to its Python equivalent). Built because the assumption docs/45
# originally made - "FastAPI already serves a live OpenAPI document, so it
# needs no source-derived fallback" - turned out false for a real,
# security-conscious FastAPI project that disables `/openapi.json`
# entirely, leaving every one of its POST/PUT/PATCH/DELETE endpoints with
# no fallback at all and a wall of honest skips.
#
# `payload: SomeModel` / `body: SomeModel` - a real, capitalized type
# annotation, the overwhelmingly common way a FastAPI handler declares a
# Pydantic request body. `Optional[X]`/`List[X]` are peeked through to
# reach the real inner type name. A small stoplist excludes FastAPI/stdlib
# types that are never a request body themselves - matched by the same
# "narrow, named convention, not a full parser" discipline as the rest of
# this module; a differently-wrapped annotation (`Annotated[X, Body()]`,
# a body split across multiple named parameters) is a real, accepted gap,
# not silently mishandled.
_FASTAPI_BODY_PARAM_RE = re.compile(
    r":\s*(?:Optional\[|List\[)?([A-Z][A-Za-z0-9_]*)\b"
)
_FASTAPI_NON_BODY_PARAM_TYPES = frozenset({
    "Request", "Response", "HTTPException", "BackgroundTasks", "UploadFile",
    "WebSocket", "Session", "AsyncSession", "UUID", "Path", "Query", "Header",
    "Cookie", "Body", "Form", "File", "Depends", "Security", "Annotated",
    "Optional", "List", "Dict", "Any", "Union", "Type", "Request",
})
# `body = await request.json()` / `await req.json()` - Python's own bare-
# assignment equivalent of the JS strategies' own `_JSON_BARE_RE` (no
# `const`/`let`/`var` exists in Python, so no such anchor is needed or
# possible here).
_PY_JSON_BARE_RE = re.compile(r"=\s*await\s+(?:request|req)\.json\(\s*\)")

# The per-route window this evidence is searched within is capped, not the
# whole rest of the file up to the next route - the same "cap the search,
# never scan unbounded real text" convention `_MAX_READ_BYTES` already
# establishes for this module - so a long handler's own unrelated, deeper
# local-variable type annotations are far less likely to be mistaken for a
# body parameter than a full, unbounded function-body scan would risk.
_PY_BODY_EVIDENCE_WINDOW_CHARS = 2000


def _reads_request_body_python(handler_text):
    """`True` when `handler_text` (the real source between one route
    decorator and the next, or end of file - the same per-route windowing
    the Express strategy already uses) shows real evidence of reading a
    request body, by either recognized shape above. Never returns field
    names - see this section's own module-level docstring for why.
    """
    if _PY_JSON_BARE_RE.search(handler_text):
        return True
    for match in _FASTAPI_BODY_PARAM_RE.finditer(handler_text):
        if match.group(1) not in _FASTAPI_NON_BODY_PARAM_TYPES:
            return True
    return False


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
    """Returns `(endpoints, warnings, unresolved_routes, files_text)`.
    Never attempted at all unless `project.frameworks` already, really
    contains "FastAPI" (Phase F's own detection, reused rather than
    re-implemented here) - the same evidence-gated scoping the Next.js
    strategy applies via its own `app`-named-directory rule. A real `.py`
    file's own content is read (capped) only to find real
    `@app.<method>(...)`/`@router.<method>(...)` decorators via a regex
    over source text - never a full AST parse, never a guessed route. A
    decorator whose path argument is not a plain string literal (Phase 4)
    is reported as an `UnresolvedRoute` rather than silently skipped - see
    `_classify_python_route_path_arg`. `include_router(prefix=...)`
    composition is resolved separately, by `route_composition.
    apply_fastapi_composition`, using `files_text` returned here.
    """
    project = context.project
    if not _is_fastapi_project(project):
        return (), (), (), {}

    root = Path(root)
    endpoints = []
    warnings = []
    unresolved = []
    files_text = {}

    for py_file in _find_python_files(root):
        text = _read_text_capped(py_file)
        source_file = py_file.relative_to(root).as_posix()
        if text is None:
            warnings.append("could not read Python file: {}".format(source_file))
            continue
        files_text[py_file.resolve()] = text
        # Route decorators in source order, so each one's own handler can
        # be approximated as "the text up to the next route decorator in
        # this same file" (bounded, see `_PY_BODY_EVIDENCE_WINDOW_CHARS`) -
        # the same technique the Express strategy already uses for its own
        # per-route `body_field_hints` windowing.
        matches = list(_PYTHON_ROUTE_DECORATOR_ANY_ARG_RE.finditer(text))
        for i, match in enumerate(matches):
            method = match.group(1).upper()
            line = text.count("\n", 0, match.start()) + 1
            kind, value = _classify_python_route_path_arg(match.group(2))
            if kind != "literal":
                reason = (
                    "path segment(s) inside the f-string cannot be established statically"
                    if kind == "template" else
                    "path argument is a variable/expression, not a string literal - cannot be "
                    "resolved statically"
                )
                unresolved.append(UnresolvedRoute(
                    method=method, raw_expression=match.group(2).strip(),
                    source_file=source_file, line=line, reason=reason,
                ))
                continue
            path = value
            if not path:
                # A decorator matched with an empty string literal path -
                # not a real, callable route; never fabricated into one.
                continue
            dynamic = bool(_FASTAPI_DYNAMIC_SEGMENT_RE.search(path))
            reads_body = False
            if method in MUTATION_METHODS:
                next_start = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                window_end = min(next_start, match.end() + _PY_BODY_EVIDENCE_WINDOW_CHARS)
                reads_body = _reads_request_body_python(text[match.end():window_end])
            endpoints.append(ApiEndpoint(
                method=method, path=path, source_file=source_file, dynamic=dynamic, line=line,
                reads_request_body=reads_body, discovered_by=("fastapi-source",),
            ))

    endpoints.sort(key=lambda e: (e.path, e.method, e.source_file))
    return tuple(endpoints), tuple(warnings), tuple(unresolved), files_text


# --- Next.js Pages Router strategy (new) -------------------------------

def _is_nextjs_project(project):
    return any(item.name == "Next.js" for item in project.frameworks)


_PAGES_API_FILE_RE = re.compile(r"\.(ts|js)$")
_PAGES_API_IGNORED_FILE_RE = re.compile(r"\.(d\.ts|test\.[tj]s|spec\.[tj]s)$")

_DEFAULT_EXPORT_RE = re.compile(r"export\s+default\b")

# Only a real, literal equality check against `req.method` - never a
# negation (`!==`), which names a method being excluded, not one actually
# handled. Both the common `if (req.method === 'GET')` shape and a
# `switch (req.method) { case 'GET':` shape are real, common, and checked
# for - never a full JS parse, the same "regex over text" discipline every
# other strategy in this module already follows.
_PAGES_METHOD_EQ_RE = re.compile(
    r"req\.method\s*===?\s*[\"'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)[\"']", re.IGNORECASE
)
_PAGES_METHOD_CASE_RE = re.compile(
    r"case\s*[\"'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)[\"']\s*:", re.IGNORECASE
)


def _find_pages_api_files(pages_api_dir_abs):
    """Every real `.ts`/`.js` file under `pages_api_dir_abs`, walked once,
    pruning ignored directories - the same "prune, don't filter afterward"
    technique every other strategy in this module already uses. Type
    declaration and test files are real files that are never real route
    handlers, excluded by name rather than silently mismatched.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(pages_api_dir_abs, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        for name in filenames:
            if _PAGES_API_FILE_RE.search(name) and not _PAGES_API_IGNORED_FILE_RE.search(name):
                found.append(Path(dirpath) / name)
    return found


def _url_path_from_pages_file(pages_dir_abs, route_file_abs):
    """`pages/api/companions/[id].ts` -> `/api/companions/[id]`;
    `pages/api/companions/index.ts` -> `/api/companions` - Pages Router's
    own real, documented `index` convention (unlike App Router, the file
    itself *is* the route - there is no route-group-folder syntax to strip
    here).
    """
    rel = route_file_abs.relative_to(pages_dir_abs)
    stem_parts = list(rel.parts[:-1]) + [rel.stem]
    if stem_parts and stem_parts[-1] == "index":
        stem_parts = stem_parts[:-1]
    return "/" + "/".join(stem_parts)


def _extract_pages_methods(text):
    """Returns `(methods, assumed)`. `methods` is every HTTP method this
    handler's own source really, textually checks `req.method` against,
    deduplicated and ordered like `METHODS`; when none are found, `methods`
    is `("GET",)` and `assumed` is `True` - Pages Router hands every method
    to the same one default-exported function, so "no explicit check
    found" genuinely means "this handler answers every method the same
    way", most commonly only ever exercised via GET. Reported as a real,
    explicit assumption (docs/38), the same "document the assumption,
    never silently assume" convention `runner.py`'s own `_resolve_base_url`
    already established for the default Next.js port - never silently
    treated as fact.
    """
    found = set(_PAGES_METHOD_EQ_RE.findall(text)) | set(_PAGES_METHOD_CASE_RE.findall(text))
    if not found:
        return ("GET",), True
    return tuple(m.upper() for m in METHODS if m in {f.upper() for f in found}), False


def _discover_nextjs_pages_endpoints(context, root, zod_registry=None):
    """Returns `(endpoints, warnings)`. Never attempted at all unless
    `project.frameworks` already, really contains "Next.js" - a bare
    `pages`-named directory alone is far too common outside Next.js
    (unlike `route.ts`/`route.js`'s own distinctive App Router filenames)
    to be trustworthy evidence by itself.

    `zod_registry`: see `_discover_nextjs_endpoints`'s own docstring.
    """
    project = context.project
    if not _is_nextjs_project(project):
        return (), ()

    root = Path(root)
    pages_dirs = tuple(d for d in project.important_directories if Path(d).name == "pages")

    endpoints = []
    warnings = []
    seen = set()

    for pages_dir_rel in pages_dirs:
        pages_dir_abs = (root / pages_dir_rel).resolve()
        api_dir_abs = pages_dir_abs / "api"
        if not api_dir_abs.is_dir():
            continue
        for route_file_abs in _find_pages_api_files(api_dir_abs):
            text = _read_text_capped(route_file_abs)
            source_file = route_file_abs.relative_to(root).as_posix()
            if text is None:
                warnings.append("could not read Pages Router API file: {}".format(source_file))
                continue
            if not _DEFAULT_EXPORT_RE.search(text):
                warnings.append(
                    "no 'export default' handler found in {}".format(source_file)
                )
                continue
            methods, assumed = _extract_pages_methods(text)
            if assumed:
                warnings.append(
                    "{} has no 'req.method' check - assuming it answers GET only; it may "
                    "really answer other methods too".format(source_file)
                )
            url_path = _url_path_from_pages_file(pages_dir_abs, route_file_abs)
            dynamic = bool(_DYNAMIC_SEGMENT_RE.search(url_path))
            hints, reads_body = _extract_body_field_hints(text)
            zod_fields = _zod_schema.find_referenced_schema_fields(text, zod_registry) if zod_registry else ()
            for method in methods:
                key = (method, url_path)
                if key in seen:
                    continue
                seen.add(key)
                is_mutation = method in MUTATION_METHODS
                endpoints.append(ApiEndpoint(
                    method=method, path=url_path, source_file=source_file, dynamic=dynamic,
                    body_field_hints=hints if is_mutation else (),
                    reads_request_body=reads_body if is_mutation else False,
                    zod_fields=zod_fields if is_mutation else (),
                    discovered_by=("nextjs-pages-router",),
                ))

    endpoints.sort(key=lambda e: (e.path, e.method))
    return tuple(endpoints), tuple(warnings)


# --- Express strategy (new) ---------------------------------------------

def _is_express_project(project):
    return any(item.name == "Express" for item in project.frameworks)


# A route-registering call's first argument, in every real shape this
# module recognizes: a template literal (`` `/x/${y}` ``), a double- or
# single-quoted literal, a plain identifier/property-path (`pathVar`,
# `routes.path`), or - the catch-all last alternative - anything else up to
# the next top-level comma/paren, so a call site is never simply invisible
# just because its argument doesn't match one of the first three shapes.
# `app`/`router` are Express's own overwhelmingly standard variable names
# for a direct call; an app built with a differently-named instance is a
# real, named scope boundary (this module's own docstring), not silently
# mishandled.
_EXPRESS_DIRECT_CALL_RE = re.compile(
    r"\b(?:app|router)\.(get|post|put|patch|delete|options|head)\s*\(\s*"
    r"(`[^`]*`|\"[^\"]*\"|'[^']*'|[A-Za-z_$][A-Za-z0-9_$.]*|[^,)]+)"
    r"\s*[,)]",
    re.IGNORECASE,
)

# `router.route('/x')` / `app.route(\`/${y}\`)` - the same argument
# alternation as `_EXPRESS_DIRECT_CALL_RE`, without the trailing comma/
# handler requirement (a `.route(...)` call takes no handler itself - its
# chained `.get(...)`/`.post(...)` calls do).
_EXPRESS_ROUTE_CHAIN_START_RE = re.compile(
    r"\b(?:app|router)\.route\s*\(\s*"
    r"(`[^`]*`|\"[^\"]*\"|'[^']*'|[A-Za-z_$][A-Za-z0-9_$.]*|[^)]+?)"
    r"\s*\)",
    re.IGNORECASE,
)

# One chained `.verb(...)` call immediately following a `.route(...)` (or
# another chained verb). Handler arguments commonly wrap the real handler in
# another call - `.post(catchErrors(login))`, `.get(asyncHandler(read))` -
# a general, widespread Express idiom (async error-boundary wrappers), not
# an edge case, so the arg capture tolerates up to two levels of nested
# parens (bounded, not a full JS/TS parse - a third+ level, e.g. an inline
# arrow function whose own body itself calls a two-argument-wrapped
# function, is a documented, accepted regex limitation).
_EXPRESS_CHAIN_ARGS_L0 = r"[^()]*"
_EXPRESS_CHAIN_ARGS_L1 = r"[^()]*(?:\(" + _EXPRESS_CHAIN_ARGS_L0 + r"\)[^()]*)*"
_EXPRESS_CHAIN_ARGS_L2 = r"[^()]*(?:\(" + _EXPRESS_CHAIN_ARGS_L1 + r"\)[^()]*)*"
_EXPRESS_CHAIN_VERB_RE = re.compile(
    r"\s*\.\s*(get|post|put|patch|delete|options|head)\s*\((" + _EXPRESS_CHAIN_ARGS_L2 + r")\)",
    re.IGNORECASE,
)

_EXPRESS_DYNAMIC_SEGMENT_RE = re.compile(r":[A-Za-z0-9_]+")

# `adminAuth.login` inside a handler expression (`.post(adminAuth.login)`,
# `.post(catchErrors(adminAuth.login))`) - the first `alias.member`-shaped
# reference found is taken as the module this route's real handler comes
# from; resolved against the route file's own real import map, never
# guessed. A bounded heuristic (the same class of accepted limitation as
# every other regex in this module) - an inline handler with no such
# reference at all simply resolves to nothing here, which is correct.
_HANDLER_ALIAS_RE = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$]*)\.[A-Za-z_$][A-Za-z0-9_$]*\b")


def _resolve_cross_file_handler_text(file_abs, import_map, handler_ref_text):
    """The real, already-on-disk source text of whichever file a route's
    own handler reference resolves to via `file_abs`'s own real require/
    import map (`route_composition._local_import_map`, reused unmodified -
    never a second, guessing copy of that graph) - `(None, None)` when
    `handler_ref_text` names no real `alias.member` reference, the alias
    resolves to no real import, or the resolved file can't be read.

    This is how a route file that only does `router.post('/login',
    adminAuth.login)` (or `.post(catchErrors(adminAuth.login))`) gets real
    body evidence at all: the real `req.body` access almost always lives in
    the imported controller file, not the route file itself.
    """
    if handler_ref_text is None:
        return None, None
    match = _HANDLER_ALIAS_RE.search(handler_ref_text)
    if match is None:
        return None, None
    spec = import_map.get(match.group(1))
    if spec is None:
        return None, None
    target = _route_composition._resolve_js_module_path_any(file_abs, spec)
    if target is None:
        return None, None
    target_text = _read_text_capped(target)
    if target_text is None:
        return None, None
    return target, target_text

_JS_TS_FILE_RE = re.compile(r"\.(ts|js)$")


def _find_js_files(root_abs):
    """Every real `.ts`/`.js` file under `root_abs`, walked once, pruning
    the same ignored directories the Next.js strategy already prunes -
    Express is a plain Node dependency, not tied to any one directory
    layout, so (unlike the Pages Router strategy) this scans the whole
    project rather than one specific, named directory.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root_abs, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        for name in filenames:
            if _JS_TS_FILE_RE.search(name) and not _PAGES_API_IGNORED_FILE_RE.search(name):
                found.append(Path(dirpath) / name)
    return found


def _classify_route_path_arg(raw):
    """Returns `("literal", path)`, `("template", raw_template_inner)`, or
    `("variable", raw_expression)` - the one, shared classification every
    Express route-defining call site (direct or `.route()`-chained) is
    judged by (Phase 4). A template literal with no `${...}` interpolation
    at all is really just a literal in disguise, and is reported as one.
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return "literal", raw[1:-1]
    if len(raw) >= 2 and raw[0] == "`" and raw[-1] == "`":
        inner = raw[1:-1]
        return ("template", inner) if "${" in inner else ("literal", inner)
    return "variable", raw


def _discover_express_endpoints(context, root, zod_registry=None, mongoose_registry=None):
    """`zod_registry`: see `_discover_nextjs_endpoints`'s own docstring.
    `mongoose_registry`: an optional `(by_model_name, file_to_model_name)`
    pair from `model_schema.build_mongoose_schema_registry` - when a
    route's own real handler (in this file, or resolved across a real
    require/import reference to another file - see `_resolve_cross_file_
    handler_text`) uses a real, registered Mongoose model, that model's own
    real required fields are attached as `ApiEndpoint.model_fields`.

    Returns `(endpoints, warnings, unresolved_routes, files_text)`. Never
    attempted at all unless `project.frameworks` already, really contains
    "Express" (Phase F's own detection, reused rather than re-implemented
    here) - the same evidence-gated scoping the FastAPI/Pages Router
    strategies apply via their own framework facts. A real `.js`/`.ts`
    file's own content is read (capped) only to find real route-defining
    calls via a regex over source text - never a full JS/TS parse, never a
    guessed route.

    Two real call shapes are recognized (Phase 4 added the second):
    `app.<method>(path, ...)`/`router.<method>(path, ...)` (unchanged from
    docs/38), and `app.route(path).<method>(...)`/`router.route(path)
    .<method>(...)`, including a multi-verb chain
    (`.route(path).get(a).post(b)`). Either shape's own path argument is
    classified (`_classify_route_path_arg`): a real string/template-with-no-
    interpolation literal becomes a real `ApiEndpoint`; a template with
    unresolved `${...}` interpolation or a bare variable/expression becomes
    an `UnresolvedRoute` instead - the method is still real, known evidence
    even when the path is not, so it is never silently dropped either way.

    `files_text` is `{absolute_path: source_text}` for every file actually
    read here - handed to `route_composition.apply_express_composition` by
    the combined entry point below so mount-prefix resolution never has to
    re-read the same files from disk a second time. Router mount-prefix
    composition (`app.use('/api', router)`) is intentionally not resolved
    in this function at all - see `route_composition.py`'s own module
    docstring for that separate, additive pass.
    """
    project = context.project
    if not _is_express_project(project):
        return (), (), (), {}

    root = Path(root)
    endpoints = []
    warnings = []
    unresolved = []
    seen = set()
    files_text = {}
    mongoose_by_model, mongoose_file_to_model = mongoose_registry if mongoose_registry else ({}, {})

    for js_file in _find_js_files(root):
        text = _read_text_capped(js_file)
        source_file = js_file.relative_to(root).as_posix()
        if text is None:
            warnings.append("could not read JS/TS file: {}".format(source_file))
            continue
        files_text[js_file.resolve()] = text
        local_import_map = _route_composition._local_import_map_any(text)

        def _add_endpoint(method, path, line, window_text, handler_ref_text=None):
            key = (method, path)
            if key in seen:
                return
            seen.add(key)
            dynamic = bool(_EXPRESS_DYNAMIC_SEGMENT_RE.search(path))
            hints, reads_body, zod_fields, model_fields = (), False, (), ()
            if method in MUTATION_METHODS:
                if window_text is not None:
                    hints, reads_body = _extract_body_field_hints(window_text)
                    if zod_registry:
                        zod_fields = _zod_schema.find_referenced_schema_fields(window_text, zod_registry)
                    if mongoose_by_model:
                        model_fields = _model_schema.find_referenced_model_fields(
                            js_file.resolve(), window_text, mongoose_by_model, mongoose_file_to_model,
                        )
                # The real handler logic commonly lives in a separate,
                # imported controller file the route file only references
                # by name (e.g. `.post(adminAuth.login)`) - resolved via
                # this route file's own real import map, and used only to
                # fill in whichever same-file evidence above came up empty
                # (never overwrites real same-file evidence that already
                # exists).
                target_file, target_text = _resolve_cross_file_handler_text(
                    js_file.resolve(), local_import_map, handler_ref_text,
                )
                if target_text is not None:
                    if not hints and not reads_body:
                        hints, reads_body = _extract_body_field_hints(target_text)
                    if not zod_fields and zod_registry:
                        zod_fields = _zod_schema.find_referenced_schema_fields(target_text, zod_registry)
                    if not model_fields and mongoose_by_model:
                        model_fields = _model_schema.find_referenced_model_fields(
                            target_file, target_text, mongoose_by_model, mongoose_file_to_model,
                        )
            endpoints.append(ApiEndpoint(
                method=method, path=path, source_file=source_file, dynamic=dynamic, line=line,
                body_field_hints=hints, reads_request_body=reads_body, zod_fields=zod_fields,
                model_fields=model_fields,
                discovered_by=("express-source",),
            ))

        # Direct calls (`app.get('/x', ...)`) - route calls in source
        # order, so each one's own handler body can be approximated as
        # "the text up to the next route call in this same file" (docs/45).
        direct_matches = list(_EXPRESS_DIRECT_CALL_RE.finditer(text))
        for i, match in enumerate(direct_matches):
            method = match.group(1).upper()
            kind, value = _classify_route_path_arg(match.group(2))
            line = text.count("\n", 0, match.start()) + 1
            if kind == "literal":
                if not value.startswith("/"):
                    # A real call matched, but its first argument is not a
                    # real path literal (a middleware name, a regex, an
                    # empty string, or Express's own `app.get(settingName)`
                    # dual-use form) - never fabricated into a route.
                    continue
                window_end = direct_matches[i + 1].start() if i + 1 < len(direct_matches) else len(text)
                window_text = text[match.end():window_end]
                _add_endpoint(method, value, line, window_text, window_text)
            else:
                reason = (
                    "path segment(s) inside the template literal cannot be established statically"
                    if kind == "template" else
                    "path argument is a variable/expression, not a string literal - cannot be "
                    "resolved statically"
                )
                unresolved.append(UnresolvedRoute(
                    method=method, raw_expression=match.group(2).strip(),
                    source_file=source_file, line=line, reason=reason,
                ))

        # `.route(path).verb(...)` chains - a chain start is matched
        # independently of the direct-call regex above (its own call shape
        # has no trailing comma), then every chained verb call immediately
        # following it is consumed in a small loop (not a fixed regex, so
        # `.get(a).post(b).patch(c)` chains of any length are handled the
        # same way as a single `.get(a)`).
        for chain_match in _EXPRESS_ROUTE_CHAIN_START_RE.finditer(text):
            kind, value = _classify_route_path_arg(chain_match.group(1))
            chain_line = text.count("\n", 0, chain_match.start()) + 1
            pos = chain_match.end()
            verbs_found = 0
            while True:
                verb_match = _EXPRESS_CHAIN_VERB_RE.match(text, pos)
                if verb_match is None:
                    break
                verbs_found += 1
                method = verb_match.group(1).upper()
                if kind == "literal":
                    if value.startswith("/"):
                        _add_endpoint(method, value, chain_line, None, verb_match.group(2))
                else:
                    reason = (
                        "path segment(s) inside the template literal cannot be established statically"
                        if kind == "template" else
                        "path argument is a variable/expression, not a string literal - cannot be "
                        "resolved statically"
                    )
                    unresolved.append(UnresolvedRoute(
                        method=method, raw_expression=chain_match.group(1).strip(),
                        source_file=source_file, line=chain_line, reason=reason,
                    ))
                pos = verb_match.end()
            if verbs_found == 0:
                warnings.append(
                    "found a route() call for {!r} in {} but could not parse its chained HTTP "
                    "verb call(s) - possibly a handler shape this regex-based scan does not "
                    "recognize (e.g. an inline function with its own parentheses)"
                    .format(chain_match.group(1).strip(), source_file)
                )

    endpoints.sort(key=lambda e: (e.path, e.method, e.source_file))
    return tuple(endpoints), tuple(warnings), tuple(unresolved), files_text


# --- OpenAPI strategy (Phase 4: OpenAPI as an independent discovery source) -
#
# Every other strategy above only ever reads a target's own route-declaring
# *source code*. A project that ships a real, already-checked-out OpenAPI/
# Swagger document (`openapi.json`/`swagger.json`/`.yaml`/`.yml`, found the
# same evidence-gated way `resolution.find_static_openapi_schema_detailed`
# already looks for one, reused here rather than duplicated) describes its
# own real API surface directly - walking its own `paths` object finds real
# endpoints even when source-code discovery above finds none at all (a
# project whose routes are all built by a runtime/dynamic mechanism no
# static-text strategy could ever recognize, but which still publishes a
# real, accurate schema). Never treated as more authoritative than a real
# source-code find when both describe the same (method, path) - see the
# combined entry point's own merge step for how the two are fused, not
# duplicated.

_OPENAPI_HTTP_METHOD_KEYS = frozenset(m.lower() for m in METHODS)
_OPENAPI_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


def _discover_openapi_endpoints(root):
    """Returns `(endpoints, warnings)`. `None`/malformed/oversized
    candidate files are never silently ignored - `find_static_openapi_
    schema_detailed` already reports each one as a real warning; this adds
    one more only when the document itself was found but its `paths`
    object is missing or unusable.
    """
    doc, source_file, schema_warnings = find_static_openapi_schema_detailed(root)
    warnings = list(schema_warnings)
    if doc is None:
        return (), tuple(warnings)

    paths_obj = doc.get("paths")
    if not isinstance(paths_obj, dict):
        warnings.append("{} declares no usable top-level 'paths' object".format(source_file))
        return (), tuple(warnings)

    endpoints = []
    for path, path_item in paths_obj.items():
        if not isinstance(path_item, dict) or not isinstance(path, str) or not path.startswith("/"):
            continue
        for method_key, operation in path_item.items():
            if not isinstance(method_key, str) or method_key.lower() not in _OPENAPI_HTTP_METHOD_KEYS:
                continue
            if not isinstance(operation, dict):
                continue
            method = method_key.upper()
            dynamic = bool(_OPENAPI_PATH_PARAM_RE.search(path))
            endpoints.append(ApiEndpoint(
                method=method, path=path, source_file=source_file, dynamic=dynamic,
                discovered_by=("openapi",),
            ))

    endpoints.sort(key=lambda e: (e.path, e.method))
    return tuple(endpoints), tuple(warnings)


# --- unsupported-framework reporting (Phase 4) ------------------------------
#
# `project/detectors.py` detects real evidence for far more backend/API
# frameworks than this module has a discovery strategy for. A project using
# one of these must never read the same as "zero endpoints" a truly
# API-less or framework-less project would also show - the two are
# completely different facts, and collapsing them together is exactly the
# kind of silent misreporting the Phase 4 audit (docs/56) flagged. Reusing
# `project/detectors.py`'s own real `_BACKEND_FRAMEWORKS` name list would
# reach into that module's own private constant; duplicated narrowly here
# instead, minus the frameworks this module already has real strategies
# for - the same "duplicate a small list rather than import another
# module's private constant" precedent this file's own `_IGNORED_DIR_NAMES`
# docstring already established.
_BACKEND_FRAMEWORKS_WITHOUT_STRATEGY = frozenset({
    "NestJS", "Flask", "Django", "Spring Boot", "Laravel", "ASP.NET",
})


def _unsupported_frameworks(project):
    return tuple(sorted(
        item.name for item in project.frameworks
        if item.name in _BACKEND_FRAMEWORKS_WITHOUT_STRATEGY
    ))


# --- combined entry point ----------------------------------------------

@dataclass(frozen=True)
class DiscoveryOutcome:
    """Phase 4's full discovery result - `discover_api_endpoints_detailed`'s
    own return shape. `discover_api_endpoints` (unchanged signature, every
    existing caller untouched) remains a thin `(endpoints, warnings)`
    wrapper around this for backward compatibility.
    """

    endpoints: tuple
    warnings: tuple
    unresolved_routes: tuple = ()
    strategy_counts: tuple = ()
    unsupported_frameworks: tuple = ()


def _merge_endpoints_with_provenance(*endpoint_lists):
    """Deduplicates by `(method, path)` across every list, in the order
    given - the first occurrence's own fields (body hints, line, dynamic
    detection, ...) win, but every later duplicate's own `discovered_by`
    tag(s) are still folded into the kept endpoint's own tuple (Phase 4's
    evidence-fusion requirement: two independent sources agreeing on the
    same endpoint is additional confidence, never a reason to discard one
    of them silently).
    """
    merged = {}
    order = []
    for lst in endpoint_lists:
        for endpoint in lst:
            key = (endpoint.method, endpoint.path)
            if key in merged:
                existing = merged[key]
                combined_by = tuple(dict.fromkeys(existing.discovered_by + endpoint.discovered_by))
                if combined_by != existing.discovered_by:
                    merged[key] = replace(existing, discovered_by=combined_by)
            else:
                merged[key] = endpoint
                order.append(key)
    return [merged[k] for k in order]


def discover_api_endpoints_detailed(context, root):
    """The one, full-detail discovery entry point (Phase 4). Runs every
    strategy above (including the two additive router-composition passes
    and the independent OpenAPI pass), merges the results with provenance
    preserved, and separately collects every real `UnresolvedRoute`, a
    per-strategy endpoint-count breakdown, and any detected-but-
    unimplemented backend framework - see `DiscoveryOutcome`'s own
    docstring for the exact shape. `discover_api_endpoints` (below) remains
    the original, narrower `(endpoints, warnings)` entry point every
    existing caller already uses, unchanged.
    """
    root = Path(root)
    project = context.project

    zod_registry = (
        _zod_schema.build_zod_schema_registry(root)
        if (_is_nextjs_project(project) or _is_express_project(project))
        else {}
    )
    mongoose_registry = (
        _model_schema.build_mongoose_schema_registry(root)
        if _is_express_project(project)
        else ({}, {})
    )
    nextjs_endpoints, nextjs_warnings = _discover_nextjs_endpoints(context, root, zod_registry)
    pages_endpoints, pages_warnings = _discover_nextjs_pages_endpoints(context, root, zod_registry)
    fastapi_endpoints, fastapi_warnings, fastapi_unresolved, fastapi_files_text = (
        _discover_fastapi_endpoints(context, root)
    )
    express_endpoints, express_warnings, express_unresolved, express_files_text = (
        _discover_express_endpoints(context, root, zod_registry, mongoose_registry)
    )
    openapi_endpoints, openapi_warnings = _discover_openapi_endpoints(root)

    # Phase 4: router-mount-prefix composition - additive, over each
    # strategy's own already-found raw endpoints and already-read file
    # text (no re-reading from disk). A strategy with nothing to compose
    # over (its own gating evidence absent) simply contributes no change.
    composition_warnings = []
    if express_endpoints:
        express_endpoints, express_composition_warnings = _route_composition.apply_express_composition(
            root, express_endpoints, express_files_text,
        )
        composition_warnings.extend(express_composition_warnings)
    if fastapi_endpoints:
        fastapi_endpoints, fastapi_composition_warnings = _route_composition.apply_fastapi_composition(
            root, fastapi_endpoints, fastapi_files_text,
        )
        composition_warnings.extend(fastapi_composition_warnings)

    # A strategy is only listed at all when it actually ran (its own
    # gating framework evidence was present) - OpenAPI has no framework
    # gate (a project of any framework, or none, may ship a real static
    # OpenAPI document) and is always listed.
    _strategy_ran = {
        "nextjs-app-router": _is_nextjs_project(project),
        "nextjs-pages-router": _is_nextjs_project(project),
        "fastapi-source": _is_fastapi_project(project),
        "express-source": _is_express_project(project),
        "openapi": True,
    }
    strategy_counts = tuple(
        (name, len(lst)) for name, lst in (
            ("nextjs-app-router", nextjs_endpoints),
            ("nextjs-pages-router", pages_endpoints),
            ("fastapi-source", fastapi_endpoints),
            ("express-source", express_endpoints),
            ("openapi", openapi_endpoints),
        ) if _strategy_ran[name]
    )

    endpoints = _merge_endpoints_with_provenance(
        nextjs_endpoints, pages_endpoints, fastapi_endpoints, express_endpoints, openapi_endpoints,
    )

    # Phase 2 (API contract understanding, docs/53): one whole-project pass
    # over the project's own existing test files/Postman collections,
    # attached to every mutating endpoint it structurally matches - the same
    # "build a registry once, attach real matched evidence after the merge"
    # shape the Zod registry above already establishes, done as a single
    # post-merge pass here (rather than inside each strategy) since it
    # needs the already-deduplicated endpoint list, not each strategy's own
    # partial one.
    test_registry = _test_evidence.build_test_evidence_registry(root)
    test_evidence_count = 0
    if test_registry:
        attached = []
        for e in endpoints:
            fields = (
                _test_evidence.find_test_evidence_for_endpoint(e.method, e.path, test_registry)
                if e.method in MUTATION_METHODS else None
            )
            if fields:
                test_evidence_count += 1
                attached.append(replace(e, test_evidence_fields=tuple(fields.items())))
            else:
                attached.append(e)
        endpoints = attached
    strategy_counts = strategy_counts + (("test-evidence", test_evidence_count),)

    endpoints.sort(key=lambda e: (e.path, e.method))
    unresolved_routes = tuple(express_unresolved) + tuple(fastapi_unresolved)
    warnings = (
        tuple(nextjs_warnings) + tuple(pages_warnings) + tuple(fastapi_warnings)
        + tuple(express_warnings) + tuple(openapi_warnings) + tuple(composition_warnings)
    )
    unsupported = _unsupported_frameworks(project)

    return DiscoveryOutcome(
        endpoints=tuple(endpoints), warnings=warnings, unresolved_routes=unresolved_routes,
        strategy_counts=strategy_counts, unsupported_frameworks=unsupported,
    )


def discover_api_endpoints(context, root):
    """The original, narrower entry point (docs/30 onward): runs every
    strategy and merges the results - never one replacing another. A
    strategy that finds nothing (its own gating evidence absent)
    contributes an empty result, so e.g. a pure Next.js App Router
    project's own output is unaffected by the Express strategy existing at
    all, and vice versa - proven directly by the existing regression
    suites for each strategy continuing to pass unmodified.

    Deduplicated by `(method, path)` across *all* strategies combined.
    Unchanged in shape since docs/30 - a thin `(endpoints, warnings)` view
    over `discover_api_endpoints_detailed`'s own fuller result, kept
    exactly as-is so every existing caller (`runner.py`, `web/server.py`,
    every regression test) needs no change at all.
    """
    outcome = discover_api_endpoints_detailed(context, root)
    return outcome.endpoints, outcome.warnings
