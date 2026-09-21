# Step 28 — G5.1: Autonomous QA Action Selection Controller

**Status:** IMPLEMENTED (2026-09-11).
**Phase:** G5, Part 1. **G5.1 = autonomous action selection.** Nothing more. This is the first piece of a multi-stage phase, not a complete autonomous agent — see "What G5.1 deliberately does not do" below before assuming otherwise.
**Scope:** a new `qa_agent/agent/` package — `models.py`, `actions.py`, `parser.py`, `prompts.py`, `controller.py`, `__init__.py`. Not wired into `__main__.py` or any CLI flag — an internal API only, per this step's own explicit scope.

## What G5.1 is

The eventual G5 loop is:

```
OBSERVE → REASON → CHOOSE NEXT QA ACTION → EXECUTE DETERMINISTIC ACTION → OBSERVE RESULT → REPEAT → ...
```

G5.1 implements exactly one link: **CHOOSE NEXT QA ACTION**. Given a `QAState` (a bounded, read-only view of what G1-G4 have already produced) and any `AIProvider`, `qa_agent.agent.select_next_action(state, provider)` returns a structured `ControllerDecision` — continue with exactly one deterministically-eligible action, stop, or error. **It never executes anything.** No subprocess, no shell command, no HTTP request, no static analyzer, no runtime check, no repair, no file write. `qa_agent/agent/` contains zero calls to `discover_project`, `plan_runtime_qa`, `run_runtime_plan`, `runner.run`, `diagnose_runtime_failure`, or `repair_runtime_failure` — confirmed directly by a source-level isolation test, not just stated.

## Architecture

```
QA State  (models.QAState — a bounded, duck-typed reference to real G1-G4 output)
   ↓
Action Registry  (actions.ACTION_REGISTRY + actions.eligible_actions(state) — deterministic, no AI)
   ↓
AIProvider  (MockProvider / OllamaProvider / OpenRouterProvider — unmodified, reused as-is)
   ↓
Structured Decision  (parser.validate_decision_response — schema only)
   ↓
Deterministic Validation  (controller.select_next_action — re-checks the AI's own selection
                            against the exact same eligible set computed before the AI was ever asked)
```

The AI is a strategist, never an authority — enforced at two points, not just asserted: the eligible-action allowlist is computed **before** the AI is consulted (so the prompt itself can only ever offer real, currently-valid choices), and the AI's own selection is independently re-checked against that exact same set **after** the response comes back. Naming anything else — a shell command, a real-but-currently-ineligible action, an invented capability — is rejected, unconditionally, regardless of how the prompt was phrased or what the model claims.

## The QA state

`QAState` never duplicates G1-G4's own models — every field is either `None`/empty or a direct reference to something already produced elsewhere (`repository_context`, `runtime_plan`, `execution_results`, `static_analysis_result`, `diagnoses`, `repairs`). `completed_action_ids` is a **computed property**, never a separately-tracked list — it can never drift from the real data it summarizes, because it is recomputed from that data every time it's read. `remaining_budget` is a hard, deterministic cap (`max_iterations - iteration`) the controller enforces on its own, before ever spending a token: at zero budget, `select_next_action` returns `stop` without consulting the AI at all.

Deliberately **duck-typed** against `RepositoryContext`/`RuntimeQAPlan`/`RuntimeCheckResult`/`RuntimeDiagnosis` — `qa_agent/agent/` imports zero symbols from `qa_agent.runtime`, `qa_agent.project`, `qa_agent.ai.diagnosis`, or `qa_agent.ai.runtime_repair`. This is the same zero-import precedent `qa_agent/ai/diagnosis.py` already established for reading deterministic output as plain data (Phase G Part 3), applied one layer higher: G5.1 reads the *combined* output of every prior phase without depending on any of their concrete types.

## Available actions

Ten actions were investigated — the nine this step's own spec named, plus `static_assets`, a real, already-implemented, safe executor found during this step's own repository audit that the spec's list happened to omit.

| Action id | Maps to | Implemented |
|---|---|---|
| `project_discovery` | `qa_agent.project.discover_project` + `build_repository_context` | Yes |
| `runtime_plan` | `qa_agent.runtime.plan_runtime_qa` | Yes |
| `static_analysis` | `qa_agent.runner.run` (ruff/pyright/mypy/eslint/shellcheck) | Yes |
| `server_startup` | Runtime check id `server-startup` (`qa_agent.runtime.executor`) | Yes |
| `build_verification` | Runtime check id `build-verification` | Yes |
| `test_suite_verification` | Runtime check id `test-suite-verification` | Yes |
| `environment_validation` | Runtime check id `environment-configuration` | Yes |
| `static_assets` | Runtime check id `static-assets` | Yes |
| `runtime_diagnosis` | `qa_agent.ai.diagnose_runtime_failure(s)` (G3) | Yes |
| `runtime_repair` | `qa_agent.ai.repair_runtime_failure(s)` (G4) | **No — deliberately** |

`runtime_repair` is the one entry whose underlying capability is real, complete, and already verified (G4) but which G5.1 never exposes as eligible — proven directly by a test that constructs a maximally favorable state (a real, diagnosed failure, every dependency satisfied) and confirms `runtime_repair` still never appears in the eligible set, and that the AI naming it explicitly is still rejected. Repair *selection* is explicitly reserved for **G5.3**, per this project's own roadmap — not a limitation of G4, a deliberate scope boundary of this step.

## Eligibility — deterministic, not a hard-coded priority list

