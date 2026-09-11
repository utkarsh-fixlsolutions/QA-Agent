"""AI provider, prompting, explanation, summary, suggested-fix, and repair-
proposal foundation (Phase D Parts 1-6, Phase E Part 1). See docs/step-log.md.

Isolated from the deterministic analyzer pipeline in one direction only:
nothing in `runner.py`, `adapters.py`, `config.py`, `analysis_bridge.py`,
`watch.py`, or its supporting modules imports this package - only
`__main__.py` does, deliberately, as the one composition root that wires
enabled AI features into the CLI (Phase D Part 6). `report.py` accepts an
optional explanations mapping, an optional summary, and an optional
suggested-fixes mapping to render (Phase D Parts 3-5). A suggested fix is
advisory only and is never applied automatically - QA-Agent never edits a
source file anywhere in this project.

`repair.py` (Phase E Part 1) adds a structured `RepairProposal` alongside
`SuggestedFix` - same never-invents-a-finding, never-applied-automatically
guarantees, but with a self-reported confidence and an explicit line range
for a later phase to build on. It is not wired into `__main__.py` or any
other pipeline module - reachable only by importing `qa_agent.ai` directly,
same as every other AI feature was before its own CLI-integration part.

`workspace.py` (Phase E Part 2) applies a `RepairProposal` inside an
isolated temporary directory, never the user's real project - stdlib only,
and independent of the provider/prompt/schema modules above (it consumes a
proposal purely as data). Also not wired into `__main__.py` or any pipeline
module; no patch is ever written back to a real file anywhere in this
project yet.

`validator.py` (Phase E Part 3) is the one exception to "nothing in this
package imports the deterministic engine, only the other way around" - it
deliberately imports `runner.run` to rerun the real analyzers against a
Part 2 workspace and measure whether a repair actually helped. The
one-directional isolation rule itself is unaffected: pipeline modules still
never import `qa_agent.ai`. Its result type is `RepairValidationResult`,
not `ValidationResult` - `schemas.py` already uses that name for an
unrelated concept (LLM-response validation) and this module does not
shadow it. Also not wired into `__main__.py` or any pipeline module, and
the AI never grades its own repair - only the rerun analyzers' own output
does.

`decision.py` (Phase E Part 4) is a pure, deterministic policy over a
`RepairValidationResult`: `decide_repair()` reads only its `status` and
`ValidationComparison`'s counts/flags - never a provider, never AI-
generated text, never a rerun analyzer - to decide whether a repair is an
eligible *candidate* (`accept_candidate`/`reject`/`hold`/
`validation_failed`). "Accepted" here never means the real project was
modified; nothing in this module writes anywhere. Also not wired into
`__main__.py` or any pipeline module.

`apply.py` (Phase E Part 5) is the one module in this whole package that
writes to a real project file - and only for a decision whose `action` is
exactly `accept_candidate`, and only after re-verifying every precondition
against the file's *current* state. Every other decision is refused before
anything is touched; the write itself is backup-then-atomic-replace, with
automatic restore on failure. `report.py` accepts one further optional
parameter, `repair_result`, to render "Verified Repair Applied"/"Repair
Skipped" (Phase E Part 5) - omitted or `None`, output stays byte-for-byte
identical to every earlier phase. Not wired into `__main__.py`, any
pipeline module, or any CLI flag - `approve_repair()`/`reject_repair()` are
plain service functions for a later interactive layer to call.

`repair_loop.py` (Phase E Part 6) is the orchestrator: `run_repair_loop()`
calls Parts 1-5, in order, once per finding, up to a bounded
`max_iterations` - inventing no repair logic of its own and never letting
the AI decide success. Like `validator.py`, it deliberately imports
`runner.run` (needed by `validate_repair`, which it calls); the
isolation rule stays one-directional. Its own `STATUS_VALIDATION_FAILED`
is intentionally not re-exported here - `validator.py`'s already-exported
constant of the same name carries the identical value, and re-exporting a
second module's constant under that name would shadow it. Also not wired
into `__main__.py`, any pipeline module, watch mode, or any CLI flag.

`diagnosis.py`/`diagnosis_models.py`/`diagnosis_prompts.py`/
`diagnosis_parser.py` (Phase G Part 3) are this package's second exception
to "nothing in this package imports the deterministic engine" -
`diagnose_runtime_failure`/`diagnose_runtime_failures` read a real
`qa_agent.runtime.RuntimeCheckResult`/`RuntimeExecutionResult` and a real
`qa_agent.project.RepositoryContext` as plain input data, the same
direction `validator.py` already established for `runner.run`'s output.
The reverse is never true: `qa_agent/runtime/` still imports nothing from
this package, enforced by its own isolation tests. The AI never determines
whether a runtime check passed or failed - Phase G Part 2's own
`RuntimeCheckResult.status` remains the sole authority; this module only
interprets evidence for checks already determined to have failed
(`fail`/`timeout`/`error`), returning `NOT_APPLICABLE` with zero provider
calls for anything else. Every claim in a returned `RuntimeDiagnosis` is
checked against the real supplied evidence before being accepted
(`response_is_grounded`) - an unsupported claim becomes `INVALID_RESPONSE`,
never a silently-accepted fabrication. Wired into the CLI only via
`discover --diagnose` (implies `--execute-runtime-plan`); AI is off by
default, exactly like every other AI feature in this project.
"""

