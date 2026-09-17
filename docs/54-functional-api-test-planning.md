# Step 54 — Functional API Test Planning + Execution (Phase 3)

**Status:** IMPLEMENTED (2026-09-17).
**Goal:** make the agent answer "given the APIs and their contracts, what meaningful functional tests should I run, and what dependencies exist between them?" as a real, structured, additive stage on top of the existing verification pipeline — not a second, competing HTTP layer.

## Audit finding

Before writing any code, `discovery.py`, `models.py`, `resolution.py`, `runner.py`, `http_client.py`, `render.py` (all already extended through Phase 1/Phase 2) were re-read in full. Confirmed:

- `resolve_and_execute` already calls every endpoint once, tier-ordered (non-dynamic GET → dynamic GET → mutations), resolving a dynamic path parameter or request body from real prior evidence or a clearly-labeled synthetic value. This remains correct and is **unchanged**.
- `http_client.call_endpoint`'s pass/fail rule is a fixed "2xx + valid JSON" check — there is no concept anywhere of "a 404 is the *correct*, expected outcome for this specific request." Confirmed no existing mechanism does this.
- No prior data flow exists from a `POST`'s own created-resource response into a *later* request; tier-1 evidence gathering only ever looks at non-dynamic GET responses. A create → read → update → delete → verify chain was not expressible.
- No structured "test plan" object existed anywhere — endpoints are executed independently, in tier order, with no notion of one test's *purpose* or its relationship to another.
- No prior mechanism ever called the same endpoint twice with two different real outcomes expected (e.g. `GET /items/{id}` once to read an existing item, once more, later, to confirm it is gone) — `ApiTestResult.calls` is a strict one-entry-per-endpoint contract and cannot express this.

These are the real, confirmed gaps this phase closes. Nothing above was duplicated — every new mechanism below reuses `call_endpoint`, `build_request_body`, `resolve_path_parameter`, and `synthesize_value` unmodified.

## Design

**Additive, not a replacement.** `resolve_and_execute`/`generate_and_execute_negative_cases`/`validate_response_schemas` are completely unchanged — every existing regression test, CLI flag, CSV/HTML export, and web UI field for `ApiTestResult.calls` keeps working exactly as before. Phase 3 adds a **second, distinct pass** that runs after the existing one, over the same real running server, producing its own `test_plan`/`functional_results` fields on `ApiTestResult`. The two passes may call the same real endpoint more than once (a "get by id" test and, later, a "verify deletion" test both target `GET /items/{id}`) — something the original `calls` contract was never designed to express, so it was never forced into it.

