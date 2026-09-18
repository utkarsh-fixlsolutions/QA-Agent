"""Express and FastAPI router-mount composition (Phase 4, generalized API
discovery): resolves the concrete, prefixed path of an endpoint originally
discovered inside a router file that is mounted under a path prefix
elsewhere in the project (`app.use('/api', router)` /
`app.include_router(users.router, prefix="/api/users")`) - a real, common
composition idiom neither framework's own per-file strategy in
discovery.py resolves by itself (each one only ever sees one file's own
literal route declarations, never how that file's own router instance is
wired into the rest of the app - both strategies' own module docstrings in
discovery.py have always named this as a real, undone gap).

Both resolvers work the same general way, over the project's own real
import/require graph, never a project-specific convention:

1. Find every real mount statement in every already-scanned file
   (`<x>.use(prefix, [...middleware,] routerIdentifier)` for Express;
   `<x>.include_router(routerIdentifier, prefix=...)` for FastAPI).
2. Resolve `routerIdentifier` to the real file it was imported from, via
   that same file's own real `require`/`import` statements - never a
   guess, never a special-cased file name or project convention. A mount
   whose router argument is not a plain identifier (an inline function, a
   call expression) is never mistaken for a router - Express's own
   documented convention already puts the router last; a middleware
   argument earlier in the same call is never treated as the mount target.
3. Any file that is the resolved target of at least one real mount
   statement is never treated as its own top-level entry point - its own
   literal routes are only ever reported at the prefix(es) it is actually
   reachable through, not also at its own raw, unprefixed path.
4. A bounded-depth (`_MAX_COMPOSITION_DEPTH`) walk from every remaining
   "root" file (one that is never itself a mount target) accumulates the
   real prefix chain down to every reachable mounted file, and prepends it
   to that file's own already-discovered raw routes.

Never invents a prefix, never resolves a mount whose target could not be
traced to a real file on disk, and never hangs on a circular mount graph -
a file revisited in the same walk stops that branch with a real, honest
warning instead of recursing forever. A mounted file that this pass never
actually reaches from any root (an orphaned/ambiguous case) still reports
its own routes at their raw path rather than silently dropping them -
never worse than not running this module at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Dict, Optional, Tuple

_MAX_COMPOSITION_DEPTH = 6

# --- Express -------------------------------------------------------------

_JS_TS_RESOLVE_EXTENSIONS = (".js", ".ts", ".jsx", ".tsx")

_REQUIRE_DEFAULT_RE = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*require\s*\(\s*[\"']([^\"']+)[\"']\s*\)"
)
_REQUIRE_DESTRUCTURE_RE = re.compile(
    r"(?:const|let|var)\s*\{\s*([^}]+)\}\s*=\s*require\s*\(\s*[\"']([^\"']+)[\"']\s*\)"
)
_IMPORT_DEFAULT_RE = re.compile(
    r"\bimport\s+([A-Za-z_$][A-Za-z0-9_$]*)\s+from\s+[\"']([^\"']+)[\"']"
)
_IMPORT_DESTRUCTURE_RE = re.compile(
    r"\bimport\s*\{\s*([^}]+)\}\s*from\s+[\"']([^\"']+)[\"']"
)

# `<identifier>.use(<literal prefix>, <rest>)` - `<identifier>` is
# deliberately not restricted to `app`/`router` (unlike discovery.py's own
# per-file route regexes): a nested mount (`apiRouter.use('/users',
# userRouter)`) uses whatever real local name that file itself gave its own
# router/app instance, which this module never needs to know in advance -
# only which *file* issued the mount, which is already known (this regex
# runs once per already-identified file).
_APP_USE_MOUNT_RE = re.compile(
    r"\b[A-Za-z_$][A-Za-z0-9_$]*\.use\s*\(\s*[\"'`]([^\"'`]*)[\"'`]\s*,\s*([^;]+?)\)\s*;"
)

_SIMPLE_IDENT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


def _local_import_map(text: str) -> Dict[str, str]:
    """Real local identifier -> the real, literal relative module specifier
    it was imported from (`./foo`, `../bar/baz`) - a bare package name
    (`express`, `cors`) is never a real, on-disk composition target and is
    never recorded here.
    """
    mapping: Dict[str, str] = {}
    for match in _REQUIRE_DEFAULT_RE.finditer(text):
        name, spec = match.group(1), match.group(2)
        if spec.startswith("./") or spec.startswith("../"):
            mapping[name] = spec
    for match in _IMPORT_DEFAULT_RE.finditer(text):
        name, spec = match.group(1), match.group(2)
        if spec.startswith("./") or spec.startswith("../"):
            mapping[name] = spec
    for regex in (_REQUIRE_DESTRUCTURE_RE, _IMPORT_DESTRUCTURE_RE):
        for match in regex.finditer(text):
            names_text, spec = match.group(1), match.group(2)
            if not (spec.startswith("./") or spec.startswith("../")):
                continue
            for part in names_text.split(","):
                part = part.strip()
                if not part:
                    continue
                if " as " in part:
                    name = part.split(" as ")[-1].strip()
                else:
                    name = part.strip()
                if _SIMPLE_IDENT_RE.match(name):
                    mapping[name] = spec
    return mapping


def _resolve_js_module_path(importing_file_abs: Path, spec: str):
    """Returns a real, fully `.resolve()`d path - never one that still
    carries a literal `..` segment from `spec` (e.g. `require('../models/
    X')`, a common `controllers/` + `models/` sibling-directory layout).
    `Path.is_file()` itself would happily follow such a segment via the OS,
    but the *unresolved* `Path` object returned would then silently fail
    every dict lookup against the `.resolve()`d keys this whole module (and
    `model_schema.py`, which reuses this function) key every file map by -
    a real bug found via model_schema.py's own upward-traversal fixture,
    not specific to it.
    """
    base = importing_file_abs.parent / spec
    if base.suffix in _JS_TS_RESOLVE_EXTENSIONS:
        candidates = [base]
    else:
        candidates = [base.with_suffix(ext) for ext in _JS_TS_RESOLVE_EXTENSIONS]
        candidates += [(base / "index").with_suffix(ext) for ext in _JS_TS_RESOLVE_EXTENSIONS]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


_ALIAS_CACHE: Dict[Path, Dict[str, Path]] = {}


def _find_module_alias_map(start_file_abs: Path) -> Dict[str, Path]:
    """Real Node path aliases declared via the `module-alias` package's own,
    standard `package.json` `_moduleAliases` field (https://www.npmjs.com/
    package/module-alias) - a common, general Node.js convention (found via
    idurar-erp-crm's own real `backend/package.json`: `{"@": "src"}`), not
    specific to any one project. The nearest `package.json` walking upward
    from `start_file_abs` that declares one is used, each alias resolved to
    its own real, absolute directory; `{}` when no such declaration exists
    anywhere above this file - never guessed, never a default invented.
    Cached per that `package.json`'s own real path.
    """
    current = start_file_abs.parent
    for _ in range(24):
        candidate = current / "package.json"
        if candidate.is_file():
            cached = _ALIAS_CACHE.get(candidate)
            if cached is not None:
                return cached
            aliases: Dict[str, Path] = {}
            try:
                data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
                raw = data.get("_moduleAliases")
                if isinstance(raw, dict):
                    for prefix, target in raw.items():
                        if isinstance(prefix, str) and isinstance(target, str):
                            aliases[prefix] = (current / target).resolve()
            except (OSError, ValueError):
                pass
            _ALIAS_CACHE[candidate] = aliases
            return aliases
        parent = current.parent
        if parent == current:
            break
        current = parent
    return {}


def _local_import_map_any(text: str) -> Dict[str, str]:
    """The same real local-identifier -> module-specifier extraction
    `_local_import_map` already performs, without that function's own
    relative-path-only filter - every real `require`/`import` spec is kept,
    including a bare package name or a path alias (`@/models/Product`).
    Used only by callers that go on to try alias resolution
    (`_resolve_js_module_path_any`) themselves; a real npm package name
    simply resolves to nothing there, exactly as if it had been filtered
    out here - never treated as a real composition/model target either way.
    """
    mapping: Dict[str, str] = {}
    for match in _REQUIRE_DEFAULT_RE.finditer(text):
        mapping[match.group(1)] = match.group(2)
    for match in _IMPORT_DEFAULT_RE.finditer(text):
        mapping[match.group(1)] = match.group(2)
    for regex in (_REQUIRE_DESTRUCTURE_RE, _IMPORT_DESTRUCTURE_RE):
        for match in regex.finditer(text):
            names_text, spec = match.group(1), match.group(2)
            for part in names_text.split(","):
                part = part.strip()
                if not part:
                    continue
                name = part.split(" as ")[-1].strip() if " as " in part else part
                if _SIMPLE_IDENT_RE.match(name):
                    mapping[name] = spec
    return mapping


def _resolve_js_module_path_any(importing_file_abs: Path, spec: str) -> Optional[Path]:
    """`_resolve_js_module_path`, extended with one additional, real
    fallback: when `spec` is not a relative path, try resolving it as a
    real, declared path alias (`_find_module_alias_map`) instead of giving
    up - a bare npm package name matches no alias either and still
    correctly resolves to nothing.
    """
    if spec.startswith("./") or spec.startswith("../"):
        return _resolve_js_module_path(importing_file_abs, spec)
    for prefix, base_dir in _find_module_alias_map(importing_file_abs).items():
        if spec == prefix:
            rest = ""
        elif spec.startswith(prefix + "/"):
            rest = spec[len(prefix) + 1:]
        else:
            continue
        base = (base_dir / rest) if rest else base_dir
        if base.suffix in _JS_TS_RESOLVE_EXTENSIONS:
            candidates = [base]
        else:
            candidates = [base.with_suffix(ext) for ext in _JS_TS_RESOLVE_EXTENSIONS]
            candidates += [(base / "index").with_suffix(ext) for ext in _JS_TS_RESOLVE_EXTENSIONS]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    return None


def _extract_mount_router_identifier(args_text: str):
    """Express's own real, documented convention (`app.use(path,
    [...middleware,] router)`) puts the router last - the last
    comma-separated argument, when it is a plain identifier (never an
    inline function/call expression - those are never mistaken for a
    mountable router), is the one this whole module resolves.
    """
    parts = [p.strip() for p in args_text.split(",") if p.strip()]
    if not parts:
        return None
    last = parts[-1]
    return last if _SIMPLE_IDENT_RE.match(last) else None


def _find_express_mounts(text: str):
    mounts = []
    for match in _APP_USE_MOUNT_RE.finditer(text):
        prefix, args_text = match.group(1), match.group(2)
        router_name = _extract_mount_router_identifier(args_text)
        if router_name is not None:
            mounts.append((prefix, router_name))
    return mounts


def apply_express_composition(root, endpoints, js_files_text: Dict[Path, str]):
    """`endpoints`: every `ApiEndpoint` the plain per-file Express strategy
    already found (raw, unprefixed paths, each with a real `source_file`).
    `js_files_text`: `{absolute_file_path: already-read source text}` for
    every real `.js`/`.ts` file that strategy already scanned - reused
    rather than re-read from disk a second time.

    Returns `(new_endpoints, warnings)` - `new_endpoints` covers every
    endpoint in `endpoints`, exactly once each, with `path` prefixed
    wherever a real mount chain was resolved for its own file (and
    `discovered_by` extended with `"express-composition"` when it was).
    """
    root = Path(root)
    import_maps = {f: _local_import_map(t) for f, t in js_files_text.items()}

    mounts_by_file: Dict[Path, list] = {}
    for f, t in js_files_text.items():
        mounts = []
        for prefix, router_name in _find_express_mounts(t):
            spec = import_maps.get(f, {}).get(router_name)
            if spec is None:
                continue
            target = _resolve_js_module_path(f, spec)
            if target is None or target not in js_files_text:
                continue
            mounts.append((prefix, target))
        if mounts:
            mounts_by_file[f] = mounts

    mounted_targets = {target for mounts in mounts_by_file.values() for _prefix, target in mounts}

    endpoints_by_file: Dict[Path, list] = {}
    for e in endpoints:
        endpoints_by_file.setdefault((root / e.source_file).resolve(), []).append(e)

    warnings = []
    resolved = []
    globally_visited = set()

    def _walk(file_abs: Path, prefix: str, depth: int, visited: frozenset):
        if depth > _MAX_COMPOSITION_DEPTH:
            warnings.append(
                "express router composition under {} exceeds the maximum depth ({}) - stopped "
                "resolving further, to avoid an unbounded walk".format(file_abs.name, _MAX_COMPOSITION_DEPTH)
            )
            return
        if file_abs in visited:
            warnings.append(
                "circular express router mount detected involving {} - stopped resolving that branch"
                .format(file_abs.name)
            )
            return
        visited = visited | {file_abs}
        globally_visited.add(file_abs)
        for e in endpoints_by_file.get(file_abs, ()):
            if prefix:
                new_path = prefix.rstrip("/") + e.path
                discovered_by = tuple(dict.fromkeys(e.discovered_by + ("express-composition",)))
                resolved.append(replace(e, path=new_path, discovered_by=discovered_by))
            else:
                resolved.append(e)
        for mount_prefix, target in mounts_by_file.get(file_abs, ()):
            _walk(target, prefix.rstrip("/") + mount_prefix, depth + 1, visited)

    # A root is any file that either has its own routes or issues a mount
    # (a real, common "orchestrator" file - e.g. `app.js` - typically has
    # no literal routes of its own at all, only `require`s and `app.use(...)`
    # calls, and must still be walked from) - excluding any file that is
    # itself a real mount target of another (it is only ever reached
    # *through* that mount, never treated as its own separate entry point).
    root_candidates = (set(endpoints_by_file) | set(mounts_by_file)) - mounted_targets
    for file_abs in root_candidates:
        _walk(file_abs, "", 0, frozenset())

    # A file that really is a mount target, but this pass never actually
    # reached from any root (e.g. only mounted by a file that is itself
    # unreachable, or a genuinely orphaned router) still has real routes -
    # reported at their raw path rather than silently dropped.
    for file_abs, file_endpoints in endpoints_by_file.items():
        if file_abs in mounted_targets and file_abs not in globally_visited:
            resolved.extend(file_endpoints)

    return tuple(resolved), tuple(warnings)


# --- FastAPI ---------------------------------------------------------------

# Only real, relative imports (`from .routers import users` / `from
# .routers.users import router`) are resolved - a real, named scope
# boundary (this module's own docstring): resolving a project-absolute
# import (`from app.routers import users`) needs to know that project's own
# `sys.path`/package-root convention, which is not evidence this module can
# get from the source text alone without guessing.
_PY_FROM_IMPORT_RE = re.compile(
    r"^\s*from\s+(\.+)([\w.]*)\s+import\s+([^\n]+)$", re.MULTILINE,
)

