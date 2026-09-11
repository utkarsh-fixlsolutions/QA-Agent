# Step 22 — Phase G Part 2: Runtime Execution Engine

**Status:** IMPLEMENTED (2026-09-10).
**Phase:** G, Part 2 — the first phase in this whole project that actually launches real subprocesses. Everything through Phase G Part 1 was 100% read-only; this part is not.
**Scope:** one new public entry point, `run_runtime_plan(plan, root, configuration=None)`, executing a Phase G Part 1 `RuntimeQAPlan` for real and returning a `RuntimeExecutionResult`. Five executors implemented (Server Startup, Build Verification, Test Suite Verification, Environment Validation, Static Asset Verification); every other planned check returns `NOT_IMPLEMENTED`, deliberately.

## What "PASS" means here — read this before anything else

This engine never makes an HTTP request, opens a socket, or drives a browser — that boundary is explicit, permanent, and enforced by a source-grep isolation test. "Server Startup" succeeding means *the process stayed alive, without crashing, for its timeout window* — corroborated by a recognized "ready"-shaped log line when one appears, but a `PASS` on liveness alone when it doesn't. It never means "the server answers a real request." That is a materially different, weaker guarantee than the word "PASS" might suggest on its own, and is exactly why the reason text for a liveness-only pass says so explicitly rather than looking identical to a confirmed one. Real request-level verification is deliberately out of scope for this part.

## The two real conflicts resolved before writing any code (flagged to the user first)

**1. "Wait until startup succeeds" vs. "no HTTP" is a real tension**, not just apparent — resolved as above: liveness + optional log-pattern corroboration, never a network probe.

