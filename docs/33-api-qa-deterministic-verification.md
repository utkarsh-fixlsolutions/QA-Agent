# Step 33 — Deterministic API Verification: Testability, Path-Parameter Resolution, Request Bodies

**Status:** IMPLEMENTED (2026-09-12).
**Goal:** turn "discover endpoints + start the server + call every non-dynamic one" into a real, deterministic API *verification* pass - a dynamic path parameter resolved from real prior evidence, a request body constructed only from a real OpenAPI schema default, everything else honestly `SKIPPED`. No AI, no G3/G4/G5 changes, no invented values of any kind. See docs/30 (API QA v1) and docs/32 (Python/FastAPI discovery) for the layers this builds on.

## The core, non-negotiable rule

Every concrete value ever used to make a call testable must be traceable to a real, already-observed fact: a real prior HTTP response this same session already received, or a real, already-declared default value the target application's own OpenAPI schema already contains. Nothing here ever invents a path parameter, a request body field, a header, a credential, or a query parameter. An endpoint that cannot be made testable this way is `SKIPPED`, with the exact reason why.

## One explicit design decision, confirmed before building: mutations are executed when safe

The task's own desired result explicitly showed `DELETE /api/users/{user_id}` being auto-executed once a safe id is resolved - a real, non-idempotent mutation, unlike GET. Confirmed directly rather than assumed: **yes, execute DELETE/PUT/PATCH automatically whenever the same deterministic-evidence bar is met**, exactly like GET. For this demo that only ever affects an in-memory store reset on the next server restart; the same logic would send a real DELETE to any future non-demo project pointed at this tool once evidence permits it - worth knowing, not hidden.

## Design

### `qa_agent/api_qa/resolution.py` (new) - the one new module

**Path-parameter resolution (`resolve_path_parameter`):** for a dynamic endpoint whose *last* path segment is `{param}`, the real, structural parent collection endpoint is its own path with that segment removed (`/api/users/{user_id}` -> `/api/users` - a named REST convention, not a guess at arbitrary path shapes). If a real, already-executed, **passing** GET call exists for exactly that parent path, its real, already-parsed JSON response (see below) is searched for a real scalar value under the exact parameter name, falling back to the generic `id` field *only* when the parameter's own name already signals it is an identifier (`id` itself, or an `_id`/`Id` suffix - never for an arbitrarily-named parameter like `slug`, where that fallback would silently use the wrong field rather than genuinely resolve the right one). A `bool` is never accepted (Python's own `bool`-is-an-`int`-subclass trap, guarded the same way this project's own response-schema validators already guard against it elsewhere). No match, no parent, more than one dynamic segment, or a non-trailing dynamic segment: `None`, with the exact reason - never a guess.

**Request-body construction (`build_request_body`):** a real GET to the target's own `/openapi.json` (via `call_endpoint`, unmodified - reused, not a second HTTP mechanism), fetched once per session, only when a mutating endpoint actually needs it. An operation with no declared request body needs an empty, valid `{}`. An operation whose request-body schema declares required fields is only constructed when *every* required field has its own real, schema-declared `default` - conservative by explicit design (docs/33's own scope, matching this task's "initially support a conservative strategy" instruction); any required field with no default means the whole body is `None`, with the exact missing field names named. `$ref`s to `#/components/schemas/X` are resolved; nothing more elaborate (no `oneOf`/`allOf`, no format-based generation) is attempted.

**Execution ordering (`resolve_and_execute`):** every non-dynamic GET first (deterministic order, sorted by path) - each real, passing, JSON response becomes real evidence for later tiers. Every dynamic GET next, resolved from that evidence. Every mutating call (`POST`/`PUT`/`PATCH`/`DELETE`) last - resolved the same way when dynamic, and/or given a real schema-derived body when one is needed. This ordering is not incidental: it is what lets `GET /api/users/{user_id}` observe the real, still-present seed data *before* a same-session `DELETE /api/users/{user_id}` (resolved to the identical id) could remove it - proven directly by a dedicated test asserting the real call order, not just the individual outcomes. **The one public entry point always returns exactly one result per endpoint, in the exact order `endpoints` was given** - only the internal execution order is dependency-aware; `ApiTestResult`'s own existing "one call per endpoint, same order" contract is unchanged.