_PY_INCLUDE_ROUTER_RE = re.compile(
    r"\.include_router\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*(?:,[^)]*?prefix\s*=\s*[\"']([^\"']*)[\"'])?[^)]*\)"
)


def _py_local_import_map(file_abs: Path, text: str) -> Dict[str, Path]:
    """Real local name -> the real `.py` file it resolves to, for every
    real, relative `from .x import y` / `from .x.y import z` statement in
    `text` - handles both shapes named in this module's own docstring:
    `y` naming a real submodule file directly, or `y` naming an attribute
    (commonly the `APIRouter()` instance itself) defined inside `x`'s own
    module file.
    """
    mapping: Dict[str, Path] = {}
    for match in _PY_FROM_IMPORT_RE.finditer(text):
        dots, dotted_module, names_text = match.group(1), match.group(2), match.group(3)
        base_dir = file_abs.parent
        for _ in range(len(dots) - 1):
            base_dir = base_dir.parent
        module_dir = base_dir / Path(*dotted_module.split(".")) if dotted_module else base_dir

        for part in names_text.split(","):
            part = part.strip().rstrip("\\").strip()
            if not part or part == "(" or part == ")":
                continue
            part = part.strip("()").strip()
            if not part:
                continue
            if " as " in part:
                original, local_name = part.split(" as ", 1)
                original, local_name = original.strip(), local_name.strip()
            else:
                original = local_name = part
            if not _SIMPLE_IDENT_RE.match(local_name) or not _SIMPLE_IDENT_RE.match(original):
                continue

            submodule_file = module_dir / (original + ".py")
            if submodule_file.is_file():
                mapping[local_name] = submodule_file
                continue
            own_module_file = module_dir.with_suffix(".py")
            if own_module_file.is_file():
                mapping[local_name] = own_module_file
                continue
            init_file = module_dir / "__init__.py"
            if init_file.is_file():
                mapping[local_name] = init_file
    return mapping


