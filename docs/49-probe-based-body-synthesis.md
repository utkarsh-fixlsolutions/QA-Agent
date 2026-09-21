# Step 49 — Probe-Based Body Synthesis

**Status:** IMPLEMENTED (2026-09-16). Time-boxed to exactly this one feature, per explicit user instruction.
**Goal:** too many POST/PUT/PATCH calls were still `SKIPPED: no OpenAPI schema is available to construct a request body` - the remaining, most common real-world case docs/45/47 didn't close: no live schema, and a body-parsing style (`.formData()`, a helper function, `JSON.parse` on an accumulated raw body, ...) the source-regex strategies don't recognize either.

## Logic implemented (exactly as specified)

Only tried once `build_request_body` already found nothing (no schema, no source-derived hint) and `allow_synthetic_mutations` is `True`:

1. Send a real request with an empty body `{}`.
2. **2xx** → that really is a valid body; the probe call itself is the result, real evidence, never marked synthetic.
3. **A real validation error naming its own missing/required fields** (`_extract_missing_fields_from_error_response`, `resolution.py`) → extract those real field names, build a synthetic body via the existing `synthesize_value` (string→`qa-agent-test-value`, email→`qa-agent-test@example.com`, id-like→`1`, boolean-shaped name→`true`, count/amount/age-shaped name→`1`, otherwise→`qa-agent-test-value`), retry once, mark `synthetic=True` with the real field names listed.
4. **No usable field list** → `SKIP`, with a clear reason naming that the probe itself was tried and found nothing.

Recognizes four real, named validation-error shapes: FastAPI/Pydantic (`detail`/`loc`), express-validator-style (`errors` list with `param`/`path`/`field`), a plain field-keyed `errors` object, and zod's own `.flatten()` shape (`fieldErrors`, plain or nested under `error`) - never a universal parser; anything else is honestly skipped.

## Where it lives

`qa_agent/api_qa/resolution.py`: `_extract_missing_fields_from_error_response` (pure) + `_probe_and_synthesize_body` (makes the real probe/retry calls) - wired into `resolve_and_execute`'s tier-3 loop as the last resort right before the existing skip. `qa_agent/api_qa/synthesis.py`: `_by_name_heuristic` gained boolean (`is`/`has`-prefixed, `enabled`/`active`) and count/amount-shaped-name → `1` rules, since a "missing field" validation error never carries real type evidence - only a name - matching the same honest limitation every other rule in that function already has.

## Verified

`tests/regression/test_api_qa_probe_synthesis.py` (new, 33/33): the extractor against all four recognized shapes plus unrecognized/non-dict inputs; the probe function's three outcomes (2xx-used-directly, synthesize-and-retry, give-up) via mocked HTTP; `resolve_and_execute` wiring (fires via probe, still skips honestly when the probe finds nothing, never attempted when `allow_synthetic_mutations=False`); one real, guarded, end-to-end proof against a genuine running Node server whose POST handler parses its body via raw accumulation + `JSON.parse` (deliberately not `req.body.x`/`request.json()`, so no source-derived hint exists at all) - the call was `SKIPPED` before this step, now fires via the probe and passes for real.

All previously-passing suites re-run clean: `test_api_qa.py` 159/159, `test_api_qa_synthetic_mutations.py` 81/81, `test_api_qa_resolution.py` 70/70, `test_api_qa_negative_and_schema.py` 40/40, `test_api_qa_fastapi.py` 66/66, `test_api_qa_progress.py` 16/16.

## Explicitly not done (per today's own scope)

Not wired into `generate_and_execute_negative_cases` (negative-case body construction still calls `build_request_body` directly, unchanged) - out of the exact scope given. No other roadmap stage touched.