from .apply import (
    RepairApplicationResult,
    apply_verified_repair,
    approve_repair,
    reject_repair,
)
from .context import CodeContext, extract_context
from .diagnosis import (
    diagnose_runtime_failure,
    diagnose_runtime_failures,
    diagnosis_to_dict,
    diagnoses_to_json,
    render_diagnosis,
)
from .diagnosis_models import (
    DIAGNOSIS_AI_ERROR,
    DIAGNOSIS_DIAGNOSED,
    DIAGNOSIS_INSUFFICIENT_CONTEXT,
    DIAGNOSIS_INVALID_RESPONSE,
    DIAGNOSIS_NOT_APPLICABLE,
    DIAGNOSIS_STATUSES,
    SEVERITIES,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    RuntimeDiagnosis,
)
from .diagnosis_parser import DiagnosisResponse, response_is_grounded, validate_diagnosis_response
from .diagnosis_prompts import build_diagnosis_prompt
from .decision import (
    ACTION_ACCEPT_CANDIDATE,
    ACTION_HOLD,
    ACTION_REJECT,
    ACTION_VALIDATION_FAILED,
    RepairDecision,
    decide_repair,
)
from .explainer import Explanation, explain_finding, explain_findings
from .fixer import SuggestedFix, suggest_fix, suggest_fixes
from .mock import MockProvider
from .ollama import OllamaProvider
from .prompts import (
    GUARDRAILS,
    Prompt,
    build_explanation_prompt,
    build_fix_prompt,
    build_repair_prompt,
    build_summary_prompt,
)
from .provider import AIProvider, ConnectionResult, LLMResponse
from .repair import RepairProposal, propose_repair, propose_repairs
from .repair_loop import (
    DEFAULT_MAX_ITERATIONS,
    STATUS_APPLIED,
    STATUS_APPLY_FAILED,
    STATUS_ERROR,
    STATUS_HELD,
    STATUS_NO_PROPOSAL,
    STATUS_REJECTED,
    STATUS_WORKSPACE_FAILED,
    RepairAttemptOutcome,
    RepairLoopResult,
    RepairLoopStatistics,
    run_repair_loop,
)
from .response_parser import (
    parse_json_response,
    strip_markdown_fence,
    validate_explanation_response,
    validate_fix_response,
    validate_repair_response,
    validate_summary_response,
)
from .runtime_repair import (
    check_repair_eligibility,
    propose_runtime_repair,
    render_runtime_repair_result,
    repair_runtime_failure,
    repair_runtime_failures,
    resolve_repair_target,
    runtime_repair_result_to_dict,
    runtime_repair_results_to_json,
)
from .runtime_repair_models import (
    APPLY_NOT_ATTEMPTED,
    ELIGIBILITY_ELIGIBLE,
    ELIGIBILITY_NOT_ELIGIBLE,
    OUTCOME_ACCEPTED,
    OUTCOME_APPLIED,
    OUTCOME_APPLIED_BUT_STILL_FAILING,
    OUTCOME_ERROR,
    OUTCOME_HELD,
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_PROPOSAL_FAILED,
    OUTCOME_REJECTED,
    OUTCOME_VALIDATION_FAILED,
    OUTCOME_VERIFIED,
    OUTCOMES as RUNTIME_REPAIR_OUTCOMES,
    VERIFICATION_NOT_APPLICABLE,
    VERIFICATION_STILL_FAILING,
    VERIFICATION_UNKNOWN,
    VERIFICATION_VERIFIED,
    EligibilityResult,
    RuntimeRepairResult,
    TargetResolution,
)
from .schemas import (
    STATUS_INSUFFICIENT_CONTEXT,
    STATUS_INVALID,
    STATUS_SUCCESS,
    ExplanationResponse,
    FixResponse,
    RepairResponse,
    SummaryResponse,
    ValidationResult,
)
from .summarizer import Summary, summarize_run
from .validator import (
    STATUS_IMPROVED,
    STATUS_UNCHANGED,
    STATUS_VALIDATION_FAILED,
    STATUS_WORSENED,
    RepairValidationResult,
    ValidationComparison,
    compare_results,
    validate_repair,
)
from .workspace import (
    AppliedRepair,
    TemporaryWorkspace,
    apply_repair,
    cleanup_workspace,
    create_workspace,
)

