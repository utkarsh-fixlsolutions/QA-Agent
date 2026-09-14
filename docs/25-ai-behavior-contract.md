# Step 25 — AI Behavior Contract (pre-G5 hardening)

**Status:** IMPLEMENTED (2026-09-11).
**Phase:** none — an explicit, deliberate pause between Phase G Part 4 and any future G5 orchestration work. No new phase, no G5 code, no G1-G4 behavior change. This document and its small, additive code changes exist to make explicit what G1-G4 already do in practice, close two genuinely missing prompt-guardrail gaps, and record what G5 will be able to assume without designing G5 itself.
**Scope:** a new `qa_agent/ai/contract.py` module (three short, additive prompt-text constants); their insertion into the existing `DIAGNOSIS_GUARDRAILS`/`REPAIR_GUARDRAILS` constants without touching a single existing sentence; a small export fix (`build_runtime_repair_prompt` was missing from `qa_agent/ai/__init__.py`); six new tests proving what wasn't yet directly asserted. No schema changed. No validator changed. No existing prompt body was rewritten.

## Why this exists

G3/G4 already implement almost everything a "behavior contract" would ask for — they just don't say so in one place, and an audit of the actual prompt text found two real, narrow gaps rather than a systemic problem. This document is that one place, and the code changes close exactly those two gaps, nothing more.

## The contract

### Role

The AI is a QA reasoning engine — an interpreter and strategist, never an authority. It answers two questions, depending on which prompt it's given: *why did this already-determined runtime failure happen* (G3), and *what specific, minimal source change would plausibly fix it* (G4). It never decides whether a check passed or failed, never decides whether its own repair is correct, and never executes anything.

### Input

The model receives only what a prompt builder explicitly assembles from already-produced, real data:

- `build_diagnosis_prompt`: a real `RuntimeCheckResult` (status, reason, exception, bounded/redacted logs), the originating `RuntimeCheck` when known (category, planning reason), and a curated subset of `RepositoryContext` (repository/application type, languages, frameworks, constraints).
- `build_runtime_repair_prompt`: the same `RuntimeCheckResult`, the `RuntimeDiagnosis` already produced for it (explicitly labeled a hypothesis, not settled fact), the same curated `RepositoryContext` subset, and a real `CodeContext` (bounded surrounding source lines around a deterministically-resolved target line).

Nothing is ever assembled from a live filesystem walk, a live shell command, or anything not already captured as data before the prompt is built.

### Evidence rule: FACT / HYPOTHESIS / UNKNOWN

Treated as a behavioral/prompt contract, not a new claim-level schema — deliberately. The existing response shapes already separate these three states across distinct fields; this section documents that mapping rather than adding a fourth field that would just restate it:

- **FACT** — `observed_evidence`/`evidence`: a short quote or paraphrase of something that literally appears in the supplied evidence. Grounded structurally: `affected_files`/`affected_components` claims are checked against the real evidence (`response_is_grounded`) and rejected outright if unsupported.
- **HYPOTHESIS** — `root_cause`/`explanation`: the model's own inference, explicitly instructed to be framed as inference, never certainty, unless the evidence actually proves it (`DIAGNOSIS_GUARDRAILS`'s original text, unchanged).
- **UNKNOWN** — the sanctioned `{"insufficient_context": true, "reason": "..."}` decline, present in every prompt this project builds. Never converted into a confident claim: `validate_diagnosis_response`/`validate_repair_response` recognize this shape explicitly and short-circuit before any other field is even checked.

Two things this exact split didn't yet cover, closed by this step's own additive changes (`qa_agent/ai/contract.py`):

- **Contradictory evidence.** Neither guardrail previously told the model what to do when the evidence points more than one way. `CONTRADICTORY_EVIDENCE_CLAUSE` now instructs: don't resolve a conflict by picking arbitrarily — name the possibilities or decline, and lower confidence.
- **Free-text authority.** Both prompts' own `_ROLE` text already said "you are not the authority on whether it passed or failed" — but that only guards the status field. Nothing stopped a model from writing a `summary`/`explanation` that quietly implies a different outcome in prose. `DETERMINISTIC_AUTHORITY_CLAUSE` now says explicitly: the deterministic result is authoritative for *every* fact it reports, not just its status field, and no free text may imply otherwise. Proven directly by a new test (`test_ai_free_text_claims_never_override_the_deterministic_status`): a maximally misleading "everything passed" response still produces a `RuntimeDiagnosis.execution_status` copied verbatim from the real, unchanged `RuntimeCheckResult.status` — because that field is populated from the deterministic result's own attribute in `diagnosis.py`'s code, never derived from the parsed model response at all. This is a structural guarantee, not a content filter.

