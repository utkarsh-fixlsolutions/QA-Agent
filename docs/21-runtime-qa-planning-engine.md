# Step 21 — Phase G Part 1: Runtime QA Planning Engine

**Status:** IMPLEMENTED (2026-09-10).
**Phase:** G, Part 1 — the first part of "Runtime QA," and the first phase to look beyond static code at all. Still not execution: this part decides *what should be tested and why*, never runs anything.
**Scope:** a new package, `qa_agent/runtime/`, exposing one public entry point, `plan_runtime_qa(context)`, converting a Phase F Part 2 `RepositoryContext` into a `RuntimeQAPlan` — an ordered, dependency-aware list of `RuntimeCheck`s. No HTTP, no subprocess, no browser, no AI, no filesystem walking of its own.

## The one thing to know before reading anything else in this document

**About half of this phase's own specifying prompt's example check categories are not implemented, on purpose, and this was flagged to the user before any code was written.** Database Connectivity, Prisma/migration verification, Authentication/Authorization specifics, WebSockets, Caching, Message Queues, Cron Jobs, Feature Flags, Rate Limiting, CORS, Security Headers, Session/Cookie Handling, and File Uploads have **no underlying evidence detector anywhere in `ProjectKnowledge`/`RepositoryContext`** (Phase F Parts 1-2). Building rules for them would mean inventing evidence that does not exist — a direct violation of the "never fabricate" rule this entire engine, and every phase before it, is built on. `test_planner_never_invents_a_database_or_auth_check` exists specifically to prove this: given a fixture whose `package.json` literally declares `prisma`, `next-auth`, and `pg` as dependencies, the planner produces zero database/auth/session/cookie/JWT/CORS/rate-limit/cache/queue-shaped checks, because nothing in Part 1/2 currently reads dependency names looking for those specific libraries. Extending Part 1's detectors to recognize known auth/ORM/infra libraries is real, valuable, separate work — deferred, not silently done here.

## What is implemented, and why each category was chosen

Every category below has a real, direct evidence path already produced by Part 1/2:

| Check(s) | Fires when | Evidence |
|---|---|---|
| Server Startup | a backend framework or a real runtime entry point exists | `runtime_files` / framework evidence |
| Environment Configuration | a real `.env`/`.env.example`-shaped file exists | `environment_files` |
| API Endpoints | an `api/` directory, Next.js `app/api` routes, or a backend framework exists | `important_directories` / `important_files` / framework evidence |
| Middleware | a root `middleware.ts`/`.js` file or a `middleware/` directory exists | `runtime_files` / `important_directories` |
| Frontend Routes | a frontend framework plus a `pages/`/`app/` directory exists | frameworks / `important_directories` |
| Static Assets | a `public`/`assets`/`static` directory exists | `important_directories` |
| Build Verification | a recognized build system, or a manifest plus a package manager, exists | `build_systems` / `configuration_files` |
| CI | a real CI workflow file exists | `ci_files` |
| OpenAPI Availability | a static `openapi.yaml`/`swagger.json` exists | `specification_files` |
| Test Suite Verification | a real test directory exists | `test_directories` |
| Container Build / Startup / Environment Validation (3 checks) | a `Dockerfile`/`docker-compose.yml` exists | `docker_files` |
| Server Component Rendering | Next.js **and** a real `app/` directory (App Router specifically, not Pages Router) | frameworks + `important_directories` |
| Health Endpoint / OpenAPI (auto-generated) / Dependency Injection / Startup Events / Shutdown Events (5 checks) | FastAPI detected | framework evidence |

19 distinct check types in total, verified individually against real fixtures.

## A real gap fixed along the way, found before implementation began

Next.js's actual middleware convention is a single root-level `middleware.ts` file — not a directory. Part 1's own `_RUNTIME_BASENAMES` never recognized it, which meant the Middleware check could never fire for a real Next.js project like `lms-ai` (which has exactly this file and no `middleware/` directory at all). Fixed by adding `middleware.ts`/`middleware.js` to that set — the same small, well-justified, cross-part consistency fix category as Part 2's `shared/`/`packages/`/`libs/`/`common/` directory-name addition. Confirmed live against `lms-ai` below.

## What "evidence" means here — one deliberate philosophical shift from Part 1/2

In `ProjectKnowledge`/`RepositoryContext`, evidence proves a fact is *true* ("React is used, because `package.json` says so"). Here, evidence proves a check is *worth planning* — not that the checked behavior already exists or works. "FastAPI detected via `requirements.txt`" is honest evidence that verifying a health endpoint is *relevant* to plan for this project; it is not a claim that a health endpoint already exists. This is the entire point of a planning engine — naming what should be verified before anything has run, exactly as a QA engineer would from reading a project's stack alone. Stated explicitly in `rules.py`'s own module docstring so it's never assumed away.

