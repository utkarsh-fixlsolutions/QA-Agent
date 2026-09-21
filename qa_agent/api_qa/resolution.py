"""Deterministic API verification (docs/33-api-qa-deterministic-verification
.md): `resolve_and_execute(endpoints, base_url, timeout, config=None)`.

Turns "call every non-dynamic endpoint, skip every dynamic one" (Step 30's
own original, simplest-possible rule) into a real, evidence-based
verification pass: a dynamic path parameter is resolved from a real prior
response when the evidence genuinely supports it; a request body for a
mutating call is constructed only from a real, already-declared OpenAPI
schema default; anything that cannot be made testable this way is skipped,
honestly, with the exact reason why. Every concrete value ever used to make
a call testable is traceable to a real, already-observed fact - a real
response this same session already received, or a real default value the
target application's own OpenAPI schema already declares. Nothing here ever
invents a path parameter, a request body field, a header, or a credential.

    non-dynamic GET (any order)
        -> real response, parsed
    dynamic GET  -----------------\\
    dynamic/non-dynamic mutation --+--> resolved from a real prior
                                        collection-shaped GET response,
                                        or a real OpenAPI schema default,
                                        or SKIPPED with a clear reason

This module is the one, single place `endpoint.dynamic`/method-shaped
execution decisions are made - `runner.py` delegates to it entirely rather
than keeping its own, separate, simpler rule; `http_client.call_endpoint`
remains the one and only function that ever makes a real HTTP call, reused
here unmodified except for the small, additive `path_override`/`body`/
`resolution_evidence` parameters it already gained for exactly this purpose.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Tuple

from . import model_schema as _model_schema
from . import test_evidence as _test_evidence
from . import zod_schema as _zod_schema
from .auth_context import AuthContext
from .analysis import classify_negative_case_severity, classify_schema_validation_severity
from .http_client import call_endpoint
from .models import (
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    EVIDENCE_OPENAPI,
    EVIDENCE_SCHEMA,
    EVIDENCE_SOURCE_HINT,
    EVIDENCE_SYNTHETIC,
    EVIDENCE_TEST_EXAMPLE,
    EVIDENCE_UNKNOWN,
    ApiCallResult,
    ApiEndpoint,
    NegativeCallResult,
    SchemaValidationResult,
)
from .synthesis import synthesize_value

# docs/45-synthetic-mutation-testing.md: whether resolution ever synthesizes
# a placeholder path parameter or request-body field when no real evidence/
# schema default exists, rather than honestly skipping the call - `True` by
# default (`runner.ApiQaConfig`'s own default) so mutation testing actually
# exercises the target out of the box; every function below that can
# synthesize a value takes this as an explicit parameter, never a global,
# so a caller can restore today's strict evidence-only behavior in one place.
DEFAULT_ALLOW_SYNTHETIC_MUTATIONS = True

_MINIMAL_SYNTHETIC_BODY_FIELD = "qa-agent-test"


def _looks_like_id_param(param_name: str) -> bool:
    """The same "id, or an `_id`/`Id` suffix" convention
    `_candidate_field_names` already uses to decide when a generic `id`
    field is worth checking - reused here (docs/45) to decide whether a
    synthetic path-parameter value should be the numeric sentinel `1` (an
    id-shaped name) or the string sentinel `"qa-agent-test-id"` (anything
    else, e.g. `slug`/`category`).
    """
    lowered = param_name.lower()
    return lowered == "id" or lowered.endswith("_id") or lowered.endswith("id")


def synthesize_path_parameter_value(param_name: str):
    """The exact same numeric-sentinel-first-for-id-shaped-names rule
    `resolve_path_parameter`'s own synthetic fallback already uses
    internally - exposed publicly (Phase 3, docs/54) so `planning.py`'s
    dependency-sourced id resolution (e.g. a "verify deletion" test whose
    own prerequisite produced no usable id) can reuse the identical rule
    rather than a second, silently-divergent copy of it.
    """
    return 1 if _looks_like_id_param(param_name) else "qa-agent-test-id"

# --- path-parameter extraction -------------------------------------------

# Two real path-parameter syntaxes, both matched (docs/41-express-path
# -param-resolution.md): FastAPI's own `{param}`, and Express's own
# `:param` (added when Express discovery itself was added, docs/38 - but
# never wired in here until now, so every Express dynamic endpoint was
# silently un-resolvable: `discovery.py` correctly marked it `dynamic`,
# but this module found zero params to resolve and skipped it, every
# time). Next.js's `[param]`/`[...param]` convention is a real, different
# syntax this module still does not attempt to resolve - a documented
# scope boundary (docs/33), not a silent gap; every Next.js dynamic
# endpoint keeps Step 30's own original behavior (always skipped) unchanged.
_PATH_PARAM_RE = re.compile(r"\{([^}/]+)\}|:([A-Za-z0-9_]+)")

MUTATION_METHODS = ("POST", "PUT", "PATCH", "DELETE")


def _path_param_names(path: str) -> Tuple[str, ...]:
    return tuple(brace_name or colon_name for brace_name, colon_name in _PATH_PARAM_RE.findall(path))


def _param_token(path: str, param_name: str) -> str:
    """The real, literal token `param_name` actually appears as in `path` -
    `{param_name}` or `:param_name`, whichever is really there. Only ever
    called after `_path_param_names` has already found `param_name` as a
    real match in this exact path, so this never has to guess between the
    two syntaxes.
    """
    curly = "{" + param_name + "}"
    return curly if curly in path else ":" + param_name


def _parent_collection_path(path: str, param_name: str) -> Optional[str]:
    """The real, structural REST convention this whole strategy rests on,
    named explicitly rather than pretending to handle arbitrary path
    shapes: the collection endpoint for a dynamic segment is its own path
    with that segment (and everything after it) removed - `/api/users` for
    `/api/users/{user_id}`, and just as validly `/forms` for
    `/forms/{formId}/responses` (Phase 2, docs/53: the dynamic segment need
    not be the trailing one - a real, common nested-resource shape). Once a
    real value is resolved, the caller substitutes it into the *original*
    full path (`dynamic_endpoint.path.replace(token, value)`), which
    already preserves whatever real, literal segments follow it - this
    function only ever needs to name the real parent collection to gather
    evidence from. Returns `None` if `param_name`'s own token is not
    actually present in `path` at all (should not happen given the
    caller's own gating, but never assumed).
    """
    segments = path.strip("/").split("/")
    token = _param_token(path, param_name)
    if token not in segments:
        return None
    index = segments.index(token)
    parent = segments[:index]
    return "/" + "/".join(parent) if parent else "/"


def _candidate_field_names(param_name: str) -> Tuple[str, ...]:
    """Which real JSON object keys are worth checking for this parameter's
    real value - the exact parameter name first, always; the generic `id`
    convention only when the parameter's own name already signals it is
    an identifier (`id` itself, or an `_id`/`Id` suffix) - never for an
    arbitrarily-named parameter (`slug`, `category`, ...), where guessing
    `id` would silently use the wrong field rather than genuinely resolve
    the right one.
    """
    candidates = [param_name]
    lowered = param_name.lower()
    if (lowered == "id" or lowered.endswith("_id") or lowered.endswith("id")) and "id" not in candidates:
        candidates.append("id")
    return tuple(candidates)


def _extract_scalar_value(response_json, param_name: str):
    """The real value for `param_name`, found in the first well-formed
    object of a real, already-parsed JSON list response - or `None` if no
    real, plausible value can be found. Never returns anything but a real
    `int` or a real, non-empty `str` actually present in the data; never a
    `bool` (an `int` subclass - explicitly excluded, the same trap this
    project's own response-schema validators already guard against
    elsewhere), never `None`/a nested object/list mistaken for a scalar id.
    """
    if not isinstance(response_json, list):
        return None
    for item in response_json:
        if not isinstance(item, dict):
            continue
        for key in _candidate_field_names(param_name):
            if key not in item:
                continue
            value = item[key]
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip():
                return value
    return None


@dataclass(frozen=True)
class _Evidence:
    """One real, already-executed, PASSing GET call's own real parsed
    response - the only kind of fact this module ever resolves a dynamic
    parameter from.
    """

    endpoint: ApiEndpoint
    response_json: object


def resolve_path_parameter(
    dynamic_endpoint: ApiEndpoint, evidence: Tuple[_Evidence, ...],
    allow_synthetic_mutations: bool = DEFAULT_ALLOW_SYNTHETIC_MUTATIONS,
):
    """Returns `(concrete_path, evidence_text, synthetic_field)` -
    `synthetic_field` is `None` for a real, evidence-backed resolution, or
    the real path-parameter name when `concrete_path` was synthesized
    instead (docs/45). Returns `(None, skip_reason, None)` when no value -
    real or synthetic - could be produced. Only ever considers the ONE
    structurally-implied parent collection endpoint (see
    `_parent_collection_path`); only ever considers evidence from a real
    endpoint whose own path actually matches that parent, exactly - never
    any other already-passed response, however plausible-looking its own
    data might be.

    When real evidence is not available and `allow_synthetic_mutations` is
    `True`, falls back to a deterministic synthetic id (docs/45's own
    numeric-sentinel-first-for-id-shaped-names rule, via
    `_looks_like_id_param`/`synthesize_value`) rather than skipping - a
    real 404/200/500 against a made-up id is real, useful evidence about
    how the target handles an unknown id, never a guess about behavior.
    `allow_synthetic_mutations=False` reproduces the original evidence-only
    skip exactly.
    """
    params = _path_param_names(dynamic_endpoint.path)
    if len(params) != 1:
        return None, (
            "path parameter resolution only supports exactly one dynamic segment per endpoint "
            "(found {}) - not attempted".format(len(params))
        ), None
    param_name = params[0]
    parent_path = _parent_collection_path(dynamic_endpoint.path, param_name)
    if parent_path is None:
        return None, (
            "'{}' could not be located as a real path segment of '{}' - no structural parent "
            "collection endpoint could be determined"
            .format(_param_token(dynamic_endpoint.path, param_name), dynamic_endpoint.path)
        ), None

    def _use_synthetic_or_skip(skip_reason):
        if not allow_synthetic_mutations:
            return None, skip_reason, None
        synthetic_value = 1 if _looks_like_id_param(param_name) else "qa-agent-test-id"
        concrete_path = dynamic_endpoint.path.replace(
            _param_token(dynamic_endpoint.path, param_name), str(synthetic_value))
        evidence_text = "{}={!r} (synthetic id used: no real evidence was available)".format(
            param_name, synthetic_value)
        return concrete_path, evidence_text, param_name

    for fact in evidence:
        if fact.endpoint.path != parent_path:
            continue
        value = _extract_scalar_value(fact.response_json, param_name)
        if value is None:
            return _use_synthetic_or_skip(
                "found the real parent collection endpoint '{} {}', but its response contained no "
                "usable '{}' (or 'id') field - never guessed".format(
                    fact.endpoint.method, fact.endpoint.path, param_name)
            )
        concrete_path = dynamic_endpoint.path.replace(_param_token(dynamic_endpoint.path, param_name), str(value))
        evidence_text = "{}={!r} (from real response: {} {})".format(
            param_name, value, fact.endpoint.method, fact.endpoint.path)
        return concrete_path, evidence_text, None

    return _use_synthetic_or_skip(
        "no evidence-based value could be resolved for path parameter '{}' - the real parent "
        "collection endpoint '{}' was not found among this session's own passing GET responses"
        .format(param_name, parent_path)
    )


# --- OpenAPI-schema-based request body construction ------------------------

_OPENAPI_PATH = "/openapi.json"
_OPENAPI_SYNTHETIC_ENDPOINT = ApiEndpoint(method="GET", path=_OPENAPI_PATH, source_file="<api_qa:openapi>")


def fetch_openapi_schema(base_url: str, timeout: float):
    """A real GET to the target application's own `/openapi.json` - reuses
    `call_endpoint` unmodified (the exact same bounded read/JSON-parse
    path every other call in this package already goes through; no second
    HTTP mechanism). Returns the real, parsed schema document, or `None`
    when it is not reachable/valid - never fabricated, and never treated
    as fatal (a target with no reachable OpenAPI document simply cannot
    have a request body constructed this way; every mutating endpoint is
    then honestly `SKIPPED` for that specific, named reason).
    """
    result = call_endpoint(base_url, _OPENAPI_SYNTHETIC_ENDPOINT, timeout=timeout)
    if result.status != CALL_PASS or not isinstance(result.response_json, dict):
        return None
    return result.response_json


# Real, named, bounded locations only - never an unbounded repository scan
# (the same "prune, don't wander" discipline every discovery strategy in
# this project already follows). Phase 4 (generalized discovery) added the
# `.yaml`/`.yml` variant of each candidate (PyYAML, added to
# requirements.txt for exactly this) - a real, common OpenAPI form Phase 2
# had explicitly deferred for lacking a YAML-parsing dependency.
_STATIC_OPENAPI_CANDIDATES = (
    "openapi.json", "swagger.json", "openapi.yaml", "swagger.yaml", "openapi.yml", "swagger.yml",
    "spec/openapi.json", "specs/openapi.json", "docs/openapi.json", "api/openapi.json",
    "spec/swagger.json", "specs/swagger.json", "docs/swagger.json", "api/swagger.json",
    "spec/openapi.yaml", "specs/openapi.yaml", "docs/openapi.yaml", "api/openapi.yaml",
    "spec/swagger.yaml", "specs/swagger.yaml", "docs/swagger.yaml", "api/swagger.yaml",
    "spec/openapi.yml", "specs/openapi.yml", "docs/openapi.yml", "api/openapi.yml",
    "spec/swagger.yml", "specs/swagger.yml", "docs/swagger.yml", "api/swagger.yml",
)

_MAX_STATIC_SCHEMA_BYTES = 5_000_000

_YAML_SUFFIXES = (".yaml", ".yml")


def _parse_static_schema_text(rel, text):
    if rel.endswith(_YAML_SUFFIXES):
        import yaml  # local import: only paid for when a .yaml/.yml candidate actually exists
        return yaml.safe_load(text)
    return json.loads(text)


def find_static_openapi_schema_detailed(root):
    """Returns `(doc_or_None, source_file, warnings)` - the same real,
    already-checked-out OpenAPI/Swagger file `find_static_openapi_schema`
    itself returns just the parsed document for, plus which real candidate
    file it actually came from (Phase 4's own OpenAPI-as-independent-
    discovery-source strategy in discovery.py needs this for provenance)
    and a real, human-readable warning for every candidate that existed on
    disk but could not actually be used (too large, malformed JSON/YAML, or
    parsed but missing a top-level `paths` object) - never silently
    ignored, unlike this function's own original, warning-less behavior.
    """
    root = Path(root)
    warnings = []
    for rel in _STATIC_OPENAPI_CANDIDATES:
        candidate = root / rel
        try:
            if not candidate.is_file():
                continue
            if candidate.stat().st_size > _MAX_STATIC_SCHEMA_BYTES:
                warnings.append(
                    "skipped {} - larger than the {:.0f} MB size cap".format(
                        rel, _MAX_STATIC_SCHEMA_BYTES / 1_000_000)
                )
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
            doc = _parse_static_schema_text(rel, text)
        except OSError as exc:
            warnings.append("could not read {}: {}".format(rel, exc))
            continue
        except ValueError as exc:
            kind = "YAML" if rel.endswith(_YAML_SUFFIXES) else "JSON"
            warnings.append("{} is not valid {}: {}".format(rel, kind, exc))
            continue
        except ImportError:
            warnings.append(
                "found {} but the PyYAML dependency is not installed - cannot parse it".format(rel)
            )
            continue
        if isinstance(doc, dict) and "paths" in doc:
            return doc, rel, tuple(warnings)
        warnings.append(
            "{} was parsed but has no top-level 'paths' object - not a usable OpenAPI document".format(rel)
        )
    return None, "", tuple(warnings)


def find_static_openapi_schema(root):
    """A real, already-checked-out `openapi.json`/`swagger.json`/
    `openapi.yaml`/`swagger.yaml` file the target project ships with itself
    (Phase 2, docs/53; YAML added in Phase 4) - tried only as a fallback
    when the live server never served its own `/openapi.json` (see
    `resolve_and_execute`'s own `_schema()` closure): a running server's own
    live document reflects the code actually being tested right now, which
    outranks a possibly-stale file checked into the repository. Returns the
    real, parsed document, or `None` when nothing usable is found - never
    fabricated, never treated as fatal. A thin wrapper around `find_static_
    openapi_schema_detailed` for every existing caller that only ever
    needed the document itself, unchanged.
    """
    doc, _source_file, _warnings = find_static_openapi_schema_detailed(root)
    return doc


# Sentinel distinguishing "this operation/schema could not be found at
# all" from the real, valid fact "this operation declares no request body"
# (which is represented as a real `None`, not this sentinel).
_MISSING = object()


def _resolve_schema_ref(schema_doc: dict, node):
    if isinstance(node, dict) and set(node.keys()) == {"$ref"}:
        ref = node["$ref"]
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            return None
        return schema_doc.get("components", {}).get("schemas", {}).get(ref[len(prefix):])
    return node


def _operation_request_schema(schema_doc: dict, method: str, path: str):
    """The real request-body schema OpenAPI declares for `method path`, or
    `_MISSING` (a sentinel, not `None` - `None` is reserved for "this
    operation declares no request body at all", a real, valid, different
    fact from "the operation/schema could not be found").
    """
    operation = schema_doc.get("paths", {}).get(path, {}).get(method.lower())
    if not isinstance(operation, dict):
        return _MISSING
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return None  # no body declared - a real, valid fact
    schema = request_body.get("content", {}).get("application/json", {}).get("schema")
    if schema is None:
        return None
    resolved = _resolve_schema_ref(schema_doc, schema)
    return resolved if isinstance(resolved, dict) else _MISSING


def build_request_body(
    schema_doc, method: str, path: str, endpoint: Optional[ApiEndpoint] = None,
    allow_synthetic_mutations: bool = DEFAULT_ALLOW_SYNTHETIC_MUTATIONS,
):
    """Returns `(body_dict, evidence_text, synthetic_fields, evidence_source)`
    on success, or `(None, skip_reason, (), evidence_source)`. `synthetic_
    fields` is an empty tuple for a body built entirely from real schema
    defaults (docs/33's original, unchanged behavior) - non-empty names
    exactly which field(s) were synthesized (docs/45) rather than found as
    real evidence. `evidence_source` (Phase 2, docs/53) is one of
    `models.BODY_EVIDENCE_SOURCES` - the strongest evidence tier that
    actually justified this body, a structured fact alongside the existing
    human-readable `evidence_text`.

    Precedence when a live OpenAPI schema describes this operation:
    1. Every required property has its own real, schema-declared `default`
       -> real body, `synthetic_fields=()` (docs/33's original behavior,
       unchanged), `evidence_source=EVIDENCE_OPENAPI`.
    2. Some required properties lack a default, `allow_synthetic_mutations`
       is `True` -> those specific fields are synthesized
       (`synthesize_value`, using the property's own declared `example`/
       `enum`/`type`/`format` when present), everything else stays a real
       default - a body is never invented as a fabricated block when part
       of it is already known for real. Still `evidence_source=EVIDENCE_
       OPENAPI` - the schema itself is what named these fields as required.
    3. Same, but `allow_synthetic_mutations` is `False` -> unchanged
       original behavior: skip, honest reason.

    When no OpenAPI schema describes this operation at all (`schema is
    _MISSING`) and `allow_synthetic_mutations` is `True`, falls back to
    `endpoint`'s own evidence, in Phase 2's own explicit priority order
    (docs/53) - see `_fallback_from_source_hints`'s own docstring for the
    exact tiers. No body-reading evidence at all still skips, honestly -
    this never invents fields from nothing - and is reported as `CONTRACT
    UNKNOWN`, `evidence_source=EVIDENCE_UNKNOWN`, never silently presented
    as if the contract genuinely were known.
    """
    if schema_doc is None:
        return _fallback_from_source_hints(endpoint, allow_synthetic_mutations) or (
            None, "CONTRACT UNKNOWN - no OpenAPI schema is available to construct a request body",
            (), EVIDENCE_UNKNOWN,
        )

    schema = _operation_request_schema(schema_doc, method, path)
    if schema is _MISSING:
        return _fallback_from_source_hints(endpoint, allow_synthetic_mutations) or (
            None, "CONTRACT UNKNOWN - the OpenAPI schema does not describe {} {}".format(method, path),
            (), EVIDENCE_UNKNOWN,
        )
    if schema is None:
        return {}, "OpenAPI schema declares no request body for {} {}".format(method, path), (), EVIDENCE_OPENAPI

    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    body = {}
    missing = []
    for name in required:
        prop = properties.get(name)
        if isinstance(prop, dict) and "default" in prop:
            body[name] = prop["default"]
        else:
            missing.append(name)

    if missing and not allow_synthetic_mutations:
        return None, (
            "no deterministic request body could be constructed for {} {}: required field(s) {} "
            "have no schema default".format(method, path, ", ".join(repr(m) for m in missing))
        ), (), EVIDENCE_OPENAPI

    synthetic_fields = list(missing)
    for name in missing:
        prop = properties.get(name)
        body[name] = synthesize_value(name, prop if isinstance(prop, dict) else None)

    if synthetic_fields:
        evidence = "body {} (schema defaults + synthesized field(s) {}) for {} {}".format(
            body, ", ".join(repr(f) for f in synthetic_fields), method, path)
    elif not body:
        evidence = "empty body (OpenAPI schema declares no required fields) for {} {}".format(method, path)
    else:
        evidence = "body {} (OpenAPI schema defaults for required field(s)) for {} {}".format(body, method, path)
    return body, evidence, tuple(synthetic_fields), EVIDENCE_OPENAPI


def _fallback_from_source_hints(endpoint: Optional[ApiEndpoint], allow_synthetic_mutations: bool):
    """`None` when no fallback applies (caller uses its own honest skip
    reason); otherwise `(body, evidence, synthetic_fields, evidence_source)`
    built entirely from `endpoint`'s own evidence - never invoked at all
    when `allow_synthetic_mutations` is `False`, or when `endpoint` shows no
    evidence of reading a request body in the first place. Checked in
    Phase 2's own explicit priority order (docs/53, tiers 2-4 of the
    project's overall evidence hierarchy - tier 1, OpenAPI, is already
    handled by `build_request_body` itself before this is ever reached):

    1. `endpoint.zod_fields` (docs/50) - a real, explicit validation schema
       (`EVIDENCE_SCHEMA`) - a real declared type, not just a name.
    2. `endpoint.test_evidence_fields` (docs/53) - a real example found in
       the project's own existing tests/collections (`EVIDENCE_TEST_
       EXAMPLE`) - a real, previously-working value, stronger than a bare
       name (fed to `synthesize_value` as a real `example`, so the literal
       value is reused verbatim, the same precedence `synthesize_value`
       already gives an OpenAPI-declared `example`).
    3. `endpoint.body_field_hints` (docs/45) - route/controller source
       evidence (`EVIDENCE_SOURCE_HINT`) - a real field *name*, no type or
       value evidence.
    4. `endpoint.reads_request_body` alone (docs/45) - the weakest real
       evidence (`EVIDENCE_SOURCE_HINT`): the handler reads *a* body, shape
       unknown - a minimal one-field placeholder.
    """
    if not allow_synthetic_mutations or endpoint is None:
        return None
    if endpoint.zod_fields:
        # docs/50-zod-schema-discovery.md: real (name, type) evidence from
        # the endpoint's own referenced Zod schema - stronger than
        # `body_field_hints` alone (a real declared type, not just a name
        # to guess a type for), checked first for exactly that reason.
        body = {
            name: synthesize_value(name, _zod_schema.zod_field_to_prop(type_token))
            for name, type_token in endpoint.zod_fields
        }
        field_names = tuple(name for name, _type_token in endpoint.zod_fields)
        evidence = (
            "body {} (synthesized from the endpoint's own referenced Zod schema's real "
            "required fields and types, no live OpenAPI schema available) for {} {}".format(
                body, endpoint.method, endpoint.path)
        )
        return body, evidence, field_names, EVIDENCE_SCHEMA
    if endpoint.model_fields:
        # model_schema.py: real (name, type) evidence from a Mongoose model
        # this endpoint's own real handler actually uses - the same
        # real-type, same-tier strength `zod_fields` already gets above,
        # sourced from the ORM/DB layer instead of an application-level
        # validation schema. Checked only when no Zod schema already
        # answered this - not a guess at which one "wins" when a project
        # genuinely has both, just the existing, unmodified precedence.
        body = {
            name: synthesize_value(name, _model_schema.mongoose_field_to_prop(type_token))
            for name, type_token in endpoint.model_fields
        }
        field_names = tuple(name for name, _type_token in endpoint.model_fields)
        evidence = (
            "body {} (synthesized from the endpoint's own referenced Mongoose model's real "
            "required fields and types, no live OpenAPI schema available) for {} {}".format(
                body, endpoint.method, endpoint.path)
        )
        return body, evidence, field_names, EVIDENCE_SCHEMA
    if endpoint.test_evidence_fields:
        # Phase 2 (docs/53): a real example body found in the project's own
        # existing test files/Postman collections - each real value is
        # reused verbatim (via `synthesize_value`'s own "example" tier),
        # stronger evidence than a bare field name alone since it is a real
        # value already known to be a plausible request for this endpoint.
        body = {
            name: synthesize_value(name, {"example": value})
            for name, value in endpoint.test_evidence_fields
        }
        field_names = tuple(name for name, _value in endpoint.test_evidence_fields)
        evidence = (
            "body {} (real example values found in the project's own existing tests/collections, "
            "no live OpenAPI schema available) for {} {}".format(body, endpoint.method, endpoint.path)
        )
        return body, evidence, field_names, EVIDENCE_TEST_EXAMPLE
    if endpoint.body_field_hints:
        body = {name: synthesize_value(name, None) for name in endpoint.body_field_hints}
        evidence = (
            "body {} (synthesized from source-derived field names found in {}, no live "
            "OpenAPI schema available) for {} {}".format(
                body, endpoint.source_file, endpoint.method, endpoint.path)
        )
        return body, evidence, tuple(endpoint.body_field_hints), EVIDENCE_SOURCE_HINT
    if endpoint.reads_request_body:
        body = {_MINIMAL_SYNTHETIC_BODY_FIELD: True}
        evidence = (
            "minimal synthetic body {} ({} reads a request body but no specific field names "
            "could be extracted from its source, no live OpenAPI schema available) for {} {}"
            .format(body, endpoint.source_file, endpoint.method, endpoint.path)
        )
        return body, evidence, (_MINIMAL_SYNTHETIC_BODY_FIELD,), EVIDENCE_SOURCE_HINT
    return None


# --- probe-based body synthesis (docs/49) -----------------------------
#
# The last-resort fallback, tried only once `build_request_body` has
# already found no schema and no source-derived evidence at all. Rather
# than give up, this asks the target itself: send a real, empty `{}` body,
# and read whatever real validation error comes back as evidence - the
# target's own real response naming its own real required fields is
# stronger evidence than a static source-code guess could ever be.

# Real field-name-bearing shapes this recognizes, by name - never a
# universal parser, the same "narrow, named convention" discipline every
# other evidence-extraction function in this module already follows:
#  - FastAPI/Pydantic: {"detail": [{"loc": ["body", "email"], ...}, ...]}
#  - express-validator / a common hand-rolled shape:
#    {"errors": [{"param": "email", ...}]} / {"path": "email"} / {"field": "email"}
#  - a plain field-keyed error object: {"errors": {"email": "is required"}}
#  - zod's own `.flatten()` shape: {"fieldErrors": {"email": [...]}},
#    possibly nested under {"error": {"fieldErrors": {...}}}
_MISSING_FIELD_STOP_NAMES = frozenset({"body", "__root__", "query", "path", "header", "headers"})


def _extract_missing_fields_from_error_response(response_json):
    """Real field names named by the target's own validation-error body -
    see this section's own module-level comment for exactly which shapes
    are recognized. Returns `()`, honestly, for anything else (including
    a non-dict body, or a dict that matches none of the shapes above) -
    never a guess at what a differently-shaped error body might mean.
    """
    if not isinstance(response_json, dict):
        return ()

    names = []
    seen = set()

    def _add(value):
        if isinstance(value, list) and value:
            value = value[-1]
        if isinstance(value, str) and value and value not in _MISSING_FIELD_STOP_NAMES and value not in seen:
            seen.add(value)
            names.append(value)

    detail = response_json.get("detail")
    if isinstance(detail, list):
        for item in detail:
            if isinstance(item, dict):
                _add(item.get("loc"))

    errors = response_json.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                _add(item.get("param"))
                _add(item.get("path"))
                _add(item.get("field"))
    elif isinstance(errors, dict):
        for key in errors:
            _add(key)

    field_errors = response_json.get("fieldErrors")
    if isinstance(field_errors, dict):
        for key in field_errors:
            _add(key)
    nested_error = response_json.get("error")
    if isinstance(nested_error, dict):
        nested_field_errors = nested_error.get("fieldErrors")
        if isinstance(nested_field_errors, dict):
            for key in nested_field_errors:
                _add(key)

    return tuple(names)


def _probe_and_synthesize_body(base_url, endpoint, path_override, timeout, auth_context=None):
    """One real, evidence-gated last resort (docs/49), tried only when
    `build_request_body` already found nothing to work with: send a real
    `{}` body, and act on the target's own real response.

    - A real 2xx -> `{}` genuinely is a valid body; the probe call itself
      *is* the result, returned as-is, never marked synthetic (a real
      response just established this as fact, not a guess).
    - A real rejection naming its own missing/required fields
      (`_extract_missing_fields_from_error_response`) -> those exact real
      field names are synthesized (`synthesize_value`, name-heuristic
      only - a "missing field" error never carries real type evidence,
      only a name) and one real retry call is made with that body,
      returned with `synthetic=True` and `synthetic_fields` naming exactly
      which fields were invented.
    - Anything else (no response, a crash, or a rejection with no
      recognizable field list) -> `None` - the caller keeps its own
      existing, honest skip reason; nothing here ever guesses a field name
      that wasn't actually named in a real response.
    """
    auth_context = auth_context if auth_context is not None else AuthContext()
    probe = call_endpoint(
        base_url, endpoint, timeout=timeout, path_override=path_override,
        body={}, resolution_evidence="probe: sent an empty body to discover the target's own required fields",
        extra_headers=auth_context.headers(),
    )
    auth_context.observe(endpoint, probe)
    if probe.status == CALL_PASS:
        return probe

    missing_fields = _extract_missing_fields_from_error_response(probe.response_json)
    if not missing_fields:
        return None

    synthetic_body = {name: synthesize_value(name, None) for name in missing_fields}
    evidence = (
        "empty-body probe got {} naming required field(s) {} - body {} synthesized from those "
        "real field names".format(
            probe.status_code, ", ".join(repr(f) for f in missing_fields), synthetic_body)
    )
    retry = call_endpoint(
        base_url, endpoint, timeout=timeout, path_override=path_override,
        body=synthetic_body, resolution_evidence=evidence, extra_headers=auth_context.headers(),
    )
    auth_context.observe(endpoint, retry)
    return replace(retry, synthetic=True, synthetic_fields=missing_fields)


# --- execution ---------------------------------------------------------

def _is_dynamic_get(endpoint: ApiEndpoint) -> bool:
    return endpoint.method == "GET" and endpoint.dynamic


def _skip(endpoint: ApiEndpoint, reason: str) -> ApiCallResult:
    return ApiCallResult(endpoint=endpoint, status=CALL_SKIPPED, reason=reason)


def resolve_and_execute(endpoints: Tuple[ApiEndpoint, ...], base_url: str, timeout: float,
                         schema_doc=None, on_progress=None,
                         allow_synthetic_mutations: bool = DEFAULT_ALLOW_SYNTHETIC_MUTATIONS,
                         static_schema_doc=None, auth_context=None,
                         ) -> Tuple[ApiCallResult, ...]:
    """The one public entry point. Executes `endpoints` in a deterministic,
    dependency-aware order - every non-dynamic GET first (real evidence
    gathered from each real, passing response), then every dynamic GET
    (resolved from that evidence, or honestly `SKIPPED`), then every
    mutating call (`POST`/`PUT`/`PATCH`/`DELETE` - resolved the same way
    when dynamic, and/or given a real, schema-derived body when one is
    needed, or `SKIPPED`) - but always returns exactly one `ApiCallResult`
    per endpoint **in the same order `endpoints` was given**, matching
    `ApiTestResult`'s own existing "one call per endpoint, same order"
    contract; only the internal execution order is dependency-aware, never
    the caller-visible result order.

    `schema_doc`: an already-fetched OpenAPI document, or `None` to fetch
    it fresh from `<base_url>/openapi.json` the first time a mutating
    endpoint actually needs one (never fetched at all for a project with
    no mutating endpoints - no wasted call).

    `on_progress` (optional, docs/44-live-progress.md): called as
    `on_progress(done, total, label)` after each endpoint's own result is
    decided (whether it was really called or honestly skipped) - `total`
    is always `len(endpoints)`, known upfront, so `done` counts up to a
    real, fixed total, never an estimate. Purely an additive side-channel
    notification - `None` (the default) makes this function behave exactly
    as it always has; every existing caller is unaffected.

    `allow_synthetic_mutations` (docs/45-synthetic-mutation-testing.md,
    default `True`): whether tier 2/3 may synthesize a placeholder path
    parameter or request-body field when no real evidence/schema default
    exists, rather than honestly skipping - see `resolve_path_parameter`/
    `build_request_body`'s own docstrings. Any `ApiCallResult` produced
    this way carries `synthetic=True` and names exactly which field(s) in
    `synthetic_fields` - `False` reproduces the original evidence-only
    behavior exactly.

    `static_schema_doc` (Phase 2, docs/53): a real, already-parsed OpenAPI
    document found on disk (`find_static_openapi_schema`) - tried only when
    the live `<base_url>/openapi.json` fetch itself returns nothing, never
    ahead of it (the running server's own live document is more current
    evidence than a possibly-stale checked-in file).

    `auth_context` (`auth_context.AuthContext`, optional): a real, shared
    credential state - if any endpoint's own real, passing response in this
    run carries a real bearer token or session cookie, it is captured and
    attached to every subsequent call automatically (never targeted at a
    specific "/login"-shaped path - any endpoint's real response can be the
    source). Pass the same instance used elsewhere in this run (`runner.py`)
    so a credential captured here is also available to `generate_and_
    execute_negative_cases`/`planning.execute_test_plan`; a fresh, empty one
    is created when omitted, reproducing today's uncredentialed behavior.
    """
    results = {}
    evidence: list = []
    schema_state = {"doc": schema_doc, "fetched": schema_doc is not None}
    progress_state = {"done": 0}
    total = len(endpoints)
    auth_context = auth_context if auth_context is not None else AuthContext()

    def _call(endpoint, **kwargs):
        # The headers state is read *before* this call is made, not after -
        # a call that itself is the one producing a new credential (e.g.
        # the real login call) must never be reported as having carried
        # one; only a *later* call that genuinely received it should be.
        request_headers = auth_context.headers()
        result = call_endpoint(
            base_url, endpoint, timeout=timeout, extra_headers=request_headers, **kwargs,
        )
        auth_context.observe(endpoint, result)
        if request_headers:
            result = replace(result, auth_evidence=auth_context.evidence_for_attached_call())
        return result

    def _report(endpoint):
        if on_progress is None:
            return
        progress_state["done"] += 1
        on_progress(progress_state["done"], total, "{} {}".format(endpoint.method, endpoint.path))

    def _schema():
        # Phase 2 (docs/53): a live `/openapi.json` fetch is tried first
        # (more current evidence, unchanged from before); only when that
        # returns nothing does a real, already-checked-out static spec file
        # (`static_schema_doc`, if any) get used instead - never the other
        # way around.
        if not schema_state["fetched"]:
            schema_state["doc"] = fetch_openapi_schema(base_url, timeout) or static_schema_doc
            schema_state["fetched"] = True
        return schema_state["doc"]

    # Tier 1: every non-dynamic GET - real evidence gathered from each
    # real, passing response, deterministic order (sorted by path).
    tier1 = sorted(
        (e for e in endpoints if e.method == "GET" and not e.dynamic),
        key=lambda e: e.path,
    )
    for endpoint in tier1:
        result = _call(endpoint)
        results[id(endpoint)] = result
        if result.status == CALL_PASS and result.response_json is not None:
            evidence.append(_Evidence(endpoint=endpoint, response_json=result.response_json))
        _report(endpoint)
    evidence = tuple(evidence)

    # Tier 2: every dynamic GET - resolved from tier 1's own real evidence.
    tier2 = sorted(
        (e for e in endpoints if _is_dynamic_get(e)),
        key=lambda e: e.path,
    )
    for endpoint in tier2:
        concrete_path, note, synthetic_field = resolve_path_parameter(
            endpoint, evidence, allow_synthetic_mutations,
        )
        if concrete_path is None:
            results[id(endpoint)] = _skip(endpoint, note)
            _report(endpoint)
            continue
        call = _call(endpoint, path_override=concrete_path, resolution_evidence=note)
        if synthetic_field is not None:
            call = replace(call, synthetic=True, synthetic_fields=(synthetic_field,))
        results[id(endpoint)] = call
        _report(endpoint)

    # Tier 3: every mutating call - dynamic path resolved the same way as
    # tier 2 (using only tier 1's own evidence - never a mutation's own
    # response, which was never a "collection" fact to begin with), and/or
    # a real, schema-derived body when one is needed.
    tier3 = sorted(
        (e for e in endpoints if e.method in MUTATION_METHODS),
        key=lambda e: (e.path, e.method),
    )
    for endpoint in tier3:
        concrete_path = endpoint.path
        path_note = ""
        path_synthetic_field = None
        if endpoint.dynamic:
            resolved_path, note, path_synthetic_field = resolve_path_parameter(
                endpoint, evidence, allow_synthetic_mutations,
            )
            if resolved_path is None:
                results[id(endpoint)] = _skip(endpoint, note)
                _report(endpoint)
                continue
            concrete_path, path_note = resolved_path, note

        body = None
        body_note = ""
        body_synthetic_fields: Tuple[str, ...] = ()
        body_evidence_source = ""
        if endpoint.method in ("POST", "PUT", "PATCH"):
            body, body_note, body_synthetic_fields, body_evidence_source = build_request_body(
                _schema(), endpoint.method, endpoint.path,
                endpoint=endpoint, allow_synthetic_mutations=allow_synthetic_mutations,
            )
            if body is None and allow_synthetic_mutations:
                # docs/49-probe-based-body-synthesis.md: the true last
                # resort - no schema, no source-derived hint at all. Ask
                # the target itself rather than give up: a real, empty-
                # body probe call, acted on only when its own real
                # response actually names real required fields.
                probed_call = _probe_and_synthesize_body(
                    base_url, endpoint, concrete_path if endpoint.dynamic else None, timeout,
                    auth_context=auth_context,
                )
                if probed_call is not None:
                    if path_synthetic_field is not None:
                        merged_fields = tuple(
                            [path_synthetic_field]
                            + [f for f in probed_call.synthetic_fields if f != path_synthetic_field]
                        )
                        probed_call = replace(probed_call, synthetic=True, synthetic_fields=merged_fields)
                    probed_call = replace(probed_call, body_evidence_source=EVIDENCE_SYNTHETIC)
                    results[id(endpoint)] = probed_call
                    _report(endpoint)
                    continue
                body_note = "{} ; an empty-body probe did not reveal a usable set of required fields either".format(
                    body_note)
            if body is None:
                results[id(endpoint)] = _skip(endpoint, body_note)
                _report(endpoint)
                continue

        combined_evidence = " ; ".join(part for part in (path_note, body_note) if part)
        call = _call(
            endpoint,
            path_override=(concrete_path if endpoint.dynamic else None),
            body=body, resolution_evidence=combined_evidence,
        )
        synthetic_fields = tuple(
            ([path_synthetic_field] if path_synthetic_field is not None else []) + list(body_synthetic_fields)
        )
        if synthetic_fields:
            call = replace(call, synthetic=True, synthetic_fields=synthetic_fields)
        if body_evidence_source:
            call = replace(call, body_evidence_source=body_evidence_source)
        results[id(endpoint)] = call
        _report(endpoint)

    return tuple(results[id(endpoint)] for endpoint in endpoints)


# --- negative test cases (docs/43) -----------------------------------------
#
# Both case types below are only ever attempted against an endpoint whose
# own *positive* call, made moments earlier by `resolve_and_execute`, is
# already known to have actually passed - the same "only ever act on a real,
# already-observed fact" discipline as the rest of this module. Never called
# for an endpoint whose positive path never even worked (nothing evidence-
# based to build a negative variant from), and never a guessed/random value.

def _judge_negative_call(call: ApiCallResult):
    """A negative case PASSes when the target correctly rejected bad input
    (any 4xx), FAILs when it silently accepted it (2xx - a real validation
    gap) or crashed instead of validating (5xx, or no response at all -
    worse than a validation gap). Any other real status (e.g. an unexpected
    3xx) is also a FAIL - not the honest rejection this case expected.
    """
    code = call.status_code
    if code is not None and 400 <= code < 500:
        return CALL_PASS, "correctly rejected with {}".format(code)
    if code is not None and 200 <= code < 300:
        return CALL_FAIL, "incorrectly accepted with {}".format(code)
    if code is not None and code >= 500:
        return CALL_FAIL, "crashed with {} instead of validating".format(code)
    if code is not None:
        return CALL_FAIL, "unexpected {} response instead of a rejection".format(code)
    return CALL_FAIL, call.error or "no response was received"


def _nonexistent_id_variant(template_path: str, resolved_path: str) -> Optional[str]:
    """Given the endpoint's own template (`/items/{id}` or `/items/:id`) and
    the real concrete path a prior positive call actually resolved and
    succeeded against (`/items/42`), returns a path substituting a value
    guaranteed not to collide with that real one - a large numeric sentinel
    for a numeric id, or the real value plus a fixed suffix for a string id.
    Never a guess at what "a real id" might be - it is always derived from
    a value this session already knows really worked. `None` when the
    resolved path's shape doesn't actually match the template (should not
    happen given the caller's own gating, but never assumed).
    """
    params = _path_param_names(template_path)
    if len(params) != 1:
        return None
    token = _param_token(template_path, params[0])
    prefix, _, suffix = template_path.partition(token)
    if not resolved_path.startswith(prefix):
        return None
    remainder = resolved_path[len(prefix):]
    if suffix and remainder.endswith(suffix):
        value = remainder[: len(remainder) - len(suffix)]
    else:
        value = remainder
    if not value:
        return None
    nonexistent_value = "999999999" if value.isdigit() else value + "-does-not-exist"
    return prefix + nonexistent_value + suffix


def generate_and_execute_negative_cases(
    endpoints: Tuple[ApiEndpoint, ...], calls: Tuple[ApiCallResult, ...],
    base_url: str, timeout: float, schema_doc=None, on_progress=None,
    allow_synthetic_mutations: bool = DEFAULT_ALLOW_SYNTHETIC_MUTATIONS,
    static_schema_doc=None, auth_context=None,
) -> Tuple[NegativeCallResult, ...]:
    """One real, evidence-gated negative HTTP call per qualifying endpoint -
    `calls` must be `resolve_and_execute`'s own already-finished output for
    the same `endpoints`, in the same order (the exact contract
    `resolve_and_execute` itself already documents). Two case types only
    (docs/43's own explicit scope - no fuzzing):

    - a mutating endpoint whose positive call succeeded: the same
      schema-derived valid body, minus one required field.
    - a `:id`/`{id}` endpoint whose positive call succeeded: the same
      resolved path, with a guaranteed-nonexistent id substituted.

    `on_progress` (optional, docs/44-live-progress.md): called as
    `on_progress(done, total, label)`. `total` is the number of endpoints
    whose *positive* call already really passed (known upfront, zero extra
    I/O, from `calls` alone) - the real, full set of candidates this
    function will actually consider. `done` advances once per candidate
    finished being considered, whether or not it ultimately turned out to
    have a required field to remove (a real, small number of candidates can
    still end up producing no case at all) - `total` is never exceeded.

    `allow_synthetic_mutations` (docs/45, default `True`): passed straight
    through to `build_request_body` for the mutation-field-removal case.
    Every resulting `NegativeCallResult` also carries `synthetic=True`
    whenever the *positive* call it was derived from was itself synthetic
    (`positive_call.synthetic`) - a negative-case verdict is never
    presented with more confidence than the positive evidence it was built
    from.
    """
    call_by_id = {id(e): c for e, c in zip(endpoints, calls)}
    schema_state = {"doc": schema_doc, "fetched": schema_doc is not None}
    auth_context = auth_context if auth_context is not None else AuthContext()

    def _schema():
        # Phase 2 (docs/53): a live `/openapi.json` fetch is tried first
        # (more current evidence, unchanged from before); only when that
        # returns nothing does a real, already-checked-out static spec file
        # (`static_schema_doc`, if any) get used instead - never the other
        # way around.
        if not schema_state["fetched"]:
            schema_state["doc"] = fetch_openapi_schema(base_url, timeout) or static_schema_doc
            schema_state["fetched"] = True
        return schema_state["doc"]

    results = []

    mutation_endpoints = sorted(
        (e for e in endpoints if e.method in MUTATION_METHODS and e.method != "DELETE"
         and call_by_id[id(e)].status == CALL_PASS),
        key=lambda e: (e.path, e.method),
    )
    id_endpoints = sorted(
        (e for e in endpoints if e.dynamic and e.method in ("GET", "PUT", "DELETE")
         and call_by_id[id(e)].status == CALL_PASS and call_by_id[id(e)].resolved_path),
        key=lambda e: (e.path, e.method),
    )
    progress_state = {"done": 0}
    total = len(mutation_endpoints) + len(id_endpoints)

    def _report(endpoint):
        if on_progress is None:
            return
        progress_state["done"] += 1
        on_progress(progress_state["done"], total, "negative: {} {}".format(endpoint.method, endpoint.path))

    for endpoint in mutation_endpoints:
        positive_call = call_by_id[id(endpoint)]
        body, _, _, _ = build_request_body(
            _schema(), endpoint.method, endpoint.path,
            endpoint=endpoint, allow_synthetic_mutations=allow_synthetic_mutations,
        )
        if not body:
            _report(endpoint)
            continue  # no schema/source hints, or no required fields to remove one from
        field_to_remove = sorted(body.keys())[0]
        mutated_body = {k: v for k, v in body.items() if k != field_to_remove}
        case_name = "missing required field '{}'".format(field_to_remove)
        expected = "a 4xx rejection (required field '{}' is missing)".format(field_to_remove)
        raw = call_endpoint(
            base_url, endpoint, timeout=timeout,
            path_override=positive_call.resolved_path or None,
            body=mutated_body, resolution_evidence="negative case: {}".format(case_name),
            extra_headers=auth_context.headers(),
        )
        status, actual_summary = _judge_negative_call(raw)
        results.append(NegativeCallResult(
            endpoint=endpoint, case_name=case_name, expected=expected, status=status,
            actual_status_code=raw.status_code, actual_summary=actual_summary,
            severity=classify_negative_case_severity(status, raw.status_code) or "",
            raw_call=raw,
            synthetic=positive_call.synthetic, synthetic_fields=positive_call.synthetic_fields,
        ))
        _report(endpoint)

    for endpoint in id_endpoints:
        positive_call = call_by_id[id(endpoint)]
        nonexistent_path = _nonexistent_id_variant(endpoint.path, positive_call.resolved_path)
        if nonexistent_path is None:
            _report(endpoint)
            continue
        case_name = "nonexistent id"
        expected = "a 4xx rejection (this id should not exist)"
        raw = call_endpoint(
            base_url, endpoint, timeout=timeout, path_override=nonexistent_path,
            resolution_evidence="negative case: {}".format(case_name),
            extra_headers=auth_context.headers(),
        )
        status, actual_summary = _judge_negative_call(raw)
        results.append(NegativeCallResult(
            endpoint=endpoint, case_name=case_name, expected=expected, status=status,
            actual_status_code=raw.status_code, actual_summary=actual_summary,
            severity=classify_negative_case_severity(status, raw.status_code) or "",
            raw_call=raw,
            synthetic=positive_call.synthetic, synthetic_fields=positive_call.synthetic_fields,
        ))
        _report(endpoint)

    return tuple(results)


# --- response schema validation (docs/43) -----------------------------------

def _operation_response_schema(schema_doc: dict, method: str, path: str, status_code: int):
    """The real JSON response schema OpenAPI declares for `method path` at
    `status_code`, or `_MISSING` (never checked - no such operation/status
    is described at all) - distinct from a real `None` ("described, but
    declares no JSON body" - also never checked, but for a different,
    equally real reason). Falls back to a declared `default` response
    entry only when the exact status code isn't itself listed, the same
    real OpenAPI convention `build_request_body`'s own operation lookup
    does not need to consider (a request body has no per-status shape).
    """
    operation = schema_doc.get("paths", {}).get(path, {}).get(method.lower())
    if not isinstance(operation, dict):
        return _MISSING
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return _MISSING
    response_obj = responses.get(str(status_code)) or responses.get("default")
    if not isinstance(response_obj, dict):
        return _MISSING
    content = response_obj.get("content")
    if not isinstance(content, dict):
        return None  # a declared response with no body - nothing to check
    schema = content.get("application/json", {}).get("schema")
    if schema is None:
        return None
    resolved = _resolve_schema_ref(schema_doc, schema)
    return resolved if isinstance(resolved, dict) else _MISSING


_JSON_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _check_object_against_schema(schema: dict, obj: dict):
    """One object's real fields against one real, already-resolved response
    schema - presence of `required` fields, plus a light top-level type
    check per declared `properties` entry (no nested/`allOf` validation -
    the same explicit, documented scope boundary `_resolve_schema_ref`
    already has). Returns `(missing_fields, type_mismatches)`.
    """
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    missing = [name for name in required if name not in obj]
    mismatches = []
    for name, value in obj.items():
        prop = properties.get(name)
        if not isinstance(prop, dict):
            continue
        json_type = prop.get("type")
        check = _JSON_TYPE_CHECKS.get(json_type)
        if check is not None and not check(value):
            mismatches.append("'{}' should be {}, got {}".format(name, json_type, type(value).__name__))
    return missing, mismatches


def validate_response_schemas(
    endpoints: Tuple[ApiEndpoint, ...], calls: Tuple[ApiCallResult, ...],
    base_url: str, timeout: float, schema_doc=None, on_progress=None,
    static_schema_doc=None,
) -> Tuple[SchemaValidationResult, ...]:
    """Never re-calls any endpoint under test - only checks each real,
    already-passing GET call's already-captured `response_json` (from
    `resolve_and_execute`'s own earlier real call) against its own declared
    OpenAPI response schema. The one real network call this function can
    make is the same lightweight, cached `/openapi.json` lookup `resolve_
    and_execute`'s own mutation handling already makes when it needs one
    (`fetch_openapi_schema`) - fetched at most once, lazily, only if at
    least one passing GET call exists to actually check; pass an
    already-fetched `schema_doc` to skip it entirely. Never flips the
    originating `ApiCallResult.status` - this is additional information
    alongside an existing PASS, never a retroactive fail. `calls` must
    correspond to `endpoints`, same order, as `resolve_and_execute` itself
    already guarantees.

    `on_progress` (optional, docs/44-live-progress.md): called as
    `on_progress(done, total, label)` - `total` is the number of real,
    already-passing 2xx GET calls (known upfront, zero extra I/O).
    """
    schema_state = {"doc": schema_doc, "fetched": schema_doc is not None}

    def _schema():
        # Phase 2 (docs/53): a live `/openapi.json` fetch is tried first
        # (more current evidence, unchanged from before); only when that
        # returns nothing does a real, already-checked-out static spec file
        # (`static_schema_doc`, if any) get used instead - never the other
        # way around.
        if not schema_state["fetched"]:
            schema_state["doc"] = fetch_openapi_schema(base_url, timeout) or static_schema_doc
            schema_state["fetched"] = True
        return schema_state["doc"]

    candidates = [
        (endpoint, call) for endpoint, call in zip(endpoints, calls)
        if endpoint.method == "GET" and call.status == CALL_PASS
        and call.status_code is not None and 200 <= call.status_code < 300
    ]
    progress_state = {"done": 0}
    total = len(candidates)

    def _report(endpoint):
        if on_progress is None:
            return
        progress_state["done"] += 1
        on_progress(progress_state["done"], total, "schema: {} {}".format(endpoint.method, endpoint.path))

    results = []
    for endpoint, call in candidates:
        doc = _schema()
        schema = _operation_response_schema(
            doc, endpoint.method, endpoint.path, call.status_code,
        ) if doc is not None else _MISSING
        if schema is _MISSING or schema is None:
            results.append(SchemaValidationResult(
                endpoint=endpoint, status=CALL_SKIPPED,
                reason="no response schema is declared for {} {} -> {}".format(
                    endpoint.method, endpoint.path, call.status_code),
            ))
            _report(endpoint)
            continue

        targets = call.response_json if isinstance(call.response_json, list) else [call.response_json]
        missing, mismatches = [], []
        for item in targets:
            if not isinstance(item, dict):
                continue
            item_missing, item_mismatches = _check_object_against_schema(schema, item)
            missing.extend(m for m in item_missing if m not in missing)
            mismatches.extend(m for m in item_mismatches if m not in mismatches)

        status = CALL_FAIL if (missing or mismatches) else CALL_PASS
        results.append(SchemaValidationResult(
            endpoint=endpoint, status=status,
            missing_fields=tuple(missing), type_mismatches=tuple(mismatches),
            severity=classify_schema_validation_severity(status) or "",
            reason="" if status == CALL_PASS else "response does not match its declared schema",
        ))
        _report(endpoint)

    return tuple(results)
