# Step 55 — Generalized API Discovery and Understanding (Phase 4)

**Status:** IMPLEMENTED (2026-09-18).
**Goal:** close the real, general architectural gaps the Phase 4 audit found in the discovery pipeline — not a fix for any one project. Every capability targets a general programming/framework idiom and must work without knowing the target project's identity or file layout.

## Audit finding (recap)

Two real projects (idurar-erp-crm, lms-ai) exposed gaps that were architectural, not incidental:

- Express's `router.route(path).get(...).post(...)` chained-call shape was never recognized — only `app.get(path, ...)` direct calls were.
- A route whose path argument is a template literal with real interpolation (`` `/${entity}/create` ``) or a bare variable was silently invisible — neither reported nor fabricated, just absent.
- `app.use(prefix, router)` mounting/composition was never resolved, so every mounted router's real, effective path (e.g. `/api/login` mounted under `/api`) was reported unprefixed or missed entirely.
- FastAPI's `include_router(router, prefix=...)` composition had the same gap on the Python side.
- OpenAPI/Swagger was only ever used as a body-construction fallback for endpoints already found by source discovery — a static `openapi.json`/`.yaml` document could not, by itself, produce endpoints when source discovery found none.
- A project with zero discovered endpoints always printed one hardcoded, Next.js-specific message regardless of the real reason (nothing found vs. unresolved routes vs. an unsupported framework).
- Zero discovered endpoints always skipped server startup entirely, collapsing three different real outcomes (0 endpoints + server fine; 0 endpoints + server broken; N endpoints + server broken) into one "0 APIs" report.
- The web upload flow could let a known-bad `npm install` failure be masked by a later, misleading `'next' is not recognized` error from a server that should never have been started.

## Design

**Additive, not a rewrite.** The existing four-strategy discovery architecture is extended, not replaced. Each discovery mechanism — including the two new ones — is isolated behind its own function boundary so a future AST/parser-based strategy could supplement or replace one without redesigning the rest of the pipeline. Regex remains the implementation technique for source strategies (accepted, named scope boundary — no full JS/TS/Python parser was built), but it is never the pipeline's conceptual foundation: routes flow through the same `ApiEndpoint`/`UnresolvedRoute` model regardless of which strategy or composition pass produced them.

**`qa_agent/api_qa/models.py`** (additive):
- `ApiEndpoint.discovered_by` — a tuple of provenance tags (`"express-source"`, `"openapi"`, `"express-composition"`, ...), appended to, never replacing, as an endpoint passes through composition/merge.
- `UnresolvedRoute` — a route-defining construct with a known HTTP method but an unresolvable concrete path: `raw_expression`, `source_file`, `reason`, `method`, `line`. Recorded, never fabricated into a fake path and never fed into HTTP execution.
- `ApiTestResult.unresolved_routes` / `discovery_strategy_counts` / `unsupported_frameworks` — additive fields carrying this new information through to CLI/report output.

**`qa_agent/api_qa/discovery.py`** (most of the change):
- Express: added `.route(path).verb(...)` chain recognition (any chain length: `.get(a).post(b).patch(c)`), alongside the unchanged direct-call shape. Path arguments are classified once (`_classify_route_path_arg`) into literal / template-with-interpolation / variable; only a real literal becomes an `ApiEndpoint`. The chained-verb argument regex tolerates up to two levels of nested parentheses in the handler argument — real projects commonly wrap a handler in an error-boundary helper (`catchErrors(handler)`, `asyncHandler(handler)`), which is a general idiom, not an edge case (found and fixed via real-project validation against idurar-erp-crm — every one of its `.route()` calls uses exactly this shape).
- FastAPI: the same literal/template/variable classification, applied to `@app.<verb>(...)`/`@router.<verb>(...)` decorator path arguments.
- New `_discover_openapi_endpoints`: walks a static, on-disk OpenAPI/Swagger document's own `paths` object (via `resolution.find_static_openapi_schema_detailed`) to create endpoints independently of source discovery — runs and can succeed even when every source strategy finds zero, and reports a malformed/oversized/unusable document as a real warning rather than crashing or staying silent.
- New `_unsupported_frameworks`: a fixed, named set of frameworks with no discovery strategy (NestJS, Flask, Django, Spring Boot, Laravel, ASP.NET) — reported as a distinct fact when detected, never conflated with "nothing found."
- New `_merge_endpoints_with_provenance`: when two strategies describe the same `(method, path)`, folds them into one `ApiEndpoint` with combined `discovered_by`, never a duplicate. Two endpoints are merged only on an exact method+path match — never "look similar."
- New `discover_api_endpoints_detailed(context, root)` / `DiscoveryOutcome` — the full orchestrator (all five strategies, composition, merge, strategy counts). `discover_api_endpoints` becomes a thin two-tuple-returning wrapper around it, so every existing caller's signature and behavior is unchanged.

