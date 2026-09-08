"""Autonomous verified repair loop (Phase E Part 6): repair_loop.py.

Pure unit tests, no live LLM, no live analyzer subprocess - a `MockProvider`
configured with repair.py's own JSON schema drives Part 1, and a fake `run`
callable (matching test_ai_validator.py's own convention) drives Part 3's
"after" rerun. `apply_repair`/`apply_verified_repair` (Parts 2 and 5) need
real files on disk, so real `TempProject` fixtures are used throughout -
these tests exercise the real production pipeline end to end, only the AI
provider and the analyzer rerun are test doubles.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite, TempProject, compare_golden, normalise, run_agent  # noqa: E402

from qa_agent.adapters import Finding, ToolError  # noqa: E402
from qa_agent.ai import LLMResponse, MockProvider  # noqa: E402
from qa_agent.ai import repair_loop as repair_loop_module  # noqa: E402
from qa_agent.ai.apply import RepairApplicationResult  # noqa: E402
from qa_agent.ai.repair_loop import (  # noqa: E402
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
from qa_agent.ai.validator import STATUS_VALIDATION_FAILED as VALIDATOR_STATUS_VALIDATION_FAILED


class _RunResult:
    """A plain RunResult-shaped stand-in - the "before" baseline this loop
    reads `.findings` from."""

    def __init__(self, findings):
        self.findings = list(findings)
        self.tool_errors = []


class _AfterResult:
    """What the injected `run` callable returns for Part 3's "after" rerun."""

    def __init__(self, findings=(), tool_errors=()):
        self.findings = list(findings)
        self.tool_errors = list(tool_errors)


def _repair_response(replacement, confidence=0.9, line=1, explanation="why"):
    return json.dumps({
        "explanation": explanation, "replacement": replacement,
        "confidence": confidence, "start_line": line, "end_line": line,
    })


def _make_finding(path, line=1, tool="ruff", message="F401: unused"):
    return Finding(file=str(path), line=line, severity="error", message=message, tool=tool)


def _fake_run(after_findings=(), after_tool_errors=()):
    def run(inputs, config=None):
        return _AfterResult(findings=after_findings, tool_errors=after_tool_errors)
    return run


def _sequenced_run(*results):
    """A fake `run` returning a different result each call, in order -
    needed for a mixed multi-finding scenario where each finding's own
    validation rerun must see a different outcome.
    """
    calls = {"n": 0}

    def run(inputs, config=None):
        index = calls["n"]
        calls["n"] += 1
        return results[index] if index < len(results) else _AfterResult()

    return run


# --- single successful repair ------------------------------------------------


def test_single_successful_repair(suite):
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\nprint('hi')\n", encoding="utf-8", newline="")
        finding = _make_finding(target, line=1)
        run_result = _RunResult([finding])
        provider = MockProvider(response_text=_repair_response("# os removed", line=1))

        result = run_repair_loop(run_result, provider, run=_fake_run(after_findings=[]))

        suite.check("a RepairLoopResult is returned", isinstance(result, RepairLoopResult))
        suite.check("overall_success is True", result.overall_success)
        suite.check("exactly one outcome", len(result.outcomes) == 1)
        outcome = result.outcomes[0]
        suite.check("an outcome is returned", isinstance(outcome, RepairAttemptOutcome))
        suite.check("status is applied", outcome.status == STATUS_APPLIED)
        suite.check("the real file was actually written",
                     target.read_text(encoding="utf-8") == "# os removed\nprint('hi')\n")
        suite.check("statistics.successful is 1", result.statistics.successful == 1)
        suite.check("the file is listed in repaired_files", str(target) in result.repaired_files)
        suite.check("the file is not listed in failed_files", str(target) not in result.failed_files)


# --- multiple successful repairs --------------------------------------------


