# Step 26 — Local Model Benchmark (`tools/model_bench.py`)

**Status:** IMPLEMENTED (2026-09-11).
**Phase:** none — a standalone evaluation tool, not a phase. It measures the existing AI layer honestly; it does not change it. No G5 code, no production model-default change, no G1-G4 behavior change.
**Scope:** `tools/model_bench.py` (CLI/runner), `tools/bench_tasks.py` (the 15 tasks), `tools/bench_fixtures.py` (realistic fixture builders) — all outside `qa_agent/`. `tests/regression/test_model_bench.py` tests the harness itself.

## Why this exists

Two conversations ago, the question was raised: is it worth upgrading past `qwen2.5:0.5b`, and if so, to what? The answer settled on then was "treat it as a benchmark gate before any model-default change or G5 work, not a phase and not a silent config flip." This is that gate. It answers one question, mechanically and repeatably: *which local model is reliable enough for QA-Agent's actual AI responsibilities, and does any of them look strong enough to be worth trusting with more autonomy later?*

It does not answer "which model should we use" — that's a judgment call for whoever reads the results. The tool refuses to declare a winner in code, on purpose.

## How to run it

```
python tools/model_bench.py --models qwen2.5:0.5b
python tools/model_bench.py --models modelA modelB modelC --repeats 5
python tools/model_bench.py --models modelA --repeats 3 --timeout 60 --endpoint http://localhost:11434
```

`--models` accepts one or more Ollama model names (`nargs="+"`). `--repeats` defaults to 5, overridable. `--endpoint`/`--timeout` default to `OllamaProvider`'s own defaults, overridable for a non-default Ollama setup. `--output-dir` defaults to `benchmark_results/` at the repo root (git-ignored — results are never committed).

If a requested model isn't installed or Ollama isn't reachable, the tool reports it clearly on stderr and continues with whichever requested models *are* available — it never pulls a model, never silently substitutes one, and only aborts (exit 2) if none of the requested models are available at all.

## How models are specified — and how this stays model-agnostic

