"""Existing API test/example evidence discovery (Phase 2: API contract
understanding). Searches the target project's own test files and Postman-
style collections for a real HTTP call whose method+path match a discovered
endpoint, and (when present) a real JSON request body alongside it - the
same "narrow, named convention, not a full parser" discipline every other
discovery strategy in this project already follows (see discovery.py's own
module docstring). Never executes any test or application code; only ever
reads and pattern-matches real source text.

Recognized shapes, deliberately narrow (docs for Phase 2's own scope
control - reliable extraction from common patterns, not universal test-
framework support):
  - Supertest: `request(app).post('/api/users').send({...})`
  - axios: `axios.post('/api/users', {...})`
  - fetch: `fetch('/api/users', {method: 'POST', ..., body: JSON.stringify({...})})`
  - Python (pytest + requests/httpx/TestClient): `client.post("/api/users", json={...})`
  - Postman collections (`*.postman_collection.json`): real, structured JSON -
    parsed directly, never pattern-matched.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_MAX_READ_BYTES = 2_000_000

# Mirrors discovery.py's own `_IGNORED_DIR_NAMES` (JS/TS side) plus its
# Python-specific counterpart - duplicated narrowly rather than imported,
# the same "small shared list, duplicated per module" precedent server.py's
# own `_JS_LOCKFILE_BY_MANAGER` docstring already established for this
# project.
_IGNORED_DIR_NAMES = frozenset({
    "node_modules", ".git", ".next", ".turbo", "coverage", "dist", "build", "out",
    ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})

_TEST_FILE_RE = re.compile(r"(\.test\.[jt]sx?|\.spec\.[jt]sx?|_test\.py|test_[^/\\]+\.py)$")
_POSTMAN_FILE_RE = re.compile(r"\.postman_collection\.json$")


def _read_text_capped(path):
    try:
        if path.stat().st_size > _MAX_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _find_evidence_files(root_abs) -> Tuple[List[Path], List[Path]]:
    """Every real test file and Postman collection under `root_abs`, walked
    once, pruning ignored directories - the same "prune, don't filter
    afterward" technique discovery.py's own file finders already use.
    """
    test_files, postman_files = [], []
    for dirpath, dirnames, filenames in os.walk(root_abs, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        for name in filenames:
            full = Path(dirpath) / name
            if _POSTMAN_FILE_RE.search(name):
                postman_files.append(full)
            elif _TEST_FILE_RE.search(name):
                test_files.append(full)
    return test_files, postman_files


# --- bounded object-literal extraction (narrow, not a JS/JSON parser) -----

def _find_matching_brace(text: str, open_index: int) -> Optional[int]:
    """Real depth-tracked, string-literal-aware scan for the real closing
    `}` matching the real `{` at `open_index` - the same technique
    zod_schema.py's own `_find_matching_brace` already established for the
    exact same reason (a nested object/array value's own commas/braces must
    never be mistaken for the outer literal's own close), duplicated
    narrowly here per this project's own "duplicate a small helper rather
    than reach into another module's private symbol" precedent.
    """
    depth = 0
    in_string: Optional[str] = None
    i = open_index
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
        elif ch in "\"'`":
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


# A real top-level `key: value` pair whose value is a real string, number,
# or boolean/null literal (JS and Python spellings both accepted) - never a
# nested object/array value, which is real evidence this module honestly
# does not attempt to parse (recorded as absent, never guessed at).
_KEY_VALUE_RE = re.compile(
    r"[\"']?([A-Za-z_$][A-Za-z0-9_$]*)[\"']?\s*:\s*"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|-?\d+(?:\.\d+)?|true|True|false|False|null|None)"
)


def _parse_object_literal(text: str) -> Dict[str, object]:
    """A real, braced object/dict literal's own real scalar fields - never
    a nested object/array value, never a full JS/Python expression
    evaluator. Returns `{}` for anything unrecognized.
    """
    fields: Dict[str, object] = {}
    for match in _KEY_VALUE_RE.finditer(text):
        name, raw = match.group(1), match.group(2)
        if raw in ("true", "True"):
            fields[name] = True
        elif raw in ("false", "False"):
            fields[name] = False
        elif raw in ("null", "None"):
            fields[name] = None
        elif raw[0] in "\"'":
            fields[name] = raw[1:-1]
        else:
            fields[name] = float(raw) if "." in raw else int(raw)
    return fields


def _object_literal_entry(text, method, path, brace_pos):
    if not path.startswith("/"):
        return None
    close = _find_matching_brace(text, brace_pos)
    if close is None:
        return None
    fields = _parse_object_literal(text[brace_pos:close + 1])
    if not fields:
        return None
    return (method, path, fields)


# --- JS/TS: supertest / axios / fetch --------------------------------------

_JS_METHODS = "get|post|put|patch|delete"

_SUPERTEST_RE = re.compile(
    r"\.(" + _JS_METHODS + r")\s*\(\s*[\"'`]([^\"'`]+)[\"'`]\s*\)"
    r"[\s\S]{0,300}?\.send\s*\(\s*(\{)",
    re.IGNORECASE,
)
_AXIOS_RE = re.compile(
    r"\baxios\.(" + _JS_METHODS + r")\s*\(\s*[\"'`]([^\"'`]+)[\"'`]\s*,\s*(\{)",
    re.IGNORECASE,
)
_FETCH_RE = re.compile(
    r"\bfetch\s*\(\s*[\"'`]([^\"'`]+)[\"'`]\s*,[\s\S]{0,100}?method\s*:\s*[\"'`](" + _JS_METHODS + r")[\"'`]"
    r"[\s\S]{0,300}?body\s*:\s*JSON\.stringify\s*\(\s*(\{)",
    re.IGNORECASE,
)


def _extract_js_test_evidence(text):
    found = []
    for match in _SUPERTEST_RE.finditer(text):
        method, path, brace_pos = match.group(1).upper(), match.group(2), match.start(3)
        entry = _object_literal_entry(text, method, path, brace_pos)
        if entry is not None:
            found.append(entry)
    for match in _AXIOS_RE.finditer(text):
        method, path, brace_pos = match.group(1).upper(), match.group(2), match.start(3)
        entry = _object_literal_entry(text, method, path, brace_pos)
        if entry is not None:
            found.append(entry)
    for match in _FETCH_RE.finditer(text):
        path, method, brace_pos = match.group(1), match.group(2).upper(), match.start(3)
        entry = _object_literal_entry(text, method, path, brace_pos)
        if entry is not None:
            found.append(entry)
    return found


# --- Python: pytest + requests/httpx/TestClient -----------------------------

_PY_METHODS = "get|post|put|patch|delete"
_PY_JSON_CALL_RE = re.compile(
    r"\.(" + _PY_METHODS + r")\s*\(\s*[\"']([^\"']+)[\"']\s*,[\s\S]{0,200}?\bjson\s*=\s*(\{)",
    re.IGNORECASE,
)


def _extract_python_test_evidence(text):
    found = []
    for match in _PY_JSON_CALL_RE.finditer(text):
        method, path, brace_pos = match.group(1).upper(), match.group(2), match.start(3)
        entry = _object_literal_entry(text, method, path, brace_pos)
        if entry is not None:
            found.append(entry)
    return found


# --- Postman collections: real, structured JSON, never pattern-matched -----

def _extract_postman_evidence(text):
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    found: list = []
    items = data.get("item") if isinstance(data, dict) else None
    _walk_postman_items(items, found)
    return found


def _walk_postman_items(items, found):
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if "item" in item:  # a folder - recurse, never a real request itself
            _walk_postman_items(item.get("item"), found)
            continue
        request = item.get("request")
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        if not isinstance(method, str):
            continue
        url = request.get("url")
        if isinstance(url, dict):
            raw_url = url.get("raw", "")
        elif isinstance(url, str):
            raw_url = url
        else:
            continue
        if not isinstance(raw_url, str) or not raw_url:
            continue
        body = request.get("body")
        if not isinstance(body, dict) or body.get("mode") != "raw":
            continue
        raw_body = body.get("raw")
        if not isinstance(raw_body, str):
            continue
        try:
            parsed_body = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed_body, dict) and parsed_body:
            found.append((method.upper(), raw_url, parsed_body))


# --- path matching (dynamic segments treated as wildcards) -----------------

_URL_PREFIX_RE = re.compile(r"^https?://[^/]+")
_TEMPLATE_VARY_SEGMENT_RE = re.compile(r"^[:{\[]")


def _normalize_path(raw_path: str) -> str:
    raw_path = _URL_PREFIX_RE.sub("", raw_path)
    if "?" in raw_path:
        raw_path = raw_path.split("?", 1)[0]
    if not raw_path.startswith("/"):
        raw_path = "/" + raw_path
    return raw_path.rstrip("/") or "/"


def _segments_match(endpoint_path: str, candidate_path: str) -> bool:
    """`endpoint_path` (a discovered `ApiEndpoint.path`) may carry a dynamic
    segment (`{id}`/`:id`/`[id]`); `candidate_path` (found in a real test
    file or Postman collection) commonly uses a literal value there instead
    (e.g. `/users/1`) - segment count must match exactly, and a dynamic
    endpoint segment matches any single real candidate segment; every other
    segment must match literally.
    """
    e_segs = _normalize_path(endpoint_path).strip("/").split("/")
    c_segs = _normalize_path(candidate_path).strip("/").split("/")
    if len(e_segs) != len(c_segs):
        return False
    for e_seg, c_seg in zip(e_segs, c_segs):
        if _TEMPLATE_VARY_SEGMENT_RE.match(e_seg):
            continue
        if e_seg != c_seg:
            return False
    return True


# --- combined entry point ---------------------------------------------------

def build_test_evidence_registry(root) -> Dict[Tuple[str, str], Dict[str, object]]:
    """Returns `{(method, real_candidate_path): {field_name: real_value}}` -
    one whole-project pass (the same "build a registry once, shared across
    every endpoint" pattern zod_schema.py's own `build_zod_schema_registry`
    already established), so a discovery strategy just matches each of its
    own endpoints against it via `find_test_evidence_for_endpoint`. The
    first real match found for a given `(method, path)` wins - never
    overwritten by a later, equally-real but different example.
    """
    root = Path(root)
    test_files, postman_files = _find_evidence_files(root)
    entries: list = []
    for path in test_files:
        text = _read_text_capped(path)
        if text is None:
            continue
        entries.extend(_extract_js_test_evidence(text))
        entries.extend(_extract_python_test_evidence(text))
    for path in postman_files:
        text = _read_text_capped(path)
        if text is None:
            continue
        entries.extend(_extract_postman_evidence(text))

    registry: Dict[Tuple[str, str], Dict[str, object]] = {}
    for method, path, fields in entries:
        key = (method, path)
        if key not in registry:
            registry[key] = fields
    return registry


def find_test_evidence_for_endpoint(method: str, path: str, registry) -> Optional[Dict[str, object]]:
    """The real fields+values found for the first registry entry whose own
    real method matches and whose own real path structurally matches
    `path` (dynamic segments treated as wildcards, see `_segments_match`) -
    `None` when no real match exists. Never a guess at an unrelated
    endpoint's own example.
    """
    for (m, p), fields in registry.items():
        if m == method and _segments_match(path, p):
            return fields
    return None