def test_multiple_successful_repairs(suite):
    with TempProject() as root:
        a = root / "a.py"
        b = root / "b.py"
        a.write_text("import os\nprint('a')\n", encoding="utf-8", newline="")
        b.write_text("import sys\nprint('b')\n", encoding="utf-8", newline="")
        finding_a = _make_finding(a, line=1, message="F401: os unused")
        finding_b = _make_finding(b, line=1, message="F401: sys unused")
        run_result = _RunResult([finding_a, finding_b])
        provider = MockProvider(response_text=_repair_response("# removed", line=1))

        result = run_repair_loop(run_result, provider, max_iterations=5,
                                  run=_fake_run(after_findings=[]))

        suite.check("both findings attempted", result.statistics.attempted == 2)
        suite.check("both repairs succeeded", result.statistics.successful == 2)
        suite.check("both files were written",
                     a.read_text(encoding="utf-8") == "# removed\nprint('a')\n"
                     and b.read_text(encoding="utf-8") == "# removed\nprint('b')\n")
        suite.check("both files listed in repaired_files",
                     str(a) in result.repaired_files and str(b) in result.repaired_files)


# --- repair rejected ----------------------------------------------------------


def test_repair_rejected(suite):
    with TempProject() as root:
        target = root / "a.py"
        original_bytes = b"import os\nprint('hi')\n"
        target.write_bytes(original_bytes)
        finding = _make_finding(target, line=1)
        run_result = _RunResult([finding])
        provider = MockProvider(response_text=_repair_response("import sys; os = 1", line=1))
        new_finding = _make_finding(target, line=1, message="E702: multiple statements")

        result = run_repair_loop(run_result, provider, run=_fake_run(after_findings=[new_finding]))

        outcome = result.outcomes[0]
        suite.check("status is rejected", outcome.status == STATUS_REJECTED)
        suite.check("the original file is untouched", target.read_bytes() == original_bytes)
        suite.check("statistics.rejected is 1", result.statistics.rejected == 1)
        suite.check("the file is in failed_files, not repaired_files",
                     str(target) in result.failed_files and str(target) not in result.repaired_files)


# --- held (unchanged) ----------------------------------------------------------


def test_repair_held_when_unchanged(suite):
    with TempProject() as root:
        target = root / "a.py"
        original_bytes = b"import os\nprint('hi')\n"
        target.write_bytes(original_bytes)
        finding = _make_finding(target, line=1)
        run_result = _RunResult([finding])
        # A no-op "fix" - the model's replacement is identical to the line
        # it targets, so the target finding survives unchanged.
        provider = MockProvider(response_text=_repair_response("import os", line=1))

        result = run_repair_loop(run_result, provider, run=_fake_run(after_findings=[finding]))

        outcome = result.outcomes[0]
        suite.check("status is held", outcome.status == STATUS_HELD)
        suite.check("the original file is untouched", target.read_bytes() == original_bytes)
        suite.check("statistics.held is 1", result.statistics.held == 1)


# --- validation failure -------------------------------------------------------


def test_validation_failure(suite):
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\nprint('hi')\n", encoding="utf-8", newline="")
        finding = _make_finding(target, line=1, tool="ruff")
        run_result = _RunResult([finding])
        provider = MockProvider(response_text=_repair_response("# removed", line=1))
        # The target's own tool fails during the rerun - Part 3's own
        # "cannot tell resolved from tool-never-ran" safety case.
        fake_run = _fake_run(after_findings=[], after_tool_errors=[("ruff", ToolError("boom"))])

        result = run_repair_loop(run_result, provider, run=fake_run)

        outcome = result.outcomes[0]
        suite.check("status is validation_failed", outcome.status == VALIDATOR_STATUS_VALIDATION_FAILED)
        suite.check("statistics.validation_failed is 1", result.statistics.validation_failed == 1)