**`qa_agent/api_qa/models.py`** (additive):
- `TEST_CATEGORY_SMOKE` / `_FUNCTIONAL_POSITIVE` / `_FUNCTIONAL_WORKFLOW` / `_EXPECTED_NEGATIVE` (`TEST_CATEGORIES`) — the small, closed taxonomy the phase spec asks for, nothing larger.
- `EXPECTED_SUCCESS` / `EXPECTED_NOT_FOUND` (`EXPECTED_STATUS_KINDS`) — what "correct" means for one planned test's real outcome. A closed, two-value set; never an arbitrary caller-supplied status code.
- `PlannedTest` — one deterministic plan entry: `test_id`, `endpoint`, `purpose`, `category`, `depends_on` (human-facing prerequisite test ids), `expected_status_kind`, `request_source`, `id_source_test_id` (the *one* specific prior test a dynamic parameter's runtime value should come from, or `None` to use the general evidence-pool mechanism `resolve_path_parameter` already provides).
- `PlannedTestResult` — one test's real, executed outcome: the underlying real `ApiCallResult` (or `None` if never attempted), the real substituted path-parameter value (so a *later* dependent test can reuse it without re-extracting it), and this test's own `status`/`verdict_reason` — deliberately separate from `call.status`, because for an `EXPECTED_NOT_FOUND` test a real 404 makes `call.status == CALL_FAIL` (the unrelated, correct, generic 2xx-only rule) while the *test* itself correctly PASSes.
- `ApiTestResult.test_plan` / `functional_results` (additive, default `()`).

**`qa_agent/api_qa/planning.py`** (new): `build_test_plan(endpoints)` and `execute_test_plan(plan, base_url, timeout, ...)`.

- `build_test_plan` is pure, deterministic, no I/O, no AI. It groups endpoints by a real structural "resource" (a collection `GET`/`POST` at `/api/users` alongside a directly-nested item endpoint `/api/users/{id}` — reuses `resolution._parent_collection_path`/`_path_param_names` unmodified to find this, never a second grouping heuristic). For a resource with the full CRUD shape it plans: list → get-by-id (depends on list) → create → update *(if `PUT`/`PATCH` exists)* → delete (depends on the update, or the create if there is no update) → verify-deletion *(only if `DELETE` and the item `GET` both exist)*, expecting 404. A resource missing some of these methods simply gets fewer, still-correct steps (a GET-only API never gets a fabricated create/delete). Every endpoint not covered by a resource group — a standalone `GET /health`, a lone `POST /login`, a too-deeply-nested dynamic endpoint — still gets exactly one plan entry, so nothing discovered is ever silently absent from the plan. The plan is built **already in dependency order** (a prerequisite is always appended before anything depending on it) — `execute_test_plan` is a single ordered walk, no separate topological sort.
- `execute_test_plan` reuses `resolve_path_parameter` unchanged for the "resolve from the general evidence pool" case (list → get-by-id, and any standalone dynamic endpoint), and adds one small, new mechanism for the "resolve from one specific prior test's own response" case (create → update/delete, delete → verify): either the source test's own already-resolved parameter value is reused directly (verify reusing exactly the id delete just used), or a real id is extracted from the source test's real, single-object JSON response via `resolution._extract_scalar_value` (reused by wrapping the one object in a one-element list — the exact shape that function already expects; no duplicate extraction logic). Request bodies for `POST`/`PUT`/`PATCH` steps go through `resolution.build_request_body` unmodified — the same Phase 2 evidence hierarchy (OpenAPI → Zod → test-evidence → source hints → synthetic), never a second body-construction mechanism. Every real HTTP call goes through `http_client.call_endpoint` unmodified.
- `_judge_test_outcome` is the one new piece of pass/fail logic: for `EXPECTED_SUCCESS` it mirrors `call.status` exactly (unchanged meaning); for `EXPECTED_NOT_FOUND` it PASSes only on a real 404 and FAILs on anything else (a 2xx, a 5xx, or no response) — a closed, two-branch, code-only rule, never AI-decided and never a blanket "any non-2xx is fine."

**`qa_agent/api_qa/runner.py`**: `build_test_plan`/`execute_test_plan` called once, after the existing `resolve_and_execute`/negative-cases/schema-validation stages, using the same real `base_url`/`static_schema_doc`/`allow_synthetic_mutations` already established for the run. `_combine_progress` gained a fourth phase-scoped callback (`plan_cb`) alongside the existing three, same additive-total pattern.

**`qa_agent/api_qa/render.py`**: `render_test_plan(result)` (new) — a distinct CLI block (never merged into the existing per-endpoint table, since the two can legitimately disagree in shape) showing every test's category/purpose/dependency/concrete request/expected-vs-actual outcome, plus a final pass/fail/skip tally. `to_dict()` gained a `functional_test_plan` key, additive, alongside the existing `calls`/`negative_calls`/`schema_validations`. CSV/HTML export for the functional plan is explicitly **not** implemented this round (see Explicitly not done).

**CLI (`qa_agent/__main__.py`)**: prints `render_test_plan(api_result)` right after the existing `render_api_qa(api_result)` output, only when the plan is non-empty.

## Example workflow (real, from the controlled demo app)

```
T1  GET /api/users                 -> 200 PASS   (discovers user id 1)
T2  GET /api/users/{id}            -> 200 PASS   (depends on T1, id=1)
T3  POST /api/users                -> 201 PASS   (creates id=4; body from OpenAPI evidence)
T4  PUT  /api/users/{id}           -> 200 PASS   (depends on T3, id=4; body from test-evidence)
T5  DELETE /api/users/{id}         -> 204 PASS   (depends on T4, id=4)
T6  GET /api/users/{id}            -> 404 PASS   (depends on T5, id=4; expected=not_found)
T7  GET /health                    -> 200 PASS   (standalone smoke test)
```

Notably, the existing (unchanged) `resolve_and_execute` pass on the same run reports `PUT /api/users/:id -> 404 Failing` for the exact same endpoint - not a Phase 3 bug, but a real, honest illustration of the gap this phase closes: that pass executes mutations in a fixed `(path, method)` sort order with no notion of dependency, so its own `DELETE` happened to run *before* its own `PUT` against the same synthetic id. The functional plan's dependency-aware ordering never has this problem, because create/update/delete/verify are ordered by real data flow, not alphabetically.

## Failure demonstration (real, from the controlled demo app)

One controlled bug was introduced into a copy of the demo app: `DELETE /api/users/:id` returns a real `204` but never actually removes the resource from the store. Re-running the same agent, unmodified:

```
T5  DELETE /api/users/{id}         -> 204 PASS   (the target's own false claim, honestly recorded)
T6  GET /api/users/{id}            -> 200 FAIL   (expected 404, got 200 instead)
```

`T6`'s `verdict_reason`: `"expected 404 after deletion, got 200 instead"`. The functional plan detected the real defect from real HTTP evidence alone - no AI, no repair attempted (out of scope, per the phase spec).

## Evidence (request data + runtime ids)

- `T3`'s body: `{"name": "Ada Lovelace"}` - from the demo app's own `openapi.json` (`EVIDENCE_OPENAPI`), example value reused verbatim.
- `T4`'s body: `{"name": "Ada Lovelace Updated"}` - from the demo app's own `tests/users.test.js` Supertest example (`EVIDENCE_TEST_EXAMPLE`, docs/53's own test-evidence discovery, unmodified).
- `T3`'s created id (`4`, following the demo's own seeded starting state) flows into `T4`/`T5`/`T6` via `PlannedTestResult.resolved_param_value` / real single-object response extraction - never invented.
- `T2`'s id (`1`) flows from `T1`'s own real collection response via the unchanged `resolve_path_parameter` mechanism.

## Scope control - explicitly not done this round

- No universal workflow engine or general topological sort - the plan is built already in dependency order; execution is a single ordered walk.
- No repair/diagnosis of a detected functional failure (a later phase's concern, matching the same boundary Phase 1/2 already drew).
- No CSV/HTML export rows for the functional plan yet - `to_dict()`'s new `functional_test_plan` key is available for any future export/UI work, but the existing CSV/HTML renderers were not extended this round.
- No support for a workflow spanning more than one `PUT`/`PATCH` step, or more than one resource level of nesting - matches the phase spec's own "small-scale, not a huge taxonomy" instruction.
- Query parameters/custom headers as planning inputs remain out of scope, unchanged from Phase 2.

## Files changed

- `qa_agent/api_qa/models.py` - `TEST_CATEGORY_*`/`TEST_CATEGORIES`, `EXPECTED_SUCCESS`/`EXPECTED_NOT_FOUND`/`EXPECTED_STATUS_KINDS`, `PlannedTest`, `PlannedTestResult`, `ApiTestResult.test_plan`/`functional_results`.
- `qa_agent/api_qa/planning.py` (new) - `build_test_plan`, `execute_test_plan`.
- `qa_agent/api_qa/resolution.py` - `synthesize_path_parameter_value` (small, reusable export of the existing synthetic-id rule).
- `qa_agent/api_qa/runner.py` - Phase 3 stage wired in additively; `_combine_progress` gained a fourth callback.
- `qa_agent/api_qa/render.py` - `render_test_plan`, `to_dict()`'s new `functional_test_plan` key.
- `qa_agent/api_qa/__init__.py` - new names exported.
- `qa_agent/__main__.py` - prints the functional test plan report after the existing API QA report.
- `tests/regression/test_api_qa_functional_plan.py` (new) - plan generation, dependency ordering, runtime id extraction, CRUD workflow, expected-negative outcome, a real caught defect, contract integration, a real Node end-to-end run, and a Phase 1 regression check.
- `tests/regression/test_api_qa_progress.py` - updated for `_combine_progress`'s new, fourth callback (an intentional, documented extension - not a weakened test).
- `tests/regression/test_api_qa_synthetic_mutations.py` - its hand-rolled `_FakeResult` stub gained the new `functional_results` attribute `to_dict()` now reads.

## Verified

- New suite: `tests/regression/test_api_qa_functional_plan.py` - 57/57 checks passed, including one real, guarded, subprocess-based end-to-end run against a genuine Node HTTP server.
- Real, controlled CLI demonstration (`python -m qa_agent discover <path> --api-test`) against a hand-built Express-shaped demo app - happy path (7/7 functional tests passed) and the intentional-bug variant (6/7 passed, the real defect caught as `FAIL` with an honest reason) both shown above.
- Full regression suite (`python tests/run_all.py --quick`) - same 2 pre-existing, unrelated failures as every prior phase (`test_ai_openrouter.py`, `integration/test_watch_pipeline.py`), zero new regressions.
