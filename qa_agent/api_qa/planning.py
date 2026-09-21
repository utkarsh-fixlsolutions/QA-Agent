"""Functional API test planning + dependency-aware execution (Phase 3,
docs/54-functional-api-test-planning.md): `build_test_plan(endpoints)` ->
`Tuple[PlannedTest, ...]`, `execute_test_plan(plan, base_url, timeout, ...)`
-> `Tuple[PlannedTestResult, ...]`.

Turns "call every discovered endpoint independently" (`resolution.py`'s own
existing, still-unchanged verification pass) into a second, additive,
*workflow*-aware pass: when the real discovered API shape implies an obvious
CRUD relationship (a collection `GET`/`POST` alongside an item `{id}` `GET`/
`PUT`/`PATCH`/`DELETE`), a real create -> read -> update -> delete -> verify
chain is planned and executed in that order, with runtime data (a real id
from a real `POST` response) flowing into later requests - never invented.
An endpoint with no such relationship (a GET-only API, a standalone
`POST /login`, ...) still gets a plan entry; nothing discovered is ever
silently left out of the plan.

Deliberately small and deterministic (docs/54's own scope section): the plan
is built entirely in dependency order in the first place (a prerequisite
test is always appended before anything that depends on it), so execution is
a single, ordered walk - no separate topological sort, no general workflow
engine. All real HTTP mechanics (path-parameter resolution, request-body
construction, the one real HTTP call itself) are reused unmodified from
`resolution.py`/`http_client.py` - this module only decides which tests to
plan, in what order, and what "correct" means for each one's real outcome.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from .auth_context import AuthContext
from .http_client import call_endpoint
from .models import (
    CALL_FAIL,
    CALL_PASS,
    CALL_SKIPPED,
    EXPECTED_NOT_FOUND,
    EXPECTED_SUCCESS,
    TEST_CATEGORY_EXPECTED_NEGATIVE,
    TEST_CATEGORY_FUNCTIONAL_POSITIVE,
    TEST_CATEGORY_FUNCTIONAL_WORKFLOW,
    TEST_CATEGORY_SMOKE,
    ApiEndpoint,
    PlannedTest,
    PlannedTestResult,
)
from .resolution import (
    DEFAULT_ALLOW_SYNTHETIC_MUTATIONS,
    MUTATION_METHODS,
    _Evidence,
    _extract_scalar_value,
    _param_token,
    _parent_collection_path,
    _path_param_names,
    build_request_body,
    fetch_openapi_schema,
    resolve_path_parameter,
    synthesize_path_parameter_value,
)
from .server import _HEALTH_PATH_MARKERS

_UPDATE_METHODS = ("PUT", "PATCH")


def _is_health_shaped(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in _HEALTH_PATH_MARKERS)


def _is_simple_item_path(endpoint: ApiEndpoint, parent_path: str, param_name: str) -> bool:
    """`True` only for the common, directly-nested shape a CRUD workflow can
    be planned around (`/resource/{id}`) - the dynamic segment is the
    path's own last segment, immediately after `parent_path`. A more deeply
    nested dynamic endpoint (`/forms/{formId}/responses`) still gets its own
    standalone plan entry (see `build_test_plan`'s own fallback loop) - just
    never a create/update/delete/verify chain, a deliberate, named scope
    boundary (docs/54), not a silent gap.
    """
    token = _param_token(endpoint.path, param_name)
    expected = (parent_path.rstrip("/") + "/" + token) if parent_path != "/" else "/" + token
    return endpoint.path.rstrip("/") == expected.rstrip("/")


class _IdAllocator:
    def __init__(self):
        self._next = 1

    def next_id(self) -> str:
        test_id = "T{}".format(self._next)
        self._next += 1
        return test_id


def build_test_plan(endpoints: Tuple[ApiEndpoint, ...]) -> Tuple[PlannedTest, ...]:
    """A real, structured functional test plan built entirely from
    `endpoints` - deterministic, no I/O, no AI. Every discovered endpoint
    gets at least one `PlannedTest`; a CRUD-shaped resource (a collection
    `GET`/`POST` plus a simple `/resource/{id}` item endpoint) additionally
    gets a real create -> read -> update -> delete -> verify chain, exactly
    as far as the endpoints actually discovered support - never a fabricated
    step for a method the target does not actually expose (docs/54, section
    8's own "the planner must adapt to what is actually discovered" rule).
    """
    allocator = _IdAllocator()
    plan: List[PlannedTest] = []
    used: set = set()

    non_dynamic_by_path: Dict[str, Dict[str, ApiEndpoint]] = {}
    for e in endpoints:
        if not e.dynamic:
            non_dynamic_by_path.setdefault(e.path, {})[e.method] = e

    resource_groups: Dict[str, List[ApiEndpoint]] = {}
    for e in endpoints:
        if not e.dynamic:
            continue
        params = _path_param_names(e.path)
        if len(params) != 1:
            continue
        parent = _parent_collection_path(e.path, params[0])
        if parent is None or not _is_simple_item_path(e, parent, params[0]):
            continue
        resource_groups.setdefault(parent, []).append(e)

    for parent in sorted(resource_groups):
        item_by_method = {e.method: e for e in resource_groups[parent]}
        collection_by_method = non_dynamic_by_path.get(parent, {})

        list_test_id: Optional[str] = None
        if "GET" in collection_by_method:
            e = collection_by_method["GET"]
            test_id = allocator.next_id()
            category = TEST_CATEGORY_SMOKE if _is_health_shaped(e.path) else TEST_CATEGORY_FUNCTIONAL_POSITIVE
            plan.append(PlannedTest(
                test_id=test_id, endpoint=e, category=category,
                purpose="discover existing resource id(s) for {}".format(parent),
                depends_on=(), expected_status_kind=EXPECTED_SUCCESS,
                request_source="none (collection listing)",
            ))
            list_test_id, used = test_id, used | {id(e)}

        if "GET" in item_by_method:
            e = item_by_method["GET"]
            test_id = allocator.next_id()
            if list_test_id:
                purpose = "verify retrieval of an existing resource using a real id from {}".format(list_test_id)
                request_source = "real id from {}'s own response".format(list_test_id)
            else:
                purpose = "verify retrieval of a resource using a synthetic id (no list endpoint was discovered)"
                request_source = "synthetic (no real evidence available)"
            plan.append(PlannedTest(
                test_id=test_id, endpoint=e, category=TEST_CATEGORY_FUNCTIONAL_POSITIVE,
                purpose=purpose, depends_on=(list_test_id,) if list_test_id else (),
                expected_status_kind=EXPECTED_SUCCESS, request_source=request_source,
            ))
            used = used | {id(e)}

        create_test_id: Optional[str] = None
        if "POST" in collection_by_method:
            e = collection_by_method["POST"]
            test_id = allocator.next_id()
            has_workflow = any(m in item_by_method for m in _UPDATE_METHODS) or "DELETE" in item_by_method
            plan.append(PlannedTest(
                test_id=test_id, endpoint=e,
                category=TEST_CATEGORY_FUNCTIONAL_WORKFLOW if has_workflow else TEST_CATEGORY_FUNCTIONAL_POSITIVE,
                purpose="verify resource creation", depends_on=(),
                expected_status_kind=EXPECTED_SUCCESS, request_source="contract evidence",
            ))
            create_test_id, used = test_id, used | {id(e)}

        update_test_id: Optional[str] = None
        if create_test_id:
            for method in _UPDATE_METHODS:
                if method not in item_by_method:
                    continue
                e = item_by_method[method]
                test_id = allocator.next_id()
                plan.append(PlannedTest(
                    test_id=test_id, endpoint=e, category=TEST_CATEGORY_FUNCTIONAL_WORKFLOW,
                    purpose="verify resource update using the id created by {}".format(create_test_id),
                    depends_on=(create_test_id,), expected_status_kind=EXPECTED_SUCCESS,
                    request_source="runtime id from {}'s own response".format(create_test_id),
                    id_source_test_id=create_test_id,
                ))
                update_test_id, used = test_id, used | {id(e)}
                break  # one update step is enough for this milestone (docs/54 scope)

        delete_test_id: Optional[str] = None
        delete_source = update_test_id or create_test_id
        if delete_source and "DELETE" in item_by_method:
            e = item_by_method["DELETE"]
            test_id = allocator.next_id()
            plan.append(PlannedTest(
                test_id=test_id, endpoint=e, category=TEST_CATEGORY_FUNCTIONAL_WORKFLOW,
                purpose="verify resource deletion using the id created by {}".format(create_test_id),
                depends_on=(delete_source,), expected_status_kind=EXPECTED_SUCCESS,
                request_source="runtime id from {}'s own response".format(delete_source),
                id_source_test_id=delete_source,
            ))
            delete_test_id, used = test_id, used | {id(e)}

        if delete_test_id and "GET" in item_by_method:
            e = item_by_method["GET"]
            test_id = allocator.next_id()
            plan.append(PlannedTest(
                test_id=test_id, endpoint=e, category=TEST_CATEGORY_EXPECTED_NEGATIVE,
                purpose="verify the resource no longer exists after deletion",
                depends_on=(delete_test_id,), expected_status_kind=EXPECTED_NOT_FOUND,
                request_source="runtime id from {}".format(delete_test_id),
                id_source_test_id=delete_test_id,
            ))
            used = used | {id(e)}

    # Everything not already covered by a resource-group workflow above -
    # standalone GETs (including a bare `/health`), a lone POST with no
    # sibling item endpoint, a dynamic endpoint too complex for a workflow
    # (see `_is_simple_item_path`) - still gets exactly one plan entry, so
    # nothing discovered is ever silently absent from the plan (docs/54,
    # section 8).
    for e in sorted(endpoints, key=lambda ep: (ep.path, ep.method)):
        if id(e) in used:
            continue
        test_id = allocator.next_id()
        category = TEST_CATEGORY_SMOKE if _is_health_shaped(e.path) else TEST_CATEGORY_FUNCTIONAL_POSITIVE
        if e.dynamic:
            purpose = "verify {} {} using a synthetic id (no related list/create endpoint was discovered)".format(
                e.method, e.path)
            request_source = "synthetic (no real evidence available)"
        elif e.method in MUTATION_METHODS:
            purpose = "verify {} {} responds correctly".format(e.method, e.path)
            request_source = "contract evidence"
        else:
            purpose = "verify {} {} responds correctly".format(e.method, e.path)
            request_source = "none"
        plan.append(PlannedTest(
            test_id=test_id, endpoint=e, category=category, purpose=purpose,
            depends_on=(), expected_status_kind=EXPECTED_SUCCESS, request_source=request_source,
        ))
        used.add(id(e))

    return tuple(plan)


def _resolve_id_from_source(results_by_id: Dict[str, PlannedTestResult], source_test_id: str, param_name: str):
    """Returns `(value, note)` or `(None, None)`. Two real sources, checked
    in this order: (1) the source test already resolved its *own* dynamic
    parameter to a real value (docs/54's "reuse the same id" chain - e.g. a
    "verify deletion" test reusing exactly the id its own "delete"
    prerequisite already used); (2) the source test's own real, single-
    object JSON response (a real `POST`/`PUT`/`PATCH` create/update reply) -
    extracted via `resolution._extract_scalar_value`, reused unmodified by
    wrapping the one object in a one-element list (the exact shape that
    function already expects).
    """
    source = results_by_id.get(source_test_id)
    if source is None:
        return None, None
    if source.resolved_param_value is not None:
        return source.resolved_param_value, "reused the id resolved by {}".format(source_test_id)
    call = source.call
    if call is None or not isinstance(call.response_json, dict):
        return None, None
    value = _extract_scalar_value([call.response_json], param_name)
    if value is None:
        return None, None
    return value, "{}={!r} (from the real response {} produced)".format(param_name, value, source_test_id)


def _judge_test_outcome(test: PlannedTest, call) -> Tuple[str, str]:
    """This *test's* own verdict - independent of `call.status` for an
    `EXPECTED_NOT_FOUND` test (docs/54, section 9): the target's own real
    status code is compared against what this specific test actually
    expects, never against a blanket "2xx is the only success" rule. Never
    invented, never AI-decided - a closed, two-branch, code-only rule.
    """
    if test.expected_status_kind == EXPECTED_NOT_FOUND:
        code = call.status_code
        if code == 404:
            return CALL_PASS, "resource correctly no longer found (404) after deletion, as expected"
        if code is not None:
            return CALL_FAIL, "expected 404 after deletion, got {} instead".format(code)
        return CALL_FAIL, call.error or "expected 404 after deletion, but no response was received"
    if call.status == CALL_PASS:
        return CALL_PASS, "HTTP {}".format(call.status_code)
    return CALL_FAIL, call.error or call.reason or "the request did not succeed"


def execute_test_plan(
    plan: Tuple[PlannedTest, ...], base_url: str, timeout: float,
    schema_doc=None, static_schema_doc=None,
    allow_synthetic_mutations: bool = DEFAULT_ALLOW_SYNTHETIC_MUTATIONS,
    on_progress=None, auth_context=None,
) -> Tuple[PlannedTestResult, ...]:
    """Executes `plan` in the exact order it was built - already dependency-
    correct (`build_test_plan` only ever appends a test after every test it
    depends on), so no separate ordering step is needed. Every real HTTP
    call is made through `http_client.call_endpoint` (the one, unchanged
    HTTP mechanism this whole package already uses); every request body
    through `resolution.build_request_body` (the same, unchanged Phase
    2 evidence hierarchy) - this function only ever decides *which* real id
    a dynamic test should use and *what a correct outcome looks like* for
    that specific test.
    """
    results_by_id: Dict[str, PlannedTestResult] = {}
    list_evidence: List[_Evidence] = []
    schema_state = {"doc": schema_doc, "fetched": schema_doc is not None}
    total = len(plan)
    progress_state = {"done": 0}
    auth_context = auth_context if auth_context is not None else AuthContext()

    def _schema():
        if not schema_state["fetched"]:
            schema_state["doc"] = fetch_openapi_schema(base_url, timeout) or static_schema_doc
            schema_state["fetched"] = True
        return schema_state["doc"]

    def _report(test):
        if on_progress is None:
            return
        progress_state["done"] += 1
        on_progress(progress_state["done"], total, "{}: {} {}".format(test.test_id, test.endpoint.method, test.endpoint.path))

    def _finish(test, call, resolved_param_value, status, reason):
        results_by_id[test.test_id] = PlannedTestResult(
            test_id=test.test_id, test=test, call=call,
            resolved_param_value=resolved_param_value, status=status, verdict_reason=reason,
        )
        _report(test)

    for test in plan:
        endpoint = test.endpoint
        concrete_path = endpoint.path
        path_note = ""
        resolved_param_value: Optional[str] = None
        path_synthetic_field: Optional[str] = None

        if endpoint.dynamic:
            params = _path_param_names(endpoint.path)
            if len(params) != 1:
                _finish(test, None, None, CALL_SKIPPED,
                        "path parameter resolution only supports exactly one dynamic segment per endpoint")
                continue
            param_name = params[0]
            if test.id_source_test_id is not None:
                value, note = _resolve_id_from_source(results_by_id, test.id_source_test_id, param_name)
                if value is None:
                    if not allow_synthetic_mutations:
                        _finish(test, None, None, CALL_SKIPPED, (
                            "no runtime id was available from {} and synthetic fallback is disabled"
                        ).format(test.id_source_test_id))
                        continue
                    value = synthesize_path_parameter_value(param_name)
                    note = "{}={!r} (synthetic id used: {} produced no usable value)".format(
                        param_name, value, test.id_source_test_id)
                    path_synthetic_field = param_name
                resolved_param_value = str(value)
                concrete_path = endpoint.path.replace(_param_token(endpoint.path, param_name), str(value))
                path_note = note
            else:
                resolved, note, synthetic_field = resolve_path_parameter(
                    endpoint, tuple(list_evidence), allow_synthetic_mutations,
                )
                if resolved is None:
                    _finish(test, None, None, CALL_SKIPPED, note)
                    continue
                concrete_path, path_note, path_synthetic_field = resolved, note, synthetic_field

        body, body_note, body_synthetic_fields, body_evidence_source = None, "", (), ""
        if endpoint.method in ("POST", "PUT", "PATCH"):
            body, body_note, body_synthetic_fields, body_evidence_source = build_request_body(
                _schema(), endpoint.method, endpoint.path,
                endpoint=endpoint, allow_synthetic_mutations=allow_synthetic_mutations,
            )
            if body is None:
                _finish(test, None, resolved_param_value, CALL_SKIPPED, body_note)
                continue

        combined_evidence = " ; ".join(part for part in (path_note, body_note) if part)
        request_headers = auth_context.headers()
        raw_call = call_endpoint(
            base_url, endpoint, timeout=timeout,
            path_override=(concrete_path if endpoint.dynamic else None),
            body=body, resolution_evidence=combined_evidence, extra_headers=request_headers,
        )
        auth_context.observe(endpoint, raw_call)
        if request_headers:
            raw_call = replace(raw_call, auth_evidence=auth_context.evidence_for_attached_call())
        synthetic_fields = tuple(
            ([path_synthetic_field] if path_synthetic_field is not None else []) + list(body_synthetic_fields)
        )
        call = raw_call
        if synthetic_fields:
            call = replace(call, synthetic=True, synthetic_fields=synthetic_fields)
        if body_evidence_source:
            call = replace(call, body_evidence_source=body_evidence_source)

        status, reason = _judge_test_outcome(test, call)
        _finish(test, call, resolved_param_value, status, reason)

        if endpoint.method == "GET" and not endpoint.dynamic and call.status == CALL_PASS and call.response_json is not None:
            list_evidence.append(_Evidence(endpoint=endpoint, response_json=call.response_json))

    return tuple(results_by_id[test.test_id] for test in plan)