def test_validation_exception_is_recorded_not_raised(suite):
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\nprint('hi')\n", encoding="utf-8", newline="")
        finding = _make_finding(target, line=1)
        run_result = _RunResult([finding])
        provider = MockProvider(response_text=_repair_response("# removed", line=1))

        def _broken_run(inputs, config=None):
            raise RuntimeError("analyzer subprocess exploded")

        result = run_repair_loop(run_result, provider, run=_broken_run)
        outcome = result.outcomes[0]
        suite.check("a broken analyzer rerun still yields validation_failed, not a crash",
                     outcome.status == VALIDATOR_STATUS_VALIDATION_FAILED)


# --- proposal failure / provider unavailable --------------------------------


def test_provider_unavailable_yields_no_proposal(suite):
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\nprint('hi')\n", encoding="utf-8", newline="")
        finding = _make_finding(target)
        run_result = _RunResult([finding])
        provider = MockProvider(fail=True, failure_message="connection refused - offline")

        result = run_repair_loop(run_result, provider)
        outcome = result.outcomes[0]
        suite.check("status is no_proposal when the provider is unavailable",
                     outcome.status == STATUS_NO_PROPOSAL)
        suite.check("statistics.failed counts it", result.statistics.failed == 1)


def test_malformed_provider_response_yields_no_proposal(suite):
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\nprint('hi')\n", encoding="utf-8", newline="")
        finding = _make_finding(target)
        run_result = _RunResult([finding])
        provider = MockProvider(response_text="not valid json at all")

        result = run_repair_loop(run_result, provider)
        outcome = result.outcomes[0]
        suite.check("status is no_proposal on a malformed response",
                     outcome.status == STATUS_NO_PROPOSAL)


def test_apply_repair_rejection_yields_workspace_failed(suite):
    """Part 2's own safety net (an invalid line range) firing inside the
    loop - a genuine, different failure mode than "no proposal".
    """
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\n", encoding="utf-8", newline="")
        finding = _make_finding(target, line=1)
        run_result = _RunResult([finding])
        provider = MockProvider(response_text=_repair_response("# removed", line=99))

        result = run_repair_loop(run_result, provider)
        outcome = result.outcomes[0]
        suite.check("status is workspace_failed for an out-of-range line",
                     outcome.status == STATUS_WORKSPACE_FAILED)


def test_apply_failure_yields_apply_failed_status(suite):
    """decide_repair() said accept_candidate, but the write itself (Part
    5) failed - a genuine, distinct failure mode, verified by controlling
    apply_verified_repair's own outcome directly (this suite's established
    monkey-patch technique, matching test_graceful_continuation... above).
    """
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\nprint('hi')\n", encoding="utf-8", newline="")
        finding = _make_finding(target, line=1)
        run_result = _RunResult([finding])
        provider = MockProvider(response_text=_repair_response("# removed", line=1))

        original_apply = repair_loop_module.apply_verified_repair

        def _always_fails(decision):
            return RepairApplicationResult(success=False, reason="simulated write failure",
                                            decision_used=decision)

        repair_loop_module.apply_verified_repair = _always_fails
        try:
            result = run_repair_loop(run_result, provider, run=_fake_run(after_findings=[]))
        finally:
            repair_loop_module.apply_verified_repair = original_apply

        outcome = result.outcomes[0]
        suite.check("status is apply_failed when the write itself fails",
                     outcome.status == STATUS_APPLY_FAILED)
        suite.check("statistics.failed counts an apply_failed outcome", result.statistics.failed == 1)
        suite.check("statistics.successful is 0", result.statistics.successful == 0)
        suite.check("the original file is untouched (the real write never actually happened)",
                     target.read_text(encoding="utf-8") == "import os\nprint('hi')\n")


# --- iteration limit -----------------------------------------------------------


