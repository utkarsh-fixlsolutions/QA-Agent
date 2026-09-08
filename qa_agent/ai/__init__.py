"""AI provider, prompting, explanation, summary, and suggested-fix
foundation (Phase D Parts 1-5). See docs/step-log.md.

Isolated from the deterministic analyzer pipeline in one direction only:
nothing in `runner.py`, `adapters.py`, `config.py`, `analysis_bridge.py`,
`__main__.py`, or watch mode imports this package. `report.py` accepts an
optional explanations mapping, an optional summary, and an optional
suggested-fixes mapping to render (Phase D Parts 3-5), but nothing calls
`explain_findings()`/`summarize_run()`/`suggest_fixes()` and passes their
result in yet - CLI flags and `.qa-agent.json` integration are later Phase
D parts, not this one. A suggested fix is advisory only and is never
applied automatically - QA-Agent never edits a source file anywhere in
this project.
"""

from .context import CodeContext, extract_context
from .explainer import Explanation, explain_finding, explain_findings
from .fixer import SuggestedFix, suggest_fix, suggest_fixes
from .mock import MockProvider
from .ollama import OllamaProvider
from .prompts import (
    GUARDRAILS,
    Prompt,
    build_explanation_prompt,
    build_fix_prompt,
    build_summary_prompt,
)
from .provider import AIProvider, ConnectionResult, LLMResponse
from .response_parser import (
    parse_json_response,
    strip_markdown_fence,
    validate_explanation_response,
    validate_fix_response,
    validate_summary_response,
)
from .schemas import (
    STATUS_INSUFFICIENT_CONTEXT,
    STATUS_INVALID,
    STATUS_SUCCESS,
    ExplanationResponse,
    FixResponse,
    SummaryResponse,
    ValidationResult,
)
from .summarizer import Summary, summarize_run

__all__ = [
    "AIProvider",
    "CodeContext",
    "ConnectionResult",
    "Explanation",
    "ExplanationResponse",
    "FixResponse",
    "GUARDRAILS",
    "LLMResponse",
    "MockProvider",
    "OllamaProvider",
    "Prompt",
    "STATUS_INSUFFICIENT_CONTEXT",
    "STATUS_INVALID",
    "STATUS_SUCCESS",
    "SuggestedFix",
    "Summary",
    "SummaryResponse",
    "ValidationResult",
    "build_explanation_prompt",
    "build_fix_prompt",
    "build_summary_prompt",
    "explain_finding",
    "explain_findings",
    "extract_context",
    "parse_json_response",
    "strip_markdown_fence",
    "suggest_fix",
    "suggest_fixes",
    "summarize_run",
    "validate_explanation_response",
    "validate_fix_response",
    "validate_summary_response",
]
