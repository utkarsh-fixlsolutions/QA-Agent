# Step 29 — G5.2: Autonomous QA Execution Loop

**Status:** IMPLEMENTED (2026-09-11).
**Phase:** G5, Part 2. **G5.2 = execute what G5.1 decides, collect evidence, update state, repeat.** G5.1 decided *what* should happen next; G5.2 is the first part of this whole project that actually *does* it, autonomously, bounded, over multiple steps - and nothing more than that. This is not the complete autonomous QA agent - see "What G5.2 deliberately does not do" below.
**Scope:** two new files in the existing `qa_agent/agent/` package - `executor.py`, `loop.py` - plus a small, additive, backward-compatible extension to G5.1's own five files (target selection support, needed for `runtime_diagnosis` to choose *which* failure to interpret) and one new CLI subcommand, `python -m qa_agent agent <path>`.

## The loop

```
Observe (QAState)
   ↓
Decide (G5.1: select_next_action - AI picks one action, or stop)
   ↓
Validate (G5.1's own deterministic re-check - already done inside select_next_action)
   ↓
Execute (G5.2: executor.execute_action - the real G1-G4 function, for real)
   ↓
Collect Evidence (the real result becomes part of QAState)
   ↓
Update State (dataclasses.replace - a new, immutable QAState, never a mutation)
   ↓
Decide Again
   ↓
... until AI stop / budget exhausted / no actions remain / a real provider or controller failure
```