def test_iteration_limit_reached(suite):
    with TempProject() as root:
        findings = []
        for i in range(5):
            f = root / "f{}.py".format(i)
            f.write_text("import os\n", encoding="utf-8", newline="")
            findings.append(_make_finding(f, line=1, message="F401: unused {}".format(i)))
        run_result = _RunResult(findings)
        provider = MockProvider(fail=True)  # every attempt just declines - simplest to bound

        result = run_repair_loop(run_result, provider, max_iterations=2)

        suite.check("only max_iterations findings were attempted", len(result.outcomes) == 2)
        suite.check("statistics.total_findings reports the true total", result.statistics.total_findings == 5)
        suite.check("statistics.attempted respects the limit", result.statistics.attempted == 2)
        suite.check("statistics.iterations_used respects the limit", result.statistics.iterations_used == 2)


def test_default_max_iterations_is_three(suite):
    suite.check("the documented default is 3", DEFAULT_MAX_ITERATIONS == 3)


def test_invalid_max_iterations_falls_back_to_default_not_infinite(suite):
    with TempProject() as root:
        findings = []
        for i in range(6):
            f = root / "f{}.py".format(i)
            f.write_text("import os\n", encoding="utf-8", newline="")
            findings.append(_make_finding(f, line=1, message="F401: unused {}".format(i)))
        run_result = _RunResult(findings)
        provider = MockProvider(fail=True)

        for bad_value in (-1, "three", None, True):
            result = run_repair_loop(run_result, provider, max_iterations=bad_value)
            suite.check("invalid max_iterations={!r} falls back to the safe default, never infinite"
                        .format(bad_value),
                        len(result.outcomes) == DEFAULT_MAX_ITERATIONS)


def test_max_iterations_zero_means_select_but_attempt_nothing(suite):
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\n", encoding="utf-8", newline="")
        finding = _make_finding(target)
        run_result = _RunResult([finding])
        provider = MockProvider(fail=True)

        result = run_repair_loop(run_result, provider, max_iterations=0)
        suite.check("total_findings still reports the real total", result.statistics.total_findings == 1)
        suite.check("nothing was attempted", result.statistics.attempted == 0)
        suite.check("no outcomes recorded", result.outcomes == ())


# --- mixed success/failure, statistics --------------------------------------


def test_mixed_outcomes_and_statistics(suite):
    with TempProject() as root:
        applied_file = root / "applied.py"
        rejected_file = root / "rejected.py"
        no_proposal_file = root / "no_proposal.py"
        for f in (applied_file, rejected_file, no_proposal_file):
            f.write_text("import os\nprint('x')\n", encoding="utf-8", newline="")

        finding_applied = _make_finding(applied_file, line=1, message="F401: applied")
        finding_rejected = _make_finding(rejected_file, line=1, message="F401: rejected")
        finding_no_proposal = _make_finding(no_proposal_file, line=1, message="F401: none")
        run_result = _RunResult([finding_applied, finding_rejected, finding_no_proposal])

        class _MixedProvider:
            name = "mixed"

            def __init__(self):
                self.calls = 0

            def generate(self, prompt):
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(text=_repair_response("# fixed", line=1), provider=self.name)
                if self.calls == 2:
                    return LLMResponse(text=_repair_response("import sys; os=1", line=1), provider=self.name)
                return LLMResponse(error="declined", provider=self.name)

            def test_connection(self):
                raise AssertionError("must never be called")

        worsening_finding = _make_finding(rejected_file, line=1, message="E702: bad")
        fake_run = _sequenced_run(
            _AfterResult(findings=[]),                      # applied_file's rerun: clean
            _AfterResult(findings=[worsening_finding]),      # rejected_file's rerun: worse
        )

        result = run_repair_loop(run_result, _MixedProvider(), max_iterations=3, run=fake_run)

        suite.check("three outcomes recorded", len(result.outcomes) == 3)
        suite.check("first finding applied", result.outcomes[0].status == STATUS_APPLIED)
        suite.check("second finding rejected", result.outcomes[1].status == STATUS_REJECTED)
        suite.check("third finding got no proposal", result.outcomes[2].status == STATUS_NO_PROPOSAL)

        stats = result.statistics
        suite.check("statistics is a real RepairLoopStatistics", isinstance(stats, RepairLoopStatistics))
        suite.check("total_findings is 3", stats.total_findings == 3)
        suite.check("attempted is 3", stats.attempted == 3)
        suite.check("successful is 1", stats.successful == 1)
        suite.check("rejected is 1", stats.rejected == 1)
        suite.check("held is 0", stats.held == 0)
        suite.check("validation_failed is 0", stats.validation_failed == 0)
        # "failed" = no_proposal + workspace_failed + apply_failed + error
        suite.check("failed counts the no_proposal outcome", stats.failed == 1)
        suite.check("iterations_used equals attempted (no retries in this design)",
                     stats.iterations_used == stats.attempted)
        suite.check("elapsed_seconds is a real, non-negative measurement", stats.elapsed_seconds >= 0)

        suite.check("repaired_files has exactly the applied file",
                     result.repaired_files == (str(applied_file),))
        suite.check("failed_files has the rejected and no-proposal files, not the applied one",
                     str(rejected_file) in result.failed_files
                     and str(no_proposal_file) in result.failed_files
                     and str(applied_file) not in result.failed_files)


