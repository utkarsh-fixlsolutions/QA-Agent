# Step 30 — API QA v1: Next.js App Router Endpoint Testing

**Status:** IMPLEMENTED (2026-09-12).
**Goal:** make the agent actually useful for real API testing - detect a real API project, discover its real endpoints, start its real server, call those endpoints for real, and report structured, honest pass/fail evidence. Not G5 orchestration (paused for this task), not diagnosis, not repair - those already exist (G3/G4) and are explicitly not touched here.

## A real scope correction, made before any code was written

The task named **Express/Node.js** as the first target framework and **`D:\Major projects\lms-ai`** as the real dogfooding target. Inspecting `lms-ai` first (per this task's own "inspect before coding" instruction) found it is a **Next.js App Router** project - `package.json` lists `next`/`react`, there is no `express` dependency anywhere in the repository (confirmed by a repository-wide search), no `server.js`, and its API surface is `app/api/*/route.ts` files exporting `GET`/`POST` functions, a completely different discovery mechanism from Express's `app.get(...)`/`router.post(...)`.

Building an Express-only scanner would have made "demonstrable end-to-end on `lms-ai`" impossible without fabricating a result, so this was raised directly rather than guessed at. The resolution (user's own choice, presented with concrete trade-offs): **build v1 for Next.js App Router route handlers**, the framework `lms-ai` actually is, so real dogfooding is genuinely possible today. Express/Node route-scanning (`app.get`/`router.post`) is real, common, and deliberately deferred to a later v2 - not silently dropped, not half-built alongside this.

## What already existed, and what this reuses

Inspected before writing anything: `qa_agent/project/` (`discover_project`, `ProjectKnowledge`, `RepositoryContext` - `important_directories` already recognizes `app`/`routes`/`controllers` by name, `runtime_files`/`entry_points` already recognize `server.js`/`app.py`/etc.), `qa_agent/runtime/` (`RuntimeCheck`/`RuntimeCheckResult`/`RuntimeExecutionResult` shapes, and `executor.py`'s own npm-script discovery + process-tree management - the existing `api-endpoints`/`server-startup` `RuntimeCheck`s already *plan* for this kind of check but have no real HTTP-calling executor behind them), the CLI's `discover` subcommand structure (`--context`/`--runtime-plan`/`--execute-runtime-plan`/`--diagnose`/`--repair-runtime`, each implying the ones before it).

This capability reuses `RepositoryContext` as its one real input - `qa_agent/api_qa/` never re-walks a filesystem or re-detects a framework; every fact (package managers, `app`/`routes` directories) is reached via `context.project`. It deliberately does **not** import `qa_agent.runtime`: that engine's own `run_until_ready_or_timeout` always terminates the process it starts before returning (correct for a liveness check, wrong for "keep the server up so real HTTP calls can be made against it"), and `runtime/executor.py` is already shipped and tested - not modifying it at all is the simplest way to guarantee "existing G1-G4 behavior remains intact" completely. `qa_agent/api_qa/server.py` instead duplicates the small amount of process-start/kill/ready-detection logic it actually needs, the same "small, deliberately duplicated helper" convention this project has used before (`_prompt_text`/`_string_list`-style helpers across `qa_agent/agent/`'s own files) rather than reaching into another module's private internals.

## Design (`qa_agent/api_qa/` - new package)

- **`models.py`** - `ApiEndpoint` (method, URL path, real source file, `dynamic` flag), `ApiCallResult` (references an `ApiEndpoint`, never duplicates it - the same "reference, don't duplicate" pattern `RuntimeExecutionResult` established for `RuntimeQAPlan`), `ApiTestResult` (the whole-session outcome). Status constants deliberately mirror `runtime/execution_models.py`'s own `STATUS_PASS`/`STATUS_FAIL` naming so a later phase connecting a failure here to G3 diagnosis sees a familiar shape - not a promise that connection exists yet (it does not; see below).
- **`discovery.py`** - `discover_api_endpoints(context, root)`. Scoped to real `app`-named directories `context.project.important_directories` already found; walks only that subtree for files literally named `route.ts`/`route.js`; extracts real exported HTTP method functions via a regex over source text (`export async function GET(...)`, `export const POST = ...`) - never a full TS/JS parse, never a guessed method. Route-group segments (`(marketing)`) are stripped from the URL, a documented Next.js convention. Dynamic segments (`[id]`) are preserved literally and the endpoint is marked `dynamic=True`. A route file with no recognized export produces a warning, never a fabricated endpoint.
- **`server.py`** - `discover_server_start_command(root, project)` (real npm `dev`/`start` script only, same evidence rule `runtime/executor.py` already established), `start_and_wait_ready(command, cwd, timeout, ...)` (like `run_until_ready_or_timeout`, but does **not** kill the process on a ready/no-signal outcome - only on a genuine crash), and `ServerHandle.stop()` (always safe to call, even when nothing ever started).
- **`http_client.py`** - `call_endpoint(base_url, endpoint, timeout)`. Stdlib `urllib.request` only (the same choice `qa_agent/ai/ollama.py` already made, and this project's own Dependency philosophy: isolate a library behind our own abstraction only when one is actually needed - nothing here needs more than urlopen + a bounded read). Handles `HTTPError` (a real, received non-2xx response - status/body/content-type all captured as real evidence, never treated as a connection failure), `URLError`/timeout (no response ever received - `status_code` stays `None`), and caps the real read at `MAX_RESPONSE_READ_BYTES` (64KB) so a runaway response can never grow this process's memory unbounded. Pass/fail rule, deliberately simple: a 2xx status code, and - only when the response declares a JSON content-type - a body that actually parses as JSON. A truncated body is never claimed valid or invalid either way.
- **`runner.py`** - `run_api_qa(context, root, config=None)`, the one public orchestration entry point: discover endpoints -> discover a start command -> start the server -> resolve a real base URL (parsed from the server's own startup log line when observed - e.g. `- Local:        http://localhost:3000` - falling back to Next.js's documented default port only when nothing was observed, and saying so explicitly in a warning) -> call every non-dynamic endpoint -> always stop the server (a `finally`, mirroring `runtime/executor.py`'s own "never leaves a real process running" guarantee). Every missing precondition (no endpoints, no start command, a crashed server) is a normal, honest `ApiTestResult`, never an exception.
- **`render.py`** - plain-text and JSON/dict presentation, mirroring `runtime/execution_render.py`'s own role. ASCII only (`->`, not `→`) - a real Windows console `UnicodeEncodeError` was hit and fixed during this step's own testing.

## What this deliberately does not do

Discover Express (`app.get`/`router.post`) or Pages Router (`pages/api/**`) endpoints - real, common patterns, explicitly deferred to a documented v2, not half-supported unreliably. Invent a value for a dynamic route segment (`[id]`) to make it callable - discovered and reported, never called automatically. Perform any authentication, negative testing, schema/contract validation, or browser automation - all explicitly out of scope per this task. Connect a failing call to G3 diagnosis or G4 repair - the result shapes are deliberately compatible for that, but no such wiring exists yet (a natural, named next step, not started).

## CLI

```
python -m qa_agent discover <path> --api-test
```

Implies `--context` (needs `RepositoryContext` to discover endpoints and a start command). No AI. Starts a real dev server it always stops afterward, exactly like `--execute-runtime-plan` already does for its own five check types.

## Tests

`tests/regression/test_api_qa.py` - **39 test functions, 89 checks**: model validation (rejecting unrecognized method/status values); discovery (simple routes, multiple methods in one file, `export const` handlers, route-group stripping, dynamic-segment marking, unrecognized-export warnings, `node_modules`/`.next` exclusion, a non-Next.js project producing nothing, dedup); the HTTP client, entirely offline via the same `urlopen`-swap technique `test_ai_openrouter.py` already established (2xx+valid-JSON pass, 2xx+non-JSON-content-type pass, a real `HTTPError` 500 captured as real evidence not a connection failure, a 2xx with a broken JSON body correctly failing, connection-refused, timeout, the bounded response sample, the bounded raw read, a truncated body never claiming JSON validity either way, the real HTTP method being sent); server lifecycle (command discovery preferring `dev` over `start`, `not_found` for a missing binary, a real ready-signal detection and a real crash detection, both via real `sys.executable -c "..."` subprocesses - `sys.stdout.flush()` required, the same buffering gotcha `test_runtime_execution.py` already documented); orchestration (`run_api_qa` skipping honestly when there are no endpoints or no start command, reporting a real crashed server, never calling a dynamic route even when the server is genuinely running); two full real end-to-end tests (a real `npm run dev` running a plain Node `http` server standing in for `next dev` - this environment has no full Next.js install - proving a real pass and a real fail both get reported correctly, guarded by `_npm_available()` and skipped cleanly otherwise, the same precedent `test_runtime_execution.py` already set); reporting (method/path/status/timing shown, failure evidence shown, no-calls handled, warnings surfaced); CLI wiring (the flag reports honestly with nothing discovered, is a no-op without the flag, and a full real end-to-end CLI invocation).

## Dogfooding: `D:\Major projects\lms-ai`

```
python -m qa_agent discover "D:\Major projects\lms-ai" --api-test
```

**Discovered:** 2 real endpoints - `GET /api/keep-alive`, `GET /api/sentry-example-api` (both from real `app/api/*/route.ts` files; `lms-ai` has no dynamic API routes).

**Real result (warm cache, ~14s total):**

```
Server status: started (matched ready signal: '✓ Ready in 5.6s')
Base URL:      http://localhost:3000

GET /api/keep-alive           -> 200   (5112ms) PASS
GET /api/sentry-example-api   -> 500   (733ms)  FAIL
      HTTP 500 response

2 call(s) - 1 fail, 1 pass. Total time: 13.65s
```

Both outcomes verified correct by reading the real source: `/api/keep-alive` really returns 200 (it pings Supabase and reports success). `/api/sentry-example-api` really always throws (`throw new SentryExampleAPIError(...)`) - it is Next.js's own generated Sentry example route, deliberately faulty to test error monitoring. The tool caught a real, intentional 500 correctly, not a false positive. The dev server (`npm run dev`, Turbopack) was confirmed genuinely stopped afterward - `curl` to port 3000 failed immediately once the CLI process exited.

**A real limitation found while dogfooding:** the very first cold run (uncached Turbopack compile, plus an unrelated stray `D:\package-lock.json` on this machine that made Next.js print a "multiple lockfiles" warning) took long enough that the default 20s server-startup timeout was exceeded, and both calls then failed with a connection timeout - an honest, correctly-reported failure, not a bug, but a real usability rough edge: **v1 has no CLI flag to raise `--server-startup-timeout`**, so a genuinely slow cold start (first Turbopack compile, a large monorepo) can make a real, eventually-healthy server look like a QA failure on the very first run. Named here rather than silently worked around.

## Files changed

`qa_agent/api_qa/__init__.py`, `models.py`, `discovery.py`, `server.py`, `http_client.py`, `runner.py`, `render.py` (all new); `qa_agent/__main__.py` (+`--api-test` flag on `discover`, wired after `--context`); `tests/regression/test_api_qa.py` (new, 39 test functions, 89 checks); `docs/12-architecture.md` (+`api_qa/` module row); `docs/step-log.md` (this entry). **Untouched:** every G1-G4 module (`project/`, `runtime/`, `ai/`), `qa_agent/agent/` (G5.1/G5.2), the production default provider/model, every existing CLI flag's behavior.

## Verified

`test_api_qa.py` - 89/89 checks, including two real, real-subprocess, real-HTTP end-to-end tests. Full suite: **34/36** (`integration/test_watch_pipeline.py`'s pre-existing, unrelated timing flake, and `regression/test_ai_openrouter.py`'s pre-existing, unrelated environmental flake - a leftover `OPENROUTER_API_KEY` in this shell's environment from an earlier step's live verification causes one `api_key=None` isolation test to observe the real env var instead; neither caused by, or fixed by, this step).

## What should be next

Connecting a real `ApiCallResult` failure (e.g. the `/api/sentry-example-api` 500 above) to G3 diagnosis and G4 repair - the result shapes here were deliberately built compatible with that, but no such wiring exists yet. A `--server-startup-timeout` CLI flag (the one real rough edge found dogfooding). Express/Node route discovery as v2, using the same `ApiEndpoint`/`ApiCallResult`/`ApiTestResult` models. Pages Router (`pages/api/**`) support, if a real project needing it is found. Any of these, or resuming G5 orchestration, is the user's call - none was started here per this task's own explicit scope.

**Status: API QA v1 (Next.js App Router) CLOSED.**
