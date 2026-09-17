# Step 31 — API QA -> G3 Diagnosis -> G4 Verified Repair Bridge

**Status:** IMPLEMENTED (2026-09-12). **Extended same day** with a source-code evidence upgrade (see "Evidence upgrade" below) after real dogfooding showed diagnosis quality was too weak - both real models tried either named no concrete file, or failed grounding, because they were never actually given the failing route's own source code.
**Goal:** connect API QA v1's real failing `ApiCallResult`s to the existing, unmodified G3 diagnosis and G4 verified-repair pipeline - no new diagnosis engine, no new repair engine, no full G5 loop. See docs/30-api-qa-v1.md for API QA v1 itself.

## The flow

```
API Test (run_api_qa, unchanged)
   -> a real failure found (ApiCallResult, status=fail)
   -> [--api-diagnose] diagnose_api_failure()  -> G3's diagnose_runtime_failure(), unmodified
   -> [--api-repair]   repair_api_failure()    -> G4's repair_runtime_failure(), unmodified, runtime_check=None
   -> [a real write happened?] -> re-call the same real endpoint (api_qa's own server/http_client)
   -> report: API QA Results -> AI Diagnosis -> Repair Attempt -> Re-test
```

## The one new file: `qa_agent/api_qa/ai_bridge.py`

The one, narrow, explicit place in `qa_agent/api_qa/` that imports `qa_agent.ai` (G3/G4) and `qa_agent.runtime` (for the one shape they already read) - the same "one file crosses the boundary, everything else stays exactly as it was" precedent `qa_agent/agent/executor.py` already established for G5.2. `discovery.py`/`server.py`/`http_client.py`/`runner.py`/`models.py`/`render.py` are completely untouched - `run_api_qa`'s own module docstring still says "No AI", and it is still true; `--api-test` alone behaves byte-for-byte as it did in Step 30.

**The adapter (`_build_check_result`):** a real, failing `ApiCallResult` becomes a real `RuntimeCheckResult` (Phase G Part 2's own type, imported unmodified, not a lookalike shim) - `status="fail"`, `reason`/`exception` from the real call, and `.logs`/`.details` built from real evidence only (status code, content-type, JSON validity, a bounded response sample, and critically the endpoint's real source file path - included explicitly so a diagnosis naming that file as "affected" can actually be grounded). This is the one and only new "translation" logic in this whole step; `diagnose_runtime_failure`/`repair_runtime_failure` themselves are called completely unmodified once handed this real object.

**Why `runtime_check=None` for every call into G4, stated plainly because it changes what G4's own outcome can mean here:** G4's own pre-apply candidate-runtime-check and post-apply re-verification (the mechanism that can produce `OUTCOME_VERIFIED`) both require a real, plannable `RuntimeCheck` to re-run via `run_runtime_plan` - there is no such thing as "the planned check for one specific HTTP endpoint" (the planning engine's own `api-endpoints` check exists but has no executor at all, and re-running it says nothing about one particular endpoint). Every use of G4's own `execution_result` argument is already conditioned on `runtime_check is not None`, so passing `None` for both makes G4 safely skip that machinery entirely and fall back to `decide_repair()`'s own static-analysis verdict (Phase E Part 4, called unmodified) alone for ACCEPT/REJECT - proven directly: the one real, full happy-path test below reaches `OUTCOME_APPLIED`, and asserting `OUTCOME_VERIFIED` never fires for any call this bridge makes is implicit in that same test never seeing it. `execution_result` itself is passed as `None`, not a hand-built stand-in - every one of its own uses inside G4 is already gated, so it is provably never dereferenced by any path this bridge takes; a future change to G4 that broke that assumption would raise there, which `repair_runtime_failure`'s own top-level `except Exception` already turns into a safe `OUTCOME_ERROR`, not a crash.

**A real, honest consequence of the above, worth stating directly:** because no runtime-candidate override is available, this bridge's only path to a real write is a genuine **static-analysis improvement** in the target file (`decide_repair()`'s own `improved` verdict, `introduced_count == 0`) - a materially different, narrower bar than a build/test/server-startup repair gets (which can be accepted purely on a passing real runtime re-check even when static analysis shows no change). This is not a weakening of anything - it is what G4's own decision matrix already does when no runtime-candidate signal exists, unmodified - but it means an API repair whose fix is real but produces no measurable static-analysis delta (a runtime-only behavior change) will `HOLD`, not `APPLY`, in this integration. Named here, not glossed over.