__all__ = [
    "ACTION_ACCEPT_CANDIDATE",
    "ACTION_HOLD",
    "ACTION_REJECT",
    "ACTION_VALIDATION_FAILED",
    "AIProvider",
    "APPLY_NOT_ATTEMPTED",
    "AppliedRepair",
    "CodeContext",
    "ConnectionResult",
    "DEFAULT_MAX_ITERATIONS",
    "DIAGNOSIS_AI_ERROR",
    "DIAGNOSIS_DIAGNOSED",
    "DIAGNOSIS_INSUFFICIENT_CONTEXT",
    "DIAGNOSIS_INVALID_RESPONSE",
    "DIAGNOSIS_NOT_APPLICABLE",
    "DIAGNOSIS_STATUSES",
    "DiagnosisResponse",
    "ELIGIBILITY_ELIGIBLE",
    "ELIGIBILITY_NOT_ELIGIBLE",
    "EligibilityResult",
    "Explanation",
    "ExplanationResponse",
    "FixResponse",
    "GUARDRAILS",
    "LLMResponse",
    "MockProvider",
    "OllamaProvider",
    "OUTCOME_ACCEPTED",
    "OUTCOME_APPLIED",
    "OUTCOME_APPLIED_BUT_STILL_FAILING",
    "OUTCOME_ERROR",
    "OUTCOME_HELD",
    "OUTCOME_NOT_ELIGIBLE",
    "OUTCOME_PROPOSAL_FAILED",
    "OUTCOME_REJECTED",
    "OUTCOME_VALIDATION_FAILED",
    "OUTCOME_VERIFIED",
    "Prompt",
    "RepairApplicationResult",
    "RepairAttemptOutcome",
    "RepairDecision",
    "RepairLoopResult",
    "RepairLoopStatistics",
    "RepairProposal",
    "RepairResponse",
    "RepairValidationResult",
    "RuntimeDiagnosis",
    "RuntimeRepairResult",
    "RUNTIME_REPAIR_OUTCOMES",
    "SEVERITIES",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "STATUS_APPLIED",
    "STATUS_APPLY_FAILED",
    "STATUS_ERROR",
    "STATUS_HELD",
    "STATUS_IMPROVED",
    "STATUS_INSUFFICIENT_CONTEXT",
    "STATUS_INVALID",
    "STATUS_NO_PROPOSAL",
    "STATUS_REJECTED",
    "STATUS_SUCCESS",
    "STATUS_UNCHANGED",
    "STATUS_VALIDATION_FAILED",
    "STATUS_WORKSPACE_FAILED",
    "STATUS_WORSENED",
    "SuggestedFix",
    "Summary",
    "SummaryResponse",
    "TargetResolution",
    "TemporaryWorkspace",
    "VERIFICATION_NOT_APPLICABLE",
    "VERIFICATION_STILL_FAILING",
    "VERIFICATION_UNKNOWN",
    "VERIFICATION_VERIFIED",
    "ValidationComparison",
    "ValidationResult",
    "apply_repair",
    "apply_verified_repair",
    "approve_repair",
    "build_explanation_prompt",
    "build_fix_prompt",
    "build_repair_prompt",
    "build_summary_prompt",
    "build_diagnosis_prompt",
    "check_repair_eligibility",
    "cleanup_workspace",
    "compare_results",
    "create_workspace",
    "decide_repair",
    "diagnose_runtime_failure",
    "diagnose_runtime_failures",
    "diagnoses_to_json",
    "diagnosis_to_dict",
    "explain_finding",
    "explain_findings",
    "extract_context",
    "parse_json_response",
    "propose_repair",
    "propose_repairs",
    "propose_runtime_repair",
    "reject_repair",
    "render_diagnosis",
    "render_runtime_repair_result",
    "repair_runtime_failure",
    "repair_runtime_failures",
    "resolve_repair_target",
    "response_is_grounded",
    "run_repair_loop",
    "runtime_repair_result_to_dict",
    "runtime_repair_results_to_json",
    "strip_markdown_fence",
    "suggest_fix",
    "suggest_fixes",
    "summarize_run",
    "validate_diagnosis_response",
    "validate_explanation_response",
    "validate_fix_response",
    "validate_repair",
    "validate_repair_response",
    "validate_summary_response",
]
