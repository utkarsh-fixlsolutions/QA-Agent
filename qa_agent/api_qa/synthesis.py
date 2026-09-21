"""Deterministic synthetic-value generation for required fields with no real
evidence or schema default (docs/45-synthetic-mutation-testing.md). Pure
function, no I/O, no randomness - the same value every time for the same
inputs, so a demo run is reproducible and every synthesized value is
traceable to a rule in this file, never invented ad hoc elsewhere.

Most string values this module returns carry an unmistakable
"qa-agent-test" marker so a write made with them is trivially identifiable
(and cleanable) in a real, live database - never indistinguishable from
real user data. The two exceptions are deliberate, fixed sentinels chosen
for realism instead: a password-shaped field gets a value that actually
satisfies a typical password policy (`QaAgentTest123!` - still plainly a
test value, just without the marker's hyphens, which a password field
rarely accepts), and a date/date-time gets a fixed, recognizable
`2025-01-01T00:00:00Z`. Callers are responsible for recording *that* a
value was synthesized (`ApiCallResult.synthetic`/`synthetic_fields`) - this
module only ever decides *what* the value is.
"""

from __future__ import annotations

import re
from typing import Optional

# `isActive`/`is_active`/`hasAccess`/`has_access` - a real "is"/"has"
# prefix followed by a word boundary (an uppercase letter or underscore),
# never a bare substring match - `field_name.lower().startswith("is")`
# alone would misfire on an unrelated word like "island"/"isbn". Checked
# against the real, original field name (case matters for the camelCase
# form), not the lowered one.
_BOOLEAN_NAME_RE = re.compile(r"^(is|has)([A-Z_]|$)")

_FIXED_DATETIME = "2025-01-01T00:00:00Z"
# The canonical example ObjectId from MongoDB's own documentation - a real,
# valid 24-hex-character shape (so a Mongoose `required: true` ObjectId
# field's own cast validation actually accepts it, unlike the bare integer
# `1` used elsewhere for a generic id-shaped name), and instantly
# recognizable as a placeholder to anyone who has ever used MongoDB, rather
# than an arbitrary-looking invented hex string.
_TEST_OBJECT_ID = "507f1f77bcf86cd799439011"
_TEST_EMAIL = "qa-agent-test@example.com"
_TEST_PASSWORD = "QaAgentTest123!"
_TEST_URL = "https://qa-agent-test.example.com"
_TEST_USER = "qa-agent-test-user"
_TEST_ID = "qa-agent-test-id"
_TEST_VALUE = "qa-agent-test-value"

_JSON_TYPE_DEFAULTS = {
    "integer": 1,
    "number": 1,
    "boolean": True,
    "array": [],
    "object": {},
}


def _by_format(json_type: Optional[str], json_format: Optional[str], field_name: str) -> Optional[object]:
    """A value implied by the schema's own declared `type`/`format` -
    checked before any name-based heuristic, since a declared format is a
    more specific, more real fact than a guess from the field's own name.
    """
    fmt = (json_format or "").lower()
    if fmt == "email":
        return _TEST_EMAIL
    if fmt == "objectid":
        return _TEST_OBJECT_ID
    if fmt in ("date", "date-time"):
        return _FIXED_DATETIME
    if fmt in ("password",) or "password" in field_name.lower():
        return _TEST_PASSWORD
    if json_type in _JSON_TYPE_DEFAULTS:
        return _JSON_TYPE_DEFAULTS[json_type]
    if json_type == "string":
        return _by_name_heuristic(field_name)
    return None


def _by_name_heuristic(field_name: str) -> object:
    """No schema information at all - a placeholder chosen from the
    field's own name, most-specific rule first. Never returns anything
    without the "qa-agent-test" marker (or the plain integer `1` for an
    id-shaped field, itself an obviously-synthetic sentinel value).
    """
    lowered = field_name.lower()
    if "password" in lowered:
        return _TEST_PASSWORD
    if "email" in lowered:
        return _TEST_EMAIL
    if "url" in lowered or "link" in lowered:
        return _TEST_URL
    if "date" in lowered or "time" in lowered:
        return _FIXED_DATETIME
    if lowered == "id" or lowered.endswith("_id") or lowered.endswith("id"):
        return 1
    # docs/49-probe-based-body-synthesis.md: no real type evidence exists
    # for a field a target only ever reported as "missing" (never "wrong
    # type") - a validation-error probe can name the field, never its real
    # type - so a number/boolean-shaped value is still only ever a name
    # heuristic here, the same honest limitation every other rule in this
    # function already has, not a guess dressed up as fact.
    if _BOOLEAN_NAME_RE.match(field_name) or "enabled" in lowered or "active" in lowered:
        return True
    if any(token in lowered for token in ("count", "amount", "quantity", "qty", "number", "num", "age", "price", "total")):
        return 1
    if "name" in lowered or "title" in lowered or "username" in lowered:
        return _TEST_USER
    return _TEST_VALUE


def synthesize_value(field_name: str, prop: Optional[dict] = None) -> object:
    """The synthetic value to use for `field_name`, in order of how much
    real evidence backs it:

    1. `prop["example"]` - a real, schema-declared example value.
    2. `prop["enum"][0]` - a real, schema-declared valid value.
    3. `prop["type"]`/`prop["format"]` - a format-aware placeholder.
    4. No schema info at all - a name-based heuristic on `field_name` alone.

    `prop` is `None` (or has neither `example`/`enum`/`type`) for a purely
    source-derived field hint (docs/45) - falls straight through to (4).
    """
    if isinstance(prop, dict):
        if "example" in prop:
            return prop["example"]
        enum = prop.get("enum")
        if isinstance(enum, list) and enum:
            return enum[0]
        by_format = _by_format(prop.get("type"), prop.get("format"), field_name)
        if by_format is not None:
            return by_format
    return _by_name_heuristic(field_name)