**The real, final "was it fixed?" answer is this bridge's own, never G4's:** since G4's own verification is structurally unavailable here, the actual re-test - restarting the real dev server (already stopped after the original test) and making one more real HTTP call against the same endpoint - is `repair_api_failure`'s own job, reusing `api_qa.server`/`api_qa.http_client` exactly as `runner.run_api_qa` already does for the very first call. This is never presented as G4's own `VERIFIED`; it is a distinct, explicitly-labeled "Re-test" result, always restarted and always stopped again afterward, matching this project's "never leave a real process running" guarantee everywhere else it already applies.

## Evidence upgrade: the route's real source code

**Why diagnosis was too weak, found by inspecting `_build_check_result` directly:** the adapter only ever put the endpoint's `source_file` *path string* into evidence - never the file's actual content. A model has no way to recognize "this route unconditionally throws" or "this is a deliberate Sentry demo route" from a file path alone; it can only guess, and this project's own grounding check correctly rejects guesses.

**The fix (`_read_source_excerpt(root, source_file)`, new in `ai_bridge.py`):** the real file at `root/source_file` is read (capped at 200KB raw, refusing - not partially reading - anything larger), then bounded to 2000 characters with a clear, explicit truncation marker when longer, and appended into `_build_check_result`'s own `.logs` as a clearly delimited `Source file content (...):` block. Two independent bounds, not one: the per-excerpt cap keeps one huge file from dominating the evidence budget before `diagnosis_prompts.build_diagnosis_prompt`'s own existing `_bounded_logs` (redaction + a second, whole-payload truncation) even runs. This is the key design win of this whole upgrade: because the excerpt flows through `.logs` exactly like any other evidence line, it automatically gets the **existing** secret-redaction pass for free - no new redaction logic was written or needed. When no root is given, or the file cannot be read, this is stated as an explicit "not available" line - never silently omitted, matching this project's evidence-honesty discipline everywhere else.

`root` was threaded through as a new, optional (`root=None`, graceful degradation) parameter on `diagnose_api_failure`/`repair_api_failure`/`_build_check_result` - `diagnose_and_repair_api_failures` (the orchestration entry point, already receiving `root`) now passes it through automatically; `__main__.py`'s own CLI wiring needed no change at all.

**The prompt upgrade (`DIAGNOSIS_GUARDRAILS`, `qa_agent/ai/diagnosis_prompts.py`):** one new, purely additive clause (the existing sentences are untouched - the same technique the AI Behavior Contract step already established, verified the same way: no test anywhere asserts exact prompt-string equality) telling the model to prefer a precise explanation grounded in real source code over a vague one when that code is present, to name the exact construct responsible (a class, an unconditional throw, an explicit demo/test comment), and to never name a file that does not literally appear in the evidence. This is shared, unmodified prompt infrastructure (`build_diagnosis_prompt` serves every G3 diagnosis call, not only API failures) - deliberately extended rather than forked, per this task's own "prefer extending the bridge / evidence assembly rather than rewriting diagnosis core" instruction; the new guidance is safe and beneficial for every existing caller (build/test/server-startup diagnosis too), not API-specific in a way that would require a second prompt builder.

**Grounding safety is unchanged, and is what makes the new evidence safe to add:** `response_is_grounded` (`qa_agent/ai/diagnosis_parser.py`) was not touched - a claim still must appear, verbatim, somewhere in the real evidence. Adding source code only *enlarges the space of true things the evidence can support*; it does not relax what counts as grounded. Proven directly, not just argued: a dedicated test shows the identical AI claim (`affected_components=["SentryExampleAPIError"]`) is rejected as ungrounded when the source file does not exist, and accepted once it does - and a second test shows a claim absent from the source too is still rejected exactly as before.

## Safety rules, enforced structurally (defense in depth, matching this project's established G5.2 precedent)

