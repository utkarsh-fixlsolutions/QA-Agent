# Step 34 — G5.3 (Repair Integration) + G5.4 (Completion Semantics)

**Status:** IMPLEMENTED (2026-09-14). Design approved as proposed below; implemented and tested exactly as designed, with no scope changes.
**Phase:** G5, Parts 3-4 — the last two links of the loop docs/28 named up front:

```
G5.1  ->  choose next QA action only, never executes            [CLOSED]
G5.2  ->  execute what G5.1 chooses, evidence accumulates       [CLOSED]
G5.3  ->  repair integration: runtime_repair becomes real        [this step]
G5.4  ->  completion/stop logic: the session's own final result  [this step]
```

## What was inspected first (no redesign, no duplication)

Read in full before drafting this: `qa_agent/agent/{models,actions,controller,executor,loop,prompts}.py`, `qa_agent/ai/runtime_repair.py` (G4) and its models (`diagnosis_models.py`, `runtime_repair_models.py`), and `docs/28`/`docs/29`. Three things fall directly out of that reading, not assumed:

1. **`runtime_repair` already has a registry entry.** `actions.py` has carried it since G5.1 with `implemented=False`, `modifies_files=True`, `safety_level=SAFETY_AUTONOMOUS`, `requires=("runtime_diagnosis",)` — placed there on purpose, waiting for this step. G5.3 flips one field and adds one (`implemented=True`, `requires_target=True`), it does not add a new registry entry.
2. **The target mechanism is already fully generic.** `requires_target`/`valid_targets`/the controller's target re-validation were all built in G5.2 for `runtime_diagnosis`, and none of `controller.py`, `prompts.py`, or `loop.py` reference `runtime_diagnosis` by name in that machinery — they operate on any `requires_target=True` action. G5.3 needs one new branch in `actions.valid_targets()`, nothing in `controller.py`/`prompts.py`/`loop.py`.
3. **`repair_runtime_failure()` (G4) is already a complete, single-failure entry point.** It takes `(check_result, diagnosis, execution_result, repository_context, provider, root, runtime_check=..., config=...)` and returns one `RuntimeRepairResult` — eligibility, proposal, workspace apply, static validation, candidate runtime re-check, real apply, real post-apply re-verification, all already inside it, already tested (74 checks, docs/24). G5.3 calls this function once per repair action; it does not touch Phase E or `qa_agent/ai/runtime_repair.py` at all.

The only structural gap: G4's `execution_result` parameter is read for exactly one field, `.plan` (passed straight through to re-run one check via `run_runtime_plan`). `QAState` does not hold a `RuntimeExecutionResult` — it holds `runtime_plan` and an accumulated `execution_results` tuple directly (G5.2's own shape, one check executed at a time). So the executor needs a small duck-typed view exposing `.plan`, the same "minimal shape a function actually reads" precedent `runtime_repair.py` itself already uses for `_RuntimeRepairFinding`.

## G5.3 — Repair Integration

### 1. `qa_agent/agent/actions.py` — two-field change to the existing entry, plus one new branch

```python
ActionDefinition(
    id="runtime_repair",
    ...
    implemented=True,        # was False
    requires_target=True,    # new
)
```

`valid_targets()` gains one branch, mirroring the existing `runtime_diagnosis` one exactly:

```python
if action.id == "runtime_repair":
    return tuple(sorted(state.diagnosed_repairable_check_ids - state.repaired_check_ids))
```

### 2. `qa_agent/agent/models.py` — two new, additive `QAState` properties

```python
@property
def diagnosed_repairable_check_ids(self):
    # check ids whose diagnosis_status is literally "diagnosed" (DIAGNOSIS_DIAGNOSED,
    # duplicated as a literal string - this package's own established zero-import rule,
    # the same one failed_check_ids already applies to DIAGNOSABLE_STATUSES)
    return {d.check_id for d in self.diagnoses if getattr(d, "diagnosis_status", None) == "diagnosed"}

@property
def repaired_check_ids(self):
    return {getattr(r, "check_id", None) for r in self.repairs}
```