# --- graceful continuation after an unexpected failure ----------------------


def test_graceful_continuation_after_an_unexpected_failure(suite):
    """The last line of defence this part itself adds, on top of Parts
    1-5's own already-exhaustive safety nets: even a completely unforeseen
    exception inside this module's own per-finding orchestration must not
    stop the next finding from being attempted.
    """
    with TempProject() as root:
        a = root / "a.py"
        b = root / "b.py"
        a.write_text("import os\n", encoding="utf-8", newline="")
        b.write_text("import sys\n", encoding="utf-8", newline="")
        finding_a = _make_finding(a, line=1, message="F401: a")
        finding_b = _make_finding(b, line=1, message="F401: b")
        run_result = _RunResult([finding_a, finding_b])
        provider = MockProvider(response_text=_repair_response("# fixed", line=1))

        original_decide = repair_loop_module.decide_repair
        calls = {"n": 0}

        def _flaky_decide(proposal, applied, validation):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("something nobody anticipated")
            return original_decide(proposal, applied, validation)

        repair_loop_module.decide_repair = _flaky_decide
        try:
            result = run_repair_loop(run_result, provider, run=_fake_run(after_findings=[]))
        finally:
            repair_loop_module.decide_repair = original_decide

        suite.check("two outcomes recorded despite the first one crashing",
                     len(result.outcomes) == 2)
        suite.check("the first finding is recorded as an error, not lost",
                     result.outcomes[0].status == STATUS_ERROR)
        suite.check("the underlying exception message is preserved",
                     "something nobody anticipated" in result.outcomes[0].detail)
        suite.check("the second finding was still processed normally",
                     result.outcomes[1].status == STATUS_APPLIED)
        suite.check("the second file really was written despite the first one's crash",
                     b.read_text(encoding="utf-8") == "# fixed\n")
        suite.check("overall_success is still True - the loop itself completed",
                     result.overall_success)


# --- overall orchestration result -------------------------------------------


def test_overall_success_true_even_with_zero_repairs_applied(suite):
    with TempProject() as root:
        target = root / "a.py"
        target.write_text("import os\n", encoding="utf-8", newline="")
        finding = _make_finding(target)
        run_result = _RunResult([finding])
        provider = MockProvider(fail=True)

        result = run_repair_loop(run_result, provider)
        suite.check("overall_success is True - the process ran correctly, even with 0 successes",
                     result.overall_success)
        suite.check("but statistics.successful is honestly 0", result.statistics.successful == 0)


def test_overall_success_false_on_invalid_run_result(suite):
    result = run_repair_loop(None, MockProvider())
    suite.check("overall_success is False when no usable RunResult was given",
                not result.overall_success)
    suite.check("no exception, a structured empty result instead", result.outcomes == ())
    suite.check("statistics are all zero", result.statistics.total_findings == 0
                and result.statistics.attempted == 0)


