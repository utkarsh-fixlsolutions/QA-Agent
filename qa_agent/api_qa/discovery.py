"""API endpoint discovery (Phase G5-adjacent, docs/30-api-qa-v1.md; docs/32
-fastapi-discovery-and-startup.md; docs/38-broader-route-discovery.md):
`discover_api_endpoints(context, root)`.

Four independent, additive strategies, combined by the one public function
at the bottom of this file - none replaces any other:

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
  already established, applied to Express's own real API shape.

A route with no recognized handler is reported as a warning, not silently
dropped and not fabricated into an endpoint that was never really there.

A FastAPI `APIRouter`'s own `prefix=` composition is a real, common
pattern this module does not attempt to resolve, deliberately, rather than
half-supporting it unreliably - named here, not silently gapped.

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
from dataclasses import replace
from pathlib import Path

from . import test_evidence as _test_evidence
from . import zod_schema as _zod_schema
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
        # Route decorators in source order, so each one's own handler can
        # be approximated as "the text up to the next route decorator in
        # this same file" (bounded, see `_PY_BODY_EVIDENCE_WINDOW_CHARS`) -
        # the same technique the Express strategy already uses for its own
        # per-route `body_field_hints` windowing.
        matches = list(_PYTHON_ROUTE_DECORATOR_RE.finditer(text))
        for i, match in enumerate(matches):
            path = match.group(2)
            if not path:
                # A decorator matched with an empty string literal path -
                # not a real, callable route; never fabricated into one.
                continue
            method = match.group(1).upper()
            line = text.count("\n", 0, match.start()) + 1
            dynamic = bool(_FASTAPI_DYNAMIC_SEGMENT_RE.search(path))
            reads_body = False
            if method in MUTATION_METHODS:
                next_start = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                window_end = min(next_start, match.end() + _PY_BODY_EVIDENCE_WINDOW_CHARS)
                reads_body = _reads_request_body_python(text[match.end():window_end])
            endpoints.append(ApiEndpoint(
                method=method, path=path, source_file=source_file, dynamic=dynamic, line=line,
                reads_request_body=reads_body,
            ))

    endpoints.sort(key=lambda e: (e.path, e.method, e.source_file))
    return tuple(endpoints), tuple(warnings)


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
                ))

    endpoints.sort(key=lambda e: (e.path, e.method))
    return tuple(endpoints), tuple(warnings)


# --- Express strategy (new) ---------------------------------------------

def _is_express_project(project):
    return any(item.name == "Express" for item in project.frameworks)


# `app.get('/x', ...)` / `router.post("/y", ...)` - a real call naming a
# real HTTP method and a real, literal path string, the exact same
# "decorator/call with a literal-string first argument" shape the FastAPI
# strategy's own `_PYTHON_ROUTE_DECORATOR_RE` already established. `app`/
# `router` are Express's own overwhelmingly standard variable names for
# this; an app built with a differently-named instance is a real, named
# scope boundary (this module's own docstring), not silently mishandled.
_EXPRESS_ROUTE_CALL_RE = re.compile(
    r"\b(?:app|router)\.(get|post|put|patch|delete|options|head)\s*\(\s*[\"'`]([^\"'`]*)[\"'`]",
    re.IGNORECASE,
)
_EXPRESS_DYNAMIC_SEGMENT_RE = re.compile(r":[A-Za-z0-9_]+")

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


def _discover_express_endpoints(context, root, zod_registry=None):
    """`zod_registry`: see `_discover_nextjs_endpoints`'s own docstring.

    Returns `(endpoints, warnings)`. Never attempted at all unless
    `project.frameworks` already, really contains "Express" (Phase F's own
    detection, reused rather than re-implemented here) - the same
    evidence-gated scoping the FastAPI/Pages Router strategies apply via
    their own framework facts. A real `.js`/`.ts` file's own content is
    read (capped) only to find real `app.<method>(...)`/`router.<method>
    (...)` calls via a regex over source text - never a full JS/TS parse,
    never a guessed route. A router mounted under a path prefix
    (`app.use('/api', router)`) is not resolved - a real, documented scope
    boundary (this module's own module docstring), the same one already
    drawn for FastAPI's `APIRouter(prefix=...)`.
    """
    project = context.project
    if not _is_express_project(project):
        return (), ()

    root = Path(root)
    endpoints = []
    warnings = []
    seen = set()

    for js_file in _find_js_files(root):
        text = _read_text_capped(js_file)
        source_file = js_file.relative_to(root).as_posix()
        if text is None:
            warnings.append("could not read JS/TS file: {}".format(source_file))
            continue
        # Route calls in source order, so each one's own handler body can be
        # approximated as "the text up to the next route call in this same
        # file" (docs/45) - a real, named scope boundary (not a full JS/TS
        # parse: a route call site that isn't immediately followed by its
        # own handler's real closing brace before the next route call would
        # see a widened, slightly-too-generous window) rather than falling
        # back to whole-file hints, which would wrongly blend two different
        # routes' fields together.
        matches = list(_EXPRESS_ROUTE_CALL_RE.finditer(text))
        for i, match in enumerate(matches):
            path = match.group(2)
            if not path.startswith("/"):
                # A real call matched, but its first argument is not a
                # real path literal (a middleware name, a regex, an empty
                # string) - never fabricated into a route that was never
                # really there.
                continue
            method = match.group(1).upper()
            line = text.count("\n", 0, match.start()) + 1
            dynamic = bool(_EXPRESS_DYNAMIC_SEGMENT_RE.search(path))
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            hints, reads_body, zod_fields = (), False, ()
            if method in MUTATION_METHODS:
                window_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                window_text = text[match.end():window_end]
                hints, reads_body = _extract_body_field_hints(window_text)
                if zod_registry:
                    zod_fields = _zod_schema.find_referenced_schema_fields(window_text, zod_registry)
            endpoints.append(ApiEndpoint(
                method=method, path=path, source_file=source_file, dynamic=dynamic, line=line,
                body_field_hints=hints, reads_request_body=reads_body, zod_fields=zod_fields,
            ))

    endpoints.sort(key=lambda e: (e.path, e.method, e.source_file))
    return tuple(endpoints), tuple(warnings)


# --- combined entry point ----------------------------------------------

def discover_api_endpoints(context, root):
    """The one public entry point: runs every strategy above and merges
    the results - never one replacing another. A strategy that finds
    nothing (its own gating evidence absent) contributes an empty result,
    so e.g. a pure Next.js App Router project's own output is unaffected by
    the Express strategy existing at all, and vice versa - proven directly
    by the existing regression suites for each strategy continuing to pass
    unmodified.

    Deduplicated by `(method, path)` across *all* strategies combined (in
    the unlikely event the same method+path were somehow found by more
    than one - never expected in practice, since App Router/Pages Router
    both gate on the same Next.js framework fact but scan mutually
    exclusive directories, and FastAPI/Express gate on mutually exclusive
    framework evidence, but guarded anyway rather than assumed impossible).
    """
    root = Path(root)
    # docs/50-zod-schema-discovery.md: one whole-project pass, built only
    # when there's any real, already-detected JS/TS framework evidence at
    # all - a pure Python/FastAPI project never pays for a JS-file walk it
    # could not possibly need. Shared across all three JS/TS strategies
    # below so a schema defined in one file is still found when the route
    # that references it lives in a completely different one.
    zod_registry = (
        _zod_schema.build_zod_schema_registry(root)
        if (_is_nextjs_project(context.project) or _is_express_project(context.project))
        else {}
    )
    nextjs_endpoints, nextjs_warnings = _discover_nextjs_endpoints(context, root, zod_registry)
    pages_endpoints, pages_warnings = _discover_nextjs_pages_endpoints(context, root, zod_registry)
    fastapi_endpoints, fastapi_warnings = _discover_fastapi_endpoints(context, root)
    express_endpoints, express_warnings = _discover_express_endpoints(context, root, zod_registry)

    endpoints = []
    seen = set()
    for endpoint in list(nextjs_endpoints) + list(pages_endpoints) + list(fastapi_endpoints) + list(express_endpoints):
        key = (endpoint.method, endpoint.path)
        if key in seen:
            continue
        seen.add(key)
        endpoints.append(endpoint)

    # Phase 2 (API contract understanding, docs/53): one whole-project pass
    # over the project's own existing test files/Postman collections,
    # attached to every mutating endpoint it structurally matches - the same
    # "build a registry once, attach real matched evidence after the merge"
    # shape the Zod registry above already establishes, done as a single
    # post-merge pass here (rather than inside each of the four strategies)
    # since it needs the already-deduplicated endpoint list, not each
    # strategy's own partial one.
    test_registry = _test_evidence.build_test_evidence_registry(root)
    if test_registry:
        attached = []
        for e in endpoints:
            fields = (
                _test_evidence.find_test_evidence_for_endpoint(e.method, e.path, test_registry)
                if e.method in MUTATION_METHODS else None
            )
            attached.append(replace(e, test_evidence_fields=tuple(fields.items())) if fields else e)
        endpoints = attached

    endpoints.sort(key=lambda e: (e.path, e.method))
    warnings = tuple(nextjs_warnings) + tuple(pages_warnings) + tuple(fastapi_warnings) + tuple(express_warnings)
    return tuple(endpoints), warnings
