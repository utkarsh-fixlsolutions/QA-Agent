"""Real Mongoose model/schema discovery: a small, bounded parser for
`new (mongoose.)Schema({...})` definitions plus the `mongoose.model('Name',
schemaVar)` call that binds one to a real, exported model name - and
resolving which model a given route's own real handler actually uses, so
its real `required: true` fields become the same kind of strong evidence
`zod_schema.py` already provides for Zod, feeding the identical
`EVIDENCE_SCHEMA` tier in `resolution.py` rather than a second, competing
mechanism.

Mirrors `zod_schema.py`'s own scope discipline - a real, named boundary,
not a full JS/TS parse:

- Only a schema and its `mongoose.model(...)` binding defined in the *same
  file* are recognized (idurar-erp-crm's own `Admin.js`, and the
  overwhelming majority of real Mongoose projects, both do this) - a schema
  variable exported from one file and passed to `mongoose.model()` in a
  different one is a real, accepted gap, never guessed at.
- A field's own required-ness is read only from an explicit `required:
  true` / `required: [true, '...']` - Mongoose's own validator-function
  form (`required: function() {...}`) is real, valid Mongoose but is never
  evaluated (it would need running the project's own code) and is treated
  as not-required rather than guessed either way.
- Resolving *which* model a route's own handler references reuses
  `route_composition.py`'s existing import-map/module-resolution helpers
  unmodified (the same real, general require/import graph, never a second
  copy of it) - the two real, general patterns this module recognizes for
  "this file uses that model" are (1) the model's own local import
  identifier appears anywhere else in the file (`const Product =
  require(...); new Product(req.body)` / `createCRUDController(Product)`),
  and (2) a string literal in the file names a real, registered model
  exactly (`createAuthMiddleware('Admin')` - a common factory-by-name
  convention, found via idurar-erp-crm's own real login controller).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Tuple

from .route_composition import _local_import_map_any, _resolve_js_module_path_any
from .zod_schema import _find_js_files, _find_matching_brace, _read_text_capped, _split_top_level

# `const adminSchema = new Schema({` / `const adminSchema = new mongoose.Schema({`
_MONGOOSE_SCHEMA_DEF_RE = re.compile(
    r"(?:const|let|var)\s+(\w+)\s*=\s*new\s+(?:mongoose\s*\.\s*)?Schema\s*\(\s*\{"
)

# `mongoose.model('Admin', adminSchema)` - the real, exported model name and
# the real schema variable it binds to, wherever this call appears in the
# file (assigned to `module.exports` or not - both are real, valid forms).
_MONGOOSE_MODEL_RE = re.compile(
    r"mongoose\s*\.\s*model\s*\(\s*[\"']([\w]+)[\"']\s*,\s*(\w+)\s*[,)]"
)

_MONGOOSE_REQUIRED_RE = re.compile(r"\brequired\s*:\s*(?:true\b|\[\s*true\b)")
_STRIP_IMPORT_LINE_RE = re.compile(r"^.*\b(?:require\s*\(|import\b).*$", re.MULTILINE)
_MONGOOSE_TYPE_RE = re.compile(r"\btype\s*:\s*([A-Za-z_$][A-Za-z0-9_$.]*)")
_BARE_TYPE_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$.]*$")


def _type_token_from_dotted(dotted: str) -> str:
    """`mongoose.Schema.Types.ObjectId` / `Schema.Types.ObjectId` /
    `ObjectId` / `String` -> the one, real, final segment - this module's
    own small, closed type-token vocabulary is keyed by that segment alone,
    the same "final segment only" reading `_url_path_from_route_file`-style
    helpers elsewhere in this package already use for a dotted real name.
    """
    return dotted.rsplit(".", 1)[-1]


def _parse_mongoose_field_value(value_text: str) -> Optional[Tuple[str, str]]:
    """`(type_token, "")` when this field's own value text is real evidence
    it is required, `None` otherwise (not required, or an array-shorthand/
    unrecognized shape this module does not confidently type). Handles both
    real Mongoose shapes: a bare type shorthand (`title: String` - never
    required on its own, shorthand carries no `required` clause to read),
    and an options object (`title: { type: String, required: true }`).
    """
    value_text = value_text.strip()
    if _BARE_TYPE_RE.match(value_text):
        return None
    if not value_text.startswith("{"):
        return None
    if not _MONGOOSE_REQUIRED_RE.search(value_text):
        return None
    type_match = _MONGOOSE_TYPE_RE.search(value_text)
    type_token = _type_token_from_dotted(type_match.group(1)) if type_match else "Mixed"
    return type_token, ""


def parse_mongoose_schema_body(body_text: str) -> Tuple[Tuple[str, str], ...]:
    """Every real, required field a `new Schema({ ... })` body (the text
    strictly between its outer braces) declares, as `(field_name,
    type_token)` pairs, in declared order - mirrors `zod_schema.
    parse_zod_object_body`'s own contract exactly. A field this module
    cannot confidently confirm as required is excluded, never included with
    a guessed requiredness.
    """
    fields = []
    for part in _split_top_level(body_text):
        key_text, sep, value_text = part.partition(":")
        if not sep:
            continue
        name = key_text.strip().strip("'\"`")
        if not name or not re.match(r"^[A-Za-z_$][A-Za-z0-9_$]*$", name):
            continue
        parsed = _parse_mongoose_field_value(value_text.strip())
        if parsed is not None:
            fields.append((name, parsed[0]))
    return tuple(fields)


def build_mongoose_schema_registry(root) -> Tuple[Dict[str, Tuple[Tuple[str, str], ...]], Dict[Path, str]]:
    """One whole-project pass, mirroring `zod_schema.build_zod_schema_
    registry`'s own shape: `(by_model_name, file_to_model_name)`.
    `by_model_name` maps a real, exported Mongoose model name to its own
    real required fields; `file_to_model_name` maps the real, absolute file
    that defines that model to the same name, so a controller's own local
    import of that file can be resolved back to it. A model name defined
    more than once keeps whichever real definition was found first - never
    merged, never guessed at which one a project "really" means.
    """
    root = Path(root)
    by_model_name: Dict[str, Tuple[Tuple[str, str], ...]] = {}
    file_to_model_name: Dict[Path, str] = {}
    for js_file in _find_js_files(root):
        text = _read_text_capped(js_file)
        if text is None:
            continue
        schema_vars: Dict[str, Tuple[Tuple[str, str], ...]] = {}
        for match in _MONGOOSE_SCHEMA_DEF_RE.finditer(text):
            var_name = match.group(1)
            open_brace = match.end() - 1
            close_brace = _find_matching_brace(text, open_brace)
            if close_brace == -1:
                continue
            schema_vars[var_name] = parse_mongoose_schema_body(text[open_brace + 1:close_brace])
        for match in _MONGOOSE_MODEL_RE.finditer(text):
            model_name, schema_var = match.group(1), match.group(2)
            fields = schema_vars.get(schema_var)
            if fields is None or model_name in by_model_name:
                continue
            by_model_name[model_name] = fields
            file_to_model_name[js_file.resolve()] = model_name
    return by_model_name, file_to_model_name


def find_referenced_model_fields(
    file_abs: Path, file_text: str,
    by_model_name: Dict[str, Tuple[Tuple[str, str], ...]],
    file_to_model_name: Dict[Path, str],
) -> Tuple[Tuple[str, str], ...]:
    """The real, required fields of whichever real Mongoose model
    `file_text` (one handler-defining file's own real source) actually
    uses - `()` when neither real signal below is found. Checked in order:

    1. A locally imported model - `file_text`'s own real `require`/`import`
       map is resolved (`route_composition._local_import_map`, the exact
       same real graph mount-composition already uses) against `file_to_
       model_name`; the local identifier it was imported as must also
       appear somewhere else in `file_text` (real usage, not just an unused
       import) - counted as more than the one occurrence the import
       statement itself already contributes.
    2. A string literal naming a real, registered model exactly
       (`createAuthMiddleware('Admin')`) - a common factory-by-name
       convention (found via idurar-erp-crm's own real login controller,
       which never imports a model at all).

    Never a guess at an unrelated model, and never both combined into one
    body - the first real signal found wins.
    """
    if not by_model_name:
        return ()
    import_map = _local_import_map_any(file_text)
    # A require/import line's own text routinely contains the local name
    # twice already (the bound identifier, and the same word again inside
    # its own module-path string, e.g. `require('../models/Product')`) -
    # stripped entirely before counting, so only real usage *elsewhere* in
    # the file is ever counted as real evidence, never the import
    # statement's own text mistaken for a second, real reference.
    usage_text = _STRIP_IMPORT_LINE_RE.sub("", file_text)
    for local_name, spec in import_map.items():
        target = _resolve_js_module_path_any(file_abs, spec)
        if target is None:
            continue
        model_name = file_to_model_name.get(target)
        if model_name is None:
            continue
        if re.search(r"\b{}\b".format(re.escape(local_name)), usage_text):
            return by_model_name[model_name]

    for model_name, fields in by_model_name.items():
        if re.search(r"[\"']{}[\"']".format(re.escape(model_name)), file_text):
            return fields
    return ()


# --- bridging to synthesis.py's own (type, format) vocabulary --------------

_MONGOOSE_TOKEN_TO_PROP = {
    "String": {"type": "string"},
    "Number": {"type": "number"},
    "Boolean": {"type": "boolean"},
    "Date": {"type": "string", "format": "date-time"},
    "ObjectId": {"type": "string", "format": "objectid"},
    "Mixed": {"type": "string"},
    "Buffer": {"type": "string"},
    "Map": {"type": "object"},
    "Array": {"type": "array"},
}


def mongoose_field_to_prop(type_token: str) -> dict:
    """This module's own small type-token vocabulary, translated to the
    JSON-schema-style `{"type": ..., "format": ...}` shape `synthesis.
    synthesize_value` already understands - reuses that one canonical
    synthesis function, the same bridge `zod_schema.zod_field_to_prop`
    already established for Zod.
    """
    return _MONGOOSE_TOKEN_TO_PROP.get(type_token, {"type": "string"})
