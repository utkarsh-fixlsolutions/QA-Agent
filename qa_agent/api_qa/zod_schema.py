"""Real Zod schema discovery (docs/50-zod-schema-discovery.md): a small,
bounded parser for `z.object({...})` definitions, and matching one to the
specific mutating route that actually references it.

Deliberately not a JS/TS parser - a real, named scope boundary, the same
"narrow, recognizable convention" discipline `discovery.py` already
established for route discovery itself:

- Only a *top-level* `z.object({...})` is read. A nested `z.object({...})`
  used as one field's own value is recorded as that field's type
  (`"object"`), never descended into - a real, accepted limit, not a
  silent miss (nested-object test data is rare to actually need for a
  body to pass basic validation).
- Only the schema's own first, real `z.<type>(...)` call in a field's
  chain decides its type; further chained calls (`.min(1)`, `.trim()`,
  ...) are not interpreted, except `.email()`/`.optional()`/`.nullable()`/
  `.default(...)`, which are specifically recognized (format and
  required-ness are both real, useful facts worth the extra check).
- `.refine()`/`.transform()`/`.superRefine()`-based validation logic is
  invisible to this module entirely - it only ever reads the schema's own
  declared shape, never runtime logic.

Every field this module ever returns is a REQUIRED field - `.optional()`/
`.nullable()`/`.default(...)` fields are real evidence they are not
required, so they are excluded, never included and then invented a value
for regardless.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional, Tuple

_IGNORED_DIR_NAMES = frozenset({
    "node_modules", ".git", ".next", ".turbo", "coverage", "dist", "build", "out",
})
_JS_TS_FILE_RE = re.compile(r"\.(ts|tsx|js|jsx)$")
_IGNORED_FILE_RE = re.compile(r"\.(d\.ts|test\.[tj]sx?|spec\.[tj]sx?)$")

_MAX_READ_BYTES = 2_000_000

# `const createUserSchema = z.object({` / `export const createUserSchema: SomeType = z.object({`
_ZOD_SCHEMA_DEF_RE = re.compile(
    r"(?:export\s+)?const\s+(\w+)\s*(?::\s*[^=]+)?=\s*z\.object\s*\(\s*\{"
)

# A route handler referencing a schema by name - `.parse(`/`.safeParse(`
# calls, or the schema passed directly to a `validate(...)`-shaped
# middleware/helper - the two overwhelmingly common real patterns.
_ZOD_REFERENCE_RE = re.compile(
    r"\b(\w+)\.(?:parse|safeParse)\s*\(|\bvalidate\s*\(\s*(\w+)\s*[,)]"
)

_ZOD_BASE_TYPE_RE = re.compile(r"\bz\.(\w+)\s*\(")


def _find_matching_brace(text: str, open_index: int) -> int:
    """The real index of the `}` that closes the `{` at `open_index` -
    depth-tracked, skipping over string-literal contents (so a `{`/`}`
    character *inside* a string is never mistaken for real structure) -
    `-1` if the text ends before a real match is found.
    """
    depth = 0
    i = open_index
    in_string: Optional[str] = None
    length = len(text)
    while i < length:
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
        elif ch in ("'", '"', "`"):
            in_string = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_level(text: str, sep: str = ",") -> Tuple[str, ...]:
    """`text` split on `sep` only where it appears at real top-level
    nesting depth zero (never inside a nested `(...)`/`[...]`/`{...}`, and
    never inside a string literal) - what a real parser's tokenizer would
    give a naive `str.split` for free, needed here because Zod field
    chains routinely contain their own, real nested parens/brackets
    (`z.array(z.string())`, `z.enum(["a", "b"])`).
    """
    parts = []
    depth = 0
    current = []
    in_string: Optional[str] = None
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if in_string:
            current.append(ch)
            if ch == "\\" and i + 1 < length:
                current.append(text[i + 1])
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if current:
        parts.append("".join(current))
    return tuple(p for p in parts if p.strip())


# The real Zod base-call name -> this module's own small, closed type-token
# vocabulary (`zod_field_to_prop` below maps these to the JSON-schema-style
# `type`/`format` `synthesis.synthesize_value` already understands).
_ZOD_TYPE_MAP = {
    "string": "string", "number": "number", "bigint": "number",
    "boolean": "boolean", "date": "date", "array": "array",
    "object": "object", "enum": "enum", "nativeEnum": "enum",
    "literal": "string", "any": "string", "unknown": "string", "record": "object",
}


def _parse_zod_field_chain(chain_text: str) -> Optional[str]:
    """The real type token for one field's own real value-chain text
    (everything after its `key:`), or `None` when either the field is real
    evidence it is *not* required (`.optional()`/`.nullable()`/
    `.default(...)` found anywhere in the chain) or no recognized `z.
    <type>(...)` base call could be found at all (an unrecognized shape -
    never guessed).
    """
    if re.search(r"\.(optional|nullable)\s*\(\s*\)", chain_text) or ".default(" in chain_text:
        return None
    match = _ZOD_BASE_TYPE_RE.search(chain_text)
    if not match:
        return None
    base = _ZOD_TYPE_MAP.get(match.group(1))
    if base is None:
        return None
    if base == "string" and ".email(" in chain_text:
        return "email"
    return base


def _strip_key_quotes(key: str) -> str:
    key = key.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"', "`"):
        return key[1:-1]
    return key


def parse_zod_object_body(body_text: str) -> Tuple[Tuple[str, str], ...]:
    """Every real, required field this `z.object({ ... })` body (the text
    strictly between the outer braces) declares, as `(field_name,
    type_token)` pairs, in declared order. A field whose own chain this
    module cannot confidently type, or that is real evidence it is
    optional, is excluded - never included with a guessed type.
    """
    fields = []
    for part in _split_top_level(body_text):
        key_text, sep, value_text = part.partition(":")
        if not sep:
            continue
        key_text = key_text.strip()
        quoted = len(key_text) >= 2 and key_text[0] == key_text[-1] and key_text[0] in ("'", '"', "`")
        name = _strip_key_quotes(key_text)
        if not name:
            continue
        # A bare (unquoted) key must really look like a valid JS
        # identifier - a quoted key (`"user-name": ...`) is real, valid JS
        # even with characters (like `-`) an identifier could never have,
        # so it's accepted as-is once real quotes were actually stripped.
        if not quoted and not re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", name):
            continue
        type_token = _parse_zod_field_chain(value_text)
        if type_token is not None:
            fields.append((name, type_token))
    return tuple(fields)


def _find_js_files(root_abs: Path):
    found = []
    for dirpath, dirnames, filenames in os.walk(root_abs, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIR_NAMES]
        for name in filenames:
            if _JS_TS_FILE_RE.search(name) and not _IGNORED_FILE_RE.search(name):
                found.append(Path(dirpath) / name)
    return found


def _read_text_capped(path: Path):
    try:
        if path.stat().st_size > _MAX_READ_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def build_zod_schema_registry(root) -> Dict[str, Tuple[Tuple[str, str], ...]]:
    """Every real, top-level `const <name> = z.object({...})` definition
    found anywhere in the project, keyed by its own real variable name -
    the one, whole-project pass this needs (a schema is commonly defined
    in a file the route that uses it never itself is). A name defined more
    than once (rare - a real naming collision across files) keeps whichever
    real definition was found first; never merged, never guessed at which
    one a given route "really" means.
    """
    root = Path(root)
    registry: Dict[str, Tuple[Tuple[str, str], ...]] = {}
    for js_file in _find_js_files(root):
        text = _read_text_capped(js_file)
        if text is None:
            continue
        for match in _ZOD_SCHEMA_DEF_RE.finditer(text):
            name = match.group(1)
            if name in registry:
                continue
            open_brace = match.end() - 1
            close_brace = _find_matching_brace(text, open_brace)
            if close_brace == -1:
                continue
            fields = parse_zod_object_body(text[open_brace + 1:close_brace])
            if fields:
                registry[name] = fields
    return registry


def find_referenced_schema_fields(
    handler_text: str, registry: Dict[str, Tuple[Tuple[str, str], ...]],
) -> Tuple[Tuple[str, str], ...]:
    """The real, required fields of whichever real Zod schema
    `handler_text` (one route handler's own source window, the same
    per-route windowing `discovery.py`'s other body-evidence extraction
    already uses) actually references by name (`x.parse(...)`/
    `x.safeParse(...)`/`validate(x)`) - `()` when no reference is found, or
    the referenced name was never a real schema this registry actually
    has (never a guess at an unrelated schema).
    """
    for match in _ZOD_REFERENCE_RE.finditer(handler_text):
        name = match.group(1) or match.group(2)
        if name in registry:
            return registry[name]
    return ()


# --- bridging to synthesis.py's own (type, format) vocabulary --------------

_ZOD_TOKEN_TO_PROP = {
    "email": {"type": "string", "format": "email"},
    "date": {"type": "string", "format": "date-time"},
    "string": {"type": "string"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "array": {"type": "array"},
    "object": {"type": "object"},
    "enum": {"type": "string"},
}


def zod_field_to_prop(type_token: str) -> dict:
    """This module's own small type-token vocabulary, translated to the
    JSON-schema-style `{"type": ..., "format": ...}` shape
    `synthesis.synthesize_value` already understands - reuses that one
    canonical synthesis function rather than a second, competing one.
    """
    return _ZOD_TOKEN_TO_PROP.get(type_token, {"type": "string"})