A repair is only ever attempted when `do_repair` was requested **and** a diagnosis actually completed with `diagnosis_status == DIAGNOSIS_DIAGNOSED` - enforced once in `diagnose_and_repair_api_failures` (the orchestration entry point) and enforced again, independently, by G4's own `check_repair_eligibility` (unmodified) - so even a caller that bypassed the orchestration function entirely still cannot repair an undiagnosed or incompletely-diagnosed failure. A real write is only ever attempted inside G4's own existing temporary-workspace/static-regression-gate/atomic-apply machinery - nothing here writes to a file directly. Every AI failure mode (offline provider, malformed response, an ungrounded/hallucinated claim) produces a structured, honestly-labeled outcome, never a crash and never a fabricated success - proven directly, not just assumed, during real dogfooding below.

## CLI

```
python -m qa_agent discover <path> --api-test                                 # unchanged from Step 30
python -m qa_agent discover <path> --api-test --api-diagnose                  # + G3 diagnosis on real failures
python -m qa_agent discover <path> --api-test --api-diagnose --api-repair     # + G4 verified repair + re-test
```

`--api-diagnose` implies `--api-test`; `--api-repair` implies `--api-diagnose`. Reuses the existing `--ai-provider`/`--ai-model` flags unchanged (no new provider surface). Zero AI calls without `--api-diagnose`/`--api-repair` - proven by a dedicated call-counting test.

## Tests

`tests/regression/test_api_ai_bridge.py` - **29 test functions, 71 checks**: the adapter (real evidence mapped correctly, a connection failure with no status code handled honestly); the new source-code evidence layer (`_read_source_excerpt` returns real content / `None` for a missing file or no root / truncates a large file with a clear marker; `_build_check_result` includes the real content when `root` is given and honestly notes "not available" when it is not or the file is missing; the real content and file path both reach the actual built prompt text - not just an internal data structure); `diagnose_api_failure` (a grounded success, an ungrounded/hallucinated claim correctly rejected, **the identical AI claim rejected without the source file present and accepted once it exists** - the concrete before/after proof this upgrade exists to deliver, a claim absent from the source too still correctly rejected, a passing call short-circuits with zero provider calls, provider failure, malformed response); `repair_api_failure` (no affected file / nonexistent file -> `NOT_ELIGIBLE`, an offline provider -> no write, and **one full real happy-path test**: a real `npm run dev` Node server that genuinely returns 500, a real G3-shaped diagnosis, a scripted repair proposal, a real static-analysis-improvement rerun via an injected fake `run`, a real file patch, a real server restart, and a real re-test HTTP call that genuinely now returns 200); the orchestration function's safety rules; rendering; and CLI wiring (`--api-test` alone unchanged, `--api-diagnose` shows AI Diagnosis only, `--api-repair` wires through without crashing).

## Dogfooding: `D:\Major projects\lms-ai`, the known `GET /api/sentry-example-api` -> 500 failure

### Before this evidence upgrade (previous step's own real results, for direct comparison)

```
AI Diagnosis:
  Summary: The runtime executor determined that the HTTP request to /api/sentry-example-api
           failed with status 500, indicating a server error.
  Confidence: 0.95
  Affected components: api/sentry-example-api
  Recommended action: Investigate the application's logs and server logs to identify the
           root cause of the 500 error and fix the affected components.

Runtime Repair: not attempted
  Reason: diagnosis names no affected file
```

A real, DIAGNOSED result - but the model named only a vague **component**, never a concrete **file**, so G4's own eligibility gate correctly refused to guess a repair target.

### After this evidence upgrade, same real endpoint, same real local model (`qwen2.5:0.5b`)

