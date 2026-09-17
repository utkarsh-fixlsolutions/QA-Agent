# Status and Demo Readiness (2026-09-15)

**Purpose of this document:** a stakeholder-facing snapshot for the end-of-week (Friday
2026-09-19) demo — what the QA Agent actually does today, what approach it follows, what is
fully working and verified right now, and what concretely remains. Everything below is backed
by real test runs against this repository on 2026-09-15, not aspirational. See
`docs/step-log.md` for the full chronological engineering log this summarizes.

## The approach

The agent is being built API-first: point it at a real web project (Next.js, Express, or
FastAPI), and it works the way a QA engineer would —

1. **Discover** the real HTTP endpoints directly from source code (route files/decorators),
   never guessed or hard-coded.
2. **Start the project's own real dev server** (its actual `npm run dev` / `uvicorn` command,
   auto-detected), and wait until it is genuinely accepting connections — not just until its
   logs look ready.
3. **Resolve real inputs from real evidence** — e.g. call `GET /api/users` first, read a real
   `id` out of its real response, and only then call `GET /api/users/{id}` with that id. A
   required field with no safe, evidence-backed value is honestly `SKIPPED`, never invented.
4. **Execute real HTTP calls** against the running server and record the real result.
5. **Go beyond happy-path pass/fail**: also send deliberately invalid requests (negative
   tests) to confirm the server actually rejects bad input, and validate that successful
   responses actually match their own declared API schema.
6. **Report** results back — in the terminal, as CSV/HTML export, and in a browser UI with
   live progress — always showing real evidence (the actual response, the actual server log)
   behind every verdict, never an AI opinion presented as fact.

An AI layer (Groq/OpenRouter) sits on top of this as an *optional* explanation and
auto-repair assistant. It is not required for the pipeline above to work — deliberately, so
the demo does not depend on an API key or model availability.

## Completed and verified

**Framework/endpoint discovery** — Next.js App Router, Next.js Pages Router, Express, and
FastAPI, all from real source code, all gated on real framework-detection evidence (never
fires on a same-named folder/pattern alone). *(docs/30, 32, 38)*

**Real server lifecycle** — auto-detects each framework's real start command and working
directory, picks the right interpreter (including a real project `.venv`), waits for a real
"ready" signal, then confirms with a real TCP connect probe before any test call is sent (this
closed a real race condition found during dogfooding). Server is always stopped afterward, and
its own live console output is captured and shown on failure. *(docs/32, 37, 39)*

**Deterministic verification depth** — dynamic path parameters and request bodies resolved
only from real prior evidence (never fabricated); GET calls run first to gather evidence, then
dynamic GETs, then mutating calls (POST/PUT/PATCH/DELETE) last, in dependency order. *(docs/33)*

**Negative test cases** — for endpoints that already passed a positive call: strips a required
field from a valid request body and confirms it's correctly rejected; substitutes a
guaranteed-nonexistent id and confirms a correct 404/400 rather than a data leak. Flags a real
validation gap (accepted anyway) as HIGH severity, a crash (5xx instead of a clean rejection)
as CRITICAL.

**Response schema validation** — successful GET responses are checked against their own
declared OpenAPI response schema (required fields present, top-level types correct) without
changing the original call's pass/fail verdict — reported as separate, additional evidence.

**Severity + expected/actual** — every failing call now carries a deterministic severity
(CRITICAL/HIGH/MEDIUM/LOW, rule-based off status code — never AI-guessed) and a plain-English
"expected X, got Y" built from the real response.

**Live progress reporting** — the web UI now shows a real, live progress bar (endpoint N of
total, updating as each phase — primary calls, negative cases, schema checks — actually runs)
instead of a blank wait, via a background job + polling endpoint.

**Reporting/export** — CSV and self-contained HTML export, color-coded by outcome, including
server log tail on failure.

**Browser UI** — folder upload, static analysis + API results in one view, negative-tests
table, severity badges, schema-mismatch badges, expected/actual panels, live progress bar.

**Local deployment** — pinned dependencies, an unattended launch script bound to
`127.0.0.1:8000` only (the API-test path executes the uploaded project's own code — a trusted-
tool posture, not public-facing, by explicit design). *(docs/40)*

**AI layer (optional)** — runtime failure diagnosis, verified auto-repair with rollback, and an
autonomous plan → execute → diagnose → repair → report loop (G1–G5), independent of the
API-QA pipeline above and unaffected by anything built today.

**Test verification as of today:** the two newest feature sets (negative tests/schema
validation/severity, and live progress) pass 40/40 and 16/16 checks respectively, including
real end-to-end runs against a real Node server — not just mocks. The full regression +
integration suite (42 suites) passes 40/42, with the same two pre-existing, unrelated flakes
(`test_ai_openrouter.py`, `test_watch_pipeline.py`) that have shown up in every prior step's
log — i.e. today's work introduced zero regressions.

## In progress right now (built and passing, not yet committed)

The negative-tests/schema-validation/severity/expected-actual work and the live-progress bar
described above are **code-complete and fully verified** (see test counts above) but still sit
as uncommitted changes on `feature/phase-g`, and have not yet been added to `docs/step-log.md`
or given their own `docs/4x-*.md` write-up. This is the one piece of the project's own
documentation discipline that is currently behind the code, not ahead of it — closing it is the
first item below.

## What remains before Friday's demo

1. **Commit and document today's work** — step-log entry + the missing `docs/44-live-progress.md`
   (already referenced from code comments) + a short negative-tests/schema-validation doc,
   matching the convention every prior feature has followed.
2. **Pick and rehearse the demo project(s)** — at least one project per supported framework
   (Next.js, Express, FastAPI) with at least one real, intentional bug, so the demo shows a
   real FAIL/negative-test catch live, not just green checkmarks.
3. **Decide demo scope for the AI layer** — show it live (requires a configured `GROQ_API_KEY`)
   or explicitly present it as "available, optional" — not yet decided.
4. **No automated tests yet for `web/server.py` itself** (the engine underneath it is fully
   tested; the FastAPI layer is only manually verified) — acceptable for a demo, named here as
   a real gap rather than hidden.

## Known, explicit scope boundaries (not gaps to fix this week)

- Express/FastAPI router-mount-prefix resolution (`app.use('/api', router)`,
  `APIRouter(prefix=...)`) is not resolved yet.
- Negative-case generation covers two case types only (missing required field, nonexistent
  id) — no fuzzing/long-string/special-character generation yet.
- Response schema validation is top-level required-fields + primitive types only — no deep
  nested validation.
- No authentication/multi-tenant hardening — the tool is a trusted-user, on-premise tool by
  design (see docs/39, docs/40), not a public-facing service.

---

## Reusable prompt for keeping this document current

To regenerate/update this document in a fresh Claude Code session against this repo, use:

> Read `docs/step-log.md` in full, `git status`/`git diff --stat` for anything uncommitted,
> and the plan file (if one exists) for in-flight work. Then update
> `docs/41-status-and-demo-readiness.md`: (1) confirm every "completed" claim by actually
> running the relevant test file(s), not by trusting the log text alone; (2) list what changed
> since the last version of this doc; (3) give an honest, concrete "remaining before demo"
> list — no filler, no rounding partial work up to "done." Keep the same structure (Approach /
> Completed / In progress / Remaining / Scope boundaries) and keep every claim traceable to a
> file, doc, or test run.