### Action rule

The AI may recommend an action (`recommended_action`, already a free-text field — **left unchanged**, per this step's own explicit scope). It does not select, authorize, or execute one. G4 is the concrete, already-built proof this separation works in practice: `check_repair_eligibility()` and `resolve_repair_target()` are 100% deterministic, read only structured fields (`diagnosis_status`, `affected_files`, `observed_evidence`), and never consult `summary`/`root_cause`/`explanation` prose for any decision. `decide_repair()` (Phase E Part 4) reads only measured analyzer/runtime output, never the AI's own `confidence` or `explanation`. This project does not yet have a formal action-selection protocol for a multi-step loop — deliberately not designed here (see "Deferred to G5" below).

### Output contract

Every structured response reuses an existing, already-tested schema and validator — no duplicate validation system exists or was added:

- Diagnosis: `DiagnosisResponse` / `validate_diagnosis_response` (Phase G Part 3).
- Repair: `RepairResponse` / `validate_repair_response` (Phase E Part 1) — G4's own prompt (`build_runtime_repair_prompt`) deliberately targets this exact schema rather than inventing a parallel one, so the same validator parses both a static-analysis repair proposal and a runtime one.

### Failure behavior

- **Insufficient evidence** → the sanctioned decline shape → `DIAGNOSIS_INSUFFICIENT_CONTEXT` / the repair equivalent. Never guessed past.
- **Contradictory evidence** → now explicitly instructed (see above) to decline or name the conflict rather than pick arbitrarily; not separately validator-enforced (there is no deterministic way to detect "the model resolved an ambiguity it should have flagged" from the response alone — this is a prompt-level instruction, not a new check).
- **Unparseable/malformed response** → `STATUS_INVALID` → `DIAGNOSIS_INVALID_RESPONSE` / `PROPOSAL_FAILED`. Never silently interpreted as valid; already exhaustively tested.
- **A claim the evidence doesn't support** (`affected_files`/`affected_components`) → rejected outright by `response_is_grounded`, already tested.
- **A claim of success that contradicts the real status** — this is the one gap named honestly rather than solved with new code: there is no general-purpose validator that reads `summary`/`root_cause` prose and checks it against `execution_status` (that would be exactly the "hallucination detector" G3's own original design explicitly declined to build, and duplicating/extending validators was out of this step's agreed scope). What *is* true, and is now the thing actually tested: nothing downstream ever consults that prose as if it were authoritative. `execution_status` is copied from the real result, not derived from the model. G4's eligibility gate never reads `summary` at all. The risk is contained architecturally, not filtered out of the text.

### Grounding requirements

Unchanged, already-existing, already-tested: `response_is_grounded` (G3) checks every `affected_files`/`affected_components` claim against real captured logs/reason/exception, the originating `RuntimeCheck`'s `required_evidence`, and `RepositoryContext`'s known languages/frameworks/files/directories. `resolve_repair_target`/`_locate_line` (G4) never invent a line number — a real evidence-token match anchors a real line, no match anchors honestly at line 1, explicitly marked `localized=False`.

### Repository context: bounded, curated, never a full dump

Audited, not assumed: `diagnosis_prompts._repository_context_lines()` and `runtime_repair_prompts._repository_context_lines()` already rendered only `repository_type`, `application_type`, `languages`, `frameworks` (plus `constraints` for diagnosis) — never `architecture_summary`, `repository_layout`, or `known_limitations`, and never a full `RepositoryContext` dump. This was true before this step; it is now directly tested (`test_diagnosis_prompt_repository_context_is_curated_not_a_full_dump` / the repair equivalent), constructing a `RepositoryContext` with 50-entry `architecture_summary`/`repository_layout`/`known_limitations` lists and confirming none of it leaks into the built prompt.

Phase D (`explain`/`fix`/`summary` prompts) does not receive `RepositoryContext` at all, and integrating it is explicitly out of scope for this step — not evaluated as "should it," left exactly as it already was.

### Deterministic authority

Restated, not re-implemented: Phase G Part 2's `RuntimeCheckResult.status` remains the sole authority on pass/fail (Phase G Part 3's own long-standing rule). Phase E Part 4's `decide_repair()` remains the sole authority on whether a candidate repair is accepted (Phase G Part 4's own long-standing rule, including the fact that its own "target resolved" signal is a static-regression guard for a runtime repair, not the real signal — documented already in `docs/24`). Nothing in this step changes either.

