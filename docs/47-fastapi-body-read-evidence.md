# Step 47 — FastAPI Body-Read Evidence (Closing the Last "Always Skipped" Gap)

**Status:** IMPLEMENTED (2026-09-16).
**Goal:** a real, 161-endpoint FastAPI project (a healthcare/ABDM integration backend) came back with **every single** POST endpoint `SKIPPED: no OpenAPI schema is available to construct a request body` - the exact gap docs/45 (synthetic mutation testing) was built to close, but for FastAPI specifically, it wasn't closed at all. Traced to a wrong assumption made explicit in docs/45's own "explicitly not doing" section, now corrected.

## Root cause

docs/45 deliberately did not build a Python/FastAPI equivalent of the JS/TS source-derived `body_field_hints` fallback, reasoning: *"FastAPI already serves live OpenAPI, so it only needs the required-field-without-default relaxation."* That assumption is false for a real, security-conscious FastAPI deployment: many production FastAPI apps disable `/openapi.json`/`/docs`/`/redoc` entirely (`FastAPI(openapi_url=None, docs_url=None, redoc_url=None)`), a common and reasonable hardening practice, especially for a backend handling health-record data. When that happens, `fetch_openapi_schema` returns `None` for the whole session, `build_request_body`'s only fallback (`endpoint.body_field_hints`/`reads_request_body`, populated only by the Next.js/Express discovery strategies) is empty for every Python endpoint, and every single mutating FastAPI endpoint falls straight through to the honest skip - regardless of how many there are.

## Fix

A Python/FastAPI equivalent of the JS/TS body-read-evidence extraction (`discovery.py`), deliberately narrower than the JS/TS version: it only ever detects **that** a handler reads a request body, never **which** fields - resolving a `payload: SomeModel` parameter's own real field names would require locating and parsing that model's class definition (often in a different, imported file entirely), a real, named scope boundary this does not attempt, matching the "no Zod/Joi/Pydantic-model AST parsing" boundary docs/45 already drew for the JS side.

Two recognized shapes, scanned within a bounded (2000-char), per-route window (the same "text up to the next route decorator in this file" technique the Express strategy already established, so two different routes in one file never blend evidence):

1. **A real, capitalized type annotation on a handler parameter** - `payload: SomeModel`, peeking through `Optional[X]`/`List[X]` wrappers - the overwhelmingly common way a FastAPI handler declares a Pydantic request body. A small stoplist (`Request`, `Response`, `UploadFile`, `Depends`, `Query`, `Path`, `Header`, `Cookie`, ...) excludes FastAPI/stdlib types that are never themselves a request body.
2. **A bare `body = await request.json()`** (or `req.json()`) - Python's own equivalent of the JS strategies' bare-assignment case, with no `const`/`let`/`var` anchor needed (Python has no such syntax).

`ApiEndpoint.reads_request_body` is set from either shape; `body_field_hints` stays empty for every FastAPI endpoint (by design - see above). This is enough for `build_request_body`'s already-existing minimal-fallback tier (docs/45) to send a real, small synthetic body (`{"qa-agent-test": true}`) instead of skipping - the endpoint now actually fires.

## What this looks like in practice

For a Pydantic model with its own required fields (`name: str`, `email: str`, ...), the minimal fallback body won't satisfy them - and it doesn't need to, to be useful: the real server's own real validation now genuinely runs and correctly rejects the incomplete body with a real `422`, which is authentic, useful signal ("this endpoint was really reached and really validated its input") that a silent skip could never produce. Every such call is clearly `synthetic=True`, so this is never confused with a real-evidence pass or fail. Extracting the model's own real required-field names (closing the 422 gap for good) is a real, named next step - not done here, and not silently promised.

## Explicitly not done in this step

- No Pydantic model class resolution/field-name extraction - a real, named scope boundary (see above), consistent with the equivalent JS/TS boundary already drawn in docs/45.
- No change to the JS/TS strategies, `build_request_body`, `synthesize_value`, or any reporting/labeling logic - this step only adds one new discovery-side evidence source that plugs into the exact same, already-built, already-tested fallback machinery.
- FastAPI's own `APIRouter(prefix=...)` composition remains the same pre-existing, unrelated, already-documented scope boundary (docs/32) - unchanged.

## Tests

`tests/regression/test_api_qa_fastapi.py` (new): a Pydantic-model-parameter fixture (recognized, no field names extracted, GET never falsely flagged), a bare `await request.json()` fixture (recognized), a no-evidence fixture (a plain `int` path parameter never mistaken for a body), and a two-routes-in-one-file fixture (proving per-route windowing - a POST's own evidence never leaks onto a DELETE below it). One real, guarded, end-to-end proof against a genuine running FastAPI+uvicorn server with `/openapi.json` explicitly disabled (the exact real-world scenario that surfaced this bug): the POST call that was always `SKIPPED` before this fix now actually executes, is clearly labeled synthetic, and gets a real `422` from the server's own real Pydantic validation.

**Verified:** `test_api_qa_fastapi.py` - 66/66 (61 pre-existing unchanged + 5 new). Full suite unaffected beyond the two pre-existing, unrelated flakes documented in every prior step's log.
