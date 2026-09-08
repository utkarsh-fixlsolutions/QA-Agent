"""Repair decision engine (Phase E Part 4): decision.py.

Pure unit tests, no network, no live LLM, no analyzer subprocess -
`decide_repair()` takes no provider and reruns nothing, so there is
nothing here to mock beyond plain data. Real `RepairProposal`,
`AppliedRepair`, `RepairValidationResult`, and `ValidationComparison`
instances are used throughout so these tests exercise the real production
types, alongside a handful of plain duck-typed doubles specifically to
prove the "missing/partial fields fail closed" requirement does not depend
on the real dataclasses.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite  # noqa: E402

from qa_agent.adapters import Finding  # noqa: E402
from qa_agent.ai.repair import RepairProposal  # noqa: E402
from qa_agent.ai.workspace import AppliedRepair  # noqa: E402
from qa_agent.ai.validator import (  # noqa: E402
    STATUS_IMPROVED,
    STATUS_UNCHANGED,
    STATUS_WORSENED,
    RepairValidationResult,
    ValidationComparison,
)
from qa_agent.ai.decision import (  # noqa: E402
    ACTION_ACCEPT_CANDIDATE,
    ACTION_HOLD,
    ACTION_REJECT,
    ACTION_VALIDATION_FAILED,
    RepairDecision,
    decide_repair,
)


def _finding(file="a.py", line=1, severity="error", message="F401: unused", tool="ruff"):
    return Finding(file=file, line=line, severity=severity, message=message, tool=tool)


def _proposal(finding):
    return RepairProposal(
        finding=finding, explanation="why", replacement="fixed", confidence=0.9,
        model="mock", file=finding.file, start_line=finding.line, end_line=finding.line,
    )


def _applied(proposal):
    return AppliedRepair(ok=True, proposal=proposal, workspace_file=Path("workspace_copy.py"))


def _comparison(before_count, after_count, removed_count, introduced_count,
                 target_resolved=None, unchanged_count=0):
    return ValidationComparison(
        before_count=before_count, after_count=after_count,
        removed_count=removed_count, introduced_count=introduced_count,
        unchanged_count=unchanged_count, target_resolved=target_resolved,
    )


def _result(status, comparison=None, error=None):
    return RepairValidationResult(status=status, comparison=comparison, error=error)


# --- accept_candidate --------------------------------------------------------


def test_accept_candidate_improved_target_resolved_no_new_findings(suite):
    finding = _finding()
    proposal = _proposal(finding)
    applied = _applied(proposal)
    comparison = _comparison(before_count=1, after_count=0, removed_count=1,
                              introduced_count=0, target_resolved=True)
    validation = _result(STATUS_IMPROVED, comparison=comparison)

    decision = decide_repair(proposal, applied, validation)
    suite.check("a RepairDecision is returned", isinstance(decision, RepairDecision))
    suite.check("action is accept_candidate", decision.action == ACTION_ACCEPT_CANDIDATE)
    suite.check("a reason is given", bool(decision.reason))
    suite.check("counts are carried through", decision.before_count == 1 and decision.after_count == 0
                and decision.removed_count == 1 and decision.introduced_count == 0)
    suite.check("proposal reference is carried through", decision.proposal is proposal)
    suite.check("applied_repair reference is carried through", decision.applied_repair is applied)
    suite.check("validation_result reference is carried through", decision.validation_result is validation)


def test_accept_candidate_reason_mentions_removed_count(suite):
    comparison = _comparison(before_count=2, after_count=1, removed_count=1,
                              introduced_count=0, target_resolved=True)
    decision = decide_repair(None, None, _result(STATUS_IMPROVED, comparison=comparison))
    suite.check("action is accept_candidate", decision.action == ACTION_ACCEPT_CANDIDATE)
    suite.check("the reason names the removed count", "1" in decision.reason)


# --- reject: worsened ---------------------------------------------------------


def test_reject_when_worsened(suite):
    comparison = _comparison(before_count=1, after_count=2, removed_count=0, introduced_count=1)
    decision = decide_repair(None, None, _result(STATUS_WORSENED, comparison=comparison))
    suite.check("action is reject", decision.action == ACTION_REJECT)
    suite.check("the reason mentions the introduced finding", "1" in decision.reason)


def test_reject_when_after_count_exceeds_before_count_even_if_status_disagrees(suite):
    """The literal 'OR after_count > before_count' clause: trusted
    independently of the status label, a deliberate defensive check.
    """
    comparison = _comparison(before_count=1, after_count=3, removed_count=0, introduced_count=2)
    # A status that would normally not reach the worsened branch, paired
    # with counts that clearly show things got worse anyway.
    decision = decide_repair(None, None, _result(STATUS_UNCHANGED, comparison=comparison))
    suite.check("still rejected on the counts alone, regardless of the status label",
                decision.action == ACTION_REJECT)


# --- reject: improved but introduced new findings ---------------------------


def test_reject_when_improved_but_new_findings_introduced(suite):
    comparison = _comparison(before_count=2, after_count=2, removed_count=1,
                              introduced_count=1, target_resolved=True)
    decision = decide_repair(None, None, _result(STATUS_IMPROVED, comparison=comparison))
    suite.check("action is reject, not accept_candidate", decision.action == ACTION_REJECT)
    suite.check("the reason explains why", "new finding" in decision.reason)


# --- hold: unchanged -----------------------------------------------------------


def test_hold_when_unchanged(suite):
    comparison = _comparison(before_count=1, after_count=1, removed_count=0,
                              introduced_count=0, unchanged_count=1)
    decision = decide_repair(None, None, _result(STATUS_UNCHANGED, comparison=comparison))
    suite.check("action is hold", decision.action == ACTION_HOLD)


# --- hold: improved but target resolution unconfirmed -----------------------


def test_hold_when_improved_but_target_not_resolved(suite):
    """target_resolved is explicitly False - something else improved, but
    not the finding this repair was actually meant to fix.
    """
    comparison = _comparison(before_count=2, after_count=1, removed_count=1,
                              introduced_count=0, target_resolved=False)
    decision = decide_repair(None, None, _result(STATUS_IMPROVED, comparison=comparison))
    suite.check("action is hold, not accept_candidate", decision.action == ACTION_HOLD)


def test_hold_when_improved_but_target_resolved_is_none(suite):
    """target_resolved is None (no target finding was known) - unconfirmed
    is not the same as confirmed-resolved, so this must not silently
    accept.
    """
    comparison = _comparison(before_count=2, after_count=1, removed_count=1,
                              introduced_count=0, target_resolved=None)
    decision = decide_repair(None, None, _result(STATUS_IMPROVED, comparison=comparison))
    suite.check("action is hold when target resolution is unknown", decision.action == ACTION_HOLD)


# --- validation_failed --------------------------------------------------------


def test_validation_failed_when_status_is_validation_failed(suite):
    decision = decide_repair(None, None, _result("validation_failed", error="workspace file missing"))
    suite.check("action is validation_failed", decision.action == ACTION_VALIDATION_FAILED)
    suite.check("the underlying error is surfaced", "workspace file missing" in decision.reason)
    suite.check("no counts are fabricated", decision.before_count is None
                and decision.after_count is None)


def test_validation_failed_when_validation_result_is_none(suite):
    decision = decide_repair(None, None, None)
    suite.check("action is validation_failed on a None ValidationResult",
                decision.action == ACTION_VALIDATION_FAILED)
    suite.check("a clear reason is given", bool(decision.reason))


def test_validation_failed_when_status_is_unrecognized(suite):
    decision = decide_repair(None, None, _result("something_else"))
    suite.check("an unrecognized status fails closed to validation_failed",
                decision.action == ACTION_VALIDATION_FAILED)


def test_validation_failed_when_comparison_is_missing_despite_known_status(suite):
    """A malformed ValidationResult: a recognized status string but no
    comparison object at all - nothing to derive counts from.
    """
    decision = decide_repair(None, None, _result(STATUS_IMPROVED, comparison=None))
    suite.check("no comparison means validation_failed, not a guess",
                decision.action == ACTION_VALIDATION_FAILED)


# --- missing/partial fields fail closed (but not to validation_failed) -----


def test_hold_when_comparison_is_missing_a_count_field(suite):
    """A recognized status AND a real comparison object, but one required
    count is itself missing/wrong-typed - validation genuinely completed,
    so this fails closed to hold, not validation_failed (documented choice).
    """
    class _PartialComparison:
        before_count = 3
        after_count = None  # missing
        removed_count = 1
        introduced_count = 0
        target_resolved = True

    decision = decide_repair(None, None, _result(STATUS_IMPROVED, comparison=_PartialComparison()))
    suite.check("missing after_count fails closed to hold, not validation_failed",
                decision.action == ACTION_HOLD)


def test_hold_when_a_count_is_a_boolean_not_a_real_int(suite):
    """bool is an int subclass in Python - the same trap already guarded
    against in config.py, response_parser.py, and workspace.py.
    """
    class _BoolComparison:
        before_count = 1
        after_count = True  # bool, not a real count
        removed_count = 1
        introduced_count = 0
        target_resolved = True

    decision = decide_repair(None, None, _result(STATUS_IMPROVED, comparison=_BoolComparison()))
    suite.check("a boolean count fails closed to hold, not accepted as a real int",
                decision.action == ACTION_HOLD)


def test_decision_never_raises_on_an_attribute_that_raises(suite):
    """The last line of defence: even a ValidationResult whose own
    attribute access raises must not crash decide_repair.
    """
    class _Explosive:
        @property
        def status(self):
            raise RuntimeError("something nobody anticipated")

    decision = decide_repair(None, None, _Explosive())
    suite.check("an exception inside attribute access still yields validation_failed, not a crash",
                decision.action == ACTION_VALIDATION_FAILED)


# --- determinism, provider independence, and isolation ----------------------


def test_decision_is_deterministic(suite):
    comparison = _comparison(before_count=1, after_count=0, removed_count=1,
                              introduced_count=0, target_resolved=True)
    validation = _result(STATUS_IMPROVED, comparison=comparison)
    first = decide_repair(None, None, validation)
    second = decide_repair(None, None, validation)
    suite.check("identical action across repeated calls with identical input",
                first.action == second.action)
    suite.check("identical reason across repeated calls with identical input",
                first.reason == second.reason)


def test_decision_matrix_is_exhaustive_across_every_combination(suite):
    """Every (status, target_resolved, introduced_count) combination this
    policy is documented to handle produces exactly the documented action -
    a single table-driven check of the whole matrix at once.
    """
    cases = [
        # (status, before, after, removed, introduced, target_resolved) -> expected action
        (STATUS_WORSENED, 1, 2, 0, 1, None, ACTION_REJECT),
        (STATUS_UNCHANGED, 1, 1, 0, 0, None, ACTION_HOLD),
        (STATUS_IMPROVED, 2, 1, 1, 0, True, ACTION_ACCEPT_CANDIDATE),
        (STATUS_IMPROVED, 2, 2, 1, 1, True, ACTION_REJECT),
        (STATUS_IMPROVED, 2, 1, 1, 0, False, ACTION_HOLD),
        (STATUS_IMPROVED, 2, 1, 1, 0, None, ACTION_HOLD),
    ]
    all_ok = True
    for status, before, after, removed, introduced, target_resolved, expected in cases:
        comparison = _comparison(before, after, removed, introduced, target_resolved=target_resolved)
        decision = decide_repair(None, None, _result(status, comparison=comparison))
        if decision.action != expected:
            all_ok = False
            print("    mismatch:", status, target_resolved, introduced, "->", decision.action,
                  "expected", expected)
    suite.check("every documented matrix combination produces its documented action", all_ok)


def test_decision_module_never_imports_ai_generation_infrastructure(suite):
    """Checked directly against the real source: decision.py has no reason
    to import a provider, prompt builder, or schema module - it never
    calls the AI, matching this part's own hard rule. Checks for actual
    import statements, the same false-positive-avoiding style already used
    in test_ai_workspace.py/test_ai_repair.py (a docstring merely
    *mentioning* a concept must not trip this).
    """
    source = (REPO_ROOT / "qa_agent" / "ai" / "decision.py").read_text(encoding="utf-8")
    forbidden_imports = [
        "from .provider", "from .ollama", "from .mock", "from .prompts",
        "from .schemas", "from .response_parser", "from .context",
        "import provider", "import ollama", "import mock", "import prompts",
        "import schemas", "import response_parser",
    ]
    found = [token for token in forbidden_imports if token in source]
    suite.check("no AI-generation-infrastructure imports appear in decision.py's real source",
                not found, "  [{}]".format(found))


def test_decision_module_never_reruns_analyzers_or_writes(suite):
    source = (REPO_ROOT / "qa_agent" / "ai" / "decision.py").read_text(encoding="utf-8")
    forbidden = ["from ..runner", "from .runner", "write_text", "write_bytes",
                 "import subprocess", "from .workspace"]
    found = [token for token in forbidden if token in source]
    suite.check("no rerun/write tokens appear in decision.py's real source",
                not found, "  [{}]".format(found))


def test_decision_engine_is_not_invoked_by_the_normal_analyze_path(suite):
    """No CLI integration in this part: nothing in the deterministic
    pipeline (including __main__.py, which already legitimately imports
    qa_agent.ai as of Phase D Part 6) references decide_repair.
    """
    pipeline_modules = [
        "runner.py", "adapters.py", "report.py", "config.py",
        "analysis_bridge.py", "watch.py", "debouncer.py",
        "fsmonitor.py", "live_report.py", "gitdiff.py", "__main__.py",
    ]
    offenders = []
    for name in pipeline_modules:
        text = (REPO_ROOT / "qa_agent" / name).read_text(encoding="utf-8")
        if "decide_repair" in text or "RepairDecision" in text:
            offenders.append(name)
    suite.check("no pipeline module references decide_repair/RepairDecision",
                not offenders, "  [{}]".format(offenders))


if __name__ == "__main__":
    suite = Suite("Repair decision engine (Phase E Part 4)")
    sys.exit(suite.run([
        test_accept_candidate_improved_target_resolved_no_new_findings,
        test_accept_candidate_reason_mentions_removed_count,
        test_reject_when_worsened,
        test_reject_when_after_count_exceeds_before_count_even_if_status_disagrees,
        test_reject_when_improved_but_new_findings_introduced,
        test_hold_when_unchanged,
        test_hold_when_improved_but_target_not_resolved,
        test_hold_when_improved_but_target_resolved_is_none,
        test_validation_failed_when_status_is_validation_failed,
        test_validation_failed_when_validation_result_is_none,
        test_validation_failed_when_status_is_unrecognized,
        test_validation_failed_when_comparison_is_missing_despite_known_status,
        test_hold_when_comparison_is_missing_a_count_field,
        test_hold_when_a_count_is_a_boolean_not_a_real_int,
        test_decision_never_raises_on_an_attribute_that_raises,
        test_decision_is_deterministic,
        test_decision_matrix_is_exhaustive_across_every_combination,
        test_decision_module_never_imports_ai_generation_infrastructure,
        test_decision_module_never_reruns_analyzers_or_writes,
        test_decision_engine_is_not_invoked_by_the_normal_analyze_path,
    ]))
