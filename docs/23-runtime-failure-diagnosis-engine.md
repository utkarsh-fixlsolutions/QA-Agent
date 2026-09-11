# Step 23 — Phase G Part 3: AI Runtime Failure Diagnosis Engine

**Status:** IMPLEMENTED (2026-09-11).
**Phase:** G, Part 3 — the first real AI reasoning layer for runtime QA. Interprets *why* a Phase G Part 2 check failed; never decides *whether* it failed.
**Scope:** two new public entry points in `qa_agent/ai/` — `diagnose_runtime_failure(check_result, repository_context, provider, runtime_check=None)` and `diagnose_runtime_failures(execution_result, repository_context, provider)` — converting a real `RuntimeCheckResult`/`RuntimeExecutionResult` into a structured, grounded `RuntimeDiagnosis`. AI is opt-in (`discover --diagnose`); off by default, zero AI calls without it.

## Where this lives, and why that was the one thing to get right first

The first draft of this prompt asked for `qa_agent/runtime/diagnosis.py`. That would have broken an existing, tested guarantee: `qa_agent/runtime/`'s entire identity, since Phase G Part 1, is "deterministic only, no AI" — enforced by source-grep isolation tests in both `test_runtime_planner.py` and `test_runtime_execution.py`. The corrected prompt fixed this explicitly (a "CRITICAL PACKAGE BOUNDARY" section), and everything here lives in `qa_agent/ai/` instead — the one package every AI-calling module in this project already lives in. The precedent for reading the deterministic side's output as input already existed: `validator.py` (Phase E Part 3) is documented as "the one exception to 'nothing in this package imports the deterministic engine, only the other way around'" — it reads `runner.run`'s real output. This phase is a second instance of that exact same, already-accepted pattern, not a new architectural exception.

## Core principle, and how it's actually enforced, not just stated

The deterministic `RuntimeCheckResult.status` (Phase G Part 2) remains the sole authority on pass/fail. Concretely: `diagnose_runtime_failure` only ever calls the AI for a check whose real status is `fail`/`timeout`/`error` — anything else (`pass`/`skipped`/`not_implemented`) returns `NOT_APPLICABLE` with **zero provider calls**, verified directly with a call-counting fake provider, not just an output-shape assertion. Nothing here ever writes to a `RuntimeCheckResult` or a `RuntimeExecutionResult` — verified directly (`test_deterministic_execution_result_is_never_mutated`: every field, byte-identical, before and after diagnosis).

## Grounding: the most important requirement, implemented structurally

Every `RuntimeDiagnosis` that claims a specific `affected_files`/`affected_components` value is checked against the real evidence it was actually given — the check's own captured logs/reason/exception, the originating `RuntimeCheck`'s `required_evidence` (when known), and `RepositoryContext`'s own known languages/frameworks/files/directories. A claim naming something that appears nowhere in that real input is rejected outright (`INVALID_RESPONSE`), never silently accepted. Deliberately structural, not a natural-language fact-checker, per this phase's own explicit instruction not to build one — a substring presence check, not linguistic reasoning.

**One honest limitation of this approach, found by real dogfooding, not by inspection:** the grounding check only verifies `affected_files`/`affected_components` — it cannot and does not verify that free-text fields (`summary`/`root_cause`) are themselves *true*. A real dogfood run produced a structurally valid, fully-grounded diagnosis whose summary read "The server.js file was loaded successfully without any errors" — for a check that had, in reality, crashed with exit code 1. The response passed every structural/grounding check (no invented file or component names) while still being factually wrong in its prose. This is a real, named boundary of what "grounded" means here: it constrains *what the model is allowed to name*, not *what the model is allowed to claim about the things it names*. Building a stronger check for the latter is explicitly out of scope (the same "do not build a perfect fact-checker" instruction this phase started with) — named here so it isn't mistaken for a solved problem.

## Confidence — reject, not clamp, and why that's a deliberate difference