`qa_agent.agent.run_agent_loop(state, provider, root, config=None)` is the one new public entry point. It never hard-codes a fixed action sequence — every decision comes from a real call to `select_next_action`, and the loop only ever does what that call (already independently re-validated by G5.1's own controller) says. Proven directly, not just asserted: three different real, real-subprocess end-to-end tests each drive a *different* AI-scripted sequence (`build → test → stop`; `discovery → plan → build → stop`; `discovery → plan → server_startup → build → stop`) through the identical loop code, and the executed order in each case matches exactly what the script said, never a fixed list.

## Executable actions

| Action id | Existing implementation used | Target required | Dependencies |
|---|---|---|---|
| `project_discovery` | `qa_agent.project.discover_project` + `build_repository_context` | No | none |
| `runtime_plan` | `qa_agent.runtime.plan_runtime_qa` | No | `project_discovery` |
| `static_analysis` | `qa_agent.runner.run` | No | `project_discovery` |
| `server_startup` | `qa_agent.runtime.run_runtime_plan`, scoped to the one planned `server-startup` check | No | `runtime_plan` (+ must be planned) |
| `build_verification` | same, `build-verification` | No | `runtime_plan` (+ must be planned) |
| `test_suite_verification` | same, `test-suite-verification` | No | `runtime_plan` (+ must be planned) |
| `environment_validation` | same, `environment-configuration` | No | `runtime_plan` (+ must be planned) |
| `static_assets` | same, `static-assets` | No | `runtime_plan` (+ must be planned) |
| `runtime_diagnosis` | `qa_agent.ai.diagnose_runtime_failure` (G3), for one target check | **Yes** - a real, currently-undiagnosed failed check id | `runtime_plan` (+ a real failure must exist) |

`runtime_repair` has no executor and is never eligible (unchanged from G5.1) — G5.2 never applies a repair; that integration is explicitly G5.3's own job.

Every executor call reuses the existing engine exactly as it already exists - no new engine, no reimplemented subprocess logic, no duplicated diagnosis logic. The five runtime-check executors all use the same technique `qa_agent/ai/runtime_repair.py`'s own `_run_single_check` already established (Phase G Part 4): a `RuntimeQAPlan` containing only the one target check, run through the real, unmodified `run_runtime_plan`.

## Target resolution — a small, additive G5.1 extension

`runtime_diagnosis` is the one action that needs to name *which* failure to interpret. The AI is never allowed to invent this — `actions.valid_targets(action, state)` computes the real, currently-undiagnosed failed check ids, the same list `build_action_selection_prompt` now shows the AI (`"runtime_diagnosis [...]: ... (valid targets: build-verification, test-suite-verification)"`), and `controller.select_next_action` independently re-validates the AI's own `target` claim against that exact same set before ever returning a `continue` decision — naming a real-but-wrong check id, a fabricated one, or a target for an action that doesn't use one are all rejected the same way an invalid `next_action` already was.

This required extending G5.1's own `ControllerDecision`/`DecisionResponse`/`ActionDefinition` with one new field each (`target`, `target`, `requires_target` - all with backward-compatible defaults) and `parser.py`/`prompts.py`/`controller.py` with the corresponding structural/eligibility checks - additive throughout, never a redesign. Proven directly: every one of G5.1's own 68 pre-existing checks (none of which ever supply a `target`) still passes unmodified against the extended code.

## State flow

`QAState` (G5.1's own model, reused without a competing state type) carries everything one iteration needs to hand the next one something meaningful: `repository_context`, `runtime_plan`, `execution_results`, `static_analysis_result`, `diagnoses`, `iteration`, `max_iterations`. Every executor returns a *new* `QAState` (via `dataclasses.replace` - `QAState` and every G1-G4 result type it holds are already frozen, so nothing here needs to defend against in-place mutation separately), which the loop then passes into the *next* `select_next_action` call. Proven directly, not just by counting calls: a dedicated test captures the exact `QAState` object handed to the decider on both the first and second calls and asserts the second one's `runtime_plan` is the real, non-`None` result of the first action - the actual property this whole loop exists to guarantee.

## Safety boundary

The AI can only ever recommend one of `qa_agent.agent.ACTION_REGISTRY`'s ten fixed, hand-authored action ids (and, for `runtime_diagnosis`, a real, currently-valid target). It cannot execute a shell command, an arbitrary subprocess, arbitrary Python, an arbitrary filesystem write, or an arbitrary HTTP request - there is no generic command-execution tool anywhere in this package for it to reach, and `executor.execute_action` only ever dispatches through `_EXECUTORS`, a fixed, closed dict of nine real function references. Every one of these is rejected before `execute_action` is ever called, proven directly (not just by absence of a bad outcome): `rm -rf .`, `python -c "..."`, `curl https://...`, and `../../../etc/passwd` are each sent as a scripted `next_action`, and a dedicated test asserts the real executor function is never invoked, the real filesystem is provably unchanged, and the session still terminates safely (bounded by `max_iterations`, never hanging).

Existing deterministic executors may, and do, use real subprocesses internally exactly where they already did (build/test commands, a real dev-server process) - that is G1-G4's own existing, already-verified behavior, unchanged by this step.

## Tests

`tests/regression/test_agent_loop.py` — **30 test functions, 67 checks**: loop-level unit tests with injected fake `decide`/`execute` (every termination reason, both recoverable-vs-fatal error categories, budget/no-action bounding even under a maximally adversarial always-invalid decider, the state-threading proof, execution-history field coverage); executor-level unit tests against real fixtures (project discovery, a missing-precondition error, a real passing build, a real G3 diagnosis call, a genuine crash caught safely); full end-to-end adversarial tests (shell command / arbitrary Python / curl / path traversal / invalid target, each proven never to reach the executor); three distinct real, multi-step, real-subprocess loop integrations, each driven by a different scripted sequence; a repeated-successful-action test proving G5.1's own eligibility rule holds inside the real G5.2 loop; and two CLI-level tests. `tests/regression/test_agent_controller.py` (G5.1) gained one new test confirming the execution boundary is real on both sides, and had its own two isolation tests rescoped to G5.1's own five files specifically — the same "a legitimate new file extends the directory, the existing isolation guarantee is restated more precisely, not weakened" fix this project has now made three times (Phase G Part 2's `executor.py` joining `qa_agent/runtime/`; Phase G Part 3's `diagnosis.py` joining `qa_agent/ai/`; this one).

## CLI

```
python -m qa_agent agent . --ai-provider mock --max-iterations 5
python -m qa_agent agent . --objective "Perform a QA assessment" --ai-provider cloud --ai-model poolside/laguna-s-2.1:free --max-iterations 10
```

Prints the real objective/provider, every history entry (rejected or executed, with status and summary), the real termination reason, and an explicit `ai_stopped` flag - never claiming the QA objective was "satisfied," only reporting what the session actually did. Exit code `2` for a genuine controller/provider failure, `0` otherwise (a QA failure recorded as evidence is not a CLI error, matching this project's own exit-code philosophy of distinguishing "ran cleanly" from "found something").

## What G5.2 deliberately does not do

Decide diagnosis is warranted and orchestrate a diagnose-then-something sequence on its own - the AI still makes that call, one action at a time, same as everything else. Apply a repair, or expose `runtime_repair` as eligible - unchanged from G5.1, explicitly reserved for **G5.3**. Judge whether the overall QA objective is "objectively satisfied" - `AgentResult.ai_stopped` distinguishes an AI-chosen stop from a deterministic one, but neither is a claim of success; that judgment is explicitly deferred to **G5.4**. Give the AI any form of direct execution access - no shell, no arbitrary subprocess, no arbitrary Python, no arbitrary filesystem write, no arbitrary HTTP, no browser automation, no unrestricted agent tool of any kind.

**Status: G5.2 (Autonomous QA Execution Loop) CLOSED. G5.3 not started.**