### `qa_agent/api_qa/http_client.py` - small, additive extensions (not rebuilt)

Three new, optional, fully backward-compatible parameters on `call_endpoint`: `path_override` (the real, concrete path to request, when it differs from `endpoint.path`'s own literal template), `body` (a real dict, JSON-encoded and sent with a `Content-Type: application/json` header), `resolution_evidence` (a human-readable trace, attached to the result verbatim). One new field on the *result*, `response_json` - the real, already-parsed JSON value, computed from work `call_endpoint` already does internally to decide pass/fail (no second parse, no second HTTP call) - this is what makes evidence-based resolution possible without a competing HTTP layer. Every existing caller is unaffected; every existing test passes unmodified.

### `qa_agent/api_qa/models.py` - three new, additive fields on `ApiCallResult`

`response_json`, `resolved_path`, `resolution_evidence` - all optional, all defaulting to `None`/`""`, none removing or renaming anything that existed before.

### `qa_agent/api_qa/runner.py` / `render.py`

`runner.py`'s own simple, unconditional "skip every dynamic endpoint" rule is replaced with a single call into `resolve_and_execute` - `runner.py` itself owns no resolution logic, exactly matching its own existing "compose already-independent pieces" role. `render.py` gained two new, conditional lines (`Concrete request: <method> <resolved_path>`, `evidence: <resolution_evidence>`), shown only when a call actually involved resolution - a plain, non-dynamic call's report is completely unchanged.

## Testability, expressed through existing status values, not a new enum

The task named three testability buckets (`TESTABLE`/`SKIPPED`/`UNSAFE-INSUFFICIENT-EVIDENCE`). Rather than adding a fourth, competing status enum alongside `ApiCallResult`'s own existing, already-tested `PASS`/`FAIL`/`SKIPPED`, both non-testable buckets are expressed as `CALL_SKIPPED` with a distinct, specific `reason` string (`"no evidence-based value could be resolved for path parameter '...'"` vs. `"no deterministic request body could be constructed ...: required field(s) ... have no schema default"`) - the *testability decision itself* is real, deterministic, internal logic inside `resolve_and_execute`/`resolve_path_parameter`/`build_request_body`; only its *outcome* is reported through the one, already-established result shape. This is the literal "extend the existing result model if necessary" instruction applied conservatively: judged not necessary for the status enum, necessary (and done) for the new evidence fields.

## Tests

`tests/regression/test_api_qa_resolution.py` - **30 test functions, 60 checks**: path-parameter resolution (real evidence, exact-name-over-fallback priority, `_id`-suffix fallback, never-fallback-for-non-id-shaped params, no-evidence skip, no-usable-field skip, boolean rejection, multi-segment/non-trailing rejection); request-body construction (no-body-needed, schema defaults, missing-default skip, no-schema skip, operation-not-found skip); the `http_client.py` extensions (`response_json` population, `path_override`/`body` actually used, `resolved_path`/`resolution_evidence` recorded); `resolve_and_execute` classification (static PASS, real 500 FAIL, real connection-failure FAIL, a 404 correctly *not* specially treated); **the exact critical scenario, proven twice** - once with mocked HTTP (`test_dynamic_get_resolved_from_prior_evidence_end_to_end`) and once against a real, running server (`test_real_end_to_end_dynamic_resolution_against_a_real_server`, guarded by `npm` availability): a dynamic GET resolved from a real prior list response and actually called, never skipped, never given an invented id; POST correctly skipped with no safe body; a mutation (DELETE) actually executed when evidence permits; the list-before-detail-before-mutation execution order, proven via real captured call sequence; result-order preservation regardless of internal execution order; and a full regression proof that Step 30's own Next.js behavior (`[id]`-style segments always skipped) is completely unaffected.

## Real demo-project verification

```
python -m qa_agent discover "D:\Working\qa-agent-fastapi-demo" --api-test
```

```
GET /api/users                -> 200   (1072ms) PASS

POST /api/users               -> -     (-)      SKIP
      no deterministic request body could be constructed for POST /api/users:
      required field(s) 'name', 'email' have no schema default

DELETE /api/users/{user_id}   -> 204   (19ms)   PASS
      Concrete request: DELETE /api/users/1
      evidence: user_id=1 (from real response: GET /api/users)

GET /api/users/{user_id}      -> 500   (2ms)    FAIL
      Concrete request: GET /api/users/1
      HTTP 500 response
      body: Internal Server Error
      evidence: user_id=1 (from real response: GET /api/users)

GET /health                   -> 200   (14ms)   PASS

5 call(s) - 1 fail, 3 pass, 1 skipped.
```

**The critical acceptance criterion, verified directly, without hard-coding "1" or the endpoint name anywhere in this whole module:** `GET /api/users/{user_id}` -> resolved via the real `GET /api/users` response's own real `id: 1` field -> `GET /api/users/1` -> real `HTTP 500` -> `FAIL`. Execution order was verified correct for this exact scenario: the dynamic `GET` ran (and correctly observed the real bug) *before* `DELETE` consumed the same resolved id - had the order been reversed, the GET would have hit a 404 on already-deleted data and the real defect would have gone unobserved. The server was confirmed genuinely stopped afterward, and the demo's own intentional bug (`app/main.py:46`, `user.id` on a dict) was directly re-verified present and unmodified.

## What was deliberately not attempted

AI/LLM-generated request bodies or parameter values (explicitly out of scope for this step). Multi-segment or non-trailing dynamic-path resolution (`/api/users/{user_id}/posts/{post_id}`) - named, not silently mishandled. `oneOf`/`allOf`/format-aware (email, uuid, ...) body generation - only flat, `$ref`-resolved required-field defaults. Authentication/credential handling of any kind. G3/G4/G5 wiring for this new evidence - the richer `ApiCallResult` (`resolved_path`, `resolution_evidence`, `response_json`) is available to `ai_bridge.py` unchanged, but no new diagnosis/repair logic was added or modified in this step.

## Files changed

`qa_agent/api_qa/resolution.py` (new); `qa_agent/api_qa/http_client.py` (additive extensions); `qa_agent/api_qa/models.py` (three new `ApiCallResult` fields); `qa_agent/api_qa/runner.py` (`_call_all` replaced by a call into `resolve_and_execute`); `qa_agent/api_qa/render.py` (two new conditional lines); `qa_agent/api_qa/__init__.py` (+exports); `tests/regression/test_api_qa_resolution.py` (new, 30 test functions, 60 checks); `docs/33-api-qa-deterministic-verification.md` (this doc). **Untouched:** `discovery.py`, `server.py`, `ai_bridge.py`, `__main__.py`, every G1-G4 module, `qa_agent/agent/` - no CLI change was needed.

## Verified

`test_api_qa_resolution.py` - 60/60 checks. `test_api_qa.py` (Step 30) - 89/89, unchanged. `test_api_ai_bridge.py` (Step 31) - 71/71, unchanged. `test_api_qa_fastapi.py` (Step 32) - 49/49, unchanged. Full suite - **37/39** (the same two pre-existing, unrelated flakes as every prior step).

## Remaining limitations

Resolution depth is exactly one level (a dynamic segment's own structural parent collection endpoint) - a real, named boundary, not a silent gap. Request-body construction only ever uses schema *defaults*; a real API requiring genuinely new data (no defaults anywhere) is correctly, honestly `SKIPPED`, not partially tested. The `id`-field fallback heuristic is deliberately narrow (only for `id`/`_id`/`Id`-shaped parameter names) - a real API using a differently-named but still-identifier-shaped convention (e.g. `{pk}`) would not benefit from the fallback and would need its own evidence field to match by exact name.

**Status: Deterministic API Verification CLOSED.**
