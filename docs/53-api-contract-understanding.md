# Step 53 — API Contract Understanding (Phase 2)

**Status:** IMPLEMENTED (2026-09-17).
**Goal:** make the agent able to answer "what do I know about this endpoint, what evidence tells me that, and how should I construct a request from that evidence?" as a real, structured, explicit part of the existing API QA lifecycle — not a separate pipeline.

## Audit finding

Before writing any code, the existing `qa_agent/api_qa/` implementation was read in full (`discovery.py`, `models.py`, `resolution.py`, `runner.py`, `synthesis.py`, `zod_schema.py`, `http_client.py`, `render.py`). Confirmed already built, reused unchanged:

- Live OpenAPI extraction (`fetch_openapi_schema`, `_operation_request_schema`, one-level `$ref` resolution), required-field/default extraction, response-schema validation.
- Zod schema discovery and per-route attachment (`zod_schema.py`, docs/50).
- Source-derived body-field hints (`discovery.py`'s `body_field_hints`/`reads_request_body`).
- Probe-based body synthesis (docs/49) as a last resort.
- Dynamic path-parameter resolution from a real prior GET response (`resolve_path_parameter`).
- Rich existing evidence trail on every call (`ApiCallResult.resolved_path`/`resolution_evidence`/`synthetic`/`synthetic_fields`).
- An evidence fallback *order* already close to Phase 2's own target (OpenAPI → Zod → source hints → probe → synthetic) — most of the priority hierarchy already existed; it was never labeled as a structured, checkable fact, and three real gaps existed in it (see below).

Real, confirmed gaps this phase closes:
1. No static `openapi.json`/`swagger.json` file discovery — only a live `/openapi.json` fetch.
2. No "existing test/example evidence" tier at all.
3. No explicit, structured provenance label — only a free-text `resolution_evidence` string.
4. Dynamic path-parameter resolution required the dynamic segment to be the *trailing* one — `/forms/{formId}/responses`-shaped nested resources were always refused.

## Design

**`qa_agent/api_qa/models.py`**: `EVIDENCE_OPENAPI`/`EVIDENCE_SCHEMA`/`EVIDENCE_TEST_EXAMPLE`/`EVIDENCE_SOURCE_HINT`/`EVIDENCE_SYNTHETIC`/`EVIDENCE_UNKNOWN` (`BODY_EVIDENCE_SOURCES`) — a closed, structured provenance vocabulary. `ApiEndpoint.test_evidence_fields` (additive) and `ApiCallResult.body_evidence_source` (additive).

**`qa_agent/api_qa/test_evidence.py`** (new): the same "narrow, named convention, not a full parser" discipline as every other discovery strategy. Recognizes Supertest (`.send({...})`), axios, `fetch`+`JSON.stringify`, and Python `client.post(..., json={...})`-shaped calls in real test files (`.test.js`, `.spec.ts`, `test_*.py`, ...), plus real, structured JSON parsing (never pattern-matched) of Postman `*.postman_collection.json` files. `build_test_evidence_registry(root)` builds one whole-project registry (the same shape `zod_schema.py`'s own registry already established); `find_test_evidence_for_endpoint` matches a discovered endpoint against it with dynamic segments treated as wildcards. Wired into `discovery.py`'s combined entry point as one additive post-merge pass, exactly mirroring how the Zod registry is already attached.

**`qa_agent/api_qa/resolution.py`**: `find_static_openapi_schema(root)` (new) — a bounded, named set of real file locations (`openapi.json`, `swagger.json`, and the same under `spec/`/`specs/`/`docs/`/`api/`), JSON only (no YAML dependency exists in this project today — a named, honest limitation, not silently attempted). Wired as a fallback in `resolve_and_execute`/`generate_and_execute_negative_cases`/`validate_response_schemas`'s own existing lazy `_schema()` closures: the live fetch is always tried first (more current evidence); the static file is only ever used when that returns nothing. `build_request_body`/`_fallback_from_source_hints` now return a 4th value, `evidence_source`, and `_fallback_from_source_hints` gained a new tier — `endpoint.test_evidence_fields`, checked after Zod (stronger: a real type) and before `body_field_hints` (weaker: a bare name) — matching Phase 2's own explicit priority order. When nothing at all is found, the skip reason now literally says `CONTRACT UNKNOWN` and `evidence_source=EVIDENCE_UNKNOWN`, rather than a plain "no schema" message that could be mistaken for a real absence-of-body fact.

`_parent_collection_path` (path-parameter resolution) is generalized from "the dynamic segment must be the last one" to "the dynamic segment may appear anywhere" — the actual substitution already worked for any position (a plain string `.replace()`); only the *evidence-gathering* boundary needed relaxing. `/forms/{formId}/responses` now resolves `formId` from a real `GET /forms` response, preserving the literal `/responses` suffix.

**`qa_agent/api_qa/runner.py`**: `find_static_openapi_schema(root)` called once, passed as `static_schema_doc=` to all three resolution.py entry points.

**`qa_agent/api_qa/render.py`**: `body_evidence_source` surfaced in the CLI (`[source: X]` next to the existing evidence line), `to_dict`/CSV (`body_evidence_source` column), and HTML (`[Evidence: X]` in the detail cell) — the same places `synthetic`/`synthetic_fields` were already surfaced, extended consistently.

## Evidence hierarchy (as implemented)

1. `EVIDENCE_OPENAPI` — a live or static OpenAPI/Swagger schema.
2. `EVIDENCE_SCHEMA` — an explicit validation schema (Zod).
3. `EVIDENCE_TEST_EXAMPLE` — a real example found in the project's own tests/Postman collections.
4. `EVIDENCE_SOURCE_HINT` — route/controller source evidence (a field name, or a bare "reads a body" fact).
5. *Model/service evidence* — **not implemented**, named and deferred (see below).
6. `EVIDENCE_SYNTHETIC` — no real evidence; an invented placeholder (probe-based synthesis also lands here).
`EVIDENCE_UNKNOWN` is a distinct, honest sixth state: no body could be constructed at all.

## Files changed

`qa_agent/api_qa/test_evidence.py` (new), `qa_agent/api_qa/models.py`, `qa_agent/api_qa/discovery.py`, `qa_agent/api_qa/resolution.py`, `qa_agent/api_qa/runner.py`, `qa_agent/api_qa/render.py`, `qa_agent/api_qa/__init__.py`, `tests/regression/test_api_qa_contract_evidence.py` (new, 49 checks), `tests/regression/test_api_qa_resolution.py` (+2 net checks: one test intentionally rewritten - see below), `tests/regression/test_api_qa_synthetic_mutations.py`/`test_api_qa_zod_schema.py` (call-site updates only, for `build_request_body`'s new 4-tuple return).

**One existing test intentionally changed, not weakened:** `test_resolve_path_parameter_rejects_non_trailing_param` asserted the *old* limitation (a non-trailing dynamic segment must always be refused) as correct behavior. Phase 2 deliberately lifts that limitation (the whole point of the nested-route requirement) - renamed to `test_resolve_path_parameter_resolves_a_non_trailing_param` and rewritten to assert the new, intentional, correct outcome (`/api/users/{user_id}/profile` + real evidence for `/api/users` → `/api/users/1/profile`), same as this project's own documented convention for a deliberate safety/capability correction.

## Verified

`test_api_qa_contract_evidence.py` (new, 49/49): test-evidence extraction (Supertest/axios/fetch/Python/Postman), path-segment wildcard matching, evidence-source tagging for every tier (A/B/C), evidence precedence (D: schema > test-example > source-hint), `CONTRACT UNKNOWN` (F), nested dynamic-route resolution (G), static OpenAPI file discovery, **one real end-to-end proof** (H): a real Node server that only accepts `POST /api/users` when real evidence-derived fields are present — the blind `{"qa-agent-test": true}` fallback would fail it, and the real, test-evidence-constructed request passes. Also confirms Phase 1's readiness gate is untouched (I).

All previously-passing `test_api_qa*.py` suites remain green (`test_api_qa.py` 191/191 unaffected, `test_api_qa_fastapi.py` 66/66, `test_api_qa_negative_and_schema.py` 40/40, `test_api_qa_probe_synthesis.py` 33/33, `test_api_qa_progress.py` 16/16, `test_api_qa_resolution.py` 72/72 [+2, one test rewritten as above], `test_api_qa_synthetic_mutations.py` 81/81, `test_api_qa_zod_schema.py` 35/35).

**Real, live end-to-end** (`discover --api-test`, never started manually) against a real Express-shaped demo (7 endpoints: `/health`, `/api/users`, `/api/users/:id`, `/api/users/:id/posts` [nested dynamic], `POST /api/users` [static `openapi.json` evidence], `POST /api/comments` [test-evidence only, no OpenAPI], `DELETE /api/users/:id`) - all 7 Working, each mutation's real evidence source visible in the report, the nested dynamic route correctly resolved from a real prior `GET /api/users` response.

`python tests/run_all.py --quick`: 44/46 (one new suite added) - same 2 pre-existing, unrelated flakes (`test_ai_openrouter.py`, `test_watch_pipeline.py`), zero new regressions.

## Explicitly not done

- Tier 5, "model/service evidence" (reading ORM/DB model field definitions) - no existing infrastructure to read from, and building one would be new, unrelated, universal-parsing work explicitly out of this phase's scope.
- YAML OpenAPI/Swagger files (`openapi.yaml`/`swagger.yaml`) - this project has no YAML-parsing dependency; adding one is real, separate infrastructure work, not attempted without it being explicitly asked for.
- Query parameters and custom headers are still not modeled as request evidence at all (an already-known, pre-existing gap, unchanged by this phase).
- Full per-field structured typing beyond what each evidence tier already provides natively (OpenAPI's declared `type`, Zod's declared type token) - a test-evidence-derived field's "type" is only ever the real example value's own Python type, never separately declared.
