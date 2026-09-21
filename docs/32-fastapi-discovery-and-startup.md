# Step 32 — Python/FastAPI Discovery and Server Startup

**Status:** IMPLEMENTED (2026-09-12).
**Goal:** the smallest missing capability identified by the capability audit against `D:\Working\qa-agent-fastapi-demo` - extend `qa_agent/api_qa/` with a second, additive Python/FastAPI discovery-and-startup strategy alongside the existing Next.js one, so the already-built API-calling/diagnosis/repair chain can reach a real Python project for the first time. No G5 wiring, no diagnosis, no repair, no configuration/secret repair, no automatic dependency installation - this step is scoped entirely to "discover the routes, find a real way to start it, and become reachable."

## The real problem, from the audit

`qa_agent/api_qa/discovery.py` only ever looked for `route.ts`/`route.js` files. `qa_agent/api_qa/server.py` only ever looked for an npm `dev`/`start` script. Against a real FastAPI project, both returned nothing - not a false negative from a bug, just two strategies that had never been asked to recognize a third ecosystem. Separately, the audit found that even the older, generic `qa_agent/runtime/executor.py`'s own Python entry-point heuristic would have picked the *wrong* file for this exact project (`app/main.py`, which never calls `uvicorn.run()`) over the real runner script (`run.py`) - a real, concrete illustration of why "the command must be derived from real project evidence," never a filename guess.

## Design: two independent, additive strategies, never one replacing the other

### `discovery.py` - FastAPI route discovery