Run three times in this session (a real 0.5B model's own well-documented, pre-existing reliability variance - not something this step changes or should try to hide): **one real, clean success**, and two real, honest failures (a schema-invalid response once, one ungrounded-claim rejection once) - reported exactly as they happened, not cherry-picked:

```
AI Diagnosis:
  Summary: An HTTP 500 error was returned from the API endpoint /api/sentry-example-api.
           This likely indicates a server-side issue with the application code or the
           backend service.
  Confidence: 0.90
  Evidence: The source code shows a class called `SentryExampleAPIError` that extends
           the `Error` class.; The code shows an unconditional throw error and a handler
           that always fails regardless of input.; The application code uses a route to
           test Sentry's error monitoring, and the error is thrown at the backend.
  Affected files: app/api/sentry-example-api/route.ts
  Affected components: app/api/sentry-example-api/route.ts
  Recommended action: Investigate the source code for any potential bugs or issues that
           could be causing the error.
```

**A genuine, direct improvement, the same tiny model, the same real endpoint:** it now names a real, concrete **file** (`app/api/sentry-example-api/route.ts` - previously only a vague component string), and its own evidence explicitly recognizes the unconditional throw and that this is a Sentry error-testing route - exactly what the code shows, not a guess. Re-running with `--api-repair` immediately afterward hit one of this same model's own two other real, honest failure modes (an ungrounded claim, correctly rejected) - a genuine repair was not reached in this session, an accurate reflection of a 0.5B model's real reliability, not a claim this step fixed model reliability itself (it did not try to, and should not).

### The real OpenRouter cloud provider (`poolside/laguna-s-2.1:free`)

Rate-limited (`HTTP 429`) on every real attempt this session (three tries, including one retry after a 20s backoff) - a real, honest free-tier constraint, not a diagnosis-quality result either way. Reported exactly as it happened; not retried indefinitely to manufacture a different outcome.

**Every real result above was reported honestly, none fabricated.** The dev server and the Ollama server this session started for testing were both confirmed genuinely stopped afterward.

## Files changed

`qa_agent/api_qa/ai_bridge.py` (extended: `_read_source_excerpt`, `_build_check_result`/`diagnose_api_failure`/`repair_api_failure` now accept and use a real `root`); `qa_agent/ai/diagnosis_prompts.py` (`DIAGNOSIS_GUARDRAILS` - one new, purely additive clause); `qa_agent/api_qa/__init__.py` (+exports, unchanged this extension); `qa_agent/__main__.py` (unchanged this extension - `root` was already threaded through); `tests/regression/test_api_ai_bridge.py` (+10 test functions, 71 checks total); `docs/12-architecture.md`; `docs/step-log.md`; `docs/31-api-qa-ai-bridge.md` (this doc, extended). **Untouched:** every G1-G4 module beyond the one additive prompt clause, `qa_agent/agent/` (G5.1/G5.2), `qa_agent/api_qa/discovery.py`/`server.py`/`http_client.py`/`runner.py`/`models.py`/`render.py` (Step 30's own files), `response_is_grounded` (grounding logic itself, not touched - only the evidence it checks against grew), the production default provider/model.

## Verified

`test_api_ai_bridge.py` - **71/71 checks** (up from 54 before this extension), including one real, real-subprocess, real-HTTP, real-file-write, real-re-test happy path, and the direct before/after grounding proof described above. Full suite - **35/37** (the same two pre-existing, unrelated flakes as every prior step).

## Known limitations

No separate server-log capture is fed into diagnosis for an individual API call - only the real HTTP request/response evidence plus the route's own real source code. Adding continuous server-output capture across the whole API-test session remains out of scope (would mean extending `server.py`'s already-shipped process lifecycle); named explicitly. The static-analysis-only acceptance bar for API repairs means a real runtime-only fix with no static-analysis delta will `HOLD`, not `APPLY`. Repair remains bounded to one attempt per failure. **A real 0.5B local model's own reliability is not something this step changes or claims to fix** - the evidence/prompt upgrade measurably improves diagnosis *quality when the model succeeds*, proven directly by the before/after dogfooding above, but a tiny model can still fail to produce valid JSON, or make an ungrounded claim, on any given real call; larger/more capable models are expected to benefit more consistently, not tested exhaustively here. The source-excerpt cap (2000 characters) means a very large route handler file will be head-truncated - acceptable for this project's own real evidence (lms-ai's real route files are well under 1KB) but a real, named bound, not unlimited.

**Status: API QA -> G3/G4 Bridge CLOSED. No G5 loop, no Express support, added in this step.**
