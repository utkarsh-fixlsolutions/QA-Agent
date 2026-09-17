# Step 45 — Synthetic Mutation Testing: POST/PUT/PATCH/DELETE Actually Execute

**Status:** IMPLEMENTED (2026-09-16).
**Goal:** close the gap that made the agent look like it "just parses" the API - almost every POST/PUT/PATCH/DELETE call against a real, hand-rolled project was silently `SKIPPED`, never actually executed. See docs/33 (deterministic verification, the strict evidence-only rule this step deliberately widens) and docs/41 (status/demo-readiness doc that first surfaced the user's complaint).

## The real cause, found by reading the code, not the docs

`resolve_and_execute` already starts a real dev server and fires real HTTP calls. But every mutating call's request body came from `build_request_body`, which only ever succeeded when the target served a live `/openapi.json` **and** every required field in its schema had an explicit `default`. Neither is true for almost any hand-rolled Next.js/Express project (no OpenAPI at all), and rarely true even for FastAPI (Pydantic required fields normally have no default). In practice, nearly every mutating call was `SKIPPED: no OpenAPI schema is available to construct a request body` - which reads exactly like "the agent doesn't really test POST/PUT/DELETE," even though GET calls really did execute.

## The core trade-off, confirmed with the user before building

docs/33's own rule - never invent a value, only ever use real evidence or a real schema default - is what made demo mutation testing mostly a wall of `SKIPPED`. The user explicitly approved widening this: synthesize a clearly-labeled placeholder value for a required field with no real evidence, **on the condition that it is toggleable, every synthetic call is unmistakably labeled, and real-evidence and synthetic results are never blended into one number anywhere** (CLI, web, CSV/HTML export, AI diagnosis write-ups). This document's whole design follows from that condition.

## Design

### `qa_agent/api_qa/synthesis.py` (new) - the one place a value is invented

`synthesize_value(field_name, prop=None)` - pure, no I/O, deterministic (same input, same output, every time - a demo run is reproducible). Precedence, most-real-evidence-first:

1. `prop["example"]` - a real fact the schema itself declares.
2. `prop["enum"][0]` - same reasoning.
3. `prop["type"]`/`prop["format"]` - a format-aware placeholder (`email`, `date`/`date-time`, `password`-shaped field name, JSON `type`).
4. No schema info at all (a purely source-derived field hint) - a name-based heuristic on `field_name` alone (`email`/`url`/`date`/`id`-shaped/`name`-`title`-`username`/generic fallback).

Most synthesized strings carry an unmistakable `qa-agent-test` marker, so a write made with one is trivially identifiable (and cleanable) in a real, live database. Two deliberate exceptions, chosen for realism instead of the marker: a password-shaped field gets `QaAgentTest123!` (a value that actually satisfies a typical password policy), and a date/date-time gets the fixed `2025-01-01T00:00:00Z`.

### `ApiQaConfig.allow_synthetic_mutations` (default `True`) - opt-out, not hardcoded

Threaded through `resolve_and_execute` -> `build_request_body`/`resolve_path_parameter`, and through `generate_and_execute_negative_cases`. `False` reproduces docs/33's original, strict, evidence-only behavior exactly - proven directly: every one of `test_api_qa_resolution.py`'s own original skip-behavior tests still exists, now passing `allow_synthetic_mutations=False` explicitly, still green, unchanged.

### `build_request_body` widened, never narrowed

A required field with a real schema default still uses it exactly as before - `synthetic_fields` stays empty for that field. A required field with **no** default is now synthesized (via `synthesize_value`, using the property's own `example`/`enum`/`type`/`format` when present) rather than skipping the whole body - a body is never invented as one fabricated block when part of it is already real; `synthetic_fields` names exactly which keys were invented.

With **no** OpenAPI schema describing the operation at all, falls back to `ApiEndpoint.body_field_hints` (new, populated by `discovery.py` - see below): specific field names become a fully-synthetic body. When the handler is only known to read *some* body at all (`ApiEndpoint.reads_request_body`, new) but no field names could be extracted, a minimal one-field synthetic body (`{"qa-agent-test": true}`) is sent rather than skipping - still enough to exercise the endpoint. With no body-reading evidence at all, still an honest skip, unchanged.

### `discovery.py` - one additional regex pass per JS/TS strategy

For Next.js App Router, Pages Router, and Express, a mutating endpoint's own route-handler source is scanned (text already being read for method extraction - no new file I/O) for the handful of overwhelmingly common ways a handler reads its own request body:

- `const { a, b } = await request.json()` / `await req.json()` (destructured)
- `const { a, b } = req.body` (destructured)
- `req.body.fieldName` (direct property access)
- a bare `= await request.json()` / bare `req.body` with no destructuring - no field names from this shape alone, but still real evidence the handler reads a body (`reads_request_body`)

This is a real, named, narrower scope boundary - not a JS/TS parser - matching the "regex over a recognizable convention, anything unusual is a named limitation" discipline `discovery.py` already established for route discovery itself. Express windows each route call's own handler text to the next route call in the same file, so two different POST routes in one file never blend fields together.

**A real bug found and fixed via the end-to-end test below:** the first version of the destructure regex (`\{([^}]+)\}\s*=\s*await request.json()`) could start matching from *any* earlier `{` in the file - including a function body's own opening brace - and capture garbage like `"const"` as a fake field name, because `[^}]+` happily consumes a nested `{` on its way to the first real `}`. Fixed by anchoring the pattern to a real `const`/`let`/`var` declaration, so it only ever starts at a genuine destructuring assignment.

### `resolve_path_parameter` widened the same way

No real parent-collection evidence, synthesis allowed: a synthetic id is used instead of skipping - the numeric sentinel `1` for an id-shaped parameter name (`id`, or an `_id`/`Id` suffix), else the string sentinel `qa-agent-test-id`. The endpoint is actually called: a real 404/200/500 against a made-up id is real, useful evidence about how the target handles an unknown id, not a guess about behavior.

### Reporting never blends real evidence with synthetic results

`ApiCallResult`/`NegativeCallResult` gained `synthetic: bool` and `synthetic_fields: Tuple[str, ...]` - additive, default `False`/`()`, every existing construction site unaffected. `render.py`'s new `_summary_counts` computes real-evidence (`pass`/`fail`/`skipped`) and synthetic (`pass`/`fail`) tallies **once**, exposed three ways:

- `to_dict()` gains a top-level `"summary"` key - the web UI reads this directly rather than recomputing it, so it can never silently drift from the CLI/CSV/HTML split.
- The CLI's summary line splits into two clauses: `"N call(s) - X pass/Y fail (real evidence); A pass/B fail (synthetic data)"`, plus a per-call `[SYNTHETIC: field1, field2]` tag.
- HTML export gets a `[Synthetic Data: field1, field2]` detail-column suffix; CSV gains `synthetic`/`synthetic_fields` columns.

The web UI (`index.html`) shows a `SYNTHETIC DATA` badge on result cards and negative-test rows, and any AI-diagnosis write-up for a failing synthetic call gets an explicit note that the diagnosis is against invented data, not real evidence - so a synthetic FAIL is never presented with the same confidence as a real one.

### Negative test cases inherit this automatically

`generate_and_execute_negative_cases` already only ever fires for an endpoint whose *positive* call already passed. Once the widening above makes far more positive calls actually succeed (instead of skip), negative-case coverage grows for free - no separate logic needed beyond updating its own `build_request_body` call site for the new 3-tuple return and copying `synthetic`/`synthetic_fields` from the positive call onto the resulting `NegativeCallResult`, so a negative-case verdict is never presented with more confidence than the positive evidence it was built from.

## Explicitly not done in this step

- No Zod/Joi/Pydantic-model AST parsing - field hints stay `req.body`/`request.json()` access patterns only (cheap, general, framework-agnostic).
- No FastAPI source-derived hints - unnecessary, since FastAPI already serves a live OpenAPI document by default; it only needed the required-field-without-default relaxation.
- Express/FastAPI router-mount-prefix resolution remains the same pre-existing, unrelated, already-documented scope boundary (docs/38) - unchanged.
- No fuzzing/long-string/special-character negative-case generation - unrelated, unchanged scope boundary from docs/43.
- `docs/43`/`docs/44` (negative tests/schema validation/severity, live progress) remain undocumented despite being fully implemented and tested in code - inherited debt from a prior session, not addressed by this step; named here rather than silently carried forward again.

## Tests

`tests/regression/test_api_qa_synthetic_mutations.py` (new, 29 test functions, 81 checks): `synthesize_value`'s precedence and every named heuristic; `discovery.py`'s widened body-field-hint extraction (destructured, direct-access, bare-assignment, deduplication, and the real regex bug found and fixed); `build_request_body`'s synthesis/mixed-real-and-synthetic/minimal-fallback/still-skips-with-no-evidence/config-opt-out behavior; `resolve_path_parameter`'s numeric-vs-string synthetic sentinel; `resolve_and_execute` marking a synthesized call `synthetic` end-to-end (mocked HTTP) and the opt-out flag reproducing the original skip; `render.py`'s real-vs-synthetic summary split in both `to_dict()` and the CLI text; one real, guarded, end-to-end proof against a genuine running Node server standing in for a hand-rolled Next.js project with **no OpenAPI schema at all** - before this step, its POST call was always `SKIPPED`; now it fires with a synthesized body, passes for real, and is clearly labeled synthetic.

`tests/regression/test_api_qa_resolution.py` (70/70, unchanged pass count): every original skip-behavior test kept, now passing `allow_synthetic_mutations=False` explicitly where the default (`True`) would otherwise change its outcome - proof the strict, evidence-only mode this step's opt-out flag restores is byte-for-byte the same behavior docs/33 originally built.
