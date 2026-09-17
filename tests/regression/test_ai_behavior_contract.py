"""AI Behavior Contract (docs/25-ai-behavior-contract.md).

Audits the additive-only guardrail clauses in `qa_agent/ai/contract.py`,
confirms they actually reached both the diagnosis and repair prompts
without disturbing anything already there, and proves two structural
guarantees the earlier G3/G4 test suites established in practice but never
asserted directly: `RepositoryContext` stays a small, curated subset in
every prompt that includes it - never a full dump - and the AI's own free
text can never override what the deterministic system actually recorded.

Every other behavior this contract describes (insufficient-context
decline, ungrounded-claim rejection, malformed-response handling,
provider-failure grace, no-invented-line-numbers, confidence validation)
already has direct, exhaustive coverage in test_runtime_diagnosis.py and
test_runtime_repair.py - deliberately not duplicated here, per this
project's own "behavioral coverage, not a specific number of tests" rule.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite  # noqa: E402

from qa_agent.ai import (  # noqa: E402
    CANDIDATE_ONLY_CLAUSE,
    CONTRADICTORY_EVIDENCE_CLAUSE,
    DETERMINISTIC_AUTHORITY_CLAUSE,
    DIAGNOSIS_DIAGNOSED,
    MockProvider,
    RuntimeDiagnosis,
    build_diagnosis_prompt,
    build_runtime_repair_prompt,
    diagnose_runtime_failure,
    extract_context,
)
from qa_agent.ai.diagnosis_prompts import DIAGNOSIS_GUARDRAILS  # noqa: E402
from qa_agent.ai.runtime_repair_prompts import REPAIR_GUARDRAILS  # noqa: E402
from qa_agent.project.context import RepositoryContext  # noqa: E402
from qa_agent.project.models import DetectedItem, ProjectKnowledge  # noqa: E402
from qa_agent.runtime.execution_models import STATUS_FAIL, RuntimeCheckResult  # noqa: E402


def _project(**overrides):
    kwargs = dict(root_path="C:/fake", repository_type="single-package", application_type="backend")
    kwargs.update(overrides)
    return ProjectKnowledge(**kwargs)


def _check_result(status=STATUS_FAIL, reason="build failed", logs=()):
    return RuntimeCheckResult(
        id="build-verification", name="Build Verification", status=status,
        start_time="", end_time="", duration=1.0, reason=reason, logs=logs,
    )


def _diagnosis_for(check_result):
    return RuntimeDiagnosis(
        check_id=check_result.id, check_name=check_result.name,
        execution_status=check_result.status, diagnosis_status=DIAGNOSIS_DIAGNOSED,
        summary="s", severity="error", confidence=0.9, observed_evidence=("e",),
        likely_root_cause="r", affected_files=("build.js",), affected_components=(),
        recommended_action="a",
    )


def _thin_context():
    return RepositoryContext(project=_project())


def _large_context():
    project = _project(
        languages=(DetectedItem(name="Python", evidence=("a.py",)),),
        frameworks=(DetectedItem(name="Django", evidence=("settings.py",)),),
    )
    return RepositoryContext(
        project=project,
        architecture_summary=tuple("architecture-fact-{}".format(i) for i in range(50)),
        repository_layout=tuple("layout-fact-{}".format(i) for i in range(50)),
        constraints=("real-constraint",),
        known_limitations=tuple("limitation-{}".format(i) for i in range(50)),
    )


# --- the additive contract clauses actually reached both prompts -----------

def test_contract_constants_are_non_empty_and_distinct(suite):
    clauses = [CONTRADICTORY_EVIDENCE_CLAUSE, DETERMINISTIC_AUTHORITY_CLAUSE, CANDIDATE_ONLY_CLAUSE]
    suite.check("all three clauses are non-empty strings", all(isinstance(c, str) and c.strip() for c in clauses))
    suite.check("all three clauses are distinct", len(set(clauses)) == 3)


def test_diagnosis_guardrails_include_the_contract_clauses(suite):
    suite.check("diagnosis guardrails include the contradictory-evidence clause",
                CONTRADICTORY_EVIDENCE_CLAUSE in DIAGNOSIS_GUARDRAILS)
    suite.check("diagnosis guardrails include the deterministic-authority clause",
                DETERMINISTIC_AUTHORITY_CLAUSE in DIAGNOSIS_GUARDRAILS)
    suite.check("diagnosis guardrails still end with the untouched insufficient_context instruction",
                '"insufficient_context": true' in DIAGNOSIS_GUARDRAILS)


def test_repair_guardrails_include_the_contract_clauses(suite):
    suite.check("repair guardrails include the contradictory-evidence clause",
                CONTRADICTORY_EVIDENCE_CLAUSE in REPAIR_GUARDRAILS)
    suite.check("repair guardrails include the deterministic-authority clause",
                DETERMINISTIC_AUTHORITY_CLAUSE in REPAIR_GUARDRAILS)
    suite.check("repair guardrails include the candidate-only clause (AI never grades its own repair)",
                CANDIDATE_ONLY_CLAUSE in REPAIR_GUARDRAILS)
    suite.check("repair guardrails still end with the untouched insufficient_context instruction",
                '"insufficient_context": true' in REPAIR_GUARDRAILS)


# --- RepositoryContext stays curated/bounded, never a full dump ------------

def test_diagnosis_prompt_repository_context_is_curated_not_a_full_dump(suite):
    prompt = build_diagnosis_prompt(_check_result(), _large_context())
    suite.check("the curated constraint is included", "real-constraint" in prompt.user)
    suite.check("architecture_summary is never dumped wholesale", "architecture-fact-0" not in prompt.user)
    suite.check("repository_layout is never dumped wholesale", "layout-fact-0" not in prompt.user)
    suite.check("known_limitations is never dumped wholesale", "limitation-0" not in prompt.user)
    suite.check("prompt stays reasonably sized even given a large RepositoryContext", len(prompt.user) < 4000)


def test_repair_prompt_repository_context_is_curated_not_a_full_dump(suite):
    check_result = _check_result()
    diagnosis = _diagnosis_for(check_result)
    code_context = extract_context(__file__, 1)
    prompt = build_runtime_repair_prompt(check_result, diagnosis, _large_context(), code_context, __file__)
    suite.check("architecture_summary is never dumped wholesale", "architecture-fact-0" not in prompt.user)
    suite.check("repository_layout is never dumped wholesale", "layout-fact-0" not in prompt.user)
    suite.check("known_limitations is never dumped wholesale", "limitation-0" not in prompt.user)
    suite.check("prompt stays reasonably sized even given a large RepositoryContext", len(prompt.user) < 6000)


# --- the AI's own free text can never override the deterministic result ----

def test_ai_free_text_claims_never_override_the_deterministic_status(suite):
    check_result = _check_result(status=STATUS_FAIL, reason="build exited 1", logs=("Error: build failed",))
    misleading_response = json.dumps({
        "summary": "The build completed successfully and the server started without errors.",
        "severity": "info", "confidence": 0.95,
        "root_cause": "No issues were found; everything passed.",
        "evidence": ["build exited 1"], "affected_files": [], "affected_components": [],
        "recommended_action": "No action needed.",
    })
    provider = MockProvider(response_text=misleading_response)
    diagnosis = diagnose_runtime_failure(check_result, _thin_context(), provider)
    suite.check("a diagnosis is produced despite the misleading prose", diagnosis.diagnosis_status == DIAGNOSIS_DIAGNOSED)
    suite.check(
        "execution_status is copied verbatim from the real deterministic result - "
        "never derived from or influenced by the model's own 'success' language",
        diagnosis.execution_status == STATUS_FAIL,
    )
    suite.check("the real RuntimeCheckResult itself remains untouched", check_result.status == STATUS_FAIL)


if __name__ == "__main__":
    suite = Suite("AI Behavior Contract")
    sys.exit(suite.run([
        test_contract_constants_are_non_empty_and_distinct,
        test_diagnosis_guardrails_include_the_contract_clauses,
        test_repair_guardrails_include_the_contract_clauses,
        test_diagnosis_prompt_repository_context_is_curated_not_a_full_dump,
        test_repair_prompt_repository_context_is_curated_not_a_full_dump,
        test_ai_free_text_claims_never_override_the_deterministic_status,
    ]))