## Deterministic ordering

Every `RuntimeCheck` is assigned a `recommended_order` (a dense `1..N` sequence) computed from two things only: dependency depth (a check never appears before anything it depends on) and priority, with `id` as the final, always-available alphabetical tie-break — never insertion order, which would make the plan depend on which rule happened to run first. Verified directly: a dependency never appears after the check that needs it (`test_a_check_never_appears_before_its_own_dependency`), the order sequence has no gaps or repeats, and the identical input produces byte-identical output across two separate calls.

## Error contract, serialization, CLI

`plan_runtime_qa()` never raises (proven with a deliberately broken input object). `plan_to_dict()`/`plan_to_json()` produce a plain, JSON-safe structure, verified to round-trip losslessly; `render_markdown()` produces a real table. `python -m qa_agent discover <path> --runtime-plan` prints the Part 1 report, then the Part 2 `RepositoryContext`, then the plan, in that order — `--runtime-plan` implies `--context` so the evidence behind every planned check is always visible alongside it, never a bare list of checks with no way to see why.

## Files changed

**New:** `qa_agent/runtime/{__init__,models,rules,planner,render}.py`; `tests/regression/test_runtime_planner.py` (41 test functions, 85 checks); this document.
**Modified:** `qa_agent/project/detectors.py` (+`middleware.ts`/`.js` recognition, see above); `qa_agent/__main__.py` (+`--runtime-plan` flag on the `discover` subcommand only); `README.md`, `docs/12-architecture.md`, `docs/step-log.md`.
**Untouched, byte-for-byte:** `qa_agent/ai/`, the entire repair pipeline (Phase E), `runner.py`, `adapters.py`, `validator.py`, `decision.py`, `apply.py`, `watch.py` — confirmed by rerunning the full suite before and after this part.

## Test results

`python tests/run_all.py` — **26/27 suites** (the one failure is `integration/test_watch_pipeline.py`'s pre-existing, unrelated timing flake). `test_runtime_planner.py` alone: 85/85 checks. Every earlier suite, including Part 1's and Part 2's own, unaffected.

## Dogfooding, two real projects, real output

**This project's own repository:** correctly plans only `Build Verification` and `Test Suite Verification` — accurate; qa_agent has no backend-framework-shaped entry point, no Docker, no CI, exactly matching Part 2's own dogfooding of the same repository.

**A real external Next.js project** (`D:\Major projects\lms-ai`) — real output, 10 checks planned:

```
[1] Server Startup           (constants/index.ts, middleware.ts)
[2] Build Verification       (next.config.ts, package.json, tsconfig.json, ...)
[3] Environment Configuration (.env.local, .env.sentry-build-plugin)
[4] Static Assets            (public)
[5] API Endpoints            (an api/ directory exists)              depends on: server-startup
[6] Frontend Routes          (Next.js, React; route dir: app)        depends on: server-startup
[7] Middleware               (middleware.ts)                         depends on: server-startup
[8] CI                       (.github/workflows/ci.yml, ...)         depends on: build-verification
[9] Server Component Rendering (Next.js App Router: app/ present)    depends on: server-startup
[10] Test Suite Verification (components/__tests__, lib/__tests__, ...) depends on: build-verification
```

This is the direct, concrete realization of the very first thing discussed in this whole project's Phase F/G planning conversation — a Next.js app's App Router, its API routes, and its middleware, each correctly identified with real evidence and a correct dependency order. Not executed — planned, honestly, only where real evidence exists.

## Bugs discovered

One, described above — the missing `middleware.ts`/`.js` recognition in Part 1's `_RUNTIME_BASENAMES`, found and fixed before implementation began (flagged to the user ahead of the work, per their explicit instruction), not discovered mid-build.

## Deferred, per this phase's own explicit non-goals and the evidence gap named above

Everything this phase's own specifying prompt named but has no evidence source for today (database/ORM/auth/session/cookie/JWT/WebSocket/cache/queue/cron/feature-flag/rate-limit/CORS/security-header checks) — real, valuable, and explicitly deferred pending new Part 1 evidence detectors, not silently dropped. Also out of scope, per this phase's own non-goals: any actual execution of a planned check (Phase G Part 2 or later), API/middleware/browser testing itself, repair, autonomous workflows.

**Status: Phase G Part 1 CLOSED.**
