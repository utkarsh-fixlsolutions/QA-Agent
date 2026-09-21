# Step 57 — Auth/Session Propagation

**Status:** IMPLEMENTED (2026-09-18).
**Goal:** close the second open gap from the Phase 4 audit — idurar-erp-crm's real run showed 5/5 non-skipped calls returning the identical `401 "No authentication token, authorization denied."`, because a real credential produced by one call never reached any other. General for any project whose API requires a bearer token or session cookie — no endpoint name, path, or field name is ever special-cased to one project.

## Audit finding

Traced directly against idurar-erp-crm's real source: `authUser.js` returns a real JWT on a successful login, nested at `result.token` (not top-level); `isValidAuthToken.js` reads it back via the standard `Authorization: Bearer <token>` header. No part of the existing pipeline ever captured a value from one call's response and carried it into another call's request headers — `http_client.call_endpoint` had no mechanism to send extra headers at all, and nothing tracked credential state across a run.

## Design

**`qa_agent/api_qa/auth_context.py`** (new): the same "evidence discovered by one call flows into a later one" pattern already established for path-parameter id resolution (`resolution._Evidence`) and Phase 3's create-then-use-the-real-id chaining (`planning.py`), applied to authentication instead of resource identity.

- `find_auth_token(response_json)` — a bounded, named vocabulary of common token field names (`token`, `accessToken`, `jwt`, `sessionToken`, ...), checked at the top level first, then one level of nesting into every dict-valued field (covers idurar's own real `result.token` shape generally, not as a special case). A value must be a string of plausible length to be considered a token at all - never a guess at an unrelated field.
- `AuthContext` — one real, shared credential state per run: `headers()` returns the real `Authorization`/`Cookie` headers to attach to the *next* call (`{}` until something has actually been captured); `observe(endpoint, result)` updates state from a completed call's real result, **only ever from a real `CALL_PASS`** (a rejection proves nothing about what a real credential looks like); the first real token found in a run is kept for its whole duration, never replaced by a later one.
- Never targets "/login"-shaped paths specifically - any endpoint's real, successful response can be the source, checked the same way every time.

**`qa_agent/api_qa/http_client.py`**: `call_endpoint` gained `extra_headers` (additive, merged into the request, never overriding `Content-Type`) and now captures every real `Set-Cookie` header from a successful response onto the new `ApiCallResult.response_cookies` field - this function still only ever sends what a caller decides, never invents a header itself.

**`qa_agent/api_qa/models.py`**: additive `ApiCallResult.response_cookies` (raw `Set-Cookie` values) and `auth_evidence` (a human-readable trace of *which* earlier call's response produced the credential this call carried - `""` when none was attached, the same "empty when there is none" convention `resolution_evidence` already follows).

**Wired into all three real-call-making passes**, sharing one `AuthContext` instance built once by `runner.py` so a credential captured in one pass is available to the others:
- `resolution.resolve_and_execute` (all three tiers, plus the probe/retry helper) - a small `_call` closure reads `auth_context.headers()` immediately before every real call and calls `auth_context.observe(...)` immediately after.
- `resolution.generate_and_execute_negative_cases` - attaches the current credential to a negative-case call, but deliberately never calls `observe` on its own result (a case deliberately sending bad/missing data expecting a 4xx must never be trusted as evidence of what a real credential looks like).
- `planning.execute_test_plan` - the same attach-then-observe pattern around its one real call site.

## Explicitly not done

- No credential refresh/re-login on expiry - the first real token captured is kept for the whole run; a token that expires mid-run is a real, honestly-reported failure on whichever call hits it, not silently retried.
- No OAuth/API-key-header conventions beyond a bearer token and a cookie - the two, by far most common real conventions; a project using a custom header scheme is a named, disclosed gap, not guessed at.
- No credential capture from a `CALL_FAIL`/negative-case response, even if it happens to contain a token-shaped field - only a real, passing response is ever trusted.

## Verified

New suite `tests/regression/test_api_qa_auth_context.py`: 24/24, including a full real, live end-to-end test - not a mock - against a genuine local HTTP server replicating idurar's exact real conventions (a token nested at `result.token`, read back via `Authorization: Bearer`): a login call passes, its real token is captured, and a second, otherwise-401-only endpoint receives it automatically and passes for real.

Fixed six duplicated test fixtures (`_FakeHeaders` in `test_api_qa.py`, `test_api_qa_functional_plan.py`, `test_api_qa_negative_and_schema.py`, `test_api_qa_probe_synthesis.py`, `test_api_qa_progress.py`, `test_api_qa_resolution.py`, `test_api_qa_synthetic_mutations.py`) that only implemented `.get()`, not the real `email.message.Message` interface's `.get_all()` this feature's own `Set-Cookie` capture now calls - stale fixtures, not a behavior change.

All pre-existing api_qa suites remain green; full suite 49/51 (the same two pre-existing, unrelated failures - `test_ai_openrouter.py`, `test_watch_pipeline.py` - confirmed unrelated across this entire session).

Real-project validation against idurar-erp-crm was not possible this round: its backend still cannot fully start in this environment (the separate, previously-identified missing-database precondition) - so this feature's correctness rests on the real-HTTP-server end-to-end test above, deliberately modeled on idurar's own confirmed real response/header shape, rather than a live idurar run. Revisit once the database-provisioning gap is closed.