`_discover_fastapi_endpoints(context, root)` is gated behind a real, already-detected FastAPI framework fact (`project.frameworks`, Phase F's own detection, reused rather than re-implemented) - it never runs at all for a project where FastAPI was not really detected, so an unrelated Python project's own incidental `@something.get(...)`-shaped text (a real coincidence: modern Flask supports the identical decorator shorthand) is never mistaken for a real FastAPI route. When gated in, it walks every `.py` file under the project root (pruning `.venv`/`venv`/`__pycache__`/etc., the same "prune before descending" technique the Next.js strategy's own walk already uses) and regex-matches `@app.<method>(...)`/`@router.<method>(...)` decorators with a real, literal string path argument - never a full AST parse, matching the Next.js strategy's own "regex over text" convention. A route with an empty or non-literal path argument is skipped, never fabricated.

`discover_api_endpoints(context, root)` (the one public entry point) now runs both strategies and merges the results, deduplicated by `(method, path)` - a pure Next.js project's own output is provably unaffected (the FastAPI strategy contributes nothing when its own gate is closed), proven directly by the full, unmodified Next.js regression suite continuing to pass.

**`ApiEndpoint` gained one new, purely additive field: `line: Optional[int] = None`** - the real 1-based source line a FastAPI decorator was found on. The Next.js strategy never populates it (it finds a whole exported function, not one decorator line) - left honestly `None` there, never guessed.

### `server.py` - Python/FastAPI server-start discovery

`discover_server_start_command` tries the existing Node/npm strategy first, completely unchanged; only when no JS package manager is detected **and** FastAPI is really detected does it try the new Python strategy, `_discover_python_start_command`:

1. **`_find_uvicorn_run_entrypoint`** - checks common runner-script names (`run.py`, `main.py`, `app.py`, `asgi.py`, `wsgi.py`) plus every real entry point Project Discovery already found, and reads each one's real content looking for a literal `uvicorn.run(` call. The first real match wins. This is the direct fix for the audit's own finding: `app/main.py` is checked and correctly rejected (it has no such call); `run.py` is checked and correctly accepted.
2. **Module-target fallback** (`_find_fastapi_app_module`) - only tried when no runner script is found: scans the same already-known files for a real `<name> = FastAPI(...)` assignment, and derives a standard `uvicorn <module>:<app>` command (e.g. `app.main:app`) from the real file path and the real assigned variable name. A router's own `APIRouter(prefix=...)` composition and an app object in a file outside the already-known set are not resolved - real, documented scope boundaries, not silent gaps.

Both paths are checked by real file content, never assumed from a filename.

### Virtual environment awareness (`_select_interpreter`)

Before returning any Python command, the project's own `.venv`/`venv` interpreter (`Scripts\python.exe` on Windows, `bin/python` elsewhere) is preferred when one exists on disk; otherwise bare `python` is used, exactly the same graceful-degradation convention every other evidence-based lookup in this package already follows. The interpreter executable is always invoked directly, as the first element of a real argv list - no shell-activation script, no `activate.bat`/`source ... /activate`, matching this step's own explicit requirement.

### Dependency validation (`check_python_dependencies`)

Before `_discover_python_start_command` ever returns a command, it runs `<interpreter> -c "import fastapi, uvicorn"` - a fixed, literal probe string this project itself controls (never code read from or supplied by the target project, so this is not arbitrary execution), `shell=False`, direct interpreter invocation. A real import failure turns "found a command" into `(None, "found a Python/FastAPI startup command (...), but fastapi, uvicorn not importable via '<interpreter>': <the real ModuleNotFoundError>")` - a deterministic, specific failure the caller can act on, never a command that gets returned only to crash opaquely later. No installation is ever attempted - explicitly out of scope.

**A real, concrete illustration of exactly why this matters, found while testing this step, not invented for it:** on the machine this was built on, QA-Agent's own venv (`sys.executable`) genuinely lacks `fastapi`/`uvicorn` - but bare `python`, resolved fresh via a subprocess's own PATH search, happened to resolve to a *different*, unrelated per-user Python installation that *did* have them. Two different "python"s, two different answers, same machine. This is precisely the kind of ambiguity venv-preference plus an explicit, real dependency check is meant to resolve deterministically rather than leaving to chance.

## What was deliberately not attempted

`APIRouter(prefix=...)` composition (a router's own mount path is not resolved - a route declared `@router.get("/items")` is reported at `/items`, not e.g. `/api/items`, if some other file mounts that router under a prefix). Any `.py` file outside `project.entry_points`/`project.important_files` as a candidate for the module-target fallback (a real, bounded scope choice - not an unbounded second whole-repo walk). Dynamic path parameters remain represented as dynamic, never resolved to a concrete value (unchanged from Step 30 - `/api/users/{user_id}` is discovered, marked `dynamic=True`, and `run_api_qa`'s own existing, unmodified rule skips calling it automatically). No CLI flags changed - `--api-test`/`--api-diagnose`/`--api-repair` all pick up FastAPI support automatically, since `runner.py`/`ai_bridge.py`/`__main__.py` are completely untouched.

## Tests

`tests/regression/test_api_qa_fastapi.py` - **25 test functions, 49 checks**: FastAPI route discovery (GET/POST/router routes, dynamic-parameter preservation without invention, line-number tracking, gated-behind-real-detection, `.venv` directories ignored, Next.js discovery unaffected, and a fixture reproducing the real demo's exact 5-route set); server-start discovery (`run.py` with `uvicorn.run()` found and preferred, `app/main.py` correctly *not* selected just because of its name, the module-target fallback, JS-strategy priority preserved, missing-evidence reported honestly); interpreter selection (`.venv` preferred, plain `venv` recognized, graceful fallback with no venv, the project's own venv used end-to-end); dependency validation (available/missing/nonexistent-interpreter, and the missing-dependency case wired correctly into `discover_server_start_command`'s own return value); and one real, guarded, full end-to-end subprocess test (skipped cleanly when `fastapi`/`uvicorn` are not importable via this environment's own interpreter, mirroring `test_runtime_execution.py`'s own `_npm_available()` precedent).

## Real demo-project verification

```
python -m qa_agent discover "D:\Working\qa-agent-fastapi-demo" --api-test
```

No CLI, config, or orchestration change was needed for this to work - `--api-test` picked up FastAPI support automatically the moment `discovery.py`/`server.py` gained it.

```
Frameworks:
  - FastAPI  (evidence: requirements.txt)

API QA Results
--------------
  Server status: started (matched ready signal: 'INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)')
  Base URL:      http://127.0.0.1:8000
  Endpoints:     5 discovered

  GET /api/users                -> 200   (1136ms) PASS
  POST /api/users               -> 422   (2ms)    FAIL
        HTTP 422 response
        body: {"detail":[{"type":"missing","loc":["body"],"msg":"Field required","input":null}]}
  DELETE /api/users/{user_id}   -> -     (-)      SKIP  (dynamic route segment, never invented)
  GET /api/users/{user_id}      -> -     (-)      SKIP  (dynamic route segment, never invented)
  GET /health                   -> 200   (1ms)    PASS

  5 call(s) - 1 fail, 2 pass, 2 skipped.
```

Verified directly, in order:
1. **Project discovery identifies FastAPI** - `Frameworks: FastAPI (evidence: requirements.txt)`, confirmed.
2. **API discovery returns real FastAPI routes** - all 5 real routes found (`/health`, `GET`/`POST /api/users`, `GET`/`DELETE /api/users/{user_id}`), nothing invented, nothing missed.
3. **Server startup discovery finds a valid Python/uvicorn strategy** - `run.py` correctly selected (verified separately, directly: `command = ['D:\...\.venv\Scripts\python.exe', 'run.py']`), not `app/main.py`.
4. **The target project's `.venv` is preferred** - the exact venv interpreter path was used, verified directly.
5. **The server can actually be started** - real `uvicorn` process launched.
6. **The server becomes reachable** - `matched ready signal: 'INFO: Uvicorn running on http://127.0.0.1:8000...'`.
7. **`/health` can be reached** - real `200 OK`.
8. **`/api/users` can be reached** - real `200 OK` for GET (POST correctly gets a real `422` - it was called with no body, an existing, pre-Step-32, framework-agnostic `http_client.py` behavior, not a FastAPI-specific gap - see Known Limitations).
9. **`/api/users/{user_id}` remains dynamic** - both `GET` and `DELETE` on it were discovered and reported, never called, never given an invented id.

The server was confirmed genuinely stopped after every run in this step (`curl` to `127.0.0.1:8000` failed immediately once each process exited). The demo project's own intentional bug (`app/main.py:46`, `user.id` on a dict) was directly re-verified present and unmodified after this whole step.

## Files changed

`qa_agent/api_qa/discovery.py` (`_discover_nextjs_endpoints` - renamed only, logic unchanged; `_discover_fastapi_endpoints`/`discover_api_endpoints` - new/extended); `qa_agent/api_qa/server.py` (`_is_fastapi_project`, `_venv_python`, `_select_interpreter`, `_find_uvicorn_run_entrypoint`, `_find_fastapi_app_module`, `check_python_dependencies`, `_discover_python_start_command` - all new; `discover_server_start_command` - extended, JS-path behavior unchanged); `qa_agent/api_qa/models.py` (`ApiEndpoint.line` - new, optional field); `tests/regression/test_api_qa_fastapi.py` (new, 25 test functions, 49 checks); `docs/12-architecture.md`; `docs/step-log.md`; `docs/32-fastapi-discovery-and-startup.md` (this doc). **Untouched:** `qa_agent/api_qa/runner.py`, `http_client.py`, `render.py`, `ai_bridge.py`, `__init__.py`; `qa_agent/__main__.py`; every G1-G4 module; `qa_agent/agent/` (G5.1/G5.2); the Node/npm code paths in `discovery.py`/`server.py`, verified behavior-identical by the full pre-existing Next.js regression suite.

## Verified

`test_api_qa_fastapi.py` - 49/49 checks. `test_api_qa.py` (Step 30, Next.js) - 89/89, unchanged. `test_api_ai_bridge.py` (Step 31) - 71/71, unchanged. Full suite - **36/38** (the same two pre-existing, unrelated flakes as every prior step - `test_watch_pipeline.py`'s timing flake, `test_ai_openrouter.py`'s leftover-env-var flake).

## Known limitations

`POST`/`PUT`/`PATCH` calls are made with no request body (an existing, framework-agnostic `http_client.py` limitation from Step 30, not introduced or fixed here) - a real FastAPI `422` for a required-body endpoint is the correct, honest result of that, not a new gap. `APIRouter(prefix=...)` composition is not resolved. A `.py` file outside `project.entry_points`/`project.important_files` holding the real `FastAPI()` instantiation would not be found by the module-target fallback (bounded scope, not a whole-repo second walk). Dependency validation checks import-ability only (`fastapi`/`uvicorn` specifically) - it does not check version compatibility or any other package the target app itself might need at runtime.

**Answering directly: can QA-Agent now start the FastAPI demo and communicate with its discovered HTTP endpoints? YES** - shown above with real, live evidence: a real server start via the demo's own venv and `run.py`, a real "ready" signal match, a real base URL, and real HTTP 200/422 responses from real endpoints, including the two genuinely dynamic ones correctly left uncalled. No diagnosis, no repair, and no G5 wiring were implemented or attempted in this step.

**Status: Python/FastAPI Discovery and Server Startup CLOSED.**