**`qa_agent/api_qa/route_composition.py`** (new file): `apply_express_composition` / `apply_fastapi_composition`. Each builds a per-file import/require graph (Express: `require`/`import`; FastAPI: relative `from .x import y` only — resolving project-absolute imports would require guessing `sys.path`/package-root conventions this module has no way to know), finds mount call sites (`app.use(prefix, [...middleware,] router)` / `include_router(router, prefix=...)`), and does a bounded-depth (6), cycle-safe recursive walk from every real root — computed as `(files with their own routes ∪ files with mount statements) − files that are mount targets`, so a pure orchestrator file with zero routes of its own (just `require`s and `app.use` calls) is still walked from correctly. No file name, directory name, or variable name is ever special-cased. An orphaned mount target unreachable from any root still reports its own raw, unprefixed routes rather than losing them.

**`qa_agent/api_qa/resolution.py`**: `_STATIC_OPENAPI_CANDIDATES` extended to `.yaml`/`.yml` variants; `find_static_openapi_schema_detailed` added (returns the document, its source file, and a tuple of warnings for every unusable candidate); `find_static_openapi_schema` becomes a thin wrapper preserving the original public signature. New `PyYAML` dependency (`requirements.txt`), used only here, behind `yaml.safe_load`.

**`qa_agent/api_qa/runner.py`**: discovery now always runs before the server-start decision (the old `if not endpoints: skip with a Next.js-specific message` branch is gone). A new `precondition_failure` parameter short-circuits straight to `SERVER_START_FAILED` with the real, given detail *after* discovery has run, without ever attempting to start a doomed server. `strategy_counts`/`unresolved_routes`/`unsupported_frameworks` are folded into human-readable warnings and the new `ApiTestResult` fields.

**`web/server.py`**: when a dependency install fails, the real failure detail is now passed through as `precondition_failure` to `run_api_qa`, so it becomes the reported `server_status`/`server_detail` verbatim — a later, misleading secondary error (e.g. `'next' is not recognized`, from a server that should never have started) can no longer replace or mask it. No package-manager abstraction was built; this is a narrow, single parameter threaded through.

**`qa_agent/api_qa/render.py`**: additive `unresolved_routes`/`discovery_strategy_counts`/`unsupported_frameworks` in both `to_dict()` and the plain-text CLI report, printed only when non-empty.

## Explicitly not done

- No full JS/TS/Python AST parser — regex remains the implementation technique, isolated behind strategy function boundaries as directed.
- No project-absolute Python import resolution (`from app.routers import x`) — only relative imports.
- No JS/TS comment stripping — a `//`-commented-out `.route()` call can still be matched and reported as an (honest, harmless) unresolved route; found during idurar-erp-crm validation (a commented-out, additionally typo'd line), never fabricated into a real endpoint, but a known, disclosed precision gap.
- No live `/openapi.json` fetch as a discovery source — only a static, on-disk document; a live fetch requires a running server, which discovery must not depend on.
- No broad Python static analysis, no package-manager abstraction, no automatic modification of the target project.

## Verified

New suite `tests/regression/test_api_qa_phase4.py` (17 test functions covering the full approved-capability checklist, synthetic fixtures only): 47/47. All pre-existing api_qa suites stay green after the change (`test_api_qa.py` 191/191, `test_api_qa_fastapi.py`, `test_api_qa_zod_schema.py`, `test_api_qa_synthetic_mutations.py` — the last needed its own hand-rolled `_FakeResult` test double updated with the three new `ApiTestResult` fields, a stale fixture rather than a real regression).

Real-project validation (not hardcoded as fixtures, used only to check the general mechanism):
- **idurar-erp-crm** (Express, monorepo): first run surfaced a real bug — every `.route()` call failed to parse because idurar's handlers are universally wrapped in an error-boundary helper (`catchErrors(handler)`), which the original nested-paren-free regex could not match. Fixed generally (bounded two-level nested-paren tolerance, not an idurar-specific pattern). After the fix: 18 real endpoints discovered, all correctly prefixed under `/api` via real mount composition (`app.use('/api', coreAuthRouter)` etc.); 12 dynamically-generated entity routes (`` `/${entity}/create` `` and similar) correctly reported as `UnresolvedRoute`, never fabricated, never enumerated via any idurar-specific logic. Server failed to start in this environment (`nodemon` not installed) — correctly reported as 18 endpoints discovered, 18 calls skipped for that real reason, never collapsed into "0 APIs."
- **lms-ai** (Next.js App Router): existing discovery unchanged — 2 endpoints found (`/api/keep-alive`, `/api/sentry-example-api`); real dev server actually started (Turbopack ready) but the HTTP readiness probe found no response, an existing, unrelated readiness-gate behavior, not a Phase 4 regression.
- A third, previously-untouched FastAPI project was not available locally to test `include_router(prefix=...)` "in the wild"; skipped and disclosed rather than fabricated.

## Verdict

The implementation improves general API discovery capability: every fix (chain-call recognition, nested-paren-tolerant handler parsing, mount composition via real import-graph resolution, OpenAPI-as-a-source, honest unresolved-route/unsupported-framework reporting, decoupled discovery/startup, precondition short-circuiting) is keyed to a language/framework idiom, never to a project's identity, file name, or directory layout. The one workaround-shaped fix (the nested-paren bound in the chain-verb regex) was written and verified against idurar's real code but contains no idurar-specific string, path, or convention — it is a general fix for a general, widely-used Express pattern (error-boundary handler wrappers), confirmed general by the fact it was derived from the *shape* of the bug, not from idurar's names.
