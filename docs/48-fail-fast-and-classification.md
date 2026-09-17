# Step 48 — Fail-Fast on Unreachable Server + Four-Label Classification

**Status:** IMPLEMENTED (2026-09-16).
**Goal:** two explicitly time-boxed, user-requested changes only, each finished completely: (1) stop the whole run immediately, honestly, when the server never becomes reachable, instead of proceeding to call every endpoint against it; (2) show every endpoint under exactly one of four final labels - Working / Failing / Not Working / Skipped.

## 1. Fail-fast on unreachable server

**Before:** when the real TCP connect-probe (docs/39, docs/46) never succeeded, `run_api_qa` logged a warning and proceeded anyway - every endpoint then failed with a real, honest, but uninformative "connection refused," indistinguishable at a glance from a genuinely broken API.

**Now:** the run stops immediately at that point. Every endpoint is `SKIPPED` with one clear, shared reason, and a new server status, `SERVER_UNREACHABLE` (`"unreachable"`, distinct from `SERVER_START_FAILED`/`SERVER_CRASHED` - the process really did start and is still running, it just never accepted a real connection), replaces `SERVER_STARTED`. The message leads with the exact requested text:

> "Server is not reachable. Stopping the run. The process \<matched a ready signal / stayed running without a recognized ready signal\> but never accepted a real TCP connection on \<url\> (\<a real port actually observed.../an unconfirmed guess...\>) within \<N\>s."

Still built entirely from real, already-known facts (docs/46) - never a fixed sentence. No new HTTP call, no wasted per-endpoint timeout wait - `_skip_all` (already used for `SERVER_START_FAILED`/`SERVER_CRASHED`) is reused unchanged.

## 2. Four-label classification

New pure function, `qa_agent/api_qa/analysis.py`'s `classify_call_outcome(call)`, returning exactly one of `CLASSIFICATION_WORKING` / `CLASSIFICATION_FAILING` / `CLASSIFICATION_NOT_WORKING` / `CLASSIFICATION_SKIPPED` (the literal strings `"Working"`/`"Failing"`/`"Not Working"`/`"Skipped"`):

- **Skipped** - `CALL_SKIPPED`, never actually attempted.
- **Working** - `CALL_PASS` (a real 2xx, valid body when JSON was declared).
- **Not Working** - `CALL_FAIL` with no response at all (`status_code is None`) or a real 5xx - unreachable/crashed, the same real-world distinction `classify_call_severity`'s own CRITICAL bucket already draws.
- **Failing** - `CALL_FAIL` with any other real status (4xx, unexpected 3xx, or a declared-JSON body that didn't parse) - reachable, but wrong.

Wired everywhere a call is already shown: `_call_to_dict`/`to_dict()` gain a `"classification"` key (and `to_dict()`'s own `"summary"` block gains a `"classification"` count breakdown, via new `_classification_counts`); the CLI's per-call line now shows the classification label in place of the old bare PASS/FAIL/SKIP symbol, plus a new summary line (`Classification: N Working, N Failing, N Not Working, N Skipped`); CSV gains a `classification` column (right after `path`); HTML export gains a dedicated `Classification` column; the web UI's result cards and Test Plan result cells now show the classification label instead of the old PASS/FAIL/SKIP badge text (same color, `to_dict()`'s own `classification` field consumed directly, nothing recomputed client-side).

## Explicitly not done (per the user's own explicit scope for this step)

No other roadmap stage touched. No test-data cleanup, no "Understand Context," no auth support, no results history - all deliberately deferred, per instruction, to a later step.

## Tests

`tests/regression/test_api_qa.py`: `test_run_api_qa_stops_fast_when_the_port_never_binds` (replaces the old "warns, then proceeds" test - proves the new `SERVER_UNREACHABLE` status, the exact stop message, and that no real HTTP call is ever attempted); `test_run_api_qa_dynamic_routes_are_never_called` updated to use a real, actually-listening Node server (its old fixture relied on the removed "proceed anyway" behavior to reach the code path it was actually testing); CLI-text and CSV-header assertions updated for the new classification label/column (`"PASS"`/`"FAIL"` substring checks replaced with `"Working"`/`"Not Working"`).

**Verified:** `test_api_qa.py` - 159/159. `test_api_qa_synthetic_mutations.py` - 81/81, `test_api_qa_negative_and_schema.py` - 40/40, `test_api_qa_fastapi.py` - 66/66, `test_api_qa_progress.py` - 16/16, `test_api_qa_resolution.py` - 70/70, `test_api_ai_bridge.py` - 71/71, all unchanged. Full suite run pending completion at time of writing.