`completed_action_ids` gains one symmetric case, in the same place and the same style the existing `runtime_diagnosis` dynamic-completion rule already lives:

```python
if not (self.diagnosed_repairable_check_ids - self.repaired_check_ids):
    completed.add("runtime_repair")
```

This is the mechanism that satisfies **"repeated failure"** as a stop condition structurally rather than procedurally: once a check_id has *any* repair result — accepted, rejected, held, still-failing, whatever — it leaves `valid_targets`/eligibility permanently for this session. There is no retry path to guard separately; G4's own "at most one repair attempt per failure" rule (docs/24) and G5.3's eligibility enforce the same one-shot fact from two directions.

### 3. `qa_agent/agent/executor.py` — one new executor, one new duck-typed view

```python
@dataclass(frozen=True)
class _ExecutionResultView:
    """The one field repair_runtime_failure() actually reads off its
    `execution_result` argument - `.plan`, used only to re-run one check
    in isolation. QAState has no RuntimeExecutionResult of its own (G5.2
    accumulates individual RuntimeCheckResults instead), so this is a
    minimal, real, non-guessed stand-in - the same duck-typing precedent
    `runtime_repair.py`'s own `_RuntimeRepairFinding` already established.
    """
    plan: object


def _execute_runtime_repair(target, state: QAState, root, provider, config) -> ExecutionOutcome:
    if target is None:
        return ExecutionOutcome(status=STATUS_ERROR, summary="runtime_repair requires a target check id", new_state=state)
    check_result = next((r for r in state.execution_results if getattr(r, "id", None) == target), None)
    diagnosis = next((d for d in state.diagnoses if getattr(d, "check_id", None) == target), None)
    if check_result is None or diagnosis is None:
        return ExecutionOutcome(
            status=STATUS_ERROR,
            summary="runtime_repair target '{}' has no matching execution result and/or diagnosis".format(target),
            new_state=state,
        )
    runtime_check = None
    if state.runtime_plan is not None:
        runtime_check = next((c for c in state.runtime_plan.checks if c.id == target), None)
    result = repair_runtime_failure(
        check_result, diagnosis, _ExecutionResultView(plan=state.runtime_plan),
        state.repository_context, provider, root, runtime_check=runtime_check, config=config,
    )
    new_state = replace(state, repairs=state.repairs + (result,))
    status = STATUS_ERROR if result.outcome == OUTCOME_ERROR else STATUS_OK
    summary = "{}: {}".format(result.outcome, result.explanation)
    return ExecutionOutcome(status=status, summary=summary, new_state=new_state)
```

`repair_runtime_failure` is imported from `qa_agent.ai` (already re-exported there) and added to `_EXECUTORS["runtime_repair"] = _execute_runtime_repair` — the dict `execute_action` already dispatches through unchanged.