def _find_fastapi_mounts(text: str):
    """Every real `<x>.include_router(<identifier>[.<attr>][, ...,
    prefix="...")]` call - `identifier` (before any `.router`-style
    attribute access) is what this module's own import map resolves;
    `prefix` is `""` (never `None`) when the call carries no real `prefix=`
    keyword at all - a real, valid, undecorated mount.
    """
    mounts = []
    for match in _PY_INCLUDE_ROUTER_RE.finditer(text):
        target_expr, prefix = match.group(1), match.group(2) or ""
        local_name = target_expr.split(".", 1)[0]
        if _SIMPLE_IDENT_RE.match(local_name):
            mounts.append((prefix, local_name))
    return mounts


def apply_fastapi_composition(root, endpoints, py_files_text: Dict[Path, str]):
    """The same real, import-graph-based composition `apply_express_
    composition` performs, for FastAPI's own `include_router(router,
    prefix=...)` idiom. See that function's own docstring for the exact
    contract and return shape - identical here.
    """
    root = Path(root)
    import_maps = {f: _py_local_import_map(f, t) for f, t in py_files_text.items()}

    mounts_by_file: Dict[Path, list] = {}
    for f, t in py_files_text.items():
        mounts = []
        for prefix, local_name in _find_fastapi_mounts(t):
            target = import_maps.get(f, {}).get(local_name)
            if target is None or target not in py_files_text:
                continue
            mounts.append((prefix, target))
        if mounts:
            mounts_by_file[f] = mounts

    mounted_targets = {target for mounts in mounts_by_file.values() for _prefix, target in mounts}

    endpoints_by_file: Dict[Path, list] = {}
    for e in endpoints:
        endpoints_by_file.setdefault((root / e.source_file).resolve(), []).append(e)

    warnings = []
    resolved = []
    globally_visited = set()

    def _walk(file_abs: Path, prefix: str, depth: int, visited: frozenset):
        if depth > _MAX_COMPOSITION_DEPTH:
            warnings.append(
                "fastapi router composition under {} exceeds the maximum depth ({}) - stopped "
                "resolving further, to avoid an unbounded walk".format(file_abs.name, _MAX_COMPOSITION_DEPTH)
            )
            return
        if file_abs in visited:
            warnings.append(
                "circular fastapi router include detected involving {} - stopped resolving that branch"
                .format(file_abs.name)
            )
            return
        visited = visited | {file_abs}
        globally_visited.add(file_abs)
        for e in endpoints_by_file.get(file_abs, ()):
            if prefix:
                new_path = prefix.rstrip("/") + e.path
                discovered_by = tuple(dict.fromkeys(e.discovered_by + ("fastapi-composition",)))
                resolved.append(replace(e, path=new_path, discovered_by=discovered_by))
            else:
                resolved.append(e)
        for mount_prefix, target in mounts_by_file.get(file_abs, ()):
            _walk(target, prefix.rstrip("/") + mount_prefix, depth + 1, visited)

    # See `apply_express_composition`'s own comment on this exact line -
    # identical reasoning: an orchestrator module (`main.py`) with no
    # routes of its own, only `include_router(...)` calls, must still be
    # walked from as a root.
    root_candidates = (set(endpoints_by_file) | set(mounts_by_file)) - mounted_targets
    for file_abs in root_candidates:
        _walk(file_abs, "", 0, frozenset())

    for file_abs, file_endpoints in endpoints_by_file.items():
        if file_abs in mounted_targets and file_abs not in globally_visited:
            resolved.extend(file_endpoints)

    return tuple(resolved), tuple(warnings)