### Provider/model independence

Unchanged: `AIProvider` remains a `Protocol`; `OllamaProvider`/`MockProvider` remain the only two implementations; the model string remains a pure runtime parameter (`--ai-model`, `ai.model` config). This step adds no model-specific logic anywhere — the same guardrail text is sent regardless of which Ollama model answers it, and `qwen2.5:0.5b` continues to work unmodified (reconfirmed by rerunning the full G3/G4 suites, which use `MockProvider` throughout, plus the CLI's own `--ai-provider mock` tests).

## Deferred to G5 (named, not designed)

A future G5 orchestrator will need to close the loop this project's architecture already anticipates but has not yet built:

```
OBSERVE → INTERPRET → REQUEST/RECOMMEND ALLOWED ACTION → DETERMINISTIC CONTROLLER
    → TOOL EXECUTION → NEW EVIDENCE → REPEAT → FINAL QA RESULT
```

G1-G4 already provide OBSERVE (Phase F/G1/G2's own deterministic output) and INTERPRET (G3's diagnosis, G4's repair proposal) as real, tested, working pieces. What does not exist yet, and is deliberately not designed in this step:

- A structured, machine-actionable "recommended next action" schema. `recommended_action` stays exactly what it already was — a free-text string a person reads — per this step's own explicit instruction not to lock in an action-selection protocol before G5's own design exists.
- Any controller loop that calls the AI more than once per QA failure, or that lets an AI recommendation influence which deterministic tool runs next.
- Any relaxation of the rule that the AI never decides success — G5 will need its own explicit statement of how a multi-step loop's *final* QA result is decided, and that is G5's design question, not this step's.

## What was NOT changed

Per this step's own agreed scope: no G3/G4 prompt body was rewritten (only additive insertions, verified safe against every existing test — no test in this project asserts exact prompt-string equality anywhere); no new schema or validator was created; no claim-level FACT/HYPOTHESIS/UNKNOWN tagging was added to any response shape; `recommended_action` is untouched; Phase D's prompt builders/signatures/call sites are untouched; the default AI model/provider configuration is untouched; no G5 orchestrator, action-selection schema, agent framework, browser automation, or arbitrary command execution was introduced.

## Files changed

**New:** `qa_agent/ai/contract.py` (three short prompt-text constants); `tests/regression/test_ai_behavior_contract.py` (6 new test functions, 21 checks); this document.
**Modified, additive only:** `qa_agent/ai/diagnosis_prompts.py` (`DIAGNOSIS_GUARDRAILS` gains two new sentences, inserted between existing ones, nothing removed); `qa_agent/ai/runtime_repair_prompts.py` (`REPAIR_GUARDRAILS` gains three new sentences, same treatment); `qa_agent/ai/__init__.py` (+exports for the three new constants, plus a genuine small fix found during the audit: `build_runtime_repair_prompt` was never re-exported from G4's own work — corrected here since it blocked writing a direct test against it, not a new feature).
**Untouched, byte-for-byte:** every schema (`schemas.py`), every parser/validator (`response_parser.py`, `diagnosis_parser.py`), every Phase E repair module, `diagnosis.py`/`runtime_repair.py`'s own orchestration logic, `RepositoryContext`/`ProjectKnowledge`, Phase D's prompt builders and call sites, the default model/provider configuration.

## Test results

`test_ai_behavior_contract.py`: **21/21 checks** — the three contract constants are non-empty and distinct; both guardrail constants carry the new clauses while keeping their original, untouched `insufficient_context` instruction intact; both prompt builders stay curated/bounded against a deliberately oversized `RepositoryContext`; and the free-text-never-overrides-deterministic-status guarantee is proven directly rather than assumed.

`test_runtime_diagnosis.py`: **78/78** (unchanged from before this step). `test_runtime_repair.py`: **74/74** (unchanged). Full suite: **29/30** (the one failure is `integration/test_watch_pipeline.py`'s pre-existing, unrelated timing flake, unrelated to this step).

**Status: AI Behavior Contract CLOSED. G5 not started.**