`eligible_actions(state)` is a pure function of `state` — same input, same output, always, with no AI influence whatsoever. An action is eligible only when it is `implemented`, not already completed, every declared dependency is satisfied, and (for the five runtime-check actions) the specific check was actually *planned* for this repository — a real constraint the static `requires` list alone cannot express (a plan can legitimately omit Server Startup for a repository with no runnable entry point). `runtime_diagnosis`'s own eligibility is genuinely dynamic: it becomes eligible again whenever a new, undiagnosed failure exists, and ineligible again once every known failure has a diagnosis — this emerges from the real state (failed check ids minus diagnosed check ids), not from a fixed scenario baked into the controller.

## Structured output

```json
{"decision": "continue", "next_action": "build_verification", "reason": "...", "evidence_needed": [...]}
{"decision": "stop", "next_action": null, "reason": "...", "evidence_needed": []}
```

`parser.validate_decision_response` reuses `qa_agent.ai`'s own `ValidationResult`/`STATUS_SUCCESS`/`STATUS_INVALID` and `parse_json_response` — the same shared primitives `diagnosis_parser.py`/`response_parser.py` already established, not a new validation system. It performs *structural* validation only (well-formed JSON, correct types, `next_action` null iff `decision == "stop"`); whether a structurally-valid `next_action` is currently *eligible* is a stateful question only `controller.py` can answer, mirroring exactly how `diagnosis_parser.py`'s schema validation and grounding check stay two separate steps.

## Error handling

Every required failure mode — a zero/negative budget, no eligible action, a provider timeout/failure, malformed JSON, an invalid schema, an ineligible or invented action selection, a genuinely unexpected exception — returns `ControllerDecision(decision="error", ...)` with a clear `reason`/`error`. Never raises. Never invents a fallback action, per this step's own explicit instruction. The two cases that need no AI opinion at all (`budget exhausted`, `no eligible actions`) return `decision="stop"` deterministically, with zero provider calls — proven directly with a call-counting fake provider, the same technique G3's own "PASS never reaches the AI" test already used.

## Prompt design

Bounded and curated, never a repository dump: repository context uses the same small subset `diagnosis_prompts.py`/`runtime_repair_prompts.py` already established (repository/application type, languages, frameworks); runtime check results and diagnoses are rendered as short one-line summaries (`id [status]: reason`), never raw logs — `qa_agent/agent/prompts.py` contains zero references to `.logs` anywhere, confirmed directly. The guardrail text additively reuses `qa_agent.ai.contract`'s existing `CONTRADICTORY_EVIDENCE_CLAUSE`/`DETERMINISTIC_AUTHORITY_CLAUSE` (the AI Behavior Contract work from the step before this one) alongside new, G5.1-specific guardrails: choose only from the supplied allowlist, never invent a capability, never claim to execute anything, prefer the action that reduces the most real uncertainty, and stop rather than guess when nothing useful remains.

## Safety metadata

Each `ActionDefinition` carries a `safety_level` (`READ_ONLY` / `ASSISTED` / `AUTONOMOUS`) and `modifies_files` flag — minimal, deterministic metadata for a **future** G5 stage to filter by permission mode. No mode concept exists yet anywhere in this project, and G5.1 does not build one — these fields exist now so a later stage does not need a data-shape change to start using them.

## Testing

`tests/regression/test_agent_controller.py` — **50 test functions, 68 checks**: the registry's own data integrity; deterministic eligibility (including the dynamic planned-check and undiagnosed-failure cases, and the never-eligible `runtime_repair` case); every parser rejection (unknown decision, the model claiming `"error"` itself, malformed JSON, missing/wrong-typed fields, `next_action` supplied for `stop`, a list where a string is required); the controller's own end-to-end continue/stop paths; every adversarial case named in this step's own spec (`rm -rf .`, `edit_file`, `run_arbitrary_command`, a real-but-currently-unavailable action) all rejected; deterministic-authority proofs (an AI call changes neither eligibility, nor a real execution result, nor `runtime_repair`'s own `implemented` flag); `MockProvider`/`OllamaProvider`/`OpenRouterProvider` shape-compatibility with zero live network calls; bounded-prompt proofs; and two source-level isolation tests (`qa_agent/agent/` never imports or calls into `qa_agent.runtime`/`qa_agent.project`/any execution function, and no deterministic engine imports `qa_agent.agent` back).

## Regression

`test_runtime_diagnosis.py` (G3): **78/78**, unchanged. `test_runtime_repair.py` (G4): **74/74**, unchanged. Full suite: **33/34** (the one failure is `integration/test_watch_pipeline.py`'s pre-existing, unrelated timing flake, documented since this project's early phases).

## Intended future flow

```
G5.1  (this step)  →  autonomous action selection only, never executes
   ↓
G5.2  →  the execution loop: actually run whatever G5.1 selects, feed the real
         result back into a new QAState, call select_next_action() again, repeat
   ↓
G5.3  →  repair integration: the point at which `runtime_repair` finally
         becomes a real, eligible action, wired to the existing G4 pipeline
   ↓
G5.4  →  completion/stop logic: how a multi-step session decides and reports
         its own final QA result
```

**G5 is not complete. Only G5.1 is.**

## What G5.1 deliberately does not do

Execute a single action it selects. Run a subprocess, a shell command, an HTTP request, a static analyzer, a runtime check, or a repair. Modify a file. Loop — `select_next_action` is called once per decision by a caller; no loop exists anywhere in this package. Expose `runtime_repair` as eligible, regardless of how favorable the state is. Build a permission/mode-enforcement system (only the metadata fields a future stage would need). Add a CLI surface — `python -m qa_agent agent ...` does not exist yet. Change G1-G4's own logic, safety gates, or default provider/model in any way.

**Status: G5.1 (Autonomous QA Action Selection Controller) CLOSED. G5.2 not started.**