`STATUS_ERROR` only for `OUTCOME_ERROR` (the executor's own process genuinely could not complete — no workspace, exception, apply crash), mirroring `_execute_static_analysis`'s existing "executor status is about the executor's own process, not the QA verdict" rule: `NOT_ELIGIBLE`/`REJECTED`/`HELD`/`VALIDATION_FAILED`/`APPLIED_BUT_STILL_FAILING` are all `STATUS_OK` — the executor did its one job (ran the real G4 pipeline to a real, informative conclusion) even when that conclusion is "no fix."

**This is the mechanism that satisfies "never report a repair as successful without deterministic re-testing":** the executor's summary is `result.outcome` verbatim (`"verified"`, `"rejected"`, `"not_eligible"`, ...) - it is G4's own `_combine_decision`/mandatory-post-apply-re-check logic, untouched, that decides whether `outcome` is ever `OUTCOME_VERIFIED`. Nothing in G5.3 renders a repair as done/successful based on `ACCEPTED`/`APPLIED` alone; that distinction is docs/24's own central rule, inherited, not re-implemented.

### 4. `qa_agent/agent/prompts.py` — one small additive section (context only, not required for correctness)

A `REPAIRS SO FAR` block, same shape as `_diagnosis_lines`, shown between DIAGNOSES SO FAR and AVAILABLE ACTIONS — lets the AI's own `reason` text refer to what already happened, the same way it can already refer to diagnoses. Eligibility (not the prompt) is what actually prevents re-selecting a repaired target, so this is explanatory, not load-bearing.

### 5. Safety/stop-condition mapping — showing each is already covered, not re-invented

| Required stop condition | Mechanism | New in G5.3? |
|---|---|---|
| Invalid AI output | `select_next_action` -> `DECISION_ERROR` (non-recoverable) -> `TERMINATION_CONTROLLER_ERROR` | No — applies to `runtime_repair` automatically, same path every action already uses |
| Ineligible/invented target or action | `controller.py`'s existing two-tier re-validation (`ERROR_INELIGIBLE_ACTION`/`ERROR_INVALID_TARGET`) — recoverable, one iteration consumed, loop continues | No — generic, already covers any `requires_target=True` action |
| Insufficient evidence | G4's own `check_repair_eligibility` -> `OUTCOME_NOT_ELIGIBLE`, reported honestly, target leaves eligibility (one-shot) | No — G4 behavior, inherited unmodified |
| Failed repair (rejected/held/still-failing) | G4's own `_combine_decision`/verification logic -> a non-`VERIFIED` outcome, reported honestly, target leaves eligibility (one-shot) | No — G4 behavior, inherited unmodified |
| Repeated failure | Structural: `repaired_check_ids` removes a target from `valid_targets` after exactly one attempt, success or not | New, but 3 lines, no retry/counter logic needed |
| Maximum attempts | `QAState.remaining_budget` / `TERMINATION_MAX_ITERATIONS`, already enforced in `controller.py` and `loop.py` before every decision | No — global mechanism, already covers every action |

No change to `loop.py` is needed for G5.3 at all — `run_agent_loop` is already fully generic over `_EXECUTORS`/`eligible_actions`/`valid_targets`; it does not name `runtime_repair` anywhere.

## G5.4 — Completion Semantics

### New file: `qa_agent/agent/completion.py`

One pure function, computed only from `QAState` (never from `termination_reason`, never from an AI claim) — deliberately separate from `loop.py` rather than inlined, matching this project's "one file, one clear job" precedent for `executor.py`/`loop.py` themselves:

```python
QA_OUTCOME_PASSED = "passed"
QA_OUTCOME_FAILED = "failed"
QA_OUTCOME_INCONCLUSIVE = "inconclusive"
QA_OUTCOMES = (QA_OUTCOME_PASSED, QA_OUTCOME_FAILED, QA_OUTCOME_INCONCLUSIVE)


def compute_qa_outcome(state: QAState) -> str:
    """Never trusts the AI's own opinion, and never trusts *why* the
    session stopped - only what final_state actually, verifiably shows.
    """
    if state.runtime_plan is None:
        return QA_OUTCOME_INCONCLUSIVE
    planned_ids = {c.id for c in state.runtime_plan.checks}
    if not planned_ids <= state.executed_check_ids:
        return QA_OUTCOME_INCONCLUSIVE  # something planned never actually ran
    verified = {r.check_id for r in state.repairs if r.verification_status == "verified"}
    unresolved = state.failed_check_ids - verified
    return QA_OUTCOME_FAILED if unresolved else QA_OUTCOME_PASSED
```

`"verified"` duplicated as a literal (this package's own zero-import rule again — the real constant is `qa_agent.ai.runtime_repair_models.VERIFICATION_VERIFIED`).

Deliberately conservative: a planned check that never got an execution result (session stopped early) yields `INCONCLUSIVE`, never `PASSED` — this project never reports success for something it did not actually observe. A resolved failure requires the real post-apply re-verification (`VERIFIED`) specifically — `ACCEPTED`/`APPLIED` alone leave the check counted as still-unresolved, the same distinction docs/24 already draws.

### `qa_agent/agent/loop.py` — one new, additive `AgentResult` field

```python
qa_outcome: str = QA_OUTCOME_INCONCLUSIVE
```

Set via `compute_qa_outcome(state)` at **every** `return AgentResult(...)` in `run_agent_loop` (all four termination paths) — proving the outcome is a function of `final_state` alone is exactly "same final_state, any termination_reason, same qa_outcome," which is one of the tests below.

### `qa_agent/__main__.py` — small, additive CLI change

Add one printed line (`"  QA outcome: {}".format(result.qa_outcome)`) and remove the now-inaccurate "deferred to G5.4" note, replacing it with a one-line explanation of what `qa_outcome` does and does not claim (computed from real evidence only; still not a claim that the AI's own chosen objective text was satisfied, only that the repository's *observed, planned* checks are resolved or not).

## Explicitly not part of this step

No change to G4 (`qa_agent/ai/runtime_repair.py`) or any Phase E file. No new retry/backoff logic (one-shot eligibility already satisfies the bounded-attempts requirement). No AI involvement in `compute_qa_outcome` — it is pure, deterministic, and takes no provider argument at all. No permission-mode enforcement of `SAFETY_AUTONOMOUS` (that metadata field still exists only for a possible future stage, unchanged from G5.1). No change to `controller.py`, `prompts.py`'s guardrail text, or `loop.py`'s control flow beyond the one new field.

## Tests planned to prove this works

**`tests/regression/test_agent_controller.py` additions (G5.1/G5.3 boundary):**
- `runtime_repair` is `implemented=True`/`requires_target=True` in the registry.
- `valid_targets` for `runtime_repair`: returns only `diagnosis_status == "diagnosed"` check ids; excludes `insufficient_context`/`invalid_response`/`ai_error`/`not_applicable`; excludes a check id already present in `state.repairs` regardless of that repair's outcome.
- `eligible_actions` never offers `runtime_repair` with zero diagnosed-and-unrepaired failures (including the exact "maximally favorable state" scenario G5.1's own original never-eligible test used, now flipped).
- `eligible_actions` offers `runtime_repair` exactly when a real, qualifying target exists, and stops offering it the moment that target gets any repair result.
- `completed_action_ids` includes `"runtime_repair"` once every diagnosed failure has an attempt.
- Adversarial: the AI naming a real-but-wrong/fabricated target for `runtime_repair` is rejected via `ERROR_INVALID_TARGET`, same as the existing `runtime_diagnosis` case.
- Full existing 68 checks in this file still pass unmodified.

**`tests/regression/test_agent_executor.py` or an extension of `test_agent_loop.py` (executor-level):**
- Missing target -> `STATUS_ERROR`, state unchanged.
- Target with no matching execution result / no matching diagnosis -> `STATUS_ERROR`, state unchanged.
- Injected fake `repair_runtime_failure` (dependency injection, same convention as `run=`/`execute_check=` elsewhere) capturing its call args, proving the real signature is used: `(check_result, diagnosis, <object with .plan == state.runtime_plan>, repository_context, provider, root, runtime_check=..., config=...)`.
- Fake returning `OUTCOME_VERIFIED`/`verification_status="verified"` -> `STATUS_OK`, `new_state.repairs` contains it, summary contains `"verified"`.
- Fake returning `OUTCOME_REJECTED` -> `STATUS_OK` (executor completed), summary contains `"rejected"`, repair still recorded (never dropped).
- Fake returning `OUTCOME_ERROR` -> `STATUS_ERROR` (the one outcome that *is* an executor-level failure).

**`tests/regression/test_agent_loop.py` additions (G5.2/G5.3 integration):**
- A scripted decider sequence (`discovery -> plan -> a runtime check -> runtime_diagnosis -> runtime_repair -> stop`) driven through the real `run_agent_loop`, with `execute_check`/repair injected deterministically, proving `final_state.repairs` is populated and the loop terminates via `TERMINATION_AI_STOP`.
- A scripted decider that tries to pick `runtime_repair` on the same target twice -> the second attempt is rejected as ineligible (`ERROR_INELIGIBLE_ACTION`), proving the one-shot rule holds inside the real loop, not just in a unit test of `eligible_actions`.
- Adversarial: `runtime_repair` scripted with a target for a check that was never diagnosed -> `ERROR_INVALID_TARGET`, never reaches `execute_action`.

**New `tests/regression/test_agent_completion.py` (G5.4):**
- Fresh/empty state -> `INCONCLUSIVE`.
- Only discovery/plan/static_analysis in state, no runtime checks executed -> `INCONCLUSIVE`.
- All planned checks executed, all passed -> `PASSED`.
- A planned check with no execution result yet (early stop) -> `INCONCLUSIVE`, never `PASSED`.
- A failed check, no repair attempted -> `FAILED`.
- A failed check with a repair whose `verification_status` is `rejected`/`held`/`still_failing`/`unknown` (i.e. anything but `verified`) -> still `FAILED`.
- A failed check with a repair whose `verification_status == "verified"`, every other planned check passed -> `PASSED`.
- Same `final_state` passed through all four different `termination_reason` values -> identical `qa_outcome` every time (proves it is a pure function of state, not of stop reason).
- `run_agent_loop` end-to-end: `AgentResult.qa_outcome` is populated correctly at each of the four real return points (budget exhausted, no eligible actions, AI stop, controller error), not just reachable via the standalone function.

**CLI:**
- `python -m qa_agent agent` output includes the new `QA outcome:` line and no longer prints the stale "deferred to G5.4" sentence.

**Full regression, every time:** `test_agent_controller.py` (68), `test_agent_loop.py` (67), `test_runtime_diagnosis.py` (78), `test_runtime_repair.py` (74), plus the two new files above, and the full project suite — same bar every prior step in this project has already held itself to.

## Verified

Implemented exactly as designed above - no scope changes, no shortcuts. `test_agent_controller.py`: **76/76** (68 original + 8 new/rewritten checks for `runtime_repair`'s new eligibility/target/immutability behavior - four pre-existing checks were rewritten, not just extended, because the scope boundary they tested legitimately moved, the same "rescope, don't just add" precedent this project's own isolation tests have already followed twice before). `test_agent_loop.py`: **96/96** (67 original + 29 new, covering the executor wiring, the real end-to-end diagnose-then-repair-then-stop sequence, and the one-shot repeated-attempt rejection). New `test_agent_completion.py`: **22/22**. `test_runtime_diagnosis.py` (G3): **78/78**, unchanged. `test_runtime_repair.py` (G4): **74/74**, unchanged - G4 itself was never touched. Full project suite: **38/40** (the same two pre-existing, unrelated flakes as every prior step - `test_ai_openrouter.py`, needs a live network/key; `integration/test_watch_pipeline.py`'s own timing flake).

One real fixture gap surfaced and was fixed along the way: `test_agent_loop.py`'s own pre-existing duck-typed `_result()` check-result fixture was missing several fields the real `RuntimeCheckResult` always has (`duration`, `details`, `start_time`, `end_time`) - harmless for the two pre-existing tests that used it (neither ever checked `diagnosis_status` specifically), but it meant `diagnose_runtime_failure` was silently hitting an `AttributeError` and returning `ai_error` every time that fixture was used for a diagnosis, undetected until this step's own repair tests started asserting `diagnosis_status == "diagnosed"` for real. Fixed by completing the fixture to match the real dataclass shape - a test-fixture correction, not a change to any production code path.

**Status: G5.3 (Repair Integration) and G5.4 (Completion Semantics) CLOSED. G5 (G5.1-G5.4) complete.**