**2. Discovering a *real* command without touching Project Discovery.** Building/starting/testing a project needs a real, authored command (`package.json`'s `scripts.build`, say) that `ProjectKnowledge`/`RepositoryContext` never captured, and this phase's own DO-NOT-MODIFY list forbids touching Project Discovery, Repository Context, or the Runtime Planner. Resolved by having `executor.py` do its own small, scoped, read-only reads of `package.json`'s `scripts` section and `requirements.txt`/`pyproject.toml`'s content (for a real `pytest` dependency) — entirely inside `qa_agent/runtime/`, never extending or importing anything new from `qa_agent/project/`. Command discovery still only ever uses *real, authored evidence* — an npm script the developer wrote, or a Python entry-point file Part 1 already found and categorized. When no such evidence exists, the check is `SKIPPED` with the exact reason why; never a guessed command (`_discover_build_command` on a pure-Python project raises `CheckSkipped` outright — there is no universal Python "build" convention to guess at).

## Command discovery, concretely

| Check | Command source | Fallback |
|---|---|---|
| Server Startup | `package.json` `scripts.dev`/`scripts.start` (JS), or the single detected runtime entry point (`python <file>`), or `manage.py` → `python manage.py runserver --noreload` (Django-specific, avoids the reloader spawning a second child process) | `SKIPPED` if ambiguous (2+ entry points) or absent |
| Build Verification | `package.json` `scripts.build` only | `SKIPPED` for any non-JS stack — no universal Python build convention exists to guess at |
| Test Suite Verification | `package.json` `scripts.test`, or `pytest` if it's a real, declared dependency | `SKIPPED` otherwise — never defaults to running `pytest` just because a `tests/` directory exists |

Poetry/uv-managed Python projects get `poetry run`/`uv run` prefixed automatically (real package-manager evidence Part 1 already detected).

## Process management — the part that actually matters for safety

**A real Windows correctness issue, handled explicitly:** `Popen.terminate()` alone only signals the process this module directly launched. When that process is a shell wrapper (`shell=True`, needed for npm/pnpm/yarn/bun/poetry/uv's `.cmd` shims on Windows — the same reason `adapters.py`'s `ESLintAdapter` already needs `use_shell=True`), the *real* work (`node.exe`, `next.exe`, ...) is a grandchild `terminate()` never reaches, leaving it orphaned. Fixed with `taskkill /F /T /PID <pid>` on Windows (kills the whole tree) and `os.killpg` over a dedicated process group (`os.setsid`) on POSIX. Every execution path — ready, crash, or timeout — always calls this cleanup exactly once, in a `finally` block, before returning.

**Ready-detection mechanics:** a background thread reads the child's stdout/stderr (merged) line by line into a queue; the main thread polls it with a 200ms granularity against the overall timeout, matching lines against a small set of common "ready"-shaped phrases (`ready`, `listening`, `running on`, `compiled successfully`, `application startup complete`, ...) - a best-effort early exit, never the authoritative pass/fail signal (see above).

**A real bug found by dogfooding, not by inspection:** npm echoes a script's own command text before running it (`"> myapp@1.0 dev\n> next dev\n"`). A test fixture whose *script source itself* happened to contain the word "ready" (for test-authoring convenience) matched against that echo line instead of the real runtime output — burning the full timeout waiting for a "second" match that would never come, since the loop had already exited on the (wrong) first one. Fixed by excluding any line starting with `>` from ready-pattern matching, while still capturing it in the real log output.

## The five implemented executors

- **Server Startup** — discovers and launches the start command, watches for readiness or a crash, always terminates the process tree.
- **Build Verification** — discovers and runs the build command to real completion (or timeout), captures the real exit code and output.
- **Test Suite Verification** — same shape as Build Verification, for the discovered test command; does not parse framework-specific test output (a real, named non-goal - `pytest`'s and `jest`'s own result formats are structurally different, and parsing either is deferred, not attempted).
- **Environment Validation** — re-verifies (live, not from stale discovery-time data) that every previously-detected environment file still exists, and specifically flags the case where only an `.env.example` exists with no real `.env`/`.env.local` alongside it — the actually load-bearing gap for whether the app can even start. Never reads file contents.
- **Static Asset Verification** — re-verifies that every planned static-asset directory still exists. No file content inspection, no HTTP.

Every other planned check (14 of the 19 Phase G Part 1 can plan — API Endpoints, Middleware, Frontend Routes, CI, OpenAPI Availability, the Docker cluster, Server Component Rendering, the FastAPI cluster) returns `NOT_IMPLEMENTED` — verified directly by a real Docker-shaped fixture producing exactly that status for its container checks, not a silently-skipped or fabricated result.

## Execution rules, verified directly

One check's failure, timeout, or internal crash never stops the rest (`test_one_check_failing_never_stops_the_rest`) - each check runs inside its own `try/except`, mirroring `runner.py`'s own per-adapter isolation for the deterministic analyzer pipeline. A missing command on `PATH` is `SKIPPED`, never `ERROR` and never a fabricated `PASS`. An unexpected internal exception becomes `ERROR` with the real exception text preserved. Results preserve the plan's own `recommended_order` exactly. The same plan against the same disk state produces identical statuses and reasons across two separate runs.

## CLI

`python -m qa_agent discover <path> --execute-runtime-plan` (implies `--runtime-plan`, which implies `--context`) prints all four sections in order: the Part 1 report, the Part 2 context, the plan, then the execution results — nothing else. Without this flag, nothing this phase built is ever reached; `--runtime-plan` alone still only plans, never executes (verified directly).

## Files changed

**New:** `qa_agent/runtime/{executor,execution_models,execution_result,execution_render,execution_errors}.py`; `tests/regression/test_runtime_execution.py` (43 test functions, 89 checks); this document.
**Modified:** `qa_agent/runtime/__init__.py` (+exports, purely additive); `qa_agent/__main__.py` (+`--execute-runtime-plan` flag, +an accuracy fix to `discover`'s own top-level description - it could truthfully say "no subprocess" before this phase, and can no longer, unconditionally); `tests/regression/test_runtime_planner.py` (one isolation test rescoped from the whole `qa_agent/runtime/` directory to Part 1's own four files by name - it was never meant to forbid Part 2's own, deliberate subprocess use, only to keep Part 1's planning logic pure, and the directory-wide glob it used stopped being precise once Part 2 legitimately existed).
**Untouched, byte-for-byte:** `qa_agent/ai/`, the entire Phase E repair pipeline, `qa_agent/project/` in full (Project Discovery and Repository Context), `qa_agent/runtime/{models,rules,planner,render}.py` (the Runtime Planner itself), `runner.py`, `adapters.py`, `watch.py` - confirmed by a direct source-grep test plus rerunning the full suite before and after this part.

## Test results

`python tests/run_all.py` - **27/28 suites** (the one failure is `integration/test_watch_pipeline.py`'s pre-existing, unrelated timing flake). `test_runtime_execution.py` alone: 89/89 checks - covering both status/model contracts, process mechanics (`run_to_completion`/`run_until_ready_or_timeout` against real `sys.executable` subprocesses - success, real nonzero exit, real timeout-and-cleanup, the npm-echo false-positive fix above), command discovery for every documented source and every documented refusal, all five executors' pass/fail paths (including live re-verification catching a file/directory removed between planning and execution), orchestration guarantees (independence, ordering, determinism, `NOT_IMPLEMENTED`, missing-command handling, a genuinely broken check becoming `ERROR` not a crash), serialization, rendering, the CLI flag (all four sections, correct implication chain), isolation (no AI, no browser/HTTP import, Project Discovery/Repository Context/Runtime Planner untouched), and two real end-to-end npm-based tests (guarded, skipped cleanly if npm isn't on `PATH`, exactly like this project's existing `eslint_available()` precedent).

## Dogfooding, real output, real side effects

**This project's own repository:** Build Verification and Test Suite Verification both `SKIPPED` — correctly. (One honest wrinkle worth naming, not a bug: this repo's own detected package managers include `npm`, purely because a test fixture subdirectory - `tests/fixtures/eslint/` - has its own `package.json`. `_discover_build_command` correctly looked for a build script at the real project root, found none, and skipped - evidence-based and honest, if momentarily surprising given the repo is actually pure Python.)

**A real external Next.js project** (`D:\Major projects\lms-ai`), real `--execute-runtime-plan`-equivalent run, 137 real seconds:

```
[PASS] Server Startup (12.4s)   - real npm dev server launched, stayed alive, cleanly killed
[TIME] Build Verification (90.3s) - a real `next build` (with Sentry's production-compile
                                     step) genuinely did not finish inside the configured
                                     timeout - honestly reported as TIMEOUT, not a false PASS
[PASS] Environment Configuration
[PASS] Static Assets
[N/I ] API Endpoints, Frontend Routes, Middleware, CI, Server Component Rendering
[PASS] Test Suite Verification (34.7s) - a real `npm test` (Jest) ran to completion, exit 0
```

Confirmed directly afterward, via the OS process list: zero orphaned `node.exe` processes remained from this run, including after the Build Verification timeout deliberately killed a genuinely in-progress, substantial build - the process-tree-cleanup claim above is not theoretical.

## Bugs discovered

One real one, described above under "Process management" - the npm-echo false-positive in ready-pattern matching, found by dogfooding (a test fixture whose own script source happened to contain a matched word), fixed by excluding `>`-prefixed lines from matching.

## Deferred, per this phase's own explicit non-goals

Executors for the 14 remaining check types (API Endpoints, Middleware, Frontend Routes, CI, OpenAPI Availability, the Docker cluster, Server Component Rendering, the FastAPI cluster) - real, valuable, and each would need its own real verification strategy, deliberately not built here. Framework-specific test-output parsing (pytest vs. Jest result shapes). Any AI reasoning about a result. Any repair. Browser automation, HTTP-level API testing (Phase G Part 3's likely job, not this one's). Retry logic (`retryable` exists on `RuntimeCheckResult` as a field for a future phase to set meaningfully; nothing in this phase ever sets it `True`).

**Status: Phase G Part 2 CLOSED.**
