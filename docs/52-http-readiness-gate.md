# Step 52 — HTTP Readiness Gate (Phase 1: Reliable Server Startup + Environment Readiness Gate)

**Status:** IMPLEMENTED (2026-09-17).
**Goal:** a strict, user-specified milestone - the agent must autonomously start the target app, determine where it is actually listening, verify it is really reachable, and only then allow API testing. No unrelated feature work.

## Audit finding (read before assuming anything was broken)

The specific 5-step failure the user described (matched a "ready" log → guessed port → failed TCP probe → **still executed API calls**) does **not** reproduce in the code as it stood before this step - it was already fixed by two prior changes, confirmed by reading `runner.py` directly, not assumed:

- docs/46 (ready-signal false-positive fix): a bare keyword match alone never ends the startup wait early - only a real observed URL/port does.
- docs/48 (fail-fast + classification): `run_api_qa` already calls `wait_until_connectable` after resolving `base_url`, and on failure returns `SERVER_UNREACHABLE` immediately with every endpoint `CALL_SKIPPED` for one clear reason - `resolve_and_execute` (the real HTTP calls) is never reached. This exact path was already covered by a real regression test, `test_run_api_qa_stops_fast_when_the_port_never_binds`.

What was genuinely missing, confirmed by direct code search (zero matches for "health"/"readiness" anywhere in `api_qa` before this step):

1. **No HTTP-level readiness check existed at all** - only a raw TCP connect. Something that binds a port but never actually speaks HTTP (wrong protocol, a stray unrelated process, a placeholder listener) would have passed the old gate and gone on to real API execution, producing confusing per-call HTTP failures instead of one honest environment failure.
2. `_resolve_base_url`'s own real-vs-guessed precedence had no direct unit test.
3. The unreachable-run's reason text didn't name the exact command/cwd/timeout used.

## Design

**`qa_agent/api_qa/server.py`** (new): `check_http_readiness(base_url, endpoints=(), timeout=5.0)` - one real GET against the first candidate path that answers *at all*. Candidates, in order: a real, already-discovered endpoint whose own path looks like a health/liveness check (`"health"`/`"keep-alive"` substring - reused evidence, never invented), then `/health`, `/api/health`, `/`. Deliberately lenient: any real HTTP response, including a 404/500 (`urllib.error.HTTPError`), counts as `reached=True` - a real application-level error proves the server is genuinely answering and must never be confused with it being unreachable. Only a true connection-level failure on every candidate (`URLError`/`OSError`) counts as `reached=False`. Never part of `resolve_and_execute`'s own real test calls - a separate, clearly-scoped probe.

**`qa_agent/api_qa/runner.py`**: new `ApiQaConfig.http_readiness_timeout` (default 5s, independent of `connect_probe_timeout`). `run_api_qa` now calls `check_http_readiness` immediately after the existing TCP gate succeeds; `reached=False` stops the run exactly like a TCP failure - `SERVER_UNREACHABLE`, every endpoint `CALL_SKIPPED`, one honest reason naming that TCP *did* succeed (so it's never confused with a plain connection-refused). Both failure reasons (TCP and HTTP) now name the real command, cwd, and timeout used. `ApiTestResult.http_readiness_detail` (new, additive) carries the readiness evidence on both the blocked and the successful path.

## Verified

**Unit/integration (`tests/regression/test_api_qa.py`, +25 checks, 191/191 total in that file):** `check_http_readiness` reaches on a real 200, reaches on a real 404 (never confused with unreachable), does not reach when nothing is listening, prefers a real already-discovered health-shaped endpoint; `_resolve_base_url`'s real-vs-guessed precedence, directly, for the first time; two new full-stack `run_api_qa` tests - one proving the run proceeds when the readiness probe only ever gets a 404, one proving the run blocks when something really binds the port but never speaks HTTP at all (the one scenario the old TCP-only gate could not catch); one more case distinguishing "no ready signal at all" from "matched ready signal but never bound" (both already-real branches, now both directly tested).

**Real end-to-end, no unit tests substituted:** a tiny, real, controlled demo app (Node `http` server standing in for `next dev`, two real routes) run via `python -m qa_agent discover <path> --api-test`, never started manually:
- Success: server started, real port `4700` observed, both endpoints called and passed.
- Negative #1 (ready log printed, port never bound): `unreachable`, 0 real calls, one honest reason.
- Negative #2 (real TCP listener that never speaks HTTP - the genuinely new gate): `unreachable`, TCP explicitly named as having succeeded, 0 real calls, distinct reason from #1.

`python tests/run_all.py --quick`: 43/45 before and after (same 2 pre-existing, unrelated flakes - `test_ai_openrouter.py`, `test_watch_pipeline.py`). Zero regressions.

## Explicitly not done (Phase 1 scope only)

- No config-provided/declared health endpoint (e.g. a custom package.json field, Docker/K8s healthcheck) - only heuristic path-name matching against already-discovered routes.
- No live demo walkthrough for the Python/FastAPI startup path specifically (it shares the same gate code and is covered by the existing 66/66 FastAPI regression suite, but section 11's manual walkthrough was done for the JS path only, per this project's own MERN-first priority).
- Synthetic data, API contract discovery, AI diagnosis/repair, UI, memory, regression-suite work: untouched, as explicitly scoped out.
