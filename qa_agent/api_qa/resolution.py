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

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from .http_client import call_endpoint
from .models import CALL_PASS, CALL_SKIPPED, ApiCallResult, ApiEndpoint

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
    shapes: the collection endpoint for `/api/users/{user_id}` (or
    Express's own `/api/users/:user_id`) is its own path with the last
    dynamic segment (and everything after it) removed - `/api/users`.
    Returns `None` if that segment is not actually the last segment of
    `path` (a shape this strategy does not attempt to resolve - never a
    guess at what the "real" parent might be).
    """
    segments = path.strip("/").split("/")
    token = _param_token(path, param_name)
    if not segments or segments[-1] != token:
        return None
    parent = segments[:-1]
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


def resolve_path_parameter(dynamic_endpoint: ApiEndpoint, evidence: Tuple[_Evidence, ...]):
    """Returns `(concrete_path, evidence_text)` on success, or `(None,
    skip_reason)` when no real, trustworthy value could be found - never a
    guess. Only ever considers the ONE structurally-implied parent
    collection endpoint (see `_parent_collection_path`); only ever
    considers evidence from a real endpoint whose own path actually
    matches that parent, exactly - never any other already-passed
    response, however plausible-looking its own data might be.
    """
    params = _path_param_names(dynamic_endpoint.path)
    if len(params) != 1:
        return None, (
            "path parameter resolution only supports exactly one dynamic segment per endpoint "
            "(found {}) - not attempted".format(len(params))
        )
    param_name = params[0]
    parent_path = _parent_collection_path(dynamic_endpoint.path, param_name)
    if parent_path is None:
        return None, (
            "'{}' is not the final path segment of '{}' - this strategy only resolves a "
            "trailing dynamic segment against its own structural parent collection endpoint"
            .format(_param_token(dynamic_endpoint.path, param_name), dynamic_endpoint.path)
        )

    for fact in evidence:
        if fact.endpoint.path != parent_path:
            continue
        value = _extract_scalar_value(fact.response_json, param_name)
        if value is None:
            return None, (
                "found the real parent collection endpoint '{} {}', but its response contained no "
                "usable '{}' (or 'id') field - never guessed".format(
                    fact.endpoint.method, fact.endpoint.path, param_name)
            )
        concrete_path = dynamic_endpoint.path.replace(_param_token(dynamic_endpoint.path, param_name), str(value))
        evidence_text = "{}={!r} (from real response: {} {})".format(
            param_name, value, fact.endpoint.method, fact.endpoint.path)
        return concrete_path, evidence_text

    return None, (
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


def build_request_body(schema_doc, method: str, path: str):
    """Returns `(body_dict, evidence_text)` on success, or `(None,
    skip_reason)`. Conservative by design (docs/33's own explicit scope):
    a body is only ever constructed when every *required* property either
    needs no value at all (none are required) or each required property
    has its own real, schema-declared `default` - never a placeholder
    value invented to satisfy an unfulfillable required field.
    """
    if schema_doc is None:
        return None, "no OpenAPI schema is available to construct a request body"

    schema = _operation_request_schema(schema_doc, method, path)
    if schema is _MISSING:
        return None, "the OpenAPI schema does not describe {} {}".format(method, path)
    if schema is None:
        return {}, "OpenAPI schema declares no request body for {} {}".format(method, path)

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

    if missing:
        return None, (
            "no deterministic request body could be constructed for {} {}: required field(s) {} "
            "have no schema default".format(method, path, ", ".join(repr(m) for m in missing))
        )

    evidence = (
        "empty body (OpenAPI schema declares no required fields) for {} {}".format(method, path)
        if not body else
        "body {} (OpenAPI schema defaults for required field(s)) for {} {}".format(body, method, path)
    )
    return body, evidence


# --- execution ---------------------------------------------------------

def _is_dynamic_get(endpoint: ApiEndpoint) -> bool:
    return endpoint.method == "GET" and endpoint.dynamic


def _skip(endpoint: ApiEndpoint, reason: str) -> ApiCallResult:
    return ApiCallResult(endpoint=endpoint, status=CALL_SKIPPED, reason=reason)


def resolve_and_execute(endpoints: Tuple[ApiEndpoint, ...], base_url: str, timeout: float,
                         schema_doc=None) -> Tuple[ApiCallResult, ...]:
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
    """
    results = {}
    evidence: list = []
    schema_state = {"doc": schema_doc, "fetched": schema_doc is not None}

    def _schema():
        if not schema_state["fetched"]:
            schema_state["doc"] = fetch_openapi_schema(base_url, timeout)
            schema_state["fetched"] = True
        return schema_state["doc"]

    # Tier 1: every non-dynamic GET - real evidence gathered from each
    # real, passing response, deterministic order (sorted by path).
    tier1 = sorted(
        (e for e in endpoints if e.method == "GET" and not e.dynamic),
        key=lambda e: e.path,
    )
    for endpoint in tier1:
        result = call_endpoint(base_url, endpoint, timeout=timeout)
        results[id(endpoint)] = result
        if result.status == CALL_PASS and result.response_json is not None:
            evidence.append(_Evidence(endpoint=endpoint, response_json=result.response_json))
    evidence = tuple(evidence)

    # Tier 2: every dynamic GET - resolved from tier 1's own real evidence.
    tier2 = sorted(
        (e for e in endpoints if _is_dynamic_get(e)),
        key=lambda e: e.path,
    )
    for endpoint in tier2:
        concrete_path, note = resolve_path_parameter(endpoint, evidence)
        if concrete_path is None:
            results[id(endpoint)] = _skip(endpoint, note)
            continue
        results[id(endpoint)] = call_endpoint(
            base_url, endpoint, timeout=timeout, path_override=concrete_path, resolution_evidence=note,
        )

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
        if endpoint.dynamic:
            resolved_path, note = resolve_path_parameter(endpoint, evidence)
            if resolved_path is None:
                results[id(endpoint)] = _skip(endpoint, note)
                continue
            concrete_path, path_note = resolved_path, note

        body = None
        body_note = ""
        if endpoint.method in ("POST", "PUT", "PATCH"):
            body, body_note = build_request_body(_schema(), endpoint.method, endpoint.path)
            if body is None:
                results[id(endpoint)] = _skip(endpoint, body_note)
                continue

        combined_evidence = " ; ".join(part for part in (path_note, body_note) if part)
        results[id(endpoint)] = call_endpoint(
            base_url, endpoint, timeout=timeout,
            path_override=(concrete_path if endpoint.dynamic else None),
            body=body, resolution_evidence=combined_evidence,
        )

    return tuple(results[id(endpoint)] for endpoint in endpoints)