Every model is used through `OllamaProvider(endpoint=..., model=<name>, timeout=...)` — the exact same, unmodified provider class `qa_agent/ai/ollama.py` already ships. The benchmark constructs a fresh provider per model name; nothing in this tool or in `qa_agent.ai` has any model-specific branching. The production default (`qa_agent/config.py`'s `AIConfig`, `--ai-model`/`--ai-provider` on the main CLI) is completely untouched by this tool — running a benchmark has zero effect on what `discover --diagnose`/`--repair-runtime` actually uses.

## How repetitions work

Each of the 15 tasks is sent to each requested model `--repeats` times (default 5), independently — the prompt is built once per task (so every repetition and every model sees byte-identical input) and a fresh provider call is made each time, since a local model's sampling is not perfectly deterministic even with the same prompt. Total attempts = `models × tasks × repeats`.

## What each category measures

| Category | Measures |
|---|---|
| **A — Runtime diagnosis** | Can it interpret real, unambiguous failure evidence into a correct, grounded diagnosis? (3 tasks) |
| **B — Adversarial / grounding** | Does it decline or hedge honestly when evidence is missing/thin/contradictory, or does it fabricate? Does it follow real evidence over a tempting-but-irrelevant distractor? (3 tasks) |
| **C — Repair** | Can it propose a plausible, schema-valid, line-accurate repair - and correctly decline when there isn't enough signal to safely propose one? (3 tasks) |
| **D — Constraint following** | Does it comply with an explicit "JSON only" instruction *literally* (not just well enough for the lenient real parser to recover it), and does it respect an explicit single-file restriction? (2 tasks) |
| **E — Phase D AI capabilities** | Explanation/summary quality against a real finding and a real multi-finding run, without inventing extra findings or counts. (2 tasks) |
| **F — Early G5 probes** | **Informational only.** No schema exists for "recommend next action" (the AI Behavior Contract work explicitly declined to design one before G5 exists) - these two tasks send a plain, clearly-labeled exploratory question and record the raw answer for a human to read. Never scored, never validated, never influences anything. (2 tasks) |

Every task's prompt is built with a real `qa_agent.ai` prompt builder (`build_diagnosis_prompt`, `build_runtime_repair_prompt`, `build_explanation_prompt`, `build_summary_prompt`) and, for A-E, validated with the real, unmodified validator (`validate_diagnosis_response`, `validate_repair_response`, `validate_explanation_response`, `validate_summary_response`) plus `response_is_grounded` where applicable. No duplicate parsing or validation logic exists anywhere in this tool.

Fixtures are realistic, not toy strings: real `RuntimeCheckResult`/`RuntimeCheck`/`RepositoryContext`/`Finding` objects (the same dataclasses `qa_agent.runtime`/`qa_agent.project`/`qa_agent.adapters` already use), and repair/explain tasks read a real small source file on disk via the real `extract_context()` - the same function G3/G4's own prompts use, not a hand-typed code snippet. Repair tasks are additionally checked by running the real, unmodified `apply_repair()` (Phase E Part 2) against a real `TemporaryWorkspace`, the closest thing to a "grounding" check a repair proposal has: does it actually apply against the real file it names.

## What is recorded, per attempt, and why it's never collapsed to one score

`provider_ok`/`provider_error`, `schema_status` (`success` / `insufficient_context` / `invalid` / `provider_error` / `not_applicable` for a Category F probe), `grounded` (`True`/`False`/`None` when not applicable), `constraints` (a dict, e.g. `{"json_only": true}`), `correctness` (`0`/`1`/`2`/`None`), `correctness_source` (`mechanical`/`manual_pending`/`not_applicable`), latency, start/end timestamps, and the raw response text. A model is never judged by one number - a model with a high schema-valid rate but a low grounded rate, or vice versa, looks different in this data on purpose.

**"Schema valid"** means the model followed the response contract at all - a full structured answer *or* the sanctioned `insufficient_context` decline both count; only genuinely malformed/invalid output counts against it. **"Grounded"** is stricter and only meaningful for a `success` response: did every specific claim actually appear in the real evidence it was given. **"Consistency"** (per the exact formula given for this benchmark) is stricter still: `(successful AND grounded) / repeats` - a model that declines honestly every time has 100% schema-valid but 0% consistency, and that is the correct, intended reading, not a bug.

## Correctness scoring — mechanical where genuinely possible, manual otherwise

A hard, universal floor applies before anything task-specific: a `success` response with `grounded=False` is always `0` (an ungrounded claim is never "correct," regardless of the task). Beyond that floor, only tasks whose *correct* answer is itself mechanically checkable get an automatic verdict:

- **Declines are expected** (B4, B5, C9): `insufficient_context` scores `2`; a confident `success` anyway scores `0`/`1` depending on how much it actually claimed.
- **A specific grounded name is expected over a distractor** (B6): naming the real file scores `2`, naming the distractor scores `0`.
- **A literal constraint is being measured, not diagnosis/repair quality** (D10, D11): scored purely on `constraint_json_only` / whether a second file was mentioned.

Every other task (A1-A3, C7-C8, E12-E13) is genuinely open-ended - "is this a *good* explanation" cannot be judged by a regex without pretending subjective quality is deterministic. Those get `correctness=None, correctness_source="manual_pending"`, and the benchmark's own printed summary lists each one's `scoring_guidance` (also saved in the JSON) as a reminder of exactly what a human reviewer should look for.

## Reading the output

The printed summary is a per-model table (`CATEGORY / SCHEMA VALID% / GROUNDED% / AVG CORRECTNESS / CONSTRAINT% / AVG LAT(s)`), the weakest category by consistency, any task whose consistency is below 100%, every provider error and malformed response (so a real failure is never silently averaged away), Category F's raw text for manual reading, and a final reminder of which tasks still need a human correctness pass. The full JSON (`benchmark_results/<timestamp>.json`, git-ignored) carries every field of every attempt for deeper analysis or a second reviewer.

## What this tool deliberately does not do

Declare a winner. Pull a model automatically. Substitute one model for another. Execute anything a model generates. Give a model shell or filesystem-write access (repair tasks only ever write inside a throwaway `TemporaryWorkspace`, cleaned up every run). Change `qa_agent/config.py`'s AI defaults. Modify G1-G4 behavior. Implement a G5 orchestrator or a formal next-action schema - Category F is explicitly unscored and informational, exactly as scoped.

## Known limitations

- Correctness for the open-ended tasks (A1-A3, C7-C8, E12-E13) requires a human pass over the saved JSON - by design, not an oversight (see above).
- The `plausible_count`/`_mentions_other_file` checks are explicitly-documented heuristics (a loose regex over numbers/filenames in free text), not exact - they contribute one input to a judgment, never a full substitute for reading the response.
- A single benchmark run's sample size (`repeats`, default 5) is small for a genuinely noisy local model - repeat with a larger `--repeats` before treating a close result as decisive.
- No tokens/sec figure is computed beyond `LLMResponse.latency_seconds` (wall-clock per call, already provided by `OllamaProvider` itself) - a dedicated tokens/sec metric would need Ollama's own `/api/generate` eval-count fields, not currently read by `OllamaProvider` and out of this step's scope to add.

**Status: Model Benchmark Harness CLOSED. No model selected, no default changed, no G5 started.**