def test_empty_findings_list_is_a_clean_no_op(suite):
    run_result = _RunResult([])
    result = run_repair_loop(run_result, MockProvider())
    suite.check("overall_success is True for a clean, empty run", result.overall_success)
    suite.check("nothing attempted", result.statistics.attempted == 0)


def test_run_repair_loop_never_raises_on_a_broken_run_result(suite):
    class _Explosive:
        @property
        def findings(self):
            raise RuntimeError("something nobody anticipated")

    result = run_repair_loop(_Explosive(), MockProvider())
    suite.check("a broken run_result still yields a structured result, not a crash",
                isinstance(result, RepairLoopResult) and not result.overall_success)


# --- isolation and no-behavior-change-when-unused ---------------------------


def test_repair_loop_module_never_imports_provider_or_prompt_infrastructure(suite):
    source = (REPO_ROOT / "qa_agent" / "ai" / "repair_loop.py").read_text(encoding="utf-8")
    forbidden_imports = [
        "from .provider", "from .ollama", "from .mock", "from .prompts",
        "from .schemas", "from .response_parser",
    ]
    found = [token for token in forbidden_imports if token in source]
    suite.check("no provider/prompt/schema imports appear in repair_loop.py's real source",
                not found, "  [{}]".format(found))


def test_repair_loop_is_not_invoked_by_the_normal_analyze_path(suite):
    pipeline_modules = [
        "runner.py", "adapters.py", "report.py", "config.py",
        "analysis_bridge.py", "watch.py", "debouncer.py",
        "fsmonitor.py", "live_report.py", "gitdiff.py", "__main__.py",
    ]
    offenders = []
    for name in pipeline_modules:
        text = (REPO_ROOT / "qa_agent" / name).read_text(encoding="utf-8")
        if "run_repair_loop" in text or "RepairLoopResult" in text:
            offenders.append(name)
    suite.check("no pipeline module references run_repair_loop/RepairLoopResult",
                not offenders, "  [{}]".format(offenders))


def test_phase_c_golden_output_unaffected(suite):
    """The real, end-to-end proof: running the actual CLI - which never
    calls run_repair_loop() at all, this part having no CLI integration -
    must still match every existing golden file exactly.
    """
    with TempProject() as root:
        target = root / "has_issues.py"
        target.write_text(
            "import json\n\n\ndef f(x):\n    if x == None:\n        return 1\n"
            "    y = 2\n    return x\n",
            encoding="utf-8",
        )
        proc = run_agent([str(target)])
        actual = normalise(proc.stdout, root)
    compare_golden(suite, "cli_findings.txt", actual)


if __name__ == "__main__":
    suite = Suite("Autonomous verified repair loop (Phase E Part 6)")
    sys.exit(suite.run([
        test_single_successful_repair,
        test_multiple_successful_repairs,
        test_repair_rejected,
        test_repair_held_when_unchanged,
        test_validation_failure,
        test_validation_exception_is_recorded_not_raised,
        test_provider_unavailable_yields_no_proposal,
        test_malformed_provider_response_yields_no_proposal,
        test_apply_repair_rejection_yields_workspace_failed,
        test_apply_failure_yields_apply_failed_status,
        test_iteration_limit_reached,
        test_default_max_iterations_is_three,
        test_invalid_max_iterations_falls_back_to_default_not_infinite,
        test_max_iterations_zero_means_select_but_attempt_nothing,
        test_mixed_outcomes_and_statistics,
        test_graceful_continuation_after_an_unexpected_failure,
        test_overall_success_true_even_with_zero_repairs_applied,
        test_overall_success_false_on_invalid_run_result,
        test_empty_findings_list_is_a_clean_no_op,
        test_run_repair_loop_never_raises_on_a_broken_run_result,
        test_repair_loop_module_never_imports_provider_or_prompt_infrastructure,
        test_repair_loop_is_not_invoked_by_the_normal_analyze_path,
        test_phase_c_golden_output_unaffected,
    ]))