`validate_repair_response` (Phase E Part 1) clamps an out-of-range confidence into `[0.0, 1.0]`, because a repair proposal's confidence feeds a later phase's programmatic decision. `validate_diagnosis_response` rejects an out-of-range or wrongly-typed confidence outright instead — a diagnosis is advisory text a person reads, and a model that returns `confidence: 1.5` or `confidence: true` has demonstrated its whole response can't be trusted at face value, not just that one field. Verified directly: booleans (`bool` is an `int` subclass in Python — the exact trap `response_parser.py`'s own `_is_number_not_bool` already guards against, reused here identically), strings, `NaN`, `Infinity`/`-Infinity` (both parse successfully via Python's `json.loads`, which accepts them as a non-standard JSON extension — caught by the ordinary `0.0 <= x <= 1.0` range check, since every comparison against `NaN` is `False`), and out-of-range numbers are all rejected.

## Log handling: bounded, honest about what it can't do

`RuntimeCheckResult.logs` is a single, already-merged stdout+stderr stream — Phase G Part 2's own design (the two streams were never captured separately). This phase's own instruction to "prioritize stderr" is honored as far as the actual data allows: the real `exception`/`reason` fields (captured independently, Python-side, never subject to truncation) are always included in full; the merged log itself gets head+tail truncation at 3000 characters with an explicit `[... N character(s) truncated ...]` marker — never silently presented as complete. An empty log is stated explicitly (`"(no output was captured)"`), never left as a blank, ambiguous section.

**Secret redaction, best-effort, stated as such:** `KEY=`/`TOKEN=`/`SECRET=`/`PASSWORD=`-shaped assignments, bearer tokens, and credentials embedded in a URL are pattern-matched and replaced with `[REDACTED]` before any log text reaches a prompt. Not an exhaustive secret scanner — this phase's own requirement was "at minimum, do not introduce any new mechanism that intentionally exposes secrets," not a general-purpose secret detector.

## Files changed

**New:** `qa_agent/ai/diagnosis.py`, `diagnosis_models.py`, `diagnosis_prompts.py`, `diagnosis_parser.py`; `tests/regression/test_runtime_diagnosis.py` (37 test functions, 78 checks); this document.
**Modified:** `qa_agent/ai/__init__.py` (+exports, purely additive); `qa_agent/__main__.py` (+`--diagnose`/`--ai-provider`/`--ai-model` flags on `discover`, implying `--execute-runtime-plan`); `tests/regression/test_repository_context.py` (one isolation test rescoped — see "A second, incidental test fix" below).
**Untouched, byte-for-byte:** `qa_agent/runtime/` in full (Project Discovery, Repository Context, the Runtime Planner, the Runtime Executor), `runner.py`, `adapters.py`, `watch.py`, every pre-existing Phase D/E AI module (`explainer.py`, `fixer.py`, `summarizer.py`, `repair.py`, `repair_loop.py`, `decision.py`, `apply.py`, `workspace.py`, `validator.py`) — confirmed by a direct source-grep test plus rerunning the full suite before and after this part.

## A second, incidental test fix, same category as Part 2's own

Phase F Part 2's own isolation test (`test_qa_agent_ai_package_is_completely_untouched_by_this_phase`) asserted that *no file* in `qa_agent/ai/` would ever reference `RepositoryContext` — a true, correct guarantee for Part 2's own completion criteria, but one Part 2's own documentation explicitly said might change: "wiring `RepositoryContext` into any AI prompt builder [is] an explicit, separate decision for a later phase." This is that later phase. Rescoped the test to name Phase D/E's own nine pre-existing AI modules explicitly, rather than the whole directory — the same over-broad-glob fix already applied once before, to Part 2's own subprocess-isolation test, when Part 2 itself extended the directory it was checking.

## Test results

`python tests/run_all.py` — **28/29 suites** (the one failure is `integration/test_watch_pipeline.py`'s pre-existing, unrelated timing flake). `test_runtime_diagnosis.py` alone: 78/78 checks — covering the model contract, a real end-to-end diagnosis through the actual discover→context→plan→execute pipeline, every structured failure mode (insufficient-context decline, malformed JSON, markdown-fenced JSON, a missing required field, provider failure), exhaustive confidence validation (boundaries, out-of-range, boolean, string, `NaN`/`Infinity`), PASS/SKIPPED/NOT_IMPLEMENTED never reaching the AI (call-counted, not just asserted on output), multiple independent failures diagnosed differently, grounding acceptance and rejection (both via the real pipeline and a direct unit test), log truncation/empty-output/secret-redaction, serialization, rendering (including the `NOT_APPLICABLE`-renders-to-nothing case), the "never raises" contract, working without a known `RuntimeCheck`, the deterministic-result-never-mutated guarantee, both isolation checks, and the CLI's implication chain.

## Dogfooding, real output, real live Ollama (`qwen2.5:0.5b`), no fabricated results

**This project's own repository:** both planned checks (`build-verification`, `test-suite-verification`) are genuinely `SKIPPED` (no evidence-based command exists) — nothing diagnosable, zero AI calls, matching every prior phase's own honest assessment of this same repo.

**A real, controlled fixture** (`server.js` requiring `express` with no `node_modules` installed — a genuine, reproducible crash, not fabricated evidence) — **9 real live-Ollama attempts** across several runs:
- **2 `DIAGNOSED`.** One was genuinely accurate: *"Server startup failed due to a missing Express module... Cannot find module 'express'... Affected files: server.js, Affected components: express"* — correct, well-grounded, confidence 0.90. **One was structurally valid but factually wrong** — *"The server.js file was loaded successfully without any errors"* for a check that had actually crashed — the honest limitation named above, caught by dogfooding, not invented for this report.
- **5 `INVALID_RESPONSE`** — a malformed `evidence` field shape (twice), a genuine grounding rejection (the model named something outside the real evidence), and malformed JSON with a bad escape sequence.
- **2 `AI_ERROR`**, both from my own initial misconfiguration (the CLI's `--ai-provider` default model name doesn't match the one real model actually pulled locally, and a too-short default timeout) — not an engine defect; resolved by passing `--ai-model qwen2.5:0.5b` explicitly and a longer timeout, exactly as any real user with only this small model installed would need to.

**A real `lms-ai` timeout** (`build-verification`, reproducing the exact real 90s-exceeding `next build` timeout from Phase G Part 2's own dogfooding, at a shorter 45s bound here): one real diagnosis attempt, correctly returned `INVALID_RESPONSE` (malformed JSON with a bad `\escape` sequence) — captured and reported honestly rather than retried until a cleaner result appeared.

**No genuine `insufficient_context` decline was observed** across roughly 9 real attempts, despite trying — the small local model tends to either produce a structurally valid guess or malformed JSON rather than using the sanctioned decline shape. Reported honestly, per this phase's own explicit instruction, rather than manufactured.

## Bugs discovered

None in the diagnosis engine's own logic — every structured failure mode (malformed JSON, bad confidence, ungrounded claims, provider errors) was caught and classified exactly as designed, on the first real attempt in every case. The two `AI_ERROR` results above were caused by my own dogfooding setup (wrong default model name, short timeout), not by the engine.

## Explicit confirmations

- **No file is ever modified by the diagnosis engine** — confirmed by a direct source-grep test (`test_diagnosis_modules_never_execute_subprocesses_or_touch_the_filesystem`) finding no `subprocess`/file-removal/`shutil` usage anywhere in the four new modules.
- **The deterministic runtime result remains authoritative** — confirmed directly: `RuntimeExecutionResult`/`RuntimeCheckResult` are byte-identical before and after diagnosis runs against them, and a check's diagnosability is decided purely from its own already-determined `status` string.

## Deferred to Phase G Part 4

Automatic repair, patch generation, or any file modification triggered by a diagnosis; retry loops; parallel AI execution; new runtime/framework/package-manager detectors; API/middleware/browser testing; a stronger grounding check for free-text claims (the "factually wrong but structurally valid" gap named above); multi-agent orchestration.

**Status: Phase G Part 3 CLOSED.**
